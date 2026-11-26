"""
Tests unitarios para el Paso C-19: Filtro de Regimen de Mercado (marea) y
Fuerza Relativa (rs_ibex) en Walk-Forward
=========================================================================
Verifica de forma determinista y sin efectos secundarios en red ni disco:
  (a) Efecto selectivo de filtros: sobre un panel sintetico donde el IBEX es
      claramente BAJISTA, verifica que activando filtro_marea_estricto=True las
      senales individualmente ALCISTA pasan a 'LATERAL' y mejora el acierto de
      signo de las sobrevivientes; verifica tambien el filtro umbral_rs_ibex,
      la funcion _extraer_marea y cero look-ahead.
  (b) Jerarquia e inmutabilidad de configs: el hash de challenger_c19 difiere
      de baseline_v5, challenger_c16, challenger_c17 y challenger_c18b, hereda
      sus parametros sin alterar baseline_v5 ni services/signal_engine.py.
  (c) Evaluacion de estudio completo: ejecutar_estudio_filtros_c19 devuelve
      baseline_v5, challenger_c18b y challenger_c19 con metricas 3 clases,
      signo C10/C11, monotonia de confianza y pareados contra challenger_c18b
      y baseline_v5, en equivalencia bit a bit con simular_emision_y_resolucion_wf.
  (d) Inmutabilidad en BD real: ejecucion en Val OOS 2026 sobre el Parquet local
      confirmando total_changes == 0 y hashes SHA-256 identicos de stocklab.db
      y data/backtest_cache_6y.parquet antes y despues.
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
    FILTRO_MAREA_ESTRICTO_C19,
    TABLA_CALIBRACION_C18_TRAIN,
    UMBRAL_RS_IBEX_C19,
    ConfigExperimentoMotor,
    _aplicar_filtros_macro_rs_c19,
    _extraer_marea,
    calcular_hash_config,
    construir_config_challenger_c16,
    construir_config_challenger_c17,
    construir_config_challenger_c18b,
    construir_config_challenger_c19,
    ejecutar_estudio_filtros_c19,
    evaluar_experimento_wf,
    simular_emision_y_resolucion_wf,
)
import utils.market_calendar as mc

MADRID_TZ = ZoneInfo("Europe/Madrid")
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REAL_DB_PATH = os.path.join(REPO_ROOT, "stocklab.db")
PARQUET_PATH = os.path.join(REPO_ROOT, "data", "backtest_cache_6y.parquet")


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Parchea get_ahora_madrid en market_calendar, emision y predicciones."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _sha256_fichero(ruta: str) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _construir_panel_marea_bajista_c19() -> Tuple[
    pd.DataFrame, List[str], List[str], Dict[str, float]
]:
    """
    Construye un panel OHLCV sintetico determinista donde:
      - ^IBEX cae de forma sostenida -> marea == 'BAJISTA' en las fechas de emision.
      - BULL_TRAP1.MC y BULL_TRAP2.MC muestran tendencia alcista previa (generan
        senal ALCISTA individual en contra de la marea) pero revierten a la baja
        tras la emision (trampas alcistas que fallan en signo).
      - BEAR_OK1.MC y BEAR_OK2.MC muestran tendencia bajista alineada con la marea
        y siguen cayendo tras la emision (aciertan en signo).
    """
    n_sesiones = 105
    fechas = mc.bdate_range_bme("2026-04-01", periods=n_sesiones)
    tickers = ["BULL_TRAP1.MC", "BULL_TRAP2.MC", "BEAR_OK1.MC", "BEAR_OK2.MC"]
    todos = tickers + [sig_eng.TICKER_BENCHMARK]

    cols = pd.MultiIndex.from_product(
        [todos, ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)
    indices_emision = [60, 76, 92]
    tilts = {
        "BULL_TRAP1.MC": 1.6,
        "BULL_TRAP2.MC": 1.6,
        "BEAR_OK1.MC": -1.6,
        "BEAR_OK2.MC": -1.6,
    }

    for idx_tk, tk in enumerate(todos):
        p0 = 30.0 + idx_tk * 6.0
        closes = np.zeros(n_sesiones, dtype=float)
        vols = np.full(n_sesiones, 120000.0, dtype=float)
        closes[0] = p0

        if tk == sig_eng.TICKER_BENCHMARK:
            for i in range(1, n_sesiones):
                closes[i] = closes[i - 1] * 0.992
            opens = np.roll(closes, 1)
            opens[0] = closes[0]
            highs = np.maximum(opens, closes) * 1.006
            lows = np.minimum(opens, closes) * 0.994
        elif tk.startswith("BULL_TRAP"):
            # Base suave y aceleracion alcista en las 8 sesiones previas a cada
            # fecha de emision para lograr consenso 4/4 (incluyendo MACD_hist > 0),
            # seguida de caida tras la fecha de emision (trampa alcista).
            for i in range(1, n_sesiones):
                closes[i] = closes[i - 1] * 1.001
            for te in indices_emision:
                for k in range(8, 0, -1):
                    idx_pre = te - k + 1
                    closes[idx_pre] = closes[idx_pre - 1] * 1.022
                for k in range(1, 6):
                    closes[te + k] = closes[te] * (1.0 - 0.015 * k)
                if te + 6 < n_sesiones:
                    for j in range(te + 6, n_sesiones):
                        closes[j] = closes[j - 1] * 1.002
            opens = np.roll(closes, 1)
            opens[0] = closes[0]
            highs = np.maximum(opens, closes) * 1.012
            lows = np.minimum(opens, closes) * 0.988
        else:
            # Tendencia bajista con aceleracion previa a cada emision y
            # continuacion bajista posterior (acierta en signo).
            for i in range(1, n_sesiones):
                closes[i] = closes[i - 1] * 0.996
            for te in indices_emision:
                for k in range(8, 0, -1):
                    idx_pre = te - k + 1
                    closes[idx_pre] = closes[idx_pre - 1] * 0.978
                for k in range(1, 6):
                    closes[te + k] = closes[te] * (1.0 - 0.015 * k)
                if te + 6 < n_sesiones:
                    for j in range(te + 6, n_sesiones):
                        closes[j] = closes[j - 1] * 0.997
            opens = np.roll(closes, 1)
            opens[0] = closes[0]
            highs = np.maximum(opens, closes) * 1.012
            lows = np.minimum(opens, closes) * 0.988

        df[(tk, "Open")] = opens
        df[(tk, "High")] = highs
        df[(tk, "Low")] = lows
        df[(tk, "Close")] = closes
        df[(tk, "Volume")] = vols

    fechas_emision = [
        pd.Timestamp(fechas[i]).strftime("%Y-%m-%d") for i in indices_emision
    ]
    return df, tickers, fechas_emision, tilts


class TestC19FiltroMareaRsIbex(unittest.TestCase):
    """Suite de pruebas unitarias para el Paso C-19."""

    def setUp(self):
        self.dt_fijo = datetime(2026, 11, 23, 20, 0, tzinfo=MADRID_TZ)

    def test_a_efecto_selectivo_filtros_marea_y_rs(self):
        """
        (a) Efecto selectivo de filtros: sobre un panel sintetico donde ^IBEX
        es BAJISTA, verifica que filtro_marea_estricto=True reclasifica las
        senales ALCISTA a 'LATERAL' y eleva el acierto de signo de las
        sobrevivientes; comprueba tambien umbral_rs_ibex y cero look-ahead.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_emision, tilts = (
                _construir_panel_marea_bajista_c19()
            )
            cfg_sin = ConfigExperimentoMotor(
                nombre_modelo="c19-test-sin-filtro",
                filtro_marea_estricto=False,
                umbral_rs_ibex=0.0,
            )
            cfg_marea = ConfigExperimentoMotor(
                nombre_modelo="c19-test-marea",
                filtro_marea_estricto=True,
                umbral_rs_ibex=0.0,
            )

            df_sin = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                config=cfg_sin,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            df_marea = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                config=cfg_marea,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )

            self.assertGreater(len(df_sin), 0)
            self.assertTrue((df_sin["marea"] == sig_eng.MAREA_BAJISTA).all())

            # Sin filtro deben existir senales ALCISTA (bull traps) y BAJISTA
            alc_sin = df_sin[df_sin["senal"] == sig_eng.SENAL_ALCISTA]
            baj_sin = df_sin[df_sin["senal"] == sig_eng.SENAL_BAJISTA]
            self.assertGreater(len(alc_sin), 0)
            self.assertGreater(len(baj_sin), 0)

            # Con filtro_marea_estricto=True, ninguna ALCISTA sobrevive bajo marea BAJISTA
            alc_marea = df_marea[df_marea["senal"] == sig_eng.SENAL_ALCISTA]
            baj_marea = df_marea[df_marea["senal"] == sig_eng.SENAL_BAJISTA]
            self.assertEqual(len(alc_marea), 0)
            self.assertEqual(len(baj_marea), len(baj_sin))

            # Las filas que eran ALCISTA pasan a LATERAL con conviccion BAJA y pct acotado
            idx_alc = alc_sin.index
            self.assertTrue((df_marea.loc[idx_alc, "senal"] == sig_eng.SENAL_LATERAL).all())
            self.assertTrue((df_marea.loc[idx_alc, "conviccion"] == "BAJA").all())
            self.assertTrue(
                (
                    df_marea.loc[idx_alc, "pct_estimado"].abs()
                    <= df_marea.loc[idx_alc, "umbral_v1"] + 1e-9
                ).all()
            )

            # Mejora el acierto de signo en las senales direccionales sobrevivientes
            ev_sin = evaluar_experimento_wf(df_sin, df_panel=df_panel)
            ev_marea = evaluar_experimento_wf(df_marea, df_panel=df_panel)
            self.assertIsNotNone(ev_sin["tasa_signo_c10"])
            self.assertIsNotNone(ev_marea["tasa_signo_c10"])
            self.assertGreater(ev_marea["tasa_signo_c10"], ev_sin["tasa_signo_c10"])

            # Comprobacion de fronteras de umbral_rs_ibex y _extraer_marea
            self.assertEqual(_extraer_marea({"marea": "bajista"}, None), "BAJISTA")
            self.assertEqual(_extraer_marea({"marea": "BAJISTA"}, {"estado": "ALCISTA"}), "ALCISTA")
            self.assertEqual(_extraer_marea({}, None), "NEUTRA")

            cfg_rs = ConfigExperimentoMotor(filtro_marea_estricto=False, umbral_rs_ibex=2.0)
            s_alc_ok, _, _, f1 = _aplicar_filtros_macro_rs_c19(
                "ALCISTA", 1.5, 1.5, "ALTA", 0.8, "NEUTRA", -1.9, cfg_rs
            )
            s_alc_bloq, p_alc_bloq, c_alc_bloq, f2 = _aplicar_filtros_macro_rs_c19(
                "ALCISTA", 1.5, 1.5, "ALTA", 0.8, "NEUTRA", -2.0, cfg_rs
            )
            s_baj_ok, _, _, f3 = _aplicar_filtros_macro_rs_c19(
                "BAJISTA", -1.5, -1.5, "ALTA", 0.8, "NEUTRA", 1.9, cfg_rs
            )
            s_baj_bloq, p_baj_bloq, c_baj_bloq, f4 = _aplicar_filtros_macro_rs_c19(
                "BAJISTA", -1.5, -1.5, "ALTA", 0.8, "NEUTRA", 2.0, cfg_rs
            )
            self.assertEqual((s_alc_ok, f1), ("ALCISTA", False))
            self.assertEqual((s_alc_bloq, p_alc_bloq, c_alc_bloq, f2), ("LATERAL", 0.8, "BAJA", True))
            self.assertEqual((s_baj_ok, f3), ("BAJISTA", False))
            self.assertEqual((s_baj_bloq, p_baj_bloq, c_baj_bloq, f4), ("LATERAL", -0.8, "BAJA", True))

            # Cero look-ahead para challenger_c19 ante perturbacion extrema post-emision
            f0 = fechas_emision[0]
            cfg_c19 = construir_config_challenger_c19()
            df_c19_orig = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=[f0],
                tickers=tickers,
                config=cfg_c19,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            df_pert = df_panel.copy()
            mask_fut = pd.Series(
                [pd.Timestamp(ts).strftime("%Y-%m-%d") > f0 for ts in df_pert.index],
                index=df_pert.index,
            )
            for col in df_pert.columns:
                df_pert.loc[mask_fut, col] = df_pert.loc[mask_fut, col] * 4.0
            df_c19_pert = simular_emision_y_resolucion_wf(
                df_panel=df_pert,
                fechas_emision=[f0],
                tickers=tickers,
                config=cfg_c19,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            for col in ("senal", "pct_estimado", "confianza", "marea", "rs_ibex"):
                pd.testing.assert_series_equal(
                    df_c19_orig[col].reset_index(drop=True),
                    df_c19_pert[col].reset_index(drop=True),
                )

    def test_b_jerarquia_e_inmutabilidad_configs(self):
        """
        (b) Jerarquia e inmutabilidad de configs: challenger_c19 hereda C-16,
        C-17 y C-18b, tiene params_hash unico y determinista distinto al resto,
        no altera baseline_v5 y mantiene intacto services/signal_engine.py.
        """
        pesos_tend_antes = copy.deepcopy(sig_eng.PESOS_TENDENCIA)
        pesos_rango_antes = copy.deepcopy(sig_eng.PESOS_RANGO)
        umbrales_antes = copy.deepcopy(sig_eng.UMBRALES_LATERAL)
        hash_motor_antes = sig_eng.calcular_params_hash_motor()
        estado_v5_antes = copy.deepcopy(sig_eng.verificar_estado_congelado_v5())

        cfg_base = ConfigExperimentoMotor()
        cfg_c16 = construir_config_challenger_c16()
        cfg_c17 = construir_config_challenger_c17()
        cfg_c18b = construir_config_challenger_c18b()
        cfg_c19 = construir_config_challenger_c19()

        # Defaults neutros en baseline_v5 y versiones anteriores
        for cfg_prev in (cfg_base, cfg_c16, cfg_c17, cfg_c18b):
            self.assertFalse(cfg_prev.filtro_marea_estricto)
            self.assertEqual(cfg_prev.umbral_rs_ibex, 0.0)

        # Herencia completa en challenger_c19
        self.assertEqual(cfg_c19.pesos_horizonte, cfg_c18b.pesos_horizonte)
        self.assertEqual(cfg_c19.peso_tilt_por_hz, cfg_c18b.peso_tilt_por_hz)
        self.assertEqual(cfg_c19.reversion_sobreextension_1d, cfg_c18b.reversion_sobreextension_1d)
        self.assertEqual(cfg_c19.vol_confirm_boost_por_hz, cfg_c18b.vol_confirm_boost_por_hz)
        self.assertEqual(cfg_c19.vol_no_confirm_damp_por_hz, cfg_c18b.vol_no_confirm_damp_por_hz)
        self.assertEqual(
            cfg_c19.amortiguacion_rvol_pico_por_hz, cfg_c18b.amortiguacion_rvol_pico_por_hz
        )
        self.assertEqual(
            cfg_c19.multiplicador_conviccion_por_hz, cfg_c18b.multiplicador_conviccion_por_hz
        )
        self.assertEqual(cfg_c19.modo_confianza, "v6_calibrada")
        self.assertEqual(cfg_c19.tabla_calibracion_confianza, TABLA_CALIBRACION_C18_TRAIN)

        # Reglas especificas de C-19
        self.assertEqual(cfg_c19.filtro_marea_estricto, FILTRO_MAREA_ESTRICTO_C19)
        self.assertEqual(cfg_c19.umbral_rs_ibex, UMBRAL_RS_IBEX_C19)
        self.assertTrue(cfg_c19.filtro_marea_estricto)
        self.assertGreater(cfg_c19.umbral_rs_ibex, 0.0)

        # Hashes deterministas y distintos entre todas las variantes
        hashes = [
            calcular_hash_config(cfg_base),
            calcular_hash_config(cfg_c16),
            calcular_hash_config(cfg_c17),
            calcular_hash_config(cfg_c18b),
            calcular_hash_config(cfg_c19),
        ]
        self.assertEqual(len(set(hashes)), 5)
        for h in hashes:
            self.assertEqual(len(h), 64)
        self.assertEqual(hashes[-1], calcular_hash_config(construir_config_challenger_c19()))

        # Sensibilidad del hash a cambios en filtro_marea_estricto y umbral_rs_ibex
        cfg_c19_sin_marea = construir_config_challenger_c19()
        cfg_c19_sin_marea.filtro_marea_estricto = False
        self.assertNotEqual(calcular_hash_config(cfg_c19_sin_marea), hashes[-1])

        cfg_c19_otro_rs = construir_config_challenger_c19()
        cfg_c19_otro_rs.umbral_rs_ibex = 2.0
        self.assertNotEqual(calcular_hash_config(cfg_c19_otro_rs), hashes[-1])

        # signal_engine intacto
        self.assertEqual(sig_eng.PESOS_TENDENCIA, pesos_tend_antes)
        self.assertEqual(sig_eng.PESOS_RANGO, pesos_rango_antes)
        self.assertEqual(sig_eng.UMBRALES_LATERAL, umbrales_antes)
        self.assertEqual(sig_eng.calcular_params_hash_motor(), hash_motor_antes)
        self.assertEqual(sig_eng.verificar_estado_congelado_v5(), estado_v5_antes)

    def test_c_evaluacion_estudio_completo_c19(self):
        """
        (c) Evaluacion de estudio completo: ejecutar_estudio_filtros_c19 devuelve
        baseline_v5, challenger_c18b y challenger_c19 con metricas de 3 clases,
        signo C10/C11, monotonia, pareados contra c18b y baseline_v5, y coincide
        bit a bit con simular_emision_y_resolucion_wf directo.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_emision, tilts = (
                _construir_panel_marea_bajista_c19()
            )
            estudio = ejecutar_estudio_filtros_c19(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )

            for k in ("baseline_v5", "challenger_c18b", "challenger_c19"):
                self.assertIn(k, estudio)
                self.assertIn(k, estudio["variantes"])
                self.assertIn(k, estudio["selectividad"])
                self.assertIn(k, estudio["monotonia_confianza"])
                var = estudio[k]
                self.assertIn("tasa_3c_v1_c10", var)
                self.assertIn("tasa_3c_v2_c12", var)
                self.assertIn("tasa_signo_c10", var)
                self.assertIn("tasa_signo_c11", var)
                self.assertIn("pct_direccionales", var)
                self.assertIn("monotonia_confianza", var)
                self.assertEqual(var["n_fechas"], len(fechas_emision))

            # Comparaciones pareadas presentes y consistentes
            self.assertIn("comparacion_c19_vs_c18b", estudio)
            self.assertIn("comparacion_c19_vs_baseline", estudio)
            self.assertIn("challenger_c18b", estudio["comparaciones_vs_baseline"])
            self.assertIn("challenger_c19", estudio["comparaciones_vs_baseline"])

            comp_c19_c18b = estudio["comparacion_c19_vs_c18b"]
            comp_c19_base = estudio["comparacion_c19_vs_baseline"]
            for comp in (comp_c19_c18b, comp_c19_base):
                self.assertEqual(comp["n_fechas_comunes"], len(fechas_emision))
                self.assertIn("delta_3c_v1_c10_pp", comp)
                self.assertIn("delta_3c_v2_c12_pp", comp)
                self.assertIn("delta_signo_c10_pp", comp)
                self.assertIn("delta_signo_c11_pp", comp)
                self.assertIn("tests_pareados", comp)

            self.assertEqual(
                comp_c19_c18b["baseline_hash"],
                calcular_hash_config(construir_config_challenger_c18b()),
            )
            self.assertEqual(
                comp_c19_c18b["challenger_hash"],
                calcular_hash_config(construir_config_challenger_c19()),
            )

            # Equivalencia bit a bit con simular_emision_y_resolucion_wf directo
            df_c19_directo = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision[:2],
                tickers=tickers[:3],
                config=construir_config_challenger_c19(),
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            df_c19_est = estudio["challenger_c19"]["df_simulacion"]
            df_c19_sub = df_c19_est[
                df_c19_est["fecha_prediccion"].isin(fechas_emision[:2])
                & df_c19_est["ticker"].isin(tickers[:3])
            ].reset_index(drop=True)
            pd.testing.assert_frame_equal(
                df_c19_directo.reset_index(drop=True),
                df_c19_sub,
                check_exact=True,
            )

    def test_d_inmutabilidad_bd_real_val_oos_2026(self):
        """
        (d) Inmutabilidad en BD real: ejecuta ejecutar_estudio_filtros_c19 sobre
        una ventana Val OOS 2026 del Parquet local leyendo tilts en modo solo
        lectura y verifica que stocklab.db y data/backtest_cache_6y.parquet
        mantienen hashes SHA-256 identicos y total_changes == 0.
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
            sha_inm11_antes = db_pred._calcular_sha256_inmutables(conn_ro)
            sha_inm10_antes = db_pred._calcular_sha256_inmutables_10_sin_modelo(conn_ro)
            sha_inm12_antes = db_pred._calcular_sha256_inmutables_11_desde_corte(conn_ro)
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
        disponibles = sorted(
            t
            for t in df_parquet.columns.get_level_values(0).unique()
            if t != sig_eng.TICKER_BENCHMARK
        )
        tickers_oos = disponibles[::4][:25]
        cols_needed = [
            c
            for c in df_parquet.columns
            if c[0] in set(tickers_oos + [sig_eng.TICKER_BENCHMARK])
        ]
        fechas_all = [
            pd.Timestamp(ts).strftime("%Y-%m-%d") for ts in df_parquet.index
        ]
        fechas_val_2026 = [
            f for f in fechas_all if "2026-02-01" <= f <= "2026-09-15"
        ][::25][:6]
        idx_fin = max(i for i, f in enumerate(fechas_all) if f <= "2026-10-05")
        idx_ini = max(0, idx_fin - 260)
        df_ventana = df_parquet.loc[:, cols_needed].iloc[idx_ini : idx_fin + 1].copy()

        with _reloj_madrid(self.dt_fijo):
            estudio_oos = ejecutar_estudio_filtros_c19(
                df_panel=df_ventana,
                fechas_emision=fechas_val_2026,
                tickers=tickers_oos,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts_db,
            )

        df_c19 = estudio_oos["challenger_c19"]["df_simulacion"]
        self.assertGreater(len(df_c19), 0)
        self.assertLessEqual(
            estudio_oos["challenger_c19"]["pct_direccionales"],
            estudio_oos["challenger_c18b"]["pct_direccionales"],
        )
        self.assertIn("comparacion_c19_vs_c18b", estudio_oos)
        self.assertIn("comparacion_c19_vs_baseline", estudio_oos)

        sha_db_despues = _sha256_fichero(REAL_DB_PATH)
        sha_pq_despues = _sha256_fichero(PARQUET_PATH)
        conn_ro2 = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            self.assertEqual(conn_ro2.total_changes, 0)
            sha_inm11_despues = db_pred._calcular_sha256_inmutables(conn_ro2)
            sha_inm10_despues = db_pred._calcular_sha256_inmutables_10_sin_modelo(conn_ro2)
            sha_inm12_despues = db_pred._calcular_sha256_inmutables_11_desde_corte(conn_ro2)
            sha_res_despues = db_pred._calcular_sha256_resueltas(conn_ro2)
        finally:
            conn_ro2.close()

        self.assertEqual(sha_db_despues, sha_db_antes)
        self.assertEqual(sha_pq_despues, sha_pq_antes)
        self.assertEqual(sha_inm11_despues, sha_inm11_antes)
        self.assertEqual(sha_inm10_despues, sha_inm10_antes)
        self.assertEqual(sha_inm12_despues, sha_inm12_antes)
        self.assertEqual(sha_res_despues, sha_res_antes)


if __name__ == "__main__":
    unittest.main()
