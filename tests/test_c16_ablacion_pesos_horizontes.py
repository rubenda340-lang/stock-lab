"""
Tests unitarios para el Paso C-16: Ablacion y Re-ponderacion de Componentes por
Horizonte (1D, 3D, 1S) y Volumen (RVOL) en Walk-Forward Fuera de Muestra
================================================================================
Verifica de forma determinista y sin efectos secundarios en red ni disco:
  (a) Inmutabilidad del baseline v5-frozen: construir y ejecutar
      construir_config_challenger_c16() y ejecutar_estudio_ablacion_c16() no modifica
      PESOS_TENDENCIA, PESOS_RANGO, PESOS_TRANSICION ni calcular_params_hash_motor()
      en services/signal_engine.py, y el hash de configuracion de challenger_c16 es
      distinto del de baseline_v5.
  (b) Efecto matematico de la re-ponderacion en 1D vs 1S: ante un panel sintetico
      donde en 1D los cierres con momentum y RVOL extremos revierten al dia siguiente
      mientras que las tendencias a 1S continuan, challenger_c16 supera a baseline_v5
      en acierto de signo en 1D sin degradar 1S.
  (c) Ejecucion completa de ejecutar_estudio_ablacion_c16: devuelve las 4 variantes
      (baseline_v5, ablacion_volumen, ablacion_pesos_1d, challenger_c16) con sus
      diferenciales pareados por fecha y etiquetas de muestra C-09/C-13.
  (d) Validacion en solo lectura sobre data/backtest_cache_6y.parquet e inmutabilidad
      de stocklab.db: comprueba en una ventana fuera de muestra del Parquet local que
      challenger_c16 mejora el desempeno respecto a baseline_v5 y que stocklab.db
      tiene total_changes == 0 y los mismos hashes SHA-256 antes y despues.
"""

from contextlib import contextmanager
import copy
from datetime import datetime
import hashlib
import os
import sqlite3
from typing import Dict, List, Tuple
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import database.predicciones as db_pred
import services.emision as se
import services.signal_engine as sig_eng
from services.walk_forward_harness import (
    ConfigExperimentoMotor,
    calcular_hash_config,
    construir_config_ablacion_pesos_1d_c16,
    construir_config_ablacion_volumen_c16,
    construir_config_challenger_c16,
    ejecutar_estudio_ablacion_c16,
    simular_emision_y_resolucion_wf,
)
import utils.market_calendar as mc

MADRID_TZ = ZoneInfo("Europe/Madrid")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REAL_DB_PATH = os.path.join(REPO_ROOT, "stocklab.db")
PARQUET_PATH = os.path.join(REPO_ROOT, "data", "backtest_cache_6y.parquet")


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Parchea get_ahora_madrid en utils.market_calendar, services.emision y database.predicciones."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _sha256_fichero(ruta: str) -> str:
    with open(ruta, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _construir_panel_reversion_1d_continuacion_1s() -> Tuple[
    pd.DataFrame, List[str], List[str], Dict[str, float]
]:
    """
    Construye un panel sintetico determinista donde:
      - Los activos tienen una tendencia estructural sostenida a 5 sesiones (1S)
        (alcista en activos pares, bajista en impares),
      - En las fechas de emision elegidas se produce un pico puntual de sobreextension
        (salto brusco de cierre + pico de volumen RVOL > 1.6) que revierte exactamente
        al dia siguiente (1D) antes de retomar la tendencia a 5 sesiones (1S).
    De este modo, en 1D la continuacion pura de momentum/RSI + boost de volumen falla
    en el salto overnight, mientras que la reversion suave + tilt alineado de C-16
    acierta el signo en 1D sin degradar la continuacion a 1S.
    """
    n_sesiones = 95
    fechas = mc.bdate_range_bme("2026-04-01", periods=n_sesiones)
    tickers = ["REV1.MC", "REV2.MC", "REV3.MC", "REV4.MC"]
    todos = tickers + [sig_eng.TICKER_BENCHMARK]

    cols = pd.MultiIndex.from_product(
        [todos, ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)

    # Indices de las sesiones de emision donde ocurre pico + reversion a 1D
    indices_emision = [62, 68, 74, 80, 86]
    tilts: Dict[str, float] = {}

    for idx_tk, tk in enumerate(todos):
        es_alcista_macro = (idx_tk % 2 == 0)
        p0 = 30.0 + idx_tk * 10.0
        closes = np.zeros(n_sesiones, dtype=float)
        vols = np.full(n_sesiones, 100000.0, dtype=float)
        closes[0] = p0

        if tk == sig_eng.TICKER_BENCHMARK:
            # IBEX en rango lateral neutro para no sesgar marea hacia un solo lado
            for i in range(1, n_sesiones):
                closes[i] = p0 * (1.0 + 0.002 * np.sin(i * 0.4))
        else:
            # El tilt fundamental refleja el exceso de valoracion en el pico de agotamiento
            signo_macro = 1.0 if es_alcista_macro else -1.0
            tilts[tk] = -1.90 * signo_macro

            # Deriva estructural sostenida que domina a 1S (5 sesiones)
            drift_base = 0.0080 * signo_macro
            for i in range(1, n_sesiones):
                closes[i] = closes[i - 1] * (1.0 + drift_base)

            for idx_e, te in enumerate(indices_emision):
                if idx_e < 3:
                    # Regimen 1 (3 fechas): climax de sobrecompra/sobreventa extrema en t_e
                    # (RSI > 78 si alcista o RSI < 22 si bajista, |comp_momentum| > 1.5,
                    # |comp_rsi| > 1.4), con desaceleracion en la ultima vela que hace girar
                    # ligeramente MACD antes del cierre t_e:
                    # - baseline_v5 (sin reversion de momentum/RSI ni tilt en 1D) emite
                    #   continuacion en t_e y falla ante la reversion de 1D (t_e + 1),
                    # - challenger_c16 invierte comp_momentum y comp_rsi sobreextendidos,
                    #   suma el tilt contrarian (+0.30) y emite la senal de reversion en 1D
                    #   (permitida por Elder al estar RSI > 75 / < 25), acertando el signo
                    #   en 1D y manteniendo 100% de acierto en 1S.
                    base_pre = closes[te - 6]
                    closes[te - 5] = base_pre * (1.0 + signo_macro * 0.035)
                    closes[te - 4] = closes[te - 5] * (1.0 + signo_macro * 0.040)
                    closes[te - 3] = closes[te - 4] * (1.0 + signo_macro * 0.045)
                    closes[te - 2] = closes[te - 3] * (1.0 + signo_macro * 0.045)
                    closes[te - 1] = closes[te - 2] * (1.0 - signo_macro * 0.004)
                    closes[te] = closes[te - 1] * (1.0 - signo_macro * 0.004)
                    vols[te] = 92000.0  # RVOL ~ 0.92

                    # En t_e + 1 (1D) revierte con fuerza (-2.6% / +2.6%)
                    closes[te + 1] = closes[te] * (1.0 - signo_macro * 0.026)
                    # En t_e + 2 .. t_e + 5 (1S) retoma la tendencia estructural (+4.8% / -4.8% neto)
                    closes[te + 2] = closes[te] * (1.0 + signo_macro * 0.012)
                    closes[te + 3] = closes[te] * (1.0 + signo_macro * 0.024)
                    closes[te + 4] = closes[te] * (1.0 + signo_macro * 0.036)
                    closes[te + 5] = closes[te] * (1.0 + signo_macro * 0.048)
                else:
                    # Regimen 2 (2 fechas): pico de volumen (RVOL > 1.8) que se agota en 1D
                    base_pre = closes[te - 2]
                    closes[te - 1] = base_pre * (1.0 + signo_macro * 0.022)
                    closes[te] = closes[te - 1] * (1.0 + signo_macro * 0.032)
                    vols[te] = 210000.0  # RVOL > 1.8

                    closes[te + 1] = closes[te] * (1.0 - signo_macro * 0.024)
                    closes[te + 2] = closes[te] * (1.0 + signo_macro * 0.012)
                    closes[te + 3] = closes[te] * (1.0 + signo_macro * 0.025)
                    closes[te + 4] = closes[te] * (1.0 + signo_macro * 0.038)
                    closes[te + 5] = closes[te] * (1.0 + signo_macro * 0.052)

        opens = np.roll(closes, 1)
        opens[0] = closes[0]
        highs = np.maximum(opens, closes) * 1.006
        lows = np.minimum(opens, closes) * 0.994

        df[(tk, "Open")] = opens
        df[(tk, "High")] = highs
        df[(tk, "Low")] = lows
        df[(tk, "Close")] = closes
        df[(tk, "Volume")] = vols

    fechas_emision = [pd.Timestamp(fechas[i]).strftime("%Y-%m-%d") for i in indices_emision]
    return df, tickers, fechas_emision, tilts


class TestC16AblacionPesosHorizontes(unittest.TestCase):
    """Suite de pruebas unitarias para el Paso C-16 (ablacion walk-forward y re-ponderacion)."""

    def setUp(self):
        self.dt_fijo = datetime(2026, 11, 8, 12, 0, tzinfo=MADRID_TZ)

    def test_a_inmutabilidad_baseline_v5_frozen_y_hashes_distintos(self):
        """
        (a) Inmutabilidad del baseline v5-frozen:
        Verifica que construir y ejecutar construir_config_challenger_c16() y
        ejecutar_estudio_ablacion_c16() no modifica PESOS_TENDENCIA, PESOS_RANGO,
        PESOS_TRANSICION ni calcular_params_hash_motor() en services/signal_engine.py,
        y que el hash de configuracion de challenger_c16 es distinto del de baseline_v5.
        """
        pesos_tend_antes = copy.deepcopy(sig_eng.PESOS_TENDENCIA)
        pesos_rango_antes = copy.deepcopy(sig_eng.PESOS_RANGO)
        pesos_trans_antes = copy.deepcopy(sig_eng.PESOS_TRANSICION)
        hash_motor_antes = sig_eng.calcular_params_hash_motor()
        estado_v5_antes = copy.deepcopy(sig_eng.verificar_estado_congelado_v5())

        cfg_base = ConfigExperimentoMotor()
        cfg_vol = construir_config_ablacion_volumen_c16()
        cfg_pesos = construir_config_ablacion_pesos_1d_c16()
        cfg_c16 = construir_config_challenger_c16()

        h_base = calcular_hash_config(cfg_base)
        h_vol = calcular_hash_config(cfg_vol)
        h_pesos = calcular_hash_config(cfg_pesos)
        h_c16 = calcular_hash_config(cfg_c16)

        self.assertEqual(len(h_base), 64)
        self.assertEqual(len(h_c16), 64)
        self.assertNotEqual(h_c16, h_base)
        self.assertNotEqual(h_vol, h_base)
        self.assertNotEqual(h_pesos, h_base)
        self.assertEqual(len({h_base, h_vol, h_pesos, h_c16}), 4)

        # Verificar que todos los horizontes (1D, 3D, 1S, 1M) estan documentados en challenger_c16
        for hz in ("1D", "3D", "1S", "1M"):
            self.assertIn(hz, cfg_c16.pesos_horizonte)
            self.assertIn(hz, cfg_c16.peso_tilt_por_hz)
            self.assertIn(hz, cfg_c16.vol_confirm_boost_por_hz)
            self.assertIn(hz, cfg_c16.vol_no_confirm_damp_por_hz)
            self.assertIn(hz, cfg_c16.amortiguacion_rvol_pico_por_hz)
            self.assertAlmostEqual(
                sum(cfg_c16.pesos_horizonte[hz].values()), 1.0, places=6
            )

        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_emision, tilts = (
                _construir_panel_reversion_1d_continuacion_1s()
            )
            _ = ejecutar_estudio_ablacion_c16(
                df_panel=df_panel,
                fechas_emision=fechas_emision[:2],
                tickers=tickers[:2],
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )

        self.assertEqual(sig_eng.PESOS_TENDENCIA, pesos_tend_antes)
        self.assertEqual(sig_eng.PESOS_RANGO, pesos_rango_antes)
        self.assertEqual(sig_eng.PESOS_TRANSICION, pesos_trans_antes)
        self.assertEqual(sig_eng.calcular_params_hash_motor(), hash_motor_antes)
        self.assertEqual(sig_eng.verificar_estado_congelado_v5(), estado_v5_antes)

    def test_b_efecto_matematico_reponderacion_1d_vs_1s_panel_sintetico(self):
        """
        (b) Efecto matematico de la re-ponderacion en 1D vs 1S:
        Ante un panel sintetico donde en 1D los cierres con momentum y RVOL extremos
        revierten al dia siguiente mientras que las tendencias a 1S continuan,
        verifica que challenger_c16 supera a baseline_v5 en acierto de signo en 1D
        sin degradar 1S.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_emision, tilts = (
                _construir_panel_reversion_1d_continuacion_1s()
            )
            estudio = ejecutar_estudio_ablacion_c16(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )

            ev_base = estudio["baseline_v5"]["evaluacion"]
            ev_c16 = estudio["challenger_c16"]["evaluacion"]

            sig_1d_base = ev_base["por_horizonte"]["1D"]["tasa_signo_c10"]
            sig_1d_c16 = ev_c16["por_horizonte"]["1D"]["tasa_signo_c10"]
            self.assertIsNotNone(sig_1d_base)
            self.assertIsNotNone(sig_1d_c16)
            self.assertGreater(
                sig_1d_c16,
                sig_1d_base,
                f"challenger_c16 debe superar a baseline_v5 en signo 1D ({sig_1d_c16}% vs {sig_1d_base}%)",
            )

            # En 1S, donde la tendencia continua, challenger_c16 no degrada el acierto de signo ni 3 clases
            sig_1s_base = ev_base["por_horizonte"]["1S"]["tasa_signo_c10"]
            sig_1s_c16 = ev_c16["por_horizonte"]["1S"]["tasa_signo_c10"]
            t3c_1s_base = ev_base["por_horizonte"]["1S"]["tasa_3c_v1_c10"]
            t3c_1s_c16 = ev_c16["por_horizonte"]["1S"]["tasa_3c_v1_c10"]
            self.assertGreaterEqual(sig_1s_c16, sig_1s_base)
            self.assertGreaterEqual(t3c_1s_c16, t3c_1s_base)

    def test_c_estructura_completa_ejecutar_estudio_ablacion_c16(self):
        """
        (c) Ejecucion completa de ejecutar_estudio_ablacion_c16:
        Verifica que devuelve las 4 variantes (baseline_v5, ablacion_volumen,
        ablacion_pesos_1d, challenger_c16) con sus diferenciales pareados por fecha
        y etiquetas de muestra C-09/C-13, y que el resultado coincide con
        simular_emision_y_resolucion_wf individual.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_emision, tilts = (
                _construir_panel_reversion_1d_continuacion_1s()
            )
            estudio = ejecutar_estudio_ablacion_c16(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )

            claves_esperadas = (
                "baseline_v5",
                "ablacion_volumen",
                "ablacion_pesos_1d",
                "challenger_c16",
            )
            for k in claves_esperadas:
                self.assertIn(k, estudio)
                self.assertIn(k, estudio["variantes"])
                var_info = estudio[k]
                self.assertIn("etiqueta_muestra", var_info)
                self.assertIn("fechas", var_info["etiqueta_muestra"])
                self.assertEqual(var_info["n_fechas"], len(fechas_emision))
                self.assertIn("inferencia", var_info)
                self.assertEqual(
                    var_info["inferencia"]["v1_c10"]["n_fechas"], len(fechas_emision)
                )
                for hz in ("1D", "3D", "1S"):
                    self.assertIn(hz, var_info["por_horizonte"])
                    self.assertIn(
                        "etiqueta_muestra", var_info["por_horizonte"][hz]
                    )

            for k_chall in ("ablacion_volumen", "ablacion_pesos_1d", "challenger_c16"):
                self.assertIn(k_chall, estudio["comparaciones_vs_baseline"])
                comp = estudio["comparaciones_vs_baseline"][k_chall]
                self.assertEqual(comp["n_fechas_comunes"], len(fechas_emision))
                self.assertIn("delta_3c_v1_c10_pp", comp)
                self.assertIn("delta_signo_c10_pp", comp)
                self.assertIn("t_stat_pareado_por_fecha", comp)
                self.assertIn("tests_pareados", comp)
                for hz in ("1D", "3D", "1S"):
                    self.assertIn(hz, comp["por_horizonte"])
                    self.assertIn(
                        "pareado_signo_c10", comp["por_horizonte"][hz]
                    )

            # Comprobar equivalencia exacta con simular_emision_y_resolucion_wf directo
            df_c16_directo = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision[:2],
                tickers=tickers[:2],
                config=construir_config_challenger_c16(),
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            df_c16_estudio = estudio["challenger_c16"]["df_simulacion"]
            df_c16_sub = df_c16_estudio[
                df_c16_estudio["fecha_prediccion"].isin(fechas_emision[:2])
                & df_c16_estudio["ticker"].isin(tickers[:2])
            ].reset_index(drop=True)
            pd.testing.assert_frame_equal(
                df_c16_directo.reset_index(drop=True),
                df_c16_sub,
                check_exact=True,
            )

    def test_d_validacion_solo_lectura_parquet_e_inmutabilidad_stocklab_db(self):
        """
        (d) Validacion en solo lectura sobre data/backtest_cache_6y.parquet e
        inmutabilidad de stocklab.db:
        Comprueba en una ventana fuera de muestra del Parquet local que challenger_c16
        mejora el desempeno respecto a baseline_v5 y que stocklab.db tiene
        total_changes == 0 y los mismos hashes SHA-256 antes y despues.
        """
        self.assertTrue(os.path.exists(REAL_DB_PATH), "stocklab.db debe existir")
        self.assertTrue(
            os.path.exists(PARQUET_PATH), "data/backtest_cache_6y.parquet debe existir"
        )

        sha_db_antes = _sha256_fichero(REAL_DB_PATH)
        sha_pq_antes = _sha256_fichero(PARQUET_PATH)

        conn_ro = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            cambios_antes = conn_ro.total_changes
            sha_inm_antes = db_pred._calcular_sha256_inmutables(conn_ro)
            sha_res_antes = db_pred._calcular_sha256_resueltas(conn_ro)
            tilts_db = dict(
                conn_ro.execute(
                    "SELECT ticker, AVG(tilt) FROM predicciones WHERE tilt IS NOT NULL GROUP BY ticker"
                ).fetchall()
            )
        finally:
            conn_ro.close()

        self.assertEqual(cambios_antes, 0)

        df_parquet = pd.read_parquet(PARQUET_PATH)
        tickers_muestra = [
            "SAN.MC",
            "BBVA.MC",
            "IBE.MC",
            "ITX.MC",
            "TEF.MC",
            "REP.MC",
            "CABK.MC",
            "AMS.MC",
            "FER.MC",
            "ACS.MC",
        ]
        disponibles = set(df_parquet.columns.get_level_values(0).unique())
        tickers_reales = [t for t in tickers_muestra if t in disponibles]
        cols_needed = [
            c
            for c in df_parquet.columns
            if c[0] in set(tickers_reales + [sig_eng.TICKER_BENCHMARK])
        ]

        # Ventana fuera de muestra en 2026 pre-freeze (junio-agosto 2026)
        fechas_all = [
            pd.Timestamp(ts).strftime("%Y-%m-%d") for ts in df_parquet.index
        ]
        fechas_val_oos = [
            f for f in fechas_all if "2026-06-01" <= f <= "2026-08-31"
        ][::12][:5]
        idx_fin = max(
            i for i, f in enumerate(fechas_all) if f <= "2026-09-18"
        )
        idx_ini = max(0, idx_fin - 150)
        df_ventana = df_parquet.loc[:, cols_needed].iloc[idx_ini : idx_fin + 1].copy()

        with _reloj_madrid(self.dt_fijo):
            estudio_oos = ejecutar_estudio_ablacion_c16(
                df_panel=df_ventana,
                fechas_emision=fechas_val_oos,
                tickers=tickers_reales,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts_db,
            )

        comp_c16 = estudio_oos["comparaciones_vs_baseline"]["challenger_c16"]
        # Verificar que challenger_c16 mejora sobre baseline_v5 en la ventana fuera de muestra
        self.assertGreaterEqual(comp_c16["delta_3c_v1_c10_pp"], 0.0)
        self.assertGreater(
            comp_c16["por_horizonte"]["1D"]["delta_3c_v1_c10_pp"], 0.0
        )

        sha_db_despues = _sha256_fichero(REAL_DB_PATH)
        sha_pq_despues = _sha256_fichero(PARQUET_PATH)
        conn_ro2 = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            self.assertEqual(conn_ro2.total_changes, 0)
            sha_inm_despues = db_pred._calcular_sha256_inmutables(conn_ro2)
            sha_res_despues = db_pred._calcular_sha256_resueltas(conn_ro2)
        finally:
            conn_ro2.close()

        self.assertEqual(sha_db_despues, sha_db_antes)
        self.assertEqual(sha_pq_despues, sha_pq_antes)
        self.assertEqual(sha_inm_despues, sha_inm_antes)
        self.assertEqual(sha_res_despues, sha_res_antes)


if __name__ == "__main__":
    unittest.main()
