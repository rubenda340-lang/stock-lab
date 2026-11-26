"""
Tests para tools/backup_db.py — Paso C-24
=========================================
Pruebas exhaustivas sobre bases de datos SQLite temporales y sinteticas:
  (i)   Copia correcta con manifest y --verify OK
  (ii)  Copia mientras otra conexion escribe en el origen: integrity_check OK
  (iii) Alterar un byte de la copia hace que --verify devuelva 2
  (iv)  Rotacion: 20 copias automaticas con fechas simuladas + ficheros ajenos
  (v)   BD inexistente -> codigo 3 sin crear archivos
  (vi)  El origen no cambia (SHA-256 antes y despues sin escritores)
  (vii) Ejecucion con subprocess y PYTHONIOENCODING=cp1252 devuelve 0
  (viii) Ruta de destino con espacios
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

# Asegurar importacion de tools.backup_db
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from tools import backup_db


def _crear_bd_sintetica(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE empresas (ticker TEXT PRIMARY KEY, nombre TEXT);")
    cur.execute("CREATE TABLE predicciones (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, precio REAL);")
    cur.execute("INSERT INTO empresas VALUES ('SAN.MC', 'Banco Santander'), ('BBVA.MC', 'BBVA');")
    cur.execute("INSERT INTO predicciones (ticker, precio) VALUES ('SAN.MC', 4.50), ('BBVA.MC', 9.20);")
    conn.commit()
    conn.close()


class TestBackupDB(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_backup_db_")
        self.src_db = os.path.join(self.tmp_dir, "test_stocklab.db")
        _crear_bd_sintetica(self.src_db)
        self.dest_dir = os.path.join(self.tmp_dir, "backups")
        os.makedirs(self.dest_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_copia_correcta_con_manifest_y_verify_ok(self):
        """(i) Copia correcta con manifest y --verify OK."""
        success, code, manifest = backup_db.create_backup(self.src_db, self.dest_dir)
        self.assertTrue(success)
        self.assertEqual(code, 0)
        self.assertIsNotNone(manifest)

        backups = [f for f in os.listdir(self.dest_dir) if f.endswith(".db")]
        self.assertEqual(len(backups), 1)
        backup_file = os.path.join(self.dest_dir, backups[0])
        manifest_file = backup_file + ".manifest.json"

        self.assertTrue(os.path.exists(manifest_file))
        with open(manifest_file, "r", encoding="utf-8") as mf:
            mdata = json.load(mf)
        self.assertEqual(mdata["sha256"], backup_db.calculate_sha256(backup_file))
        self.assertTrue(mdata["esquema_coincide"])
        self.assertEqual(mdata["recuentos_tablas"]["empresas"], 2)

        # Probar verificacion
        v_ok, v_code = backup_db.verify_backup(backup_file)
        self.assertTrue(v_ok)
        self.assertEqual(v_code, 0)

    def test_02_copia_mientras_escritura_concurrente_integrity_ok(self):
        """(ii) Copia mientras otra conexion escribe en el origen: la copia pasa integrity_check."""
        stop_writing = threading.Event()

        def writer_job():
            conn = sqlite3.connect(self.src_db, timeout=10.0)
            while not stop_writing.is_set():
                try:
                    conn.execute("INSERT INTO predicciones (ticker, precio) VALUES ('SAN.MC', 4.51);")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
                time.sleep(0.01)
            conn.close()

        w_thread = threading.Thread(target=writer_job)
        w_thread.start()

        try:
            success, code, manifest = backup_db.create_backup(self.src_db, self.dest_dir)
            self.assertTrue(success)
            self.assertEqual(code, 0)
        finally:
            stop_writing.set()
            w_thread.join()

        backups = [f for f in os.listdir(self.dest_dir) if f.endswith(".db")]
        self.assertEqual(len(backups), 1)
        backup_file = os.path.join(self.dest_dir, backups[0])

        v_ok, v_code = backup_db.verify_backup(backup_file)
        self.assertTrue(v_ok)
        self.assertEqual(v_code, 0)

    def test_03_alterar_un_byte_devuelve_codigo_2(self):
        """(iii) Alterar un byte de la copia hace que --verify devuelva 2."""
        success, code, manifest = backup_db.create_backup(self.src_db, self.dest_dir)
        self.assertEqual(code, 0)

        backups = [f for f in os.listdir(self.dest_dir) if f.endswith(".db")]
        backup_file = os.path.join(self.dest_dir, backups[0])

        # Alterar un byte del archivo
        with open(backup_file, "r+b") as f:
            f.seek(100)
            original_byte = f.read(1)
            new_byte = b"\x00" if original_byte != b"\x00" else b"\xff"
            f.seek(100)
            f.write(new_byte)

        v_ok, v_code = backup_db.verify_backup(backup_file)
        self.assertFalse(v_ok)
        self.assertEqual(v_code, 2)

    def test_04_rotacion_20_copias_diarias_mensuales_ajenos_intactos(self):
        """
        (iv) Rotacion: crea 20 copias automaticas con fechas simuladas junto a ficheros ajenos
        (stocklab_2026-10-12_pre_emision.db, notas.txt) y comprueba 14 diarias + mensuales
        y que los ajenos siguen intactos.
        """
        # Crear ficheros ajenos
        manual_db = os.path.join(self.dest_dir, "stocklab_2026-10-12_pre_emision.db")
        with open(manual_db, "w", encoding="utf-8") as f:
            f.write("backup manual")
        manual_sha_before = backup_db.calculate_sha256(manual_db)

        notas_txt = os.path.join(self.dest_dir, "notas.txt")
        with open(notas_txt, "w", encoding="utf-8") as f:
            f.write("notas del sistema")

        # Simular 20 copias automaticas con fechas distintas:
        # 16 dias en octubre 2026 (20261001 a 20261016)
        # 2 dias en septiembre 2026 (20260901, 20260915)
        # 2 dias en agosto 2026 (20260801, 20260815)
        simulated_dates = []
        for d in range(1, 17):
            simulated_dates.append(f"202610{d:02d}_120000")
        simulated_dates.append("20260901_120000")
        simulated_dates.append("20260915_120000")
        simulated_dates.append("20260801_120000")
        simulated_dates.append("20260815_120000")
        self.assertEqual(len(simulated_dates), 20)

        for sdate in simulated_dates:
            fname = f"stocklab_{sdate}.db"
            fpath = os.path.join(self.dest_dir, fname)
            shutil.copy(self.src_db, fpath)
            man_path = fpath + ".manifest.json"
            sha = backup_db.calculate_sha256(fpath)
            manifest = {
                "fecha_utc": f"2026-XX-XXT12:00:00Z",
                "sha256": sha,
                "recuentos_tablas": {"empresas": 2},
            }
            with open(man_path, "w", encoding="utf-8") as mf:
                json.dump(manifest, mf)

        # Ejecutar rotacion con keep_daily=14 y keep_monthly=12
        backup_db.rotate_backups(self.dest_dir, keep_daily=14, keep_monthly=12)

        # Ficheros ajenos intactos
        self.assertTrue(os.path.exists(manual_db))
        self.assertTrue(os.path.exists(notas_txt))
        self.assertEqual(backup_db.calculate_sha256(manual_db), manual_sha_before)

        # Comprobar copias conservadas:
        remaining = [f for f in os.listdir(self.dest_dir) if backup_db.PATTERN_BACKUP.match(f)]
        # De octubre: los 14 dias mas recientes (dias 3 al 16) = 14 copias.
        # De septiembre: la mas reciente (20260915) = 1 copia mensual.
        # De agosto: la mas reciente (20260815) = 1 copia mensual.
        # Total esperado conservado: 14 + 1 + 1 = 16 copias.
        # Las copias antiguas 20261001, 20261002, 20260901, 20260801 deben haber sido eliminadas.
        self.assertEqual(len(remaining), 16)
        self.assertIn("stocklab_20261016_120000.db", remaining)
        self.assertIn("stocklab_20261003_120000.db", remaining)
        self.assertNotIn("stocklab_20261001_120000.db", remaining)
        self.assertNotIn("stocklab_20261002_120000.db", remaining)
        self.assertIn("stocklab_20260915_120000.db", remaining)
        self.assertNotIn("stocklab_20260901_120000.db", remaining)
        self.assertIn("stocklab_20260815_120000.db", remaining)
        self.assertNotIn("stocklab_20260801_120000.db", remaining)

    def test_05_bd_inexistente_devuelve_codigo_3_sin_crear_archivos(self):
        """(v) BD inexistente -> codigo 3 sin crear archivos."""
        fake_db = os.path.join(self.tmp_dir, "no_existe_absolutamente.db")
        dest = os.path.join(self.tmp_dir, "dest_inexistente")
        success, code, manifest = backup_db.create_backup(fake_db, dest, retry_seconds=0.1)
        self.assertFalse(success)
        self.assertEqual(code, 3)
        self.assertIsNone(manifest)
        # No debe haber archivos .db en el destino
        if os.path.exists(dest):
            self.assertEqual(len([f for f in os.listdir(dest) if f.endswith(".db")]), 0)

    def test_06_origen_no_cambia_sha256_inmutable(self):
        """(vi) El origen no cambia (SHA-256 antes y despues, sin escritores)."""
        sha_before = backup_db.calculate_sha256(self.src_db)
        success, code, manifest = backup_db.create_backup(self.src_db, self.dest_dir)
        self.assertTrue(success)
        self.assertEqual(code, 0)
        sha_after = backup_db.calculate_sha256(self.src_db)
        self.assertEqual(sha_before, sha_after)

    def test_07_subprocess_pythonioencoding_cp1252_devuelve_0(self):
        """(vii) El script se ejecuta con subprocess y PYTHONIOENCODING=cp1252 y devuelve 0."""
        script_path = os.path.join(REPO_ROOT, "tools", "backup_db.py")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp1252"
        env["PYTHONUTF8"] = "0"

        cmd = [sys.executable, script_path, "--db", self.src_db, "--dest", self.dest_dir]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}")

        # Probar tambien --verify con subprocess y cp1252 (pasando --dest para no tocar el backup.log real)
        backups = [f for f in os.listdir(self.dest_dir) if f.endswith(".db")]
        self.assertTrue(len(backups) > 0)
        backup_file = os.path.join(self.dest_dir, backups[0])
        cmd_v = [sys.executable, script_path, "--verify", backup_file, "--dest", self.dest_dir]
        res_v = subprocess.run(cmd_v, env=env, capture_output=True, text=True)
        self.assertEqual(res_v.returncode, 0, f"STDOUT: {res_v.stdout}\nSTDERR: {res_v.stderr}")

    def test_08_ruta_destino_con_espacios(self):
        """(viii) Ruta de destino con espacios."""
        dest_con_espacios = os.path.join(self.tmp_dir, "carpeta con espacios en ruta")
        success, code, manifest = backup_db.create_backup(self.src_db, dest_con_espacios)
        self.assertTrue(success)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(dest_con_espacios))
        backups = [f for f in os.listdir(dest_con_espacios) if f.endswith(".db")]
        self.assertEqual(len(backups), 1)
        v_ok, v_code = backup_db.verify_backup(os.path.join(dest_con_espacios, backups[0]))
        self.assertTrue(v_ok)
        self.assertEqual(v_code, 0)

    def test_09_rotacion_conserva_multiples_copias_mismo_dia_por_keep_last(self):
        """(ix) Dos o mas copias automaticas en el mismo dia UTC sobreviven ambas gracias a keep_last=5."""
        # Simular 3 copias el mismo dia (ej. manana 08:00, mediodia 13:00, tarde 18:40)
        times = ["080000", "130000", "184000"]
        for t in times:
            fname = f"stocklab_20261021_{t}.db"
            fpath = os.path.join(self.dest_dir, fname)
            shutil.copy(self.src_db, fpath)
            sha = backup_db.calculate_sha256(fpath)
            with open(fpath + ".manifest.json", "w", encoding="utf-8") as mf:
                json.dump({"fecha_utc": f"2026-10-21T{t[:2]}:{t[2:4]}:{t[4:]}Z", "sha256": sha}, mf)

        backup_db.rotate_backups(self.dest_dir, keep_last=5, keep_daily=30, keep_monthly=12)

        # Las 3 copias del mismo dia deben sobrevivir
        for t in times:
            fname = f"stocklab_20261021_{t}.db"
            self.assertTrue(os.path.exists(os.path.join(self.dest_dir, fname)))
            self.assertTrue(os.path.exists(os.path.join(self.dest_dir, fname + ".manifest.json")))

    def test_10_rotacion_mas_de_keep_last_mismo_dia_conserva_n_recientes_y_anteriores(self):
        """(x) Con > keep_last copias en el mismo dia, conserva exactamente las keep_last mas recientes de ese dia y la mas reciente de cada dia anterior."""
        # 7 copias en el dia 20261021 (horas 01..07)
        for h in range(1, 8):
            fname = f"stocklab_20261021_{h:02d}0000.db"
            fpath = os.path.join(self.dest_dir, fname)
            shutil.copy(self.src_db, fpath)
            sha = backup_db.calculate_sha256(fpath)
            with open(fpath + ".manifest.json", "w", encoding="utf-8") as mf:
                json.dump({"fecha_utc": f"2026-10-21T{h:02d}:00:00Z", "sha256": sha}, mf)

        # 2 copias en el dia 20261020 (horas 10 y 18)
        for h in (10, 18):
            fname = f"stocklab_20261020_{h:02d}0000.db"
            fpath = os.path.join(self.dest_dir, fname)
            shutil.copy(self.src_db, fpath)
            sha = backup_db.calculate_sha256(fpath)
            with open(fpath + ".manifest.json", "w", encoding="utf-8") as mf:
                json.dump({"fecha_utc": f"2026-10-20T{h:02d}:00:00Z", "sha256": sha}, mf)

        # 1 copia en el dia 20261019
        fname_19 = "stocklab_20261019_120000.db"
        fpath_19 = os.path.join(self.dest_dir, fname_19)
        shutil.copy(self.src_db, fpath_19)
        sha_19 = backup_db.calculate_sha256(fpath_19)
        with open(fpath_19 + ".manifest.json", "w", encoding="utf-8") as mf:
            json.dump({"fecha_utc": "2026-10-19T12:00:00Z", "sha256": sha_19}, mf)

        # Ejecutar con keep_last=5, keep_daily=30
        backup_db.rotate_backups(self.dest_dir, keep_last=5, keep_daily=30, keep_monthly=12)

        # Del dia 21: deben sobrevivir las 5 mas recientes (03..07), y eliminarse las 2 mas antiguas (01 y 02)
        for h in range(3, 8):
            self.assertTrue(os.path.exists(os.path.join(self.dest_dir, f"stocklab_20261021_{h:02d}0000.db")))
            self.assertTrue(os.path.exists(os.path.join(self.dest_dir, f"stocklab_20261021_{h:02d}0000.db.manifest.json")))
        for h in (1, 2):
            self.assertFalse(os.path.exists(os.path.join(self.dest_dir, f"stocklab_20261021_{h:02d}0000.db")))
            self.assertFalse(os.path.exists(os.path.join(self.dest_dir, f"stocklab_20261021_{h:02d}0000.db.manifest.json")))

        # Del dia 20: debe sobrevivir la mas reciente (18) y eliminarse la antigua (10)
        self.assertTrue(os.path.exists(os.path.join(self.dest_dir, "stocklab_20261020_180000.db")))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, "stocklab_20261020_100000.db")))

        # Del dia 19: debe sobrevivir la unica diaria (12)
        self.assertTrue(os.path.exists(os.path.join(self.dest_dir, "stocklab_20261019_120000.db")))

    def test_11_copia_con_tag_generacion_manifest_y_proteccion_inmune(self):
        """(xi) Copia con --tag pre_c06 genera nombre con sufijo, manifest protegido y sobrevive a 20 rotaciones posteriores."""
        # 1. Crear copia con tag pre_c06
        success, code, mdata = backup_db.create_backup(self.src_db, self.dest_dir, tag="pre_c06")
        self.assertTrue(success)
        self.assertEqual(code, 0)
        self.assertEqual(mdata.get("tag"), "pre_c06")
        self.assertEqual(mdata.get("protected"), True)

        tagged_files = [f for f in os.listdir(self.dest_dir) if f.endswith("_pre_c06.db")]
        self.assertEqual(len(tagged_files), 1)
        tagged_db = os.path.join(self.dest_dir, tagged_files[0])
        tagged_man = tagged_db + ".manifest.json"
        self.assertTrue(os.path.exists(tagged_db))
        self.assertTrue(os.path.exists(tagged_man))

        # Verificar contenido en disco del manifest
        with open(tagged_man, "r", encoding="utf-8") as mf:
            disk_man = json.load(mf)
        self.assertEqual(disk_man["tag"], "pre_c06")
        self.assertEqual(disk_man["protected"], True)

        # 2. Simular 20 copias automaticas posteriores en el mismo dia y en dias distintos
        for d in range(1, 11):
            for h in (8, 18):
                fname = f"stocklab_202610{d:02d}_{h:02d}0000.db"
                fpath = os.path.join(self.dest_dir, fname)
                shutil.copy(self.src_db, fpath)
                sha = backup_db.calculate_sha256(fpath)
                with open(fpath + ".manifest.json", "w", encoding="utf-8") as mf:
                    json.dump({"fecha_utc": f"2026-10-{d:02d}T{h:02d}:00:00Z", "sha256": sha}, mf)

        # Ejecutar rotacion estricta keep_last=3, keep_daily=5
        backup_db.rotate_backups(self.dest_dir, keep_last=3, keep_daily=5, keep_monthly=2)

        # La copia con tag DEBE seguir intacta
        self.assertTrue(os.path.exists(tagged_db))
        self.assertTrue(os.path.exists(tagged_man))

        # Y pasa verify_backup
        v_ok, v_code = backup_db.verify_backup(tagged_db)
        self.assertTrue(v_ok)
        self.assertEqual(v_code, 0)

    def test_12_tag_invalido_rechazado_con_value_error_y_codigo_salida(self):
        """(xii) Tags invalidos (con espacios, ../, simbolos raros o vacios) son rechazados."""
        tags_invalidos = ["tag con espacios", "tag/con/slashes", "../escape", "tag$raro", "", "a" * 41]
        for bad_tag in tags_invalidos:
            with self.subTest(tag=bad_tag):
                with self.assertRaises(ValueError):
                    backup_db.create_backup(self.src_db, self.dest_dir, tag=bad_tag)

        # Probar rechazo en linea de comandos via subprocess pasando --dest aislado
        script_path = os.path.join(REPO_ROOT, "tools", "backup_db.py")
        cmd = [sys.executable, script_path, "--db", self.src_db, "--dest", self.dest_dir, "--tag", "tag invalido"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)

    def test_13_copias_manuales_antiguas_permanecen_intactas(self):
        """(xiii) Copias manuales tipo stocklab_2026-10-12_pre_emision.db no son eliminadas por rotacion."""
        manual_names = [
            "stocklab_2026-10-12_pre_emision.db",
            "stocklab_2026-10-12_pre_emision_s12.db",
            "stocklab_manual_especial.db",
        ]
        for mname in manual_names:
            mpath = os.path.join(self.dest_dir, mname)
            with open(mpath, "w", encoding="utf-8") as f:
                f.write(f"contenido manual {mname}")

        # Simular copias automaticas viejas que provocan rotacion
        for d in range(1, 10):
            fname = f"stocklab_202607{d:02d}_120000.db"
            fpath = os.path.join(self.dest_dir, fname)
            shutil.copy(self.src_db, fpath)
            sha = backup_db.calculate_sha256(fpath)
            with open(fpath + ".manifest.json", "w", encoding="utf-8") as mf:
                json.dump({"fecha_utc": f"2026-07-{d:02d}T12:00:00Z", "sha256": sha}, mf)

        backup_db.rotate_backups(self.dest_dir, keep_last=2, keep_daily=2, keep_monthly=1)

        for mname in manual_names:
            self.assertTrue(os.path.exists(os.path.join(self.dest_dir, mname)))

    def test_14_list_backups_indica_tag_y_proteccion(self):
        """(xiv) list_backups muestra estado con tag y proteccion en stdout."""
        backup_db.create_backup(self.src_db, self.dest_dir, tag="hito_v5")
        backup_db.create_backup(self.src_db, self.dest_dir)

        import io
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = backup_db.list_backups(self.dest_dir)
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("tag=hito_v5", output)
        self.assertIn("Verificada", output)


if __name__ == "__main__":
    unittest.main()
