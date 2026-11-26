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


def _inyectar_estilos_auth() -> None:
    st.markdown(
        """
        <style>
            /* Quitar padding superior innecesario del TopBar en pantalla de login */
            section[data-testid="stMain"] [data-testid="stMainBlockContainer"] {
                padding-top: clamp(1.8rem, 4.5vh, 3.6rem) !important;
                padding-bottom: 2.5rem !important;
            }

            /* ── Panel Izquierdo: Identidad y Propuesta de Valor ───────── */
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
                box-shadow: 0 0 22px -4px rgba(56, 189, 248, 0.48), inset 0 1px 0 rgba(255,255,255,0.18);
                flex-shrink: 0;
            }
            .sl-brand-name {
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
                font-size: 1.75rem;
                font-weight: 800;
                letter-spacing: -0.045em;
                line-height: 1.05;
                background: linear-gradient(180deg, #ffffff 20%, #cbd5e1 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            .sl-brand-sub {
                font-family: 'Inter', sans-serif;
                font-size: 0.62rem;
                font-weight: 700;
                letter-spacing: 0.22em;
                color: #7c90ad;
                text-transform: uppercase;
                margin-top: 3px;
            }
            .sl-hero-headline {
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
                font-size: clamp(1.75rem, 2.7vw, 2.35rem);
                font-weight: 800;
                line-height: 1.15;
                letter-spacing: -0.038em;
                color: #f8fafc;
                margin: 0 0 12px 0;
            }
            .sl-hero-headline span {
                background: linear-gradient(95deg, #38bdf8, #818cf8);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            .sl-hero-copy {
                font-family: 'Inter', sans-serif;
                font-size: 0.92rem;
                line-height: 1.6;
                color: #94a3b8;
                margin: 0 0 22px 0;
                max-width: 510px;
            }
            .sl-feat-grid {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 12px;
                margin-bottom: 20px;
            }
            @media (max-width: 640px) {
                .sl-feat-grid { grid-template-columns: 1fr; }
            }
            .sl-feat-card {
                background: linear-gradient(155deg, rgba(22, 34, 54, 0.72), rgba(11, 17, 30, 0.92));
                border: 1px solid rgba(148, 163, 184, 0.13);
                border-radius: 13px;
                padding: 14px 15px;
                position: relative;
                overflow: hidden;
            }
            .sl-feat-card::before {
                content: "";
                position: absolute;
                top: 0;
                left: 14%;
                right: 14%;
                height: 1px;
                background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.42), transparent);
            }
            .sl-feat-title {
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
                font-size: 0.84rem;
                font-weight: 700;
                color: #f1f5f9;
                margin-bottom: 4px;
                display: flex;
                align-items: center;
                gap: 7px;
            }
            .sl-feat-dot {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                flex-shrink: 0;
            }
            .sl-feat-desc {
                font-family: 'Inter', sans-serif;
                font-size: 0.76rem;
                color: #94a3b8;
                line-height: 1.45;
            }
            .sl-trust-pills {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
            }
            .sl-trust-pill {
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.69rem;
                font-weight: 600;
                color: #94a3b8;
                background: rgba(15, 23, 42, 0.75);
                border: 1px solid rgba(148, 163, 184, 0.16);
                border-radius: 999px;
                padding: 4px 11px;
            }

            /* ── Panel Derecho: Tarjeta Glassmorphic de Acceso ─────────── */
            .st-key-sl_auth_card {
                background: linear-gradient(160deg, rgba(20, 31, 50, 0.90) 0%, rgba(10, 16, 28, 0.98) 100%) !important;
                border: 1px solid rgba(56, 189, 248, 0.22) !important;
                border-radius: 18px !important;
                padding: 26px 26px 22px 26px !important;
                box-shadow: 0 18px 48px -12px rgba(0, 0, 0, 0.62), 0 0 36px -14px rgba(56, 189, 248, 0.22), inset 0 1px 0 rgba(255, 255, 255, 0.08) !important;
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
                font-size: 1.15rem;
                font-weight: 700;
                color: #f8fafc;
                letter-spacing: -0.02em;
                margin-bottom: 4px;
            }
            .sl-card-sub {
                font-family: 'Inter', sans-serif;
                font-size: 0.80rem;
                color: #94a3b8;
                margin-bottom: 16px;
            }

            /* Botón Google elegante */
            .sl-google-btn {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                width: 100%;
                height: 44px;
                padding: 0 16px;
                border-radius: 11px;
                background: rgba(248, 250, 252, 0.96);
                color: #0f172a !important;
                text-decoration: none !important;
                font-family: 'Inter', sans-serif;
                font-weight: 700;
                font-size: 0.86rem;
                border: 1px solid rgba(255, 255, 255, 0.32);
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.30);
                transition: transform 160ms ease, box-shadow 160ms ease, background 160ms ease;
                margin-bottom: 12px;
            }
            .sl-google-btn:hover {
                transform: translateY(-1px);
                background: #ffffff;
                box-shadow: 0 6px 24px rgba(56, 189, 248, 0.28);
            }
            .sl-auth-or {
                display: flex;
                align-items: center;
                gap: 10px;
                color: #64748b;
                font-size: 0.69rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.09em;
                margin: 10px 0 12px 0;
            }
            .sl-auth-or::before, .sl-auth-or::after {
                content: "";
                flex: 1;
                height: 1px;
                background: rgba(148, 163, 184, 0.16);
            }

            /* Pestañas encapsuladas dentro de la tarjeta */
            .st-key-sl_auth_card .stTabs [data-baseweb="tab-list"] {
                background: rgba(8, 12, 21, 0.85) !important;
                border: 1px solid rgba(148, 163, 184, 0.14) !important;
                border-radius: 11px !important;
                padding: 4px !important;
                gap: 4px !important;
            }
            .st-key-sl_auth_card .stTabs [data-baseweb="tab"] {
                flex: 1 !important;
                justify-content: center !important;
                border-radius: 8px !important;
                font-size: 0.80rem !important;
                font-weight: 600 !important;
                padding: 7px 10px !important;
                color: #94a3b8 !important;
            }
            .st-key-sl_auth_card .stTabs [data-baseweb="tab"][aria-selected="true"] {
                background: linear-gradient(135deg, rgba(56, 189, 248, 0.20), rgba(99, 102, 241, 0.14)) !important;
                border: 1px solid rgba(56, 189, 248, 0.42) !important;
                color: #e0f6ff !important;
            }
            .st-key-sl_auth_card .stTabs [data-baseweb="tab-highlight"],
            .st-key-sl_auth_card .stTabs [data-baseweb="tab-border"] {
                display: none !important;
            }

            /* Formulario e inputs integrados (sin doble caja pesada) */
            .st-key-sl_auth_card div[data-testid="stForm"] {
                background: transparent !important;
                border: none !important;
                padding: 4px 0 0 0 !important;
            }
            .st-key-sl_auth_card div[data-testid="stTextInput"] label p {
                font-size: 0.70rem !important;
                font-weight: 700 !important;
                letter-spacing: 0.07em !important;
                color: #94a3b8 !important;
            }
            .st-key-sl_auth_card div[data-testid="stTextInput"] input {
                background: #0b111e !important;
                border: 1px solid rgba(148, 163, 184, 0.18) !important;
                border-radius: 10px !important;
                color: #f8fafc !important;
                height: 42px !important;
            }
            .st-key-sl_auth_card div[data-testid="stTextInput"] input:focus {
                border-color: rgba(56, 189, 248, 0.58) !important;
                box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.14) !important;
            }
            .st-key-sl_auth_card div[data-testid="stCheckbox"] label p {
                font-size: 0.77rem !important;
                color: #94a3b8 !important;
                text-transform: none !important;
                line-height: 1.4 !important;
            }

            /* Botón primario Obsidian Neon armonizado (en vez de azul plano chillón) */
            .st-key-sl_auth_card div[data-testid="stFormSubmitButton"] button,
            .st-key-sl_auth_card button[kind="primary"] {
                background: linear-gradient(135deg, rgba(56, 189, 248, 0.28), rgba(99, 102, 241, 0.24)) !important;
                border: 1px solid rgba(56, 189, 248, 0.58) !important;
                color: #e0f6ff !important;
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
                font-weight: 700 !important;
                font-size: 0.88rem !important;
                height: 43px !important;
                border-radius: 10px !important;
                box-shadow: 0 0 22px -6px rgba(56, 189, 248, 0.50), inset 0 1px 0 rgba(255, 255, 255, 0.16) !important;
                transition: all 180ms ease !important;
            }
            .st-key-sl_auth_card div[data-testid="stFormSubmitButton"] button:hover,
            .st-key-sl_auth_card button[kind="primary"]:hover {
                background: linear-gradient(135deg, rgba(56, 189, 248, 0.40), rgba(99, 102, 241, 0.34)) !important;
                border-color: rgba(125, 211, 252, 0.85) !important;
                color: #ffffff !important;
                transform: translateY(-1px) !important;
            }

            .sl-auth-footnote {
                text-align: center;
                font-family: 'Inter', sans-serif;
                font-size: 0.71rem;
                color: #64748b;
                margin-top: 1.6rem;
                line-height: 1.5;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_panel_marca() -> None:
    st.markdown(
        """
        <div class="sl-brand-lockup">
            <div class="sl-brand-mark"></div>
            <div>
                <div class="sl-brand-name">StockLab</div>
                <div class="sl-brand-sub">INVESTMENT TERMINAL</div>
            </div>
        </div>
        <h1 class="sl-hero-headline">
            Tu espacio privado para <span>invertir con método</span>
        </h1>
        <p class="sl-hero-copy">
            Cada usuario dispone de su propio terminal aislado: cartera real y simulada,
            watchlist con niveles tácticos, alertas automáticas por correo y análisis
            fundamental del mercado español.
        </p>
        <div class="sl-feat-grid">
            <div class="sl-feat-card">
                <div class="sl-feat-title">
                    <span class="sl-feat-dot" style="background:#38bdf8;box-shadow:0 0 8px #38bdf8;"></span>
                    Cartera Real y Simulada
                </div>
                <div class="sl-feat-desc">
                    Curva diaria de patrimonio, plusvalía latente y realizada, liquidez y control de comisiones.
                </div>
            </div>
            <div class="sl-feat-card">
                <div class="sl-feat-title">
                    <span class="sl-feat-dot" style="background:#34d399;box-shadow:0 0 8px #34d399;"></span>
                    Radar Watchlist
                </div>
                <div class="sl-feat-desc">
                    Precios objetivo en caída o ruptura con cálculo exacto de distancia y señales al instante.
                </div>
            </div>
            <div class="sl-feat-card">
                <div class="sl-feat-title">
                    <span class="sl-feat-dot" style="background:#fbbf24;box-shadow:0 0 8px #fbbf24;"></span>
                    Alertas 24/7 por Correo
                </div>
                <div class="sl-feat-desc">
                    Avisos automáticos cuando tus objetivos se cumplen o una posición varía más de tu umbral.
                </div>
            </div>
            <div class="sl-feat-card">
                <div class="sl-feat-title">
                    <span class="sl-feat-dot" style="background:#a78bfa;box-shadow:0 0 8px #a78bfa;"></span>
                    Ranking BME e IA
                </div>
                <div class="sl-feat-desc">
                    Scoring fundamental calibrado, radar técnico y asistente inteligente sobre tus activos.
                </div>
            </div>
        </div>
        <div class="sl-trust-pills">
            <span class="sl-trust-pill">Aislamiento privado RLS</span>
            <span class="sl-trust-pill">Cifrado HTTPS</span>
            <span class="sl-trust-pill">116 cotizadas BME · IBEX 35</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_recovery_card(recovery_token: str) -> None:
    st.markdown(
        '<div class="sl-card-title">Establecer nueva contraseña</div>'
        '<div class="sl-card-sub">Introduce tu nueva clave de acceso para recuperar tu cuenta.</div>',
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

    _, col_left, _, col_right, _ = st.columns([0.16, 1.12, 0.14, 0.96, 0.16], vertical_alignment="center")

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
                    '<div class="sl-card-title">Accede a tu terminal</div>'
                    '<div class="sl-card-sub">Inicia sesión con Google, contraseña o enlace mágico.</div>',
                    unsafe_allow_html=True,
                )

                google_url = get_google_oauth_url()
                if google_url:
                    st.markdown(
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
                        <div class="sl-auth-or">o accede con tu correo</div>
                        """,
                        unsafe_allow_html=True,
                    )

                tab_login, tab_signup, tab_magic = st.tabs(["Iniciar Sesión", "Crear Cuenta", "Enlace Mágico"])

                with tab_login:
                    with st.form("sl_form_login", clear_on_submit=False):
                        email_in = st.text_input("CORREO ELECTRÓNICO", placeholder="tu@correo.com", key="auth_login_email")
                        pwd_in = st.text_input("CONTRASEÑA", type="password", placeholder="••••••••", key="auth_login_pwd")
                        submit_login = st.form_submit_button("Entrar en StockLab", type="primary", use_container_width=True)

                    if submit_login:
                        with st.spinner("Verificando credenciales..."):
                            ok, msg = sign_in_email(email_in, pwd_in)
                        if ok:
                            st.rerun()
                        else:
                            st.error(msg)

                    with st.expander("¿Problemas para entrar? (Recuperar clave / Reenviar confirmación)", expanded=False):
                        email_help = st.text_input("TU CORREO ELECTRÓNICO", placeholder="tu@correo.com", key="auth_help_email")
                        c_h1, c_h2 = st.columns(2)
                        with c_h1:
                            if st.button("Restablecer clave", key="auth_rec_btn", use_container_width=True):
                                ok_r, msg_r = send_password_reset(email_help)
                                if ok_r:
                                    st.success(msg_r)
                                else:
                                    st.error(msg_r)
                        with c_h2:
                            if st.button("Reenviar verificación", key="auth_resend_btn", use_container_width=True):
                                ok_c, msg_c = resend_confirmation_email(email_help)
                                if ok_c:
                                    st.success(msg_c)
                                else:
                                    st.error(msg_c)

                with tab_signup:
                    with st.form("sl_form_signup", clear_on_submit=False):
                        name_up = st.text_input("NOMBRE", placeholder="Tu nombre", key="auth_up_name")
                        email_up = st.text_input("CORREO ELECTRÓNICO", placeholder="tu@correo.com", key="auth_up_email")
                        pwd_up1 = st.text_input("CONTRASEÑA (MÍN. 8 CARACTERES)", type="password", key="auth_up_pwd1")
                        pwd_up2 = st.text_input("REPETIR CONTRASEÑA", type="password", key="auth_up_pwd2")
                        acepta_aviso = st.checkbox(
                            "Entiendo que StockLab es una herramienta informativa y no ofrece asesoramiento financiero.",
                            value=False,
                            key="auth_up_legal",
                        )
                        submit_up = st.form_submit_button("Crear mi cuenta privada", type="primary", use_container_width=True)

                    if submit_up:
                        if not acepta_aviso:
                            st.warning("Debes aceptar el aviso informativo para crear tu cuenta.")
                        elif pwd_up1 != pwd_up2:
                            st.error("Las contraseñas no coinciden.")
                        else:
                            with st.spinner("Creando cuenta privada..."):
                                ok_u, msg_u, req_conf = sign_up_email(email_up, pwd_up1, name_up)
                            if ok_u and not req_conf:
                                st.rerun()
                            elif ok_u:
                                st.success(msg_u)
                            else:
                                st.error(msg_u)

                with tab_magic:
                    st.caption("Recibe un enlace de acceso directo en tu correo sin necesidad de recordar contraseña.")
                    with st.form("sl_form_magic", clear_on_submit=False):
                        email_mag = st.text_input("CORREO ELECTRÓNICO", placeholder="tu@correo.com", key="auth_magic_email")
                        submit_mag = st.form_submit_button("Enviar enlace mágico", type="primary", use_container_width=True)

                    if submit_mag:
                        with st.spinner("Enviando enlace de acceso..."):
                            ok_m, msg_m = send_magic_link(email_mag)
                        if ok_m:
                            st.success(msg_m)
                        else:
                            st.error(msg_m)

    st.markdown(
        """
        <div class="sl-auth-footnote">
            StockLab · Plataforma privada de seguimiento y análisis bursátil · Datos aislados por cuenta (PostgreSQL RLS)<br>
            Aviso informativo: los datos, métricas y señales no constituyen asesoramiento financiero ni recomendación de inversión.
        </div>
        """,
        unsafe_allow_html=True,
    )
