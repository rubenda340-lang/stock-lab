"""
Tests para el Paso C-11: Entrada operable desde la apertura de la sesion siguiente (S+1)
========================================================================================
Verifica sobre base de datos SQLite temporal sintetica en tempfile.TemporaryDirectory,
sin llamadas reales a Internet/Yahoo Finance y con el reloj parcheado en
utils.market_calendar, services.emision y database.predicciones:
  (a) Efecto del gap de apertura en 1D: un valor cierra en S a 10.00 (senal ALCISTA
      emitida a las 18:40), abre en S+1 con gap alcista a Open = 10.25 y cierra en S+1
      (fecha_objetivo) a Close = 10.15. Desde el cierre de S subio +1.50% (> 0.8%,
      acierto = 1 en c10_precio_cierre), pero para quien entra en la apertura operable
      de S+1 cayo -0.9756% (< -0.8%, clase_real = 'BAJISTA', acierto = 0 en
      c11_apertura_precio).
  (b) Entrada operable en 1S con dividendo entre S+1 y fecha_objetivo: verifica que
      c11_apertura_precio y c11_apertura_retorno_total difieren exactamente por el
      dividendo.
  (c) obtener_resueltas, obtener_historial y obtener_resumen_c11 exponen correctamente
      todas las columnas y el delta_apertura_vs_cierre_pp global y por horizonte.
  (d) Inmutabilidad de predicciones (SHA256_INMUTABLES_11 y SHA256_RESUELTAS intactos)
      e idempotencia en segunda ejecucion.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
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


def _construir_df_raw_ohlcv(datos_por_ticker: dict) -> pd.DataFrame:
    """
    Construye un DataFrame MultiIndex (ticker, metrica) simulando yfinance.download
    con auto_adjust=False y group_by='ticker'.
    datos_por_ticker: {ticker: {fecha_iso: (open_val, close_val, adj_close_val)}}
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
        for f_str, (o_val, c_val, adj_val) in mapa_fechas.items():
            f_dt = pd.to_datetime(f_str)
            hi = max(float(o_val), float(c_val)) * 1.01
            lo = min(float(o_val), float(c_val)) * 0.99
            df.loc[f_dt, (ticker, "Open")] = float(o_val)
            df.loc[f_dt, (ticker, "High")] = hi
            df.loc[f_dt, (ticker, "Low")] = lo
            df.loc[f_dt, (ticker, "Close")] = float(c_val)
            df.loc[f_dt, (ticker, "Adj Close")] = float(adj_val)
            df.loc[f_dt, (ticker, "Volume")] = 100000.0
    return df


class TestC11EntradaOperable(unittest.TestCase):
    """Suite unitaria para la evaluacion desde apertura siguiente (S+1) en evaluaciones."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(prefix="test_c11_")
        self.temp_dir = self._tmp_ctx.name
        self.temp_db = os.path.join(self.temp_dir, "test_synthetic_c11.db")
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

    def _insertar_prediccion(
        self,
        ticker: str = "SAN.MC",
        empresa: str = "Banco Santander",
        fecha_prediccion: str = "2026-10-14",
        horizonte: str = "1D",
        dias: int = 1,
        fecha_objetivo: str = "2026-10-15",
        senal: str = "ALCISTA",
        pct_estimado: float = 1.5,
        banda_pct: float = 1.2,
        precio_entrada: float = 10.0,
        precio_real: float = 10.15,
        variacion_real: float = 1.5,
        acierto: int = 1,
        error_pct: float = 0.0,
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
                    0.0, 0.1, '2026-10-14T18:40:00', 'v1-dynamic-zone', 0,
                    '2026-10-14T16:40:00Z', 'hash_c11', 68.0, '{"adx":25.0}'
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

    def test_a_efecto_gap_apertura_en_1d(self):
        """
        (a) Efecto del gap de apertura en 1D: un valor cierra en S a 10.00 (senal ALCISTA
        emitida a las 18:40), abre en S+1 con gap alcista a Open = 10.25 y cierra en S+1
        (fecha_objetivo) a Close = 10.15.
        Desde el cierre de S subio +1.50% (> 0.8%, acierto = 1 en c10_precio_cierre),
        pero para quien entra en la apertura operable de S+1 cayo -0.9756% (< -0.8%,
        clase_real = 'BAJISTA', acierto = 0 en c11_apertura_precio).
        """
        pid = self._insertar_prediccion(
            ticker="SAN.MC",
            empresa="Banco Santander",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=1.50,
            precio_entrada=10.00,
            precio_real=10.15,
            variacion_real=1.50,
            acierto=1,
            error_pct=0.0,
        )

        df_raw = _construir_df_raw_ohlcv(
            {
                "SAN.MC": {
                    "2026-10-14": (9.90, 10.00, 10.00),
                    "2026-10-15": (10.25, 10.15, 10.15),
                }
            }
        )

        dt_eval = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            res = db_pred.evaluar_predicciones_c10_c11(df_raw_ohlcv=df_raw)

        self.assertEqual(res["evaluadas_precio"], 1)
        self.assertEqual(res["evaluadas_total"], 1)
        self.assertEqual(res["evaluadas_apertura_precio"], 1)
        self.assertEqual(res["evaluadas_apertura_total"], 1)
        self.assertEqual(res["pendientes"], 0)

        conn = get_connection()
        try:
            rows = {
                r[0]: r
                for r in conn.execute(
                    """
                    SELECT regla, precio_entrada_eval, precio_salida_eval, variacion_pct,
                           umbral_aplicado, clase_real, acierto, acierto_signo,
                           error_pct, diferencia_dividendo_pct
                    FROM evaluaciones
                    WHERE prediccion_id = ?
                    """,
                    (pid,),
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertEqual(len(rows), 4)

        # Cierre S -> Cierre S+1: +1.50%, ALCISTA, acierto = 1
        r_c10_p = rows[db_pred.REGLA_C10_PRECIO_CIERRE]
        self.assertAlmostEqual(r_c10_p[1], 10.00, places=4)
        self.assertAlmostEqual(r_c10_p[2], 10.15, places=4)
        self.assertAlmostEqual(r_c10_p[3], 1.50, places=4)
        self.assertAlmostEqual(r_c10_p[4], 0.80, places=4)
        self.assertEqual(r_c10_p[5], "ALCISTA")
        self.assertEqual(r_c10_p[6], 1)
        self.assertEqual(r_c10_p[7], 1)

        # Apertura S+1 (10.25) -> Cierre S+1 (10.15): -0.9756%, BAJISTA, acierto = 0
        r_c11_p = rows[db_pred.REGLA_C11_APERTURA_PRECIO]
        self.assertAlmostEqual(r_c11_p[1], 10.25, places=4)
        self.assertAlmostEqual(r_c11_p[2], 10.15, places=4)
        self.assertAlmostEqual(r_c11_p[3], -0.9756, places=4)
        self.assertAlmostEqual(r_c11_p[4], 0.80, places=4)
        self.assertEqual(r_c11_p[5], "BAJISTA")
        self.assertEqual(r_c11_p[6], 0)
        self.assertEqual(r_c11_p[7], 0)
        self.assertAlmostEqual(r_c11_p[8], 2.4756, places=4)
        # diferencia_dividendo_pct en c11_apertura_precio registra var_ap_precio - var_precio_c10
        self.assertAlmostEqual(r_c11_p[9], -2.4756, places=4)

        # Retorno total desde apertura (sin dividendo coincide con precio)
        r_c11_rt = rows[db_pred.REGLA_C11_APERTURA_RETORNO_TOTAL]
        self.assertAlmostEqual(r_c11_rt[1], 10.25, places=4)
        self.assertAlmostEqual(r_c11_rt[2], 10.15, places=4)
        self.assertAlmostEqual(r_c11_rt[3], -0.9756, places=4)
        self.assertEqual(r_c11_rt[5], "BAJISTA")
        self.assertEqual(r_c11_rt[6], 0)
        self.assertAlmostEqual(r_c11_rt[9], 0.0, places=4)

    def test_b_entrada_operable_1s_con_dividendo_entre_s1_y_fecha_objetivo(self):
        """
        (b) Entrada operable en 1S con dividendo entre S+1 y fecha_objetivo:
        Verifica que c11_apertura_precio y c11_apertura_retorno_total difieren
        exactamente por el dividendo.
        Escenario:
          - Emision en S = 2026-10-14: Close = 20.00, Adj Close = 19.00 (factor 0.95 por div de 1.00 EUR)
          - Entrada operable en S+1 = 2026-10-15 (aun cum-dividend):
            Open = 20.40, Close = 20.00, Adj Close = 19.00 -> Open_Adj = 20.40 * (19.00 / 20.00) = 19.38
          - Ex-dividend antes de fecha_objetivo = 2026-10-21 (1S):
            En 2026-10-21: Close = 20.20, Adj Close = 20.20
          - En precio desde Apertura S+1: 20.20 / 20.40 - 1 = -0.9804% (LATERAL en 1S con umbral 1.8%,
            acierto = 0 para senal ALCISTA).
          - En retorno total desde Apertura S+1: 20.20 / 19.38 - 1 = +4.2312% (> +1.8%, ALCISTA,
            acierto = 1 para senal ALCISTA).
          - Diferencia por dividendo en c11_apertura_retorno_total:
            round(4.2312 - (-0.9804), 4) = +5.2116%.
        """
        pid = self._insertar_prediccion(
            ticker="TEF.MC",
            empresa="Telefonica",
            fecha_prediccion="2026-10-14",
            horizonte="1S",
            dias=5,
            fecha_objetivo="2026-10-21",
            senal="ALCISTA",
            pct_estimado=3.50,
            banda_pct=2.0,
            precio_entrada=20.00,
            precio_real=20.20,
            variacion_real=1.00,
            acierto=0,
            error_pct=2.50,
        )

        df_raw = _construir_df_raw_ohlcv(
            {
                "TEF.MC": {
                    "2026-10-14": (19.80, 20.00, 19.00),
                    "2026-10-15": (20.40, 20.00, 19.00),
                    "2026-10-16": (19.10, 19.20, 19.20),
                    "2026-10-19": (19.30, 19.50, 19.50),
                    "2026-10-20": (19.60, 19.90, 19.90),
                    "2026-10-21": (20.00, 20.20, 20.20),
                }
            }
        )

        dt_eval = datetime(2026, 10, 21, 19, 15, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            res = db_pred.evaluar_predicciones_c10_c11(df_raw_ohlcv=df_raw)

        self.assertEqual(res["evaluadas_apertura_precio"], 1)
        self.assertEqual(res["evaluadas_apertura_total"], 1)

        conn = get_connection()
        try:
            rows = {
                r[0]: r
                for r in conn.execute(
                    """
                    SELECT regla, precio_entrada_eval, precio_salida_eval, variacion_pct,
                           umbral_aplicado, clase_real, acierto, acierto_signo,
                           error_pct, diferencia_dividendo_pct
                    FROM evaluaciones
                    WHERE prediccion_id = ?
                    """,
                    (pid,),
                ).fetchall()
            }
        finally:
            conn.close()

        r_ap = rows[db_pred.REGLA_C11_APERTURA_PRECIO]
        r_art = rows[db_pred.REGLA_C11_APERTURA_RETORNO_TOTAL]

        self.assertAlmostEqual(r_ap[1], 20.40, places=4)
        self.assertAlmostEqual(r_ap[2], 20.20, places=4)
        self.assertAlmostEqual(r_ap[3], -0.9804, places=4)
        self.assertAlmostEqual(r_ap[4], 1.80, places=4)
        self.assertEqual(r_ap[5], "LATERAL")
        self.assertEqual(r_ap[6], 0)
        self.assertEqual(r_ap[7], 0)

        self.assertAlmostEqual(r_art[1], 19.38, places=4)
        self.assertAlmostEqual(r_art[2], 20.20, places=4)
        self.assertAlmostEqual(r_art[3], 4.2312, places=4)
        self.assertAlmostEqual(r_art[4], 1.80, places=4)
        self.assertEqual(r_art[5], "ALCISTA")
        self.assertEqual(r_art[6], 1)
        self.assertEqual(r_art[7], 1)

        dif_esperada = round(r_art[3] - r_ap[3], 4)
        self.assertAlmostEqual(dif_esperada, 5.2116, places=4)
        self.assertAlmostEqual(r_art[9], dif_esperada, places=4)

    def test_c_columnas_left_join_y_obtener_resumen_c11_global_y_por_horizonte(self):
        """
        (c) Verifica que obtener_resueltas, obtener_historial y obtener_resumen_c11
        exponen correctamente todas las columnas C-11 y el delta_apertura_vs_cierre_pp
        global y desglosado por horizonte (1D, 3D, 1S, 1M).
        """
        # 1) 1D: acierta en Cierre S (+1.50%), falla en Apertura S+1 (-0.9756%)
        pid_1d = self._insertar_prediccion(
            ticker="SAN.MC",
            empresa="Santander",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=1.5,
            precio_entrada=10.00,
            precio_real=10.15,
            variacion_real=1.5,
            acierto=1,
            modelo="v5-frozen",
            resuelta=1,
        )
        # 2) 3D: falla en Cierre S (+0.50% <= 1.2%), acierta en Apertura S+1
        #    (Open S+1 = 9.80 -> Close obj = 10.05 => +2.5510% > 1.2%)
        pid_3d = self._insertar_prediccion(
            ticker="BBVA.MC",
            empresa="BBVA",
            fecha_prediccion="2026-10-14",
            horizonte="3D",
            dias=3,
            fecha_objetivo="2026-10-19",
            senal="ALCISTA",
            pct_estimado=2.0,
            precio_entrada=10.00,
            precio_real=10.05,
            variacion_real=0.5,
            acierto=0,
            modelo="v5-frozen",
            resuelta=1,
        )
        # 3) 1S: acierta en ambos en retorno total, pero en precio falla en apertura
        pid_1s = self._insertar_prediccion(
            ticker="TEF.MC",
            empresa="Telefonica",
            fecha_prediccion="2026-10-14",
            horizonte="1S",
            dias=5,
            fecha_objetivo="2026-10-21",
            senal="ALCISTA",
            pct_estimado=3.5,
            precio_entrada=20.00,
            precio_real=20.20,
            variacion_real=1.0,
            acierto=0,
            modelo="v5-frozen",
            resuelta=1,
        )
        # 4) Pendiente (resuelta = 0) para verificar NULLs en obtener_historial
        pid_pend = self._insertar_prediccion(
            ticker="IBE.MC",
            empresa="Iberdrola",
            fecha_prediccion="2026-10-21",
            horizonte="1M",
            dias=20,
            fecha_objetivo="2026-11-18",
            senal="LATERAL",
            pct_estimado=0.0,
            precio_entrada=12.00,
            resuelta=0,
        )

        df_raw = _construir_df_raw_ohlcv(
            {
                "SAN.MC": {
                    "2026-10-14": (9.95, 10.00, 10.00),
                    "2026-10-15": (10.25, 10.15, 10.15),
                },
                "BBVA.MC": {
                    "2026-10-14": (10.00, 10.00, 10.00),
                    "2026-10-15": (9.80, 9.90, 9.90),
                    "2026-10-16": (9.90, 10.00, 10.00),
                    "2026-10-19": (10.00, 10.05, 10.05),
                },
                "TEF.MC": {
                    "2026-10-14": (19.80, 20.00, 19.00),
                    "2026-10-15": (20.40, 20.00, 19.00),
                    "2026-10-21": (20.00, 20.20, 20.20),
                },
            }
        )

        cols_c11 = {
            "precio_apertura_c11",
            "var_apertura_precio_c11",
            "acierto_apertura_precio_c11",
            "var_apertura_total_c11",
            "acierto_apertura_total_c11",
        }

        dt_eval = datetime(2026, 10, 21, 19, 30, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            db_pred.evaluar_predicciones_c10_c11(df_raw_ohlcv=df_raw)

            df_res = db_pred.obtener_resueltas(horizonte="Todos", limite=50)
            self.assertEqual(len(df_res), 3)
            self.assertTrue(cols_c11.issubset(set(df_res.columns)))

            r_1d = df_res.loc[df_res["id"] == pid_1d].iloc[0]
            self.assertAlmostEqual(float(r_1d["precio_apertura_c11"]), 10.25, places=4)
            self.assertAlmostEqual(float(r_1d["var_apertura_precio_c11"]), -0.9756, places=4)
            self.assertEqual(int(r_1d["acierto_apertura_precio_c11"]), 0)
            self.assertAlmostEqual(float(r_1d["var_apertura_total_c11"]), -0.9756, places=4)
            self.assertEqual(int(r_1d["acierto_apertura_total_c11"]), 0)

            r_3d = df_res.loc[df_res["id"] == pid_3d].iloc[0]
            self.assertAlmostEqual(float(r_3d["precio_apertura_c11"]), 9.80, places=4)
            self.assertAlmostEqual(float(r_3d["var_apertura_precio_c11"]), 2.5510, places=4)
            self.assertEqual(int(r_3d["acierto_apertura_precio_c11"]), 1)

            df_hist = db_pred.obtener_historial(limite=50, solo_resueltas=False)
            self.assertEqual(len(df_hist), 4)
            self.assertTrue(cols_c11.issubset(set(df_hist.columns)))
            h_pend = df_hist.loc[df_hist["id"] == pid_pend].iloc[0]
            for c in cols_c11:
                self.assertTrue(pd.isna(h_pend[c]))

            # Resumen C-11 global (3 evaluadas):
            # Cierre precio: SAN acierta (1), BBVA falla (0), TEF falla (0) -> 1/3 = 33.33%
            # Apertura precio: SAN falla (0), BBVA acierta (1), TEF falla (0) -> 1/3 = 33.33%
            # Cierre retorno total: SAN acierta (1), BBVA falla (0), TEF acierta (1) -> 2/3 = 66.67%
            # Apertura retorno total: SAN falla (0), BBVA acierta (1), TEF acierta (1) -> 2/3 = 66.67%
            res_c11 = db_pred.obtener_resumen_c11(horizonte="Todos", incluir_legacy=False)
            self.assertEqual(res_c11["n_evaluadas"], 3)
            self.assertAlmostEqual(res_c11["tasa_cierre_precio"], 33.33, places=2)
            self.assertAlmostEqual(res_c11["tasa_apertura_precio"], 33.33, places=2)
            self.assertAlmostEqual(res_c11["delta_apertura_vs_cierre_pp"], 0.0, places=2)
            self.assertAlmostEqual(res_c11["tasa_cierre_retorno_total"], 66.67, places=2)
            self.assertAlmostEqual(res_c11["tasa_apertura_retorno_total"], 66.67, places=2)
            self.assertAlmostEqual(res_c11["delta_apertura_vs_cierre_total_pp"], 0.0, places=2)

            # Desglose por horizonte en por_horizonte
            por_hz = res_c11["por_horizonte"]
            self.assertEqual(set(por_hz.keys()), {"1D", "3D", "1S", "1M"})

            # 1D: Cierre 100% -> Apertura 0% (delta -100.0 pp)
            self.assertEqual(por_hz["1D"]["n_evaluadas"], 1)
            self.assertAlmostEqual(por_hz["1D"]["tasa_cierre_precio"], 100.0, places=2)
            self.assertAlmostEqual(por_hz["1D"]["tasa_apertura_precio"], 0.0, places=2)
            self.assertAlmostEqual(por_hz["1D"]["delta_apertura_vs_cierre_pp"], -100.0, places=2)

            # 3D: Cierre 0% -> Apertura 100% (delta +100.0 pp)
            self.assertEqual(por_hz["3D"]["n_evaluadas"], 1)
            self.assertAlmostEqual(por_hz["3D"]["tasa_cierre_precio"], 0.0, places=2)
            self.assertAlmostEqual(por_hz["3D"]["tasa_apertura_precio"], 100.0, places=2)
            self.assertAlmostEqual(por_hz["3D"]["delta_apertura_vs_cierre_pp"], 100.0, places=2)

            # 1S: Cierre precio 0% -> Apertura precio 0%, Cierre RT 100% -> Apertura RT 100%
            self.assertEqual(por_hz["1S"]["n_evaluadas"], 1)
            self.assertAlmostEqual(por_hz["1S"]["tasa_cierre_precio"], 0.0, places=2)
            self.assertAlmostEqual(por_hz["1S"]["tasa_apertura_precio"], 0.0, places=2)
            self.assertAlmostEqual(por_hz["1S"]["tasa_cierre_retorno_total"], 100.0, places=2)
            self.assertAlmostEqual(por_hz["1S"]["tasa_apertura_retorno_total"], 100.0, places=2)

            # 1M: 0 evaluadas
            self.assertEqual(por_hz["1M"]["n_evaluadas"], 0)
            self.assertIsNone(por_hz["1M"]["tasa_cierre_precio"])
            self.assertIsNone(por_hz["1M"]["delta_apertura_vs_cierre_pp"])

            # Filtro directo por horizonte='1D'
            res_1d_only = db_pred.obtener_resumen_c11(horizonte="1D", incluir_legacy=False)
            self.assertEqual(res_1d_only["n_evaluadas"], 1)
            self.assertAlmostEqual(res_1d_only["delta_apertura_vs_cierre_pp"], -100.0, places=2)

    def test_d_inmutabilidad_predicciones_e_idempotencia_segunda_ejecucion(self):
        """
        (d) Verifica que tras ejecutar evaluar_predicciones_c10_c11:
          1. SHA256_INMUTABLES_11, SHA256_RESUELTAS y el hash completo de predicciones
             son 100% identicos antes y despues.
          2. Una segunda ejecucion devuelve 0 nuevas evaluadas en las 4 reglas y no
             hace llamadas a red.
          3. Si una prediccion ya tenia las 2 reglas C-10 pero le faltaban las 2 reglas
             C-11, el backfill inserta unicamente las 2 filas C-11 pendientes.
        """
        pid = self._insertar_prediccion(
            ticker="SAN.MC",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=1.5,
            precio_entrada=10.00,
            precio_real=10.15,
            variacion_real=1.5,
            acierto=1,
            resuelta=1,
        )

        conn = get_connection()
        try:
            h11_antes = _sha256_inmutables_11(conn)
            hres_antes = _sha256_resueltas(conn)
            htotal_antes = _sha256_todas_columnas_predicciones(conn)

            # Simular estado pre-C11: ya existen las 2 filas C-10 para pid
            db_pred.init_evaluaciones_table(conn=conn)
            conn.execute(
                """
                INSERT INTO evaluaciones (
                    prediccion_id, regla, precio_entrada_eval, precio_salida_eval,
                    variacion_pct, umbral_aplicado, clase_real, acierto,
                    acierto_signo, error_pct, diferencia_dividendo_pct, evaluada_en
                ) VALUES (?, ?, 10.00, 10.15, 1.50, 0.8, 'ALCISTA', 1, 1, 0.0, 0.0, '2026-10-15T17:00:00Z')
                """,
                (pid, db_pred.REGLA_C10_PRECIO_CIERRE),
            )
            conn.execute(
                """
                INSERT INTO evaluaciones (
                    prediccion_id, regla, precio_entrada_eval, precio_salida_eval,
                    variacion_pct, umbral_aplicado, clase_real, acierto,
                    acierto_signo, error_pct, diferencia_dividendo_pct, evaluada_en
                ) VALUES (?, ?, 10.00, 10.15, 1.50, 0.8, 'ALCISTA', 1, 1, 0.0, 0.0, '2026-10-15T17:00:00Z')
                """,
                (pid, db_pred.REGLA_C10_RETORNO_TOTAL),
            )
            conn.commit()
        finally:
            conn.close()

        df_raw = _construir_df_raw_ohlcv(
            {
                "SAN.MC": {
                    "2026-10-14": (9.95, 10.00, 10.00),
                    "2026-10-15": (10.25, 10.15, 10.15),
                }
            }
        )

        dt_eval = datetime(2026, 10, 16, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval), \
             patch("yfinance.download", return_value=df_raw) as mock_yf:
            # Primera ejecucion sobre estado pre-C11: C-10 ya estaban (0 nuevas), C-11 se insertan (1 y 1)
            res1 = db_pred.evaluar_predicciones_c10_c11()
            self.assertEqual(res1["evaluadas_precio"], 0)
            self.assertEqual(res1["evaluadas_total"], 0)
            self.assertEqual(res1["evaluadas_apertura_precio"], 1)
            self.assertEqual(res1["evaluadas_apertura_total"], 1)
            self.assertEqual(res1["pendientes"], 0)
            self.assertEqual(mock_yf.call_count, 1)

            # Segunda ejecucion: estrictamente idempotente (0 nuevas, sin llamadas a red)
            res2 = db_pred.evaluar_predicciones_c10_c11()
            self.assertEqual(res2["evaluadas_precio"], 0)
            self.assertEqual(res2["evaluadas_total"], 0)
            self.assertEqual(res2["evaluadas_apertura_precio"], 0)
            self.assertEqual(res2["evaluadas_apertura_total"], 0)
            self.assertEqual(res2["pendientes"], 0)
            self.assertEqual(mock_yf.call_count, 1)

        conn = get_connection()
        try:
            self.assertEqual(h11_antes, _sha256_inmutables_11(conn))
            self.assertEqual(hres_antes, _sha256_resueltas(conn))
            self.assertEqual(htotal_antes, _sha256_todas_columnas_predicciones(conn))
            by_rule = dict(
                conn.execute(
                    "SELECT regla, COUNT(*) FROM evaluaciones GROUP BY regla"
                ).fetchall()
            )
            self.assertEqual(
                by_rule,
                {
                    "c10_precio_cierre": 1,
                    "c10_retorno_total": 1,
                    "c11_apertura_precio": 1,
                    "c11_apertura_retorno_total": 1,
                },
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
