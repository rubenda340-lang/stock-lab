"""
views/auth.py — Portada y Puerta de Acceso Multiusuario de StockLab (Obsidian Neon)

Soporta:
  - Inicio de sesión con Google (OAuth PKCE S256, `target="_blank"` compatible con Streamlit Cloud)
  - Inicio de sesión y registro con correo electrónico y contraseña
  - Acceso sin contraseña mediante Enlace Mágico (Magic Link)
  - Reenvío de correo de confirmación de cuenta
  - Flujo completo de recuperación y establecimiento de nueva contraseña (`type=recovery`)
"""

from __future__ import annotations

import html as _html
import streamlit as st
from services.auth_service import (
    complete_oauth_from_pkce_code,
    complete_oauth_from_tokens,
    get_google_oauth_url,
    resend_confirmation_email,
    send_magic_link,
    send_password_reset,
    sign_in_email,
    sign_up_email,
    update_password_with_token,
)


def _procesar_callback_oauth() -> None:
    """
    Procesa los callbacks de retorno desde Supabase Auth:
      1) PKCE S256 (`?code=...`): Google OAuth o enlaces PKCE.
      2) Flujo de recuperación de contraseña (`auth_type=recovery` + `auth_at`).
      3) Fallback implícito (`?auth_at=...` / `#access_token=...`) para Magic Link o enlaces de email.
    """
    qp = st.query_params

    # 1. Callback PKCE (compatible con iframe de Streamlit Community Cloud)
    pkce_code = qp.get("code")
    oauth_s = qp.get("oauth_s") or ""
    if pkce_code:
        try:
            for k in ("code", "oauth_s", "error", "error_description"):
                if k in st.query_params:
                    del st.query_params[k]
        except Exception:
            pass
        ok, msg = complete_oauth_from_pkce_code(str(pkce_code), str(oauth_s))
        if ok:
            st.rerun()
        else:
            st.session_state["_auth_flash_error"] = msg
        return

    # Error devuelto por proveedor OAuth
    oauth_err = qp.get("error_description") or qp.get("error")
    if oauth_err:
        try:
            for k in ("error", "error_description", "error_code", "oauth_s"):
                if k in st.query_params:
                    del st.query_params[k]
        except Exception:
            pass
        st.session_state["_auth_flash_error"] = f"Error de acceso: {oauth_err}"
        return

    # 2. Callback de tokens (Magic Link, confirmación o recuperación de contraseña)
    at = qp.get("auth_at")
    rt = qp.get("auth_rt", "")
    exp_raw = qp.get("auth_exp", "3600")
    auth_type = str(qp.get("auth_type") or "").lower()

    if at:
        try:
            exp_i = int(exp_raw)
        except Exception:
            exp_i = 3600
        try:
            for k in ("auth_at", "auth_rt", "auth_exp", "auth_type"):
                if k in st.query_params:
                    del st.query_params[k]
        except Exception:
            pass

        if auth_type == "recovery":
            st.session_state["_sl_recovery_token"] = str(at)
            st.rerun()
            return

        ok, msg = complete_oauth_from_tokens(str(at), str(rt), exp_i)
        if ok:
            st.rerun()
        else:
            st.session_state["_auth_flash_error"] = msg
        return

    # Puente JS ligero solo si hay #access_token=... en la URL (Magic Link / Recovery)
    st.components.v1.html(
        """
        <script>
        (function() {
            try {
                const win = window.parent || window;
                const hash = win.location.hash || "";
                if (hash.includes("access_token=")) {
                    const p = new URLSearchParams(hash.replace(/^#/, ""));
                    const at = p.get("access_token");
                    const rt = p.get("refresh_token") || "";
                    const exp = p.get("expires_in") || "3600";
                    const tp = p.get("type") || "";
                    if (at) {
                        const url = new URL(win.location.href);
                        url.hash = "";
                        url.searchParams.set("auth_at", at);
                        if (rt) url.searchParams.set("auth_rt", rt);
                        url.searchParams.set("auth_exp", exp);
                        if (tp) url.searchParams.set("auth_type", tp);
                        win.location.replace(url.toString());
                    }
                }
            } catch (e) {}
        })();
        </script>
        """,
        height=0,
    )


def _render_html(markup: str) -> None:
    """Inyecta HTML puro sin sangrías ni líneas en blanco (evita bloques de código CommonMark en Streamlit)."""
    clean = "\n".join(line.strip() for line in markup.splitlines() if line.strip())
    st.markdown(clean, unsafe_allow_html=True)


def _inyectar_estilos_auth() -> None:
    _render_html(
        """
        <style>
            /* Contenedor editorial amplio (ocupa ~80% en monitores panorámicos sin estirar el formulario) */
            section[data-testid="stMain"] [data-testid="stMainBlockContainer"] {
                max-width: 1420px !important;
                margin: 0 auto !important;
                padding: clamp(1.5rem, 4.5vh, 3.4rem) clamp(1.5rem, 4vw, 3.5rem) 1.8rem !important;
            }

            /* Fondo ambiental sutil con cuadrícula fintech panorámica */
            section[data-testid="stMain"]::after {
                content: "";
                position: fixed;
                inset: 0;
                pointer-events: none;
                z-index: 0;
                background-image:
                    linear-gradient(rgba(148, 163, 184, 0.035) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(148, 163, 184, 0.035) 1px, transparent 1px);
                background-size: 46px 46px;
                mask-image: radial-gradient(ellipse 88% 78% at 50% 42%, #000 18%, transparent 88%);
            }

            /* ── Columna Izquierda: Identidad Institucional ────────────── */
            .sl-brand-lockup {
                display: inline-flex;
                align-items: center;
                gap: 14px;
                margin-bottom: 18px;
            }
            .sl-brand-mark {
                width: 44px;
                height: 44px;
                border-radius: 12px;
                border: 1px solid rgba(56, 189, 248, 0.45);
                background:
                    linear-gradient(#7dd3fc, #38bdf8) 10px 24px / 5px 9px no-repeat,
                    linear-gradient(#a5f3fc, #38bdf8) 19px 17px / 5px 16px no-repeat,
                    linear-gradient(#d8fff8, #67e8f9) 28px 10px / 5px 23px no-repeat,
                    linear-gradient(145deg, #1b3b4e, #0e1e31);
                box-shadow: 0 0 24px -4px rgba(56, 189, 248, 0.48), inset 0 1px 0 rgba(255,255,255,0.20);
                flex-shrink: 0;
            }
            .sl-brand-name {
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
                font-size: 1.65rem;
                font-weight: 800;
                letter-spacing: -0.045em;
                line-height: 1.05;
                color: #f8fafc;
            }
            .sl-brand-sub {
                font-family: 'Inter', sans-serif;
                font-size: 0.60rem;
                font-weight: 700;
                letter-spacing: 0.22em;
                color: #7c90ad;
                text-transform: uppercase;
                margin-top: 4px;
            }

            .sl-hero-headline {
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
                font-size: clamp(2.2rem, 3.2vw, 3.05rem);
                font-weight: 800;
                line-height: 1.11;
                letter-spacing: -0.04em;
                color: #f8fafc;
                margin: 0 0 16px 0;
            }
            .sl-hero-accent {
                display: block;
                background: linear-gradient(95deg, #7dd3fc 0%, #38bdf8 48%, #818cf8 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }
            .sl-hero-copy {
                font-family: 'Inter', sans-serif;
                font-size: 0.98rem;
                line-height: 1.62;
                color: #94a3b8;
                margin: 0 0 24px 0;
                max-width: 620px;
            }

            /* Cuadrícula 2x2 simétrica con iconos SVG de trazo fino */
            .sl-feat-grid {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 14px;
                margin-bottom: 22px;
            }
            @media (max-width: 640px) {
                .sl-feat-grid { grid-template-columns: 1fr; }
            }
            .sl-feat-card {
                background: linear-gradient(155deg, rgba(20, 31, 50, 0.72), rgba(10, 16, 28, 0.92));
                border: 1px solid rgba(148, 163, 184, 0.12);
                border-radius: 13px;
                padding: 17px 18px;
                min-height: 104px;
                display: flex;
                flex-direction: column;
                justify-content: flex-start;
                position: relative;
                overflow: hidden;
                transition: border-color 200ms ease, transform 200ms ease;
            }
            .sl-feat-card:hover {
                border-color: rgba(56, 189, 248, 0.30);
                transform: translateY(-1px);
            }
            .sl-feat-head {
                display: flex;
                align-items: center;
                gap: 9px;
                margin-bottom: 6px;
            }
            .sl-feat-icon {
                width: 26px;
                height: 26px;
                border-radius: 7px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                flex-shrink: 0;
            }
            .sl-feat-title {
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
                font-size: 0.84rem;
                font-weight: 700;
                color: #f1f5f9;
                letter-spacing: -0.015em;
            }
            .sl-feat-desc {
                font-family: 'Inter', sans-serif;
                font-size: 0.76rem;
                color: #94a3b8;
                line-height: 1.48;
            }

            /* Barra horizontal de métricas de confianza (simétrica) */
            .sl-metrics-bar {
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 12px;
                padding-top: 18px;
                border-top: 1px solid rgba(148, 163, 184, 0.12);
            }
            .sl-metric-item {
                display: flex;
                flex-direction: column;
                gap: 2px;
            }
            .sl-metric-val {
                font-family: 'JetBrains Mono', monospace;
                font-size: 1.05rem;
                font-weight: 700;
                color: #e2e8f0;
                letter-spacing: -0.02em;
            }
            .sl-metric-lbl {
                font-family: 'Inter', sans-serif;
                font-size: 0.71rem;
                color: #64748b;
            }

            /* ── Columna Derecha: Tarjeta Glassmorphic de Acceso ───────── */
            .st-key-sl_auth_card {
                background: linear-gradient(165deg, rgba(20, 31, 50, 0.92) 0%, rgba(10, 15, 27, 0.98) 100%) !important;
                border: 1px solid rgba(148, 163, 184, 0.18) !important;
                border-radius: 18px !important;
                padding: 24px 26px 22px 26px !important;
                min-height: 430px !important;
                box-shadow: 0 22px 54px -14px rgba(0, 0, 0, 0.68), 0 0 38px -16px rgba(56, 189, 248, 0.24), inset 0 1px 0 rgba(255, 255, 255, 0.08) !important;
                position: relative !important;
            }
            .st-key-sl_auth_card::before {
                content: "";
                position: absolute;
                top: 0;
                left: 16%;
                right: 16%;
                height: 1.5px;
                background: linear-gradient(90deg, transparent, #38bdf8, #818cf8, transparent);
                pointer-events: none;
            }
            .sl-card-title {
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
                font-size: 1.22rem;
                font-weight: 700;
                color: #f8fafc;
                letter-spacing: -0.025em;
                margin-bottom: 4px;
            }
            .sl-card-sub {
                font-family: 'Inter', sans-serif;
                font-size: 0.81rem;
                color: #94a3b8;
                line-height: 1.45;
                margin-bottom: 18px;
            }

            /* Botón Google Obsidian-Glass (elegante, sin fogonazo blanco) */
            .sl-google-btn {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                width: 100%;
                height: 44px;
                padding: 0 16px;
                border-radius: 11px;
                background: rgba(255, 255, 255, 0.06);
                color: #f8fafc !important;
                text-decoration: none !important;
                font-family: 'Inter', sans-serif;
                font-weight: 600;
                font-size: 0.86rem;
                letter-spacing: -0.01em;
                border: 1px solid rgba(148, 163, 184, 0.24);
                box-shadow: 0 4px 14px rgba(0, 0, 0, 0.24), inset 0 1px 0 rgba(255, 255, 255, 0.08);
                transition: transform 160ms ease, box-shadow 160ms ease, background 160ms ease, border-color 160ms ease;
                margin-bottom: 14px;
            }
            .sl-google-btn:hover {
                transform: translateY(-1px);
                background: rgba(255, 255, 255, 0.11);
                border-color: rgba(56, 189, 248, 0.48);
                box-shadow: 0 8px 22px rgba(0, 0, 0, 0.35), 0 0 18px -6px rgba(56, 189, 248, 0.35);
            }
            .sl-auth-or {
                display: flex;
                align-items: center;
                gap: 12px;
                color: #64748b;
                font-size: 0.68rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.10em;
                margin: 12px 0 14px 0;
            }
            .sl-auth-or::before, .sl-auth-or::after {
                content: "";
                flex: 1;
                height: 1px;
                background: rgba(148, 163, 184, 0.22);
            }

            /* Pestañas encapsuladas simétricas dentro de la tarjeta */
            .st-key-sl_auth_card .stTabs [data-baseweb="tab-list"] {
                background: rgba(8, 12, 21, 0.88) !important;
                border: 1px solid rgba(148, 163, 184, 0.14) !important;
                border-radius: 11px !important;
                padding: 4px !important;
                gap: 4px !important;
            }
            .st-key-sl_auth_card .stTabs [data-baseweb="tab"] {
                flex: 1 !important;
                justify-content: center !important;
                border-radius: 8px !important;
                font-size: 0.79rem !important;
                font-weight: 600 !important;
                padding: 8px 8px !important;
                color: #94a3b8 !important;
                white-space: nowrap !important;
            }
            .st-key-sl_auth_card .stTabs [data-baseweb="tab"]:hover {
                color: #e2e8f0 !important;
            }
            .st-key-sl_auth_card .stTabs [data-baseweb="tab"][aria-selected="true"] {
                background: linear-gradient(135deg, rgba(56, 189, 248, 0.20), rgba(99, 102, 241, 0.14)) !important;
                border: 1px solid rgba(56, 189, 248, 0.42) !important;
                color: #e0f6ff !important;
                box-shadow: 0 0 14px -6px rgba(56, 189, 248, 0.45) !important;
            }
            .st-key-sl_auth_card .stTabs [data-baseweb="tab-highlight"],
            .st-key-sl_auth_card .stTabs [data-baseweb="tab-border"] {
                display: none !important;
            }

            /* Formulario e inputs integrados */
            .st-key-sl_auth_card div[data-testid="stForm"] {
                background: transparent !important;
                border: none !important;
                padding: 6px 0 0 0 !important;
            }
            .st-key-sl_auth_card div[data-testid="stTextInput"] label p {
                font-size: 0.69rem !important;
                font-weight: 700 !important;
                letter-spacing: 0.08em !important;
                color: #94a3b8 !important;
            }
            .st-key-sl_auth_card div[data-testid="stTextInput"] input {
                background: #0a101d !important;
                border: 1px solid rgba(148, 163, 184, 0.18) !important;
                border-radius: 10px !important;
                color: #f8fafc !important;
                height: 44px !important;
                font-size: 0.88rem !important;
            }
            .st-key-sl_auth_card div[data-testid="stTextInput"] input::placeholder {
                color: #64748b !important;
            }
            .st-key-sl_auth_card div[data-testid="stTextInput"] input:focus {
                border-color: rgba(56, 189, 248, 0.60) !important;
                box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.14) !important;
            }
            .st-key-sl_auth_card div[data-testid="stCheckbox"] label p {
                font-size: 0.77rem !important;
                color: #94a3b8 !important;
                text-transform: none !important;
                letter-spacing: 0 !important;
                line-height: 1.45 !important;
            }

            /* Botón primario Obsidian Neon armonizado */
            .st-key-sl_auth_card div[data-testid="stFormSubmitButton"] button,
            .st-key-sl_auth_card button[kind="primary"] {
                background: linear-gradient(135deg, rgba(56, 189, 248, 0.30), rgba(99, 102, 241, 0.26)) !important;
                border: 1px solid rgba(56, 189, 248, 0.58) !important;
                color: #f0f9ff !important;
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
                font-weight: 700 !important;
                font-size: 0.88rem !important;
                height: 44px !important;
                border-radius: 10px !important;
                box-shadow: 0 6px 20px -6px rgba(56, 189, 248, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.16) !important;
                transition: all 180ms ease !important;
            }
            .st-key-sl_auth_card div[data-testid="stFormSubmitButton"] button:hover,
            .st-key-sl_auth_card button[kind="primary"]:hover {
                background: linear-gradient(135deg, rgba(56, 189, 248, 0.42), rgba(99, 102, 241, 0.36)) !important;
                border-color: rgba(125, 211, 252, 0.88) !important;
                color: #ffffff !important;
                transform: translateY(-1px) !important;
            }

            /* Pie institucional simétrico */
            .sl-auth-footer {
                margin-top: 1.8rem;
                padding-top: 1rem;
                border-top: 1px solid rgba(148, 163, 184, 0.12);
                display: flex;
                justify-content: space-between;
                align-items: center;
                flex-wrap: wrap;
                gap: 8px 20px;
                font-family: 'Inter', sans-serif;
                font-size: 0.72rem;
                color: #64748b;
            }
        </style>
        """
    )


def _render_panel_marca() -> None:
    _render_html(
        """
        <div class="sl-brand-lockup">
            <div class="sl-brand-mark"></div>
            <div>
                <div class="sl-brand-name">StockLab</div>
                <div class="sl-brand-sub">INVESTMENT TERMINAL</div>
            </div>
        </div>
        <div class="sl-hero-headline">
            Inversión bursátil,
            <span class="sl-hero-accent">con claridad y control.</span>
        </div>
        <p class="sl-hero-copy">
            Centraliza tu cartera real y simulada, monitoriza objetivos en el mercado español
            y recibe alertas automáticas por correo cuando tus valores alcancen niveles clave.
        </p>
        <div class="sl-feat-grid">
            <div class="sl-feat-card">
                <div class="sl-feat-head">
                    <span class="sl-feat-icon" style="background:rgba(56,189,248,0.12);border:1px solid rgba(56,189,248,0.28);color:#38bdf8;">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>
                    </span>
                    <span class="sl-feat-title">Cartera Real y Simulada</span>
                </div>
                <div class="sl-feat-desc">
                    Evolución diaria del patrimonio, plusvalía latente y realizada, liquidez y control de comisiones.
                </div>
            </div>
            <div class="sl-feat-card">
                <div class="sl-feat-head">
                    <span class="sl-feat-icon" style="background:rgba(52,211,153,0.12);border:1px solid rgba(52,211,153,0.28);color:#34d399;">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/></svg>
                    </span>
                    <span class="sl-feat-title">Radar Watchlist</span>
                </div>
                <div class="sl-feat-desc">
                    Precios objetivo en caída o ruptura con cálculo exacto de distancia y seguimiento personalizado.
                </div>
            </div>
            <div class="sl-feat-card">
                <div class="sl-feat-head">
                    <span class="sl-feat-icon" style="background:rgba(251,191,36,0.12);border:1px solid rgba(251,191,36,0.28);color:#fbbf24;">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
                    </span>
                    <span class="sl-feat-title">Alertas por Correo</span>
                </div>
                <div class="sl-feat-desc">
                    Notificaciones automáticas cuando un objetivo se cumple o una posición supera tu umbral.
                </div>
            </div>
            <div class="sl-feat-card">
                <div class="sl-feat-head">
                    <span class="sl-feat-icon" style="background:rgba(167,139,250,0.12);border:1px solid rgba(167,139,250,0.28);color:#a78bfa;">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                    </span>
                    <span class="sl-feat-title">Ranking BME e IA</span>
                </div>
                <div class="sl-feat-desc">
                    Valoración fundamental calibrada, indicadores técnicos reales y asistente privado de análisis.
                </div>
            </div>
        </div>
        <div class="sl-metrics-bar">
            <div class="sl-metric-item">
                <span class="sl-metric-val">116</span>
                <span class="sl-metric-lbl">Cotizadas BME · IBEX 35</span>
            </div>
            <div class="sl-metric-item">
                <span class="sl-metric-val">24/7</span>
                <span class="sl-metric-lbl">Vigilancia automática</span>
            </div>
            <div class="sl-metric-item">
                <span class="sl-metric-val">100%</span>
                <span class="sl-metric-lbl">Espacio privado por cuenta</span>
            </div>
        </div>
        """
    )


def _render_recovery_card(recovery_token: str) -> None:
    st.markdown(
        '<div class="sl-card-title">Establecer nueva contraseña</div>'
        '<div class="sl-card-sub">Define una nueva contraseña segura para recuperar el acceso a tu cuenta.</div>',
        unsafe_allow_html=True,
    )
    with st.form("sl_form_new_password"):
        p1 = st.text_input("NUEVA CONTRASEÑA (MÍN. 8 CARACTERES)", type="password", key="rec_new_p1")
        p2 = st.text_input("REPETIR NUEVA CONTRASEÑA", type="password", key="rec_new_p2")
        sub = st.form_submit_button("Guardar nueva contraseña", type="primary", use_container_width=True)

    if sub:
        if p1 != p2:
            st.error("Las contraseñas no coinciden.")
        else:
            ok, msg = update_password_with_token(recovery_token, p1)
            if ok:
                st.session_state.pop("_sl_recovery_token", None)
                st.session_state["_auth_flash_success"] = msg
                st.rerun()
            else:
                st.error(msg)

    if st.button("Volver al inicio de sesión", key="rec_cancel_btn", use_container_width=True):
        st.session_state.pop("_sl_recovery_token", None)
        st.rerun()


def show_auth_gate() -> None:
    """Renderiza la portada y puerta de acceso obligatoria antes de cargar el terminal StockLab."""
    _procesar_callback_oauth()
    _inyectar_estilos_auth()

    col_left, _, col_right = st.columns([1.28, 0.10, 0.82], vertical_alignment="center")

    with col_left:
        _render_panel_marca()

    with col_right:
        with st.container(key="sl_auth_card"):
            flash_ok = st.session_state.pop("_auth_flash_success", None)
            if flash_ok:
                st.success(flash_ok)
            flash_err = st.session_state.pop("_auth_flash_error", None)
            if flash_err:
                st.error(flash_err)

            rec_tok = st.session_state.get("_sl_recovery_token")
            if rec_tok:
                _render_recovery_card(rec_tok)
            else:
                st.markdown(
                    '<div class="sl-card-title">Inicia sesión</div>'
                    '<div class="sl-card-sub">Accede a tu espacio privado con Google o con tu correo electrónico.</div>',
                    unsafe_allow_html=True,
                )

                google_url = get_google_oauth_url()
                if google_url:
                    _render_html(
                        f"""
                        <a class="sl-google-btn" href="{_html.escape(google_url)}" target="_blank" rel="noopener noreferrer">
                            <svg width="18" height="18" viewBox="0 0 24 24">
                                <path fill="#4285F4" d="M23.49 12.27c0-.79-.07-1.54-.2-2.27H12v4.3h6.45a5.52 5.52 0 0 1-2.39 3.62v3.01h3.87c2.26-2.08 3.56-5.15 3.56-8.66z"/>
                                <path fill="#34A853" d="M12 24c3.24 0 5.96-1.07 7.95-2.91l-3.87-3.01c-1.07.72-2.45 1.15-4.08 1.15-3.14 0-5.8-2.12-6.75-4.97H1.25v3.1A12 12 0 0 0 12 24z"/>
                                <path fill="#FBBC05" d="M5.25 14.26A7.21 7.21 0 0 1 4.88 12c0-.79.14-1.56.37-2.26v-3.1H1.25A12 12 0 0 0 0 12c0 1.94.46 3.77 1.25 5.36l4-3.1z"/>
                                <path fill="#EA4335" d="M12 4.77c1.76 0 3.34.61 4.59 1.8l3.44-3.44C17.95 1.19 15.23 0 12 0A12 12 0 0 0 1.25 6.64l4 3.1c.95-2.85 3.61-4.97 6.75-4.97z"/>
                            </svg>
                            Continuar con Google
                        </a>
                        <div class="sl-auth-or">o continúa con tu correo</div>
                        """
                    )

                tab_login, tab_signup, tab_magic = st.tabs(["Iniciar Sesión", "Crear Cuenta", "Enlace Mágico"])

                with tab_login:
                    with st.form("sl_form_login", clear_on_submit=False):
                        email_in = st.text_input("CORREO ELECTRÓNICO", placeholder="nombre@correo.com", key="auth_login_email")
                        pwd_in = st.text_input("CONTRASEÑA", type="password", placeholder="Introduce tu contraseña", key="auth_login_pwd")
                        submit_login = st.form_submit_button("Entrar en StockLab", type="primary", use_container_width=True)

                    if submit_login:
                        with st.spinner("Verificando credenciales..."):
                            ok, msg = sign_in_email(email_in, pwd_in)
                        if ok:
                            st.rerun()
                        else:
                            st.error(msg)

                    with st.expander("¿Problemas para acceder?", expanded=False):
                        email_help = st.text_input("CORREO ASOCIADO A TU CUENTA", placeholder="nombre@correo.com", key="auth_help_email")
                        c_h1, c_h2 = st.columns(2)
                        with c_h1:
                            if st.button("Restablecer clave", key="auth_rec_btn", use_container_width=True):
                                ok_r, msg_r = send_password_reset(email_help)
                                if ok_r:
                                    st.success(msg_r)
                                else:
                                    st.error(msg_r)
                        with c_h2:
                            if st.button("Reenviar confirmación", key="auth_resend_btn", use_container_width=True):
                                ok_c, msg_c = resend_confirmation_email(email_help)
                                if ok_c:
                                    st.success(msg_c)
                                else:
                                    st.error(msg_c)

                with tab_signup:
                    with st.form("sl_form_signup", clear_on_submit=False):
                        name_up = st.text_input("NOMBRE", placeholder="Tu nombre", key="auth_up_name")
                        email_up = st.text_input("CORREO ELECTRÓNICO", placeholder="nombre@correo.com", key="auth_up_email")
                        pwd_up1 = st.text_input("CONTRASEÑA (MÍN. 8 CARACTERES)", type="password", placeholder="Mínimo 8 caracteres", key="auth_up_pwd1")
                        pwd_up2 = st.text_input("REPETIR CONTRASEÑA", type="password", placeholder="Repite tu contraseña", key="auth_up_pwd2")
                        acepta_aviso = st.checkbox(
                            "Entiendo que StockLab es una herramienta informativa y no ofrece asesoramiento financiero.",
                            value=False,
                            key="auth_up_legal",
                        )
                        submit_up = st.form_submit_button("Crear cuenta privada", type="primary", use_container_width=True)

                    if submit_up:
                        if not acepta_aviso:
                            st.warning("Debes aceptar el aviso informativo para crear tu cuenta.")
                        elif pwd_up1 != pwd_up2:
                            st.error("Las contraseñas no coinciden.")
                        else:
                            with st.spinner("Creando tu cuenta privada..."):
                                ok_u, msg_u, req_conf = sign_up_email(email_up, pwd_up1, name_up)
                            if ok_u and not req_conf:
                                st.rerun()
                            elif ok_u:
                                st.success(msg_u)
                            else:
                                st.error(msg_u)

                with tab_magic:
                    st.caption("Te enviaremos un enlace de acceso directo a tu correo para entrar en un clic, sin contraseña.")
                    with st.form("sl_form_magic", clear_on_submit=False):
                        email_mag = st.text_input("CORREO ELECTRÓNICO", placeholder="nombre@correo.com", key="auth_magic_email")
                        submit_mag = st.form_submit_button("Enviar enlace de acceso", type="primary", use_container_width=True)

                    if submit_mag:
                        with st.spinner("Enviando enlace de acceso..."):
                            ok_m, msg_m = send_magic_link(email_mag)
                        if ok_m:
                            st.success(msg_m)
                        else:
                            st.error(msg_m)

    _render_html(
        """
        <div class="sl-auth-footer">
            <span>StockLab · Plataforma independiente de análisis y seguimiento bursátil</span>
            <span>Contenido estrictamente informativo; no constituye asesoramiento financiero ni recomendación de inversión.</span>
        </div>
        """
    )
