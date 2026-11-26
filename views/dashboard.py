import html as _html
import streamlit as st
from database import (
    query_all,
    query_val,
    obtener_watchlist,
    calcular_posiciones_abiertas_filtradas,
    calcular_liquidez_filtrada,
    get_connection,
    obtener_empresas_disponibles,
)
import market_data
import pandas as pd
import os
import time
import datetime as _dt
import concurrent.futures
from services.alert_engine import obtener_alertas
from services.data_provider import get_intraday_variation, get_price_batch
from components.stock_chart import render_stock_chart

# ═══════════════════════════════════════════════════════════════════
# CF-013: Sincronización de eventos desacoplada del Dashboard.
# Las funciones sincronizar_eventos_db() y obtener_eventos_db()
# ahora residen en services/event_sync.py y se ejecutan globalmente
# desde app.py al inicio de cada sesión.
# ═══════════════════════════════════════════════════════════════════
from services.event_sync import sincronizar_eventos_db, obtener_eventos_db


@st.cache_data(ttl=300, show_spinner=False)
def obtener_variaciones_rapidas(mercado: str = None) -> list:
    """
    Función RÁPIDA: descarga solo precios y calcula variaciones diarias.
    Usa ThreadPoolExecutor con get_intraday_variation de data_provider en paralelo para obtener la 
    variación intradía exacta en tiempo real, con fallback a get_price_batch
    únicamente para los tickers fallidos.
    """
    try:
        if mercado:
            empresas_rows = query_all(
                "SELECT ticker, nombre, es_ibex35 FROM empresas WHERE activa = 1 AND mercado = ? ORDER BY nombre",
                (mercado,)
            )
        else:
            empresas_rows = query_all(
                "SELECT ticker, nombre, es_ibex35 FROM empresas WHERE activa = 1 ORDER BY nombre"
            )
        if not empresas_rows:
            return []

        tickers_tupla = tuple(emp["ticker"].strip().upper() for emp in empresas_rows)

        # 1. Cargar variación en tiempo real en paralelo para todos los tickers (vía data_provider)
        variaciones_dict = {}
        def fetch_ticker_var(ticker):
            try:
                p, v = get_intraday_variation(ticker)
                if p and p > 0:
                    return ticker, p, v
                return ticker, None, None
            except Exception:
                return ticker, None, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            futures = {executor.submit(fetch_ticker_var, t): t for t in tickers_tupla}
            for future in concurrent.futures.as_completed(futures):
                try:
                    ticker, p, v = future.result(timeout=6)
                    if p is not None and v is not None:
                        variaciones_dict[ticker] = (p, v)
                except Exception:
                    pass

        # 2. Identificar tickers que no se pudieron obtener por tiempo real (fast_info)
        tickers_fallidos = [t for t in tickers_tupla if t not in variaciones_dict]

        # 3. Descargar histórico batch únicamente como fallback para los tickers fallidos
        datos_mercado = pd.DataFrame()
        if tickers_fallidos:
            datos_mercado = get_price_batch(tuple(tickers_fallidos))

        datos_variacion: list = []
        for emp in empresas_rows:
            ticker = emp["ticker"].strip().upper()
            precio_actual = 0.0
            variacion = 0.0
            encontrado = False

            # 1. Intentar usar la variación intradía en tiempo real obtenida
            if ticker in variaciones_dict:
                precio_actual, variacion = variaciones_dict[ticker]
                encontrado = True

            # 2. Fallback a datos del batch histórico (solo si falló tiempo real)
            if not encontrado and not datos_mercado.empty:
                try:
                    if isinstance(datos_mercado.columns, pd.MultiIndex):
                        if ticker in datos_mercado.columns.get_level_values(0):
                            df_ticker = datos_mercado[ticker]
                            p_rec, v_rec = get_intraday_variation(ticker, df_historico=df_ticker)
                            if p_rec > 0:
                                precio_actual = p_rec
                                variacion = v_rec
                                encontrado = True
                    else:
                        if 'Close' in datos_mercado.columns:
                            p_rec, v_rec = get_intraday_variation(ticker, df_historico=datos_mercado)
                            if p_rec > 0:
                                precio_actual = p_rec
                                variacion = v_rec
                                encontrado = True
                except Exception:
                    pass

            if encontrado:
                datos_variacion.append({
                    "nombre": emp["nombre"],
                    "ticker": ticker,
                    "precio": precio_actual,
                    "variacion": variacion,
                    "score_calidad": 0.0,
                    "score_oportunidad": 0.0,
                    "es_ibex35": int(emp.get("es_ibex35") or 0),
                    "moneda": "EUR",
                })
        return datos_variacion
    except Exception as e:
        print(f"Error en obtener_variaciones_rapidas: {e}")
        return []


@st.cache_data(ttl=1800, show_spinner=False)
def obtener_datos_pulso_mercado(mercado: str = None) -> list:
    """
    Función COMPLETA: precio + variación + scores de calidad/oportunidad.
    TTL 30 min — los scores fundamentales no cambian cada 5 minutos.
    Usada por: Top Calidad y Top Oportunidad.
    """
    try:
        if mercado:
            empresas_rows = query_all(
                "SELECT ticker, nombre, sector, es_ibex35 FROM empresas WHERE activa = 1 AND mercado = ? ORDER BY nombre",
                (mercado,)
            )
        else:
            empresas_rows = query_all(
                "SELECT ticker, nombre, sector, es_ibex35 FROM empresas WHERE activa = 1 ORDER BY nombre"
            )
        if not empresas_rows:
            return []

        tickers_tupla = tuple(emp["ticker"].strip().upper() for emp in empresas_rows)

        # La carga de fundamentales está cacheada 12h en ranking.py
        from views.ranking import cargar_todos_los_fundamentales_en_paralelo, calcular_score_stocklab_v7
        # Convertir a lista para la firma de cargar_todos_los_fundamentales_en_paralelo
        diccionario_fundamentales = cargar_todos_los_fundamentales_en_paralelo(list(tickers_tupla))
        datos_mercado = get_price_batch(tickers_tupla)

        datos_variacion: list = []
        if not datos_mercado.empty:
            es_multi = isinstance(datos_mercado.columns, pd.MultiIndex)
            for emp in empresas_rows:
                ticker = emp["ticker"].strip().upper()
                if es_multi:
                    if ticker not in datos_mercado.columns.get_level_values(0):
                        continue
                    df_ticker = datos_mercado[ticker]
                elif len(empresas_rows) == 1:
                    df_ticker = datos_mercado
                else:
                    continue
                if df_ticker.empty or 'Close' not in df_ticker.columns or df_ticker['Close'].isnull().all():
                    continue
                
                precios = df_ticker['Close'].dropna()
                info_f = diccionario_fundamentales.get(ticker, {})

                # Contrato Intradía SSOT: Precio actual y variación respecto a cierre anterior
                precio_actual, variacion = get_intraday_variation(
                    ticker=ticker,
                    df_historico=df_ticker,
                    fundamentales=info_f
                )

                score_v7  = calcular_score_stocklab_v7(
                    df_historico=df_ticker,
                    info=info_f,
                    variacion_diaria=variacion,
                    sector=emp.get("sector", "")
                )
                datos_variacion.append({
                    "nombre": emp["nombre"],
                    "ticker": ticker,
                    "precio": precio_actual,
                    "variacion": variacion,
                    "score_calidad":      score_v7.get('calidad',      0.0),
                    "score_oportunidad":  score_v7.get('oportunidad',  0.0),
                    "es_ibex35":          int(emp.get("es_ibex35") or 0),
                    "moneda": "EUR",
                })
        return datos_variacion
    except Exception as e:
        print(f"Error al obtener el pulso del mercado: {e}")
        return []


def _fmt_price_dash(precio) -> str:
    """Formatea un precio adaptando decimales para evitar truncar penny stocks (<1 EUR) a 0.00."""
    try:
        pf = float(precio)
    except (TypeError, ValueError):
        return "—"
    dec = 4 if abs(pf) < 1.0 else 2
    return f"{pf:.{dec}f}"


def render_mini_list_row(row, metric_type):
    """Render a single asset row as a Flexbox HTML div — no fixed height."""
    ticker_limpio = _html.escape(str(row['ticker']).replace('.MC', ''))
    nombre_raw = str(row['nombre'] or '')
    nombre_limpio = nombre_raw[:14] + ".." if len(nombre_raw) > 16 else nombre_raw
    nombre_limpio_esc = _html.escape(nombre_limpio)
    nombre_title_esc = _html.escape(nombre_raw)

    precio_txt = _fmt_price_dash(row['precio'])

    if metric_type == 'variacion':
        val = row['variacion']
        signo = "+" if val >= 0 else ""
        color_badge = "#34d399" if val >= 0 else "#f87171"
        bg_badge = "rgba(16, 185, 129, 0.12)" if val >= 0 else "rgba(239, 68, 68, 0.12)"
        texto_badge = f"{signo}{val:.2f}%"
    elif metric_type == 'oportunidad':
        val = row['score_oportunidad']
        color_badge = "#38bdf8"
        bg_badge = "rgba(56, 189, 248, 0.10)"
        texto_badge = f"{val:.1f}"
    else:  # calidad
        val = row['score_calidad']
        color_badge = "#f59e0b"
        bg_badge = "rgba(245, 158, 11, 0.10)"
        texto_badge = f"{val:.1f}"

    # Build clean, single-line HTML — no leading whitespace that Streamlit could emit as stray text
    return (
        f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:7px 0;margin:0;">'
        f'<div style="display:flex;flex-direction:column;gap:3px;overflow:hidden;min-width:0;flex:1;">'
        f'<span style="font-family:\'Inter\',sans-serif;font-weight:700;font-size:0.82rem;color:#e8ecf1;line-height:1.2;'
        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
        f'{ticker_limpio}</span>'
        f'<span style="font-family:\'Inter\',sans-serif;font-size:0.71rem;color:#8c9ba5;line-height:1.2;'
        f'white-space:nowrap;text-overflow:ellipsis;overflow:hidden;" title="{nombre_title_esc}">'
        f'{nombre_limpio_esc}</span>'
        f'</div>'
        f'<div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">'
        f'<span style="font-family:\'JetBrains Mono\',monospace;font-weight:500;font-size:0.76rem;color:#94a3b8;white-space:nowrap;">'
        f'{precio_txt}</span>'
        f'<span style="background:{bg_badge};color:{color_badge};font-family:\'JetBrains Mono\',monospace;'
        f'font-weight:700;font-size:0.70rem;padding:2px 6px;border-radius:4px;min-width:50px;text-align:right;white-space:nowrap;">'
        f'{texto_badge}</span>'
        f'</div>'
        f'</div>'
    )

def show():
    # ── CSS Premium Institucional — Unified StockLab Design System ────
    st.markdown("""
<style>
    /* ═══════════════════════════════════════════════════════════════════
       🎨 STOCKLAB DASHBOARD — Premium Dark Trading Terminal Theme
       Unified with Mi Cartera V2, Ranking & Watchlist Design System
       Inspired by TradingView Pro / Binance / Bloomberg Terminal
       ═══════════════════════════════════════════════════════════════════ */

    /* ── Google Font: cargada centralizadamente en app.py ──────────── */

    /* ── Keyframe Animations ────────────────────────────────────────── */
    @keyframes fade-in-up {
        0%   { opacity: 0; transform: translateY(6px); }
        100% { opacity: 1; transform: translateY(0); }
    }

    @keyframes gradient-shift {
        0%   { background-position: 0% 50%; }
        50%  { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
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
    ::-webkit-scrollbar {
        width: 6px !important;
        height: 6px !important;
    }
    ::-webkit-scrollbar-track {
        background: #0d121c !important;
        border-radius: 3px !important;
    }
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

    h5, .stMarkdown h5 {
        color: #f1f5f9 !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📊 TARJETAS KPI — Métricas con Brillo Neón Sutil
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stMetric"] {
        background: var(--sl-surface-glass) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: var(--sl-radius-lg) !important;
        padding: 20px 18px !important;
        transition: transform 240ms var(--sl-ease), border-color 240ms var(--sl-ease), box-shadow 240ms var(--sl-ease) !important;
        box-shadow: var(--sl-shadow-card) !important;
        animation: fade-in-up 0.24s var(--sl-ease) both !important;
        position: relative !important;
        overflow: hidden !important;
    }

    /* Subtle top accent line on KPI cards */
    div[data-testid="stMetric"]::before {
        content: "" !important;
        display: block !important;
        position: absolute !important;
        top: 0 !important;
        left: 18% !important;
        right: 18% !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.55), transparent) !important;
        opacity: 0.65 !important;
        transition: opacity 240ms var(--sl-ease), left 240ms var(--sl-ease), right 240ms var(--sl-ease) !important;
    }

    div[data-testid="stMetric"]:hover {
        border-color: var(--sl-border-hover) !important;
        box-shadow: var(--sl-shadow-hover) !important;
        transform: translateY(-3px) !important;
    }

    div[data-testid="stMetric"]:hover::before {
        left: 8% !important;
        right: 8% !important;
        opacity: 1 !important;
    }

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

    /* Positive delta */
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"][style*="color: rgb(9, 171, 59)"],
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"] svg[style*="fill: rgb(9, 171, 59)"] {
        filter: none !important;
    }

    /* Negative delta */
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"][style*="color: rgb(255, 43, 43)"],
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"] svg[style*="fill: rgb(255, 43, 43)"] {
        filter: none !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📈 CHART CONTAINERS — Seamless Graph Integration
       ═══════════════════════════════════════════════════════════════════ */

    /* Plotly chart wrapper */
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

    /* Altair / VegaLite chart wrapper */
    div[data-testid="stVegaLiteChart"] {
        background: linear-gradient(180deg, #131a29 0%, #101623 100%) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 14px !important;
        padding: 16px !important;
        transition: all 0.3s ease !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
    }

    div[data-testid="stVegaLiteChart"]:hover {
        border-color: rgba(56, 189, 248, 0.18) !important;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📦 CONTAINERS — Glassmorphic Panels (Destacadas sidebar)
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
        background: linear-gradient(180deg, #131a29 0%, #101623 100%) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 14px !important;
        padding: 12px 14px !important;
        transition: all 0.35s cubic-bezier(0.25, 0.46, 0.45, 0.94) !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
        position: relative !important;
        overflow: hidden !important;
    }

    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"]::before { display: none !important; }

    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: rgba(56, 189, 248, 0.28) !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.32), 0 12px 28px -10px rgba(0, 0, 0, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
        transform: translateY(-2px) !important;
    }

    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"]:hover::before { display: none !important; }

    /* ═══════════════════════════════════════════════════════════════════
       🔀 SEGMENTED CONTROL — Period Toggle (1D, 5D, 1M, 6M, YTD, 5Y)
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stSegmentedControl"] button {
        background: var(--sl-bg-sunken) !important;
        border: 1px solid var(--sl-border-default) !important;
        color: var(--sl-text-secondary) !important;
        font-weight: 600 !important;
        font-family: var(--sl-font-mono) !important;
        transition: all 200ms var(--sl-ease) !important;
        border-radius: 8px !important;
        font-size: 0.78rem !important;
        padding: 5px 11px !important;
        white-space: nowrap !important;
    }

    div[data-testid="stSegmentedControl"] button:hover {
        background: rgba(56, 189, 248, 0.09) !important;
        color: #f8fafc !important;
        border-color: rgba(56, 189, 248, 0.32) !important;
    }

    div[data-testid="stSegmentedControl"] button[aria-checked="true"],
    div[data-testid="stSegmentedControl"] button[aria-pressed="true"],
    div[data-testid="stSegmentedControl"] button[kind="segmented_controlActive"] {
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.20), rgba(99, 102, 241, 0.14)) !important;
        border-color: rgba(56, 189, 248, 0.50) !important;
        color: #7dd3fc !important;
        box-shadow: 0 0 16px -4px rgba(56, 189, 248, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.12) !important;
    }

    /* Align segmented controls inside headers flush-right on a single crisp row (Streamlit 1.58 stButtonGroup) */
    .st-key-periodo_ibex_dashboard,
    .st-key-dash_filtro_alertas,
    div[data-testid="stElementContainer"]:has(div[data-testid="stButtonGroup"]),
    div[data-testid="stElementContainer"]:has(div[data-testid="stSegmentedControl"]),
    div[data-testid="stSegmentedControl"] {
        display: flex !important;
        justify-content: flex-end !important;
        width: 100% !important;
    }
    .st-key-periodo_ibex_dashboard > div,
    .st-key-dash_filtro_alertas > div,
    div[data-testid="stButtonGroup"],
    div[data-testid="stSegmentedControl"] > div,
    div[data-testid="stSegmentedControl"] [data-baseweb="button-group"] {
        display: flex !important;
        flex-wrap: nowrap !important;
        justify-content: flex-end !important;
        margin-left: auto !important;
        width: auto !important;
        gap: 4px !important;
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

    [data-baseweb="popover"] {
        background-color: #111723 !important;
        border: 1px solid rgba(56, 189, 248, 0.18) !important;
        border-radius: 12px !important;
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.85) !important;
    }

    [data-baseweb="menu"] {
        background-color: #111723 !important;
    }

    [data-baseweb="menu"] li {
        color: #cbd5e1 !important;
        transition: all 0.2s ease !important;
        font-family: 'Inter', sans-serif !important;
    }

    [data-baseweb="menu"] li:hover {
        background-color: rgba(56, 189, 248, 0.08) !important;
        color: #38bdf8 !important;
    }

    /* Form & Selectbox label typography - 100% harmonized with Ranking, Cartera, Watchlist & Empresas */
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
        margin: 2rem 0 !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🌐 GLOBAL TEXT & MISC — Typography Polish
       ═══════════════════════════════════════════════════════════════════ */
    .stMarkdown p {
        font-family: 'Inter', sans-serif !important;
    }

    .stMarkdown strong {
        color: #f1f5f9 !important;
    }

    /* Caption text polish */
    .stCaption, [data-testid="stCaptionContainer"] {
        color: #78879c !important;
        font-family: 'Inter', sans-serif !important;
    }

    /* Tooltip styling */
    div[data-testid="stTooltipContent"] {
        background-color: #182133 !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 8px !important;
        color: #cbd5e1 !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.32), 0 12px 28px -10px rgba(0, 0, 0, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🖥️ RESPONSIVE — Smaller screens polish
       ═══════════════════════════════════════════════════════════════════ */
    @media (max-width: 768px) {
        div[data-testid="stMetric"] {
            padding: 16px 14px !important;
        }
        div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
            font-size: 1.2rem !important;
        }
    }

    /* Las listas y el monitor usan la misma superficie glassmorphic que los indicadores. */
    .dash-panel {
        position: relative;
        overflow: hidden;
        background: var(--sl-surface-glass);
        border: 1px solid var(--sl-border-default);
        border-radius: var(--sl-radius-lg);
        box-shadow: var(--sl-shadow-card);
        box-sizing: border-box;
        padding: 17px 19px;
        min-width: 0;
        transition: transform 240ms var(--sl-ease), border-color 240ms var(--sl-ease), box-shadow 240ms var(--sl-ease);
    }
    .dash-panel::before {
        content: "";
        position: absolute;
        top: 0;
        left: 16%;
        right: 16%;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.42), transparent);
        opacity: 0.65;
        transition: opacity 240ms var(--sl-ease), left 240ms var(--sl-ease), right 240ms var(--sl-ease);
        pointer-events: none;
    }
    .dash-panel:hover {
        transform: translateY(-2px);
        border-color: var(--sl-border-hover);
        box-shadow: var(--sl-shadow-hover);
    }
    .dash-panel:hover::before {
        left: 8%;
        right: 8%;
        opacity: 1;
    }
    .dash-summary-panel {
        min-height: 158px;
        height: 100%;
        display: flex;
        flex-direction: column;
    }
    .dash-panel-label {
        color: var(--sl-text-secondary);
        font: 700 0.72rem/1.4 var(--sl-font-body);
        letter-spacing: 0.085em;
        text-transform: uppercase;
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 12px;
        padding-bottom: 8px;
        border-bottom: 1px solid var(--sl-border-subtle);
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
    .dash-divider {
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(148, 163, 184, 0.16), transparent);
        margin: 24px 0 20px;
    }

    /* ── Terminal Empty State Shell ──────────────────────────────────── */
    .dash-empty-shell {
        background: linear-gradient(180deg, #121826 0%, #0e1420 100%);
        border: 1px dashed rgba(148, 163, 184, 0.22);
        border-radius: 16px;
        padding: 36px 28px;
        text-align: center;
        margin: 16px 0;
        position: relative;
        overflow: hidden;
    }
    .dash-empty-shell::before { display: none !important; }
    .dash-empty-icon {
        font-size: 2.2rem;
        margin-bottom: 10px;
    }
    .dash-empty-title {
        font-family: 'Inter', sans-serif;
        font-size: 1.05rem;
        font-weight: 700;
        color: #e2e8f0;
        margin-bottom: 6px;
    }
    .dash-empty-desc {
        font-family: 'Inter', sans-serif;
        font-size: 0.84rem;
        color: var(--sl-text-secondary);
        max-width: 480px;
        margin: 0 auto;
        line-height: 1.45;
    }
</style>
""", unsafe_allow_html=True)

    # ── Cabecera de página premium institucional ─────────────────────────
    st.html("""
    <div class="sl-page-header">
        <h1 class="sl-page-title">Dashboard</h1>
        <p class="sl-page-subtitle">Visión macroeconómica, pulso de mercado y métricas ejecutivas</p>
    </div>
    """)

    try:
        # ── Mercados / segmentos disponibles ─────────────────────────────
        mercados_rows = query_all(
            "SELECT DISTINCT mercado FROM empresas WHERE mercado IS NOT NULL AND activa = 1 ORDER BY mercado"
        )
        mercados_db = [row["mercado"] for row in mercados_rows]
    except Exception as e:
        st.error(f"Error al conectar con la base de datos: {e}")
        return

    opciones_mercado = []
    if "BME" in mercados_db:
        opciones_mercado.extend(["BME", "IBEX 35", "Mercado Continuo"])
    for m_db in mercados_db:
        if m_db != "BME":
            opciones_mercado.append(m_db)

    # Sincronizar cartera activa con Mi Cartera (por defecto la que tenga posiciones, o SIMULADA)
    if "tipo_cartera_activa" not in st.session_state:
        try:
            _pos_r_init = calcular_posiciones_abiertas_filtradas("REAL")
            st.session_state["tipo_cartera_activa"] = "REAL" if _pos_r_init else "SIMULADA"
        except Exception:
            st.session_state["tipo_cartera_activa"] = "SIMULADA"

    _tipo_cartera_cur = st.session_state.get("tipo_cartera_activa", "SIMULADA")
    if _tipo_cartera_cur not in ("SIMULADA", "REAL"):
        _tipo_cartera_cur = "SIMULADA"

    col_sel_merc, col_sel_cart = st.columns([2.4, 1.2])
    with col_sel_merc:
        if opciones_mercado:
            _mercado_labels = {
                "BME": "BME (Todos)",
                "IBEX 35": "IBEX 35",
                "Mercado Continuo": "Mercado Continuo",
            }
            mercado_sel = st.selectbox(
                "MERCADO",
                opciones_mercado,
                index=0,
                key="dashboard_mercado_sel",
                format_func=lambda m: _mercado_labels.get(m, m),
            )
        else:
            mercado_sel = None
            st.info("No hay mercados registrados todavía.")

    with col_sel_cart:
        _cartera_opts = ["SIMULADA", "REAL"]
        tipo_cartera_dash = st.selectbox(
            "CARTERA ACTIVA",
            _cartera_opts,
            index=_cartera_opts.index(_tipo_cartera_cur),
            key="dashboard_cartera_sel",
            format_func=lambda c: "Simulada" if c == "SIMULADA" else "Real",
        )
        if tipo_cartera_dash != st.session_state.get("tipo_cartera_activa"):
            st.session_state["tipo_cartera_activa"] = tipo_cartera_dash

    _cartera_etq = "Simulada" if tipo_cartera_dash == "SIMULADA" else "Real"
    _cartera_origen_alerta = f"Cartera {_cartera_etq}"

    mercado_base_db = "BME" if mercado_sel in ("BME", "IBEX 35", "Mercado Continuo") else mercado_sel
    _mercado_subtitulo = {
        "BME": "Bolsas y Mercados Españoles",
        "IBEX 35": "Índice selectivo IBEX 35",
        "Mercado Continuo": "Empresas BME fuera del IBEX 35",
    }.get(mercado_sel, f"Mercado {mercado_sel}" if mercado_sel else "Bolsas y Mercados Españoles")

    st.markdown('<div class="dash-divider"></div>', unsafe_allow_html=True)

    # ── KPIs ─────────────────────────────────────────────────────────
    df_watchlist = pd.DataFrame()
    posiciones_cartera = []
    liquidez_cartera = 0.0
    if mercado_sel:
        try:
            if mercado_sel == "IBEX 35":
                total_empresas = query_val(
                    "SELECT COUNT(*) FROM empresas WHERE mercado = 'BME' AND es_ibex35 = 1 AND activa = 1"
                )
            elif mercado_sel == "Mercado Continuo":
                total_empresas = query_val(
                    "SELECT COUNT(*) FROM empresas WHERE mercado = 'BME' AND COALESCE(es_ibex35, 0) = 0 AND activa = 1"
                )
            else:
                total_empresas = query_val(
                    "SELECT COUNT(*) FROM empresas WHERE mercado = ? AND activa = 1",
                    (mercado_sel,),
                )

            # Usar la nueva función de watchlist para obtener el conteo real
            df_watchlist = obtener_watchlist()
            total_watchlist = len(df_watchlist)

            posiciones_cartera = calcular_posiciones_abiertas_filtradas(tipo_cartera_dash)
            liquidez_cartera = float(calcular_liquidez_filtrada(tipo_cartera_dash) or 0.0)
            total_cartera = len(posiciones_cartera)
        except Exception as e:
            st.error(f"Error al obtener estadísticas: {e}")
            total_empresas = 0
            total_watchlist = 0
            total_cartera = 0
            liquidez_cartera = 0.0
    else:
        total_empresas = 0
        total_watchlist = 0
        total_cartera = 0
        liquidez_cartera = 0.0

    # ── Tarjetas métricas institucionales ─────────────────────────────
    st.markdown(f"""
    <div class="sl-kpi-grid" style="--sl-columns:4">
      <div class="sl-kpi-card" style="--sl-kpi-accent:var(--sl-accent)">
        <div class="sl-kpi-value">
          {_html.escape(str(mercado_sel or "—"))}
        </div>
        <div class="sl-kpi-label">
          Mercado Activo
        </div>
        <div class="sl-kpi-caption">{_html.escape(str(_mercado_subtitulo))}</div>
      </div>
      <div class="sl-kpi-card" style="--sl-kpi-accent:var(--sl-pos)">
        <div class="sl-kpi-value">
          {total_empresas}
        </div>
        <div class="sl-kpi-label">
          Empresas en Cobertura
        </div>
        <div class="sl-kpi-caption">Activos analizados en {_html.escape(str(mercado_sel or 'BME'))}</div>
      </div>
      <div class="sl-kpi-card" style="--sl-kpi-accent:var(--sl-warn)">
        <div class="sl-kpi-value">
          {total_watchlist}
        </div>
        <div class="sl-kpi-label">
          Radar Watchlist
        </div>
        <div class="sl-kpi-caption">Objetivos tácticos en radar</div>
      </div>
      <div class="sl-kpi-card" style="--sl-kpi-accent:#b4a7fc">
        <div class="sl-kpi-value">
          {total_cartera}
        </div>
        <div class="sl-kpi-label">
          Posiciones ({_cartera_etq})
        </div>
        <div class="sl-kpi-caption">Liquidez: {liquidez_cartera:,.2f} €</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="dash-divider"></div>', unsafe_allow_html=True)

    # ── Estructura Principal: Gráfico IBEX + Pulso del Mercado ───────

    # ── Helper compartido por ambas fases ────────────────────────────
    def _build_top_card(emoji, label, rows_html):
        """Floating dark-glass HTML card for a top-N list."""
        return (
            '<div class="dash-panel"><div class="dash-panel-label">'
            f'<span>{label}</span></div>'
            f'<div style="display:flex;flex-direction:column;">{rows_html}</div>'
            '</div>'
        )

    def _rows(df_sub, metric):
        html_parts = []
        rows_list = list(df_sub.iterrows())
        for i, (_, row) in enumerate(rows_list):
            html_parts.append(render_mini_list_row(row, metric))
            if i < len(rows_list) - 1:
                html_parts.append(
                    "<div style='height:1px;background:rgba(148,163,184,0.08);margin:0;'></div>"
                )
        return "".join(html_parts)

    # ═══════════════════════════════════════════════════════════════════
    # SINCRONIZACIÓN UNIFICADA DE MERCADO — StockLab Loading Terminal
    # Descarga y cálculo en paralelo de cotizaciones y métricas clave
    # ═══════════════════════════════════════════════════════════════════
    with st.spinner("Sincronizando panel de mercado y cotizaciones en tiempo real..."):
        try:
            datos_universo = obtener_datos_pulso_mercado(mercado_base_db)
        except Exception:
            datos_universo = []

        if not datos_universo:
            try:
                datos_universo = obtener_variaciones_rapidas(mercado_base_db)
            except Exception:
                datos_universo = []

        # Filtrar en memoria según segmento elegido (instantáneo, sin nuevas descargas)
        if mercado_sel == "IBEX 35":
            datos_variacion = [d for d in datos_universo if int(d.get("es_ibex35") or 0) == 1]
        elif mercado_sel == "Mercado Continuo":
            datos_variacion = [d for d in datos_universo if int(d.get("es_ibex35") or 0) == 0]
        else:
            datos_variacion = datos_universo

    # ── Tarjetas de Pulso del Mercado (Renderizado Inmediato y Completo) ──
    cant_cotizadas = len(datos_variacion) if datos_variacion else 0
    _amp_html = ""
    if datos_variacion:
        _n_suben = sum(1 for d in datos_variacion if float(d.get("variacion") or 0) > 0.05)
        _n_bajan = sum(1 for d in datos_variacion if float(d.get("variacion") or 0) < -0.05)
        _n_planas = max(0, cant_cotizadas - _n_suben - _n_bajan)
        _amp_html = (
            f' <span style="display:inline-flex;align-items:center;gap:8px;margin-left:8px;'
            f'padding:2px 10px;border-radius:999px;background:rgba(15,23,42,0.75);'
            f'border:1px solid rgba(148,163,184,0.16);font-family:\'JetBrains Mono\',monospace;'
            f'font-size:0.71rem;font-weight:600;">'
            f'<span style="color:#34d399;">▲ {_n_suben}</span>'
            f'<span style="color:#64748b;">·</span>'
            f'<span style="color:#94a3b8;">● {_n_planas}</span>'
            f'<span style="color:#64748b;">·</span>'
            f'<span style="color:#f87171;">▼ {_n_bajan}</span>'
            f'</span>'
        )
    st.markdown(
        f'<div class="sl-section-title">Pulso del Mercado '
        f'<span class="active-counter-badge">[ {cant_cotizadas} cotizadas ]</span>{_amp_html}</div>',
        unsafe_allow_html=True,
    )
    col_c1, col_c2, col_c3, col_c4 = st.columns(4)

    if datos_variacion:
        df_completo = pd.DataFrame(datos_variacion)
        top_ganadoras   = df_completo.sort_values("variacion", ascending=False).head(3)
        top_perdedoras  = df_completo.sort_values("variacion", ascending=True).head(3)
        top_oportunidad = df_completo.sort_values("score_oportunidad", ascending=False).head(3) if "score_oportunidad" in df_completo.columns else pd.DataFrame()
        top_calidad     = df_completo.sort_values("score_calidad", ascending=False).head(3) if "score_calidad" in df_completo.columns else pd.DataFrame()

        with col_c1:
            st.markdown(
                _build_top_card("🚀", "Top Ganadoras",  _rows(top_ganadoras,  'variacion')),
                unsafe_allow_html=True,
            )
        with col_c2:
            st.markdown(
                _build_top_card("🔻", "Top Perdedoras", _rows(top_perdedoras, 'variacion')),
                unsafe_allow_html=True,
            )
        with col_c3:
            if not top_oportunidad.empty and top_oportunidad["score_oportunidad"].max() > 0:
                st.markdown(
                    _build_top_card("🎯", "Top Oportunidad", _rows(top_oportunidad, 'oportunidad')),
                    unsafe_allow_html=True,
                )
            else:
                st.info("Sin datos de oportunidad")
        with col_c4:
            if not top_calidad.empty and top_calidad["score_calidad"].max() > 0:
                st.markdown(
                    _build_top_card("🏆", "Top Calidad", _rows(top_calidad, 'calidad')),
                    unsafe_allow_html=True,
                )
            else:
                st.info("Sin datos de calidad")
    else:
        with col_c1:
            st.info("Sin datos de mercado")
        with col_c2:
            st.info("Sin datos de mercado")
        with col_c3:
            st.info("Sin datos de mercado")
        with col_c4:
            st.info("Sin datos de mercado")

    st.markdown('<div class="dash-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sl-section-title">Cotización Histórica IBEX 35</div>', unsafe_allow_html=True)

    # 3. GRÁFICO IBEX 35 A PANTALLA COMPLETA
    with st.container(border=False):
        # Header: info a la izquierda, selector de periodo pegado a la derecha en una sola fila
        col_graf_info, col_graf_periodos = st.columns([3.8, 2.7], vertical_alignment="center")

        with col_graf_periodos:
            opciones_periodo_ibex = ["1D", "5D", "1M", "6M", "YTD", "5Y"]
            periodo_ibex = st.segmented_control(
                "Selección de Período",
                options=opciones_periodo_ibex,
                default="6M",
                key="periodo_ibex_dashboard",
                label_visibility="collapsed"
            )
            if not periodo_ibex:
                periodo_ibex = "6M"

        mapeo_periodo_ibex = {
            "1D": ("1d", "5m"),
            "5D": ("5d", "30m"),
            "1M": ("1mo", "1d"),
            "6M": ("6mo", "1d"),
            "YTD": ("ytd", "1d"),
            "5Y": ("5y", "1d")
        }

        api_period_ibex, api_interval_ibex = mapeo_periodo_ibex[periodo_ibex]

        try:
            with st.spinner("Cargando histórico del IBEX 35..."):
                df_ibex = market_data.get_price_history("^IBEX", period=api_period_ibex, interval=api_interval_ibex)
            
            df_ibex = df_ibex.dropna(subset=['Close'])

            if not df_ibex.empty:
                # Lectura única de datos de mercado para 1D (evita 3 llamadas redundantes)
                datos_mercado_ibex = None
                if periodo_ibex == "1D":
                    try:
                        datos_mercado_ibex = market_data.get_stock_data("^IBEX") or {}
                    except Exception:
                        datos_mercado_ibex = {}

                    # Estructuración robusta del DataFrame intradía 1D (velas 5m):
                    # - No desplazar 5D (son velas de 30m, desplazar +5m distorsiona las marcas horarias).
                    # - Solo añadir el punto de subasta de cierre 17:35 si la sesión ya ha cerrado.
                    try:
                        precio_subasta_real_ibex = datos_mercado_ibex.get('precio_actual')

                        if precio_subasta_real_ibex is not None:
                            precio_apertura_real_ibex = float(df_ibex['Open'].iloc[0])

                            df_shifted_ibex = df_ibex.copy()
                            df_shifted_ibex.index = df_shifted_ibex.index + pd.Timedelta(minutes=5)

                            partes_ibex = []
                            idx_0900_ibex = df_shifted_ibex.index[0] - pd.Timedelta(minutes=5)
                            partes_ibex.append(pd.DataFrame({
                                'Open': [precio_apertura_real_ibex],
                                'High': [precio_apertura_real_ibex],
                                'Low': [precio_apertura_real_ibex],
                                'Close': [precio_apertura_real_ibex],
                                'Volume': [0]
                            }, index=[idx_0900_ibex]))
                            partes_ibex.append(df_shifted_ibex)

                            # Verificar si la última vela pertenece a una sesión ya cerrada (o >= 17:30)
                            last_ts = df_shifted_ibex.index[-1]
                            idx_1735_ibex = last_ts.normalize() + pd.Timedelta(hours=17, minutes=35)
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

                            if sesion_cerrada and last_ts < idx_1735_ibex:
                                partes_ibex.append(pd.DataFrame({
                                    'Open': [precio_subasta_real_ibex],
                                    'High': [precio_subasta_real_ibex],
                                    'Low': [precio_subasta_real_ibex],
                                    'Close': [precio_subasta_real_ibex],
                                    'Volume': [0]
                                }, index=[idx_1735_ibex]))

                            df_grafico_ibex = pd.concat(partes_ibex).sort_index()
                            df_grafico_ibex['Fecha_Visible'] = df_grafico_ibex.index.strftime('%m-%d %H:%M')

                            df_ibex = df_grafico_ibex
                    except Exception:
                        pass

                precio_actual_ibex = float(df_ibex['Close'].iloc[-1])
                if periodo_ibex == "1D":
                    variacion_ibex = (datos_mercado_ibex or {}).get('variacion_diaria')
                    if variacion_ibex is None:
                        ref_val_ibex = (datos_mercado_ibex or {}).get('previous_close')
                        if ref_val_ibex and ref_val_ibex > 0:
                            variacion_ibex = ((precio_actual_ibex - ref_val_ibex) / ref_val_ibex) * 100.0
                        else:
                            ref_val_ibex = float(df_ibex['Open'].iloc[0])
                            variacion_ibex = ((precio_actual_ibex - ref_val_ibex) / ref_val_ibex) * 100.0 if ref_val_ibex != 0 else 0.0
                else:
                    ref_val_ibex = float(df_ibex['Close'].iloc[0])
                    variacion_ibex = ((precio_actual_ibex - ref_val_ibex) / ref_val_ibex) * 100.0 if ref_val_ibex != 0 else 0.0

                # ── Cabecera elegante: Título · Precio (pts) · Delta ─────────
                color_delta = "#34d399" if variacion_ibex >= 0 else "#f87171"
                bg_delta    = "rgba(52,211,153,0.12)" if variacion_ibex >= 0 else "rgba(248,113,113,0.12)"
                signo_delta = "+" if variacion_ibex >= 0 else ""
                with col_graf_info:
                    st.markdown(
                        f"""
<div style='display:flex; align-items:center; gap:8px 12px; padding:4px 4px 18px 4px; flex-wrap:wrap;'>
  <span style='font-family:Inter,sans-serif; font-weight:600; font-size:1.05rem;
               color:#94a3b8; letter-spacing:0.5px; white-space:nowrap;
               position:relative; top:1px;'>IBEX 35</span>
  <span style='font-family:JetBrains Mono,monospace; font-weight:700; font-size:1.35rem;
               color:#ffffff; letter-spacing:-0.5px; white-space:nowrap;'
  >{precio_actual_ibex:,.2f}</span>
  <span style='font-family:Inter,sans-serif; font-weight:500; font-size:0.85rem;
               color:#64748b; white-space:nowrap; position:relative; top:1px;'>pts</span>
  <span style='font-family:JetBrains Mono,monospace; font-weight:700; font-size:0.88rem;
               color:{color_delta}; background:{bg_delta};
               padding:3px 9px; border-radius:6px;
               border:1px solid {color_delta}22;
               white-space:nowrap;'>{signo_delta}{variacion_ibex:.2f}%</span>
</div>""",
                        unsafe_allow_html=True
                    )

                # Formateo de fechas para Hover y Ticks
                def formatear_fecha_hover(dt):
                    if periodo_ibex in ["1D", "5D"]:
                        return dt.strftime('%d %b, %H:%M')
                    else:
                        return dt.strftime('%d %b, %Y')

                df_ibex['Fecha_Hover'] = df_ibex.index.map(formatear_fecha_hover)

                # Ticks del Eje X espaciados y limpios (5 ticks)
                n_ticks = 5
                total_pts = len(df_ibex)
                tick_indices = [int(i * (total_pts - 1) / (n_ticks - 1)) for i in range(n_ticks)] if total_pts > 1 else [0]
                tick_indices = sorted(list(set(tick_indices)))

                tick_labels_map = {}
                for idx in tick_indices:
                    dt = df_ibex.index[idx]
                    label = df_ibex['Fecha_Hover'].iloc[idx]
                    if periodo_ibex == "1D":
                        text = dt.strftime('%H:%M')
                    elif periodo_ibex == "5D":
                        text = dt.strftime('%d %b %H:%M')
                    elif periodo_ibex in ["1M", "6M", "YTD"]:
                        text = dt.strftime('%d %b')
                    else:  # 5Y
                        text = dt.strftime('%Y')
                    tick_labels_map[label] = text

                color_linea = "#10B981" if variacion_ibex >= 0 else "#EF4444"

                render_stock_chart(
                    labels=df_ibex['Fecha_Hover'].tolist(),
                    prices=df_ibex['Close'].tolist(),
                    last_close=float(df_ibex['Close'].iloc[-1]),
                    line_color=color_linea,
                    tick_labels_map=tick_labels_map,
                    currency="pts",
                    ticker="^IBEX",
                    title="IBEX 35"
                )
            else:
                st.info("No hay datos históricos disponibles para el IBEX 35 en el periodo seleccionado.")

        except Exception as e:
            st.warning(f"No se pudo cargar los datos del IBEX 35: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # NUEVAS SECCIONES — Fila Resumen · Centro de Alertas · Próximos Eventos
    # Añadidas al final del Dashboard sin modificar nada de lo anterior.
    # ══════════════════════════════════════════════════════════════════════════
    # Los datos completos del mercado ya han sido sincronizados unificadamente
    # en la cabecera, alimentando alertas y widgets de resumen con datos reales.

    # ── Datos adicionales ──────────────────────────────────────────────────────
    posiciones_abiertas = posiciones_cartera

    # Carga de empresas para resolución de nombres en alertas
    try:
        empresas_rows = obtener_empresas_disponibles()
    except Exception:
        empresas_rows = []

    # Actualización silenciosa de eventos corporativos (máximo 1 vez por sesión, sin DELETE/commit por rerun)
    if not st.session_state.get("_eventos_sincronizados"):
        try:
            sincronizar_eventos_db()
            st.session_state["_eventos_sincronizados"] = True
        except Exception:
            pass

    try:
        eventos_db = obtener_eventos_db()
    except Exception:
        eventos_db = []

    # ── Motor Central de Alertas ──────────────────────────────────────────────
    # El Dashboard NO calcula alertas. Delega completamente en alert_engine.py
    # usando siempre el universo completo (datos_universo) para no perder activos
    # cuando el usuario filtra Pulso del Mercado por IBEX 35 o Mercado Continuo.
    alertas = obtener_alertas(
        df_watchlist=df_watchlist,
        posiciones_abiertas=posiciones_abiertas,
        datos_variacion=datos_universo,
        empresas_rows=empresas_rows,
        eventos_db=eventos_db,
        origen_cartera=_cartera_origen_alerta,
    )

    # Contadores derivados (para las tarjetas de resumen)
    n_criticas     = sum(1 for a in alertas if a['estado'] == 'critica')
    n_advertencias = sum(1 for a in alertas if a['estado'] == 'advertencia')
    n_positivas    = sum(1 for a in alertas if a['estado'] == 'positiva')
    n_informativas = sum(1 for a in alertas if a['estado'] == 'informativa')
    total_alertas  = len(alertas)

    # ── Próximos Eventos ──────────────────────────────────────────────────────
    _hoy = _dt.date.today()
    _meses_es = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
                 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']

    def _fecha_str(d):
        dias = (d - _hoy).days
        if dias == 0:  return 'Hoy'
        if dias == 1:  return 'Mañana'
        return f"{d.day} {_meses_es[d.month - 1]}"

    eventos_proximos = []
    for ev in eventos_db:
        ev_fecha = ev['fecha']
        if ev_fecha >= _hoy:
            dias = (ev_fecha - _hoy).days
            eventos_proximos.append({
                'empresa': ev['empresa'],
                'evento': ev['evento'],
                'fecha': ev_fecha,
                'importancia': ev['importancia'],
                'tipo': ev['tipo'],
                'dias': dias,
                'fecha_str': _fecha_str(ev_fecha)
            })

    # Configuraciones de importancia y tipo de evento (sin iconos clipart)
    _imp_cfg = {
        'alta':  {'emoji': '', 'color': '#f87171',
                  'bg': 'rgba(239,68,68,0.12)',  'label': 'Alta'},
        'media': {'emoji': '', 'color': '#34d399',
                  'bg': 'rgba(16,185,129,0.12)', 'label': 'Media'},
        'baja':  {'emoji': '', 'color': '#fbbf24',
                  'bg': 'rgba(245,158,11,0.12)', 'label': 'Baja'},
        'info':  {'emoji': '', 'color': '#38bdf8',
                  'bg': 'rgba(56,189,248,0.10)',  'label': 'Info'},
    }
    _tipo_cfg = {
        'resultados':   {'icono': ''},
        'dividendo':    {'icono': ''},
        'ex_dividendo': {'icono': ''},
        'junta':        {'icono': ''},
        'otro':         {'icono': ''},
    }

    # ── Resumen de cartera para la fila ───────────────────────────────────────
    _cartera_rent_pct = None
    _cartera_pnl_hoy  = None
    dict_variacion = {
        str(d['ticker']).strip().upper(): d
        for d in (datos_universo or [])
        if d.get('ticker')
    }

    if posiciones_abiertas:
        _val_act = 0.0
        _coste_g = 0.0
        _pnl_hoy = 0.0
        _pos_con_precio = 0
        _pos_con_var = 0

        for _pos in posiciones_abiertas:
            ticker_k = str(_pos.get('ticker', '')).strip().upper()
            _acc = float(_pos.get('acciones') or 0)
            if _acc <= 0:
                continue

            _p = None
            _var = None
            if ticker_k in dict_variacion:
                _row = dict_variacion[ticker_k]
                _p = _row.get('precio')
                _var = _row.get('variacion')
            else:
                # Fallback individual si una posición no vino en el batch (p. ej. suspendida o fuera de mercado)
                try:
                    _fb = market_data.get_stock_data(ticker_k) or {}
                    _p = _fb.get('precio_actual')
                    _var = _fb.get('variacion_diaria')
                except Exception:
                    pass

            if _p is not None and not pd.isna(_p) and float(_p) > 0:
                _pf = float(_p)
                _val_act += _acc * _pf
                _coste_g += float(_pos.get('coste_total') or (_acc * _pf))
                _pos_con_precio += 1

                if _var is not None and not pd.isna(_var) and float(_var) != -100:
                    _vf = float(_var)
                    _p_ayer = _pf / (1 + _vf / 100.0)
                    _pnl_hoy += _acc * (_pf - _p_ayer)
                    _pos_con_var += 1

        if _pos_con_precio > 0 and _coste_g > 0:
            _cartera_rent_pct = ((_val_act - _coste_g) / _coste_g) * 100
        if _pos_con_var > 0:
            _cartera_pnl_hoy = _pnl_hoy

    # ── Objetivos alcanzados en watchlist ─────────────────────────────────────
    # Se respeta el tipo_objetivo para no generar falsos positivos:
    #   COMPRA_EN_CAIDA  → alcanzado si precio_actual <= precio_objetivo
    #   COMPRA_EN_RUPTURA → alcanzado si precio_actual >= precio_objetivo
    _wl_con_obj   = 0
    _wl_obj_alc   = 0
    if not df_watchlist.empty:
        _po_series = pd.to_numeric(df_watchlist['precio_objetivo'], errors='coerce')
        _wl_con_obj = int((_po_series > 0).sum())
        if dict_variacion:
            for _, _wlr in df_watchlist.iterrows():
                _po = _wlr.get('precio_objetivo')
                _tipo_obj = _wlr.get('tipo_objetivo', 'COMPRA_EN_CAIDA')
                try:
                    _precio_obj = float(_po)
                except (TypeError, ValueError):
                    continue
                if pd.isna(_precio_obj) or _precio_obj <= 0:
                    continue
                _tk_wl = str(_wlr.get('ticker', '')).strip().upper()
                _row_wl = dict_variacion.get(_tk_wl)
                if _row_wl and _row_wl.get('precio') is not None and not pd.isna(_row_wl.get('precio')):
                    _precio_act = float(_row_wl['precio'])
                    if _tipo_obj == 'COMPRA_EN_RUPTURA':
                        if _precio_act >= _precio_obj:
                            _wl_obj_alc += 1
                    else:  # COMPRA_EN_CAIDA (por defecto)
                        if _precio_act <= _precio_obj:
                            _wl_obj_alc += 1

    # ═══════════════════════════════════════════════════════════════════════════
    # FILA DE RESUMEN — 4 tarjetas al mismo estilo que Top Ganadoras
    # ═══════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="dash-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sl-section-title">Monitor Ejecutivo de Posiciones y Alertas</div>', unsafe_allow_html=True)

    def _mini_card(emoji, titulo, cuerpo_html):
        return (
            '<div class="dash-panel dash-summary-panel"><div class="dash-panel-label">'
            f'<span>{titulo}</span></div>'
            f'<div style="flex:1;display:flex;flex-direction:column;justify-content:center;overflow:hidden;">'
            f'{cuerpo_html}'
            f'</div>'
            f'</div>'
        )

    _col_s1, _col_s2, _col_s3, _col_s4 = st.columns(4)

    # — Tarjeta Alertas ────────────────────────────────────────────────────────
    with _col_s1:
        if total_alertas == 0:
            _cuerpo_alerta = (
                '<div style="font-family:\'Inter\',sans-serif;font-size:0.78rem;'
                'color:var(--sl-text-secondary);">No existen alertas activas</div>'
            )
        else:
            _badges = []
            if n_criticas:
                _badges.append(
                    f'<span style="display:inline-flex;align-items:center;gap:5px;background:rgba(239,68,68,0.12);color:#f87171;'
                    f'font-family:\'Inter\',sans-serif;font-size:0.68rem;font-weight:700;'
                    f'padding:2px 8px;border-radius:999px;border:1px solid rgba(239,68,68,0.24);">'
                    f'<span style="width:6px;height:6px;border-radius:50%;background:#f87171;display:inline-block;"></span>'
                    f'{n_criticas} crítica{"s" if n_criticas!=1 else ""}</span>'
                )
            if n_advertencias:
                _badges.append(
                    f'<span style="display:inline-flex;align-items:center;gap:5px;background:rgba(245,158,11,0.12);color:#fbbf24;'
                    f'font-family:\'Inter\',sans-serif;font-size:0.68rem;font-weight:700;'
                    f'padding:2px 8px;border-radius:999px;border:1px solid rgba(245,158,11,0.24);">'
                    f'<span style="width:6px;height:6px;border-radius:50%;background:#fbbf24;display:inline-block;"></span>'
                    f'{n_advertencias} moderada{"s" if n_advertencias != 1 else ""}</span>'
                )
            if n_positivas:
                _badges.append(
                    f'<span style="display:inline-flex;align-items:center;gap:5px;background:rgba(16,185,129,0.12);color:#34d399;'
                    f'font-family:\'Inter\',sans-serif;font-size:0.68rem;font-weight:700;'
                    f'padding:2px 8px;border-radius:999px;border:1px solid rgba(16,185,129,0.24);">'
                    f'<span style="width:6px;height:6px;border-radius:50%;background:#34d399;display:inline-block;"></span>'
                    f'{n_positivas} buena{"s" if n_positivas != 1 else ""}</span>'
                )
            if n_informativas:
                _badges.append(
                    f'<span style="display:inline-flex;align-items:center;gap:5px;background:rgba(56,189,248,0.12);color:#38bdf8;'
                    f'font-family:\'Inter\',sans-serif;font-size:0.68rem;font-weight:700;'
                    f'padding:2px 8px;border-radius:999px;border:1px solid rgba(56,189,248,0.24);">'
                    f'<span style="width:6px;height:6px;border-radius:50%;background:#38bdf8;display:inline-block;"></span>'
                    f'{n_informativas} informativa{"s" if n_informativas != 1 else ""}</span>'
                )
            _cuerpo_alerta = (
                f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:1.3rem;'
                f'font-weight:800;color:#e8ecf1;margin-bottom:6px;">{total_alertas}'
                f'<span style="font-size:0.75rem;color:#64748b;font-weight:500;'
                f'font-family:\'Inter\',sans-serif;"> alerta{"s" if total_alertas != 1 else ""} activa{"s" if total_alertas != 1 else ""}</span></div>'
                f'<div style="display:flex;gap:5px;flex-wrap:wrap;">'
                + " ".join(_badges) +
                f'</div>'
            )
        st.markdown(_mini_card("🔔", "Alertas", _cuerpo_alerta), unsafe_allow_html=True)

    # — Tarjeta Próximos Eventos ───────────────────────────────────────────────
    with _col_s2:
        if not eventos_proximos:
            _cuerpo_ev = (
                '<div style="font-family:\'Inter\',sans-serif;font-size:0.78rem;'
                'color:var(--sl-text-secondary);">Sin eventos próximos</div>'
            )
        else:
            _lineas_ev = []
            for _ev in eventos_proximos[:3]:
                _imp = _imp_cfg.get(_ev['importancia'], _imp_cfg['info'])
                _tipo = _tipo_cfg.get(_ev['tipo'], _tipo_cfg['otro'])
                _emp_ev_esc = _html.escape(str(_ev["empresa"] or ""))
                _lineas_ev.append(
                    f'<div style="display:flex;justify-content:space-between;'
                    f'align-items:center;padding:3px 0;">'
                    f'<span style="font-family:\'Inter\',sans-serif;font-size:0.75rem;'
                    f'color:#c4cad4;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:125px;" title="{_emp_ev_esc}">'
                    f'{_tipo["icono"]} {_emp_ev_esc}</span>'
                    f'<span style="font-family:\'JetBrains Mono\',monospace;'
                    f'font-size:0.68rem;color:{_imp["color"]};font-weight:700;'
                    f'white-space:nowrap;flex-shrink:0;">{_html.escape(str(_ev["fecha_str"]))}</span>'
                    f'</div>'
                )
                if _ev is not eventos_proximos[:3][-1]:
                    _lineas_ev.append(
                        "<div style='height:1px;background:rgba(255,255,255,0.04);margin:0;'>"
                        "</div>"
                    )
            _cuerpo_ev = "".join(_lineas_ev)
        st.markdown(_mini_card("📅", "Próximos Eventos", _cuerpo_ev), unsafe_allow_html=True)

    # — Tarjeta Mi Cartera ─────────────────────────────────────────────────────
    with _col_s3:
        if posiciones_abiertas:
            if _cartera_rent_pct is not None:
                _sgn_r = "+" if _cartera_rent_pct >= 0 else ""
                _clr_r = "#10b981" if _cartera_rent_pct >= 0 else "#ef4444"
                _bg_r  = "rgba(16,185,129,0.10)" if _cartera_rent_pct >= 0 else "rgba(239,68,68,0.10)"
                rent_html = f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:1.2rem;font-weight:800;color:{_clr_r};">{_sgn_r}{_cartera_rent_pct:.2f}%</span>'
            else:
                rent_html = f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:1.2rem;font-weight:800;color:#64748b;">--%</span>'
                _clr_r = "#64748b"
                _bg_r = "rgba(100,116,139,0.10)"
                
            if _cartera_pnl_hoy is not None:
                _sgn_h = "+" if _cartera_pnl_hoy >= 0 else ""
                _clr_h = "#10b981" if _cartera_pnl_hoy >= 0 else "#ef4444"
                pnl_html = f'{_sgn_h}{_cartera_pnl_hoy:.2f}€ hoy'
            else:
                _clr_h = "#64748b"
                pnl_html = '-- hoy'
                
            _cuerpo_cart = (
                f'<div style="display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 6px;margin-bottom:3px;">'
                f'{rent_html}'
                f'<span style="background:{_bg_r};color:{_clr_r};font-family:\'Inter\',sans-serif;'
                f'font-size:0.65rem;font-weight:700;padding:1px 5px;border-radius:4px;white-space:nowrap;">total</span>'
                f'</div>'
                f'<div style="font-family:\'Inter\',sans-serif;font-size:0.75rem;'
                f'color:{_clr_h};margin-bottom:2px;">'
                f'{pnl_html}</div>'
                f'<div style="font-family:\'Inter\',sans-serif;font-size:0.72rem;color:var(--sl-text-secondary);">'
                f'{len(posiciones_abiertas)} abiertas · {liquidez_cartera:,.2f}€ liq.</div>'
            )
        else:
            _cuerpo_cart = (
                f'<div style="font-family:\'Inter\',sans-serif;font-size:0.78rem;'
                f'color:var(--sl-text-secondary);margin-bottom:4px;">Sin posiciones abiertas</div>'
                f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:0.74rem;'
                f'color:#94a3b8;">Liquidez: {liquidez_cartera:,.2f} €</div>'
            )
        st.markdown(_mini_card("💼", f"Mi Cartera · {_cartera_etq}", _cuerpo_cart), unsafe_allow_html=True)

    # — Tarjeta Watchlist ──────────────────────────────────────────────────────
    with _col_s4:
        if total_watchlist > 0:
            _cuerpo_wl = (
                f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:1.3rem;'
                f'font-weight:800;color:#e8ecf1;margin-bottom:4px;">{total_watchlist}'
                f'<span style="font-size:0.75rem;color:#64748b;font-weight:500;'
                f'font-family:\'Inter\',sans-serif;"> empresas</span></div>'
            )
            if _wl_con_obj > 0:
                _clr_obj = "#34d399" if _wl_obj_alc > 0 else "#fbbf24"
                _cuerpo_wl += (
                    f'<div style="display:flex;align-items:center;gap:6px;font-family:\'Inter\',sans-serif;font-size:0.75rem;'
                    f'color:{_clr_obj};margin-bottom:2px;">'
                    f'<span style="width:6px;height:6px;border-radius:50%;background:{_clr_obj};box-shadow:0 0 8px {_clr_obj}88;display:inline-block;flex-shrink:0;"></span>'
                    f'{_wl_obj_alc} objetivo'
                    f'{"s" if _wl_obj_alc!=1 else ""} alcanzado'
                    f'{"s" if _wl_obj_alc!=1 else ""}</div>'
                    f'<div style="font-family:\'Inter\',sans-serif;font-size:0.72rem;'
                    f'color:var(--sl-text-muted);">{_wl_con_obj} con precio objetivo</div>'
                )
            else:
                _cuerpo_wl += (
                    '<div style="font-family:\'Inter\',sans-serif;font-size:0.75rem;'
                    'color:var(--sl-text-muted);">Sin precios objetivo definidos</div>'
                )
        else:
            _cuerpo_wl = (
                '<div style="font-family:\'Inter\',sans-serif;font-size:0.78rem;'
                'color:var(--sl-text-secondary);">Watchlist vacía</div>'
            )
        st.markdown(_mini_card("⭐", "Watchlist", _cuerpo_wl), unsafe_allow_html=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # CENTRO DE ALERTAS
    # ═══════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="dash-divider"></div>', unsafe_allow_html=True)
    badge_al_txt = f"{total_alertas} {'activa' if total_alertas == 1 else 'activas'}"

    _opts_al = ["Todas"]
    _cnt_al_map = {
        "Todas": total_alertas,
        "Críticas": n_criticas,
        "Positivas": n_positivas,
        "Moderadas": n_advertencias,
        "Informativas": n_informativas,
    }
    if n_criticas:
        _opts_al.append("Críticas")
    if n_positivas:
        _opts_al.append("Positivas")
    if n_advertencias:
        _opts_al.append("Moderadas")
    if n_informativas:
        _opts_al.append("Informativas")

    _fil_al = "Todas"
    if len(_opts_al) > 2:
        if st.session_state.get("dash_filtro_alertas") not in _opts_al:
            st.session_state["dash_filtro_alertas"] = "Todas"
        col_al_hdr, col_al_fil = st.columns([3.3, 2.7], vertical_alignment="center")
        with col_al_hdr:
            st.markdown(
                f'<div class="sl-section-title" style="margin-bottom:0;">Centro de Alertas <span class="active-counter-badge">[ {badge_al_txt} ]</span> '
                f'<span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:0.70rem;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:1.2px;margin-left:8px;">'
                f'│ &nbsp;Atención inmediata y niveles clave</span></div>',
                unsafe_allow_html=True,
            )
        with col_al_fil:
            _fil_al = st.segmented_control(
                "Filtrar alertas",
                options=_opts_al,
                default="Todas",
                key="dash_filtro_alertas",
                format_func=lambda o: o if o == "Todas" else f"{o} ({_cnt_al_map.get(o, 0)})",
                label_visibility="collapsed",
            ) or "Todas"
        st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
    else:
        st.markdown(
            f'<div class="sl-section-title">Centro de Alertas <span class="active-counter-badge">[ {badge_al_txt} ]</span> '
            f'<span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:0.70rem;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:1.2px;margin-left:8px;">'
            f'│ &nbsp;Atención inmediata y niveles clave</span></div>',
            unsafe_allow_html=True,
        )

    if not alertas:
        st.markdown("""
        <div class="dash-empty-shell">
            <div class="dash-empty-title">Sin Alertas Pendientes</div>
            <div class="dash-empty-desc">Todos los indicadores, cotizaciones y niveles clave se encuentran bajo control.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        if _fil_al == "Críticas":
            alertas_mostrar = [a for a in alertas if a.get("estado") == "critica"]
        elif _fil_al == "Positivas":
            alertas_mostrar = [a for a in alertas if a.get("estado") == "positiva"]
        elif _fil_al == "Moderadas":
            alertas_mostrar = [a for a in alertas if a.get("estado") == "advertencia"]
        elif _fil_al == "Informativas":
            alertas_mostrar = [a for a in alertas if a.get("estado") == "informativa"]
        else:
            alertas_mostrar = alertas

        for _alerta in alertas_mostrar:
            # Construir etiqueta de tiempo + origen
            _origen_label = str(_alerta.get('origen', '') or '')
            _tiempo_label = str(_alerta.get('fecha', 'Hoy') or 'Hoy')
            _meta_label   = _html.escape(
                f'{_tiempo_label} · {_origen_label}'
                if _origen_label else _tiempo_label
            )
            _emp_al_esc = _html.escape(str(_alerta.get("empresa", "") or ""))
            _msg_al_esc = _html.escape(str(_alerta.get("mensaje", "") or ""))
            _border_color = _alerta.get('color', '#38bdf8')
            st.markdown(
                f'<div style="'
                f'border-radius:12px;padding:16px 20px;margin-bottom:10px;'
                f'border:1px solid rgba(148,163,184,0.12);'
                f'border-left:3px solid {_border_color};'
                f'background:var(--sl-surface-glass);'
                f'box-shadow:var(--sl-shadow-card);'
                f'position:relative;overflow:hidden;">'
                f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;">'
                f'<div style="display:flex;align-items:flex-start;gap:12px;flex:1;">'
                f'<span style="width:8px;height:8px;border-radius:50%;background:{_border_color};box-shadow:0 0 10px {_border_color}99;margin-top:6px;flex-shrink:0;display:inline-block;"></span>'
                f'<div>'
                f'<div style="font-family:\'Inter\',sans-serif;font-weight:700;'
                f'font-size:0.92rem;color:#ffffff;margin-bottom:4px;letter-spacing:-0.2px;">'
                f'{_emp_al_esc}</div>'
                f'<div style="font-family:\'Inter\',sans-serif;font-size:0.83rem;'
                f'color:#cbd5e1;line-height:1.5;">{_msg_al_esc}</div>'
                f'</div></div>'
                f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:0.72rem;font-weight:600;'
                f'color:#94a3b8;background:rgba(255,255,255,0.04);border:1px solid rgba(100,116,139,0.2);'
                f'padding:4px 10px;border-radius:6px;white-space:nowrap;flex-shrink:0;">'
                f'{_meta_label}</span>'
                f'</div></div>',
                unsafe_allow_html=True
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # PRÓXIMOS EVENTOS
    # ═══════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="dash-divider"></div>', unsafe_allow_html=True)
    cant_ev = len(eventos_proximos)
    badge_ev_txt = f"{cant_ev} {'evento corporativo' if cant_ev == 1 else 'eventos corporativos'}"
    st.markdown(
        f'<div class="sl-section-title">Próximos Eventos <span class="active-counter-badge">[ {badge_ev_txt} ]</span> '
        f'<span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:0.70rem;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:1.2px;margin-left:8px;">'
        f'│ &nbsp;Calendario corporativo táctico</span></div>',
        unsafe_allow_html=True
    )

    if not eventos_proximos:
        st.markdown("""
        <div class="dash-empty-shell">
            <div class="dash-empty-title">Sin Eventos Próximos</div>
            <div class="dash-empty-desc">No hay presentaciones de resultados, juntas ni dividendos programados en los próximos días.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        _filas_ev = ""
        for _i, _ev in enumerate(eventos_proximos):
            _imp  = _imp_cfg.get(_ev['importancia'], _imp_cfg['info'])
            _tipo = _tipo_cfg.get(_ev['tipo'], _tipo_cfg['otro'])
            _bg_fila = "rgba(255,255,255,0.02)" if _i % 2 == 0 else "transparent"
            _dias_label = (
                "Hoy" if _ev['dias'] == 0
                else ("Mañana" if _ev['dias'] == 1
                      else f"+{_ev['dias']}d")
            )
            _emp_t_esc = _html.escape(str(_ev["empresa"] or ""))
            _ev_t_txt = str(_ev["evento"] or "").replace("Presentacion ", "Presentación ")
            _ev_t_esc = _html.escape(_ev_t_txt)
            _filas_ev += (
                f'<tr style="background:{_bg_fila};border-bottom:1px solid rgba(100,116,139,0.08);">'
                # Empresa
                f'<td style="padding:12px 18px;font-family:\'Inter\',sans-serif;'
                f'font-weight:700;font-size:0.86rem;color:#ffffff;white-space:nowrap;">'
                f'{_emp_t_esc}</td>'
                # Evento
                f'<td style="padding:12px 18px;font-family:\'Inter\',sans-serif;'
                f'font-size:0.83rem;color:#cbd5e1;">'
                f'{_tipo["icono"]} {_ev_t_esc}</td>'
                # Fecha
                f'<td style="padding:12px 18px;font-family:\'JetBrains Mono\',monospace;'
                f'font-size:0.81rem;color:#94a3b8;white-space:nowrap;">'
                f'{_html.escape(str(_ev["fecha_str"]))}</td>'
                # Días
                f'<td style="padding:12px 18px;text-align:center;">'
                f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:0.75rem;'
                f'color:#38bdf8;font-weight:600;background:rgba(56,189,248,0.10);border:1px solid rgba(56,189,248,0.24);'
                f'padding:3px 9px;border-radius:6px;">{_dias_label}</span></td>'
                # Importancia
                f'<td style="padding:12px 18px;text-align:center;">'
                f'<span style="background:{_imp["bg"]};color:{_imp["color"]};'
                f'font-family:\'Inter\',sans-serif;font-size:0.72rem;font-weight:600;'
                f'padding:3px 12px;border-radius:6px;border:1px solid {_imp["color"]}30;'
                f'white-space:nowrap;">'
                f'{_imp["emoji"]} {_imp["label"]}</span></td>'
                f'</tr>'
            )

        st.markdown(
            '<div style="background:linear-gradient(180deg, #131a29 0%, #101623 100%);'
            'border-radius:var(--sl-radius-lg);border:1px solid var(--sl-border-default);overflow-x:auto;'
            'box-shadow:0 1px 2px rgba(0,0,0,0.24),0 8px 24px -8px rgba(0,0,0,0.34),inset 0 1px 0 rgba(255,255,255,0.04);">'
            '<table style="width:100%;border-collapse:collapse;">'
            '<thead>'
            '<tr style="border-bottom:1px solid rgba(100,116,139,0.22);background:rgba(255,255,255,0.02);">'
            + "".join(
                f'<th style="padding:12px 18px;text-align:{align};font-family:\'Inter\',sans-serif;'
                f'font-size:0.70rem;font-weight:700;color:#94a3b8;text-transform:uppercase;'
                f'letter-spacing:1.2px;">{col}</th>'
                for col, align in [
                    ('Empresa', 'left'), ('Evento', 'left'), ('Fecha', 'left'),
                    ('Días', 'center'), ('Importancia', 'center')
                ]
            ) +
            '</tr></thead>'
            f'<tbody>{_filas_ev}</tbody>'
            '</table></div>',
            unsafe_allow_html=True
        )

