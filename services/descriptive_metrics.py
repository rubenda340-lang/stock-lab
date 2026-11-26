"""
Módulo de Métricas Descriptivas y de Riesgo Histórico
=====================================================
Calcula estadísticas descriptivas para el Panel de Análisis:
- Volatilidad realizada anualizada (20d, 60d, 252d) y percentiles históricos (1 a 3 años, data <= t).
- Frecuencias empíricas de movimientos históricos (|r| > X% a h días en 252d) con etiqueta de no pronóstico.
- Extremos de 52 semanas y drawdowns.
- Métricas de liquidez (volumen en € y frecuencia de días planos).
- Sensibilidad y retorno relativo vs IBEX 35 (Beta y Correlación 60d).
- Carga de snapshot fundamental con fecha de auditoría.
Utiliza la capa de calidad de datos para filtrar saltos artificiales o splits no ajustados.
"""

import os
import glob
import json
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
import streamlit as st

from services.data_quality import filtrar_serie_volatilidad_limpia, auditar_serie_precios

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CACHE_PRECIOS = os.path.join(BASE_DIR, "data", "backtest_cache_6y.parquet")
CACHE_FUNDAMENTALS_DIR = os.path.join(BASE_DIR, "data", "cache", "fundamentals_dp")


@st.cache_data(ttl=3600, show_spinner=False)
def cargar_datos_cache_historico() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Carga las matrices de precios, volumen y rangos desde el parquet local (cacheado en memoria)."""
    if not os.path.exists(CACHE_PRECIOS):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    df_cache = pd.read_parquet(CACHE_PRECIOS)
    closes = df_cache.xs("Close", level=1, axis=1)
    highs = df_cache.xs("High", level=1, axis=1)
    lows = df_cache.xs("Low", level=1, axis=1)
    vols = df_cache.xs("Volume", level=1, axis=1)
    return closes, highs, lows, vols


def calcular_metricas_volatilidad(
    precios: pd.Series,
    volumen: Optional[pd.Series] = None,
    highs: Optional[pd.Series] = None,
    lows: Optional[pd.Series] = None
) -> Dict:
    """
    Calcula la volatilidad realizada anualizada a 20d, 60d y 252d sobre retornos limpios,
    así como el percentil de la volatilidad actual frente a su propia historia (1 y 3 años, datos <= t).
    """
    ret_limpio = filtrar_serie_volatilidad_limpia(precios, volumen=volumen, high=highs, low=lows)
    
    # Volatilidades móviles anualizadas (retorno diario std * sqrt(252) * 100)
    vol_20_series = ret_limpio.rolling(20).std() * np.sqrt(252) * 100.0
    vol_60_series = ret_limpio.rolling(60).std() * np.sqrt(252) * 100.0
    vol_252_series = ret_limpio.rolling(252).std() * np.sqrt(252) * 100.0

    vol_20_act = float(vol_20_series.dropna().iloc[-1]) if len(vol_20_series.dropna()) > 0 else np.nan
    vol_60_act = float(vol_60_series.dropna().iloc[-1]) if len(vol_60_series.dropna()) > 0 else np.nan
    vol_252_act = float(vol_252_series.dropna().iloc[-1]) if len(vol_252_series.dropna()) > 0 else np.nan

    # Percentiles históricos de vol_20 (datos estrictamente <= t)
    # 1 año (252 días de historia)
    hist_1y = vol_20_series.dropna().iloc[-252:]
    pct_1y = float((hist_1y <= vol_20_act).mean() * 100.0) if len(hist_1y) >= 60 else np.nan

    # 3 años (756 días de historia)
    hist_3y = vol_20_series.dropna().iloc[-756:]
    pct_3y = float((hist_3y <= vol_20_act).mean() * 100.0) if len(hist_3y) >= 200 else np.nan

    # Interpretación descriptiva institucional
    if np.isnan(pct_1y):
        regimen = "Datos insuficientes"
    elif pct_1y < 20.0:
        regimen = "Compresión extrema (Percentil < 20% histórico)"
    elif pct_1y < 40.0:
        regimen = "Volatilidad moderada-baja (Percentil 20-40%)"
    elif pct_1y <= 60.0:
        regimen = "Régimen histórico promedio (Percentil 40-60%)"
    elif pct_1y <= 85.0:
        regimen = "Volatilidad moderada-alta (Percentil 60-85%)"
    else:
        regimen = "Expansión extrema de volatilidad (Percentil > 85%)"

    return {
        "vol_20d_anual": vol_20_act,
        "vol_60d_anual": vol_60_act,
        "vol_252d_anual": vol_252_act,
        "percentil_1y": pct_1y,
        "percentil_3y": pct_3y,
        "regimen_descriptivo": regimen,
        "serie_vol_20": vol_20_series,
        "serie_vol_60": vol_60_series,
    }


def calcular_frecuencias_historicas(
    precios: pd.Series,
    ventana_dias: int = 252
) -> List[Dict]:
    """
    Calcula la frecuencia histórica de movimientos observados mayores que ±X%
    a diferentes horizontes en los últimos 252 días (barras completas).
    Para h > 1 indica explícitamente 'k de n ventanas solapadas'.
    Incluye n visible y advertencia obligatoria de no pronóstico.
    """
    p_reciente = precios.iloc[-ventana_dias - 25:]  # Margen para ventanas
    horizontes = [("1D", 1), ("3D", 3), ("5D (1S)", 5), ("20D (1M)", 20)]
    umbrales = [0.01, 0.02, 0.03, 0.05, 0.10]  # 1%, 2%, 3%, 5%, 10%

    filas = []
    for nom_hz, h in horizontes:
        ret_h = (p_reciente - p_reciente.shift(h)) / p_reciente.shift(h)
        # Tomar las últimas `ventana_dias` observaciones válidas
        ret_valid = ret_h.dropna().iloc[-ventana_dias:]
        n_obs = len(ret_valid)

        for u in umbrales:
            u_pct = round(u * 100.0, 1)
            k_sube = int((ret_valid > u).sum())
            k_baja = int((ret_valid < -u).sum())
            k_extremo = int((ret_valid.abs() > u).sum())

            frec_sube = float((ret_valid > u).mean() * 100.0) if n_obs > 0 else np.nan
            frec_baja = float((ret_valid < -u).mean() * 100.0) if n_obs > 0 else np.nan
            frec_extremo = float((ret_valid.abs() > u).mean() * 100.0) if n_obs > 0 else np.nan

            desc_obs = f"{k_extremo} de {n_obs} ventanas solapadas" if h > 1 else f"{k_extremo} de {n_obs} barras"

            filas.append({
                "horizonte": nom_hz,
                "dias_h": h,
                "umbral_pct": u_pct,
                "n_observaciones": n_obs,
                "k_extremo": k_extremo,
                "desc_obs": desc_obs,
                "frec_sube_pct": round(frec_sube, 1),
                "frec_baja_pct": round(frec_baja, 1),
                "frec_extremo_total_pct": round(frec_extremo, 1),
                "etiqueta": "Frecuencia histórica, no pronóstico",
            })
    return filas


def calcular_extremos_y_drawdown(
    precios: pd.Series,
    highs: Optional[pd.Series] = None,
    lows: Optional[pd.Series] = None,
    volumen: Optional[pd.Series] = None,
    ventana_dias: int = 252
) -> Dict:
    """
    Calcula la distancia porcentual al máximo y mínimo de 52 semanas,
    la posición dentro del rango y el drawdown actual / máximo de 252 días.
    Aplica filtro de calidad de datos para excluir barras anómalas (splits no ajustados)
    e informa del número de barras excluidas por ticker.
    """
    diag = auditar_serie_precios(precios, volumen=volumen, high=highs, low=lows)
    mask_anomala = diag["es_split_no_ajustado"]
    
    p_window = precios.iloc[-ventana_dias:].copy()
    mask_sub = mask_anomala.iloc[-ventana_dias:] if len(mask_anomala) >= ventana_dias else mask_anomala
    n_excluidas = int(mask_sub.sum())

    if n_excluidas > 0:
        p_window[mask_sub] = np.nan
        p_window = p_window.ffill().bfill()

    p_act = float(p_window.iloc[-1])

    h_sub = highs.iloc[-ventana_dias:].copy() if highs is not None else p_window.copy()
    l_sub = lows.iloc[-ventana_dias:].copy() if lows is not None else p_window.copy()
    if n_excluidas > 0:
        h_sub[mask_sub] = np.nan
        h_sub = h_sub.ffill().bfill()
        l_sub[mask_sub] = np.nan
        l_sub = l_sub.ffill().bfill()

    max_52w = float(h_sub.max())
    min_52w = float(l_sub.min())

    dist_max = float(((p_act - max_52w) / max_52w) * 100.0) if max_52w > 0 else 0.0
    dist_min = float(((p_act - min_52w) / min_52w) * 100.0) if min_52w > 0 else 0.0

    rango_total = max_52w - min_52w
    posicion_rango_pct = float(((p_act - min_52w) / rango_total) * 100.0) if rango_total > 0 else 50.0

    # Drawdown móvil sobre serie limpia
    pico_acum = p_window.cummax()
    drawdown_series = (p_window - pico_acum) / pico_acum
    drawdown_actual_pct = float(drawdown_series.iloc[-1] * 100.0)
    max_drawdown_pct = float(drawdown_series.min() * 100.0)

    return {
        "precio_actual": p_act,
        "max_52w": max_52w,
        "min_52w": min_52w,
        "distancia_max_52w_pct": dist_max,
        "distancia_min_52w_pct": dist_min,
        "posicion_rango_52w_pct": min(100.0, max(0.0, posicion_rango_pct)),
        "drawdown_actual_pct": drawdown_actual_pct,
        "max_drawdown_252d_pct": max_drawdown_pct,
        "barras_excluidas": n_excluidas,
    }


def calcular_metricas_liquidez(
    precios: pd.Series,
    volumen: Optional[pd.Series] = None,
    volumenes_mercado_medios: Optional[pd.Series] = None,
    ventana_dias: int = 252
) -> Dict:
    """
    Calcula el volumen medio en euros negociado (20d y 60d), el porcentaje de días planos
    (cambio 0.00%) y la clasificación relativa de tercil de liquidez en BME.
    """
    p_w = precios.iloc[-ventana_dias:]
    r_diario = p_w.pct_change()
    pct_ceros = float((r_diario.abs() < 1e-7).mean() * 100.0)

    if volumen is not None:
        v_w = volumen.iloc[-ventana_dias:]
        vol_eur = p_w * v_w
        vol_eur_20d = float(vol_eur.iloc[-20:].mean()) if len(vol_eur) >= 20 else float(vol_eur.mean())
        vol_eur_60d = float(vol_eur.iloc[-60:].mean()) if len(vol_eur) >= 60 else float(vol_eur.mean())
    else:
        vol_eur_20d = np.nan
        vol_eur_60d = np.nan

    # Clasificación de tercil de liquidez
    if volumenes_mercado_medios is not None and not np.isnan(vol_eur_60d):
        q1 = volumenes_mercado_medios.quantile(0.333)
        q2 = volumenes_mercado_medios.quantile(0.666)
        if vol_eur_60d >= q2:
            tercil = "Tercil Alto (Alta Liquidez)"
            alerta_open = False
        elif vol_eur_60d >= q1:
            tercil = "Tercil Medio (Liquidez Moderada)"
            alerta_open = False
        else:
            tercil = "Tercil Bajo (Baja Liquidez / Aperturas no verificadas)"
            alerta_open = True
    else:
        tercil = "Sin clasificar"
        alerta_open = False

    return {
        "volumen_eur_20d": vol_eur_20d,
        "volumen_eur_60d": vol_eur_60d,
        "pct_dias_sin_variacion": pct_ceros,
        "tercil_liquidez": tercil,
        "alerta_microestructura_open": alerta_open,
    }


def calcular_sensibilidad_benchmark(
    precios_activo: pd.Series,
    precios_ibex: pd.Series,
    ventana_corr: int = 60
) -> Dict:
    """
    Calcula rentabilidad relativa histórica pasada contra IBEX 35 (1M, 3M, 6M, 1A)
    y la correlación / beta móvil a 60 días.
    """
    idx_comun = precios_activo.dropna().index.intersection(precios_ibex.dropna().index)
    if len(idx_comun) < 30:
        return {}

    p_a = precios_activo.loc[idx_comun]
    p_ib = precios_ibex.loc[idx_comun]

    r_a = p_a.pct_change().dropna()
    r_ib = p_ib.pct_change().dropna()
    idx_r = r_a.index.intersection(r_ib.index)
    r_a = r_a.loc[idx_r]
    r_ib = r_ib.loc[idx_r]

    # Retorno relativo acumulado
    def ret_rel(n_bars):
        if len(p_a) <= n_bars:
            return np.nan
        r_act = (p_a.iloc[-1] - p_a.iloc[-n_bars - 1]) / p_a.iloc[-n_bars - 1]
        r_bm = (p_ib.iloc[-1] - p_ib.iloc[-n_bars - 1]) / p_ib.iloc[-n_bars - 1]
        return float((r_act - r_bm) * 100.0)

    rel_1m = ret_rel(21)
    rel_3m = ret_rel(63)
    rel_6m = ret_rel(126)
    rel_1y = ret_rel(252)

    # Correlación y Beta móvil 60d
    r_a_60 = r_a.iloc[-ventana_corr:]
    r_ib_60 = r_ib.iloc[-ventana_corr:]

    var_ib = float(np.var(r_ib_60))
    cov_mat = np.cov(r_a_60, r_ib_60)
    cov_a_ib = float(cov_mat[0, 1])

    beta_60d = float(cov_a_ib / var_ib) if var_ib > 1e-10 else np.nan
    corr_60d = float(np.corrcoef(r_a_60, r_ib_60)[0, 1]) if len(r_a_60) > 10 else np.nan

    return {
        "retorno_relativo_1m_pct": rel_1m,
        "retorno_relativo_3m_pct": rel_3m,
        "retorno_relativo_6m_pct": rel_6m,
        "retorno_relativo_1a_pct": rel_1y,
        "correlacion_ibex_60d": corr_60d,
        "beta_ibex_60d": beta_60d,
    }


def cargar_snapshot_fundamental(ticker: str) -> Tuple[Dict, str]:
    """Carga los datos fundamentales del disco y la fecha del archivo de snapshot."""
    clean_tk = ticker.replace(".", "_")
    f_path = os.path.join(CACHE_FUNDAMENTALS_DIR, f"{clean_tk}.json")
    if not os.path.exists(f_path):
        return {}, "No disponible"

    try:
        from datetime import datetime
        mtime = os.path.getmtime(f_path)
        fecha_str = datetime.fromtimestamp(mtime).strftime("%d/%m/%Y")
        with open(f_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data, fecha_str
    except Exception:
        return {}, "No disponible"
