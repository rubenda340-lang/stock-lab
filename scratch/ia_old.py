"""
Módulo de Inteligencia Artificial — StockLab Terminal
=====================================================
Estructura definida por wireframe (v2):

  1. 🤖 Agente de IA         — prompt bar con indicador de modelo + tarjetas
                                de preguntas frecuentes + chat conversacional
  2. 🎯 Radar Predictivo     — selector horizontal de horizonte (1D/3D/7D/1M)
                                + tabla unificada (Empresa, Precio, RSI, Confianza,
                                Proyección, Predicción, Tendencia)
                                + inspección detallada con cono algorítmico
  3. 📜 Historial Predictivo — selector de horizonte + tabla de auditoría

Motor: services/signal_engine.py · Asistente: services/assistant_engine.py
LLM Agent: services/llm_agent.py · Tracking: database/predicciones.py
"""

import pandas as pd
import streamlit as st

from components.stock_chart import render_stock_chart
from database import predicciones as db_pred
from database.connection import query_all
from services import assistant_engine, llm_agent, signal_engine
from services.data_provider import get_price_batch

_ICONOS_SENAL = {"ALCISTA": "🟢", "LATERAL": "🟡", "BAJISTA": "🔴"}

# Mapa de horizontes: clave UI → clave interna del motor
_HZ_MAP = {
    "1D": "1D",
    "3D": "3D",
    "1S": "1S",
    "7D": "1S",   # 7 días = 1 semana en el motor
    "1M": "1M",
}

# Etiqueta legible de cada horizonte (para títulos y diagnósticos)
_HZ_LABELS = {
    "1D": "1 Día",
    "3D": "3 Días",
    "1S": "1 Semana",
    "7D": "1 Semana",
    "1M": "1 Mes",
}

# Definición de cada tipo de diagnóstico (popup de inspección del radar)
_DEFINICIONES_SENAL = {
    "ALCISTA": "Los indicadores técnicos (RSI, MACD, medias móviles y momentum) apuntan "
               "mayoritariamente al alza: el escenario más probable es una revalorización "
               "del precio en este horizonte.",
    "LATERAL": "Los indicadores técnicos aparecen divididos o en zona neutra: se espera "
               "un movimiento acotado del precio, sin una dirección clara dominante en "
               "este horizonte.",
    "BAJISTA": "Los indicadores técnicos (RSI, MACD, medias móviles y momentum) apuntan "
               "mayoritariamente a la baja: el escenario más probable es una depreciación "
               "del precio en este horizonte.",
}


# ═══════════════════════════════════════════════════════════════════
# 🎨 CSS — LENGUAJE VISUAL INSTITUCIONAL DEL ECOSISTEMA STOCKLAB
# ═══════════════════════════════════════════════════════════════════

def _css() -> None:
    st.markdown(
        """
<style>
/* ── Animaciones ──────────────────────────────────────────────────── */
@keyframes fade-in-up {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
}
@keyframes led-breathe {
    0%, 100% { opacity: 0.35; }
    50%      { opacity: 0.85; }
}

/* ── Fondo Global ─────────────────────────────────────────────────── */
section[data-testid="stMain"] {
    background: #07090f !important;
    background-image:
        radial-gradient(ellipse 90% 55% at 50% -5%,  rgba(0,229,255,0.035) 0%, transparent 60%),
        radial-gradient(ellipse 55% 45% at 92% 105%, rgba(16,185,129,0.025) 0%, transparent 55%),
        radial-gradient(ellipse 40% 30% at 8%  80%,  rgba(99,102,241,0.015) 0%, transparent 60%) !important;
}

/* ── Ajuste Equilibrado del Espaciado Superior (Bajo menú fijo) ──── */
.main .block-container,
div[data-testid="stMainBlockContainer"] {
    padding-top: 0.5rem !important;
}
.topbar-spacer {
    height: 76px !important;
    min-height: 76px !important;
    margin-bottom: -18px !important;
}
div:has(> .ia-section-header),
div[data-testid="stElementContainer"]:has(.ia-section-header) {
    margin-top: 0 !important;
    padding-top: 0 !important;
}

/* ── Título de Sección — centrado, limpio y sobrio ────────────────── */
.ia-section-header {
    text-align: center;
    margin: 0 0 10px 0 !important;
    padding: 0 !important;
    animation: fade-in-up 0.25s ease-out both;
}
.ia-section-title {
    font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif !important;
    font-size: 1.35rem !important;
    font-weight: 800 !important;
    letter-spacing: -0.03em !important;
    color: #ffffff !important;
    margin: 0 0 2px 0 !important;
}
.ia-section-sub {
    font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif !important;
    font-size: 0.82rem !important;
    color: #94a3b8 !important;
    font-weight: 500 !important;
    letter-spacing: -0.01em !important;
}

/* ── Divisor con Glowing Gradient ──────────────────────────────────── */
.ia-divider {
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(0,229,255,0.32), rgba(16,185,129,0.22), transparent);
    margin: 32px 0 26px;
}

/* ── Barra de Prompt Unificada (Estilo Gemini / ChatGPT) ──────────── */
.st-key-ia_prompt_box,
div[data-testid="stForm"]:has(.ia-prompt-form),
.st-key-ia_form {
    background: linear-gradient(160deg, #0d111d, #080a12) !important;
    border: 1px solid rgba(0, 229, 255, 0.22) !important;
    border-radius: 14px !important;
    padding: 6px 10px 6px 14px !important;
    margin: 26px auto 6px auto !important;
    max-width: 860px !important;
    box-shadow: 0 4px 25px rgba(0, 0, 0, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.06) !important;
    transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1) !important;
    box-sizing: border-box !important;
}
.st-key-ia_prompt_box:hover,
.st-key-ia_prompt_box:focus-within,
div[data-testid="stForm"]:has(.ia-prompt-form):hover,
.st-key-ia_form:hover,
div[data-testid="stForm"]:has(.ia-prompt-form):focus-within,
.st-key-ia_form:focus-within {
    border-color: rgba(0, 229, 255, 0.48) !important;
    box-shadow: 0 0 24px rgba(0, 229, 255, 0.16), 0 8px 32px rgba(0, 0, 0, 0.65) !important;
}

/* Forzar centrado vertical estricto en el eje Y y distribución equilibrada */
.st-key-ia_prompt_box div[data-testid="stVerticalBlock"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stVerticalBlock"],
.st-key-ia_form div[data-testid="stVerticalBlock"] {
    gap: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: center !important;
}
.st-key-ia_prompt_box div[data-testid="stHorizontalBlock"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stHorizontalBlock"],
.st-key-ia_form div[data-testid="stHorizontalBlock"] {
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    justify-content: space-between !important;
    gap: 8px !important;
    margin: 0 !important;
    padding: 0 !important;
    width: 100% !important;
}
.st-key-ia_prompt_box div[data-testid="stColumn"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stColumn"],
.st-key-ia_form div[data-testid="stColumn"] {
    display: flex !important;
    flex-direction: row !important;
    justify-content: center !important;
    align-items: center !important;
    margin: 0 !important;
    padding: 0 !important;
    min-height: 38px !important;
    height: 38px !important;
}

/* Columna 1: Buscador (ocupa todo el espacio disponible) */
.st-key-ia_prompt_box div[data-testid="stColumn"]:nth-child(1),
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stColumn"]:nth-child(1),
.st-key-ia_form div[data-testid="stColumn"]:nth-child(1) {
    flex: 1 1 auto !important;
    width: auto !important;
    min-width: 0 !important;
    justify-content: flex-start !important;
}

/* Columna 2: Pequeña flecha de envío (a la izquierda del selector) */
.st-key-ia_prompt_box div[data-testid="stColumn"]:nth-child(2),
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stColumn"]:nth-child(2),
.st-key-ia_form div[data-testid="stColumn"]:nth-child(2) {
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: auto !important;
    justify-content: center !important;
}

/* Columna 3: Selector de modelos de Gemini (a la derecha) */
.st-key-ia_prompt_box div[data-testid="stColumn"]:nth-child(3),
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stColumn"]:nth-child(3),
.st-key-ia_form div[data-testid="stColumn"]:nth-child(3) {
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: 175px !important;
    max-width: 220px !important;
    justify-content: center !important;
}

.st-key-ia_prompt_box div[data-testid="stColumn"] > div,
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stColumn"] > div,
.st-key-ia_form div[data-testid="stColumn"] > div,
.st-key-ia_prompt_box div[data-testid="stElementContainer"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stElementContainer"],
.st-key-ia_form div[data-testid="stElementContainer"] {
    margin: 0 !important;
    padding: 0 !important;
    width: 100% !important;
    height: 100% !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}

/* 1. Buscador centrado y transparente dentro del box */
.st-key-ia_prompt_box div[data-testid="stTextInput"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"],
.st-key-ia_form div[data-testid="stTextInput"] {
    margin: 0 !important;
    padding: 0 !important;
    width: 100% !important;
    display: flex !important;
    align-items: center !important;
}
.st-key-ia_prompt_box div[data-testid="stTextInput"] div[data-testid="stWidgetLabel"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"] div[data-testid="stWidgetLabel"],
.st-key-ia_form div[data-testid="stTextInput"] div[data-testid="stWidgetLabel"] {
    display: none !important;
    margin: 0 !important;
    padding: 0 !important;
}
.st-key-ia_prompt_box div[data-testid="stTextInput"] div[data-baseweb="input"],
.st-key-ia_prompt_box div[data-testid="stTextInput"] div[data-baseweb="base-input"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"] div[data-baseweb="input"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"] div[data-baseweb="base-input"],
.st-key-ia_form div[data-testid="stTextInput"] div[data-baseweb="input"],
.st-key-ia_form div[data-testid="stTextInput"] div[data-baseweb="base-input"] {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    outline: none !important;
    height: 38px !important;
    min-height: 38px !important;
    max-height: 38px !important;
    box-sizing: border-box !important;
    display: flex !important;
    align-items: center !important;
    width: 100% !important;
    padding: 0 !important;
}
.st-key-ia_prompt_box div[data-testid="stTextInput"] input,
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"] input,
.st-key-ia_form div[data-testid="stTextInput"] input {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    outline: none !important;
    color: #f1f5f9 !important;
    font-size: 0.92rem !important;
    font-family: 'Inter', sans-serif !important;
    padding: 0 4px !important;
    height: 100% !important;
    box-sizing: border-box !important;
    line-height: 38px !important;
}
.st-key-ia_prompt_box div[data-testid="stTextInput"] input::placeholder,
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"] input::placeholder,
.st-key-ia_form div[data-testid="stTextInput"] input::placeholder {
    color: #64748b !important;
    font-size: 0.88rem !important;
    font-style: normal !important;
}
.st-key-ia_prompt_box div[data-testid="stTextInput"] input:focus,
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"] input:focus,
.st-key-ia_form div[data-testid="stTextInput"] input:focus {
    outline: none !important;
    border: none !important;
    box-shadow: none !important;
}

/* 2. Botón pequeña flecha de envío (a la izquierda del selector) */
.st-key-ia_prompt_box button[kind="secondary"],
.st-key-ia_prompt_box button[kind="primary"],
.st-key-ia_prompt_box button,
div[data-testid="stForm"]:has(.ia-prompt-form) button[kind="secondaryFormSubmit"],
div[data-testid="stForm"]:has(.ia-prompt-form) button[kind="primaryFormSubmit"],
div[data-testid="stForm"]:has(.ia-prompt-form) button,
.st-key-ia_form button[kind="secondaryFormSubmit"],
.st-key-ia_form button[kind="primaryFormSubmit"],
.st-key-ia_form button {
    background: rgba(0, 229, 255, 0.08) !important;
    border: 1px solid rgba(0, 229, 255, 0.28) !important;
    color: #00e5ff !important;
    font-size: 0.85rem !important;
    font-weight: 700 !important;
    border-radius: 50% !important;
    height: 34px !important;
    min-height: 34px !important;
    max-height: 34px !important;
    width: 34px !important;
    min-width: 34px !important;
    max-width: 34px !important;
    box-sizing: border-box !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    margin: 0 !important;
    padding: 0 !important;
    cursor: pointer !important;
    box-shadow: 0 0 10px rgba(0, 229, 255, 0.10) !important;
    transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1) !important;
}
.st-key-ia_prompt_box button:hover,
div[data-testid="stForm"]:has(.ia-prompt-form) button:hover,
.st-key-ia_form button:hover {
    background: #00e5ff !important;
    border-color: #00e5ff !important;
    color: #040812 !important;
    transform: scale(1.08) !important;
    box-shadow: 0 0 16px rgba(0, 229, 255, 0.5) !important;
}
.st-key-ia_prompt_box button div[data-testid="stMarkdownContainer"],
.st-key-ia_prompt_box button p,
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stFormSubmitButton"] button div[data-testid="stMarkdownContainer"],
.st-key-ia_form div[data-testid="stFormSubmitButton"] button div[data-testid="stMarkdownContainer"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stFormSubmitButton"] button p,
.st-key-ia_form div[data-testid="stFormSubmitButton"] button p {
    margin: 0 !important;
    padding: 0 !important;
    line-height: 1 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}

/* 3. Selector de modelos de Gemini (Totalmente integrado sin parches) */
.st-key-ia_prompt_box div[data-testid="stSelectbox"] {
    margin: 0 !important;
    padding: 0 !important;
    width: 100% !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-testid="stWidgetLabel"] {
    display: none !important;
    margin: 0 !important;
    padding: 0 !important;
}
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
    margin: 0 !important;
    width: 100% !important;
    height: 34px !important;
    min-height: 34px !important;
    max-height: 34px !important;
    display: flex !important;
    align-items: center !important;
}

/* Control interno real de BaseWeb: píldora translúcida cian idéntica al diseño original */
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
    background: rgba(0, 229, 255, 0.08) !important;
    border: 1px solid rgba(0, 229, 255, 0.28) !important;
    border-radius: 9px !important;
    height: 34px !important;
    min-height: 34px !important;
    max-height: 34px !important;
    box-sizing: border-box !important;
    cursor: pointer !important;
    transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1) !important;
    padding: 0 8px 0 12px !important;
    margin: 0 !important;
    width: 100% !important;
    display: flex !important;
    align-items: center !important;
    justify-content: space-between !important;
    box-shadow: none !important;
}
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] > div:hover {
    background: rgba(0, 229, 255, 0.14) !important;
    border-color: rgba(0, 229, 255, 0.50) !important;
    box-shadow: 0 0 14px rgba(0, 229, 255, 0.22) !important;
}

/* Contenedor del valor seleccionado y todos sus niveles interiores */
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] > div > div,
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] > div > div > div,
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] [data-testid="stSelectboxSelectedValue"],
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] [data-baseweb="select-value"] {
    display: flex !important;
    align-items: center !important;
    justify-content: flex-start !important;
    height: 34px !important;
    min-height: 34px !important;
    max-height: 34px !important;
    line-height: 34px !important;
    padding: 0 !important;
    margin: 0 !important;
    position: relative !important;
    top: 0 !important;
    bottom: 0 !important;
    transform: none !important;
    box-sizing: border-box !important;
}

/* El input oculto de BaseWeb para que no empuje el texto verticalmente */
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] input {
    position: absolute !important;
    opacity: 0 !important;
    pointer-events: none !important;
    width: 0 !important;
    height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}

/* Texto del modelo seleccionado — centrado vertical exacto */
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] * {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.74rem !important;
    font-weight: 700 !important;
    color: #67e8f9 !important;
    letter-spacing: 0.3px !important;
    background: transparent !important;
    vertical-align: middle !important;
}

/* Contenedor del icono SVG de la flecha */
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-baseweb="select"] > div > div:last-child {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    height: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
    flex-shrink: 0 !important;
}

/* Flechita SVG dentro del select */
.st-key-ia_prompt_box div[data-testid="stSelectbox"] svg {
    fill: #00e5ff !important;
    color: #00e5ff !important;
    width: 14px !important;
    height: 14px !important;
    display: block !important;
    margin: 0 !important;
}

/* Menú desplegable emergente de modelos (Popover del selectbox) */
div[data-baseweb="popover"]:has(ul[role="listbox"]) {
    background: transparent !important;
    border: none !important;
}
div[data-baseweb="popover"] ul[role="listbox"] {
    background: #0b0f19 !important;
    border: 1px solid rgba(0, 229, 255, 0.35) !important;
    border-radius: 12px !important;
    box-shadow: 0 12px 35px rgba(0, 0, 0, 0.8), 0 0 20px rgba(0, 229, 255, 0.15) !important;
    padding: 6px !important;
}
div[data-baseweb="popover"] li[role="option"] {
    background: transparent !important;
    border-radius: 8px !important;
    color: #cbd5e1 !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.78rem !important;
    font-weight: 600 !important;
    padding: 8px 12px !important;
    margin: 2px 0 !important;
    transition: all 0.15s ease !important;
    cursor: pointer !important;
}
div[data-baseweb="popover"] li[role="option"]:hover,
div[data-baseweb="popover"] li[role="option"][aria-selected="true"] {
    background: rgba(0, 229, 255, 0.16) !important;
    color: #00e5ff !important;
    box-shadow: inset 0 0 8px rgba(0, 229, 255, 0.15) !important;
}

.ia-model-badge {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 6px !important;
    padding: 0 13px !important;
    height: 34px !important;
    min-height: 34px !important;
    max-height: 34px !important;
    box-sizing: border-box !important;
    border-radius: 9px !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.76rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.5px !important;
    white-space: nowrap !important;
    width: auto !important;
    margin: 0 !important;
    line-height: 1 !important;
    cursor: default !important;
    transition: all 0.2s ease !important;
}
.ia-model-badge.gemini {
    background: rgba(0, 229, 255, 0.08) !important;
    border: 1px solid rgba(0, 229, 255, 0.28) !important;
    color: #67e8f9 !important;
}
.ia-model-badge.reglas {
    background: rgba(251, 191, 36, 0.08) !important;
    border: 1px solid rgba(251, 191, 36, 0.28) !important;
    color: #fbbf24 !important;
}

/* ── Barra de Uso Diario de IA (0% a 100%) ────────────────────────── */
.ia-usage-wrap {
    max-width: 860px;
    margin: -6px auto 18px auto !important;
    padding: 8px 14px 10px 14px;
    background: rgba(13, 17, 29, 0.58);
    border: 1px solid rgba(0, 229, 255, 0.12);
    border-radius: 10px;
    box-sizing: border-box;
    backdrop-filter: blur(8px);
    transition: all 0.25s ease;
}
.ia-usage-wrap:hover {
    border-color: rgba(0, 229, 255, 0.26);
    background: rgba(13, 17, 29, 0.78);
    box-shadow: 0 4px 15px rgba(0, 0, 0, 0.35);
}
.ia-usage-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 7px;
    font-family: 'Inter', sans-serif;
}
.ia-usage-left {
    display: flex;
    align-items: center;
    gap: 8px;
}
.ia-usage-dot-status {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    display: inline-block;
    background: #00e5ff;
    box-shadow: 0 0 7px #00e5ff;
}
.ia-usage-dot-status.aviso {
    background: #f59e0b;
    box-shadow: 0 0 7px #f59e0b;
}
.ia-usage-dot-status.limite {
    background: #ef4444;
    box-shadow: 0 0 7px #ef4444;
}
.ia-usage-title {
    font-size: 0.75rem;
    font-weight: 700;
    color: #e2e8f0;
    letter-spacing: 0.4px;
    text-transform: uppercase;
}
.ia-usage-model-tag {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.68rem;
    font-weight: 600;
    color: #67e8f9;
    background: rgba(0, 229, 255, 0.08);
    border: 1px solid rgba(0, 229, 255, 0.20);
    padding: 1px 7px;
    border-radius: 5px;
}
.ia-usage-right {
    display: flex;
    align-items: center;
    gap: 10px;
}
.ia-usage-detail {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.68rem;
    color: #94a3b8;
}
.ia-usage-pct {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.76rem;
    font-weight: 800;
    color: #00e5ff;
    padding: 1px 7px;
    background: rgba(0, 229, 255, 0.10);
    border-radius: 5px;
    letter-spacing: 0.3px;
}
.ia-usage-pct.aviso {
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.12);
}
.ia-usage-pct.limite {
    color: #f87171;
    background: rgba(248, 113, 113, 0.15);
}
.ia-usage-track {
    width: 100%;
    height: 6px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 999px;
    overflow: hidden;
    position: relative;
}
.ia-usage-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #00e5ff 0%, #10b981 100%);
    box-shadow: 0 0 8px rgba(0, 229, 255, 0.4);
    transition: width 0.4s cubic-bezier(0.16, 1, 0.3, 1), background 0.3s ease;
}
.ia-usage-fill.aviso {
    background: linear-gradient(90deg, #f59e0b 0%, #fbbf24 100%);
    box-shadow: 0 0 8px rgba(245, 158, 11, 0.45);
}
.ia-usage-fill.limite {
    background: linear-gradient(90deg, #f43f5e 0%, #ef4444 100%);
    box-shadow: 0 0 10px rgba(239, 68, 68, 0.65);
}

/* ── Tarjetas de Preguntas Frecuentes ─────────────────────────────── */
.ia-faq-label {
    text-align: center;
    font-family: 'Inter', sans-serif;
    font-size: 0.82rem;
    font-weight: 600;
    color: #64748b;
    letter-spacing: 0.3px;
    margin: 0 0 14px 0;
}

.st-key-ia_chips button {
    background: linear-gradient(160deg, #101420 0%, #0c0f18 100%) !important;
    border: 1px solid rgba(0,229,255,0.12) !important;
    border-radius: 14px !important;
    color: #cbd5e1 !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.80rem !important;
    font-weight: 600 !important;
    padding: 16px 14px !important;
    min-height: 72px !important;
    white-space: normal !important;
    line-height: 1.35 !important;
    transition: all 0.25s cubic-bezier(0.25, 0.46, 0.45, 0.94) !important;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4) !important;
    position: relative !important;
    overflow: hidden !important;
}
.st-key-ia_chips button::before {
    content: '' !important;
    position: absolute !important;
    top: 0 !important; left: 20% !important; right: 20% !important;
    height: 1px !important;
    background: linear-gradient(90deg, transparent, rgba(0,229,255,0.4), transparent) !important;
    opacity: 0.5 !important;
    transition: opacity 0.25s ease !important;
}
.st-key-ia_chips button:hover {
    background: linear-gradient(160deg, #141a2a 0%, #0f1420 100%) !important;
    border-color: rgba(0,229,255,0.35) !important;
    color: #ffffff !important;
    box-shadow: 0 0 18px rgba(0,229,255,0.15), 0 6px 20px rgba(0,0,0,0.5) !important;
    transform: translateY(-2px) !important;
}
.st-key-ia_chips button:hover::before {
    opacity: 1 !important;
}

/* ── Segmented Control Centrado ──────────────────────────────────── */
div[data-testid="stSegmentedControl"] {
    display: flex !important;
    justify-content: center !important;
    margin: 0 auto 16px auto !important;
    background: #0d111d !important;
    border: 1px solid rgba(0,229,255,0.18) !important;
    border-radius: 12px !important;
    padding: 3px !important;
    width: fit-content !important;
}
div[data-testid="stSegmentedControl"] button {
    border-radius: 9px !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    padding: 6px 18px !important;
    color: #94a3b8 !important;
    border: none !important;
    transition: all 0.2s ease !important;
}
div[data-testid="stSegmentedControl"] button[aria-selected="true"] {
    background: rgba(0, 229, 255, 0.16) !important;
    color: #00e5ff !important;
    box-shadow: 0 0 12px rgba(0, 229, 255, 0.25) !important;
    border: 1px solid rgba(0, 229, 255, 0.35) !important;
}

/* ── Mensajes del Chat ────────────────────────────────────────────── */
/* ── Modal Pop-up: Centrado Total y Oscurecimiento de Pantalla Completa ── */
div[data-testid="stDialog"],
div[data-baseweb="modal"] {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    right: 0 !important;
    bottom: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    background-color: rgba(3, 6, 14, 0.84) !important;
    backdrop-filter: blur(8px) !important;
    -webkit-backdrop-filter: blur(8px) !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    z-index: 999999 !important;
    margin: 0 !important;
    padding: 0 !important;
}

div[data-testid="stDialog"] > div,
div[data-testid="stDialog"] div[role="dialog"],
div[data-baseweb="modal"] > div:last-child {
    background: #080b13 !important;
    border: 1px solid rgba(0, 229, 255, 0.25) !important;
    border-radius: 18px !important;
    box-shadow: 0 25px 85px rgba(0, 0, 0, 0.96), 0 0 55px rgba(0, 229, 255, 0.18) !important;
    padding: 24px 30px !important;
    max-width: 1200px !important;
    width: 88vw !important;
    max-height: 90vh !important;
    overflow-y: auto !important;
    box-sizing: border-box !important;
    margin: auto !important;
    align-self: center !important;
}

div[data-testid="stDialog"] h2 {
    color: #ffffff !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 700 !important;
    font-size: 1.15rem !important;
    letter-spacing: -0.3px !important;
    margin-bottom: 16px !important;
}

/* Ocultar la X superior derecha para dejar un único botón claro de cerrar abajo */
div[data-testid="stDialog"] button[aria-label="Close"],
div[data-testid="stDialog"] button[data-testid="stBaseButton-header"] {
    display: none !important;
}

/* ── Cabecera Exclusiva del Modal de Inspección de Radar ───────────── */
.ia-radar-modal-header {
    background: linear-gradient(135deg, rgba(0, 229, 255, 0.08) 0%, rgba(15, 23, 42, 0.7) 100%);
    border: 1px solid rgba(0, 229, 255, 0.22);
    border-radius: 12px;
    padding: 12px 18px;
    margin-bottom: 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
}
.ia-radar-modal-title-box {
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.ia-radar-modal-subtag {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    font-weight: 700;
    color: #38bdf8;
    letter-spacing: 1.2px;
    text-transform: uppercase;
}
.ia-radar-modal-title {
    font-family: 'Inter', sans-serif;
    color: #ffffff;
    font-size: 1.22rem;
    font-weight: 700;
    letter-spacing: -0.3px;
}
.ia-radar-modal-ticker {
    color: #64748b;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.95rem;
    font-weight: 500;
    margin-left: 6px;
}
.ia-radar-modal-stats {
    display: flex;
    align-items: center;
    gap: 12px;
}
.ia-radar-stat-box {
    background: rgba(11, 15, 25, 0.85);
    border: 1px solid rgba(0, 229, 255, 0.15);
    border-radius: 8px;
    padding: 6px 14px;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
}
.ia-radar-stat-lbl {
    font-family: 'Inter', sans-serif;
    font-size: 0.58rem;
    font-weight: 700;
    color: #64748b;
    letter-spacing: 0.8px;
    text-transform: uppercase;
}
.ia-radar-stat-val {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.05rem;
    font-weight: 800;
    color: #f1f5f9;
}

/* ── Tarjeta de Diagnóstico Institucional (Panel derecho del modal) ── */
.ia-diag-card {
    background: linear-gradient(165deg, #0e121e 0%, #080b13 100%);
    border: 1px solid rgba(0, 229, 255, 0.18);
    border-radius: 14px;
    padding: 18px 20px;
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.5);
    display: flex;
    flex-direction: column;
    gap: 14px;
}
.ia-diag-card.alcista {
    border-top: 3px solid #10b981;
}
.ia-diag-card.bajista {
    border-top: 3px solid #ef4444;
}
.ia-diag-card.lateral {
    border-top: 3px solid #f59e0b;
}

.ia-diag-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.ia-diag-title {
    font-family: 'Inter', sans-serif;
    font-size: 1.05rem;
    font-weight: 700;
    color: #ffffff;
    letter-spacing: -0.2px;
}
.ia-diag-badge {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    font-weight: 800;
    letter-spacing: 0.6px;
    padding: 4px 10px;
    border-radius: 6px;
}
.ia-diag-badge.alcista {
    background: rgba(16, 185, 129, 0.15);
    color: #34d399;
    border: 1px solid rgba(16, 185, 129, 0.35);
    box-shadow: 0 0 10px rgba(16, 185, 129, 0.2);
}
.ia-diag-badge.bajista {
    background: rgba(239, 68, 68, 0.15);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.35);
    box-shadow: 0 0 10px rgba(239, 68, 68, 0.2);
}
.ia-diag-badge.lateral {
    background: rgba(245, 158, 11, 0.15);
    color: #fbbf24;
    border: 1px solid rgba(245, 158, 11, 0.35);
    box-shadow: 0 0 10px rgba(245, 158, 11, 0.2);
}

.ia-diag-metrics-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
}
.ia-diag-mini-kpi {
    background: rgba(11, 15, 25, 0.9);
    border: 1px solid rgba(0, 229, 255, 0.10);
    border-radius: 8px;
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
}
.ia-diag-mini-kpi .mini-lbl {
    font-family: 'Inter', sans-serif;
    font-size: 0.58rem;
    font-weight: 700;
    color: #64748b;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    margin-bottom: 2px;
}
.ia-diag-mini-kpi .mini-val {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.98rem;
    font-weight: 800;
}
.ia-diag-mini-kpi .mini-val.cyan { color: #00e5ff; }
.ia-diag-mini-kpi .mini-val.pos { color: #34d399; }
.ia-diag-mini-kpi .mini-val.neg { color: #f87171; }
.ia-diag-mini-kpi .mini-val.neutral { color: #94a3b8; }

.ia-diag-desc-box {
    background: rgba(0, 229, 255, 0.04);
    border-left: 3px solid rgba(0, 229, 255, 0.4);
    border-radius: 8px;
    padding: 10px 12px;
    line-height: 1.45;
}
.ia-diag-desc-title {
    font-family: 'Inter', sans-serif;
    font-size: 0.74rem;
    font-weight: 700;
    color: #38bdf8;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 4px;
}
.ia-diag-desc-text {
    font-family: 'Inter', sans-serif;
    font-size: 0.80rem;
    color: #94a3b8;
    line-height: 1.45;
}

.ia-diag-conclusion {
    font-family: 'Inter', sans-serif;
    font-size: 0.82rem;
    color: #cbd5e1;
    line-height: 1.5;
    background: rgba(255, 255, 255, 0.02);
    border-radius: 8px;
    padding: 10px 12px;
    border: 1px solid rgba(255, 255, 255, 0.05);
}

.ia-diag-factors-wrap {
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.ia-diag-factors-title {
    font-family: 'Inter', sans-serif;
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    margin-bottom: 2px;
}
.ia-diag-factors-title.favor { color: #34d399; }
.ia-diag-factors-title.contra { color: #f87171; }

.ia-diag-list {
    margin: 0;
    padding-left: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 5px;
}
.ia-diag-list li {
    font-family: 'Inter', sans-serif;
    font-size: 0.80rem;
    color: #cbd5e1;
    line-height: 1.35;
    padding: 5px 8px 5px 12px;
    border-radius: 6px;
    position: relative;
}
.ia-diag-list.favor li {
    background: rgba(16, 185, 129, 0.06);
    border-left: 2px solid #10b981;
}
.ia-diag-list.contra li {
    background: rgba(239, 68, 68, 0.06);
    border-left: 2px solid #ef4444;
}

.ia-modal-user-card {
    background: linear-gradient(135deg, rgba(0, 229, 255, 0.08), rgba(14, 165, 233, 0.04));
    border: 1px solid rgba(0, 229, 255, 0.22);
    border-radius: 10px;
    padding: 10px 14px;
    margin-bottom: 16px;
}
.ia-modal-user-tag {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.62rem;
    font-weight: 700;
    color: #38bdf8;
    letter-spacing: 1px;
    text-transform: uppercase;
    margin-bottom: 4px;
}
.ia-modal-user-query {
    font-family: 'Inter', sans-serif;
    color: #f1f5f9;
    font-size: 0.94rem;
    font-weight: 500;
}
.ia-modal-bot-card {
    background: linear-gradient(160deg, #101422 0%, #0a0d16 100%);
    border: 1px solid rgba(0, 229, 255, 0.16);
    border-left: 3px solid #00e5ff;
    border-radius: 10px;
    padding: 18px 20px;
    margin-bottom: 14px;
}
.ia-modal-bot-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    padding-bottom: 8px;
}
.ia-modal-bot-title {
    font-family: 'Inter', sans-serif;
    font-weight: 700;
    color: #ffffff;
    font-size: 1.02rem;
}
.ia-modal-badge {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.64rem;
    font-weight: 700;
    color: #00e5ff;
    background: rgba(0, 229, 255, 0.12);
    border: 1px solid rgba(0, 229, 255, 0.30);
    border-radius: 6px;
    padding: 3px 8px;
    letter-spacing: 0.5px;
}
.ia-modal-badge.aviso {
    color: #fbbf24 !important;
    background: rgba(251, 191, 36, 0.12) !important;
    border-color: rgba(251, 191, 36, 0.35) !important;
}
.ia-modal-bot-text {
    font-family: 'Inter', sans-serif;
    color: #cbd5e1;
    font-size: 0.90rem;
    line-height: 1.65;
}

/* ── Expander Estilizado ──────────────────────────────────────────── */
details[data-testid="stExpander"] {
    background: #0a0d16 !important;
    border: 1px solid rgba(0, 229, 255, 0.15) !important;
    border-radius: 12px !important;
    margin-top: 16px !important;
}
details[data-testid="stExpander"] summary {
    font-family: 'Inter', sans-serif !important;
    font-weight: 600 !important;
    color: #e2e8f0 !important;
}
details[data-testid="stExpander"] summary:hover {
    color: #00e5ff !important;
}

/* ── Shell para Estados Vacíos ────────────────────────────────────── */
.ia-empty-shell {
    background: linear-gradient(145deg, #0d111a 0%, #080a10 100%);
    border: 1px dashed rgba(0,229,255,0.20);
    border-radius: 16px;
    padding: 36px 28px;
    text-align: center;
    margin: 16px 0;
    position: relative;
    overflow: hidden;
}
.ia-empty-shell::before {
    content: '';
    position: absolute;
    top: 0; left: 20%; right: 20%;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(0,229,255,0.4), transparent);
}
.ia-empty-icon  { font-size: 2.2rem; margin-bottom: 10px; filter: drop-shadow(0 0 12px rgba(0,229,255,0.3)); }
.ia-empty-title { font-family:'Inter',sans-serif; font-size:1.05rem; font-weight:700; color:#e2e8f0; margin-bottom:6px; }
.ia-empty-desc  { font-family:'Inter',sans-serif; font-size:0.84rem; color:#64748b; max-width:580px; margin:0 auto; line-height:1.5; }

/* ── Selectores Desplegables de Horizonte y Filtro de Señales en Radar e Historial (Estilo Ranking centrado) ── */
div:has(> div > div > .st-key-ia_radar_hz_select),
div:has(> div > div > .st-key-ia_radar_conv_select),
div:has(> div > div > .st-key-ia_hist_hz_select),
.st-key-ia_radar_hz_select,
.st-key-ia_radar_conv_select,
.st-key-ia_hist_hz_select {
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    margin: 4px auto 12px auto !important;
    width: 100% !important;
}
.st-key-ia_radar_hz_select div[data-testid="stSelectbox"],
.st-key-ia_radar_conv_select div[data-testid="stSelectbox"],
.st-key-ia_hist_hz_select div[data-testid="stSelectbox"] {
    width: 100% !important;
}
.st-key-ia_radar_hz_select label[data-testid="stWidgetLabel"],
.st-key-ia_radar_conv_select label[data-testid="stWidgetLabel"],
.st-key-ia_hist_hz_select label[data-testid="stWidgetLabel"] {
    text-align: center !important;
    justify-content: center !important;
    display: flex !important;
    width: 100% !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.70rem !important;
    font-weight: 700 !important;
    color: #94a3b8 !important;
    letter-spacing: 1.2px !important;
    text-transform: uppercase !important;
    margin-bottom: 6px !important;
}
.st-key-ia_radar_hz_select label[data-testid="stWidgetLabel"] p,
.st-key-ia_radar_conv_select label[data-testid="stWidgetLabel"] p,
.st-key-ia_hist_hz_select label[data-testid="stWidgetLabel"] p {
    text-align: center !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.70rem !important;
    font-weight: 700 !important;
    color: #94a3b8 !important;
    letter-spacing: 1.2px !important;
    text-transform: uppercase !important;
    margin: 0 !important;
}
.st-key-ia_radar_hz_select div[data-testid="stSelectbox"] > div > div,
.st-key-ia_radar_conv_select div[data-testid="stSelectbox"] > div > div,
.st-key-ia_hist_hz_select div[data-testid="stSelectbox"] > div > div {
    background-color: #0d1017 !important;
    border: 1px solid rgba(0, 229, 255, 0.18) !important;
    border-radius: 10px !important;
    color: #c4cad4 !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.86rem !important;
    height: 42px !important;
    min-height: 42px !important;
    max-height: 42px !important;
    display: flex !important;
    align-items: center !important;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4) !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}
.st-key-ia_radar_hz_select div[data-testid="stSelectbox"] > div > div:hover,
.st-key-ia_radar_conv_select div[data-testid="stSelectbox"] > div > div:hover,
.st-key-ia_hist_hz_select div[data-testid="stSelectbox"] > div > div:hover {
    border-color: rgba(0, 229, 255, 0.38) !important;
    box-shadow: 0 0 12px rgba(0, 229, 255, 0.1) !important;
}
.st-key-ia_radar_hz_select div[data-testid="stSelectbox"] > div > div:focus-within,
.st-key-ia_radar_conv_select div[data-testid="stSelectbox"] > div > div:focus-within,
.st-key-ia_hist_hz_select div[data-testid="stSelectbox"] > div > div:focus-within {
    border-color: rgba(0, 229, 255, 0.55) !important;
    box-shadow: 0 0 16px rgba(0, 229, 255, 0.2) !important;
}

/* ── Fila de Métricas KPI del Historial Predictivo ────────────────── */
.ia-hist-kpi-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin: 14px 0 18px 0;
}
.ia-hist-kpi-row.ia-hist-kpi-row-5 {
    grid-template-columns: repeat(5, 1fr);
}
@media (max-width: 1100px) {
    .ia-hist-kpi-row.ia-hist-kpi-row-5 {
        grid-template-columns: repeat(3, 1fr);
    }
}
@media (max-width: 900px) {
    .ia-hist-kpi-row,
    .ia-hist-kpi-row.ia-hist-kpi-row-5 {
        grid-template-columns: repeat(2, 1fr);
    }
}
.ia-hist-kpi-card {
    background: linear-gradient(145deg, #0e121a 0%, #080a0f 100%);
    border: 1px solid rgba(0, 229, 255, 0.10);
    border-radius: 12px;
    padding: 14px 18px 12px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
    position: relative;
    overflow: hidden;
    transition: border-color 0.25s ease, box-shadow 0.25s ease;
}
.ia-hist-kpi-card::before {
    content: '';
    position: absolute;
    top: 0; left: 16%; right: 16%;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(0, 229, 255, 0.4), transparent);
    opacity: 0.6;
}
.ia-hist-kpi-card:hover {
    border-color: rgba(0, 229, 255, 0.35);
    box-shadow: 0 0 18px rgba(0, 229, 255, 0.12), 0 6px 24px rgba(0, 0, 0, 0.5);
}
.ia-hist-kpi-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
}
.ia-hist-kpi-title {
    font-family: 'Inter', sans-serif;
    font-size: 0.64rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: #64748b;
}
.ia-hist-kpi-icon {
    font-size: 0.95rem;
}
.ia-hist-kpi-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.55rem;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: #f1f5f9;
    line-height: 1.2;
    margin-bottom: 4px;
}
.ia-hist-kpi-value.cyan { color: #00e5ff; }
.ia-hist-kpi-value.pos { color: #34d399; }
.ia-hist-kpi-value.amber { color: #fbbf24; }
.ia-hist-kpi-value.neg { color: #f87171; }
.ia-hist-kpi-caption {
    font-family: 'Inter', sans-serif;
    font-size: 0.72rem;
    color: #64748b;
    font-weight: 500;
}

/* ── Pestañas en Historial Predictivo ────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {
    gap: 10px !important;
    background: rgba(13, 16, 23, 0.75) !important;
    border: 1px solid rgba(255, 255, 255, 0.08) !important;
    border-radius: 12px !important;
    padding: 6px !important;
    margin: 8px 0 16px 0 !important;
    justify-content: center !important;
}
.stTabs [data-baseweb="tab"] {
    color: #64748b !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.88rem !important;
    font-weight: 600 !important;
    border-radius: 9px !important;
    transition: all 0.25s ease !important;
    padding: 9px 22px !important;
    border: 1px solid transparent !important;
    background: transparent !important;
}
.stTabs [data-baseweb="tab"]:hover {
    color: #cbd5e1 !important;
    background: rgba(0, 229, 255, 0.06) !important;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
    background: linear-gradient(135deg, rgba(0, 229, 255, 0.16) 0%, rgba(16, 185, 129, 0.08) 100%) !important;
    color: #00e5ff !important;
    border: 1px solid rgba(0, 229, 255, 0.35) !important;
    box-shadow: 0 2px 10px rgba(0, 229, 255, 0.15) !important;
}
.stTabs [data-baseweb="tab-highlight"] {
    display: none !important;
}

/* ── Avisos y tiras de estado de Historial ───────────────────────── */
.ia-hist-status-strip {
    background: linear-gradient(135deg, rgba(0, 229, 255, 0.06) 0%, rgba(13, 16, 23, 0.75) 100%);
    border: 1px solid rgba(0, 229, 255, 0.18);
    border-radius: 10px;
    padding: 10px 16px;
    font-family: 'Inter', sans-serif;
    font-size: 0.82rem;
    color: #94a3b8;
    margin-bottom: 12px;
}
.ia-hist-empty-notice {
    text-align: center;
    color: #cbd5e1;
    font-size: 0.88rem;
    padding: 32px 20px;
    background: linear-gradient(180deg, rgba(13, 18, 30, 0.70) 0%, rgba(8, 11, 20, 0.85) 100%);
    border: 1px dashed rgba(0, 229, 255, 0.22);
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
    border-radius: 14px;
    margin: 14px 0 16px 0;
    font-family: 'Inter', sans-serif;
    line-height: 1.6;
}
.ia-hist-empty-icon {
    font-size: 2rem;
    margin-bottom: 8px;
    display: inline-block;
}
.ia-hist-empty-title {
    font-size: 0.95rem;
    font-weight: 700;
    color: #f1f5f9;
    margin-bottom: 4px;
}
.ia-hist-empty-desc {
    font-size: 0.80rem;
    color: #64748b;
    max-width: 540px;
    margin: 0 auto;
}

/* ── Botones de Reset y Gestión del Historial ─────────────────────── */
.st-key-btn_trigger_limpiar_hist button,
.st-key-btn_trigger_limpiar_hist_empty button {
    background: rgba(239, 68, 68, 0.07) !important;
    border: 1px solid rgba(239, 68, 68, 0.25) !important;
    color: #f87171 !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.76rem !important;
    font-weight: 600 !important;
    border-radius: 8px !important;
    transition: all 0.2s ease !important;
    padding: 6px 14px !important;
}
.st-key-btn_trigger_limpiar_hist button:hover,
.st-key-btn_trigger_limpiar_hist_empty button:hover {
    background: rgba(239, 68, 68, 0.18) !important;
    border-color: rgba(239, 68, 68, 0.50) !important;
    color: #ffffff !important;
    box-shadow: 0 2px 10px rgba(239, 68, 68, 0.25) !important;
}

/* ── Disclaimer Institucional Pie de Página ──────────────────────── */
.ia-disclaimer {
    margin: 36px auto 16px auto;
    padding: 12px 22px;
    background: rgba(13, 18, 30, 0.45);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
    text-align: center;
    font-family: 'Inter', sans-serif;
    font-size: 0.74rem;
    color: #64748b;
    max-width: 820px;
    line-height: 1.55;
    letter-spacing: 0.15px;
}
.ia-disclaimer b {
    color: #94a3b8;
    font-weight: 600;
}

/* ── Tablas Premium Estilo StockLab (Área IA) — Cobertura 100% Sin Desfases ── */
.ia-table-wrapper {
    background: linear-gradient(180deg, #0b0f19 0%, #070910 100%) !important;
    border: 1px solid rgba(0, 229, 255, 0.18) !important;
    border-radius: 14px !important;
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
    overflow: hidden !important;
    margin: 14px 0 20px 0 !important;
    width: 100% !important;
    box-sizing: border-box !important;
}

.ia-table-scroll {
    overflow-x: auto !important;
    overflow-y: auto !important;
    max-height: 480px !important;
    width: 100% !important;
    box-sizing: border-box !important;
}

.ia-table-scroll::-webkit-scrollbar {
    width: 6px;
    height: 6px;
}
.ia-table-scroll::-webkit-scrollbar-track {
    background: rgba(11, 15, 25, 0.8);
}
.ia-table-scroll::-webkit-scrollbar-thumb {
    background: rgba(0, 229, 255, 0.25);
    border-radius: 4px;
}
.ia-table-scroll::-webkit-scrollbar-thumb:hover {
    background: rgba(0, 229, 255, 0.5);
}

table.ia-custom-table,
section[data-testid="stMain"] table.ia-custom-table {
    width: 100% !important;
    min-width: 100% !important;
    table-layout: fixed !important;
    border-collapse: separate !important;
    border-spacing: 0 !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.86rem !important;
    color: #cbd5e1 !important;
    margin: 0 !important;
    padding: 0 !important;
    box-sizing: border-box !important;
}

table.ia-custom-table thead tr {
    background: #0d1220 !important;
    position: sticky;
    top: 0;
    z-index: 5;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
    width: 100% !important;
}

table.ia-custom-table thead th {
    padding: 13px 16px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.1px;
    color: #64748b;
    border-bottom: 1px solid rgba(0, 229, 255, 0.25);
    white-space: nowrap;
    user-select: none;
    background: #0d1220 !important;
    box-sizing: border-box !important;
}

table.ia-custom-table thead th.text-left { text-align: left; }
table.ia-custom-table thead th.text-center { text-align: center; }
table.ia-custom-table thead th.text-right { text-align: right; }

table.ia-custom-table tbody tr {
    border-bottom: 1px solid rgba(255, 255, 255, 0.035);
    transition: background 0.15s ease;
    width: 100% !important;
}

table.ia-custom-table tbody tr:hover {
    background: rgba(0, 229, 255, 0.045) !important;
}

table.ia-custom-table tbody td {
    padding: 11px 16px;
    vertical-align: middle;
    white-space: nowrap;
    box-sizing: border-box !important;
}

table.ia-custom-table tbody td.text-left { text-align: left; }
table.ia-custom-table tbody td.text-center { text-align: center; }
table.ia-custom-table tbody td.text-right { text-align: right; }

/* Chips / Badges en Celdas */
.ia-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    padding: 3px 9px;
    border-radius: 6px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.4px;
    line-height: 1;
}

.ia-pill.alcista, .ia-pill.positivo, .ia-pill.acierto {
    background: rgba(16, 185, 129, 0.12);
    color: #34d399;
    border: 1px solid rgba(16, 185, 129, 0.3);
}

.ia-pill.bajista, .ia-pill.negativo, .ia-pill.fallo {
    background: rgba(239, 68, 68, 0.12);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.3);
}

.ia-pill.lateral, .ia-pill.neutro {
    background: rgba(245, 158, 11, 0.12);
    color: #fbbf24;
    border: 1px solid rgba(245, 158, 11, 0.3);
}

.ia-pill.dividendo {
    background: rgba(168, 85, 247, 0.12);
    color: #c084fc;
    border: 1px solid rgba(168, 85, 247, 0.35);
}

.ia-pill.resultados {
    background: rgba(56, 189, 248, 0.12);
    color: #38bdf8;
    border: 1px solid rgba(56, 189, 248, 0.35);
}

.ia-pill.invalidada {
    background: rgba(251, 146, 60, 0.12);
    color: #fb923c;
    border: 1px solid rgba(251, 146, 60, 0.35);
}

/* Formato numérico StockLab */
.ia-mono-val {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 600;
}
.ia-val-pos {
    color: #34d399;
}
.ia-val-neg {
    color: #f87171;
}
.ia-val-neutral {
    color: #94a3b8;
}
.ia-company-name {
    font-weight: 600;
    color: #f1f5f9;
}

/* ── Variante Compacta de Tabla (Radar Predictivo) ────────────────── */
.ia-table-compact table.ia-custom-table thead th {
    padding: 10px 10px;
    letter-spacing: 0.8px;
}
.ia-table-compact table.ia-custom-table tbody td {
    padding: 8px 10px;
}

/* ── Botón de Inspección (ojo) — columna Gráfico ──────────────────── */
.ia-eye-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 27px;
    height: 27px;
    border-radius: 8px;
    background: rgba(0, 229, 255, 0.07);
    border: 1px solid rgba(0, 229, 255, 0.22);
    font-size: 0.84rem;
    line-height: 1;
    text-decoration: none;
    cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
}
.ia-eye-link:hover {
    background: rgba(0, 229, 255, 0.18);
    border-color: rgba(0, 229, 255, 0.55);
    transform: scale(1.12);
}
</style>
        """,
        unsafe_allow_html=True,
    )


# ═══════════════════════════════════════════════════════════════════
# UTILIDADES
# ═══════════════════════════════════════════════════════════════════

def _md_a_html(texto: str) -> str:
    """Markdown mínimo del agente → HTML seguro para la burbuja del chat."""
    import html as _html
    import re as _re
    t = _html.escape(texto)
    t = _re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noopener noreferrer" '
        r'style="color:#00e5ff; text-decoration:underline;">\1</a>',
        t,
    )
    t = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = _re.sub(r"^[-•] (.+)$", r"• \1", t, flags=_re.M)
    return t.replace("\n", "<br>")


def _render_tabla_html(df: pd.DataFrame, max_height: int = 480,
                       center_all: bool = False, compact: bool = False,
                       col_widths: dict = None) -> str:
    """Convierte cualquier DataFrame de pandas a una tabla HTML simétrica y estética estilo StockLab.

    Parámetros opcionales:
      center_all — centra el texto de TODAS las columnas (cabeceras y celdas).
      compact    — reduce el padding de las celdas para aprovechar el espacio.
      col_widths — dict {col_name: "width_css"} para definir anchos exactos al 100%.
    """
    if df is None or df.empty:
        return ""

    import html as _html
    cols = list(df.columns)

    def _col_align(col_name: str) -> str:
        name_l = col_name.lower()
        if any(k in name_l for k in ["empresa", "nombre", "evento", "descripción", "descripcion"]):
            return "text-left"
        if any(k in name_l for k in ["fecha", "tipo", "señal", "senal", "predicción", "prediccion", "resultado", "horizonte", "score", "#", "pos", "ticker"]):
            return "text-center"
        return "text-right"

    aligns = (["text-center"] * len(cols)) if center_all else [_col_align(c) for c in cols]

    def _col_w_style(col_name: str) -> str:
        if col_widths and col_name in col_widths:
            return f"width: {col_widths[col_name]}; max-width: {col_widths[col_name]};"
        return ""

    def _format_cell(val, col_name: str) -> str:
        if pd.isna(val):
            return '<span class="ia-val-neutral">—</span>'

        s = str(val).strip()
        s_lower = s.lower()
        col_l = col_name.lower()

        # Columna de acciones con HTML propio (botón de inspección)
        if col_l in ("gráfico", "grafico"):
            return s

        # Badges para predicciones y señales
        if "alcista" in s_lower:
            return '<span class="ia-pill alcista">🟢 ALCISTA</span>'
        if "bajista" in s_lower:
            return '<span class="ia-pill bajista">🔴 BAJISTA</span>'
        if "lateral" in s_lower:
            return '<span class="ia-pill lateral">🟡 LATERAL</span>'
        if "invalidada" in s_lower:
            return '<span class="ia-pill invalidada">⚡ Invalidada</span>'
        if "acierto" in s_lower:
            return '<span class="ia-pill acierto">✅ Acierto</span>'
        if "fallo" in s_lower:
            return '<span class="ia-pill fallo">❌ Fallo</span>'
        if "dividendo" in s_lower:
            return '<span class="ia-pill dividendo">💰 Dividendo</span>'
        if "resultado" in s_lower or "earnings" in s_lower:
            return '<span class="ia-pill resultados">📊 Resultados</span>'

        # Indicador RSI con semáforo técnico
        if col_l == "rsi":
            try:
                v_num = float(s)
                if v_num <= 35:
                    return f'<span class="ia-mono-val ia-val-pos" title="Sobreventa">{s}</span>'
                elif v_num >= 65:
                    return f'<span class="ia-mono-val ia-val-neg" title="Sobrecompra">{s}</span>'
                return f'<span class="ia-mono-val ia-val-neutral">{s}</span>'
            except Exception:
                pass

        # Variación / tendencia con icono o signo
        if "📈" in s or (s.startswith("+") and "%" in s):
            return f'<span class="ia-mono-val ia-val-pos">{_html.escape(s)}</span>'
        if "📉" in s or (s.startswith("-") and "%" in s):
            return f'<span class="ia-mono-val ia-val-neg">{_html.escape(s)}</span>'

        # Valores monetarios o numéricos clave
        if col_l in ["precio", "price"]:
            return f'<span class="ia-mono-val" style="color:#f1f5f9; font-weight:600;">{_html.escape(s)}</span>'
        if col_l == "score":
            try:
                sv = float(s)
                sc = "#34d399" if sv >= 70 else ("#fbbf24" if sv >= 40 else "#f87171")
                return f'<span class="ia-mono-val" style="color:{sc}; font-weight:700;">{_html.escape(s)}</span>'
            except Exception:
                return f'<span class="ia-mono-val" style="color:#64748b;">{_html.escape(s)}</span>'
        if col_l in ["confianza", "conf"]:
            return f'<span class="ia-mono-val" style="color:#00e5ff; font-weight:600;">{_html.escape(s)}</span>'
        if col_l in ["fecha", "date"]:
            return f'<span class="ia-mono-val" style="color:#94a3b8;">{_html.escape(s)}</span>'
        if col_l in ["empresa", "nombre"]:
            return f'<span class="ia-company-name">{_html.escape(s)}</span>'

        if "€" in s or "%" in s or s.replace(".", "", 1).replace("-", "", 1).isdigit():
            return f'<span class="ia-mono-val">{_html.escape(s)}</span>'

        return _html.escape(s)

    colgroup_html = ""
    if col_widths:
        cols_tags = "".join(
            f'<col style="{_col_w_style(str(c))}">' for c in cols
        )
        colgroup_html = f"<colgroup>{cols_tags}</colgroup>"

    th_cells = "".join(
        f'<th class="{aligns[i]}" style="{_col_w_style(str(col))}">{_html.escape(str(col))}</th>'
        for i, col in enumerate(cols)
    )

    tr_rows = []
    for _, row in df.iterrows():
        td_cells = "".join(
            f'<td class="{aligns[i]}" style="{_col_w_style(str(col))}">{_format_cell(row[col], str(col))}</td>'
            for i, col in enumerate(cols)
        )
        tr_rows.append(f'<tr>{td_cells}</tr>')

    rows_html = "".join(tr_rows)

    wrapper_cls = "ia-table-wrapper ia-table-compact" if compact else "ia-table-wrapper"
    return f"""<div class="{wrapper_cls}"><div class="ia-table-scroll" style="max-height: {max_height}px;"><table class="ia-custom-table">{colgroup_html}<thead><tr>{th_cells}</tr></thead><tbody>{rows_html}</tbody></table></div></div>"""


def _render_chat_item(item: dict) -> None:
    """Renderiza un par pregunta/respuesta del hilo de chat como burbujas en línea."""
    import html as _html
    q = item.get("q", "")
    r = item.get("r", {})
    texto = r.get("texto", "")
    m_tag = r.get("modelo") or item.get("m", "IA")
    
    # Sincronización estricta: si el texto tiene aviso de cuota/saturación o proviene del motor local
    es_local = "Motor Local" in m_tag or "> ⚠️" in texto or "> ⏱️" in texto
    if es_local:
        badge_cls = "aviso"
        if "Motor Local" not in m_tag:
            m_tag = f"Motor Local ({m_tag})"
    else:
        badge_cls = ""

    st.markdown(
        f"""
        <div class="ia-modal-user-card">
            <div class="ia-modal-user-tag">🧑 TÚ</div>
            <div class="ia-modal-user-query">{_html.escape(q)}</div>
        </div>
        <div class="ia-modal-bot-card">
            <div class="ia-modal-bot-head">
                <div class="ia-modal-bot-title">🤖 {r.get('titulo', 'Respuesta')}</div>
                <span class="ia-modal-badge {badge_cls}">{m_tag}</span>
            </div>
            <div class="ia-modal-bot-text">{_md_a_html(r.get('texto', ''))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if r.get("df") is not None and not r["df"].empty:
        st.html(_render_tabla_html(r["df"], max_height=320))


def _conclusion_horizonte(nombre: str, ind: dict, s_hz: dict, hz_label: str) -> str:
    """Texto descriptivo del diagnóstico para un horizonte concreto (HTML seguro)."""
    icono = _ICONOS_SENAL.get(s_hz["senal"], "⚪")
    vol_d_pct = float(ind["vol_d"]) * 100.0
    vol_anual = float(ind["vol_d"]) * (252 ** 0.5) * 100.0
    return (
        f"{icono} <b>{nombre}</b> presenta señal <b>{s_hz['senal']}</b> a {hz_label.lower()} "
        f"({s_hz['pct']:+.2f}% esperado, confianza {s_hz['confianza']:.0f}%). "
        f"Volatilidad diaria ≈ {vol_d_pct:.1f}% (anualizada ≈ {vol_anual:.0f}%). "
        f"Recuerda: es una estimación probabilística, no una garantía."
    )


def _render_inspeccion_activo(sub: pd.DataFrame, ticker: str, nombre: str,
                              analisis: dict, hz_key: str, hz_label: str) -> None:
    """Gráfico de proyección algorítmica + diagnóstico para un activo y horizonte dados.

    Replica el panel del inspector clásico del radar pero parametrizado por
    horizonte (1D/3D/1S/1M): el cono y el diagnóstico usan la señal de ese
    horizonte en formato ampliado de alta fidelidad.
    """
    ind = analisis["indicadores"]
    senales = analisis["senales"]
    s_hz = senales[hz_key]
    dias = signal_engine.HORIZONTES.get(hz_key, 5)

    col_chart, col_info = st.columns([1.65, 1.25], gap="large")
    with col_chart:
        closes = sub["Close"]
        sma20 = closes.rolling(20).mean()
        sma50 = closes.rolling(50).mean()
        cono = signal_engine.proyectar_cono(
            ind["close"], s_hz["score"], ind["vol_d"],
            ind["ultima_fecha"], dias=dias,
        )
        fechas_hist = [pd.Timestamp(d).strftime("%d %b %Y") for d in sub.index]
        fechas_fut = ([pd.Timestamp(d).strftime("%d %b %Y") for d in cono["fechas"]]
                      if cono else [])
        labels = fechas_hist + fechas_fut
        n_hist, n_fut = len(fechas_hist), len(fechas_fut)

        def _pad_hist(serie: pd.Series) -> list:
            return [None if pd.isna(v) else round(float(v), 4)
                    for v in serie] + [None] * n_fut

        ultimo = float(closes.iloc[-1])
        extra_series = [
            {"label": "SMA20", "data": _pad_hist(sma20),
             "color": "#f59e0b", "dash": [4, 4], "width": 1.0},
            {"label": "SMA50", "data": _pad_hist(sma50),
             "color": "#8b5cf6", "dash": [4, 4], "width": 1.0},
        ]
        if cono:
            def _pad_cone(vals) -> list:
                return ([None] * (n_hist - 1) + [ultimo]
                        + [round(float(v), 4) for v in vals])

            extra_series += [
                {"label": "Pesimista", "data": _pad_cone(cono["pesimista"]),
                 "color": "#ef4444", "dash": [6, 4], "width": 1.2},
                {"label": "Optimista", "data": _pad_cone(cono["optimista"]),
                 "color": "#10b981", "dash": [6, 4], "width": 1.2,
                 "fill_to": "-1", "fill_color": "rgba(0, 229, 255, 0.07)"},
                {"label": "Escenario base", "data": _pad_cone(cono["base"]),
                 "color": "#00e5ff", "dash": [8, 4], "width": 1.6},
            ]

        color_linea = "#10B981" if ultimo >= float(closes.iloc[0]) else "#EF4444"

        render_stock_chart(
            labels=labels,
            prices=_pad_hist(closes),
            last_close=ultimo,
            line_color=color_linea,
            currency="EUR",
            ticker=ticker,
            title=f"{nombre} — Proyección Algorítmica ({hz_label})",
            show_reference_line=True,
            show_measure_tool=False,
            show_zoom=True,
            height=460,
            y_padding_ratio=0.06,
            extra_series=extra_series,
        )

    with col_info:
        a_favor, en_contra = signal_engine.explicar_senal(ind)
        definicion = _DEFINICIONES_SENAL.get(s_hz["senal"], "")
        conclusion_html = _conclusion_horizonte(nombre, ind, s_hz, hz_label)
        senal_cls = s_hz["senal"].lower()
        icono_senal = _ICONOS_SENAL.get(s_hz["senal"], "⚪")
        vol_pct = float(ind["vol_d"]) * 100.0

        items_favor = "".join(f"<li>✓ {f}</li>" for f in a_favor) if a_favor else "<li>Ningún factor relevante</li>"
        items_contra = "".join(f"<li>✕ {f}</li>" for f in en_contra) if en_contra else "<li>Ningún factor relevante</li>"

        st.html(
            f"""
            <div class="ia-diag-card {senal_cls}">
                <div class="ia-diag-header">
                    <div class="ia-diag-title">Diagnóstico {hz_label}</div>
                    <div class="ia-diag-badge {senal_cls}">{icono_senal} {s_hz['senal']}</div>
                </div>
                <div class="ia-diag-metrics-grid">
                    <div class="ia-diag-mini-kpi">
                        <span class="mini-lbl">Confianza</span>
                        <span class="mini-val cyan">{s_hz['confianza']:.0f}%</span>
                    </div>
                    <div class="ia-diag-mini-kpi">
                        <span class="mini-lbl">Proyección</span>
                        <span class="mini-val {'pos' if s_hz['pct']>=0 else 'neg'}">{s_hz['pct']:+.2f}%</span>
                    </div>
                    <div class="ia-diag-mini-kpi">
                        <span class="mini-lbl">Volatilidad (D)</span>
                        <span class="mini-val neutral">~{vol_pct:.1f}%</span>
                    </div>
                </div>
                <div class="ia-diag-desc-box">
                    <div class="ia-diag-desc-title">📖 Escenario {s_hz['senal'].capitalize()}</div>
                    <div class="ia-diag-desc-text">{definicion}</div>
                </div>
                <div class="ia-diag-conclusion">
                    {conclusion_html}
                </div>
                <div class="ia-diag-factors-wrap">
                    <div class="ia-diag-factors-title favor">Factores a favor ({len(a_favor)})</div>
                    <ul class="ia-diag-list favor">
                        {items_favor}
                    </ul>
                    <div class="ia-diag-factors-title contra">Factores en contra ({len(en_contra)})</div>
                    <ul class="ia-diag-list contra">
                        {items_contra}
                    </ul>
                </div>
            </div>
            """
        )


@st.dialog("Inspección de Activo — Radar Predictivo", width="large")
def _dialog_inspeccion_radar(ticker: str, nombre: str, hz_sel: str,
                             tickers: tuple, tilts: tuple) -> None:
    """Popup de gran formato con la proyección algorítmica y el diagnóstico al horizonte filtrado."""
    hz_key = _HZ_MAP.get(hz_sel, "1S")
    hz_label = _HZ_LABELS.get(hz_sel, hz_sel)

    df_batch = get_price_batch(tuple(tickers))
    sub = signal_engine.obtener_serie_ticker(df_batch, ticker)
    if sub is None or sub.empty:
        st.warning(f"No hay datos suficientes para {nombre} en este momento.")
        return
    ticker2tilt = dict(zip(tickers, tilts))
    tilt_val = ticker2tilt.get(ticker, 0.0)
    analisis = signal_engine.calcular_senales_ticker(sub, tilt_val, ticker=ticker, nombre=nombre)
    if not analisis:
        st.warning(f"No se pudo generar el análisis de {nombre}.")
        return

    # Extraer métricas clave de la sesión para la cabecera
    closes = sub["Close"]
    ultimo_p = float(closes.iloc[-1])
    penultimo_p = float(closes.iloc[-2]) if len(closes) > 1 else ultimo_p
    var_sesion = ((ultimo_p - penultimo_p) / penultimo_p) * 100.0 if penultimo_p > 0 else 0.0
    var_cls = "ia-val-pos" if var_sesion >= 0 else "ia-val-neg"
    var_sign = "+" if var_sesion >= 0 else ""

    st.html(
        f"""
        <div class="ia-radar-modal-header">
            <div class="ia-radar-modal-title-box">
                <div class="ia-radar-modal-subtag">PROYECCIÓN ALGORÍTMICA · HORIZONTE {hz_label.upper()}</div>
                <div class="ia-radar-modal-title">{nombre} <span class="ia-radar-modal-ticker">({ticker})</span></div>
            </div>
            <div class="ia-radar-modal-stats">
                <div class="ia-radar-stat-box">
                    <span class="ia-radar-stat-lbl">ÚLTIMO CIERRE</span>
                    <span class="ia-radar-stat-val">{ultimo_p:.2f} €</span>
                </div>
                <div class="ia-radar-stat-box">
                    <span class="ia-radar-stat-lbl">VAR. SESIÓN</span>
                    <span class="ia-radar-stat-val {var_cls}">{var_sign}{var_sesion:.2f}%</span>
                </div>
            </div>
        </div>
        """
    )

    _render_inspeccion_activo(sub, ticker, nombre, analisis, hz_key, hz_label)

    st.markdown("<div style='margin-top: 14px;'></div>", unsafe_allow_html=True)
    c_space, c_close = st.columns([4.2, 1.2])
    with c_close:
        if st.button("✕ Cerrar", key="ia_eye_modal_close_btn", use_container_width=True):
            st.session_state.pop("ia_eye_item", None)
            st.rerun()


# ═══════════════════════════════════════════════════════════════════
# 🤖 SECCIÓN 1 — AGENTE DE IA
# ═══════════════════════════════════════════════════════════════════

def _seccion_agente(ctx: dict) -> None:
    """Sección del asistente conversacional: prompt bar, chips y hilo de chat con memoria."""

    # ── Título centrado ──────────────────────────────────────────────
    st.markdown(
        """
        <div class="ia-section-header">
            <div class="ia-section-title">Agente de IA</div>
            <div class="ia-section-sub">Asistente conversacional y análisis fundamental-técnico</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Callback para enviar pregunta y limpiar el input ─────────────
    def _enviar_pregunta() -> None:
        texto = st.session_state.get("ia_prompt_input", "").strip()
        if texto:
            st.session_state["ia_pending"] = texto
            st.session_state["ia_prompt_input"] = ""

    # ── Estado del modelo seleccionado ───────────────────────────────
    st.session_state.setdefault("ia_modelo_sel", llm_agent.get_default_modelo())
    if st.session_state["ia_modelo_sel"] not in llm_agent.MODELOS_DISPONIBLES:
        st.session_state["ia_modelo_sel"] = llm_agent.get_default_modelo()

    # ── Prompt bar unificado (Container interactivo con flexbox) ─────
    with st.container(key="ia_prompt_box"):
        c_input, c_send, c_badge = st.columns([7.2, 0.45, 2.35], vertical_alignment="center")
        c_input.text_input(
            "",
            key="ia_prompt_input",
            label_visibility="collapsed",
            placeholder="💬  Pregunta a Gemini o consulta tus empresas…",
            on_change=_enviar_pregunta,
        )
        c_send.button("➤", key="ia_send_btn", on_click=_enviar_pregunta)

        if llm_agent.disponible():
            idx = llm_agent.MODELOS_DISPONIBLES.index(st.session_state["ia_modelo_sel"])
            def _formato_modelo_dropdown(m: str) -> str:
                if llm_agent.es_cuota_agotada(m):
                    return f"{m} (Agotado)"
                return m

            c_badge.selectbox(
                "Modelo",
                llm_agent.MODELOS_DISPONIBLES,
                index=idx,
                key="ia_modelo_sel",
                label_visibility="collapsed",
                format_func=_formato_modelo_dropdown,
            )
        else:
            c_badge.html(
                '<div class="ia-model-badge reglas">Motor Reglas</div>'
            )

    # ── Barra de Uso Diario (0% a 100%) por Modelo ───────────────────
    if llm_agent.disponible():
        modelo_activo = st.session_state.get("ia_modelo_sel", llm_agent.DEFAULT_MODELO)
        uso_hoy = llm_agent.obtener_uso_hoy(modelo_activo)

        pct = uso_hoy["porcentaje"]
        pct_clamped = min(100.0, max(0.0, pct))
        if pct == 0:
            pct_str = "0%"
        elif pct < 1.0:
            pct_str = f"{pct:.1f}%"
        else:
            pct_str = f"{pct:.0f}%" if pct.is_integer() else f"{pct:.1f}%"

        req_act = uso_hoy["peticiones"]
        req_lim = uso_hoy["limite_peticiones"]
        tok_act = uso_hoy["tokens"]
        tok_lim = uso_hoy["limite_tokens"]
        estado = uso_hoy["estado"]  # "normal", "aviso", "limite"

        def _fmt_tok(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
            if n >= 1_000:
                return f"{n / 1_000:.1f}k".replace(".0k", "k")
            return str(n)

        tok_str = f"{_fmt_tok(tok_act)} / {_fmt_tok(tok_lim)} tokens"
        req_str = f"{req_act} / {req_lim:,} peticiones".replace(",", ".")
        fill_width = f"{max(pct_clamped, 0.8):.2f}" if pct_clamped > 0 else "0"

        agotada = uso_hoy.get("agotada", False)
        titulo_uso = "Cuota diaria agotada" if agotada else "Uso diario"
        alerta_agotada_html = (
            f'<div style="margin-top: 10px; font-family: \'Inter\', sans-serif; font-size: 0.74rem; color: #fca5a5; background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.25); border-radius: 8px; padding: 8px 12px; display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;">'
            f'<div>⚠️ Has alcanzado el límite diario de Google para <b>{modelo_activo}</b> ({req_lim}/{req_lim}). Las respuestas se generan mediante el motor local.</div>'
            f'<div style="color: #67e8f9; font-weight: 600;">💡 Disponibles: <b>Gemini 3.5 Flash Lite</b> (500 consultas/día) o <b>Gemini 3.7 Flash</b></div>'
            f'</div>'
        ) if agotada else ""

        st.markdown(
            f"""
            <div class="ia-usage-wrap">
                <div class="ia-usage-meta">
                    <div class="ia-usage-left">
                        <span class="ia-usage-dot-status {estado}"></span>
                        <span class="ia-usage-title" style="{'color: #f87171;' if agotada else ''}">{titulo_uso}</span>
                        <span class="ia-usage-model-tag">{modelo_activo}</span>
                    </div>
                    <div class="ia-usage-right">
                        <span class="ia-usage-detail">{req_str} • {tok_str}</span>
                        <span class="ia-usage-pct {estado}">{pct_str}</span>
                    </div>
                </div>
                <div class="ia-usage-track" title="Uso diario de {modelo_activo}: {pct_str} ({req_str}, {tok_str})">
                    <div class="ia-usage-fill {estado}" style="width: {fill_width}%;"></div>
                </div>
                {alerta_agotada_html}
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── Sugerencias / Chips rápidos (rotan cada día) ─────────────────
    sugeridas = assistant_engine.preguntas_sugeridas_dia()
    with st.container(key="ia_chips"):
        cols = st.columns(len(sugeridas))
        for i, sugerida in enumerate(sugeridas):
            if cols[i].button(sugerida, key=f"ia_chip_{i}", use_container_width=True):
                st.session_state["ia_pending"] = sugerida

    # ── Procesar pregunta y añadirla al hilo de chat ─────────────────
    pendiente = st.session_state.pop("ia_pending", None) if hasattr(st, "session_state") else None
    if pendiente:
        modelo_activo = st.session_state.get("ia_modelo_sel", llm_agent.get_default_modelo())
        with st.spinner(f"Consultando con {modelo_activo}…"):
            respuesta = llm_agent.responder(pendiente, ctx, modelo_elegido=modelo_activo)
        hist = st.session_state.setdefault("ia_chat_hist", [])
        hist.append({"q": pendiente, "r": respuesta, "m": modelo_activo})
        st.session_state["ia_chat_hist"] = hist[-20:]  # máx. 20 interacciones visibles
        st.session_state["ia_chat_scroll"] = True
        st.rerun()

    # ── Hilo de conversación (burbujas en línea, más reciente abajo) ──
    hist_chat = st.session_state.get("ia_chat_hist", []) if hasattr(st, "session_state") else []
    if hist_chat:
        c_tit, c_limpiar = st.columns([4.5, 1.2], vertical_alignment="center")
        with c_tit:
            st.markdown(
                '<div style="font-family: \'Inter\', sans-serif; font-size: 0.82rem; '
                'color: #64748b; font-weight: 600; margin-top: 18px;">'
                f'💬 Conversación ({len(hist_chat)} mensaje(s))</div>',
                unsafe_allow_html=True,
            )
        with c_limpiar:
            if st.button("🗑 Limpiar conversación", key="ia_chat_clear",
                         use_container_width=True):
                st.session_state.pop("ia_chat_hist", None)
                st.session_state.pop("ia_gemini_hist", None)
                st.rerun()

        for item in hist_chat:
            _render_chat_item(item)

        # Ancla + auto-scroll al final solo tras añadir un mensaje nuevo
        st.markdown('<div id="ia-chat-end"></div>', unsafe_allow_html=True)
        if hasattr(st, "session_state") and st.session_state.pop("ia_chat_scroll", False):
            st.components.v1.html(
                """<script>
                try {
                    const doc = window.parent?.document || window.document;
                    const el = doc.querySelector('#ia-chat-end');
                    if (el) { el.scrollIntoView({behavior: 'smooth', block: 'end'}); }
                } catch (e) {}
                </script>""",
                height=0,
            )


# ═══════════════════════════════════════════════════════════════════
# 🎯 SECCIÓN 2 — RADAR PREDICTIVO
# ═══════════════════════════════════════════════════════════════════

def _seccion_radar(radar: pd.DataFrame, tickers: tuple, nombres: tuple,
                   tilts: tuple) -> None:
    """Radar de predicciones con selector de horizonte y tabla única."""

    # ── Título centrado ──────────────────────────────────────────────
    st.markdown(
        """
        <div class="ia-section-header">
            <div class="ia-section-title">Radar Predictivo</div>
            <div class="ia-section-sub">Señales algorítmicas multitemporales y proyección técnica</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if radar.empty:
        st.markdown(
            """
            <div class="ia-empty-shell">
                <div class="ia-empty-icon">⚠️</div>
                <div class="ia-empty-title">Radar sin datos</div>
                <div class="ia-empty-desc">No se han podido descargar los precios de mercado. Recarga en unos instantes.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    # Sincronizar horizonte si viene por query param desde el enlace del ojo
    mapa_codigo_a_label = {
        "1D": "1 Día",
        "3D": "3 Días",
        "7D": "1 Semana",
        "1M": "1 Mes",
    }
    hz_param_pre = st.query_params.get("ia_hz")
    if hz_param_pre in mapa_codigo_a_label:
        st.session_state["ia_radar_hz_select"] = mapa_codigo_a_label[hz_param_pre]

    # ── Selector desplegable de horizonte (Estilo Ranking, perfectamente centrado) ──
    opciones_filtro = ["1 Día", "3 Días", "1 Semana", "1 Mes"]
    mapa_filtro_a_hz = {
        "1 Día": "1D",
        "3 Días": "3D",
        "1 Semana": "7D",
        "1 Mes": "1M",
    }
    default_idx = 2  # "1 Semana" (7D)
    if "ia_radar_hz_select" in st.session_state and st.session_state["ia_radar_hz_select"] in opciones_filtro:
        default_idx = opciones_filtro.index(st.session_state["ia_radar_hz_select"])

    opciones_conviccion = [
        "Todas las empresas",
        "⭐ Alta convicción (Confianza ≥ 75%)",
        "🟢 Solo Alcistas",
        "🔴 Solo Bajistas",
        "⚡ Divergencia Táctica (Corto vs Medio)",
    ]

    _, col_center, _ = st.columns([1.3, 1.8, 1.3])
    with col_center:
        filtro_elegido = st.selectbox(
            "HORIZONTE TEMPORAL",
            opciones_filtro,
            index=default_idx,
            key="ia_radar_hz_select",
        )
        conv_elegida = st.selectbox(
            "FILTRAR SEÑALES",
            opciones_conviccion,
            index=0,
            key="ia_radar_conv_select",
        )
    hz_sel = mapa_filtro_a_hz.get(filtro_elegido, "7D")
    hz_key = _HZ_MAP.get(hz_sel, "1S")

    # Banner de advertencia institucional: Señales experimentales sin significación demostrada
    st.markdown(
        """
        <div style="background: rgba(245, 158, 11, 0.07); border: 1px solid rgba(245, 158, 11, 0.22);
                    border-radius: 8px; padding: 10px 14px; margin: 8px auto 14px auto; font-family: 'Inter', sans-serif;">
            <div style="color: #fbbf24; font-weight: 600; font-size: 0.80rem; display: flex; align-items: center; gap: 6px;">
                <span>⚠️</span> <span>SEÑALES EXPERIMENTALES — SIN PODER PREDICTIVO DEMOSTRADO</span>
            </div>
            <div style="color: #94a3b8; font-size: 0.74rem; line-height: 1.45; margin-top: 3px;">
                Las etiquetas direccionales (ALCISTA / BAJISTA) y sus porcentajes de confianza se muestran exclusivamente con fines de investigación y forward-testing congelado. Los backtests cuantitativos institucionales han verificado que carecen de significación estadística (|t| &lt; 1,0) en acciones líquidas tras costes. No constituyen recomendación ni señal operativa.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Construir tabla con columnas del horizonte seleccionado ───────
    df = radar.copy()
    if conv_elegida == "⭐ Alta convicción (Confianza ≥ 75%)":
        df = df[df[f"conf_{hz_key}"] >= 75.0]
    elif conv_elegida == "🟢 Solo Alcistas":
        df = df[df[f"senal_{hz_key}"] == "ALCISTA"]
    elif conv_elegida == "🔴 Solo Bajistas":
        df = df[df[f"senal_{hz_key}"] == "BAJISTA"]
    elif conv_elegida == "⚡ Divergencia Táctica (Corto vs Medio)":
        cond_div = (
            ((df["senal_1D"] == "BAJISTA") & (df["senal_1S"] == "ALCISTA")) |
            ((df["senal_1D"] == "ALCISTA") & (df["senal_1S"] == "BAJISTA")) |
            ((df["senal_1D"] == "BAJISTA") & (df["senal_1M"] == "ALCISTA")) |
            ((df["rsi"] < 35) & (df["senal_1S"] == "ALCISTA"))
        )
        df = df[cond_div]

    def _senal_icon(senal: str) -> str:
        return _ICONOS_SENAL.get(senal, "⚪")

    import html as _html
    hz_label = _HZ_LABELS.get(hz_sel, hz_sel)

    if df.empty:
        st.markdown(
            f"""
            <div class="ia-hist-empty-notice" style="margin: 20px 0;">
                <div class="ia-hist-empty-icon">🔍</div>
                <div class="ia-hist-empty-title">Sin resultados para {conv_elegida}</div>
                <div class="ia-hist-empty-desc">Ningún activo cumple el criterio seleccionado en el horizonte <b>{filtro_elegido}</b>. Cambia de horizonte o selecciona <b>Todas las empresas</b>.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        disp = pd.DataFrame()
    else:
        disp = pd.DataFrame({
            "Empresa": df["empresa"],
            "Precio": df["precio"].map(lambda v: f"{v:.2f} €"),
            "RSI": df["rsi"].map(lambda v: f"{v:.0f}"),
            "Confianza (Exp.)": df[f"conf_{hz_key}"].map(lambda v: f"{v:.0f}%"),
            "Proyección": df[f"pct_{hz_key}"].map(lambda v: f"{v:+.2f}%"),
            "Predicción (Exp.)": [f"{_senal_icon(s)} {s}" for s in df[f"senal_{hz_key}"]],
            "Gráfico": [
                f'<a class="ia-eye-link" href="?page=IA&amp;ia_eye={t}&amp;ia_hz={hz_sel}" target="_self" '
                f'title="Ver proyección y diagnóstico de {_html.escape(str(n), quote=True)} a {hz_label}">👁</a>'
                for t, n in zip(df["ticker"], df["empresa"])
            ],
        })

        col_widths_radar = {
            "Empresa": "24%",
            "Precio": "13%",
            "RSI": "10%",
            "Confianza (Exp.)": "13%",
            "Proyección": "14%",
            "Predicción (Exp.)": "16%",
            "Gráfico": "10%",
        }
        st.html(
            _render_tabla_html(
                disp,
                max_height=480,
                center_all=True,
                compact=True,
                col_widths=col_widths_radar,
            )
        )

    # ── Pie del Radar: info modelo + botón de refresco ──────────────
    m_radar = db_pred.obtener_metricas(hz_key)
    n_pend_radar = len(disp)

    c_info_r, c_action_r = st.columns([3.2, 1.3])
    with c_info_r:
        st.markdown(
            f"""
            <div style="font-family: 'Inter', sans-serif; font-size: 0.72rem; color: #475569; padding-top: 6px;">
                Modelo oficial <b>{db_pred.MODELO_VERSION}</b> · {len(disp)} empresas ·
                {n_pend_radar} proyecciones activas ({filtro_elegido})
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c_action_r:
        if st.button(
            "🔄 Refrescar vista...",
            key="btn_trigger_refrescar_radar",
            use_container_width=True,
            help="Refresca los cálculos del radar en memoria con los precios actuales (sin modificar predicciones guardadas)",
        ):
            try:
                signal_engine.calcular_radar.clear()
            except Exception:
                pass
            st.toast("✅ Vista del radar refrescada en memoria.", icon="🔄")
            st.rerun()

    # ── Popup de inspección rápida (columna Gráfico → query param) ───
    eye_param = st.query_params.get("ia_eye")
    hz_param = st.query_params.get("ia_hz")
    if eye_param:
        hz_target = hz_param if hz_param in _HZ_MAP else hz_sel
        try:
            del st.query_params["ia_eye"]
            if "ia_hz" in st.query_params:
                del st.query_params["ia_hz"]
        except Exception:
            pass
        if eye_param in tickers:
            st.session_state["ia_eye_item"] = {"ticker": eye_param, "hz": hz_target}

    eye_item = st.session_state.pop("ia_eye_item", None)
    if eye_item and eye_item.get("ticker") in tickers:
        _idx_eye = tickers.index(eye_item["ticker"])
        _dialog_inspeccion_radar(
            eye_item["ticker"], nombres[_idx_eye],
            eye_item.get("hz", hz_sel), tickers, tilts,
        )


# ═══════════════════════════════════════════════════════════════════
# 📜 SECCIÓN 3 — HISTORIAL PREDICTIVO
# ═══════════════════════════════════════════════════════════════════

@st.dialog("Archivar Historial de Predicciones", width="small")
def _dialog_limpiar_historial(hz_key: str, hz_label: str, n_res_hz: int, n_res_total: int) -> None:
    """Diálogo modal con confirmación de seguridad para archivar (soft-delete) predicciones resueltas."""
    st.markdown(
        """
        <div style="background: rgba(59, 130, 246, 0.08); border: 1px solid rgba(59, 130, 246, 0.25);
                    border-radius: 10px; padding: 12px 14px; margin-bottom: 16px; font-family: 'Inter', sans-serif;">
            <div style="color: #60a5fa; font-weight: 700; font-size: 0.88rem; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
                <span>📦</span> <span>Archivado Histórico (Soft Delete)</span>
            </div>
            <div style="color: #94a3b8; font-size: 0.78rem; line-height: 1.5;">
                Las señales resueltas se moverán al archivo histórico. Se ocultan de la vista operativa diaria
                pero <b>todos los registros y su auditoría se conservan intactos en la base de datos</b>.
                No se destruye ninguna fila. Las señales en seguimiento activo no se verán afectadas.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if hz_key != "Todos":
        opciones = [
            f"Solo horizonte '{hz_label}' ({n_res_hz} señales)",
            f"Todos los horizontes ({n_res_total} señales)",
        ]
        default_radio_idx = 1 if (n_res_hz == 0 and n_res_total > 0) else 0
        seleccion = st.radio(
            "Selecciona el alcance a archivar:",
            opciones,
            index=default_radio_idx,
            key="radio_reset_hist_alcance",
        )
        borrar_solo_hz = (seleccion == opciones[0])
        n_a_borrar = n_res_hz if borrar_solo_hz else n_res_total
    else:
        st.markdown(
            f"""
            <div style="font-family: 'Inter', sans-serif; font-size: 0.84rem; color: #cbd5e1; margin-bottom: 12px; background: rgba(13,16,23,0.6); padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
                Se archivarán las <b>{n_res_total}</b> predicciones resueltas del historial.
            </div>
            """,
            unsafe_allow_html=True,
        )
        borrar_solo_hz = False
        n_a_borrar = n_res_total

    if n_a_borrar == 0:
        st.info("No hay señales resueltas en el alcance seleccionado para archivar.")
        if st.button("Cerrar", use_container_width=True, key="btn_cerrar_reset_0"):
            st.rerun()
        return

    confirmado = st.checkbox(
        f"Confirmo archivar {n_a_borrar} señales resueltas",
        key="chk_confirm_archive",
    )

    st.markdown("<div style='height: 6px;'></div>", unsafe_allow_html=True)
    c_cancel, c_confirm = st.columns([1, 1.25])
    with c_cancel:
        if st.button("Cancelar", use_container_width=True, key="btn_cancelar_reset_hist"):
            st.rerun()
    with c_confirm:
        if st.button(
            f"📦 Archivar ({n_a_borrar})",
            type="primary",
            use_container_width=True,
            disabled=not confirmado,
            key="btn_confirmar_reset_hist",
        ):
            hz_param = hz_key if borrar_solo_hz else None
            archivadas = db_pred.limpiar_resueltas(hz_param)
            st.toast(f"✅ Se han archivado {archivadas} predicciones resueltas (soft-delete).", icon="📦")
            st.rerun()


def _seccion_historial(radar: Optional[pd.DataFrame] = None) -> None:
    """Historial de predicciones con filtro de horizonte y métricas cuantitativas."""

    # ── Título centrado con subtítulo estilizado ─────────────────────
    st.markdown(
        """
        <div class="ia-section-header">
            <div class="ia-section-title">Historial Predictivo</div>
            <div class="ia-section-sub">Auditoría cuantitativa y seguimiento de señales emitidas</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Selector desplegable de horizonte (Estilo Ranking centrado) ──
    opciones_filtro = ["Todos", "1 Día", "3 Días", "1 Semana", "1 Mes"]
    mapa_filtro_a_hz = {
        "Todos": "Todos",
        "1 Día": "1D",
        "3 Días": "3D",
        "1 Semana": "1S",
        "1 Mes": "1M",
    }
    default_idx = 0
    if "ia_hist_hz_select" in st.session_state and st.session_state["ia_hist_hz_select"] in opciones_filtro:
        default_idx = opciones_filtro.index(st.session_state["ia_hist_hz_select"])

    _, col_sel, _ = st.columns([1.65, 1.35, 1.65])
    with col_sel:
        filtro_elegido = st.selectbox(
            "FILTRAR POR HORIZONTE",
            opciones_filtro,
            index=default_idx,
            key="ia_hist_hz_select",
        )
    hz_sel_key = mapa_filtro_a_hz.get(filtro_elegido, "Todos")

    # ── Métricas calculadas con precisión por SQL sin truncamiento ────
    try:
        m = db_pred.obtener_metricas(hz_sel_key)
        df_resueltas = db_pred.obtener_resueltas(hz_sel_key, limite=250)
        df_pendientes = db_pred.obtener_pendientes(hz_sel_key, solo_vigentes=True, limite=1500)
    except Exception as e:
        st.warning(f"No se pudieron cargar las métricas del historial en este momento: {e}")
        return

    if m.get("total", 0) == 0:
        st.markdown(
            """
            <div class="ia-empty-shell">
                <div class="ia-empty-icon">🌱</div>
                <div class="ia-empty-title">Historial en Fase de Acumulación</div>
                <div class="ia-empty-desc">Las señales generadas hoy ya están registradas. Cuando venzan sus horizontes, el sistema las liquidará contra el precio real y mostrará aquí las métricas de acierto.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    total_señales = m.get("total", 0)
    total_resueltas = m.get("resueltas", 0)
    total_pendientes = len(df_pendientes) if not df_pendientes.empty else m.get("pendientes", 0)
    tasa_acierto = m.get("tasa_acierto")
    score_medio = m.get("score_medio")
    n_invalidadas = m.get("invalidadas", 0)

    tasa_str = f"{tasa_acierto:.1f}%" if tasa_acierto is not None else "En cálculo"
    tasa_cls = "pos" if (tasa_acierto is not None and tasa_acierto >= 50.0) else ("amber" if tasa_acierto is not None else "cyan")
    score_str = f"{score_medio:.0f}/100" if score_medio is not None else "—"
    score_cls = "pos" if (score_medio is not None and score_medio >= 70) else ("amber" if score_medio is not None and score_medio >= 40 else ("neg" if score_medio is not None else "cyan"))

    inv_caption = f" · {n_invalidadas} invalidadas" if n_invalidadas > 0 else ""

    # Conteo dinámico de empresas únicas en seguimiento (evita hardcodeo '116 empresas')
    n_empresas_vig = len(df_pendientes["ticker"].unique()) if not df_pendientes.empty else m.get("vigentes", 0)
    caption_vivas = f"{n_empresas_vig} empresas" if hz_sel_key != "Todos" else "4 horizontes"

    # ── Tarjetas KPI del Historial Predictivo ─────────────────────────
    kpis_html = f"""
    <div class="ia-hist-kpi-row ia-hist-kpi-row-5">
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Total Señales</span>
                <span class="ia-hist-kpi-icon">🎯</span>
            </div>
            <div class="ia-hist-kpi-value cyan">{total_señales}</div>
            <div class="ia-hist-kpi-caption">Registradas ({filtro_elegido})</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Resueltas</span>
                <span class="ia-hist-kpi-icon">📋</span>
            </div>
            <div class="ia-hist-kpi-value">{total_resueltas}</div>
            <div class="ia-hist-kpi-caption">Liquidadas vs precio real{inv_caption}</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Tasa de Acierto</span>
                <span class="ia-hist-kpi-icon">🏆</span>
            </div>
            <div class="ia-hist-kpi-value {tasa_cls}">{tasa_str}</div>
            <div class="ia-hist-kpi-caption">Dirección confirmada</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Score Medio</span>
                <span class="ia-hist-kpi-icon">📊</span>
            </div>
            <div class="ia-hist-kpi-value {score_cls}">{score_str}</div>
            <div class="ia-hist-kpi-caption">Calidad global (dir. + precisión)</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">En Seguimiento</span>
                <span class="ia-hist-kpi-icon">⏳</span>
            </div>
            <div class="ia-hist-kpi-value amber">{total_pendientes}</div>
            <div class="ia-hist-kpi-caption">Vivas ({caption_vivas})</div>
        </div>
    </div>
    """
    st.html(kpis_html)

    hz_fmt_map = {"1D": "1 Día", "3D": "3 Días", "1S": "1 Semana", "1M": "1 Mes"}

    # ── Pestañas Institucionales: Resueltas vs Seguimiento Activo ──────
    n_res = len(df_resueltas)
    n_pend = len(df_pendientes)

    tab_res, tab_pend = st.tabs([
        f"📋 Historial de Resueltas ({n_res})",
        f"⏳ En Seguimiento Activo ({n_pend})",
    ])

    with tab_res:
        if not df_resueltas.empty:
            # Desglose cuantitativo por dirección de señal
            df_alc = df_resueltas[df_resueltas["senal"].str.upper() == "ALCISTA"]
            df_baj = df_resueltas[df_resueltas["senal"].str.upper() == "BAJISTA"]
            df_lat = df_resueltas[df_resueltas["senal"].str.upper() == "LATERAL"]

            tasa_alc = (df_alc["acierto"].sum() / len(df_alc) * 100.0) if len(df_alc) > 0 else 0.0
            tasa_baj = (df_baj["acierto"].sum() / len(df_baj) * 100.0) if len(df_baj) > 0 else 0.0
            tasa_lat = (df_lat["acierto"].sum() / len(df_lat) * 100.0) if len(df_lat) > 0 else 0.0

            ret_alc_medio = df_alc["variacion_real"].dropna().mean() if len(df_alc) > 0 else 0.0
            ret_baj_medio = (-df_baj["variacion_real"].dropna()).mean() if len(df_baj) > 0 else 0.0
            u_lat_lbl = f"±{db_pred.UMBRALES_LATERAL.get(hz_sel_key, 0.8):.1f}%" if hz_sel_key != "Todos" else "en rango"

            st.markdown(
                f"""
                <div style="display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap;">
                    <div style="flex: 1; min-width: 140px; background: rgba(34, 197, 94, 0.06); border: 1px solid rgba(34, 197, 94, 0.2); border-radius: 8px; padding: 7px 12px; font-family: 'Inter', sans-serif;">
                        <div style="font-size: 0.68rem; color: #86efac; font-weight: 600; text-transform: uppercase;">🟢 Alcistas ({len(df_alc)})</div>
                        <div style="font-size: 0.88rem; color: #ffffff; font-weight: 700; margin-top: 2px;">{tasa_alc:.1f}% <span style="font-size: 0.72rem; color: #94a3b8; font-weight: 400;">acierto</span> <span style="font-size: 0.75rem; color: {'#4ade80' if ret_alc_medio>=0 else '#f87171'}; font-weight: 600; margin-left: 4px;">({ret_alc_medio:+.2f}% ret.)</span></div>
                    </div>
                    <div style="flex: 1; min-width: 140px; background: rgba(239, 68, 68, 0.06); border: 1px solid rgba(239, 68, 68, 0.2); border-radius: 8px; padding: 7px 12px; font-family: 'Inter', sans-serif;">
                        <div style="font-size: 0.68rem; color: #fca5a5; font-weight: 600; text-transform: uppercase;">🔴 Bajistas ({len(df_baj)})</div>
                        <div style="font-size: 0.88rem; color: #ffffff; font-weight: 700; margin-top: 2px;">{tasa_baj:.1f}% <span style="font-size: 0.72rem; color: #94a3b8; font-weight: 400;">acierto</span> <span style="font-size: 0.75rem; color: {'#4ade80' if ret_baj_medio>=0 else '#f87171'}; font-weight: 600; margin-left: 4px;">({ret_baj_medio:+.2f}% ret.)</span></div>
                    </div>
                    <div style="flex: 1; min-width: 140px; background: rgba(234, 179, 8, 0.06); border: 1px solid rgba(234, 179, 8, 0.2); border-radius: 8px; padding: 7px 12px; font-family: 'Inter', sans-serif;">
                        <div style="font-size: 0.68rem; color: #fde047; font-weight: 600; text-transform: uppercase;">🟡 Laterales ({len(df_lat)})</div>
                        <div style="font-size: 0.88rem; color: #ffffff; font-weight: 700; margin-top: 2px;">{tasa_lat:.1f}% <span style="font-size: 0.72rem; color: #94a3b8; font-weight: 400;">acierto ({u_lat_lbl})</span></div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Columna Resultado con distinción de invalidaciones
            col_motivo = df_resueltas.get("motivo_resolucion")
            resultados = []
            for i, row in df_resueltas.iterrows():
                motivo = row.get("motivo_resolucion", "vencida") if col_motivo is not None else "vencida"
                if motivo == "invalidada":
                    resultados.append("⚡ Invalidada")
                elif row.get("acierto") == 1:
                    resultados.append("✅ Acierto")
                else:
                    resultados.append("❌ Fallo")

            # Columna Score con formato
            col_score = df_resueltas.get("score_calidad")
            scores_fmt = (
                col_score.map(lambda v: f"{v:.0f}" if pd.notna(v) else "—")
                if col_score is not None
                else pd.Series(["—"] * len(df_resueltas))
            )

            disp_r = pd.DataFrame({
                "Fecha": df_resueltas["fecha_resolucion"],
                "Empresa": df_resueltas["empresa"],
                "Horizonte": df_resueltas["horizonte"].map(lambda h: hz_fmt_map.get(str(h), str(h))),
                "Señal": df_resueltas["senal"],
                "Entrada": df_resueltas["precio_entrada"].map(lambda v: f"{v:.2f} €" if pd.notna(v) else "—"),
                "Cierre": df_resueltas["precio_real"].map(lambda v: f"{v:.2f} €" if pd.notna(v) else "—"),
                "Est. (%)": df_resueltas["pct_estimado"].map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "—"),
                "Real (%)": df_resueltas["variacion_real"].map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "—"),
                "Score": scores_fmt,
                "Resultado": resultados,
            })

            col_widths_hist = {
                "Fecha": "10%",
                "Empresa": "14%",
                "Horizonte": "8%",
                "Señal": "9%",
                "Entrada": "10%",
                "Cierre": "10%",
                "Est. (%)": "9%",
                "Real (%)": "9%",
                "Score": "8%",
                "Resultado": "13%",
            }
            st.html(_render_tabla_html(disp_r, max_height=450, center_all=True, compact=True, col_widths=col_widths_hist))

            # Pie de tabla con botón discreto para resetear historial
            c_info, c_action = st.columns([3.2, 1.3])
            with c_info:
                st.markdown(
                    f"""
                    <div style="font-family: 'Inter', sans-serif; font-size: 0.74rem; color: #64748b; padding-top: 6px;">
                        Mostrando las últimas <b>{n_res}</b> predicciones resueltas auditadas contra precio real.
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with c_action:
                if st.button(
                    "🗑️ Limpiar resueltas...",
                    key="btn_trigger_limpiar_hist",
                    use_container_width=True,
                    help=f"Eliminar predicciones resueltas ({filtro_elegido} o todas)",
                ):
                    m_glob = db_pred.obtener_metricas("Todos")
                    _dialog_limpiar_historial(
                        hz_key=hz_sel_key,
                        hz_label=filtro_elegido,
                        n_res_hz=n_res,
                        n_res_total=m_glob.get("resueltas", 0),
                    )
        else:
            st.markdown(
                f"""
                <div class="ia-hist-empty-notice">
                    <div class="ia-hist-empty-icon">🌱</div>
                    <div class="ia-hist-empty-title">Sin señales resueltas para {filtro_elegido}</div>
                    <div class="ia-hist-empty-desc">
                        No hay señales vencidas ni liquidadas aún para este horizonte temporal.<br>
                        Las señales vigentes se encuentran activas en la pestaña <b>En Seguimiento Activo</b> hasta alcanzar su fecha objetivo de vencimiento.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            m_glob = db_pred.obtener_metricas("Todos")
            n_res_glob = m_glob.get("resueltas", 0)
            if n_res_glob > 0:
                _, col_reset = st.columns([3.2, 1.3])
                with col_reset:
                    if st.button(
                        "🗑️ Gestionar Historial...",
                        key="btn_trigger_limpiar_hist_empty",
                        use_container_width=True,
                        help="Gestionar o eliminar predicciones resueltas de otros horizontes",
                    ):
                        _dialog_limpiar_historial(
                            hz_key=hz_sel_key,
                            hz_label=filtro_elegido,
                            n_res_hz=0,
                            n_res_total=n_res_glob,
                        )

    with tab_pend:
        if not df_pendientes.empty:
            hz_tag = filtro_elegido.upper() if hz_sel_key != "Todos" else "TODOS LOS HORIZONTES"
            st.markdown(
                f"""
                <div class="ia-hist-status-strip">
                    <b>🟢 {n_pend} señales vivas en seguimiento</b> ({hz_tag}) · Se liquidarán automáticamente al precio de cierre al llegar su vencimiento.
                </div>
                """,
                unsafe_allow_html=True,
            )
            precios_map = dict(zip(radar["ticker"], radar["precio"])) if (radar is not None and not radar.empty) else {}
            precios_act = []
            vars_act = []
            for _, r in df_pendientes.iterrows():
                tk = r.get("ticker")
                p_in = r.get("precio_entrada")
                p_cur = precios_map.get(tk)
                if p_cur is not None and pd.notna(p_cur) and p_cur > 0:
                    precios_act.append(f"{p_cur:.2f} €")
                    if p_in is not None and pd.notna(p_in) and p_in > 0:
                        v = ((p_cur - p_in) / p_in) * 100.0
                        vars_act.append(f"{v:+.2f}%")
                    else:
                        vars_act.append("—")
                else:
                    precios_act.append("—")
                    vars_act.append("—")

            disp_p = pd.DataFrame({
                "Emisión": df_pendientes["fecha_prediccion"],
                "Empresa": df_pendientes["empresa"],
                "Horizonte": df_pendientes["horizonte"].map(lambda h: hz_fmt_map.get(str(h), str(h))),
                "Señal": df_pendientes["senal"],
                "Entrada": df_pendientes["precio_entrada"].map(lambda v: f"{v:.2f} €" if pd.notna(v) else "—"),
                "Actual": precios_act,
                "Var. (%)": vars_act,
                "Est. (%)": df_pendientes["pct_estimado"].map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "—"),
                "Vencimiento": df_pendientes["fecha_objetivo"],
            })
            col_widths_pend = {
                "Emisión": "11%",
                "Empresa": "18%",
                "Horizonte": "10%",
                "Señal": "10%",
                "Entrada": "10%",
                "Actual": "10%",
                "Var. (%)": "10%",
                "Est. (%)": "10%",
                "Vencimiento": "11%",
            }
            st.html(_render_tabla_html(disp_p, max_height=450, center_all=True, compact=True, col_widths=col_widths_pend))
        else:
            st.markdown(
                f"""
                <div class="ia-hist-empty-notice">
                    <div class="ia-hist-empty-icon">⏳</div>
                    <div class="ia-hist-empty-title">Sin señales en seguimiento</div>
                    <div class="ia-hist-empty-desc">
                        No hay señales vigentes en seguimiento para el horizonte <b>{filtro_elegido}</b>.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )



# ═══════════════════════════════════════════════════════════════════
# 🚀 PUNTO DE ENTRADA PRINCIPAL
# ═══════════════════════════════════════════════════════════════════

def _cargar_tilts(tickers: tuple) -> tuple:
    """Tilt fundamental [-2, 2] desde el score persistido del ranking."""
    try:
        filas = query_all("SELECT ticker, score FROM ranking_estado")
        scores = {r["ticker"]: float(r["score"]) for r in filas}
    except Exception:
        scores = {}
    return tuple(max(-2.0, min(2.0, (scores.get(t, 50.0) - 50.0) / 25.0))
                 for t in tickers)


def precargar_ia() -> None:
    """
    Precarga y calienta todas las cachés y datos del área de Inteligencia Artificial
    para que la navegación a la pestaña 'IA' sea instantánea (0 ms).

    Ejecuta:
      1. Cálculo y almacenamiento en caché (@st.cache_data) del Radar Predictivo.
      2. Inicialización de la tabla de predicciones, resolución automática de vencidas
         y registro de las señales emitidas hoy.
      3. Lectura y cacheo en memoria de los fundamentales en disco para el Asistente.
      4. Precarga de las métricas e historial de predicciones.
      5. Precarga del estado y límites de uso del modelo de IA por defecto.
    """
    try:
        empresas = query_all(
            "SELECT ticker, nombre FROM empresas WHERE activa = 1 ORDER BY nombre"
        )
        if not empresas:
            return

        tickers = tuple(e["ticker"] for e in empresas)
        nombres = tuple(e["nombre"] for e in empresas)
        tilts = _cargar_tilts(tickers)

        # 1. Radar predictivo multitemporal (cacheado con @st.cache_data 5 min)
        radar = signal_engine.calcular_radar(tickers, nombres, tilts)

        # 2. Tracking de predicciones: resolución y registro de señales de hoy
        try:
            db_pred.init_predicciones_table()
            if radar is not None and not radar.empty:
                precios = dict(zip(radar["ticker"], radar["precio"]))
                db_pred.resolver_predicciones(precios)
                db_pred.registrar_predicciones(radar)
            st.session_state["_ia_tracking_sincronizado"] = True
        except Exception:
            pass

        # 3. Fundamentales en disco para el Asistente (@st.cache_data 12h)
        try:
            assistant_engine.cargar_fundamentales_disco(tickers)
        except Exception:
            pass

        # 4. Métricas cuantitativas del Historial Predictivo
        try:
            for hz in ("Todos", "1D", "3D", "1S", "1M"):
                db_pred.obtener_metricas(hz)
                db_pred.obtener_resueltas(hz, limite=250)
                db_pred.obtener_pendientes(hz, solo_vigentes=True, limite=1500)
        except Exception:
            pass

        # 5. Estado de uso diario del modelo LLM
        try:
            if llm_agent.disponible():
                modelo_def = llm_agent.get_default_modelo()
                llm_agent.obtener_uso_hoy(modelo_def)
        except Exception:
            pass

    except Exception as e:
        print(f"[PREWARM IA ERROR] {e}")


def show() -> None:
    """Punto de entrada principal para la vista de Inteligencia Artificial."""
    _css()

    # ── Datos base ───────────────────────────────────────────────────
    empresas = query_all(
        "SELECT ticker, nombre FROM empresas WHERE activa = 1 ORDER BY nombre"
    )
    tickers = tuple(e["ticker"] for e in empresas)
    nombres = tuple(e["nombre"] for e in empresas)
    tilts = _cargar_tilts(tickers)

    # ── Radar + tracking (registro y resolución automática) ─────────
    radar = signal_engine.calcular_radar(tickers, nombres, tilts)
    if not st.session_state.get("_ia_tracking_sincronizado"):
        try:
            db_pred.init_predicciones_table()
            if not radar.empty:
                precios = dict(zip(radar["ticker"], radar["precio"]))
                db_pred.resolver_predicciones(precios)
                db_pred.registrar_predicciones(radar)
            st.session_state["_ia_tracking_sincronizado"] = True
        except Exception:
            pass  # el tracking nunca debe romper la vista

    # ── Contexto compartido para el asistente ────────────────────────
    ctx = {
        "radar": radar,
        "tickers": tickers,
        "nombre2ticker": dict(zip(nombres, tickers)),
        "ticker2nombre": dict(zip(tickers, nombres)),
        "fundamentales": assistant_engine.cargar_fundamentales_disco(tickers),
    }

    # ── Flujo de secciones (sin header de página ni status strip) ────
    _seccion_agente(ctx)
    st.markdown('<div class="ia-divider"></div>', unsafe_allow_html=True)
    _seccion_radar(radar, tickers, nombres, tilts)
    st.markdown('<div class="ia-divider"></div>', unsafe_allow_html=True)
    _seccion_historial(radar=radar)

    # ── Disclaimer compacto ──────────────────────────────────────────
    st.markdown(
        '<div class="ia-disclaimer"><b>⚠️ Aviso:</b> Las señales y proyecciones '
        'son estimaciones probabilísticas. No constituyen recomendación de inversión. '
        'Rentabilidades pasadas no garantizan rendimientos futuros.</div>',
        unsafe_allow_html=True,
    )
