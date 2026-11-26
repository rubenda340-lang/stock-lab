import html as _html
import math
import streamlit as st
from database import (
    query_all,
    agregar_a_watchlist,
    eliminar_de_watchlist,
    existe_en_watchlist,
    obtener_watchlist,
    calcular_posiciones_abiertas_filtradas,
)
import market_data
import numpy as np
import pandas as pd
from components.stock_chart import render_stock_chart
import yfinance as yf
from utils.scoring import calcular_score_completo, generar_resumen_fundamental
from services.data_provider import get_fundamentals, _normalizar_dividend_yield
import requests
import concurrent.futures
import datetime as _dt
import json
import os
import time as _time

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


# ── CACHÉ PERSISTENTE DE FUNDAMENTALES EN DISCO ──────────────────────
# Almacena respuestas válidas de Yahoo Finance como JSON locales.
# Evita depender de Yahoo en cada clic del usuario y permite arranques instantáneos.
_FUND_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "cache", "fundamentals")
_FUND_DISK_TTL = 43200  # 12 horas


def _ticker_filename(ticker):
    """Convierte un ticker a nombre de archivo seguro: BBVA.MC -> BBVA_MC.json"""
    return ticker.replace('.', '_').replace('/', '_').replace('\\', '_') + '.json'


def _obtener_clase_sector(sec: str) -> tuple[str, str]:
    """Retorna la clase CSS de la pastilla y el nombre de sector normalizado."""
    sec_str = sec or "General"
    sec_lower = sec_str.lower()
    if any(k in sec_lower for k in ["tecno", "software", "comunic", "tech"]):
        return "sec-tech", sec_str
    if any(k in sec_lower for k in ["finan", "banc", "segur", "bank"]):
        return "sec-fin", sec_str
    if any(k in sec_lower for k in ["salud", "farma", "biotec", "health"]):
        return "sec-health", sec_str
    if any(k in sec_lower for k in ["energ", "petr", "gas", "renov"]):
        return "sec-energy", sec_str
    if any(k in sec_lower for k in ["consum", "retail", "lujo", "aliment"]):
        return "sec-consume", sec_str
    return "sec-default", sec_str


def _leer_cache_disco_fund(ticker, ignore_ttl=False):
    """Lee datos fundamentales del caché en disco. Retorna dict o None."""
    try:
        filepath = os.path.join(_FUND_CACHE_DIR, _ticker_filename(ticker))
        if not os.path.exists(filepath):
            return None
        if not ignore_ttl:
            mtime = os.path.getmtime(filepath)
            if (_time.time() - mtime) > _FUND_DISK_TTL:
                return None
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _guardar_cache_disco_fund(ticker, data):
    """Guarda datos fundamentales en caché de disco (JSON)."""
    try:
        os.makedirs(_FUND_CACHE_DIR, exist_ok=True)
        filepath = os.path.join(_FUND_CACHE_DIR, _ticker_filename(ticker))
        data_limpia = {}
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool, type(None), list, dict)):
                data_limpia[k] = v
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data_limpia, f, ensure_ascii=False, default=str)
    except Exception:
        pass


def _enriquecer_con_fast_info(ticker_obj, info):
    """
    Complementa el dict info con datos de fast_info para campos clave ausentes.
    fast_info es un endpoint ligero de yfinance que a veces reporta datos
    que el endpoint principal .info omite (ej: marketCap para FCC, Prisa, Deoleo).
    """
    try:
        fi = ticker_obj.fast_info
        # Capitalización bursátil
        if info.get('marketCap') is None:
            mc = getattr(fi, 'market_cap', None)
            if mc is not None and isinstance(mc, (int, float)) and mc > 0:
                info['marketCap'] = mc
        # Rango 52 semanas
        if info.get('fiftyTwoWeekHigh') is None:
            yh = getattr(fi, 'year_high', None)
            if yh is not None and isinstance(yh, (int, float)):
                info['fiftyTwoWeekHigh'] = yh
        if info.get('fiftyTwoWeekLow') is None:
            yl = getattr(fi, 'year_low', None)
            if yl is not None and isinstance(yl, (int, float)):
                info['fiftyTwoWeekLow'] = yl
        # Precio actual
        if info.get('currentPrice') is None and info.get('regularMarketPrice') is None:
            lp = getattr(fi, 'last_price', None)
            if lp is not None and isinstance(lp, (int, float)):
                info['currentPrice'] = lp
        # Cierre anterior
        if info.get('previousClose') is None:
            pc = getattr(fi, 'previous_close', None)
            if pc is not None and isinstance(pc, (int, float)):
                info['previousClose'] = pc
    except Exception:
        pass  # fast_info puede fallar en tickers muy raros; no es crítico
    return info


def _format_cap(valor):
    """Formatea capitalización bursátil en formato legible."""
    if not valor:
        return "N/A"
    if valor >= 1_000_000_000_000:
        return f"{valor/1_000_000_000_000:.2f} T"
    elif valor >= 1_000_000_000:
        return f"{valor/1_000_000_000:.2f} B"
    elif valor >= 1_000_000:
        return f"{valor/1_000_000:.2f} M"
    return f"{valor:,.2f}"


def _fmt_price(precio, suffix="", use_comma=False) -> str:
    """Formatea un precio con decimales adaptativos según la magnitud.
    - precio < 0.01  → 4 decimales (penny stocks tipo Urbas: 0.0021)
    - precio < 1.0   → 4 decimales
    - precio >= 1.0  → 2 decimales
    """
    if precio is None:
        return "—"
    precio_f = float(precio)
    decimals = 4 if abs(precio_f) < 1.0 else 2
    if use_comma:
        return f"{precio_f:,.{decimals}f}{suffix}"
    return f"{precio_f:.{decimals}f}{suffix}"


def _cargar_empresas():
    """Carga las empresas activas de la base de datos como lista de diccionarios."""
    return query_all("""
        SELECT id, nombre, ticker, mercado, sector, es_ibex35, descripcion
        FROM empresas
        WHERE activa = 1
        ORDER BY nombre
    """)


def cambiar_empresa_callback(opcion_seleccionada):
    """Callback seguro para actualizar el selector de empresas desde botones del catálogo."""
    st.session_state["selector_global_empresas"] = opcion_seleccionada


def _mostrar_datos_mercado(ticker, precio_actual_calc=None, variacion_calc=None):
    """Obtiene y muestra datos de mercado de yfinance para un ticker."""
    try:
        with st.spinner("Obteniendo datos de mercado actualizados..."):
            datos = market_data.get_stock_data(ticker)

        moneda = datos.get('moneda', 'EUR')
        precio = precio_actual_calc if precio_actual_calc is not None else datos.get('precio_actual')
        variacion = variacion_calc if variacion_calc is not None else datos.get('variacion_diaria')

        str_precio = _fmt_price(precio, suffix=f" {moneda}") if precio is not None else "N/A"
        str_var = f"{variacion:+.2f}%" if variacion is not None else None

        st.metric("Precio Actual", str_precio, str_var)

    except ValueError as e:
        st.warning(f"No se pudieron obtener los datos de mercado: {str(e)}")
    except Exception as e:
        st.warning(f"Ocurrió un error inesperado al contactar con el mercado: {e}")


def _calcular_indicadores(df: pd.DataFrame) -> dict:
    """
    Calcula indicadores técnicos reales sobre serie diaria OHLCV (1A):
    - sma20 / sma50 (Series + último valor)
    - dist_sma50_pct: distancia porcentual del cierre a SMA 50
    - rsi14: RSI de 14 sesiones (0-100)
    - vol_20d_pct: volatilidad anualizada de 20 sesiones (%)
    - pos_52s_pct: posición del precio actual dentro del rango mín-máx de 52 semanas (0-100%)
    """
    resultado = {
        "sma20": None,
        "sma50": None,
        "sma20_val": None,
        "sma50_val": None,
        "dist_sma50_pct": None,
        "rsi14": None,
        "vol_20d_pct": None,
        "pos_52s_pct": None,
    }
    if df is None or df.empty or "Close" not in df.columns:
        return resultado

    close = pd.to_numeric(df["Close"], errors="coerce").dropna()
    if len(close) < 15:
        return resultado

    precio_act = float(close.iloc[-1])

    if len(close) >= 20:
        sma20 = close.rolling(window=20).mean()
        resultado["sma20"] = sma20
        val20 = float(sma20.iloc[-1])
        if not math.isnan(val20) and val20 > 0:
            resultado["sma20_val"] = val20

        rets = close.pct_change().dropna().tail(20)
        if len(rets) >= 10:
            vol = float(rets.std() * math.sqrt(252.0) * 100.0)
            if not math.isnan(vol) and not math.isinf(vol):
                resultado["vol_20d_pct"] = vol

    if len(close) >= 50:
        sma50 = close.rolling(window=50).mean()
        resultado["sma50"] = sma50
        val50 = float(sma50.iloc[-1])
        if not math.isnan(val50) and val50 > 0:
            resultado["sma50_val"] = val50
            resultado["dist_sma50_pct"] = ((precio_act - val50) / val50) * 100.0

    # RSI-14
    delta = close.diff()
    gain = float(delta.where(delta > 0, 0.0).rolling(window=14).mean().iloc[-1])
    loss = float((-delta.where(delta < 0, 0.0)).rolling(window=14).mean().iloc[-1])
    if not (math.isnan(gain) or math.isnan(loss)):
        if loss > 0:
            resultado["rsi14"] = 100.0 - (100.0 / (1.0 + (gain / loss)))
        elif gain > 0:
            resultado["rsi14"] = 100.0
        else:
            resultado["rsi14"] = 50.0

    # Posición en rango 52 semanas (últimas ~252 sesiones)
    ventana_52 = close.tail(252)
    min_52 = float(ventana_52.min())
    max_52 = float(ventana_52.max())
    if max_52 > min_52:
        resultado["pos_52s_pct"] = max(0.0, min(100.0, ((precio_act - min_52) / (max_52 - min_52)) * 100.0))

    return resultado


def _mostrar_grafico_historico(ticker, df, periodo_seleccionado, extra_series=None):
    """Muestra el gráfico de precios históricos para un ticker. Componente Unificado."""
    if df is None or df.empty:
        st.info("No hay datos históricos disponibles para el periodo seleccionado.")
        return

    try:
        df_df: pd.DataFrame = df.dropna(subset=['Close']).copy()
        if df_df.empty:
            st.info("No hay datos válidos tras limpiar valores vacíos.")
            return

        # Obtener moneda
        try:
            _cache_key_m = f"_datos_mercado_{ticker}"
            _dm_m = st.session_state.get(_cache_key_m) or market_data.get_stock_data(ticker) or {}
            moneda = _dm_m.get("moneda", "EUR")
        except Exception:
            moneda = "EUR"

        # Formateo de fechas para Hover (eje X en el tooltip)
        def formatear_fecha_hover(dt):
            if periodo_seleccionado in ["1D", "5D"]:
                return dt.strftime('%d %b, %H:%M')
            else:
                return dt.strftime('%d %b, %Y')

        df_df['Fecha_Hover'] = df_df.index.map(formatear_fecha_hover)

        # Ticks del Eje X espaciados y limpios (5 ticks)
        n_ticks = 5
        indices = [int(i * (len(df_df) - 1) / (n_ticks - 1)) for i in range(n_ticks)] if len(df_df) > 1 else [0]
        indices = sorted(list(set(indices)))

        tick_labels_map = {}
        for idx in indices:
            dt = df_df.index[idx]
            label_hover = df_df['Fecha_Hover'].iloc[idx]
            if periodo_seleccionado == "1D":
                text = dt.strftime('%H:%M')
            elif periodo_seleccionado == "5D":
                text = dt.strftime('%d %b %H:%M')
            elif periodo_seleccionado in ["1M", "6M", "YTD"]:
                text = dt.strftime('%d %b')
            else:  # 5Y
                text = dt.strftime('%Y')
            tick_labels_map[label_hover] = text

        precio_actual = float(df_df['Close'].iloc[-1])
        ref_val = float(df_df['Close'].iloc[-1])

        ref_inicio = float(df_df['Open'].iloc[0]) if periodo_seleccionado == "1D" else float(df_df['Close'].iloc[0])
        variacion_periodo = ((precio_actual - ref_inicio) / ref_inicio) * 100.0 if ref_inicio != 0 else 0.0

        color_linea = "#10B981" if variacion_periodo >= 0 else "#EF4444"

        render_stock_chart(
            labels=df_df['Fecha_Hover'].tolist(),
            prices=df_df['Close'].tolist(),
            last_close=ref_val,
            line_color=color_linea,
            tick_labels_map=tick_labels_map,
            currency=moneda,
            ticker=ticker,
            title=f"Histórico {ticker}",
            height=420,
            extra_series=extra_series,
        )

    except Exception as e:
        st.warning(f"Ocurrió un error inesperado al renderizar el gráfico: {e}")


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_info_yahoo(ticker):
    """
    Descarga info desde Yahoo Finance y enriquece con fast_info.
    Lanza excepción si la respuesta está vacía para que @st.cache_data
    NO almacene resultados fallidos (Streamlit no cachea excepciones).
    """
    t = yf.Ticker(ticker, session=_yf_session)
    info = t.info

    # Validar que la respuesta contiene datos mínimos de mercado
    if not info or len(info) < 5:
        raise ValueError(f"Yahoo devolvió respuesta vacía para {ticker}")

    tiene_precio = (info.get('regularMarketPrice') or
                    info.get('currentPrice') or
                    info.get('previousClose'))
    if not tiene_precio:
        raise ValueError(f"Yahoo devolvió respuesta sin datos de precio para {ticker}")

    # Enriquecer con fast_info para campos que .info omite
    info = _enriquecer_con_fast_info(t, info)
    return info


def _obtener_info_completa(ticker, force_refresh=False):
    """
    Obtiene toda la información de un ticker con caché multinivel resiliente:
      1. Disco local JSON (TTL 12 h) — respuesta instantánea (~1ms) sin depender de Yahoo
      2. Yahoo Finance en vivo + fast_info — si no hay caché o ha expirado
      3. Disco stale sin TTL — fallback de emergencia si Yahoo falla o da timeout
    """
    # Nivel 1: Caché en disco válido (< 12h) para respuesta inmediata
    if not force_refresh:
        disk_data = _leer_cache_disco_fund(ticker)
        if disk_data:
            return disk_data

    # Nivel 2: Consulta a Yahoo Finance en vivo + enriquecimiento fast_info
    try:
        info = _fetch_info_yahoo(ticker)
        # Éxito → persistir en disco para futuras sesiones y clics
        _guardar_cache_disco_fund(ticker, info)
        return info
    except Exception:
        pass

    # Nivel 3: Fallback de emergencia — datos en disco aunque hayan superado el TTL
    disk_stale = _leer_cache_disco_fund(ticker, ignore_ttl=True)
    if disk_stale:
        return disk_stale

    return {}


def _generar_resumen_corto(info, sector):
    """Genera un resumen en español de una línea para la empresa."""
    # Usamos campos de yfinance disponibles para construir un resumen siempre en español
    pais = info.get('country', '')
    empleados = info.get('fullTimeEmployees')
    industria = info.get('industry', '')

    partes = []
    if sector and sector != '—':
        partes.append(f"Compañía cotizada en el sector de {sector.lower()}")
    if industria:
        partes.append(f"especializada en {industria.lower()}")
    if pais:
        partes.append(f"con sede en {pais}")
    if empleados:
        partes.append(f"y una plantilla de {empleados:,} empleados".replace(',', '.'))

    if partes:
        return '. '.join([' '.join(partes)]) + '.'
    return "Ficha de cotización de mercado, histórico y análisis fundamental unificado."


def _extraer_dominio(url_str):
    """Extrae el dominio base de una URL para el buscador de logotipos Clearbit."""
    if not url_str:
        return None
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(url_str)
        domain = parsed.netloc or parsed.path
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return None


def _generar_iniciales(nombre):
    """Genera un fallback de iniciales de 2 caracteres a partir de un nombre."""
    if not nombre:
        return "SL"
    palabras = nombre.strip().split()
    if len(palabras) >= 2:
        return (palabras[0][0] + palabras[1][0]).upper()
    return nombre[:2].upper()


def _get_currency_symbol(curr_code):
    """Devuelve el símbolo monetario de forma elegante."""
    if curr_code == 'EUR':
        return '€'
    elif curr_code == 'USD':
        return '$'
    elif curr_code == 'GBP':
        return '£'
    return f" {curr_code}"


def _mostrar_ficha(empresa):
    """Muestra la ficha detallada de una empresa seleccionada."""
    ticker = empresa['ticker']
    nombre_empresa = empresa['nombre']
    mercado_val = empresa.get('mercado') or '—'
    sector_val = empresa.get('sector') or '—'
    ibex_val = bool(empresa.get('es_ibex35', False))

    # ── Carga paralela: info extendida + data_provider + mercado + noticias ──
    _cache_key = f"_datos_mercado_{ticker}"
    with st.spinner(f"Sincronizando análisis y cotización de {nombre_empresa}..."):
        def _fetch_noticias_safe(nom, tk):
            try:
                from services.news_sentiment import obtener_noticias
                return obtener_noticias(nom, tk, max_items=6)
            except Exception:
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            fut_info    = executor.submit(_obtener_info_completa, ticker)
            fut_dp      = executor.submit(get_fundamentals, ticker)
            fut_mercado = executor.submit(market_data.get_stock_data, ticker)
            fut_news    = executor.submit(_fetch_noticias_safe, nombre_empresa, ticker)
            try:
                ticker_info = fut_info.result(timeout=25)
            except Exception:
                ticker_info = {}
            try:
                _info_dp = fut_dp.result(timeout=15) or {}
            except Exception:
                _info_dp = {}
            try:
                datos_mercado_base = fut_mercado.result(timeout=10) or {}
            except Exception:
                datos_mercado_base = {}
            try:
                noticias_empresa = fut_news.result(timeout=4) or []
            except Exception:
                noticias_empresa = []

    # Guardar en session_state para reutilizar sin rellamar a la API
    st.session_state[_cache_key] = datos_mercado_base
    info_scoring = _info_dp if (_info_dp and _info_dp.get("_data_source") != "fallback") else ticker_info

    # Contexto personal local (0 llamadas de red): ¿tiene posición abierta en Mi Cartera?
    cartera_badge_html = ""
    try:
        tk_u = ticker.strip().upper()
        partes_cart = []
        for tipo_c in ("REAL", "SIMULADA"):
            for p_c in calcular_posiciones_abiertas_filtradas(tipo_c) or []:
                if str(p_c.get("ticker") or "").strip().upper() == tk_u:
                    partes_cart.append(f"{float(p_c.get('acciones') or 0.0):.2f} acc. ({tipo_c.capitalize()})")
        if partes_cart:
            det_c = " · ".join(partes_cart)
            cartera_badge_html = (
                f"<span class='meta-badge' style='background:rgba(99,102,241,0.14);color:#a5b4fc;"
                f"border:1px solid rgba(129,140,248,0.34);' title='Posición abierta en Mi Cartera: {det_c}'>"
                f"En Cartera · {partes_cart[0]}</span>"
            )
    except Exception:
        cartera_badge_html = ""

    # Generar resumen breve (una línea)
    resumen_corto = _generar_resumen_corto(ticker_info, sector_val)

    # ── BOTÓN VOLVER AL CATÁLOGO ─────────────────────────────────────────
    def _volver_al_catalogo_cb():
        st.session_state["selector_global_empresas"] = "--- Seleccione una empresa ---"

    col_back, _ = st.columns([2.5, 7.5])
    with col_back:
        st.button(
            "← Volver al Catálogo",
            key=f"btn_back_catalog_{ticker}",
            on_click=_volver_al_catalogo_cb,
        )

    # ── CABECERA PREMIUM — Ticker · Nombre · Badges (sin logo) ───────────
    ibex_badge_html = "<span class='meta-badge ibex-badge'>IBEX 35</span>" if ibex_val else ""
    sec_cls, sec_txt = _obtener_clase_sector(sector_val)

    st.markdown(
        f"<div class='empresa-header-card'>"
        f"  <div class='empresa-header-top'>"
        f"    <span class='ticker-badge'>{ticker}</span>"
        f"    <span class='empresa-nombre'>{_html.escape(str(nombre_empresa))}</span>"
        f"    {ibex_badge_html}"
        f"    {cartera_badge_html}"
        f"  </div>"
        f"  <div class='empresa-subline'>{_html.escape(str(resumen_corto))}</div>"
        f"  <div class='empresa-header-meta'>"
        f"    <span class='meta-badge'>{_html.escape(str(mercado_val))}</span>"
        f"    <span class='emp-sec-pill {sec_cls}'>{_html.escape(str(sec_txt))}</span>"
        f"  </div>"
        f"</div>",
        unsafe_allow_html=True
    )


    # ── DESCRIPCIÓN (colapsada por defecto) ───────────────────────────────
    # Prioridad: base de datos → longBusinessSummary de yfinance (siempre en inglés, lo avisamos)
    desc_bbdd = empresa.get('descripcion')
    desc_yf   = ticker_info.get('longBusinessSummary', '')
    desc_mostrar = desc_bbdd or desc_yf or None

    if desc_mostrar:
        with st.expander("Ver descripción de la empresa", expanded=False):
            # Nota: yfinance solo devuelve descripciones en inglés; mostramos aviso si aplica
            if not desc_bbdd and desc_yf:
                st.caption("La descripción ha sido obtenida de fuentes externas y puede estar en inglés.")
            st.markdown(
                f"<p class='empresa-desc'>{_html.escape(str(desc_mostrar))}</p>",
                unsafe_allow_html=True
            )

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── FILA DE INDICADORES CLAVE (METRICS STRIP UNIFICADA CON DATA_PROVIDER) ──
    curr_symbol = _get_currency_symbol(ticker_info.get('currency', 'EUR'))

    def _num_val(d1, d2, key):
        for src in (d1, d2):
            v = (src or {}).get(key)
            if isinstance(v, (int, float)) and not (math.isnan(v) or math.isinf(v)):
                return float(v)
        return None

    cap_raw = _num_val(info_scoring, ticker_info, 'marketCap')
    cap_val = _format_cap(cap_raw) if (cap_raw is not None and cap_raw > 0) else "N/A"

    # PER: trailingPE positivo preferido, fallback a forwardPE positivo (evita N/A si trailingPE < 0)
    t_pe = _num_val(info_scoring, ticker_info, 'trailingPE')
    f_pe = _num_val(info_scoring, ticker_info, 'forwardPE')
    if t_pe is not None and t_pe > 0:
        pe_val = f"{t_pe:.2f}"
        pe_lbl = "PER"
    elif f_pe is not None and f_pe > 0:
        pe_val = f"{f_pe:.2f}"
        pe_lbl = "PER (Fwd)"
    else:
        pe_val = "N/A"
        pe_lbl = "PER"

    # Dividendo: info_scoring ya viene normalizado a decimal (0.04 = 4%) desde data_provider;
    # usar _normalizar_dividend_yield solo sobre ticker_info crudo como fallback.
    div_dec = _num_val(info_scoring, None, 'dividendYield')
    if div_dec is not None and div_dec > 0.35:
        div_dec = div_dec / 100.0
    if div_dec is None or div_dec <= 0:
        div_dec = _normalizar_dividend_yield(ticker_info)
    if div_dec is not None and div_dec > 0:
        div_val = f"{div_dec * 100:.2f}%"
    else:
        div_val = "Sin dividendo"

    # Beta
    beta_raw = _num_val(info_scoring, ticker_info, 'beta')
    beta_val = f"{beta_raw:.2f}" if beta_raw is not None else "N/A"

    # Rango 52 semanas
    low_52 = _num_val(ticker_info, info_scoring, 'fiftyTwoWeekLow')
    high_52 = _num_val(ticker_info, info_scoring, 'fiftyTwoWeekHigh')
    rango_52 = (
        f"{_fmt_price(low_52, suffix=curr_symbol)} – {_fmt_price(high_52, suffix=curr_symbol)}"
        if (low_52 is not None and high_52 is not None and low_52 > 0 and high_52 > 0)
        else "N/A"
    )

    # ROE normalizado consistentemente con Ranking V8.5 (corrige >=100% y 0.0%)
    try:
        from views.ranking import _normalizar_ratio_pct as _norm_pct_rnk
    except Exception:
        _norm_pct_rnk = lambda v, i=None: (float(v) * 100.0 if abs(float(v)) <= 5.0 else float(v)) if v is not None else None

    roe_pct = _norm_pct_rnk(info_scoring.get('returnOnEquity'), info_scoring)
    if roe_pct is None:
        roe_pct = _norm_pct_rnk(ticker_info.get('returnOnEquity'), ticker_info)
    roe_val = f"{roe_pct:.2f}%" if roe_pct is not None else "N/A"

    st.markdown(
        f"<div class='metrics-strip'>"
        f"  <div class='metric-mini-card'>"
        f"    <span class='metric-mini-label'>Capitalización</span>"
        f"    <span class='metric-mini-value'>{cap_val}</span>"
        f"  </div>"
        f"  <div class='metric-mini-card'>"
        f"    <span class='metric-mini-label'>{pe_lbl}</span>"
        f"    <span class='metric-mini-value'>{pe_val}</span>"
        f"  </div>"
        f"  <div class='metric-mini-card'>"
        f"    <span class='metric-mini-label'>Dividendo</span>"
        f"    <span class='metric-mini-value'>{div_val}</span>"
        f"  </div>"
        f"  <div class='metric-mini-card'>"
        f"    <span class='metric-mini-label'>Beta</span>"
        f"    <span class='metric-mini-value'>{beta_val}</span>"
        f"  </div>"
        f"  <div class='metric-mini-card'>"
        f"    <span class='metric-mini-label'>52 Semanas</span>"
        f"    <span class='metric-mini-value'>{rango_52}</span>"
        f"  </div>"
        f"  <div class='metric-mini-card'>"
        f"    <span class='metric-mini-label'>ROE</span>"
        f"    <span class='metric-mini-value'>{roe_val}</span>"
        f"  </div>"
        f"</div>",
        unsafe_allow_html=True
    )

    st.markdown("<div style='height: 18px;'></div>", unsafe_allow_html=True)

    # ── BOTONES DE ACCIÓN EN FILA COMPACTA ────────────────────────────────
    ya_en_watchlist = existe_en_watchlist(ticker)
    col_btn_watchlist, col_btn_cartera, _ = st.columns([1.5, 1.5, 3])
    
    with col_btn_watchlist:
        if ya_en_watchlist:
            with st.popover("En Watchlist", width="stretch"):
                wl_det_html = ""
                try:
                    df_wl_cur = obtener_watchlist()
                    if df_wl_cur is not None and not df_wl_cur.empty:
                        fila_wl = df_wl_cur[df_wl_cur["ticker"].astype(str).str.upper() == ticker.strip().upper()]
                        if not fila_wl.empty:
                            r_wl = fila_wl.iloc[0]
                            p_obj_w = r_wl.get("precio_objetivo")
                            est_w = "Compra en caída" if r_wl.get("tipo_objetivo") == "COMPRA_EN_CAIDA" else "Compra por ruptura"
                            mot_w = _html.escape(str(r_wl.get("motivo") or "Sin motivo"))
                            obj_txt_w = f"{float(p_obj_w):.2f} € ({est_w})" if (p_obj_w is not None and not pd.isna(p_obj_w) and float(p_obj_w) > 0) else "Sin objetivo"
                            wl_det_html = (
                                f"<div style='font-size:0.78rem;color:#94a3b8;margin-bottom:10px;line-height:1.45;'>"
                                f"<b>Objetivo:</b> {obj_txt_w}<br><b>Motivo:</b> <i>{mot_w}</i></div>"
                            )
                except Exception:
                    wl_det_html = ""
                st.markdown(
                    f"<div style='font-family:Inter,sans-serif;font-size:0.84rem;color:#cbd5e1;margin-bottom:8px;'>"
                    f"<b>{_html.escape(str(nombre_empresa))}</b> ({ticker}) forma parte de tu Watchlist.</div>"
                    f"{wl_det_html}",
                    unsafe_allow_html=True,
                )
                if st.button("Quitar de Watchlist", key=f"btn_rm_wl_{ticker}", type="secondary", width="stretch"):
                    if eliminar_de_watchlist(ticker):
                        st.toast(f"{ticker} eliminado de la Watchlist")
                        st.rerun()
                    else:
                        st.error("No se pudo eliminar de la Watchlist")
        else:
            # Usar popover para mostrar el formulario al hacer clic (claves aisladas por ticker)
            with st.popover("Añadir a Watchlist", width="stretch"):
                st.markdown(
                    f"<div style='font-family:Plus Jakarta Sans,Inter,sans-serif;font-size:1.05rem;font-weight:700;color:#f8fafc;margin-bottom:8px;'>"
                    f"Añadir a Watchlist</div>"
                    f"<div style='font-family:JetBrains Mono,monospace;font-size:0.85rem;color:#38bdf8;margin-bottom:14px;'>"
                    f"{ticker} <span style='font-family:Inter,sans-serif;color:#94a3b8;'>· {_html.escape(str(nombre_empresa))}</span></div>",
                    unsafe_allow_html=True
                )

                # Opciones predefinidas de motivo
                motivos_predefinidos = [
                    "Esperar corrección para entrar",
                    "Seguimiento de dividendos",
                    "Ruptura de resistencia técnica",
                    "Noticias positivas recientes",
                    "Recomendación de analistas",
                    "Otro motivo..."
                ]

                motivo_seleccionado = st.selectbox(
                    "MOTIVO DE SEGUIMIENTO",
                    options=motivos_predefinidos,
                    index=None,
                    placeholder="Selecciona un motivo...",
                    key=f"wl_motivo_{ticker}",
                )

                # Campo personalizado si selecciona "Otro motivo..."
                motivo_personalizado = ""
                if motivo_seleccionado == "Otro motivo...":
                    motivo_personalizado = st.text_input(
                        "ESPECIFICA EL MOTIVO",
                        placeholder="Ej: Valor infravalorado, crecimiento esperado...",
                        key=f"wl_otro_{ticker}",
                    )

                # Precio objetivo opcional
                precio_objetivo_input = st.number_input(
                    "PRECIO OBJETIVO (OPCIONAL)",
                    min_value=0.0,
                    step=0.5,
                    format="%.2f",
                    help="Precio objetivo de entrada para activar alertas",
                    key=f"wl_obj_{ticker}",
                )

                # Selector de Estrategia de Objetivo
                tipo_estrat_label = st.radio(
                    "ESTRATEGIA DE OBJETIVO",
                    ["📉 Comprar cuando BAJE hasta este precio", "📈 Comprar cuando SUBA (Ruptura)"],
                    format_func=lambda x: x.replace("📉 ", "").replace("📈 ", ""),
                    index=0,
                    horizontal=True,
                    key=f"wl_estrat_{ticker}",
                )
                tipo_estrat = "COMPRA_EN_CAIDA" if "BAJE" in tipo_estrat_label else "COMPRA_EN_RUPTURA"

                # Aviso de coherencia estrategia vs precio actual
                _p_ref_wl = (datos_mercado_base or {}).get("precio_actual")
                if _p_ref_wl and precio_objetivo_input > 0:
                    try:
                        from views.watchlist import _validar_coherencia_objetivo
                        _av_wl, _dist_wl = _validar_coherencia_objetivo(_p_ref_wl, precio_objetivo_input, tipo_estrat)
                        if _av_wl:
                            st.warning(_av_wl)
                        elif _dist_wl:
                            st.caption(_dist_wl)
                    except Exception:
                        pass

                # Botón de confirmación
                if st.button("Confirmar Adición", key=f"wl_confirm_{ticker}", type="primary", width="stretch"):
                    motivo_final = (
                        motivo_personalizado.strip()
                        if motivo_seleccionado == "Otro motivo..."
                        else (motivo_seleccionado or "").strip()
                    )
                    if motivo_final:
                        ticker_final = ticker.split(" - ")[0].strip()
                        val_objetivo = precio_objetivo_input if precio_objetivo_input > 0 else None

                        try:
                            datos_mercado = datos_mercado_base or market_data.get_stock_data(ticker_final)
                            precio_actual = datos_mercado.get('precio_actual')

                            if precio_actual:
                                if agregar_a_watchlist(ticker_final, precio_actual, motivo_final, val_objetivo, tipo_estrat):
                                    st.success(f"{ticker_final} añadido a la watchlist")
                                    st.rerun()
                                else:
                                    st.error("Error al guardar en la base de datos")
                            else:
                                st.warning("No se pudo obtener el precio actual. Intenta de nuevo.")
                        except Exception as e:
                            st.error(f"Error técnico: {e}")
                    elif motivo_seleccionado == "Otro motivo...":
                        st.warning("Por favor, especifica un motivo personalizado")
                    else:
                        st.warning("Por favor, selecciona un motivo")

    with col_btn_cartera:
        if st.button("Añadir a Cartera", key=f"btn_cartera_{ticker}", width="stretch"):
            # Preseleccionar empresa, precio y acción para abrir directamente el modal de compra en Mi Cartera
            st.session_state['compra_preseleccionada'] = ticker
            st.session_state['accion_activa'] = 'comprar'
            _p_cartera = (datos_mercado_base or {}).get('precio_actual')
            if _p_cartera is not None and isinstance(_p_cartera, (int, float)) and _p_cartera > 0:
                st.session_state['precio_operacion_input'] = float(_p_cartera)
            st.session_state['menu_activo'] = "Mi Cartera"
            st.rerun()

    st.markdown("<div style='height: 24px;'></div>", unsafe_allow_html=True)

    opciones_periodo = ["1D", "5D", "1M", "6M", "YTD", "5Y"]

    # Mapeo de periodos a parámetros API (period, interval)
    mapeo_periodo = {
        "1D": ("1d", "5m"),
        "5D": ("5d", "30m"),
        "1M": ("1mo", "1d"),
        "6M": ("6mo", "1d"),
        "YTD": ("ytd", "1d"),
        "5Y": ("5y", "1d")
    }

    df = None
    variacion_calc = None
    precio_actual_calc = None

    es_fallback_diario = False
    try:
        with st.spinner("Cargando datos históricos..."):
            periodo_seleccionado = st.session_state.get(f"periodo_{empresa['ticker']}", "6M") or "6M"
            api_period, api_interval = mapeo_periodo[periodo_seleccionado]

            try:
                df = market_data.get_price_history(empresa['ticker'], period=api_period, interval=api_interval)
            except Exception as e:
                # Fallback robusto para valores de baja liquidez
                if periodo_seleccionado in ["1D", "5D"]:
                    df = market_data.get_price_history(empresa['ticker'], period="5d", interval="1d")
                    es_fallback_diario = True
                else:
                    raise e

        if df is not None and not df.empty:
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            df = df.dropna(subset=['Close'])

            if not df.empty:
                # Estructuración robusta del DataFrame intradía 1D (velas 5m):
                # - No desplazar 5D (son velas de 30m) ni fallback diario (5d/1d).
                # - Solo añadir el punto de subasta de cierre 17:35 si la sesión ya ha cerrado.
                if periodo_seleccionado == "1D" and not es_fallback_diario:
                    try:
                        _cache_key_inner = f"_datos_mercado_{empresa['ticker']}"
                        datos_mercado = st.session_state.get(_cache_key_inner) or market_data.get_stock_data(empresa['ticker'])
                        precio_subasta_real = datos_mercado.get('precio_actual')

                        if precio_subasta_real is not None:
                            precio_apertura_real = float(df['Open'].iloc[0])

                            df_shifted = df.copy()
                            df_shifted.index = df_shifted.index + pd.Timedelta(minutes=5)

                            partes_emp = []
                            idx_0900 = df_shifted.index[0] - pd.Timedelta(minutes=5)
                            partes_emp.append(pd.DataFrame({
                                'Open': [precio_apertura_real],
                                'High': [precio_apertura_real],
                                'Low': [precio_apertura_real],
                                'Close': [precio_apertura_real],
                                'Volume': [0]
                            }, index=[idx_0900]))
                            partes_emp.append(df_shifted)

                            last_ts = df_shifted.index[-1]
                            idx_1735 = last_ts.normalize() + pd.Timedelta(hours=17, minutes=35)
                            try:
                                from zoneinfo import ZoneInfo
                                ahora_mad = _dt.datetime.now(ZoneInfo("Europe/Madrid"))
                                sesion_cerrada = (
                                    last_ts.date() < ahora_mad.date()
                                    or ahora_mad.time() >= _dt.time(17, 35)
                                    or (last_ts.hour > 17 or (last_ts.hour == 17 and last_ts.minute >= 30))
                                )
                            except Exception:
                                sesion_cerrada = (last_ts.hour > 17 or (last_ts.hour == 17 and last_ts.minute >= 30))

                            if sesion_cerrada and last_ts < idx_1735:
                                partes_emp.append(pd.DataFrame({
                                    'Open': [precio_subasta_real],
                                    'High': [precio_subasta_real],
                                    'Low': [precio_subasta_real],
                                    'Close': [precio_subasta_real],
                                    'Volume': [0]
                                }, index=[idx_1735]))

                            df_grafico = pd.concat(partes_emp).sort_index()
                            df_grafico['Fecha_Visible'] = df_grafico.index.strftime('%m-%d %H:%M')

                            df = df_grafico
                    except Exception:
                        pass # Si falla, seguimos con datos históricos puros

                precio_actual_calc = float(df['Close'].iloc[-1])
                if periodo_seleccionado == "1D":
                    # Unificación con panel Destacadas: usar variacion_diaria oficial de la API (basada en Previous Close)
                    try:
                        _cache_key_inner = f"_datos_mercado_{empresa['ticker']}"
                        datos_mercado = st.session_state.get(_cache_key_inner) or market_data.get_stock_data(empresa['ticker'])
                        variacion_calc = datos_mercado.get('variacion_diaria')
                        if variacion_calc is None:
                            raise ValueError("Variación no disponible")
                    except Exception:
                        # Fallback manual con previous_close si falla
                        try:
                            _cache_key_inner = f"_datos_mercado_{empresa['ticker']}"
                            datos_mercado = st.session_state.get(_cache_key_inner) or market_data.get_stock_data(empresa['ticker'])
                            precio_ayer = datos_mercado.get('previous_close')
                            if precio_ayer and precio_ayer > 0:
                                variacion_calc = ((precio_actual_calc - precio_ayer) / precio_ayer) * 100.0
                            else:
                                raise ValueError("Previous close inválido")
                        except Exception:
                            apertura = float(df['Open'].iloc[0])
                            variacion_calc = ((precio_actual_calc - apertura) / apertura) * 100.0
                else:
                    primer_dato = float(df['Close'].iloc[0])
                    variacion_calc = ((precio_actual_calc - primer_dato) / primer_dato) * 100.0
    except Exception as e:
        st.warning(f"No se pudieron cargar los datos históricos para calcular la variación: {e}")

    # ── Cabecera compacta: Precio · Delta · Selector de periodo en una sola fila ──
    try:
        _cache_key_hdr = f"_datos_mercado_{empresa['ticker']}"
        datos_mercado_hdr = st.session_state.get(_cache_key_hdr) or market_data.get_stock_data(empresa['ticker'])
        moneda_hdr = datos_mercado_hdr.get('moneda', 'EUR')
        precio_hdr = precio_actual_calc if precio_actual_calc is not None else datos_mercado_hdr.get('precio_actual')
        variacion_hdr = variacion_calc if variacion_calc is not None else datos_mercado_hdr.get('variacion_diaria')
    except Exception:
        moneda_hdr, precio_hdr, variacion_hdr = 'EUR', None, None

    color_delta = "#34d399" if (variacion_hdr or 0) >= 0 else "#f87171"
    signo_delta = "+" if (variacion_hdr or 0) >= 0 else ""
    separador = "<span style='color:rgba(100,116,139,0.3); font-weight:300; margin:0 6px;'>│</span>"
    str_precio = (_fmt_price(precio_hdr, suffix=f" {moneda_hdr}", use_comma=True)
                 if precio_hdr is not None else "—")
    str_delta  = f"{signo_delta}{variacion_hdr:.2f}%" if variacion_hdr is not None else ""

    # Datos adicionales: Máx, Mín, Vol del PERIODO seleccionado (desde el DataFrame cargado)
    curr_sym_hdr = _get_currency_symbol(moneda_hdr)
    if df is not None and not df.empty:
        try:
            _df_clean = df.dropna(subset=['Close', 'High', 'Low'])
            period_high = float(_df_clean['High'].max()) if 'High' in _df_clean.columns else float(_df_clean['Close'].max())
            period_low  = float(_df_clean['Low'].min())  if 'Low'  in _df_clean.columns else float(_df_clean['Close'].min())
            period_vol  = int(_df_clean['Volume'].sum()) if 'Volume' in _df_clean.columns else None
            str_high = _fmt_price(period_high, suffix=curr_sym_hdr, use_comma=True)
            str_low  = _fmt_price(period_low,  suffix=curr_sym_hdr, use_comma=True)
            str_vol  = _format_cap(period_vol) if period_vol is not None else "N/A"
        except Exception:
            str_high = str_low = str_vol = "N/A"
    else:
        str_high = str_low = str_vol = "N/A"

    col_precio_hdr, col_periodo_hdr = st.columns([3.6, 2.2], vertical_alignment="center")
    with col_precio_hdr:
        st.markdown(
            f"<div style='display:flex; align-items:center; gap:6px; padding:2px 0; flex-wrap:wrap;'>"
            f"<span style='font-family:Inter,sans-serif; font-weight:600; font-size:1.05rem; "
            f"color:#94a3b8; white-space:nowrap; letter-spacing:0.3px;'>Datos de Mercado</span>"
            f"&nbsp;&nbsp;{separador}&nbsp;&nbsp;"
            f"<span style='font-family:JetBrains Mono,monospace; font-variant-numeric:tabular-nums; font-weight:700; font-size:1.25rem; "
            f"color:#f1f5f9; white-space:nowrap;'>{str_precio}</span>"
            f"&nbsp;&nbsp;{separador}&nbsp;&nbsp;"
            f"<span style='font-family:JetBrains Mono,monospace; font-variant-numeric:tabular-nums; font-weight:700; font-size:1.05rem; "
            f"color:{color_delta}; white-space:nowrap;'>{str_delta}</span>"
            f"&nbsp;&nbsp;{separador}&nbsp;&nbsp;"
            f"<span style='font-family:Inter,sans-serif; font-size:0.85rem; color:#6b7a8d; white-space:nowrap;'>"
            f"  <span style='color:#34d399; font-weight:700;'>▲</span> Máx: <span style='font-family:JetBrains Mono,monospace; color:#cbd5e1; font-weight:600;'>{str_high}</span>"
            f"</span>"
            f"&nbsp;&nbsp;{separador}&nbsp;&nbsp;"
            f"<span style='font-family:Inter,sans-serif; font-size:0.85rem; color:#6b7a8d; white-space:nowrap;'>"
            f"  <span style='color:#f87171; font-weight:700;'>▼</span> Mín: <span style='font-family:JetBrains Mono,monospace; color:#cbd5e1; font-weight:600;'>{str_low}</span>"
            f"</span>"
            f"&nbsp;&nbsp;{separador}&nbsp;&nbsp;"
            f"<span style='font-family:Inter,sans-serif; font-size:0.85rem; color:#6b7a8d; white-space:nowrap;'>"
            f"  Vol: <span style='font-family:JetBrains Mono,monospace; color:#cbd5e1; font-weight:600;'>{str_vol}</span>"
            f"</span>"
            f"</div>",
            unsafe_allow_html=True
        )
    with col_periodo_hdr:
        periodo_seleccionado = st.segmented_control(
            "Selector de Periodo",
            options=opciones_periodo,
            default="6M",
            key=f"periodo_{empresa['ticker']}",
            label_visibility="collapsed"
        )
        if not periodo_seleccionado:
            periodo_seleccionado = "6M"

    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    # ── Serie diaria 1A compartida (técnicos + overlays + Motor A) ──────────
    try:
        df_scoring = market_data.get_price_history(ticker, period="1y", interval="1d")
        if df_scoring is not None and not df_scoring.empty:
            df_scoring = df_scoring.dropna(subset=["Close"]).sort_index()
        else:
            df_scoring = df
    except Exception:
        df_scoring = df

    indicadores_tec = _calcular_indicadores(df_scoring)

    # ── Overlays técnicos opcionales (desactivados por defecto) ─────────────
    extra_series_chart = []
    if periodo_seleccionado in ("1M", "6M", "YTD", "5Y") and df is not None and not df.empty:
        ov_c1, ov_c2, _ = st.columns([1.1, 1.1, 5.8])
        with ov_c1:
            ver_sma20 = st.checkbox("SMA 20", value=False, key=f"emp_sma20_{ticker}")
        with ov_c2:
            ver_sma50 = st.checkbox("SMA 50", value=False, key=f"emp_sma50_{ticker}")

        if ver_sma20 or ver_sma50:
            src_sma = df if periodo_seleccionado == "5Y" else df_scoring
            if src_sma is not None and not src_sma.empty and "Close" in src_sma.columns:
                s_src = pd.to_numeric(src_sma["Close"], errors="coerce").dropna()
                idx_src = pd.to_datetime(s_src.index)
                if getattr(idx_src, "tz", None) is not None:
                    idx_src = idx_src.tz_localize(None)
                s_src = pd.Series(s_src.values, index=idx_src.normalize()).groupby(level=0).last().sort_index()

                idx_vis = pd.to_datetime(df.dropna(subset=["Close"]).index)
                if getattr(idx_vis, "tz", None) is not None:
                    idx_vis = idx_vis.tz_localize(None)
                idx_vis_norm = idx_vis.normalize()

                if ver_sma20 and len(s_src) >= 20:
                    s20 = s_src.rolling(20).mean().reindex(idx_vis_norm, method="ffill")
                    extra_series_chart.append({
                        "label": "SMA 20",
                        "data": [round(float(v), 4) if pd.notna(v) else None for v in s20.values],
                        "color": "#38bdf8",
                        "width": 1.4,
                    })
                if ver_sma50 and len(s_src) >= 50:
                    s50 = s_src.rolling(50).mean().reindex(idx_vis_norm, method="ffill")
                    extra_series_chart.append({
                        "label": "SMA 50",
                        "data": [round(float(v), 4) if pd.notna(v) else None for v in s50.values],
                        "color": "#f59e0b",
                        "dash": [6, 4],
                        "width": 1.5,
                    })

    # ── Gráfico ──────────────────────────────────────────────────────────────
    _mostrar_grafico_historico(
        empresa['ticker'],
        df,
        periodo_seleccionado,
        extra_series=extra_series_chart if extra_series_chart else None,
    )

    # ── Micro-tira Técnica Real (RSI-14 · Dist. SMA50 · Vol 20d · Pos. 52s) ──
    rsi_v = indicadores_tec.get("rsi14")
    d50_v = indicadores_tec.get("dist_sma50_pct")
    vol_v = indicadores_tec.get("vol_20d_pct")
    p52_v = indicadores_tec.get("pos_52s_pct")
    if any(x is not None for x in (rsi_v, d50_v, vol_v, p52_v)):
        rsi_txt = f"{rsi_v:.1f}" if rsi_v is not None else "N/A"
        rsi_tag = "Sobrecompra" if (rsi_v is not None and rsi_v > 70) else ("Sobreventa" if (rsi_v is not None and rsi_v < 30) else "Neutro")
        rsi_col = "#f87171" if (rsi_v is not None and rsi_v > 70) else ("#34d399" if (rsi_v is not None and rsi_v < 30) else "#38bdf8")

        d50_txt = f"{d50_v:+.1f}%" if d50_v is not None else "N/A"
        d50_col = "#34d399" if (d50_v is not None and d50_v >= 0) else "#f87171"

        vol_txt = f"{vol_v:.1f}%" if vol_v is not None else "N/A"
        vol_col = "#f87171" if (vol_v is not None and vol_v > 35) else ("#fbbf24" if (vol_v is not None and vol_v > 22) else "#34d399")

        p52_txt = f"{p52_v:.0f}%" if p52_v is not None else "N/A"
        p52_col = "#34d399" if (p52_v is not None and p52_v >= 75) else ("#38bdf8" if (p52_v is not None and p52_v >= 35) else "#fbbf24")

        st.markdown(
            "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:10px;margin:10px 0 4px 0;'>"
            f"<div class='metric-mini-card'><span class='metric-mini-label'>RSI (14) · {rsi_tag}</span><span class='metric-mini-value' style='color:{rsi_col};'>{rsi_txt}</span></div>"
            f"<div class='metric-mini-card'><span class='metric-mini-label'>Dist. SMA 50</span><span class='metric-mini-value' style='color:{d50_col};'>{d50_txt}</span></div>"
            f"<div class='metric-mini-card'><span class='metric-mini-label'>Volatilidad 20d</span><span class='metric-mini-value' style='color:{vol_col};'>{vol_txt}</span></div>"
            f"<div class='metric-mini-card'><span class='metric-mini-label'>Pos. Rango 52s</span><span class='metric-mini-value' style='color:{p52_col};'>{p52_txt}</span></div>"
            "</div>",
            unsafe_allow_html=True,
        )

    # ── Índice de Salud Fundamental (Motor A) ────────────────────────────────
    try:
        from views.ranking import _calcular_cobertura_fundamental
        _cov_emp = _calcular_cobertura_fundamental(info_scoring, sector_val)
        _mod_badge = "Modelo Financiero" if _cov_emp["modelo_financiero"] else "Modelo Estándar"
        _cov_badge_txt = f"Datos: {_cov_emp['disponibles']}/{_cov_emp['total']} · {_cov_emp['nivel']}"
        _cov_col = "#34d399" if _cov_emp["nivel"] == "Alta" else ("#38bdf8" if _cov_emp["nivel"] == "Media" else "#fbbf24")
        _cov_pills_html = (
            f' <span class="active-counter-badge" style="color:#cbd5e1;border-color:rgba(148,163,184,0.28);background:rgba(148,163,184,0.08);">{_mod_badge}</span>'
            f' <span class="active-counter-badge" style="color:{_cov_col};border-color:{_cov_col}44;background:{_cov_col}14;">{_cov_badge_txt}</span>'
        )
    except Exception:
        _cov_pills_html = ""

    st.markdown('<div class="emp-divider"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sl-section-title">Índice de Salud Fundamental '
        '<span class="active-counter-badge">[ Motor A · 0-100 pts ]</span>'
        f'{_cov_pills_html}'
        '</div>',
        unsafe_allow_html=True
    )

    with st.spinner("Calculando métricas fundamentales..."):
        try:
            var_diaria_scoring = (datos_mercado_base or {}).get("variacion_diaria")
            if var_diaria_scoring is None:
                var_diaria_scoring = variacion_calc if periodo_seleccionado == "1D" and variacion_calc is not None else 0.0

            score_data = calcular_score_completo(
                df_historico=df_scoring,
                info_fundamentales=info_scoring,
                variacion_diaria=float(var_diaria_scoring),
                sector=sector_val,
            )

            # Píldora coloreada por nivel de salud fundamental
            score_val = score_data['score_global']
            score_color = "#34d399" if score_val >= 70 else "#fbbf24" if score_val >= 45 else "#f87171"
            st.markdown(
                f"<div class='score-global-pill' style='border-color:rgba(148,163,184,0.14);"
                f" background:linear-gradient(135deg,{score_color}12, #111723 65%);"
                f" box-shadow: var(--sl-shadow-card);'>"
                f"<div style='display:flex;align-items:center;gap:12px;'>"
                f"<span style='width:10px;height:10px;border-radius:50%;background:{score_color};box-shadow:0 0 12px {score_color}99;flex-shrink:0;display:inline-block;'></span>"
                f"<div>"
                f"<div class='score-label'>Índice de Salud Fundamental StockLab</div>"
                f"<div style='font-family:Inter,sans-serif;font-size:0.78rem;color:#94a3b8;'>Evaluación global del activo basada en 5 dimensiones estratégicas</div>"
                f"</div>"
                f"</div>"
                f"<div style='display:flex;align-items:baseline;gap:6px;'>"
                f"<span class='score-number' style='color:{score_color};font-variant-numeric:tabular-nums;'>{score_val:.0f}</span>"
                f"<span class='score-denom'>/ 100 pts</span>"
                f"</div>"
                f"</div>",
                unsafe_allow_html=True
            )

            # 5 Tarjetas Ejecutivas de Pilares Fundamentales (sin iconos decorativos)
            pilares = [
                {"label": "Calidad Negocio", "val": score_data['calidad'], "max": 30, "color": "#34d399"},
                {"label": "Valoración",       "val": score_data['val'],     "max": 25, "color": "#fbbf24"},
                {"label": "Momentum",         "val": score_data['mom'],     "max": 20, "color": "#38bdf8"},
                {"label": "Dividendos",       "val": score_data['div'],     "max": 15, "color": "#10b981"},
                {"label": "Seguridad",        "val": score_data['seg'],     "max": 10, "color": "#a78bfa"},
            ]

            cards_html = []
            for p in pilares:
                pct = min(100.0, max(0.0, (p['val'] / p['max']) * 100.0)) if p['max'] > 0 else 0
                cards_html.append(
                    f'<div style="background:var(--sl-surface-glass);'
                    f'border:1px solid var(--sl-border-default);border-radius:12px;padding:16px 14px;'
                    f'box-shadow:var(--sl-shadow-card);position:relative;overflow:hidden;'
                    f'display:flex;flex-direction:column;justify-content:space-between;min-height:115px;">'
                    f'<div style="display:flex;align-items:center;gap:7px;margin-bottom:8px;">'
                    f'<span style="width:6px;height:6px;border-radius:50%;background:{p["color"]};display:inline-block;flex-shrink:0;"></span>'
                    f'<span style="font-family:\'Inter\',sans-serif;font-size:0.68rem;font-weight:600;'
                    f'color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;">{p["label"]}</span>'
                    f'</div>'
                    f'<div style="display:flex;align-items:baseline;gap:4px;margin-bottom:8px;">'
                    f'<span style="font-family:\'JetBrains Mono\',monospace;font-variant-numeric:tabular-nums;font-size:1.45rem;font-weight:700;'
                    f'color:{p["color"]};line-height:1;">{p["val"]:.1f}</span>'
                    f'<span style="font-family:\'Inter\',sans-serif;font-size:0.75rem;color:#64748b;font-weight:500;">/{p["max"]} pts</span>'
                    f'</div>'
                    f'<div style="height:4px;background:rgba(148,163,184,0.12);border-radius:999px;overflow:hidden;">'
                    f'<div style="width:{pct}%;height:100%;background:{p["color"]};border-radius:999px;"></div>'
                    f'</div>'
                    f'</div>'
                )

            st.markdown(
                f'<div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(min(100%, 145px), 1fr));gap:12px;margin:12px 0 16px 0;">'
                + "".join(cards_html) +
                '</div>',
                unsafe_allow_html=True
            )

            resumen_txt = generar_resumen_fundamental(info_scoring)
            if resumen_txt:
                st.markdown(
                    f'<div style="background:var(--sl-surface-glass);'
                    f'border:1px solid var(--sl-border-default);border-left:3px solid #38bdf8;border-radius:12px;'
                    f'padding:14px 18px;margin-top:14px;font-family:\'Inter\',sans-serif;font-size:0.84rem;'
                    f'color:#cbd5e1;line-height:1.55;box-shadow:var(--sl-shadow-card);">'
                    f'<span style="color:#7dd3fc;font-weight:600;margin-right:6px;">Diagnóstico Fundamental:</span>'
                    f'{resumen_txt}</div>',
                    unsafe_allow_html=True
                )

        except Exception as e:
            st.warning(f"No se pudieron calcular las métricas unificadas: {e}")

    # ── Noticias Recientes y Sentimiento (gratuito, cacheado 30 min) ─────────
    st.markdown('<div class="emp-divider"></div>', unsafe_allow_html=True)
    with st.expander(f"Noticias Recientes y Sentimiento ({len(noticias_empresa)})", expanded=False):
        if not noticias_empresa:
            st.info("No se han encontrado titulares recientes disponibles para esta compañía.")
        else:
            try:
                from services.news_sentiment import resumen_sentimiento
                sent_res = resumen_sentimiento(noticias_empresa)
                sent_lbl = sent_res.get("etiqueta", "Mixto")
                sent_col = "#34d399" if sent_lbl == "Favorable" else ("#f87171" if sent_lbl == "Desfavorable" else "#38bdf8")
                st.markdown(
                    f"<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px;'>"
                    f"<span style='padding:3px 10px;border-radius:999px;font-size:0.72rem;font-weight:700;"
                    f"background:{sent_col}18;border:1px solid {sent_col}44;color:{sent_col};'>"
                    f"Sentimiento: {_html.escape(str(sent_lbl))}</span>"
                    f"<span style='font-size:0.78rem;color:#94a3b8;'>"
                    f"{sent_res.get('positivas', 0)} positivas · {sent_res.get('neutrales', 0)} neutrales · {sent_res.get('negativas', 0)} negativas</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            except Exception:
                pass

            filas_news = []
            for n_item in noticias_empresa[:6]:
                tit = _html.escape(str(n_item.get("titulo") or "Sin titular"))
                fuente = _html.escape(str(n_item.get("fuente") or "Fuente externa"))
                fecha_n = _html.escape(str(n_item.get("fecha") or ""))
                enlace = str(n_item.get("link") or n_item.get("enlace") or "").strip()
                sent_n = str(n_item.get("sentimiento") or "Neutral")
                c_s = "#34d399" if sent_n == "Positivo" else ("#f87171" if sent_n == "Negativo" else "#94a3b8")
                tit_html = (
                    f"<a href='{_html.escape(enlace)}' target='_blank' rel='noopener' "
                    f"style='color:#e2e8f0;text-decoration:none;font-weight:600;'>{tit}</a>"
                    if enlace.startswith(("http://", "https://"))
                    else f"<span style='color:#e2e8f0;font-weight:600;'>{tit}</span>"
                )
                filas_news.append(
                    f"<div style='padding:10px 14px;border-radius:10px;background:rgba(11,17,30,0.65);"
                    f"border:1px solid rgba(148,163,184,0.10);margin-bottom:8px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px;'>"
                    f"<div style='font-size:0.84rem;line-height:1.45;'>{tit_html}"
                    f"<div style='font-size:0.72rem;color:#64748b;margin-top:3px;'>{fuente}{(' · ' + fecha_n) if fecha_n else ''}</div></div>"
                    f"<span style='font-size:0.68rem;font-weight:700;color:{c_s};padding:2px 8px;border-radius:999px;"
                    f"border:1px solid {c_s}44;background:{c_s}14;white-space:nowrap;'>{_html.escape(sent_n)}</span>"
                    f"</div>"
                )
            st.markdown("".join(filas_news), unsafe_allow_html=True)




def renderizar_catalogo_botones_premium(df_filtrado, sufijo="all", es_pestana_sectores=False, sector_fijo=""):
    """Renderiza catálogo usando botones nativos de Streamlit estilizados como tarjetas."""
    if df_filtrado.empty:
        st.info("No hay empresas disponibles en esta sección.")
        return

    columnas_por_fila = 3
    for i in range(0, len(df_filtrado), columnas_por_fila):
        bloque = df_filtrado.iloc[i : i + columnas_por_fila]
        cols = st.columns(columnas_por_fila)
        
        for idx, (original_index, row) in enumerate(bloque.iterrows()):
            # EXTRACCIÓN GARANTIZADA DEL TICKER REAL (NO EL ÍNDICE DEL BUCLE)
            if 'Ticker' in row and pd.notna(row['Ticker']):
                ticker = str(row['Ticker']).strip()
            else:
                ticker = str(original_index).strip() # Si el ticker es el índice del DataFrame
            
            nombre = str(row.get('Nombre', '---')).strip()
            sector = sector_fijo if es_pestana_sectores else str(row.get('Sector', '---')).strip()
            mercado = str(row.get('Market', row.get('Mercado', 'BME'))).strip()
            
            # Formato compuesto EXACTO que espera el selectbox superior para que funcione el clic
            opcion_compuesta = f"{ticker} - {nombre}"
            
            # Marcador de IBEX 35 usando la etiqueta de color que el CSS transformará en badge flotante
            es_ibex = str(row.get('IBEX 35', 'No')).strip().lower() in ['sí', 'si', 'yes', 'true']
            texto_ibex = " :orange[IBEX 35]" if es_ibex else ""
            
            # Construcción limpia de la tarjeta: Ticker en verde, Nombre destacado, Sectores abajo en gris
            texto_boton = f":green[**{ticker}**]{texto_ibex}\n\n**{nombre}**\n\n:gray[{sector}   •   {mercado}]"
            
            with cols[idx]:
                st.button(
                    label=texto_boton,
                    key=f"btn_{ticker}_{sufijo}_{idx}", # Clave única blindada contra duplicados
                    on_click=cambiar_empresa_callback,
                    args=(opcion_compuesta,),
                    width="stretch" # <-- ESTO OBLIGA A REVENTAR EL ANCHO DE LA COLUMNA NATIVAMENTE
                )


def _mostrar_listado(empresas):
    """Muestra el listado general de todas las empresas con diseño premium usando DataFrames."""
    st.markdown('<div class="emp-divider"></div>', unsafe_allow_html=True)
    
    # Convertir lista de diccionarios a DataFrame
    df = pd.DataFrame(empresas)
    if df.empty:
        st.markdown("""
        <div class="emp-empty-shell">
            <div class="emp-empty-title">Sin Empresas Registradas</div>
            <div class="emp-empty-desc">No hay empresas registradas actualmente en la base de datos de StockLab.</div>
        </div>
        """, unsafe_allow_html=True)
        return
    
    # Renombrar columnas para consistencia
    df = df.rename(columns={
        'ticker': 'Ticker',
        'nombre': 'Nombre',
        'sector': 'Sector',
        'mercado': 'Mercado',
        'es_ibex35': 'IBEX 35'
    })
    
    # Normalizar columna IBEX 35 a texto
    df['IBEX 35'] = df['IBEX 35'].apply(lambda x: 'Sí' if x else 'No')
    
    # 1. TARJETAS DE RESUMEN (KPIs Institucionales)
    total_empresas = len(df)
    total_ibex = len(df[df['IBEX 35'] == 'Sí'])
    sectores_unicos = df['Sector'].nunique()
    
    st.markdown(f"""
    <div class="sl-kpi-grid" style="--sl-columns:3">
      <div class="sl-kpi-card" style="--sl-kpi-accent:var(--sl-accent)">
        <div class="sl-kpi-value">
          {total_empresas}
        </div>
        <div class="sl-kpi-label">
          Empresas en Seguimiento
        </div>
        <div class="sl-kpi-caption">Total registradas en base de datos</div>
      </div>
      <div class="sl-kpi-card" style="--sl-kpi-accent:var(--sl-warn)">
        <div class="sl-kpi-value">
          {total_ibex}
        </div>
        <div class="sl-kpi-label">
          Componentes IBEX 35
        </div>
        <div class="sl-kpi-caption">Ponderación selectivo español</div>
      </div>
      <div class="sl-kpi-card" style="--sl-kpi-accent:#b4a7fc">
        <div class="sl-kpi-value">
          {sectores_unicos}
        </div>
        <div class="sl-kpi-label">
          Sectores Cubiertos
        </div>
        <div class="sl-kpi-caption">Diversificación sectorial activa</div>
      </div>
    </div>
    """, unsafe_allow_html=True)
    
    # --- CONFIGURACIÓN DE LAS PESTAÑAS (TABS) ACTUALIZADA ---
    tabs = st.tabs(["Todas las Empresas", "Componentes IBEX 35", "Agrupado por Sectores"])

    with tabs[0]:
        st.markdown(f'<div class="sl-section-title">Catálogo Completo <span class="active-counter-badge">[ {total_empresas} empresas ]</span></div>', unsafe_allow_html=True)
        renderizar_catalogo_botones_premium(df, sufijo="todas")

    with tabs[1]:
        st.markdown(f'<div class="sl-section-title">Componentes IBEX 35 <span class="active-counter-badge">[ {total_ibex} empresas ]</span></div>', unsafe_allow_html=True)
        if 'IBEX 35' in df.columns:
            df_ibex = df[df['IBEX 35'].str.lower().isin(['sí', 'si', 'yes', 'true'])]
            renderizar_catalogo_botones_premium(df_ibex, sufijo="ibex")
        else:
            st.warning("No se encontró la columna IBEX 35 en los datos.")

    with tabs[2]:
        st.markdown(f'<div class="sl-section-title">Empresas por Sectores <span class="active-counter-badge">[ {sectores_unicos} sectores ]</span></div>', unsafe_allow_html=True)
        if 'Sector' in df.columns:
            for sector_nombre in sorted(df['Sector'].unique()):
                df_sector = df[df['Sector'] == sector_nombre]
                with st.expander(f"{sector_nombre} ({len(df_sector)} empresas)"):
                    # Reemplazamos espacios en el nombre del sector para crear un sufijo seguro
                    sufijo_sector = str(sector_nombre).replace(" ", "_").lower()
                    renderizar_catalogo_botones_premium(
                        df_sector, 
                        sufijo=f"sector_{sufijo_sector}", 
                        es_pestana_sectores=True, 
                        sector_fijo=sector_nombre
                    )


def show():
    # ── CSS Premium Institucional — Inyección inmediata antes de renderizar contenido ──
    st.markdown("""
<style>
    /* ═══════════════════════════════════════════════════════════════════
       🎨 STOCKLAB EMPRESAS — Premium Dark Trading Terminal Theme
       Unified with Dashboard, Mi Cartera V2, Ranking & Watchlist
       Inspired by TradingView Pro / Binance / Bloomberg Terminal
       ═══════════════════════════════════════════════════════════════════ */

    /* ── Google Font: cargada centralizadamente en app.py ──────────── */

    /* ── Keyframe Animations ────────────────────────────────────────── */
    @keyframes fade-in-up {
        0%   { opacity: 0; transform: translateY(6px); }
        100% { opacity: 1; transform: translateY(0); }
    }

    @keyframes glow-pulse {
        0%, 100% { box-shadow: none; }
        50%      { box-shadow: none; }
    }

    /* ── Global Foundation ──────────────────────────────────────────── */
    .block-container {
        padding-top: 1.6rem !important;
        padding-bottom: 3rem !important;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
    }

    /* ── Custom Scrollbar ───────────────────────────────────────────── */
    ::-webkit-scrollbar { width: 6px !important; height: 6px !important; }
    ::-webkit-scrollbar-track { background: #0d121c !important; border-radius: 3px !important; }
    ::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.20) !important; border-radius: 999px !important; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(148, 163, 184, 0.36) !important; }

    /* ═══════════════════════════════════════════════════════════════════
       ✍️ GRADIENT HEADINGS — Bloomberg Premium Typography
       ═══════════════════════════════════════════════════════════════════ */
    h1, .stMarkdown h1 {
        background: none !important;
        -webkit-background-clip: initial !important;
        -webkit-text-fill-color: initial !important;
        background-clip: initial !important;
        color: #f8fafc !important;
        font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
        font-weight: 800 !important;
        letter-spacing: -0.025em !important;
        text-shadow: none !important;
    }

    h2, .stMarkdown h2 {
        background: none !important;
        -webkit-background-clip: initial !important;
        -webkit-text-fill-color: initial !important;
        background-clip: initial !important;
        color: #f1f5f9 !important;
        font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
        font-weight: 700 !important;
        letter-spacing: -0.025em !important;
        text-shadow: none !important;
    }

    h3, .stMarkdown h3 {
        background: none !important;
        -webkit-background-clip: initial !important;
        -webkit-text-fill-color: initial !important;
        background-clip: initial !important;
        color: #e2e8f0 !important;
        font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
        font-weight: 600 !important;
        letter-spacing: -0.025em !important;
        text-shadow: none !important;
    }

    h4, .stMarkdown h4, h5, .stMarkdown h5 {
        color: #f1f5f9 !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📊 TARJETAS KPI — Métricas con Brillo Eléctrico
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stMetric"] {
        background: linear-gradient(180deg, #131a29 0%, #101623 100%) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 12px !important;
        padding: 20px 18px !important;
        transition: all 0.25s ease !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
        animation: fade-in-up 0.22s ease-out both !important;
        position: relative !important;
        overflow: hidden !important;
    }

    div[data-testid="stMetric"]::before { display: none !important; }

    div[data-testid="stMetric"]:hover {
        border-color: rgba(56, 189, 248, 0.28) !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.32), 0 12px 28px -10px rgba(0, 0, 0, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
        transform: translateY(-2px) !important;
    }

    div[data-testid="stMetric"]:hover::before { display: none !important; }

    div[data-testid="stMetric"] label {
        color: #78879c !important;
        font-size: 0.7rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 1.2px !important;
        margin-bottom: 10px !important;
        font-family: 'Inter', sans-serif !important;
    }

    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        color: #f1f5f9 !important;
        font-weight: 800 !important;
        font-size: 1.45rem !important;
        font-family: 'JetBrains Mono', 'Inter', monospace !important;
        margin-top: 6px !important;
        letter-spacing: -0.5px !important;
        text-shadow: none !important;
    }

    div[data-testid="stMetric"] div[data-testid="stMetricDelta"] {
        font-size: 0.85rem !important;
        font-weight: 700 !important;
        margin-top: 6px !important;
        font-family: 'JetBrains Mono', monospace !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📈 CHART CONTAINERS — Seamless Graph Integration
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stPlotlyChart"] {
        background: linear-gradient(180deg, #131a29 0%, #101623 100%) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 14px !important;
        padding: 8px !important;
        transition: all 0.3s ease !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
        overflow: hidden !important;
    }

    div[data-testid="stPlotlyChart"]:hover {
        border-color: rgba(56, 189, 248, 0.18) !important;
        box-shadow: 0 6px 24px rgba(0, 0, 0, 0.4) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📦 CONTAINERS — Glassmorphic Panels
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
        background: linear-gradient(180deg, #131a29 0%, #101623 100%) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 14px !important;
        transition: all 0.35s cubic-bezier(0.25, 0.46, 0.45, 0.94) !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
        overflow: hidden !important;
    }

    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: rgba(56, 189, 248, 0.28) !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.32), 0 12px 28px -10px rgba(0, 0, 0, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🔀 SEGMENTED CONTROL — Period Toggle
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stSegmentedControl"] button {
        background: #0d121c !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        color: #78879c !important;
        font-weight: 600 !important;
        font-family: 'Inter', sans-serif !important;
        transition: all 0.3s ease !important;
        border-radius: 8px !important;
        font-size: 0.82rem !important;
    }

    div[data-testid="stSegmentedControl"] button:hover {
        background: rgba(56, 189, 248, 0.06) !important;
        color: #cbd5e1 !important;
        border-color: rgba(56, 189, 248, 0.2) !important;
    }

    div[data-testid="stSegmentedControl"] button[aria-checked="true"] {
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.12), rgba(16, 185, 129, 0.08)) !important;
        border-color: rgba(56, 189, 248, 0.3) !important;
        color: #38bdf8 !important;
        box-shadow: none !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📝 FORM INPUTS / SELECTS — Dark Terminal Inputs
       ═══════════════════════════════════════════════════════════════════ */
    .stSelectbox [data-baseweb="select"] {
        background-color: #0d121c !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 10px !important;
        color: #f1f5f9 !important;
        font-family: 'Inter', sans-serif !important;
        transition: border-color 0.3s ease, box-shadow 0.3s ease !important;
    }

    .stSelectbox [data-baseweb="select"]:focus-within {
        border-color: rgba(56, 189, 248, 0.31) !important;
        box-shadow: none !important;
    }

    [data-baseweb="popover"] {
        background-color: #111723 !important;
        border: 1px solid rgba(56, 189, 248, 0.18) !important;
        border-radius: 12px !important;
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.85) !important;
    }

    [data-baseweb="menu"] { background-color: #111723 !important; }
    [data-baseweb="menu"] li { color: #cbd5e1 !important; transition: all 0.2s ease !important; font-family: 'Inter', sans-serif !important; }
    [data-baseweb="menu"] li:hover { background-color: rgba(56, 189, 248, 0.08) !important; color: #38bdf8 !important; }

    /* Form & Selectbox label typography - 100% harmonized with Ranking, Cartera & Watchlist */
    section[data-testid="stMain"] div[data-testid="stSelectbox"] label[data-testid="stWidgetLabel"],
    .stSelectbox label, .stMultiSelect label,
    div[data-testid="stNumberInput"] label,
    div[data-testid="stTextInput"] label,
    div[data-testid="stTextArea"] label,
    div[data-testid="stCheckbox"] label {
        min-height: 1.5rem !important;
        margin-bottom: 0.25rem !important;
        display: flex !important;
        align-items: center !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.76rem !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
    }

    section[data-testid="stMain"] div[data-testid="stSelectbox"] label[data-testid="stWidgetLabel"] p,
    .stSelectbox label p, .stMultiSelect label p,
    div[data-testid="stNumberInput"] label p,
    div[data-testid="stTextInput"] label p,
    div[data-testid="stTextArea"] label p,
    div[data-testid="stCheckbox"] label p {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.76rem !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
        margin: 0 !important;
        line-height: 1.4 !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🏷️ TABS — Glassmorphic Navigation
       ═══════════════════════════════════════════════════════════════════ */
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px !important;
        background: rgba(15, 18, 25, 0.5) !important;
        border-radius: 12px !important;
        padding: 4px !important;
    }

    .stTabs [data-baseweb="tab"] {
        color: #78879c !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 600 !important;
        border-radius: 10px !important;
        transition: all 0.3s ease !important;
        padding: 10px 18px !important;
    }

    .stTabs [data-baseweb="tab"]:hover {
        background: rgba(56, 189, 248, 0.05) !important;
        color: #cbd5e1 !important;
    }

    .stTabs [data-baseweb="tab"][aria-selected="true"] {
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.1), rgba(16, 185, 129, 0.06)) !important;
        color: #38bdf8 !important;
        border-bottom-color: transparent !important;
    }

    .stTabs [data-baseweb="tab-highlight"] {
        background-color: #38bdf8 !important;
        border-radius: 2px !important;
        height: 2px !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📂 EXPANDERS — Sector Drawers
       ═══════════════════════════════════════════════════════════════════ */
    details[data-testid="stExpander"] {
        background: linear-gradient(160deg, rgba(22, 26, 37, 0.5), rgba(15, 18, 25, 0.7)) !important;
        border: 1px solid rgba(100, 116, 139, 0.1) !important;
        border-radius: 14px !important;
        transition: all 0.3s ease !important;
    }

    details[data-testid="stExpander"]:hover {
        border-color: rgba(56, 189, 248, 0.15) !important;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.2) !important;
    }

    details[data-testid="stExpander"] summary {
        color: #cbd5e1 !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 600 !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🎴 CATALOG CARD BUTTONS — Premium Company Cards
       ═══════════════════════════════════════════════════════════════════ */
    /* General Action Buttons inside columns and popovers */
    div[data-testid="stPopover"] > button,
    div[data-testid="column"] div[data-testid="stButton"] button {
        height: 48px !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.94rem !important;
        font-weight: 600 !important;
        color: #e2e8f0 !important;
        border: 1px solid rgba(56, 189, 248, 0.18) !important;
        background: linear-gradient(135deg, rgba(22, 26, 37, 0.8), rgba(15, 18, 25, 0.9)) !important;
        border-radius: 10px !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25) !important;
        transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
        width: 100% !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        gap: 8px !important;
    }

    div[data-testid="stPopover"] > button:hover,
    div[data-testid="column"] div[data-testid="stButton"] button:hover {
        border-color: rgba(56, 189, 248, 0.39) !important;
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.08), rgba(16, 185, 129, 0.05)) !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3) !important;
        transform: translateY(-1px) !important;
        color: #38bdf8 !important;
    }


    .active-counter-badge {
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        color: #38bdf8 !important;
        background: rgba(56, 189, 248, 0.08) !important;
        border: 1px solid rgba(56, 189, 248, 0.25) !important;
        border-radius: 6px !important;
        padding: 2px 8px !important;
        letter-spacing: 0.5px !important;
        margin-left: 6px !important;
    }

    /* ── Glowing Gradient Divider Line ─────────────────────────────────── */
    .emp-divider {
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(148, 163, 184, 0.16), transparent);
        margin: 24px 0 20px;
    }

    /* ── Terminal Empty State Shell ──────────────────────────────────── */
    .emp-empty-shell {
        background: linear-gradient(180deg, #121826 0%, #0e1420 100%);
        border: 1px dashed rgba(148, 163, 184, 0.22);
        border-radius: 16px;
        padding: 48px 32px;
        text-align: center;
        margin: 24px 0;
        position: relative;
        overflow: hidden;
    }
    .emp-empty-shell::before { display: none !important; }
    .emp-empty-icon {
        font-size: 2.6rem;
        margin-bottom: 12px;
    }
    .emp-empty-title {
        font-family: 'Inter', sans-serif;
        font-size: 1.15rem;
        font-weight: 700;
        color: #e2e8f0;
        margin-bottom: 8px;
    }
    .emp-empty-desc {
        font-family: 'Inter', sans-serif;
        font-size: 0.86rem;
        color: var(--sl-text-secondary);
        max-width: 520px;
        margin: 0 auto;
        line-height: 1.5;
    }

    /* Solo las tarjetas del catálogo comparten estas claves en las pestañas. */
    .stTabs [class*="st-key-btn_"] button {
        position: relative !important;
        overflow: hidden !important;
        width: 100% !important;
        min-height: 112px !important;
        padding: 18px 22px !important;
        display: flex !important;
        flex-direction: column !important;
        align-items: stretch !important;
        justify-content: center !important;
        text-align: left !important;
        background: var(--sl-surface-glass) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: var(--sl-radius-lg) !important;
        box-shadow: var(--sl-shadow-card) !important;
        contain: layout style !important;
        transition: transform 200ms var(--sl-ease), border-color 200ms var(--sl-ease), box-shadow 200ms var(--sl-ease) !important;
    }
    .stTabs [class*="st-key-btn_"] button::before {
        content: "" !important;
        position: absolute !important;
        top: 0 !important;
        left: 18% !important;
        right: 18% !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.55), transparent) !important;
        opacity: 0.45 !important;
        transition: opacity 220ms var(--sl-ease), left 220ms var(--sl-ease), right 220ms var(--sl-ease) !important;
    }
    .stTabs [class*="st-key-btn_"] button:is(:hover, :focus-visible) {
        transform: translateY(-3px) !important;
        background: var(--sl-surface-glass-hover) !important;
        border-color: var(--sl-border-hover) !important;
        box-shadow: var(--sl-shadow-hover) !important;
    }
    .stTabs [class*="st-key-btn_"] button:is(:hover, :focus-visible)::before {
        left: 8% !important;
        right: 8% !important;
        opacity: 1 !important;
    }
    .stTabs [class*="st-key-btn_"] button > div,
    .stTabs [class*="st-key-btn_"] button [data-testid="stMarkdownContainer"] {
        width: 100% !important;
        max-width: 100% !important;
        display: block !important;
        text-align: left !important;
    }
    .stTabs [class*="st-key-btn_"] button p {
        width: 100% !important;
        text-align: left !important;
        margin: 0 !important;
        line-height: 1.4 !important;
    }
    .stTabs [class*="st-key-btn_"] button p:first-child {
        display: flex !important;
        justify-content: space-between !important;
        align-items: center !important;
        gap: 8px !important;
    }
    .stTabs [class*="st-key-btn_"] button p:first-child strong {
        color: var(--sl-accent-bright) !important;
        font: 700 0.91rem/1.35 var(--sl-font-mono) !important;
        font-variant-numeric: tabular-nums;
    }
    .stTabs [class*="st-key-btn_"] button p:first-child span:last-child:not(:has(strong)) {
        margin-left: auto;
        padding: 2px 8px;
        border-radius: 999px;
        background: var(--sl-warn-soft);
        border: 1px solid rgba(251, 191, 36, 0.25);
        color: var(--sl-warn) !important;
        font: 700 0.67rem/1.4 var(--sl-font-body);
        letter-spacing: 0.04em;
        white-space: nowrap;
    }
    .stTabs [class*="st-key-btn_"] button p:nth-child(2) {
        margin: 7px 0 3px !important;
        color: var(--sl-text-heading);
        font: 700 0.97rem/1.4 var(--sl-font-display) !important;
        overflow-wrap: anywhere;
    }
    .stTabs [class*="st-key-btn_"] button p:nth-child(3),
    .stTabs [class*="st-key-btn_"] button p:nth-child(3) span {
        color: var(--sl-text-secondary) !important;
        font: 500 0.78rem/1.45 var(--sl-font-body) !important;
    }

    .stButton > button:not([kind]) {
        font-family: 'Inter', sans-serif !important;
        border-radius: 10px !important;
        transition: all 0.3s ease !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🔔 ALERTS — Neon-Tinted Notifications
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stAlert"] {
        border-radius: 12px !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 500 !important;
    }

    div[role="alert"]:has(> div > .stAlertContentInfo),
    .stAlert[data-baseweb="notification"][kind="info"] {
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.06), rgba(56, 189, 248, 0.02)) !important;
        border-left: 4px solid #38bdf8 !important;
        border-radius: 0 12px 12px 0 !important;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.2) !important;
    }

    div[role="alert"]:has(> div > .stAlertContentSuccess),
    .stAlert[data-baseweb="notification"][kind="positive"] {
        background: linear-gradient(135deg, rgba(16, 185, 129, 0.06), rgba(16, 185, 129, 0.02)) !important;
        border-left: 4px solid #10b981 !important;
        border-radius: 0 12px 12px 0 !important;
    }

    div[role="alert"]:has(> div > .stAlertContentWarning),
    .stAlert[data-baseweb="notification"][kind="warning"] {
        background: linear-gradient(135deg, rgba(245, 158, 11, 0.06), rgba(245, 158, 11, 0.02)) !important;
        border-left: 4px solid #f59e0b !important;
        border-radius: 0 12px 12px 0 !important;
    }

    div[role="alert"]:has(> div > .stAlertContentError),
    .stAlert[data-baseweb="notification"][kind="negative"] {
        background: linear-gradient(135deg, rgba(239, 68, 68, 0.06), rgba(239, 68, 68, 0.02)) !important;
        border-left: 4px solid #ef4444 !important;
        border-radius: 0 12px 12px 0 !important;
    }

    /* ── Separadores — Gradient Dividers ─────────────────────────────── */
    hr {
        border: none !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(148, 163, 184, 0.16), transparent) !important;
        margin: 1.2rem 0 !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🌐 GLOBAL TEXT & MISC — Typography Polish
       ═══════════════════════════════════════════════════════════════════ */
    .stMarkdown p { font-family: 'Inter', sans-serif !important; }
    .stMarkdown strong { color: #f1f5f9 !important; }
    .stCaption, [data-testid="stCaptionContainer"] { color: #78879c !important; font-family: 'Inter', sans-serif !important; }

    div[data-testid="stTooltipContent"] {
        background-color: #182133 !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 8px !important;
        color: #cbd5e1 !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.32), 0 12px 28px -10px rgba(0, 0, 0, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
    }

    /* ── Popover (Watchlist modal) — Unified dark interior ──────────── */
    div[data-testid="stPopover"] > div {
        background: linear-gradient(160deg, #131a29, #111723) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 14px !important;
        box-shadow: 0 12px 48px rgba(0, 0, 0, 0.6) !important;
    }

    /* Kill inner nested backgrounds inside popover */
    div[data-testid="stPopover"] > div > div,
    div[data-testid="stPopover"] > div > div > div,
    div[data-testid="stPopover"] [data-testid="stVerticalBlock"],
    div[data-testid="stPopover"] [data-testid="stVerticalBlockBorderWrapper"] {
        background: transparent !important;
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }

    /* Popover inner inputs */
    div[data-testid="stPopover"] .stSelectbox [data-baseweb="select"],
    div[data-testid="stPopover"] .stNumberInput input,
    div[data-testid="stPopover"] .stTextInput input {
        background-color: #111723 !important;
        border: 1px solid rgba(100, 116, 139, 0.2) !important;
        border-radius: 10px !important;
        color: #f1f5f9 !important;
    }

    div[data-testid="stPopover"] .stRadio label {
        color: #cbd5e1 !important;
        font-family: 'Inter', sans-serif !important;
    }

    /* ── Responsive ──────────────────────────────────────────────────── */
    @media (max-width: 768px) {
        div[data-testid="stMetric"] { padding: 16px 14px !important; }
        div[data-testid="stMetric"] div[data-testid="stMetricValue"] { font-size: 1.2rem !important; }
    }

    /* ═══════════════════════════════════════════════════════════════════
       🏢 COMPANY HEADER CARD — Premium Badge Layout
       ═══════════════════════════════════════════════════════════════════ */
    .empresa-header-card {
        background: radial-gradient(ellipse 75% 75% at 92% 0%, rgba(56, 189, 248, 0.14), transparent 58%), var(--sl-surface-glass) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: var(--sl-radius-xl) !important;
        padding: 26px 30px !important;
        margin: 0.35rem 0 1.1rem 0 !important;
        box-shadow: var(--sl-shadow-card) !important;
        animation: fade-in-up 0.24s var(--sl-ease) both !important;
        position: relative !important;
        overflow: hidden !important;
    }

    .empresa-header-card::before {
        content: "" !important;
        display: block !important;
        position: absolute !important;
        top: 0 !important;
        left: 14% !important;
        right: 14% !important;
        height: 1.5px !important;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.65), rgba(99, 102, 241, 0.45), transparent) !important;
    }

    .empresa-header-top {
        display: flex !important;
        align-items: center !important;
        gap: 12px !important;
        flex-wrap: wrap !important;
        margin-bottom: 6px !important;
    }

    .ticker-badge {
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.15), rgba(99, 102, 241, 0.08)) !important;
        border: 1px solid rgba(56, 189, 248, 0.38) !important;
        color: #7dd3fc !important;
        font-family: var(--sl-font-mono) !important;
        font-size: 0.85rem !important;
        font-weight: 700 !important;
        padding: 4px 12px !important;
        border-radius: 8px !important;
        letter-spacing: 1.2px !important;
        box-shadow: 0 0 14px -4px rgba(56, 189, 248, 0.35) !important;
        white-space: nowrap !important;
        flex-shrink: 0 !important;
    }

    .empresa-nombre {
        font-family: var(--sl-font-display) !important;
        font-size: clamp(1.25rem, 2.1vw, 1.65rem) !important;
        overflow-wrap: anywhere;
        font-weight: 800 !important;
        color: #f1f5f9 !important;
        letter-spacing: -0.5px !important;
        flex: 1 !important;
        min-width: 0 !important;
    }

    .empresa-subline {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.9rem !important;
        color: #94a3b8 !important;
        line-height: 1.4 !important;
        margin-bottom: 12px !important;
    }

    .empresa-header-meta {
        display: flex !important;
        align-items: center !important;
        gap: 8px !important;
        flex-wrap: wrap !important;
    }

    .meta-badge {
        display: inline-flex !important;
        align-items: center !important;
        background: rgba(22, 27, 40, 0.9) !important;
        border: 1px solid rgba(100,116,139,0.2) !important;
        color: #94a3b8 !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.74rem !important;
        font-weight: 600 !important;
        padding: 4px 12px !important;
        border-radius: 20px !important;
        letter-spacing: 0.3px !important;
        white-space: nowrap !important;
        transition: all 0.2s ease !important;
    }

    .ibex-badge {
        background: linear-gradient(135deg, rgba(251,191,36,0.1), rgba(251,191,36,0.05)) !important;
        border-color: rgba(251,191,36,0.35) !important;
        color: #fbbf24 !important;
        text-shadow: none !important;
    }

    .empresa-desc {
        color: #94a3b8 !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.9rem !important;
        line-height: 1.7 !important;
        margin: 0 !important;
        padding-bottom: 12px !important;
    }

    /* ── Sector Chromatic Pills ──────────────────────────────────────── */
    .emp-sec-pill {
        display: inline-flex !important;
        align-items: center !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.72rem !important;
        font-weight: 600 !important;
        padding: 4px 12px !important;
        border-radius: 20px !important;
        letter-spacing: 0.3px !important;
        white-space: nowrap !important;
    }
    .sec-tech    { background: rgba(56, 189, 248, 0.12) !important; color: #38bdf8 !important; border: 1px solid rgba(56, 189, 248, 0.35) !important; }
    .sec-fin     { background: rgba(251, 191, 36, 0.12) !important; color: #fbbf24 !important; border: 1px solid rgba(251, 191, 36, 0.35) !important; }
    .sec-health  { background: rgba(52, 211, 153, 0.12) !important; color: #34d399 !important; border: 1px solid rgba(52, 211, 153, 0.35) !important; }
    .sec-energy  { background: rgba(251, 146, 60, 0.12) !important; color: #fb923c !important; border: 1px solid rgba(251, 146, 60, 0.35) !important; }
    .sec-consume { background: rgba(192, 132, 252, 0.12) !important; color: #c084fc !important; border: 1px solid rgba(192, 132, 252, 0.35) !important; }
    .sec-default { background: rgba(100, 116, 139, 0.12) !important; color: #94a3b8 !important; border: 1px solid rgba(100, 116, 139, 0.30) !important; }

    /* ═══════════════════════════════════════════════════════════════════
       📂 KEY METRICS STRIP — Compact KPIs Strip
       ═══════════════════════════════════════════════════════════════════ */
    .metrics-strip {
        display: grid !important;
        grid-template-columns: repeat(6, 1fr) !important;
        gap: 12px !important;
        margin: 0.8rem 0 1.2rem 0 !important;
        width: 100% !important;
    }

    @media (max-width: 900px) {
        .metrics-strip { grid-template-columns: repeat(3, 1fr) !important; }
    }
    @media (max-width: 600px) {
        .metrics-strip { grid-template-columns: repeat(2, 1fr) !important; }
    }

    .metric-mini-card {
        background: var(--sl-surface-glass) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: var(--sl-radius-lg) !important;
        padding: 15px 16px !important;
        display: flex !important;
        flex-direction: column !important;
        gap: 6px !important;
        box-shadow: var(--sl-shadow-card) !important;
        transition: transform 220ms var(--sl-ease), border-color 220ms var(--sl-ease), box-shadow 220ms var(--sl-ease) !important;
        position: relative !important;
        overflow: hidden !important;
    }

    .metric-mini-card::before {
        content: "" !important;
        display: block !important;
        position: absolute !important;
        top: 0 !important;
        left: 20% !important;
        right: 20% !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.45), transparent) !important;
        opacity: 0.55 !important;
        transition: opacity 220ms var(--sl-ease) !important;
    }

    .metric-mini-card:hover {
        transform: translateY(-2px) !important;
        border-color: var(--sl-border-hover) !important;
        box-shadow: var(--sl-shadow-hover) !important;
    }
    .metric-mini-card:hover::before { opacity: 1 !important; }

    .metric-mini-label {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.68rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 1.1px !important;
        color: #94a3b8 !important;
    }

    .metric-mini-value {
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 1.15rem !important;
        font-weight: 800 !important;
        color: #ffffff !important;
        letter-spacing: -0.3px !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🏥 SCORE PILL — Índice de Salud Fundamental (Motor A)
       ═══════════════════════════════════════════════════════════════════ */
    .score-global-pill {
        display: flex !important;
        align-items: center !important;
        gap: 14px !important;
        padding: 14px 22px !important;
        border: 1px solid !important;
        border-radius: 14px !important;
        margin-bottom: 18px !important;
        animation: fade-in-up 0.22s ease-out both !important;
    }

    .score-label {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 1.2px !important;
        color: #78879c !important;
        flex: 1 !important;
    }

    .score-number {
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 2.1rem !important;
        font-weight: 800 !important;
        letter-spacing: -1.5px !important;
        line-height: 1 !important;
    }

    .score-denom {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.9rem !important;
        font-weight: 500 !important;
        color: #64748b !important;
        align-self: flex-end !important;
        margin-bottom: 4px !important;
    }

    /* ── Metric Cards padding override ───────────────────────────────── */
    div[data-testid="stMetric"] {
        padding: 26px 22px !important;
    }

    /* ── Section header ─────────────────────────────────────────────── */
    .section-header {
        display: flex !important;
        align-items: center !important;
        gap: 10px !important;
        margin-bottom: 14px !important;
    }

    .section-icon {
        font-size: 1.15rem !important;
    }

    .section-title {
        font-family: var(--sl-font-display) !important;
        font-size: 1.06rem !important;
        font-weight: 700 !important;
        color: var(--sl-text-heading) !important;
        letter-spacing: -0.02em !important;
    }

</style>
""", unsafe_allow_html=True)

    # ── Cabecera de página premium institucional ─────────────────────────
    st.html("""
    <div class="sl-page-header">
        <h1 class="sl-page-title">Empresas</h1>
        <p class="sl-page-subtitle">Directorio de cotizadas, análisis fundamental y cobertura de mercado</p>
    </div>
    """)
    
    # Inicializar selector_global_empresas si no existe (para navegación desde otros módulos)
    if "selector_global_empresas" not in st.session_state:
        st.session_state["selector_global_empresas"] = "--- Seleccione una empresa ---"

    try:
        empresas = _cargar_empresas()
    except Exception as e:
        st.error(f"Error al conectar con la base de datos: {e}")
        return

    if not empresas:
        st.info("No hay empresas registradas actualmente.")
        return

    # Configuración del Buscador Superior vinculado a session_state
    opciones = ["--- Seleccione una empresa ---"] + [f"{emp['ticker']} - {emp['nombre']}" for emp in empresas]
    
    # Validar y asegurar que el valor en session_state sea válido
    valor_actual = st.session_state.get("selector_global_empresas", "--- Seleccione una empresa ---")
    if valor_actual not in opciones:
        # Si el valor no es válido (ej: empresa eliminada), resetear al default
        st.session_state["selector_global_empresas"] = "--- Seleccione una empresa ---"
        valor_actual = "--- Seleccione una empresa ---"
    
    seleccion_str = st.selectbox(
        "SELECCIONA UNA EMPRESA PARA VER SU FICHA DETALLADA",
        options=opciones,
        key="selector_global_empresas"
    )

    # Control de Vistas: Ficha de detalle vs Catálogo
    if seleccion_str != "--- Seleccione una empresa ---":
        empresa_seleccionada = next(
            (emp for emp in empresas if f"{emp['ticker']} - {emp['nombre']}" == seleccion_str),
            None
        )
        if empresa_seleccionada:
            st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)
            _mostrar_ficha(empresa_seleccionada)
    else:
        _mostrar_listado(empresas)