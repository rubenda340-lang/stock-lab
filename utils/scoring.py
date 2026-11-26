"""
Módulo Centralizado de Cálculo de Scores - StockLab
[V8.3] Delegación al motor único en views.ranking — Single Source of Truth.

Este módulo actúa como adaptador: traduce la salida del motor V8.3
(calidad 4-componentes + oportunidad 3-componentes) al formato de
5 pilares que consume la vista Empresas.

Uso:
    from utils.scoring import calcular_score_completo
    
    score_data = calcular_score_completo(df_historico, info_fundamentales)
    # Retorna dict con: calidad, val, mom, div, seg, score_global
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional


def _normalizar_info_para_motor(info: dict) -> dict:
    """
    Asegura que `info` siga el contrato normalizado del motor de Ranking V8.3
    (debtToEquity en ratio decimal, dividendYield en decimal puro y priceToSales).
    Si `info` ya proviene de services.data_provider (`_data_source` presente),
    se devuelve intacto.
    """
    if not info or info.get("_data_source"):
        return info or {}

    norm = dict(info)

    # Yahoo .info entrega debtToEquity en porcentaje (ej: 49.17 para 0.4917)
    dte = norm.get("debtToEquity")
    if isinstance(dte, (int, float)) and not pd.isna(dte):
        norm["debtToEquity"] = float(dte) / 100.0

    # Yahoo .info usa priceToSalesTrailing12Months en vez de priceToSales
    if norm.get("priceToSales") is None and norm.get("priceToSalesTrailing12Months") is not None:
        norm["priceToSales"] = norm.get("priceToSalesTrailing12Months")

    # Normalizar dividendYield a decimal puro (evita ×100 en yields < 1% como MTS.MC 0.82%)
    try:
        from services.data_provider import _normalizar_dividend_yield
        norm["dividendYield"] = _normalizar_dividend_yield(info)
    except Exception:
        pass

    return norm


def calcular_score_completo(
    df_historico: Optional[pd.DataFrame] = None,
    info_fundamentales: Optional[dict] = None,
    variacion_diaria: float = 0.0,
    sector: str = ""
) -> Dict[str, float]:
    """
    Calcula todas las sub-métricas y el Score Global delegando al motor V8.3.
    
    Internamente usa _desglosar_calidad_detalle() y _desglosar_oportunidad_detalle()
    del motor de ranking, y mapea sus 7 componentes a los 5 pilares de la vista Empresas:
    
        Motor V8.3                          →   Pilar Empresas
        ─────────────────────────────────────────────────────────
        calidad_negocio (0-40)              →   calidad (0-30)
        valoracion (0-40)                   →   val (0-25)
        momentum_tecnico (0-30)             →   mom (0-20)
        + momentum_fundamental (0-30)
        dividendos (0-10)                   →   div (0-15)
        fortaleza_financiera (0-30)         →   seg (0-10)
        + ventaja_competitiva (0-15)
    
    Parámetros:
    -----------
    df_historico : pd.DataFrame, opcional
        DataFrame histórico con columnas OHLCV para análisis técnico
    info_fundamentales : dict, opcional
        Diccionario con datos fundamentales de yfinance (.info)
    variacion_diaria : float, opcional
        Variación diaria en porcentaje para cálculo de momentum
    
    Retorna:
    --------
    dict con claves:
        - 'calidad': float (0-30)
        - 'val': float (0-25)
        - 'mom': float (0-20)
        - 'div': float (0-15)
        - 'seg': float (0-10)
        - 'score_global': float (0-100, suma exacta de las 5 métricas)
    """
    if info_fundamentales is None:
        info_fundamentales = {}

    info_norm = _normalizar_info_para_motor(info_fundamentales)
    sector_efectivo = sector or info_fundamentales.get("sector") or ""

    try:
        # Importación lazy para evitar dependencia circular
        from views.ranking import (
            _desglosar_calidad_detalle,
            _desglosar_oportunidad_detalle,
        )

        # Obtener desgloses del motor V8.3 (Single Source of Truth)
        desg_cal = _desglosar_calidad_detalle(info_norm, sector=sector_efectivo)
        desg_opo = _desglosar_oportunidad_detalle(
            df_historico, info_norm, variacion_diaria, sector=sector_efectivo
        )
        
        # ── Mapeo V8.3 → 5 Pilares Empresas ──
        
        # Pilar 1: Calidad (max 30) ← calidad_negocio (max 40), reescalado
        calidad = min(30.0, desg_cal['calidad_negocio'] * (30.0 / 40.0))
        
        # Pilar 2: Valoración (max 25) ← valoracion (max 40), reescalado
        val = min(25.0, desg_opo['valoracion'] * (25.0 / 40.0))
        
        # Pilar 3: Momentum (max 20) ← momentum_tecnico (max 30) + momentum_fundamental (max 30)
        mom_bruto = desg_opo['momentum_tecnico'] + desg_opo['momentum_fundamental']
        mom = min(20.0, mom_bruto * (20.0 / 60.0))
        
        # Pilar 4: Dividendos (max 15) ← dividendos (max 10), reescalado
        div = min(15.0, desg_cal['dividendos'] * (15.0 / 10.0))
        
        # Pilar 5: Seguridad (max 10) ← fortaleza_financiera (max 30) + ventaja_competitiva (max 15)
        seg_bruto = desg_cal['fortaleza_financiera'] + desg_cal['ventaja_competitiva']
        seg = min(10.0, seg_bruto * (10.0 / 45.0))
        
        # Score Global = suma de pilares (max 100)
        score_global = calidad + val + mom + div + seg
        
    except Exception as e:
        print(f"⚠️ Error en calcular_score_completo (adaptador V8.3): {e}")
        # Fallback seguro: puntuaciones neutras
        calidad = 15.0
        val = 12.0
        mom = 10.0
        div = 7.0
        seg = 5.0
        score_global = 49.0
    
    return {
        'calidad': round(calidad, 1),
        'val': round(val, 1),
        'mom': round(mom, 1),
        'div': round(div, 1),
        'seg': round(seg, 1),
        'score_global': round(score_global, 1)
    }


def generar_resumen_fundamental(info: dict) -> str:
    """
    [V8.3] Genera un resumen legible de fundamentales delegando al motor de ranking.
    """
    try:
        from views.ranking import generar_resumen_fundamental_v7
        return generar_resumen_fundamental_v7(info)
    except Exception:
        # Fallback inline si la importación falla
        try:
            per = float(info.get('trailingPE', 0.0) or 0.0)
            dividend_yield_raw = float(info.get('dividendYield', 0.0) or 0.0)
            p_b = float(info.get('priceToBook', 0.0) or 0.0)
            roe_raw = float(info.get('returnOnEquity', 0.0) or 0.0)
            
            dividend_yield_pct = dividend_yield_raw * 100 if abs(dividend_yield_raw) < 1 else dividend_yield_raw
            roe_pct = roe_raw * 100 if abs(roe_raw) < 1 else roe_raw
            
            if per > 0:
                return (f"PER: {round(per, 2)} | Div Yield: {round(dividend_yield_pct, 2)}% | "
                        f"P/B: {round(p_b, 2)} | ROE: {round(roe_pct, 2)}%")
            else:
                return "Datos fundamentales limitados"
        except (ValueError, TypeError):
            return "Datos fundamentales no disponibles"
