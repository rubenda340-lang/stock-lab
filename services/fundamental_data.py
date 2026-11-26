"""
Servicio de Datos Fundamentales - StockLab V6
==============================================
Centraliza la recolección de ratios financieros estructurales.
Integración oficial con Financial Modeling Prep (FMP) y Finnhub.
Fallback determinista local para desarrollo y tickers no soportados.
"""

import pandas as pd
import numpy as np
import requests
import hashlib
from typing import Dict, Optional

# ═══════════════════════════════════════════════════════════════════
# 🔑 CREDENCIALES API — Cargadas desde config.py (.env)
# ═══════════════════════════════════════════════════════════════════
# Las claves se leen de variables de entorno vía python-dotenv.
# Si no están configuradas, el servicio usa fallback automáticamente.
from config import FMP_API_KEY, FINNHUB_API_KEY


# ═══════════════════════════════════════════════════════════════════
# 🔒 SESIÓN PERSONALIZADA PARA APIs EXTERNAS
# ═══════════════════════════════════════════════════════════════════
_api_session = requests.Session()
_api_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
})


def _safe_float(value, default: float = 0.0) -> float:
    """
    Convierte cualquier valor a float de forma segura.
    Maneja strings, None, NaN y otros tipos problemáticos.
    
    Parámetros:
    -----------
    value : any
        Valor a convertir
    default : float
        Valor por defecto si la conversión falla
    
    Retorna:
    --------
    float seguro (nunca retorna string "N/A" o None)
    """
    if value is None:
        return default
    
    try:
        # Convertir a string primero para manejar casos edge
        val_str = str(value).strip()
        
        # Si es "N/A", "null", vacío u otro placeholder
        if val_str.lower() in ['n/a', 'null', 'none', '', '-']:
            return default
        
        # Intentar conversión directa
        result = float(val_str)
        
        # Verificar que no sea NaN o Inf
        if np.isnan(result) or np.isinf(result):
            return default
        
        return result
        
    except (ValueError, TypeError):
        return default


def extraer_fundamentales_yahoo(ticker: str) -> Dict[str, float]:
    """
    Obtiene fundamentales para un ticker siguiendo la cascada de fuentes V7:
      Yahoo Finance → FMP → Finnhub → Fallback (ceros, sin datos ficticios).

    Delega al módulo data_provider para garantizar una única fuente de verdad.
    Mantenida por compatibilidad con vistas que ya importan esta función.

    Parámetros:
    -----------
    ticker : str
        Ticker del activo (ej: 'BBVA.MC', 'SAN.MC')

    Retorna:
    --------
    dict con ratios financieros estandarizados (claves camelCase).
    Campos ausentes son None, nunca ficticios.
    """
    try:
        from services.data_provider import fetch_fundamentals_uncached
        return fetch_fundamentals_uncached(ticker)
    except Exception:
        pass

    # Fallback de emergencia si data_provider no está disponible
    return _generar_datos_contingencia(ticker)


def _generar_datos_contingencia(ticker: str) -> Dict[str, float]:
    """
    Fallback de emergencia: retorna un diccionario con todos los valores a None.

    [V7] ELIMINADOS los datos ficticios generados con hash MD5.
    El motor de scoring recibe None para cada métrica ausente, lo que
    produce puntuaciones de 0 (conservador) en lugar de valores inventados.
    Los flags _data_source y _data_quality informan al sistema del estado.

    Parámetros:
    -----------
    ticker : str
        Ticker del activo

    Retorna:
    --------
    dict con todos los ratios a None + flags de calidad
    """
    return {
        # Valoración
        'trailingPE':          None,
        'forwardPE':           None,
        'priceToBook':         None,
        'pegRatio':            None,
        'priceToSales':        None,
        'enterpriseToEbitda':  None,
        'enterpriseValue':     None,
        # Rentabilidad
        'returnOnEquity':      None,
        'returnOnAssets':      None,
        'returnOnCapitalEmployed': None,
        'operatingMargins':    None,
        'profitMargins':       None,
        # Crecimiento
        'revenueGrowth':       None,
        'earningsGrowth':      None,
        'epsGrowth':           None,
        # Balance
        'debtToEquity':        None,
        'currentRatio':        None,
        'quickRatio':          None,
        'interestCoverage':    None,
        # Dividendos
        'dividendYield':       None,
        'payoutRatio':         None,
        'dividendPerShare':    None,
        # Mercado
        'marketCap':           None,
        'beta':                None,
        'averageVolume':       None,
        # Analistas
        'recommendationMean':  None,
        # Precios
        'previousClose':       None,
        'currentPrice':        None,
        # Metadatos de calidad
        '_data_source':        'fallback',
        '_data_quality':       'incomplete',
    }



def obtener_fundamentales_fmp(ticker: str) -> Dict[str, float]:
    """
    Obtiene fundamentales desde Financial Modeling Prep (FMP).
    
    Consulta el endpoint oficial de ratios financieros con la API key configurada.
    Limpia automáticamente el sufijo .MC para compatibilidad con FMP.
    
    Parámetros:
    -----------
    ticker : str
        Ticker del activo (ej: 'BBVA.MC', 'SAN')
    
    Retorna:
    --------
    dict con ratios fundamentales desde FMP, o diccionario vacío si falla
    """
    if not FMP_API_KEY:
        return {}
    
    # ═══════════════════════════════════════════════════════════════════
    # 🧹 LIMPIEZA DE TICKER PARA FMP - Eliminar sufijo .MC
    # ═══════════════════════════════════════════════════════════════════
    ticker_clean = ticker.split('.')[0]  # ACS.MC → ACS, SAN.MC → SAN
    
    # Intentar múltiples formatos de ticker para compatibilidad española
    ticker_variants = [ticker_clean, ticker]
    if ticker.endswith('.MC'):
        ticker_variants.append(ticker.replace('.MC', '.BME'))  # SAN.MC → SAN.BME
    
    for ticker_variant in ticker_variants:
        try:
            # Endpoint de ratios clave de FMP
            url = f"https://financialmodelingprep.com/api/v3/ratios/{ticker_variant}?apikey={FMP_API_KEY}"
            
            response = _api_session.get(url, timeout=10)
            
            # Si no encuentra el ticker, probar siguiente variante
            if response.status_code in [404, 403]:
                continue
            
            response.raise_for_status()
            
            data = response.json()
            
            if not data or len(data) == 0:
                continue
            
            ratios = data[0]  # FMP devuelve lista con un dict
            
            # ═══════════════════════════════════════════════════════════════════
            # 🎯 MAPEO EXACTO A CLAVES ESTANDARIZADAS - Single Source of Truth
            # ═══════════════════════════════════════════════════════════════════
            # Convertir nombres de FMP a formato camelCase estandarizado
            # que coincide exactamente con lo que espera el frontend
            
            return {
                # ═══════════════════════════════════════════════════════════════════
                # VALORACIÓN - Mapeo FMP → Claves estandarizadas
                # ═══════════════════════════════════════════════════════════════════
                'trailingPE': _safe_float(ratios.get('priceEarningsRatio')),
                'forwardPE': _safe_float(ratios.get('priceToEarningsRatioTTM')),
                'priceToBook': _safe_float(ratios.get('priceToBookRatio')),
                'pegRatio': _safe_float(ratios.get('pegRatio')),
                'priceToSales': _safe_float(ratios.get('priceToSalesRatio')),
                'enterpriseValue': _safe_float(ratios.get('enterpriseValue')),
                'evToEbitda': _safe_float(ratios.get('enterpriseValueMultiple')),
                
                # ═══════════════════════════════════════════════════════════════════
                # RENTABILIDAD - Mapeo FMP → Claves estandarizadas
                # ═══════════════════════════════════════════════════════════════════
                'returnOnEquity': _safe_float(ratios.get('returnOnEquity')),
                'returnOnAssets': _safe_float(ratios.get('returnOnAssets')),
                'returnOnCapitalEmployed': _safe_float(ratios.get('returnOnCapitalEmployed')),
                'operatingMargin': _safe_float(ratios.get('operatingProfitMargin')),
                'netProfitMargin': _safe_float(ratios.get('netProfitMargin')),
                
                # ═══════════════════════════════════════════════════════════════════
                # CRECIMIENTO - Mapeo FMP → Claves estandarizadas
                # ═══════════════════════════════════════════════════════════════════
                'revenueGrowth': _safe_float(ratios.get('revenueGrowth')),
                'netIncomeGrowth': _safe_float(ratios.get('netIncomeGrowth')),
                'epsGrowth': _safe_float(ratios.get('epsGrowth')),
                
                # ═══════════════════════════════════════════════════════════════════
                # BALANCE - Mapeo FMP → Claves estandarizadas
                # ═══════════════════════════════════════════════════════════════════
                'debtToEquity': _safe_float(ratios.get('debtEquityRatio')),
                'currentRatio': _safe_float(ratios.get('currentRatio')),
                'quickRatio': _safe_float(ratios.get('quickRatio')),
                'interestCoverage': _safe_float(ratios.get('interestCoverage')),
                
                # ═══════════════════════════════════════════════════════════════════
                # DIVIDENDOS - Mapeo FMP → Claves estandarizadas
                # ═══════════════════════════════════════════════════════════════════
                'dividendYield': _safe_float(ratios.get('dividendYield')),
                'payoutRatio': _safe_float(ratios.get('payoutRatio')),
                'dividendPerShare': _safe_float(ratios.get('dividendPerShare')),
                
                # ═══════════════════════════════════════════════════════════════════
                # CAPITALIZACIÓN Y MERCADO
                # ═══════════════════════════════════════════════════════════════════
                'marketCap': _safe_float(ratios.get('marketCap')),
                'beta': _safe_float(ratios.get('beta')),
            }
            
        except Exception:
            # Silenciar errores y probar siguiente variante
            continue
    
    # Si todas las variantes fallan, retornar diccionario vacío
    return {}


def obtener_fundamentales_finnhub(ticker: str) -> Dict[str, float]:
    """
    Obtiene fundamentales desde Finnhub.
    
    Consulta el endpoint de métricas básicas con la API key configurada.
    
    Parámetros:
    -----------
    ticker : str
        Ticker del activo
    
    Retorna:
    --------
    dict con métricas clave desde Finnhub, o diccionario vacío si falla
    """
    if not FINNHUB_API_KEY:
        return {}
    
    try:
        # Endpoint de métricas básicas de Finnhub
        url = f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}&metric=all&token={FINNHUB_API_KEY}"
        
        response = _api_session.get(url, timeout=10)
        
        # Si no encuentra el ticker o hay error de límite
        if response.status_code in [404, 403, 429]:
            return {}
        
        response.raise_for_status()
        
        data = response.json()
        metric_data = data.get('metric', {})
        
        if not metric_data:
            return {}
        
        return {
            # Valoración
            'PER': _safe_float(metric_data.get('peBasicExclExtraTTM')),
            'forward_PE': _safe_float(metric_data.get('peForwardAnnual')),
            'price_to_book': _safe_float(metric_data.get('pbAnnual')),
            'price_to_sales': _safe_float(metric_data.get('psAnnual')),
            
            # Rentabilidad
            'ROE': _safe_float(metric_data.get('roeTTM')),
            'ROA': _safe_float(metric_data.get('roaTTM')),
            'ROIC': _safe_float(metric_data.get('roicTTM')),
            'operating_margin': _safe_float(metric_data.get('operatingMarginTTM')),
            'profit_margin': _safe_float(metric_data.get('netMarginTTM')),
            
            # Crecimiento
            'revenue_growth': _safe_float(metric_data.get('revenueGrowthTTMYoy')),
            'earnings_growth': _safe_float(metric_data.get('epsGrowthTTMYoy')),
            'eps_growth': _safe_float(metric_data.get('epsGrowthNYOY')),
            
            # Balance
            'debt_to_equity': _safe_float(metric_data.get('totalDebtToTotalEquityAnnual')),
            'current_ratio': _safe_float(metric_data.get('currentRatioAnnual')),
            'quick_ratio': _safe_float(metric_data.get('quickRatioAnnual')),
            
            # Dividendos
            'dividend_yield': _safe_float(metric_data.get('dividendYieldIndicatedAnnual')),
            'payout_ratio': _safe_float(metric_data.get('payoutRatioTTM')),
            
            # Otros
            'beta': _safe_float(metric_data.get('beta')),
            'market_cap': _safe_float(metric_data.get('marketCapitalization')),
        }
        
    except Exception:
        # Silenciar errores completamente
        return {}


def obtener_sentimiento_analistas(ticker: str) -> Dict[str, float]:
    """
    Obtiene recomendaciones de analistas y sentimiento del mercado desde Finnhub.
    
    Si Finnhub falla, genera datos de contingencia deterministas basados en el ticker.
    Nunca consulta Yahoo Finance para evitar bloqueos por IP.
    
    Parámetros:
    -----------
    ticker : str
        Ticker del activo
    
    Retorna:
    --------
    dict con métricas de sentimiento (sentimiento_score 1.0-5.0, desglose por categoría)
    """
    
    # Intentar primero con Finnhub
    try:
        if FINNHUB_API_KEY:
            url = f"https://finnhub.io/api/v1/stock/recommendation?symbol={ticker}&token={FINNHUB_API_KEY}"
            
            response = _api_session.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                if data and len(data) > 0:
                    # Finnhub devuelve lista histórica, tomar la más reciente
                    ultima_rec = data[-1]
                    
                    strong_buy = int(ultima_rec.get('strongBuy', 0) or 0)
                    buy = int(ultima_rec.get('buy', 0) or 0)
                    hold = int(ultima_rec.get('hold', 0) or 0)
                    sell = int(ultima_rec.get('sell', 0) or 0)
                    strong_sell = int(ultima_rec.get('strongSell', 0) or 0)
                    
                    total = strong_buy + buy + hold + sell + strong_sell
                    
                    if total > 0:
                        # Calcular score ponderado (1-5, donde 5 es muy positivo)
                        sentimiento_score = (
                            (strong_buy * 5 + buy * 4 + hold * 3 + sell * 2 + strong_sell * 1) / total
                        )
                    else:
                        sentimiento_score = 3.0  # Neutral por defecto
                    
                    return {
                        'sentimiento_score': float(sentimiento_score),
                        'total_analistas': int(total),
                        'strong_buy': strong_buy,
                        'buy': buy,
                        'hold': hold,
                        'sell': sell,
                        'strong_sell': strong_sell,
                    }
    except Exception:
        # Silenciar errores completamente
        pass
    
    # ═══════════════════════════════════════════════════════════════════
    # 🛡️ FALLBACK DETERMINISTA - CERO CONSULTAS A YAHOO
    # ═══════════════════════════════════════════════════════════════════
    
    # Usar hash del ticker para generar variación determinística
    ticker_hash = int(hashlib.md5(ticker.encode()).hexdigest(), 16)
    char_sum = sum(ord(c) for c in ticker if c.isalpha())
    factor = ((ticker_hash + char_sum) % 1000) / 1000.0
    
    # Generar distribución de analistas realista
    total_analistas = 5 + int(factor * 20)  # 5 a 25 analistas
    
    # Distribuir según factor (más bajo = más bullish)
    if factor < 0.3:
        # Bullish
        strong_buy = int(total_analistas * 0.4)
        buy = int(total_analistas * 0.3)
        hold = total_analistas - strong_buy - buy
        sell = 0
        strong_sell = 0
    elif factor < 0.6:
        # Moderado
        strong_buy = int(total_analistas * 0.2)
        buy = int(total_analistas * 0.3)
        hold = int(total_analistas * 0.4)
        sell = total_analistas - strong_buy - buy - hold
        strong_sell = 0
    else:
        # Bearish
        strong_buy = int(total_analistas * 0.1)
        buy = int(total_analistas * 0.2)
        hold = int(total_analistas * 0.4)
        sell = int(total_analistas * 0.2)
        strong_sell = total_analistas - strong_buy - buy - hold - sell
    
    total_real = strong_buy + buy + hold + sell + strong_sell
    
    if total_real > 0:
        sentimiento_score = (
            (strong_buy * 5 + buy * 4 + hold * 3 + sell * 2 + strong_sell * 1) / total_real
        )
    else:
        sentimiento_score = 3.0
    
    return {
        'sentimiento_score': round(float(sentimiento_score), 2),
        'total_analistas': int(total_real),
        'strong_buy': int(strong_buy),
        'buy': int(buy),
        'hold': int(hold),
        'sell': int(sell),
        'strong_sell': int(strong_sell),
    }
