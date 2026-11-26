import yfinance as yf
import streamlit as st
import pandas as pd
import requests

_yf_session = requests.Session()
_yf_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0'
})


@st.cache_data(ttl=300, show_spinner=False)
def get_stock_data(ticker: str) -> dict:
    """
    Obtiene los datos de mercado de un ticker usando yfinance.
    Utiliza @st.cache_data con TTL de 5 minutos para evitar llamadas repetidas.
    Retorna un diccionario con:
    - precio_actual
    - variacion_diaria (en porcentaje)
    - maximo_52_semanas
    - minimo_52_semanas
    - capitalizacion
    - moneda
    
    Lanza ValueError si el ticker es inválido o no se puede conectar.
    """
    ticker = ticker.upper().strip()
    
    try:
        t = yf.Ticker(ticker, session=_yf_session)
        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}
        
        # Para tickers inválidos, yfinance suele devolver un diccionario casi vacío
        current_price = info.get('currentPrice') or info.get('regularMarketPrice')
        previous_close = info.get('previousClose')
        
        # Fallback 1: fast_info (mucho más ligero y resistente a rate-limiting)
        if current_price is None:
            try:
                fi = t.fast_info
                current_price = getattr(fi, 'last_price', None)
                previous_close = getattr(fi, 'previous_close', None)
            except Exception:
                pass

        # Fallback 2: get_intraday_variation (aprovecha la caché en memoria del batch descargado)
        if current_price is None:
            try:
                from services.data_provider import get_intraday_variation
                cp, var = get_intraday_variation(ticker)
                if cp and cp > 0:
                    current_price = cp
                    if var is not None and previous_close is None:
                        previous_close = cp / (1.0 + (var / 100.0)) if var != -100 else cp
            except Exception:
                pass

        if current_price is None:
            raise ValueError(f"No se pudieron obtener datos para el ticker '{ticker}'. Puede que sea inválido o no tenga cotización actual.")
            
        # Calcular variación diaria
        variacion_diaria = 0.0
        if previous_close and previous_close > 0:
            variacion_diaria = ((current_price - previous_close) / previous_close) * 100.0
        
        # Blindaje contra valores no numéricos (strings "N/A", None, etc.)
        max_52_raw = info.get('fiftyTwoWeekHigh') if info else None
        min_52_raw = info.get('fiftyTwoWeekLow') if info else None
        
        # Fallback para 52 semanas desde fast_info
        if max_52_raw is None:
            try:
                max_52_raw = getattr(t.fast_info, 'year_high', None)
                min_52_raw = getattr(t.fast_info, 'year_low', None)
            except Exception:
                pass

        # Validación estricta de tipo antes de asignar
        maximo_52_semanas = float(max_52_raw) if isinstance(max_52_raw, (int, float)) and not pd.isna(max_52_raw) else None
        minimo_52_semanas = float(min_52_raw) if isinstance(min_52_raw, (int, float)) and not pd.isna(min_52_raw) else None
            
        cap_val = info.get('marketCap') if info else None
        if cap_val is None:
            try:
                cap_val = getattr(t.fast_info, 'market_cap', None)
            except Exception:
                pass

        curr_val = info.get('currency', 'EUR') if info else 'EUR'
        if not curr_val:
            try:
                curr_val = getattr(t.fast_info, 'currency', 'EUR')
            except Exception:
                curr_val = 'EUR'

        return {
            "precio_actual": current_price,
            "variacion_diaria": variacion_diaria,
            "previous_close": previous_close,
            "maximo_52_semanas": maximo_52_semanas,
            "minimo_52_semanas": minimo_52_semanas,
            "capitalizacion": cap_val,
            "moneda": curr_val
        }
        
    except ValueError:
        # Re-lanzar ValueError explícitos (ej. ticker no encontrado arriba)
        raise
    except Exception as e:
        # Envolver errores de conexión o parsing de yfinance
        raise ValueError(f"Error de conexión o de yfinance al obtener datos de '{ticker}': {e}")


def _fetch_price_history_uncached(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Obtiene los datos históricos directamente de yfinance sin aplicar caché."""
    try:
        t = yf.Ticker(ticker, session=_yf_session)
        df = t.history(period=period, interval=interval)
        
        if df.empty:
            raise ValueError(f"No se pudieron obtener datos históricos para el ticker '{ticker}'. Puede que sea inválido o no tenga cotización en el período '{period}' con intervalo '{interval}'.")
            
        return df
        
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Error de conexión o de yfinance al obtener el histórico de '{ticker}': {e}")


@st.cache_data(ttl=300, show_spinner=False)
def _get_price_history_intraday(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Caché con TTL corto (5 minutos) para datos intradía (5m, 30m)."""
    return _fetch_price_history_uncached(ticker, period, interval)


@st.cache_data(ttl=43200, show_spinner=False)
def _get_price_history_daily(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Caché con TTL largo (12 horas) para datos históricos diarios (1d)."""
    return _fetch_price_history_uncached(ticker, period, interval)


def get_price_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """
    Obtiene el histórico de precios de un ticker usando yfinance.
    Utiliza caché adaptativa:
    - 5 minutos para intervalos intradía (5m, 30m).
    - 12 horas para intervalos diarios (1d).
    
    Parámetros:
    - ticker: Ticker de la empresa (ej. 'AAPL', 'MSFT').
    - period: Período de tiempo ('1d', '5d', '1mo', '3mo', '6mo', '1y', 'ytd', '5y').
    - interval: Intervalo entre registros ('5m', '30m', '1d').
    
    Retorna:
    - Un DataFrame de pandas con el histórico de precios.
    
    Lanza ValueError si el período o el intervalo es inválido, si el ticker es inválido o no se puede conectar.
    """
    allowed_periods = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "ytd", "5y"}
    allowed_intervals = {"5m", "30m", "1d"}
    
    if period not in allowed_periods:
        raise ValueError(f"Período '{period}' no es válido. Los períodos permitidos son: {', '.join(sorted(allowed_periods))}")
    if interval not in allowed_intervals:
        raise ValueError(f"Intervalo '{interval}' no es válido. Los intervalos permitidos son: {', '.join(sorted(allowed_intervals))}")
        
    ticker = ticker.upper().strip()
    
    if interval in ("5m", "30m"):
        return _get_price_history_intraday(ticker, period, interval)
    else:
        return _get_price_history_daily(ticker, period, interval)


