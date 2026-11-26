"""
Tests unitarios para el Paso C-15: Arnes Walk-Forward Honesto y Reproducible de Experimentos
============================================================================================
Verifica en memoria y en modo solo lectura (sin llamadas a Internet/Yahoo Finance y sin
escribir en stocklab.db ni alterar data/backtest_cache_6y.parquet):
  (a) Cero look-ahead ante perturbacion extrema post-emision: alterar precios y volumenes
      en fechas posteriores (fecha > fecha_prediccion) no cambia ni una sola coma de las
      senales emitidas, scores, porcentajes estimados, umbrales ni confianzas en fecha_prediccion.
  (b) Reproducibilidad exacta de v5-frozen con ConfigExperimentoMotor() por defecto:
      las senales, scores, porcentajes, bandas y confianzas generados por el arnes
      coinciden exactamente con calcular_radar_desde_batch / calcular_fila_ticker de
      services.signal_engine, sin mutar ninguna constante global de signal_engine.
  (c) Exclusion de zombis C-27 y metricas completas C-09..C-14:
      verifica que los activos zombi/iliquidos son filtrados cuando aplicar_filtro_c27=True,
      y que evaluar_experimento_wf calcula etiqueta_muestra (C-09), tasas de 3 clases y
      de signo en C-10/C-11/C-12, inferencia por bloques de fecha (C-13), las 5 referencias
      ingenuas (C-14) y monotonia de confianza (Spearman y terciles T1_baja, T2_media, T3_alta).
  (d) Comparador pareado comparar_challenger_vs_baseline_wf:
      verifica el calculo de diferenciales en puntos porcentuales, t_stat_pareado_por_fecha,
      n_fechas_ganadas_challenger y n_fechas_ganadas_baseline entre dos configuraciones.
  (e) Ejecucion en solo lectura sobre una ventana real de data/backtest_cache_6y.parquet:
      ejecuta el arnes sobre datos reales del Parquet local sin escribir nada en disco
      y comprueba que SHA256_DB, SHA256_INMUTABLES_11 y SHA256_RESUELTAS de stocklab.db
      y el SHA256 del Parquet permanecen identicos antes y despues.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
import sqlite3
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
    calcular_fila_ticker_experimental,
    calcular_hash_config,
    comparar_challenger_vs_baseline_wf,
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
    """Parchea get_ahora_madrid en utils.market_calendar, services.emision y database.predicciones."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _sha256_fichero(ruta: str) -> str:
    with open(ruta, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _construir_panel_sintetico(
    n_sesiones: int = 90,
    incluir_zombi: bool = False,
    seed: int = 42,
) -> Tuple[pd.DataFrame, List[str], List[str]]:
    """
    Construye un DataFrame MultiIndex (ticker, campo) sintetico con barras habiles BME
    para activos liquidos ('AAA.MC', 'BBB.MC', 'CCC.MC'), el benchmark '^IBEX' y
    opcionalmente un valor zombi ('ZOM.MC' con volumen 0).
    """
    rng = np.random.default_rng(seed)
    fechas = mc.bdate_range_bme("2026-05-04", periods=n_sesiones)
    tickers_activos = ["AAA.MC", "BBB.MC", "CCC.MC"]
    if incluir_zombi:
        tickers_activos.append("ZOM.MC")
    todos_tickers = tickers_activos + [sig_eng.TICKER_BENCHMARK]

    cols = pd.MultiIndex.from_product(
        [todos_tickers, ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)

    for idx_t, tk in enumerate(todos_tickers):
        p0 = 20.0 + idx_t * 10.0
        drift = 0.0025 if idx_t % 2 == 0 else -0.0020
        ruido = rng.normal(loc=drift, scale=0.012, size=n_sesiones)
        closes = p0 * np.cumprod(1.0 + ruido)
        opens = np.roll(closes, 1) * (1.0 + rng.normal(0.0, 0.002, size=n_sesiones))
        opens[0] = p0
        highs = np.maximum(opens, closes) * 1.008
        lows = np.minimum(opens, closes) * 0.992
        vols = rng.uniform(80000.0, 250000.0, size=n_sesiones)

        if tk == "ZOM.MC":
            vols[40:] = 0.0

        df[(tk, "Open")] = opens
        df[(tk, "High")] = highs
        df[(tk, "Low")] = lows
        df[(tk, "Close")] = closes
        df[(tk, "Volume")] = vols

    fechas_str = [pd.Timestamp(f).strftime("%Y-%m-%d") for f in fechas]
    return df, tickers_activos, fechas_str


class TestC15WalkForwardHarness(unittest.TestCase):
    """Suite de 5 pruebas unitarias para el arnes walk-forward C-15."""

    def setUp(self):
        self.dt_fijo = datetime(2026, 11, 8, 12, 0, tzinfo=MADRID_TZ)

    def test_a_cero_look_ahead_perturbacion_extrema_post_emision(self):
        """
        (a) Cero look-ahead ante perturbacion extrema post-emision:
        Perturbar por completo (x100 o x0.01) todos los precios y volumenes en
        fechas > fecha_prediccion deja IDENTICAS todas las columnas de emision
        (senal, pct_estimado, pct_estimado_raw, confianza, score_motor, score_bruto,
        sigma_cierre_pct, umbral_v2) en fecha_prediccion.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_str = _construir_panel_sintetico(n_sesiones=85)
            fechas_emision = fechas_str[65:70]
            f_corte = fechas_emision[-1]

            df_base = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                config=ConfigExperimentoMotor(),
                horizontes=("1D", "3D", "1S"),
            )
            self.assertFalse(df_base.empty)

            # Crear copia con perturbacion extrema en todas las fechas posteriores a cada emision
            # Probamos especificamente en la ultima fecha de emision f_corte:
            df_perturbado = df_panel.copy(deep=True)
            mask_futuro = [
                pd.Timestamp(ts).strftime("%Y-%m-%d") > f_corte for ts in df_perturbado.index
            ]
            for tk in tickers + [sig_eng.TICKER_BENCHMARK]:
                df_perturbado.loc[mask_futuro, (tk, "Open")] *= 50.0
                df_perturbado.loc[mask_futuro, (tk, "High")] *= 60.0
                df_perturbado.loc[mask_futuro, (tk, "Low")] *= 0.02
                df_perturbado.loc[mask_futuro, (tk, "Close")] *= 50.0
                df_perturbado.loc[mask_futuro, (tk, "Volume")] *= 1000.0

            df_pert = simular_emision_y_resolucion_wf(
                df_panel=df_perturbado,
                fechas_emision=[f_corte],
                tickers=tickers,
                config=ConfigExperimentoMotor(),
                horizontes=("1D", "3D", "1S"),
            )
            df_base_corte = df_base[df_base["fecha_prediccion"] == f_corte].reset_index(
                drop=True
            )
            df_pert_corte = df_pert.reset_index(drop=True)

            cols_emision_invariantes = [
                "ticker",
                "fecha_prediccion",
                "horizonte",
                "dias",
                "fecha_objetivo",
                "senal",
                "pct_estimado",
                "pct_estimado_raw",
                "banda_pct",
                "confianza",
                "conviccion",
                "score_motor",
                "score_bruto",
                "precio_entrada",
                "umbral_v1",
                "sigma_cierre_pct",
                "umbral_v2",
                "comp_rsi",
                "comp_macd",
                "comp_tendencia",
                "comp_momentum",
                "adx",
                "rvol",
                "marea",
                "rs_ibex",
            ]
            pd.testing.assert_frame_equal(
                df_base_corte[cols_emision_invariantes],
                df_pert_corte[cols_emision_invariantes],
                check_exact=True,
            )
            # En cambio, el precio_real (en f_obj > f_corte) si refleja el futuro perturbado
            self.assertFalse(
                np.allclose(
                    df_base_corte["precio_real"].to_numpy(dtype=float),
                    df_pert_corte["precio_real"].to_numpy(dtype=float),
                )
            )

    def test_b_reproducibilidad_exacta_v5_frozen_por_defecto(self):
        """
        (b) Reproducibilidad exacta de v5-frozen con ConfigExperimentoMotor() por defecto:
        Verifica que ConfigExperimentoMotor() reproduce exactamente las salidas de
        calcular_radar_desde_batch / calcular_fila_ticker de services.signal_engine
        y que las constantes globales de signal_engine permanecen intactas.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_str = _construir_panel_sintetico(n_sesiones=80)
            f_emision = fechas_str[65]
            df_hasta_s = df_panel.loc[
                [pd.Timestamp(ts).strftime("%Y-%m-%d") <= f_emision for ts in df_panel.index]
            ]

            hash_motor_antes = sig_eng.get_params_hash()
            cfg_default = ConfigExperimentoMotor()
            self.assertEqual(len(calcular_hash_config(cfg_default)), 64)
            self.assertEqual(
                calcular_hash_config(cfg_default), cfg_default.calcular_hash()
            )

            # Comparar contra calcular_radar_desde_batch oficial de signal_engine
            radar_oficial = sig_eng.calcular_radar_desde_batch(
                df_hasta_s,
                tuple(tickers),
                tuple(tickers),
                tuple(0.0 for _ in tickers),
                sentimientos=None,
                usar_calibracion=False,
            )
            df_wf = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=[f_emision],
                tickers=tickers,
                config=cfg_default,
                horizontes=("1D", "3D", "1S", "1M"),
            )

            self.assertEqual(len(df_wf), len(tickers) * 4)
            for _, row_rad in radar_oficial.iterrows():
                tk = row_rad["ticker"]
                for hz in ("1D", "3D", "1S", "1M"):
                    sub_wf = df_wf[
                        (df_wf["ticker"] == tk) & (df_wf["horizonte"] == hz)
                    ].iloc[0]
                    self.assertEqual(sub_wf["senal"], row_rad[f"senal_{hz}"])
                    self.assertAlmostEqual(
                        float(sub_wf["pct_estimado"]), float(row_rad[f"pct_{hz}"]), places=6
                    )
                    self.assertAlmostEqual(
                        float(sub_wf["confianza"]), float(row_rad[f"conf_{hz}"]), places=6
                    )
                    self.assertAlmostEqual(
                        float(sub_wf["score_motor"]), float(row_rad[f"score_{hz}"]), places=6
                    )
                    self.assertAlmostEqual(
                        float(sub_wf["banda_pct"]), float(row_rad[f"banda_{hz}"]), places=6
                    )

            # Verificar que ejecutar un challenger con parametros muy distintos NO altera signal_engine
            cfg_custom = ConfigExperimentoMotor(
                nombre_modelo="exp-test",
                umbrales_lateral_v1={"1D": 2.5, "3D": 3.5, "1S": 4.5, "1M": 6.0},
                multiplicador_conviccion_por_hz={"1D": 1.8, "3D": 1.8, "1S": 1.8, "1M": 1.8},
                vol_confirm_boost=1.40,
                vol_no_confirm_damp=0.50,
                escala_1d_cierre_a_apertura=0.5,
                penalizar_bajista_1d_en_marea_bajista=True,
                modo_confianza="experimental_c19",
                k_sigma_v2=0.65,
            )
            _ = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=[f_emision],
                tickers=tickers,
                config=cfg_custom,
                horizontes=("1D", "3D", "1S"),
            )
            self.assertEqual(sig_eng.get_params_hash(), hash_motor_antes)
            self.assertEqual(
                sig_eng.UMBRALES_LATERAL, {"1D": 0.8, "3D": 1.2, "1S": 1.8, "1M": 3.0}
            )

    def test_c_exclusion_zombis_c27_y_metricas_completas_c09_a_c14(self):
        """
        (c) Exclusion de zombis C-27 y metricas completas C-09..C-14:
        Verifica que ZOM.MC (con volumen 0) queda excluido cuando aplicar_filtro_c27=True
        e incluido cuando aplicar_filtro_c27=False, y que evaluar_experimento_wf
        devuelve la estructura completa C-09..C-14 y monotonia de confianza.
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_str = _construir_panel_sintetico(
                n_sesiones=85, incluir_zombi=True
            )
            fechas_emision = fechas_str[62:70]

            df_con_c27 = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                aplicar_filtro_c27=True,
                horizontes=("1D", "3D", "1S"),
            )
            self.assertNotIn("ZOM.MC", set(df_con_c27["ticker"].unique()))
            self.assertEqual(
                set(df_con_c27["ticker"].unique()), {"AAA.MC", "BBB.MC", "CCC.MC"}
            )

            df_sin_c27 = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                aplicar_filtro_c27=False,
                horizontes=("1D", "3D", "1S"),
            )
            self.assertIn("ZOM.MC", set(df_sin_c27["ticker"].unique()))

            # Verificar acierto_signo == None para senales LATERAL y 0/1 para direccionales (C-09)
            filas_lat = df_con_c27[df_con_c27["senal"] == "LATERAL"]
            if not filas_lat.empty:
                self.assertTrue(filas_lat["acierto_signo_c10"].isna().all())
                self.assertTrue(filas_lat["acierto_signo_c11"].isna().all())
            filas_dir = df_con_c27[df_con_c27["senal"].isin(["ALCISTA", "BAJISTA"])]
            if not filas_dir.empty:
                self.assertTrue(filas_dir["acierto_signo_c10"].isin([0, 1]).all())

            ev = evaluar_experimento_wf(df_con_c27, df_panel=df_panel)
            # C-09 etiqueta de muestra
            self.assertIn("etiqueta_muestra", ev)
            self.assertIn("fechas", ev["etiqueta_muestra"])
            self.assertEqual(ev["n_fechas"], len(fechas_emision))

            # C-10 / C-11 / C-12 tasas de 3 clases y signo
            for k_3c in ("v1_c10", "v2_c12", "v1_c11", "v2_c11"):
                self.assertIn(k_3c, ev["tasas_3clases"])
                self.assertIsNotNone(ev["tasas_3clases"][k_3c])
            for k_sig in ("c10", "c11"):
                self.assertIn(k_sig, ev["tasas_signo"])

            # C-13 inferencia por bloques de fecha
            inf_v1 = ev["inferencia"]["v1_c10"]
            self.assertEqual(inf_v1["n_fechas"], len(fechas_emision))
            self.assertFalse(inf_v1["muestra_suficiente"])
            self.assertEqual(inf_v1["estado_muestra"], "muestra_insuficiente")

            # C-14 5 referencias ingenuas + motor (6 filas)
            ref_c14 = ev["referencias_ingenuas_c14"]
            self.assertEqual(len(ref_c14["tabla"]), 6)
            codigos = [x["codigo"] for x in ref_c14["tabla"]]
            self.assertEqual(
                codigos,
                [
                    "motor",
                    "siempre_alcista",
                    "siempre_bajista",
                    "siempre_lateral",
                    "continuacion_ultimo_movimiento",
                    "direccion_ibex",
                ],
            )
            # Identidad algebraica de las 3 referencias constantes
            por_cod = ref_c14["por_codigo"]
            suma_3c_const = (
                por_cod["siempre_alcista"]["tasa_acierto_3clases_v1"]
                + por_cod["siempre_bajista"]["tasa_acierto_3clases_v1"]
                + por_cod["siempre_lateral"]["tasa_acierto_3clases_v1"]
            )
            self.assertAlmostEqual(suma_3c_const, 100.0, places=1)
            self.assertIsNone(por_cod["siempre_lateral"]["tasa_acierto_signo"])

            # Monotonia de confianza: Spearman y terciles T1_baja, T2_media, T3_alta
            mono = ev["monotonia_confianza"]
            self.assertIn("spearman_3c_v1_c10", mono)
            for t_key in ("T1_baja", "T2_media", "T3_alta"):
                self.assertIn(t_key, mono["terciles"])
                self.assertGreater(mono["terciles"][t_key]["n"], 0)

            # Desglose por horizonte
            for hz in ("1D", "3D", "1S"):
                self.assertIn(hz, ev["por_horizonte"])
                self.assertEqual(ev["por_horizonte"][hz]["n_fechas"], len(fechas_emision))

    def test_d_comparador_pareado_challenger_vs_baseline(self):
        """
        (d) Comparador pareado comparar_challenger_vs_baseline_wf:
        Verifica que al comparar un challenger con conviccion reforzada frente al
        baseline se devuelven los diferenciales exactos en pp y las estadisticas
        pareadas por fecha de emision (t_stat_pareado_por_fecha,
        n_fechas_ganadas_challenger, n_fechas_ganadas_baseline).
        """
        with _reloj_madrid(self.dt_fijo):
            df_panel, tickers, fechas_str = _construir_panel_sintetico(
                n_sesiones=90, seed=123
            )
            fechas_emision = fechas_str[60:72]

            cfg_base = ConfigExperimentoMotor(nombre_modelo="v5-frozen-wf")
            cfg_chall = ConfigExperimentoMotor(
                nombre_modelo="challenger-conv-alta",
                multiplicador_conviccion_por_hz={
                    "1D": 2.5,
                    "3D": 2.5,
                    "1S": 2.5,
                    "1M": 2.5,
                },
            )

            df_base = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                config=cfg_base,
                horizontes=("1D", "3D", "1S"),
            )
            df_chall = simular_emision_y_resolucion_wf(
                df_panel=df_panel,
                fechas_emision=fechas_emision,
                tickers=tickers,
                config=cfg_chall,
                horizontes=("1D", "3D", "1S"),
            )

            # Con multiplicador_conviccion_por_hz = 2.5, el challenger debe emitir mas LATERAL
            n_lat_base = int((df_base["senal"] == "LATERAL").sum())
            n_lat_chall = int((df_chall["senal"] == "LATERAL").sum())
            self.assertGreaterEqual(n_lat_chall, n_lat_base)

            comp = comparar_challenger_vs_baseline_wf(
                df_base, df_chall, df_panel=df_panel
            )
            self.assertEqual(comp["baseline_modelo"], "v5-frozen-wf")
            self.assertEqual(comp["challenger_modelo"], "challenger-conv-alta")
            self.assertEqual(comp["n_fechas_comunes"], len(fechas_emision))
            self.assertIn("t_stat_pareado_por_fecha", comp)
            self.assertIn("n_fechas_ganadas_challenger", comp)
            self.assertIn("n_fechas_ganadas_baseline", comp)
            self.assertEqual(
                comp["n_fechas_ganadas_challenger"]
                + comp["n_fechas_ganadas_baseline"]
                + comp["n_fechas_empatadas"],
                len(fechas_emision),
            )
            expected_delta = round(
                comp["challenger"]["tasa_3c_v1_c10"]
                - comp["baseline"]["tasa_3c_v1_c10"],
                2,
            )
            self.assertAlmostEqual(comp["delta_3c_v1_c10_pp"], expected_delta, places=2)
            for hz in ("1D", "3D", "1S"):
                self.assertIn(hz, comp["por_horizonte"])
                self.assertIn("t_stat_pareado_por_fecha", comp["por_horizonte"][hz])

    def test_e_ejecucion_solo_lectura_sobre_parquet_real_e_inmutabilidad_db(self):
        """
        (e) Ejecucion en solo lectura sobre una ventana real de data/backtest_cache_6y.parquet:
        Verifica que el arnes corre sobre datos reales del Parquet local sin tocar
        stocklab.db ni el fichero Parquet.
        """
        self.assertTrue(os.path.exists(REAL_DB_PATH), "stocklab.db debe existir")
        self.assertTrue(
            os.path.exists(PARQUET_PATH), "data/backtest_cache_6y.parquet debe existir"
        )

        sha_db_antes = _sha256_fichero(REAL_DB_PATH)
        sha_pq_antes = _sha256_fichero(PARQUET_PATH)

        conn_ro = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            sha_inm_antes = db_pred._calcular_sha256_inmutables(conn_ro)
            sha_res_antes = db_pred._calcular_sha256_resueltas(conn_ro)
        finally:
            conn_ro.close()

        df_parquet = pd.read_parquet(PARQUET_PATH)
        self.assertFalse(df_parquet.empty)

        # Seleccionar un subconjunto representativo de tickers del IBEX y 5 fechas historicas
        tickers_candidatos = [
            "SAN.MC",
            "BBVA.MC",
            "IBE.MC",
            "ITX.MC",
            "TEF.MC",
            "REP.MC",
        ]
        if isinstance(df_parquet.columns, pd.MultiIndex):
            disponibles = set(df_parquet.columns.get_level_values(0).unique())
        else:
            disponibles = set()
        tickers_reales = [t for t in tickers_candidatos if t in disponibles]
        self.assertGreaterEqual(len(tickers_reales), 3)

        cols_needed = [
            c
            for c in df_parquet.columns
            if c[0] in set(tickers_reales + [sig_eng.TICKER_BENCHMARK])
        ]
        # Recortar a una ventana de 120 sesiones con margen posterior para resolver 1D/3D/1S
        df_ventana = df_parquet.loc[:, cols_needed].iloc[-140:-20].copy()
        fechas_ventana = [
            pd.Timestamp(ts).strftime("%Y-%m-%d") for ts in df_ventana.index
        ]
        fechas_emision = fechas_ventana[-15:-10]

        with _reloj_madrid(self.dt_fijo):
            df_sim = simular_emision_y_resolucion_wf(
                df_panel=df_ventana,
                fechas_emision=fechas_emision,
                tickers=tickers_reales,
                config=ConfigExperimentoMotor(),
                horizontes=("1D", "3D", "1S"),
                aplicar_filtro_c27=True,
            )
            self.assertFalse(df_sim.empty)
            self.assertTrue((df_sim["resuelta"] == 1).all())

            ev = evaluar_experimento_wf(df_sim, df_panel=df_ventana)
            self.assertEqual(ev["n_fechas"], len(fechas_emision))
            self.assertGreater(ev["n_resueltas"], 0)
            self.assertIsNotNone(ev["tasa_3c_v1_c10"])
            self.assertIsNotNone(ev["tasa_3c_v2_c12"])
            self.assertIsNotNone(ev["tasa_3c_v1_c11"])
            self.assertIsNotNone(ev["tasa_signo_c10"])
            self.assertEqual(len(ev["referencias_ingenuas_c14"]["tabla"]), 6)

        sha_db_despues = _sha256_fichero(REAL_DB_PATH)
        sha_pq_despues = _sha256_fichero(PARQUET_PATH)
        conn_ro2 = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
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
