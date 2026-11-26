"""
Módulo Mi Cartera V2.0 - StockLab Premium

Sistema completo de gestión de patrimonio con diseño institucional
de nivel Bloomberg/TradingView. Incluye:
- Pestañas superiores (Cartera Simulada / Cartera Real)
- KPI hero + secondary + tertiary con jerarquía real
- Gráfico de evolución del patrimonio (posición elevada)
- Gráficos de distribución por sectores y empresas
- Cards compactas de posiciones (-40% altura)
- Insights rápidos en línea horizontal
- Formulario de operaciones activado por CTA
- Diagnóstico como barra de estado inline
- Historial colapsable
"""

import html as _html
import time
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, date, timedelta
from database import (
    agregar_operacion,
    obtener_operaciones,
    actualizar_operacion,
    eliminar_operacion,
    calcular_posiciones_abiertas_filtradas,
    calcular_patrimonio_total_filtrado,
    calcular_desglose_resultados_filtrado,
    calcular_liquidez_filtrada,
    obtener_empresas_disponibles,
    obtener_historial_patrimonio,
    obtener_distribucion_sectores_filtrada,
    obtener_distribucion_empresas_filtrada,
    query_all,
    obtener_precios_actuales_batch
)
import market_data
from components.stock_chart import render_stock_chart


# ── CSS Premium Institucional v8 — Finance Terminal Layout ─────────────────────
STYLES = """
<style>
    /* === STOCKLAB CARTERA - Finance Terminal Premium v8 === */

    /* Google Font: cargada centralizadamente en app.py */

    /* --- Animations --- */
    @keyframes fade-in-up {
        from { opacity: 0; transform: translateY(8px); }
        to   { opacity: 1; transform: translateY(0);    }
    }
    @keyframes led-breathe {
        0%, 100% { opacity: 0.4; }
        50%       { opacity: 0.85; }
    }

    /* --- Global Foundation --- */
    .block-container {
        padding-top: 1.6rem !important;
        padding-bottom: 3rem !important;
        font-family: 'Inter', sans-serif !important;
    }
    ::-webkit-scrollbar { width: 5px !important; height: 5px !important; }
    ::-webkit-scrollbar-track { background: #0d121c !important; }
    ::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.20) !important; border-radius: 999px !important; }

    /* --- MODULE HEADER - Premium Finance Banner --- */
    .cartera-page-header {
        display: flex;
        align-items: baseline;
        flex-wrap: wrap;
        gap: 4px 14px;
    }

    /* --- KPI HERO CARD --- */
    .kpi-hero {
        background:
            radial-gradient(ellipse 75% 80% at 50% -35%, rgba(56, 189, 248, 0.18), transparent 68%),
            radial-gradient(ellipse 55% 60% at 85% 110%, rgba(99, 102, 241, 0.10), transparent 65%),
            var(--sl-surface-glass);
        border: 1px solid rgba(56, 189, 248, 0.34);
        border-radius: var(--sl-radius-xl);
        padding: 26px 32px;
        margin-bottom: 10px;
        position: relative;
        overflow: hidden;
        animation: fade-in-up 0.25s var(--sl-ease) both;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.38), 0 0 32px -12px rgba(56, 189, 248, 0.24), inset 0 1px 0 rgba(255, 255, 255, 0.08);
        transition: border-color 240ms var(--sl-ease), box-shadow 240ms var(--sl-ease);
    }
    .kpi-hero:hover {
        border-color: rgba(56, 189, 248, 0.50);
        box-shadow: 0 8px 28px rgba(0, 0, 0, 0.44), 0 0 38px -10px rgba(56, 189, 248, 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.10);
    }
    .kpi-hero::after {
        content: "";
        position: absolute;
        top: 0;
        left: 16%;
        right: 16%;
        height: 1.5px;
        background: linear-gradient(90deg, transparent, #7dd3fc, #818cf8, transparent);
        pointer-events: none;
    }
    .kpi-hero::before {
        content: "";
        position: absolute;
        inset: 0;
        pointer-events: none;
        background-image:
            linear-gradient(var(--sl-border-subtle) 1px, transparent 1px),
            linear-gradient(90deg, var(--sl-border-subtle) 1px, transparent 1px);
        background-size: 28px 28px;
        mask-image: linear-gradient(90deg, #000, transparent 32% 68%, #000);
        opacity: 0.55;
    }
    .kpi-hero-label {
        font-family: var(--sl-font-body);
        font-size: 0.72rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 1.6px;
        color: var(--sl-text-secondary);
        margin-bottom: 6px;
    }
    .kpi-hero-value {
        font-family: var(--sl-font-mono);
        font-size: clamp(1.7rem, 4.4vw, 2.9rem);
        font-weight: 700;
        letter-spacing: -0.05em;
        color: #7dd3fc;
        text-shadow: 0 0 28px rgba(56, 189, 248, 0.32);
        line-height: 1;
    }
    .kpi-hero-perf {
        font-family: 'Inter', sans-serif;
        font-size: 0.85rem;
        font-weight: 700;
        margin-top: 4px;
        display: inline-flex;
        align-items: center;
        gap: 4px;
    }
    .kpi-hero-perf.positive { color: #34d399; }
    .kpi-hero-perf.negative { color: #f87171; }
    
    .kpi-hero-sub {
        font-family: 'Inter', sans-serif;
        font-size: 0.72rem;
        color: var(--sl-text-muted);
        letter-spacing: 0.2px;
    }

    /* --- KPI METRICS - Secondary / Tertiary --- */
    div[data-testid="stMetric"],
    [data-testid="metric-container"],
    .stMetric,
    div[class*="stMetric"],
    .stMetricCustom {
        background: var(--sl-surface-glass) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: var(--sl-radius-lg) !important;
        padding: 14px 18px !important;
        min-height: 98px !important;
        display: flex !important;
        flex-direction: column !important;
        justify-content: center !important;
        transition: transform 240ms var(--sl-ease), border-color 240ms var(--sl-ease), box-shadow 240ms var(--sl-ease) !important;
        box-shadow: var(--sl-shadow-card) !important;
        position: relative !important;
        overflow: hidden !important;
    }
    div[data-testid="stMetric"]::before,
    .stMetric::before,
    div[class*="stMetric"]::before,
    .stMetricCustom::before {
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
    div[data-testid="stMetric"]:hover,
    .stMetric:hover,
    div[class*="stMetric"]:hover,
    .stMetricCustom:hover {
        border-color: var(--sl-border-hover) !important;
        box-shadow: var(--sl-shadow-hover) !important;
        transform: translateY(-2px) !important;
    }
    div[data-testid="stMetric"]:hover::before,
    .stMetric:hover::before,
    .stMetricCustom:hover::before {
        left: 8% !important;
        right: 8% !important;
        opacity: 1 !important;
    }

    div[data-testid="stMetric"] label,
    .stMetric label,
    div[class*="stMetric"] label,
    .stMetricCustom label {
        color: var(--sl-text-secondary) !important;
        font-size: 0.68rem !important;
        font-weight: 600 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.08em !important;
        font-family: 'Inter', sans-serif !important;
        margin-bottom: 4px !important;
    }

    div[data-testid="stMetric"] div[data-testid="stMetricValue"],
    .stMetric div[class*="stMetricValue"],
    div[class*="stMetric"] [data-testid="stMetricValue"],
    .stMetricCustom .metric-value-container {
        color: #38bdf8 !important;
        font-weight: 800 !important;
        font-size: 1.15rem !important;
        font-family: 'JetBrains Mono', monospace !important;
        letter-spacing: -0.5px !important;
        text-shadow: none !important;
        display: flex !important;
        align-items: baseline !important;
        gap: 8px !important;
    }

    /* Custom Metric Value inline delta styling */
    .stMetricCustom .value-main {
        color: #38bdf8;
        text-shadow: none;
    }
    .stMetricCustom .value-delta {
        font-size: 0.78rem !important;
        font-weight: 700 !important;
        font-family: 'JetBrains Mono', monospace !important;
    }
    .stMetricCustom .value-delta.positive {
        color: #34d399 !important;
        text-shadow: none !important;
    }
    .stMetricCustom .value-delta.negative {
        color: #f87171 !important;
        text-shadow: none !important;
    }

    /* Dynamic Green LED value */
    div[data-testid="stMetric"]:has(div[data-testid="stMetricDelta"] svg[style*="fill: rgb(9, 171, 59)"]) div[data-testid="stMetricValue"],
    div[data-testid="stMetric"]:has(div[data-testid="stMetricDelta"][style*="color: rgb(9, 171, 59)"]) div[data-testid="stMetricValue"],
    .stMetric:has(span[class*="stMetricDelta"][style*="color: rgb(9, 171, 59)"]) div[class*="stMetricValue"] {
        color: #34d399 !important;
        text-shadow: none !important;
    }

    /* Dynamic Red LED value */
    div[data-testid="stMetric"]:has(div[data-testid="stMetricDelta"] svg[style*="fill: rgb(255, 43, 43)"]) div[data-testid="stMetricValue"],
    div[data-testid="stMetric"]:has(div[data-testid="stMetricDelta"][style*="color: rgb(255, 43, 43)"]) div[data-testid="stMetricValue"],
    .stMetric:has(span[class*="stMetricDelta"][style*="color: rgb(255, 43, 43)"]) div[class*="stMetricValue"] {
        color: #f87171 !important;
        text-shadow: none !important;
    }

    div[data-testid="stMetric"] div[data-testid="stMetricDelta"],
    .stMetric div[class*="stMetricDelta"],
    div[class*="stMetric"] [data-testid="stMetricDelta"] {
        font-size: 0.78rem !important;
        font-weight: 700 !important;
        font-family: 'JetBrains Mono', monospace !important;
    }

    /* --- STATUS STRIP — Diagnostic inline bar --- */
    .status-strip {
        background: linear-gradient(135deg, rgba(11,14,22,0.6), rgba(9,12,18,0.7)) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 8px !important;
        padding: 8px 16px !important;
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        flex-wrap: wrap !important;
        gap: 0 !important;
        margin: 4px 0 8px !important;
    }
    .status-item {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-family: 'Inter', sans-serif;
        font-size: 0.68rem;
        font-weight: 500;
        color: #94a3b8;
        letter-spacing: 0.4px;
        white-space: nowrap;
    }
    .status-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        display: inline-block;
    }
    .status-dot.green { background: #34d399; box-shadow: 0 0 6px rgba(52,211,153,0.5); }
    .status-dot.red   { background: #f87171; box-shadow: 0 0 6px rgba(248,113,113,0.5); }
    .status-dot.amber { background: #fbbf24; box-shadow: 0 0 6px rgba(251,191,36,0.5); }
    .status-dot.gray  { background: #64748b; }
    
    .status-sep { color: #1e293b; margin: 0 16px; font-size: 0.75rem; }

    /* --- INSIGHTS STRIP — Positions summary inline --- */
    .insights-strip {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 0;
        padding: 8px 16px;
        margin-bottom: 8px;
        background: linear-gradient(135deg, rgba(11,14,22,0.7), rgba(9,12,18,0.8));
        border: 1px solid rgba(148, 163, 184, 0.12);
        border-radius: 8px;
    }
    .insight-item {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.76rem;
        color: #8fa0b5;
        white-space: nowrap;
    }
    .insight-ticker { color: #c8d8e8; font-weight: 700; }
    .insight-value  { font-weight: 700; }
    .insight-value.positive { color: #34d399; }
    .insight-value.negative { color: #f87171; }
    .insight-value.neutral  { color: #38bdf8; }
    .insight-sep { color: #2a3445; margin: 0 14px; font-size: 0.8rem; }

    /* --- POSITION CARDS - Compact Premium Trading Card --- */
    .pos-card {
        background: var(--sl-surface-glass) !important;
        border: 1px solid var(--sl-border-default) !important;
        border-radius: var(--sl-radius-lg) !important;
        padding: 14px 20px !important;
        margin-bottom: 0px !important; /* Managed by integrated buttons below */
        transition: border-color 220ms var(--sl-ease), box-shadow 220ms var(--sl-ease), transform 220ms var(--sl-ease) !important;
        position: relative !important;
        overflow: hidden !important;
        animation: fade-in-up 0.25s var(--sl-ease) both !important;
        box-shadow: var(--sl-shadow-card) !important;
    }
    .pos-card::before {
        content: "" !important;
        display: block !important;
        position: absolute !important;
        top: 0 !important;
        left: 16% !important;
        right: 16% !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.48), transparent) !important;
        opacity: 0.45 !important;
        transition: opacity 220ms var(--sl-ease), left 220ms var(--sl-ease), right 220ms var(--sl-ease) !important;
    }
    .pos-card::after {
        content: '' !important;
        position: absolute !important;
        top: 12% !important; bottom: 12% !important;
        left: 0 !important; width: 3px !important;
        background: linear-gradient(180deg, #38bdf8, #34d399) !important;
        border-radius: 0 3px 3px 0 !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.45) !important;
        animation: none !important;
    }
    .pos-card.profit { border-color: rgba(52, 211, 153, 0.20) !important; }
    .pos-card.profit::after {
        background: linear-gradient(180deg, #10b981, #34d399) !important;
        box-shadow: 0 0 12px rgba(52, 211, 153, 0.50) !important;
    }
    .pos-card.loss { border-color: rgba(248, 113, 113, 0.20) !important; }
    .pos-card.loss::after {
        background: linear-gradient(180deg, #ef4444, #f87171) !important;
        box-shadow: 0 0 12px rgba(248, 113, 113, 0.50) !important;
    }
    .pos-card.take-profit { border-color: rgba(52, 211, 153, 0.34) !important; }
    .pos-card.take-profit::after {
        background: linear-gradient(180deg, #10b981, #34d399) !important;
        top: 6% !important; bottom: 6% !important;
        box-shadow: 0 0 14px rgba(52, 211, 153, 0.60) !important;
        animation: none !important;
    }
    .pos-card.stop-loss { border-color: rgba(248, 113, 113, 0.34) !important; }
    .pos-card.stop-loss::after {
        background: linear-gradient(180deg, #ef4444, #f87171) !important;
        top: 6% !important; bottom: 6% !important;
        box-shadow: 0 0 14px rgba(248, 113, 113, 0.60) !important;
        animation: none !important;
    }
    .pos-card:hover {
        border-color: var(--sl-border-hover) !important;
        box-shadow: var(--sl-shadow-hover) !important;
    }
    .pos-card:hover::before { opacity: 1 !important; left: 8% !important; right: 8% !important; }

    /* Card sub-elements */
    .pos-card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
    .pos-ticker    { font-family:'JetBrains Mono',monospace; font-size:0.95rem; font-weight:700; color:#e4eaf4; letter-spacing:0.5px; }
    .pos-name      { font-family:var(--sl-font-body); font-size:0.73rem; color:var(--sl-text-secondary); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:220px; }

    .pos-alert-badge { padding:2px 8px; border-radius:6px; font-size:0.58rem; font-weight:700; font-family:'Inter',sans-serif; letter-spacing:0.4px; white-space:nowrap; }
    .pos-alert-badge.tp { background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.28); }
    .pos-alert-badge.sl { background: rgba(239,68,68,0.15);  color: #f87171; border: 1px solid rgba(239,68,68,0.28); }

    .pos-grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 4px 12px;
        margin-bottom: 6px;
    }
    .pos-field-label { font-family:var(--sl-font-body); font-size:0.64rem; font-weight:600; color:var(--sl-text-muted); text-transform:uppercase; letter-spacing:0.07em; margin-bottom:2px; }
    .pos-field-value { font-family:var(--sl-font-mono); font-size:0.84rem; font-weight:600; color:var(--sl-text-primary); }
    .pos-field-value.positive { color: #34d399; }
    .pos-field-value.negative { color: #f87171; }

    .pos-pnl-bar-wrap { background: rgba(255,255,255,0.03); border-radius: 2px; height: 2px; margin: 4px 0 6px; overflow: hidden; }
    .pos-pnl-bar      { height: 100%; border-radius: 2px; transition: width 0.6s cubic-bezier(0.25,0.46,0.45,0.94); }
    .pos-pnl-bar.green { background: linear-gradient(90deg, #065f46, #10b981, #34d399); }
    .pos-pnl-bar.red   { background: linear-gradient(90deg, #7f1d1d, #ef4444, #f87171); }

    .pos-card-footer { border-top: 1px solid rgba(255,255,255,0.03); padding-top: 6px; display: flex; justify-content: space-between; align-items: flex-end; }
    .pos-valor-label  { font-family:var(--sl-font-body); font-size:0.64rem; color:var(--sl-text-muted); text-transform:uppercase; letter-spacing:0.07em; }
    .pos-valor-num    { font-family:'JetBrains Mono',monospace; font-size:0.96rem; font-weight:700; color:#e4eaf4; margin-top:2px; }
    .pos-ganancia-num { font-family:'JetBrains Mono',monospace; font-size:0.82rem; font-weight:700; }
    .pos-ganancia-num.positive { color: #34d399; }
    .pos-ganancia-num.negative { color: #f87171; }

    /* Notes toggle using native details/summary */
    .pos-notes { margin-top: 4px; border-top: 1px solid rgba(255,255,255,0.03); padding-top: 4px; }
    .pos-notes summary {
        font-family: 'Inter', sans-serif;
        font-size: 0.63rem; font-weight: 600;
        color: var(--sl-text-muted); cursor: pointer;
        list-style: none; user-select: none;
        transition: color 0.2s ease;
    }
    .pos-notes summary::-webkit-details-marker { display: none; }
    .pos-notes summary:hover { color: #8fa0b5; }
    .pos-notes[open] summary { color: #8fa0b5; }
    .pos-notes-content {
        font-family: 'Inter', sans-serif;
        font-size: 0.69rem; font-style: italic;
        color: #718299; margin-top: 4px; line-height: 1.4; padding-left: 4px;
    }

    /* --- CHARTS - Plotly / Altair Containers --- */
    div[data-testid="stVegaLiteChart"],
    div[data-testid="stPlotlyChart"] {
        background: linear-gradient(160deg, rgba(12,16,26,0.65), rgba(8,11,18,0.8)) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 12px !important;
        padding: 12px !important;
        transition: border-color 0.25s ease !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
        position: relative !important;
        overflow: hidden !important;
    }
    div[data-testid="stVegaLiteChart"]:hover,
    div[data-testid="stPlotlyChart"]:hover {
        border-color: rgba(56, 189, 248, 0.28) !important;
    }

    /* --- BUTTONS - Premium Action Buttons --- */
    button:not(.st-key-topbar_nav *) {
        font-family: 'Inter', sans-serif !important;
    }
    div[data-testid="column"]:not(.st-key-topbar_nav *) button,
    .stButton:not(.st-key-topbar_nav *) button,
    div[class*="stButton"]:not(.st-key-topbar_nav *) button {
        background: linear-gradient(145deg, #182133, #131a29) !important;
        border: 1px solid rgba(100,116,139,0.18) !important;
        color: #8fa0b5 !important;
        border-radius: 8px !important;
        padding: 6px 12px !important;
        font-weight: 600 !important;
        font-size: 0.76rem !important;
        transition: all 0.2s cubic-bezier(0.25,0.46,0.45,0.94) !important;
        letter-spacing: 0.2px !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.2) !important;
    }
    div[data-testid="column"]:not(.st-key-topbar_nav *) button:hover,
    .stButton:not(.st-key-topbar_nav *) button:hover,
    div[class*="stButton"]:not(.st-key-topbar_nav *) button:hover {
        background: linear-gradient(145deg, #1a2038, #182133) !important;
        border-color: rgba(56, 189, 248, 0.28) !important;
        color: #38bdf8 !important;
        box-shadow: 0 4px 14px rgba(0,0,0,0.35) !important;
    }

    /* Integrated Buttons specifically for Position Cards */
    div:has(> .pos-card) + div {
        margin-top: -8px !important;
    }
    div:has(> .pos-card) + div button {
        border-top-left-radius: 0px !important;
        border-top-right-radius: 0px !important;
        border-bottom-left-radius: 8px !important;
        border-bottom-right-radius: 8px !important;
        border-top: none !important;
        padding: 6px 0 !important;
        font-size: 0.72rem !important;
        background: linear-gradient(145deg, #111723, #0d121c) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
    }
    div:has(> .pos-card) + div button:hover {
        background: linear-gradient(145deg, #14192b, #131a29) !important;
        border-color: rgba(56, 189, 248, 0.28) !important;
        color: #38bdf8 !important;
    }

    /* Primary buttons */
    button[kind="primary"]:not(.st-key-topbar_nav *),
    .stFormSubmitButton button,
    [data-testid="stFormSubmitButton"] button,
    button[class*="baseButton-primary"]:not(.st-key-topbar_nav *) {
        background: linear-gradient(135deg, #38bdf8 0%, #0284c7 100%) !important;
        border: 1px solid rgba(56, 189, 248, 0.30) !important;
        color: #ffffff !important;
        font-weight: 700 !important;
        letter-spacing: 0.2px !important;
        box-shadow: 0 4px 14px rgba(0,0,0,0.3) !important;
        animation: none !important;
    }
    button[kind="primary"]:not(.st-key-topbar_nav *):hover,
    .stFormSubmitButton button:hover,
    [data-testid="stFormSubmitButton"] button:hover,
    button[class*="baseButton-primary"]:not(.st-key-topbar_nav *):hover {
        background: linear-gradient(135deg, #38bdf8 0%, #0ea5e9 100%) !important;
        border-color: rgba(56, 189, 248, 0.39) !important;
        box-shadow: 0 6px 16px rgba(0,0,0,0.4) !important;
        transform: translateY(-1px) !important;
    }

    /* --- FORM - Dark Terminal Form --- */
    div[data-testid="stForm"],
    .stForm,
    form,
    div[class*="stForm"] {
        background: linear-gradient(160deg, rgba(13,17,27,0.85), rgba(9,12,19,0.95)) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 14px !important;
        padding: 20px 24px !important;
        box-shadow: 0 6px 32px rgba(0,0,0,0.45) !important;
        position: relative !important;
    }

    /* Universal Form Control Selectors */
    input, select, textarea, [data-baseweb="select"] {
        background-color: #0d121c !important;
        border: 1px solid rgba(100,116,139,0.15) !important;
        border-radius: 8px !important;
        color: #aab8ca !important;
        font-family: 'Inter', sans-serif !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
    }
    input:focus, select:focus, textarea:focus, [data-baseweb="select"]:focus-within {
        border-color: rgba(56, 189, 248, 0.35) !important;
        box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.06) !important;
    }

    /* Form & Selectbox label typography - 100% harmonized with Ranking */
    section[data-testid="stMain"] div[data-testid="stSelectbox"] label[data-testid="stWidgetLabel"],
    .stSelectbox label, .stMultiSelect label,
    div[data-testid="stNumberInput"] label,
    div[data-testid="stDateInput"] label,
    div[data-testid="stTextInput"] label,
    label[class*="st-"] {
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
    div[data-testid="stDateInput"] label p,
    div[data-testid="stTextInput"] label p {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.76rem !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
        margin: 0 !important;
        line-height: 1.4 !important;
    }

    /* --- BOTONES DEL SELECTOR DE CARTERA (SIMULADA / REAL) --- */
    .st-key-btn_cartera_simulada button,
    .st-key-btn_cartera_real button,
    section[data-testid="stMain"] .st-key-btn_cartera_simulada div.stButton button,
    section[data-testid="stMain"] .st-key-btn_cartera_real div.stButton button,
    section[data-testid="stMain"] .st-key-btn_cartera_simulada div[data-testid="stButton"] button,
    section[data-testid="stMain"] .st-key-btn_cartera_real div[data-testid="stButton"] button {
        background: var(--sl-bg-sunken) !important;
        border: 1px solid var(--sl-border-default) !important;
        color: var(--sl-text-secondary) !important;
        font-weight: 600 !important;
        font-family: var(--sl-font-body) !important;
        border-radius: 10px !important;
        font-size: 0.80rem !important;
        padding: 9px 14px !important;
        white-space: nowrap !important;
        transition: all 200ms var(--sl-ease) !important;
        margin: 0 !important;
        width: 100% !important;
        box-shadow: none !important;
    }

    .st-key-btn_cartera_simulada button *,
    .st-key-btn_cartera_real button *,
    section[data-testid="stMain"] .st-key-btn_cartera_simulada div.stButton button *,
    section[data-testid="stMain"] .st-key-btn_cartera_real div.stButton button * {
        color: var(--sl-text-secondary) !important;
        font-weight: 600 !important;
        white-space: nowrap !important;
    }

    .st-key-btn_cartera_simulada button:hover,
    .st-key-btn_cartera_real button:hover,
    section[data-testid="stMain"] .st-key-btn_cartera_simulada div.stButton button:hover,
    section[data-testid="stMain"] .st-key-btn_cartera_real div.stButton button:hover {
        background: rgba(56, 189, 248, 0.08) !important;
        border-color: rgba(56, 189, 248, 0.34) !important;
    }

    .st-key-btn_cartera_simulada button:hover *,
    .st-key-btn_cartera_real button:hover * {
        color: #f8fafc !important;
    }

    /* CARTERA ACTIVA / SELECCIONADA (Resplandor Cyan Neón Sutil) */
    .st-key-btn_cartera_simulada button[kind="primary"],
    .st-key-btn_cartera_real button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_cartera_simulada div.stButton button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_cartera_real div.stButton button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_cartera_simulada div[data-testid="stButton"] button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_cartera_real div[data-testid="stButton"] button[data-testid="baseButton-primary"] {
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.20) 0%, rgba(99, 102, 241, 0.12) 100%) !important;
        border: 1.5px solid rgba(56, 189, 248, 0.65) !important;
        box-shadow: 0 0 22px -6px rgba(56, 189, 248, 0.50), inset 0 1px 0 rgba(255, 255, 255, 0.14) !important;
    }

    .st-key-btn_cartera_simulada button[kind="primary"] *,
    .st-key-btn_cartera_real button[kind="primary"] *,
    section[data-testid="stMain"] .st-key-btn_cartera_simulada div[data-testid="stButton"] button[data-testid="baseButton-primary"] *,
    section[data-testid="stMain"] .st-key-btn_cartera_real div[data-testid="stButton"] button[data-testid="baseButton-primary"] * {
        color: #ffffff !important;
        font-weight: 700 !important;
    }

    /* --- BOTONES DE FILTRO DE TIEMPO DE PATRIMONIO (1M, 6M, YTD, 1A, Todo) --- */
    .st-key-btn_filtro_1m button,
    .st-key-btn_filtro_6m button,
    .st-key-btn_filtro_ytd button,
    .st-key-btn_filtro_1a button,
    .st-key-btn_filtro_todo button,
    section[data-testid="stMain"] .st-key-btn_filtro_1m div.stButton button,
    section[data-testid="stMain"] .st-key-btn_filtro_6m div.stButton button,
    section[data-testid="stMain"] .st-key-btn_filtro_ytd div.stButton button,
    section[data-testid="stMain"] .st-key-btn_filtro_1a div.stButton button,
    section[data-testid="stMain"] .st-key-btn_filtro_todo div.stButton button {
        background: #111723 !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        color: #64748b !important;
        font-weight: 600 !important;
        font-family: 'Inter', sans-serif !important;
        border-radius: 8px !important;
        font-size: 0.80rem !important;
        padding: 6px 12px !important;
        transition: all 0.2s ease !important;
        margin: 0 !important;
        box-shadow: none !important;
    }

    .st-key-btn_filtro_1m button *,
    .st-key-btn_filtro_6m button *,
    .st-key-btn_filtro_ytd button *,
    .st-key-btn_filtro_1a button *,
    .st-key-btn_filtro_todo button * {
        color: #64748b !important;
        font-weight: 600 !important;
    }

    .st-key-btn_filtro_1m button:hover,
    .st-key-btn_filtro_6m button:hover,
    .st-key-btn_filtro_ytd button:hover,
    .st-key-btn_filtro_1a button:hover,
    .st-key-btn_filtro_todo button:hover {
        background: rgba(56, 189, 248, 0.04) !important;
        border-color: rgba(56, 189, 248, 0.2) !important;
    }

    .st-key-btn_filtro_1m button:hover *,
    .st-key-btn_filtro_6m button:hover *,
    .st-key-btn_filtro_ytd button:hover *,
    .st-key-btn_filtro_1a button:hover *,
    .st-key-btn_filtro_todo button:hover * {
        color: #94a3b8 !important;
    }

    /* FILTRO DE TIEMPO ACTIVO (Idéntico a Cartera Simulada / Real: Cyan Glow + Borde Cyan Neón) */
    .st-key-btn_filtro_1m button[kind="primary"],
    .st-key-btn_filtro_6m button[kind="primary"],
    .st-key-btn_filtro_ytd button[kind="primary"],
    .st-key-btn_filtro_1a button[kind="primary"],
    .st-key-btn_filtro_todo button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_1m div.stButton button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_6m div.stButton button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_ytd div.stButton button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_1a div.stButton button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_todo div.stButton button[kind="primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_1m div.stButton button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_6m div.stButton button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_ytd div.stButton button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_1a div.stButton button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_todo div.stButton button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_1m div[data-testid="stButton"] button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_6m div[data-testid="stButton"] button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_ytd div[data-testid="stButton"] button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_1a div[data-testid="stButton"] button[data-testid="baseButton-primary"],
    section[data-testid="stMain"] .st-key-btn_filtro_todo div[data-testid="stButton"] button[data-testid="baseButton-primary"] {
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.20) 0%, rgba(99, 102, 241, 0.12) 100%) !important;
        border: 1.5px solid rgba(56, 189, 248, 0.60) !important;
        box-shadow: 0 0 18px -5px rgba(56, 189, 248, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.12) !important;
    }

    .st-key-btn_filtro_1m button[kind="primary"] *,
    .st-key-btn_filtro_6m button[kind="primary"] *,
    .st-key-btn_filtro_ytd button[kind="primary"] *,
    .st-key-btn_filtro_1a button[kind="primary"] *,
    .st-key-btn_filtro_todo button[kind="primary"] *,
    section[data-testid="stMain"] .st-key-btn_filtro_1m div.stButton button[data-testid="baseButton-primary"] *,
    section[data-testid="stMain"] .st-key-btn_filtro_6m div.stButton button[data-testid="baseButton-primary"] *,
    section[data-testid="stMain"] .st-key-btn_filtro_ytd div.stButton button[data-testid="baseButton-primary"] *,
    section[data-testid="stMain"] .st-key-btn_filtro_1a div.stButton button[data-testid="baseButton-primary"] *,
    section[data-testid="stMain"] .st-key-btn_filtro_todo div.stButton button[data-testid="baseButton-primary"] * {
        color: #ffffff !important;
        font-weight: 700 !important;
    }

    /* --- BOTONES DEL SELECTOR DE TIPO DE OPERACIÓN EN EL DIÁLOGO/MODAL --- */
    .st-key-dlg_tipo_compra button,
    .st-key-dlg_tipo_venta button,
    .st-key-dlg_tipo_div button,
    .st-key-dlg_tipo_ingreso button,
    .st-key-dlg_tipo_retirada button,
    .st-key-dlg_tipo_comision button,
    div[data-testid="stDialog"] .st-key-dlg_tipo_compra div.stButton button,
    div[data-testid="stDialog"] .st-key-dlg_tipo_venta div.stButton button,
    div[data-testid="stDialog"] .st-key-dlg_tipo_div div.stButton button,
    div[data-testid="stDialog"] .st-key-dlg_tipo_ingreso div.stButton button,
    div[data-testid="stDialog"] .st-key-dlg_tipo_retirada div.stButton button,
    div[data-testid="stDialog"] .st-key-dlg_tipo_comision div.stButton button {
        background: #111723 !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        color: #64748b !important;
        font-weight: 600 !important;
        font-family: 'Inter', sans-serif !important;
        border-radius: 8px !important;
        font-size: 0.78rem !important;
        padding: 8px 4px !important;
        transition: all 0.2s ease !important;
        margin: 0 !important;
        box-shadow: none !important;
    }

    .st-key-dlg_tipo_compra button *,
    .st-key-dlg_tipo_venta button *,
    .st-key-dlg_tipo_div button *,
    .st-key-dlg_tipo_ingreso button *,
    .st-key-dlg_tipo_retirada button *,
    .st-key-dlg_tipo_comision button * {
        color: #64748b !important;
        font-weight: 600 !important;
    }

    .st-key-dlg_tipo_compra button:hover,
    .st-key-dlg_tipo_venta button:hover,
    .st-key-dlg_tipo_div button:hover,
    .st-key-dlg_tipo_ingreso button:hover,
    .st-key-dlg_tipo_retirada button:hover,
    .st-key-dlg_tipo_comision button:hover {
        background: rgba(56, 189, 248, 0.04) !important;
        border-color: rgba(56, 189, 248, 0.2) !important;
    }

    .st-key-dlg_tipo_compra button:hover *,
    .st-key-dlg_tipo_venta button:hover *,
    .st-key-dlg_tipo_div button:hover *,
    .st-key-dlg_tipo_ingreso button:hover *,
    .st-key-dlg_tipo_retirada button:hover *,
    .st-key-dlg_tipo_comision button:hover * {
        color: #94a3b8 !important;
    }

    /* BOTÓN TIPO DE OPERACIÓN ACTIVO (Resplandor Cyan Neón Destacado) */
    .st-key-dlg_tipo_compra button[kind="primary"],
    .st-key-dlg_tipo_venta button[kind="primary"],
    .st-key-dlg_tipo_div button[kind="primary"],
    .st-key-dlg_tipo_ingreso button[kind="primary"],
    .st-key-dlg_tipo_retirada button[kind="primary"],
    .st-key-dlg_tipo_comision button[kind="primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_compra div.stButton button[kind="primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_venta div.stButton button[kind="primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_div div.stButton button[kind="primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_ingreso div.stButton button[kind="primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_retirada div.stButton button[kind="primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_comision div.stButton button[kind="primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_compra div.stButton button[data-testid="baseButton-primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_venta div.stButton button[data-testid="baseButton-primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_div div.stButton button[data-testid="baseButton-primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_ingreso div.stButton button[data-testid="baseButton-primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_retirada div.stButton button[data-testid="baseButton-primary"],
    div[data-testid="stDialog"] .st-key-dlg_tipo_comision div.stButton button[data-testid="baseButton-primary"] {
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.18) 0%, rgba(16, 185, 129, 0.10) 100%) !important;
        border: 1.5px solid #38bdf8 !important;
        box-shadow: none !important;
    }

    .st-key-dlg_tipo_compra button[kind="primary"] *,
    .st-key-dlg_tipo_venta button[kind="primary"] *,
    .st-key-dlg_tipo_div button[kind="primary"] *,
    .st-key-dlg_tipo_ingreso button[kind="primary"] *,
    .st-key-dlg_tipo_retirada button[kind="primary"] *,
    .st-key-dlg_tipo_comision button[kind="primary"] *,
    div[data-testid="stDialog"] .st-key-dlg_tipo_compra div.stButton button[data-testid="baseButton-primary"] *,
    div[data-testid="stDialog"] .st-key-dlg_tipo_venta div.stButton button[data-testid="baseButton-primary"] *,
    div[data-testid="stDialog"] .st-key-dlg_tipo_div div.stButton button[data-testid="baseButton-primary"] *,
    div[data-testid="stDialog"] .st-key-dlg_tipo_ingreso div.stButton button[data-testid="baseButton-primary"] *,
    div[data-testid="stDialog"] .st-key-dlg_tipo_retirada div.stButton button[data-testid="baseButton-primary"] *,
    div[data-testid="stDialog"] .st-key-dlg_tipo_comision div.stButton button[data-testid="baseButton-primary"] * {
        color: #ffffff !important;
        font-weight: 700 !important;
    }

    /* --- CTA BOTÓN NUEVA OPERACIÓN (Resplandor Progresivo Ámbar estilo SCORE FINAL de Ranking) --- */
    @keyframes glow-pulse-amber-cta {
        0%, 100% {
            box-shadow: none;
            border-color: rgba(245, 158, 11, 0.45);
        }
        50% {
            box-shadow: none;
            border-color: rgba(245, 158, 11, 0.95);
        }
    }

    .st-key-btn_cta_nueva_op button,
    section[data-testid="stMain"] .st-key-btn_cta_nueva_op div.stButton button,
    section[data-testid="stMain"] .st-key-btn_cta_nueva_op div[data-testid="stButton"] button {
        background: linear-gradient(135deg, rgba(245, 158, 11, 0.14) 0%, rgba(245, 158, 11, 0.05) 100%) !important;
        border-width: 1px !important;
        border-style: solid !important;
        border-color: rgba(245, 158, 11, 0.45);
        color: #fbbf24 !important;
        font-weight: 700 !important;
        font-size: 0.92rem !important;
        letter-spacing: 0.02em !important;
        border-radius: 10px !important;
        padding: 10px 24px !important;
        animation: none !important;
        transition: transform 0.2s ease !important;
    }

    .st-key-btn_cta_nueva_op button *,
    section[data-testid="stMain"] .st-key-btn_cta_nueva_op div.stButton button * {
        color: #fbbf24 !important;
        font-weight: 700 !important;
    }

    .st-key-btn_cta_nueva_op button:hover,
    section[data-testid="stMain"] .st-key-btn_cta_nueva_op div.stButton button:hover {
        background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%) !important;
        border-color: #fbbf24 !important;
        box-shadow: 0 0 24px -5px rgba(245, 158, 11, 0.55) !important;
        transform: translateY(-2px) !important;
    }

    .st-key-btn_cta_nueva_op button:hover * {
        color: #ffffff !important;
    }

    /* Selector premium de tipos de operación en el formulario */
    form div[data-testid="stSegmentedControl"],
    div[data-testid="stForm"] div[data-testid="stSegmentedControl"] {
        background: rgba(8,10,18,0.95) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 12px !important;
        padding: 6px !important;
        margin-bottom: 12px !important;
        display: flex !important;
        width: 100% !important;
    }
    form div[data-testid="stSegmentedControl"] [role="radiogroup"],
    div[data-testid="stForm"] div[data-testid="stSegmentedControl"] [role="radiogroup"] {
        background: transparent !important;
        border: none !important;
        padding: 0 !important;
        display: flex !important;
        width: 100% !important;
        gap: 0px !important;
    }
    form div[data-testid="stSegmentedControl"] button,
    div[data-testid="stForm"] div[data-testid="stSegmentedControl"] button,
    form div[data-testid="stSegmentedControl"] [role="radio"],
    div[data-testid="stForm"] div[data-testid="stSegmentedControl"] [role="radio"] {
        background: transparent !important;
        border: 1px solid transparent !important;
        padding: 10px 0 !important;
        font-size: 0.78rem !important;
        letter-spacing: 0.3px !important;
        flex: 1 !important;
        width: auto !important;
        box-shadow: none !important;
    }

    /* --- EXPANDERS --- */
    div[data-testid="stExpander"],
    .stExpander {
        background: linear-gradient(160deg, #111723, #0d121c) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 12px !important;
        overflow: hidden !important;
        box-shadow: 0 4px 20px rgba(0,0,0,0.3) !important;
        transition: border-color 0.25s ease !important;
    }
    div[data-testid="stExpander"]:hover,
    .stExpander:hover { border-color: rgba(56, 189, 248, 0.28) !important; }

    /* --- DataFrames --- */
    div[data-testid="stDataFrame"],
    .stDataFrame {
        background: linear-gradient(160deg, #131a29, #111723) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 8px !important;
    }
    /* Hover in DataFrame rows */
    div[data-testid="stDataFrame"] tr:hover {
        background-color: rgba(255,255,255,0.02) !important;
    }

    /* --- Typography (armonizada con Plus Jakarta Sans + Inter) --- */
    h1, h2, h3, h4, h5, h6 {
        font-family: var(--sl-font-display) !important;
    }
    h2:not(.sl-section-title), h3 {
        font-family: var(--sl-font-display) !important;
        font-size: 1.06rem !important;
        font-weight: 700 !important;
        color: var(--sl-text-heading) !important;
        letter-spacing: -0.024em !important;
        margin-top: 4px !important;
        margin-bottom: 8px !important;
    }
    .stMarkdown p { font-family: var(--sl-font-body) !important; color: var(--sl-text-secondary) !important; }

    /* --- GRADIENT DIVIDER (Harmonized glowing line with Ranking) --- */
    .grad-divider {
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.28), transparent);
        margin: 24px 0 20px;
    }

    /* --- SECTION TITLES & MONOSPACE ACTIVE COUNTER BADGE --- */
    .cartera-section-title {
        margin: 18px 0 12px 0 !important;
        display: flex !important;
        align-items: center !important;
        gap: 10px !important;
    }
    .cartera-section-title::before {
        content: "" !important;
        display: inline-block !important;
        width: 3px !important;
        height: 1rem !important;
        background: linear-gradient(180deg, #7dd3fc 0%, #38bdf8 55%, #6366f1 100%) !important;
        border-radius: 999px !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.58) !important;
        flex-shrink: 0 !important;
    }
    .cartera-section-meta {
        font: 600 0.68rem var(--sl-font-body);
        color: var(--sl-text-muted);
        text-transform: uppercase;
        letter-spacing: 0.08em;
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

    /* --- CHROMATIC SECTOR PILLS (Harmonized with Ranking) --- */
    .cartera-sec-pill {
        font-family: 'Inter', sans-serif;
        font-size: 0.58rem;
        font-weight: 700;
        letter-spacing: 0.4px;
        text-transform: uppercase;
        padding: 2px 8px;
        border-radius: 12px;
        display: inline-flex;
        align-items: center;
        white-space: nowrap;
    }
    .sec-tech    { background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }
    .sec-fin     { background: rgba(251, 191, 36, 0.12); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.3); }
    .sec-health  { background: rgba(52, 211, 153, 0.12); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.3); }
    .sec-energy  { background: rgba(251, 146, 60, 0.12); color: #fb923c; border: 1px solid rgba(251, 146, 60, 0.3); }
    .sec-consume { background: rgba(192, 132, 252, 0.12); color: #c084fc; border: 1px solid rgba(192, 132, 252, 0.3); }
    .sec-default { background: rgba(148, 163, 184, 0.10); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.25); }

    /* --- TERMINAL EMPTY STATE SHELL (Harmonized with Ranking Inspector) --- */
    .cartera-empty-shell {
        background: linear-gradient(180deg, #121826 0%, #0e1420 100%);
        border: 1px dashed rgba(148, 163, 184, 0.22);
        border-radius: 14px;
        padding: 32px 24px;
        text-align: center;
        margin: 12px 0 16px 0;
        position: relative;
        overflow: hidden;
    }
    .cartera-empty-shell::before { display: none !important; }
    .cartera-empty-icon {
        font-size: 2.2rem;
        margin-bottom: 8px;
    }
    .cartera-empty-title {
        font-family: 'Inter', sans-serif;
        font-size: 1.02rem;
        font-weight: 700;
        color: #e2e8f0;
        margin-bottom: 6px;
    }
    .cartera-empty-desc {
        font-family: 'Inter', sans-serif;
        font-size: 0.82rem;
        color: #64748b;
        max-width: 520px;
        margin: 0 auto;
        line-height: 1.5;
    }

    /* --- DESGLOSE DE RESULTADOS (4 PILLS COMPACTAS) --- */
    .pnl-breakdown-strip {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 10px;
        margin: 10px 0 12px 0;
    }
    .pnl-pill {
        background: var(--sl-surface-glass);
        border: 1px solid var(--sl-border-default);
        border-radius: 10px;
        padding: 10px 14px;
        display: flex;
        flex-direction: column;
        gap: 3px;
        transition: border-color 0.2s ease;
    }
    .pnl-pill:hover { border-color: rgba(56, 189, 248, 0.28); }
    .pnl-pill-label {
        font-family: 'Inter', sans-serif;
        font-size: 0.63rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #78879c;
    }
    .pnl-pill-val {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.96rem;
        font-weight: 700;
        color: #e2e8f0;
        font-variant-numeric: tabular-nums;
    }
    .pnl-pill-val.pos { color: #34d399; }
    .pnl-pill-val.neg { color: #f87171; }
    .pnl-pill-val.div { color: #38bdf8; }
    .pnl-pill-val.fee { color: #fbbf24; }

    .order-preview-box {
        background: rgba(15, 23, 42, 0.72);
        border: 1px solid rgba(56, 189, 248, 0.22);
        border-radius: 10px;
        padding: 10px 14px;
        margin: 8px 0 6px 0;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
        font-family: 'Inter', sans-serif;
        font-size: 0.80rem;
        color: #cbd5e1;
    }
    .order-preview-box.warn {
        border-color: rgba(239, 68, 68, 0.45);
        background: rgba(239, 68, 68, 0.08);
    }
    .order-preview-mono {
        font-family: 'JetBrains Mono', monospace;
        font-weight: 700;
    }

    @media (max-width: 768px) {
        .cartera-page-header { flex-direction: column; align-items: flex-start; }
        .pos-grid       { grid-template-columns: repeat(2,1fr) !important; }
        .pnl-breakdown-strip { grid-template-columns: repeat(2,1fr) !important; }
        .insights-strip { flex-direction: column; align-items: flex-start; }
    }
</style>
"""

# Reusable gradient divider
DIVIDER_HTML = '<div class="grad-divider"></div>'



def _formatear_moneda(valor: float) -> str:
    """Formatea un valor como moneda en euros."""
    return f"{valor:,.2f} €"


def _formatear_pct(valor: float) -> str:
    """Formatea un valor como porcentaje."""
    signo = "+" if valor >= 0 else ""
    return f"{signo}{valor:.2f}%"


def _obtener_precio_actual(ticker: str) -> float:
    """Obtiene el precio actual de un ticker con manejo de errores."""
    try:
        datos = market_data.get_stock_data(ticker)
        return datos.get('precio_actual', 0.0)
    except Exception:
        return 0.0


def _limpiar_estados_modales_cartera():
    """Limpia todos los flags de modales/formularios de cartera (usado en on_dismiss, Cancelar y cambio de cartera)."""
    for k in (
        "compra_preseleccionada",
        "precio_operacion_input",
        "cartera_form_open",
        "modal_venta",
        "editar_operacion",
        "confirmar_eliminar",
        "tipo_operacion_dialog",
        "edit_mode",
        "dlg_last_ticker_compra",
    ):
        st.session_state.pop(k, None)
    st.session_state["accion_activa"] = None
    st.session_state["cartera_modal_open"] = False


def _on_change_tipo_cartera():
    """Callback para cambiar el tipo de cartera activa desde el segmented control."""
    seleccion = st.session_state.get("selector_tipo_cartera")
    if seleccion == "🎯 CARTERA SIMULADA":
        st.session_state["tipo_cartera_activa"] = "SIMULADA"
        _limpiar_estados_modales_cartera()
    elif seleccion == "🏛️ CARTERA REAL":
        st.session_state["tipo_cartera_activa"] = "REAL"
        _limpiar_estados_modales_cartera()


def _renderizar_pestanas_superiores():
    """Renderiza el selector superior para alternar entre carteras de forma nativa e instantánea."""
    if "tipo_cartera_activa" not in st.session_state:
        st.session_state["tipo_cartera_activa"] = "SIMULADA"

    tipo_actual = st.session_state["tipo_cartera_activa"]

    col_sim, col_real, col_empty = st.columns([2.15, 2.0, 5.85], vertical_alignment="center")

    with col_sim:
        es_sim = tipo_actual == "SIMULADA"
        tipo_btn_sim = "primary" if es_sim else "secondary"
        if st.button("CARTERA SIMULADA", key="btn_cartera_simulada", use_container_width=True, type=tipo_btn_sim):
            if st.session_state["tipo_cartera_activa"] != "SIMULADA":
                st.session_state["tipo_cartera_activa"] = "SIMULADA"
                _limpiar_estados_modales_cartera()
                st.rerun()

    with col_real:
        es_real = tipo_actual == "REAL"
        tipo_btn_real = "primary" if es_real else "secondary"
        if st.button("CARTERA REAL", key="btn_cartera_real", use_container_width=True, type=tipo_btn_real):
            if st.session_state["tipo_cartera_activa"] != "REAL":
                st.session_state["tipo_cartera_activa"] = "REAL"
                _limpiar_estados_modales_cartera()
                st.rerun()


def _calcular_rendimiento_ytd(tipo_cartera: str) -> float:
    """Calcula el rendimiento porcentual exclusivo del año actual (YTD)."""
    prefijo = f"[{tipo_cartera}]"
    year_actual = datetime.now().year
    fecha_inicio_ano = f"{year_actual}-01-01"
    fecha_fin_ano = f"{year_actual}-12-31 23:59:59"

    # Obtener historial de patrimonio
    historial = obtener_historial_patrimonio(tipo_cartera)
    if not historial:
        return 0.0

    df_h = pd.DataFrame(historial)
    df_h['fecha_dt'] = pd.to_datetime(df_h['fecha'])

    # Registros anteriores al año actual
    df_prev = df_h[df_h['fecha_dt'] < pd.to_datetime(fecha_inicio_ano)]
    patrimonio_inicio = float(df_prev.iloc[-1]['patrimonio']) if not df_prev.empty else 0.0

    # Registros del año actual (<= 31 de Diciembre del año actual)
    df_ano_actual = df_h[(df_h['fecha_dt'] >= pd.to_datetime(fecha_inicio_ano)) & (df_h['fecha_dt'] <= pd.to_datetime(fecha_fin_ano))]

    if df_ano_actual.empty:
        return 0.0

    patrimonio_fin_ano = float(df_ano_actual.iloc[-1]['patrimonio'])

    # Capital aportado neto durante este año
    ops_efectivo_ano = query_all("""
        SELECT tipo, importe_total FROM cartera 
        WHERE tipo IN ('INGRESO_EFECTIVO', 'RETIRADA_EFECTIVO') 
        AND notas LIKE ? AND fecha >= ? AND fecha <= ?
    """, (f"%{prefijo}%", fecha_inicio_ano, f"{year_actual}-12-31"))

    capital_aportado_ano = 0.0
    for op in ops_efectivo_ano:
        if op['tipo'] == 'INGRESO_EFECTIVO':
            capital_aportado_ano += op.get('importe_total', 0.0) or 0.0
        elif op['tipo'] == 'RETIRADA_EFECTIVO':
            capital_aportado_ano -= op.get('importe_total', 0.0) or 0.0

    if patrimonio_inicio > 0:
        base = patrimonio_inicio + max(capital_aportado_ano, 0.0)
        beneficio_ano = patrimonio_fin_ano - patrimonio_inicio - capital_aportado_ano
        return (beneficio_ano / base * 100) if base > 0 else 0.0
    else:
        # Si la cartera comenzó este año
        capital_base = capital_aportado_ano
        if capital_base <= 0:
            ops_compras_ano = query_all("""
                SELECT cantidad, precio_unitario, comision FROM cartera
                WHERE tipo = 'COMPRA' AND notas LIKE ? AND fecha >= ? AND fecha <= ?
            """, (f"%{prefijo}%", fecha_inicio_ano, f"{year_actual}-12-31"))
            capital_base = sum((op['cantidad'] * op['precio_unitario']) + (op.get('comision') or 0.0) for op in ops_compras_ano)
        
        beneficio_ano = patrimonio_fin_ano - capital_base
        return (beneficio_ano / capital_base * 100) if capital_base > 0 else 0.0


def _renderizar_tarjetas_kpi():
    """Renderiza KPIs con jerarquía hero / secondary / tertiary."""
    tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")

    # Usar función optimizada con filtro por tipo de cartera
    resumen = calcular_patrimonio_total_filtrado(tipo_cartera)

    # Obtener número total de operaciones filtradas
    todas_ops = obtener_operaciones()
    prefijo = f"[{tipo_cartera}]"
    ops_filtradas = [op for op in todas_ops if prefijo in (op.get('notas') or '')]
    num_operaciones = len(ops_filtradas)

    # Calcular Capital Aportado (Deposits - Withdrawals)
    ops_efectivo = query_all("""
        SELECT tipo, importe_total FROM cartera 
        WHERE tipo IN ('INGRESO_EFECTIVO', 'RETIRADA_EFECTIVO') AND notas LIKE ?
    """, (f"%{prefijo}%",))

    capital_aportado = 0.0
    for op in ops_efectivo:
        if op['tipo'] == 'INGRESO_EFECTIVO':
            capital_aportado += op['importe_total']
        elif op['tipo'] == 'RETIRADA_EFECTIVO':
            capital_aportado -= op['importe_total']

    # Calcular Beneficio Acumulado (Patrimonio Total - Capital Aportado)
    beneficio_acumulado = resumen['patrimonio_total'] - capital_aportado
    beneficio_pct = (beneficio_acumulado / capital_aportado * 100) if capital_aportado > 0 else 0.0
    delta_beneficio = f"{beneficio_pct:+.2f}%"

    # Rendimiento YTD específico del año actual
    ytd_pct = _calcular_rendimiento_ytd(tipo_cartera)
    perf_class_ytd = 'positive' if ytd_pct >= 0 else 'negative'
    arrow_ytd = '▲' if ytd_pct >= 0 else '▼'
    delta_ytd = f"{ytd_pct:+.2f}%"

    # ── HERO KPI: Patrimonio Total (ancho completo, máximo protagonismo) ──
    hero_html = f"""
    <div class="kpi-hero" style="text-align: center;">
        <div class="kpi-hero-label">PATRIMONIO TOTAL</div>
        <div class="kpi-hero-value">{_formatear_moneda(resumen['patrimonio_total'])}</div>
        <div class="kpi-hero-perf {perf_class_ytd}">
            {arrow_ytd} {delta_ytd} este año
        </div>
        <div style="height: 1px; background: rgba(255,255,255,0.04); margin: 12px 32px 10px;"></div>
        <div class="kpi-hero-sub">Capital aportado: {_formatear_moneda(capital_aportado)}</div>
    </div>"""
    st.html(hero_html)

    # ── SECONDARY KPIs (3 cols): Beneficio, Liquidez, Capital Invertido ──
    sec_cols = st.columns(3)
    with sec_cols[0]:
        perf_class = 'positive' if beneficio_acumulado >= 0 else 'negative'
        arrow = '▲' if beneficio_acumulado >= 0 else '▼'
        st.html(f"""
        <div class="stMetricCustom">
            <label>BENEFICIO ACUMULADO</label>
            <div class="metric-value-container">
                <span class="value-main">{_formatear_moneda(beneficio_acumulado)}</span>
                <span class="value-delta {perf_class}">{arrow} {delta_beneficio}</span>
            </div>
        </div>
        """)
    with sec_cols[1]:
        st.metric(label="LIQUIDEZ DISPONIBLE", value=_formatear_moneda(resumen['liquidez']))
    with sec_cols[2]:
        st.metric(label="CAPITAL INVERTIDO", value=_formatear_moneda(resumen['capital_invertido']))

    st.markdown("<div style='margin-bottom: 8px;'></div>", unsafe_allow_html=True)

    # ── TERTIARY KPIs (2 cols): Capital Aportado, Nº Operaciones ─────────
    ter_cols = st.columns(2)
    with ter_cols[0]:
        st.metric(label="CAPITAL APORTADO", value=_formatear_moneda(capital_aportado))
    with ter_cols[1]:
        st.metric(label="OPERACIONES REALIZADAS", value=f"{num_operaciones} ops")

    # ── DESGLOSE DE RESULTADOS (Latente · Realizada · Dividendos · Comisiones) ──
    desglose = calcular_desglose_resultados_filtrado(tipo_cartera)
    pl_lat = desglose['plusvalia_latente']
    pl_rea = desglose['plusvalia_realizada']
    divs_c = desglose['dividendos_cobrados']
    coms_t = desglose['comisiones_totales']

    cls_lat = "pos" if pl_lat > 0 else ("neg" if pl_lat < 0 else "")
    cls_rea = "pos" if pl_rea > 0 else ("neg" if pl_rea < 0 else "")
    s_lat = f"+{_formatear_moneda(pl_lat)}" if pl_lat > 0 else _formatear_moneda(pl_lat)
    s_rea = f"+{_formatear_moneda(pl_rea)}" if pl_rea > 0 else _formatear_moneda(pl_rea)

    st.html(f"""
    <div class="pnl-breakdown-strip">
        <div class="pnl-pill" title="Ganancia o pérdida no realizada en posiciones actualmente abiertas">
            <span class="pnl-pill-label">Plusvalía Latente</span>
            <span class="pnl-pill-val {cls_lat}">{s_lat}</span>
        </div>
        <div class="pnl-pill" title="Ganancia o pérdida consolidada en ventas ejecutadas (neta de comisiones)">
            <span class="pnl-pill-label">Plusvalía Realizada</span>
            <span class="pnl-pill-val {cls_rea}">{s_rea}</span>
        </div>
        <div class="pnl-pill" title="Total acumulado de dividendos cobrados en esta cartera">
            <span class="pnl-pill-label">Dividendos Cobrados</span>
            <span class="pnl-pill-val div">{_formatear_moneda(divs_c)}</span>
        </div>
        <div class="pnl-pill" title="Suma de comisiones de compraventa y gastos de bróker registrados">
            <span class="pnl-pill-label">Comisiones Pagadas</span>
            <span class="pnl-pill-val fee">{_formatear_moneda(coms_t)}</span>
        </div>
    </div>
    """)


def _renderizar_diagnostico_cartera():
    """Renderiza indicadores cuantitativos dinámicos como barra de estado inline compacta."""
    tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")
    resumen = calcular_patrimonio_total_filtrado(tipo_cartera)
    patrimonio_total = resumen['patrimonio_total']
    liquidez = resumen['liquidez']

    desglose = calcular_desglose_resultados_filtrado(tipo_cartera)
    capital_aportado = desglose['capital_aportado']
    beneficio_acumulado = patrimonio_total - capital_aportado
    beneficio_pct = (beneficio_acumulado / capital_aportado * 100.0) if capital_aportado > 0 else 0.0

    # Calcular concentración exacta y calidad media ponderada (conectada al motor V8.5)
    posiciones = calcular_posiciones_abiertas_filtradas(tipo_cartera)
    max_peso_pct = 0.0
    max_peso_tk = ""
    calidad_ponderada = None
    if posiciones and patrimonio_total > 0:
        tickers = [pos['ticker'] for pos in posiciones]
        precios_batch = obtener_precios_actuales_batch(tickers)

        # Leer scores de calidad en SQLite (0 llamadas de red)
        scores_map = {}
        try:
            rows_sc = query_all("SELECT UPPER(ticker) AS tk, score FROM ranking_estado")
            scores_map = {str(r['tk']): float(r['score']) for r in rows_sc if r.get('score') is not None}
        except Exception:
            scores_map = {}

        suma_val_pos = 0.0
        suma_val_cal = 0.0
        suma_peso_cal = 0.0
        for pos in posiciones:
            precio_act = float(precios_batch.get(pos['ticker'], 0.0) or 0.0)
            valor_pos = (pos['acciones'] * precio_act) if precio_act > 0 else float(pos['coste_total'])
            peso_pat = (valor_pos / patrimonio_total) * 100.0 if patrimonio_total > 0 else 0.0
            if peso_pat > max_peso_pct:
                max_peso_pct = peso_pat
                max_peso_tk = str(pos['ticker']).replace('.MC', '')
            suma_val_pos += valor_pos
            sc_tk = scores_map.get(str(pos['ticker']).strip().upper())
            if sc_tk is not None and valor_pos > 0:
                suma_val_cal += valor_pos * sc_tk
                suma_peso_cal += valor_pos
        if suma_peso_cal > 0:
            calidad_ponderada = suma_val_cal / suma_peso_cal

    # Construir items de estado inline con cifras reales
    items = []

    # 1. Rentabilidad
    if beneficio_acumulado > 0:
        items.append(f'<span class="status-item"><span class="status-dot green"></span>Rentabilidad {beneficio_pct:+.1f}%</span>')
    elif beneficio_acumulado < 0:
        items.append(f'<span class="status-item"><span class="status-dot red"></span>Rentabilidad {beneficio_pct:+.1f}%</span>')
    else:
        items.append('<span class="status-item"><span class="status-dot gray"></span>Rentabilidad neutra</span>')

    # 2. Liquidez
    liq_pct = (liquidez / patrimonio_total * 100.0) if patrimonio_total > 0 else 0.0
    if patrimonio_total > 0 and liq_pct < 10.0:
        items.append(f'<span class="status-item"><span class="status-dot amber"></span>Liquidez {liq_pct:.1f}% (baja)</span>')
    elif patrimonio_total > 0:
        items.append(f'<span class="status-item"><span class="status-dot green"></span>Liquidez {liq_pct:.1f}%</span>')
    else:
        items.append('<span class="status-item"><span class="status-dot gray"></span>Sin capital</span>')

    # 3. Concentración
    if max_peso_pct > 40.0:
        items.append(f'<span class="status-item"><span class="status-dot red"></span>Concentración alta: {max_peso_tk} ({max_peso_pct:.1f}%)</span>')
    elif max_peso_pct > 25.0:
        items.append(f'<span class="status-item"><span class="status-dot amber"></span>Mayor peso: {max_peso_tk} ({max_peso_pct:.1f}%)</span>')
    elif posiciones:
        items.append(f'<span class="status-item"><span class="status-dot green"></span>Diversificación equilibrada (máx. {max_peso_pct:.1f}%)</span>')
    else:
        items.append('<span class="status-item"><span class="status-dot gray"></span>Sin posiciones abiertas</span>')

    # 4. Calidad media ponderada de la cartera (V8.5)
    if calidad_ponderada is not None:
        dot_cal = "green" if calidad_ponderada >= 65.0 else ("amber" if calidad_ponderada >= 45.0 else "red")
        items.append(f'<span class="status-item"><span class="status-dot {dot_cal}"></span>Calidad cartera: {calidad_ponderada:.1f}/100</span>')

    sep = '<span class="status-sep">│</span>'
    strip_html = f'<div class="status-strip">{sep.join(items)}</div>'
    st.html(strip_html)


def _renderizar_grafico_evolucion():
    """Renderiza el gráfico de evolución del patrimonio usando Chart.js (mismo motor que IBEX 35)."""
    st.html('<h2 class="sl-section-title cartera-section-title">Evolución del Patrimonio</h2>')

    if "periodo_patrimonio" not in st.session_state:
        st.session_state["periodo_patrimonio"] = "Todo"

    periodo_actual = st.session_state["periodo_patrimonio"]

    # Filtros de tiempo nativos con estado guardado y resaltado activo
    col_filtro1, col_filtro2, col_filtro3, col_filtro4, col_filtro5 = st.columns(5)

    with col_filtro1:
        if st.button("1M", key="btn_filtro_1m", use_container_width=True, type="primary" if periodo_actual == "1M" else "secondary"):
            if st.session_state["periodo_patrimonio"] != "1M":
                st.session_state["periodo_patrimonio"] = "1M"
                st.rerun()

    with col_filtro2:
        if st.button("6M", key="btn_filtro_6m", use_container_width=True, type="primary" if periodo_actual == "6M" else "secondary"):
            if st.session_state["periodo_patrimonio"] != "6M":
                st.session_state["periodo_patrimonio"] = "6M"
                st.rerun()

    with col_filtro3:
        if st.button("YTD", key="btn_filtro_ytd", use_container_width=True, type="primary" if periodo_actual == "YTD" else "secondary"):
            if st.session_state["periodo_patrimonio"] != "YTD":
                st.session_state["periodo_patrimonio"] = "YTD"
                st.rerun()

    with col_filtro4:
        if st.button("1A", key="btn_filtro_1a", use_container_width=True, type="primary" if periodo_actual == "1A" else "secondary"):
            if st.session_state["periodo_patrimonio"] != "1A":
                st.session_state["periodo_patrimonio"] = "1A"
                st.rerun()

    with col_filtro5:
        if st.button("Todo", key="btn_filtro_todo", use_container_width=True, type="primary" if periodo_actual == "Todo" else "secondary"):
            if st.session_state["periodo_patrimonio"] != "Todo":
                st.session_state["periodo_patrimonio"] = "Todo"
                st.rerun()

    # Obtener historial filtrado por tipo de cartera activa
    tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")
    historial = obtener_historial_patrimonio(tipo_cartera)

    if not historial:
        st.info("No hay datos históricos disponibles. Añade operaciones para ver la evolución.")
        return

    df_historial = pd.DataFrame(historial)
    df_historial['fecha'] = pd.to_datetime(df_historial['fecha'])

    # Aplicar filtro temporal relativo a la fecha máxima de las operaciones del usuario
    max_fecha = df_historial['fecha'].max()

    if periodo_actual == "1M":
        fecha_inicio = max_fecha - timedelta(days=30)
        df_historial = df_historial[df_historial['fecha'] >= fecha_inicio]
    elif periodo_actual == "6M":
        fecha_inicio = max_fecha - timedelta(days=180)
        df_historial = df_historial[df_historial['fecha'] >= fecha_inicio]
    elif periodo_actual == "YTD":
        fecha_inicio = datetime(max_fecha.year, 1, 1)
        df_historial = df_historial[df_historial['fecha'] >= fecha_inicio]
    elif periodo_actual == "1A":
        fecha_inicio = max_fecha - timedelta(days=365)
        df_historial = df_historial[df_historial['fecha'] >= fecha_inicio]

    if df_historial.empty:
        st.info("No hay datos en el periodo seleccionado.")
        return

    # Manejar caso de punto único: duplicar para que Chart.js pueda dibujar la línea
    if len(df_historial) == 1:
        fila = df_historial.iloc[0].copy()
        fila['fecha'] = fila['fecha'] - pd.Timedelta(days=1)
        df_historial = pd.concat([pd.DataFrame([fila]), df_historial], ignore_index=True)

    # Preparar datos para render_stock_chart (Chart.js)
    labels = [f.strftime('%d %b, %Y') for f in df_historial['fecha']]
    prices = df_historial['patrimonio'].tolist()
    last_close = float(prices[0])  # Referencia: primer valor del periodo

    # Línea opcional de Capital Aportado (distingue crecimiento por mercado vs ingresos de efectivo)
    extra_series_cap = None
    if 'capital_aportado' in df_historial.columns and df_historial['capital_aportado'].max() > 0:
        mostrar_cap = st.checkbox(
            "Mostrar línea de Capital Aportado",
            value=True,
            key=f"chk_cap_aportado_{tipo_cartera}",
        )
        if mostrar_cap:
            extra_series_cap = [{
                "label": "Capital Aportado",
                "data": [float(v) for v in df_historial['capital_aportado'].tolist()],
                "color": "#f59e0b",
                "dash": [6, 4],
                "width": 1.6,
            }]

    # Color: verde si patrimonio subió, rojo si bajó
    line_color = "#10B981" if prices[-1] >= prices[0] else "#EF4444"

    # Tick labels: 5 puntos equidistantes en el eje X
    n_ticks = min(5, len(labels))
    total_pts = len(labels)
    if total_pts > 1:
        tick_indices = [int(i * (total_pts - 1) / (n_ticks - 1)) for i in range(n_ticks)]
    else:
        tick_indices = [0]
    tick_indices = sorted(set(tick_indices))
    tick_labels_map = {labels[i]: labels[i] for i in tick_indices}

    render_stock_chart(
        labels=labels,
        prices=prices,
        last_close=last_close,
        line_color=line_color,
        tick_labels_map=tick_labels_map,
        currency="EUR",
        ticker="PATRIMONIO",
        title="Evolución del Patrimonio",
        show_reference_line=(extra_series_cap is None),
        show_measure_tool=False,
        show_zoom=False,
        height=320,
        y_padding_ratio=0.10,
        main_label="Patrimonio",
        extra_series=extra_series_cap,
    )


def _ajustar_texto_plotly(texto: str, max_len: int = 14) -> str:
    """Ajusta texto largo insertando saltos de línea HTML (<br>) estilo Excel para evitar cortes."""
    if not texto or len(texto) <= max_len:
        return texto
    palabras = texto.split(" ")
    lineas = []
    linea_actual = []
    len_actual = 0
    for p in palabras:
        if len_actual + len(p) > max_len and linea_actual:
            lineas.append(" ".join(linea_actual))
            linea_actual = [p]
            len_actual = len(p)
        else:
            linea_actual.append(p)
            len_actual += len(p) + 1
    if linea_actual:
        lineas.append(" ".join(linea_actual))
    return "<br>".join(lineas)


def _renderizar_distribucion_cartera():
    """Renderiza los gráficos de distribución por sectores y empresas."""
    st.html('<h2 class="sl-section-title cartera-section-title">Distribución de Cartera <span class="cartera-section-meta">Sector · Empresa</span></h2>')

    tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")
    empresas = obtener_distribucion_empresas_filtrada(tipo_cartera)

    # Paleta de colores satinada en concordancia con el tema Obsidian Slate
    PREMIUM_PALETTE = [
        {"fill": "rgba(56, 189, 248, 0.16)", "line": "#38bdf8"},   # Azul Cielo Satinado
        {"fill": "rgba(52, 211, 153, 0.16)", "line": "#34d399"},   # Esmeralda Suave
        {"fill": "rgba(129, 140, 248, 0.16)", "line": "#818cf8"},  # Índigo
        {"fill": "rgba(251, 191, 36, 0.16)", "line": "#fbbf24"},   # Ámbar Suave
        {"fill": "rgba(96, 165, 250, 0.16)", "line": "#60a5fa"},   # Azul Acero
        {"fill": "rgba(244, 114, 182, 0.16)", "line": "#f472b6"},  # Rosa Satinado
        {"fill": "rgba(45, 212, 191, 0.16)", "line": "#2dd4bf"},   # Turquesa Suave
        {"fill": "rgba(167, 139, 250, 0.16)", "line": "#a78bfa"}   # Lavanda
    ]

    sector_to_color = {}

    def get_color_for_sector(s):
        if s in sector_to_color:
            return sector_to_color[s]
        hash_val = sum(ord(c) for c in s) if s else 0
        return PREMIUM_PALETTE[hash_val % len(PREMIUM_PALETTE)]

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Por Sectores**")
        sectores = obtener_distribucion_sectores_filtrada(tipo_cartera)

        if sectores:
            df_sectores = pd.DataFrame(sectores)
            if not df_sectores.empty:
                df_sectores = df_sectores.sort_values(by='porcentaje', ascending=True)
                
                # Ajustar texto estilo Excel introduciendo saltos de línea <br> para evitar cortes
                df_sectores['sector_formateado'] = df_sectores['sector'].apply(lambda x: _ajustar_texto_plotly(x, max_len=14))

                # Mapeo determinista de sector -> color
                todos_sectores = sorted(list(set(df_sectores['sector'].tolist())))
                sector_to_color.update({s: PREMIUM_PALETTE[i % len(PREMIUM_PALETTE)] for i, s in enumerate(todos_sectores)})

                fig_sectores = px.bar(
                    df_sectores,
                    x='porcentaje',
                    y='sector_formateado',
                    orientation='h',
                    labels={'porcentaje': '%', 'sector_formateado': 'Sector'}
                )

                # Generar listas de colores basadas en el sector
                colors_fill = [get_color_for_sector(row['sector'])["fill"] for _, row in df_sectores.iterrows()]
                colors_line = [get_color_for_sector(row['sector'])["line"] for _, row in df_sectores.iterrows()]

                fig_sectores.update_traces(
                    marker_color=colors_fill,
                    marker_line_color=colors_line,
                    marker_line_width=1.5,
                    texttemplate='<b>%{x:.1f}%</b>',
                    textposition='outside',
                    textfont=dict(family="JetBrains Mono, monospace", size=11, color=colors_line),
                    cliponaxis=False,
                    hovertemplate="<b>Sector:</b> %{customdata[1]}<br><b>Porcentaje:</b> %{x:.1f}%<br><b>Valor:</b> %{customdata[0]:,.2f} €<extra></extra>",
                    customdata=df_sectores[['valor', 'sector']].values
                )

                fig_sectores.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    margin=dict(l=135, r=45, t=10, b=45),
                    showlegend=False,
                    height=250,
                    hoverlabel=dict(
                        bgcolor="#131a29",
                        bordercolor="rgba(148, 163, 184, 0.22)",
                        font=dict(family="Inter, sans-serif", size=12, color="#f1f5f9")
                    ),
                    xaxis=dict(
                        showgrid=True, 
                        gridcolor="rgba(148, 163, 184, 0.07)",
                        zeroline=False,
                        showline=False,
                        ticks="",
                        title=None,
                        range=[0, 110],
                        tickvals=[0, 20, 40, 60, 80, 100],
                        ticktext=["0%", "20%", "40%", "60%", "80%", "100%"],
                        fixedrange=True,
                        tickfont=dict(family="Inter, sans-serif", size=10, color="#64748b")
                    ),
                    yaxis=dict(
                        showgrid=False,
                        automargin=True,
                        zeroline=False,
                        showline=False,
                        ticks="",
                        title=None,
                        fixedrange=True,
                        tickfont=dict(family="Inter, sans-serif", size=10.5, color="#94a3b8")
                    )
                )

                st.plotly_chart(fig_sectores, use_container_width=True, config={'displayModeBar': False}, key="dist_sectores_v4")
            else:
                st.html("""
                <div class="cartera-empty-shell" style="padding: 24px 16px;">
                    <div class="cartera-empty-title" style="font-size: 0.92rem;">Distribución no disponible</div>
                    <div class="cartera-empty-desc" style="font-size: 0.78rem;">Añade operaciones a la cartera para generar gráficos de asignación sectorial y por activos.</div>
                </div>
                """)
        else:
            st.html("""
            <div class="cartera-empty-shell" style="padding: 24px 16px;">
                <div class="cartera-empty-title" style="font-size: 0.92rem;">Distribución no disponible</div>
                <div class="cartera-empty-desc" style="font-size: 0.78rem;">Añade operaciones a la cartera para generar gráficos de asignación sectorial y por activos.</div>
            </div>
            """)

    with col2:
        st.markdown("**Por Empresas**")

        if empresas:
            df_empresas = pd.DataFrame(empresas[:10])  # Top 10
            if not df_empresas.empty:
                df_empresas = df_empresas.sort_values(by='porcentaje', ascending=True)
                
                # Ajustar texto estilo Excel introduciendo saltos de línea <br> para evitar cortes
                df_empresas['nombre_formateado'] = df_empresas['nombre'].apply(lambda x: _ajustar_texto_plotly(x, max_len=14))

                # Si ya cargamos sectores, podemos reutilizar el mapeo de sector_to_color creado en col1 si existe
                if not sector_to_color and 'df_sectores' in locals() and not df_sectores.empty:
                    todos_sectores = sorted(list(set(df_sectores['sector'].tolist())))
                    sector_to_color.update({s: PREMIUM_PALETTE[i % len(PREMIUM_PALETTE)] for i, s in enumerate(todos_sectores)})
                elif not sector_to_color:
                    # Si no hay df_sectores, crear mapeo a partir de las empresas
                    sectores_empresas = sorted(list(set(df_empresas['sector'].tolist()))) if 'sector' in df_empresas.columns else []
                    sector_to_color.update({s: PREMIUM_PALETTE[i % len(PREMIUM_PALETTE)] for i, s in enumerate(sectores_empresas)})

                fig_empresas = px.bar(
                    df_empresas,
                    x='porcentaje',
                    y='nombre_formateado',
                    orientation='h',
                    labels={'porcentaje': '%', 'nombre_formateado': 'Empresa'}
                )

                # Generar listas de colores basadas en el sector de la empresa
                colors_fill = [get_color_for_sector(row.get('sector', 'Otros'))["fill"] for _, row in df_empresas.iterrows()]
                colors_line = [get_color_for_sector(row.get('sector', 'Otros'))["line"] for _, row in df_empresas.iterrows()]

                fig_empresas.update_traces(
                    marker_color=colors_fill,
                    marker_line_color=colors_line,
                    marker_line_width=1.5,
                    texttemplate='<b>%{x:.1f}%</b>',
                    textposition='outside',
                    textfont=dict(family="JetBrains Mono, monospace", size=11, color=colors_line),
                    cliponaxis=False,
                    hovertemplate="<b>Empresa:</b> %{customdata[2]} (%{customdata[0]})<br><b>Sector:</b> %{customdata[3]}<br><b>Porcentaje:</b> %{x:.1f}%<br><b>Valor:</b> %{customdata[1]:,.2f} €<extra></extra>",
                    customdata=df_empresas[['ticker', 'valor', 'nombre', 'sector']].values
                )

                fig_empresas.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    margin=dict(l=135, r=45, t=10, b=45),
                    showlegend=False,
                    height=250,
                    hoverlabel=dict(
                        bgcolor="#131a29",
                        bordercolor="rgba(148, 163, 184, 0.22)",
                        font=dict(family="Inter, sans-serif", size=12, color="#f1f5f9")
                    ),
                    xaxis=dict(
                        showgrid=True, 
                        gridcolor="rgba(148, 163, 184, 0.07)",
                        zeroline=False,
                        showline=False,
                        ticks="",
                        title=None,
                        range=[0, 110],
                        tickvals=[0, 20, 40, 60, 80, 100],
                        ticktext=["0%", "20%", "40%", "60%", "80%", "100%"],
                        fixedrange=True,
                        tickfont=dict(family="Inter, sans-serif", size=10, color="#64748b")
                    ),
                    yaxis=dict(
                        showgrid=False,
                        automargin=True,
                        zeroline=False,
                        showline=False,
                        ticks="",
                        title=None,
                        fixedrange=True,
                        tickfont=dict(family="Inter, sans-serif", size=10.5, color="#94a3b8")
                    )
                )

                st.plotly_chart(fig_empresas, use_container_width=True, config={'displayModeBar': False}, key="dist_empresas_v4")
            else:
                st.html("""
                <div class="cartera-empty-shell" style="padding: 24px 16px;">
                    <div class="cartera-empty-title" style="font-size: 0.92rem;">Distribución no disponible</div>
                    <div class="cartera-empty-desc" style="font-size: 0.78rem;">Añade operaciones a la cartera para generar gráficos de asignación sectorial y por activos.</div>
                </div>
                """)
        else:
            st.html("""
            <div class="cartera-empty-shell" style="padding: 24px 16px;">
                <div class="cartera-empty-title" style="font-size: 0.92rem;">Distribución no disponible</div>
                <div class="cartera-empty-desc" style="font-size: 0.78rem;">Añade operaciones a la cartera para generar gráficos de asignación sectorial y por activos.</div>
            </div>
            """)


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


def _renderizar_cards_posiciones():
    """Renderiza las posiciones abiertas como cards visuales compactas."""
    # Obtener tipo de cartera activa
    tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")

    # Usar función optimizada con filtro por tipo de cartera
    posiciones = calcular_posiciones_abiertas_filtradas(tipo_cartera)
    num_pos = len(posiciones) if posiciones else 0
    badge_txt = f"{num_pos} {'posición' if num_pos == 1 else 'posiciones'}"
    st.html(f'<h2 class="sl-section-title cartera-section-title">Posiciones Abiertas <span class="active-counter-badge">[ {badge_txt} ]</span></h2>')

    # Umbrales de alerta para Take Profit y Stop Loss
    UMBRAL_TAKE_PROFIT = 10.0
    UMBRAL_STOP_LOSS = -5.0

    if not posiciones:
        st.html("""
        <div class="cartera-empty-shell">
            <div class="cartera-empty-title">Sin Posiciones Abiertas</div>
            <div class="cartera-empty-desc">
                Actualmente no tienes posiciones abiertas en esta cartera. Utiliza el botón inferior para registrar una compra o añadir liquidez.
            </div>
        </div>
        """)
        _renderizar_cta_nueva_operacion()
        return

    # Obtener todos los tickers y cargar precios en batch (más eficiente)
    tickers = [pos['ticker'] for pos in posiciones]
    precios_batch = obtener_precios_actuales_batch(tickers)

    # --- CÁLCULOS DE INSIGHTS ---
    mejor_pos = None
    peor_pos = None
    mayor_peso_pos = None

    mejor_ganancia_pct = -999999.0
    peor_ganancia_pct = 999999.0
    max_peso_pct = -1.0

    posiciones_calculadas = []
    total_valor_posiciones = 0.0

    for pos in posiciones:
        precio_act = float(precios_batch.get(pos['ticker'], 0) or 0)
        precio_valido = precio_act > 0
        comisiones_compra = pos.get('comisiones', 0.0)
        coste_total_compra = (pos['acciones'] * pos['precio_medio']) + comisiones_compra
        valor_posicion = (pos['acciones'] * precio_act) if precio_valido else coste_total_compra
        ganancia_abs = (valor_posicion - coste_total_compra) if precio_valido else 0.0
        ganancia_pct = (ganancia_abs / coste_total_compra * 100) if (precio_valido and coste_total_compra > 0) else 0.0

        total_valor_posiciones += valor_posicion

        posiciones_calculadas.append({
            'pos': pos,
            'precio_actual': precio_act,
            'precio_valido': precio_valido,
            'comisiones_compra': comisiones_compra,
            'coste_total_compra': coste_total_compra,
            'valor_posicion': valor_posicion,
            'ganancia_abs': ganancia_abs,
            'ganancia_pct': ganancia_pct
        })

    for pdata in posiciones_calculadas:
        ganancia_pct = pdata['ganancia_pct']
        valor_posicion = pdata['valor_posicion']

        if ganancia_pct > mejor_ganancia_pct:
            mejor_ganancia_pct = ganancia_pct
            mejor_pos = pdata

        if ganancia_pct < peor_ganancia_pct:
            peor_ganancia_pct = ganancia_pct
            peor_pos = pdata

        peso_invertido = (valor_posicion / total_valor_posiciones * 100) if total_valor_posiciones > 0 else 0.0
        if peso_invertido > max_peso_pct:
            max_peso_pct = peso_invertido
            mayor_peso_pos = pdata

    # ── INSIGHTS: Línea horizontal compacta (no tarjetas) ─────────────────
    insights_parts = []
    if mejor_pos:
        val_class = "positive" if mejor_pos['ganancia_pct'] >= 0 else "negative"
        insights_parts.append(
            f'<span class="insight-item">Mejor: '
            f'<span class="insight-ticker">{mejor_pos["pos"]["ticker"]}</span> '
            f'<span class="insight-value {val_class}">{mejor_pos["ganancia_pct"]:+.1f}%</span></span>'
        )
    if peor_pos and peor_pos is not mejor_pos:
        val_class = "positive" if peor_pos['ganancia_pct'] >= 0 else "negative"
        insights_parts.append(
            f'<span class="insight-item">Peor: '
            f'<span class="insight-ticker">{peor_pos["pos"]["ticker"]}</span> '
            f'<span class="insight-value {val_class}">{peor_pos["ganancia_pct"]:+.1f}%</span></span>'
        )
    if mayor_peso_pos:
        insights_parts.append(
            f'<span class="insight-item">Mayor peso: '
            f'<span class="insight-ticker">{mayor_peso_pos["pos"]["ticker"]}</span> '
            f'<span class="insight-value neutral">{max_peso_pct:.1f}%</span></span>'
        )

    if insights_parts:
        sep = '<span class="insight-sep">•</span>'
        insights_html = f'<div class="insights-strip">{sep.join(insights_parts)}</div>'
        st.html(insights_html)

    # ── CARDS DE POSICIONES (compactas) ───────────────────────────────────
    for idx, pdata in enumerate(posiciones_calculadas):
        pos = pdata['pos']
        precio_actual = pdata['precio_actual']
        precio_valido = pdata.get('precio_valido', precio_actual > 0)
        comisiones_compra = pdata['comisiones_compra']
        coste_total_compra = pdata['coste_total_compra']
        valor_posicion = pdata['valor_posicion']
        ganancia_abs = pdata['ganancia_abs']
        ganancia_pct = pdata['ganancia_pct']

        with st.container():

            # Calcular precio de equilibrio (Break-even)
            precio_break_even = coste_total_compra / pos['acciones'] if pos['acciones'] > 0 else 0.0

            # Determinar color según ganancia
            color_ganancia = "#10b981" if ganancia_abs >= 0 else "#ef4444"
            signo = "+" if ganancia_abs >= 0 else ""

            # Determinar clase de alerta para el borde y el LED lateral (solo si hay cotización válida)
            alert_class = ""
            if precio_valido:
                if ganancia_pct >= UMBRAL_TAKE_PROFIT:
                    alert_class = " take-profit"
                elif ganancia_pct <= UMBRAL_STOP_LOSS:
                    alert_class = " stop-loss"
                elif ganancia_abs > 0:
                    alert_class = " profit"
                elif ganancia_abs < 0:
                    alert_class = " loss"

            # Determinar badge de alerta visual en la cabecera
            badge_html = ""
            if precio_valido:
                if ganancia_pct >= UMBRAL_TAKE_PROFIT:
                    badge_html = "<span class='pos-alert-badge tp'>OBJETIVO</span>"
                elif ganancia_pct <= UMBRAL_STOP_LOSS:
                    badge_html = "<span class='pos-alert-badge sl'>STOP LOSS</span>"

            # Notas: sólo si existen, usando toggle nativo details/summary (escapado XSS)
            notas_html = ""
            if pos.get('notas') and pos['notas'].strip():
                nota_texto = _html.escape(str(pos['notas']))
                notas_html = f"""
                <details class="pos-notes">
                    <summary>Nota</summary>
                    <div class="pos-notes-content">{nota_texto}</div>
                </details>"""

            # PNL Progress Bar y clases de texto (CSS define .positive/.negative para texto y .green/.red para barra)
            bar_color_class = "green" if ganancia_abs >= 0 else "red"
            pnl_text_class = ("positive" if ganancia_abs >= 0 else "negative") if (precio_valido and ganancia_pct != 0) else ""
            bar_pct = min(100.0, abs(ganancia_pct) * 5.0) if (precio_valido and abs(ganancia_pct) > 0) else 0.0
            if bar_pct < 5.0 and precio_valido and abs(ganancia_pct) > 0:
                bar_pct = 5.0  # mínimo visible

            precio_actual_str = _formatear_moneda(precio_actual) if precio_valido else "—"
            rent_str = f"{signo}{ganancia_pct:.2f}%" if precio_valido else "—"

            # Sector pill
            sec_cls, sec_txt = _obtener_clase_sector(pos.get('sector'))
            ticker_esc = _html.escape(str(pos.get('ticker', '')))
            nombre_esc = _html.escape(str(pos.get('nombre', '')))
            sec_txt_esc = _html.escape(str(sec_txt))

            # Card compacta
            html_codigo = f"""
            <div class="pos-card{alert_class}">
                <div class="pos-card-head">
                    <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;">
                        <div class="pos-ticker">{ticker_esc}</div>
                        <span class="cartera-sec-pill {sec_cls}">{sec_txt_esc}</span>
                        <div class="pos-name">{nombre_esc}</div>
                    </div>
                    {badge_html}
                </div>

                <div class="pos-grid">
                    <div>
                        <div class="pos-field-label">Acciones</div>
                        <div class="pos-field-value">{pos['acciones']:.2f}</div>
                    </div>
                    <div>
                        <div class="pos-field-label">P. Medio</div>
                        <div class="pos-field-value">{_formatear_moneda(pos['precio_medio'])}</div>
                    </div>
                    <div>
                        <div class="pos-field-label">P. Actual</div>
                        <div class="pos-field-value">{precio_actual_str}</div>
                    </div>
                    <div>
                        <div class="pos-field-label">P. Equilibrio</div>
                        <div class="pos-field-value">{_formatear_moneda(precio_break_even)}</div>
                    </div>
                    <div>
                        <div class="pos-field-label">Rentabilidad</div>
                        <div class="pos-field-value {pnl_text_class}">{rent_str}</div>
                    </div>
                    <div>
                        <div class="pos-field-label">1ª Compra</div>
                        <div class="pos-field-value">{pos['primera_compra']}</div>
                    </div>
                </div>

                <div class="pos-pnl-bar-wrap">
                    <div class="pos-pnl-bar {bar_color_class}" style="width: {bar_pct}%;"></div>
                </div>

                <div class="pos-card-footer">
                    <div>
                        <div class="pos-valor-label">Valor Posición</div>
                        <div class="pos-valor-num">{_formatear_moneda(valor_posicion)}</div>
                    </div>
                    <div style="text-align: right;">
                        <div class="pos-valor-label">Comisión</div>
                        <div class="pos-ganancia-num">{_formatear_moneda(comisiones_compra)}</div>
                    </div>
                </div>
                {notas_html}
            </div>"""
            st.html(html_codigo)

            # Botones de acción
            col_btn1, col_btn2, col_btn3, col_btn4 = st.columns(4)

            with col_btn1:
                if st.button("Detalle", key=f"btn_detalle_{pos['empresa_id']}", use_container_width=True):
                    st.session_state["menu_activo"] = "🏢 Empresas"
                    st.session_state["selector_global_empresas"] = f"{pos['ticker']} - {pos['nombre']}"
                    st.rerun()

            with col_btn2:
                if st.button("+ Comprar", key=f"btn_comprar_{pos['empresa_id']}", use_container_width=True):
                    st.session_state["cartera_modal_open"] = False
                    st.session_state['compra_preseleccionada'] = pos['ticker']
                    st.session_state['precio_operacion_input'] = precio_actual if precio_valido else pos['precio_medio']
                    st.session_state['scroll_al_techo'] = True
                    st.session_state['accion_activa'] = 'comprar'
                    st.rerun()

            with col_btn3:
                if st.button("Vender", key=f"btn_vender_{pos['empresa_id']}", use_container_width=True):
                    st.session_state["cartera_modal_open"] = False
                    st.session_state['modal_venta'] = {
                        'empresa_id': pos['empresa_id'],
                        'ticker': pos['ticker'],
                        'nombre': pos['nombre'],
                        'acciones_disponibles': pos['acciones'],
                        'precio_actual': precio_actual if precio_valido else pos['precio_medio'],
                        'tipo_cartera': tipo_cartera,
                    }
                    st.session_state['precio_operacion_input'] = precio_actual if precio_valido else pos['precio_medio']
                    st.session_state['scroll_al_techo'] = True
                    st.session_state['accion_activa'] = 'vender'
                    st.rerun()

            with col_btn4:
                if st.button("Editar", key=f"btn_editar_{pos['empresa_id']}", use_container_width=True):
                    # Cargar TODAS las operaciones individuales de compra/venta de esta empresa en esta cartera
                    prefijo = f"[{tipo_cartera}]"
                    ops_db = query_all("""
                        SELECT id, tipo, cantidad, precio_unitario, comision, notas, fecha
                        FROM cartera
                        WHERE empresa_id = ? AND tipo IN ('COMPRA', 'VENTA') AND notas LIKE ?
                        ORDER BY fecha DESC, id DESC
                    """, (pos['empresa_id'], f"%{prefijo}%"))

                    st.session_state["cartera_modal_open"] = False
                    st.session_state['scroll_al_techo'] = True
                    st.session_state['editar_operacion'] = {
                        'empresa_id': pos['empresa_id'],
                        'ticker': pos['ticker'],
                        'empresa_nombre': pos['nombre'],
                        'tipo_cartera': tipo_cartera,
                        'ops': ops_db or [],
                    }
                    st.session_state['accion_activa'] = 'editar'
                    st.rerun()

    # ── CTA Nueva Operación ───────────────────────────────────────────────
    _renderizar_cta_nueva_operacion()


def _renderizar_cta_nueva_operacion():
    """Renderiza el botón CTA para abrir el formulario de nueva operación (punto único en show())."""
    st.markdown("<div style='margin-top: 8px;'></div>", unsafe_allow_html=True)
    col_cta1, col_cta2, col_cta3 = st.columns([2, 1, 2])
    with col_cta2:
        if st.button("+ Nueva Operación", key="btn_cta_nueva_op", use_container_width=True):
            # Limpiar cualquier compra preseleccionada previa para que las 6 pestañas estén habilitadas
            _limpiar_estados_modales_cartera()
            st.session_state["accion_activa"] = "nueva_operacion"
            st.session_state["cartera_modal_open"] = True
            st.rerun()


@st.dialog("Añadir Operación", width="large", on_dismiss=_limpiar_estados_modales_cartera)
def _dialog_nueva_operacion():
    """Renderiza el formulario de operaciones como popup modal flotante."""

    # CSS del modal
    st.markdown("""
    <style>
        div[data-testid="stDialog"] > div {
            background: #111723 !important;
            border: 1px solid rgba(148, 163, 184, 0.12) !important;
            border-radius: 16px !important;
        }
        div[data-testid="stDialog"] h2 {
            color: #ffffff !important;
            font-family: 'Inter', sans-serif !important;
            font-weight: 700 !important;
        }
    </style>
    """, unsafe_allow_html=True)

    tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")
    es_precompra = (
        st.session_state.get("accion_activa") == "comprar"
        and "compra_preseleccionada" in st.session_state
    )

    if "tipo_operacion_dialog" not in st.session_state:
        st.session_state["tipo_operacion_dialog"] = "Compra"

    if es_precompra:
        st.session_state["tipo_operacion_dialog"] = "Compra"

    tipo_operacion = st.session_state["tipo_operacion_dialog"]

    st.markdown('<div style="font-family:Inter,sans-serif;font-size:0.75rem;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">Tipo de Operación</div>', unsafe_allow_html=True)

    c1, c2, c3, c4, c5, c6 = st.columns(6)

    with c1:
        if st.button("Compra", key="dlg_tipo_compra", use_container_width=True, type="primary" if tipo_operacion == "Compra" else "secondary"):
            if st.session_state["tipo_operacion_dialog"] != "Compra":
                st.session_state["tipo_operacion_dialog"] = "Compra"
                st.rerun()

    with c2:
        if st.button("Venta", key="dlg_tipo_venta", use_container_width=True, type="primary" if tipo_operacion == "Venta" else "secondary", disabled=es_precompra):
            if st.session_state["tipo_operacion_dialog"] != "Venta":
                st.session_state["tipo_operacion_dialog"] = "Venta"
                st.rerun()

    with c3:
        if st.button("Dividendos", key="dlg_tipo_div", use_container_width=True, type="primary" if tipo_operacion == "Dividendos" else "secondary", disabled=es_precompra):
            if st.session_state["tipo_operacion_dialog"] != "Dividendos":
                st.session_state["tipo_operacion_dialog"] = "Dividendos"
                st.rerun()

    with c4:
        if st.button("Ingreso Efectivo", key="dlg_tipo_ingreso", use_container_width=True, type="primary" if tipo_operacion == "Ingreso Efectivo" else "secondary", disabled=es_precompra):
            if st.session_state["tipo_operacion_dialog"] != "Ingreso Efectivo":
                st.session_state["tipo_operacion_dialog"] = "Ingreso Efectivo"
                st.rerun()

    with c5:
        if st.button("Retirada Efectivo", key="dlg_tipo_retirada", use_container_width=True, type="primary" if tipo_operacion == "Retirada Efectivo" else "secondary", disabled=es_precompra):
            if st.session_state["tipo_operacion_dialog"] != "Retirada Efectivo":
                st.session_state["tipo_operacion_dialog"] = "Retirada Efectivo"
                st.rerun()

    with c6:
        if st.button("Comisiones", key="dlg_tipo_comision", use_container_width=True, type="primary" if tipo_operacion == "Comisiones" else "secondary", disabled=es_precompra):
            if st.session_state["tipo_operacion_dialog"] != "Comisiones":
                st.session_state["tipo_operacion_dialog"] = "Comisiones"
                st.rerun()

    st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)

    empresas = obtener_empresas_disponibles()
    venta_sin_posiciones = False
    empresa_sel = None
    cantidad = 0.0
    precio = 0.01
    comision = 0.0
    importe = 0.01
    fecha = date.today()
    notas = ""

    # Determinar índice preseleccionado para la compra
    index_sel = 0
    if es_precompra:
        ticker_pre = st.session_state.get("compra_preseleccionada")
        for i, e in enumerate(empresas):
            if e['ticker'] == ticker_pre:
                index_sel = i
                break

    if tipo_operacion == "Compra":
        opciones_empresas = [f"{e['ticker']} - {e['nombre']}" for e in empresas]
        empresa_sel = st.selectbox("Empresa", options=opciones_empresas, index=index_sel, key="dlg_empresa_sel")
        ticker_actual_dlg = empresa_sel.split(" - ")[0] if empresa_sel else ""
        tk_c_key = ticker_actual_dlg.replace(".", "_")

        # Precargar precio de mercado real al seleccionar una empresa
        # (usa precio_operacion_input solo si sigue seleccionada la empresa precomprada)
        _tk_pre_cmp = st.session_state.get("compra_preseleccionada")
        if es_precompra and ticker_actual_dlg == _tk_pre_cmp and float(st.session_state.get('precio_operacion_input') or 0.0) > 0.01:
            precio_default = float(st.session_state['precio_operacion_input'])
        else:
            p_mkt = float(_obtener_precio_actual(ticker_actual_dlg) or 0.0)
            precio_default = p_mkt if p_mkt > 0.01 else 0.01

        col1, col2, col3 = st.columns(3)

        with col1:
            cantidad = st.number_input("Cantidad", min_value=0.01, value=1.00, step=0.01, format="%.2f", key=f"dlg_cantidad_{tk_c_key}")

        with col2:
            precio = st.number_input("Precio (€)", min_value=0.01, value=round(float(precio_default), 4), step=0.01, format="%.4f", key=f"dlg_precio_{tk_c_key}")

        with col3:
            comision = st.number_input("Comisión (€)", min_value=0.0, value=0.0, step=0.01, format="%.2f", key=f"dlg_comision_{tk_c_key}")

        # Vista previa en vivo: Coste estimado vs Liquidez disponible
        liq_disp_c = float(calcular_liquidez_filtrada(tipo_cartera) or 0.0)
        coste_est_c = (float(cantidad) * float(precio)) + float(comision)
        max_acc_aseq = max(0.0, (liq_disp_c - float(comision)) / float(precio)) if precio > 0 else 0.0
        insuf_c = coste_est_c > (liq_disp_c + 0.001)
        box_cls = "order-preview-box warn" if insuf_c else "order-preview-box"
        estado_txt = (
            f'<span style="color:#f87171;font-weight:700;">Saldo insuficiente (máx. {max_acc_aseq:.2f} acc.)</span>'
            if insuf_c
            else f'<span style="color:#34d399;">Saldo suficiente (máx. {max_acc_aseq:.2f} acc.)</span>'
        )
        st.html(
            f'<div class="{box_cls}">'
            f'<span>Coste estimado: <span class="order-preview-mono">{_formatear_moneda(coste_est_c)}</span> · '
            f'Liquidez {tipo_cartera}: <span class="order-preview-mono">{_formatear_moneda(liq_disp_c)}</span></span>'
            f'{estado_txt}'
            f'</div>'
        )

        fecha = st.date_input("Fecha", date.today(), max_value=date.today(), key="dlg_fecha")
        notas = st.text_area("Notas (opcional)", height=60, key="dlg_notas")

    elif tipo_operacion == "Venta":
        posiciones_venta = calcular_posiciones_abiertas_filtradas(tipo_cartera)
        if not posiciones_venta:
            venta_sin_posiciones = True
            st.info(f"No tienes posiciones abiertas disponibles para vender en tu cartera **{tipo_cartera}**.")
        else:
            mapa_pos_v = {f"{p['ticker']} - {p['nombre']}": p for p in posiciones_venta}
            empresa_sel = st.selectbox("Empresa en Cartera", options=list(mapa_pos_v.keys()), key="dlg_empresa_venta_sel")
            pos_v_sel = mapa_pos_v[empresa_sel]
            tk_v_key = str(pos_v_sel['ticker']).replace(".", "_")
            max_acc = max(0.01, float(pos_v_sel['acciones']))
            st.caption(f"Acciones disponibles en cartera {tipo_cartera}: **{max_acc:.4f}**")

            col1, col2, col3 = st.columns(3)

            with col1:
                cantidad = st.number_input("Cantidad", min_value=0.01, max_value=max_acc, value=min(1.0, max_acc), step=0.01, format="%.4f", key=f"dlg_cantidad_venta_{tk_v_key}")

            with col2:
                precio_def_v = _obtener_precio_actual(pos_v_sel['ticker']) or float(pos_v_sel.get('precio_medio', 0.01))
                if precio_def_v <= 0.01:
                    precio_def_v = 0.01
                precio = st.number_input("Precio (€)", min_value=0.01, value=float(precio_def_v), step=0.01, format="%.2f", key=f"dlg_precio_venta_{tk_v_key}")

            with col3:
                comision = st.number_input("Comisión (€)", min_value=0.0, step=0.01, format="%.2f", key=f"dlg_comision_venta_{tk_v_key}")

            fecha = st.date_input("Fecha", date.today(), max_value=date.today(), key="dlg_fecha_venta")
            notas = st.text_area("Notas (opcional)", height=60, key="dlg_notas_venta")

    elif tipo_operacion == "Dividendos":
        opciones_empresas = [f"{e['ticker']} - {e['nombre']}" for e in empresas]
        empresa_sel = st.selectbox("Empresa", options=opciones_empresas, key="dlg_empresa_div")

        col1, col2 = st.columns(2)

        with col1:
            importe = st.number_input("Importe Total (€)", min_value=0.01, step=0.01, format="%.2f", key="dlg_importe_div")

        with col2:
            fecha = st.date_input("Fecha", date.today(), max_value=date.today(), key="dlg_fecha_div")

        notas = st.text_area("Notas (opcional)", height=60, key="dlg_notas_div")

    else:
        # Movimientos de efectivo
        col1, col2 = st.columns(2)

        with col1:
            importe = st.number_input("Importe (€)", min_value=0.01, step=0.01, format="%.2f", key="dlg_importe_efe")

        with col2:
            fecha = st.date_input("Fecha", date.today(), max_value=date.today(), key="dlg_fecha_efe")

        notas = st.text_area("Notas (opcional)", height=60, key="dlg_notas_efe")

    st.markdown("<div style='margin-top: 8px;'></div>", unsafe_allow_html=True)

    col_b1, col_b2 = st.columns(2)
    with col_b1:
        submitted = st.button(
            "Confirmar Operación",
            key="dlg_btn_confirmar",
            use_container_width=True,
            type="primary",
            disabled=venta_sin_posiciones,
        )
    with col_b2:
        cancel = st.button("Cancelar", key="dlg_btn_cancelar", use_container_width=True)

    if cancel:
        _limpiar_estados_modales_cartera()
        st.rerun()

    if submitted:
        if tipo_operacion == "Compra":
            ticker_sel = empresa_sel.split(" - ")[0]
            empresa_data = next((e for e in empresas if e['ticker'] == ticker_sel), None)

            if empresa_data:
                exito = agregar_operacion(
                    empresa_id=empresa_data['id'],
                    tipo="COMPRA",
                    cantidad=cantidad,
                    precio_unitario=precio,
                    comision=comision,
                    fecha=fecha.strftime("%Y-%m-%d"),
                    notas=f"[{tipo_cartera}] {notas}" if notas else f"[{tipo_cartera}]",
                    tipo_cartera=tipo_cartera,
                )

                if exito:
                    st.success("Compra registrada correctamente")
                    _limpiar_estados_modales_cartera()
                    time.sleep(0.6)
                    st.rerun()

        elif tipo_operacion == "Venta" and empresa_sel:
            pos_v_submit = locals().get("mapa_pos_v", {}).get(empresa_sel)
            empresa_id_v = pos_v_submit['empresa_id'] if pos_v_submit else None
            if empresa_id_v is None:
                ticker_sel = empresa_sel.split(" - ")[0]
                empresa_data = next((e for e in empresas if e['ticker'] == ticker_sel), None)
                empresa_id_v = empresa_data['id'] if empresa_data else None

            if empresa_id_v is not None:
                exito = agregar_operacion(
                    empresa_id=empresa_id_v,
                    tipo="VENTA",
                    cantidad=cantidad,
                    precio_unitario=precio,
                    comision=comision,
                    fecha=fecha.strftime("%Y-%m-%d"),
                    notas=f"[{tipo_cartera}] {notas}" if notas else f"[{tipo_cartera}]",
                    tipo_cartera=tipo_cartera,
                )

                if exito:
                    st.success("Venta registrada correctamente")
                    _limpiar_estados_modales_cartera()
                    time.sleep(0.6)
                    st.rerun()

        elif tipo_operacion == "Dividendos":
            ticker_sel = empresa_sel.split(" - ")[0]
            empresa_data = next((e for e in empresas if e['ticker'] == ticker_sel), None)

            if empresa_data:
                exito = agregar_operacion(
                    empresa_id=empresa_data['id'],
                    tipo="DIVIDENDO",
                    importe_total=importe,
                    fecha=fecha.strftime("%Y-%m-%d"),
                    notas=f"[{tipo_cartera}] {notas}" if notas else f"[{tipo_cartera}]",
                    tipo_cartera=tipo_cartera,
                )

                if exito:
                    st.success("Dividendo registrado correctamente")
                    _limpiar_estados_modales_cartera()
                    time.sleep(0.6)
                    st.rerun()

        elif tipo_operacion == "Ingreso Efectivo":
            exito = agregar_operacion(
                tipo="INGRESO_EFECTIVO",
                importe_total=importe,
                fecha=fecha.strftime("%Y-%m-%d"),
                notas=f"[{tipo_cartera}] {notas}" if notas else f"[{tipo_cartera}]",
                tipo_cartera=tipo_cartera,
            )

            if exito:
                st.success("Ingreso registrado correctamente")
                _limpiar_estados_modales_cartera()
                time.sleep(0.6)
                st.rerun()

        elif tipo_operacion == "Retirada Efectivo":
            exito = agregar_operacion(
                tipo="RETIRADA_EFECTIVO",
                importe_total=importe,
                fecha=fecha.strftime("%Y-%m-%d"),
                notas=f"[{tipo_cartera}] {notas}" if notas else f"[{tipo_cartera}]",
                tipo_cartera=tipo_cartera,
            )

            if exito:
                st.success("Retirada registrada correctamente")
                _limpiar_estados_modales_cartera()
                time.sleep(0.6)
                st.rerun()

        elif tipo_operacion == "Comisiones":
            exito = agregar_operacion(
                tipo="COMISION",
                importe_total=importe,
                fecha=fecha.strftime("%Y-%m-%d"),
                notas=f"[{tipo_cartera}] {notas}" if notas else f"[{tipo_cartera}]",
                tipo_cartera=tipo_cartera,
            )

            if exito:
                st.success("Comisión registrada correctamente")
                _limpiar_estados_modales_cartera()
                time.sleep(0.6)
                st.rerun()


def _renderizar_historial_colapsable():
    """Renderiza el historial de operaciones en sección colapsable (estrictamente filtrado por cartera activa)."""
    tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")
    prefijo = f"[{tipo_cartera}]"
    todas_ops = obtener_operaciones()
    ops_cartera = [op for op in todas_ops if prefijo in (op.get('notas') or '')]
    cant_ops = len(ops_cartera)

    with st.expander(f"Historial de Operaciones  [ {cant_ops} registros ]", expanded=False):
        # Filtros con claves aisladas por tipo de cartera
        col_filtro1, col_filtro2 = st.columns(2)
        empresas_disp = obtener_empresas_disponibles()

        with col_filtro1:
            filtro_tipo = st.selectbox(
                "TIPO DE OPERACIÓN",
                options=["Todas", "COMPRA", "VENTA", "DIVIDENDO", "INGRESO_EFECTIVO", "RETIRADA_EFECTIVO", "COMISION"],
                key=f"hist_filtro_tipo_{tipo_cartera}",
            )

        with col_filtro2:
            filtro_empresa = st.selectbox(
                "EMPRESA",
                options=["Todas"] + [f"{e['ticker']} - {e['nombre']}" for e in empresas_disp],
                key=f"hist_filtro_empresa_{tipo_cartera}",
            )

        # Obtener operaciones filtradas estrictamente por cartera activa
        empresa_id = None
        if filtro_empresa != "Todas":
            ticker = filtro_empresa.split(" - ")[0]
            empresa_data = next((e for e in empresas_disp if e['ticker'] == ticker), None)
            if empresa_data:
                empresa_id = empresa_data['id']

        tipo_filtro = filtro_tipo if filtro_tipo != "Todas" else None
        operaciones_raw = obtener_operaciones(empresa_id=empresa_id, tipo=tipo_filtro)
        operaciones = [op for op in operaciones_raw if prefijo in (op.get('notas') or '')]

        if not operaciones:
            st.html("""
            <div class="cartera-empty-shell" style="padding: 24px 16px;">
                <div class="cartera-empty-title" style="font-size: 0.92rem;">Sin Operaciones Registradas</div>
                <div class="cartera-empty-desc" style="font-size: 0.78rem;">No se encontraron movimientos que coincidan con los filtros seleccionados.</div>
            </div>
            """)
            return

        # Construir DataFrame
        filas = []
        for op in operaciones:
            filas.append({
                'Fecha': op['fecha'],
                'Tipo': op['tipo'],
                'Ticker': op.get('ticker') or '—',
                'Empresa': op.get('empresa_nombre') or '—',
                'Cantidad': f"{op['cantidad']:.2f}" if (op.get('cantidad') or 0) > 0 else '—',
                'Precio': _formatear_moneda(op['precio_unitario']) if (op.get('precio_unitario') or 0) > 0 else '—',
                'Importe': _formatear_moneda(op.get('importe_total', 0)),
                'Comisión': _formatear_moneda(op['comision']),
                'Notas': op['notas'] or '—'
            })

        df_historial = pd.DataFrame(filas)

        st.dataframe(
            df_historial,
            use_container_width=True,
            hide_index=True,
            height=min(400, len(filas) * 35 + 70)
        )

        # Exportar CSV + Gestión completa (editar / eliminar cualquier movimiento del historial)
        st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
        h_col1, h_col2, h_col3, h_col4 = st.columns([4.2, 1.3, 1.3, 1.8], vertical_alignment="bottom")

        etiquetas_hist = [
            f"#{o['id']} · {o['fecha']} · {o['tipo']} · {(o.get('ticker') or 'Efectivo')} · {_formatear_moneda(float(o.get('importe_total') or 0.0))}"
            for o in operaciones
        ]
        with h_col1:
            sel_h_idx = st.selectbox(
                "GESTIONAR MOVIMIENTO DEL HISTORIAL",
                options=list(range(len(operaciones))),
                format_func=lambda i: etiquetas_hist[i],
                key=f"hist_sel_op_{tipo_cartera}",
            )
        op_h_sel = operaciones[sel_h_idx]

        with h_col2:
            if st.button("Editar", key=f"btn_hist_edit_{tipo_cartera}", use_container_width=True):
                _limpiar_estados_modales_cartera()
                st.session_state["scroll_al_techo"] = True
                st.session_state["editar_operacion"] = {
                    "empresa_id": op_h_sel.get("empresa_id"),
                    "ticker": op_h_sel.get("ticker") or "EFECTIVO",
                    "empresa_nombre": op_h_sel.get("empresa_nombre") or "Movimiento de Efectivo / Gasto",
                    "tipo_cartera": tipo_cartera,
                    "ops": [op_h_sel],
                }
                st.session_state["accion_activa"] = "editar"
                st.rerun()

        with h_col3:
            if st.button("Eliminar", key=f"btn_hist_del_{tipo_cartera}", use_container_width=True):
                _limpiar_estados_modales_cartera()
                st.session_state["scroll_al_techo"] = True
                st.session_state["confirmar_eliminar"] = {
                    "id": op_h_sel["id"],
                    "tipo": op_h_sel["tipo"],
                    "cantidad": float(op_h_sel.get("cantidad") or 0.0),
                    "ticker": op_h_sel.get("ticker") or "Efectivo",
                    "fecha": op_h_sel["fecha"],
                    "precio_unitario": float(op_h_sel.get("precio_unitario") or 0.0),
                    "importe_total": float(op_h_sel.get("importe_total") or 0.0),
                }
                st.rerun()

        with h_col4:
            csv_hist = pd.DataFrame([
                {
                    "ID": o["id"],
                    "Fecha": o["fecha"],
                    "Cartera": tipo_cartera,
                    "Tipo": o["tipo"],
                    "Ticker": o.get("ticker") or "",
                    "Empresa": o.get("empresa_nombre") or "",
                    "Cantidad": float(o.get("cantidad") or 0.0),
                    "Precio_EUR": float(o.get("precio_unitario") or 0.0),
                    "Importe_EUR": float(o.get("importe_total") or 0.0),
                    "Comision_EUR": float(o.get("comision") or 0.0),
                    "Notas": o.get("notas") or "",
                }
                for o in operaciones
            ]).to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label=f"Exportar CSV ({len(operaciones)})",
                data=csv_hist,
                file_name=f"stocklab_cartera_{tipo_cartera.lower()}.csv",
                mime="text/csv",
                use_container_width=True,
                key=f"btn_hist_csv_{tipo_cartera}",
            )


def show():
    """Función principal del módulo Mi Cartera V2.0."""
    if st.session_state.get('scroll_al_techo'):
        st.iframe('''
            <script>
                setTimeout(function() {
                    const mainContainer = window.parent.document.querySelector('.main');
                    if (mainContainer) mainContainer.scrollTo({top: 0, behavior: 'auto'});
                    window.parent.scrollTo(0, 0);
                }, 200);
            </script>
        ''', height=1)
        st.session_state['scroll_al_techo'] = False

    # Inyectar estilos premium de forma limpia
    st.html(STYLES)

    # Cabecera premium institucional
    cabecera_html = """
    <div class="sl-page-header cartera-page-header">
        <h1 class="sl-page-title">Mi Cartera</h1>
        <span class="sl-page-subtitle">Gestión integral de inversiones</span>
    </div>
    """
    st.html(cabecera_html)

    # 1. Selector de Cartera (Arriba del todo)
    _renderizar_pestanas_superiores()

    # Modales centrados (Estado Único — nunca más de un @st.dialog activo por rerun)
    accion = st.session_state.get('accion_activa')
    if accion == 'nueva_operacion' or (accion == 'comprar' and 'compra_preseleccionada' in st.session_state):
        _dialog_nueva_operacion()
    elif accion == 'vender' and 'modal_venta' in st.session_state:
        _renderizar_modal_venta()
    elif accion == 'editar' and 'editar_operacion' in st.session_state:
        _renderizar_modal_edicion()
    elif 'confirmar_eliminar' in st.session_state:
        _renderizar_confirmacion_eliminacion()

    with st.container(key="cartera_main_content"):

        # ── BLOQUE 1: KPIs con jerarquía real (Hero + Secondary + Tertiary) ──
        try:
            _renderizar_tarjetas_kpi()
        except Exception as e:
            st.error(f"Error al cargar métricas: {str(e)}")

        # ── BLOQUE 2: Estado de la cartera (línea compacta inline) ───────────
        try:
            _renderizar_diagnostico_cartera()
        except Exception as e:
            st.error(f"Error al cargar diagnóstico de cartera: {str(e)}")

        st.html(DIVIDER_HTML)

        # ── BLOQUE 3: Gráfico de Evolución (posición elevada, protagonismo) ──
        try:
            _renderizar_grafico_evolucion()
        except Exception as e:
            st.error(f"Error al cargar gráfico de evolución: {str(e)}")

        st.html(DIVIDER_HTML)

        # ── BLOQUE 4: Posiciones Abiertas (ancho completo, cards compactas) ──
        try:
            _renderizar_cards_posiciones()
        except Exception as e:
            st.error(f"Error al cargar posiciones: {str(e)}")

        # ── BLOQUE 4b: Formulario Nueva Operación ahora es un @st.dialog popup ──
        # (Se activa automáticamente al pulsar el CTA '+ Nueva Operación')

        st.html(DIVIDER_HTML)

        # ── BLOQUE 5: Distribución de Cartera ────────────────────────────────
        try:
            _renderizar_distribucion_cartera()
        except Exception as e:
            st.error(f"Error al cargar distribución: {str(e)}")

        st.html(DIVIDER_HTML)

        # ── BLOQUE 6: Historial de Operaciones (colapsable) ───────────────────
        try:
            _renderizar_historial_colapsable()
        except Exception as e:
            st.error(f"Error al cargar historial: {str(e)}")


_DIALOG_CSS = """
<style>
    div[data-testid="stDialog"] > div {
        background: #111723 !important;
        border: 1px solid rgba(148, 163, 184, 0.14) !important;
        border-radius: 16px !important;
    }
    div[data-testid="stDialog"] h2 {
        color: #ffffff !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
    }
</style>
"""


@st.dialog("Registrar Venta", width="large", on_dismiss=_limpiar_estados_modales_cartera)
def _renderizar_modal_venta():
    """Renderiza el modal centrado para vender posiciones."""
    if 'modal_venta' not in st.session_state:
        return
    st.markdown(_DIALOG_CSS, unsafe_allow_html=True)

    datos_venta = st.session_state['modal_venta']
    tipo_cartera = datos_venta.get("tipo_cartera") or st.session_state.get("tipo_cartera_activa", "SIMULADA")

    st.info(f"**{datos_venta['ticker']} - {datos_venta['nombre']}** | Acciones disponibles: **{datos_venta['acciones_disponibles']:.4f}** · Cartera **{tipo_cartera}**")

    with st.form("form_venta", clear_on_submit=True):
        col1, col2 = st.columns(2)

        with col1:
            max_acc_venta = max(0.01, float(datos_venta['acciones_disponibles']))
            cantidad_venta = st.number_input(
                "Cantidad a Vender",
                min_value=0.01,
                max_value=max_acc_venta,
                step=0.01,
                format="%.4f"
            )

            fecha_venta = st.date_input("Fecha de Venta", value=date.today(), max_value=date.today())

        with col2:
            precio_default = st.session_state.get('precio_operacion_input', float(datos_venta.get('precio_actual', 0.01)))
            if precio_default <= 0.01:
                precio_default = 0.01
            precio_venta = st.number_input("Precio de Venta (€)", min_value=0.01, value=float(precio_default), step=0.01, format="%.4f")
            comision_venta = st.number_input("Comisión (€)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
            notas_venta = st.text_area("Notas (opcional)", placeholder="Motivo de la venta...", height=80)

        col_btn1, col_btn2 = st.columns(2)

        with col_btn1:
            submitted = st.form_submit_button("Confirmar Venta", use_container_width=True, type="primary")

        with col_btn2:
            cancel = st.form_submit_button("Cancelar", use_container_width=True)

        if submitted:
            exito = agregar_operacion(
                empresa_id=datos_venta['empresa_id'],
                tipo="VENTA",
                cantidad=cantidad_venta,
                precio_unitario=precio_venta,
                comision=comision_venta,
                fecha=fecha_venta.strftime("%Y-%m-%d"),
                notas=f"[{tipo_cartera}] {notas_venta}" if notas_venta else f"[{tipo_cartera}]",
                tipo_cartera=tipo_cartera,
            )

            if exito:
                st.success(f"Venta registrada: {cantidad_venta} acciones de {datos_venta['ticker']}")
                _limpiar_estados_modales_cartera()
                st.rerun()
            else:
                st.error("Error al registrar la venta.")

        if cancel:
            _limpiar_estados_modales_cartera()
            st.rerun()


@st.dialog("Editar Operación", width="large", on_dismiss=_limpiar_estados_modales_cartera)
def _renderizar_modal_edicion():
    """Renderiza el modal centrado para editar una operación individual."""
    if 'editar_operacion' not in st.session_state:
        return
    st.markdown(_DIALOG_CSS, unsafe_allow_html=True)

    edit_ctx = st.session_state['editar_operacion']
    tipo_cartera = edit_ctx.get("tipo_cartera") or st.session_state.get("tipo_cartera_activa", "SIMULADA")
    prefijo = f"[{tipo_cartera}]"
    ops_list = edit_ctx.get("ops") or []

    # Compatibilidad con dict plano heredado
    if not ops_list and edit_ctx.get("id"):
        ops_list = [edit_ctx]

    st.info(f"**{edit_ctx['ticker']} - {edit_ctx['empresa_nombre']}** · Cartera **{tipo_cartera}**")

    if not ops_list:
        st.warning("No se encontraron operaciones individuales editables para esta posición.")
        if st.button("Cerrar", key="btn_cerrar_edit_vacio"):
            _limpiar_estados_modales_cartera()
            st.rerun()
        return

    if len(ops_list) > 1:
        etiquetas_ops = [
            f"#{o['id']} · {o['fecha']} · {o['tipo']} · {float(o['cantidad']):.2f} acc @ {_formatear_moneda(float(o['precio_unitario']))}"
            for o in ops_list
        ]
        sel_idx = st.selectbox(
            "OPERACIÓN INDIVIDUAL A EDITAR",
            options=list(range(len(ops_list))),
            format_func=lambda i: etiquetas_ops[i],
            key=f"sel_op_edit_{edit_ctx['empresa_id']}_{tipo_cartera}",
        )
        op = ops_list[sel_idx]
    else:
        op = ops_list[0]

    op_id = op['id']
    tipo_op_orig = str(op.get('tipo') or 'COMPRA').upper()
    es_op_acciones = tipo_op_orig in ('COMPRA', 'VENTA')
    notas_limpias = (op.get('notas') or "").replace("[SIMULADA]", "").replace("[REAL]", "").strip()
    fecha_op_val = min(date.today(), datetime.strptime(str(op['fecha'])[:10], "%Y-%m-%d").date())

    with st.form(f"form_editar_operacion_{op_id}"):
        col1, col2 = st.columns(2)

        if es_op_acciones:
            with col1:
                nuevo_tipo = st.selectbox("Tipo", options=["COMPRA", "VENTA"], index=0 if tipo_op_orig == "COMPRA" else 1)
                nueva_fecha = st.date_input("Fecha", value=fecha_op_val, max_value=date.today())
                nueva_cantidad = st.number_input("Cantidad", min_value=0.01, value=max(0.01, float(op.get('cantidad') or 0.01)), step=0.01, format="%.4f")

            with col2:
                nuevo_precio = st.number_input("Precio Unitario (€)", min_value=0.01, value=max(0.01, float(op.get('precio_unitario') or 0.01)), step=0.01, format="%.4f")
                nueva_comision = st.number_input("Comisión (€)", min_value=0.0, value=max(0.0, float(op.get('comision') or 0.0)), step=0.01, format="%.2f")
                nuevas_notas = st.text_area("Notas", value=notas_limpias, height=80)
            nuevo_importe = round(float(nueva_cantidad) * float(nuevo_precio), 2)
        elif tipo_op_orig == "DIVIDENDO":
            with col1:
                nuevo_tipo = st.selectbox("Tipo", options=["DIVIDENDO"], index=0)
                nueva_fecha = st.date_input("Fecha", value=fecha_op_val, max_value=date.today())
            with col2:
                nuevo_importe = st.number_input("Importe (€)", min_value=0.01, value=max(0.01, float(op.get('importe_total') or 0.01)), step=0.01, format="%.2f")
                nuevas_notas = st.text_area("Notas", value=notas_limpias, height=80)
            nueva_cantidad = 0.0
            nuevo_precio = 0.0
            nueva_comision = 0.0
        else:
            tipos_efectivo = ["INGRESO_EFECTIVO", "RETIRADA_EFECTIVO", "COMISION"]
            idx_te = tipos_efectivo.index(tipo_op_orig) if tipo_op_orig in tipos_efectivo else 0
            with col1:
                nuevo_tipo = st.selectbox("Tipo", options=tipos_efectivo, index=idx_te)
                nueva_fecha = st.date_input("Fecha", value=fecha_op_val, max_value=date.today())
            with col2:
                nuevo_importe = st.number_input("Importe (€)", min_value=0.01, value=max(0.01, float(op.get('importe_total') or 0.01)), step=0.01, format="%.2f")
                nuevas_notas = st.text_area("Notas", value=notas_limpias, height=80)
            nueva_cantidad = 0.0
            nuevo_precio = 0.0
            nueva_comision = 0.0

        col_btn1, col_btn2, col_btn3 = st.columns([2, 1.5, 1.2])

        with col_btn1:
            submitted = st.form_submit_button("Guardar Cambios", use_container_width=True, type="primary")

        with col_btn2:
            cancel = st.form_submit_button("Cancelar", use_container_width=True)

        with col_btn3:
            pedir_eliminar = st.form_submit_button("Eliminar", use_container_width=True)

        if submitted:
            notas_guardar = f"{prefijo} {nuevas_notas.strip()}".strip() if nuevas_notas.strip() else prefijo
            exito = actualizar_operacion(
                operacion_id=op_id,
                tipo=nuevo_tipo,
                cantidad=nueva_cantidad,
                precio_unitario=nuevo_precio,
                importe_total=nuevo_importe,
                fecha=nueva_fecha.strftime("%Y-%m-%d"),
                comision=nueva_comision,
                notas=notas_guardar,
                tipo_cartera=tipo_cartera,
            )

            if exito:
                st.success("Operación actualizada correctamente")
                _limpiar_estados_modales_cartera()
                st.rerun()
            else:
                st.error("Error al actualizar la operación.")

        if cancel:
            _limpiar_estados_modales_cartera()
            st.rerun()

        if pedir_eliminar:
            st.session_state["confirmar_eliminar"] = {
                "id": op_id,
                "tipo": op["tipo"],
                "cantidad": float(op.get("cantidad") or 0.0),
                "ticker": edit_ctx["ticker"],
                "fecha": op["fecha"],
                "precio_unitario": float(op.get("precio_unitario") or 0.0),
                "importe_total": float(op.get("importe_total") or 0.0),
            }
            st.session_state.pop("editar_operacion", None)
            st.session_state["accion_activa"] = None
            st.rerun()


@st.dialog("Confirmar Eliminación", width="small", on_dismiss=_limpiar_estados_modales_cartera)
def _renderizar_confirmacion_eliminacion():
    """Renderiza el modal centrado de confirmación para eliminar cualquier tipo de operación."""
    if 'confirmar_eliminar' not in st.session_state:
        return
    st.markdown(_DIALOG_CSS, unsafe_allow_html=True)

    op = st.session_state['confirmar_eliminar']
    tipo_op = str(op.get('tipo') or '').upper()

    if tipo_op in ('COMPRA', 'VENTA'):
        detalle_del = (
            f"**{tipo_op}** de **{float(op.get('cantidad') or 0.0):.4f}** acciones de **{op.get('ticker')}** "
            f"el **{op.get('fecha')}** a **{_formatear_moneda(float(op.get('precio_unitario') or 0.0))}**"
        )
    else:
        detalle_del = (
            f"**{tipo_op}** ({op.get('ticker') or 'Efectivo'}) por **{_formatear_moneda(float(op.get('importe_total') or 0.0))}** "
            f"el **{op.get('fecha')}**"
        )
    st.warning(f"¿Estás seguro de eliminar esta operación?\n\n{detalle_del}")

    col1, col2 = st.columns(2)

    with col1:
        if st.button("Sí, Eliminar", key="btn_confirmar_eliminar", use_container_width=True, type="primary"):
            exito = eliminar_operacion(op['id'])
            if exito:
                st.success("Operación eliminada correctamente")
                _limpiar_estados_modales_cartera()
                st.rerun()
            else:
                st.error("Error al eliminar la operación.")

    with col2:
        if st.button("Cancelar", key="btn_cancelar_eliminar", use_container_width=True):
            _limpiar_estados_modales_cartera()
            st.rerun()
