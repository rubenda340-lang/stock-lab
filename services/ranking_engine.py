"""
Motor de Ranking Cuantitativo - StockLab V6
============================================
Centraliza la lógica matemática exacta de los dos macro-scores independientes
y el score final ponderado para inversión a largo plazo.
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional


def calcular_score_calidad(datos_fundamentales: Dict[str, float]) -> float:
    """
    Calcula el Score de Calidad Empresarial (Máx 100 puntos).
    
    NOTA: La nota global devuelta es estrictamente la suma matemática directa
    de los componentes del desglose (_desglosar_calidad_detalle) para garantizar
    correlación del 100% entre la barra superior y el acordeón abierto.
    
    Componentes:
    - Rentabilidad (20 pts): ROE, ROA, ROIC
    - Crecimiento (20 pts): EPS Growth, Revenue Growth
    - Balance (20 pts): Debt/Equity, Current Ratio
    - Dividendos (15 pts): Yield, Payout Ratio
    - Valoración (15 pts): PER, P/B, PEG
    - Consistencia (10 pts): Base por estabilidad
    
    Parámetros:
    -----------
    datos_fundamentales : dict
        Diccionario con ratios fundamentales (todos como floats)
    
    Retorna:
    --------
    float entre 0 y 100 (suma exacta de componentes del desglose)
    """
    try:
        # Invocar internamente al desglose y devolver la suma matemática directa
        desglose = _desglosar_calidad_detalle(datos_fundamentales)
        score = (
            desglose.get('rentabilidad', 0.0) +
            desglose.get('crecimiento', 0.0) +
            desglose.get('fortaleza_financiera', 0.0) +
            desglose.get('dividendos', 0.0) +
            desglose.get('valoracion', 0.0) +
            desglose.get('consistencia', 0.0)
        )
        return round(min(100.0, max(0.0, score)), 1)
    except Exception as e:
        print(f"⚠️ Error calculando score calidad: {e}")
        return 0.0


def _calcular_score_calidad_legacy(datos_fundamentales: Dict[str, float]) -> float:
    """
    [DEPRECATED] Implementación legacy mantenida solo como referencia.
    Usar calcular_score_calidad() que ahora invoca internamente el desglose.
    """
    score = 0.0
    
    try:
        # ── RENTABILIDAD (Máx 20 pts) ──
        roe = datos_fundamentales.get('ROE', 0.0)
        roa = datos_fundamentales.get('ROA', 0.0)
        roic = datos_fundamentales.get('ROIC', 0.0)
        
        # Convertir decimales a porcentajes si es necesario
        if abs(roe) < 1:
            roe *= 100
        if abs(roa) < 1:
            roa *= 100
        if abs(roic) < 1:
            roic *= 100
        
        # ROE scoring (0-8 pts)
        if roe > 20:
            score_roe = 8
        elif roe > 15:
            score_roe = 6
        elif roe > 10:
            score_roe = 4
        elif roe > 5:
            score_roe = 2
        else:
            score_roe = 0
        
        # ROA scoring (0-6 pts)
        if roa > 10:
            score_roa = 6
        elif roa > 7:
            score_roa = 4
        elif roa > 5:
            score_roa = 3
        elif roa > 2:
            score_roa = 1
        else:
            score_roa = 0
        
        # ROIC scoring (0-6 pts)
        if roic > 15:
            score_roic = 6
        elif roic > 10:
            score_roic = 4
        elif roic > 7:
            score_roic = 3
        elif roic > 3:
            score_roic = 1
        else:
            score_roic = 0
        
        rentabilidad = min(20, score_roe + score_roa + score_roic)
        
        # ── CRECIMIENTO (Máx 20 pts) ──
        eps_growth = datos_fundamentales.get('eps_growth', 0.0)
        revenue_growth = datos_fundamentales.get('revenue_growth', 0.0)
        
        if abs(eps_growth) < 1:
            eps_growth *= 100
        if abs(revenue_growth) < 1:
            revenue_growth *= 100
        
        # EPS Growth scoring (0-10 pts)
        if eps_growth > 20:
            score_eps = 10
        elif eps_growth > 10:
            score_eps = 7
        elif eps_growth > 5:
            score_eps = 5
        elif eps_growth > 0:
            score_eps = 2
        else:
            score_eps = 0
        
        # Revenue Growth scoring (0-10 pts)
        if revenue_growth > 15:
            score_rev = 10
        elif revenue_growth > 10:
            score_rev = 7
        elif revenue_growth > 5:
            score_rev = 5
        elif revenue_growth > 0:
            score_rev = 2
        else:
            score_rev = 0
        
        crecimiento = min(20, score_eps + score_rev)
        
        # ── BALANCE (Máx 20 pts) ──
        debt_to_equity = datos_fundamentales.get('debt_to_equity', 0.0)
        current_ratio = datos_fundamentales.get('current_ratio', 0.0)
        
        # Debt/Equity scoring (0-10 pts) - menor es mejor
        if debt_to_equity == 0:
            score_deuda = 10  # Sin deuda = perfecto
        elif debt_to_equity < 0.5:
            score_deuda = 10
        elif debt_to_equity < 1.0:
            score_deuda = 7
        elif debt_to_equity < 2.0:
            score_deuda = 4
        else:
            score_deuda = 0
        
        # Current Ratio scoring (0-10 pts)
        if current_ratio > 2.0:
            score_current = 10
        elif current_ratio > 1.5:
            score_current = 7
        elif current_ratio > 1.0:
            score_current = 5
        elif current_ratio > 0.8:
            score_current = 2
        else:
            score_current = 0
        
        balance = min(20, score_deuda + score_current)
        
        # ── DIVIDENDOS (Máx 15 pts) ──
        dividend_yield = datos_fundamentales.get('dividend_yield', 0.0)
        payout_ratio = datos_fundamentales.get('payout_ratio', 0.0)
        
        if abs(dividend_yield) < 1:
            dividend_yield *= 100
        if abs(payout_ratio) < 1:
            payout_ratio *= 100
        
        # Dividend Yield scoring (0-8 pts)
        if dividend_yield > 5:
            score_yield = 8
        elif dividend_yield > 3:
            score_yield = 6
        elif dividend_yield > 1:
            score_yield = 4
        elif dividend_yield > 0:
            score_yield = 2
        else:
            score_yield = 0
        
        # Payout Ratio scoring (0-7 pts) - menor es más sostenible
        if payout_ratio == 0 and dividend_yield == 0:
            score_payout = 0  # No paga dividendos
        elif payout_ratio < 50:
            score_payout = 7
        elif payout_ratio < 70:
            score_payout = 5
        elif payout_ratio < 90:
            score_payout = 3
        else:
            score_payout = 0
        
        dividendos = min(15, score_yield + score_payout)
        
        # ── VALORACIÓN (Máx 15 pts) ──
        per = datos_fundamentales.get('PER', 0.0)
        price_to_book = datos_fundamentales.get('price_to_book', 0.0)
        peg = datos_fundamentales.get('peg_ratio', 0.0)
        
        # PER scoring (0-6 pts) - menor es mejor
        if per > 0 and per < 10:
            score_per = 6
        elif per >= 10 and per < 15:
            score_per = 4
        elif per >= 15 and per < 20:
            score_per = 2
        else:
            score_per = 0
        
        # P/B scoring (0-5 pts)
        if price_to_book > 0 and price_to_book < 1.5:
            score_pb = 5
        elif price_to_book >= 1.5 and price_to_book < 3:
            score_pb = 3
        elif price_to_book >= 3 and price_to_book < 5:
            score_pb = 1
        else:
            score_pb = 0
        
        # PEG scoring (0-4 pts)
        if peg > 0 and peg < 1:
            score_peg = 4
        elif peg >= 1 and peg < 2:
            score_peg = 2
        else:
            score_peg = 0
        
        valoracion = min(15, score_per + score_pb + score_peg)
        
        # ── CONSISTENCIA (Máx 10 pts) ──
        # Puntuación base por tener datos completos y positivos
        datos_validos = sum(1 for v in [roe, roa, roic, eps_growth, revenue_growth] if v > 0)
        
        if datos_validos >= 5:
            consistencia = 10
        elif datos_validos >= 3:
            consistencia = 7
        elif datos_validos >= 1:
            consistencia = 4
        else:
            consistencia = 0
        
        # ── SCORE TOTAL CALIDAD ──
        score = rentabilidad + crecimiento + balance + dividendos + valoracion + consistencia
        
    except Exception as e:
        print(f"⚠️ Error calculando score calidad: {e}")
        score = 0.0
    
    return round(min(100.0, max(0.0, score)), 1)


def calcular_score_oportunidad(df_historico: pd.DataFrame, datos_sentimiento: Dict) -> float:
    """
    Calcula el Score de Oportunidad de Inversión (Máx 100 puntos).
    
    NOTA: La nota global devuelta es estrictamente la suma matemática directa
    de los componentes del desglose (_desglosar_oportunidad_detalle) para garantizar
    correlación del 100% entre la barra superior y el acordeón abierto.
    
    Componentes:
    - Tendencia y Momentum (45 pts): SMA crossovers, RSI, MACD
    - Infravaloración (20 pts): PER vs media histórica/sectorial
    - Sentimiento, Volumen y Riesgo (35 pts): Analistas, volumen, volatilidad
    
    Parámetros:
    -----------
    df_historico : pd.DataFrame
        DataFrame con precios históricos (columnas OHLCV)
    datos_sentimiento : dict
        Diccionario con métricas de sentimiento de analistas
    
    Retorna:
    --------
    float entre 0 y 100 (suma exacta de componentes del desglose)
    """
    try:
        # Invocar internamente al desglose y devolver la suma matemática directa
        # Extraer variación diaria del histórico si está disponible
        variacion_diaria = 0.0
        if df_historico is not None and not df_historico.empty:
            try:
                if isinstance(df_historico.columns, pd.MultiIndex):
                    precios = df_historico['Close'].dropna()
                else:
                    precios = df_historico['Close'].dropna() if 'Close' in df_historico.columns else pd.Series()
                
                if len(precios) >= 2:
                    precio_actual = precios.iloc[-1]
                    precio_anterior = precios.iloc[-2]
                    variacion_diaria = ((precio_actual - precio_anterior) / precio_anterior) * 100
            except Exception:
                pass
        
        desglose = _desglosar_oportunidad_detalle(df_historico, datos_sentimiento, variacion_diaria)
        score = (
            desglose.get('momentum_tecnico', 0.0) +
            desglose.get('valoracion', 0.0) +
            desglose.get('sentimiento', 0.0) +
            desglose.get('volumen_riesgo', 0.0)
        )
        return round(min(100.0, max(0.0, score)), 1)
    except Exception as e:
        print(f"⚠️ Error calculando score oportunidad: {e}")
        return 50.0  # Valor neutro por defecto


def _calcular_score_oportunidad_legacy(df_historico: pd.DataFrame, datos_sentimiento: Dict) -> float:
    """
    [DEPRECATED] Implementación legacy mantenida solo como referencia.
    Usar calcular_score_oportunidad() que ahora invoca internamente el desglose.
    """
    score = 0.0
    
    try:
        # ── TENDENCIA Y MOMENTUM (Máx 45 pts) ──
        tendencia_momentum = 0.0
        
        if df_historico is not None and not df_historico.empty and 'Close' in df_historico.columns:
            precios = df_historico['Close'].dropna()
            
            if len(precios) >= 200:
                # Calcular SMAs
                sma_50 = precios.rolling(window=50).mean()
                sma_200 = precios.rolling(window=200).mean()
                
                precio_actual = precios.iloc[-1]
                sma_50_actual = sma_50.iloc[-1]
                sma_200_actual = sma_200.iloc[-1]
                
                # Golden Cross / Death Cross (0-15 pts)
                if precio_actual > sma_50_actual and sma_50_actual > sma_200_actual:
                    tendencia_momentum += 15  # Tendencia alcista fuerte
                elif precio_actual > sma_50_actual:
                    tendencia_momentum += 10  # Por encima de SMA50
                elif precio_actual > sma_200_actual:
                    tendencia_momentum += 5   # Por encima de SMA200
                
                # RSI (0-15 pts)
                if len(precios) >= 14:
                    delta = precios.diff()
                    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                    
                    loss_val = loss.iloc[-1]
                    gain_val = gain.iloc[-1]
                    if loss_val != 0:
                        rs = gain_val / loss_val
                        rsi_actual = 100 - (100 / (1 + rs))
                    else:
                        rsi_actual = 100.0
                    
                    if 40 <= rsi_actual <= 60:
                        tendencia_momentum += 15  # Zona neutral óptima
                    elif 30 <= rsi_actual < 40:
                        tendencia_momentum += 12  # Posible rebote
                    elif 60 < rsi_actual <= 70:
                        tendencia_momentum += 10  # Alcista pero no sobrecomprado
                    elif rsi_actual < 30:
                        tendencia_momentum += 8   # Sobrevendido (oportunidad)
                
                # MACD (0-15 pts)
                if len(precios) >= 26:
                    exp12 = precios.ewm(span=12, adjust=False).mean()
                    exp26 = precios.ewm(span=26, adjust=False).mean()
                    macd_line = exp12 - exp26
                    signal_line = macd_line.ewm(span=9, adjust=False).mean()
                    
                    macd_actual = macd_line.iloc[-1]
                    signal_actual = signal_line.iloc[-1]
                    
                    if macd_actual > signal_actual and macd_actual > 0:
                        tendencia_momentum += 15  # MACD alcista
                    elif macd_actual > signal_actual:
                        tendencia_momentum += 10  # Cruce alcista reciente
                    elif macd_actual < 0:
                        tendencia_momentum += 3   # MACD negativo
        
        # ── INFRAVALORACIÓN (Máx 20 pts) ──
        infravaloracion = 0.0
        
        # Aquí se compararía PER actual vs media histórica o sectorial
        # Por ahora usamos un placeholder basado en datos disponibles
        per_actual = datos_sentimiento.get('per_actual', 0)
        per_medio_sector = datos_sentimiento.get('per_medio_sector', 0)
        
        if per_actual > 0 and per_medio_sector > 0:
            ratio_val = per_actual / per_medio_sector
            
            if ratio_val < 0.7:
                infravaloracion = 20  # Muy infravalorado
            elif ratio_val < 0.85:
                infravaloracion = 15
            elif ratio_val < 1.0:
                infravaloracion = 10
            elif ratio_val < 1.15:
                infravaloracion = 5
        else:
            # Fallback: puntuación neutra si no hay datos de comparación
            infravaloracion = 10
        
        # ── SENTIMIENTO, VOLUMEN Y RIESGO (Máx 35 pts) ──
        sentimiento_volumen = 0.0
        
        # Sentimiento de analistas (0-15 pts)
        sentimiento_score = datos_sentimiento.get('sentimiento_score', 3.0)
        
        if sentimiento_score >= 4.5:
            sentimiento_volumen += 15
        elif sentimiento_score >= 4.0:
            sentimiento_volumen += 12
        elif sentimiento_score >= 3.5:
            sentimiento_volumen += 9
        elif sentimiento_score >= 3.0:
            sentimiento_volumen += 6
        elif sentimiento_score >= 2.5:
            sentimiento_volumen += 3
        
        # Volumen relativo (0-10 pts)
        volumen_actual = datos_sentimiento.get('volumen_actual', 0)
        volumen_promedio = datos_sentimiento.get('volumen_promedio', 0)
        
        if volumen_promedio > 0:
            ratio_volumen = volumen_actual / volumen_promedio
            
            if ratio_volumen > 2.0:
                sentimiento_volumen += 10  # Volumen muy alto
            elif ratio_volumen > 1.5:
                sentimiento_volumen += 7
            elif ratio_volumen > 1.0:
                sentimiento_volumen += 5
            else:
                sentimiento_volumen += 2
        
        # Volatilidad/Riesgo (0-10 pts) - menor volatilidad = más puntos
        volatilidad = datos_sentimiento.get('volatilidad', 0.0)
        
        if volatilidad < 0.15:
            sentimiento_volumen += 10  # Baja volatilidad
        elif volatilidad < 0.25:
            sentimiento_volumen += 7
        elif volatilidad < 0.35:
            sentimiento_volumen += 4
        else:
            sentimiento_volumen += 2  # Alta volatilidad
        
        # ── SCORE TOTAL OPORTUNIDAD ──
        score = tendencia_momentum + infravaloracion + sentimiento_volumen
        
    except Exception as e:
        print(f"⚠️ Error calculando score oportunidad: {e}")
        score = 50.0  # Valor neutro por defecto
    
    return round(min(100.0, max(0.0, score)), 1)


def calcular_score_final(score_calidad: float, score_oportunidad: float) -> float:
    """
    Calcula el Score Final StockLab aplicando la ponderación estricta
    de inversión a largo plazo.
    
    Fórmula: (score_calidad * 0.60) + (score_oportunidad * 0.40)
    
    Parámetros:
    -----------
    score_calidad : float
        Score de Calidad Empresarial (0-100)
    score_oportunidad : float
        Score de Oportunidad de Inversión (0-100)
    
    Retorna:
    --------
    float entre 0 y 100
    """
    score_final = (score_calidad * 0.60) + (score_oportunidad * 0.40)
    
    return round(min(100.0, max(0.0, score_final)), 1)


def calcular_ranking_completo(ticker: str, df_historico: pd.DataFrame, 
                              datos_fundamentales: Dict, 
                              datos_sentimiento: Dict) -> Dict[str, float]:
    """
    Calcula todos los scores para un ticker de forma integrada.
    
    Parámetros:
    -----------
    ticker : str
        Ticker del activo
    df_historico : pd.DataFrame
        DataFrame con precios históricos
    datos_fundamentales : dict
        Ratios fundamentales
    datos_sentimiento : dict
        Métricas de sentimiento y mercado
    
    Retorna:
    --------
    dict con:
    - 'ticker': str
    - 'score_calidad': float (0-100)
    - 'score_oportunidad': float (0-100)
    - 'score_final': float (0-100)
    """
    score_calidad = calcular_score_calidad(datos_fundamentales)
    score_oportunidad = calcular_score_oportunidad(df_historico, datos_sentimiento)
    score_final = calcular_score_final(score_calidad, score_oportunidad)
    
    return {
        'ticker': ticker,
        'score_calidad': score_calidad,
        'score_oportunidad': score_oportunidad,
        'score_final': score_final
    }


def _desglosar_calidad_detalle(datos_fundamentales: Dict[str, float]) -> Dict[str, float]:
    """
    Desglosa la Calidad Empresarial en sus 6 componentes internos detallados.
    
    Retorna dict con:
    - rentabilidad: (0-20) - ROE, ROA, ROIC
    - crecimiento: (0-20) - EPS Growth, Revenue Growth
    - fortaleza_financiera: (0-20) - Debt/Equity, Liquidez, Cobertura Intereses
    - dividendos: (0-15) - Yield + Sostenibilidad Payout Ratio
    - valoracion: (0-15) - PER, P/B, PEG
    - consistencia: (0-10) - Historial de beneficios + Market Cap
    """
    try:
        # ═══════════════════════════════════════════════════════════════════
        # 🎯 CLAVES ESTANDARIZADAS CAMELCASE - Single Source of Truth
        # ═══════════════════════════════════════════════════════════════════
        
        # ── RENTABILIDAD (Máx 20 pts) ──
        roe = datos_fundamentales.get('returnOnEquity', 0.0) or 0.0
        roa = datos_fundamentales.get('returnOnAssets', 0.0) or 0.0
        roic = datos_fundamentales.get('returnOnCapitalEmployed', 0.0) or 0.0
        
        if abs(roe) < 1:
            roe *= 100
        if abs(roa) < 1:
            roa *= 100
        if abs(roic) < 1:
            roic *= 100
        
        score_roe = min(8, max(0, roe / 2.5))  # >20% = 8pts
        score_roa = min(6, max(0, roa / 1.67))  # >10% = 6pts
        score_roic = min(6, max(0, roic / 1.67))  # >10% = 6pts
        
        rentabilidad = round(min(20, score_roe + score_roa + score_roic), 1)
        
        # ── CRECIMIENTO (Máx 20 pts) ──
        eps_growth = datos_fundamentales.get('epsGrowth', 0.0) or 0.0
        rev_growth = datos_fundamentales.get('revenueGrowth', 0.0) or 0.0
        
        if abs(eps_growth) < 1:
            eps_growth *= 100
        if abs(rev_growth) < 1:
            rev_growth *= 100
        
        score_eps = min(10, max(0, eps_growth / 2))  # >20% = 10pts
        score_rev = min(10, max(0, rev_growth / 2))  # >20% = 10pts
        
        crecimiento = round(min(20, score_eps + score_rev), 1)
        
        # ── FORTALEZA DEL BALANCE (Máx 20 pts) ──
        debt_equity = datos_fundamentales.get('debtToEquity', 0.0) or 0.0
        current_ratio = datos_fundamentales.get('currentRatio', 0.0) or 0.0
        interest_coverage = datos_fundamentales.get('interestCoverage', 0.0) or 0.0
        
        score_deuda = 10 if debt_equity < 0.5 else (7 if debt_equity < 1.0 else (4 if debt_equity < 2.0 else 0))
        score_liquidez = 5 if current_ratio > 1.5 else (3 if current_ratio > 1.0 else 0)
        score_cobertura = 5 if interest_coverage > 5 else (3 if interest_coverage > 2 else 0)
        
        fortaleza_financiera = round(min(20, score_deuda + score_liquidez + score_cobertura), 1)
        
        # ── DIVIDENDOS (Máx 15 pts) ──
        dividend_yield = datos_fundamentales.get('dividendYield', 0.0) or 0.0
        payout_ratio = datos_fundamentales.get('payoutRatio', 0.0) or 0.0
        
        if abs(dividend_yield) < 1:
            dividend_yield *= 100
        if abs(payout_ratio) < 1:
            payout_ratio *= 100
        
        score_yield = 8 if dividend_yield > 3 else (5 if dividend_yield > 1 else 0)
        score_payout = 7 if payout_ratio < 60 else (4 if payout_ratio < 80 else 0)
        
        dividendos = round(min(15, score_yield + score_payout), 1)
        
        # ── VALORACIÓN POR MÚLTIPLOS (Máx 15 pts) ──
        per = datos_fundamentales.get('trailingPE', 0.0) or 0.0
        p_b = datos_fundamentales.get('priceToBook', 0.0) or 0.0
        peg = datos_fundamentales.get('pegRatio', 0.0) or 0.0
        
        score_per = 6 if (per > 0 and per < 12) else (4 if per <= 18 else 2 if per <= 25 else 0)
        score_pb = 5 if (p_b > 0 and p_b < 1.5) else (3 if p_b <= 3 else 0)
        score_peg = 4 if (peg > 0 and peg < 1) else (2 if peg <= 2 else 0)
        
        valoracion = round(min(15, score_per + score_pb + score_peg), 1)
        
        # ── CONSISTENCIA DEL NEGOCIO (Máx 10 pts) ──
        market_cap = datos_fundamentales.get('marketCap', 0.0) or 0.0
        
        score_cap = 5 if market_cap > 5_000_000_000 else (3 if market_cap > 1_000_000_000 else 0)
        score_historial = 5 if market_cap > 10_000_000_000 else (3 if market_cap > 2_000_000_000 else 0)
        
        consistencia = round(min(10, score_cap + score_historial), 1)
        
        return {
            'rentabilidad': rentabilidad,
            'crecimiento': crecimiento,
            'fortaleza_financiera': fortaleza_financiera,
            'dividendos': dividendos,
            'valoracion': valoracion,
            'consistencia': consistencia
        }
    except Exception:
        return {
            'rentabilidad': 0.0,
            'crecimiento': 0.0,
            'fortaleza_financiera': 0.0,
            'dividendos': 0.0,
            'valoracion': 0.0,
            'consistencia': 0.0
        }


def _desglosar_oportunidad_detalle(df_historico: Optional[pd.DataFrame], 
                                   datos_fundamentales: Dict[str, float],
                                   variacion_diaria: float) -> Dict[str, float]:
    """
    Desglosa la Oportunidad de Inversión en sus 4 componentes internos detallados.
    
    Retorna dict con:
    - momentum_tecnico: (0-45) - SMA50/200 + RSI + MACD
    - valoracion: (0-20) - PER vs media histórica/sectorial
    - sentimiento: (0-15) - Recomendaciones de consenso
    - volumen_riesgo: (0-20) - Volatilidad + Liquidez
    """
    try:
        # ── TENDENCIA Y MOMENTUM TÉCNICO (Máx 45 pts) ──
        momentum_tecnico = 0.0
        
        if df_historico is not None and not df_historico.empty:
            try:
                if isinstance(df_historico.columns, pd.MultiIndex):
                    precios = df_historico['Close'].dropna()
                else:
                    precios = df_historico['Close'].dropna() if 'Close' in df_historico.columns else pd.Series()
                
                if len(precios) >= 200:
                    sma_50 = precios.rolling(window=50).mean().iloc[-1]
                    sma_200 = precios.rolling(window=200).mean().iloc[-1]
                    precio_actual = precios.iloc[-1]
                    
                    # Golden Cross / Death Cross (15 pts)
                    if precio_actual > sma_50 > sma_200:
                        momentum_tecnico += 15  # Golden Cross
                    elif precio_actual > sma_50:
                        momentum_tecnico += 10
                    elif precio_actual > sma_200:
                        momentum_tecnico += 5
                
                if len(precios) >= 50:
                    sma_20 = precios.rolling(window=20).mean().iloc[-1]
                    precio_actual = precios.iloc[-1]
                    
                    # Posición respecto a SMA20 (15 pts)
                    if precio_actual > sma_20:
                        momentum_tecnico += 15
                    elif precio_actual > sma_20 * 0.95:
                        momentum_tecnico += 8
                    
                    # RSI (15 pts)
                    if len(precios) >= 14:
                        delta = precios.diff()
                        gain = delta.where(delta > 0, 0).rolling(window=14).mean().iloc[-1]
                        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean().iloc[-1]
                        
                        if loss != 0:
                            rs = gain / loss
                            rsi = 100 - (100 / (1 + rs))
                        else:
                            rsi = 100.0
                            
                        if 40 <= rsi <= 60:
                            momentum_tecnico += 15  # Zona óptima
                        elif 30 <= rsi < 40 or 60 < rsi <= 70:
                            momentum_tecnico += 10
                        elif rsi < 30:
                            momentum_tecnico += 8  # Sobrevendido
                
                # Variación diaria positiva (bonus hasta 5 pts)
                if variacion_diaria > 0:
                    momentum_tecnico += min(5, variacion_diaria * 2)
                    
            except Exception:
                pass
        
        if momentum_tecnico == 0:
            momentum_tecnico = 22.5  # Valor neutro
        
        # ── INFRAVALORACIÓN RELATIVA (Máx 20 pts) ──
        per = datos_fundamentales.get('trailingPE') or datos_fundamentales.get('PER', 0.0) or 0.0
        
        if per > 0 and per < 10:
            infravaloracion = 20
        elif per >= 10 and per < 15:
            infravaloracion = 15
        elif per >= 15 and per < 20:
            infravaloracion = 10
        elif per >= 20 and per < 25:
            infravaloracion = 5
        else:
            infravaloracion = 0
        
        # ── SENTIMIENTO DE ANALISTAS (Máx 15 pts) ──
        recommendation = datos_fundamentales.get('recommendationMean') or datos_fundamentales.get('RecommendationMean', 3.0) or 3.0
        
        if recommendation <= 1.5:
            sentimiento = 15  # Strong Buy
        elif recommendation <= 2.0:
            sentimiento = 12  # Buy
        elif recommendation <= 2.5:
            sentimiento = 10  # Moderate Buy
        elif recommendation <= 3.0:
            sentimiento = 7   # Hold
        elif recommendation <= 3.5:
            sentimiento = 4   # Moderate Sell
        else:
            sentimiento = 0   # Sell
        
        # ── VOLUMEN Y RIESGO DE MERCADO (Máx 20 pts) ──
        volumen_riesgo = 0.0
        
        # Volatilidad (10 pts) - baja volatilidad es mejor
        # Intentamos usar beta o volatility
        volatility = datos_fundamentales.get('beta') or datos_fundamentales.get('Volatility', 0.0) or 0.0
        if abs(volatility) < 1:
            volatility *= 100
        
        if volatility < 15:
            volumen_riesgo += 10
        elif volatility < 25:
            volumen_riesgo += 7
        elif volatility < 35:
            volumen_riesgo += 4
        
        # Liquidez/Volumen (10 pts)
        avg_volume = datos_fundamentales.get('averageVolume') or datos_fundamentales.get('AverageVolume', 0.0) or 0.0
        
        if avg_volume > 1_000_000:
            volumen_riesgo += 10
        elif avg_volume > 500_000:
            volumen_riesgo += 7
        elif avg_volume > 100_000:
            volumen_riesgo += 4
        
        return {
            'momentum_tecnico': round(momentum_tecnico, 1),
            'valoracion': round(infravaloracion, 1),
            'sentimiento': round(sentimiento, 1),
            'volumen_riesgo': round(volumen_riesgo, 1)
        }
    except Exception:
        return {
            'momentum_tecnico': 0.0,
            'valoracion': 0.0,
            'sentimiento': 0.0,
            'volumen_riesgo': 0.0
        }
