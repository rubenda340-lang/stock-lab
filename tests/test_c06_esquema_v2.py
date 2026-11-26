"""
Tests para el Paso C-06 (C-06a y C-06b): Esquema v2 de predicciones, marcado legacy y migracion
===============================================================================================
Verifica sobre bases de datos temporales (y copia en TemporaryDirectory de
stocklab.db abierta en mode=ro), sin red y con el reloj parcheado en
utils.market_calendar, services.emision y database.predicciones:
  (a) Migracion de una tabla v1 con datos sinteticos a v2 via migrar_predicciones_v2:
      conserva el mismo numero de filas y los mismos id, etiqueta con
      modelo = 'legacy-pre-freeze' las filas con fecha_prediccion < '2026-10-12',
      mantiene modelo = 'v5-frozen' desde '2026-10-12', verifica que
      SHA256_INMUTABLES_10_SIN_MODELO, SHA256_INMUTABLES_11 desde '2026-10-12' y
      SHA256_RESUELTAS son identicos antes y despues, deja emitida_en/params_hash/
      score_bruto/features_json en NULL en las filas migradas, y una segunda llamada
      es idempotente (migrada=False). Incluye verificacion de ROLLBACK ante fallo simulado.
  (b) Convivencia de dos modelos ("v5-frozen" y "challenger-v1" / "challenger-test")
      para el mismo (ticker, horizonte, fecha_prediccion): ambos se insertan sin
      conflicto (tanto via registrar_predicciones/INSERT como via ejecutar_emision_diaria),
      mientras que insertar dos veces el mismo (ticker, horizonte, fecha_prediccion, modelo)
      es ignorado por ON CONFLICT IGNORE.
  (c) Las filas nuevas insertadas con registrar_predicciones rellenan emitida_en
      (en UTC ISO-8601 con reloj inyectado), params_hash, score_bruto y
      features_json valido y determinista (sort_keys=True).
  (d) init_predicciones_table() sobre una BD ya en esquema v2 no realiza escrituras
      (comprobando total_changes de la conexion SQLite == 0 y PRAGMA data_version).
  (e) Ensayo sobre copia de produccion: copia stocklab.db (abierta en mode=ro con uri=True)
      via sqlite3 backup API a un fichero temporal en tempfile.TemporaryDirectory,
      ejecuta migrar_predicciones_v2 sobre la copia temporal y verifica
      filas_antes == filas_despues == 4960, 1384 filas < '2026-10-12' con
      modelo == 'legacy-pre-freeze', 3576 filas >= '2026-10-12' con modelo == 'v5-frozen',
      SHA256_INMUTABLES_10_SIN_MODELO y SHA256_RESUELTAS identicos, y filtrado de
      obtener_resueltas y obtener_metricas con incluir_legacy=False vs incluir_legacy=True.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import database.connection
import database.predicciones as db_pred
from database.connection import get_connection
import services.emision as se
import services.signal_engine as sig_eng
import utils.market_calendar as mc

MADRID_TZ = ZoneInfo("Europe/Madrid")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DB_PATH = os.path.join(REPO_ROOT, "stocklab.db")

_DDL_V1 = """
CREATE TABLE predicciones (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker             TEXT    NOT NULL,
    empresa            TEXT,
    fecha_prediccion   TEXT    NOT NULL,
    horizonte          TEXT    NOT NULL,
    dias               INTEGER NOT NULL,
    fecha_objetivo     TEXT    NOT NULL,
    senal              TEXT    NOT NULL,
    pct_estimado       REAL,
    banda_pct          REAL,
    confianza          REAL,
    precio_entrada     REAL    NOT NULL,
    score_motor        REAL,
    modelo             TEXT    NOT NULL DEFAULT 'v1-rules',
    resuelta           INTEGER NOT NULL DEFAULT 0,
    fecha_resolucion   TEXT,
    precio_real        REAL,
    variacion_real     REAL,
    acierto            INTEGER,
    error_pct          REAL,
    motivo_resolucion  TEXT,
    score_calidad      REAL,
    pct_estimado_raw   REAL,
    umbral_zona        REAL,
    sigma              REAL,
    comp_rsi           REAL,
    comp_macd          REAL,
    comp_tendencia     REAL,
    comp_momentum      REAL,
    adx                REAL,
    rvol               REAL,
    marea              TEXT,
    rs_ibex            REAL,
    sentimiento        REAL,
    tilt               REAL,
    hora_calculo       TEXT,
    regla_eval         TEXT    DEFAULT 'v1-dynamic-zone',
    archivada          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(ticker, horizonte, fecha_prediccion) ON CONFLICT IGNORE
);
"""


def _sha256_inmutables_11(conn: sqlite3.Connection) -> str:
    """Formula exacta de SHA256_INMUTABLES_11 (Regla 7)."""
    orig_rf = conn.row_factory
    try:
        conn.row_factory = None
        return hashlib.sha256(
            repr(
                conn.execute(
                    "SELECT id, ticker, fecha_prediccion, horizonte, senal, pct_estimado, "
                    "precio_entrada, modelo, score_motor, tilt, hora_calculo "
                    "FROM predicciones ORDER BY id ASC"
                ).fetchall()
            ).encode("utf-8")
        ).hexdigest()
    finally:
        conn.row_factory = orig_rf


_sha256_inmutables = _sha256_inmutables_11


def _sha256_inmutables_10_sin_modelo(conn: sqlite3.Connection) -> str:
    """Formula exacta de SHA256_INMUTABLES_10_SIN_MODELO (Regla 7)."""
    orig_rf = conn.row_factory
    try:
        conn.row_factory = None
        return hashlib.sha256(
            repr(
                conn.execute(
                    "SELECT id, ticker, fecha_prediccion, horizonte, senal, pct_estimado, "
                    "precio_entrada, score_motor, tilt, hora_calculo "
                    "FROM predicciones ORDER BY id ASC"
                ).fetchall()
            ).encode("utf-8")
        ).hexdigest()
    finally:
        conn.row_factory = orig_rf


def _sha256_inmutables_11_desde_12oct(conn: sqlite3.Connection) -> str:
    """Hash de las 11 columnas inmutables filtrado por WHERE fecha_prediccion >= '2026-10-12'."""
    orig_rf = conn.row_factory
    try:
        conn.row_factory = None
        return hashlib.sha256(
            repr(
                conn.execute(
                    "SELECT id, ticker, fecha_prediccion, horizonte, senal, pct_estimado, "
                    "precio_entrada, modelo, score_motor, tilt, hora_calculo "
                    "FROM predicciones WHERE fecha_prediccion >= '2026-10-12' ORDER BY id ASC"
                ).fetchall()
            ).encode("utf-8")
        ).hexdigest()
    finally:
        conn.row_factory = orig_rf


def _sha256_resueltas(conn: sqlite3.Connection) -> str:
    """Formula exacta de SHA256_RESUELTAS (Regla 7)."""
    orig_rf = conn.row_factory
    try:
        conn.row_factory = None
        return hashlib.sha256(
            repr(
                conn.execute(
                    "SELECT id, resuelta, precio_real, variacion_real, acierto "
                    "FROM predicciones WHERE resuelta != 0 ORDER BY id ASC"
                ).fetchall()
            ).encode("utf-8")
        ).hexdigest()
    finally:
        conn.row_factory = orig_rf


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Parchea get_ahora_madrid en los tres espacios de nombres requeridos."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _crear_batch_sintetico(tickers, fecha_sesion: str):
    """Crea un DataFrame MultiIndex (ticker, campo) con 45 barras habiles hasta fecha_sesion."""
    end_dt = pd.to_datetime(fecha_sesion)
    fechas = pd.bdate_range(end=end_dt, periods=45)
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)
    for idx_i, f_dt in enumerate(fechas):
        p = 10.0 + idx_i * 0.08
        for t in tickers:
            df.loc[f_dt, (t, "Open")] = p
            df.loc[f_dt, (t, "High")] = p * 1.01
            df.loc[f_dt, (t, "Low")] = p * 0.99
            df.loc[f_dt, (t, "Close")] = p
            df.loc[f_dt, (t, "Volume")] = 60000.0
    return df


class TestC06EsquemaV2(unittest.TestCase):
    """Suite de pruebas para C-06a: esquema v2 y migracion transaccional."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_c06_")
        self.temp_db = os.path.join(self.temp_dir, "test_synthetic_c06.db")
        self.orig_db_path = database.connection.DB_PATH
        database.connection.DB_PATH = self.temp_db
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

    def tearDown(self):
        database.connection.DB_PATH = self.orig_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

    def _crear_bd_v1_sintetica(self):
        """Crea una tabla predicciones en esquema v1 con filas sinteticas antes y despues del 12-oct."""
        conn = sqlite3.connect(self.temp_db)
        try:
            conn.executescript(_DDL_V1)
            filas_v1 = [
                (
                    101, "SAN.MC", "Banco Santander", "2026-10-02", "1D", 1, "2026-10-05",
                    "ALCISTA", 1.25, 1.5, 68.0, 4.50, 0.82, "v5-frozen",
                    1, "2026-10-05", 4.60, 2.2222, 1, 0.9722, "vencida", 89.2,
                    1.21, 0.8, 0.015, 0.4, 0.5, 0.9, 1.1, 28.5, 1.3, "ALCISTA",
                    2.1, 0.0, 0.2, "2026-10-02T19:00:00", "v1-dynamic-zone", 1,
                ),
                (
                    205, "ECR.MC", "Ercros", "2026-09-22", "1D", 1, "2026-09-23",
                    "LATERAL", 0.10, 1.2, 52.0, 3.49, 0.05, "v5-frozen",
                    2, "2026-10-19", None, None, None, None, "sin_cotizacion", None,
                    0.10, 0.8, 0.012, 0.0, 0.1, 0.0, 0.1, 18.0, 0.8, "NEUTRA",
                    -0.5, 0.0, 0.0, "2026-10-05T00:07:04", "v1-dynamic-zone", 0,
                ),
                (
                    310, "BBVA.MC", "BBVA", "2026-10-15", "1M", 20, "2026-11-12",
                    "BAJISTA", -3.40, 4.1, 71.5, 9.20, -0.95, "v5-frozen",
                    0, None, None, None, None, None, None, None,
                    -3.40, 3.0, 0.018, -0.6, -0.8, -1.1, -1.2, 31.0, 1.6, "BAJISTA",
                    -3.2, -0.3, -0.4, "2026-10-15T18:45:10", "v1-dynamic-zone", 0,
                ),
                (
                    412, "ITX.MC", "Inditex", "2026-10-12", "1D", 1, "2026-10-13",
                    "ALCISTA", 1.10, 1.4, 66.0, 50.00, 0.78, "v5-frozen",
                    1, "2026-10-13", 50.60, 1.2000, 1, 0.1000, "vencida", 92.0,
                    1.10, 0.8, 0.013, 0.5, 0.4, 0.8, 0.9, 25.0, 1.2, "ALCISTA",
                    1.5, 0.1, 0.1, "2026-10-12T18:40:00", "v1-dynamic-zone", 0,
                ),
            ]
            placeholders = ",".join(["?"] * 38)
            conn.executemany(
                f"INSERT INTO predicciones VALUES ({placeholders})",
                filas_v1,
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_migracion_v1_a_v2_sintetica_e_idempotencia(self):
        """
        (a) Migracion de tabla v1 con datos sinteticos a v2 via migrar_predicciones_v2:
        conserva recuento e ids, etiqueta < '2026-10-12' como 'legacy-pre-freeze' y
        mantiene 'v5-frozen' desde '2026-10-12', verifica SHA256_INMUTABLES_10_SIN_MODELO,
        SHA256_INMUTABLES_11_DESDE_12OCT y SHA256_RESUELTAS identicos, deja las 4
        columnas v2 en NULL, y una segunda llamada es idempotente (migrada=False).
        """
        self._crear_bd_v1_sintetica()

        conn = sqlite3.connect(self.temp_db)
        try:
            ids_antes = [r[0] for r in conn.execute("SELECT id FROM predicciones ORDER BY id ASC").fetchall()]
            h_inm_11_antes = _sha256_inmutables_11(conn)
            h_inm_10_antes = _sha256_inmutables_10_sin_modelo(conn)
            h_inm_11_ge_antes = _sha256_inmutables_11_desde_12oct(conn)
            h_res_antes = _sha256_resueltas(conn)
            cols_antes = [r[1] for r in conn.execute("PRAGMA table_info(predicciones)").fetchall()]
            self.assertEqual(len(cols_antes), 38)
        finally:
            conn.close()

        res1 = db_pred.migrar_predicciones_v2(db_path=self.temp_db)
        self.assertTrue(res1["migrada"])
        self.assertEqual(res1["filas_antes"], 4)
        self.assertEqual(res1["filas_despues"], 4)
        self.assertEqual(res1["filas_legacy_marcadas"], 2)
        self.assertEqual(res1["hash_inmutables_antes"], h_inm_11_antes)
        self.assertNotEqual(res1["hash_inmutables_despues"], h_inm_11_antes)
        self.assertEqual(res1["hash_inmutables_10_sin_modelo_antes"], h_inm_10_antes)
        self.assertEqual(res1["hash_inmutables_10_sin_modelo_despues"], h_inm_10_antes)
        self.assertEqual(res1["hash_inmutables_11_desde_12oct_antes"], h_inm_11_ge_antes)
        self.assertEqual(res1["hash_inmutables_11_desde_12oct_despues"], h_inm_11_ge_antes)
        self.assertEqual(res1["hash_resueltas_antes"], h_res_antes)
        self.assertEqual(res1["hash_resueltas_despues"], h_res_antes)

        conn = sqlite3.connect(self.temp_db)
        try:
            ids_despues = [r[0] for r in conn.execute("SELECT id FROM predicciones ORDER BY id ASC").fetchall()]
            self.assertEqual(ids_despues, ids_antes)
            self.assertEqual(ids_despues, [101, 205, 310, 412])
            self.assertEqual(_sha256_inmutables_10_sin_modelo(conn), h_inm_10_antes)
            self.assertEqual(_sha256_inmutables_11_desde_12oct(conn), h_inm_11_ge_antes)
            self.assertEqual(_sha256_resueltas(conn), h_res_antes)

            modelos_por_id = conn.execute(
                "SELECT id, fecha_prediccion, modelo FROM predicciones ORDER BY id ASC"
            ).fetchall()
            self.assertEqual(
                modelos_por_id,
                [
                    (101, "2026-10-02", "legacy-pre-freeze"),
                    (205, "2026-09-22", "legacy-pre-freeze"),
                    (310, "2026-10-15", "v5-frozen"),
                    (412, "2026-10-12", "v5-frozen"),
                ],
            )

            cols_despues = [r[1] for r in conn.execute("PRAGMA table_info(predicciones)").fetchall()]
            self.assertEqual(len(cols_despues), 42)
            self.assertEqual(cols_despues[:38], cols_antes)
            self.assertEqual(
                cols_despues[38:],
                ["emitida_en", "params_hash", "score_bruto", "features_json"],
            )

            nuevas_vals = conn.execute(
                "SELECT emitida_en, params_hash, score_bruto, features_json FROM predicciones ORDER BY id ASC"
            ).fetchall()
            for row_nv in nuevas_vals:
                self.assertEqual(row_nv, (None, None, None, None))

            idx_names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'predicciones'"
                ).fetchall()
            }
            self.assertIn("idx_pred_fecha_modelo", idx_names)
            self.assertIn("idx_pred_resuelta_fecha_obj", idx_names)
            self.assertIn("idx_pred_ticker_hz_fecha", idx_names)
        finally:
            conn.close()

        # Verificar filtrado de obtener_resueltas y obtener_metricas sobre la BD sintetica migrada
        df_res_no_leg = db_pred.obtener_resueltas(incluir_legacy=False)
        self.assertEqual(df_res_no_leg["id"].tolist(), [412])
        df_res_con_leg = db_pred.obtener_resueltas(incluir_legacy=True)
        self.assertEqual(sorted(df_res_con_leg["id"].tolist()), [101, 412])

        m_no_leg = db_pred.obtener_metricas(incluir_legacy=False)
        self.assertEqual(m_no_leg["total_db"], 4)
        self.assertEqual(m_no_leg["resueltas"], 1)
        self.assertEqual(m_no_leg["legacy_resueltas"], 1)
        self.assertEqual(m_no_leg["sin_cotizacion"], 0)

        m_con_leg = db_pred.obtener_metricas(incluir_legacy=True)
        self.assertEqual(m_con_leg["total_db"], 4)
        self.assertEqual(m_con_leg["resueltas"], 2)
        self.assertEqual(m_con_leg["legacy_resueltas"], 1)
        self.assertEqual(m_con_leg["sin_cotizacion"], 1)

        # Segunda llamada: idempotente (migrada=False)
        res2 = db_pred.migrar_predicciones_v2(db_path=self.temp_db)
        self.assertEqual(res2, {"migrada": False, "filas_antes": 4, "filas_despues": 4})

    def test_a_rollback_ante_error_en_migracion(self):
        """
        Verifica que si ocurre un fallo de verificacion dentro de migrar_predicciones_v2
        (en cualquiera de las comprobaciones de hash), se lanza RuntimeError y se hace
        ROLLBACK dejando la tabla v1 intacta.
        """
        self._crear_bd_v1_sintetica()
        conn_check = sqlite3.connect(self.temp_db)
        h_inm_orig = _sha256_inmutables_11(conn_check)
        conn_check.close()

        for fn_name in (
            "_calcular_sha256_inmutables_10_sin_modelo",
            "_calcular_sha256_inmutables_11_desde_corte",
            "_calcular_sha256_resueltas",
        ):
            orig_fn = getattr(db_pred, fn_name)

            def _fake_hash(conn, tabla="predicciones", _orig=orig_fn):
                if tabla == "predicciones_v2":
                    return "0" * 64
                return _orig(conn, tabla)

            with patch.object(db_pred, fn_name, side_effect=_fake_hash):
                with self.assertRaises(RuntimeError):
                    db_pred.migrar_predicciones_v2(db_path=self.temp_db)

            conn = sqlite3.connect(self.temp_db)
            try:
                self.assertFalse(db_pred._es_esquema_v2(conn))
                cols = [r[1] for r in conn.execute("PRAGMA table_info(predicciones)").fetchall()]
                self.assertEqual(len(cols), 38)
                self.assertEqual(_sha256_inmutables_11(conn), h_inm_orig)
                tbl_v2 = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='predicciones_v2'"
                ).fetchone()
                self.assertIsNone(tbl_v2)
            finally:
                conn.close()

    def test_b_convivencia_dos_modelos_y_conflicto_mismo_modelo(self):
        """
        (b) Convivencia de dos modelos ("v5-frozen" y "challenger-v1") para el mismo
        (ticker, horizonte, fecha_prediccion): ambos se insertan sin conflicto,
        mientras que insertar dos veces el mismo (ticker, horizonte, fecha_prediccion, modelo)
        es ignorado por ON CONFLICT IGNORE. Verifica tambien ejecutar_emision_diaria
        con un segundo modelo ("challenger-test").
        """
        db_pred.init_predicciones_table()

        df_radar_v5 = pd.DataFrame([
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
                "score_bruto_1D": 0.851234,
                "pct_raw_1D": 1.20,
            }
        ])
        df_radar_challenger = pd.DataFrame([
            {
                "ticker": "SAN.MC",
                "empresa": "Banco Santander",
                "precio": 4.50,
                "ultima_fecha": "2026-10-15",
                "senal_1D": "BAJISTA",
                "pct_1D": -1.10,
                "banda_1D": 1.50,
                "conf_1D": 62.0,
                "score_1D": -0.74,
                "score_bruto_1D": -0.74321,
                "pct_raw_1D": -1.10,
            }
        ])

        dt_emision = datetime(2026, 10, 15, 19, 10, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_emision):
            ins_v5 = db_pred.registrar_predicciones(
                df_radar_v5, fecha_prediccion="2026-10-15", modelo="v5-frozen"
            )
            ins_ch = db_pred.registrar_predicciones(
                df_radar_challenger, fecha_prediccion="2026-10-15", modelo="challenger-v1"
            )
            # Reintentar insertar el mismo (ticker, horizonte, fecha_prediccion, modelo)
            ins_v5_dup = db_pred.registrar_predicciones(
                df_radar_challenger, fecha_prediccion="2026-10-15", modelo="v5-frozen"
            )
            ins_ch_dup = db_pred.registrar_predicciones(
                df_radar_v5, fecha_prediccion="2026-10-15", modelo="challenger-v1"
            )

        self.assertEqual(ins_v5, 1)
        self.assertEqual(ins_ch, 1)
        self.assertEqual(ins_v5_dup, 0)
        self.assertEqual(ins_ch_dup, 0)

        conn = get_connection()
        try:
            # Verificar tambien INSERT directo contra la clausula UNIQUE(...) ON CONFLICT IGNORE de la tabla
            conn.execute(
                """
                INSERT INTO predicciones
                (ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo, senal, precio_entrada, modelo)
                VALUES ('SAN.MC', 'Banco Santander', '2026-10-15', '1D', 1, '2026-10-16', 'LATERAL', 99.0, 'v5-frozen')
                """
            )
            conn.commit()

            rows = conn.execute(
                "SELECT modelo, senal, precio_entrada FROM predicciones "
                "WHERE ticker = 'SAN.MC' AND fecha_prediccion = '2026-10-15' AND horizonte = '1D' "
                "ORDER BY id ASC"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual((rows[0][0], rows[0][1], rows[0][2]), ("v5-frozen", "ALCISTA", 4.50))
            self.assertEqual((rows[1][0], rows[1][1], rows[1][2]), ("challenger-v1", "BAJISTA", 4.50))
        finally:
            conn.close()

        # Verificar tambien convivencia en ejecutar_emision_diaria con challenger-test
        conn = get_connection()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS empresas (ticker TEXT PRIMARY KEY, nombre TEXT, activa INTEGER)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ranking_estado (ticker TEXT PRIMARY KEY, posicion INTEGER, score REAL)"
            )
            conn.execute("INSERT OR IGNORE INTO empresas VALUES ('BBVA.MC', 'BBVA', 1)")
            conn.execute("INSERT OR IGNORE INTO ranking_estado VALUES ('BBVA.MC', 1, 60.0)")
            conn.commit()
        finally:
            conn.close()

        df_batch = _crear_batch_sintetico(["BBVA.MC", "^IBEX"], "2026-10-16")
        dt_emision_16 = datetime(2026, 10, 16, 19, 10, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_emision_16), \
             patch.object(se, "descargar_price_batch", return_value=df_batch), \
             patch.object(se, "sincronizar_cache_panel_historico", return_value=False), \
             patch.object(se, "resolver_predicciones", return_value=0):
            res_champ = se.ejecutar_emision_diaria(force=False, verbose=False, modelo="v5-frozen")
            res_chall = se.ejecutar_emision_diaria(force=False, verbose=False, modelo="challenger-test")
            res_chall_2 = se.ejecutar_emision_diaria(force=False, verbose=False, modelo="challenger-test")

        self.assertEqual(res_champ["status"], "success")
        self.assertEqual(res_champ["insertadas"], 4)
        self.assertEqual(res_chall["status"], "success")
        self.assertEqual(res_chall["insertadas"], 4)
        self.assertEqual(res_chall_2["status"], "already_emitted")
        self.assertEqual(res_chall_2["insertadas"], 0)

    def test_c_columnas_v2_rellenadas_en_nuevas_emisiones(self):
        """
        (c) Las filas nuevas insertadas con registrar_predicciones rellenan
        emitida_en (en UTC ISO-8601 con reloj inyectado), params_hash,
        score_bruto y features_json valido.
        """
        db_pred.init_predicciones_table()

        df_batch = _crear_batch_sintetico(["SAN.MC", "^IBEX"], "2026-10-15")
        radar = sig_eng.calcular_radar_desde_batch(
            df_batch, ("SAN.MC",), ("Banco Santander",), (0.2,),
            sentimientos={"SAN.MC": 0.1},
            usar_calibracion=False,
        )
        self.assertIn("score_bruto", radar.columns)
        self.assertIn("score_bruto_1D", radar.columns)

        # Reloj en Madrid: 2026-10-15 19:40:12 CEST (UTC+2) -> 2026-10-15T17:40:12Z en UTC
        dt_madrid = datetime(2026, 10, 15, 19, 40, 12, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_madrid):
            n_ins = db_pred.registrar_predicciones(radar, fecha_prediccion="2026-10-15")

        self.assertEqual(n_ins, 4)

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT horizonte, emitida_en, params_hash, score_motor, score_bruto, features_json "
                "FROM predicciones WHERE ticker = 'SAN.MC' ORDER BY dias ASC"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 4)
        expected_ph = sig_eng.get_params_hash()
        self.assertEqual(len(expected_ph), 64)

        for row in rows:
            hz, emitida_en, params_hash, score_motor, score_bruto, features_json = (
                row[0], row[1], row[2], row[3], row[4], row[5]
            )
            self.assertEqual(emitida_en, "2026-10-15T17:40:12Z")
            self.assertEqual(params_hash, expected_ph)
            self.assertIsNotNone(score_bruto)
            self.assertTrue(np.isfinite(float(score_bruto)))
            self.assertAlmostEqual(round(float(score_bruto), 3), float(score_motor), places=3)

            self.assertIsNotNone(features_json)
            parsed = json.loads(features_json)
            self.assertIsInstance(parsed, dict)
            # Verificar que es JSON compacto y con claves ordenadas (sort_keys=True)
            re_dumped = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            self.assertEqual(features_json, re_dumped)
            for req_key in ("comp_rsi", "comp_macd", "comp_tendencia", "comp_momentum", "adx", "rvol", "score_bruto"):
                self.assertIn(req_key, parsed, f"Falta clave {req_key} en features_json para {hz}")

    def test_d_init_predicciones_table_sin_escrituras_en_v2(self):
        """
        (d) init_predicciones_table() sobre una BD ya en esquema v2 no realiza
        escrituras (comprobando total_changes de la conexion SQLite == 0).
        """
        db_pred.init_predicciones_table()

        conn = sqlite3.connect(self.temp_db)
        try:
            self.assertTrue(db_pred._es_esquema_v2(conn))
            changes_before = conn.total_changes
            data_ver_before = conn.execute("PRAGMA data_version").fetchone()[0]

            db_pred.init_predicciones_table(conn=conn)

            self.assertEqual(conn.total_changes, changes_before)
            self.assertEqual(conn.total_changes, 0)
            self.assertFalse(conn.in_transaction)
            data_ver_after = conn.execute("PRAGMA data_version").fetchone()[0]
            self.assertEqual(data_ver_after, data_ver_before)
        finally:
            conn.close()

        # Comprobar tambien cuando init_predicciones_table() abre su propia conexion via get_connection()
        conn_tracked = []
        orig_get_conn = db_pred.get_connection

        class _ConnProxy:
            def __init__(self, real_conn):
                self._c = real_conn
                self.closed_changes = None
                self.executed_sql = []

            def execute(self, sql, *args, **kwargs):
                self.executed_sql.append(sql.strip())
                return self._c.execute(sql, *args, **kwargs)

            def executescript(self, *args, **kwargs):
                self.executed_sql.append("EXECUTESCRIPT")
                return self._c.executescript(*args, **kwargs)

            def commit(self):
                self.executed_sql.append("COMMIT")
                return self._c.commit()

            def close(self):
                self.closed_changes = self._c.total_changes
                return self._c.close()

            def __getattr__(self, name):
                return getattr(self._c, name)

        def _wrapped_get_conn():
            p = _ConnProxy(orig_get_conn())
            conn_tracked.append(p)
            return p

        with patch.object(db_pred, "get_connection", side_effect=_wrapped_get_conn):
            db_pred.init_predicciones_table()

        self.assertEqual(len(conn_tracked), 1)
        proxy = conn_tracked[0]
        self.assertEqual(proxy.closed_changes, 0)
        for sql_stmt in proxy.executed_sql:
            upper_s = sql_stmt.upper()
            self.assertFalse(upper_s.startswith("CREATE"))
            self.assertFalse(upper_s.startswith("ALTER"))
            self.assertFalse(upper_s.startswith("UPDATE"))
            self.assertNotEqual(upper_s, "COMMIT")
            self.assertNotEqual(upper_s, "EXECUTESCRIPT")

    @unittest.skipUnless(os.path.exists(REAL_DB_PATH), "stocklab.db no disponible")
    def test_e_ensayo_migracion_sobre_copia_de_stocklab_db(self):
        """
        (e) Ensayo sobre copia de produccion: copia stocklab.db (abierta en mode=ro)
        a un fichero temporal en tempfile.TemporaryDirectory, ejecuta migrar_predicciones_v2
        sobre la copia temporal y verifica:
          * total_antes >= 4960 y filas_antes == filas_despues == total_antes,
          * las 1384 filas con fecha_prediccion < '2026-10-12' quedan con modelo == 'legacy-pre-freeze',
          * las (total_antes - 1384) filas con fecha_prediccion >= '2026-10-12' mantienen modelo == 'v5-frozen',
          * los 4 hashes calculados sobre la copia antes y despues coinciden al 100% entre si y con src_conn,
          * obtener_resueltas(incluir_legacy=False) y obtener_metricas(incluir_legacy=False)
            excluyen las filas < '2026-10-12', mientras que con incluir_legacy=True
            incluyen las filas legacy.
        Si stocklab.db ya esta en esquema v2 (tras la Parte Final), comprueba idempotencia
        y hashes sobre la copia v2 y ademas ensaya la migracion v1 -> v2 sobre una copia
        reconstruida en v1.
        """
        with open(REAL_DB_PATH, "rb") as f:
            sha_db_real_antes = hashlib.sha256(f.read()).hexdigest()

        with tempfile.TemporaryDirectory(prefix="ensayo_c06_") as tmp_dir:
            copia_db_path = os.path.join(tmp_dir, "stocklab_copia_ensayo.db")

            # Copiar stocklab.db abierta estrictamente en mode=ro via sqlite3 backup API
            src_conn = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
            try:
                es_v2_origen = db_pred._es_esquema_v2(src_conn)
                total_antes = src_conn.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
                h_inm_11_real = _sha256_inmutables_11(src_conn)
                h_inm_10_real = _sha256_inmutables_10_sin_modelo(src_conn)
                h_inm_11_ge_real = _sha256_inmutables_11_desde_12oct(src_conn)
                h_res_real = _sha256_resueltas(src_conn)

                dst_conn = sqlite3.connect(copia_db_path)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

            self.assertGreaterEqual(total_antes, 4960)

            conn_copia_pre = sqlite3.connect(copia_db_path)
            try:
                filas_antes = conn_copia_pre.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
                h_inm_11_copia_antes = _sha256_inmutables_11(conn_copia_pre)
                h_inm_10_copia_antes = _sha256_inmutables_10_sin_modelo(conn_copia_pre)
                h_inm_11_ge_copia_antes = _sha256_inmutables_11_desde_12oct(conn_copia_pre)
                h_res_copia_antes = _sha256_resueltas(conn_copia_pre)
            finally:
                conn_copia_pre.close()

            self.assertEqual(filas_antes, total_antes)
            self.assertEqual(h_inm_11_copia_antes, h_inm_11_real)
            self.assertEqual(h_inm_10_copia_antes, h_inm_10_real)
            self.assertEqual(h_inm_11_ge_copia_antes, h_inm_11_ge_real)
            self.assertEqual(h_res_copia_antes, h_res_real)

            if not es_v2_origen:
                res_mig = db_pred.migrar_predicciones_v2(db_path=copia_db_path)
                self.assertTrue(res_mig["migrada"])
                self.assertEqual(res_mig["filas_antes"], total_antes)
                self.assertEqual(res_mig["filas_despues"], total_antes)
                self.assertEqual(res_mig["filas_legacy_marcadas"], 1384)
                self.assertEqual(res_mig["hash_inmutables_antes"], h_inm_11_real)
                self.assertNotEqual(res_mig["hash_inmutables_despues"], h_inm_11_real)
                self.assertEqual(res_mig["hash_inmutables_10_sin_modelo_antes"], h_inm_10_real)
                self.assertEqual(res_mig["hash_inmutables_10_sin_modelo_despues"], h_inm_10_real)
                self.assertEqual(res_mig["hash_inmutables_11_desde_12oct_antes"], h_inm_11_ge_real)
                self.assertEqual(res_mig["hash_inmutables_11_desde_12oct_despues"], h_inm_11_ge_real)
                self.assertEqual(res_mig["hash_resueltas_antes"], h_res_real)
                self.assertEqual(res_mig["hash_resueltas_despues"], h_res_real)
            else:
                # Si stocklab.db ya esta en v2, primero verificar idempotencia directa
                res_idemp_v2 = db_pred.migrar_predicciones_v2(db_path=copia_db_path)
                self.assertEqual(
                    res_idemp_v2,
                    {"migrada": False, "filas_antes": total_antes, "filas_despues": total_antes},
                )
                # Ademas ensayar la migracion desde una copia revertida a v1 (con modelo='v5-frozen')
                copia_v1_path = os.path.join(tmp_dir, "stocklab_copia_v1.db")
                shutil.copy2(copia_db_path, copia_v1_path)
                conn_v1 = sqlite3.connect(copia_v1_path)
                try:
                    cols_38_exprs = [
                        "'v5-frozen' AS modelo" if c == "modelo" else c
                        for c in db_pred._COLUMNAS_V1_38
                    ]
                    cols_38_csv = ", ".join(db_pred._COLUMNAS_V1_38)
                    sel_38_csv = ", ".join(cols_38_exprs)
                    conn_v1.execute(
                        _DDL_V1.replace("CREATE TABLE predicciones", "CREATE TABLE predicciones_v1_tmp")
                    )
                    conn_v1.execute(
                        f"INSERT INTO predicciones_v1_tmp ({cols_38_csv}) "
                        f"SELECT {sel_38_csv} FROM predicciones ORDER BY id ASC"
                    )
                    conn_v1.execute("DROP TABLE predicciones")
                    conn_v1.execute("ALTER TABLE predicciones_v1_tmp RENAME TO predicciones")
                    conn_v1.commit()
                    self.assertFalse(db_pred._es_esquema_v2(conn_v1))
                finally:
                    conn_v1.close()

                res_mig_v1 = db_pred.migrar_predicciones_v2(db_path=copia_v1_path)
                self.assertTrue(res_mig_v1["migrada"])
                self.assertEqual(res_mig_v1["filas_antes"], total_antes)
                self.assertEqual(res_mig_v1["filas_despues"], total_antes)
                self.assertEqual(res_mig_v1["filas_legacy_marcadas"], 1384)
                self.assertEqual(res_mig_v1["hash_inmutables_despues"], h_inm_11_real)
                self.assertEqual(res_mig_v1["hash_inmutables_10_sin_modelo_despues"], h_inm_10_real)
                self.assertEqual(res_mig_v1["hash_inmutables_11_desde_12oct_despues"], h_inm_11_ge_real)
                self.assertEqual(res_mig_v1["hash_resueltas_despues"], h_res_real)

            # Verificar sobre la copia migrada (en v2)
            conn_copia = sqlite3.connect(copia_db_path)
            try:
                self.assertTrue(db_pred._es_esquema_v2(conn_copia))
                filas_despues = conn_copia.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
                self.assertEqual(filas_antes, total_antes)
                self.assertEqual(filas_despues, total_antes)

                h_inm_11_copia_despues = _sha256_inmutables_11(conn_copia)
                h_inm_10_copia_despues = _sha256_inmutables_10_sin_modelo(conn_copia)
                h_inm_11_ge_copia_despues = _sha256_inmutables_11_desde_12oct(conn_copia)
                h_res_copia_despues = _sha256_resueltas(conn_copia)

                self.assertEqual(h_inm_10_copia_despues, h_inm_10_copia_antes)
                self.assertEqual(h_inm_10_copia_despues, h_inm_10_real)
                self.assertEqual(h_inm_11_ge_copia_despues, h_inm_11_ge_copia_antes)
                self.assertEqual(h_inm_11_ge_copia_despues, h_inm_11_ge_real)
                self.assertEqual(h_res_copia_despues, h_res_copia_antes)
                self.assertEqual(h_res_copia_despues, h_res_real)
                if es_v2_origen:
                    self.assertEqual(h_inm_11_copia_despues, h_inm_11_copia_antes)
                    self.assertEqual(h_inm_11_copia_despues, h_inm_11_real)

                # Verificar etiquetado legacy (< '2026-10-12') vs v5-frozen (>= '2026-10-12')
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

                grupos_modelo = conn_copia.execute(
                    "SELECT modelo, COUNT(*) FROM predicciones GROUP BY modelo ORDER BY modelo"
                ).fetchall()
                self.assertEqual(
                    grupos_modelo,
                    [("legacy-pre-freeze", 1384), ("v5-frozen", total_antes - 1384)],
                )

                n_no_nulas_migradas_4960 = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones "
                    "WHERE id <= 4960 AND (emitida_en IS NOT NULL OR params_hash IS NOT NULL "
                    "OR score_bruto IS NOT NULL OR features_json IS NOT NULL)"
                ).fetchone()[0]
                self.assertEqual(n_no_nulas_migradas_4960, 0)

                exp_res_sin_leg = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones "
                    "WHERE resuelta = 1 "
                    "AND fecha_prediccion >= '2026-10-12' AND modelo != 'legacy-pre-freeze'"
                ).fetchone()[0]
                exp_res_con_leg = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones WHERE resuelta = 1"
                ).fetchone()[0]
                exp_res_legacy = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones "
                    "WHERE resuelta = 1 AND modelo = 'legacy-pre-freeze'"
                ).fetchone()[0]
                self.assertGreaterEqual(exp_res_legacy, 1032)
                exp_sin_cot_sin_leg = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones "
                    "WHERE resuelta = 2 "
                    "AND fecha_prediccion >= '2026-10-12' AND modelo != 'legacy-pre-freeze'"
                ).fetchone()[0]
                exp_sin_cot_con_leg = conn_copia.execute(
                    "SELECT COUNT(*) FROM predicciones WHERE resuelta = 2"
                ).fetchone()[0]

                # Segunda llamada idempotente sobre la copia
                res_idemp = db_pred.migrar_predicciones_v2(conn=conn_copia)
                self.assertEqual(
                    res_idemp,
                    {"migrada": False, "filas_antes": total_antes, "filas_despues": total_antes},
                )
            finally:
                conn_copia.close()

            # Verificar obtener_resueltas, obtener_historial y obtener_metricas sobre la copia migrada
            prev_db_path = database.connection.DB_PATH
            database.connection.DB_PATH = copia_db_path
            try:
                lim_total = max(10000, total_antes + 1000)
                df_res_sin_leg = db_pred.obtener_resueltas(limite=lim_total, incluir_legacy=False)
                self.assertEqual(len(df_res_sin_leg), exp_res_sin_leg)
                self.assertEqual(int((df_res_sin_leg["fecha_prediccion"] < "2026-10-12").sum()), 0)
                self.assertEqual(set(df_res_sin_leg["modelo"].unique()), {"v5-frozen"})

                df_res_con_leg = db_pred.obtener_resueltas(limite=lim_total, incluir_legacy=True)
                self.assertEqual(len(df_res_con_leg), exp_res_con_leg)
                self.assertEqual(int((df_res_con_leg["fecha_prediccion"] < "2026-10-12").sum()), exp_res_legacy)
                self.assertEqual(
                    int((df_res_con_leg["fecha_prediccion"] >= "2026-10-12").sum()),
                    exp_res_sin_leg,
                )

                df_hist_sin_leg = db_pred.obtener_historial(
                    limite=lim_total, solo_resueltas=False, incluir_legacy=False
                )
                self.assertEqual(len(df_hist_sin_leg), total_antes - 1384)
                self.assertEqual(int((df_hist_sin_leg["fecha_prediccion"] < "2026-10-12").sum()), 0)

                df_hist_con_leg = db_pred.obtener_historial(
                    limite=lim_total, solo_resueltas=False, incluir_legacy=True
                )
                self.assertEqual(len(df_hist_con_leg), total_antes)

                m_sin_leg = db_pred.obtener_metricas(incluir_legacy=False)
                self.assertEqual(m_sin_leg["total_db"], total_antes)
                self.assertEqual(m_sin_leg["resueltas"], exp_res_sin_leg)
                self.assertEqual(m_sin_leg["legacy_resueltas"], exp_res_legacy)
                self.assertEqual(m_sin_leg["sin_cotizacion"], exp_sin_cot_sin_leg)
                self.assertEqual(
                    sum(v["n"] for v in m_sin_leg["por_horizonte"].values()),
                    exp_res_sin_leg,
                )

                m_con_leg = db_pred.obtener_metricas(incluir_legacy=True)
                self.assertEqual(m_con_leg["total_db"], total_antes)
                self.assertEqual(m_con_leg["resueltas"], exp_res_con_leg)
                self.assertEqual(m_con_leg["legacy_resueltas"], exp_res_legacy)
                self.assertEqual(m_con_leg["sin_cotizacion"], exp_sin_cot_con_leg)
                self.assertEqual(
                    sum(v["n"] for v in m_con_leg["por_horizonte"].values()),
                    exp_res_con_leg,
                )
            finally:
                database.connection.DB_PATH = prev_db_path

        # Verificar que stocklab.db real no ha sido tocada en absoluto
        with open(REAL_DB_PATH, "rb") as f:
            sha_db_real_despues = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(sha_db_real_despues, sha_db_real_antes)


if __name__ == "__main__":
    unittest.main()
