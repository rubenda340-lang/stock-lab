"""
Tests unitarios exhaustivos para la Capa de Calidad de Datos (services/data_quality.py).
Usa unittest de la librería estándar de Python.
"""

import unittest
import numpy as np
import pandas as pd

from services.data_quality import (
    detectar_retornos_extremos,
    detectar_rachas_retorno_cero,
    detectar_precios_estancados,
    detectar_posible_split_no_ajustado,
    auditar_serie_precios,
    filtrar_serie_volatilidad_limpia,
)


class TestDataQuality(unittest.TestCase):

    def test_detectar_retornos_extremos(self):
        fechas = pd.date_range("2025-01-01", periods=5)
        precios = pd.Series([10.0, 10.5, 15.0, 14.8, 14.5], index=fechas)
        retornos = precios.pct_change()
        extremos = detectar_retornos_extremos(retornos, umbral=0.25)

        self.assertFalse(extremos.iloc[0])
        self.assertFalse(extremos.iloc[1])
        self.assertTrue(extremos.iloc[2])   # (15 - 10.5) / 10.5 = +42.8%
        self.assertFalse(extremos.iloc[3])
        self.assertFalse(extremos.iloc[4])

    def test_detectar_posible_split_no_ajustado(self):
        fechas = pd.date_range("2025-01-01", periods=6)
        # Simular contrasplit 1:10 (de 0.015 a 0.150)
        precios = pd.Series([0.015, 0.015, 0.150, 0.148, 0.152, 0.150], index=fechas)
        retornos = precios.pct_change()
        volumen = pd.Series([1000000, 950000, 100000, 98000, 102000, 95000], index=fechas)

        splits = detectar_posible_split_no_ajustado(precios, retornos, volumen=volumen)
        self.assertTrue(splits.iloc[2])  # Salto de ~10x
        self.assertEqual(splits.sum(), 1)

    def test_detectar_rachas_retorno_cero(self):
        fechas = pd.date_range("2025-01-01", periods=8)
        # Racha de 3 ceros en retornos
        retornos = pd.Series([0.01, 0.0, 0.0, 0.0, -0.02, 0.0, 0.0, 0.03], index=fechas)
        rachas = detectar_rachas_retorno_cero(retornos, longitud_minima=3)

        self.assertFalse(rachas.iloc[0])
        self.assertTrue(rachas.iloc[1])
        self.assertTrue(rachas.iloc[2])
        self.assertTrue(rachas.iloc[3])
        self.assertFalse(rachas.iloc[4])
        self.assertFalse(rachas.iloc[5])  # Solo 2 ceros consecutivos, no llega a 3
        self.assertFalse(rachas.iloc[6])
        self.assertFalse(rachas.iloc[7])

    def test_detectar_precios_estancados(self):
        fechas = pd.date_range("2025-01-01", periods=8)
        precios = pd.Series([4.8, 5.0, 5.0, 5.0, 5.0, 5.2, 5.1, 5.1], index=fechas)
        high = pd.Series([4.9, 5.0, 5.0, 5.0, 5.0, 5.3, 5.2, 5.2], index=fechas)
        low = pd.Series([4.7, 5.0, 5.0, 5.0, 5.0, 5.1, 5.0, 5.0], index=fechas)

        estancados = detectar_precios_estancados(precios, high=high, low=low, longitud_racha=3)
        self.assertFalse(estancados.iloc[0])
        self.assertFalse(estancados.iloc[1])  # 4.8 -> 5.0 hubo cambio (+4.16%)
        self.assertTrue(estancados.iloc[2])   # Racha de 3 precios idénticos (5.0)
        self.assertTrue(estancados.iloc[3])
        self.assertTrue(estancados.iloc[4])
        self.assertFalse(estancados.iloc[5])  # 5.0 -> 5.2 hubo cambio

    def test_auditar_serie_precios(self):
        fechas = pd.date_range("2025-01-01", periods=6)
        precios = pd.Series([10.0, 10.0, 10.0, 10.0, 15.0, 15.0], index=fechas)
        volumen = pd.Series([5000, 0, 0, 0, 10000, 12000], index=fechas)

        df_diag = auditar_serie_precios(precios, volumen=volumen, racha_cero=3)
        self.assertEqual(len(df_diag), 6)
        self.assertTrue(df_diag["es_racha_cero"].iloc[2])
        self.assertTrue(df_diag["es_volumen_nulo"].iloc[1])
        self.assertTrue(df_diag["es_extremo_25"].iloc[4])  # +50%

    def test_filtrar_serie_volatilidad_limpia(self):
        fechas = pd.date_range("2025-01-01", periods=6)
        # Día 2 tiene un contrasplit 1:5 (+400%)
        precios = pd.Series([1.0, 1.05, 5.25, 5.30, 5.20, 5.25], index=fechas)
        ret_limpio = filtrar_serie_volatilidad_limpia(precios)

        self.assertTrue(np.isnan(ret_limpio.iloc[2]))
        self.assertEqual(round(float(ret_limpio.iloc[1]), 4), 0.05)
        self.assertEqual(round(float(ret_limpio.iloc[3]), 4), round((5.30 - 5.25) / 5.25, 4))


if __name__ == "__main__":
    unittest.main()
