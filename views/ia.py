from services.descriptive_metrics import (
    cargar_datos_cache_historico,
    calcular_metricas_volatilidad,
    calcular_frecuencias_historicas,
    calcular_extremos_y_drawdown,
    calcular_metricas_liquidez,
    calcular_sensibilidad_benchmark,
    cargar_snapshot_fundamental,
)
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

import os
from typing import Dict, Optional

import numpy as np
import pandas as pd
import streamlit as st

from components.stock_chart import render_stock_chart
from database import predicciones as db_pred
from database.connection import query_all
from services import assistant_engine, llm_agent, signal_engine
from services.data_provider import get_price_batch
from services.signal_engine import MOTOR_VERSION

_ICONOS_SENAL = {"ALCISTA": "▲", "LATERAL": "—", "BAJISTA": "▼"}


# ── Cachés de lectura en memoria para métricas de historial/auditoría (sin alterar SQL ni cálculos) ──
@st.cache_data(ttl=300, show_spinner=False)
def _ia_obtener_metricas(horizonte: str = "Todos", incluir_legacy: bool = False):
    hz = horizonte or "Todos"
    return db_pred.obtener_metricas(hz, incluir_legacy=incluir_legacy)


@st.cache_data(ttl=300, show_spinner=False)
def _ia_obtener_resumen_c10(horizonte: str = "Todos", incluir_legacy: bool = False):
    hz = horizonte or "Todos"
    return db_pred.obtener_resumen_c10(hz, incluir_legacy=incluir_legacy)


@st.cache_data(ttl=300, show_spinner=False)
def _ia_obtener_resumen_c11(horizonte: str = "Todos", incluir_legacy: bool = False):
    hz = horizonte or "Todos"
    return db_pred.obtener_resumen_c11(hz, incluir_legacy=incluir_legacy)


@st.cache_data(ttl=300, show_spinner=False)
def _ia_obtener_resumen_c12(horizonte: str = "Todos", incluir_legacy: bool = False):
    hz = horizonte or "Todos"
    return db_pred.obtener_resumen_c12(hz, incluir_legacy=incluir_legacy)


@st.cache_data(ttl=300, show_spinner=False)
def _ia_obtener_tabla_c14(horizonte: str = "Todos", incluir_legacy: bool = False):
    hz = horizonte or "Todos"
    return db_pred.obtener_tabla_referencias_ingenuas(hz, incluir_legacy=incluir_legacy)


@st.cache_data(ttl=300, show_spinner=False)
def _ia_obtener_resueltas(horizonte: str = "Todos", limite: int = 250):
    hz = horizonte or "Todos"
    return db_pred.obtener_resueltas(hz, limite=limite)


@st.cache_data(ttl=300, show_spinner=False)
def _ia_obtener_pendientes(horizonte: str = "Todos", solo_vigentes: bool = True, limite: int = 1500):
    hz = horizonte or "Todos"
    return db_pred.obtener_pendientes(hz, solo_vigentes=solo_vigentes, limite=limite)


@st.cache_data(ttl=3600, show_spinner=False)
def _ia_vol_eur_mercado():
    """Precalcula el volumen medio en euros (60d) del universo para el tercil de liquidez."""
    closes, _, _, vols = cargar_datos_cache_historico()
    if closes.empty or vols.empty:
        return None
    return (closes * vols).rolling(60).mean().iloc[-1]


def _limpiar_caches_ia_pred() -> None:
    for fn in (
        _ia_obtener_metricas,
        _ia_obtener_resumen_c10,
        _ia_obtener_resumen_c11,
        _ia_obtener_resumen_c12,
        _ia_obtener_tabla_c14,
        _ia_obtener_resueltas,
        _ia_obtener_pendientes,
        _ia_vol_eur_mercado,
    ):
        try:
            fn.clear()
        except Exception:
            pass

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

/* ── Cabeceras ────────────────────────────────────────────────────── */
div:has(> .ia-section-header),
div[data-testid="stElementContainer"]:has(.ia-section-header) {
    margin-top: 0 !important;
    padding-top: 0 !important;
}

/* ── Título de Sección — centrado, limpio y sobrio ────────────────── */
.ia-section-header {
    text-align: center;
    margin: 0 0 10px;
}
.ia-section-header.sl-page-header {
    align-items: center;
    justify-content: center;
    flex-direction: column;
}
.ia-section-header.sl-page-header::after {
    left: 50%;
    transform: translateX(-50%);
}
.ia-section-header .sl-section-title {
    justify-content: center;
}
.ia-section-sub {
    margin-top: 4px;
    color: var(--sl-text-secondary);
    font: 450 .9rem/1.55 var(--sl-font-body);
}

/* ── Divisor con Glowing Gradient ──────────────────────────────────── */
.ia-divider {
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(148, 163, 184, 0.16), transparent);
    margin: 32px 0 26px;
}

/* ── Barra de Prompt Unificada (Estilo Gemini / ChatGPT Flagship) ─── */
.st-key-ia_prompt_box,
div[data-testid="stForm"]:has(.ia-prompt-form),
.st-key-ia_form {
    position: relative !important;
    background:
        radial-gradient(ellipse 65% 120% at 50% -30%, rgba(56, 189, 248, 0.12), transparent 72%),
        var(--sl-surface-glass) !important;
    border: 1px solid rgba(56, 189, 248, 0.36) !important;
    border-radius: 18px !important;
    padding: 8px 12px 8px 16px !important;
    margin: 26px auto 10px auto !important;
    max-width: 880px !important;
    box-shadow: 0 6px 26px rgba(0, 0, 0, 0.42), 0 0 28px -10px rgba(56, 189, 248, 0.26), inset 0 1px 0 rgba(255, 255, 255, 0.08) !important;
    transition: border-color 220ms var(--sl-ease), box-shadow 220ms var(--sl-ease), transform 220ms var(--sl-ease) !important;
    box-sizing: border-box !important;
}
.st-key-ia_prompt_box:hover,
.st-key-ia_prompt_box:focus-within,
div[data-testid="stForm"]:has(.ia-prompt-form):hover,
.st-key-ia_form:hover,
div[data-testid="stForm"]:has(.ia-prompt-form):focus-within,
.st-key-ia_form:focus-within {
    border-color: rgba(56, 189, 248, 0.62) !important;
    box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.14), 0 0 36px -8px rgba(56, 189, 248, 0.36), 0 12px 34px rgba(0, 0, 0, 0.50), inset 0 1px 0 rgba(255, 255, 255, 0.11) !important;
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

/* Columna 1: Formulario (Buscador + Botón Enviar) */
.st-key-ia_prompt_box > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(1),
.st-key-ia_prompt_box div[data-testid="stForm"] div[data-testid="stColumn"]:nth-child(1) {
    flex: 1 1 auto !important;
    width: auto !important;
    min-width: 0 !important;
    justify-content: flex-start !important;
}

/* Columna 2 dentro del form: Pequeña flecha de envío */
.st-key-ia_prompt_box div[data-testid="stForm"] div[data-testid="stColumn"]:nth-child(2) {
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: auto !important;
    justify-content: center !important;
}

/* Columna 2 exterior: Selector de modelos de Gemini (a la derecha, fuera del form) */
.st-key-ia_prompt_box > div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(2) {
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: 175px !important;
    max-width: 220px !important;
    justify-content: center !important;
}

/* Eliminar borde/padding del st.form anidado para mantener la píldora única */
.st-key-ia_prompt_box div[data-testid="stForm"] {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
    margin: 0 !important;
    box-shadow: none !important;
    width: 100% !important;
}
.st-key-ia_prompt_box div[data-testid="InputInstructions"] {
    display: none !important;
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
.st-key-ia_prompt_box div[data-testid="stTextInput"] label[data-testid="stWidgetLabel"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"] div[data-testid="stWidgetLabel"],
div[data-testid="stForm"]:has(.ia-prompt-form) div[data-testid="stTextInput"] label[data-testid="stWidgetLabel"],
.st-key-ia_form div[data-testid="stTextInput"] div[data-testid="stWidgetLabel"],
.st-key-ia_form div[data-testid="stTextInput"] label[data-testid="stWidgetLabel"] {
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
    background: rgba(56, 189, 248, 0.08) !important;
    border: 1px solid rgba(56, 189, 248, 0.28) !important;
    color: #38bdf8 !important;
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
    box-shadow: none !important;
    transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1) !important;
}
.st-key-ia_prompt_box button:hover,
div[data-testid="stForm"]:has(.ia-prompt-form) button:hover,
.st-key-ia_form button:hover {
    background: linear-gradient(135deg, #38bdf8, #6366f1) !important;
    border-color: #7dd3fc !important;
    color: #ffffff !important;
    transform: scale(1.08) !important;
    box-shadow: 0 0 18px rgba(56, 189, 248, 0.55) !important;
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
.st-key-ia_prompt_box div[data-testid="stSelectbox"] div[data-testid="stWidgetLabel"],
.st-key-ia_prompt_box div[data-testid="stSelectbox"] label[data-testid="stWidgetLabel"],
.st-key-ia_prompt_box [data-testid="stWidgetLabel"] {
    display: none !important;
    width: 0 !important;
    height: 0 !important;
    overflow: hidden !important;
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
    background: rgba(56, 189, 248, 0.08) !important;
    border: 1px solid rgba(56, 189, 248, 0.28) !important;
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
    background: rgba(56, 189, 248, 0.14) !important;
    border-color: rgba(56, 189, 248, 0.39) !important;
    box-shadow: none !important;
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
    color: #7dd3fc !important;
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
    fill: #38bdf8 !important;
    color: #38bdf8 !important;
    width: 14px !important;
    height: 14px !important;
    display: block !important;
    margin: 0 !important;
}

/* Menú desplegable emergente (Popover del selectbox — sin alterar geometría virtualizada BaseWeb) */
div[data-baseweb="popover"]:has(ul[role="listbox"]) {
    background: transparent !important;
    border: none !important;
}
div[data-baseweb="popover"] ul[role="listbox"] {
    background: #101829 !important;
    border: 1px solid rgba(56, 189, 248, 0.28) !important;
    border-radius: 12px !important;
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.72) !important;
}
div[data-baseweb="popover"] li[role="option"] {
    background: transparent !important;
    color: #cbd5e1 !important;
    font-family: var(--sl-font-body) !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    transition: background-color 0.12s ease, color 0.12s ease !important;
    cursor: pointer !important;
}
div[data-baseweb="popover"] li[role="option"]:hover,
div[data-baseweb="popover"] li[role="option"][aria-selected="true"] {
    background: rgba(56, 189, 248, 0.16) !important;
    color: #7dd3fc !important;
    box-shadow: none !important;
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
    background: rgba(56, 189, 248, 0.08) !important;
    border: 1px solid rgba(56, 189, 248, 0.28) !important;
    color: #7dd3fc !important;
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
    background: rgba(13, 19, 32, 0.88);
    border: 1px solid rgba(148, 163, 184, 0.14);
    border-radius: 10px;
    box-sizing: border-box;
    transition: border-color 0.2s ease, background-color 0.2s ease;
}
.ia-usage-wrap:hover {
    border-color: rgba(56, 189, 248, 0.26);
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
    background: #38bdf8;
    box-shadow: 0 0 7px #38bdf8;
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
    color: #7dd3fc;
    background: rgba(56, 189, 248, 0.08);
    border: 1px solid rgba(56, 189, 248, 0.20);
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
    color: #38bdf8;
    padding: 1px 7px;
    background: rgba(56, 189, 248, 0.10);
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
    background: linear-gradient(90deg, #38bdf8 0%, #818cf8 55%, #34d399 100%);
    box-shadow: 0 0 12px rgba(56, 189, 248, 0.55);
    transition: width 0.4s cubic-bezier(0.16, 1, 0.3, 1), background 0.3s ease;
}
.ia-usage-fill.aviso {
    background: linear-gradient(90deg, #f59e0b 0%, #fbbf24 100%);
    box-shadow: 0 0 12px rgba(245, 158, 11, 0.50);
}
.ia-usage-fill.limite {
    background: linear-gradient(90deg, #f43f5e 0%, #ef4444 100%);
    box-shadow: 0 0 12px rgba(239, 68, 68, 0.55);
}

/* ── Tarjetas de Preguntas Frecuentes ─────────────────────────────── */
.ia-faq-label {
    text-align: center;
    font-family: 'Inter', sans-serif;
    font-size: 0.82rem;
    font-weight: 600;
    color: var(--sl-text-secondary);
    letter-spacing: 0.3px;
    margin: 0 0 14px 0;
}

.st-key-ia_chips button {
    background: var(--sl-surface-glass) !important;
    border: 1px solid var(--sl-border-default) !important;
    border-radius: var(--sl-radius-lg) !important;
    color: #d6e0ee !important;
    font-family: var(--sl-font-body) !important;
    font-size: 0.80rem !important;
    font-weight: 600 !important;
    padding: 12px 13px !important;
    height: 92px !important;
    min-height: 92px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    white-space: normal !important;
    line-height: 1.35 !important;
    transition: transform 220ms var(--sl-ease), background 220ms var(--sl-ease), border-color 220ms var(--sl-ease), box-shadow 220ms var(--sl-ease) !important;
    box-shadow: var(--sl-shadow-card) !important;
    position: relative !important;
    overflow: hidden !important;
}
.st-key-ia_chips button::before {
    content: "" !important;
    display: block !important;
    position: absolute !important;
    top: 0 !important;
    left: 18% !important;
    right: 18% !important;
    height: 1px !important;
    background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.55), transparent) !important;
    opacity: 0.4 !important;
    transition: opacity 220ms var(--sl-ease), left 220ms var(--sl-ease), right 220ms var(--sl-ease) !important;
}
.st-key-ia_chips button:hover {
    transform: translateY(-3px) !important;
    background: var(--sl-surface-glass-hover) !important;
    border-color: var(--sl-border-hover) !important;
    color: var(--sl-text-heading) !important;
    box-shadow: var(--sl-shadow-hover) !important;
}
.st-key-ia_chips button:hover::before {
    left: 8% !important;
    right: 8% !important;
    opacity: 1 !important;
}

/* ── Segmented Control Centrado ──────────────────────────────────── */
div[data-testid="stSegmentedControl"] {
    display: flex !important;
    justify-content: center !important;
    margin: 0 auto 16px auto !important;
    background: #111723 !important;
    border: 1px solid rgba(56, 189, 248, 0.18) !important;
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
    background: rgba(56, 189, 248, 0.16) !important;
    color: #38bdf8 !important;
    box-shadow: none !important;
    border: 1px solid rgba(56, 189, 248, 0.30) !important;
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
    background: #0d121c !important;
    border: 1px solid rgba(56, 189, 248, 0.25) !important;
    border-radius: 18px !important;
    box-shadow: 0 25px 85px rgba(0, 0, 0, 0.96) !important;
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
    background: linear-gradient(135deg, rgba(56, 189, 248, 0.08) 0%, rgba(15, 23, 42, 0.7) 100%);
    border: 1px solid rgba(56, 189, 248, 0.22);
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
    border: 1px solid rgba(148, 163, 184, 0.12);
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
    background: linear-gradient(165deg, #111723 0%, #0d121c 100%);
    border: 1px solid rgba(56, 189, 248, 0.18);
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
    box-shadow: none;
}
.ia-diag-badge.bajista {
    background: rgba(239, 68, 68, 0.15);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.35);
    box-shadow: none;
}
.ia-diag-badge.lateral {
    background: rgba(245, 158, 11, 0.15);
    color: #fbbf24;
    border: 1px solid rgba(245, 158, 11, 0.35);
    box-shadow: none;
}

.ia-diag-metrics-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
}
.ia-diag-mini-kpi {
    background: rgba(11, 15, 25, 0.9);
    border: 1px solid rgba(148, 163, 184, 0.12);
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
.ia-diag-mini-kpi .mini-val.cyan { color: #38bdf8; }
.ia-diag-mini-kpi .mini-val.pos { color: #34d399; }
.ia-diag-mini-kpi .mini-val.neg { color: #f87171; }
.ia-diag-mini-kpi .mini-val.neutral { color: #94a3b8; }

.ia-diag-desc-box {
    background: rgba(56, 189, 248, 0.04);
    border-left: 3px solid rgba(56, 189, 248, 0.31);
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
    background: linear-gradient(135deg, rgba(56, 189, 248, 0.08), rgba(14, 165, 233, 0.04));
    border: 1px solid rgba(56, 189, 248, 0.22);
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
    background: linear-gradient(160deg, #131a29 0%, #0d121c 100%);
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-left: 3px solid #38bdf8;
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
    color: #38bdf8;
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.30);
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
    background: #0d121c !important;
    border: 1px solid rgba(148, 163, 184, 0.12) !important;
    border-radius: 12px !important;
    margin-top: 16px !important;
}
details[data-testid="stExpander"] summary {
    font-family: 'Inter', sans-serif !important;
    font-weight: 600 !important;
    color: #e2e8f0 !important;
}
details[data-testid="stExpander"] summary:hover {
    color: #38bdf8 !important;
}

/* ── Shell para Estados Vacíos ────────────────────────────────────── */
.ia-empty-shell {
    background: linear-gradient(180deg, #121826 0%, #0e1420 100%);
    border: 1px dashed rgba(148, 163, 184, 0.22);
    border-radius: 16px;
    padding: 36px 28px;
    text-align: center;
    margin: 16px 0;
    position: relative;
    overflow: hidden;
}
.ia-empty-shell::before {
    display: none;
}
.ia-empty-icon  { font-size: 2.2rem; margin-bottom: 10px; }
.ia-empty-title { font-family:'Inter',sans-serif; font-size:1.05rem; font-weight:700; color:#e2e8f0; margin-bottom:6px; }
.ia-empty-desc  { font-family:'Inter',sans-serif; font-size:0.84rem; color:#64748b; max-width:580px; margin:0 auto; line-height:1.5; }

/* ── Selectores Desplegables de Horizonte y Filtro de Señales en Radar e Historial ── */
.st-key-ia_radar_hz_select,
.st-key-ia_radar_conv_select,
.st-key-ia_hist_hz_select,
.st-key-ia_desc_ticker_select {
    width: 100% !important;
    margin: 0 !important;
}
.st-key-ia_radar_hz_select div[data-testid="stSelectbox"],
.st-key-ia_radar_conv_select div[data-testid="stSelectbox"],
.st-key-ia_hist_hz_select div[data-testid="stSelectbox"],
.st-key-ia_desc_ticker_select div[data-testid="stSelectbox"] {
    width: 100% !important;
}
.st-key-ia_radar_hz_select label[data-testid="stWidgetLabel"],
.st-key-ia_radar_conv_select label[data-testid="stWidgetLabel"],
.st-key-ia_hist_hz_select label[data-testid="stWidgetLabel"],
.st-key-ia_desc_ticker_select label[data-testid="stWidgetLabel"] {
    text-align: left !important;
    justify-content: flex-start !important;
    display: flex !important;
    width: 100% !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.70rem !important;
    font-weight: 700 !important;
    color: #94a3b8 !important;
    letter-spacing: 1.2px !important;
    text-transform: uppercase !important;
    margin-bottom: 6px !important;
    min-height: 18px !important;
}
.st-key-ia_radar_hz_select label[data-testid="stWidgetLabel"] p,
.st-key-ia_radar_conv_select label[data-testid="stWidgetLabel"] p,
.st-key-ia_hist_hz_select label[data-testid="stWidgetLabel"] p,
.st-key-ia_desc_ticker_select label[data-testid="stWidgetLabel"] p {
    text-align: left !important;
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
.st-key-ia_hist_hz_select div[data-testid="stSelectbox"] > div > div,
.st-key-ia_desc_ticker_select div[data-testid="stSelectbox"] > div > div {
    background-color: #0d121c !important;
    border: 1px solid rgba(56, 189, 248, 0.18) !important;
    border-radius: 10px !important;
    color: #cbd5e1 !important;
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
.st-key-ia_hist_hz_select div[data-testid="stSelectbox"] > div > div:hover,
.st-key-ia_desc_ticker_select div[data-testid="stSelectbox"] > div > div:hover {
    border-color: rgba(56, 189, 248, 0.38) !important;
    box-shadow: none !important;
}
.st-key-ia_radar_hz_select div[data-testid="stSelectbox"] > div > div:focus-within,
.st-key-ia_radar_conv_select div[data-testid="stSelectbox"] > div > div:focus-within,
.st-key-ia_hist_hz_select div[data-testid="stSelectbox"] > div > div:focus-within,
.st-key-ia_desc_ticker_select div[data-testid="stSelectbox"] > div > div:focus-within {
    border-color: rgba(56, 189, 248, 0.43) !important;
    box-shadow: none !important;
}

/* ── Fila de Métricas KPI del Historial y Panel Descriptivo ────────── */
.ia-hist-kpi-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin: 14px 0 18px 0;
}
.ia-hist-kpi-row.ia-hist-kpi-row-5 {
    grid-template-columns: repeat(5, 1fr);
}
.ia-hist-kpi-row.ia-hist-kpi-row-6 {
    grid-template-columns: repeat(6, 1fr);
}
@media (max-width: 1200px) {
    .ia-hist-kpi-row.ia-hist-kpi-row-6 {
        grid-template-columns: repeat(3, 1fr);
    }
}
@media (max-width: 1100px) {
    .ia-hist-kpi-row.ia-hist-kpi-row-5 {
        grid-template-columns: repeat(3, 1fr);
    }
}
@media (max-width: 900px) {
    .ia-hist-kpi-row,
    .ia-hist-kpi-row.ia-hist-kpi-row-5,
    .ia-hist-kpi-row.ia-hist-kpi-row-6 {
        grid-template-columns: repeat(2, 1fr);
    }
}
.ia-hist-kpi-card.compact .ia-hist-kpi-value {
    font-size: 1.25rem !important;
}
.ia-hist-kpi-card {
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--sl-surface-glass);
    border: 1px solid var(--sl-border-default);
    border-radius: var(--sl-radius-lg);
    padding: 15px 18px 13px;
    box-shadow: var(--sl-shadow-card);
    position: relative;
    overflow: hidden;
    transition: transform 220ms var(--sl-ease), border-color 220ms var(--sl-ease), box-shadow 220ms var(--sl-ease);
}
.ia-hist-kpi-card::before {
    content: "";
    display: block;
    position: absolute;
    top: 0;
    left: 18%;
    right: 18%;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.52), transparent);
    opacity: 0.65;
    transition: opacity 220ms var(--sl-ease), left 220ms var(--sl-ease), right 220ms var(--sl-ease);
}
.ia-hist-kpi-card:hover {
    transform: translateY(-2px);
    border-color: var(--sl-border-hover);
    box-shadow: var(--sl-shadow-hover);
}
.ia-hist-kpi-card:hover::before {
    left: 8%;
    right: 8%;
    opacity: 1;
}
.ia-hist-kpi-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
}
.ia-hist-kpi-title {
    font-family: var(--sl-font-body);
    font-size: 0.69rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--sl-text-secondary);
}
.ia-hist-kpi-icon {
    font-size: 0.95rem;
}
.ia-hist-kpi-value {
    font-family: var(--sl-font-mono);
    font-variant-numeric: tabular-nums;
    font-size: 1.55rem;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: #f1f5f9;
    line-height: 1.2;
    margin-bottom: 4px;
}
.ia-hist-kpi-value.cyan { color: #38bdf8; }
.ia-hist-kpi-value.pos { color: #34d399; }
.ia-hist-kpi-value.amber { color: #fbbf24; }
.ia-hist-kpi-value.neg { color: #f87171; }
.ia-hist-kpi-caption {
    margin-top: auto;
    font-family: var(--sl-font-body);
    font-size: 0.72rem;
    color: var(--sl-text-muted);
    font-weight: 500;
}

/* ── Pestañas del Área IA — Segmented Control Centrado y Simétrico ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 8px !important;
    background: linear-gradient(180deg, rgba(14, 18, 28, 0.85) 0%, rgba(9, 12, 19, 0.95) 100%) !important;
    border: 1px solid rgba(148, 163, 184, 0.12) !important;
    border-radius: 14px !important;
    padding: 6px !important;
    margin: 12px auto 26px auto !important;
    max-width: 680px !important;
    justify-content: center !important;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
}
.stTabs [data-baseweb="tab"] {
    color: #94a3b8 !important;
    font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif !important;
    font-size: 0.88rem !important;
    font-weight: 600 !important;
    border-radius: 10px !important;
    transition: all 0.22s ease !important;
    padding: 9px 20px !important;
    border: 1px solid transparent !important;
    background: transparent !important;
    flex: 1 !important;
    text-align: center !important;
    justify-content: center !important;
}
.stTabs [data-baseweb="tab"]:hover {
    color: #ffffff !important;
    background: rgba(56, 189, 248, 0.06) !important;
    border-color: rgba(56, 189, 248, 0.15) !important;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
    background: linear-gradient(135deg, rgba(56, 189, 248, 0.18) 0%, rgba(14, 165, 233, 0.12) 100%) !important;
    color: #38bdf8 !important;
    font-weight: 700 !important;
    border: 1px solid rgba(56, 189, 248, 0.30) !important;
    box-shadow: 0 2px 14px rgba(56, 189, 248, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.1) !important;
}
.stTabs [data-baseweb="tab-highlight"],
.stTabs [data-baseweb="tab-border"] {
    display: none !important;
}

/* ── Badges y Pastillas Institucionales StockLab ────────────────── */
.tag-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    font-size: 0.70rem;
    padding: 3px 10px;
    border-radius: 6px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    line-height: 1.2;
    white-space: nowrap;
}
.tag-badge.tag-cyan {
    background: rgba(56, 189, 248, 0.09);
    border: 1px solid rgba(56, 189, 248, 0.30);
    color: #38bdf8;
    box-shadow: none;
}
.tag-badge.tag-amber {
    background: rgba(245, 158, 11, 0.09);
    border: 1px solid rgba(245, 158, 11, 0.32);
    color: #fbbf24;
    box-shadow: none;
}
.tag-badge.tag-emerald, .tag-badge.tag-green {
    background: rgba(16, 185, 129, 0.09);
    border: 1px solid rgba(16, 185, 129, 0.32);
    color: #34d399;
    box-shadow: none;
}
.tag-badge.tag-rose, .tag-badge.tag-red {
    background: rgba(239, 68, 68, 0.09);
    border: 1px solid rgba(239, 68, 68, 0.32);
    color: #f87171;
    box-shadow: none;
}
.tag-badge.tag-purple {
    background: rgba(168, 85, 247, 0.09);
    border: 1px solid rgba(168, 85, 247, 0.32);
    color: #c084fc;
    box-shadow: 0 0 10px rgba(168, 85, 247, 0.10);
}

/* ── Avisos y tiras de estado de Historial ───────────────────────── */
.ia-hist-status-strip {
    background: linear-gradient(135deg, rgba(56, 189, 248, 0.06) 0%, rgba(13, 16, 23, 0.75) 100%);
    border: 1px solid rgba(56, 189, 248, 0.18);
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
    border: 1px dashed rgba(148, 163, 184, 0.22);
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

/* ── Etiqueta Auxiliar de Control para Columnas Sincronizadas ── */
.ia-control-label {
    font-family: 'Inter', sans-serif !important;
    font-size: 0.70rem !important;
    font-weight: 700 !important;
    color: #94a3b8 !important;
    letter-spacing: 1.2px !important;
    text-transform: uppercase !important;
    margin-bottom: 6px !important;
    display: flex !important;
    align-items: center !important;
    height: 18px !important;
}

/* ── Botones de Refrescar en Radar y Panel Descriptivo ────────────────── */
.st-key-ia_btn_refrescar_panel,
.st-key-btn_trigger_refrescar_radar {
    width: 100% !important;
    margin: 0 !important;
}
.st-key-ia_btn_refrescar_panel button,
.st-key-btn_trigger_refrescar_radar button {
    background: rgba(56, 189, 248, 0.08) !important;
    border: 1px solid rgba(56, 189, 248, 0.28) !important;
    color: #38bdf8 !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.78rem !important;
    font-weight: 600 !important;
    border-radius: 9px !important;
    height: 42px !important;
    min-height: 42px !important;
    max-height: 42px !important;
    width: 100% !important;
    margin: 0 !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35) !important;
    transition: all 0.2s ease !important;
}
.st-key-ia_btn_refrescar_panel button:hover,
.st-key-btn_trigger_refrescar_radar button:hover {
    background: rgba(56, 189, 248, 0.18) !important;
    border-color: rgba(56, 189, 248, 0.43) !important;
    color: #ffffff !important;
    box-shadow: none !important;
}

/* ── Encabezados de Sección del Panel Descriptivo ────────────────── */
.ia-desc-section-head {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 24px 0 14px 0;
    padding-bottom: 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
}
.ia-desc-section-num {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.78rem;
    font-weight: 800;
    color: #38bdf8;
    background: rgba(56, 189, 248, 0.1);
    border: 1px solid rgba(56, 189, 248, 0.28);
    border-radius: 6px;
    padding: 3px 9px;
    line-height: 1;
}
.ia-desc-section-title {
    font-family: 'Plus Jakarta Sans', system-ui, sans-serif;
    font-size: 1.02rem;
    font-weight: 700;
    color: #f8fafc;
    letter-spacing: -0.02em;
}

/* ── Banners Institucionales con Estética Terminal ─────────────── */
.ia-desc-banner,
.ia-lab-banner {
    background: linear-gradient(135deg, rgba(245, 158, 11, 0.05) 0%, rgba(13, 17, 26, 0.8) 100%);
    border: 1px solid rgba(245, 158, 11, 0.26);
    border-left: 4px solid #f59e0b;
    border-radius: 12px;
    padding: 14px 20px;
    margin: 12px 0 20px 0;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
    animation: fade-in-up 0.25s ease-out both;
}
.ia-desc-banner-head,
.ia-lab-banner-head {
    color: #fbbf24;
    font-family: 'Plus Jakarta Sans', system-ui, sans-serif;
    font-weight: 700;
    font-size: 0.84rem;
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    letter-spacing: 0.2px;
}
.ia-desc-banner-body,
.ia-lab-banner-body {
    color: #94a3b8;
    font-family: 'Inter', sans-serif;
    font-size: 0.78rem;
    line-height: 1.55;
}
.ia-desc-banner-body b,
.ia-lab-banner-body b {
    color: #e2e8f0;
}

/* ── Barra de Metadatos del Panel Cuantitativo ──────────────────── */
.ia-desc-meta-strip {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 8px;
    background: rgba(11, 17, 30, 0.78);
    border: 1px solid var(--sl-border-default);
    border-radius: 10px;
    padding: 6px 10px;
    min-height: 42px;
    height: auto;
    box-sizing: border-box;
    flex-wrap: nowrap;
    overflow: hidden;
}
.ia-desc-meta-strip .tag-badge {
    font-size: 0.64rem;
    padding: 2px 7px;
    flex-shrink: 0;
}
.ia-desc-meta-text {
    font-family: var(--sl-font-body);
    font-size: 0.71rem;
    color: var(--sl-text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.ia-desc-meta-text b {
    color: #e2e8f0;
}

/* ── Tarjeta Visual de Rango 52 Semanas ──────────────────────────── */
.ia-range-card {
    background: linear-gradient(180deg, #131a29 0%, #101623 100%);
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-radius: 14px;
    padding: 18px 24px;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04);
    margin-bottom: 8px;
    position: relative;
    overflow: hidden;
}
.ia-range-card::before {
    display: none;
}
.ia-range-labels {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    margin-bottom: 12px;
}
.ia-range-bound {
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.ia-range-bound.right {
    text-align: right;
}
.ia-range-label {
    font-family: 'Inter', sans-serif;
    font-size: 0.64rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: #64748b;
}
.ia-range-val {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.15rem;
    font-weight: 800;
    color: #f1f5f9;
}
.ia-range-track {
    width: 100%;
    height: 9px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 999px;
    position: relative;
    overflow: visible;
    margin-bottom: 12px;
}
.ia-range-fill {
    height: 100%;
    background: linear-gradient(90deg, #38bdf8 0%, #34d399 100%);
    border-radius: 999px;
    box-shadow: none;
    transition: width 0.4s ease;
}
.ia-range-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-family: 'Inter', sans-serif;
    font-size: 0.72rem;
    color: #64748b;
    border-top: 1px solid rgba(255, 255, 255, 0.04);
    padding-top: 8px;
}
.ia-regime-strip {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: -6px;
    margin-bottom: 22px;
    font-family: 'Inter', sans-serif;
    font-size: 0.80rem;
    color: #94a3b8;
}

/* ── Chips y Badges de Historial y Laboratorio ───────────────────── */
.ia-stat-chip {
    flex: 1;
    min-width: 150px;
    border-radius: 10px;
    padding: 8px 14px;
    font-family: 'Inter', sans-serif;
    border: 1px solid rgba(255, 255, 255, 0.08);
    background: rgba(13, 17, 26, 0.6);
    display: flex;
    flex-direction: column;
    gap: 3px;
    transition: all 0.2s ease;
}
.ia-stat-chip:hover {
    border-color: rgba(56, 189, 248, 0.25);
    box-shadow: none;
}
.ia-stat-chip.pos {
    background: rgba(34, 197, 94, 0.05);
    border-color: rgba(34, 197, 94, 0.25);
}
.ia-stat-chip.neg {
    background: rgba(239, 68, 68, 0.05);
    border-color: rgba(239, 68, 68, 0.25);
}
.ia-stat-chip.amber {
    background: rgba(245, 158, 11, 0.05);
    border-color: rgba(245, 158, 11, 0.25);
}
.ia-stat-chip.slate {
    background: rgba(148, 163, 184, 0.05);
    border-color: rgba(148, 163, 184, 0.2);
}
.ia-stat-chip-label {
    font-size: 0.66rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: #94a3b8;
}
.ia-stat-chip-val {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.95rem;
    font-weight: 800;
    color: #ffffff;
}

/* ── Tabla de Frecuencias Cuantitativas del Panel ───────────────── */
.ia-desc-table-wrap {
    width: 100%;
    overflow-x: auto;
    background: linear-gradient(180deg, #111723 0%, #0d121c 100%);
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-radius: 12px;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04);
    margin-bottom: 20px;
}
.ia-desc-table {
    width: 100%;
    border-collapse: collapse;
    font-family: 'Inter', sans-serif;
    font-size: 0.82rem;
    text-align: left;
}
.ia-desc-table thead tr {
    background: rgba(13, 18, 28, 0.92);
    border-bottom: 1px solid rgba(56, 189, 248, 0.22);
}
.ia-desc-table th {
    padding: 12px 16px;
    color: #94a3b8;
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 1.1px;
    text-transform: uppercase;
    font-family: 'JetBrains Mono', monospace;
}
.ia-desc-table td {
    padding: 11px 16px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    color: #cbd5e1;
}
.ia-desc-table tbody tr:last-child td {
    border-bottom: none;
}
.ia-desc-table tbody tr:hover {
    background: rgba(56, 189, 248, 0.04);
}

/* ── Tarjetas Ejecutivas de Auditoría y Metodología ─────────────── */
.ia-audit-container {
    max-width: 1080px;
    margin: 0 auto 28px auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
    animation: fade-in-up 0.3s ease-out both;
}
.ia-audit-hero-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 6px;
}
@media (max-width: 900px) {
    .ia-audit-hero-row {
        grid-template-columns: repeat(2, 1fr);
    }
}
.ia-audit-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
}
@media (max-width: 850px) {
    .ia-audit-grid {
        grid-template-columns: 1fr;
    }
}
.ia-audit-card {
    background: var(--sl-surface-glass);
    border: 1px solid var(--sl-border-default);
    border-radius: var(--sl-radius-lg);
    padding: 20px 24px;
    box-shadow: var(--sl-shadow-card);
    position: relative;
    overflow: hidden;
    transition: transform 220ms var(--sl-ease), border-color 220ms var(--sl-ease), box-shadow 220ms var(--sl-ease);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
}
.ia-audit-card.full-width {
    grid-column: 1 / -1;
}
.ia-audit-card::before {
    content: "";
    display: block;
    position: absolute;
    top: 0;
    left: 16%;
    right: 16%;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.48), transparent);
    opacity: 0.55;
    transition: opacity 220ms var(--sl-ease), left 220ms var(--sl-ease), right 220ms var(--sl-ease);
}
.ia-audit-card:hover {
    transform: translateY(-2px);
    border-color: var(--sl-border-hover);
    box-shadow: var(--sl-shadow-hover);
}
.ia-audit-card:hover::before {
    left: 8%;
    right: 8%;
    opacity: 1;
}
.ia-audit-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
}
.ia-audit-title-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
}
.ia-audit-icon {
    font-size: 1.25rem;
    line-height: 1;
}
.ia-audit-title {
    font-family: 'Plus Jakarta Sans', system-ui, sans-serif;
    font-size: 0.96rem;
    font-weight: 700;
    color: #f8fafc;
    letter-spacing: -0.01em;
}
.ia-audit-desc {
    font-family: 'Inter', sans-serif;
    font-size: 0.84rem;
    color: #94a3b8;
    line-height: 1.6;
    margin-bottom: 8px;
}
.ia-audit-highlight {
    background: rgba(13, 17, 26, 0.7);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    padding: 8px 12px;
    margin-top: 10px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.74rem;
    line-height: 1.55;
    color: #cbd5e1;
    display: block;
}
.ia-audit-highlight.cyan {
    border-color: rgba(56, 189, 248, 0.2);
    color: #38bdf8;
}
.ia-audit-highlight.amber {
    border-color: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
}
.ia-audit-highlight.emerald {
    border-color: rgba(16, 185, 129, 0.2);
    color: #34d399;
}
.ia-audit-footer {
    text-align: center;
    margin-top: 20px;
    padding: 14px 22px;
    background: linear-gradient(145deg, rgba(14, 18, 28, 0.7) 0%, rgba(9, 12, 19, 0.85) 100%);
    border: 1px dashed rgba(148, 163, 184, 0.22);
    border-radius: 12px;
    font-size: 0.80rem;
    color: #94a3b8;
    font-family: 'Inter', sans-serif;
    box-shadow: 0 4px 18px rgba(0, 0, 0, 0.35);
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
    background: var(--sl-bg-surface) !important;
    border: 1px solid var(--sl-border-default) !important;
    border-radius: var(--sl-radius-lg) !important;
    box-shadow: var(--sl-shadow-card) !important;
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
.ia-table-scroll::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.20) !important; border-radius: 999px !important; }
.ia-table-scroll::-webkit-scrollbar-thumb:hover { background: rgba(148, 163, 184, 0.36) !important; }

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
    background: var(--sl-bg-elevated) !important;
    position: sticky;
    top: 0;
    z-index: 5;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
    width: 100% !important;
}

table.ia-custom-table thead th {
    padding: 13px 16px;
    font-family: var(--sl-font-body);
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--sl-text-secondary);
    border-bottom: 1px solid var(--sl-border-default);
    white-space: nowrap;
    user-select: none;
    background: var(--sl-bg-elevated) !important;
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
    background: var(--sl-accent-soft) !important;
}

table.ia-custom-table tbody td {
    padding: 11px 16px;
    vertical-align: middle;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    box-sizing: border-box !important;
}
table.ia-custom-table.ia-table-auto {
    table-layout: auto !important;
}
table.ia-custom-table tbody td.ia-cell-wrap {
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
    word-break: break-word;
    line-height: 1.45;
    min-width: 180px;
}
table.ia-custom-table tbody tr + tr td {
    border-top: 1px solid var(--sl-border-subtle);
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
    width: 28px;
    height: 28px;
    border-radius: 8px;
    background: rgba(56, 189, 248, 0.08);
    border: 1px solid rgba(56, 189, 248, 0.24);
    color: #7dd3fc !important;
    font-family: 'Inter', sans-serif;
    font-size: 0.88rem;
    font-weight: 700;
    line-height: 1;
    text-decoration: none !important;
    cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease, transform 0.15s ease;
}
.ia-eye-link:hover {
    background: rgba(56, 189, 248, 0.20);
    border-color: rgba(56, 189, 248, 0.50);
    color: #e0f2fe !important;
    transform: translateY(-1px);
}
</style>
        """,
        unsafe_allow_html=True,
    )


# ═══════════════════════════════════════════════════════════════════
# UTILIDADES
# ═══════════════════════════════════════════════════════════════════

def _md_a_html(texto: str) -> str:
    """Markdown del agente → HTML seguro para la burbuja del chat."""
    import html as _html
    import re as _re
    t = _html.escape(texto)
    t = _re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noopener noreferrer" '
        r'style="color:#38bdf8; text-decoration:underline;">\1</a>',
        t,
    )
    t = _re.sub(r"^#{2,4}\s+(.+)$", r'<div style="font-weight:700; color:#e2e8f0; margin:6px 0 2px 0;">\1</div>', t, flags=_re.M)
    t = _re.sub(r"^&gt;\s*(.+)$", r'<div style="border-left:2px solid #38bdf8; padding-left:10px; color:#94a3b8; margin:4px 0;">\1</div>', t, flags=_re.M)
    # Convertir viñetas (-, *, •) ANTES de cursiva para evitar colisión con "* item *cursiva*"
    t = _re.sub(r"^[-*•]\s+(.+)$", r"• \1", t, flags=_re.M)
    t = _re.sub(r"`([^`]+)`", r'<code style="background:rgba(56,189,248,0.10); color:#7dd3fc; padding:1px 5px; border-radius:4px; font-size:0.84em;">\1</code>', t)
    t = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = _re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", t)
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

    _COLS_TEXTO_LIBRE = (
        "titular", "evento", "descripción", "descripcion", "detalle",
        "motivo", "métrica", "metrica", "valor", "fuente", "sector", "origen",
    )

    def _col_align(col_name: str) -> str:
        name_l = col_name.lower()
        if any(k in name_l for k in ("empresa", "nombre") + _COLS_TEXTO_LIBRE):
            return "text-left"
        if any(k in name_l for k in ["fecha", "tipo", "señal", "senal", "predicción", "prediccion", "resultado", "horizonte", "score", "#", "pos", "ticker", "estado", "sentimiento"]):
            return "text-center"
        return "text-right"

    def _es_col_wrap(col_name: str) -> bool:
        name_l = col_name.lower()
        return any(k in name_l for k in ("titular", "evento", "detalle", "motivo", "descripción", "descripcion", "métrica", "metrica", "valor"))

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

        # Restringir píldoras exclusivamente a columnas de señal/estado/tipo/sentimiento
        # (nunca machacar titulares de noticias, eventos, detalles ni motivos)
        es_col_estado = any(
            k in col_l for k in ("señal", "senal", "predicción", "prediccion", "resultado", "tipo", "estado", "sentimiento")
        )
        if es_col_estado and len(s) <= 26:
            if "alcista" in s_lower:
                return '<span class="ia-pill alcista">ALCISTA</span>'
            if "bajista" in s_lower:
                return '<span class="ia-pill bajista">BAJISTA</span>'
            if "lateral" in s_lower:
                return '<span class="ia-pill lateral">LATERAL</span>'
            if "invalidada" in s_lower:
                return '<span class="ia-pill invalidada">Invalidada</span>'
            if "acierto" in s_lower:
                return '<span class="ia-pill acierto">Acierto</span>'
            if "fallo" in s_lower:
                return '<span class="ia-pill fallo">Fallo</span>'
            if "dividendo" in s_lower:
                return '<span class="ia-pill dividendo">Dividendo</span>'
            if "resultado" in s_lower or "earnings" in s_lower:
                return '<span class="ia-pill resultados">Resultados</span>'
            if s_lower in ("positiva", "positivo"):
                return f'<span class="ia-pill acierto">{_html.escape(s)}</span>'
            if s_lower in ("crítica", "critica", "negativa", "negativo"):
                return f'<span class="ia-pill fallo">{_html.escape(s)}</span>'
            if s_lower in ("neutral", "info", "informativa"):
                return f'<span class="ia-pill lateral">{_html.escape(s)}</span>'

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
            return f'<span class="ia-mono-val" style="color:#38bdf8; font-weight:600;">{_html.escape(s)}</span>'
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
            f'<td class="{aligns[i]}{" ia-cell-wrap" if (not col_widths and _es_col_wrap(str(col))) else ""}" '
            f'style="{_col_w_style(str(col))}">{_format_cell(row[col], str(col))}</td>'
            for i, col in enumerate(cols)
        )
        tr_rows.append(f'<tr>{td_cells}</tr>')

    rows_html = "".join(tr_rows)

    wrapper_cls = "ia-table-wrapper ia-table-compact" if compact else "ia-table-wrapper"
    table_cls = "ia-custom-table" if col_widths else "ia-custom-table ia-table-auto"
    return f"""<div class="{wrapper_cls}"><div class="ia-table-scroll" style="max-height: {max_height}px;"><table class="{table_cls}">{colgroup_html}<thead><tr>{th_cells}</tr></thead><tbody>{rows_html}</tbody></table></div></div>"""


_TOOL_LABELS = {
    "ficha_empresa": "Ficha empresa",
    "riesgo_empresa": "Perfil riesgo",
    "resumen_sector": "Sectores BME",
    "mis_alertas": "Alertas",
    "comparar_empresas": "Comparativa",
    "noticias_empresa": "Noticias",
    "buscar_actualidad_web": "Búsqueda web",
    "mi_cartera": "Mi Cartera",
    "mi_watchlist": "Watchlist",
    "top_momentum": "Momentum",
    "senales": "Radar",
    "sobreventa": "Sobreventa",
    "sobrecompra": "Sobrecompra",
    "value_per_dividendo": "Value",
    "top_dividendos": "Dividendos",
    "proximos_eventos": "Eventos",
    "top_ranking": "Ranking",
    "mas_volatiles": "Volatilidad",
    "top_ganadoras_semana": "Ganadoras 1S",
    "top_perdedoras_semana": "Perdedoras 1S",
    "top_ganadoras_mes": "Ganadoras 1M",
    "top_perdedoras_mes": "Perdedoras 1M",
    "metricas_predicciones": "Auditoría IA",
    "proyeccion_empresa": "Proyección",
    "motor_local": "Motor local",
}


def _render_chat_item(item: dict, is_last: bool = False, msg_idx: int = 0) -> None:
    """Renderiza un par pregunta/respuesta del hilo de chat con fuentes y seguimientos clicables."""
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

    tools = r.get("herramientas") or []
    if tools:
        pills = "".join(
            f'<span style="font-family:\'JetBrains Mono\',monospace; font-size:0.64rem; '
            f'color:#7dd3fc; background:rgba(56,189,248,0.09); border:1px solid rgba(56,189,248,0.22); '
            f'border-radius:999px; padding:2px 8px;">{_html.escape(_TOOL_LABELS.get(t, t))}</span>'
            for t in tools[:5]
        )
        tools_html = f'<div style="display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-bottom:10px;"><span style="font-size:0.66rem; color:#64748b; text-transform:uppercase; letter-spacing:0.06em;">Fuentes:</span>{pills}</div>'
    elif not es_local:
        tools_html = '<div style="display:flex; align-items:center; gap:6px; margin-bottom:10px;"><span style="font-size:0.66rem; color:#64748b; text-transform:uppercase; letter-spacing:0.06em;">Modo:</span><span style="font-family:\'JetBrains Mono\',monospace; font-size:0.64rem; color:#94a3b8; background:rgba(148,163,184,0.08); border:1px solid rgba(148,163,184,0.18); border-radius:999px; padding:2px 8px;">Conocimiento general</span></div>'
    else:
        tools_html = ""

    st.markdown(
        f"""
        <div class="ia-modal-user-card">
            <div class="ia-modal-user-tag">TÚ</div>
            <div class="ia-modal-user-query">{_html.escape(q)}</div>
        </div>
        <div class="ia-modal-bot-card">
            <div class="ia-modal-bot-head">
                <div class="ia-modal-bot-title">{_html.escape(str(r.get('titulo', 'Respuesta')))}</div>
                <span class="ia-modal-badge {badge_cls}">{_html.escape(str(m_tag))}</span>
            </div>
            {tools_html}
            <div class="ia-modal-bot-text">{_md_a_html(r.get('texto', ''))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if r.get("df") is not None and not r["df"].empty:
        st.html(_render_tabla_html(r["df"], max_height=320))

    # Sugerencias de seguimiento clicables solo tras el último mensaje del hilo
    sugs = r.get("sugerencias") or []
    if is_last and sugs:
        st.markdown(
            '<div style="font-family:\'Inter\',sans-serif; font-size:0.70rem; color:#64748b; '
            'text-transform:uppercase; letter-spacing:0.07em; margin:8px 0 4px 2px;">Continuar análisis</div>',
            unsafe_allow_html=True,
        )
        cols_fu = st.columns(len(sugs[:3]))
        for j, sug_txt in enumerate(sugs[:3]):
            if cols_fu[j].button(sug_txt, key=f"ia_followup_{msg_idx}_{j}", use_container_width=True):
                st.session_state["ia_pending"] = sug_txt
                st.rerun()


def _conclusion_horizonte(nombre: str, ind: dict, s_hz: dict, hz_label: str) -> str:
    """Texto descriptivo del diagnóstico para un horizonte concreto (HTML seguro)."""
    import html as _html
    icono = _ICONOS_SENAL.get(s_hz["senal"], "·")
    vol_d_pct = float(ind["vol_d"]) * 100.0
    vol_anual = float(ind["vol_d"]) * (252 ** 0.5) * 100.0
    return (
        f"{icono} <b>{_html.escape(str(nombre))}</b> presenta señal <b>{_html.escape(str(s_hz['senal']))}</b> a {_html.escape(str(hz_label).lower())} "
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
        fechas_hist = [pd.Timestamp(d).strftime("%d %b %Y") for d in sub.index]
        labels = fechas_hist

        def _pad_hist(serie: pd.Series) -> list:
            return [None if pd.isna(v) else round(float(v), 4) for v in serie]

        ultimo = float(closes.iloc[-1])
        extra_series = [
            {"label": "SMA20", "data": _pad_hist(sma20),
             "color": "#f59e0b", "dash": [4, 4], "width": 1.0},
            {"label": "SMA50", "data": _pad_hist(sma50),
             "color": "#8b5cf6", "dash": [4, 4], "width": 1.0},
        ]

        color_linea = "#10B981" if ultimo >= float(closes.iloc[0]) else "#EF4444"

        render_stock_chart(
            labels=labels,
            prices=_pad_hist(closes),
            last_close=ultimo,
            line_color=color_linea,
            currency="EUR",
            ticker=ticker,
            title=f"{nombre} — Evolución Técnica ({hz_label})",
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
        icono_senal = _ICONOS_SENAL.get(s_hz["senal"], "·")
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
                <div class="ia-diag-metrics-grid" style="grid-template-columns: 1fr 1fr;">
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
                    <div class="ia-diag-desc-title">Escenario {s_hz['senal'].capitalize()}</div>
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


def _cerrar_inspeccion_radar() -> None:
    st.session_state.pop("ia_eye_item", None)


@st.dialog("Inspección de Activo — Radar Predictivo", width="large", on_dismiss=_cerrar_inspeccion_radar)
def _dialog_inspeccion_radar(ticker: str, nombre: str, hz_sel: str,
                             tickers: tuple, tilts: tuple) -> None:
    """Popup de gran formato con la proyección algorítmica y el diagnóstico al horizonte filtrado."""
    hz_key = _HZ_MAP.get(hz_sel, "1S")
    hz_label = _HZ_LABELS.get(hz_sel, hz_sel)

    # Reutilizar exactamente la misma clave de caché de precios que signal_engine.calcular_radar
    batch_symbols = list(tickers)
    if signal_engine.TICKER_BENCHMARK not in batch_symbols:
        batch_symbols.append(signal_engine.TICKER_BENCHMARK)
    df_batch = get_price_batch(tuple(batch_symbols))
    sub = signal_engine.obtener_serie_ticker(df_batch, ticker)
    if sub is None or sub.empty:
        st.warning(f"No hay datos suficientes para {nombre} en este momento.")
        return
    df_ibex = signal_engine.obtener_serie_ticker(df_batch, signal_engine.TICKER_BENCHMARK)
    marea = signal_engine.calcular_marea_mercado(df_ibex)
    ticker2tilt = dict(zip(tickers, tilts))
    tilt_val = ticker2tilt.get(ticker, 0.0)
    # Coherente al 100% con calcular_radar (sin sentimiento RSS en vivo ni descarga extra)
    analisis = signal_engine.calcular_senales_ticker(
        sub, tilt_val, marea=marea, ticker="", nombre=""
    )
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

    ultimo_p_txt = f"{ultimo_p:.4f} €" if 0 < abs(ultimo_p) < 0.10 else f"{ultimo_p:.2f} €"

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
                    <span class="ia-radar-stat-val">{ultimo_p_txt}</span>
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
        <div class="ia-section-header sl-page-header">
            <h1 class="sl-page-title">Agente de IA</h1>
            <div class="sl-page-subtitle">Asistente conversacional y análisis fundamental-técnico</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Estado del modelo seleccionado ───────────────────────────────
    st.session_state.setdefault("ia_modelo_sel", llm_agent.get_default_modelo())
    if st.session_state["ia_modelo_sel"] not in llm_agent.MODELOS_DISPONIBLES:
        st.session_state["ia_modelo_sel"] = llm_agent.get_default_modelo()

    # ── Prompt bar unificado (Form para Enter/Click sin envío al cambiar de modelo) ──
    with st.container(key="ia_prompt_box"):
        c_form, c_badge = st.columns([7.65, 2.35], vertical_alignment="center")
        with c_form:
            with st.form("ia_prompt_form", clear_on_submit=True, border=False):
                c_input, c_send = st.columns([12.0, 0.9], vertical_alignment="center")
                texto_prompt = c_input.text_input(
                    "Consulta al Agente de IA",
                    key="ia_prompt_input",
                    label_visibility="collapsed",
                    placeholder="Pregunta a Gemini o consulta tus empresas…",
                )
                enviado = c_send.form_submit_button("➤")
                if enviado and (texto_prompt or "").strip():
                    st.session_state["ia_pending"] = texto_prompt.strip()

        if llm_agent.disponible():
            def _formato_modelo_dropdown(m: str) -> str:
                if llm_agent.es_cuota_agotada(m):
                    return f"{m} (Agotado)"
                return m

            c_badge.selectbox(
                "Modelo",
                llm_agent.MODELOS_DISPONIBLES,
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
        estado = uso_hoy["estado"]  # "normal", "aviso", "limite"

        def _fmt_tok(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
            if n >= 1_000:
                return f"{n / 1_000:.1f}k".replace(".0k", "k")
            return str(n)

        tok_str = f"{_fmt_tok(tok_act)} tokens hoy"
        req_str = f"{req_act} / {req_lim:,} peticiones".replace(",", ".")
        fill_width = f"{max(pct_clamped, 0.8):.2f}" if pct_clamped > 0 else "0"

        agotada = uso_hoy.get("agotada", False)
        titulo_uso = "Cuota diaria agotada" if agotada else "Uso diario"
        if agotada:
            alts_disp = llm_agent.obtener_modelos_alternativos(modelo_activo)
            if alts_disp:
                alts_fmt = " o ".join(
                    f"<b>{m}</b>" + (f" ({llm_agent.LIMITES_DIARIOS[m]['requests']} consultas/día)" if llm_agent.LIMITES_DIARIOS.get(m, {}).get("requests", 0) >= 100 else "")
                    for m in alts_disp[:3]
                )
                alts_html = f'<div style="color: #67e8f9; font-weight: 600;">Disponibles: {alts_fmt}</div>'
            else:
                alts_html = '<div style="color: #94a3b8; font-weight: 600;">Todos los modelos en la nube están agotados hoy (usando motor local).</div>'
            alerta_agotada_html = (
                f'<div style="margin-top: 10px; font-family: \'Inter\', sans-serif; font-size: 0.74rem; color: #fca5a5; background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.25); border-radius: 8px; padding: 8px 12px; display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;">'
                f'<div>Has alcanzado el límite diario de Google para <b>{modelo_activo}</b> ({req_lim}/{req_lim}). Las respuestas se generan mediante el motor local.</div>'
                f'{alts_html}'
                f'</div>'
            )
        else:
            alerta_agotada_html = ""

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
        etiqueta_spinner = modelo_activo if llm_agent.disponible() else "Motor Local"
        with st.spinner(f"Consultando con {etiqueta_spinner}…"):
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
                f'Conversación ({len(hist_chat)} mensaje(s))</div>',
                unsafe_allow_html=True,
            )
        with c_limpiar:
            if st.button("Limpiar conversación", key="ia_chat_clear",
                         use_container_width=True):
                st.session_state.pop("ia_chat_hist", None)
                st.session_state.pop("ia_gemini_hist", None)
                st.session_state.pop("ia_ultimo_ticker", None)
                st.rerun()

        for idx_m, item in enumerate(hist_chat):
            _render_chat_item(item, is_last=(idx_m == len(hist_chat) - 1), msg_idx=idx_m)

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
            <h2 class="sl-section-title">Radar Predictivo</h2>
            <div class="ia-section-sub sl-page-subtitle">Señales algorítmicas multitemporales y proyección técnica</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if radar.empty:
        st.markdown(
            """
            <div class="ia-empty-shell">
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

    # ── Selector desplegable de horizonte y filtros de convicción ──
    opciones_filtro = ["1 Día", "3 Días", "1 Semana", "1 Mes"]
    mapa_filtro_a_hz = {
        "1 Día": "1D",
        "3 Días": "3D",
        "1 Semana": "7D",
        "1 Mes": "1M",
    }
    if st.session_state.get("ia_radar_hz_select") not in opciones_filtro:
        st.session_state["ia_radar_hz_select"] = "1 Semana"

    opciones_conviccion = [
        "Todas las empresas",
        "🟢 Solo Alcistas",
        "🔴 Solo Bajistas",
        "⚡ Divergencia Táctica (Corto vs Medio)",
    ]

    # Pre-cálculo para el resumen de mercado del horizonte activo
    hz_current_sel = st.session_state["ia_radar_hz_select"]
    hz_pre_sel = mapa_filtro_a_hz.get(hz_current_sel, "7D")
    hz_pre_key = _HZ_MAP.get(hz_pre_sel, "1S")

    total_universo = len(radar)
    col_pre_hz = f"senal_{hz_pre_key}"
    if col_pre_hz in radar.columns:
        n_alc = int((radar[col_pre_hz] == "ALCISTA").sum())
        n_baj = int((radar[col_pre_hz] == "BAJISTA").sum())
        n_lat = int((radar[col_pre_hz] == "LATERAL").sum())
    else:
        n_alc = n_baj = n_lat = 0

    # ── Tira Resumen del Mercado (4 KPIs Simétricos) ──
    st.html(f"""
    <div class="ia-hist-kpi-row" style="margin-bottom: 12px;">
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Universo Analizado</span>
            </div>
            <div class="ia-hist-kpi-value cyan">{total_universo}</div>
            <div class="ia-hist-kpi-caption">Empresas en cobertura BME</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Señales Alcistas</span>
            </div>
            <div class="ia-hist-kpi-value pos">{n_alc}</div>
            <div class="ia-hist-kpi-caption">Proyección positiva ({hz_current_sel})</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Señales Bajistas</span>
            </div>
            <div class="ia-hist-kpi-value neg">{n_baj}</div>
            <div class="ia-hist-kpi-caption">Proyección negativa ({hz_current_sel})</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Señales Laterales</span>
            </div>
            <div class="ia-hist-kpi-value amber">{n_lat}</div>
            <div class="ia-hist-kpi-caption">Rango acotado ({hz_current_sel})</div>
        </div>
    </div>
    """)

    # ── Barra de Control Simétrica (3 Columnas Alineadas) ──
    c_hz, c_conv, c_btn = st.columns([1.5, 1.8, 1.0], vertical_alignment="bottom")
    with c_hz:
        filtro_elegido = st.selectbox(
            "HORIZONTE TEMPORAL",
            opciones_filtro,
            key="ia_radar_hz_select",
        )
    with c_conv:
        conv_elegida = st.selectbox(
            "FILTRAR SEÑALES",
            opciones_conviccion,
            index=0,
            key="ia_radar_conv_select",
            format_func=lambda x: x.replace("🟢 ", "").replace("🔴 ", "").replace("⚡ ", ""),
        )
    with c_btn:
        if st.button(
            "Refrescar",
            key="btn_trigger_refrescar_radar",
            use_container_width=True,
            help="Descarga precios actualizados y recalcula las señales del radar",
        ):
            try:
                get_price_batch.clear()
                signal_engine.calcular_radar.clear()
                _limpiar_caches_ia_pred()
            except Exception:
                pass
            st.toast("Precios y señales del radar actualizados.")
            st.rerun()

    hz_sel = mapa_filtro_a_hz.get(filtro_elegido, "7D")
    hz_key = _HZ_MAP.get(hz_sel, "1S")

    # ── Banner Institucional de Advertencia de Laboratorio ──
    st.html("""
    <div class="ia-lab-banner">
        <div class="ia-lab-banner-head">
            <span>LABORATORIO EXPERIMENTAL — SEÑALES ALGORÍTMICAS Y FORWARD-TESTING CONGELADO</span>
        </div>
        <div class="ia-lab-banner-body">
            Esta vista se mantiene exclusivamente con fines de investigación cuantitativa, trazabilidad y forward-testing congelado.
            Como concluye la auditoría institucional, las señales direccionales <b>no son operables</b> debido a la fricción real de comisiones, horquillas e impuestos (&ge; 20 pb por operación). <b>No constituyen recomendación ni asesoramiento financiero.</b>
        </div>
    </div>
    """)

    # ── Construir tabla con columnas del horizonte seleccionado ───────
    df = radar.copy()
    if conv_elegida == "🟢 Solo Alcistas":
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
        return _ICONOS_SENAL.get(senal, "·")

    hz_label = _HZ_LABELS.get(hz_sel, hz_sel)
    conv_label_clean = conv_elegida.replace("🟢 ", "").replace("🔴 ", "").replace("⚡ ", "")

    if df.empty:
        st.markdown(
            f"""
            <div class="ia-hist-empty-notice" style="margin: 20px 0;">
                <div class="ia-hist-empty-title">Sin resultados para {conv_label_clean}</div>
                <div class="ia-hist-empty-desc">Ningún activo cumple el criterio seleccionado en el horizonte <b>{filtro_elegido}</b>. Cambia de horizonte o selecciona <b>Todas las empresas</b>.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        disp = pd.DataFrame()
    else:
        def _fmt_precio_radar(v) -> str:
            if pd.isna(v):
                return "—"
            vf = float(v)
            return f"{vf:.4f} €" if 0 < abs(vf) < 0.10 else f"{vf:.2f} €"

        disp = pd.DataFrame({
            "Empresa": df["empresa"],
            "Precio": df["precio"].map(_fmt_precio_radar),
            "RSI": df["rsi"].map(lambda v: f"{v:.0f}" if pd.notna(v) else "—"),
            "Proyección (Exp.)": df[f"pct_{hz_key}"].map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "—"),
            "Predicción (Exp.)": [f"{_senal_icon(s)} {s}" for s in df[f"senal_{hz_key}"]],
        })

        col_widths_radar = {
            "Empresa": "30%",
            "Precio": "16%",
            "RSI": "12%",
            "Proyección (Exp.)": "20%",
            "Predicción (Exp.)": "22%",
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

        # ── Barra de inspección en sesión (sin recargar la página ni perder el chat) ──
        df_insp_src = df if not df.empty else radar
        tickers_insp = list(df_insp_src["ticker"])
        mapa_tk_nom = dict(zip(df_insp_src["ticker"], df_insp_src["empresa"]))
        if st.session_state.get("ia_radar_insp_ticker") not in tickers_insp:
            st.session_state["ia_radar_insp_ticker"] = tickers_insp[0]

        st.markdown("<div style='height: 6px;'></div>", unsafe_allow_html=True)
        c_insp_sel, c_insp_btn = st.columns([3.2, 1.1], vertical_alignment="bottom")
        with c_insp_sel:
            tk_elegido = st.selectbox(
                "INSPECCIONAR DIAGNÓSTICO DE ACTIVO",
                tickers_insp,
                key="ia_radar_insp_ticker",
                format_func=lambda t: f"{mapa_tk_nom.get(t, t)} ({t})",
            )
        with c_insp_btn:
            if st.button(
                f"Ver diagnóstico ({hz_label}) ↗",
                key="btn_ia_radar_inspeccionar",
                use_container_width=True,
            ):
                st.session_state["ia_eye_item"] = {"ticker": tk_elegido, "hz": hz_sel}

    # ── Pie del Radar: metadatos y estado del universo ──────────────
    st.markdown(
        f"""
        <div style="font-family: 'Inter', sans-serif; font-size: 0.74rem; color: #64748b; padding: 6px 4px 10px 4px; display: flex; justify-content: space-between; align-items: center; border-top: 1px solid rgba(255, 255, 255, 0.04); margin-top: 6px;">
            <span>Modelo oficial <b style="color: #94a3b8;">{MOTOR_VERSION}</b> · <b>{len(disp)}</b> empresas visualizadas ({filtro_elegido})</span>
            <span>Universo BME Continuo + IBEX 35 · Emisión diaria consolidada</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Popup de inspección rápida (en sesión o por deep-link ?ia_eye=) ───
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

    eye_item = st.session_state.get("ia_eye_item")
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
    if os.environ.get("STOCKLAB_DEV") != "1":
        return
    st.markdown(
        """
        <div style="background: rgba(59, 130, 246, 0.08); border: 1px solid rgba(59, 130, 246, 0.25);
                    border-radius: 10px; padding: 12px 14px; margin-bottom: 16px; font-family: 'Inter', sans-serif;">
            <div style="color: #60a5fa; font-weight: 700; font-size: 0.88rem; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
                <span>Archivado Histórico (Soft Delete)</span>
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
            f"Archivar ({n_a_borrar})",
            type="primary",
            use_container_width=True,
            disabled=not confirmado,
            key="btn_confirmar_reset_hist",
        ):
            hz_param = hz_key if borrar_solo_hz else None
            archivadas = db_pred.limpiar_resueltas(hz_param)
            _limpiar_caches_ia_pred()
            st.toast(f"Se han archivado {archivadas} predicciones resueltas (soft-delete).")
            st.rerun()



def _render_tabla_referencias_ingenuas_html(tabla_c14: Optional[Dict], es_auditoria: bool = False) -> str:
    """Genera la tabla comparativa institucional de las 5 referencias ingenuas vs el Motor [C-14]."""
    if not tabla_c14 or not isinstance(tabla_c14, dict) or not tabla_c14.get("tabla"):
        return ""
    import html as _html_local
    et = _html_local.escape(str(tabla_c14.get("etiqueta_muestra") or ""))
    mejor_3c = tabla_c14.get("mejor_estrategia_3clases", "motor")
    mejor_sig = tabla_c14.get("mejor_estrategia_signo", "motor")
    supera_3c = tabla_c14.get("motor_supera_todas_3clases", False)
    supera_sig = tabla_c14.get("motor_supera_todas_signo", False)

    rows_html = []
    for r in tabla_c14["tabla"]:
        cod = r.get("codigo", "")
        nom = r.get("nombre", cod)
        is_motor = (cod == "motor")
        row_style = "background: rgba(56, 189, 248, 0.07); font-weight: 700;" if is_motor else ""
        nom_html = f"<b style='color:#38bdf8;'>{nom}</b> <span class='tag-badge tag-cyan' style='font-size:0.60rem;'>OFICIAL</span>" if is_motor else f"<b>{nom}</b>"

        # 3 clases v1
        t_v1 = r.get("tasa_acierto_3clases_v1")
        d_v1 = r.get("delta_vs_motor_3clases_v1_pp")
        t_v1_txt = f"{t_v1:.1f}%" if t_v1 is not None else "s/d"
        if is_motor:
            d_v1_txt = "<span style='color:#94a3b8; font-size:0.75rem;'>base</span>"
        elif d_v1 is not None:
            color_d = "#34d399" if d_v1 > 0 else ("#f87171" if d_v1 < 0 else "#94a3b8")
            d_v1_txt = f"<span style='color:{color_d}; font-size:0.75rem;'>{d_v1:+.2f} pp</span>"
        else:
            d_v1_txt = "-"

        # 3 clases v2
        t_v2 = r.get("tasa_acierto_3clases_v2")
        d_v2 = r.get("delta_vs_motor_3clases_v2_pp")
        t_v2_txt = f"{t_v2:.1f}%" if t_v2 is not None else "s/d"
        if is_motor:
            d_v2_txt = "<span style='color:#94a3b8; font-size:0.75rem;'>base</span>"
        elif d_v2 is not None:
            color_d = "#34d399" if d_v2 > 0 else ("#f87171" if d_v2 < 0 else "#94a3b8")
            d_v2_txt = f"<span style='color:{color_d}; font-size:0.75rem;'>{d_v2:+.2f} pp</span>"
        else:
            d_v2_txt = "-"

        # Signo
        t_sig = r.get("tasa_acierto_signo")
        d_sig = r.get("delta_vs_motor_signo_pp")
        nd = int(r.get("n_direccionales") or 0)
        t_sig_txt = f"{t_sig:.1f}%" if t_sig is not None else "N/A"
        if is_motor:
            d_sig_txt = "<span style='color:#94a3b8; font-size:0.75rem;'>base</span>"
        elif d_sig is not None:
            color_d = "#34d399" if d_sig > 0 else ("#f87171" if d_sig < 0 else "#94a3b8")
            d_sig_txt = f"<span style='color:{color_d}; font-size:0.75rem;'>{d_sig:+.2f} pp</span>"
        else:
            d_sig_txt = "<span style='color:#64748b; font-size:0.75rem;'>N/A</span>"

        # Inferencia IC 95%
        inf = r.get("inferencia_signo") if (r.get("inferencia_signo") and nd > 0) else r.get("inferencia_3clases") or {}
        if inf.get("ic95_inf") is not None and inf.get("ic95_sup") is not None:
            ic_txt = f"[{inf['ic95_inf']:.1f}%, {inf['ic95_sup']:.1f}%] &plusmn;{inf.get('ic95_semiancho_pct', 0.0):.1f} pp"
        else:
            ic_txt = "<span style='color:#64748b;'>s/d</span>"

        rows_html.append(f"""
        <tr style="{row_style}">
            <td style="font-family:'Inter', sans-serif;">{nom_html}</td>
            <td style="font-family:'JetBrains Mono', monospace;"><b>{t_v1_txt}</b> {d_v1_txt}</td>
            <td style="font-family:'JetBrains Mono', monospace;"><b>{t_v2_txt}</b> {d_v2_txt}</td>
            <td style="font-family:'JetBrains Mono', monospace;"><b>{t_sig_txt}</b> {d_sig_txt}</td>
            <td style="font-family:'JetBrains Mono', monospace; text-align:center;">{nd if nd > 0 else '0 (N/A)'}</td>
            <td style="font-family:'JetBrains Mono', monospace; font-size:0.76rem; color:#cbd5e1;">{ic_txt}</td>
        </tr>
        """)

    tbody = "".join(rows_html)

    sup_sig_txt = "&#10003; Supera a todas las referencias" if supera_sig else "No supera a todas las referencias"
    sup_3c_txt = "&#10003; Supera a todas" if supera_3c else "Prevalencia de siempre lateral"

    # Construir el Veredicto C-14 dinámicamente con las cifras reales de tabla_c14 (cero hardcodeo)
    filas_c14 = tabla_c14.get("tabla") or []
    por_codigo = {r.get("codigo", ""): r for r in filas_c14}
    r_motor = por_codigo.get("motor") or {}
    t_sig_motor = r_motor.get("tasa_acierto_signo")
    t_sig_motor_txt = f"{t_sig_motor:.1f}%" if t_sig_motor is not None else "s/d"

    nombres_cortos = {
        "siempre_alcista": "Alcista",
        "siempre_bajista": "Bajista",
        "continuacion_ultimo_movimiento": "Continuacion",
        "direccion_ibex": "IBEX",
    }
    deltas_signo_parts = []
    if t_sig_motor is not None:
        comp_signo = []
        for r in filas_c14:
            cod = r.get("codigo", "")
            if cod == "motor":
                continue
            t_ref = r.get("tasa_acierto_signo")
            if t_ref is not None:
                diff_pp = float(t_sig_motor) - float(t_ref)
                lbl = nombres_cortos.get(cod, r.get("nombre", cod))
                comp_signo.append((diff_pp, lbl))
        comp_signo.sort(key=lambda x: x[0])
        deltas_signo_parts = [f"{d:+.1f} pp vs {lbl}" for d, lbl in comp_signo]
    deltas_signo_txt = f" ({', '.join(deltas_signo_parts)})" if deltas_signo_parts else ""

    r_mejor_3c = por_codigo.get(mejor_3c) or por_codigo.get("siempre_lateral") or r_motor
    nom_mejor_3c = _html_local.escape(str(r_mejor_3c.get("nombre") or mejor_3c))
    v1_m3 = r_mejor_3c.get("tasa_acierto_3clases_v1")
    v2_m3 = r_mejor_3c.get("tasa_acierto_3clases_v2")
    v1_m3_txt = f"{v1_m3:.1f}%" if v1_m3 is not None else "s/d"
    v2_m3_txt = f"{v2_m3:.1f}%" if v2_m3 is not None else "s/d"
    if supera_3c:
        sup_3c_footer = sup_3c_txt
        veredicto_3c = f"En 3 Clases el Motor <b>{sup_3c_txt}</b> ({v1_m3_txt} v1, {v2_m3_txt} v2)."
    elif mejor_3c == "siempre_lateral":
        sup_3c_footer = sup_3c_txt
        veredicto_3c = (
            f"En 3 Clases rige la <b>{sup_3c_txt}</b> ({v1_m3_txt} v1, {v2_m3_txt} v2) "
            f"propia del contexto de consolidacion del mercado."
        )
    else:
        sup_3c_footer = f"Lidera {nom_mejor_3c}"
        veredicto_3c = f"En 3 Clases lidera <b>{nom_mejor_3c}</b> ({v1_m3_txt} v1, {v2_m3_txt} v2)."

    if es_auditoria:
        card_html = f"""
        <div class="ia-audit-card full-width">
            <div class="ia-audit-head">
                <div class="ia-audit-title-wrap">
                    <span class="ia-audit-icon">[C-14]</span>
                    <span class="ia-audit-title">Tabla Completa de Referencias Ingenuas: Motor vs 5 Estrategias Base (MEDIDO)</span>
                </div>
                <span class="tag-badge tag-cyan">{et}</span>
            </div>
            <div class="ia-audit-desc">
                Comparativa exhaustiva y simultanea del motor de senales (<b>v5-frozen</b>) contra las 5 referencias ingenuas sin look-ahead sobre exactamente las mismas observaciones resueltas:
                <b>1) Siempre alcista</b>, <b>2) Siempre bajista</b>, <b>3) Siempre lateral</b>, <b>4) Continuacion del ultimo movimiento</b> (inercia del propio activo), y <b>5) Direccion del IBEX</b> (marea del mercado general).
            </div>
            <div class="ia-desc-table-wrap" style="margin-top: 10px;">
                <table class="ia-desc-table" style="width:100%;">
                    <thead>
                        <tr>
                            <th>Estrategia</th>
                            <th>3 Clases v1 (fijo)</th>
                            <th>3 Clases v2 (vol)</th>
                            <th>Acierto Signo</th>
                            <th style="text-align:center;">N Dir</th>
                            <th>IC 95% (C-13 por fecha)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {tbody}
                    </tbody>
                </table>
            </div>
            <div class="ia-audit-highlight cyan" style="margin-top: 10px;">
                <span>Veredicto C-14:</span> En Acierto de Signo el Motor ({t_sig_motor_txt}) <b>{sup_sig_txt}</b>{deltas_signo_txt}. {veredicto_3c}
            </div>
        </div>
        """
    else:
        card_html = f"""
        <div style="background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(51, 65, 85, 0.6); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; font-family: 'Inter', sans-serif;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 8px; flex-wrap:wrap; gap:8px;">
                <div style="display:flex; align-items:center; gap:8px;">
                    <span class="tag-badge tag-cyan" style="font-weight:700;">[C-14]</span>
                    <span style="font-size:0.86rem; font-weight:700; color:#f1f5f9;">Tabla Completa de Referencias Ingenuas: Motor (v5-frozen) vs 5 Estrategias Base</span>
                </div>
                <span class="tag-badge tag-cyan" style="font-size:0.70rem;">{et}</span>
            </div>
            <div class="ia-desc-table-wrap">
                <table class="ia-desc-table" style="width:100%;">
                    <thead>
                        <tr>
                            <th>Estrategia</th>
                            <th>3 Clases v1 (fijo)</th>
                            <th>3 Clases v2 (vol)</th>
                            <th>Acierto Signo</th>
                            <th style="text-align:center;">N Dir</th>
                            <th>IC 95% (C-13 por fecha)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {tbody}
                    </tbody>
                </table>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px; font-size:0.73rem; color:#94a3b8; flex-wrap:wrap; gap:8px;">
                <span>Signo: el Motor <b>{sup_sig_txt}</b> | 3 Clases: <b>{sup_3c_footer}</b></span>
                <span>Datos estrictamente &le; fecha_prediccion (cero look-ahead)</span>
            </div>
        </div>
        """
    return card_html

def _seccion_historial(radar: Optional[pd.DataFrame] = None) -> None:
    """Historial de predicciones con filtro de horizonte y métricas cuantitativas."""

    # ── Título centrado con subtítulo estilizado ─────────────────────
    st.markdown(
        """
        <div class="ia-section-header">
            <h2 class="sl-section-title">Historial Predictivo</h2>
            <div class="ia-section-sub sl-page-subtitle">Auditoría cuantitativa y seguimiento de señales emitidas</div>
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
    if st.session_state.get("ia_hist_hz_select") not in opciones_filtro:
        st.session_state["ia_hist_hz_select"] = "Todos"

    _, col_sel, _ = st.columns([1.65, 1.35, 1.65])
    with col_sel:
        filtro_elegido = st.selectbox(
            "FILTRAR POR HORIZONTE",
            opciones_filtro,
            key="ia_hist_hz_select",
        )
    hz_sel_key = mapa_filtro_a_hz.get(filtro_elegido, "Todos")

    # ── Métricas calculadas con precisión por SQL sin truncamiento (cacheadas 5 min) ────
    try:
        m = _ia_obtener_metricas(hz_sel_key, False)
        resumen_c10 = _ia_obtener_resumen_c10(hz_sel_key, False)
        resumen_c11 = _ia_obtener_resumen_c11(hz_sel_key, False)
        resumen_c12 = _ia_obtener_resumen_c12(hz_sel_key, False)
        df_resueltas = _ia_obtener_resueltas(hz_sel_key, limite=250)
        df_pendientes = _ia_obtener_pendientes(hz_sel_key, solo_vigentes=True, limite=1500)
    except Exception as e:
        st.warning(f"No se pudieron cargar las métricas del historial en este momento: {e}")
        return

    if m.get("total", 0) == 0:
        st.markdown(
            """
            <div class="ia-empty-shell">
                <div class="ia-empty-title">Historial en Fase de Acumulación</div>
                <div class="ia-empty-desc">Las señales generadas hoy ya están registradas. Cuando venzan sus horizontes, el sistema las liquidará contra el precio real y mostrará aquí las métricas de acierto.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    total_senales = m.get("total", 0)
    total_resueltas = m.get("resueltas", 0)
    total_pendientes = len(df_pendientes) if not df_pendientes.empty else m.get("pendientes", 0)
    tasa_acierto = m.get("tasa_acierto_3clases", m.get("tasa_acierto"))
    score_medio = m.get("score_medio")
    n_invalidadas = m.get("invalidadas", 0)

    score_str = f"{score_medio:.0f}/100" if score_medio is not None else "-"
    score_cls = "pos" if (score_medio is not None and score_medio >= 70) else ("amber" if score_medio is not None and score_medio >= 40 else ("neg" if score_medio is not None else "cyan"))

    inv_caption = f" &middot; {n_invalidadas} invalidadas" if n_invalidadas > 0 else ""

    # Conteo dinamico de empresas unicas en seguimiento
    n_empresas_vig = len(df_pendientes["ticker"].unique()) if not df_pendientes.empty else m.get("vigentes", 0)
    caption_vivas = f"{n_empresas_vig} empresas" if hz_sel_key != "Todos" else "4 horizontes"

    # C-09: muestra, Acierto en 3 clases y Acierto de signo sobre el TOTAL SQL de
    # resueltas (obtener_metricas), nunca sobre las 250 filas de la tabla visual.
    import html as _html_c09

    etiqueta_muestra = m.get("etiqueta_muestra") or db_pred.formatear_etiqueta_muestra(0, 0, None, None, hz_sel_key)
    etiqueta_html = _html_c09.escape(etiqueta_muestra)
    n_fechas_emision = int(m.get("n_fechas") or 0)
    pct_base_lateral = m.get("ref_siempre_lateral_pct")
    dist_reales_m = m.get("dist_clases_reales") or {}
    aciertos_lat_base = int(dist_reales_m.get("n_lateral_real") or 0)
    n_valid_res = int(m.get("n_senales") or 0)
    tasa_signo = m.get("tasa_acierto_signo")
    n_dir = int(m.get("n_direccionales") or 0)
    aciertos_signo = int(m.get("aciertos_signo") or 0)
    ref_azar = float(m.get("ref_azar_signo_pct") or 50.0)
    ref_subida = m.get("ref_frecuencia_subida_pct")
    ref_lat_txt = f"{pct_base_lateral:.1f}%" if pct_base_lateral is not None else "s/d"
    ref_sub_txt = f"{ref_subida:.1f}%" if ref_subida is not None else "s/d"
    inf_3c = m.get("inferencia_3clases") or {}
    inf_signo = m.get("inferencia_signo") or {}
    muestra_suficiente = bool(inf_3c.get("muestra_suficiente", False))
    aviso_muestra_txt = inf_3c.get("aviso_muestra") or f"Muestra insuficiente ({n_fechas_emision}/30 fechas de emision)"

    if inf_3c.get("ic95_inf") is not None and inf_3c.get("ic95_sup") is not None:
        ic_3c_txt = f"IC 95% (por fecha): [{inf_3c['ic95_inf']:.1f}%, {inf_3c['ic95_sup']:.1f}%] &middot; &plusmn;{inf_3c.get('ic95_semiancho_pct', 0.0):.1f} pp"
        se_3c_txt = f"N_ef={inf_3c.get('n_efectivo', 0.0):.1f}/{n_fechas_emision} fechas (SE cluster: &plusmn;{inf_3c.get('se_cluster_pct', 0.0):.2f}% vs i.i.d. &plusmn;{inf_3c.get('se_iid_ingenuo_pct', 0.0):.2f}%)"
    else:
        ic_3c_txt = "IC 95%: n/d"
        se_3c_txt = f"{n_fechas_emision} fechas"

    if inf_signo.get("ic95_inf") is not None and inf_signo.get("ic95_sup") is not None:
        ic_sig_txt = f"IC 95% (por fecha): [{inf_signo['ic95_inf']:.1f}%, {inf_signo['ic95_sup']:.1f}%] &middot; &plusmn;{inf_signo.get('ic95_semiancho_pct', 0.0):.1f} pp"
        se_sig_txt = f"N_ef={inf_signo.get('n_efectivo', 0.0):.1f}/{n_fechas_emision} fechas (SE cluster: &plusmn;{inf_signo.get('se_cluster_pct', 0.0):.2f}% vs i.i.d. &plusmn;{inf_signo.get('se_iid_ingenuo_pct', 0.0):.2f}%)"
    else:
        ic_sig_txt = "IC 95%: n/d"
        se_sig_txt = f"{n_fechas_emision} fechas"

    tasa_str = f"{tasa_acierto:.1f}%" if tasa_acierto is not None else "En calculo"
    tasa_cls = (
        "pos" if (tasa_acierto is not None and pct_base_lateral is not None and tasa_acierto > pct_base_lateral)
        else ("amber" if tasa_acierto is not None else "cyan")
    )
    caption_acierto = f"{ic_3c_txt} &middot; {se_3c_txt} &middot; Ref. siempre lateral: {ref_lat_txt}"

    signo_str = f"{tasa_signo:.1f}%" if tasa_signo is not None else "s/d"
    signo_cls = "pos" if (tasa_signo is not None and tasa_signo > ref_azar) else ("amber" if tasa_signo is not None else "cyan")
    caption_signo = f"{ic_sig_txt} &middot; {se_sig_txt} &middot; Ref. azar: {ref_azar:.1f}% &middot; Subidas: {ref_sub_txt}"
    font_size_val = "1.35rem"

    badge_insuficiente = ""
    if not muestra_suficiente:
        badge_insuficiente = f"<div style='margin-top: 4px;'><span class='tag-badge tag-amber' title='Muestra insuficiente ({n_fechas_emision}/30 fechas de emisión)' style='font-size: 0.63rem; padding: 2px 6px; white-space: normal; line-height: 1.25; max-width: 100%;'>Muestra insuficiente ({n_fechas_emision}/30)</span></div>"

    # ── Tarjetas KPI del Historial Predictivo ─────────────────────────
    kpis_html = f"""
    <div class="ia-hist-kpi-row ia-hist-kpi-row-6">
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Total Señales</span>
            </div>
            <div class="ia-hist-kpi-value cyan">{total_senales}</div>
            <div class="ia-hist-kpi-caption">Registradas ({filtro_elegido})</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Resueltas</span>
            </div>
            <div class="ia-hist-kpi-value">{total_resueltas}</div>
            <div class="ia-hist-kpi-caption">Liquidadas vs precio real{inv_caption}</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Acierto en 3 clases</span>
            </div>
            <div class="ia-hist-kpi-value {tasa_cls}" style="font-size: {font_size_val};">{tasa_str}</div>
            {badge_insuficiente}
            <div class="ia-hist-kpi-caption" style="color: #94a3b8;" title="{caption_acierto}">{caption_acierto}</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Acierto de signo (ALC/BAJ)</span>
            </div>
            <div class="ia-hist-kpi-value {signo_cls}" style="font-size: {font_size_val};">{signo_str}</div>
            {badge_insuficiente}
            <div class="ia-hist-kpi-caption" style="color: #94a3b8;" title="{caption_signo}">{caption_signo}</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Score Medio</span>
            </div>
            <div class="ia-hist-kpi-value {score_cls}">{score_str}</div>
            <div class="ia-hist-kpi-caption">Calidad global (dir. + precisión)</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">En Seguimiento</span>
            </div>
            <div class="ia-hist-kpi-value amber">{total_pendientes}</div>
            <div class="ia-hist-kpi-caption">Vivas ({caption_vivas})</div>
        </div>
    </div>
    """
    st.html(kpis_html)

    # C-09: etiqueta de muestra visible y desglose por horizonte (3 clases vs signo)
    por_hz_m = m.get("por_horizonte") or {}
    hz_lineas_c09 = []
    for hz_k in ("1D", "3D", "1S", "1M"):
        e_h = por_hz_m.get(hz_k) or {}
        et_h = _html_c09.escape(
            e_h.get("etiqueta_muestra") or db_pred.formatear_etiqueta_muestra(0, 0, None, None, hz_k)
        )
        t3_h = e_h.get("tasa_acierto_3clases")
        if t3_h is None:
            txt_h = f"<b>{hz_k}</b>: sin resueltas"
        else:
            rl_h = e_h.get("ref_siempre_lateral_pct")
            ts_h = e_h.get("tasa_acierto_signo")
            rs_h = e_h.get("ref_frecuencia_subida_pct")
            nd_h = int(e_h.get("n_direccionales") or 0)
            rl_txt = f"{rl_h:.1f}%" if rl_h is not None else "s/d"
            ts_txt = f"{ts_h:.1f}%" if ts_h is not None else "s/d"
            rs_txt = f"{rs_h:.1f}%" if rs_h is not None else "s/d"
            inf_3c_h = e_h.get("inferencia_3clases") or {}
            inf_s_h = e_h.get("inferencia_signo") or {}
            ic_h_3c = ""
            if inf_3c_h.get("ic95_inf") is not None and inf_3c_h.get("ic95_sup") is not None:
                ic_h_3c = f" &middot; IC 95%: [{inf_3c_h['ic95_inf']:.1f}%, {inf_3c_h['ic95_sup']:.1f}%] &plusmn;{inf_3c_h.get('ic95_semiancho_pct', 0.0):.1f} pp (N_ef={inf_3c_h.get('n_efectivo', 0.0):.1f})"
            ic_h_s = ""
            if inf_s_h.get("ic95_inf") is not None and inf_s_h.get("ic95_sup") is not None:
                ic_h_s = f" &middot; IC 95%: [{inf_s_h['ic95_inf']:.1f}%, {inf_s_h['ic95_sup']:.1f}%] &plusmn;{inf_s_h.get('ic95_semiancho_pct', 0.0):.1f} pp (N_ef={inf_s_h.get('n_efectivo', 0.0):.1f})"
            aviso_h = ""
            if not inf_3c_h.get("muestra_suficiente", False):
                aviso_h = f" <span class='tag-badge tag-amber' style='font-size:0.62rem; padding: 1px 5px;'>Muestra insuficiente ({inf_3c_h.get('n_fechas', 0)}/30 fechas)</span>"
            txt_h = (
                f"<b>{hz_k}</b>: Acierto en 3 clases <b>{t3_h:.1f}%</b> (Ref. siempre lateral: {rl_txt}{ic_h_3c})"
                f" &middot; Acierto de signo <b>{ts_txt}</b> sobre {nd_h} ALC/BAJ"
                f" (Ref. azar: 50.0% &middot; Subidas: {rs_txt}{ic_h_s}){aviso_h}"
            )
        hz_lineas_c09.append(f'<span>{txt_h} <span style="color:#64748b;">[{et_h}]</span></span>')
    prelim_c09 = "" if muestra_suficiente else f" (Muestra insuficiente: {n_fechas_emision}/30 fechas de emision)"
    st.html(f"""
    <div class="ia-regime-strip" style="margin-bottom: 10px; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
        <span>Muestra (regla v1 sellada): <b>{etiqueta_html}</b></span>
        <span>Acierto en 3 clases: senal emitida == clase real (ALCISTA / LATERAL / BAJISTA). Acierto de signo: solo ALCISTA / BAJISTA, acierta si el precio se mueve en esa direccion.</span>
    </div>
    <div class="ia-regime-strip" style="margin-bottom: 14px; flex-direction: column; align-items: flex-start; gap: 4px;">
        <span>Desglose por horizonte{prelim_c09}:</span>
        {"".join(hz_lineas_c09)}
    </div>
    """)

    n_eval_c10 = int(resumen_c10.get("n_evaluadas") or 0)
    tasa_p_c10 = resumen_c10.get("tasa_acierto_precio_c10")
    tasa_rt_c10 = resumen_c10.get("tasa_acierto_retorno_total_c10")
    n_div_c10 = int(resumen_c10.get("n_con_dividendo") or 0)
    tasa_ap_c11 = resumen_c11.get("tasa_apertura_precio")
    delta_ap_c11 = resumen_c11.get("delta_apertura_vs_cierre_pp")
    if n_eval_c10 > 0 and tasa_p_c10 is not None and tasa_rt_c10 is not None:
        et_c10 = _html_c09.escape(resumen_c10.get("etiqueta_muestra") or "")
        lat_c10 = resumen_c10.get("ref_siempre_lateral_pct")
        sig_p_c10 = resumen_c10.get("tasa_acierto_signo")
        sig_rt_c10 = resumen_c10.get("tasa_acierto_signo_retorno_total_c10")
        sub_c10 = resumen_c10.get("ref_frecuencia_subida_pct")
        nd_c10 = int(resumen_c10.get("n_direccionales") or 0)
        sig_ap_c11 = resumen_c11.get("tasa_acierto_signo_apertura")
        c11_part = ""
        if tasa_ap_c11 is not None and delta_ap_c11 is not None:
            c11_part = (
                f' | Apertura S+1: <b style="color:#f59e0b;">{tasa_ap_c11:.2f}%</b> '
                f'({delta_ap_c11:+.2f} pp vs Cierre S)'
            )
        sig_ap_txt = f" &middot; Apertura S+1: <b>{sig_ap_c11:.1f}%</b>" if sig_ap_c11 is not None else ""
        lat_c10_txt = f"{lat_c10:.1f}%" if lat_c10 is not None else "s/d"
        sig_p_txt = f"{sig_p_c10:.1f}%" if sig_p_c10 is not None else "s/d"
        sig_rt_txt = f"{sig_rt_c10:.1f}%" if sig_rt_c10 is not None else "s/d"
        sub_c10_txt = f"{sub_c10:.1f}%" if sub_c10 is not None else "s/d"
        c10_strip_html = f"""
        <div class="ia-regime-strip" style="margin-bottom: 14px; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
            <span>Evaluacion C-10/C-11 (MEDIDO) &middot; {et_c10}:</span>
            <span>Acierto en 3 clases &mdash; Precio: <b style="color:#38bdf8;">{tasa_p_c10:.2f}%</b> vs Ret. Total (c/div): <b style="color:#34d399;">{tasa_rt_c10:.2f}%</b>{c11_part} (Ref. siempre lateral: {lat_c10_txt})</span>
            <span>Acierto de signo (ALC/BAJ, n={nd_c10}) &mdash; Precio: <b>{sig_p_txt}</b> &middot; Ret. Total: <b>{sig_rt_txt}</b>{sig_ap_txt} (Ref. azar: 50.0% &middot; Subidas: {sub_c10_txt})</span>
            <span class="tag-badge tag-cyan">Ajuste por dividendo: {n_div_c10} senales</span>
        </div>
        """
        st.html(c10_strip_html)

    n_eval_c12 = int(resumen_c12.get("n_evaluadas") or 0)
    tasa_v1_c12 = resumen_c12.get("tasa_acierto_v1_precio")
    tasa_v2_c12 = resumen_c12.get("tasa_acierto_v2_precio")
    lat_v1_c12 = resumen_c12.get("pct_lateral_real_v1")
    lat_v2_c12 = resumen_c12.get("pct_lateral_real_v2")
    rango_v1_c12 = resumen_c12.get("rango_intertercil_lateral_v1_pp")
    rango_v2_c12 = resumen_c12.get("rango_intertercil_lateral_v2_pp")
    terciles_c12 = resumen_c12.get("terciles_sigma") or []
    if (
        n_eval_c12 > 0
        and tasa_v1_c12 is not None
        and tasa_v2_c12 is not None
        and lat_v1_c12 is not None
        and lat_v2_c12 is not None
    ):
        terc_parts = []
        for t_item in terciles_c12:
            cod_t = t_item.get("codigo", "")
            p1_t = t_item.get("pct_lateral_v1")
            p2_t = t_item.get("pct_lateral_v2")
            if p1_t is not None and p2_t is not None:
                terc_parts.append(f"{cod_t}: v1 {p1_t:.1f}% -> v2 {p2_t:.1f}%")
        terc_txt = " | ".join(terc_parts)
        rango_txt = (
            f"Rango |T1-T3|: v1 {rango_v1_c12:.2f} pp vs v2 {rango_v2_c12:.2f} pp"
            if (rango_v1_c12 is not None and rango_v2_c12 is not None)
            else ""
        )
        et_c12 = _html_c09.escape(resumen_c12.get("etiqueta_muestra") or "")
        sig_c12 = resumen_c12.get("tasa_acierto_signo")
        sub_c12 = resumen_c12.get("ref_frecuencia_subida_pct")
        sig_c12_txt = f"{sig_c12:.1f}%" if sig_c12 is not None else "s/d"
        sub_c12_txt = f"{sub_c12:.1f}%" if sub_c12 is not None else "s/d"
        c12_strip_html = f"""
        <div class="ia-regime-strip" style="margin-bottom: 14px; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
            <span>Evaluacion C-12 Zona Muerta k*sigma*sqrt(dias) (k=0.50) &middot; {et_c12}:</span>
            <span>Acierto en 3 clases &mdash; v1 (fijo): <b style="color:#38bdf8;">{tasa_v1_c12:.2f}%</b> vs v2 (vol): <b style="color:#34d399;">{tasa_v2_c12:.2f}%</b> | Ref. siempre lateral (LATERAL real): v1 <b>{lat_v1_c12:.2f}%</b> vs v2 <b>{lat_v2_c12:.2f}%</b> ({terc_txt})</span>
            <span>Acierto de signo (ALC/BAJ, igual en v1 y v2): <b>{sig_c12_txt}</b> (Ref. azar: 50.0% &middot; Subidas: {sub_c12_txt})</span>
            <span class="tag-badge tag-cyan">{rango_txt}</span>
        </div>
        """
        st.html(c12_strip_html)

    hz_fmt_map = {"1D": "1 Día", "3D": "3 Días", "1S": "1 Semana", "1M": "1 Mes"}

    # C-14: Tabla Completa de Referencias Ingenuas
    tabla_c14_hist = m.get("referencias_ingenuas_c14")
    if not tabla_c14_hist or not tabla_c14_hist.get("tabla"):
        try:
            tabla_c14_hist = _ia_obtener_tabla_c14(hz_sel_key, False)
        except Exception:
            tabla_c14_hist = None
    if tabla_c14_hist and tabla_c14_hist.get("tabla"):
        st.html(_render_tabla_referencias_ingenuas_html(tabla_c14_hist, es_auditoria=False))

    # ── Pestañas Institucionales: Resueltas vs Seguimiento Activo ──────
    n_res = len(df_resueltas)
    n_pend = len(df_pendientes)

    tab_res, tab_pend = st.tabs([
        f"Historial de Resueltas ({n_res})",
        f"En Seguimiento Activo ({n_pend})",
    ])

    with tab_res:
        if not df_resueltas.empty:
            if n_fechas_emision < 30:
                st.markdown(
                    f"""
                    <div style="background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.28); border-radius: 8px; padding: 10px 14px; font-family: 'Inter', sans-serif; margin-bottom: 14px;">
                        <div style="color: #fbbf24; font-weight: 700; font-size: 0.80rem; margin-bottom: 3px;">
                            MUESTRA INSUFICIENTE ({n_fechas_emision}/30 fechas de emision) &middot; {etiqueta_html}
                        </div>
                        <div style="color: #94a3b8; font-size: 0.74rem; line-height: 1.45;">
                            Por correlaci&oacute;n contempor&aacute;nea del mercado e inferencia honesta por bloques de sesi&oacute;n (C-13), las se&ntilde;ales de una misma fecha no son independientes.
                            Con {n_fechas_emision} fechas el error est&aacute;ndar robusto entre fechas (SE cluster) es significativamente mayor que el error ingenuo i.i.d. y la muestra no alcanza las <b>30 fechas m&iacute;nimas de emisi&oacute;n</b> requeridas para declarar significaci&oacute;n estad&iacute;stica ni sacar conclusiones definitivas.
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            else:
                # C-09: desglose por direccion emitida (Acierto en 3 clases) sobre el
                # total SQL de resueltas (obtener_metricas), no sobre las 250 filas visibles.
                dist_em_m = m.get("dist_senales_emitidas") or {}
                n_alc_em = int(dist_em_m.get("n_alcista") or 0)
                n_baj_em = int(dist_em_m.get("n_bajista") or 0)
                n_lat_em = int(dist_em_m.get("n_lateral") or 0)

                def _pct_txt_c09(v) -> str:
                    return f"{v:.1f}%" if v is not None else "s/d"

                def _ret_html_c09(v) -> str:
                    if v is None:
                        return ""
                    color_r = "#4ade80" if v >= 0 else "#f87171"
                    return f'<span style="font-size: 0.75rem; color: {color_r}; font-weight: 600; margin-left: 4px;">({v:+.2f}%)</span>'

                u_lat_lbl = f"+/-{db_pred.UMBRALES_LATERAL.get(hz_sel_key, 0.8):.1f}%" if hz_sel_key != "Todos" else "en rango"
                nota_c09 = 'style="font-size: 0.72rem; color: #94a3b8; font-weight: 400;"'

                box_base_lateral = f"""
                        <div class="ia-stat-chip slate">
                            <span class="ia-stat-chip-label">Ref. siempre lateral (3 clases)</span>
                            <span class="ia-stat-chip-val">{_pct_txt_c09(pct_base_lateral)} <span {nota_c09}>({aciertos_lat_base}/{n_valid_res})</span></span>
                        </div>
                """ if pct_base_lateral is not None else ""

                box_signo = f"""
                        <div class="ia-stat-chip slate">
                            <span class="ia-stat-chip-label">Acierto de signo ALC/BAJ ({n_dir})</span>
                            <span class="ia-stat-chip-val">{_pct_txt_c09(tasa_signo)} <span {nota_c09}>Ref. azar: 50.0% &middot; Subidas: {ref_sub_txt}</span></span>
                        </div>
                """

                st.markdown(
                    f"""
                    <div style="display: flex; gap: 12px; margin-bottom: 6px; flex-wrap: wrap;">
                        <div class="ia-stat-chip pos">
                            <span class="ia-stat-chip-label">Alcistas ({n_alc_em}) &middot; 3 clases</span>
                            <span class="ia-stat-chip-val">{_pct_txt_c09(m.get("tasa_alcista_3c"))} <span {nota_c09}>acierto</span>{_ret_html_c09(m.get("ret_alcista_medio"))}</span>
                        </div>
                        <div class="ia-stat-chip neg">
                            <span class="ia-stat-chip-label">Bajistas ({n_baj_em}) &middot; 3 clases</span>
                            <span class="ia-stat-chip-val">{_pct_txt_c09(m.get("tasa_bajista_3c"))} <span {nota_c09}>acierto</span>{_ret_html_c09(m.get("ret_bajista_medio"))}</span>
                        </div>
                        <div class="ia-stat-chip amber">
                            <span class="ia-stat-chip-label">Laterales ({n_lat_em}) &middot; 3 clases</span>
                            <span class="ia-stat-chip-val">{_pct_txt_c09(m.get("tasa_lateral_3c"))} <span {nota_c09}>acierto ({u_lat_lbl})</span></span>
                        </div>
                        {box_base_lateral}
                        {box_signo}
                    </div>
                    <div style="font-family: 'Inter', sans-serif; font-size: 0.72rem; color: #64748b; margin-bottom: 14px;">Muestra: {etiqueta_html}</div>
                    """,
                    unsafe_allow_html=True,
                )

            # Columna Resultado con distinción de invalidaciones
            col_motivo = df_resueltas.get("motivo_resolucion")
            resultados = []
            for i, row in df_resueltas.iterrows():
                motivo = row.get("motivo_resolucion", "vencida") if col_motivo is not None else "vencida"
                if motivo == "invalidada":
                    resultados.append("Invalidada")
                elif row.get("acierto") == 1:
                    resultados.append("Acierto")
                else:
                    resultados.append("Fallo")

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
                "Var. Precio": (
                    df_resueltas["var_precio_c10"].where(
                        df_resueltas["var_precio_c10"].notna(), df_resueltas["variacion_real"]
                    )
                    if "var_precio_c10" in df_resueltas.columns
                    else df_resueltas["variacion_real"]
                ).map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "-"),
                "Ret. Total (c/div)": (
                    df_resueltas["var_retorno_total_c10"].where(
                        df_resueltas["var_retorno_total_c10"].notna(), df_resueltas["variacion_real"]
                    )
                    if "var_retorno_total_c10" in df_resueltas.columns
                    else df_resueltas["variacion_real"]
                ).map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "-"),
                "Var. Apertura S+1": (
                    df_resueltas["var_apertura_precio_c11"]
                    if "var_apertura_precio_c11" in df_resueltas.columns
                    else pd.Series([np.nan] * len(df_resueltas), index=df_resueltas.index)
                ).map(lambda v: f"{v:+.2f}%" if pd.notna(v) else "-"),
                "Score": scores_fmt,
                "Resultado": resultados,
            })

            col_widths_hist = {
                "Fecha": "8%",
                "Empresa": "12%",
                "Horizonte": "7%",
                "Señal": "8%",
                "Entrada": "8%",
                "Cierre": "8%",
                "Est. (%)": "8%",
                "Var. Precio": "8%",
                "Ret. Total (c/div)": "9%",
                "Var. Apertura S+1": "8%",
                "Score": "6%",
                "Resultado": "10%",
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
                if os.environ.get("STOCKLAB_DEV") == "1":
                    if st.button(
                        "Limpiar resueltas...",
                        key="btn_trigger_limpiar_hist",
                        use_container_width=True,
                        help=f"Eliminar predicciones resueltas ({filtro_elegido} o todas)",
                    ):
                        m_glob = _ia_obtener_metricas("Todos", False)
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
                    <div class="ia-hist-empty-title">Sin señales resueltas para {filtro_elegido}</div>
                    <div class="ia-hist-empty-desc">
                        No hay señales vencidas ni liquidadas aún para este horizonte temporal.<br>
                        Las señales vigentes se encuentran activas en la pestaña <b>En Seguimiento Activo</b> hasta alcanzar su fecha objetivo de vencimiento.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if os.environ.get("STOCKLAB_DEV") == "1":
                m_glob = _ia_obtener_metricas("Todos", False)
                n_res_glob = m_glob.get("resueltas", 0)
                if n_res_glob > 0:
                    _, col_reset = st.columns([3.2, 1.3])
                    with col_reset:
                        if st.button(
                            "Gestionar Historial...",
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
                    <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#34d399;box-shadow:0 0 8px #34d399;margin-right:6px;vertical-align:middle;"></span><b>{n_pend} señales vivas en seguimiento</b> ({hz_tag}) · Se liquidarán automáticamente al precio de cierre al llegar su vencimiento.
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



# ═══════════════════════════════════════════════════════════════════
# 📊 RENDERIZADO DEL PANEL DESCRIPTIVO DE MERCADO Y RIESGO
# ═══════════════════════════════════════════════════════════════════

@st.fragment
def _render_panel_descriptivo(
    closes: pd.DataFrame,
    highs: pd.DataFrame,
    lows: pd.DataFrame,
    vols: pd.DataFrame,
    empresas_dict: Dict[str, str],
    ibex_set: set
) -> None:
    # ── Encabezado Principal Centrado (Estilo Radar e Historial) ────────
    st.markdown(
        """
        <div class="ia-section-header">
            <h2 class="sl-section-title">Panel Cuantitativo Descriptivo</h2>
            <div class="ia-section-sub sl-page-subtitle">Estadísticas empíricas de distribución, volatilidad realizada y liquidez institucional</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Selector de Activo y Metadatos en Barra de Control Simétrica ──
    tickers_disponibles = [t for t in empresas_dict.keys() if t in closes.columns and t != "^IBEX"]
    tickers_disponibles = sorted(tickers_disponibles, key=lambda t: (0 if t in ibex_set else 1, empresas_dict.get(t, t)))
    formato_opcion = lambda t: f"{t} — {empresas_dict.get(t, t)} {'[IBEX 35]' if t in ibex_set else '[Continuo]'}"

    f_min = str(closes.index.min())[:10] if not closes.empty else "2020-10-02"
    f_max = str(closes.index.max())[:10] if not closes.empty else "2026-10-02"

    if tickers_disponibles and st.session_state.get("ia_desc_ticker_select") not in tickers_disponibles:
        st.session_state["ia_desc_ticker_select"] = tickers_disponibles[0]

    c_sel, c_meta, c_btn = st.columns([1.3, 2.6, 0.72], vertical_alignment="bottom")
    with c_sel:
        ticker_sel = st.selectbox(
            "ACTIVO PARA ANÁLISIS",
            tickers_disponibles,
            format_func=formato_opcion,
            key="ia_desc_ticker_select",
        )
    with c_meta:
        if ticker_sel:
            es_ibex_act = ticker_sel in ibex_set
            badge_cls = "tag-cyan" if es_ibex_act else "tag-amber"
            badge_txt = "CONSTITUYENTE IBEX 35" if es_ibex_act else "MERCADO CONTINUO"
            st.html(
                f"""
                <div class="ia-control-label">RÉGIMEN Y COBERTURA HISTÓRICA</div>
                <div class="ia-desc-meta-strip">
                    <span class="tag-badge {badge_cls}">{badge_txt}</span>
                    <span class="ia-desc-meta-text"><b>{len(closes)} sesiones BME</b> ({f_min} a {f_max})</span>
                </div>
                """
            )
    with c_btn:
        if st.button("Refrescar", key="ia_btn_refrescar_panel", help="Descarga los cierres consolidados recientes y sincroniza la base del panel", use_container_width=True):
            try:
                from services.emision import refrescar_cierres_panel
                with st.spinner("Sincronizando cierres diarios..."):
                    ok = refrescar_cierres_panel(verbose=False)
                if ok:
                    try:
                        cargar_datos_cache_historico.clear()
                        _ia_vol_eur_mercado.clear()
                    except Exception:
                        pass
                    _limpiar_caches_ia_pred()
                    st.toast("Base de datos del panel sincronizada con la última sesión.")
                    st.rerun(scope="app")
                else:
                    st.toast("El panel ya está actualizado con la última sesión consolidada.")
            except Exception as e:
                st.toast(f"Error al refrescar: {e}")

    # ── Banner Institucional de Advertencia Regulatoria ───────────────
    st.html(
        """
        <div class="ia-desc-banner">
            <div class="ia-desc-banner-head">
                <span>AVISO REGULATORIO: PANEL ESTRICTAMENTE DESCRIPTIVO</span>
            </div>
            <div class="ia-desc-banner-body">
                Este panel contiene exclusivamente <b>estadísticas cuantitativas descriptivas, medidas históricas de volatilidad y perfiles de liquidez</b> basados en cotizaciones pasadas cerradas a fin de sesión (<b>corte oficial BME 18:30 CET</b>).
                <b>No contiene recomendaciones de compra o venta, proyecciones direccionales ni pronósticos de precios futuros.</b> Rentabilidades pasadas no garantizan rendimientos futuros.
            </div>
        </div>
        """
    )

    if not ticker_sel or ticker_sel not in closes.columns:
        st.warning("No hay datos históricos para el activo seleccionado.")
        return

    p = closes[ticker_sel].dropna()
    v = vols[ticker_sel].reindex(p.index) if ticker_sel in vols.columns else None
    h = highs[ticker_sel].reindex(p.index) if ticker_sel in highs.columns else None
    l = lows[ticker_sel].reindex(p.index) if ticker_sel in lows.columns else None

    if len(p) < 60:
        st.warning("Serie histórica insuficiente para análisis cuantitativo.")
        return

    vol_eur_mat = _ia_vol_eur_mercado()

    m_vol = calcular_metricas_volatilidad(p, volumen=v, highs=h, lows=l)
    m_ext = calcular_extremos_y_drawdown(p, highs=h, lows=l, volumen=v, ventana_dias=252)
    m_liq = calcular_metricas_liquidez(p, volumen=v, volumenes_mercado_medios=vol_eur_mat, ventana_dias=252)
    m_frecs = calcular_frecuencias_historicas(p, ventana_dias=252)

    p_ibex = closes["^IBEX"].dropna() if "^IBEX" in closes.columns else pd.Series(dtype=float)
    m_sens = calcular_sensibilidad_benchmark(p, p_ibex, ventana_corr=60)
    data_fund, fecha_fund = cargar_snapshot_fundamental(ticker_sel)

    def _fmt_f(val, spec: str = ".1f", sufijo: str = "", fallback: str = "N/D") -> str:
        if val is None or pd.isna(val):
            return fallback
        try:
            vf = float(val)
            if not np.isfinite(vf):
                return fallback
            return f"{format(vf, spec)}{sufijo}"
        except Exception:
            return fallback

    # ── 1. Rango 52 Semanas y Drawdown Histórico ──────────────────────
    dist_max = m_ext['distancia_max_52w_pct']
    dist_min = m_ext['distancia_min_52w_pct']
    max_dd = m_ext['max_drawdown_252d_pct']
    dd_act = m_ext['drawdown_actual_pct']
    pos_rango = m_ext['posicion_rango_52w_pct']
    pos_rango_num = float(pos_rango) if pd.notna(pos_rango) and np.isfinite(float(pos_rango)) else 50.0
    barras_excl = m_ext.get('barras_excluidas', 0)
    cls_dist_max = "neg" if (pd.notna(dist_max) and dist_max < -15) else "amber"

    st.markdown(
        """
        <div class="ia-desc-section-head">
            <span class="ia-desc-section-num">01</span>
            <span class="ia-desc-section-title">Rango de 52 Semanas y Drawdown Histórico</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    kpis_r1 = f"""
    <div class="ia-hist-kpi-row">
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Último Cierre Oficial</span>
            </div>
            <div class="ia-hist-kpi-value cyan">{_fmt_f(m_ext['precio_actual'], '.3f', ' €')}</div>
            <div class="ia-hist-kpi-caption">Corte BME 18:30 CET ({str(p.index[-1])[:10]})</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Distancia al Máximo 52s</span>
            </div>
            <div class="ia-hist-kpi-value {cls_dist_max}">{_fmt_f(dist_max, '+.2f', '%')}</div>
            <div class="ia-hist-kpi-caption">Pico 52s: {_fmt_f(m_ext['max_52w'], '.3f', ' €')}</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Distancia al Mínimo 52s</span>
            </div>
            <div class="ia-hist-kpi-value pos">{_fmt_f(dist_min, '+.2f', '%')}</div>
            <div class="ia-hist-kpi-caption">Suelo 52s: {_fmt_f(m_ext['min_52w'], '.3f', ' €')}</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Max Drawdown (252d)</span>
            </div>
            <div class="ia-hist-kpi-value neg">{_fmt_f(max_dd, '.2f', '%')}</div>
            <div class="ia-hist-kpi-caption">Actual: {_fmt_f(dd_act, '.2f', '%')}</div>
        </div>
    </div>
    <div class="ia-range-card">
        <div class="ia-range-labels">
            <div class="ia-range-bound">
                <span class="ia-range-label">MÍNIMO 52S</span>
                <span class="ia-range-val">{_fmt_f(m_ext['min_52w'], '.3f', ' €')}</span>
            </div>
            <div class="ia-range-bound" style="align-items: center;">
                <span class="tag-badge tag-cyan">POSICIÓN EN RANGO: {_fmt_f(pos_rango, '.1f', '%')}</span>
            </div>
            <div class="ia-range-bound right">
                <span class="ia-range-label">MÁXIMO 52S</span>
                <span class="ia-range-val">{_fmt_f(m_ext['max_52w'], '.3f', ' €')}</span>
            </div>
        </div>
        <div class="ia-range-track">
            <div class="ia-range-fill" style="width: {min(100.0, max(0.0, pos_rango_num)):.1f}%;"></div>
        </div>
        <div class="ia-range-footer">
            <span>Control de calidad de datos: <b style="color:#94a3b8;">{barras_excl}</b> barras anómalas excluidas en 52 semanas.</span>
            <span>Cierre oficial analizado: <b style="color:#38bdf8;">{_fmt_f(m_ext['precio_actual'], '.3f', ' €')}</b></span>
        </div>
    </div>
    """
    st.html(kpis_r1)

    # ── 2. Volatilidad Realizada & Percentil ───────────────────────────
    pct_1y = m_vol['percentil_1y']
    pct_3y = m_vol['percentil_3y']
    pct_txt = _fmt_f(pct_1y, ".1f", "%")
    pct_cls = "amber" if (pd.notna(pct_1y) and pct_1y >= 65) else ("pos" if pd.notna(pct_1y) and pct_1y <= 35 else "cyan")
    caption_pct = f"3 Años: {pct_3y:.1f}%" if pd.notna(pct_3y) else "Percentil histórico"

    st.markdown(
        """
        <div class="ia-desc-section-head">
            <span class="ia-desc-section-num">02</span>
            <span class="ia-desc-section-title">Volatilidad Realizada Anualizada y Percentiles Propios</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    kpis_r2 = f"""
    <div class="ia-hist-kpi-row">
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Volatilidad 20d</span>
            </div>
            <div class="ia-hist-kpi-value cyan">{_fmt_f(m_vol['vol_20d_anual'], '.1f', '%')}</div>
            <div class="ia-hist-kpi-caption">Desviación anualizada (1 mes)</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Volatilidad 60d</span>
            </div>
            <div class="ia-hist-kpi-value">{_fmt_f(m_vol['vol_60d_anual'], '.1f', '%')}</div>
            <div class="ia-hist-kpi-caption">Trimestral histórica</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Volatilidad 252d</span>
            </div>
            <div class="ia-hist-kpi-value">{_fmt_f(m_vol['vol_252d_anual'], '.1f', '%')}</div>
            <div class="ia-hist-kpi-caption">Anualizada histórica (1 año)</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Percentil vs 1 Año</span>
            </div>
            <div class="ia-hist-kpi-value {pct_cls}">{pct_txt}</div>
            <div class="ia-hist-kpi-caption">{caption_pct}</div>
        </div>
    </div>
    <div class="ia-regime-strip">
        <span>Régimen descriptivo de volatilidad:</span>
        <span class="tag-badge tag-cyan">{m_vol['regimen_descriptivo']}</span>
    </div>
    """
    st.html(kpis_r2)

    # ── 3. Frecuencias Históricas ─────────────────────────────────────
    st.markdown(
        """
        <div class="ia-desc-section-head">
            <span class="ia-desc-section-num">03</span>
            <span class="ia-desc-section-title">Frecuencia Histórica de Movimientos Observados (Últimas 252 Sesiones)</span>
        </div>
        <div class="ia-desc-banner" style="border-left-color: #818cf8; background: linear-gradient(135deg, rgba(99, 102, 241, 0.06) 0%, rgba(13, 17, 26, 0.75) 100%); border-color: rgba(99, 102, 241, 0.25); margin-bottom: 14px;">
            <div class="ia-desc-banner-head" style="color: #a5b4fc;">
                <span>DISTRIBUCIÓN EMPÍRICA OBSERVADA</span>
            </div>
            <div class="ia-desc-banner-body">
                Frecuencia observada empíricamente en las últimas 252 sesiones de mercado completas. No constituye pronóstico probabilístico ni proyección futura.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    df_frec = pd.DataFrame(m_frecs)
    html_frec = """<div class="ia-desc-table-wrap">
    <table class="ia-desc-table">
    <thead>
    <tr>
        <th>Horizonte (h)</th>
        <th>Umbral (&plusmn;X%)</th>
        <th>Ventanas / Barras</th>
        <th>Frec. Sube (&gt; +X%)</th>
        <th>Frec. Baja (&lt; -X%)</th>
        <th>Total (|r| &gt; X%)</th>
        <th>Etiqueta</th>
    </tr>
    </thead>
    <tbody>"""
    for _, row in df_frec.iterrows():
        obs_label = row.get("desc_obs", f"{row['n_observaciones']} barras")
        html_frec += f"""<tr>
        <td style="font-weight:700; color:#38bdf8; font-family: 'JetBrains Mono', monospace;">{row['horizonte']}</td>
        <td style="font-family: 'JetBrains Mono', monospace;">&plusmn;{_fmt_f(row['umbral_pct'], '.1f', '%')}</td>
        <td style="font-family: 'JetBrains Mono', monospace; color:#cbd5e1;">{obs_label}</td>
        <td style="color: #34d399; font-weight:600; font-family: 'JetBrains Mono', monospace;">{_fmt_f(row['frec_sube_pct'], '.1f', '%')}</td>
        <td style="color: #f87171; font-weight:600; font-family: 'JetBrains Mono', monospace;">{_fmt_f(row['frec_baja_pct'], '.1f', '%')}</td>
        <td style="font-weight:700; color:#f1f5f9; font-family: 'JetBrains Mono', monospace;">{_fmt_f(row['frec_extremo_total_pct'], '.1f', '%')}</td>
        <td><span class="tag-badge tag-cyan" style="padding: 2px 8px; font-size: 0.68rem; font-weight:700;">{row['etiqueta']}</span></td>
        </tr>"""
    html_frec += """</tbody></table></div>"""
    st.html(html_frec)

    # ── 4. Liquidez y Sensibilidad vs IBEX ────────────────────────────
    def _fmt_veur(val) -> str:
        if val is None or pd.isna(val) or not np.isfinite(float(val)):
            return "N/D"
        vf = float(val)
        return f"{vf / 1e6:.2f} M€" if vf >= 1e6 else f"{vf / 1e3:.1f} k€"

    veur_txt = _fmt_veur(m_liq['volumen_eur_20d'])
    veur_60_txt = _fmt_veur(m_liq['volumen_eur_60d'])
    pct_zeros = m_liq['pct_dias_sin_variacion']
    zeros_cls = (
        "pos" if (pd.notna(pct_zeros) and pct_zeros < 2.0)
        else ("amber" if (pd.notna(pct_zeros) and pct_zeros < 5.0) else "neg")
    )

    corr_60 = m_sens.get("correlacion_ibex_60d", np.nan)
    corr_txt = _fmt_f(corr_60, ".2f")
    beta_60 = m_sens.get("beta_ibex_60d", np.nan)
    beta_txt = _fmt_f(beta_60, ".2f")

    st.markdown(
        """
        <div class="ia-desc-section-head">
            <span class="ia-desc-section-num">04</span>
            <span class="ia-desc-section-title">Perfil de Liquidez y Sensibilidad frente al IBEX 35</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    kpis_r4 = f"""
    <div class="ia-hist-kpi-row">
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Volumen Medio 20d</span>
            </div>
            <div class="ia-hist-kpi-value cyan">{veur_txt}</div>
            <div class="ia-hist-kpi-caption">60d: {veur_60_txt}</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Días sin Variación (252d)</span>
            </div>
            <div class="ia-hist-kpi-value {zeros_cls}">{_fmt_f(pct_zeros, '.2f', '%')}</div>
            <div class="ia-hist-kpi-caption">Tercil: {m_liq['tercil_liquidez']}</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Correlación con IBEX</span>
            </div>
            <div class="ia-hist-kpi-value">{corr_txt}</div>
            <div class="ia-hist-kpi-caption">Pearson móvil a 60 días</div>
        </div>
        <div class="ia-hist-kpi-card">
            <div class="ia-hist-kpi-head">
                <span class="ia-hist-kpi-title">Beta Móvil 60d</span>
            </div>
            <div class="ia-hist-kpi-value">{beta_txt}</div>
            <div class="ia-hist-kpi-caption">Sensibilidad vs índice oficial</div>
        </div>
    </div>
    """
    st.html(kpis_r4)

    # Gráfico comparativo Base 100 vs IBEX 35 (último año / 252 sesiones comunes)
    if not p_ibex.empty:
        idx_rel = p.index.intersection(p_ibex.index)
        if len(idx_rel) >= 30:
            idx_rel_1y = idx_rel[-252:]
            p_a_1y = p.loc[idx_rel_1y]
            p_i_1y = p_ibex.loc[idx_rel_1y]
            base_a = float(p_a_1y.iloc[0])
            base_i = float(p_i_1y.iloc[0])
            if base_a > 0 and base_i > 0:
                norm_a = (p_a_1y / base_a) * 100.0
                norm_i = (p_i_1y / base_i) * 100.0
                ret_pills = []
                for lbl_r, k_r in (
                    ("1M", "retorno_relativo_1m_pct"),
                    ("3M", "retorno_relativo_3m_pct"),
                    ("6M", "retorno_relativo_6m_pct"),
                    ("1A", "retorno_relativo_1a_pct"),
                ):
                    rv = m_sens.get(k_r)
                    if rv is not None and pd.notna(rv) and np.isfinite(float(rv)):
                        rc = "#34d399" if float(rv) >= 0 else "#f87171"
                        ret_pills.append(
                            f'<span style="font-family:\'JetBrains Mono\',monospace; font-size:0.70rem; '
                            f'padding:2px 8px; border-radius:999px; background:rgba(15,23,42,0.75); '
                            f'border:1px solid rgba(148,163,184,0.16); color:#94a3b8;">'
                            f'{lbl_r} vs IBEX: <b style="color:{rc};">{float(rv):+.1f}%</b></span>'
                        )
                pills_rel_html = "".join(ret_pills)
                st.html(
                    f"""
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;
                                gap:8px; margin:14px 0 8px 0; padding:10px 14px; border-radius:10px;
                                background:rgba(13,17,26,0.65); border:1px solid rgba(148,163,184,0.12);">
                        <span style="font-family:'Inter',sans-serif; font-size:0.78rem; font-weight:600; color:#cbd5e1;">
                            Evolución Relativa (Base 100 · {len(idx_rel_1y)} sesiones):
                            <b style="color:#38bdf8;">{empresas_dict.get(ticker_sel, ticker_sel)}</b> vs
                            <b style="color:#f59e0b;">IBEX 35</b>
                        </span>
                        <div style="display:flex; flex-wrap:wrap; gap:6px;">{pills_rel_html}</div>
                    </div>
                    """
                )
                labels_rel = [pd.Timestamp(d).strftime("%d %b %Y") for d in idx_rel_1y]
                ultimo_norm = float(norm_a.iloc[-1])
                color_rel = "#38bdf8" if ultimo_norm >= float(norm_i.iloc[-1]) else "#f87171"
                render_stock_chart(
                    labels=labels_rel,
                    prices=[round(float(x), 2) for x in norm_a.values],
                    last_close=100.0,
                    line_color=color_rel,
                    currency="pts",
                    ticker=f"{ticker_sel}_REL_IBEX",
                    title=f"{empresas_dict.get(ticker_sel, ticker_sel)} vs IBEX 35 (Base 100)",
                    main_label=f"{empresas_dict.get(ticker_sel, ticker_sel)} (Base 100)",
                    show_reference_line=True,
                    show_measure_tool=False,
                    show_zoom=False,
                    height=290,
                    y_padding_ratio=0.06,
                    extra_series=[
                        {
                            "label": "IBEX 35 (Base 100)",
                            "data": [round(float(x), 2) for x in norm_i.values],
                            "color": "#f59e0b",
                            "dash": [5, 4],
                            "width": 1.6,
                        }
                    ],
                )

    if m_liq['alerta_microestructura_open']:
        st.markdown(
            """
            <div class="ia-desc-banner" style="margin-top: 14px; margin-bottom: 14px;">
                <div class="ia-desc-banner-head">
                    <span>ALERTA DE MICROESTRUCTURA DE APERTURA</span>
                </div>
                <div class="ia-desc-banner-body">
                    En el tercil bajo de liquidez, el 19.29% de las sesiones no cruzan apertura real. Los análisis basados en Open se clasifican como NO VERIFICADOS.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── 5. Snapshot Fundamental ───────────────────────────────────────
    st.markdown(
        f"""
        <div class="ia-desc-section-head">
            <span class="ia-desc-section-num">05</span>
            <span class="ia-desc-section-title">Ratios y Múltiplos Fundamentales Institucionales</span>
        </div>
        <div style="font-size: 0.74rem; color: #64748b; margin-bottom: 12px; font-family: 'Inter', sans-serif;">
            Fecha de snapshot en disco: <b style="color:#38bdf8;">{fecha_fund}</b> · Fuente: <span style="color:#94a3b8;">{data_fund.get('_data_source', 'Caché Local')}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if data_fund:
        def _f_val(val, es_pct=False):
            if val is None or pd.isna(val):
                return "N/D"
            try:
                vf = float(val)
                if not np.isfinite(vf):
                    return "N/D"
                return f"{vf*100:.2f}%" if es_pct else f"{vf:.2f}"
            except Exception:
                return "N/D"

        per_tr = _f_val(data_fund.get('trailingPE'))
        per_fwd = _f_val(data_fund.get('forwardPE'))
        pb = _f_val(data_fund.get('priceToBook'))
        ev_ebitda = _f_val(data_fund.get('enterpriseToEbitda'))
        div_y = _f_val(data_fund.get('dividendYield'), es_pct=True)
        op_margin = _f_val(data_fund.get('operatingMargins'), es_pct=True)
        debt_eq = _f_val(data_fund.get('debtToEquity'))

        kpis_r5 = f"""
        <div class="ia-hist-kpi-row ia-hist-kpi-row-6">
            <div class="ia-hist-kpi-card compact">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">PER Trailing</span>
                </div>
                <div class="ia-hist-kpi-value">{per_tr}</div>
                <div class="ia-hist-kpi-caption">Forward: {per_fwd}</div>
            </div>
            <div class="ia-hist-kpi-card compact">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">Precio / Libro</span>
                </div>
                <div class="ia-hist-kpi-value">{pb}</div>
                <div class="ia-hist-kpi-caption">Valor contable</div>
            </div>
            <div class="ia-hist-kpi-card compact">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">EV / EBITDA</span>
                </div>
                <div class="ia-hist-kpi-value">{ev_ebitda}</div>
                <div class="ia-hist-kpi-caption">Múltiplo operativo</div>
            </div>
            <div class="ia-hist-kpi-card compact">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">Div. Yield</span>
                </div>
                <div class="ia-hist-kpi-value pos">{div_y}</div>
                <div class="ia-hist-kpi-caption">Rentabilidad div.</div>
            </div>
            <div class="ia-hist-kpi-card compact">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">Margen Op.</span>
                </div>
                <div class="ia-hist-kpi-value cyan">{op_margin}</div>
                <div class="ia-hist-kpi-caption">EBIT / Ingresos</div>
            </div>
            <div class="ia-hist-kpi-card compact">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">Deuda / Fondos</span>
                </div>
                <div class="ia-hist-kpi-value">{debt_eq}</div>
                <div class="ia-hist-kpi-caption">Apalancamiento</div>
            </div>
        </div>
        """
        st.html(kpis_r5)
    else:
        st.markdown(
            """
            <div class="ia-hist-empty-notice" style="margin: 8px 0 14px 0;">
                <div class="ia-hist-empty-title">Snapshot fundamental no disponible</div>
                <div class="ia-hist-empty-desc">No existe aún un archivo de fundamentales en caché local para este activo. Consulta su ficha en <b>Empresas</b> para descargar el perfil fundamental.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ═══════════════════════════════════════════════════════════════════
# 📑 RENDERIZADO DE AUDITORÍA Y METODOLOGÍA (ADDENDUM)
# ═══════════════════════════════════════════════════════════════════

def _render_auditoria_metodologia() -> None:
    """Vista pública de auditoría con la máxima excelencia institucional, simétrica y premium StockLab."""
    st.markdown(
        """
        <div class="ia-section-header">
            <h2 class="sl-section-title">Auditoría Institucional y Metodología</h2>
            <div class="ia-section-sub sl-page-subtitle">Certificación econométrica, contrastes empíricos y marco regulatorio</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        res_c11 = _ia_obtener_resumen_c11("Todos", False)
    except Exception:
        res_c11 = {"n_evaluadas": 0, "por_horizonte": {}}

    try:
        res_c12 = _ia_obtener_resumen_c12("Todos", False)
    except Exception:
        res_c12 = {"n_evaluadas": 0, "terciles_sigma": []}

    # C-09: muestra viva sellada (v5-frozen, regla v1) con las dos metricas separadas
    import html as _html_c09

    try:
        m_vivo = _ia_obtener_metricas("Todos", False)
    except Exception:
        m_vivo = {}

    def _p_c09(v) -> str:
        return f"{v:.1f}%" if v is not None else "s/d"

    et_vivo = _html_c09.escape(
        m_vivo.get("etiqueta_muestra") or db_pred.formatear_etiqueta_muestra(0, 0, None, None, "Todos")
    )
    t3_vivo = m_vivo.get("tasa_acierto_3clases")
    lat_vivo = m_vivo.get("ref_siempre_lateral_pct")
    sig_vivo = m_vivo.get("tasa_acierto_signo")
    nd_vivo = int(m_vivo.get("n_direccionales") or 0)
    sub_vivo = m_vivo.get("ref_frecuencia_subida_pct")
    vivo_3c_txt = (
        f"Acierto en 3 clases <b>{_p_c09(t3_vivo)}</b> (Ref. siempre lateral: {_p_c09(lat_vivo)})"
    )
    vivo_signo_txt = (
        f"Acierto de signo <b>{_p_c09(sig_vivo)}</b> sobre {nd_vivo} ALC/BAJ"
        f" (Ref. azar: 50.0% &middot; Subidas: {_p_c09(sub_vivo)})"
    )
    vivo_hz_parts = []
    for hz_k in ("1D", "3D", "1S", "1M"):
        e_h = (m_vivo.get("por_horizonte") or {}).get(hz_k) or {}
        et_h = _html_c09.escape(
            e_h.get("etiqueta_muestra") or db_pred.formatear_etiqueta_muestra(0, 0, None, None, hz_k)
        )
        if e_h.get("tasa_acierto_3clases") is None:
            vivo_hz_parts.append(f"<b>{hz_k}</b>: sin resueltas [{et_h}]")
        else:
            vivo_hz_parts.append(
                f"<b>{hz_k}</b>: 3 clases {_p_c09(e_h.get('tasa_acierto_3clases'))}"
                f" (ref. lateral {_p_c09(e_h.get('ref_siempre_lateral_pct'))})"
                f" &middot; signo {_p_c09(e_h.get('tasa_acierto_signo'))}"
                f" (n={int(e_h.get('n_direccionales') or 0)}, subidas {_p_c09(e_h.get('ref_frecuencia_subida_pct'))})"
                f" [{et_h}]"
            )
    inf_3c_v = m_vivo.get("inferencia_3clases") or {}
    inf_sig_v = m_vivo.get("inferencia_signo") or {}
    n_fechas_v = int(m_vivo.get("n_fechas") or 0)
    suf_v = bool(inf_3c_v.get("muestra_suficiente", False))
    aviso_v = inf_3c_v.get("aviso_muestra") or f"Muestra insuficiente ({n_fechas_v}/30 fechas de emision)"

    if inf_3c_v.get("ic95_inf") is not None and inf_3c_v.get("ic95_sup") is not None:
        ic_3c_audit = f"IC 95% (por fecha): [{inf_3c_v['ic95_inf']:.1f}%, {inf_3c_v['ic95_sup']:.1f}%] &middot; &plusmn;{inf_3c_v.get('ic95_semiancho_pct', 0.0):.1f} pp (N_ef={inf_3c_v.get('n_efectivo', 0.0):.1f} / {n_fechas_v} fechas &middot; SE cluster &plusmn;{inf_3c_v.get('se_cluster_pct', 0.0):.2f}% vs i.i.d. &plusmn;{inf_3c_v.get('se_iid_ingenuo_pct', 0.0):.2f}%)"
    else:
        ic_3c_audit = f"IC 95%: n/d ({n_fechas_v} fechas)"

    if inf_sig_v.get("ic95_inf") is not None and inf_sig_v.get("ic95_sup") is not None:
        ic_sig_audit = f"IC 95% (por fecha): [{inf_sig_v['ic95_inf']:.1f}%, {inf_sig_v['ic95_sup']:.1f}%] &middot; &plusmn;{inf_sig_v.get('ic95_semiancho_pct', 0.0):.1f} pp (N_ef={inf_sig_v.get('n_efectivo', 0.0):.1f} / {n_fechas_v} fechas &middot; SE cluster &plusmn;{inf_sig_v.get('se_cluster_pct', 0.0):.2f}% vs i.i.d. &plusmn;{inf_sig_v.get('se_iid_ingenuo_pct', 0.0):.2f}%)"
    else:
        ic_sig_audit = f"IC 95%: n/d ({n_fechas_v} fechas)"

    c13_badge_cls = "tag-cyan" if suf_v else "tag-amber"
    c13_badge_txt = "MUESTRA SUFICIENTE" if suf_v else f"MUESTRA INSUFICIENTE ({n_fechas_v}/30 FECHAS)"

    # C-14: Tabla Completa de Referencias Ingenuas en Auditoria
    tabla_c14_audit = m_vivo.get("referencias_ingenuas_c14")
    if not tabla_c14_audit or not tabla_c14_audit.get("tabla"):
        try:
            tabla_c14_audit = _ia_obtener_tabla_c14("Todos", False)
        except Exception:
            tabla_c14_audit = None
    c14_audit_card_html = _render_tabla_referencias_ingenuas_html(tabla_c14_audit, es_auditoria=True) if tabla_c14_audit else ""

    vivo_card_html = f"""
            <div class="ia-audit-card full-width">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-icon">[C-09]</span>
                        <span class="ia-audit-title">Muestra Viva Sellada: Acierto en 3 clases vs Acierto de signo (MEDIDO)</span>
                    </div>
                    <span class="tag-badge tag-cyan">{et_vivo}</span>
                </div>
                <div class="ia-audit-desc">
                    Dos metricas con nombre propio sobre las senales v5-frozen resueltas (regla v1 sellada).
                    <b>Acierto en 3 clases</b>: la senal emitida (ALCISTA / LATERAL / BAJISTA) coincide con la clase real; su referencia ingenua es <b>"siempre lateral"</b>.
                    <b>Acierto de signo</b>: solo senales ALCISTA / BAJISTA, acierta si el precio se mueve en esa direccion; sus referencias son el <b>azar (50.0%)</b> y la <b>frecuencia base de subidas</b>.
                    Resultado: {vivo_3c_txt}; {vivo_signo_txt}.
                </div>
                <div class="ia-audit-highlight cyan">
                    <span>Desglose por horizonte:</span> {" &middot; ".join(vivo_hz_parts)}
                </div>
            </div>

            <div class="ia-audit-card full-width">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-icon">[C-13]</span>
                        <span class="ia-audit-title">Inferencia Honesta por Bloques de Fecha y N_efectivo (MEDIDO)</span>
                    </div>
                    <span class="tag-badge {c13_badge_cls}">{c13_badge_txt}</span>
                </div>
                <div class="ia-audit-desc">
                    Las senales emitidas en una misma fecha comparten el movimiento del mercado de ese dia y no son independientes entre si. Tratar N = {m_vivo.get('n_senales', 0)} senales como observaciones i.i.d. subestimaria drasticamente la varianza.
                    <br/><b>Acierto en 3 clases:</b> {vivo_3c_txt} &mdash; <b>{ic_3c_audit}</b>.
                    <br/><b>Acierto de signo:</b> {vivo_signo_txt} &mdash; <b>{ic_sig_audit}</b>.
                    <br/><b>Aviso metodologico:</b> {aviso_v}. Ninguna prueba trata las senales del mismo dia como independientes ni se declara significacion estadistica con menos de 30 fechas.
                </div>
                <div class="ia-audit-highlight {'cyan' if suf_v else 'amber'}">
                    <span>{'Suficiencia estadistica:' if suf_v else 'Aviso de inferencia honesta:'}</span> {aviso_v} &middot; Error robusto por bloques de fecha (cluster) y N_efectivo integrados en todas las metricas.
                </div>
            </div>
    """

    n_ev_c11 = int(res_c11.get("n_evaluadas") or 0)
    t_cp_c11 = res_c11.get("tasa_cierre_precio")
    t_ap_c11 = res_c11.get("tasa_apertura_precio")
    d_p_c11 = res_c11.get("delta_apertura_vs_cierre_pp")
    t_crt_c11 = res_c11.get("tasa_cierre_retorno_total")
    t_art_c11 = res_c11.get("tasa_apertura_retorno_total")
    d_rt_c11 = res_c11.get("delta_apertura_vs_cierre_total_pp")
    por_hz_c11 = res_c11.get("por_horizonte") or {}

    if n_ev_c11 > 0 and t_cp_c11 is not None and t_ap_c11 is not None and d_p_c11 is not None:
        hz_parts = []
        for hz_k in ("1D", "3D", "1S", "1M"):
            info_h = por_hz_c11.get(hz_k) or {}
            nh = int(info_h.get("n_evaluadas") or 0)
            tcp_h = info_h.get("tasa_cierre_precio")
            tap_h = info_h.get("tasa_apertura_precio")
            dp_h = info_h.get("delta_apertura_vs_cierre_pp")
            et_h_c11 = _html_c09.escape(
                info_h.get("etiqueta_muestra") or db_pred.formatear_etiqueta_muestra(0, 0, None, None, hz_k)
            )
            if nh > 0 and tcp_h is not None and tap_h is not None and dp_h is not None:
                hz_parts.append(
                    f"<b>{hz_k}</b>: 3 clases {tcp_h:.2f}% -> {tap_h:.2f}% ({dp_h:+.2f} pp)"
                    f" &middot; signo {_p_c09(info_h.get('tasa_acierto_signo'))} -> {_p_c09(info_h.get('tasa_acierto_signo_apertura'))}"
                    f" [{et_h_c11}]"
                )
            else:
                hz_parts.append(f"<b>{hz_k}</b>: s/d [{et_h_c11}]")
        hz_summary_txt = " &middot; ".join(hz_parts)
        rt_summary_txt = (
            f"Acierto en 3 clases en retorno total (c/div): Cierre S <b>{t_crt_c11:.2f}%</b> vs Apertura S+1 <b>{t_art_c11:.2f}%</b> ({d_rt_c11:+.2f} pp)."
            if (t_crt_c11 is not None and t_art_c11 is not None and d_rt_c11 is not None)
            else ""
        )
        et_c11 = _html_c09.escape(res_c11.get("etiqueta_muestra") or "")
        signo_c11_txt = (
            f" <b>Acierto de signo</b> (ALC/BAJ, n={int(res_c11.get('n_direccionales') or 0)}):"
            f" Cierre S <b>{_p_c09(res_c11.get('tasa_acierto_signo'))}</b> vs Apertura S+1"
            f" <b>{_p_c09(res_c11.get('tasa_acierto_signo_apertura'))}</b>"
            f" (Ref. azar: 50.0% &middot; Subidas desde Cierre S: {_p_c09(res_c11.get('ref_frecuencia_subida_pct'))}"
            f" &middot; desde Apertura S+1: {_p_c09(res_c11.get('ref_frecuencia_subida_apertura_pct'))})."
            f" Ref. siempre lateral (3 clases, Cierre S): {_p_c09(res_c11.get('ref_siempre_lateral_pct'))}."
        )
        c11_audit_card_html = f"""
            <div class="ia-audit-card full-width">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-icon">[C-11]</span>
                        <span class="ia-audit-title">Entrada Operable desde Apertura S+1 (Evaluacion C-11, MEDIDO)</span>
                    </div>
                    <span class="tag-badge tag-cyan">N = {n_ev_c11} EVALUADAS</span>
                </div>
                <div class="ia-audit-desc">
                    Las senales se emiten tras el cierre de la sesion S (corte 18:30 Madrid), por lo que el primer precio operable real es la apertura (<b>Open</b>) de la sesion habil siguiente (<b>S+1</b>).
                    En el agregado auditado (<b>{et_c11}</b>), el <b>Acierto en 3 clases</b> en precio pasa de <b>{t_cp_c11:.2f}%</b> (desde Cierre S) a <b>{t_ap_c11:.2f}%</b> (desde Apertura S+1), un cambio de <b>{d_p_c11:+.2f} pp</b>. {rt_summary_txt}{signo_c11_txt}
                </div>
                <div class="ia-audit-highlight cyan">
                    <span>Desglose por horizonte (Cierre S -> Apertura S+1):</span> {hz_summary_txt}
                </div>
            </div>
        """
    else:
        c11_audit_card_html = """
            <div class="ia-audit-card full-width">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-icon">[C-11]</span>
                        <span class="ia-audit-title">Entrada Operable desde Apertura S+1 (Evaluacion C-11)</span>
                    </div>
                    <span class="tag-badge tag-cyan">N = 0 EVALUADAS</span>
                </div>
                <div class="ia-audit-desc">
                    Comparativa entre el acierto medido desde el cierre de emision (S) y desde la apertura operable de la sesion habil siguiente (S+1) pendiente de senales evaluadas.
                </div>
            </div>
        """

    n_ev_c12 = int(res_c12.get("n_evaluadas") or 0)
    k_c12 = float(res_c12.get("k_sigma") or 0.50)
    t_v1_p_c12 = res_c12.get("tasa_acierto_v1_precio")
    t_v2_p_c12 = res_c12.get("tasa_acierto_v2_precio")
    t_v2_rt_c12 = res_c12.get("tasa_acierto_v2_retorno_total")
    t_v2_ap_c12 = res_c12.get("tasa_acierto_v2_apertura")
    lat_v1_g = res_c12.get("pct_lateral_real_v1")
    lat_v2_g = res_c12.get("pct_lateral_real_v2")
    rng_v1_g = res_c12.get("rango_intertercil_lateral_v1_pp")
    rng_v2_g = res_c12.get("rango_intertercil_lateral_v2_pp")
    terciles_g = res_c12.get("terciles_sigma") or []

    if (
        n_ev_c12 > 0
        and t_v1_p_c12 is not None
        and t_v2_p_c12 is not None
        and lat_v1_g is not None
        and lat_v2_g is not None
    ):
        terc_audit_parts = []
        for t_row in terciles_g:
            nom_t = t_row.get("tercil", "")
            nt = int(t_row.get("n") or 0)
            l1 = t_row.get("pct_lateral_v1")
            l2 = t_row.get("pct_lateral_v2")
            a1 = t_row.get("tasa_acierto_v1")
            a2 = t_row.get("tasa_acierto_v2")
            if nt > 0 and l1 is not None and l2 is not None and a1 is not None and a2 is not None:
                terc_audit_parts.append(
                    f"<b>{nom_t}</b> (n={nt}): LATERAL v1 {l1:.2f}% -> v2 {l2:.2f}% (acierto {a1:.2f}% -> {a2:.2f}%)"
                )
        terc_audit_txt = " &middot; ".join(terc_audit_parts)
        rng_audit_txt = (
            f"El rango intertercil |T1 - T3| de clase real LATERAL pasa de <b>{rng_v1_g:.2f} pp</b> en v1 a <b>{rng_v2_g:.2f} pp</b> en v2."
            if (rng_v1_g is not None and rng_v2_g is not None)
            else ""
        )
        rt_ap_v2_txt = (
            f"Bajo v2, el Acierto en 3 clases en retorno total es <b>{t_v2_rt_c12:.2f}%</b> y desde apertura S+1 es <b>{t_v2_ap_c12:.2f}%</b>."
            if (t_v2_rt_c12 is not None and t_v2_ap_c12 is not None)
            else ""
        )
        et_c12_aud = _html_c09.escape(res_c12.get("etiqueta_muestra") or "")
        signo_c12_txt = (
            f" <b>Acierto de signo</b> (ALC/BAJ, n={int(res_c12.get('n_direccionales') or 0)}, identico en v1 y v2):"
            f" <b>{_p_c09(res_c12.get('tasa_acierto_signo'))}</b>"
            f" (Ref. azar: 50.0% &middot; Subidas: {_p_c09(res_c12.get('ref_frecuencia_subida_pct'))})."
        )
        c12_audit_card_html = f"""
            <div class="ia-audit-card full-width">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-icon">[C-12]</span>
                        <span class="ia-audit-title">Zona Muerta Escalada por Volatilidad (Regla v2: k * sigma * sqrt(dias), k={k_c12:.2f}, MEDIDO)</span>
                    </div>
                    <span class="tag-badge tag-cyan">N = {n_ev_c12} EVALUADAS</span>
                </div>
                <div class="ia-audit-desc">
                    La Regla v1 aplica umbrales fijos por horizonte (0.8%, 1.2%, 1.8%, 3.0%), haciendo que la proporcion de observaciones con clase real LATERAL dependa de la volatilidad del valor.
                    La Regla v2 escala el umbral con la volatilidad diaria emitida (<b>umbral_v2 = {k_c12:.2f} * sigma_pct * sqrt(dias)</b>).
                    En el conjunto evaluado (<b>{et_c12_aud}</b>), la proporcion global de clase real LATERAL (referencia ingenua "siempre lateral" de cada regla) pasa de <b>{lat_v1_g:.2f}%</b> (v1) a <b>{lat_v2_g:.2f}%</b> (v2), y el <b>Acierto en 3 clases</b> en precio pasa de <b>{t_v1_p_c12:.2f}%</b> (v1) a <b>{t_v2_p_c12:.2f}%</b> (v2). {rt_ap_v2_txt} {rng_audit_txt}{signo_c12_txt}
                </div>
                <div class="ia-audit-highlight cyan">
                    <span>Proporcion LATERAL por terciles de sigma (v1 -> v2):</span> {terc_audit_txt}
                </div>
            </div>
        """
    else:
        c12_audit_card_html = """
            <div class="ia-audit-card full-width">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-icon">[C-12]</span>
                        <span class="ia-audit-title">Zona Muerta Escalada por Volatilidad (Regla v2: k * sigma * sqrt(dias))</span>
                    </div>
                    <span class="tag-badge tag-cyan">N = 0 EVALUADAS</span>
                </div>
                <div class="ia-audit-desc">
                    Comparativa entre Regla v1 (umbrales fijos) y Regla v2 (umbral escalado por volatilidad k * sigma * sqrt(dias)) por terciles de volatilidad pendiente de senales evaluadas.
                </div>
            </div>
        """

    audit_html = f"""
    <div class="ia-audit-container">
        <!-- ── Tira Ejecutiva de Certificación (4 KPIs Simétricos) ── -->
        <div class="ia-audit-hero-row">
            <div class="ia-hist-kpi-card">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">Certificación</span>
                </div>
                <div class="ia-hist-kpi-value cyan">SELLADO</div>
                <div class="ia-hist-kpi-caption">Auditado &amp; Congelado</div>
            </div>
            <div class="ia-hist-kpi-card">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">Régimen Terminal</span>
                </div>
                <div class="ia-hist-kpi-value cyan">DESCRIPTIVO</div>
                <div class="ia-hist-kpi-caption">Estadística observada pura</div>
            </div>
            <div class="ia-hist-kpi-card">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">Fricción Real</span>
                </div>
                <div class="ia-hist-kpi-value amber">&ge; 20 PB</div>
                <div class="ia-hist-kpi-caption">Comisiones + Horquillas BME</div>
            </div>
            <div class="ia-hist-kpi-card">
                <div class="ia-hist-kpi-head">
                    <span class="ia-hist-kpi-title">Hipótesis Nula</span>
                </div>
                <div class="ia-hist-kpi-value neg">AZAR (~49%)</div>
                <div class="ia-hist-kpi-caption">Acierto de signo backtest WF vs ref. azar 50%</div>
            </div>
        </div>

        <!-- ── Grid Simétrico 2x2 de Hallazgos Econométricos + Conclusión Full Width ── -->
        <div class="ia-audit-grid">
            <div class="ia-audit-card">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-title">Fricción y Costes Reales de Mercado</span>
                    </div>
                    <span class="tag-badge tag-amber">FRICCIÓN &ge; 20 PB</span>
                </div>
                <div class="ia-audit-desc">
                    La magnitud de las micro-variaciones observadas es absorbida íntegramente por los costes reales de mercado (comisiones de intermediación, horquillas bid-ask e impuestos sobre transacciones), impidiendo una explotación económica viable en el horizonte evaluado.
                </div>
                <div class="ia-audit-highlight amber">
                    <span>Impacto cuantitativo:</span> 100% de la señal absorbida por horquillas y corretaje
                </div>
            </div>

            <div class="ia-audit-card">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-title">Acierto de Signo Similar al Azar (Backtest Walk-Forward)</span>
                    </div>
                    <span class="tag-badge tag-cyan">HIT RATE ~49.0%</span>
                </div>
                <div class="ia-audit-desc">
                    En el <b>backtest walk-forward</b> archivado (observaciones del backtest, no la muestra viva), el <b>Acierto de signo</b> direccional fue ~49.0%, estadisticamente indistinguible de la referencia azar del 50% y por debajo de la base historica de subidas del mercado (50-51%), sin evidencia de alpha predictivo sostenible.
                    Esta cifra es distinta del Acierto en 3 clases y del Acierto de signo de la muestra viva sellada: muestra viva {vivo_signo_txt} [{et_vivo}].
                </div>
                <div class="ia-audit-highlight cyan">
                    <span>Contraste empírico:</span> Distribución binomial indistinguible del azar (p &gt; 0.05)
                </div>
            </div>

            <div class="ia-audit-card">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-title">'Siempre Lateral' Supera al Motor</span>
                    </div>
                    <span class="tag-badge tag-cyan">BENCHMARK INGENUO</span>
                </div>
                <div class="ia-audit-desc">
                    En <b>Acierto en 3 clases</b>, la regla pasiva e ingenua de predecir siempre LATERAL es la referencia a batir. En el backtest supero a las senales dinamicas del modelo en los mismos horizontes y activos.
                    Muestra viva sellada: {vivo_3c_txt} [{et_vivo}].
                </div>
                <div class="ia-audit-highlight cyan">
                    <span>Regla pasiva:</span> Acierto base inercial superior a predicción activa
                </div>
            </div>

            <div class="ia-audit-card">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-title">Criterio Fuera de Muestra (OOS)</span>
                    </div>
                    <span class="tag-badge tag-amber">OOS NO SUPERADO</span>
                </div>
                <div class="ia-audit-desc">
                    Ninguno de los modelos estadísticos evaluados superó los umbrales de validación econométrica prefijados para el radar de riesgo fuera de muestra (out-of-sample) en periodos de volatilidad cambiante.
                </div>
                <div class="ia-audit-highlight amber">
                    <span>Validación OOS:</span> Sobreajuste histórico detectado en backtests clásicos
                </div>
            </div>

{vivo_card_html}

{c14_audit_card_html}

{c11_audit_card_html}

{c12_audit_card_html}

            <div class="ia-audit-card full-width">
                <div class="ia-audit-head">
                    <div class="ia-audit-title-wrap">
                        <span class="ia-audit-title">Marco Regulatorio: Panel Descriptivo, No Predictivo</span>
                    </div>
                    <span class="tag-badge tag-emerald">ESTADÍSTICA OBSERVADA</span>
                </div>
                <div class="ia-audit-desc">
                    Las herramientas de esta terminal ofrecen métricas cuantitativas del comportamiento histórico observado, microestructura de liquidez y perfiles de volatilidad real; <b>no constituyen pronósticos de precios ni recomendaciones de inversión</b>. La honestidad econométrica es el pilar central de StockLab.
                </div>
                <div class="ia-audit-highlight emerald">
                    <span>Propósito institucional:</span> Máximo rigor científico, transparencia y separación estricta de sesgos
                </div>
            </div>
        </div>

        <!-- ── Pie Sellado Institucional ── -->
        <div class="ia-audit-footer">
            El detalle técnico econométrico completo, contrastes estadísticos de Diebold-Mariano y matrices de transición auditadas se encuentran formalmente archivados en el <b>Addendum Técnico Institucional v5</b>.
        </div>
    </div>
    """
    st.html(audit_html)


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

        # 0. Catch-up de emisión si se perdió el inicio programado de las 18:35 CET
        try:
            from services.emision import catch_up_emision
            catch_up_emision()
        except Exception:
            pass

        # 1. Radar predictivo multitemporal (cacheado con @st.cache_data 5 min)
        radar = signal_engine.calcular_radar(tickers, nombres, tilts)

        # 2. Tracking de predicciones: inicialización de tablas (emisión delegada en catch_up_emision)
        try:
            db_pred.init_predicciones_table()
            st.session_state["_ia_tracking_sincronizado"] = True
        except Exception:
            pass

        # 3. Fundamentales en disco para el Asistente (@st.cache_data 12h)
        try:
            assistant_engine.cargar_fundamentales_disco(tickers)
        except Exception:
            pass

        # 4. Caché rápido de históricos y tablas ligeras del área IA
        #    (evita el bucle sin caché de 5 horizontes × ~13s = ~66s en el arranque)
        try:
            cargar_datos_cache_historico()
            _ia_obtener_resueltas("Todos", limite=250)
            _ia_obtener_pendientes("Todos", solo_vigentes=True, limite=1500)
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
    empresas_raw = query_all(
        "SELECT ticker, nombre, es_ibex35 FROM empresas WHERE activa = 1 ORDER BY nombre"
    )
    tickers = tuple(e["ticker"] for e in empresas_raw)
    nombres = tuple(e["nombre"] for e in empresas_raw)
    empresas_dict = {e["ticker"]: e["nombre"] for e in empresas_raw}
    ibex_set = set(e["ticker"] for e in empresas_raw if e.get("es_ibex35") == 1)
    tilts = _cargar_tilts(tickers)

    # ── Radar + tracking (registro y resolución automática para el laboratorio) ──
    radar = signal_engine.calcular_radar(tickers, nombres, tilts)
    if not st.session_state.get("_ia_tracking_sincronizado"):
        try:
            db_pred.init_predicciones_table()
            from services.emision import catch_up_emision
            catch_up_emision()
            st.session_state["_ia_tracking_sincronizado"] = True
        except Exception:
            pass

    # ── Contexto compartido para el asistente conversacional web ─────
    ctx = {
        "radar": radar,
        "tickers": tickers,
        "nombre2ticker": dict(zip(nombres, tickers)),
        "ticker2nombre": dict(zip(tickers, nombres)),
        "fundamentales": assistant_engine.cargar_fundamentales_disco(tickers),
    }

    # 1. Agente Conversacional de IA (búsqueda web, prompt bar y FAQ) intacto
    _seccion_agente(ctx)

    st.markdown('<div class="ia-divider"></div>', unsafe_allow_html=True)

    # 2. Pestañas del Área IA con ejecución perezosa (solo calcula la pestaña activa
    #    y conserva el estado al filtrar dentro del Laboratorio o abrir inspección)
    if st.query_params.get("ia_eye") or st.session_state.get("ia_eye_item"):
        st.session_state["ia_main_tabs"] = "Laboratorio (experimental)"

    tab_analisis, tab_lab, tab_auditoria = st.tabs(
        [
            "Panel de Análisis",
            "Laboratorio (experimental)",
            "Auditoría y Metodología",
        ],
        key="ia_main_tabs",
        on_change="rerun",
    )

    with tab_analisis:
        if getattr(tab_analisis, "open", True):
            closes, highs, lows, vols = cargar_datos_cache_historico()
            _render_panel_descriptivo(closes, highs, lows, vols, empresas_dict, ibex_set)

    with tab_lab:
        if getattr(tab_lab, "open", True):
            _seccion_radar(radar, tickers, nombres, tilts)
            st.markdown('<div class="ia-divider"></div>', unsafe_allow_html=True)
            _seccion_historial(radar=radar)

    with tab_auditoria:
        if getattr(tab_auditoria, "open", True):
            _render_auditoria_metodologia()

    # ── Disclaimer compacto ──────────────────────────────────────────
    st.markdown(
        '<div class="ia-disclaimer"><b>Aviso:</b> La información aquí provista tiene carácter estrictamente analítico y cuantitativo. '
        'No constituye asesoramiento financiero ni recomendación de inversión. '
        'Rentabilidades pasadas no garantizan rendimientos futuros.</div>',
        unsafe_allow_html=True,
    )
