"""
Tests unitarios para Paso C-12: Zona muerta escalada por volatilidad
(umbral = k * sigma * sqrt(dias)) en la tabla `evaluaciones` y analisis por terciles.

Verifica en una BD temporal sintetica, sin red y con reloj parcheado en
`utils.market_calendar`, `services.emision` y `database.predicciones`:
  (a) `calcular_umbral_volatilidad_v2` escala con `sigma` y con `sqrt(dias)`
      (`1D, 3D, 1S, 1M`), respeta el suelo minimo de `0.10%` y aplica fallback
      al umbral fijo del horizonte si `sigma` es `None`, `NaN` o `<= 0`.
  (b) Dos activos con distinta volatilidad ante movimientos en `1D`:
      - Activo tranquilo (`sigma = 0.6%` diario -> `umbral_v2 = 0.30%` en `1D`):
        un movimiento de `+0.50%` es `ALCISTA` en v2 mientras que en v1 (`0.80%`)
        quedaba atrapado en `LATERAL`.
      - Activo muy volatil (`sigma = 3.2%` diario -> `umbral_v2 = 1.60%` en `1D`):
        un ruido de `+1.00%` es `LATERAL` en v2 mientras que en v1 (`0.80%`)
        se clasificaba como `ALCISTA`.
  (c) En una muestra sintetica con 3 terciles de volatilidad (`sigma = 0.7%, 1.5%, 3.0%`)
      donde los retornos diarios estan escalados por `sigma` (~ N(0, sigma^2)),
      la proporcion de `clase_real == 'LATERAL'` bajo v1 cae drasticamente del
      tercil 1 al tercil 3, mientras que bajo v2 permanece estable entre terciles
      (`rango_intertercil_lateral_v2_pp < rango_intertercil_lateral_v1_pp`).
  (d) Las reglas v1 y v2 conviven en `evaluaciones` (7 reglas por prediccion),
      `predicciones` permanece 100% inmutable y una segunda ejecucion es idempotente.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import math
import os
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import database.connection as db_conn
import database.predicciones as db_pred
from database.connection import get_connection, init_db
import services.emision as svc_emision
import utils.market_calendar as mcal

MADRID_TZ = ZoneInfo("Europe/Madrid")


@contextmanager
def _reloj_madrid(dt_fijo: datetime):
    """Parchea el reloj de Madrid en todos los modulos relevantes."""
    with patch.object(mcal, "get_ahora_madrid", return_value=dt_fijo), patch.object(
        svc_emision, "get_ahora_madrid", return_value=dt_fijo
    ), patch.object(db_pred, "get_ahora_madrid", return_value=dt_fijo):
        yield


def _sha256_inmutables_11(conn) -> str:
    rows = conn.execute(
        """
        SELECT id, ticker, horizonte, fecha_prediccion, fecha_objetivo,
               precio_entrada, senal, pct_estimado, banda_pct, confianza, modelo
        FROM predicciones
        ORDER BY id ASC
        """
    ).fetchall()
    payload = "\n".join(repr(tuple(r)) for r in rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_inmutables_10_sin_modelo(conn) -> str:
    rows = conn.execute(
        """
        SELECT id, ticker, horizonte, fecha_prediccion, fecha_objetivo,
               precio_entrada, senal, pct_estimado, banda_pct, confianza
        FROM predicciones
        ORDER BY id ASC
        """
    ).fetchall()
    payload = "\n".join(repr(tuple(r)) for r in rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_inmutables_11_desde_12oct(conn) -> str:
    rows = conn.execute(
        """
        SELECT id, ticker, horizonte, fecha_prediccion, fecha_objetivo,
               precio_entrada, senal, pct_estimado, banda_pct, confianza, modelo
        FROM predicciones
        WHERE fecha_prediccion >= '2026-10-12'
        ORDER BY id ASC
        """
    ).fetchall()
    payload = "\n".join(repr(tuple(r)) for r in rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_resueltas(conn) -> str:
    rows = conn.execute(
        """
        SELECT id, resuelta, fecha_resolucion, precio_real,
               variacion_real, acierto, error_pct
        FROM predicciones
        WHERE resuelta = 1
        ORDER BY id ASC
        """
    ).fetchall()
    payload = "\n".join(repr(tuple(r)) for r in rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_todas_columnas_predicciones(conn) -> str:
    rows = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
    payload = "\n".join(repr(tuple(r)) for r in rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _construir_df_raw_ohlcv(series_por_ticker: dict) -> pd.DataFrame:
    """
    Construye un DataFrame MultiIndex (ticker, metrica) como devuelve
    yfinance.download(..., auto_adjust=False, group_by='ticker').
    Cada entrada en series_por_ticker es:
      ticker -> {fecha_str: (open_val, close_val, adj_close_val)}
    """
    fechas = sorted(
        {f for datos in series_por_ticker.values() for f in datos.keys()}
    )
    idx = pd.to_datetime(fechas)
    frames = {}
    for ticker, datos in series_por_ticker.items():
        opens = []
        closes = []
        adjs = []
        for f in fechas:
            if f in datos:
                vals = datos[f]
                o_val, c_val, a_val = vals[0], vals[1], vals[2]
            else:
                o_val, c_val, a_val = np.nan, np.nan, np.nan
            opens.append(o_val)
            closes.append(c_val)
            adjs.append(a_val)
        frames[(ticker, "Open")] = opens
        frames[(ticker, "High")] = closes
        frames[(ticker, "Low")] = opens
        frames[(ticker, "Close")] = closes
        frames[(ticker, "Adj Close")] = adjs
        frames[(ticker, "Volume")] = [100000.0] * len(fechas)
    df = pd.DataFrame(frames, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


class TestC12ZonaMuertaVolatilidad(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_c12.db")
        self.p_db1 = patch.object(db_conn, "DB_PATH", self.db_path)
        self.p_db1.start()
        init_db()
        db_pred.init_predicciones_table()
        db_pred.init_evaluaciones_table()

    def tearDown(self):
        self.p_db1.stop()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    def _insertar_prediccion(
        self,
        *,
        ticker: str,
        empresa: str,
        fecha_prediccion: str,
        horizonte: str,
        dias: int,
        fecha_objetivo: str,
        senal: str,
        pct_estimado: float,
        banda_pct: float,
        sigma: float,
        umbral_zona: float,
        precio_entrada: float,
        precio_real: float,
        variacion_real: float,
        acierto: int,
        error_pct: float,
        modelo: str = "v5-frozen",
    ) -> int:
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias,
                    fecha_objetivo, precio_entrada, senal, pct_estimado,
                    banda_pct, confianza, sigma, umbral_zona, modelo, resuelta,
                    fecha_resolucion, precio_real, variacion_real, acierto,
                    error_pct, motivo_resolucion, score_calidad
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'vencida', 65.0)
                """,
                (
                    ticker,
                    empresa,
                    fecha_prediccion,
                    horizonte,
                    dias,
                    fecha_objetivo,
                    precio_entrada,
                    senal,
                    pct_estimado,
                    banda_pct,
                    60.0,
                    sigma,
                    umbral_zona,
                    modelo,
                    fecha_objetivo,
                    precio_real,
                    variacion_real,
                    acierto,
                    error_pct,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_a_calcular_umbral_volatilidad_v2_escala_sigma_y_sqrt_dias(self):
        """
        (a) Verifica que calcular_umbral_volatilidad_v2 escala linealmente con sigma
        y con sqrt(dias) para 1D (1), 3D (3), 1S (5), 1M (20), acepta sigma tanto
        en fraccion diaria (0.016) como en porcentaje (1.6), aplica suelo minimo
        de 0.10% y usa fallback al umbral fijo si sigma es None, NaN o <= 0.
        """
        self.assertAlmostEqual(db_pred.K_UMBRAL_SIGMA_V2, 0.50, places=4)

        # Sigma = 1.6% diario (0.016 en fraccion o 1.6 en porcentaje)
        u_1d_frac = db_pred.calcular_umbral_volatilidad_v2(0.016, 1, "1D")
        u_1d_pct = db_pred.calcular_umbral_volatilidad_v2(1.6, 1, "1D")
        self.assertAlmostEqual(u_1d_frac, 0.80, places=4)
        self.assertAlmostEqual(u_1d_pct, 0.80, places=4)

        # Escalado con sqrt(dias) para un mismo sigma = 1.6%
        u_3d = db_pred.calcular_umbral_volatilidad_v2(0.016, 3, "3D")
        u_1s = db_pred.calcular_umbral_volatilidad_v2(0.016, 5, "1S")
        u_1m = db_pred.calcular_umbral_volatilidad_v2(0.016, 20, "1M")

        self.assertAlmostEqual(u_3d, round(0.50 * 1.6 * math.sqrt(3), 4), places=4)
        self.assertAlmostEqual(u_1s, round(0.50 * 1.6 * math.sqrt(5), 4), places=4)
        self.assertAlmostEqual(u_1m, round(0.50 * 1.6 * math.sqrt(20), 4), places=4)
        self.assertLess(u_1d_frac, u_3d)
        self.assertLess(u_3d, u_1s)
        self.assertLess(u_1s, u_1m)

        # Escalado con sigma para un mismo horizonte (1D)
        u_baja = db_pred.calcular_umbral_volatilidad_v2(0.006, 1, "1D")
        u_alta = db_pred.calcular_umbral_volatilidad_v2(0.032, 1, "1D")
        self.assertAlmostEqual(u_baja, 0.30, places=4)
        self.assertAlmostEqual(u_alta, 1.60, places=4)

        # Suelo minimo de seguridad 0.10%
        u_suelo = db_pred.calcular_umbral_volatilidad_v2(0.0005, 1, "1D")
        self.assertAlmostEqual(u_suelo, 0.10, places=4)

        # Fallback a umbral fijo del horizonte si sigma es None, NaN o <= 0
        self.assertAlmostEqual(
            db_pred.calcular_umbral_volatilidad_v2(None, 1, "1D"), 0.80, places=4
        )
        self.assertAlmostEqual(
            db_pred.calcular_umbral_volatilidad_v2(float("nan"), 3, "3D"), 1.20, places=4
        )
        self.assertAlmostEqual(
            db_pred.calcular_umbral_volatilidad_v2(0.0, 5, "1S"), 1.80, places=4
        )
        self.assertAlmostEqual(
            db_pred.calcular_umbral_volatilidad_v2(-0.02, 20, "1M"), 3.00, places=4
        )

    def test_b_dos_activos_distinta_volatilidad_clasificacion_v1_vs_v2(self):
        """
        (b) Verifica que:
        - Para un activo tranquilo (sigma = 0.6% diario -> umbral_v2 = 0.30% en 1D),
          un movimiento de +0.50% es ALCISTA en v2 mientras que en v1 (0.80%) quedaba
          atrapado en LATERAL.
        - Para un activo muy volatil (sigma = 3.2% diario -> umbral_v2 = 1.60% en 1D),
          un ruido de +1.00% es LATERAL en v2 mientras que en v1 (0.80%) se clasificaba
          como ALCISTA.
        """
        pid_low = self._insertar_prediccion(
            ticker="LOWV.MC",
            empresa="Tranquila SA",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=0.60,
            banda_pct=0.60,
            sigma=0.006,  # 0.6% diario -> umbral_v2 = 0.30%
            umbral_zona=0.80,
            precio_entrada=100.00,
            precio_real=100.50,
            variacion_real=0.50,
            acierto=0,  # En v1 (0.80%), +0.50% era LATERAL -> fallo para ALCISTA
            error_pct=0.10,
        )

        pid_high = self._insertar_prediccion(
            ticker="HIGHV.MC",
            empresa="Volatil SA",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=2.00,
            banda_pct=3.20,
            sigma=0.032,  # 3.2% diario -> umbral_v2 = 1.60%
            umbral_zona=0.80,
            precio_entrada=100.00,
            precio_real=101.00,
            variacion_real=1.00,
            acierto=1,  # En v1 (0.80%), +1.00% era ALCISTA -> acierto para ALCISTA
            error_pct=1.00,
        )

        df_raw = _construir_df_raw_ohlcv(
            {
                "LOWV.MC": {
                    "2026-10-14": (100.00, 100.00, 100.00),
                    "2026-10-15": (100.00, 100.50, 100.50),
                },
                "HIGHV.MC": {
                    "2026-10-14": (100.00, 100.00, 100.00),
                    "2026-10-15": (100.00, 101.00, 101.00),
                },
            }
        )

        dt_eval = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            res = db_pred.evaluar_predicciones_c10_c11(
                df_raw_ohlcv=df_raw, incluir_c11=True, incluir_c12=True
            )

        self.assertEqual(res["evaluadas_precio"], 2)
        self.assertEqual(res["evaluadas_vol_precio_cierre"], 2)
        self.assertEqual(res["evaluadas_vol_retorno_total"], 2)
        self.assertEqual(res["evaluadas_vol_apertura_precio"], 2)

        conn = get_connection()
        try:
            rows_low = {
                r[0]: r
                for r in conn.execute(
                    """
                    SELECT regla, variacion_pct, umbral_aplicado, clase_real, acierto, acierto_signo
                    FROM evaluaciones
                    WHERE prediccion_id = ?
                    """,
                    (pid_low,),
                ).fetchall()
            }
            rows_high = {
                r[0]: r
                for r in conn.execute(
                    """
                    SELECT regla, variacion_pct, umbral_aplicado, clase_real, acierto, acierto_signo
                    FROM evaluaciones
                    WHERE prediccion_id = ?
                    """,
                    (pid_high,),
                ).fetchall()
            }
        finally:
            conn.close()

        # Activo tranquilo (+0.50%): LATERAL en v1 (umbral 0.80%), ALCISTA en v2 (umbral 0.30%)
        self.assertEqual(rows_low[db_pred.REGLA_C10_PRECIO_CIERRE][2], 0.80)
        self.assertEqual(rows_low[db_pred.REGLA_C10_PRECIO_CIERRE][3], "LATERAL")
        self.assertEqual(rows_low[db_pred.REGLA_C10_PRECIO_CIERRE][4], 0)

        self.assertAlmostEqual(
            rows_low[db_pred.REGLA_C12_VOL_PRECIO_CIERRE][2], 0.30, places=4
        )
        self.assertEqual(rows_low[db_pred.REGLA_C12_VOL_PRECIO_CIERRE][3], "ALCISTA")
        self.assertEqual(rows_low[db_pred.REGLA_C12_VOL_PRECIO_CIERRE][4], 1)
        self.assertEqual(rows_low[db_pred.REGLA_C12_VOL_PRECIO_CIERRE][5], 1)

        # Activo volatil (+1.00%): ALCISTA en v1 (umbral 0.80%), LATERAL en v2 (umbral 1.60%)
        self.assertEqual(rows_high[db_pred.REGLA_C10_PRECIO_CIERRE][2], 0.80)
        self.assertEqual(rows_high[db_pred.REGLA_C10_PRECIO_CIERRE][3], "ALCISTA")
        self.assertEqual(rows_high[db_pred.REGLA_C10_PRECIO_CIERRE][4], 1)

        self.assertAlmostEqual(
            rows_high[db_pred.REGLA_C12_VOL_PRECIO_CIERRE][2], 1.60, places=4
        )
        self.assertEqual(rows_high[db_pred.REGLA_C12_VOL_PRECIO_CIERRE][3], "LATERAL")
        self.assertEqual(rows_high[db_pred.REGLA_C12_VOL_PRECIO_CIERRE][4], 0)
        self.assertEqual(rows_high[db_pred.REGLA_C12_VOL_PRECIO_CIERRE][5], 1)

    def test_c_estabilidad_proporcion_lateral_entre_terciles_de_sigma(self):
        """
        (c) En una muestra sintetica con 3 terciles de volatilidad
        (sigma = 0.7%, 1.5%, 3.0%) donde los retornos diarios estan escalados por sigma,
        verifica que la proporcion de clase_real == 'LATERAL' bajo v1 cae drasticamente
        del tercil 1 al tercil 3, mientras que bajo v2 permanece estable entre terciles
        (rango_intertercil_lateral_v2_pp < rango_intertercil_lateral_v1_pp).
        """
        rng = np.random.default_rng(20261029)
        z_samples = rng.standard_normal(60)
        terciles_cfg = [
            ("T1", 0.007),  # 0.7% diario -> umbral_v2 = 0.35% en 1D
            ("T2", 0.015),  # 1.5% diario -> umbral_v2 = 0.75% en 1D
            ("T3", 0.030),  # 3.0% diario -> umbral_v2 = 1.50% en 1D
        ]

        series_ohlcv = {}
        for pref, sigma_frac in terciles_cfg:
            sigma_pct = sigma_frac * 100.0
            for idx_z, z_val in enumerate(z_samples):
                tk = f"{pref}_{idx_z:02d}.MC"
                ret_pct = float(z_val * sigma_pct)
                p_in = 100.0
                p_out = round(p_in * (1.0 + ret_pct / 100.0), 6)
                var_real = round((p_out / p_in - 1.0) * 100.0, 4)
                acierto_v1 = 1 if abs(var_real) <= 0.80 else 0
                self._insertar_prediccion(
                    ticker=tk,
                    empresa=f"Empresa {tk}",
                    fecha_prediccion="2026-10-14",
                    horizonte="1D",
                    dias=1,
                    fecha_objetivo="2026-10-15",
                    senal="LATERAL",
                    pct_estimado=0.0,
                    banda_pct=sigma_pct,
                    sigma=sigma_frac,
                    umbral_zona=0.80,
                    precio_entrada=p_in,
                    precio_real=p_out,
                    variacion_real=var_real,
                    acierto=acierto_v1,
                    error_pct=abs(var_real),
                )
                series_ohlcv[tk] = {
                    "2026-10-14": (p_in, p_in, p_in),
                    "2026-10-15": (p_in, p_out, p_out),
                }

        df_raw = _construir_df_raw_ohlcv(series_ohlcv)
        dt_eval = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            db_pred.evaluar_predicciones_c10_c11(
                df_raw_ohlcv=df_raw, incluir_c11=True, incluir_c12=True
            )

        res_c12 = db_pred.obtener_resumen_c12(horizonte="1D", incluir_legacy=False)
        self.assertEqual(res_c12["n_evaluadas"], 180)
        terciles = res_c12["terciles_sigma"]
        self.assertEqual(len(terciles), 3)

        t1, t2, t3 = terciles[0], terciles[1], terciles[2]
        self.assertEqual(t1["n"], 60)
        self.assertEqual(t2["n"], 60)
        self.assertEqual(t3["n"], 60)

        # Bajo v1 (umbral fijo 0.80%), % LATERAL cae fuertemente de T1 a T3
        self.assertGreater(t1["pct_lateral_v1"], t2["pct_lateral_v1"])
        self.assertGreater(t2["pct_lateral_v1"], t3["pct_lateral_v1"])
        self.assertGreater(res_c12["rango_intertercil_lateral_v1_pp"], 40.0)

        # Bajo v2 (umbral = 0.50 * sigma * sqrt(1)), % LATERAL permanece estable entre terciles
        self.assertLess(
            res_c12["rango_intertercil_lateral_v2_pp"],
            res_c12["rango_intertercil_lateral_v1_pp"],
        )
        self.assertLessEqual(res_c12["rango_intertercil_lateral_v2_pp"], 2.0)

    def test_d_convivencia_v1_v2_inmutabilidad_e_idempotencia(self):
        """
        (d) Verifica que las 4 reglas v1 y las 3 reglas v2 conviven en `evaluaciones`
        (7 reglas en total), `predicciones` permanece 100% inmutable,
        `obtener_resueltas` y `obtener_historial` exponen `umbral_v2_c12`,
        `clase_real_v2_c12` y `acierto_v2_c12`, y una segunda ejecucion es idempotente.
        """
        pid = self._insertar_prediccion(
            ticker="SAN.MC",
            empresa="Banco Santander",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=0.60,
            banda_pct=0.60,
            sigma=0.006,
            umbral_zona=0.80,
            precio_entrada=10.00,
            precio_real=10.05,
            variacion_real=0.50,
            acierto=0,
            error_pct=0.10,
        )

        conn = get_connection()
        try:
            h11_antes = _sha256_inmutables_11(conn)
            h10_antes = _sha256_inmutables_10_sin_modelo(conn)
            h11_oct_antes = _sha256_inmutables_11_desde_12oct(conn)
            hres_antes = _sha256_resueltas(conn)
            htotal_antes = _sha256_todas_columnas_predicciones(conn)
        finally:
            conn.close()

        df_raw = _construir_df_raw_ohlcv(
            {
                "SAN.MC": {
                    "2026-10-14": (10.00, 10.00, 10.00),
                    "2026-10-15": (10.01, 10.05, 10.05),
                }
            }
        )

        dt_eval = datetime(2026, 10, 15, 19, 30, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval), patch(
            "yfinance.download", return_value=df_raw
        ) as mock_yf:
            res1 = db_pred.evaluar_predicciones_c12()
            self.assertEqual(res1["evaluadas_precio"], 1)
            self.assertEqual(res1["evaluadas_total"], 1)
            self.assertEqual(res1["evaluadas_apertura_precio"], 1)
            self.assertEqual(res1["evaluadas_apertura_total"], 1)
            self.assertEqual(res1["evaluadas_vol_precio_cierre"], 1)
            self.assertEqual(res1["evaluadas_vol_retorno_total"], 1)
            self.assertEqual(res1["evaluadas_vol_apertura_precio"], 1)
            self.assertEqual(res1["pendientes"], 0)
            self.assertEqual(mock_yf.call_count, 1)

            # Segunda ejecucion: estrictamente idempotente (0 nuevas, sin llamadas a red)
            res2 = db_pred.evaluar_predicciones_c12()
            self.assertEqual(res2["evaluadas_precio"], 0)
            self.assertEqual(res2["evaluadas_total"], 0)
            self.assertEqual(res2["evaluadas_apertura_precio"], 0)
            self.assertEqual(res2["evaluadas_apertura_total"], 0)
            self.assertEqual(res2["evaluadas_vol_precio_cierre"], 0)
            self.assertEqual(res2["evaluadas_vol_retorno_total"], 0)
            self.assertEqual(res2["evaluadas_vol_apertura_precio"], 0)
            self.assertEqual(res2["pendientes"], 0)
            self.assertEqual(mock_yf.call_count, 1)

        conn = get_connection()
        try:
            self.assertEqual(h11_antes, _sha256_inmutables_11(conn))
            self.assertEqual(h10_antes, _sha256_inmutables_10_sin_modelo(conn))
            self.assertEqual(h11_oct_antes, _sha256_inmutables_11_desde_12oct(conn))
            self.assertEqual(hres_antes, _sha256_resueltas(conn))
            self.assertEqual(htotal_antes, _sha256_todas_columnas_predicciones(conn))

            by_rule = dict(
                conn.execute(
                    "SELECT regla, COUNT(*) FROM evaluaciones WHERE prediccion_id = ? GROUP BY regla",
                    (pid,),
                ).fetchall()
            )
            self.assertEqual(
                by_rule,
                {
                    "c10_precio_cierre": 1,
                    "c10_retorno_total": 1,
                    "c11_apertura_precio": 1,
                    "c11_apertura_retorno_total": 1,
                    "c12_vol_precio_cierre": 1,
                    "c12_vol_retorno_total": 1,
                    "c12_vol_apertura_precio": 1,
                },
            )
        finally:
            conn.close()

        df_res = db_pred.obtener_resueltas()
        self.assertEqual(len(df_res), 1)
        self.assertAlmostEqual(float(df_res.iloc[0]["umbral_v2_c12"]), 0.30, places=4)
        self.assertEqual(str(df_res.iloc[0]["clase_real_v2_c12"]), "ALCISTA")
        self.assertEqual(int(df_res.iloc[0]["acierto_v2_c12"]), 1)

        df_hist = db_pred.obtener_historial()
        self.assertEqual(len(df_hist), 1)
        self.assertAlmostEqual(float(df_hist.iloc[0]["umbral_v2_c12"]), 0.30, places=4)
        self.assertEqual(str(df_hist.iloc[0]["clase_real_v2_c12"]), "ALCISTA")
        self.assertEqual(int(df_hist.iloc[0]["acierto_v2_c12"]), 1)

    def test_e_control_outliers_sigma_y_reduccion_rango_intertercil(self):
        """
        (e) Verifica para C-12b:
        (i) Un outlier corrupto como sigma = 45.65 en predicciones queda acotado al tope
            razonable (8.0% diario -> umbral_v2 = 4.0% en 1D) y no genera un umbral
            absurdo de 22.8% en 1D; asimismo, sigma = 0.274 en fraccion (27.4%) se
            trata como fraccion y se acota a 8.0% diario, nunca como 0.274%.
        (ii) La sigma calculada desde la serie de cierres usa exclusivamente barras con
             fecha <= fecha_prediccion (cero look-ahead), ignorando saltos posteriores,
             y evaluar_predicciones_c12(recalcular_existentes=True) actualiza solo las
             filas 'c12_vol_*' en evaluaciones sin tocar predicciones ni c10_*/c11_*.
        (iii) El rango intertercil de LATERAL en v2 se reduce respecto a v1.
        """
        # (i) Control de outliers en _normalizar_sigma_diaria_pct y calcular_umbral_volatilidad_v2
        self.assertAlmostEqual(
            db_pred._normalizar_sigma_diaria_pct(45.651723), 8.00, places=4
        )
        self.assertAlmostEqual(
            db_pred.calcular_umbral_volatilidad_v2(45.651723, 1, "1D"), 4.00, places=4
        )
        self.assertLess(
            db_pred.calcular_umbral_volatilidad_v2(45.651723, 1, "1D"), 22.0
        )
        # Un sigma=0.274 en fraccion (27.4% diario) se multiplica por 100 y se acota a 8.0%, no a 0.274%
        self.assertAlmostEqual(
            db_pred._normalizar_sigma_diaria_pct(0.274121), 8.00, places=4
        )
        self.assertAlmostEqual(
            db_pred._normalizar_sigma_diaria_pct(0.0005), 0.20, places=4
        )

        # (ii) Cero look-ahead en serie de cierres y recalculo limpio de c12_vol_*
        # Construimos 21 sesiones hasta 2026-10-14 con volatilidad tranquila (~0.8% diario)
        # y un salto gigante posterior en 2026-10-15 y 2026-10-16 (+9% y -8%)
        fechas_previas = [
            "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22",
            "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29",
            "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
            "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13",
            "2026-10-14",
        ]
        datos_tranquilos = {}
        precio_cursor = 100.0
        for idx_f, f_str in enumerate(fechas_previas):
            if idx_f > 0:
                salto = 0.006 if (idx_f % 2 == 0) else -0.006
                precio_cursor = round(precio_cursor * (1.0 + salto), 6)
            datos_tranquilos[f_str] = (precio_cursor, precio_cursor, precio_cursor)

        p_emision = datos_tranquilos["2026-10-14"][1]
        p_obj = round(p_emision * 1.007, 6)  # +0.70% en 1D
        datos_tranquilos["2026-10-15"] = (p_emision, p_obj, p_obj)

        # Serie con caos futuro despues de fecha_prediccion (no debe contaminar sigma en 2026-10-14)
        datos_con_caos_futuro = dict(datos_tranquilos)
        datos_con_caos_futuro["2026-10-15"] = (p_emision, round(p_emision * 1.095, 6), round(p_emision * 1.095, 6))
        datos_con_caos_futuro["2026-10-16"] = (round(p_emision * 1.095, 6), round(p_emision * 0.98, 6), round(p_emision * 0.98, 6))
        datos_con_caos_futuro["2026-10-19"] = (round(p_emision * 0.98, 6), round(p_emision * 1.09, 6), round(p_emision * 1.09, 6))

        df_A = _construir_df_raw_ohlcv({"DIA.MC": datos_tranquilos})
        df_B = _construir_df_raw_ohlcv({"DIA.MC": datos_con_caos_futuro})

        sub_A = db_pred._extraer_serie_raw_ticker(df_A, "DIA.MC")
        sub_B = db_pred._extraer_serie_raw_ticker(df_B, "DIA.MC")

        sigma_A = db_pred._calcular_sigma_cierre_hasta_emision_pct(
            sub_A, "2026-10-14", sigma_fallback=45.651723
        )
        sigma_B = db_pred._calcular_sigma_cierre_hasta_emision_pct(
            sub_B, "2026-10-14", sigma_fallback=45.651723
        )
        self.assertIsNotNone(sigma_A)
        self.assertAlmostEqual(sigma_A, sigma_B, places=6)
        self.assertLess(sigma_A, 1.50)

        # Insertar prediccion con sigma corrupto 45.651723 y evaluar primero con serie corta (fallback cap 8% -> umbral 4.0%)
        pid_out = self._insertar_prediccion(
            ticker="DIA.MC",
            empresa="Distribuidora Internacional",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            pct_estimado=0.90,
            banda_pct=3.15,
            sigma=45.651723,
            umbral_zona=0.80,
            precio_entrada=p_emision,
            precio_real=p_obj,
            variacion_real=0.70,
            acierto=0,
            error_pct=0.20,
        )

        df_corto = _construir_df_raw_ohlcv(
            {
                "DIA.MC": {
                    "2026-10-14": (p_emision, p_emision, p_emision),
                    "2026-10-15": (p_emision, p_obj, p_obj),
                }
            }
        )
        dt_eval = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_eval):
            db_pred.evaluar_predicciones_c12(df_raw_ohlcv=df_corto)

        conn = get_connection()
        try:
            umb_fallback = conn.execute(
                "SELECT umbral_aplicado FROM evaluaciones WHERE prediccion_id = ? AND regla = ?",
                (pid_out, db_pred.REGLA_C12_VOL_PRECIO_CIERRE),
            ).fetchone()[0]
            h11_pre = _sha256_inmutables_11(conn)
            hres_pre = _sha256_resueltas(conn)
        finally:
            conn.close()

        # Con fallback acotado a 8.0%, el umbral en 1D es 4.00% (y no 22.8259%)
        self.assertAlmostEqual(float(umb_fallback), 4.00, places=4)

        # Ahora recalculamos existentes pasando df_A (con 21 sesiones hasta 2026-10-14)
        with _reloj_madrid(dt_eval):
            res_recalc = db_pred.evaluar_predicciones_c12(
                df_raw_ohlcv=df_A, recalcular_existentes=True
            )
        self.assertEqual(res_recalc["evaluadas_vol_precio_cierre"], 1)
        self.assertEqual(res_recalc["evaluadas_vol_retorno_total"], 1)
        self.assertEqual(res_recalc["evaluadas_vol_apertura_precio"], 1)

        conn = get_connection()
        try:
            umb_recalc, clase_recalc, acierto_recalc = conn.execute(
                "SELECT umbral_aplicado, clase_real, acierto FROM evaluaciones WHERE prediccion_id = ? AND regla = ?",
                (pid_out, db_pred.REGLA_C12_VOL_PRECIO_CIERRE),
            ).fetchone()
            self.assertEqual(h11_pre, _sha256_inmutables_11(conn))
            self.assertEqual(hres_pre, _sha256_resueltas(conn))
        finally:
            conn.close()

        self.assertLess(float(umb_recalc), 0.70)
        self.assertEqual(clase_recalc, "ALCISTA")
        self.assertEqual(int(acierto_recalc), 1)

        # (iii) Verificar reduccion de rango intertercil en muestra con 3 terciles + outlier
        rng = np.random.default_rng(20261030)
        z_vals = rng.standard_normal(30)
        series_t = {}
        for pref_t, sig_f in [("B1", 0.007), ("B2", 0.015), ("B3", 0.030)]:
            for idx_i, z_v in enumerate(z_vals):
                tk_i = f"{pref_t}_{idx_i:02d}.MC"
                ret_p = float(z_v * (sig_f * 100.0))
                p_i = 100.0
                p_o = round(p_i * (1.0 + ret_p / 100.0), 6)
                v_r = round((p_o / p_i - 1.0) * 100.0, 4)
                # En el ultimo elemento de B3 inyectamos sigma corrupto 45.65 pero con 20 barras de sigma=3.0%
                sig_db = 45.651723 if (pref_t == "B3" and idx_i == 0) else sig_f
                self._insertar_prediccion(
                    ticker=tk_i,
                    empresa=f"Emp {tk_i}",
                    fecha_prediccion="2026-10-14",
                    horizonte="1D",
                    dias=1,
                    fecha_objetivo="2026-10-15",
                    senal="LATERAL",
                    pct_estimado=0.0,
                    banda_pct=sig_f * 100.0,
                    sigma=sig_db,
                    umbral_zona=0.80,
                    precio_entrada=p_i,
                    precio_real=p_o,
                    variacion_real=v_r,
                    acierto=1 if abs(v_r) <= 0.80 else 0,
                    error_pct=abs(v_r),
                )
                series_t[tk_i] = {
                    "2026-10-14": (p_i, p_i, p_i),
                    "2026-10-15": (p_i, p_o, p_o),
                }

        df_batch_t = _construir_df_raw_ohlcv(series_t)
        with _reloj_madrid(dt_eval):
            db_pred.evaluar_predicciones_c12(
                df_raw_ohlcv=df_batch_t, recalcular_existentes=False
            )

        resumen_final = db_pred.obtener_resumen_c12(horizonte="1D", incluir_legacy=False)
        self.assertLess(
            resumen_final["rango_intertercil_lateral_v2_pp"],
            resumen_final["rango_intertercil_lateral_v1_pp"],
        )


if __name__ == "__main__":
    unittest.main()
