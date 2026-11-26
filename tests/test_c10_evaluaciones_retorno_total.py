"""
Tests para el Paso C-10a: Tabla paralela evaluaciones y resolucion en retorno total y retorno de precio
=======================================================================================================
Verifica sobre base de datos SQLite temporal sintetica en tempfile.TemporaryDirectory,
sin llamadas reales a Internet/Yahoo Finance y con el reloj parcheado en
utils.market_calendar, services.emision y database.predicciones:
  (a) Creacion idempotente de evaluaciones e indices (init_evaluaciones_table hace
      total_changes == 0 en la segunda llamada) y la tabla predicciones no sufre
      ningun cambio.
  (b) Caso SIN dividendo (Close == Adj Close en entrada 10.00 y salida 10.20,
      +2.0% en horizonte 1D con umbral 0.8%): 'c10_precio_cierre' y 'c10_retorno_total'
      dan exactamente la misma variacion (+2.0%), diferencia_dividendo_pct == 0.0,
      y ambas coinciden en acierto.
  (c) Caso CON dividendo dentro del horizonte: Close = 10.00 en fecha_prediccion,
      dividendo de 0.50 EUR antes de fecha_objetivo, Close = 9.80 en fecha_objetivo
      (-2.00% en precio sin ajustar), mientras Adj Close pasa de 9.50 a 9.80
      (+3.1579% en retorno total):
      - 'c10_precio_cierre' registra variacion_pct = -2.0 (clase_real = 'BAJISTA',
        acierto = 0 para senal ALCISTA y acierto = 1 para senal BAJISTA).
      - 'c10_retorno_total' registra variacion_pct = +3.1579 (clase_real = 'ALCISTA',
        acierto = 1 para senal ALCISTA y acierto = 0 para senal BAJISTA).
      - diferencia_dividendo_pct == +5.1579 y obtener_evaluaciones_comparadas(solo_con_dividendo=True)
        devuelve esa senal mostrando ambas variaciones y su diferencia.
  (d) Caso de sesgo evitado cuando el dividendo ocurre DESPUES de fecha_objetivo:
      ambos extremos (fecha_prediccion y fecha_objetivo) se toman de la misma descarga
      auto_adjust=False, por lo que el factor de ajuste posterior multiplica por igual
      a adj_in y adj_out, haciendo coincidir adj_out / adj_in - 1 con close_out / close_in - 1
      (diferencia_dividendo_pct == 0.0).
  (e) Inmutabilidad de predicciones: tras ejecutar evaluar_predicciones_retorno_total,
      SHA256_INMUTABLES_11 y SHA256_RESUELTAS de predicciones en la BD de test son
      100% identicos al estado previo, e invocar una segunda vez
      evaluar_predicciones_retorno_total es idempotente (evaluadas_precio == 0,
      evaluadas_total == 0, sin llamadas a red).
  (f) Enganche seguro en services.emision.ejecutar_emision_diaria: evalua senales
      resueltas y absorbe fallos de descarga auto_adjust=False sin romper la emision.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import numpy as np
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


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Parchea get_ahora_madrid en los tres espacios de nombres obligatorios."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _sha256_inmutables_11(conn: sqlite3.Connection) -> str:
    """Calcula SHA256_INMUTABLES_11 oficial."""
    orig_rf = conn.row_factory
    try:
        conn.row_factory = None
        return db_pred._calcular_sha256_inmutables(conn, "predicciones")
    finally:
        conn.row_factory = orig_rf


def _sha256_resueltas(conn: sqlite3.Connection) -> str:
    """Calcula SHA256_RESUELTAS oficial."""
    orig_rf = conn.row_factory
    try:
        conn.row_factory = None
        return db_pred._calcular_sha256_resueltas(conn, "predicciones")
    finally:
        conn.row_factory = orig_rf


def _sha256_todas_columnas_predicciones(conn: sqlite3.Connection) -> str:
    """Calcula SHA-256 de todas las filas y columnas de la tabla predicciones."""
    orig_rf = conn.row_factory
    try:
        conn.row_factory = None
        rows = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
        return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
    finally:
        conn.row_factory = orig_rf


def _construir_df_raw_multiindex(datos_por_ticker: dict) -> pd.DataFrame:
    """
    Construye un DataFrame MultiIndex (ticker, metrica) simulando yfinance.download
    con auto_adjust=False y group_by='ticker'.
    datos_por_ticker: {ticker: {fecha_iso: (close, adj_close)}}
    """
    fechas_set = set()
    for mapa_fechas in datos_por_ticker.values():
        fechas_set.update(mapa_fechas.keys())
    fechas_idx = pd.to_datetime(sorted(fechas_set))

    tickers = sorted(datos_por_ticker.keys())
    cols = pd.MultiIndex.from_product(
        [tickers, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    )
    df = pd.DataFrame(np.nan, index=fechas_idx, columns=cols, dtype=float)

    for ticker, mapa_fechas in datos_por_ticker.items():
        for f_str, (c_val, adj_val) in mapa_fechas.items():
            f_dt = pd.to_datetime(f_str)
            df.loc[f_dt, (ticker, "Open")] = float(c_val)
            df.loc[f_dt, (ticker, "High")] = float(c_val) * 1.01
            df.loc[f_dt, (ticker, "Low")] = float(c_val) * 0.99
            df.loc[f_dt, (ticker, "Close")] = float(c_val)
            df.loc[f_dt, (ticker, "Adj Close")] = float(adj_val)
            df.loc[f_dt, (ticker, "Volume")] = 100000.0
    return df


class TestC10EvaluacionesRetornoTotal(unittest.TestCase):
    """Suite unitaria para la tabla evaluaciones y calculo en retorno de precio y retorno total."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(prefix="test_c10_")
        self.temp_dir = self._tmp_ctx.name
        self.temp_db = os.path.join(self.temp_dir, "test_synthetic_c10.db")
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
        self._tmp_ctx.cleanup()
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

    def _insertar_prediccion_resuelta(
        self,
        ticker: str = "SAN.MC",
        empresa: str = "Banco Santander",
        fecha_prediccion: str = "2026-10-14",
        horizonte: str = "1D",
        dias: int = 1,
        fecha_objetivo: str = "2026-10-15",
        senal: str = "ALCISTA",
        pct_estimado: float = 2.5,
        banda_pct: float = 1.5,
        precio_entrada: float = 10.0,
        precio_real: float = 10.2,
        variacion_real: float = 2.0,
        acierto: int = 1,
        error_pct: float = 0.5,
        modelo: str = "v5-frozen",
        resuelta: int = 1,
    ) -> int:
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, pct_estimado, banda_pct, confianza, precio_entrada,
                    score_motor, modelo, resuelta, fecha_resolucion, precio_real,
                    variacion_real, acierto, error_pct, motivo_resolucion, score_calidad,
                    pct_estimado_raw, umbral_zona, sigma, comp_rsi, comp_macd,
                    comp_tendencia, comp_momentum, adx, rvol, marea, rs_ibex,
                    sentimiento, tilt, hora_calculo, regla_eval, archivada,
                    emitida_en, params_hash, score_bruto, features_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, 75.0, ?,
                    68.0, ?, ?, ?, ?,
                    ?, ?, ?, 'vencida', 80.0,
                    ?, 0.8, 0.015, 55.0, 0.2,
                    1.0, 0.5, 25.0, 1.1, 'ALCISTA', 0.3,
                    0.0, 0.1, '2026-10-14T19:00:00', 'v1-dynamic-zone', 0,
                    '2026-10-14T17:00:00Z', 'hash_c10', 68.0, '{"adx":25.0}'
                )
                """,
                (
                    ticker,
                    empresa,
                    fecha_prediccion,
                    horizonte,
                    dias,
                    fecha_objetivo,
                    senal,
                    pct_estimado,
                    banda_pct,
                    precio_entrada,
                    modelo,
                    resuelta,
                    fecha_objetivo if resuelta == 1 else None,
                    precio_real if resuelta == 1 else None,
                    variacion_real if resuelta == 1 else None,
                    acierto if resuelta == 1 else None,
                    error_pct if resuelta == 1 else None,
                    pct_estimado,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_a_creacion_idempotente_evaluaciones_e_indices_sin_tocar_predicciones(self):
        """
        (a) Verifica que init_evaluaciones_table crea la tabla evaluaciones y sus 2 indices,
        hace total_changes == 0 en la segunda llamada, y la tabla predicciones no sufre
        ningun cambio.
        """
        self._insertar_prediccion_resuelta()

        conn = get_connection()
        try:
            ddl_pred_antes = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='predicciones'"
            ).fetchone()[0]
            h11_antes = _sha256_inmutables_11(conn)
            hres_antes = _sha256_resueltas(conn)
            htotal_antes = _sha256_todas_columnas_predicciones(conn)

            creado_1 = db_pred.init_evaluaciones_table(conn=conn)
            self.assertTrue(creado_1)

            # Verificar que la tabla evaluaciones y sus 2 indices existen en sqlite_master
            row_eval = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='evaluaciones'"
            ).fetchone()
            self.assertIsNotNone(row_eval)
            sql_eval_norm = " ".join(row_eval[0].split())
            self.assertIn("UNIQUE(prediccion_id, regla) ON CONFLICT IGNORE", sql_eval_norm)

            cols_info = conn.execute("PRAGMA table_info(evaluaciones)").fetchall()
            col_names = [r[1] for r in cols_info]
            expected_cols = [
                "id",
                "prediccion_id",
                "regla",
                "precio_entrada_eval",
                "precio_salida_eval",
                "variacion_pct",
                "umbral_aplicado",
                "clase_real",
                "acierto",
                "acierto_signo",
                "error_pct",
                "diferencia_dividendo_pct",
                "evaluada_en",
            ]
            self.assertEqual(col_names, expected_cols)

            indices = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='evaluaciones'"
                ).fetchall()
            }
            self.assertIn("idx_eval_prediccion_id", indices)
            self.assertIn("idx_eval_regla_acierto", indices)

            # Segunda llamada: debe ser no-op en solo lectura (total_changes == 0)
            changes_before = conn.total_changes
            creado_2 = db_pred.init_evaluaciones_table(conn=conn)
            changes_after = conn.total_changes
            self.assertFalse(creado_2)
            self.assertEqual(changes_after - changes_before, 0)

            # La tabla predicciones no sufre ningun cambio
            ddl_pred_despues = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='predicciones'"
            ).fetchone()[0]
            self.assertEqual(ddl_pred_antes, ddl_pred_despues)
            self.assertEqual(h11_antes, _sha256_inmutables_11(conn))
            self.assertEqual(hres_antes, _sha256_resueltas(conn))
            self.assertEqual(htotal_antes, _sha256_todas_columnas_predicciones(conn))
        finally:
            conn.close()

    def test_b_caso_sin_dividendo_precio_y_retorno_total_coinciden(self):
        """
        (b) Caso SIN dividendo (Close == Adj Close en entrada 10.00 y salida 10.20,
        +2.0% en horizonte 1D con umbral 0.8%): 'c10_precio_cierre' y 'c10_retorno_total'
        dan exactamente la misma variacion (+2.0%), diferencia_dividendo_pct == 0.0,
        y ambas coinciden en acierto.
        """
        pid = self._insertar_prediccion_resuelta(
            ticker="BBVA.MC",
            empresa="BBVA",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=2.5,
            precio_entrada=10.00,
            precio_real=10.20,
            variacion_real=2.0,
            acierto=1,
            error_pct=0.5,
        )

        df_raw = _construir_df_raw_multiindex(
            {
                "BBVA.MC": {
                    "2026-10-14": (10.00, 10.00),
                    "2026-10-15": (10.20, 10.20),
                }
            }
        )

        dt_eval = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            res = db_pred.evaluar_predicciones_retorno_total(df_raw_ohlcv=df_raw)

        self.assertEqual(res["evaluadas_precio"], 1)
        self.assertEqual(res["evaluadas_total"], 1)
        self.assertEqual(res["pendientes"], 0)

        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT regla, precio_entrada_eval, precio_salida_eval, variacion_pct,
                       umbral_aplicado, clase_real, acierto, acierto_signo,
                       error_pct, diferencia_dividendo_pct, evaluada_en
                FROM evaluaciones
                WHERE prediccion_id = ?
                ORDER BY regla ASC
                """,
                (pid,),
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 2)
        by_rule = {r[0]: r for r in rows}

        r_precio = by_rule["c10_precio_cierre"]
        r_total = by_rule["c10_retorno_total"]

        self.assertAlmostEqual(r_precio[1], 10.00, places=4)
        self.assertAlmostEqual(r_precio[2], 10.20, places=4)
        self.assertAlmostEqual(r_precio[3], 2.0, places=4)
        self.assertAlmostEqual(r_precio[4], 0.8, places=4)
        self.assertEqual(r_precio[5], "ALCISTA")
        self.assertEqual(r_precio[6], 1)
        self.assertEqual(r_precio[7], 1)
        self.assertAlmostEqual(r_precio[8], 0.5, places=4)
        self.assertAlmostEqual(r_precio[9], 0.0, places=4)
        self.assertEqual(r_precio[10], "2026-10-15T17:00:00Z")

        self.assertAlmostEqual(r_total[1], 10.00, places=4)
        self.assertAlmostEqual(r_total[2], 10.20, places=4)
        self.assertAlmostEqual(r_total[3], 2.0, places=4)
        self.assertAlmostEqual(r_total[4], 0.8, places=4)
        self.assertEqual(r_total[5], "ALCISTA")
        self.assertEqual(r_total[6], 1)
        self.assertEqual(r_total[7], 1)
        self.assertAlmostEqual(r_total[8], 0.5, places=4)
        self.assertAlmostEqual(r_total[9], 0.0, places=4)
        self.assertEqual(r_total[10], "2026-10-15T17:00:00Z")

        df_comp = db_pred.obtener_evaluaciones_comparadas(solo_con_dividendo=False)
        self.assertEqual(len(df_comp), 1)
        self.assertAlmostEqual(float(df_comp.iloc[0]["var_precio_c10"]), 2.0, places=4)
        self.assertAlmostEqual(float(df_comp.iloc[0]["var_retorno_total_c10"]), 2.0, places=4)
        self.assertEqual(int(df_comp.iloc[0]["acierto_precio_c10"]), 1)
        self.assertEqual(int(df_comp.iloc[0]["acierto_retorno_total_c10"]), 1)
        self.assertAlmostEqual(float(df_comp.iloc[0]["diferencia_dividendo_pct"]), 0.0, places=4)

        df_div = db_pred.obtener_evaluaciones_comparadas(solo_con_dividendo=True)
        self.assertTrue(df_div.empty)

    def test_c_caso_con_dividendo_dentro_del_horizonte(self):
        """
        (c) Caso CON dividendo dentro del horizonte (criterio 'hecho cuando' de C-10):
        Un valor cotiza a Close = 10.00 en fecha_prediccion y reparte un dividendo de
        0.50 EUR antes de fecha_objetivo, cerrando en fecha_objetivo a Close = 9.80
        (-2.00% en precio sin ajustar), pero con el dividendo su Adj Close pasa de
        9.50 a 9.80 (+3.1579% en retorno total):
          - 'c10_precio_cierre' registra variacion_pct = -2.0 (clase_real = 'BAJISTA',
            acierto = 0 para senal ALCISTA).
          - 'c10_retorno_total' registra variacion_pct = +3.1579 (clase_real = 'ALCISTA',
            acierto = 1 para senal ALCISTA).
          - diferencia_dividendo_pct == +5.1579 y obtener_evaluaciones_comparadas(solo_con_dividendo=True)
            devuelve esa senal mostrando ambas variaciones y su diferencia.
        """
        # Senal ALCISTA que en precio sin ajustar parece fallar (-2.0%) pero con dividendo acierta (+3.1579%)
        pid_alc = self._insertar_prediccion_resuelta(
            ticker="TEF.MC",
            empresa="Telefonica",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=3.0,
            precio_entrada=10.00,
            precio_real=9.80,
            variacion_real=-2.0,
            acierto=0,
            error_pct=5.0,
            modelo="v5-frozen",
        )
        # Senal BAJISTA de un modelo challenger sobre el mismo evento corporativo
        pid_baj = self._insertar_prediccion_resuelta(
            ticker="TEF.MC",
            empresa="Telefonica",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="BAJISTA",
            pct_estimado=-2.0,
            precio_entrada=10.00,
            precio_real=9.80,
            variacion_real=-2.0,
            acierto=1,
            error_pct=0.0,
            modelo="challenger-v1",
        )
        # Senal sin dividendo en otro ticker para verificar el filtro solo_con_dividendo=True
        self._insertar_prediccion_resuelta(
            ticker="SAN.MC",
            empresa="Santander",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="LATERAL",
            pct_estimado=0.1,
            precio_entrada=5.00,
            precio_real=5.02,
            variacion_real=0.4,
            acierto=1,
            error_pct=0.3,
            modelo="v5-frozen",
        )

        df_raw = _construir_df_raw_multiindex(
            {
                "TEF.MC": {
                    "2026-10-14": (10.00, 9.50),
                    "2026-10-15": (9.80, 9.80),
                },
                "SAN.MC": {
                    "2026-10-14": (5.00, 5.00),
                    "2026-10-15": (5.02, 5.02),
                },
            }
        )

        dt_eval = datetime(2026, 10, 15, 19, 30, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            res = db_pred.evaluar_predicciones_retorno_total(df_raw_ohlcv=df_raw)

        self.assertEqual(res["evaluadas_precio"], 3)
        self.assertEqual(res["evaluadas_total"], 3)
        self.assertEqual(res["pendientes"], 0)

        conn = get_connection()
        try:
            rows_alc = {
                r[0]: r
                for r in conn.execute(
                    """
                    SELECT regla, precio_entrada_eval, precio_salida_eval, variacion_pct,
                           umbral_aplicado, clase_real, acierto, acierto_signo,
                           error_pct, diferencia_dividendo_pct
                    FROM evaluaciones
                    WHERE prediccion_id = ?
                    """,
                    (pid_alc,),
                ).fetchall()
            }
            rows_baj = {
                r[0]: r
                for r in conn.execute(
                    """
                    SELECT regla, variacion_pct, clase_real, acierto, acierto_signo,
                           diferencia_dividendo_pct
                    FROM evaluaciones
                    WHERE prediccion_id = ?
                    """,
                    (pid_baj,),
                ).fetchall()
            }
        finally:
            conn.close()

        # Verificacion de senal ALCISTA
        ev_p = rows_alc["c10_precio_cierre"]
        self.assertAlmostEqual(ev_p[1], 10.00, places=4)
        self.assertAlmostEqual(ev_p[2], 9.80, places=4)
        self.assertAlmostEqual(ev_p[3], -2.0, places=4)
        self.assertAlmostEqual(ev_p[4], 0.8, places=4)
        self.assertEqual(ev_p[5], "BAJISTA")
        self.assertEqual(ev_p[6], 0)
        self.assertEqual(ev_p[7], 0)
        self.assertAlmostEqual(ev_p[8], 5.0, places=4)
        self.assertAlmostEqual(ev_p[9], 0.0, places=4)

        ev_t = rows_alc["c10_retorno_total"]
        self.assertAlmostEqual(ev_t[1], 9.50, places=4)
        self.assertAlmostEqual(ev_t[2], 9.80, places=4)
        self.assertAlmostEqual(ev_t[3], 3.1579, places=4)
        self.assertAlmostEqual(ev_t[4], 0.8, places=4)
        self.assertEqual(ev_t[5], "ALCISTA")
        self.assertEqual(ev_t[6], 1)
        self.assertEqual(ev_t[7], 1)
        self.assertAlmostEqual(ev_t[8], 0.1579, places=4)
        self.assertAlmostEqual(ev_t[9], 5.1579, places=4)

        # Verificacion de senal BAJISTA (parece acertar en precio pero falla en retorno total)
        self.assertAlmostEqual(rows_baj["c10_precio_cierre"][1], -2.0, places=4)
        self.assertEqual(rows_baj["c10_precio_cierre"][2], "BAJISTA")
        self.assertEqual(rows_baj["c10_precio_cierre"][3], 1)
        self.assertEqual(rows_baj["c10_precio_cierre"][4], 1)

        self.assertAlmostEqual(rows_baj["c10_retorno_total"][1], 3.1579, places=4)
        self.assertEqual(rows_baj["c10_retorno_total"][2], "ALCISTA")
        self.assertEqual(rows_baj["c10_retorno_total"][3], 0)
        self.assertEqual(rows_baj["c10_retorno_total"][4], 0)
        self.assertAlmostEqual(rows_baj["c10_retorno_total"][5], 5.1579, places=4)

        # Consulta comparada filtrando solo_con_dividendo=True
        df_div = db_pred.obtener_evaluaciones_comparadas(solo_con_dividendo=True)
        self.assertEqual(len(df_div), 2)
        self.assertTrue((df_div["ticker"] == "TEF.MC").all())

        fila_alc = df_div.loc[df_div["id"] == pid_alc].iloc[0]
        self.assertAlmostEqual(float(fila_alc["variacion_real"]), -2.0, places=4)
        self.assertEqual(int(fila_alc["acierto"]), 0)
        self.assertAlmostEqual(float(fila_alc["var_precio_c10"]), -2.0, places=4)
        self.assertEqual(int(fila_alc["acierto_precio_c10"]), 0)
        self.assertAlmostEqual(float(fila_alc["var_retorno_total_c10"]), 3.1579, places=4)
        self.assertEqual(int(fila_alc["acierto_retorno_total_c10"]), 1)
        self.assertAlmostEqual(float(fila_alc["diferencia_dividendo_pct"]), 5.1579, places=4)

    def test_d_sesgo_evitado_cuando_dividendo_ocurre_despues_de_fecha_objetivo(self):
        """
        (d) Caso de sesgo evitado cuando el dividendo ocurre DESPUES de fecha_objetivo:
        Si una senal de 2026-10-05 a 2026-10-06 subio de Close 10.00 a 10.20 (+2.0%),
        y dias despues (p. ej. el 2026-10-20) el valor reparte un dividendo del 5%
        (factor de ajuste 0.95 hacia atras), en la descarga posterior auto_adjust=False:
          - Close[2026-10-05] = 10.00, Close[2026-10-06] = 10.20 -> +2.0%
          - Adj Close[2026-10-05] = 9.50, Adj Close[2026-10-06] = 9.69 -> +2.0%
        Como ambos extremos se toman de la misma descarga, el factor 0.95 se cancela
        en el cociente adj_out / adj_in y diferencia_dividendo_pct == 0.0, evitando el
        sesgo de comparar el precio_entrada sellado (10.00) contra el cierre reajustado (9.69).
        """
        pid = self._insertar_prediccion_resuelta(
            ticker="IBE.MC",
            empresa="Iberdrola",
            fecha_prediccion="2026-10-05",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-06",
            senal="ALCISTA",
            pct_estimado=2.0,
            precio_entrada=10.00,
            # En una resolucion tardia con series auto_adjust=True reajustadas tras el dividendo
            # posterior, precio_real habria caido artificialmente a 9.69 (-3.1%):
            precio_real=9.69,
            variacion_real=-3.1,
            acierto=0,
            error_pct=5.1,
        )

        # En la descarga auto_adjust=False posterior al dividendo del 20-oct, ambos Adj Close
        # de 05-oct y 06-oct llevan el mismo factor de ajuste (0.95): 9.50 y 9.69
        df_raw = _construir_df_raw_multiindex(
            {
                "IBE.MC": {
                    "2026-10-05": (10.00, 9.50),
                    "2026-10-06": (10.20, 9.69),
                }
            }
        )

        dt_eval = datetime(2026, 10, 22, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            res = db_pred.evaluar_predicciones_retorno_total(df_raw_ohlcv=df_raw)

        self.assertEqual(res["evaluadas_precio"], 1)
        self.assertEqual(res["evaluadas_total"], 1)
        self.assertEqual(res["pendientes"], 0)

        df_comp = db_pred.obtener_evaluaciones_comparadas()
        self.assertEqual(len(df_comp), 1)
        row = df_comp.iloc[0]
        self.assertEqual(int(row["id"]), pid)
        self.assertAlmostEqual(float(row["var_precio_c10"]), 2.0, places=4)
        self.assertAlmostEqual(float(row["var_retorno_total_c10"]), 2.0, places=4)
        self.assertEqual(int(row["acierto_precio_c10"]), 1)
        self.assertEqual(int(row["acierto_retorno_total_c10"]), 1)
        self.assertAlmostEqual(float(row["diferencia_dividendo_pct"]), 0.0, places=4)

    def test_e_inmutabilidad_predicciones_e_idempotencia_segunda_ejecucion(self):
        """
        (e) Verifica que tras ejecutar evaluar_predicciones_retorno_total:
          1. SHA256_INMUTABLES_11, SHA256_RESUELTAS y el hash completo de predicciones
             son 100% identicos antes y despues.
          2. Invocar una segunda vez evaluar_predicciones_retorno_total es idempotente
             (evaluadas_precio == 0, evaluadas_total == 0, pendientes == 0) y no
             invoca yfinance.download.
        """
        self._insertar_prediccion_resuelta(
            ticker="SAN.MC",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=1.5,
            precio_entrada=10.00,
            precio_real=10.20,
            variacion_real=2.0,
            acierto=1,
            error_pct=0.5,
            resuelta=1,
        )
        self._insertar_prediccion_resuelta(
            ticker="BBVA.MC",
            fecha_prediccion="2026-10-15",
            horizonte="3D",
            dias=3,
            fecha_objetivo="2026-10-20",
            senal="BAJISTA",
            pct_estimado=-2.0,
            precio_entrada=8.00,
            resuelta=0,
        )

        conn = get_connection()
        try:
            h11_antes = _sha256_inmutables_11(conn)
            hres_antes = _sha256_resueltas(conn)
            htotal_antes = _sha256_todas_columnas_predicciones(conn)
        finally:
            conn.close()

        df_raw = _construir_df_raw_multiindex(
            {
                "SAN.MC": {
                    "2026-10-14": (10.00, 9.80),
                    "2026-10-15": (10.20, 10.20),
                }
            }
        )

        dt_eval = datetime(2026, 10, 16, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval), \
             patch("yfinance.download", return_value=df_raw) as mock_yf:
            res1 = db_pred.evaluar_predicciones_retorno_total()
            self.assertEqual(res1["evaluadas_precio"], 1)
            self.assertEqual(res1["evaluadas_total"], 1)
            self.assertEqual(res1["pendientes"], 0)
            self.assertEqual(mock_yf.call_count, 1)
            self.assertIs(mock_yf.call_args.kwargs.get("auto_adjust"), False)

            # Segunda ejecucion: 0 pendientes, no descarga nada
            res2 = db_pred.evaluar_predicciones_retorno_total()
            self.assertEqual(res2["evaluadas_precio"], 0)
            self.assertEqual(res2["evaluadas_total"], 0)
            self.assertEqual(res2["pendientes"], 0)
            self.assertEqual(mock_yf.call_count, 1)

        conn = get_connection()
        try:
            self.assertEqual(h11_antes, _sha256_inmutables_11(conn))
            self.assertEqual(hres_antes, _sha256_resueltas(conn))
            self.assertEqual(htotal_antes, _sha256_todas_columnas_predicciones(conn))
            total_eval = conn.execute("SELECT COUNT(*) FROM evaluaciones").fetchone()[0]
            self.assertEqual(total_eval, 2)
        finally:
            conn.close()

    def test_f_enganche_en_emision_diaria_es_seguro_ante_fallo_de_yahoo(self):
        """
        (f) Verifica que ejecutar_emision_diaria invoca evaluar_predicciones_retorno_total
        tras resolver_predicciones() y que si yfinance.download falla al descargar
        auto_adjust=False, el bloque try/except evita romper la emision diaria.
        """
        conn = get_connection()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS empresas (ticker TEXT PRIMARY KEY, nombre TEXT, activa INTEGER)"
            )
            conn.execute(
                "INSERT INTO empresas (ticker, nombre, activa) VALUES ('SAN.MC', 'Santander', 1)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ranking_estado (ticker TEXT PRIMARY KEY, posicion INTEGER, score REAL)"
            )
            conn.commit()
        finally:
            conn.close()

        # Insertar una prediccion pendiente que vencera en 2026-10-15
        self._insertar_prediccion_resuelta(
            ticker="SAN.MC",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=2.0,
            precio_entrada=10.00,
            resuelta=0,
        )

        fechas = pd.date_range("2026-08-01", "2026-10-15", freq="B")
        cols = pd.MultiIndex.from_product(
            [["SAN.MC", "^IBEX"], ["Open", "High", "Low", "Close", "Volume"]]
        )
        df_batch = pd.DataFrame(10.0, index=fechas, columns=cols, dtype=float)
        for t in ("SAN.MC", "^IBEX"):
            df_batch[(t, "Volume")] = 100000.0
        df_batch.loc[pd.to_datetime("2026-10-15"), ("SAN.MC", "Close")] = 10.30

        dt_1845 = datetime(2026, 10, 15, 18, 45, 0, tzinfo=MADRID_TZ)
        mock_yf_fail = MagicMock(side_effect=RuntimeError("Fallo simulado de red en Yahoo"))
        with _reloj_madrid(dt_1845), \
             patch("services.emision.descargar_price_batch", return_value=df_batch), \
             patch("services.emision.sincronizar_cache_panel_historico", return_value=False), \
             patch("yfinance.download", mock_yf_fail):
            res = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["resueltas"], 1)
        self.assertEqual(mock_yf_fail.call_count, 1)

    def test_g_obtener_resueltas_y_obtener_historial_left_join_evaluaciones(self):
        """
        (g) Verifica que obtener_resueltas y obtener_historial devuelven las columnas
        var_precio_c10, var_retorno_total_c10, acierto_precio_c10, acierto_retorno_total_c10
        y diferencia_dividendo_pct mediante LEFT JOIN tanto cuando hay filas en
        evaluaciones como cuando no las hay (y tambien antes de crear evaluaciones).
        """
        pid_res = self._insertar_prediccion_resuelta(
            ticker="TEF.MC",
            empresa="Telefonica",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=3.0,
            precio_entrada=10.00,
            precio_real=9.80,
            variacion_real=-2.0,
            acierto=0,
            error_pct=5.0,
            resuelta=1,
        )
        pid_pend = self._insertar_prediccion_resuelta(
            ticker="SAN.MC",
            empresa="Santander",
            fecha_prediccion="2026-10-15",
            horizonte="3D",
            dias=3,
            fecha_objetivo="2026-10-20",
            senal="ALCISTA",
            pct_estimado=1.5,
            precio_entrada=5.00,
            resuelta=0,
        )

        cols_c10 = {
            "var_precio_c10",
            "acierto_precio_c10",
            "var_retorno_total_c10",
            "acierto_retorno_total_c10",
            "diferencia_dividendo_pct",
        }

        dt_eval = datetime(2026, 10, 15, 19, 30, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            # 1) Tabla evaluaciones creada pero vacia (sin filas aun)
            db_pred.init_evaluaciones_table()
            df_res_vacio = db_pred.obtener_resueltas(horizonte="Todos", limite=50)
            self.assertEqual(len(df_res_vacio), 1)
            self.assertTrue(cols_c10.issubset(set(df_res_vacio.columns)))
            for c in cols_c10:
                self.assertTrue(pd.isna(df_res_vacio.iloc[0][c]))

            df_hist_vacio = db_pred.obtener_historial(limite=50, solo_resueltas=False)
            self.assertEqual(len(df_hist_vacio), 2)
            self.assertTrue(cols_c10.issubset(set(df_hist_vacio.columns)))
            for c in cols_c10:
                self.assertTrue(df_hist_vacio[c].isna().all())

            # 2) Evaluamos la senal resuelta (con dividendo en el horizonte)
            df_raw = _construir_df_raw_multiindex(
                {
                    "TEF.MC": {
                        "2026-10-14": (10.00, 9.50),
                        "2026-10-15": (9.80, 9.80),
                    }
                }
            )
            db_pred.evaluar_predicciones_retorno_total(df_raw_ohlcv=df_raw)

            df_res = db_pred.obtener_resueltas(horizonte="1D", limite=50)
            self.assertEqual(len(df_res), 1)
            self.assertTrue(cols_c10.issubset(set(df_res.columns)))
            fila_r = df_res.iloc[0]
            self.assertEqual(int(fila_r["id"]), pid_res)
            self.assertAlmostEqual(float(fila_r["variacion_real"]), -2.0, places=4)
            self.assertEqual(int(fila_r["acierto"]), 0)
            self.assertAlmostEqual(float(fila_r["var_precio_c10"]), -2.0, places=4)
            self.assertEqual(int(fila_r["acierto_precio_c10"]), 0)
            self.assertAlmostEqual(float(fila_r["var_retorno_total_c10"]), 3.1579, places=4)
            self.assertEqual(int(fila_r["acierto_retorno_total_c10"]), 1)
            self.assertAlmostEqual(float(fila_r["diferencia_dividendo_pct"]), 5.1579, places=4)

            df_hist = db_pred.obtener_historial(limite=50, solo_resueltas=False)
            self.assertEqual(len(df_hist), 2)
            self.assertTrue(cols_c10.issubset(set(df_hist.columns)))
            h_res = df_hist.loc[df_hist["id"] == pid_res].iloc[0]
            h_pend = df_hist.loc[df_hist["id"] == pid_pend].iloc[0]
            self.assertAlmostEqual(float(h_res["var_precio_c10"]), -2.0, places=4)
            self.assertAlmostEqual(float(h_res["var_retorno_total_c10"]), 3.1579, places=4)
            self.assertEqual(int(h_res["acierto_precio_c10"]), 0)
            self.assertEqual(int(h_res["acierto_retorno_total_c10"]), 1)
            self.assertAlmostEqual(float(h_res["diferencia_dividendo_pct"]), 5.1579, places=4)
            for c in cols_c10:
                self.assertTrue(pd.isna(h_pend[c]))

    def test_h_obtener_resumen_c10_tasas_y_dividendo(self):
        """
        (h) Verifica que obtener_resumen_c10 calcula correctamente las tasas de acierto
        en precio y en retorno total y el recuento n_con_dividendo cuando una senal
        tiene dividendo dentro del horizonte, respetando los filtros horizonte e incluir_legacy.
        """
        # 1) Senal ALCISTA con dividendo en 1D: falla en precio (-2.0%), acierta en retorno total (+3.1579%)
        self._insertar_prediccion_resuelta(
            ticker="TEF.MC",
            empresa="Telefonica",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=3.0,
            precio_entrada=10.00,
            precio_real=9.80,
            variacion_real=-2.0,
            acierto=0,
            error_pct=5.0,
            modelo="v5-frozen",
            resuelta=1,
        )
        # 2) Senal ALCISTA sin dividendo en 1D: acierta en ambos (+2.0%)
        self._insertar_prediccion_resuelta(
            ticker="BBVA.MC",
            empresa="BBVA",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=2.0,
            precio_entrada=10.00,
            precio_real=10.20,
            variacion_real=2.0,
            acierto=1,
            error_pct=0.0,
            modelo="v5-frozen",
            resuelta=1,
        )
        # 3) Senal LATERAL sin dividendo en 3D: falla en ambos (+2.0% > 1.2%)
        self._insertar_prediccion_resuelta(
            ticker="SAN.MC",
            empresa="Santander",
            fecha_prediccion="2026-10-12",
            horizonte="3D",
            dias=3,
            fecha_objetivo="2026-10-15",
            senal="LATERAL",
            pct_estimado=0.0,
            precio_entrada=5.00,
            precio_real=5.10,
            variacion_real=2.0,
            acierto=0,
            error_pct=2.0,
            modelo="v5-frozen",
            resuelta=1,
        )
        # 4) Senal legacy resuelta con dividendo
        self._insertar_prediccion_resuelta(
            ticker="TEF.MC",
            empresa="Telefonica",
            fecha_prediccion="2026-10-05",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-06",
            senal="ALCISTA",
            pct_estimado=2.5,
            precio_entrada=10.00,
            precio_real=9.80,
            variacion_real=-2.0,
            acierto=0,
            error_pct=4.5,
            modelo="legacy-pre-freeze",
            resuelta=1,
        )

        dt_eval = datetime(2026, 10, 15, 19, 45, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            # Antes de evaluar: n_evaluadas == 0
            res_vacio = db_pred.obtener_resumen_c10()
            self.assertEqual(res_vacio["n_evaluadas"], 0)
            self.assertIsNone(res_vacio["tasa_acierto_sellada"])
            self.assertIsNone(res_vacio["tasa_acierto_precio_c10"])
            self.assertIsNone(res_vacio["tasa_acierto_retorno_total_c10"])
            self.assertEqual(res_vacio["n_con_dividendo"], 0)

            df_raw = _construir_df_raw_multiindex(
                {
                    "TEF.MC": {
                        "2026-10-05": (10.00, 9.50),
                        "2026-10-06": (9.80, 9.80),
                        "2026-10-14": (10.00, 9.50),
                        "2026-10-15": (9.80, 9.80),
                    },
                    "BBVA.MC": {
                        "2026-10-14": (10.00, 10.00),
                        "2026-10-15": (10.20, 10.20),
                    },
                    "SAN.MC": {
                        "2026-10-12": (5.00, 5.00),
                        "2026-10-15": (5.10, 5.10),
                    },
                }
            )
            db_pred.evaluar_predicciones_retorno_total(df_raw_ohlcv=df_raw)

            # Sin legacy, todos los horizontes (3 senales: 1 acierta en precio [33.33%], 2 en total [66.67%], 1 con dividendo)
            res_noleg = db_pred.obtener_resumen_c10(horizonte="Todos", incluir_legacy=False)
            self.assertEqual(res_noleg["n_evaluadas"], 3)
            self.assertAlmostEqual(res_noleg["tasa_acierto_sellada"], 33.33, places=2)
            self.assertAlmostEqual(res_noleg["tasa_acierto_precio_c10"], 33.33, places=2)
            self.assertAlmostEqual(res_noleg["tasa_acierto_retorno_total_c10"], 66.67, places=2)
            self.assertEqual(res_noleg["n_con_dividendo"], 1)

            # Solo horizonte 1D sin legacy (2 senales: 1 acierta en precio [50.0%], 2 en total [100.0%], 1 con dividendo)
            res_1d = db_pred.obtener_resumen_c10(horizonte="1D", incluir_legacy=False)
            self.assertEqual(res_1d["n_evaluadas"], 2)
            self.assertAlmostEqual(res_1d["tasa_acierto_sellada"], 50.0, places=2)
            self.assertAlmostEqual(res_1d["tasa_acierto_precio_c10"], 50.0, places=2)
            self.assertAlmostEqual(res_1d["tasa_acierto_retorno_total_c10"], 100.0, places=2)
            self.assertEqual(res_1d["n_con_dividendo"], 1)

            # Con legacy (4 senales: 1 acierta en precio [25.0%], 3 en total [75.0%], 2 con dividendo)
            res_leg = db_pred.obtener_resumen_c10(horizonte="Todos", incluir_legacy=True)
            self.assertEqual(res_leg["n_evaluadas"], 4)
            self.assertAlmostEqual(res_leg["tasa_acierto_sellada"], 25.0, places=2)
            self.assertAlmostEqual(res_leg["tasa_acierto_precio_c10"], 25.0, places=2)
            self.assertAlmostEqual(res_leg["tasa_acierto_retorno_total_c10"], 75.0, places=2)
            self.assertEqual(res_leg["n_con_dividendo"], 2)


if __name__ == "__main__":
    unittest.main()
