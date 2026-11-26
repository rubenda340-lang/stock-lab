"""
views/cuenta.py — Modal de Mi Cuenta, Preferencias de Alertas por Correo y Privacidad (RGPD)
"""

from __future__ import annotations

import html as _html
import streamlit as st
from database import guardar_preferencias_alertas, obtener_preferencias_alertas
from services.auth_service import (
    delete_all_user_data,
    get_current_user,
    is_cloud_auth_enabled,
    is_cloud_user_authenticated,
    send_password_reset,
    sign_out,
)


@st.dialog("Mi Cuenta y Alertas", width="large")
def mostrar_modal_cuenta() -> None:
    """Diálogo centrado para gestionar perfil, avisos por correo y privacidad RGPD."""
    en_nube = is_cloud_auth_enabled() and is_cloud_user_authenticated()
    user = get_current_user() if en_nube else None

    if en_nube and user:
        nombre_u = _html.escape(str(user.get("name") or "Usuario"))
        email_u = _html.escape(str(user.get("email") or ""))
        prov_u = _html.escape(str(user.get("provider") or "email").capitalize())
        badge_modo = (
            '<span style="padding:3px 9px;border-radius:999px;font-size:0.70rem;font-weight:700;'
            'background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.32);color:#34d399;">'
            f'Nube · Privado ({prov_u})</span>'
        )
    else:
        nombre_u = "Sesión Local"
        email_u = "Base de datos local SQLite (stocklab.db)"
        badge_modo = (
            '<span style="padding:3px 9px;border-radius:999px;font-size:0.70rem;font-weight:700;'
            'background:rgba(56,189,248,0.12);border:1px solid rgba(56,189,248,0.32);color:#38bdf8;">'
            'Modo Local</span>'
        )

    st.markdown(
        f"""
        <div style="padding:14px 16px;border-radius:12px;background:rgba(15,23,42,0.75);
                    border:1px solid rgba(148,163,184,0.16);display:flex;justify-content:space-between;
                    align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px;">
            <div>
                <div style="font-family:'Plus Jakarta Sans',sans-serif;font-weight:700;font-size:1.0rem;color:#f8fafc;">
                    {nombre_u}
                </div>
                <div style="font-family:'Inter',sans-serif;font-size:0.80rem;color:#94a3b8;margin-top:2px;">
                    {email_u}
                </div>
            </div>
            {badge_modo}
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Configuración de Alertas Automáticas por Correo ──────────────────
    st.markdown("##### Alertas Automáticas por Correo")
    st.caption(
        "Configura cuándo quieres que StockLab te envíe avisos automáticos por correo electrónico "
        "sobre tus observaciones de Watchlist y posiciones en cartera."
    )

    prefs = obtener_preferencias_alertas()
    with st.form("sl_form_prefs_alertas"):
        email_activo = st.toggle(
            "Activar envío de alertas por correo electrónico",
            value=bool(prefs.get("email_activo", True)),
        )
        email_dest = st.text_input(
            "CORREO DE RECEPCIÓN",
            value=str(prefs.get("email_destino") or (user.get("email") if user else "")),
            placeholder="tu@correo.com",
            disabled=not email_activo,
        )

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Radar Watchlist**")
            wl_alc = st.checkbox(
                "Avisar cuando un activo alcance el precio objetivo",
                value=bool(prefs.get("avisar_wl_alcanzado", True)),
                disabled=not email_activo,
            )
            wl_cerca = st.checkbox(
                "Avisar cuando se acerque al precio objetivo",
                value=bool(prefs.get("avisar_wl_cerca", True)),
                disabled=not email_activo,
            )
            umbral_cerca = st.number_input(
                "MARGEN DE CERCANÍA (%)",
                min_value=0.5,
                max_value=25.0,
                step=0.5,
                value=float(prefs.get("umbral_wl_cerca_pct", 3.0)),
                disabled=not (email_activo and wl_cerca),
            )
            resumen_diario = st.checkbox(
                "Enviar resumen diario al cierre BME (18:35)",
                value=bool(prefs.get("resumen_diario", False)),
                disabled=not email_activo,
            )

        with c2:
            st.markdown("**Seguimiento de Cartera**")
            av_real = st.checkbox(
                "Vigilar posiciones de Cartera Real",
                value=bool(prefs.get("avisar_cartera_real", True)),
                disabled=not email_activo,
            )
            av_sim = st.checkbox(
                "Vigilar posiciones de Cartera Simulada",
                value=bool(prefs.get("avisar_cartera_simulada", False)),
                disabled=not email_activo,
            )
            u_gan = st.number_input(
                "AVISAR SUBIDA ACUMULADA >= (%)",
                min_value=1.0,
                max_value=500.0,
                step=1.0,
                value=float(prefs.get("umbral_ganancia_pct", 15.0)),
                disabled=not (email_activo and (av_real or av_sim)),
            )
            u_per = st.number_input(
                "AVISAR CAÍDA ACUMULADA <= (%)",
                min_value=-95.0,
                max_value=-1.0,
                step=1.0,
                value=float(prefs.get("umbral_perdida_pct", -8.0)),
                disabled=not (email_activo and (av_real or av_sim)),
            )
            u_intra = st.number_input(
                "AVISAR MOVIMIENTO HOY >= (±%)",
                min_value=1.0,
                max_value=50.0,
                step=0.5,
                value=float(prefs.get("umbral_intradia_pct", 4.0)),
                disabled=not (email_activo and (av_real or av_sim)),
            )

        guardar_btn = st.form_submit_button("Guardar Preferencias", type="primary", use_container_width=True)

    if guardar_btn:
        ok_p, msg_p = guardar_preferencias_alertas({
            "email_activo": email_activo,
            "email_destino": email_dest,
            "avisar_wl_alcanzado": wl_alc,
            "avisar_wl_cerca": wl_cerca,
            "umbral_wl_cerca_pct": umbral_cerca,
            "avisar_cartera_real": av_real,
            "avisar_cartera_simulada": av_sim,
            "umbral_ganancia_pct": u_gan,
            "umbral_perdida_pct": u_per,
            "umbral_intradia_pct": u_intra,
            "resumen_diario": resumen_diario,
        })
        if ok_p:
            st.success(msg_p)
        else:
            st.error(msg_p)

    # ── Acciones de cuenta y RGPD (solo cuando la sesión nube está activa) ─
    if en_nube and user:
        st.divider()
        col_a1, col_a2 = st.columns(2)
        with col_a1:
            if st.button("Enviar correo para cambiar contraseña", use_container_width=True):
                ok_r, msg_r = send_password_reset(user.get("email", ""))
                if ok_r:
                    st.success(msg_r)
                else:
                    st.error(msg_r)
        with col_a2:
            if st.button("Cerrar Sesión", type="primary", use_container_width=True):
                sign_out()
                st.rerun()

        with st.expander("Zona de Privacidad (RGPD) — Borrar todos mis datos", expanded=False):
            st.warning(
                "Esta acción eliminará permanentemente todas tus operaciones de Cartera (Real y Simulada), "
                "tu Watchlist, tus preferencias de alertas y tu historial de consumo."
            )
            conf_del = st.text_input(
                "ESCRIBE 'BORRAR' PARA CONFIRMAR",
                key="cuenta_confirm_delete_txt",
            )
            if st.button("Eliminar todos mis datos permanentemente", type="secondary", use_container_width=True):
                if conf_del.strip().upper() == "BORRAR":
                    ok_d, msg_d = delete_all_user_data()
                    if ok_d:
                        st.success(msg_d)
                        sign_out()
                        st.rerun()
                    else:
                        st.error(msg_d)
                else:
                    st.error("Escribe exactamente BORRAR para confirmar.")
    else:
        st.info(
            "Cuando añadas `SUPABASE_URL` y `SUPABASE_ANON_KEY` en tus variables de entorno o Streamlit Secrets, "
            "se activará automáticamente el inicio de sesión con Google/Email y el aislamiento multiusuario."
        )
