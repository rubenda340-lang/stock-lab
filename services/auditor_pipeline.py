"""
StockLab V7 — Auditoría de Trazabilidad del Pipeline de Datos
==============================================================
Módulo de diagnóstico FASE 1.3 — SOLO INSTRUMENTACIÓN, SIN CAMBIOS EN SCORING.

Proporciona:
  - auditoria_pipeline_completa(ticker): trazabilidad completa API → score
  - certificacion_normalizacion(): verifica consistencia de escalas entre fuentes
  - mapa_variables_utilizadas(): inventario de claves usadas vs descargadas
  - generar_informe_certificacion(): informe final de certificación
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from services.data_provider import (
    _obtener_yahoo, _obtener_fmp, _obtener_finnhub,
    _diccionario_vacio, _tiene_datos_utiles, _safe_float,
    _CLAVES_ESTANDAR, fetch_fundamentals_uncached
)


# ═══════════════════════════════════════════════════════════════════
# 📋 VARIABLES QUE USA EL MOTOR DE SCORING V6 (views/ranking.py)
# ═══════════════════════════════════════════════════════════════════

# Variables usadas en _calcular_calidad_empresarial()
VARIABLES_CALIDAD = {
    'returnOnEquity':   {'componente': 'Calidad Negocio', 'sub': 'ROE', 'max_pts': 10},
    'operatingMargins': {'componente': 'Calidad Negocio', 'sub': 'Margen Operativo', 'max_pts': 10},
    'revenueGrowth':    {'componente': 'Calidad Negocio', 'sub': 'Crecimiento Ingresos', 'max_pts': 20},
    'debtToEquity':     {'componente': 'Fortaleza Financiera', 'sub': 'Deuda/Equity', 'max_pts': 10},
    'currentRatio':     {'componente': 'Fortaleza Financiera', 'sub': 'Ratio Liquidez', 'max_pts': 10},
    'interestCoverage': {'componente': 'Fortaleza Financiera', 'sub': 'Cobertura Intereses', 'max_pts': 10},
    'dividendYield':    {'componente': 'Dividendos', 'sub': 'Rendimiento Dividendo', 'max_pts': 10},
    'payoutRatio':      {'componente': 'Dividendos', 'sub': 'Payout Ratio', 'max_pts': 10},
    'marketCap':        {'componente': 'Ventaja Competitiva', 'sub': 'Capitalización', 'max_pts': 20},
}

# Variables usadas en _calcular_oportunidad_inversion()
VARIABLES_OPORTUNIDAD = {
    'trailingPE':          {'componente': 'Valoración', 'sub': 'PER', 'max_pts': 20},
    'priceToBook':         {'componente': 'Valoración', 'sub': 'P/B', 'max_pts': 10},
    'enterpriseToEbitda':  {'componente': 'Valoración', 'sub': 'EV/EBITDA', 'max_pts': 10},
    'revenueGrowth':       {'componente': 'Momentum Fundamental', 'sub': 'Crecimiento Ingresos', 'max_pts': 5},
}

# Todas las variables descargadas por data_provider
VARIABLES_DESCARGADAS = set(_CLAVES_ESTANDAR.keys()) - {'_data_source', '_data_quality'}

# Variables descargadas pero NO usadas por ningún motor
VARIABLES_NO_UTILIZADAS = VARIABLES_DESCARGADAS - set(VARIABLES_CALIDAD.keys()) - set(VARIABLES_OPORTUNIDAD.keys())


# ═══════════════════════════════════════════════════════════════════
# 🔍 TRAZABILIDAD DE UNA SOLA VARIABLE A TRAVÉS DEL PIPELINE
# ═══════════════════════════════════════════════════════════════════

def _trazar_variable_yahoo(ticker: str, clave: str) -> Dict[str, Any]:
    """Obtiene el valor RAW de Yahoo antes de normalización."""
    try:
        import yfinance as yf
        from services.data_provider import _session
        t = yf.Ticker(ticker, session=_session)
        info = t.info
        raw_value = info.get(clave)
        return {'raw': raw_value, 'tipo_raw': type(raw_value).__name__, 'disponible': raw_value is not None}
    except Exception as e:
        return {'raw': None, 'tipo_raw': 'error', 'disponible': False, 'error': str(e)}


def _trazar_normalizacion(clave: str, raw_value: Any, fuente: str) -> Dict[str, Any]:
    """Simula la normalización que aplica data_provider para una clave dada."""
    resultado = {'clave': clave, 'raw': raw_value, 'normalizado': None, 'transformacion': 'ninguna'}

    if raw_value is None:
        resultado['normalizado'] = None
        resultado['transformacion'] = 'None → None (dato ausente)'
        return resultado

    # NOTA: data_provider.py (SSOT) ya normaliza debtToEquity a decimal puro
    # antes de exponerlo. La traza ya no necesita re-aplicar la conversión.
    val = _safe_float(raw_value)
    resultado['normalizado'] = val
    if val is not None:
        resultado['transformacion'] = f'_safe_float({raw_value}) = {val}'
    else:
        resultado['transformacion'] = f'{raw_value} → None (_safe_float falló)'

    return resultado


# ═══════════════════════════════════════════════════════════════════
# 🎯 SIMULACIÓN EXACTA DEL MOTOR DE SCORING CON TRAZABILIDAD
# ═══════════════════════════════════════════════════════════════════

def _simular_calidad_con_traza(info: dict) -> Dict[str, Any]:
    """
    Replica EXACTAMENTE la lógica de _calcular_calidad_empresarial() de views/ranking.py
    pero registrando cada paso intermedio. NO modifica ninguna fórmula.
    """
    traza = {
        'variables': {},
        'componentes': {},
        'score_total': 0.0,
        'errores': [],
    }

    try:
        # ── CALIDAD DEL NEGOCIO (Máx 40 pts) ──

        # ROE
        roe_raw = pd.to_numeric(info.get('returnOnEquity', 0), errors='coerce') or 0.0
        roe_pct = roe_raw * 100 if abs(roe_raw) < 1 else roe_raw
        dato_original = info.get('returnOnEquity')

        if roe_pct > 20:
            score_roe = 10; regla = f'{roe_pct:.1f}% > 20% → 10 pts'
        elif roe_pct > 15:
            score_roe = 8; regla = f'{roe_pct:.1f}% > 15% → 8 pts'
        elif roe_pct > 10:
            score_roe = 6; regla = f'{roe_pct:.1f}% > 10% → 6 pts'
        elif roe_pct > 5:
            score_roe = 3; regla = f'{roe_pct:.1f}% > 5% → 3 pts'
        else:
            score_roe = 0; regla = f'{roe_pct:.1f}% ≤ 5% → 0 pts'

        traza['variables']['returnOnEquity'] = {
            'original': dato_original, 'convertido': roe_raw, 'pct': roe_pct,
            'score': score_roe, 'regla': regla, 'es_none_original': dato_original is None,
        }

        # Margen Operativo
        op_margin_raw = pd.to_numeric(info.get('operatingMargins', 0), errors='coerce') or 0.0
        op_margin_pct = op_margin_raw * 100 if abs(op_margin_raw) < 1 else op_margin_raw
        dato_original_op = info.get('operatingMargins')

        if op_margin_pct > 20:
            score_margin = 10; regla_op = f'{op_margin_pct:.1f}% > 20% → 10 pts'
        elif op_margin_pct > 15:
            score_margin = 8; regla_op = f'{op_margin_pct:.1f}% > 15% → 8 pts'
        elif op_margin_pct > 10:
            score_margin = 6; regla_op = f'{op_margin_pct:.1f}% > 10% → 6 pts'
        elif op_margin_pct > 5:
            score_margin = 3; regla_op = f'{op_margin_pct:.1f}% > 5% → 3 pts'
        else:
            score_margin = 0; regla_op = f'{op_margin_pct:.1f}% ≤ 5% → 0 pts'

        traza['variables']['operatingMargins'] = {
            'original': dato_original_op, 'convertido': op_margin_raw, 'pct': op_margin_pct,
            'score': score_margin, 'regla': regla_op, 'es_none_original': dato_original_op is None,
        }

        # Crecimiento Ingresos
        rev_growth_raw = pd.to_numeric(info.get('revenueGrowth', 0), errors='coerce') or 0.0
        rev_growth_pct = rev_growth_raw * 100 if abs(rev_growth_raw) < 1 else rev_growth_raw
        dato_original_rev = info.get('revenueGrowth')

        if rev_growth_pct > 20:
            score_crecimiento = 20; regla_rev = f'{rev_growth_pct:.1f}% > 20% → 20 pts'
        elif rev_growth_pct > 10:
            score_crecimiento = 15; regla_rev = f'{rev_growth_pct:.1f}% > 10% → 15 pts'
        elif rev_growth_pct > 5:
            score_crecimiento = 10; regla_rev = f'{rev_growth_pct:.1f}% > 5% → 10 pts'
        elif rev_growth_pct > 0:
            score_crecimiento = 5; regla_rev = f'{rev_growth_pct:.1f}% > 0% → 5 pts'
        else:
            score_crecimiento = 0; regla_rev = f'{rev_growth_pct:.1f}% ≤ 0% → 0 pts'

        traza['variables']['revenueGrowth'] = {
            'original': dato_original_rev, 'convertido': rev_growth_raw, 'pct': rev_growth_pct,
            'score': score_crecimiento, 'regla': regla_rev, 'es_none_original': dato_original_rev is None,
        }

        calidad_negocio = score_roe + score_margin + score_crecimiento
        traza['componentes']['calidad_negocio'] = {
            'score': calidad_negocio, 'max': 40,
            'desglose': f'ROE({score_roe}) + Margen({score_margin}) + Crecimiento({score_crecimiento})',
        }

        # ── FORTALEZA FINANCIERA (Máx 30 pts) ──

        # data_provider.py (SSOT) ya entrega debtToEquity en escala decimal pura:
        #   0.45 = 45% D/E,  1.00 = 100% D/E,  1.80 = 180% D/E
        # NO se debe aplicar ninguna re-normalización adicional.
        debt_equity_val = _safe_float(info.get('debtToEquity'))
        debt_equity_ratio = debt_equity_val if debt_equity_val is not None else 0.0
        dato_original_de = info.get('debtToEquity')

        if debt_equity_val is None:
            score_deuda = 0; regla_de = 'Dato ausente (None) -> 0 pts'
        elif debt_equity_ratio < 0.5:
            score_deuda = 10; regla_de = f'{debt_equity_ratio:.2f} < 0.5 -> 10 pts'
        elif debt_equity_ratio <= 1.0:
            score_deuda = 8; regla_de = f'{debt_equity_ratio:.2f} <= 1.0 -> 8 pts'
        elif debt_equity_ratio <= 2.0:
            score_deuda = 5; regla_de = f'{debt_equity_ratio:.2f} <= 2.0 -> 5 pts'
        else:
            score_deuda = 0; regla_de = f'{debt_equity_ratio:.2f} > 2.0 -> 0 pts'

        traza['variables']['debtToEquity'] = {
            'original': dato_original_de, 'ratio_decimal': debt_equity_ratio,
            'score': score_deuda, 'regla': regla_de, 'es_none_original': dato_original_de is None,
        }

        current_ratio = pd.to_numeric(info.get('currentRatio', 0), errors='coerce') or 0.0
        dato_original_cr = info.get('currentRatio')

        if current_ratio > 2.0:
            score_current = 10; regla_cr = f'{current_ratio:.2f} > 2.0 → 10 pts'
        elif current_ratio >= 1.5:
            score_current = 8; regla_cr = f'{current_ratio:.2f} ≥ 1.5 → 8 pts'
        elif current_ratio >= 1.0:
            score_current = 5; regla_cr = f'{current_ratio:.2f} ≥ 1.0 → 5 pts'
        else:
            score_current = 0; regla_cr = f'{current_ratio:.2f} < 1.0 → 0 pts'

        traza['variables']['currentRatio'] = {
            'original': dato_original_cr, 'convertido': current_ratio,
            'score': score_current, 'regla': regla_cr, 'es_none_original': dato_original_cr is None,
        }

        interest_coverage = pd.to_numeric(info.get('interestCoverage', 0), errors='coerce') or 0.0
        dato_original_ic = info.get('interestCoverage')

        if interest_coverage > 10:
            score_intereses = 10; regla_ic = f'{interest_coverage:.2f} > 10 → 10 pts'
        elif interest_coverage > 5:
            score_intereses = 8; regla_ic = f'{interest_coverage:.2f} > 5 → 8 pts'
        elif interest_coverage > 2:
            score_intereses = 5; regla_ic = f'{interest_coverage:.2f} > 2 → 5 pts'
        elif interest_coverage > 0:
            score_intereses = 3; regla_ic = f'{interest_coverage:.2f} > 0 → 3 pts'
        else:
            score_intereses = 0; regla_ic = f'{interest_coverage:.2f} ≤ 0 → 0 pts'

        traza['variables']['interestCoverage'] = {
            'original': dato_original_ic, 'convertido': interest_coverage,
            'score': score_intereses, 'regla': regla_ic, 'es_none_original': dato_original_ic is None,
        }

        fortaleza_financiera = score_deuda + score_current + score_intereses
        traza['componentes']['fortaleza_financiera'] = {
            'score': fortaleza_financiera, 'max': 30,
            'desglose': f'Deuda({score_deuda}) + Liquidez({score_current}) + Cobertura({score_intereses})',
        }

        # ── DIVIDENDOS SOSTENIBLES (Máx 10 pts) ──

        dividend_yield_raw = pd.to_numeric(info.get('dividendYield', 0), errors='coerce') or 0.0
        dividend_yield_pct = dividend_yield_raw * 100 if abs(dividend_yield_raw) < 1 else dividend_yield_raw
        dato_original_dy = info.get('dividendYield')

        payout_ratio_raw = pd.to_numeric(info.get('payoutRatio', 0), errors='coerce') or 0.0
        payout_ratio_pct = payout_ratio_raw * 100 if abs(payout_ratio_raw) < 1 else payout_ratio_raw
        dato_original_pr = info.get('payoutRatio')

        if dividend_yield_pct > 0 and payout_ratio_pct < 70:
            score_dividendos = 10; regla_div = f'Yield={dividend_yield_pct:.1f}%>0 AND Payout={payout_ratio_pct:.1f}%<70% → 10 pts'
        elif dividend_yield_pct > 0 and payout_ratio_pct < 90:
            score_dividendos = 5; regla_div = f'Yield={dividend_yield_pct:.1f}%>0 AND Payout={payout_ratio_pct:.1f}%<90% → 5 pts'
        else:
            score_dividendos = 0; regla_div = f'Yield={dividend_yield_pct:.1f}%, Payout={payout_ratio_pct:.1f}% → 0 pts'

        traza['variables']['dividendYield'] = {
            'original': dato_original_dy, 'convertido': dividend_yield_raw, 'pct': dividend_yield_pct,
            'score': score_dividendos, 'regla': regla_div, 'es_none_original': dato_original_dy is None,
        }
        traza['variables']['payoutRatio'] = {
            'original': dato_original_pr, 'convertido': payout_ratio_raw, 'pct': payout_ratio_pct,
            'score': '(compartido con dividendYield)', 'regla': regla_div, 'es_none_original': dato_original_pr is None,
        }

        traza['componentes']['dividendos'] = {
            'score': score_dividendos, 'max': 10,
            'desglose': f'Yield+Payout → {score_dividendos}',
        }

        # ── VENTAJA COMPETITIVA (Máx 20 pts) ──

        market_cap = pd.to_numeric(info.get('marketCap', 0), errors='coerce') or 0.0
        dato_original_mc = info.get('marketCap')

        if market_cap > 10_000_000_000:
            score_cap = 10; regla_cap = f'{market_cap/1e9:.1f}B > 10B → 10 pts'
        elif market_cap > 2_000_000_000:
            score_cap = 5; regla_cap = f'{market_cap/1e9:.1f}B > 2B → 5 pts'
        else:
            score_cap = 0; regla_cap = f'{market_cap/1e9:.2f}B ≤ 2B → 0 pts'

        if market_cap > 5_000_000_000:
            score_historial = 5; regla_hist = f'{market_cap/1e9:.1f}B > 5B → 5 pts'
        else:
            score_historial = 0; regla_hist = f'{market_cap/1e9:.2f}B ≤ 5B → 0 pts'

        ventaja_competitiva = score_cap + score_historial

        traza['variables']['marketCap'] = {
            'original': dato_original_mc, 'convertido': market_cap,
            'score': score_cap + score_historial,
            'regla': f'Cap: {regla_cap} | Hist: {regla_hist}',
            'es_none_original': dato_original_mc is None,
        }

        traza['componentes']['ventaja_competitiva'] = {
            'score': ventaja_competitiva, 'max': 20,
            'desglose': f'Cap({score_cap}) + Historial({score_historial})',
        }

        # ── TOTAL ──
        score_total = calidad_negocio + fortaleza_financiera + score_dividendos + ventaja_competitiva
        traza['score_total'] = round(min(100.0, max(0.0, score_total)), 1)

    except Exception as e:
        traza['errores'].append(str(e))
        traza['score_total'] = 0.0

    return traza


def _simular_oportunidad_con_traza(info: dict, df_historico: Optional[pd.DataFrame], variacion_diaria: float) -> Dict[str, Any]:
    """
    Replica EXACTAMENTE la lógica de _calcular_oportunidad_inversion() de views/ranking.py
    pero registrando cada paso intermedio. NO modifica ninguna fórmula.
    """
    traza = {
        'variables': {},
        'componentes': {},
        'score_total': 0.0,
        'errores': [],
    }

    try:
        # ── VALORACIÓN (Máx 40 pts) ──
        per = pd.to_numeric(info.get('trailingPE', 0), errors='coerce') or 0.0
        dato_per = info.get('trailingPE')

        if per > 0 and per < 12:
            score_per = 20; regla_per = f'PER={per:.1f} ∈ (0,12) → 20 pts'
        elif per >= 12 and per <= 18:
            score_per = 15; regla_per = f'PER={per:.1f} ∈ [12,18] → 15 pts'
        elif per > 18:
            score_per = 5; regla_per = f'PER={per:.1f} > 18 → 5 pts'
        else:
            score_per = 10; regla_per = f'PER={per:.1f} ≤ 0 → 10 pts (neutro)'

        traza['variables']['trailingPE'] = {
            'original': dato_per, 'convertido': per,
            'score': score_per, 'regla': regla_per, 'es_none_original': dato_per is None,
        }

        p_b = pd.to_numeric(info.get('priceToBook', 0), errors='coerce') or 0.0
        dato_pb = info.get('priceToBook')

        if p_b > 0 and p_b < 1.2:
            score_pb = 10; regla_pb = f'P/B={p_b:.2f} ∈ (0,1.2) → 10 pts'
        elif p_b >= 1.2 and p_b <= 2.5:
            score_pb = 5; regla_pb = f'P/B={p_b:.2f} ∈ [1.2,2.5] → 5 pts'
        else:
            score_pb = 0; regla_pb = f'P/B={p_b:.2f} fuera de rango → 0 pts'

        traza['variables']['priceToBook'] = {
            'original': dato_pb, 'convertido': p_b,
            'score': score_pb, 'regla': regla_pb, 'es_none_original': dato_pb is None,
        }

        ev_ebitda = pd.to_numeric(info.get('enterpriseToEbitda', 0), errors='coerce') or 0.0
        dato_ev = info.get('enterpriseToEbitda')

        if ev_ebitda > 0 and ev_ebitda < 10:
            score_ebitda = 10; regla_ev = f'EV/EBITDA={ev_ebitda:.1f} ∈ (0,10) → 10 pts'
        elif ev_ebitda >= 10 and ev_ebitda <= 15:
            score_ebitda = 7; regla_ev = f'EV/EBITDA={ev_ebitda:.1f} ∈ [10,15] → 7 pts'
        elif ev_ebitda > 15:
            score_ebitda = 3; regla_ev = f'EV/EBITDA={ev_ebitda:.1f} > 15 → 3 pts'
        else:
            score_ebitda = 5; regla_ev = f'EV/EBITDA={ev_ebitda:.1f} ≤ 0 → 5 pts (neutro)'

        traza['variables']['enterpriseToEbitda'] = {
            'original': dato_ev, 'convertido': ev_ebitda,
            'score': score_ebitda, 'regla': regla_ev, 'es_none_original': dato_ev is None,
        }

        valoracion = score_per + score_pb + score_ebitda
        traza['componentes']['valoracion'] = {
            'score': valoracion, 'max': 40,
            'desglose': f'PER({score_per}) + P/B({score_pb}) + EV/EBITDA({score_ebitda})',
        }

        # ── MOMENTUM TÉCNICO (Máx 25 pts) ──
        momentum_tecnico = 0.0
        detalles_momentum = []

        if df_historico is not None and not df_historico.empty:
            try:
                if isinstance(df_historico.columns, pd.MultiIndex):
                    precios = df_historico['Close'].dropna()
                else:
                    precios = df_historico['Close'].dropna() if 'Close' in df_historico.columns else pd.Series()

                n_barras = len(precios)
                detalles_momentum.append(f'Barras disponibles: {n_barras}')

                if n_barras >= 20:
                    sma_20 = precios.rolling(window=20).mean().iloc[-1]
                    precio_actual = precios.iloc[-1]
                    detalles_momentum.append(f'SMA20={sma_20:.2f}, Precio={precio_actual:.2f}')

                    if precio_actual > sma_20:
                        momentum_tecnico += 15
                        detalles_momentum.append('Precio > SMA20 → +15 pts')
                    else:
                        detalles_momentum.append('Precio ≤ SMA20 → +0 pts')

                    if n_barras >= 14:
                        delta = precios.diff()
                        gain = delta.where(delta > 0, 0).rolling(window=14).mean().iloc[-1]
                        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean().iloc[-1]

                        if loss != 0:
                            rs = gain / loss
                            rsi = 100 - (100 / (1 + rs))
                        else:
                            rsi = 100.0

                        detalles_momentum.append(f'RSI={rsi:.1f}')

                        if 30 <= rsi <= 70:
                            momentum_tecnico += 10
                            detalles_momentum.append('RSI ∈ [30,70] → +10 pts')
                        elif rsi < 30:
                            momentum_tecnico += 5
                            detalles_momentum.append('RSI < 30 → +5 pts')
                        else:
                            detalles_momentum.append('RSI > 70 → +0 pts')

                if variacion_diaria > 0:
                    bonus = min(5, variacion_diaria * 2)
                    momentum_tecnico += bonus
                    detalles_momentum.append(f'Variación={variacion_diaria:.2f}% → +{bonus:.1f} pts')

            except Exception as e:
                detalles_momentum.append(f'Error técnico: {e}')
        else:
            detalles_momentum.append('Sin datos históricos')

        if momentum_tecnico == 0:
            momentum_tecnico = 12.5
            detalles_momentum.append('FALLBACK: momentum=0 → 12.5 pts neutro')

        traza['componentes']['momentum_tecnico'] = {
            'score': round(momentum_tecnico, 1), 'max': 25,
            'desglose': ' | '.join(detalles_momentum),
        }

        # ── MOMENTUM FUNDAMENTAL (Máx 35 pts) ──
        momentum_fundamental = 30.0
        detalles_fund = ['Base = 30.0 pts']

        rev_growth = pd.to_numeric(info.get('revenueGrowth', 0), errors='coerce') or 0.0
        rev_growth_pct = rev_growth * 100 if abs(rev_growth) < 1 else rev_growth

        if rev_growth_pct > 15:
            momentum_fundamental = 35
            detalles_fund.append(f'RevGrowth={rev_growth_pct:.1f}% > 15% → 35 pts')
        elif rev_growth_pct > 5:
            momentum_fundamental = 32
            detalles_fund.append(f'RevGrowth={rev_growth_pct:.1f}% > 5% → 32 pts')
        elif rev_growth_pct < 0:
            momentum_fundamental = 25
            detalles_fund.append(f'RevGrowth={rev_growth_pct:.1f}% < 0% → 25 pts')
        else:
            detalles_fund.append(f'RevGrowth={rev_growth_pct:.1f}% ∈ [0,5]% → 30 pts (sin cambio)')

        traza['componentes']['momentum_fundamental'] = {
            'score': round(momentum_fundamental, 1), 'max': 35,
            'desglose': ' | '.join(detalles_fund),
        }

        # ── TOTAL ──
        score_total = valoracion + momentum_tecnico + momentum_fundamental
        traza['score_total'] = round(min(100.0, max(0.0, score_total)), 1)

    except Exception as e:
        traza['errores'].append(str(e))
        traza['score_total'] = 50.0

    return traza


# ═══════════════════════════════════════════════════════════════════
# 📊 AUDITORÍA COMPLETA DE UN TICKER
# ═══════════════════════════════════════════════════════════════════

def auditoria_pipeline_completa(ticker: str) -> Dict[str, Any]:
    """
    Realiza la trazabilidad completa del pipeline para un ticker.
    Retorna un diccionario con TODA la información del recorrido.
    """
    ticker = ticker.strip().upper()
    resultado = {
        'ticker': ticker,
        'fuente': {},
        'datos_raw_por_fuente': {},
        'datos_normalizados': {},
        'datos_recibidos_por_motor': {},
        'traza_calidad': {},
        'traza_oportunidad': {},
        'score_final': {},
        'perdida_datos': [],
        'variables_no_usadas': [],
    }

    # ── 1. Determinar fuente utilizada ──
    fuentes_intentadas = []

    yahoo_data = _obtener_yahoo(ticker)
    yahoo_ok = bool(yahoo_data and _tiene_datos_utiles(yahoo_data))
    fuentes_intentadas.append(('Yahoo', yahoo_ok, 'Respondió correctamente' if yahoo_ok else 'Sin datos útiles o error'))

    fmp_data = {}
    fmp_ok = False
    try:
        from config import FMP_API_KEY
        if FMP_API_KEY:
            fmp_data = _obtener_fmp(ticker)
            fmp_ok = bool(fmp_data and _tiene_datos_utiles(fmp_data))
            fuentes_intentadas.append(('FMP', fmp_ok, 'Respondió correctamente' if fmp_ok else 'Sin datos útiles o error'))
        else:
            fuentes_intentadas.append(('FMP', False, 'API key no configurada'))
    except ImportError:
        fuentes_intentadas.append(('FMP', False, 'API key no configurada'))

    finnhub_data = {}
    finnhub_ok = False
    try:
        from config import FINNHUB_API_KEY
        if FINNHUB_API_KEY:
            finnhub_data = _obtener_finnhub(ticker)
            finnhub_ok = bool(finnhub_data and _tiene_datos_utiles(finnhub_data))
            fuentes_intentadas.append(('Finnhub', finnhub_ok, 'Respondió correctamente' if finnhub_ok else 'Sin datos útiles o error'))
        else:
            fuentes_intentadas.append(('Finnhub', False, 'API key no configurada'))
    except ImportError:
        fuentes_intentadas.append(('Finnhub', False, 'API key no configurada'))

    # Determinar fuente ganadora
    if yahoo_ok:
        fuente_ganadora = 'yahoo'
        datos_finales = yahoo_data
    elif fmp_ok:
        fuente_ganadora = 'fmp'
        datos_finales = fmp_data
    elif finnhub_ok:
        fuente_ganadora = 'finnhub'
        datos_finales = finnhub_data
    else:
        fuente_ganadora = 'fallback'
        datos_finales = _diccionario_vacio()

    resultado['fuente'] = {
        'ganadora': fuente_ganadora,
        'intentos': fuentes_intentadas,
    }

    # ── 2. Datos normalizados (lo que sale de data_provider) ──
    resultado['datos_normalizados'] = {k: v for k, v in datos_finales.items() if not k.startswith('_')}

    # ── 3. Datos recibidos por el motor (exactamente lo que llega) ──
    resultado['datos_recibidos_por_motor'] = dict(datos_finales)

    # ── 4. Simular scoring con traza completa ──
    resultado['traza_calidad'] = _simular_calidad_con_traza(datos_finales)

    # Obtener histórico para oportunidad
    from services.data_provider import get_price_batch
    try:
        df_batch = get_price_batch((ticker,))
        if df_batch is not None and not df_batch.empty:
            if isinstance(df_batch.columns, pd.MultiIndex):
                if ticker in df_batch.columns.get_level_values(0):
                    df_ticker = df_batch[ticker]
                    if isinstance(df_ticker.columns, pd.MultiIndex):
                        df_ticker.columns = df_ticker.columns.get_level_values(0)
                else:
                    df_ticker = pd.DataFrame()
            else:
                df_ticker = df_batch
        else:
            df_ticker = pd.DataFrame()
    except Exception:
        df_ticker = pd.DataFrame()

    # Calcular variación diaria
    variacion = 0.0
    try:
        cp = datos_finales.get('currentPrice')
        pc = datos_finales.get('previousClose')
        if cp and pc and float(cp) > 0 and float(pc) > 0:
            variacion = ((float(cp) - float(pc)) / float(pc)) * 100.0
    except Exception:
        pass

    resultado['traza_oportunidad'] = _simular_oportunidad_con_traza(datos_finales, df_ticker, variacion)

    # ── 5. Score final ──
    calidad = resultado['traza_calidad']['score_total']
    oportunidad = resultado['traza_oportunidad']['score_total']
    score_final = round((calidad * 0.60) + (oportunidad * 0.40), 1)

    resultado['score_final'] = {
        'calidad': calidad,
        'oportunidad': oportunidad,
        'score_stocklab': score_final,
        'formula': f'({calidad} × 0.60) + ({oportunidad} × 0.40) = {score_final}',
    }

    # ── 6. Detección de pérdida de datos ──
    for clave in VARIABLES_CALIDAD:
        valor = datos_finales.get(clave)
        if valor is None:
            resultado['perdida_datos'].append({
                'variable': clave,
                'estado': 'None en datos finales',
                'impacto': f'Puntúa como 0 en {VARIABLES_CALIDAD[clave]["componente"]}',
            })

    for clave in VARIABLES_OPORTUNIDAD:
        if clave not in VARIABLES_CALIDAD:  # Evitar duplicados
            valor = datos_finales.get(clave)
            if valor is None:
                resultado['perdida_datos'].append({
                    'variable': clave,
                    'estado': 'None en datos finales',
                    'impacto': f'Puntúa como neutro/0 en {VARIABLES_OPORTUNIDAD[clave]["componente"]}',
                })

    # ── 7. Variables no utilizadas ──
    for clave in sorted(VARIABLES_NO_UTILIZADAS):
        valor = datos_finales.get(clave)
        resultado['variables_no_usadas'].append({
            'variable': clave,
            'valor': valor,
            'descargada': valor is not None,
        })

    return resultado


# ═══════════════════════════════════════════════════════════════════
# 📝 FORMATEADOR DE INFORME LEGIBLE
# ═══════════════════════════════════════════════════════════════════

def formatear_informe_auditoria(resultado: Dict[str, Any]) -> str:
    """Convierte el resultado de auditoria_pipeline_completa() en texto legible."""
    lines = []
    ticker = resultado['ticker']

    lines.append(f"{'='*70}")
    lines.append(f"  AUDITORÍA DE TRAZABILIDAD: {ticker}")
    lines.append(f"{'='*70}")

    # 1. Fuente
    lines.append(f"\n📡 1. FUENTE UTILIZADA")
    lines.append(f"   Fuente seleccionada: {resultado['fuente']['ganadora'].upper()}")
    for nombre, ok, motivo in resultado['fuente']['intentos']:
        icon = '✅' if ok else '❌'
        lines.append(f"   {icon} {nombre}: {motivo}")

    # 2. Datos normalizados
    lines.append(f"\n📦 2. DATOS NORMALIZADOS (salida de data_provider)")
    datos = resultado['datos_normalizados']
    for clave in sorted(datos.keys()):
        valor = datos[clave]
        if valor is not None:
            lines.append(f"   ✅ {clave}: {valor}")
        else:
            lines.append(f"   ❌ {clave}: None")

    # 3. Traza Calidad
    lines.append(f"\n🏆 3. TRAZABILIDAD CALIDAD EMPRESARIAL")
    traza_c = resultado['traza_calidad']

    for var_name, var_data in traza_c.get('variables', {}).items():
        original = var_data.get('original')
        es_none = var_data.get('es_none_original', False)
        pct = var_data.get('pct', var_data.get('ratio_final', var_data.get('convertido')))
        score = var_data.get('score')
        regla = var_data.get('regla')

        estado = '⚠️ NONE' if es_none else '✅'
        lines.append(f"   {estado} {var_name}")
        lines.append(f"      Original: {original} → Convertido: {pct}")
        lines.append(f"      Regla: {regla}")
        lines.append(f"      Score: {score} pts")
        lines.append("")

    lines.append(f"   📊 COMPONENTES:")
    for comp_name, comp_data in traza_c.get('componentes', {}).items():
        lines.append(f"      {comp_name}: {comp_data['desglose']} = {comp_data['score']} / {comp_data['max']}")

    lines.append(f"\n   🎯 SCORE CALIDAD TOTAL: {traza_c['score_total']} / 100")

    # 4. Traza Oportunidad
    lines.append(f"\n🎯 4. TRAZABILIDAD OPORTUNIDAD DE INVERSIÓN")
    traza_o = resultado['traza_oportunidad']

    for var_name, var_data in traza_o.get('variables', {}).items():
        original = var_data.get('original')
        es_none = var_data.get('es_none_original', False)
        convertido = var_data.get('convertido')
        score = var_data.get('score')
        regla = var_data.get('regla')

        estado = '⚠️ NONE' if es_none else '✅'
        lines.append(f"   {estado} {var_name}")
        lines.append(f"      Original: {original} → Convertido: {convertido}")
        lines.append(f"      Regla: {regla}")
        lines.append(f"      Score: {score} pts")
        lines.append("")

    lines.append(f"   📊 COMPONENTES:")
    for comp_name, comp_data in traza_o.get('componentes', {}).items():
        lines.append(f"      {comp_name}: {comp_data['score']} / {comp_data['max']}")
        lines.append(f"         {comp_data['desglose']}")

    lines.append(f"\n   🎯 SCORE OPORTUNIDAD TOTAL: {traza_o['score_total']} / 100")

    # 5. Score Final
    lines.append(f"\n⭐ 5. SCORE FINAL STOCKLAB")
    sf = resultado['score_final']
    lines.append(f"   Fórmula: {sf['formula']}")
    lines.append(f"   Calidad:     {sf['calidad']} × 0.60 = {sf['calidad']*0.60:.1f}")
    lines.append(f"   Oportunidad: {sf['oportunidad']} × 0.40 = {sf['oportunidad']*0.40:.1f}")
    lines.append(f"   SCORE FINAL: {sf['score_stocklab']} / 100")

    # 6. Pérdida de datos
    lines.append(f"\n🔍 6. AUDITORÍA DE PÉRDIDA DE DATOS")
    if resultado['perdida_datos']:
        for item in resultado['perdida_datos']:
            lines.append(f"   ⚠️ {item['variable']}: {item['estado']} → {item['impacto']}")
    else:
        lines.append(f"   ✅ No se detectó pérdida de datos. Todas las variables tienen valor.")

    # 7. Variables no utilizadas
    lines.append(f"\n📋 7. VARIABLES DESCARGADAS PERO NO UTILIZADAS POR EL MOTOR")
    no_usadas_con_valor = [v for v in resultado['variables_no_usadas'] if v['descargada']]
    no_usadas_sin_valor = [v for v in resultado['variables_no_usadas'] if not v['descargada']]

    if no_usadas_con_valor:
        lines.append(f"   Descargadas con valor (pero ignoradas por el scoring):")
        for v in no_usadas_con_valor:
            lines.append(f"      {v['variable']}: {v['valor']}")
    if no_usadas_sin_valor:
        lines.append(f"   Descargadas como None:")
        for v in no_usadas_sin_valor:
            lines.append(f"      {v['variable']}: None")

    lines.append(f"\n{'='*70}\n")
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════
# 📊 CERTIFICACIÓN DE NORMALIZACIÓN ENTRE FUENTES
# ═══════════════════════════════════════════════════════════════════

def certificacion_normalizacion(ticker: str) -> Dict[str, Any]:
    """
    Compara los valores normalizados de las 3 fuentes para un mismo ticker.
    Verifica que todas devuelven la misma escala tras normalización.
    """
    resultado = {'ticker': ticker, 'comparativa': {}}

    yahoo = _obtener_yahoo(ticker)
    fmp = _obtener_fmp(ticker) if True else {}
    finnhub = _obtener_finnhub(ticker) if True else {}

    claves_a_comparar = list(VARIABLES_CALIDAD.keys()) + list(set(VARIABLES_OPORTUNIDAD.keys()) - set(VARIABLES_CALIDAD.keys()))

    for clave in sorted(claves_a_comparar):
        v_yahoo = yahoo.get(clave) if yahoo else None
        v_fmp = fmp.get(clave) if fmp else None
        v_finnhub = finnhub.get(clave) if finnhub else None

        # Verificar consistencia de escala
        valores = [v for v in [v_yahoo, v_fmp, v_finnhub] if v is not None]
        consistente = True
        nota = ''

        if len(valores) >= 2:
            # Para porcentajes normalizados a decimal, todos deberían ser < 1 (excepto ratios puros)
            claves_decimal = {'returnOnEquity', 'returnOnAssets', 'operatingMargins', 'profitMargins',
                            'revenueGrowth', 'earningsGrowth', 'dividendYield', 'payoutRatio', 'debtToEquity'}
            if clave in claves_decimal:
                for v in valores:
                    if abs(v) > 2:  # Si un porcentaje normalizado es > 2, algo falla
                        consistente = False
                        nota = f'⚠️ Valor {v} parece no estar normalizado a decimal'

        resultado['comparativa'][clave] = {
            'yahoo': v_yahoo,
            'fmp': v_fmp,
            'finnhub': v_finnhub,
            'consistente': consistente,
            'nota': nota,
        }

    return resultado


def formatear_certificacion(cert: Dict[str, Any]) -> str:
    """Formatea la certificación de normalización como tabla legible."""
    lines = []
    lines.append(f"{'='*80}")
    lines.append(f"  CERTIFICACIÓN DE NORMALIZACIÓN: {cert['ticker']}")
    lines.append(f"{'='*80}")
    lines.append(f"{'Variable':<25} {'Yahoo':>12} {'FMP':>12} {'Finnhub':>12} {'Consistente':>12}")
    lines.append(f"{'-'*25} {'-'*12} {'-'*12} {'-'*12} {'-'*12}")

    for clave, datos in cert['comparativa'].items():
        y = f"{datos['yahoo']:.4f}" if datos['yahoo'] is not None else 'None'
        f = f"{datos['fmp']:.4f}" if datos['fmp'] is not None else 'None'
        h = f"{datos['finnhub']:.4f}" if datos['finnhub'] is not None else 'None'
        c = '✅' if datos['consistente'] else '❌'
        lines.append(f"{clave:<25} {y:>12} {f:>12} {h:>12} {c:>12}")
        if datos['nota']:
            lines.append(f"  → {datos['nota']}")

    lines.append(f"{'='*80}")
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════
# 🗺️ MAPA DE VARIABLES UTILIZADAS
# ═══════════════════════════════════════════════════════════════════

def mapa_variables_utilizadas() -> str:
    """Genera un mapa completo de qué variables se usan dónde."""
    lines = []
    lines.append("=" * 70)
    lines.append("  MAPA DE VARIABLES: USO REAL EN EL PIPELINE")
    lines.append("=" * 70)

    lines.append("\n📥 VARIABLES DESCARGADAS POR data_provider.py:")
    for clave in sorted(VARIABLES_DESCARGADAS):
        en_calidad = clave in VARIABLES_CALIDAD
        en_oport = clave in VARIABLES_OPORTUNIDAD
        uso = []
        if en_calidad:
            uso.append(f"Calidad/{VARIABLES_CALIDAD[clave]['componente']}")
        if en_oport:
            uso.append(f"Oportunidad/{VARIABLES_OPORTUNIDAD[clave]['componente']}")
        if not uso:
            uso_str = "❌ NO UTILIZADA por ningún motor"
        else:
            uso_str = "✅ " + " + ".join(uso)
        lines.append(f"   {clave:<25} → {uso_str}")

    lines.append(f"\n📊 RESUMEN:")
    lines.append(f"   Total descargadas:    {len(VARIABLES_DESCARGADAS)}")
    lines.append(f"   Usadas en Calidad:    {len(VARIABLES_CALIDAD)}")
    lines.append(f"   Usadas en Oportunidad:{len(VARIABLES_OPORTUNIDAD)}")
    lines.append(f"   No utilizadas:        {len(VARIABLES_NO_UTILIZADAS)}")

    lines.append(f"\n❌ VARIABLES DESCARGADAS PERO NUNCA UTILIZADAS:")
    for clave in sorted(VARIABLES_NO_UTILIZADAS):
        lines.append(f"   • {clave}")

    lines.append(f"\n⚠️ VARIABLES REFERENCIADAS EN ranking_engine.py (MOTOR B - CÓDIGO MUERTO):")
    lines.append(f"   • epsGrowth (NO existe en _CLAVES_ESTANDAR)")
    lines.append(f"   • returnOnCapitalEmployed (NO existe en _CLAVES_ESTANDAR)")
    lines.append(f"   • ROE, ROA, ROIC (claves legacy, NO camelCase)")
    lines.append(f"   • PER, price_to_book, peg_ratio (claves legacy, NO camelCase)")

    lines.append("\n" + "=" * 70)
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════
# 🏁 EJECUCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    TICKERS_AUDITORIA = [
        'SAN.MC', 'BBVA.MC', 'ITX.MC', 'REP.MC', 'TEF.MC',
        'ENCE.MC', 'DIA.MC', 'ORY.MC', 'GRF-P.MC', 'MDF.MC',
    ]

    print("\n" + "#" * 70)
    print("  FASE 1.3 -- AUDITORIA DE TRAZABILIDAD DEL PIPELINE")
    print("#" * 70)

    # Mapa de variables
    print("\n" + mapa_variables_utilizadas())

    # Auditoría individual por ticker
    for ticker in TICKERS_AUDITORIA:
        print(f"\n⏳ Auditando {ticker}...")
        try:
            resultado = auditoria_pipeline_completa(ticker)
            print(formatear_informe_auditoria(resultado))
        except Exception as e:
            print(f"❌ Error auditando {ticker}: {e}")

    # Certificación de normalización (para un ticker representativo)
    print("\n⏳ Generando certificación de normalización para SAN.MC...")
    try:
        cert = certificacion_normalizacion('SAN.MC')
        print(formatear_certificacion(cert))
    except Exception as e:
        print(f"❌ Error en certificación: {e}")

    print("\n✅ Auditoría completada.")
