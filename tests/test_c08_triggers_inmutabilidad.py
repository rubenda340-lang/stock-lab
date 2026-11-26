"""
Tests para el Paso C-08: Triggers SQLite de inmutabilidad en predicciones
=========================================================================
Verifica sobre base de datos SQLite temporal sintetica y sobre copia temporal
de stocklab.db (abierta en mode=ro con uri=True), sin red y con el reloj
parcheado en utils.market_calendar, services.emision y database.predicciones:
  (a) DELETE FROM predicciones falla con sqlite3.IntegrityError
      ("Inmutabilidad: prohibido borrar filas de predicciones") y conserva
      todas las filas.
  (b) UPDATE predicciones sobre cualquiera de las 33 columnas de emision
      falla con sqlite3.IntegrityError ("Inmutabilidad: prohibido modificar
      columnas de emision"), tanto en filas pendientes (resuelta=0) como
      resueltas (resuelta=1 y resuelta=2).
  (c) UPDATE predicciones sobre las 8 columnas de resolucion falla con
      sqlite3.IntegrityError ("Inmutabilidad: transicion de resolucion no
      permitida") cuando OLD.resuelta = 1, cuando OLD.resuelta = 2, cuando
      OLD.resuelta = 0 y NEW.resuelta = 0, y cuando NEW.resuelta toma un
      valor invalido como 3.
  (d) Las transiciones legitimas 0 -> 1 y 0 -> 2 mediante resolver_predicciones(),
      la insercion de nuevas senales mediante registrar_predicciones() y el
      cambio de archivada bajo STOCKLAB_DEV=1 funcionan sin errores.
  (e) Ensayo sobre copia temporal de stocklab.db (4960 filas): ejecuta
      asegurar_triggers_inmutabilidad, verifica que los 2 triggers quedan
      instalados, que DELETE y UPDATE de emision/resolucion ya resuelta fallan
      con sqlite3.IntegrityError, que una segunda llamada a
      asegurar_triggers_inmutabilidad e init_predicciones_table produce
      total_changes == 0, y que los 4 hashes oficiales coinciden al 100%.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database.connection
from database import predicciones as db_pred
from database.connection import get_connection
import services.emision as se
import utils.market_calendar as mc

MADRID_TZ = ZoneInfo("Europe/Madrid")
REAL_DB_PATH = os.path.join(BASE_DIR, "stocklab.db")


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Parchea get_ahora_madrid en los tres espacios de nombres requeridos."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _sha256_inmutables_11(conn: sqlite3.Connection) -> str:
    """Formula oficial SHA256_INMUTABLES_11 (C-06b)."""
    return db_pred._calcular_sha256_inmutables(conn, "predicciones")


def _sha256_inmutables_10_sin_modelo(conn: sqlite3.Connection) -> str:
    """Formula oficial SHA256_INMUTABLES_10_SIN_MODELO (C-06b)."""
    return db_pred._calcular_sha256_inmutables_10_sin_modelo(conn, "predicciones")


def _sha256_inmutables_11_desde_12oct(conn: sqlite3.Connection) -> str:
    """Formula oficial SHA256_INMUTABLES_11_DESDE_12OCT (C-06b)."""
    return db_pred._calcular_sha256_inmutables_11_desde_corte(conn, "predicciones")


def _sha256_resueltas(conn: sqlite3.Connection) -> str:
    """Formula oficial SHA256_RESUELTAS (C-06b)."""
    return db_pred._calcular_sha256_resueltas(conn, "predicciones")


class TestC08TriggersInmutabilidad(unittest.TestCase):
    """Suite unitaria para los triggers SQLite de inmutabilidad en predicciones."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_c08_")
        self.temp_db = os.path.join(self.temp_dir, "test_synthetic_c08.db")
        self.orig_db_path = database.connection.DB_PATH
        database.connection.DB_PATH = self.temp_db
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass
        db_pred.init_predicciones_table()

    def tearDown(self):
        database.connection.DB_PATH = self.orig_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

    def _poblar_tres_estados(self):
        """
        Inserta 3 filas sinteticas completas en predicciones:
          - id=1: pendiente (resuelta=0)
          - id=2: resuelta con cotizacion (resuelta=1)
          - id=3: resuelta sin cotizacion (resuelta=2)
        """
        conn = get_connection()
        try:
            filas = [
                (
                    1, "SAN.MC", "Banco Santander", "2026-10-15", "1D", 1, "2026-10-16",
                    "ALCISTA", 1.25, 1.50, 68.0, 4.50, 0.85, "v5-frozen",
                    0, None, None, None, None, None, None, None,
                    1.20, 0.80, 0.015, 0.40, 0.50, 0.80, 0.90,
                    25.0, 1.20, "ALCISTA", 1.50, 0.10, 0.20, "2026-10-15T19:00:00",
                    "v1-dynamic-zone", 0,
                    "2026-10-15T17:00:00Z", "a" * 64, 0.8512, '{"adx":25.0}',
                ),
                (
                    2, "BBVA.MC", "BBVA", "2026-10-14", "1D", 1, "2026-10-15",
                    "ALCISTA", 1.40, 1.60, 70.0, 9.00, 0.90, "v5-frozen",
                    1, "2026-10-15", 9.18, 2.00, 1, 0.60, "vencida", 88.5,
                    1.35, 0.80, 0.016, 0.50, 0.60, 0.85, 0.95,
                    28.0, 1.35, "ALCISTA", 1.80, 0.15, 0.25, "2026-10-14T19:00:00",
                    "v1-dynamic-zone", 0,
                    "2026-10-14T17:00:00Z", "b" * 64, 0.9011, '{"adx":28.0}',
                ),
                (
                    3, "ECR.MC", "Ercros", "2026-10-01", "1D", 1, "2026-10-02",
                    "LATERAL", 0.10, 1.20, 52.0, 3.50, 0.05, "legacy-pre-freeze",
                    2, "2026-10-09", None, None, None, None, "sin_cotizacion", None,
                    0.10, 0.80, 0.012, 0.00, 0.10, 0.00, 0.10,
                    18.0, 0.80, "NEUTRA", -0.50, 0.00, 0.00, "2026-10-01T19:00:00",
                    "v1-dynamic-zone", 0,
                    None, None, None, None,
                ),
            ]
            placeholders = ",".join(["?"] * 42)
            conn.executemany(f"INSERT INTO predicciones VALUES ({placeholders})", filas)
            conn.commit()
        finally:
            conn.close()

    def test_a_delete_bloqueado_por_trigger(self):
        """
        (a) DELETE FROM predicciones falla con sqlite3.IntegrityError
        ("Inmutabilidad: prohibido borrar filas de predicciones") y conserva todas las filas.
        """
        self._poblar_tres_estados()
        conn = get_connection()
        try:
            snap_antes = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
            self.assertEqual(len(snap_antes), 3)

            for where_clause in ("WHERE id = 1", "WHERE resuelta = 1", "WHERE resuelta = 2", ""):
                sql = f"DELETE FROM predicciones {where_clause}".strip()
                with self.assertRaises(sqlite3.IntegrityError) as ctx:
                    conn.execute(sql)
                self.assertIn(
                    "Inmutabilidad: prohibido borrar filas de predicciones",
                    str(ctx.exception),
                )

            snap_despues = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
            self.assertEqual(snap_despues, snap_antes)
        finally:
            conn.close()

    def test_b_update_columnas_emision_bloqueado_en_todos_los_estados(self):
        """
        (b) UPDATE predicciones sobre cualquiera de las 33 columnas de emision falla con
        sqlite3.IntegrityError ("Inmutabilidad: prohibido modificar columnas de emision"),
        tanto en filas pendientes (resuelta=0) como resueltas (resuelta=1 y resuelta=2).
        """
        self._poblar_tres_estados()

        valores_nuevos_33 = {
            "id": 999,
            "ticker": "OTRO.MC",
            "empresa": "Otra Empresa",
            "fecha_prediccion": "2026-10-20",
            "horizonte": "3D",
            "dias": 3,
            "fecha_objetivo": "2026-10-23",
            "senal": "BAJISTA",
            "pct_estimado": -5.5,
            "banda_pct": 9.9,
            "confianza": 99.0,
            "precio_entrada": 999.0,
            "score_motor": -0.99,
            "modelo": "otro-modelo",
            "pct_estimado_raw": -5.5,
            "umbral_zona": 2.5,
            "sigma": 0.099,
            "comp_rsi": -1.5,
            "comp_macd": -1.5,
            "comp_tendencia": -1.5,
            "comp_momentum": -1.5,
            "adx": 55.0,
            "rvol": 4.5,
            "marea": "BAJISTA",
            "rs_ibex": -9.9,
            "sentimiento": -0.9,
            "tilt": -0.8,
            "hora_calculo": "2026-10-20T20:00:00",
            "regla_eval": "otra-regla",
            "emitida_en": "2026-10-20T18:00:00Z",
            "params_hash": "f" * 64,
            "score_bruto": -0.9999,
            "features_json": '{"mod":1}',
        }
        self.assertEqual(
            tuple(valores_nuevos_33.keys()),
            db_pred._COLUMNAS_INMUTABLES_EMISION_33,
        )
        self.assertEqual(len(valores_nuevos_33), 33)

        conn = get_connection()
        try:
            snap_antes = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()

            for row_id in (1, 2, 3):
                for col, new_val in valores_nuevos_33.items():
                    with self.assertRaises(
                        sqlite3.IntegrityError,
                        msg=f"Debio fallar UPDATE de columna de emision {col} en id={row_id}",
                    ) as ctx:
                        conn.execute(
                            f"UPDATE predicciones SET {col} = ? WHERE id = ?",
                            (new_val, row_id),
                        )
                    self.assertIn(
                        "Inmutabilidad: prohibido modificar columnas de emision",
                        str(ctx.exception),
                    )

            snap_despues = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
            self.assertEqual(snap_despues, snap_antes)
        finally:
            conn.close()

    def test_c_update_columnas_resolucion_transiciones_invalidas_bloqueadas(self):
        """
        (c) UPDATE predicciones sobre las columnas de resolucion falla con
        sqlite3.IntegrityError ("Inmutabilidad: transicion de resolucion no permitida"):
          - cuando OLD.resuelta = 1 (cambiar precio_real, acierto, cualquiera de las 8, o volver a resuelta=0/2)
          - cuando OLD.resuelta = 2 (cambiar cualquiera de las 8 o pasar a resuelta=0/1)
          - cuando OLD.resuelta = 0 y NEW.resuelta = 0 (modificar precio_real u otra sin resolver)
          - cuando NEW.resuelta toma un valor invalido como 3, -1 o NULL.
        """
        self._poblar_tres_estados()

        mutaciones_res = {
            "resuelta": 0,
            "fecha_resolucion": "2026-10-22",
            "precio_real": 123.45,
            "variacion_real": 9.87,
            "acierto": 0,
            "error_pct": 7.65,
            "motivo_resolucion": "manipulada",
            "score_calidad": 11.1,
        }
        self.assertEqual(tuple(mutaciones_res.keys()), db_pred._COLUMNAS_RESOLUCION_8)
        self.assertEqual(len(mutaciones_res), 8)

        conn = get_connection()
        try:
            snap_antes = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()

            # 1) Fila ya resuelta (id=2, OLD.resuelta=1): ninguna de las 8 columnas puede cambiar
            for col, val in mutaciones_res.items():
                with self.assertRaises(sqlite3.IntegrityError) as ctx:
                    conn.execute(f"UPDATE predicciones SET {col} = ? WHERE id = 2", (val,))
                self.assertIn(
                    "Inmutabilidad: transicion de resolucion no permitida",
                    str(ctx.exception),
                )

            # Tampoco pasar de resuelta=1 a resuelta=2
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute("UPDATE predicciones SET resuelta = 2 WHERE id = 2")
            self.assertIn(
                "Inmutabilidad: transicion de resolucion no permitida",
                str(ctx.exception),
            )

            # 2) Fila sin_cotizacion (id=3, OLD.resuelta=2): ninguna de las 8 columnas puede cambiar
            for col, val in mutaciones_res.items():
                with self.assertRaises(sqlite3.IntegrityError) as ctx:
                    conn.execute(f"UPDATE predicciones SET {col} = ? WHERE id = 3", (val,))
                self.assertIn(
                    "Inmutabilidad: transicion de resolucion no permitida",
                    str(ctx.exception),
                )

            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                conn.execute("UPDATE predicciones SET resuelta = 1, precio_real = 3.6 WHERE id = 3")
            self.assertIn(
                "Inmutabilidad: transicion de resolucion no permitida",
                str(ctx.exception),
            )

            # 3) Fila pendiente (id=1, OLD.resuelta=0) manteniendo NEW.resuelta=0:
            #    modificar cualquiera de las otras 7 columnas de resolucion sin resolver debe fallar
            for col, val in mutaciones_res.items():
                if col == "resuelta":
                    continue
                with self.assertRaises(sqlite3.IntegrityError) as ctx:
                    conn.execute(f"UPDATE predicciones SET {col} = ? WHERE id = 1", (val,))
                self.assertIn(
                    "Inmutabilidad: transicion de resolucion no permitida",
                    str(ctx.exception),
                )

            # 4) Fila pendiente (id=1, OLD.resuelta=0) hacia valores invalidos de NEW.resuelta (3, -1, NULL)
            for invalid_res in (3, -1, 99, None):
                with self.assertRaises(sqlite3.IntegrityError) as ctx:
                    conn.execute(
                        "UPDATE predicciones SET resuelta = ?, precio_real = 4.6 WHERE id = 1",
                        (invalid_res,),
                    )
                self.assertIn(
                    "Inmutabilidad: transicion de resolucion no permitida",
                    str(ctx.exception),
                )

            snap_despues = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
            self.assertEqual(snap_despues, snap_antes)
        finally:
            conn.close()

    def test_d_operaciones_legitimas_permitidas(self):
        """
        (d) Verifica que las operaciones legitimas funcionan sin errores:
          1. Insercion de nuevas senales mediante registrar_predicciones().
          2. Transicion 0 -> 1 mediante resolver_predicciones() con serie de cierre real.
          3. Transicion 0 -> 2 (sin_cotizacion) mediante resolver_predicciones() tras 5 sesiones.
          4. Cambio de archivada (0 -> 1 y 1 -> 0) bajo STOCKLAB_DEV=1.
        """
        # 1. Insercion con registrar_predicciones en ventana valida
        dt_emision = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        df_radar = pd.DataFrame([
            {
                "ticker": "SAN.MC",
                "empresa": "Banco Santander",
                "precio": 4.50,
                "ultima_fecha": "2026-10-15",
                "senal_1D": "ALCISTA",
                "pct_1D": 1.20,
                "banda_1D": 1.50,
                "conf_1D": 65.0,
                "score_1D": 0.85,
                "score_bruto_1D": 0.8512,
                "pct_raw_1D": 1.20,
            },
            {
                "ticker": "SINCOT.MC",
                "empresa": "Sin Cotizacion SA",
                "precio": 10.00,
                "ultima_fecha": "2026-10-15",
                "senal_1D": "LATERAL",
                "pct_1D": 0.10,
                "banda_1D": 1.20,
                "conf_1D": 55.0,
                "score_1D": 0.05,
                "score_bruto_1D": 0.051,
                "pct_raw_1D": 0.10,
            },
        ])
        with _reloj_madrid(dt_emision):
            n_ins = db_pred.registrar_predicciones(df_radar, fecha_prediccion="2026-10-15")
        self.assertEqual(n_ins, 2)

        # 2. Resolver 0 -> 1 para SAN.MC y 0 -> 2 (sin_cotizacion) para SINCOT.MC
        #    El 2026-10-15 (jueves) a 1D tiene fecha_objetivo = 2026-10-16 (viernes).
        #    Si estamos en 2026-10-26 (lunes, >5 sesiones habiles tras 2026-10-16):
        #    - SAN.MC tiene barra el 2026-10-16 y en la ultima sesion cerrada (2026-10-26) -> resuelve 0 -> 1.
        #    - SINCOT.MC no tiene barras tras el 2026-10-15 (y el lote es sano >=50% en 2026-10-26) -> resuelve 0 -> 2.
        dt_resol = datetime(2026, 10, 26, 19, 0, 0, tzinfo=MADRID_TZ)
        fechas_previas = [
            d.strftime("%Y-%m-%d")
            for d in pd.bdate_range(end="2026-10-14", periods=40)
        ]
        fechas_post = [
            "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
            "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26",
        ]
        fechas_lote = pd.to_datetime(fechas_previas + fechas_post)
        cols_batch = pd.MultiIndex.from_product(
            [["SAN.MC", "SINCOT.MC"], ["Open", "High", "Low", "Close", "Volume"]]
        )
        df_batch = pd.DataFrame(float("nan"), index=fechas_lote, columns=cols_batch)
        for f_dt in fechas_lote:
            f_iso = f_dt.strftime("%Y-%m-%d")
            p_san = 4.62 if f_iso == "2026-10-16" else 4.50
            df_batch.loc[f_dt, ("SAN.MC", "Open")] = p_san
            df_batch.loc[f_dt, ("SAN.MC", "High")] = p_san
            df_batch.loc[f_dt, ("SAN.MC", "Low")] = p_san
            df_batch.loc[f_dt, ("SAN.MC", "Close")] = p_san
            df_batch.loc[f_dt, ("SAN.MC", "Volume")] = 10000.0
            if f_iso <= "2026-10-15":
                df_batch.loc[f_dt, ("SINCOT.MC", "Open")] = 10.00
                df_batch.loc[f_dt, ("SINCOT.MC", "High")] = 10.00
                df_batch.loc[f_dt, ("SINCOT.MC", "Low")] = 10.00
                df_batch.loc[f_dt, ("SINCOT.MC", "Close")] = 10.00
                df_batch.loc[f_dt, ("SINCOT.MC", "Volume")] = 10000.0

        with _reloj_madrid(dt_resol):
            n_res = db_pred.resolver_predicciones(df_batch=df_batch)
        self.assertEqual(n_res, 1)

        conn = get_connection()
        try:
            row_san = conn.execute(
                "SELECT resuelta, precio_real, acierto, motivo_resolucion "
                "FROM predicciones WHERE ticker = 'SAN.MC'"
            ).fetchone()
            row_sc = conn.execute(
                "SELECT resuelta, precio_real, acierto, motivo_resolucion "
                "FROM predicciones WHERE ticker = 'SINCOT.MC'"
            ).fetchone()
            self.assertEqual(row_san[0], 1)
            self.assertAlmostEqual(float(row_san[1]), 4.62, places=4)
            self.assertEqual(row_san[2], 1)
            self.assertEqual(row_san[3], "vencida")

            self.assertEqual(row_sc[0], 2)
            self.assertIsNone(row_sc[1])
            self.assertIsNone(row_sc[2])
            self.assertEqual(row_sc[3], "sin_cotizacion")
        finally:
            conn.close()

        # 3. Cambio de archivada bajo STOCKLAB_DEV=1
        with patch.dict(os.environ, {"STOCKLAB_DEV": "1"}):
            n_arch = db_pred.limpiar_resueltas(horizonte="1D")
            self.assertEqual(n_arch, 1)

        conn = get_connection()
        try:
            arch_san = conn.execute(
                "SELECT archivada FROM predicciones WHERE ticker = 'SAN.MC'"
            ).fetchone()[0]
            self.assertEqual(arch_san, 1)
            conn.execute("UPDATE predicciones SET archivada = 0 WHERE ticker = 'SAN.MC'")
            conn.commit()
            arch_san_2 = conn.execute(
                "SELECT archivada FROM predicciones WHERE ticker = 'SAN.MC'"
            ).fetchone()[0]
            self.assertEqual(arch_san_2, 0)
        finally:
            conn.close()

    @unittest.skipUnless(os.path.exists(REAL_DB_PATH), "stocklab.db no disponible")
    def test_e_ensayo_sobre_copia_temporal_de_stocklab_db(self):
        """
        (e) Ensayo sobre copia temporal de stocklab.db:
          - Abre stocklab.db en mode=ro con uri=True y la copia a un directorio temporal.
          - Verifica que total_antes >= 4960 y filas_antes == filas_despues == total_antes.
          - Ejecuta asegurar_triggers_inmutabilidad sobre la copia.
          - Verifica que los 2 triggers quedan instalados en sqlite_master.
          - Verifica que DELETE y UPDATE de emision/resolucion ya resuelta fallan
            con sqlite3.IntegrityError.
          - Verifica que una segunda llamada a asegurar_triggers_inmutabilidad e
            init_predicciones_table produce total_changes == 0.
          - Verifica que las filas con modelo = 'legacy-pre-freeze' (< '2026-10-12') son 1384,
            las filas con modelo = 'v5-frozen' (>= '2026-10-12') son total_antes - 1384,
            y los 4 hashes calculados sobre la copia antes y despues coinciden al 100%
            entre si y con src_conn.
        """
        with open(REAL_DB_PATH, "rb") as f:
            sha_real_antes = hashlib.sha256(f.read()).hexdigest()

        with tempfile.TemporaryDirectory(prefix="ensayo_c08_") as tmp_dir:
            copia_path = os.path.join(tmp_dir, "stocklab_copia_c08.db")

            src_conn = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
            try:
                total_antes = src_conn.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
                h_inm11_antes = _sha256_inmutables_11(src_conn)
                h_inm10_antes = _sha256_inmutables_10_sin_modelo(src_conn)
                h_inm11_12oct_antes = _sha256_inmutables_11_desde_12oct(src_conn)
                h_res_antes = _sha256_resueltas(src_conn)

                dst_conn = sqlite3.connect(copia_path)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

            self.assertGreaterEqual(total_antes, 4960)

            conn_copia_pre = sqlite3.connect(copia_path)
            try:
                filas_antes = conn_copia_pre.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
                h_inm11_copia_antes = _sha256_inmutables_11(conn_copia_pre)
                h_inm10_copia_antes = _sha256_inmutables_10_sin_modelo(conn_copia_pre)
                h_inm11_12oct_copia_antes = _sha256_inmutables_11_desde_12oct(conn_copia_pre)
                h_res_copia_antes = _sha256_resueltas(conn_copia_pre)
            finally:
                conn_copia_pre.close()

            self.assertEqual(filas_antes, total_antes)
            self.assertEqual(h_inm11_copia_antes, h_inm11_antes)
            self.assertEqual(h_inm10_copia_antes, h_inm10_antes)
            self.assertEqual(h_inm11_12oct_copia_antes, h_inm11_12oct_antes)
            self.assertEqual(h_res_copia_antes, h_res_antes)

            res_trig = db_pred.asegurar_triggers_inmutabilidad(db_path=copia_path)
            self.assertEqual(
                res_trig["triggers_activos"],
                ["trg_predicciones_inmutables_update", "trg_predicciones_no_delete"],
            )

            conn_copia = sqlite3.connect(copia_path)
            try:
                self.assertTrue(db_pred._es_esquema_v2(conn_copia))

                trigs_master = [
                    r[0]
                    for r in conn_copia.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND tbl_name = 'predicciones' ORDER BY name"
                    ).fetchall()
                ]
                self.assertEqual(
                    trigs_master,
                    ["trg_predicciones_inmutables_update", "trg_predicciones_no_delete"],
                )

                # Intentos prohibidos de DELETE y UPDATE sobre la copia
                id_primera = conn_copia.execute(
                    "SELECT MIN(id) FROM predicciones"
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError) as ctx_del:
                    conn_copia.execute("DELETE FROM predicciones WHERE id = ?", (id_primera,))
                self.assertIn(
                    "Inmutabilidad: prohibido borrar filas de predicciones",
                    str(ctx_del.exception),
                )

                with self.assertRaises(sqlite3.IntegrityError) as ctx_upd_em:
                    conn_copia.execute(
                        "UPDATE predicciones SET precio_entrada = 999.0 WHERE id = ?",
                        (id_primera,),
                    )
                self.assertIn(
                    "Inmutabilidad: prohibido modificar columnas de emision",
                    str(ctx_upd_em.exception),
                )

                id_resuelta = conn_copia.execute(
                    "SELECT id FROM predicciones WHERE resuelta = 1 ORDER BY id ASC LIMIT 1"
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError) as ctx_upd_res:
                    conn_copia.execute(
                        "UPDATE predicciones SET precio_real = 999.0 WHERE id = ?",
                        (id_resuelta,),
                    )
                self.assertIn(
                    "Inmutabilidad: transicion de resolucion no permitida",
                    str(ctx_upd_res.exception),
                )

                # Segunda llamada a asegurar_triggers_inmutabilidad e init_predicciones_table: total_changes == 0
                changes_0 = conn_copia.total_changes
                res_trig_2 = db_pred.asegurar_triggers_inmutabilidad(conn=conn_copia)
                self.assertFalse(res_trig_2["creados"])
                self.assertEqual(res_trig_2["instalados"], [])
                self.assertEqual(conn_copia.total_changes, changes_0)

                db_pred.init_predicciones_table(conn=conn_copia)
                self.assertEqual(conn_copia.total_changes, changes_0)
                self.assertEqual(conn_copia.total_changes, 0)

                # Verificacion de recuento, desglose legacy vs v5-frozen y los 4 hashes
                filas_despues = conn_copia.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
                self.assertEqual(filas_antes, total_antes)
                self.assertEqual(filas_despues, total_antes)

                n_lt_legacy = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones "
                    "WHERE fecha_prediccion < '2026-10-12' AND modelo = 'legacy-pre-freeze'"
                ).fetchone()[0]
                n_lt_otros = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones "
                    "WHERE fecha_prediccion < '2026-10-12' AND modelo != 'legacy-pre-freeze'"
                ).fetchone()[0]
                n_ge_v5 = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones "
                    "WHERE fecha_prediccion >= '2026-10-12' AND modelo = 'v5-frozen'"
                ).fetchone()[0]
                n_ge_otros = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones "
                    "WHERE fecha_prediccion >= '2026-10-12' AND modelo != 'v5-frozen'"
                ).fetchone()[0]
                self.assertEqual(n_lt_legacy, 1384)
                self.assertEqual(n_lt_otros, 0)
                self.assertEqual(n_ge_v5, total_antes - 1384)
                self.assertEqual(n_ge_otros, 0)

                h_inm11_copia_despues = _sha256_inmutables_11(conn_copia)
                h_inm10_copia_despues = _sha256_inmutables_10_sin_modelo(conn_copia)
                h_inm11_12oct_copia_despues = _sha256_inmutables_11_desde_12oct(conn_copia)
                h_res_copia_despues = _sha256_resueltas(conn_copia)

                self.assertEqual(h_inm11_copia_despues, h_inm11_copia_antes)
                self.assertEqual(h_inm11_copia_despues, h_inm11_antes)
                self.assertEqual(h_inm10_copia_despues, h_inm10_copia_antes)
                self.assertEqual(h_inm10_copia_despues, h_inm10_antes)
                self.assertEqual(h_inm11_12oct_copia_despues, h_inm11_12oct_copia_antes)
                self.assertEqual(h_inm11_12oct_copia_despues, h_inm11_12oct_antes)
                self.assertEqual(h_res_copia_despues, h_res_copia_antes)
                self.assertEqual(h_res_copia_despues, h_res_antes)
            finally:
                conn_copia.close()

        with open(REAL_DB_PATH, "rb") as f:
            sha_real_despues = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(sha_real_despues, sha_real_antes)


if __name__ == "__main__":
    unittest.main()
