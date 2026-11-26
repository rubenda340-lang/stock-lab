"""
StockLab V7 — Data Provider
============================
Única fuente de verdad para la obtención de datos de mercado y fundamentales.

Pipeline de datos (cascada de fuentes):
  1. Yahoo Finance  — primera opción, mejor cobertura para tickers .MC
  2. FMP            — si Yahoo falla y hay API key configurada
  3. Finnhub        — si FMP falla y hay API key configurada
  4. Fallback       — diccionario de ceros con flag de advertencia (SIN datos ficticios)

Todas las fuentes devuelven el mismo diccionario estandarizado con claves camelCase.
  _data_source  → 'yahoo' | 'fmp' | 'finnhub' | 'fallback'
  _data_quality → 'real' | 'partial' | 'incomplete'

Funciones públicas principales:
  fetch_fundamentals_uncached(ticker)   — cascada sin caché (para uso paralelo)
  get_fundamentals(ticker)              — cascada con caché individual (12h)
  get_price_batch(lista_tickers)        — descarga batch OHLCV 60d con caché (5min)
  get_intraday_variation(ticker, ...)   — Contrato Intradía: (precio_actual, variacion_intradia_pct) → Dashboard/Empresas
  get_closing_variation(df, ...)        — Contrato Cierre: (precio_cierre, variacion_sesion_cierre_pct) → Ranking/Oportunidad
  diagnosticar_ticker(ticker)           — diagnóstico de disponibilidad de datos
"""

import json
import os
import time
import time as _time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import streamlit as st
import concurrent.futures
from typing import Dict, List, Optional, Tuple

# Credenciales opcionales
try:
    from config import FMP_API_KEY, FINNHUB_API_KEY
except ImportError:
    FMP_API_KEY = ""
    FINNHUB_API_KEY = ""


# ═══════════════════════════════════════════════════════════════════
# 🔒 SESIÓN HTTP COMPARTIDA
# ═══════════════════════════════════════════════════════════════════

_session = requests.Session()
_session.headers.update({
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/html, */*;q=0.9',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
})


# ═══════════════════════════════════════════════════════════════════
# 🎯 ESQUEMA ESTANDARIZADO — Single Source of Truth
# ═══════════════════════════════════════════════════════════════════
# Estas son TODAS las claves que el motor de scoring V6 puede necesitar.
# Valor None = dato ausente (nunca ficticios).

_CLAVES_ESTANDAR: Dict = {
    # Valoración
    'trailingPE':          None,
    'forwardPE':           None,
    'priceToBook':         None,
    'pegRatio':            None,
    'priceToSales':        None,
    'enterpriseToEbitda':  None,
    'enterpriseValue':     None,
    # Rentabilidad (valores en decimal: 0.15 = 15%)
    'returnOnEquity':      None,
    'returnOnAssets':      None,
    'operatingMargins':    None,
    'profitMargins':       None,
    # Crecimiento (valores en decimal)
    'revenueGrowth':       None,
    'earningsGrowth':      None,
    # Balance / Solvencia
    'debtToEquity':        None,   # Escala Yahoo: 50 = 0.5 D/E
    'currentRatio':        None,
    'quickRatio':          None,
    'interestCoverage':    None,
    # Dividendos (valores en decimal: 0.04 = 4%)
    'dividendYield':       None,
    'payoutRatio':         None,
    # Mercado
    'marketCap':           None,
    'beta':                None,
    'averageVolume':       None,
    # Analistas (1=Strong Buy … 5=Strong Sell — escala Yahoo)
    'recommendationMean':  None,
    # Precios de referencia para variación diaria
    'previousClose':       None,
    'currentPrice':        None,
    # Metadatos de calidad (NO usados por el scoring)
    '_data_source':        'fallback',
    '_data_quality':       'incomplete',
}


# ─────────────────────────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────────────────────────

def _diccionario_vacio() -> Dict:
    """Retorna un dict con todas las claves a None y flags de fallback."""
    d = dict(_CLAVES_ESTANDAR)
    d['_data_source'] = 'fallback'
    d['_data_quality'] = 'incomplete'
    return d


def _safe_float(value, default=None) -> Optional[float]:
    """
    Convierte cualquier valor a float de forma segura.
    Retorna `default` (normalmente None) si el valor no es válido,
    permitiendo distinguir entre 'dato ausente' y 'dato = 0'.
    """
    if value is None:
        return default
    try:
        val_str = str(value).strip()
        if val_str.lower() in ('n/a', 'null', 'none', '', '-', 'nan', 'inf', '-inf'):
            return default
        result = float(val_str)
        if np.isnan(result) or np.isinf(result):
            return default
        return result
    except (ValueError, TypeError):
        return default


def _tiene_datos_utiles(d: Dict) -> bool:
    """True si el dict tiene datos reales en al menos alguna métrica clave."""
    # Nivel 1: métricas clásicas
    claves_criticas = ('trailingPE', 'returnOnEquity', 'debtToEquity', 'marketCap')
    if any(d.get(k) is not None for k in claves_criticas):
        return True
    # Nivel 2: rescate para empresas con precio + márgenes/crecimiento (ej: Prisa)
    tiene_precio = d.get('currentPrice') is not None or d.get('previousClose') is not None
    tiene_metricas = any(d.get(k) is not None for k in
                         ('operatingMargins', 'revenueGrowth', 'forwardPE', 'beta'))
    return tiene_precio and tiene_metricas


# ── CACHÉ PERSISTENTE DE FUNDAMENTALES EN DISCO ──────────────────────
# Almacena respuestas válidas del pipeline Yahoo/FMP/Finnhub como JSON.
# Evita depender de la conexión a Yahoo en cada carga de Ranking.
_DP_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "cache", "fundamentals_dp")
_EMPRESAS_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "data", "cache", "fundamentals")
_DP_DISK_TTL = 43200  # 12 horas


def _dp_ticker_filename(ticker: str) -> str:
    """Convierte un ticker a nombre de archivo seguro."""
    return ticker.replace('.', '_').replace('/', '_').replace('\\', '_') + '.json'


def _dp_leer_cache_disco(ticker: str, ignore_ttl: bool = False) -> Optional[Dict]:
    """Lee fundamentales estandarizados del caché en disco."""
    try:
        filepath = os.path.join(_DP_CACHE_DIR, _dp_ticker_filename(ticker))
        if not os.path.exists(filepath):
            return None
        if not ignore_ttl:
            mtime = os.path.getmtime(filepath)
            if (_time.time() - mtime) > _DP_DISK_TTL:
                return None
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _dp_guardar_cache_disco(ticker: str, data: Dict) -> None:
    """Guarda fundamentales estandarizados en caché de disco."""
    try:
        os.makedirs(_DP_CACHE_DIR, exist_ok=True)
        filepath = os.path.join(_DP_CACHE_DIR, _dp_ticker_filename(ticker))
        data_limpia = {}
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool, type(None), list, dict)):
                data_limpia[k] = v
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data_limpia, f, ensure_ascii=False, default=str)
    except Exception:
        pass


def _enriquecer_con_fast_info_dp(ticker_obj, d: Dict) -> Dict:
    """
    Complementa el dict estandarizado con datos de fast_info para campos
    clave ausentes. fast_info es un endpoint ligero de yfinance que a veces
    reporta datos que .info omite (ej: marketCap para FCC, Prisa, Deoleo).
    """
    try:
        fi = ticker_obj.fast_info
        if d.get('marketCap') is None:
            mc = getattr(fi, 'market_cap', None)
            if mc is not None and isinstance(mc, (int, float)) and mc > 0:
                d['marketCap'] = mc
        if d.get('currentPrice') is None:
            lp = getattr(fi, 'last_price', None)
            if lp is not None and isinstance(lp, (int, float)):
                d['currentPrice'] = lp
        if d.get('previousClose') is None:
            pc = getattr(fi, 'previous_close', None)
            if pc is not None and isinstance(pc, (int, float)):
                d['previousClose'] = pc
    except Exception:
        pass
    return d


def _normalizar_dividend_yield(info_dict: dict) -> Optional[float]:
    """
    Normaliza dividendYield a escala DECIMAL pura (0.0196 para 1.96%).

    Yahoo .info devuelve dividendYield en escala inconsistente para tickers .MC:
      - BBVA.MC: 3.69  (porcentaje directo, 3.69%)
      - MTS.MC:  0.82  (porcentaje directo, 0.82%)
    trailingAnnualDividendYield siempre viene en decimal puro (0.0096).

    Estrategia: usamos trailingAnnualDividendYield como fuente preferida
    cuando está disponible (siempre en decimal puro). Si no, usamos
    dividendYield y lo normalizamos dividiéndolo entre 100.
    """
    tady = _safe_float(info_dict.get('trailingAnnualDividendYield'))
    if tady is not None and tady > 0:
        return tady  # Ya está en decimal puro (ej: 0.0096)

    dy = _safe_float(info_dict.get('dividendYield'))
    if dy is not None and dy > 0:
        # Yahoo .info devuelve en porcentaje (ej: 3.69 para 3.69%)
        # Normalizamos a decimal: 3.69 → 0.0369
        return dy / 100.0

    return None


# ═══════════════════════════════════════════════════════════════════
# 📡 FUENTE 1: YAHOO FINANCE
# ═══════════════════════════════════════════════════════════════════

def _obtener_yahoo(ticker: str) -> Dict:
    """
    Obtiene fundamentales desde Yahoo Finance (yf.Ticker().info).
    Primera fuente preferida: mejor cobertura para tickers .MC del BME.
    Enriquece con fast_info para recuperar marketCap y precios ausentes.
    Reutiliza la caché local de empresas si está disponible.
    """
    try:
        # 1. Reutilizar caché en disco de views/empresas.py si existe y es válida (< 12h)
        info = None
        t = None
        try:
            emp_file = os.path.join(_EMPRESAS_CACHE_DIR, _dp_ticker_filename(ticker))
            if os.path.exists(emp_file):
                mtime = os.path.getmtime(emp_file)
                if (_time.time() - mtime) <= _DP_DISK_TTL:
                    with open(emp_file, 'r', encoding='utf-8') as f:
                        info = json.load(f)
        except Exception:
            info = None

        if not info:
            t = yf.Ticker(ticker, session=_session)
            info = t.info

        if not info or len(info) < 5:
            return {}

        # Yahoo devuelve dict casi vacío para tickers inválidos/sin datos
        if not (info.get('regularMarketPrice') or info.get('currentPrice')
                or info.get('previousClose')):
            return {}

        d = _diccionario_vacio()

        # Valoración
        d['trailingPE']         = _safe_float(info.get('trailingPE'))
        d['forwardPE']          = _safe_float(info.get('forwardPE'))
        d['priceToBook']        = _safe_float(info.get('priceToBook'))
        d['pegRatio']           = _safe_float(info.get('pegRatio') or info.get('trailingPegRatio'))
        d['priceToSales']       = _safe_float(info.get('priceToSalesTrailing12Months'))
        d['enterpriseToEbitda'] = _safe_float(info.get('enterpriseToEbitda'))
        d['enterpriseValue']    = _safe_float(info.get('enterpriseValue'))

        # Rentabilidad (Yahoo ya devuelve en decimal, ej: 0.18 para 18%)
        d['returnOnEquity']   = _safe_float(info.get('returnOnEquity'))
        d['returnOnAssets']   = _safe_float(info.get('returnOnAssets'))
        d['operatingMargins'] = _safe_float(info.get('operatingMargins'))
        d['profitMargins']    = _safe_float(info.get('profitMargins'))

        # Crecimiento (Yahoo ya devuelve en decimal, ej: 0.05 para 5%)
        d['revenueGrowth']  = _safe_float(info.get('revenueGrowth'))
        d['earningsGrowth'] = _safe_float(info.get('earningsGrowth'))

        # Balance
        # Yahoo devuelve debtToEquity como porcentaje (ej: 49.173 para 49.173% D/E). 
        # Lo normalizamos a ratio decimal puro (ej: 0.4917).
        debt_equity_raw = _safe_float(info.get('debtToEquity'))
        d['debtToEquity']    = debt_equity_raw / 100.0 if debt_equity_raw is not None else None
        d['currentRatio']    = _safe_float(info.get('currentRatio'))
        d['quickRatio']      = _safe_float(info.get('quickRatio'))
        d['interestCoverage'] = _safe_float(info.get('interestCoverage'))

        # Dividendos — normalizado a DECIMAL puro (0.0369 para 3.69%)
        d['dividendYield'] = _normalizar_dividend_yield(info)
        d['payoutRatio']   = _safe_float(info.get('payoutRatio'))

        # Mercado
        d['marketCap']     = _safe_float(info.get('marketCap'))
        d['beta']          = _safe_float(info.get('beta'))
        d['averageVolume'] = _safe_float(
            info.get('averageVolume') or info.get('averageDailyVolume10Day')
        )

        # Analistas
        d['recommendationMean'] = _safe_float(info.get('recommendationMean'))

        # Precios de referencia
        d['previousClose'] = _safe_float(
            info.get('previousClose') or info.get('regularMarketPreviousClose')
        )
        d['currentPrice']  = _safe_float(
            info.get('currentPrice') or info.get('regularMarketPrice')
        )

        # ── Enriquecer con fast_info para campos ausentes ──
        if t is not None:
            d = _enriquecer_con_fast_info_dp(t, d)
        elif d.get('marketCap') is None:
            try:
                t = yf.Ticker(ticker, session=_session)
                d = _enriquecer_con_fast_info_dp(t, d)
            except Exception:
                pass

        d['_data_source']  = 'yahoo'
        d['_data_quality'] = 'real' if _tiene_datos_utiles(d) else 'incomplete'

        return d

    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════
# 📡 FUENTE 2: FINANCIAL MODELING PREP (FMP)
# ═══════════════════════════════════════════════════════════════════

def _obtener_fmp(ticker: str) -> Dict:
    """
    Obtiene fundamentales desde Financial Modeling Prep (FMP).
    FMP devuelve todos los ratios en escala decimal pura (ej: ROE = 0.18, D/E = 0.45).
    """
    if not FMP_API_KEY:
        return {}

    ticker_clean = ticker.split('.')[0]
    variantes = [ticker_clean]
    if ticker.endswith('.MC'):
        variantes.append(ticker.replace('.MC', '.BME'))

    for variante in variantes:
        try:
            url = (f"https://financialmodelingprep.com/api/v3/ratios/"
                   f"{variante}?apikey={FMP_API_KEY}&limit=1")
            resp = _session.get(url, timeout=10)
            if resp.status_code in (403, 404, 429):
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data or not isinstance(data, list):
                continue

            r = data[0]

            # Perfil para marketCap, beta, volumen
            perfil: Dict = {}
            try:
                url_p = (f"https://financialmodelingprep.com/api/v3/profile/"
                         f"{variante}?apikey={FMP_API_KEY}")
                rp = _session.get(url_p, timeout=8)
                if rp.status_code == 200:
                    dp = rp.json()
                    if dp and isinstance(dp, list):
                        perfil = dp[0]
            except Exception:
                pass

            d = _diccionario_vacio()

            # Valoración
            d['trailingPE']         = _safe_float(r.get('priceEarningsRatio'))
            d['priceToBook']        = _safe_float(r.get('priceToBookRatio'))
            d['pegRatio']           = _safe_float(r.get('priceEarningsToGrowthRatio'))
            d['priceToSales']       = _safe_float(r.get('priceToSalesRatio'))
            d['enterpriseToEbitda'] = _safe_float(r.get('enterpriseValueMultiple'))

            # Rentabilidad (ya vienen en decimal desde FMP)
            d['returnOnEquity']   = _safe_float(r.get('returnOnEquity'))
            d['returnOnAssets']   = _safe_float(r.get('returnOnAssets'))
            d['operatingMargins'] = _safe_float(r.get('operatingProfitMargin'))
            d['profitMargins']    = _safe_float(r.get('netProfitMargin'))

            # Crecimiento (ya vienen en decimal)
            d['revenueGrowth']  = _safe_float(r.get('revenueGrowth'))
            d['earningsGrowth'] = _safe_float(r.get('netIncomeGrowth'))

            # Balance (ya vienen en decimal)
            d['debtToEquity']    = _safe_float(r.get('debtEquityRatio'))
            d['currentRatio']    = _safe_float(r.get('currentRatio'))
            d['quickRatio']      = _safe_float(r.get('quickRatio'))
            d['interestCoverage'] = _safe_float(r.get('interestCoverage'))

            # Dividendos (ya vienen en decimal)
            d['dividendYield'] = _safe_float(r.get('dividendYield'))
            d['payoutRatio']   = _safe_float(r.get('payoutRatio'))

            # Mercado (desde perfil)
            d['marketCap']     = _safe_float(perfil.get('mktCap') or r.get('marketCap'))
            d['beta']          = _safe_float(perfil.get('beta'))
            d['averageVolume'] = _safe_float(perfil.get('volAvg'))
            d['recommendationMean'] = None

            d['_data_source']  = 'fmp'
            d['_data_quality'] = 'real' if _tiene_datos_utiles(d) else 'partial'

            if _tiene_datos_utiles(d):
                return d

        except Exception:
            continue

    return {}


# ═══════════════════════════════════════════════════════════════════
# 📡 FUENTE 3: FINNHUB
# ═══════════════════════════════════════════════════════════════════

def _obtener_finnhub(ticker: str) -> Dict:
    """
    Obtiene fundamentales desde Finnhub.
    Finnhub devuelve tasas y ratios en porcentaje (escala 0-100), los normalizamos
    incondicionalmente a decimal (escala 0-1).
    """
    if not FINNHUB_API_KEY:
        return {}

    try:
        url = (f"https://finnhub.io/api/v1/stock/metric"
               f"?symbol={ticker}&metric=all&token={FINNHUB_API_KEY}")
        resp = _session.get(url, timeout=10)
        if resp.status_code in (403, 404, 429):
            return {}
        resp.raise_for_status()
        m = resp.json().get('metric', {})
        if not m:
            return {}

        d = _diccionario_vacio()

        # Valoración
        d['trailingPE']         = _safe_float(m.get('peBasicExclExtraTTM') or m.get('peTTM'))
        d['forwardPE']          = _safe_float(m.get('peForwardAnnual') or m.get('peFwdAnnual'))
        d['priceToBook']        = _safe_float(m.get('pbAnnual') or m.get('pbQuarterly'))
        d['pegRatio']           = _safe_float(m.get('pegRatio'))
        d['priceToSales']       = _safe_float(m.get('psAnnual') or m.get('psTTM'))
        d['enterpriseToEbitda'] = _safe_float(m.get('evToEbitda') or m.get('enterpriseValueToEBITDA'))

        # Helper incondicional para normalizar porcentaje de 0-100 a decimal 0-1
        def _pct_to_dec(v):
            val = _safe_float(v)
            return val / 100.0 if val is not None else None

        # Rentabilidad (de % a decimal)
        d['returnOnEquity']   = _pct_to_dec(m.get('roeTTM') or m.get('roeRfy'))
        d['returnOnAssets']   = _pct_to_dec(m.get('roaTTM') or m.get('roaRfy'))
        d['operatingMargins'] = _pct_to_dec(m.get('operatingMarginTTM') or m.get('operatingMarginAnnual'))
        d['profitMargins']    = _pct_to_dec(m.get('netMarginTTM') or m.get('netProfitMarginAnnual'))

        # Crecimiento (de % a decimal)
        d['revenueGrowth']  = _pct_to_dec(m.get('revenueGrowthTTMYoy') or m.get('revenueGrowth3Y'))
        d['earningsGrowth'] = _pct_to_dec(m.get('epsGrowthTTMYoy') or m.get('epsGrowth3Y'))

        # Balance (de % a decimal para debtToEquity; ratios puros sin tocar)
        d['debtToEquity']    = _pct_to_dec(m.get('totalDebtToTotalEquityAnnual') or m.get('totalDebtToTotalEquityQuarterly'))
        d['currentRatio']    = _safe_float(m.get('currentRatioAnnual') or m.get('currentRatioQuarterly'))
        d['quickRatio']      = _safe_float(m.get('quickRatioAnnual') or m.get('quickRatioQuarterly'))
        d['interestCoverage'] = None  # No disponible en /metric de Finnhub

        # Dividendos (de % a decimal)
        d['dividendYield'] = _pct_to_dec(m.get('dividendYieldIndicatedAnnual') or m.get('currentDividendYieldTTM'))
        d['payoutRatio']   = _pct_to_dec(m.get('payoutRatioTTM'))

        # Mercado — Finnhub marketCapitalization en millones
        mktcap_raw = _safe_float(m.get('marketCapitalization'))
        d['marketCap']     = mktcap_raw * 1_000_000 if mktcap_raw else None
        d['beta']          = _safe_float(m.get('beta') or m.get('beta3Year'))
        d['averageVolume'] = _safe_float(m.get('10DayAverageTradingVolume') or m.get('3MonthAverageTradingVolume'))

        # Analistas
        try:
            url_rec = (f"https://finnhub.io/api/v1/stock/recommendation"
                       f"?symbol={ticker}&token={FINNHUB_API_KEY}")
            rr = _session.get(url_rec, timeout=8)
            if rr.status_code == 200:
                recs = rr.json()
                if recs and isinstance(recs, list):
                    ultima = recs[-1]
                    sb = int(ultima.get('strongBuy', 0) or 0)
                    b  = int(ultima.get('buy', 0) or 0)
                    h  = int(ultima.get('hold', 0) or 0)
                    s  = int(ultima.get('sell', 0) or 0)
                    ss = int(ultima.get('strongSell', 0) or 0)
                    total = sb + b + h + s + ss
                    if total > 0:
                        d['recommendationMean'] = round(
                            (sb * 1 + b * 2 + h * 3 + s * 4 + ss * 5) / total, 2
                        )
        except Exception:
            pass

        d['_data_source']  = 'finnhub'
        d['_data_quality'] = 'real' if _tiene_datos_utiles(d) else 'partial'

        return d if _tiene_datos_utiles(d) else {}

    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════
# 🔑 FUNCIÓN INTERNA PRINCIPAL (sin caché — para uso paralelo)
# ═══════════════════════════════════════════════════════════════════

def _fetch_fundamentals_uncached(ticker: str) -> Dict:
    """
    Cascada de fuentes con caché multinivel resiliente:
      1. Disco local JSON (TTL 12 h) — respuesta instantánea sin red
      2. Yahoo Finance + fast_info → FMP → Finnhub — fuentes en vivo
      3. Disco stale sin TTL — fallback de emergencia
    Los valores ausentes son None (nunca datos ficticios).
    """
    ticker = ticker.strip().upper()

    # Nivel 1: Caché en disco válido (< 12h)
    disk_data = _dp_leer_cache_disco(ticker)
    if disk_data and _tiene_datos_utiles(disk_data):
        return disk_data

    # Nivel 2: Cascada en vivo Yahoo → FMP → Finnhub
    for _fn in (_obtener_yahoo, _obtener_fmp, _obtener_finnhub):
        try:
            datos = _fn(ticker)
            if datos and _tiene_datos_utiles(datos):
                _dp_guardar_cache_disco(ticker, datos)
                return datos
        except Exception:
            continue

    # Nivel 3: Disco stale sin TTL (datos antiguos mejor que nada)
    disk_stale = _dp_leer_cache_disco(ticker, ignore_ttl=True)
    if disk_stale and _tiene_datos_utiles(disk_stale):
        return disk_stale

    d = _diccionario_vacio()
    d['_data_source']  = 'fallback'
    d['_data_quality'] = 'incomplete'
    return d


# Alias público para importar desde ranking.py y otros módulos
fetch_fundamentals_uncached = _fetch_fundamentals_uncached


# ═══════════════════════════════════════════════════════════════════
# 🔑 FUNCIÓN PÚBLICA: get_fundamentals()  (con caché individual)
# ═══════════════════════════════════════════════════════════════════

@st.cache_data(ttl=43200, show_spinner=False)
def get_fundamentals(ticker: str) -> Dict:
    """
    Fundamentales con caché individual de 12 horas.
    Usa la misma cascada Yahoo → FMP → Finnhub → fallback.
    Preferir get_fundamentals_batch() para múltiples tickers en la vista Ranking.
    """
    return _fetch_fundamentals_uncached(ticker)


# ═══════════════════════════════════════════════════════════════════
# 📈 FUNCIÓN PÚBLICA: get_price_batch()
# ═══════════════════════════════════════════════════════════════════

# Ventana de datos del motor. Fase 1 (higiene): 90d → 1y para estabilizar
# SMA50 / ADX(14) / RSI(14) (antes ~63 sesiones, ahora ~250).
PERIODO_PRECIOS = "1y"


def descargar_price_batch(lista_tickers: tuple, period: str = PERIODO_PRECIOS) -> pd.DataFrame:
    """
    Descarga SIN caché de barras diarias OHLCV (misma configuración que
    get_price_batch). La usa la emisión programada (emitir_senales.py), que
    corre fuera de Streamlit y necesita datos frescos tras el cierre.
    """
    if not lista_tickers:
        return pd.DataFrame()
    tickers_str = " ".join(lista_tickers)
    try:
        return yf.download(
            tickers_str,
            period=period,
            interval="1d",
            group_by='ticker',
            progress=False,
            session=_session,
            auto_adjust=True,   # Corregir splits y dividendos automáticamente (retorno total)
            threads=True,
        )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def get_price_batch(lista_tickers: tuple) -> pd.DataFrame:
    """
    Descarga barras diarias OHLCV para múltiples tickers en una sola llamada.

    Usa period="1y" (~250 sesiones) para disponer de suficientes sesiones para:
      - SMA-50, ADX(14), ATR(14) con rodaje suficiente
      - RSI-14 (necesita ≥ 15 barras)
      - Variación diaria correcta

    OJO: la última barra puede ser intradía (sesión abierta). Para persistir
    señales usar services.emision, que trunca a la última barra COMPLETA.

    Parámetros:
    -----------
    lista_tickers : tuple (inmutable para cacheabilidad)

    Retorna:
    --------
    pd.DataFrame con MultiIndex de columnas (si hay múltiples tickers)
    o DataFrame simple (si hay un solo ticker).
    """
    return descargar_price_batch(lista_tickers, PERIODO_PRECIOS)


# ═══════════════════════════════════════════════════════════════════
# 💹 CONTRATOS DE PRECIO Y VARIACIÓN — SSOT
# ═══════════════════════════════════════════════════════════════════

def get_intraday_variation(ticker: str,
                           df_historico: Optional[pd.DataFrame] = None,
                           fundamentales: Optional[Dict] = None) -> Tuple[float, float]:
    """
    CONTRATO INTRADÍA (Dashboard & Empresas)
    ========================================
    Calcula precio actual en tiempo real y variación respecto al cierre anterior.

    Fórmula:
      variacion_intradia_pct = ((precio_actual - previous_close) / previous_close) * 100.0

    Cascada (optimizada: datos frescos primero, red solo como último recurso):
      1. df_historico: último cierre vs precio anterior (OHLCV 5-min, fresco) — 0 ms
      2. currentPrice + previousClose del dict fundamentales (caché ~12h) — 0 ms
      3. fast_info.last_price + fast_info.previous_close  (tiempo real Yahoo) — ~300 ms

    Retorna:
    --------
    Tuple (precio_actual: float, variacion_intradia_pct: float)
    """
    ticker = ticker.strip().upper()
    precio_actual = 0.0
    variacion_pct = 0.0

    # Fuente 1 (in-memory): df_historico — último precio conocido vs precio anterior
    # OHLCV 5-min cacheado, datos frescos. Mismo origen que get_closing_variation.
    if df_historico is not None and not df_historico.empty:
        try:
            col = df_historico['Close'] if 'Close' in df_historico.columns else None
            if col is not None:
                precios = col.dropna()
                if len(precios) >= 2:
                    precio_actual = float(precios.iloc[-1])
                    precio_ant    = float(precios.iloc[-2])
                    if precio_ant > 0:
                        variacion_pct = ((precio_actual - precio_ant) / precio_ant) * 100.0
                    return round(precio_actual, 4), round(variacion_pct, 4)
        except Exception:
            pass

    # Fuente 2 (in-memory): currentPrice + previousClose del dict de fundamentales
    # Caché ~12h, usado como fallback si no hay df_historico disponible
    if fundamentales:
        cp = _safe_float(fundamentales.get('currentPrice'))
        pc = _safe_float(fundamentales.get('previousClose'))
        if cp and cp > 0 and pc and pc > 0:
            precio_actual = cp
            variacion_pct = ((cp - pc) / pc) * 100.0
            return round(precio_actual, 4), round(variacion_pct, 4)

    # Fuente 3 (red): fast_info — solo si no hay datos precargados
    # Usado por obtener_variaciones_rapidas (llamadas paralelas sin datos previos)
    try:
        fi = yf.Ticker(ticker, session=_session).fast_info
        last_p = _safe_float(getattr(fi, 'last_price', None))
        prev_c = _safe_float(getattr(fi, 'previous_close', None))
        if last_p and last_p > 0 and prev_c and prev_c > 0:
            precio_actual = last_p
            variacion_pct = ((last_p - prev_c) / prev_c) * 100.0
            return round(precio_actual, 4), round(variacion_pct, 4)
    except Exception:
        pass

    return precio_actual, variacion_pct


def get_closing_variation(df_historico: Optional[pd.DataFrame] = None,
                          ticker: str = "",
                          fundamentales: Optional[Dict] = None) -> Tuple[float, float]:
    """
    CONTRATO CIERRE DE SESIÓN (Ranking & Oportunidad Técnica)
    ==========================================================
    Calcula la variación entre las dos últimas sesiones completas cerradas del histórico.

    Fórmula:
      variacion_sesion_cierre_pct = ((cierre_hoy - cierre_ayer) / cierre_ayer) * 100.0

    Destinado explícitamente al Ranking Cuantitativo y análisis de Oportunidad Técnica.

    Retorna:
    --------
    Tuple (precio_cierre: float, variacion_sesion_cierre_pct: float)
    """
    precio_cierre = 0.0
    variacion_pct = 0.0

    if df_historico is not None and not df_historico.empty:
        try:
            if isinstance(df_historico.columns, pd.MultiIndex):
                col = df_historico['Close'] if 'Close' in df_historico.columns else None
            else:
                col = df_historico['Close'] if 'Close' in df_historico.columns else None

            if col is not None:
                precios = col.dropna()
                if len(precios) >= 2:
                    precio_cierre = float(precios.iloc[-1])
                    precio_ant    = float(precios.iloc[-2])
                    if precio_ant > 0:
                        variacion_pct = ((precio_cierre - precio_ant) / precio_ant) * 100.0
                    return round(precio_cierre, 4), round(variacion_pct, 4)
                elif len(precios) == 1:
                    precio_cierre = float(precios.iloc[-1])
                    return round(precio_cierre, 4), 0.0
        except Exception:
            pass

    # Fallback si no hay df_historico: recurrir a variacion intradía
    return get_intraday_variation(ticker, fundamentales=fundamentales)


# ═══════════════════════════════════════════════════════════════════
# 🛠️ UTILIDADES DE DIAGNÓSTICO
# ═══════════════════════════════════════════════════════════════════

def diagnosticar_ticker(ticker: str) -> Dict:
    """
    Diagnóstico completo de disponibilidad de datos para un ticker.
    Útil para depuración y vista de configuración.
    """
    resultado = {
        'ticker': ticker,
        'yahoo':   {'disponible': False, 'calidad': 'incomplete'},
        'fmp':     {'disponible': False, 'calidad': 'incomplete'},
        'finnhub': {'disponible': False, 'calidad': 'incomplete'},
        'fuente_usada': 'fallback',
    }

    dy = _obtener_yahoo(ticker)
    if dy and _tiene_datos_utiles(dy):
        resultado['yahoo'] = {'disponible': True, 'calidad': dy.get('_data_quality', 'real')}
        resultado['fuente_usada'] = 'yahoo'

    if FMP_API_KEY:
        df = _obtener_fmp(ticker)
        if df and _tiene_datos_utiles(df):
            resultado['fmp'] = {'disponible': True, 'calidad': df.get('_data_quality', 'real')}
            if resultado['fuente_usada'] == 'fallback':
                resultado['fuente_usada'] = 'fmp'

    if FINNHUB_API_KEY:
        dfh = _obtener_finnhub(ticker)
        if dfh and _tiene_datos_utiles(dfh):
            resultado['finnhub'] = {'disponible': True, 'calidad': dfh.get('_data_quality', 'real')}
            if resultado['fuente_usada'] == 'fallback':
                resultado['fuente_usada'] = 'finnhub'

    return resultado
