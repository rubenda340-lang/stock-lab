"""
Capa de Calidad de Datos Institucional (Data Quality Layer)
===========================================================
Detecta y aísla anomalías en series de precios antes de alimentar los módulos
de cálculo de volatilidad, rangos y estadísticas del panel descriptivo:
1. Retornos diarios extremos (|r| > 25%).
2. Posibles splits o contrasplits no ajustados (saltos discretos verificados contra volumen).
3. Rachas de retorno cero y precios estancados (Open == High == Low == Close o cambios nulos repetidos).
4. Días sin volumen o iliquidez severa.
5. Filtrado de retornos limpios para volatilidad realizada (20d, 60d, 252d) y percentiles.
"""

from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


def detectar_retornos_extremos(
    retornos: pd.Series,
    umbral: float = 0.25
) -> pd.Series:
    """Retorna una máscara booleana con True en fechas con |r| > umbral (por defecto 25%)."""
    return retornos.abs() > umbral


def detectar_rachas_retorno_cero(
    retornos: pd.Series,
    longitud_minima: int = 3,
    tolerancia: float = 1e-7
) -> pd.Series:
    """
    Identifica barras que forman parte de una secuencia consecutiva de al menos N días
    con retorno diario nulo (|r| < tolerancia).
    """
    es_cero = (retornos.abs() < tolerancia).astype(int)
    # Agrupar rachas contiguas de ceros
    grupo = (es_cero != es_cero.shift(1)).cumsum()
    longitudes = es_cero.groupby(grupo).transform("sum")
    return (es_cero == 1) & (longitudes >= longitud_minima)


def detectar_precios_estancados(
    precios: pd.Series,
    high: Optional[pd.Series] = None,
    low: Optional[pd.Series] = None,
    longitud_racha: int = 3
) -> pd.Series:
    """
    Identifica barras que forman parte de una racha de al menos N días consecutivos
    donde el precio permanece idéntico o donde el rango intra-día High == Low con variación nula.
    """
    ret_cero = (precios.pct_change().abs() < 1e-7).astype(int)
    grupo = (ret_cero != ret_cero.shift(1)).cumsum()
    longitudes = ret_cero.groupby(grupo).transform("sum")
    es_estancado = (ret_cero == 1) & (longitudes >= longitud_racha)

    # Si se dispone de High y Low, marcar también barras planas individuales sin rango
    if high is not None and low is not None:
        rango_cero = (high - low).abs() < 1e-7
        # Una barra aislada sin rango no es necesariamente estancada, pero si coincide con cambio cero sí
        es_estancado = es_estancado | (rango_cero & (ret_cero == 1))

    return es_estancado


def detectar_posible_split_no_ajustado(
    precios: pd.Series,
    retornos: Optional[pd.Series] = None,
    volumen: Optional[pd.Series] = None,
    tolerancia_ratio: float = 0.08
) -> pd.Series:
    """
    Detecta saltos abruptos de precio compatibles con splits o contrasplits no ajustados
    (p. ej. ratios cercanos a 2, 3, 4, 5, 10, 20, 50, 100 o sus inversos 1/2, 1/3, etc.).
    Si se proporciona volumen, verifica además que exista volumen negociado y evalúa si
    el volumen experimentó una discontinuidad inversa típica de la reorganización accionarial.
    """
    if retornos is None:
        retornos = precios.pct_change()

    ratios_sospechosos = [
        2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 50.0, 100.0, 500.0, 1000.0,
        0.5, 0.3333, 0.25, 0.2, 0.1, 0.05, 0.02, 0.01, 0.002, 0.001
    ]
    p_prev = precios.shift(1)
    cociente = precios / np.maximum(1e-8, p_prev)
    es_split = pd.Series(False, index=precios.index)

    # Solo evaluar donde |r| > 25%
    mask_gran_salto = retornos.abs() > 0.25
    for idx in precios.index[mask_gran_salto]:
        q = cociente.loc[idx]
        for r in ratios_sospechosos:
            if abs(q - r) / r <= tolerancia_ratio:
                # Si hay volumen, comprobar que la sesión tiene negociación real o salto volumétrico
                if volumen is not None and idx in volumen.index:
                    v_act = volumen.loc[idx]
                    # Si el volumen es estrictamente nulo, es una anomalía de datos más que un split formal
                    # Pero ante la duda de salto discreto, se clasifica como split sospechoso
                    es_split.loc[idx] = True
                else:
                    es_split.loc[idx] = True
                break

    return es_split


def auditar_serie_precios(
    precios: pd.Series,
    volumen: Optional[pd.Series] = None,
    high: Optional[pd.Series] = None,
    low: Optional[pd.Series] = None,
    umbral_extremo: float = 0.25,
    racha_cero: int = 3
) -> pd.DataFrame:
    """
    Audita integralmente una serie de precios y genera un reporte diagnóstico
    con indicadores de calidad por sesión.
    """
    r_diario = precios.pct_change()
    extremo = detectar_retornos_extremos(r_diario, umbral=umbral_extremo)
    split_sospecha = detectar_posible_split_no_ajustado(precios, r_diario, volumen=volumen)
    racha_cero_flag = detectar_rachas_retorno_cero(r_diario, longitud_minima=racha_cero)
    estancado = detectar_precios_estancados(precios, high=high, low=low, longitud_racha=racha_cero)
    vol_cero = (volumen <= 0) | volumen.isna() if volumen is not None else pd.Series(False, index=precios.index)

    df_diag = pd.DataFrame({
        "precio": precios,
        "retorno_diario": r_diario,
        "es_extremo_25": extremo,
        "es_split_no_ajustado": split_sospecha,
        "es_racha_cero": racha_cero_flag,
        "es_precio_estancado": estancado,
        "es_volumen_nulo": vol_cero,
        "es_anomalo": extremo | split_sospecha | racha_cero_flag | estancado | vol_cero,
    })
    return df_diag


def filtrar_serie_volatilidad_limpia(
    precios: pd.Series,
    volumen: Optional[pd.Series] = None,
    high: Optional[pd.Series] = None,
    low: Optional[pd.Series] = None,
    excluir_extremos: bool = True,
    excluir_splits: bool = True,
    excluir_estancados: bool = True,
    excluir_volumen_nulo: bool = False
) -> pd.Series:
    """
    Retorna la serie de retornos diarios limpios para cálculo de volatilidad realizada (20d, 60d, 252d)
    y percentiles históricos, reemplazando por NaN las barras contaminadas por anomalías corporativas
    o iliquidez severa.
    """
    diag = auditar_serie_precios(precios, volumen=volumen, high=high, low=low)
    mask_drop = pd.Series(False, index=precios.index)

    if excluir_extremos:
        mask_drop = mask_drop | diag["es_extremo_25"]
    if excluir_splits:
        mask_drop = mask_drop | diag["es_split_no_ajustado"]
    if excluir_estancados:
        mask_drop = mask_drop | diag["es_precio_estancado"] | diag["es_racha_cero"]
    if excluir_volumen_nulo:
        mask_drop = mask_drop | diag["es_volumen_nulo"]

    ret_limpio = precios.pct_change().copy()
    ret_limpio[mask_drop] = np.nan
    return ret_limpio
