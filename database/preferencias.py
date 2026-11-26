"""
database/preferencias.py — Preferencias de Alertas por Correo por Usuario

Gestiona las reglas personalizadas de avisos por email:
  - Activación general de correos y dirección de destino
  - Watchlist: objetivo alcanzado y aproximación (±X%)
  - Cartera Real y/o Simulada: umbrales de ganancia (+X%), caída (-Y%) y variación diaria (±Z%)
  - Resumen diario al cierre de mercado

En modo nube multiusuario (Supabase) persiste en `public.preferencias_alertas` con RLS.
En modo local guarda en `st.session_state` (sin tocar `stocklab.db` durante tests).
"""

from __future__ import annotations

from typing import Any
from services.auth_service import (
    get_current_user,
    is_cloud_user_authenticated,
    supabase_rest,
)

DEFAULT_PREFERENCIAS_ALERTAS: dict[str, Any] = {
    "email_activo": True,
    "email_destino": "",
    "avisar_wl_alcanzado": True,
    "avisar_wl_cerca": True,
    "umbral_wl_cerca_pct": 3.0,
    "avisar_cartera_real": True,
    "avisar_cartera_simulada": False,
    "umbral_ganancia_pct": 15.0,
    "umbral_perdida_pct": -8.0,
    "umbral_intradia_pct": 4.0,
    "resumen_diario": False,
}


def _normalizar_prefs(raw: dict | None, email_default: str = "") -> dict[str, Any]:
    out = dict(DEFAULT_PREFERENCIAS_ALERTAS)
    if email_default:
        out["email_destino"] = email_default
    if not isinstance(raw, dict):
        return out

    for k_bool in (
        "email_activo",
        "avisar_wl_alcanzado",
        "avisar_wl_cerca",
        "avisar_cartera_real",
        "avisar_cartera_simulada",
        "resumen_diario",
    ):
        if k_bool in raw and raw[k_bool] is not None:
            out[k_bool] = bool(raw[k_bool])

    if raw.get("email_destino"):
        out["email_destino"] = str(raw["email_destino"]).strip().lower()

    def _clamp(val, lo, hi, fallback):
        try:
            f = float(val)
            return max(lo, min(hi, f))
        except (TypeError, ValueError):
            return fallback

    out["umbral_wl_cerca_pct"] = _clamp(raw.get("umbral_wl_cerca_pct"), 0.5, 25.0, 3.0)
    out["umbral_ganancia_pct"] = _clamp(raw.get("umbral_ganancia_pct"), 1.0, 500.0, 15.0)
    out["umbral_perdida_pct"] = _clamp(raw.get("umbral_perdida_pct"), -95.0, -1.0, -8.0)
    out["umbral_intradia_pct"] = _clamp(raw.get("umbral_intradia_pct"), 1.0, 50.0, 4.0)
    return out


def obtener_preferencias_alertas() -> dict[str, Any]:
    """Devuelve las preferencias de alertas por correo del usuario actual."""
    if is_cloud_user_authenticated():
        user = get_current_user()
        uid = user["id"]
        email_u = user.get("email", "")
        ok, data = supabase_rest(
            "GET",
            "preferencias_alertas",
            params={"user_id": f"eq.{uid}", "limit": 1},
        )
        if ok and isinstance(data, list) and data:
            return _normalizar_prefs(data[0], email_default=email_u)
        return _normalizar_prefs(None, email_default=email_u)

    try:
        import streamlit as st
        saved = st.session_state.get("_local_prefs_alertas")
        return _normalizar_prefs(saved)
    except Exception:
        return dict(DEFAULT_PREFERENCIAS_ALERTAS)


def guardar_preferencias_alertas(prefs: dict[str, Any]) -> tuple[bool, str]:
    """Guarda las preferencias de alertas del usuario actual."""
    if is_cloud_user_authenticated():
        user = get_current_user()
        uid = user["id"]
        email_u = user.get("email", "")
        limpias = _normalizar_prefs(prefs, email_default=email_u)
        payload = {"user_id": uid, **limpias}
        ok, err = supabase_rest(
            "POST",
            "preferencias_alertas",
            params={"on_conflict": "user_id"},
            payload=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        if ok:
            return True, "Preferencias de alertas guardadas en tu cuenta."
        return False, f"No se pudieron guardar las preferencias: {err}"

    try:
        import streamlit as st
        st.session_state["_local_prefs_alertas"] = _normalizar_prefs(prefs)
        return True, "Preferencias guardadas en sesión local (se persistirán en nube al activar Supabase)."
    except Exception as e:
        return False, f"No se pudieron guardar las preferencias: {e}"
