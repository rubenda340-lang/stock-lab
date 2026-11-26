"""
Tests de Conexión SQLite Robusta (WAL + busy_timeout) y Backup Consciente de WAL — StockLab
==========================================================================================
Verifica los requerimientos de robustez institucional del Paso C-24b:
  (i)   get_connection() deja journal_mode=wal, busy_timeout=30000 y synchronous=2 (FULL).
  (ii)  Concurrencia: 4 procesos concurrentes insertando 200 filas c/u vía get_connection(),
        junto a lectores simultáneos: 0 errores "database is locked" y recuento final 800.
  (iii) Copia con backup_db.py durante escrituras pasa integrity_check, queda en DELETE y no deja -wal/-shm.
  (iv)  --verify abre la copia en solo lectura inmutable sin crear archivos -wal/-shm.
  (v)   BD inexistente -> código de salida 3 en menos de 3 s usando --retry-seconds 1.
  (vi)  El hash lógico de una tabla antes y después de convertir a WAL permanece idéntico.
"""

import hashlib
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import database.connection
from tools import backup_db

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _worker_writer(db_path, worker_id, rows_to_insert, start_event, error_queue):
    """Función de nivel superior compatible con multiprocessing (spawn en Windows)."""
    database.connection.DB_PATH = db_path
    try:
        start_event.wait(timeout=10.0)
        conn = database.connection.get_connection()
        cur = conn.cursor()
        for i in range(rows_to_insert):
            cur.execute(
                "INSERT INTO concurrencia_test (worker_id, row_idx, payload) VALUES (?, ?, ?);",
                (worker_id, i, f"data_w{worker_id}_r{i}"),
            )
            conn.commit()
            time.sleep(0.001)
        conn.close()
    except Exception as e:
        error_queue.put((worker_id, str(e)))


def _worker_reader(db_path, start_event, stop_event, error_queue):
    """Función de nivel superior para lecturas concurrentes."""
    database.connection.DB_PATH = db_path
    try:
        start_event.wait(timeout=10.0)
        while not stop_event.is_set():
            conn = database.connection.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM concurrencia_test;")
            cur.fetchone()
            conn.close()
            time.sleep(0.005)
    except Exception as e:
        error_queue.put((-1, str(e)))


class TestConexionWAL(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_wal_")
        self.test_db = os.path.join(self.tmp_dir, "test_stocklab.db")
        self.dest_dir = os.path.join(self.tmp_dir, "backups")
        os.makedirs(self.dest_dir, exist_ok=True)

        self.orig_db_path = database.connection.DB_PATH
        database.connection.DB_PATH = self.test_db

    def tearDown(self):
        database.connection.DB_PATH = self.orig_db_path
        # Reintentar limpieza para Windows por locks de archivos
        for _ in range(5):
            try:
                shutil.rmtree(self.tmp_dir, ignore_errors=True)
                break
            except Exception:
                time.sleep(0.1)

    def test_01_get_connection_pragmas(self):
        """(i) get_connection deja journal_mode=wal, busy_timeout=30000 y synchronous=2 (FULL)."""
        conn = database.connection.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode;")
            jm = cur.fetchone()[0]
            self.assertEqual(str(jm).lower(), "wal")

            cur.execute("PRAGMA busy_timeout;")
            bt = cur.fetchone()[0]
            self.assertEqual(int(bt), 30000)

            cur.execute("PRAGMA synchronous;")
            sync = cur.fetchone()[0]
            self.assertEqual(int(sync), 2)
        finally:
            conn.close()

    def test_02_concurrencia_cuatro_procesos_sin_locked(self):
        """
        (ii) Concurrencia: 4 procesos insertando 200 filas c/u vía get_connection(),
        y a la vez lectores consultando: 0 errores 'database is locked' y recuento final 800.
        """
        # Inicializar tabla de pruebas
        conn = database.connection.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concurrencia_test (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER,
                row_idx INTEGER,
                payload TEXT
            );
        """)
        conn.commit()
        conn.close()

        manager = multiprocessing.Manager()
        error_queue = manager.Queue()
        start_event = multiprocessing.Event()
        stop_event = multiprocessing.Event()

        # Iniciar lector
        reader_proc = multiprocessing.Process(
            target=_worker_reader,
            args=(self.test_db, start_event, stop_event, error_queue),
        )
        reader_proc.start()

        # Iniciar 4 escritores de 200 filas cada uno
        num_writers = 4
        rows_per_writer = 200
        writers = []
        for wid in range(num_writers):
            p = multiprocessing.Process(
                target=_worker_writer,
                args=(self.test_db, wid, rows_per_writer, start_event, error_queue),
            )
            writers.append(p)
            p.start()

        # Dar señal de inicio concurrente
        start_event.set()

        for p in writers:
            p.join(timeout=30.0)
            self.assertFalse(p.is_alive(), "Un proceso escritor no finalizo a tiempo")

        stop_event.set()
        reader_proc.join(timeout=10.0)

        # Comprobar que no hubo errores en ningún proceso
        errors = []
        while not error_queue.empty():
            errors.append(error_queue.get())
        self.assertEqual(errors, [], f"Errores de concurrencia detectados: {errors}")

        # Comprobar recuento total exacto (4 * 200 = 800)
        conn = database.connection.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM concurrencia_test;")
        total_rows = cur.fetchone()[0]
        conn.close()
        self.assertEqual(total_rows, num_writers * rows_per_writer)

    def test_03_copia_backup_durante_escritura_queda_delete_sin_wal(self):
        """
        (iii) Copia con backup_db.py mientras un escritor inserta pasa integrity_check,
        queda en journal_mode=DELETE y no deja ficheros -wal/-shm en destino.
        """
        conn = database.connection.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS stress_test (id INTEGER PRIMARY KEY, valor TEXT);")
        conn.commit()
        conn.close()

        # Hilo/proceso escritor en background
        stop_writer = False

        import threading

        def bg_writer():
            w_conn = database.connection.get_connection()
            idx = 0
            while not stop_writer:
                try:
                    w_conn.execute("INSERT INTO stress_test (valor) VALUES (?);", (f"v_{idx}",))
                    w_conn.commit()
                    idx += 1
                    time.sleep(0.005)
                except Exception:
                    break
            w_conn.close()

        t = threading.Thread(target=bg_writer)
        t.start()
        time.sleep(0.05)

        try:
            success, code, manifest = backup_db.create_backup(self.test_db, self.dest_dir)
            self.assertTrue(success)
            self.assertEqual(code, 0)
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest.get("copia_journal_mode"), "delete")
        finally:
            stop_writer = True
            t.join()

        # Verificar que la copia generada está en modo DELETE y no tiene -wal/-shm
        backups = [f for f in os.listdir(self.dest_dir) if f.endswith(".db")]
        self.assertEqual(len(backups), 1)
        b_file = os.path.join(self.dest_dir, backups[0])

        b_conn = sqlite3.connect(b_file)
        cur = b_conn.cursor()
        cur.execute("PRAGMA journal_mode;")
        jm = cur.fetchone()[0]
        cur.execute("PRAGMA integrity_check;")
        int_chk = cur.fetchall()
        b_conn.close()

        self.assertEqual(str(jm).lower(), "delete")
        self.assertEqual(int_chk, [("ok",)])

        self.assertFalse(os.path.exists(b_file + "-wal"))
        self.assertFalse(os.path.exists(b_file + "-shm"))

    def test_04_verify_no_crea_ficheros_wal_shm(self):
        """(iv) --verify abre la copia en solo lectura sin crear ficheros -wal/-shm."""
        # Crear copia de seguridad
        conn = database.connection.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_tbl (id INT);")
        conn.commit()
        conn.close()

        success, code, manifest = backup_db.create_backup(self.test_db, self.dest_dir)
        self.assertTrue(success)
        self.assertEqual(code, 0)

        backups = [f for f in os.listdir(self.dest_dir) if f.endswith(".db")]
        b_file = os.path.join(self.dest_dir, backups[0])

        # Asegurar que no hay -wal ni -shm
        wal_path = b_file + "-wal"
        shm_path = b_file + "-shm"
        self.assertFalse(os.path.exists(wal_path))
        self.assertFalse(os.path.exists(shm_path))

        # Ejecutar verify_backup
        v_ok, v_code = backup_db.verify_backup(b_file)
        self.assertTrue(v_ok)
        self.assertEqual(v_code, 0)

        # Comprobar que verify NO creó -wal ni -shm
        self.assertFalse(os.path.exists(wal_path))
        self.assertFalse(os.path.exists(shm_path))

    def test_05_bd_inexistente_menos_de_tres_segundos(self):
        """(v) BD inexistente -> código 3 en menos de 3 s con --retry-seconds 1."""
        fake_db = os.path.join(self.tmp_dir, "no_existe_absolutamente_wal.db")
        fake_dest = os.path.join(self.tmp_dir, "dest_inexistente")

        t0 = time.time()
        success, code, manifest = backup_db.create_backup(fake_db, fake_dest, retry_seconds=1.0)
        elapsed = time.time() - t0

        self.assertFalse(success)
        self.assertEqual(code, 3)
        self.assertIsNone(manifest)
        self.assertLess(elapsed, 3.0, f"Tardó {elapsed:.2f} s, esperado < 3 s")

        # Probar también vía subprocess CLI
        script_path = os.path.join(REPO_ROOT, "tools", "backup_db.py")
        cmd = [
            sys.executable,
            "-B",
            script_path,
            "--db",
            fake_db,
            "--dest",
            fake_dest,
            "--retry-seconds",
            "1.0",
        ]
        t0 = time.time()
        res = subprocess.run(cmd, capture_output=True, text=True)
        elapsed_sub = time.time() - t0
        self.assertEqual(res.returncode, 3)
        self.assertLess(elapsed_sub, 3.0, f"Subprocess tardó {elapsed_sub:.2f} s, esperado < 3 s")

    def test_06_hash_logico_identico_al_convertir_a_wal(self):
        """(vi) El hash lógico de una tabla sintética antes y después de convertirla a WAL es idéntico."""
        # 1. Crear base en journal_mode=DELETE
        c = sqlite3.connect(self.test_db)
        c.execute("PRAGMA journal_mode = DELETE;")
        c.execute("""
            CREATE TABLE predicciones_test (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                fecha TEXT,
                precio REAL
            );
        """)
        for i in range(1, 101):
            c.execute(
                "INSERT INTO predicciones_test (id, ticker, fecha, precio) VALUES (?, ?, ?, ?);",
                (i, f"TICK{i}.MC", f"2026-10-{(i % 28) + 1:02d}", 10.5 + i),
            )
        c.commit()

        # Calcular hash lógico antes de WAL
        cur = c.cursor()
        cur.execute("SELECT id, ticker, fecha, precio FROM predicciones_test ORDER BY id ASC;")
        rows_before = cur.fetchall()
        hasher_before = hashlib.sha256()
        for r in rows_before:
            hasher_before.update(("|".join(str(v) for v in r) + "\n").encode("utf-8"))
        hash_before = hasher_before.hexdigest()
        c.close()

        # 2. Abrir con get_connection (convierte a WAL), ejecutar checkpoint y cerrar
        conn_wal = database.connection.get_connection()
        cur_wal = conn_wal.cursor()
        cur_wal.execute("PRAGMA journal_mode;")
        self.assertEqual(str(cur_wal.fetchone()[0]).lower(), "wal")
        cur_wal.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        cur_wal.fetchall()
        conn_wal.close()

        # 3. Re-abrir y calcular hash lógico después de WAL
        conn_after = database.connection.get_connection()
        cur_after = conn_after.cursor()
        cur_after.execute("SELECT id, ticker, fecha, precio FROM predicciones_test ORDER BY id ASC;")
        rows_after = cur_after.fetchall()
        hasher_after = hashlib.sha256()
        for r in rows_after:
            hasher_after.update(("|".join(str(v) for v in r) + "\n").encode("utf-8"))
        hash_after = hasher_after.hexdigest()
        conn_after.close()

        self.assertEqual(len(rows_before), 100)
        self.assertEqual(len(rows_after), 100)
        self.assertEqual(hash_before, hash_after)


if __name__ == "__main__":
    unittest.main()
