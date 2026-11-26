"""
Tests unitarios para el Paso C-18b: Calibracion Empirica de Confianza mediante
Binning Estatico 2D en TRAIN_2025 y Evaluacion Walk-Forward (v6_calibrada)
==============================================================================
Verifica de forma determinista y sin efectos secundarios en red ni disco:
  (a) Lookup Table determinista y cero look-ahead: la asignacion v6_calibrada
      devuelve la confianza exacta de TABLA_CALIBRACION_C18_TRAIN escalada
      linealmente al rango visual [50.0, 95.0] (acotada en [50.0, 99.0]),
      sin acceder a datos posteriores a fecha_prediccion y sin alterar las
      senales ni scores de challenger_c17.
  (b) Jerarquia de configuraciones e inmutabilidad de signal_engine:
      challenger_c18b hereda C-16 y C-17, activa modo_confianza='v6_calibrada',
      tiene params_hash determinista distinto al de challenger_c17 y mantiene
      intacto services/signal_engine.py.
  (c) Estudio de confianza C-18b sobre ventana OOS del Parquet local:
      ejecutar_estudio_confianza_c18b evalua en paralelo baseline_v5,
      challenger_c17 y challenger_c18b, devolviendo spearman_3c_v1_c10,
      spearman_signo_c10, spearman_signo_c11 y terciles T1_baja/T2_media/T3_alta,
      en equivalencia bit a bit con simular_emision_y_resolucion_wf.
  (d) Inmutabilidad de stocklab.db y Parquet: garantiza total_changes == 0 y
      hashes SHA-256 identicos antes y despues de ejecutar el estudio.
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
    TABLA_CALIBRACION_C18_TRAIN,
    ConfigExperimentoMotor,
    calcular_confianza_v6_calibrada,
    calcular_hash_config,
    construir_config_challenger_c16,
    construir_config_challenger_c17,
    construir_config_challenger_c18b,
    ejecutar_estudio_confianza_c18b,
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


def _construir_panel_sintetico_c18b() -> Tuple[
    pd.DataFrame, List[str], List[str], Dict[str, float]
]:
    """
    Construye un panel OHLCV sintetico determinista con activos tendenciales
    y laterales y volumenes variables para ejercitar celdas DIR y LATERAL
    con RVOL bajo y alto.
    """
    n_sesiones = 90
    fechas = mc.bdate_range_bme("2026-04-01", periods=n_sesiones)
    tickers = ["UP1.MC", "DN1.MC", "LAT1.MC", "LAT2.MC"]
    todos = tickers + [sig_eng.TICKER_BENCHMARK]

    cols = pd.MultiIndex.from_product(
        [todos, ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)
    indices_emision = [62, 70, 78]
    tilts = {"UP1.MC": 1.4, "DN1.MC": -1.4, "LAT1.MC": 0.1, "LAT2.MC": -0.1}

    for idx_tk, tk in enumerate(todos):
        p0 = 20.0 + idx_tk * 7.5
        closes = np.zeros(n_sesiones, dtype=float)
        vols = np.full(n_sesiones, 100000.0, dtype=float)
        closes[0] = p0

        if tk == sig_eng.TICKER_BENCHMARK:
            for i in range(1, n_sesiones):
                closes[i] = p0 * (1.0 + 0.0012 * np.sin(i * 0.30))
        elif tk == "UP1.MC":
            for i in range(1, n_sesiones):
                closes[i] = closes[i - 1] * 1.015
            vols[62] = 160000.0  # RVOL Alto
            vols[70] = 60000.0   # RVOL Bajo
        elif tk == "DN1.MC":
            for i in range(1, n_sesiones):
                closes[i] = closes[i - 1] * 0.985
            vols[62] = 65000.0   # RVOL Bajo
            vols[70] = 175000.0  # RVOL Alto
        else:
            amp = 0.0025 if tk == "LAT1.MC" else 0.0040
            for i in range(1, n_sesiones):
                closes[i] = p0 * (1.0 + amp * np.sin(i * 0.55 + idx_tk))
            vols[62] = 70000.0 if tk == "LAT1.MC" else 155000.0
            vols[70] = 155000.0 if tk == "LAT1.MC" else 70000.0

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


class TestC18CalibracionConfianzaEmpirica(unittest.TestCase):
    """Suite de pruebas unitarias para el Paso C-18b."""

    def setUp(self):
        self.dt_fijo = datetime(2026, 11, 23, 19, 30, tzinfo=MADRID_TZ)

    def test_a_lookup_table_determinista_y_cero_lookahead(self):
        """
        (a) Lookup Table determinista: verifica que v6_calibrada devuelve la
        confianza exacta de la matriz pre-calculada en [50.0, 99.0], preserva
        estrictamente la monotonia de probabilidades y garantiza cero look-ahead.
        """
        todas_probs = [
            float(p)
            for cls_d in TABLA_CALIBRACION_C18_TRAIN.values()
            for hz_d in cls_d.values()
            for p in hz_d.values()
        ]
        min_p = min(todas_probs)
        max_p = max(todas_probs)
        self.assertGreater(max_p, min_p)

        # Verificar exactitud en las 24 celdas de la matriz 2D (DIR y LATERAL x 1D/3D/1S)
        umbrales = {"1D": 0.8, "3D": 1.2, "1S": 1.8}
        pares_prob_conf = []
        for cls_k, hz_dict in TABLA_CALIBRACION_C18_TRAIN.items():
            for hz, celdas in hz_dict.items():
                u = umbrales[hz]
                for (bin_d, bin_r), prob_esp in celdas.items():
                    if cls_k == "LATERAL":
                        sen = "LATERAL"
                        pct_val = (0.20 if bin_d == "Centro" else 0.75) * u
                    else:
                        sen = "ALCISTA"
                        pct_val = (1.10 if bin_d == "Cerca" else 2.20) * u
                    rvol_val = 0.75 if bin_r == "Bajo" else 1.25

                    conf, prob_out, cls_out, key_out = calcular_confianza_v6_calibrada(
                        horizonte=hz,
                        senal=sen,
                        pct_estimado=pct_val,
                        umbral_v1=u,
                        rvol=rvol_val,
                    )
                    conf_esperada = round(
                        50.0 + 45.0 * (prob_esp - min_p) / (max_p - min_p), 2
                    )
                    self.assertEqual(cls_out, cls_k)
                    self.assertEqual(key_out, (bin_d, bin_r))
                    self.assertAlmostEqual(prob_out, prob_esp, places=2)
                    self.assertAlmostEqual(conf, conf_esperada, places=2)
                    self.assertGreaterEqual(conf, 50.0)
                    self.assertLessEqual(conf, 99.0)
                    pares_prob_conf.append((prob_esp, conf))

        # Verificar extremos y monotonia estricta de la transformacion lineal
        self.assertAlmostEqual(min(c for _, c in pares_prob_conf), 50.0, places=2)
        self.assertAlmostEqual(max(c for _, c in pares_prob_conf), 95.0, places=2)
        for p1, c1 in pares_prob_conf:
            for p2, c2 in pares_prob_conf:
                if p1 < p2:
                    self.assertLess(c1, c2)
                elif p1 == p2:
                    self.assertEqual(c1, c2)

        # Verificar fronteras exactas de binning (0.98, 1.50, 0.50) y fallback 1M
        _, _, _, k_rvol_bajo = calcular_confianza_v6_calibrada("1D", "LATERAL", 0.1, 0.8, 0.979)
        _, _, _, k_rvol_alto = calcular_confianza_v6_calibrada("1D", "LATERAL", 0.1, 0.8, 0.98)
        self.assertEqual(k_rvol_bajo, ("Centro", "Bajo"))
        self.assertEqual(k_rvol_alto, ("Centro", "Alto"))

        _, _, _, k_lat_borde = calcular_confianza_v6_calibrada("1D", "LATERAL", 0.40, 0.8, 1.0)
        self.assertEqual(k_lat_borde, ("Borde", "Alto"))

        _, _, _, k_dir_lejos = calcular_confianza_v6_calibrada("1D", "BAJISTA", -1.20, 0.8, 1.0)
        self.assertEqual(k_dir_lejos, ("Lejos", "Alto"))

        conf_1m, _, _, _ = calcular_confianza_v6_calibrada("1M", "ALCISTA", 5.0, 3.0, 1.1)
        self.assertGreaterEqual(conf_1m, 50.0)
        self.assertLessEqual(conf_1m, 99.0)

        # Verificar en simulacion walk-forward: exactitud por fila + cero look-ahead +
        # mismas senales/scores que challenger_c17
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_emision, tilts = _construir_panel_sintetico_c18b()
            cfg_c17 = construir_config_challenger_c17()
            cfg_c18b = construir_config_challenger_c18b()

            df_c17 = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                config=cfg_c17,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            df_c18b = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                config=cfg_c18b,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            self.assertGreater(len(df_c18b), 0)

            # Senales, pct_estimado y score_bruto identicos a challenger_c17
            for col in ("ticker", "fecha_prediccion", "horizonte", "senal", "pct_estimado", "score_bruto"):
                pd.testing.assert_series_equal(df_c18b[col], df_c17[col])

            # Cada fila coincide exactamente con el lookup table y esta en [50.0, 99.0]
            for row in df_c18b.itertuples(index=False):
                conf_esp, _, _, _ = calcular_confianza_v6_calibrada(
                    horizonte=row.horizonte,
                    senal=row.senal,
                    pct_estimado=row.pct_estimado,
                    umbral_v1=row.umbral_v1,
                    rvol=row.rvol,
                )
                self.assertEqual(float(row.confianza), conf_esp)
                self.assertGreaterEqual(float(row.confianza), 50.0)
                self.assertLessEqual(float(row.confianza), 99.0)

            # Perturbacion extrema en fechas posteriores a fechas_emision[0] (cero look-ahead)
            f0 = fechas_emision[0]
            df_pert = df_panel.copy()
            mask_fut = pd.Series(
                [pd.Timestamp(ts).strftime("%Y-%m-%d") > f0 for ts in df_pert.index],
                index=df_pert.index,
            )
            for col in df_pert.columns:
                if col[1] == "Volume":
                    df_pert.loc[mask_fut, col] = df_pert.loc[mask_fut, col] * 9.5
                else:
                    df_pert.loc[mask_fut, col] = df_pert.loc[mask_fut, col] * 2.5

            df_c18b_pert = simular_emision_y_resolucion_wf(
                df_panel=df_pert,
                fechas_emision=[f0],
                tickers=tickers,
                config=cfg_c18b,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts,
            )
            df_c18b_orig_f0 = df_c18b[df_c18b["fecha_prediccion"] == f0].reset_index(drop=True)
            for col in ("senal", "pct_estimado", "rvol", "confianza", "score_bruto"):
                pd.testing.assert_series_equal(
                    df_c18b_pert[col].reset_index(drop=True),
                    df_c18b_orig_f0[col],
                )

    def test_b_jerarquia_configs_e_inmutabilidad_signal_engine(self):
        """
        (b) Jerarquia de configs: challenger_c18b hereda C-16 y C-17, activa
        modo_confianza='v6_calibrada', tiene hash distinto al de challenger_c17
        y deja services/signal_engine.py intacto.
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

        # Herencia exacta de C-16 y C-17
        self.assertEqual(cfg_c18b.pesos_horizonte, cfg_c16.pesos_horizonte)
        self.assertEqual(cfg_c18b.peso_tilt_por_hz, cfg_c16.peso_tilt_por_hz)
        self.assertEqual(cfg_c18b.reversion_sobreextension_1d, cfg_c16.reversion_sobreextension_1d)
        self.assertEqual(cfg_c18b.umbral_sobreextension_1d, cfg_c16.umbral_sobreextension_1d)
        self.assertEqual(cfg_c18b.vol_confirm_boost_por_hz, cfg_c16.vol_confirm_boost_por_hz)
        self.assertEqual(cfg_c18b.vol_no_confirm_damp_por_hz, cfg_c16.vol_no_confirm_damp_por_hz)
        self.assertEqual(
            cfg_c18b.amortiguacion_rvol_pico_por_hz, cfg_c16.amortiguacion_rvol_pico_por_hz
        )
        self.assertEqual(cfg_c18b.umbral_rvol_pico, cfg_c16.umbral_rvol_pico)
        self.assertEqual(
            cfg_c18b.multiplicador_conviccion_por_hz,
            cfg_c17.multiplicador_conviccion_por_hz,
        )

        # Configuracion especifica de C-18b
        self.assertEqual(cfg_c17.modo_confianza, "v5_original")
        self.assertEqual(cfg_c18b.modo_confianza, "v6_calibrada")
        self.assertEqual(cfg_c18b.tabla_calibracion_confianza, TABLA_CALIBRACION_C18_TRAIN)

        # Hashes deterministas y distintos
        h_base = calcular_hash_config(cfg_base)
        h_c16 = calcular_hash_config(cfg_c16)
        h_c17 = calcular_hash_config(cfg_c17)
        h_c18b = calcular_hash_config(cfg_c18b)
        self.assertEqual(h_c18b, calcular_hash_config(construir_config_challenger_c18b()))
        self.assertNotEqual(h_c18b, h_c17)
        self.assertEqual(len({h_base, h_c16, h_c17, h_c18b}), 4)
        self.assertEqual(len(h_c18b), 64)

        # Sensibilidad del hash a cambios en la tabla de calibracion
        cfg_c18b_mod = construir_config_challenger_c18b()
        cfg_c18b_mod.tabla_calibracion_confianza["DIR"]["1D"][("Lejos", "Alto")] = 55.00
        self.assertNotEqual(calcular_hash_config(cfg_c18b_mod), h_c18b)

        # signal_engine intacto
        self.assertEqual(sig_eng.PESOS_TENDENCIA, pesos_tend_antes)
        self.assertEqual(sig_eng.PESOS_RANGO, pesos_rango_antes)
        self.assertEqual(sig_eng.UMBRALES_LATERAL, umbrales_antes)
        self.assertEqual(sig_eng.calcular_params_hash_motor(), hash_motor_antes)
        self.assertEqual(sig_eng.verificar_estado_congelado_v5(), estado_v5_antes)

    def test_c_estudio_confianza_c18b_oos_parquet(self):
        """
        (c) Estudio de confianza sobre una ventana Val OOS 2026 del Parquet local:
        comprueba que ejecutar_estudio_confianza_c18b devuelve las metricas de
        monotonia de confianza (spearman_3c_v1_c10, spearman_signo_c10,
        spearman_signo_c11 y terciles T1_baja/T2_media/T3_alta), mejora la
        calibracion frente a baseline_v5 y challenger_c17, y coincide bit a bit
        con simular_emision_y_resolucion_wf.
        """
        self.assertTrue(
            os.path.exists(PARQUET_PATH), "data/backtest_cache_6y.parquet debe existir"
        )
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
            estudio = ejecutar_estudio_confianza_c18b(
                df_panel=df_ventana,
                fechas_emision=fechas_val_2026,
                tickers=tickers_oos,
                horizontes=("1D", "3D", "1S"),
            )

        for k in ("baseline_v5", "challenger_c17", "challenger_c18b"):
            self.assertIn(k, estudio)
            self.assertIn(k, estudio["variantes"])
            self.assertIn(k, estudio["monotonia_confianza"])

            mono_k = estudio["monotonia_confianza"][k]
            self.assertIn("spearman_3c_v1_c10", mono_k)
            self.assertIn("spearman_signo_c10", mono_k)
            self.assertIn("spearman_signo_c11", mono_k)
            self.assertIn("terciles", mono_k)
            for t_key in ("T1_baja", "T2_media", "T3_alta"):
                self.assertIn(t_key, mono_k["terciles"])
                self.assertIn("tasa_3c_v1_c10", mono_k["terciles"][t_key])
                self.assertIn("tasa_signo_c10", mono_k["terciles"][t_key])
                self.assertIn("tasa_signo_c11", mono_k["terciles"][t_key])
            for hz in ("1D", "3D", "1S"):
                self.assertIn(hz, mono_k["por_horizonte"])
                self.assertIn("spearman_3c_v1_c10", mono_k["por_horizonte"][hz])
                self.assertIn("spearman_signo_c10", mono_k["por_horizonte"][hz])
                self.assertIn("spearman_signo_c11", mono_k["por_horizonte"][hz])

        # Verificar rangos visuales y mejora de monotonia en v6_calibrada
        df_c18b = estudio["challenger_c18b"]["df_simulacion"]
        self.assertGreater(len(df_c18b), 0)
        self.assertGreaterEqual(float(df_c18b["confianza"].min()), 50.0)
        self.assertLessEqual(float(df_c18b["confianza"].max()), 99.0)

        sp_base = estudio["monotonia_confianza"]["baseline_v5"]["spearman_3c_v1_c10"]
        sp_c17 = estudio["monotonia_confianza"]["challenger_c17"]["spearman_3c_v1_c10"]
        sp_c18b = estudio["monotonia_confianza"]["challenger_c18b"]["spearman_3c_v1_c10"]
        self.assertIsNotNone(sp_c18b)
        self.assertGreater(sp_c18b, sp_base)
        self.assertGreater(sp_c18b, sp_c17)

        # Equivalencia bit a bit con simular_emision_y_resolucion_wf directo
        with _reloj_madrid(self.dt_fijo):
            df_directo = simular_emision_y_resolucion_wf(
                df_panel=df_ventana,
                fechas_emision=fechas_val_2026[:2],
                tickers=tickers_oos[:4],
                config=construir_config_challenger_c18b(),
                horizontes=("1D", "3D", "1S"),
            )
        df_sub = df_c18b[
            df_c18b["fecha_prediccion"].isin(fechas_val_2026[:2])
            & df_c18b["ticker"].isin(tickers_oos[:4])
        ].reset_index(drop=True)
        pd.testing.assert_frame_equal(
            df_directo.reset_index(drop=True),
            df_sub,
            check_exact=True,
        )

    def test_d_inmutabilidad_stocklab_db_y_parquet(self):
        """
        (d) Inmutabilidad: garantiza que stocklab.db mantiene total_changes == 0
        y que SHA256_DB, SHA256_PARQUET, SHA256_INMUTABLES_11 y SHA256_RESUELTAS
        permanecen identicos antes y despues de ejecutar el estudio C-18b.
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
        disponibles = sorted(
            t
            for t in df_parquet.columns.get_level_values(0).unique()
            if t != sig_eng.TICKER_BENCHMARK
        )
        tickers_ho = disponibles[::6][:15]
        cols_needed = [
            c
            for c in df_parquet.columns
            if c[0] in set(tickers_ho + [sig_eng.TICKER_BENCHMARK])
        ]
        fechas_all = [
            pd.Timestamp(ts).strftime("%Y-%m-%d") for ts in df_parquet.index
        ]
        fechas_all_set = set(fechas_all)
        fechas_ho = [f for f in fechas_vivo_db if f in fechas_all_set][::4][:4]
        idx_fin = len(fechas_all) - 1
        idx_ini = max(0, idx_fin - 150)
        df_ventana = df_parquet.loc[:, cols_needed].iloc[idx_ini : idx_fin + 1].copy()

        with _reloj_madrid(self.dt_fijo):
            estudio_ho = ejecutar_estudio_confianza_c18b(
                df_panel=df_ventana,
                fechas_emision=fechas_ho,
                tickers=tickers_ho,
                horizontes=("1D", "3D", "1S"),
                tilts=tilts_db,
            )

        self.assertIn("challenger_c18b", estudio_ho["monotonia_confianza"])
        self.assertGreater(len(estudio_ho["challenger_c18b"]["df_simulacion"]), 0)

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
