"""
views/auth.py — Pantalla de Acceso Multiusuario de StockLab (Obsidian Neon)

Soporta:
  - Inicio de sesión con Google (OAuth gestionado por Supabase)
  - Inicio de sesión y registro con correo electrónico y contraseña
  - Recuperación de contraseña por correo
  - Verificación estricta de tokens OAuth contra Supabase `/auth/v1/user`
"""

from __future__ import annotations

import html as _html
import streamlit as st
from services.auth_service import (
    complete_oauth_from_pkce_code,
    complete_oauth_from_tokens,
    get_google_oauth_url,
    send_password_reset,
    sign_in_email,
    sign_up_email,
)


def _procesar_callback_oauth() -> None:
    """
    Procesa la vuelta desde Google OAuth:
    1) Flujo principal PKCE S256 (`?code=...&oauth_s=...`): intercambio seguro servidor-a-servidor.
    2) Fallback implícito (`?auth_at=...` / `#access_token=...`) para entornos locales.
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
        st.session_state["_auth_flash_error"] = f"Error de acceso con Google: {oauth_err}"
        return

    # 2. Fallback implícito
    at = qp.get("auth_at")
    rt = qp.get("auth_rt", "")
    exp_raw = qp.get("auth_exp", "3600")

    if at:
        try:
            exp_i = int(exp_raw)
        except Exception:
            exp_i = 3600
        # Limpiar query params inmediatamente para no dejar tokens en la barra de direcciones
        try:
            for k in ("auth_at", "auth_rt", "auth_exp"):
                if k in st.query_params:
                    del st.query_params[k]
        except Exception:
            pass

        ok, msg = complete_oauth_from_tokens(str(at), str(rt), exp_i)
        if ok:
            st.rerun()
        else:
            st.session_state["_auth_flash_error"] = msg
        return

    # Puente ligero solo si hay #access_token=... en la URL tras redirección OAuth de Google
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
                    if (at) {
                        const url = new URL(win.location.href);
                        url.hash = "";
                        url.searchParams.set("auth_at", at);
                        if (rt) url.searchParams.set("auth_rt", rt);
                        url.searchParams.set("auth_exp", exp);
                        win.location.replace(url.toString());
                    }
                }
            } catch (e) {}
        })();
        </script>
        """,
        height=0,
    )


def show_auth_gate() -> None:
    """Renderiza la puerta de acceso obligatoria antes de cargar el terminal StockLab."""
    _procesar_callback_oauth()

    st.markdown(
        """
        <style>
            .sl-auth-hero {
                text-align: center;
                margin: 1.5rem auto 1.4rem auto;
                max-width: 480px;
            }
            .sl-auth-brand {
                font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
                font-size: 2.15rem;
                font-weight: 800;
                letter-spacing: -0.04em;
                background: linear-gradient(180deg, #ffffff 20%, #7dd3fc 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                margin-bottom: 4px;
            }
            .sl-auth-sub {
                font-family: 'Inter', sans-serif;
                font-size: 0.88rem;
                color: #94a3b8;
                line-height: 1.5;
            }
            .sl-google-btn {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                width: 100%;
                padding: 11px 16px;
                border-radius: 10px;
                background: rgba(248, 250, 252, 0.96);
                color: #0f172a !important;
                text-decoration: none !important;
                font-family: 'Inter', sans-serif;
                font-weight: 700;
                font-size: 0.88rem;
                border: 1px solid rgba(148, 163, 184, 0.28);
                box-shadow: 0 4px 16px rgba(0, 0, 0, 0.28);
                transition: transform 160ms ease, box-shadow 160ms ease, background 160ms ease;
                margin-bottom: 14px;
            }
            .sl-google-btn:hover {
                transform: translateY(-1px);
                background: #ffffff;
                box-shadow: 0 6px 22px rgba(56, 189, 248, 0.24);
            }
            .sl-auth-or {
                display: flex;
                align-items: center;
                gap: 10px;
                color: #64748b;
                font-size: 0.72rem;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin: 12px 0 14px 0;
            }
            .sl-auth-or::before, .sl-auth-or::after {
                content: "";
                flex: 1;
                height: 1px;
                background: rgba(148, 163, 184, 0.16);
            }
            .sl-auth-trust {
                text-align: center;
                font-family: 'Inter', sans-serif;
                font-size: 0.74rem;
                color: #64748b;
                margin-top: 1.1rem;
                line-height: 1.5;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, col_center, _ = st.columns([1.2, 1.8, 1.2])
    with col_center:
        st.markdown(
            """
            <div class="sl-auth-hero">
                <div class="sl-auth-brand">StockLab</div>
                <div class="sl-auth-sub">Tu terminal privado de inversión, watchlist inteligente y alertas automáticas</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        flash_err = st.session_state.pop("_auth_flash_error", None)
        if flash_err:
            st.error(flash_err)

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
                <div class="sl-auth-or">o con tu correo electrónico</div>
                """,
                unsafe_allow_html=True,
            )

        tab_login, tab_signup = st.tabs(["Iniciar Sesión", "Crear Cuenta"])

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

            with st.expander("¿Olvidaste tu contraseña?", expanded=False):
                email_rec = st.text_input("CORREO DE RECUPERACIÓN", placeholder="tu@correo.com", key="auth_rec_email")
                if st.button("Enviar enlace de restablecimiento", key="auth_rec_btn", use_container_width=True):
                    ok_r, msg_r = send_password_reset(email_rec)
                    if ok_r:
                        st.success(msg_r)
                    else:
                        st.error(msg_r)

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

        st.markdown(
            """
            <div class="sl-auth-trust">
                Aislamiento estricto por usuario (PostgreSQL RLS) · Tus carteras, watchlist y alertas son 100% privadas.<br>
                Aviso informativo: StockLab no constituye asesoramiento financiero ni recomendación de inversión.
            </div>
            """,
            unsafe_allow_html=True,
        )
