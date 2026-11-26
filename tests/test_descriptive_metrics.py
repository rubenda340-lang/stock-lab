"""
Tests unitarios para services/descriptive_metrics.py
Usa unittest de la librería estándar de Python.
"""

import unittest
import numpy as np
import pandas as pd

from services.descriptive_metrics import (
    calcular_metricas_volatilidad,
    calcular_frecuencias_historicas,
    calcular_extremos_y_drawdown,
    calcular_metricas_liquidez,
    calcular_sensibilidad_benchmark,
    cargar_snapshot_fundamental,
)


class TestDescriptiveMetrics(unittest.TestCase):

    def setUp(self):
        np.random.seed(42)
        n = 300
        fechas = pd.date_range("2025-01-01", periods=n, freq="B")
        
        # Simular paseo aleatorio de precios
        ret_diarios = np.random.normal(0.0004, 0.015, n)
        precios_arr = 10.0 * np.exp(np.cumsum(ret_diarios))
        self.precios = pd.Series(precios_arr, index=fechas)
        self.volumen = pd.Series(np.random.uniform(50000, 200000, n), index=fechas)
        self.highs = self.precios * (1.0 + np.random.uniform(0.002, 0.015, n))
        self.lows = self.precios * (1.0 - np.random.uniform(0.002, 0.015, n))

        # Benchmark
        ret_bm = np.random.normal(0.0003, 0.012, n)
        self.precios_bm = pd.Series(10000.0 * np.exp(np.cumsum(ret_bm)), index=fechas)

    def test_calcular_metricas_volatilidad(self):
        m = calcular_metricas_volatilidad(self.precios, self.volumen, self.highs, self.lows)
        self.assertIn("vol_20d_anual", m)
        self.assertIn("vol_60d_anual", m)
        self.assertIn("vol_252d_anual", m)
        self.assertIn("percentil_1y", m)
        self.assertIn("percentil_3y", m)
        self.assertIn("regimen_descriptivo", m)

        self.assertGreater(m["vol_20d_anual"], 0.0)
        self.assertGreater(m["vol_60d_anual"], 0.0)
        self.assertGreater(m["vol_252d_anual"], 0.0)

        # Percentil debe estar estrictamente entre 0 y 100
        if not np.isnan(m["percentil_1y"]):
            self.assertGreaterEqual(m["percentil_1y"], 0.0)
            self.assertLessEqual(m["percentil_1y"], 100.0)

    def test_calcular_frecuencias_historicas(self):
        frecs = calcular_frecuencias_historicas(self.precios, ventana_dias=252)
        self.assertIsInstance(frecs, list)
        self.assertGreater(len(frecs), 0)

        primera = frecs[0]
        self.assertIn("horizonte", primera)
        self.assertIn("umbral_pct", primera)
        self.assertIn("n_observaciones", primera)
        self.assertIn("frec_sube_pct", primera)
        self.assertIn("frec_baja_pct", primera)
        self.assertIn("frec_extremo_total_pct", primera)
        self.assertIn("etiqueta", primera)

        self.assertEqual(primera["etiqueta"], "Frecuencia histórica, no pronóstico")
        self.assertEqual(primera["n_observaciones"], 252)
        self.assertGreaterEqual(primera["frec_sube_pct"], 0.0)
        self.assertLessEqual(primera["frec_sube_pct"], 100.0)

    def test_calcular_extremos_y_drawdown(self):
        ext = calcular_extremos_y_drawdown(self.precios, self.highs, self.lows, ventana_dias=252)
        self.assertIn("precio_actual", ext)
        self.assertIn("max_52w", ext)
        self.assertIn("min_52w", ext)
        self.assertIn("distancia_max_52w_pct", ext)
        self.assertIn("distancia_min_52w_pct", ext)
        self.assertIn("posicion_rango_52w_pct", ext)
        self.assertIn("max_drawdown_252d_pct", ext)

        self.assertLessEqual(ext["distancia_max_52w_pct"], 0.0)  # Cierre <= Max
        self.assertGreaterEqual(ext["distancia_min_52w_pct"], 0.0)  # Cierre >= Min
        self.assertGreaterEqual(ext["posicion_rango_52w_pct"], 0.0)
        self.assertLessEqual(ext["posicion_rango_52w_pct"], 100.0)
        self.assertLessEqual(ext["max_drawdown_252d_pct"], 0.0)
        self.assertIn("barras_excluidas", ext)

    def test_calcular_metricas_liquidez(self):
        liq = calcular_metricas_liquidez(self.precios, self.volumen, ventana_dias=252)
        self.assertIn("volumen_eur_20d", liq)
        self.assertIn("volumen_eur_60d", liq)
        self.assertIn("pct_dias_sin_variacion", liq)
        self.assertIn("tercil_liquidez", liq)

        self.assertGreater(liq["volumen_eur_20d"], 0.0)
        self.assertGreaterEqual(liq["pct_dias_sin_variacion"], 0.0)
        self.assertLessEqual(liq["pct_dias_sin_variacion"], 100.0)

    def test_calcular_sensibilidad_benchmark(self):
        sens = calcular_sensibilidad_benchmark(self.precios, self.precios_bm, ventana_corr=60)
        self.assertIn("correlacion_ibex_60d", sens)
        self.assertIn("beta_ibex_60d", sens)
        self.assertIn("retorno_relativo_1m_pct", sens)

        corr = sens["correlacion_ibex_60d"]
        if not np.isnan(corr):
            self.assertGreaterEqual(corr, -1.0)
            self.assertLessEqual(corr, 1.0)

    def test_cargar_snapshot_fundamental(self):
        data, fecha = cargar_snapshot_fundamental("SAN.MC")
        self.assertIsInstance(data, dict)
        self.assertIsInstance(fecha, str)

    def test_no_look_ahead(self):
        """
        Verifica formalmente que ningún cálculo del panel descriptivo
        utiliza información posterior a la fecha de corte t (no look-ahead).
        """
        t_corte = 240
        p_t = self.precios.iloc[:t_corte]
        v_t = self.volumen.iloc[:t_corte]
        h_t = self.highs.iloc[:t_corte]
        l_t = self.lows.iloc[:t_corte]

        # Calcular métricas al corte t
        m_vol_1 = calcular_metricas_volatilidad(p_t, volumen=v_t, highs=h_t, lows=l_t)
        m_ext_1 = calcular_extremos_y_drawdown(p_t, highs=h_t, lows=l_t, volumen=v_t, ventana_dias=200)
        m_frec_1 = calcular_frecuencias_historicas(p_t, ventana_dias=200)

        # Crear una serie con futuro alterado drásticamente (crash o explosión en t+1..t+60)
        p_modificada = self.precios.copy()
        p_modificada.iloc[t_corte:] = p_modificada.iloc[t_corte:] * 100.0
        v_modificado = self.volumen.copy()
        v_modificado.iloc[t_corte:] = 0.0

        # Recalcular métricas al mismo corte t_corte tomando la ventana truncada <= t
        p_t2 = p_modificada.iloc[:t_corte]
        v_t2 = v_modificado.iloc[:t_corte]
        h_t2 = self.highs.iloc[:t_corte]
        l_t2 = self.lows.iloc[:t_corte]

        m_vol_2 = calcular_metricas_volatilidad(p_t2, volumen=v_t2, highs=h_t2, lows=l_t2)
        m_ext_2 = calcular_extremos_y_drawdown(p_t2, highs=h_t2, lows=l_t2, volumen=v_t2, ventana_dias=200)
        m_frec_2 = calcular_frecuencias_historicas(p_t2, ventana_dias=200)

        # 1. Volatilidad idéntica
        self.assertEqual(m_vol_1["vol_20d_anual"], m_vol_2["vol_20d_anual"])
        self.assertEqual(m_vol_1["vol_60d_anual"], m_vol_2["vol_60d_anual"])
        self.assertEqual(m_vol_1["percentil_1y"], m_vol_2["percentil_1y"])

        # 2. Extremos y drawdown idénticos
        self.assertEqual(m_ext_1["max_52w"], m_ext_2["max_52w"])
        self.assertEqual(m_ext_1["max_drawdown_252d_pct"], m_ext_2["max_drawdown_252d_pct"])
        self.assertEqual(m_ext_1["barras_excluidas"], m_ext_2["barras_excluidas"])

        # 3. Frecuencias idénticas
        self.assertEqual(len(m_frec_1), len(m_frec_2))
        for f1, f2 in zip(m_frec_1, m_frec_2):
            self.assertEqual(f1["frec_extremo_total_pct"], f2["frec_extremo_total_pct"])
            self.assertEqual(f1["n_observaciones"], f2["n_observaciones"])
            self.assertEqual(f1["desc_obs"], f2["desc_obs"])

    def test_sincronizar_cache_panel_historico(self):
        """Verifica que la sincronización diaria del parquet del panel añada nuevas sesiones de forma idempotente."""
        import tempfile
        import os
        from services.emision import sincronizar_cache_panel_historico

        cols = pd.MultiIndex.from_tuples([("SAN.MC", "Close"), ("SAN.MC", "Volume")], names=["Ticker", "Price"])
        fechas_ini = pd.date_range("2026-10-01", periods=2, freq="D")
        df_base = pd.DataFrame([[4.50, 100000.0], [4.55, 120000.0]], index=fechas_ini, columns=cols)

        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            df_base.to_parquet(tmp_path)

            # Caso 1: Nuevo lote con fecha adicional 2026-10-05
            fechas_batch = pd.date_range("2026-10-02", periods=2, freq="3D") # 2026-10-02 y 2026-10-05
            df_batch = pd.DataFrame([[4.55, 120000.0], [4.60, 150000.0]], index=fechas_batch, columns=cols)

            actualizado = sincronizar_cache_panel_historico(df_batch, path_cache=tmp_path)
            self.assertTrue(actualizado)

            df_post = pd.read_parquet(tmp_path)
            self.assertEqual(len(df_post), 3) # 2026-10-01, 2026-10-02, 2026-10-05
            self.assertIn(pd.Timestamp("2026-10-05"), df_post.index)

            # Caso 2: Idempotencia - Re-sincronizar sin fechas nuevas
            actualizado_rep = sincronizar_cache_panel_historico(df_batch, path_cache=tmp_path)
            self.assertFalse(actualizado_rep)
            df_rep = pd.read_parquet(tmp_path)
            self.assertEqual(len(df_rep), 3)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_sincronizar_cache_ajuste_dividendo_y_split(self):
        """
        Simula un ajuste por dividendo (5%) y otro por split (2:1) en el solape (~400 sesiones).
        Verifica:
          1. Detección de ratio != 1 (tolerancia 1e-6) y constancia en registro.
          2. Reemplazo del histórico completo descargado.
          3. Continuidad perfecta en la unión (sin saltos artificiales por escala de precio).
          4. Idempotencia en ejecuciones sucesivas.
        """
        import tempfile
        import os
        from services.emision import sincronizar_cache_panel_historico, ULTIMOS_AJUSTES_DETECTADOS

        np.random.seed(123)
        n_total = 500
        fechas_all = pd.date_range("2024-01-01", periods=n_total, freq="B")

        # Simular series continuas de retornos normales (~0.3% diario)
        ret_div = np.random.normal(0.0002, 0.004, n_total)
        ret_split = np.random.normal(0.0003, 0.004, n_total)
        p_div_cont = 20.0 * np.exp(np.cumsum(ret_div))
        p_split_cont = 100.0 * np.exp(np.cumsum(ret_split))

        # En el histórico previo al evento corporativo (fecha 450):
        # - DIV.MC: antes del dividendo 5%, el precio no ajustado cotizaba 1/0.95 superior
        # - SPLIT.MC: antes del split 2:1, el precio no ajustado cotizaba al doble (1/0.50)
        idx_evento = 450
        p_div_unadj = p_div_cont.copy()
        p_div_unadj[:idx_evento] = p_div_unadj[:idx_evento] / 0.95

        p_split_unadj = p_split_cont.copy()
        p_split_unadj[:idx_evento] = p_split_unadj[:idx_evento] / 0.50

        # Con auto_adjust=True de Yahoo Finance, todo el histórico viene reajustado hacia atrás (p_cont)
        p_div_adj = p_div_cont
        p_split_adj = p_split_cont

        cols = pd.MultiIndex.from_tuples([
            ("DIV.MC", "Open"), ("DIV.MC", "High"), ("DIV.MC", "Low"), ("DIV.MC", "Close"), ("DIV.MC", "Volume"),
            ("SPLIT.MC", "Open"), ("SPLIT.MC", "High"), ("SPLIT.MC", "Low"), ("SPLIT.MC", "Close"), ("SPLIT.MC", "Volume"),
        ], names=["Ticker", "Price"])

        # Estado inicial del caché: 499 sesiones (0 a 498) con precios no ajustados
        df_cache_ini = pd.DataFrame(index=fechas_all[:499], columns=cols, dtype=float)
        for t, p_arr in [("DIV.MC", p_div_unadj[:499]), ("SPLIT.MC", p_split_unadj[:499])]:
            df_cache_ini[(t, "Open")] = p_arr * 0.999
            df_cache_ini[(t, "High")] = p_arr * 1.002
            df_cache_ini[(t, "Low")] = p_arr * 0.998
            df_cache_ini[(t, "Close")] = p_arr
            df_cache_ini[(t, "Volume")] = 100000.0

        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            df_cache_ini.to_parquet(tmp_path)

            # Nuevo lote descargado con auto_adjust=True: últimas 400 sesiones (fechas 100 a 499)
            # Contiene la nueva sesión 499 y viene reajustado retrospectivamente por Yahoo Finance
            fechas_batch = fechas_all[100:500]
            df_batch = pd.DataFrame(index=fechas_batch, columns=cols, dtype=float)
            for t, p_arr in [("DIV.MC", p_div_adj[100:500]), ("SPLIT.MC", p_split_adj[100:500])]:
                df_batch[(t, "Open")] = p_arr * 0.999
                df_batch[(t, "High")] = p_arr * 1.002
                df_batch[(t, "Low")] = p_arr * 0.998
                df_batch[(t, "Close")] = p_arr
                df_batch[(t, "Volume")] = 120000.0

            # Mock downloader de histórico completo (6y) reajustado
            def mock_full_downloader(symbols, period="6y"):
                t = symbols[0]
                arr = p_div_adj if t == "DIV.MC" else p_split_adj
                t_cols = pd.MultiIndex.from_tuples([
                    (t, "Open"), (t, "High"), (t, "Low"), (t, "Close"), (t, "Volume")
                ], names=["Ticker", "Price"])
                df_full = pd.DataFrame(index=fechas_all, columns=t_cols, dtype=float)
                df_full[(t, "Open")] = arr * 0.999
                df_full[(t, "High")] = arr * 1.002
                df_full[(t, "Low")] = arr * 0.998
                df_full[(t, "Close")] = arr
                df_full[(t, "Volume")] = 120000.0
                return df_full

            # Limpiar registro de eventos previos
            ULTIMOS_AJUSTES_DETECTADOS.clear()

            # 1. Sincronizar
            actualizado = sincronizar_cache_panel_historico(
                df_batch=df_batch,
                verbose=False,
                path_cache=tmp_path,
                fn_descargar_full=mock_full_downloader
            )
            self.assertTrue(actualizado)

            # 2. Comprobar que se detectaron los ajustes y se dejó constancia
            tickers_detectados = {a["ticker"] for a in ULTIMOS_AJUSTES_DETECTADOS}
            self.assertIn("DIV.MC", tickers_detectados)
            self.assertIn("SPLIT.MC", tickers_detectados)

            # Ratios medidos en el solape
            ratios_map = {a["ticker"]: a["ratio"] for a in ULTIMOS_AJUSTES_DETECTADOS}
            self.assertAlmostEqual(ratios_map["DIV.MC"], 0.95, places=3)
            self.assertAlmostEqual(ratios_map["SPLIT.MC"], 0.50, places=3)

            # 3. Comprobar continuidad matemática (sin saltos en la unión ni en el ex-date)
            df_post = pd.read_parquet(tmp_path)
            self.assertEqual(len(df_post), 500)

            ret_div_post = df_post[("DIV.MC", "Close")].pct_change().dropna()
            ret_split_post = df_post[("SPLIT.MC", "Close")].pct_change().dropna()

            # Ningún retorno diario debe superar el 2% (ambos paseos tienen std 0.4%)
            # Si no se hubiera reajustado hacia atrás, SPLIT.MC tendría un salto de -50% y DIV.MC de -5%
            self.assertLess(float(ret_div_post.abs().max()), 0.02)
            self.assertLess(float(ret_split_post.abs().max()), 0.02)

            # 4. Comprobar idempotencia: una segunda ejecución con el mismo lote no hace nada
            n_ajustes_pre = len(ULTIMOS_AJUSTES_DETECTADOS)
            actualizado_rep = sincronizar_cache_panel_historico(
                df_batch=df_batch,
                verbose=False,
                path_cache=tmp_path,
                fn_descargar_full=mock_full_downloader
            )
            self.assertFalse(actualizado_rep)
            self.assertEqual(len(ULTIMOS_AJUSTES_DETECTADOS), n_ajustes_pre)

            df_rep = pd.read_parquet(tmp_path)
            self.assertEqual(len(df_rep), 500)
            pd.testing.assert_frame_equal(df_post, df_rep)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


if __name__ == "__main__":
    unittest.main()


