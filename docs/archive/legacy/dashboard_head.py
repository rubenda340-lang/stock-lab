import streamlit as st
from database import query_all, query_val, obtener_watchlist
import market_data
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf


@st.cache_data(ttl=300)
def obtener_datos_movers_batch(lista_tickers):
    """Descarga masiva de datos para calcular variaciones diarias en una única petición."""
    if not lista_tickers:
        return pd.DataFrame()
    
    tickers_string = " ".join(lista_tickers)
    # Descargamos 5 días de histórico para calcular la variación diaria real con seguridad
    # (margen amplio para valores de baja liquidez como NEA.MC que pueden no tener volumen diario)
    try:
        datos = yf.download(tickers_string, period="5d", group_by="ticker", progress=False)
        return datos
    except Exception as e:
        st.error(f"Error al descargar datos del mercado: {e}")
        return pd.DataFrame()

def show():
    # ── CSS Premium Institucional — Unified StockLab Design System ────
    st.markdown("""
<style>
    /* ═══════════════════════════════════════════════════════════════════
       🎨 STOCKLAB DASHBOARD — Premium Dark Trading Terminal Theme
       Unified with Mi Cartera V2, Ranking & Watchlist Design System
       Inspired by TradingView Pro / Binance / Bloomberg Terminal
       ═══════════════════════════════════════════════════════════════════ */

    /* ── Google Font Import ─────────────────────────────────────────── */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

    /* ── Keyframe Animations ────────────────────────────────────────── */
    @keyframes fade-in-up {
        0%   { opacity: 0; transform: translateY(12px); }
        100% { opacity: 1; transform: translateY(0); }
    }

    @keyframes gradient-shift {
        0%   { background-position: 0% 50%; }
        50%  { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }

    @keyframes glow-pulse {
        0%, 100% { box-shadow: 0 0 8px rgba(0, 229, 255, 0.08), 0 0 20px rgba(0, 229, 255, 0.04); }
        50%      { box-shadow: 0 0 14px rgba(0, 229, 255, 0.14), 0 0 30px rgba(0, 229, 255, 0.07); }
    }

    /* ── Global Foundation ──────────────────────────────────────────── */
    section[data-testid="stMain"] {
        background: #080b12 !important;
        background-image:
            radial-gradient(ellipse 80% 60% at 50% 0%, rgba(0, 229, 255, 0.03) 0%, transparent 70%),
            radial-gradient(ellipse 60% 50% at 80% 100%, rgba(16, 185, 129, 0.02) 0%, transparent 60%) !important;
    }

    .block-container {
        padding-top: 2rem !important;
        padding-bottom: 3rem !important;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
    }

    /* ── Custom Scrollbar ───────────────────────────────────────────── */
    ::-webkit-scrollbar {
        width: 6px !important;
        height: 6px !important;
    }
    ::-webkit-scrollbar-track {
        background: #0f1219 !important;
        border-radius: 3px !important;
    }
    ::-webkit-scrollbar-thumb {
        background: linear-gradient(180deg, #1e3a5f, #00e5ff33) !important;
        border-radius: 3px !important;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: linear-gradient(180deg, #2a5080, #00e5ff55) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       ✍️ GRADIENT HEADINGS — Bloomberg Premium Typography
       ═══════════════════════════════════════════════════════════════════ */
    h1, .stMarkdown h1 {
        background: linear-gradient(135deg, #ffffff 0%, #00e5ff 50%, #a5f3fc 100%) !important;
        -webkit-background-clip: text !important;
        -webkit-text-fill-color: transparent !important;
        background-clip: text !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 900 !important;
        letter-spacing: -0.5px !important;
        text-shadow: none !important;
    }

    h2, .stMarkdown h2 {
        background: linear-gradient(135deg, #e8ecf1 0%, #94a3b8 30%, #00e5ff 70%, #67e8f9 100%) !important;
        -webkit-background-clip: text !important;
        -webkit-text-fill-color: transparent !important;
        background-clip: text !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 800 !important;
        letter-spacing: -0.3px !important;
        text-shadow: none !important;
        padding-bottom: 4px !important;
    }

    h3, .stMarkdown h3 {
        background: linear-gradient(135deg, #e2e8f0 0%, #94a3b8 40%, #67e8f9 100%) !important;
        -webkit-background-clip: text !important;
        -webkit-text-fill-color: transparent !important;
        background-clip: text !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
        letter-spacing: -0.2px !important;
        text-shadow: none !important;
    }

    h5, .stMarkdown h5 {
        color: #e8ecf1 !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📊 TARJETAS KPI — Métricas con Brillo Eléctrico
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stMetric"] {
        background: linear-gradient(145deg, #161a25 0%, #11141d 50%, #0f1219 100%) !important;
        border: 1px solid rgba(0, 229, 255, 0.12) !important;
        border-radius: 14px !important;
        padding: 22px 20px !important;
        transition: all 0.35s cubic-bezier(0.25, 0.46, 0.45, 0.94) !important;
        box-shadow:
            0 0 10px rgba(0, 229, 255, 0.06),
            0 4px 20px rgba(0, 0, 0, 0.4),
            inset 0 1px 0 rgba(255, 255, 255, 0.03) !important;
        animation: fade-in-up 0.6s ease-out both !important;
        position: relative !important;
        overflow: hidden !important;
    }

    /* Subtle top accent line on KPI cards */
    div[data-testid="stMetric"]::before {
        content: '' !important;
        position: absolute !important;
        top: 0 !important;
        left: 10% !important;
        right: 10% !important;
        height: 2px !important;
        background: linear-gradient(90deg, transparent, #00e5ff, transparent) !important;
        border-radius: 0 0 2px 2px !important;
        opacity: 0.5 !important;
        transition: opacity 0.35s ease, left 0.35s ease, right 0.35s ease !important;
    }

    div[data-testid="stMetric"]:hover {
        border-color: rgba(0, 229, 255, 0.4) !important;
        box-shadow:
            0 0 15px rgba(0, 229, 255, 0.12),
            0 0 40px rgba(0, 229, 255, 0.06),
            0 8px 32px rgba(0, 0, 0, 0.5),
            inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
        transform: translateY(-3px) !important;
    }

    div[data-testid="stMetric"]:hover::before {
        opacity: 1 !important;
        left: 5% !important;
        right: 5% !important;
    }

    div[data-testid="stMetric"] label {
        color: #6b7a8d !important;
        font-size: 0.7rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 1.2px !important;
        margin-bottom: 10px !important;
        font-family: 'Inter', sans-serif !important;
    }

    div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
        color: #e8ecf1 !important;
        font-weight: 800 !important;
        font-size: 1.45rem !important;
        font-family: 'JetBrains Mono', 'Inter', monospace !important;
        margin-top: 6px !important;
        letter-spacing: -0.5px !important;
        text-shadow: 0 0 20px rgba(0, 229, 255, 0.08) !important;
    }

    div[data-testid="stMetric"] div[data-testid="stMetricDelta"] {
        font-size: 0.85rem !important;
        font-weight: 700 !important;
        margin-top: 6px !important;
        font-family: 'JetBrains Mono', monospace !important;
    }

    /* Positive delta glow */
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"][style*="color: rgb(9, 171, 59)"],
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"] svg[style*="fill: rgb(9, 171, 59)"] {
        filter: drop-shadow(0 0 6px rgba(16, 185, 129, 0.4)) !important;
    }

    /* Negative delta glow */
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"][style*="color: rgb(255, 43, 43)"],
    div[data-testid="stMetric"] div[data-testid="stMetricDelta"] svg[style*="fill: rgb(255, 43, 43)"] {
        filter: drop-shadow(0 0 6px rgba(239, 68, 68, 0.4)) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📈 CHART CONTAINERS — Seamless Graph Integration
       ═══════════════════════════════════════════════════════════════════ */

    /* Plotly chart wrapper */
    div[data-testid="stPlotlyChart"] {
        background: linear-gradient(160deg, rgba(11, 14, 20, 0.9), rgba(8, 11, 18, 0.95)) !important;
        border: 1px solid rgba(0, 229, 255, 0.08) !important;
        border-radius: 14px !important;
        padding: 8px !important;
        transition: all 0.3s ease !important;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35) !important;
        overflow: hidden !important;
    }

    div[data-testid="stPlotlyChart"]:hover {
        border-color: rgba(0, 229, 255, 0.18) !important;
        box-shadow: 0 0 15px rgba(0, 229, 255, 0.04), 0 6px 24px rgba(0, 0, 0, 0.4) !important;
    }

    /* Altair / VegaLite chart wrapper */
    div[data-testid="stVegaLiteChart"] {
        background: linear-gradient(160deg, rgba(20, 24, 32, 0.5), rgba(15, 18, 25, 0.7)) !important;
        border: 1px solid rgba(0, 229, 255, 0.08) !important;
        border-radius: 14px !important;
        padding: 16px !important;
        transition: all 0.3s ease !important;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.25) !important;
    }

    div[data-testid="stVegaLiteChart"]:hover {
        border-color: rgba(0, 229, 255, 0.18) !important;
        box-shadow: 0 0 15px rgba(0, 229, 255, 0.04), 0 4px 20px rgba(0, 0, 0, 0.3) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📦 CONTAINERS — Glassmorphic Panels (Destacadas sidebar)
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
        background: linear-gradient(160deg, rgba(22, 26, 37, 0.7), rgba(15, 18, 25, 0.85)) !important;
        border: 1px solid rgba(0, 229, 255, 0.08) !important;
        border-radius: 14px !important;
        transition: all 0.35s cubic-bezier(0.25, 0.46, 0.45, 0.94) !important;
        overflow: hidden !important;
    }

    div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: rgba(0, 229, 255, 0.18) !important;
        box-shadow: 0 0 15px rgba(0, 229, 255, 0.05), 0 4px 20px rgba(0, 0, 0, 0.3) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🔀 SEGMENTED CONTROL — Period Toggle (1D, 5D, 1M, etc.)
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stSegmentedControl"] button {
        background: rgba(22, 26, 37, 0.6) !important;
        border: 1px solid rgba(100, 116, 139, 0.15) !important;
        color: #6b7a8d !important;
        font-weight: 600 !important;
        font-family: 'Inter', sans-serif !important;
        transition: all 0.3s ease !important;
        border-radius: 8px !important;
        font-size: 0.82rem !important;
    }

    div[data-testid="stSegmentedControl"] button:hover {
        background: rgba(0, 229, 255, 0.06) !important;
        color: #c4cad4 !important;
        border-color: rgba(0, 229, 255, 0.2) !important;
    }

    div[data-testid="stSegmentedControl"] button[aria-checked="true"] {
        background: linear-gradient(135deg, rgba(0, 229, 255, 0.12), rgba(16, 185, 129, 0.08)) !important;
        border-color: rgba(0, 229, 255, 0.3) !important;
        color: #00e5ff !important;
        box-shadow: 0 0 10px rgba(0, 229, 255, 0.08) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📝 FORM INPUTS / SELECTS — Dark Terminal Inputs
       ═══════════════════════════════════════════════════════════════════ */
    .stSelectbox [data-baseweb="select"] {
        background-color: #0f1219 !important;
        border: 1px solid rgba(100, 116, 139, 0.2) !important;
        border-radius: 10px !important;
        color: #e8ecf1 !important;
        font-family: 'Inter', sans-serif !important;
        transition: border-color 0.3s ease, box-shadow 0.3s ease !important;
    }

    [data-baseweb="popover"] {
        background-color: #141820 !important;
        border: 1px solid rgba(0, 229, 255, 0.12) !important;
        border-radius: 10px !important;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5) !important;
    }

    [data-baseweb="menu"] {
        background-color: #141820 !important;
    }

    [data-baseweb="menu"] li {
        color: #c4cad4 !important;
        transition: all 0.2s ease !important;
        font-family: 'Inter', sans-serif !important;
    }

    [data-baseweb="menu"] li:hover {
        background-color: rgba(0, 229, 255, 0.08) !important;
        color: #00e5ff !important;
    }

    .stSelectbox label {
        color: #6b7a8d !important;
        font-weight: 600 !important;
        font-size: 0.78rem !important;
        text-transform: uppercase !important;
        letter-spacing: 0.8px !important;
        font-family: 'Inter', sans-serif !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🔔 ALERTS — Neon-Tinted Notifications
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stAlert"] {
        border-radius: 12px !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 500 !important;
        backdrop-filter: blur(4px) !important;
    }

    div[role="alert"]:has(> div > .stAlertContentInfo),
    .stAlert[data-baseweb="notification"][kind="info"] {
        background: linear-gradient(135deg, rgba(0, 229, 255, 0.06), rgba(0, 229, 255, 0.02)) !important;
        border-left: 4px solid #00e5ff !important;
        border-radius: 0 12px 12px 0 !important;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.2), inset 0 0 20px rgba(0, 229, 255, 0.02) !important;
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
        background: linear-gradient(90deg,
            transparent 0%,
            rgba(0, 229, 255, 0.15) 20%,
            rgba(0, 229, 255, 0.08) 50%,
            rgba(16, 185, 129, 0.15) 80%,
            transparent 100%) !important;
        margin: 2rem 0 !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🌐 GLOBAL TEXT & MISC — Typography Polish
       ═══════════════════════════════════════════════════════════════════ */
    .stMarkdown p {
        font-family: 'Inter', sans-serif !important;
    }

    .stMarkdown strong {
        color: #e8ecf1 !important;
    }

    /* Caption text polish */
    .stCaption, [data-testid="stCaptionContainer"] {
        color: #6b7a8d !important;
        font-family: 'Inter', sans-serif !important;
    }

    /* Tooltip styling */
    div[data-testid="stTooltipContent"] {
        background-color: #1a1f2e !important;
        border: 1px solid rgba(0, 229, 255, 0.15) !important;
        border-radius: 8px !important;
        color: #c4cad4 !important;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5) !important;
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
</style>
""", unsafe_allow_html=True)

    st.title("📊 Dashboard")
    st.caption("Panel general de control de StockLab")

    try:
        # ── Mercados disponibles ─────────────────────────────────────────
        mercados_rows = query_all(
            "SELECT DISTINCT mercado FROM empresas WHERE mercado IS NOT NULL AND activa = 1 ORDER BY mercado"
        )
        mercados = [row["mercado"] for row in mercados_rows]
    except Exception as e:
        st.error(f"Error al conectar con la base de datos: {e}")
        return

    if mercados:
        mercado_sel = st.selectbox("Mercado", mercados, index=0)
    else:
        mercado_sel = None
        st.info("No hay mercados registrados todavía.")

    st.markdown("---")

    # ── KPIs ─────────────────────────────────────────────────────────
    if mercado_sel:
        try:
            total_empresas = query_val(
                "SELECT COUNT(*) FROM empresas WHERE mercado = ? AND activa = 1",
                (mercado_sel,),
            )

            # Usar la nueva función de watchlist para obtener el conteo real
            df_watchlist = obtener_watchlist()
            total_watchlist = len(df_watchlist)

            total_cartera = query_val("""
                SELECT COUNT(*) FROM cartera c
                JOIN empresas e ON c.empresa_id = e.id
                WHERE e.mercado = ?
            """, (mercado_sel,))
        except Exception as e:
            st.error(f"Error al obtener estadísticas: {e}")
            total_empresas = 0
            total_watchlist = 0
            total_cartera = 0
    else:
        total_empresas = 0
        total_watchlist = 0
        total_cartera = 0

    # ── Tarjetas métricas ────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(label="🏛️ Mercado", value=mercado_sel or "—")

    with col2:
        st.metric(label="🏢 Empresas", value=total_empresas)

    with col3:
        st.metric(label="👁️ Watchlist", value=total_watchlist)

    with col4:
        st.metric(label="💼 Posiciones", value=total_cartera)

    st.markdown("---")

    # ── Estructura Principal: Gráfico IBEX + Pulso del Mercado ───────

    # 1. CABECERA GLOBAL — Título + Precio + Selector de periodo
    #    (Fuera de las columnas para nivelar gráfico y Destacadas)
    opciones_periodo_ibex = ["1D", "5D", "1M", "6M", "YTD", "5Y"]
    col_titulo_precio, col_botones = st.columns([2, 1], vertical_alignment="center")

    with col_botones:
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

    # 2. COLUMNAS NIVELADAS — Gráfico + Destacadas arrancan a la misma altura
    col_izquierda, col_derecha = st.columns([3.8, 1])

    # ═══════════════════════════════════════════════════════════════════
    # 3. COLUMNA IZQUIERDA — Gráfico IBEX 35
    # ═══════════════════════════════════════════════════════════════════
    with col_izquierda:

        try:
            with st.spinner("Cargando histórico del IBEX 35..."):
                df_ibex = market_data.get_price_history("^IBEX", period=api_period_ibex, interval=api_interval_ibex)
            
            df_ibex = df_ibex.dropna(subset=['Close'])

            if not df_ibex.empty:
                # Estructuración robusta y aislada del DataFrame intradía (1D, 5D)
                if periodo_ibex in ["1D", "5D"]:
                    try:
                        datos_mercado_ibex_inj = market_data.get_stock_data("^IBEX")
                        precio_subasta_real_ibex = datos_mercado_ibex_inj.get('precio_actual')
                        
                        if precio_subasta_real_ibex is not None:
                            precio_apertura_real_ibex = float(df_ibex['Open'].iloc[0])
                            
                            df_shifted_ibex = df_ibex.copy()
                            df_shifted_ibex.index = df_shifted_ibex.index + pd.Timedelta(minutes=5)
                            
                            idx_0900_ibex = df_shifted_ibex.index[0] - pd.Timedelta(minutes=5)
                            fila_0900_ibex = pd.DataFrame({
                                'Open': [precio_apertura_real_ibex],
                                'High': [precio_apertura_real_ibex],
                                'Low': [precio_apertura_real_ibex],
                                'Close': [precio_apertura_real_ibex],
                                'Volume': [0]
                            }, index=[idx_0900_ibex])
                            
                            idx_1735_ibex = df_shifted_ibex.index[-1].normalize() + pd.Timedelta(hours=17, minutes=35)
                            fila_1735_ibex = pd.DataFrame({
                                'Open': [precio_subasta_real_ibex],
                                'High': [precio_subasta_real_ibex],
                                'Low': [precio_subasta_real_ibex],
                                'Close': [precio_subasta_real_ibex],
                                'Volume': [0]
                            }, index=[idx_1735_ibex])
                            
                            df_grafico_ibex = pd.concat([fila_0900_ibex, df_shifted_ibex, fila_1735_ibex]).sort_index()
                            df_grafico_ibex['Fecha_Visible'] = df_grafico_ibex.index.strftime('%m-%d %H:%M')
                            
                            df_ibex = df_grafico_ibex
                    except Exception:
                        pass

                precio_actual_ibex = float(df_ibex['Close'].iloc[-1])
                if periodo_ibex == "1D":
                    try:
                        datos_mercado_ibex = market_data.get_stock_data("^IBEX")
                        variacion_ibex = datos_mercado_ibex.get('variacion_diaria')
                        if variacion_ibex is None:
                            raise ValueError("Variación no disponible")
                    except Exception:
                        try:
                            datos_mercado_ibex = market_data.get_stock_data("^IBEX")
                            ref_val_ibex = datos_mercado_ibex.get('previous_close')
                            if ref_val_ibex and ref_val_ibex > 0:
                                variacion_ibex = ((precio_actual_ibex - ref_val_ibex) / ref_val_ibex) * 100.0
                            else:
                                raise ValueError("Previous close inválido")
                        except Exception:
                            ref_val_ibex = float(df_ibex['Open'].iloc[0])
                            variacion_ibex = ((precio_actual_ibex - ref_val_ibex) / ref_val_ibex) * 100.0 if ref_val_ibex != 0 else 0.0
                else:
                    ref_val_ibex = float(df_ibex['Close'].iloc[0])
                    variacion_ibex = ((precio_actual_ibex - ref_val_ibex) / ref_val_ibex) * 100.0 if ref_val_ibex != 0 else 0.0
                
                # ── Cabecera elegante: Título · Precio · Delta con breathing room ──
                color_delta = "#34d399" if variacion_ibex >= 0 else "#f87171"
                signo_delta = "+" if variacion_ibex >= 0 else ""
                separador = "<span style='color:rgba(100,116,139,0.3); font-weight:300; margin:0 4px;'>│</span>"
                with col_titulo_precio:
                    st.markdown(
                        f"<div style='display:flex; align-items:center; gap:6px; padding:6px 0; flex-wrap:nowrap;'>"
                        f"<span style='font-family:Inter,sans-serif; font-weight:800; font-size:1.35rem; "
                        f"background:linear-gradient(135deg,#e2e8f0,#67e8f9); -webkit-background-clip:text; "
                        f"-webkit-text-fill-color:transparent; white-space:nowrap;'>📈 IBEX 35</span>"
                        f"&nbsp;&nbsp;{separador}&nbsp;&nbsp;"
                        f"<span style='font-family:JetBrains Mono,monospace; font-weight:700; font-size:1.3rem; "
                        f"color:#e8ecf1; text-shadow:0 0 15px rgba(0,229,255,0.06); white-space:nowrap;'>"
                        f"{precio_actual_ibex:,.2f} EUR</span>"
                        f"&nbsp;&nbsp;{separador}&nbsp;&nbsp;"
                        f"<span style='font-family:JetBrains Mono,monospace; font-weight:700; font-size:1rem; "
                        f"color:{color_delta}; text-shadow:0 0 8px {color_delta}40; white-space:nowrap;'>"
                        f"{signo_delta}{variacion_ibex:.2f}%</span>"
                        f"</div>",
                        unsafe_allow_html=True
                    )
                    st.markdown("")

                # Formateo de fechas para el eje X
                if periodo_ibex in ["1D", "5D"]:
                    df_ibex['Fecha_Visible'] = df_ibex.index.strftime('%Y-%m-%d %H:%M')
                else:
                    df_ibex['Fecha_Visible'] = df_ibex.index.strftime('%Y-%m-%d')

                color_linea = "#10B981" if variacion_ibex >= 0 else "#EF4444"
                color_relleno = "rgba(16, 185, 129, 0.08)" if variacion_ibex >= 0 else "rgba(239, 68, 68, 0.08)"

                fig_ibex = go.Figure()
                fig_ibex.add_trace(go.Scattergl(
                    x=df_ibex['Fecha_Visible'],
                    y=df_ibex['Close'],
                    mode='lines',
                    name='IBEX 35',
                    line=dict(color=color_linea, width=2.5),
                    fill='tozeroy',
                    fillcolor=color_relleno,
                    hovertemplate="<b>Fecha:</b> %{x}<br><b>Cierre:</b> %{y:.2f} EUR<extra></extra>"
                ))

                ymin = float(df_ibex['Close'].min())
                ymax = float(df_ibex['Close'].max())
                fig_ibex.update_yaxes(range=[ymin * 0.997, ymax * 1.003], autorange=False)

                fig_ibex.update_xaxes(
                    type='category', 
                    tickangle=-35, 
                    nticks=10, 
                    autorange=True,
                    tickfont=dict(color="#636e72")
                )

                fig_ibex.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    margin=dict(t=10, b=50, l=10, r=10),
                    hovermode="x unified",
                    showlegend=False,
                    xaxis=dict(
                        showgrid=True,
                        gridcolor="rgba(0, 229, 255, 0.04)",
                        zeroline=False,
                        tickfont=dict(color="#636e72")
                    ),
                    yaxis=dict(
                        showgrid=True,
                        gridcolor="rgba(0, 229, 255, 0.04)",
                        zeroline=False
                    ),
                    height=480,
                    font=dict(family="Inter, sans-serif", color="#c4cad4")
                )

                # Gráfico envuelto en contenedor con borde, toolbar oculta
                with st.container(border=True):
                    st.plotly_chart(fig_ibex, width="stretch", config={'displayModeBar': False})
            else:
                st.info("No hay datos históricos disponibles para el IBEX 35 en el periodo seleccionado.")

        except Exception as e:
            st.warning(f"No se pudo cargar los datos del IBEX 35: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # 4. COLUMNA DERECHA — Destacadas Hoy (Optimizado con Batch Download)
    # ═══════════════════════════════════════════════════════════════════
    _separador_fila = "<hr style='border:none; height:1px; background:rgba(255,255,255,0.04); margin:6px 0;'>"
    with col_derecha.container(border=True, height=560):
        st.markdown("##### 📊 Destacadas Hoy")
        st.caption("Variación diaria de todas las empresas del Mercado Continuo")
        st.markdown("")

        try:
            # Consulta dinámica: obtiene TODAS las empresas registradas en la BD
            empresas_rows = query_all(
                "SELECT ticker, nombre FROM empresas WHERE activa = 1 ORDER BY nombre"
            )

            datos_variacion = []
            if empresas_rows:
                total_empresas = len(empresas_rows)
                tickers_bd = [emp["ticker"] for emp in empresas_rows]
                
                with st.spinner(f"Analizando {total_empresas} empresas del Mercado Continuo..."):
                    # Descarga masiva en una única petición
                    datos_mercado = obtener_datos_movers_batch(tickers_bd)
                    
                    # Procesamiento eficiente desde el DataFrame masivo
                    if not datos_mercado.empty and isinstance(datos_mercado.columns, pd.MultiIndex):
                        # Estructura multi-nivel (group_by="ticker")
                        for emp in empresas_rows:
                            ticker = emp["ticker"]
                            if ticker in datos_mercado.columns.get_level_values(0):
                                df_ticker = datos_mercado[ticker]
                                
                                # 🔒 Verificación de datos reales antes de procesar
                                # Salta tickers sin operaciones recientes (NEA.MC, ATRY.MC, NXT.MC, etc.)
                                if df_ticker.empty or 'Close' not in df_ticker.columns or df_ticker['Close'].isnull().all():
                                    print(f"⚠️ {ticker} sin operaciones recientes. Saltando temporalmente.")
                                    continue
                                
                                if 'Close' in df_ticker.columns and not df_ticker['Close'].dropna().empty:
                                    precios = df_ticker['Close'].dropna()
                                    if len(precios) >= 2:
                                        precio_actual = float(precios.iloc[-1])
                                        precio_anterior = float(precios.iloc[-2])
                                        variacion = ((precio_actual - precio_anterior) / precio_anterior) * 100
                                        
                                        datos_variacion.append({
                                            "nombre": emp["nombre"],
                                            "ticker": ticker,
                                            "precio": precio_actual,
                                            "variacion": variacion,
                                            "moneda": "EUR"
                                        })
                    elif not datos_mercado.empty and 'Close' in datos_mercado.columns:
                        # Estructura simple (un solo ticker o formato diferente)
                        precios = datos_mercado['Close'].dropna()
                        if len(precios) >= 2:
                            precio_actual = float(precios.iloc[-1])
                            precio_anterior = float(precios.iloc[-2])
                            variacion = ((precio_actual - precio_anterior) / precio_anterior) * 100
                            
                            # Solo procesamos el primer ticker si hay estructura simple
                            if empresas_rows:
                                datos_variacion.append({
                                    "nombre": empresas_rows[0]["nombre"],
                                    "ticker": empresas_rows[0]["ticker"],
                                    "precio": precio_actual,
                                    "variacion": variacion,
                                    "moneda": "EUR"
                                })

            if datos_variacion:
                df_pulso = pd.DataFrame(datos_variacion).sort_values("variacion", ascending=False)

                st.markdown("**🚀 Top Ganadoras**")
                top_ganadoras = df_pulso.head(3)
                for i, (_, row) in enumerate(top_ganadoras.iterrows()):
                    color = "#34d399" if row['variacion'] >= 0 else "#f87171"
                    signo = "+" if row['variacion'] >= 0 else ""
                    st.caption(f"**{row['nombre']}** · {row['precio']:.2f} {row['moneda']} · <span style='color:{color};font-weight:bold;font-family:JetBrains Mono,monospace;'>{signo}{row['variacion']:.2f}%</span>", unsafe_allow_html=True)
                    if i < len(top_ganadoras) - 1:
                        st.markdown(_separador_fila, unsafe_allow_html=True)

                st.markdown("")
                st.markdown("**📉 Top Perdedoras**")
                top_perdedoras = df_pulso.tail(3).iloc[::-1]
                for i, (_, row) in enumerate(top_perdedoras.iterrows()):
                    color = "#34d399" if row['variacion'] >= 0 else "#f87171"
                    signo = "+" if row['variacion'] >= 0 else ""
                    st.caption(f"**{row['nombre']}** · {row['precio']:.2f} {row['moneda']} · <span style='color:{color};font-weight:bold;font-family:JetBrains Mono,monospace;'>{signo}{row['variacion']:.2f}%</span>", unsafe_allow_html=True)
                    if i < len(top_perdedoras) - 1:
                        st.markdown(_separador_fila, unsafe_allow_html=True)
                
                st.markdown("")
                st.caption(f"📈 Análisis completado: {len(datos_variacion)} de {total_empresas} empresas con datos disponibles")
            else:
                st.caption("No hay datos de variación disponibles.")

        except Exception as e:
            st.error(f"Error al obtener el pulso del mercado: {e}")

