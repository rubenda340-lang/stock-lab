"""
Tests unitarios para el Paso C-17: Calibracion Walk-Forward del Umbral de
Activacion Direccional por Horizonte (multiplicador_conviccion_por_hz)
=========================================================================
Verifica de forma determinista y sin efectos secundarios en red ni disco:
  (a) Jerarquia e inmutabilidad de configuraciones: baseline_v5, challenger_c16,
      challenger_c17_moderado y challenger_c17 tienen params_hash distintos y
      deterministas; challenger_c17 hereda la re-ponderacion de C-16 y anade
      multiplicador_conviccion_por_hz > 1.0 en 1D/3D/1S; services/signal_engine.py
      permanece intacto.
  (b) Monotonia de selectividad y mejora de 3 clases: al aplicar
      challenger_c17_moderado y challenger_c17 sobre un mismo panel, el porcentaje
      de senales 'LATERAL' aumenta de forma controlada
      (%LATERAL(base) <= %LATERAL(c17_mod) <= %LATERAL(c17) < 92%), conservando
      senales direccionales activas (n_direccionales > 0), y el acierto en 3 clases
      sube al filtrar las senales tibias que caen en la zona muerta.
  (c) Ejecucion completa de ejecutar_estudio_umbrales_c17: devuelve las 4 variantes
      con sus comparaciones pareadas contra baseline_v5 y contra challenger_c16,
      incluyendo la comparativa contra las 5 referencias ingenuas de C-14.
  (d) Validacion en solo lectura sobre data/backtest_cache_6y.parquet e inmutabilidad
      de stocklab.db: comprueba sobre una ventana fuera de muestra del Parquet local
      que challenger_c17 mejora simultaneamente el Acierto en 3 clases (v1 y v2) y
      el Acierto de signo respecto a baseline_v5, y que stocklab.db mantiene
      total_changes == 0 y los mismos hashes SHA-256.
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
    construir_config_challenger_c16,
    construir_config_challenger_c17,
    construir_config_challenger_c17_moderado,
    ejecutar_estudio_umbrales_c17,
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


def _construir_panel_conviccion_mixta() -> Tuple[
    pd.DataFrame, List[str], List[str], Dict[str, float]
]:
    """
    Construye un panel sintetico determinista con 6 activos:
      - 2 activos de tendencia fuerte (FUERTE_UP.MC, FUERTE_DN.MC): senal de alta
        conviccion cuya variacion real continua fuera de la zona muerta en 1D/3D/1S.
        Permanecen direccionales bajo c17_moderado y c17.
      - 4 activos de deriva tibia (TIBIO1..4.MC): senales direccionales debiles en
        baseline_v5/challenger_c16 que tras la fecha de emision se frenan dentro de
        la zona muerta LATERAL. Al subir multiplicador_conviccion_por_hz en C-17,
        estas senales tibias pasan a LATERAL y mejora el Acierto en 3 clases.
    """
    n_sesiones = 95
    fechas = mc.bdate_range_bme("2026-04-01", periods=n_sesiones)
    tickers = [
        "FUERTE_UP.MC",
        "FUERTE_DN.MC",
        "TIBIO1.MC",
        "TIBIO2.MC",
        "TIBIO3.MC",
        "TIBIO4.MC",
    ]
    todos = tickers + [sig_eng.TICKER_BENCHMARK]

    cols = pd.MultiIndex.from_product(
        [todos, ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)
    indices_emision = [62, 72, 82]
    tilts: Dict[str, float] = {}

    for idx_tk, tk in enumerate(todos):
        p0 = 25.0 + idx_tk * 8.0
        closes = np.zeros(n_sesiones, dtype=float)
        vols = np.full(n_sesiones, 100000.0, dtype=float)
        closes[0] = p0

        if tk == sig_eng.TICKER_BENCHMARK:
            for i in range(1, n_sesiones):
                closes[i] = p0 * (1.0 + 0.0015 * np.sin(i * 0.35))
            highs = closes * 1.005
            lows = closes * 0.995
            opens = np.roll(closes, 1)
            opens[0] = closes[0]
        elif tk.startswith("FUERTE_"):
            signo = 1.0 if tk.endswith("UP.MC") else -1.0
            tilts[tk] = 1.5 * signo
            for i in range(1, n_sesiones):
                # Tendencia clara y sostenida con volatilidad suficiente para superar 2x umbral
                closes[i] = closes[i - 1] * (1.0 + signo * 0.016)
            for te in indices_emision:
                closes[te + 1] = closes[te] * (1.0 + signo * 0.022)
                closes[te + 2] = closes[te] * (1.0 + signo * 0.036)
                closes[te + 3] = closes[te] * (1.0 + signo * 0.050)
                closes[te + 4] = closes[te] * (1.0 + signo * 0.062)
                closes[te + 5] = closes[te] * (1.0 + signo * 0.075)
            opens = np.roll(closes, 1)
            opens[0] = closes[0]
            highs = np.maximum(opens, closes) * 1.018
            lows = np.minimum(opens, closes) * 0.982
        else:
            # Activos tibios: pequena deriva con baja volatilidad que genera senales
            # direccionales de baja magnitud (pct_raw cercano al umbral lateral),
            # pero cuya resolucion real queda plana dentro de la zona muerta LATERAL.
            signo = 1.0 if (idx_tk % 2 == 0) else -1.0
            tilts[tk] = 0.25 * signo
            for i in range(1, n_sesiones):
                closes[i] = closes[i - 1] * (1.0 + signo * 0.0035)
            for te in indices_emision:
                # Resolucion plana en la zona muerta para 1D, 3D y 1S
                for k in range(1, 6):
                    closes[te + k] = closes[te] * (1.0 + signo * 0.0008 * k)
            opens = np.roll(closes, 1)
            opens[0] = closes[0]
            highs = np.maximum(opens, closes) * 1.006
            lows = np.minimum(opens, closes) * 0.994

        df[(tk, "Open")] = opens
        df[(tk, "High")] = highs
        df[(tk, "Low")] = lows
        df[(tk, "Close")] = closes
        df[(tk, "Volume")] = vols

    fechas_emision = [
        pd.Timestamp(fechas[i]).strftime("%Y-%m-%d") for i in indices_emision
    ]
    return df, tickers, fechas_emision, tilts


class TestC17UmbralConviccionDireccional(unittest.TestCase):
    """Suite de pruebas unitarias para el Paso C-17."""

    def setUp(self):
        self.dt_fijo = datetime(2026, 11, 22, 12, 0, tzinfo=MADRID_TZ)

    def test_a_jerarquia_e_inmutabilidad_configuraciones(self):
        """
        (a) Jerarquia e inmutabilidad de configuraciones:
        Verifica que baseline_v5, challenger_c16, challenger_c17_moderado y
        challenger_c17 tienen params_hash distintos y deterministas, que
        challenger_c17 hereda la re-ponderacion de C-16 y anade
        multiplicador_conviccion_por_hz > 1.0, y que services/signal_engine.py
        permanece intacto.
        """
        pesos_tend_antes = copy.deepcopy(sig_eng.PESOS_TENDENCIA)
        pesos_rango_antes = copy.deepcopy(sig_eng.PESOS_RANGO)
        pesos_trans_antes = copy.deepcopy(sig_eng.PESOS_TRANSICION)
        umbrales_antes = copy.deepcopy(sig_eng.UMBRALES_LATERAL)
        hash_motor_antes = sig_eng.calcular_params_hash_motor()
        estado_v5_antes = copy.deepcopy(sig_eng.verificar_estado_congelado_v5())

        cfg_base = ConfigExperimentoMotor()
        cfg_c16 = construir_config_challenger_c16()
        cfg_c17_mod = construir_config_challenger_c17_moderado()
        cfg_c17 = construir_config_challenger_c17()

        h_base = calcular_hash_config(cfg_base)
        h_c16 = calcular_hash_config(cfg_c16)
        h_c17_mod = calcular_hash_config(cfg_c17_mod)
        h_c17 = calcular_hash_config(cfg_c17)

        # Determinismo y unicidad de hashes
        self.assertEqual(h_base, calcular_hash_config(ConfigExperimentoMotor()))
        self.assertEqual(h_c16, calcular_hash_config(construir_config_challenger_c16()))
        self.assertEqual(
            h_c17_mod, calcular_hash_config(construir_config_challenger_c17_moderado())
        )
        self.assertEqual(h_c17, calcular_hash_config(construir_config_challenger_c17()))
        self.assertEqual(len({h_base, h_c16, h_c17_mod, h_c17}), 4)
        for h in (h_base, h_c16, h_c17_mod, h_c17):
            self.assertEqual(len(h), 64)

        # Herencia exacta de C-16 en challenger_c17 y challenger_c17_moderado
        for cfg in (cfg_c17_mod, cfg_c17):
            self.assertEqual(cfg.pesos_horizonte, cfg_c16.pesos_horizonte)
            self.assertEqual(cfg.peso_tilt_por_hz, cfg_c16.peso_tilt_por_hz)
            self.assertEqual(
                cfg.reversion_sobreextension_1d, cfg_c16.reversion_sobreextension_1d
            )
            self.assertEqual(
                cfg.umbral_sobreextension_1d, cfg_c16.umbral_sobreextension_1d
            )
            self.assertEqual(
                cfg.vol_confirm_boost_por_hz, cfg_c16.vol_confirm_boost_por_hz
            )
            self.assertEqual(
                cfg.vol_no_confirm_damp_por_hz, cfg_c16.vol_no_confirm_damp_por_hz
            )
            self.assertEqual(
                cfg.amortiguacion_rvol_pico_por_hz,
                cfg_c16.amortiguacion_rvol_pico_por_hz,
            )
            self.assertEqual(cfg.umbral_rvol_pico, cfg_c16.umbral_rvol_pico)

        # Jerarquia de multiplicadores en 1D, 3D, 1S (y 1M neutro = 1.0)
        for hz in ("1D", "3D", "1S"):
            self.assertEqual(cfg_base.multiplicador_conviccion_por_hz[hz], 1.0)
            self.assertEqual(cfg_c16.multiplicador_conviccion_por_hz[hz], 1.0)
            self.assertGreater(cfg_c17_mod.multiplicador_conviccion_por_hz[hz], 1.0)
            self.assertGreaterEqual(
                cfg_c17.multiplicador_conviccion_por_hz[hz],
                cfg_c17_mod.multiplicador_conviccion_por_hz[hz],
            )
        self.assertEqual(cfg_c17_mod.multiplicador_conviccion_por_hz["1M"], 1.0)
        self.assertEqual(cfg_c17.multiplicador_conviccion_por_hz["1M"], 1.0)

        # signal_engine permanece intacto
        self.assertEqual(sig_eng.PESOS_TENDENCIA, pesos_tend_antes)
        self.assertEqual(sig_eng.PESOS_RANGO, pesos_rango_antes)
        self.assertEqual(sig_eng.PESOS_TRANSICION, pesos_trans_antes)
        self.assertEqual(sig_eng.UMBRALES_LATERAL, umbrales_antes)
        self.assertEqual(sig_eng.calcular_params_hash_motor(), hash_motor_antes)
        self.assertEqual(sig_eng.verificar_estado_congelado_v5(), estado_v5_antes)

    def test_b_monotonia_selectividad_y_mejora_3_clases(self):
        """
        (b) Monotonia de selectividad y mejora de 3 clases:
        Verifica que al aplicar challenger_c17_moderado y challenger_c17 sobre un
        mismo panel, %LATERAL(base) <= %LATERAL(c17_mod) <= %LATERAL(c17) < 92%,
        conservando senales direccionales activas (n_direccionales > 0), y que el
        acierto en 3 clases sube al filtrar senales tibias que caen en zona muerta.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_emision, tilts = (
                _construir_panel_conviccion_mixta()
            )
            estudio = ejecutar_estudio_umbrales_c17(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )

            lat_base = estudio["baseline_v5"]["pct_lateral"]
            lat_c16 = estudio["challenger_c16"]["pct_lateral"]
            lat_c17_mod = estudio["challenger_c17_moderado"]["pct_lateral"]
            lat_c17 = estudio["challenger_c17"]["pct_lateral"]

            self.assertLessEqual(lat_base, lat_c17_mod)
            self.assertLessEqual(lat_c16, lat_c17_mod)
            self.assertLessEqual(lat_c17_mod, lat_c17)
            self.assertLess(lat_c17, 92.0)

            for k in (
                "baseline_v5",
                "challenger_c16",
                "challenger_c17_moderado",
                "challenger_c17",
            ):
                self.assertGreater(
                    estudio[k]["n_direccionales"],
                    0,
                    f"{k} debe conservar senales direccionales activas",
                )

            t3c_base = estudio["baseline_v5"]["tasa_3c_v1_c10"]
            t3c_c16 = estudio["challenger_c16"]["tasa_3c_v1_c10"]
            t3c_c17 = estudio["challenger_c17"]["tasa_3c_v1_c10"]
            self.assertGreater(t3c_c17, t3c_base)
            self.assertGreater(t3c_c17, t3c_c16)

    def test_c_ejecucion_completa_estudio_umbrales_c17(self):
        """
        (c) Ejecucion completa de ejecutar_estudio_umbrales_c17:
        Comprueba que devuelve las 4 variantes con sus comparaciones pareadas contra
        baseline_v5 y contra challenger_c16, incluyendo las 5 referencias ingenuas
        de C-14, y coincide exactamente con simular_emision_y_resolucion_wf directo.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_emision, tilts = (
                _construir_panel_conviccion_mixta()
            )
            estudio = ejecutar_estudio_umbrales_c17(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )

            claves = (
                "baseline_v5",
                "challenger_c16",
                "challenger_c17_moderado",
                "challenger_c17",
            )
            for k in claves:
                self.assertIn(k, estudio)
                self.assertIn(k, estudio["variantes"])
                self.assertIn(k, estudio["selectividad"])
                self.assertIn(k, estudio["pct_direccionales"])
                var = estudio[k]
                self.assertIn("pct_direccionales", var)
                self.assertIn("pct_lateral", var)
                self.assertIn("pct_direccionales_por_horizonte", var)
                self.assertIn("referencias_ingenuas_c14", var)
                self.assertIn("etiqueta_muestra", var)
                self.assertEqual(var["n_fechas"], len(fechas_emision))

            # Verificar las 5 referencias ingenuas de C-14 (plus motor = 6 entradas)
            refs_c14 = estudio["referencias_ingenuas_c14"]
            self.assertIsNotNone(refs_c14)
            self.assertIn("tabla", refs_c14)
            self.assertIn("por_codigo", refs_c14)
            codigos_ingenuos = [
                c for c in refs_c14["por_codigo"].keys() if c != "motor"
            ]
            self.assertEqual(len(codigos_ingenuos), 5)
            self.assertIn("siempre_lateral", refs_c14["por_codigo"])

            # Comparaciones pareadas contra baseline_v5
            for k_ch in ("challenger_c16", "challenger_c17_moderado", "challenger_c17"):
                self.assertIn(k_ch, estudio["comparaciones_vs_baseline"])
                comp = estudio["comparaciones_vs_baseline"][k_ch]
                self.assertEqual(comp["n_fechas_comunes"], len(fechas_emision))
                self.assertIn("delta_3c_v1_c10_pp", comp)
                self.assertIn("delta_3c_v2_c12_pp", comp)
                self.assertIn("delta_signo_c10_pp", comp)
                self.assertIn("tests_pareados", comp)

            # Comparaciones pareadas contra challenger_c16
            self.assertIn("comparacion_c17_vs_c16", estudio)
            self.assertIn("challenger_c17", estudio["comparaciones_vs_c16"])
            self.assertIn("challenger_c17_moderado", estudio["comparaciones_vs_c16"])
            comp_17_16 = estudio["comparacion_c17_vs_c16"]
            self.assertEqual(comp_17_16["n_fechas_comunes"], len(fechas_emision))
            self.assertEqual(
                comp_17_16["baseline_hash"],
                calcular_hash_config(construir_config_challenger_c16()),
            )
            self.assertEqual(
                comp_17_16["challenger_hash"],
                calcular_hash_config(construir_config_challenger_c17()),
            )

            # Equivalencia bit a bit con simular_emision_y_resolucion_wf directo
            df_c17_directo = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision[:2],
                tickers=tickers[:3],
                config=construir_config_challenger_c17(),
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            df_c17_est = estudio["challenger_c17"]["df_simulacion"]
            df_c17_sub = df_c17_est[
                df_c17_est["fecha_prediccion"].isin(fechas_emision[:2])
                & df_c17_est["ticker"].isin(tickers[:3])
            ].reset_index(drop=True)
            pd.testing.assert_frame_equal(
                df_c17_directo.reset_index(drop=True),
                df_c17_sub,
                check_exact=True,
            )

    def test_d_validacion_solo_lectura_parquet_e_inmutabilidad_stocklab_db(self):
        """
        (d) Validacion en solo lectura sobre data/backtest_cache_6y.parquet e
        inmutabilidad de stocklab.db:
        Comprueba sobre una ventana fuera de muestra del Parquet local (Holdout Vivo)
        que challenger_c17 mejora simultaneamente el Acierto en 3 clases (v1 y v2) y
        el Acierto de signo respecto a baseline_v5, y que stocklab.db mantiene
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
            fechas_vivo_db = [
                r[0]
                for r in conn_ro.execute(
                    "SELECT DISTINCT fecha_prediccion FROM predicciones "
                    "WHERE fecha_prediccion >= '2026-10-12' AND fecha_prediccion <= '2026-11-13' "
                    "ORDER BY fecha_prediccion ASC"
                ).fetchall()
            ]
        finally:
            conn_ro.close()

        self.assertEqual(cambios_antes, 0)

        df_parquet = pd.read_parquet(PARQUET_PATH)
        # Muestra estratificada alfabetica representativa del universo BME completo
        disponibles = sorted(
            t
            for t in df_parquet.columns.get_level_values(0).unique()
            if t != sig_eng.TICKER_BENCHMARK
        )
        tickers_reales = disponibles[::4][:25]
        cols_needed = [
            c
            for c in df_parquet.columns
            if c[0] in set(tickers_reales + [sig_eng.TICKER_BENCHMARK])
        ]

        fechas_all = [
            pd.Timestamp(ts).strftime("%Y-%m-%d") for ts in df_parquet.index
        ]
        fechas_all_set = set(fechas_all)
        fechas_oos = [f for f in fechas_vivo_db if f in fechas_all_set][::3][:5]
        idx_fin = len(fechas_all) - 1
        idx_ini = max(0, idx_fin - 150)
        df_ventana = df_parquet.loc[:, cols_needed].iloc[idx_ini : idx_fin + 1].copy()

        with _reloj_madrid(self.dt_fijo):
            estudio_oos = ejecutar_estudio_umbrales_c17(
                df_panel=df_ventana,
                fechas_emision=fechas_oos,
                tickers=tickers_reales,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts_db,
            )

        comp_c17 = estudio_oos["comparaciones_vs_baseline"]["challenger_c17"]
        self.assertGreater(
            comp_c17["delta_3c_v1_c10_pp"],
            0.0,
            f"challenger_c17 debe mejorar 3c_v1 en OOS ({comp_c17['delta_3c_v1_c10_pp']} pp)",
        )
        self.assertGreater(
            comp_c17["delta_3c_v2_c12_pp"],
            0.0,
            f"challenger_c17 debe mejorar 3c_v2 en OOS ({comp_c17['delta_3c_v2_c12_pp']} pp)",
        )
        self.assertGreater(
            comp_c17["delta_signo_c10_pp"],
            0.0,
            f"challenger_c17 debe mejorar signo C10 en OOS ({comp_c17['delta_signo_c10_pp']} pp)",
        )
        self.assertGreater(estudio_oos["challenger_c17"]["n_direccionales"], 0)
        self.assertLess(estudio_oos["challenger_c17"]["pct_lateral"], 92.0)

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
