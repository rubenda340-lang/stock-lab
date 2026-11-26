"""Punto de entrada principal de StockLab"""
import streamlit as st
from database.connection import init_db
from views import (
    dashboard,
    empresas,
    watchlist,
    ranking,
    mi_cartera_v2 as mi_cartera,
    ia
)

# 1. Configurar la página de Streamlit (debe ser el primer comando st)
st.set_page_config(
    page_title="StockLab",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 2. Inicializar la base de datos automáticamente con caché (solo una vez por sesión)
@st.cache_resource
def inicializar_base_datos():
    """Inicializa la base de datos una única vez y mantiene la conexión en caché."""
    init_db()
    return True

if "db_initialized" not in st.session_state:
    inicializar_base_datos()
    st.session_state.db_initialized = True

# 2b. Sincronización y Precarga Global del Ecosistema
#     Se gestiona centralizadamente en la sección 3b (tras inyectar el diseño del Loading Terminal).


# 3. Estilos CSS Globales - Sistema de Diseño StockLab Obsidian Neon (Institutional Fintech)
st.markdown("""
<style>
    /* 0. Carga centralizada de tipografías (una sola vez, evita FOUT en cambio de pestañas) */
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

    /* 0b. Design Tokens Centralizados — Obsidian Neon Institutional */
    :root {
        --sl-font-display: 'Plus Jakarta Sans', 'Inter', system-ui, sans-serif;
        --sl-font-body: 'Inter', system-ui, sans-serif;
        --sl-font-mono: 'JetBrains Mono', 'Consolas', monospace;
        --sl-nav-height: 76px;
        --sl-gutter: clamp(1rem, 3vw, 3.25rem);

        --sl-bg-canvas: #080c15;
        --sl-bg-surface: #0f1726;
        --sl-bg-elevated: #162236;
        --sl-bg-sunken: #0b111e;
        --sl-bg-topbar: rgba(8, 12, 22, 0.94);
        --sl-surface-glass: linear-gradient(155deg, rgba(22, 34, 54, 0.90) 0%, rgba(13, 21, 35, 0.97) 100%);
        --sl-surface-glass-hover: linear-gradient(155deg, rgba(26, 40, 64, 0.95) 0%, rgba(16, 25, 42, 0.99) 100%);

        --sl-border-subtle: rgba(148, 163, 184, 0.09);
        --sl-border-default: rgba(148, 163, 184, 0.15);
        --sl-border-hover: rgba(56, 189, 248, 0.40);
        --sl-border-focus: rgba(56, 189, 248, 0.62);

        --sl-text-primary: #eef2f8;
        --sl-text-heading: #f8fafc;
        --sl-text-secondary: #a3b4cc;
        --sl-text-muted: #7c90ad;

        --sl-accent: #38bdf8;
        --sl-accent-bright: #7dd3fc;
        --sl-accent-deep: #6366f1;
        --sl-accent-soft: rgba(56, 189, 248, 0.11);
        --sl-accent-border: rgba(56, 189, 248, 0.34);
        --sl-accent-glow: rgba(56, 189, 248, 0.26);

        --sl-pos: #34d399;
        --sl-pos-soft: rgba(16, 185, 129, 0.11);
        --sl-neg: #f87171;
        --sl-neg-soft: rgba(239, 68, 68, 0.11);
        --sl-warn: #fbbf24;
        --sl-warn-soft: rgba(245, 158, 11, 0.11);
        --sl-violet: #a78bfa;
        --sl-violet-soft: rgba(167, 139, 250, 0.12);

        --sl-radius-sm: 8px;
        --sl-radius-md: 10px;
        --sl-radius-lg: 14px;
        --sl-radius-xl: 18px;

        --sl-shadow-card: 0 4px 16px -4px rgba(0, 0, 0, 0.44), inset 0 1px 0 rgba(255, 255, 255, 0.06);
        --sl-shadow-hover: 0 8px 24px -6px rgba(0, 0, 0, 0.56), 0 0 22px -8px rgba(56, 189, 248, 0.24), inset 0 1px 0 rgba(255, 255, 255, 0.09);
        --sl-ease: cubic-bezier(0.22, 1, 0.36, 1);
    }

    html, body {
        background-color: var(--sl-bg-canvas) !important;
        color: var(--sl-text-primary) !important;
        -webkit-font-smoothing: antialiased !important;
        -moz-osx-font-smoothing: grayscale !important;
        text-rendering: optimizeLegibility !important;
    }

    /* 1. Ocultar totalmente Sidebar y controles de colapso */
    section[data-testid="stSidebar"],
    button[data-testid="stSidebarCollapseButton"],
    div[data-testid="collapsedControl"] {
        display: none !important;
    }

    /* 2. Ocultar la barra superior nativa de Streamlit (Deploy / menú) */
    header[data-testid="stHeader"] {
        display: none !important;
    }

    /* La columna vacía equilibra el espacio de la marca en escritorio. */
    section[data-testid="stMain"] [data-testid="stMainBlockContainer"] {
        padding: clamp(.75rem, 1.6vw, 1.6rem) var(--sl-gutter) 3.5rem !important;
        font-family: var(--sl-font-body);
    }
    .st-key-topbar_nav {
        position: fixed !important;
        inset: 0 0 auto !important;
        width: 100% !important;
        height: var(--sl-nav-height) !important;
        box-sizing: border-box;
        padding: 0 var(--sl-gutter) !important;
        display: flex !important;
        justify-content: center !important;
        background: var(--sl-bg-topbar) !important;
        backdrop-filter: blur(6px);
        -webkit-backdrop-filter: blur(6px);
        border-bottom: 1px solid rgba(148, 163, 184, 0.14);
        box-shadow: 0 4px 20px rgba(2, 6, 23, 0.42), inset 0 -1px 0 rgba(56, 189, 248, 0.08);
        transform: translateZ(0);
        will-change: transform;
        contain: layout style;
        z-index: 99999 !important;
    }
    .st-key-topbar_nav::after {
        content: "";
        position: absolute;
        left: 18%;
        right: 18%;
        bottom: -1px;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.42), rgba(99, 102, 241, 0.32), transparent);
        pointer-events: none;
    }
    .st-key-topbar_nav [data-testid="stHorizontalBlock"] {
        display: grid !important;
        grid-template-columns: minmax(160px, 2.05fr) minmax(94px, 1.08fr) minmax(90px, 1.02fr) minmax(90px, 1.02fr) minmax(84px, 0.96fr) minmax(98px, 1.12fr) minmax(68px, 0.74fr) minmax(160px, 2.05fr);
        gap: 6px !important;
        align-items: center !important;
        width: 100%;
    }
    .st-key-topbar_nav [data-testid="stHorizontalBlock"] > div {
        width: 100% !important;
        min-width: 0 !important;
    }
    /* Los bloques de Streamlit que envuelven la barra ya aportan tres huecos. */
    .topbar-spacer {
        height: calc(var(--sl-nav-height) - 3rem) !important;
        min-height: calc(var(--sl-nav-height) - 3rem) !important;
        pointer-events: none;
    }
    .stocklab-logo-container {
        position: relative;
        padding-left: 46px;
        width: max-content;
        text-align: left;
        line-height: 1;
    }
    .stocklab-logo-container::before {
        content: "";
        position: absolute;
        left: 0;
        top: 50%;
        transform: translateY(-50%);
        width: 36px;
        height: 36px;
        border-radius: 10px;
        border: 1px solid rgba(56, 189, 248, 0.42);
        background:
            linear-gradient(#7dd3fc, #38bdf8) 8px 20px / 4px 7px no-repeat,
            linear-gradient(#a5f3fc, #38bdf8) 15px 14px / 4px 13px no-repeat,
            linear-gradient(#d8fff8, #67e8f9) 22px 8px / 4px 19px no-repeat,
            linear-gradient(145deg, #1b3b4e, #0e1e31);
        box-shadow: 0 0 18px -4px rgba(56, 189, 248, 0.42), inset 0 1px 0 rgba(255,255,255,0.16);
        transition: box-shadow 240ms var(--sl-ease), border-color 240ms var(--sl-ease);
    }
    .stocklab-logo-title {
        background: linear-gradient(180deg, #ffffff 20%, #d8e6f8 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        font: 800 1.38rem/1.15 var(--sl-font-display);
        letter-spacing: -.048em;
    }
    .stocklab-logo-subtitle {
        margin-top: 4px;
        color: var(--sl-text-muted);
        font: 600 .54rem/1.1 var(--sl-font-body);
        letter-spacing: .18em;
    }
    @media (max-width: 1080px) {
        .st-key-topbar_nav [data-testid="stHorizontalBlock"] {
            grid-template-columns: minmax(150px, 1.55fr) minmax(82px, 1.06fr) minmax(78px, 1.02fr) minmax(78px, 1.02fr) minmax(72px, 0.95fr) minmax(86px, 1.12fr) minmax(56px, 0.74fr) minmax(24px, 0.45fr);
        }
    }
    @media (max-width: 740px) {
        :root { --sl-nav-height: 90px; }
        .st-key-topbar_nav [data-testid="stHorizontalBlock"] {
            grid-template-columns: minmax(68px, 1fr) repeat(3, minmax(0, 1fr));
            gap: 2px !important;
        }
        .st-key-topbar_nav [data-testid="stHorizontalBlock"] > div:first-child { grid-row: span 2; }
        .st-key-topbar_nav [data-testid="stHorizontalBlock"] > div:last-child { display: none; }
        .stocklab-logo-container { padding-left: 0; }
        .stocklab-logo-container::before { display: none; }
        .stocklab-logo-title { font-size: 1.06rem; }
        .stocklab-logo-subtitle { font-size: .44rem; letter-spacing: .05em; }
    }

    /* La barra comparte los mismos estados en las seis vistas. */
    section[data-testid="stMain"] .st-key-topbar_nav div[data-testid="stButton"] button {
        position: relative !important;
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        width: 100% !important;
        height: 38px !important;
        min-height: 38px !important;
        padding: 0 10px !important;
        background: transparent !important;
        color: var(--sl-text-secondary) !important;
        border: 1px solid transparent !important;
        border-radius: var(--sl-radius-md) !important;
        box-shadow: none !important;
        transform: none !important;
        animation: none !important;
        overflow: visible !important;
        transition: background 200ms var(--sl-ease), border-color 200ms var(--sl-ease), color 200ms var(--sl-ease), box-shadow 200ms var(--sl-ease) !important;
    }
    section[data-testid="stMain"] .st-key-topbar_nav div[data-testid="stButton"] button :is(p, span) {
        font: 600 clamp(.76rem, 1.15vw, .87rem)/1.2 var(--sl-font-body) !important;
        color: inherit !important;
        letter-spacing: -.012em !important;
        white-space: nowrap;
        text-transform: none !important;
    }
    section[data-testid="stMain"] .st-key-topbar_nav div[data-testid="stButton"] button::before,
    section[data-testid="stMain"] .st-key-topbar_nav div[data-testid="stButton"] button::after { content: none !important; }
    section[data-testid="stMain"] .st-key-topbar_nav div[data-testid="stButton"] button:hover {
        color: var(--sl-text-heading) !important;
        background: rgba(56, 189, 248, 0.07) !important;
        border-color: rgba(148, 163, 184, 0.22) !important;
    }
    section[data-testid="stMain"] .st-key-topbar_nav div[data-testid="stButton"] button:is([kind="primary"], [data-testid="baseButton-primary"]) {
        color: #e0f6ff !important;
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.18), rgba(99, 102, 241, 0.12)) !important;
        border-color: rgba(56, 189, 248, 0.46) !important;
        box-shadow: 0 0 20px -6px rgba(56, 189, 248, 0.45), inset 0 1px 0 rgba(224, 246, 255, 0.18) !important;
    }
    section[data-testid="stMain"] .st-key-topbar_nav div[data-testid="stButton"] button:is([kind="primary"], [data-testid="baseButton-primary"])::after {
        content: "" !important;
        position: absolute !important;
        left: 22% !important;
        right: 22% !important;
        bottom: -1px !important;
        height: 2px !important;
        border-radius: 999px !important;
        background: linear-gradient(90deg, transparent, #38bdf8, #818cf8, transparent) !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.75) !important;
    }
    section[data-testid="stMain"] .st-key-topbar_nav div[data-testid="stButton"] button:focus-visible {
        outline: 2px solid var(--sl-accent-bright) !important;
        outline-offset: 2px !important;
    }

    section[data-testid="stMain"] {
        position: relative !important;
        background-color: var(--sl-bg-canvas) !important;
        background-image: none !important;
        scroll-behavior: auto !important;
    }
    /* Iluminación ambiental rasterizada en una capa fija GPU (0 ms de repintado al hacer scroll) */
    section[data-testid="stMain"]::before {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        z-index: 0;
        background-image:
            radial-gradient(ellipse 68% 420px at 14% 0%, rgba(56, 189, 248, 0.095), transparent 76%),
            radial-gradient(ellipse 52% 360px at 88% 4%, rgba(99, 102, 241, 0.075), transparent 74%);
        transform: translateZ(0);
    }
    section[data-testid="stMain"] > * {
        position: relative;
        z-index: 1;
    }
    .sl-page-header {
        position: relative;
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: .45rem 1.35rem;
        padding: .3rem 0 1.15rem;
        margin-bottom: .65rem;
        border-bottom: 1px solid var(--sl-border-default);
    }
    .sl-page-header::after {
        content: "";
        position: absolute;
        bottom: -1px;
        left: 0;
        width: 4rem;
        height: 2px;
        border-radius: 999px;
        background: linear-gradient(90deg, var(--sl-accent-bright), var(--sl-accent), var(--sl-accent-deep));
        box-shadow: 0 0 14px rgba(56, 189, 248, 0.65);
    }
    [data-testid="stMain"] .sl-page-header .sl-page-title {
        margin: 0 !important;
        padding: 0 !important;
        background: linear-gradient(180deg, #ffffff 22%, #cbd5e1 100%) !important;
        -webkit-background-clip: text !important;
        -webkit-text-fill-color: transparent !important;
        background-clip: text !important;
        color: var(--sl-text-heading) !important;
        font: 800 clamp(1.7rem, 2.65vw, 2.35rem)/1.15 var(--sl-font-display) !important;
        letter-spacing: -.042em !important;
    }
    [data-testid="stMain"] .sl-page-header .sl-page-subtitle {
        margin: 0 !important;
        padding: 0 !important;
        color: var(--sl-text-secondary) !important;
        font: 450 .92rem/1.55 var(--sl-font-body) !important;
        letter-spacing: -.005em !important;
    }
    [data-testid="stMain"] .sl-section-title {
        display: flex !important;
        flex-wrap: wrap !important;
        align-items: center !important;
        gap: .45rem .7rem !important;
        margin: 1.25rem 0 .9rem !important;
        color: var(--sl-text-heading) !important;
        font: 700 1.08rem/1.35 var(--sl-font-display) !important;
        padding: 0 !important;
        letter-spacing: -.024em !important;
    }
    .sl-section-title::before {
        content: "" !important;
        display: inline-block !important;
        width: 3px !important;
        height: 1rem !important;
        border-radius: 999px !important;
        background: linear-gradient(180deg, #7dd3fc 0%, #38bdf8 55%, #6366f1 100%) !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.58) !important;
        flex-shrink: 0 !important;
    }
    .sl-kpi-grid {
        display: grid;
        grid-template-columns: repeat(var(--sl-columns), minmax(0, 1fr));
        gap: clamp(.75rem, 1.3vw, 1.15rem);
        margin: 1rem 0 1.55rem;
        contain: layout style;
    }
    .sl-kpi-card {
        --sl-kpi-accent: var(--sl-accent-bright);
        min-width: 0;
        position: relative;
        text-align: center;
        padding: clamp(1.1rem, 1.8vw, 1.55rem) 1rem;
        border: 1px solid var(--sl-border-default);
        border-radius: var(--sl-radius-lg);
        background:
            radial-gradient(ellipse 85% 65% at 50% -12%, color-mix(in srgb, var(--sl-kpi-accent) 13%, transparent), transparent 72%),
            var(--sl-surface-glass);
        box-shadow: var(--sl-shadow-card);
        overflow: hidden;
        transition: transform 240ms var(--sl-ease), border-color 240ms var(--sl-ease), box-shadow 240ms var(--sl-ease), background 240ms var(--sl-ease);
    }
    .sl-kpi-card::before {
        content: "";
        position: absolute;
        top: 0;
        left: 16%;
        right: 16%;
        height: 1.5px;
        background: linear-gradient(90deg, transparent, var(--sl-kpi-accent), transparent);
        opacity: 0.78;
        transition: opacity 240ms var(--sl-ease), left 240ms var(--sl-ease), right 240ms var(--sl-ease);
    }
    .sl-kpi-card:hover {
        transform: translateY(-3px);
        border-color: color-mix(in srgb, var(--sl-kpi-accent) 44%, rgba(148, 163, 184, 0.2));
        box-shadow:
            0 8px 24px rgba(0, 0, 0, 0.40),
            0 0 28px -8px color-mix(in srgb, var(--sl-kpi-accent) 34%, transparent),
            inset 0 1px 0 rgba(255, 255, 255, 0.09);
    }
    .sl-kpi-card:hover::before {
        left: 8%;
        right: 8%;
        opacity: 1;
    }
    .sl-kpi-value {
        color: var(--sl-kpi-accent);
        font: 700 clamp(1.6rem, 2.5vw, 2.22rem)/1.18 var(--sl-font-mono);
        font-variant-numeric: tabular-nums;
        letter-spacing: -.045em;
        overflow-wrap: anywhere;
        text-shadow: 0 0 22px color-mix(in srgb, var(--sl-kpi-accent) 28%, transparent);
    }
    .sl-kpi-label {
        margin-top: .68rem;
        color: var(--sl-text-primary);
        font: 650 .71rem/1.4 var(--sl-font-body);
        text-transform: uppercase;
        letter-spacing: .09em;
    }
    .sl-kpi-caption {
        margin-top: .28rem;
        color: var(--sl-text-muted);
        font: 450 .78rem/1.4 var(--sl-font-body);
    }
    @media (max-width: 740px) {
        .sl-page-header { flex-direction: column; gap: .45rem; }
        .sl-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .sl-kpi-card:last-child:nth-child(odd) { grid-column: 1 / -1; }
    }
    @media (prefers-reduced-motion: reduce) {
        .sl-click-curtain, .sl-transition-curtain { display: none !important; }
        [class*="st-key-stocklab_page_"] *,
        [class*="st-key-stocklab_page_"] *::before,
        [class*="st-key-stocklab_page_"] *::after,
        .st-key-topbar_nav button {
            animation-duration: .01ms !important;
            animation-iteration-count: 1 !important;
            transition-duration: .01ms !important;
        }
    }

    /* 6. Bloqueo de overflow horizontal */
    html, body, section[data-testid="stMain"], .main {
        overflow-x: hidden !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       ✨ STOCKLAB TRANSICIÓN EDITORIAL NEÓN SUTIL (~0.36s)
       ═══════════════════════════════════════════════════════════════════ */
    .sl-click-curtain {
        position: fixed !important;
        top: var(--sl-nav-height) !important;
        left: 0 !important;
        width: 100vw !important;
        height: calc(100vh - var(--sl-nav-height)) !important;
        background: rgba(8, 12, 21, 0.90) !important;
        backdrop-filter: blur(12px) !important;
        -webkit-backdrop-filter: blur(12px) !important;
        z-index: 99990 !important;
        pointer-events: none !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
    }

    .sl-transition-curtain {
        position: fixed !important;
        top: var(--sl-nav-height) !important;
        left: 0 !important;
        width: 100vw !important;
        height: calc(100vh - var(--sl-nav-height)) !important;
        background: rgba(8, 12, 21, 0.90) !important;
        backdrop-filter: blur(12px) !important;
        -webkit-backdrop-filter: blur(12px) !important;
        z-index: 99990 !important;
        pointer-events: none !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        animation: sl-page-fade 0.38s var(--sl-ease) forwards !important;
    }

    .sl-transition-beam {
        position: absolute !important;
        top: 0 !important;
        left: 0 !important;
        width: 100% !important;
        height: 2px !important;
        background: linear-gradient(90deg, transparent 0%, rgba(56, 189, 248, 0.85) 45%, rgba(99, 102, 241, 0.75) 58%, transparent 100%) !important;
        box-shadow: 0 0 14px rgba(56, 189, 248, 0.65) !important;
        animation: sl-beam-sweep 0.36s ease-out forwards !important;
    }

    .sl-transition-badge {
        display: inline-flex !important;
        align-items: center !important;
        gap: 10px !important;
        padding: 9px 20px !important;
        background: linear-gradient(145deg, rgba(19, 30, 48, 0.94), rgba(11, 17, 30, 0.96)) !important;
        border: 1px solid rgba(56, 189, 248, 0.32) !important;
        border-radius: 999px !important;
        box-shadow: 0 12px 32px rgba(0, 0, 0, 0.52), 0 0 24px -6px rgba(56, 189, 248, 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.08) !important;
        backdrop-filter: blur(14px) !important;
        -webkit-backdrop-filter: blur(14px) !important;
        animation: sl-badge-pulse 0.36s var(--sl-ease) forwards !important;
        user-select: none !important;
    }

    .sl-transition-badge-icon {
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        width: 24px !important;
        height: 24px !important;
        border-radius: 50% !important;
        background: rgba(56, 189, 248, 0.14) !important;
        border: 1px solid rgba(56, 189, 248, 0.38) !important;
        box-shadow: 0 0 12px rgba(56, 189, 248, 0.30) !important;
    }

    .sl-transition-badge-text {
        display: flex !important;
        flex-direction: row !important;
        align-items: center !important;
        gap: 8px !important;
        line-height: 1.2 !important;
    }

    .sl-badge-brand {
        font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
        font-size: 0.78rem !important;
        font-weight: 700 !important;
        letter-spacing: -0.01em !important;
        color: #f1f5f9 !important;
    }

    .sl-badge-status {
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 0.65rem !important;
        font-weight: 500 !important;
        letter-spacing: 0.08em !important;
        color: #38bdf8 !important;
        text-transform: uppercase !important;
    }

    @keyframes sl-badge-pulse {
        0%   { opacity: 0.85; transform: scale(0.98); }
        45%  { opacity: 1; transform: scale(1.0); }
        100% { opacity: 0; transform: scale(1.01); }
    }

    @keyframes sl-page-fade {
        0%   { opacity: 1; visibility: visible; }
        45%  { opacity: 0.92; visibility: visible; }
        100% { opacity: 0; visibility: hidden; }
    }

    @keyframes sl-beam-sweep {
        0%   { opacity: 0.35; transform: scaleX(0.25); }
        55%  { opacity: 0.85; transform: scaleX(1); }
        100% { opacity: 0; transform: scaleX(1); }
    }

    /* ═══════════════════════════════════════════════════════════════════
       💎 STOCKLAB LOADING TERMINAL — Global Fullscreen Sync Overlay
       ═══════════════════════════════════════════════════════════════════ */

    /* 1. Silenciar widgets de depuración y estado nativos de Streamlit */
    [data-testid="stStatusWidget"],
    div[data-testid="stStatusWidget"],
    .stStatusWidget,
    div[data-testid="stToolbar"] {
        display: none !important;
        visibility: hidden !important;
    }

    /* 2. Keyframe de giro suave */
    @keyframes sl-terminal-spin {
        0%   { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }

    /* 3. Overlay Fullscreen para st.spinner */
    div[data-testid="stSpinner"] {
        position: fixed !important;
        inset: 0 !important;
        top: 0 !important;
        left: 0 !important;
        width: 100vw !important;
        height: 100vh !important;
        background: rgba(11, 15, 23, 0.84) !important;
        backdrop-filter: blur(16px) !important;
        -webkit-backdrop-filter: blur(16px) !important;
        z-index: 999999 !important;
        display: flex !important;
        flex-direction: column !important;
        align-items: center !important;
        justify-content: center !important;
        margin: 0 !important;
        padding: 0 !important;
        pointer-events: all !important;
    }

    /* 4. Caja Flotante Central del Terminal — Glassmorphic con sutil borde neón */
    div[data-testid="stSpinner"] > div {
        display: flex !important;
        flex-direction: column !important;
        align-items: center !important;
        justify-content: center !important;
        background: linear-gradient(155deg, rgba(22, 34, 54, 0.95) 0%, rgba(12, 19, 32, 0.98) 100%) !important;
        border: 1px solid rgba(56, 189, 248, 0.28) !important;
        border-radius: 18px !important;
        padding: 36px 44px !important;
        max-width: 440px !important;
        width: 90% !important;
        text-align: center !important;
        position: relative !important;
        box-shadow: 0 28px 68px rgba(0, 0, 0, 0.65), 0 0 36px -10px rgba(56, 189, 248, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.08) !important;
    }

    /* Ocultar icono/círculo nativo pequeño de Streamlit */
    div[data-testid="stSpinner"] [data-testid="stSpinnerIcon"],
    div[data-testid="stSpinner"] span[class*="ewh6kot"],
    div[data-testid="stSpinner"] svg,
    div[data-testid="stSpinner"] i,
    div[data-testid="stSpinner"] [role="progressbar"],
    div[data-testid="stSpinner"] > div > *:first-child:not(:last-child) {
        display: none !important;
        visibility: hidden !important;
        width: 0 !important;
        height: 0 !important;
        opacity: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
    }

    /* 5. Anillo Giratorio Satinado (Sky + Indigo Neon) */
    div[data-testid="stSpinner"] > div::before {
        content: '' !important;
        display: block !important;
        width: 46px !important;
        height: 46px !important;
        border-radius: 50% !important;
        border: 2.5px solid rgba(148, 163, 184, 0.14) !important;
        border-top: 2.5px solid #38bdf8 !important;
        border-right: 2.5px solid #818cf8 !important;
        box-shadow: 0 0 18px rgba(56, 189, 248, 0.30) !important;
        animation: sl-terminal-spin 0.8s cubic-bezier(0.5, 0.15, 0.45, 0.85) infinite !important;
        margin: 0 auto 18px auto !important;
        order: -2 !important;
    }

    /* 6. Subtítulo Institucional Superior */
    div[data-testid="stSpinner"] > div::after {
        content: 'STOCKLAB TERMINAL' !important;
        display: block !important;
        font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
        font-size: 0.65rem !important;
        font-weight: 700 !important;
        letter-spacing: 0.20em !important;
        color: #64748b !important;
        text-transform: uppercase !important;
        margin-bottom: 6px !important;
        order: -1 !important;
    }

    /* 7. Tipografía Contextual Dinámica */
    div[data-testid="stSpinner"] > div > *,
    div[data-testid="stSpinner"] > div span,
    div[data-testid="stSpinner"] > div div {
        font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
        color: #e2e8f0 !important;
        font-size: 0.94rem !important;
        font-weight: 500 !important;
        letter-spacing: -0.01em !important;
        line-height: 1.5 !important;
        margin: 0 !important;
    }
</style>
""", unsafe_allow_html=True)

# 3a. Puerta de Autenticación Multiusuario (solo activa si Supabase está configurado)
from services.auth_service import (
    get_current_user as _get_auth_user,
    is_cloud_auth_enabled as _is_cloud_auth,
    is_cloud_user_authenticated as _is_cloud_authed,
)

if _is_cloud_auth() and not _is_cloud_authed():
    from views.auth import show_auth_gate
    show_auth_gate()
    st.stop()

# 3b. Precarga Global Unificada del Ecosistema StockLab (StockLab Loading Terminal)
#     Ejecuta una única vez por sesión para calentar todas las cachés en memoria
#     (Dashboard, Empresas, Watchlist, Ranking, Mi Cartera e IA). Tras esto, la navegación es instantánea (0 ms).
if "_ecosistema_precargado" not in st.session_state:
    with st.spinner("Sincronizando datos de Stocklab"):
        try:
            from services.prewarm import precargar_ecosistema_completo
            precargar_ecosistema_completo()
        except Exception as e:
            print(f"[PREWARM ERROR] {e}")
    st.session_state["_ecosistema_precargado"] = True
    st.session_state["_eventos_sincronizados"] = True

# 4. Definición de Menú y Vistas disponibles (Sin Emojis)
opciones_menu = {
    "Dashboard": dashboard,
    "Empresas": empresas,
    "Watchlist": watchlist,
    "Ranking": ranking,
    "Mi Cartera": mi_cartera,
    "IA": ia
}

paginas = list(opciones_menu.keys())

# Inicializar estado del menú si no existe
if "menu_activo" not in st.session_state:
    pagina_desde_url = st.query_params.get("page", None)
    st.session_state["menu_activo"] = pagina_desde_url if pagina_desde_url in paginas else "Dashboard"

# Mapeador defensivo para soportar llamadas con emojis, alias o nombres antiguos de callbacks
normalizar_menu = {
    "📊 Dashboard": "Dashboard",
    "🏢 Empresas": "Empresas",
    "🎯 Watchlist": "Watchlist",
    "🏆 Ranking": "Ranking",
    "💼 Mi Cartera": "Mi Cartera",
    "🤖 Inteligencia Artificial": "IA",
    "Inteligencia Artificial": "IA",
    "🤖 IA": "IA",
    "IA": "IA",
    "Panel de Análisis": "IA",
    "Análisis": "IA",
    "Configuración": "Dashboard",
    "⚙️ Configuración": "Dashboard"
}
if st.session_state["menu_activo"] in normalizar_menu:
    st.session_state["menu_activo"] = normalizar_menu[st.session_state["menu_activo"]]

# 5. Callback de navegación (evita doble rerun — patrón on_click)
def _cambiar_pagina(pagina):
    st.session_state["menu_activo"] = pagina

# Detección de cambio de área para la transición fluida de 0.5s
if "menu_activo_anterior" not in st.session_state:
    st.session_state["menu_activo_anterior"] = st.session_state["menu_activo"]
    es_cambio_de_area = False
else:
    es_cambio_de_area = (st.session_state["menu_activo_anterior"] != st.session_state["menu_activo"])
    st.session_state["menu_activo_anterior"] = st.session_state["menu_activo"]

# 6. Renderizado Nativo de Topbar Superior
with st.container(key="topbar_nav"):
    cols = st.columns([2.2, 1, 1, 1, 1, 1, 1, 2.2], vertical_alignment="center")
    
    # Columna 0: Logo StockLab + INVESTMENT TERMINAL
    with cols[0]:
        st.markdown("""
        <div class="stocklab-logo-container">
            <div class="stocklab-logo-title">StockLab</div>
            <div class="stocklab-logo-subtitle">INVESTMENT TERMINAL</div>
        </div>
        """, unsafe_allow_html=True)
    
    # Columnas 1-6: Tabs de Navegación Nativas (100% Streamlit st.button + on_click)
    for idx, pag in enumerate(paginas):
        with cols[idx + 1]:
            es_activa = st.session_state["menu_activo"] == pag
            tipo_boton = "primary" if es_activa else "secondary"
            st.button(
                pag,
                key=f"topbar_nav_{pag}",
                use_container_width=True,
                type=tipo_boton,
                on_click=_cambiar_pagina,
                args=(pag,)
            )

    # Columna 7: Acceso compacto a Mi Cuenta y Alertas (alineado a la derecha)
    with cols[7]:
        _u_top = _get_auth_user()
        _lbl_cuenta = (
            (_u_top.get("name", "").split()[0][:12] if _u_top and _u_top.get("name") else "Mi Cuenta")
            if _u_top
            else "Cuenta"
        )
        _, _c_acc = st.columns([1.0, 1.2])
        with _c_acc:
            if st.button(_lbl_cuenta, key="topbar_btn_cuenta", use_container_width=True, type="secondary"):
                st.session_state["mostrar_modal_cuenta"] = True

st.markdown("<div class='topbar-spacer'></div>", unsafe_allow_html=True)

if st.session_state.pop("mostrar_modal_cuenta", False):
    from views.cuenta import mostrar_modal_cuenta
    mostrar_modal_cuenta()

# ── Transición Editorial Sutil (~0.38s) entre Áreas (TopBar fija) ──
if es_cambio_de_area:
    import time
    curtain_id = f"sl_curtain_{int(time.time()*1000)}"
    nombre_modulo = str(st.session_state.get("menu_activo", "TERMINAL")).upper()
    st.html(f"""
    <div id="{curtain_id}" class="sl-transition-curtain">
        <div class="sl-transition-beam"></div>
        <div class="sl-transition-badge">
            <div class="sl-transition-badge-icon">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
                </svg>
            </div>
            <div class="sl-transition-badge-text">
                <span class="sl-badge-brand">StockLab</span>
                <span class="sl-badge-status">{nombre_modulo}</span>
            </div>
        </div>
    </div>
    <script>
    (function() {{
        // Eliminar inmediatamente cortina previa de click si existiera
        const clickCurtain = document.getElementById('sl-click-curtain');
        if (clickCurtain) clickCurtain.remove();

        // Autolimpieza tras terminar exactamente la animación de ~0.38s
        const cur = document.getElementById('{curtain_id}');
        if (cur) {{
            setTimeout(() => {{
                if (cur && cur.parentNode) cur.remove();
            }}, 420);
        }}
    }})();
    </script>
    """, unsafe_allow_javascript=True)

# Script persistente para respuesta instantánea al hacer clic en las pestañas del topbar
st.html("""
<script>
(function() {
    function setupNavTransitions() {
        const buttons = document.querySelectorAll('.st-key-topbar_nav button:not(.st-key-topbar_btn_cuenta button)');
        buttons.forEach(btn => {
            if (!btn.dataset.slTransitionBound) {
                btn.dataset.slTransitionBound = "true";
                btn.addEventListener('click', function() {
                    const isPrimary = this.getAttribute('kind') === 'primary' || 
                                      this.getAttribute('data-testid') === 'baseButton-primary';
                    if (!isPrimary) {
                        // Crear cortina instantánea bajo el menú superior (top: 76px)
                        const prev = document.getElementById('sl-click-curtain');
                        if (prev) prev.remove();
                        const curtain = document.createElement('div');
                        curtain.id = 'sl-click-curtain';
                        curtain.className = 'sl-click-curtain';
                        const beam = document.createElement('div');
                        beam.className = 'sl-transition-beam';
                        curtain.appendChild(beam);
                        document.body.appendChild(curtain);
                        // Temporizador de seguridad
                        setTimeout(() => {
                            if (curtain && curtain.parentNode) curtain.remove();
                        }, 2400);
                    }
                });
            }
        });
    }
    setupNavTransitions();
    let slNavScheduled = false;
    const obs = new MutationObserver(() => {
        if (!slNavScheduled) {
            slNavScheduled = true;
            requestAnimationFrame(() => {
                slNavScheduled = false;
                setupNavTransitions();
            });
        }
    });
    const navTarget = document.querySelector('.st-key-topbar_nav') || document.body;
    obs.observe(navTarget, { childList: true, subtree: true });
})();
</script>
""", unsafe_allow_javascript=True)

# 6. Renderizar de forma dinámica la sección seleccionada (con aislamiento estricto de DOM por vista)
if st.session_state["menu_activo"] in opciones_menu:
    with st.container(key=f"stocklab_page_{st.session_state['menu_activo']}"):
        opciones_menu[st.session_state["menu_activo"]].show()

# 6b. Pie institucional discreto — Aviso informativo y de responsabilidad
st.markdown(
    """
    <div style="margin-top:3.2rem;padding-top:1.1rem;border-top:1px solid rgba(148,163,184,0.10);
                display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;
                font-family:'Inter',sans-serif;font-size:0.72rem;color:#64748b;letter-spacing:0.01em;">
        <span>StockLab · Plataforma de seguimiento y análisis bursátil</span>
        <span>Aviso informativo: los datos, métricas y señales no constituyen asesoramiento financiero ni recomendación de inversión.</span>
    </div>
    """,
    unsafe_allow_html=True,
)

# 7. Armonización Final Global — Tipografía, Superficies, Controles y Separadores (6 Áreas)
st.markdown("""
<style>
    /* Números tabulares simétricos en KPIs, métricas y celdas financieras */
    div[data-testid="stMetricValue"],
    div[data-testid="stMetricDelta"],
    .kpi-hero-value,
    .kpi-num,
    .sl-kpi-value,
    .ia-hist-kpi-value,
    .ia-mono-val,
    .rnk-price,
    .score-badge {
        font-variant-numeric: tabular-nums !important;
    }

    /* Jerarquía tipográfica 100% homogénea en las 6 áreas (Plus Jakarta Sans en títulos) */
    section[data-testid="stMain"] h1:not(.sl-page-title),
    section[data-testid="stMain"] .stMarkdown h1:not(.sl-page-title),
    section[data-testid="stMain"] h2:not(.sl-section-title),
    section[data-testid="stMain"] .stMarkdown h2:not(.sl-section-title),
    section[data-testid="stMain"] h3,
    section[data-testid="stMain"] .stMarkdown h3 {
        background: none !important;
        -webkit-background-clip: initial !important;
        -webkit-text-fill-color: initial !important;
        background-clip: initial !important;
        text-shadow: none !important;
        font-family: var(--sl-font-display) !important;
        color: var(--sl-text-heading) !important;
        letter-spacing: -0.025em !important;
    }
    section[data-testid="stMain"] h2:not(.sl-section-title),
    section[data-testid="stMain"] .stMarkdown h2:not(.sl-section-title) {
        color: #f1f5f9 !important;
    }
    section[data-testid="stMain"] h3,
    section[data-testid="stMain"] .stMarkdown h3 {
        color: #e2e8f0 !important;
    }

    /* Títulos de página y sección idénticos en las 6 áreas (autoridad de cascada final) */
    section[data-testid="stMain"] .sl-page-header .sl-page-title {
        background: linear-gradient(180deg, #ffffff 22%, #cbd5e1 100%) !important;
        -webkit-background-clip: text !important;
        -webkit-text-fill-color: transparent !important;
        background-clip: text !important;
        font: 800 clamp(1.7rem, 2.65vw, 2.35rem)/1.15 var(--sl-font-display) !important;
        letter-spacing: -0.042em !important;
    }
    section[data-testid="stMain"] .sl-page-header .sl-page-subtitle {
        color: var(--sl-text-secondary) !important;
        font: 450 0.92rem/1.55 var(--sl-font-body) !important;
    }
    section[data-testid="stMain"] .sl-section-title {
        display: flex !important;
        flex-wrap: wrap !important;
        align-items: center !important;
        gap: 0.45rem 0.7rem !important;
        color: var(--sl-text-heading) !important;
        font: 700 1.08rem/1.35 var(--sl-font-display) !important;
        letter-spacing: -0.024em !important;
    }
    section[data-testid="stMain"] .sl-section-title::before {
        content: "" !important;
        display: inline-block !important;
        width: 3px !important;
        height: 1rem !important;
        border-radius: 999px !important;
        background: linear-gradient(180deg, #7dd3fc 0%, #38bdf8 55%, #6366f1 100%) !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.58) !important;
        flex-shrink: 0 !important;
    }

    /* Pastillas de contador homogéneas en Dashboard, Empresas, Watchlist, Ranking y Cartera */
    section[data-testid="stMain"] .active-counter-badge,
    section[data-testid="stMain"] .table-counter {
        display: inline-flex !important;
        align-items: center !important;
        font-family: var(--sl-font-mono) !important;
        font-size: 0.70rem !important;
        font-weight: 700 !important;
        color: #7dd3fc !important;
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.13), rgba(99, 102, 241, 0.08)) !important;
        border: 1px solid rgba(56, 189, 248, 0.30) !important;
        border-radius: 999px !important;
        padding: 2px 10px !important;
        letter-spacing: 0.04em !important;
        margin-left: 6px !important;
        box-shadow: 0 0 14px -5px rgba(56, 189, 248, 0.36) !important;
        vertical-align: middle !important;
    }

    /* Pestañas internas armonizadas con resplandor neón sutil */
    [data-testid="stMain"] .stTabs [data-baseweb="tab-list"] {
        background: linear-gradient(180deg, rgba(13, 20, 34, 0.88), rgba(9, 14, 24, 0.96)) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: 13px !important;
        padding: 5px !important;
        gap: 6px !important;
        box-shadow: 0 4px 18px rgba(0, 0, 0, 0.36), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
    }
    [data-testid="stMain"] .stTabs [data-baseweb="tab"] {
        color: var(--sl-text-secondary) !important;
        font-family: var(--sl-font-display) !important;
        font-weight: 600 !important;
        border-radius: 9px !important;
        border: 1px solid transparent !important;
        transition: color 200ms var(--sl-ease), background 200ms var(--sl-ease), border-color 200ms var(--sl-ease), box-shadow 200ms var(--sl-ease) !important;
    }
    [data-testid="stMain"] .stTabs [data-baseweb="tab"]:hover {
        color: var(--sl-text-heading) !important;
        background: rgba(56, 189, 248, 0.06) !important;
        border-color: rgba(148, 163, 184, 0.16) !important;
    }
    [data-testid="stMain"] .stTabs [data-baseweb="tab"][aria-selected="true"] {
        color: #e0f6ff !important;
        font-weight: 700 !important;
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.18), rgba(99, 102, 241, 0.11)) !important;
        border-color: rgba(56, 189, 248, 0.38) !important;
        box-shadow: 0 0 18px -6px rgba(56, 189, 248, 0.40), inset 0 1px 0 rgba(255, 255, 255, 0.10) !important;
    }
    [data-testid="stMain"] .stTabs [data-baseweb="tab-highlight"],
    [data-testid="stMain"] .stTabs [data-baseweb="tab-border"] {
        display: none !important;
    }

    /* Selectboxes, Inputs y Popovers unificados (sin tocar el Prompt Box de IA) */
    section[data-testid="stMain"] :not(.st-key-ia_prompt_box) .stSelectbox [data-baseweb="select"],
    section[data-testid="stMain"] :not(.st-key-ia_prompt_box) .stMultiSelect [data-baseweb="select"] {
        background-color: var(--sl-bg-sunken) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: 10px !important;
        transition: border-color 200ms var(--sl-ease), box-shadow 200ms var(--sl-ease), background-color 200ms var(--sl-ease) !important;
    }
    section[data-testid="stMain"] :not(.st-key-ia_prompt_box) .stSelectbox [data-baseweb="select"]:hover,
    section[data-testid="stMain"] :not(.st-key-ia_prompt_box) .stMultiSelect [data-baseweb="select"]:hover {
        border-color: rgba(56, 189, 248, 0.32) !important;
    }
    section[data-testid="stMain"] :not(.st-key-ia_prompt_box) .stSelectbox [data-baseweb="select"]:focus-within,
    section[data-testid="stMain"] :not(.st-key-ia_prompt_box) .stMultiSelect [data-baseweb="select"]:focus-within {
        border-color: rgba(56, 189, 248, 0.56) !important;
        box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.12) !important;
    }

    /* Popover y menús desplegables oscuros (sin backdrop-filter ni alteración de geometría virtualizada) */
    [data-baseweb="popover"] [data-baseweb="menu"],
    div[data-baseweb="popover"] ul[role="listbox"] {
        background: #0f1726 !important;
        border: 1px solid rgba(56, 189, 248, 0.26) !important;
        border-radius: 12px !important;
        box-shadow: 0 14px 36px rgba(0, 0, 0, 0.72) !important;
    }
    [data-baseweb="menu"] li,
    div[data-baseweb="popover"] li[role="option"] {
        transition: background-color 120ms ease, color 120ms ease !important;
    }
    [data-baseweb="menu"] li:hover,
    div[data-baseweb="popover"] li[role="option"]:hover {
        background-color: rgba(56, 189, 248, 0.13) !important;
        color: #7dd3fc !important;
    }

    /* Expanders uniformes con resplandor sutil al pasar el ratón */
    div[data-testid="stExpander"],
    details[data-testid="stExpander"] {
        background: var(--sl-surface-glass) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: var(--sl-radius-lg) !important;
        box-shadow: var(--sl-shadow-card) !important;
        transition: border-color 220ms var(--sl-ease), box-shadow 220ms var(--sl-ease) !important;
    }
    div[data-testid="stExpander"]:hover,
    details[data-testid="stExpander"]:hover {
        border-color: var(--sl-border-hover) !important;
        box-shadow: var(--sl-shadow-hover) !important;
    }

    /* Scrollbar refinado y uniforme */
    ::-webkit-scrollbar {
        width: 6px !important;
        height: 6px !important;
    }
    ::-webkit-scrollbar-track {
        background: #080c15 !important;
    }
    ::-webkit-scrollbar-thumb {
        background: rgba(148, 163, 184, 0.22) !important;
        border-radius: 999px !important;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: rgba(56, 189, 248, 0.45) !important;
    }

    /* Separadores hairline con sutil luz central cian en las 6 vistas */
    hr, .stDivider hr,
    .dash-divider, .wl-divider, .emp-divider,
    .ranking-divider, .grad-divider, .ia-divider {
        border: none !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent 0%, rgba(148, 163, 184, 0.16) 22%, rgba(56, 189, 248, 0.28) 50%, rgba(148, 163, 184, 0.16) 78%, transparent 100%) !important;
    }
</style>
""", unsafe_allow_html=True)