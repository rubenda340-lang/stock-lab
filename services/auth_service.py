"""
services/auth_service.py — Autenticación Multiusuario y Cliente REST Supabase (RLS)

Provee autenticación real (Google OAuth + Email/Contraseña + Recuperación de clave)
y acceso REST autenticado por JWT contra Supabase PostgREST, aplicando Row-Level
Security (RLS) a nivel de motor PostgreSQL (`auth.uid() = user_id`).

Modo dual sin romper desarrollo local ni los tests congelados:
  - Si `SUPABASE_URL` y `SUPABASE_ANON_KEY` están configurados (entorno o st.secrets),
    activa el modo nube multiusuario con login obligatorio.
  - Si NO están configurados, StockLab opera en modo local SQLite sin cambios.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

_SESSION_USER_KEY = "_sl_auth_user"
_HTTP_TIMEOUT = 12

_ERR_MAP_ES = {
    "invalid login credentials": "Correo electrónico o contraseña incorrectos.",
    "email not confirmed": "Debes confirmar tu correo electrónico desde el enlace recibido antes de entrar.",
    "user already registered": "Ya existe una cuenta registrada con este correo electrónico.",
    "password should be at least": "La contraseña debe tener al menos 8 caracteres.",
    "unable to validate email address": "El formato del correo electrónico no es válido.",
    "email rate limit exceeded": "Has superado el límite temporal de envíos de correo. Espera unos minutos.",
    "signup is disabled": "El registro de nuevas cuentas está desactivado temporalmente.",
}


def _leer_config(clave: str, default: str = "") -> str:
    """Lee una variable de entorno o de st.secrets sin lanzar error si no existe."""
    val = os.getenv(clave)
    if val and val.strip():
        return val.strip()
    try:
        import streamlit as st
        if clave in st.secrets:
            s_val = str(st.secrets[clave]).strip()
            if s_val:
                return s_val
    except Exception:
        pass
    return default


def get_supabase_url() -> str:
    return _leer_config("SUPABASE_URL").rstrip("/")


def get_supabase_anon_key() -> str:
    return _leer_config("SUPABASE_ANON_KEY")


def get_app_public_url() -> str:
    return _leer_config("APP_PUBLIC_URL", "http://localhost:8501").rstrip("/")


def is_cloud_auth_enabled() -> bool:
    """True si Supabase Auth + PostgreSQL multiusuario están configurados."""
    url = get_supabase_url()
    anon = get_supabase_anon_key()
    return bool(url.startswith("https://") and len(anon) >= 20)


def _traducir_error_auth(resp_json: Any, fallback: str) -> str:
    msg = ""
    if isinstance(resp_json, dict):
        msg = str(
            resp_json.get("error_description")
            or resp_json.get("msg")
            or resp_json.get("message")
            or resp_json.get("error")
            or ""
        ).strip()
    low = msg.lower()
    for patron, texto_es in _ERR_MAP_ES.items():
        if patron in low:
            return texto_es
    return msg if msg else fallback


def _auth_headers(access_token: Optional[str] = None) -> dict[str, str]:
    anon = get_supabase_anon_key()
    hdrs = {
        "apikey": anon,
        "Content-Type": "application/json",
    }
    if access_token:
        hdrs["Authorization"] = f"Bearer {access_token}"
    elif anon.startswith("eyJ"):
        # Solo enviar Authorization: Bearer con claves JWT clásicas (anon);
        # las nuevas claves `sb_publishable_...` se envían únicamente en `apikey`.
        hdrs["Authorization"] = f"Bearer {anon}"
    return hdrs


def _construir_sesion_usuario(data: dict) -> Optional[dict]:
    """Normaliza la respuesta de Supabase Auth en un dict de sesión consistente."""
    access_token = data.get("access_token")
    user_obj = data.get("user") or {}
    uid = user_obj.get("id")
    if not access_token or not uid:
        return None

    meta = user_obj.get("user_metadata") or {}
    email = str(user_obj.get("email") or "").strip().lower()
    full_name = str(
        meta.get("full_name")
        or meta.get("name")
        or (email.split("@")[0] if email else "Usuario")
    ).strip()
    expires_in = int(data.get("expires_in") or 3600)
    return {
        "id": str(uid),
        "email": email,
        "name": full_name,
        "avatar_url": str(meta.get("avatar_url") or meta.get("picture") or ""),
        "provider": str((user_obj.get("app_metadata") or {}).get("provider") or "email"),
        "access_token": str(access_token),
        "refresh_token": str(data.get("refresh_token") or ""),
        "expires_at": int(time.time()) + max(60, expires_in),
    }


def _guardar_sesion_en_estado(user_dict: dict) -> None:
    try:
        import streamlit as st
        st.session_state[_SESSION_USER_KEY] = user_dict
    except Exception:
        pass


def get_current_user() -> Optional[dict]:
    """
    Devuelve el usuario autenticado actual (con renovación automática de JWT)
    o None si no hay sesión iniciada.
    """
    if not is_cloud_auth_enabled():
        return None
    try:
        import streamlit as st
        user = st.session_state.get(_SESSION_USER_KEY)
    except Exception:
        return None

    if not isinstance(user, dict) or not user.get("id") or not user.get("access_token"):
        return None

    # Refrescar automáticamente si faltan menos de 3 minutos para expirar
    expires_at = int(user.get("expires_at") or 0)
    if expires_at > 0 and time.time() >= (expires_at - 180):
        rt = user.get("refresh_token")
        if rt:
            ok, refreshed = _refresh_session(rt)
            if ok and refreshed:
                _guardar_sesion_en_estado(refreshed)
                return refreshed
        if time.time() >= expires_at:
            sign_out()
            return None

    return user


def is_cloud_user_authenticated() -> bool:
    return get_current_user() is not None


def get_current_user_id() -> str:
    """Devuelve el UUID del usuario en modo nube, o 'local' en modo SQLite local."""
    user = get_current_user()
    if user and user.get("id"):
        return str(user["id"])
    return "local"


def get_current_access_token() -> Optional[str]:
    user = get_current_user()
    return user.get("access_token") if user else None


def _refresh_session(refresh_token: str) -> tuple[bool, Optional[dict]]:
    base = get_supabase_url()
    if not base or not refresh_token:
        return False, None
    try:
        resp = requests.post(
            f"{base}/auth/v1/token?grant_type=refresh_token",
            headers=_auth_headers(),
            json={"refresh_token": refresh_token},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            sesion = _construir_sesion_usuario(resp.json())
            if sesion:
                return True, sesion
    except Exception:
        pass
    return False, None


def sign_in_email(email: str, password: str) -> tuple[bool, str]:
    """Inicia sesión con correo electrónico y contraseña en Supabase Auth."""
    if not is_cloud_auth_enabled():
        return False, "Supabase no está configurado en este entorno."
    email_limpio = (email or "").strip().lower()
    if not email_limpio or not password:
        return False, "Introduce tu correo electrónico y contraseña."

    try:
        resp = requests.post(
            f"{get_supabase_url()}/auth/v1/token?grant_type=password",
            headers=_auth_headers(),
            json={"email": email_limpio, "password": password},
            timeout=_HTTP_TIMEOUT,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code == 200:
            sesion = _construir_sesion_usuario(data)
            if sesion:
                _guardar_sesion_en_estado(sesion)
                return True, "Sesión iniciada correctamente."
            return False, "Respuesta de autenticación incompleta."
        return False, _traducir_error_auth(data, "No se pudo iniciar sesión.")
    except requests.RequestException:
        return False, "No se pudo conectar con el servidor de autenticación. Inténtalo de nuevo."
    except Exception as e:
        return False, f"Error inesperado al iniciar sesión: {e}"


def sign_up_email(email: str, password: str, full_name: str = "") -> tuple[bool, str, bool]:
    """
    Registra una nueva cuenta con email y contraseña.
    Retorna `(ok, mensaje, requiere_confirmacion_email)`.
    """
    if not is_cloud_auth_enabled():
        return False, "Supabase no está configurado en este entorno.", False
    email_limpio = (email or "").strip().lower()
    nombre_limpio = (full_name or "").strip()
    if not email_limpio or "@" not in email_limpio:
        return False, "Introduce un correo electrónico válido.", False
    if not password or len(password) < 8:
        return False, "La contraseña debe tener al menos 8 caracteres.", False

    try:
        payload: dict[str, Any] = {
            "email": email_limpio,
            "password": password,
            "data": {"full_name": nombre_limpio} if nombre_limpio else {},
        }
        resp = requests.post(
            f"{get_supabase_url()}/auth/v1/signup",
            headers=_auth_headers(),
            json=payload,
            timeout=_HTTP_TIMEOUT,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code in (200, 201):
            # Si Supabase devuelve sesión directa (confirmación desactivada)
            sesion = _construir_sesion_usuario(data)
            if sesion:
                _guardar_sesion_en_estado(sesion)
                return True, "Cuenta creada e inicio de sesión automático completado.", False
            # Confirmación por correo requerida
            if data.get("id") or (data.get("user") or {}).get("id"):
                return (
                    True,
                    "Cuenta creada. Revisa tu bandeja de entrada y confirma tu correo antes de entrar.",
                    True,
                )
        return False, _traducir_error_auth(data, "No se pudo crear la cuenta."), False
    except requests.RequestException:
        return False, "No se pudo conectar con el servidor de registro.", False
    except Exception as e:
        return False, f"Error inesperado al registrar la cuenta: {e}", False


def send_password_reset(email: str) -> tuple[bool, str]:
    """Envía un correo de restablecimiento de contraseña vía Supabase Auth."""
    if not is_cloud_auth_enabled():
        return False, "Supabase no está configurado en este entorno."
    email_limpio = (email or "").strip().lower()
    if not email_limpio or "@" not in email_limpio:
        return False, "Introduce un correo electrónico válido."

    try:
        resp = requests.post(
            f"{get_supabase_url()}/auth/v1/recover",
            headers=_auth_headers(),
            json={
                "email": email_limpio,
                "redirect_to": get_app_public_url(),
            },
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code in (200, 204):
            return True, "Si el correo existe, recibirás un enlace para restablecer tu contraseña."
        data = resp.json() if resp.content else {}
        return False, _traducir_error_auth(data, "No se pudo enviar el correo de recuperación.")
    except requests.RequestException:
        return False, "Error de red al solicitar la recuperación de contraseña."


def is_google_oauth_enabled() -> bool:
    """Consulta `/auth/v1/settings` (cacheado 5 min) para saber si Google OAuth está activo."""
    if not is_cloud_auth_enabled():
        return False
    try:
        import streamlit as st
        return _check_google_provider_cached(get_supabase_url(), get_supabase_anon_key())
    except Exception:
        return False


def _fetch_google_provider(base_url: str, anon_key: str) -> bool:
    try:
        hdrs = {"apikey": anon_key}
        if anon_key.startswith("eyJ"):
            hdrs["Authorization"] = f"Bearer {anon_key}"
        resp = requests.get(f"{base_url}/auth/v1/settings", headers=hdrs, timeout=6)
        if resp.status_code == 200:
            ext = (resp.json() or {}).get("external") or {}
            return bool(ext.get("google"))
    except Exception:
        pass
    return False


try:
    import streamlit as _st_auth

    @_st_auth.cache_data(ttl=300, show_spinner=False)
    def _check_google_provider_cached(base_url: str, anon_key: str) -> bool:
        return _fetch_google_provider(base_url, anon_key)

    @_st_auth.cache_resource
    def _get_pkce_store() -> dict[str, tuple[str, float]]:
        return {}
except Exception:
    _FALLBACK_PKCE_STORE: dict[str, tuple[str, float]] = {}

    def _check_google_provider_cached(base_url: str, anon_key: str) -> bool:
        return _fetch_google_provider(base_url, anon_key)

    def _get_pkce_store() -> dict[str, tuple[str, float]]:
        return _FALLBACK_PKCE_STORE


def _limpiar_pkce_expirados(store: dict[str, tuple[str, float]]) -> None:
    ahora = time.time()
    expirados = [k for k, (_, exp) in list(store.items()) if exp < ahora]
    for k in expirados:
        store.pop(k, None)


def get_google_oauth_url() -> str:
    """
    Construye la URL de autorización OAuth de Google con PKCE S256 (RFC 7636).
    Usa `?oauth_s=...` + `?code=...` (compatible con el iframe de Streamlit Cloud
    sin exponer tokens en la URL ni depender de fragmentos #hash).
    """
    base = get_supabase_url()
    if not base or not is_google_oauth_enabled():
        return ""

    store = _get_pkce_store()
    _limpiar_pkce_expirados(store)

    # Reutilizar verifier de esta sesión de Streamlit si sigue vigente
    state_id = None
    verifier = None
    try:
        import streamlit as st
        st_prev = st.session_state.get("_sl_pkce_state")
        if st_prev and st_prev in store and store[st_prev][1] > time.time() + 60:
            state_id = st_prev
            verifier = store[st_prev][0]
    except Exception:
        pass

    if not state_id or not verifier:
        state_id = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        store[state_id] = (verifier, time.time() + 600)
        try:
            import streamlit as st
            st.session_state["_sl_pkce_state"] = state_id
        except Exception:
            pass

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    redirect_url = f"{get_app_public_url()}/?oauth_s={state_id}"

    params = urlencode({
        "provider": "google",
        "redirect_to": redirect_url,
        "code_challenge": challenge,
        "code_challenge_method": "s256",
    })
    return f"{base}/auth/v1/authorize?{params}"


def complete_oauth_from_pkce_code(auth_code: str, state_id: str) -> tuple[bool, str]:
    """Intercambia un código PKCE (`?code=...`) servidor-a-servidor contra Supabase."""
    if not is_cloud_auth_enabled() or not auth_code or not state_id:
        return False, "Parámetros de autorización OAuth incompletos."

    store = _get_pkce_store()
    _limpiar_pkce_expirados(store)
    entry = store.pop(state_id, None)
    if not entry:
        return False, "La solicitud de acceso con Google ha expirado. Vuelve a pulsar «Continuar con Google»."

    verifier, _ = entry
    try:
        resp = requests.post(
            f"{get_supabase_url()}/auth/v1/token?grant_type=pkce",
            headers=_auth_headers(),
            json={"auth_code": auth_code, "code_verifier": verifier},
            timeout=_HTTP_TIMEOUT,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code == 200:
            sesion = _construir_sesion_usuario(data)
            if sesion:
                _guardar_sesion_en_estado(sesion)
                return True, "Sesión iniciada con Google."
            return False, "Respuesta de sesión incompleta tras validar con Google."
        return False, _traducir_error_auth(data, "No se pudo completar el acceso con Google.")
    except requests.RequestException:
        return False, "Error de conexión al validar el acceso con Google."


def complete_oauth_from_tokens(access_token: str, refresh_token: str = "", expires_in: int = 3600) -> tuple[bool, str]:
    """
    Valida un access_token OAuth directamente contra `/auth/v1/user` en Supabase
    (nunca confía en parámetros URL sin verificación del servidor) y abre sesión.
    """
    if not is_cloud_auth_enabled() or not access_token:
        return False, "Token de autenticación ausente."
    try:
        resp = requests.get(
            f"{get_supabase_url()}/auth/v1/user",
            headers=_auth_headers(access_token),
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return False, "La sesión de Google no es válida o ha expirado."
        user_obj = resp.json()
        sesion = _construir_sesion_usuario({
            "access_token": access_token,
            "refresh_token": refresh_token or "",
            "expires_in": expires_in,
            "user": user_obj,
        })
        if not sesion:
            return False, "No se pudieron obtener los datos del usuario."
        _guardar_sesion_en_estado(sesion)
        return True, "Sesión iniciada con Google."
    except requests.RequestException:
        return False, "Error de conexión al validar la sesión de Google."


def sign_out() -> None:
    """Cierra la sesión en Supabase y limpia todos los datos privados de session_state."""
    try:
        import streamlit as st
        user = st.session_state.get(_SESSION_USER_KEY)
        if isinstance(user, dict) and user.get("access_token") and is_cloud_auth_enabled():
            try:
                requests.post(
                    f"{get_supabase_url()}/auth/v1/logout",
                    headers=_auth_headers(user["access_token"]),
                    timeout=5,
                )
            except Exception:
                pass
        for k in (
            _SESSION_USER_KEY,
            "ia_chat_hist",
            "ia_gemini_hist",
            "ia_ultimo_ticker",
            "ia_pending",
            "mostrar_modal_cuenta",
            "compra_preseleccionada",
            "accion_activa",
            "venta_activa",
            "edicion_activa",
            "eliminacion_activa",
        ):
            st.session_state.pop(k, None)
    except Exception:
        pass


def supabase_rest(
    method: str,
    table: str,
    access_token: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    payload: Any = None,
    prefer: Optional[str] = None,
) -> tuple[bool, Any]:
    """
    Cliente PostgREST autenticado por JWT (aplica Row-Level Security en Supabase).
    Retorna `(ok, data_or_error_str)`.
    """
    token = access_token or get_current_access_token()
    base = get_supabase_url()
    if not base or not token:
        return False, "Sesión de usuario no disponible."

    headers = _auth_headers(token)
    if prefer:
        headers["Prefer"] = prefer

    url = f"{base}/rest/v1/{table}"
    try:
        resp = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            json=payload,
            timeout=_HTTP_TIMEOUT,
        )
        if 200 <= resp.status_code < 300:
            if resp.content:
                try:
                    return True, resp.json()
                except Exception:
                    return True, resp.text
            return True, None
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        return False, f"HTTP {resp.status_code}: {err}"
    except requests.RequestException as e:
        return False, f"Error de conexión con Supabase: {e}"


def delete_all_user_data() -> tuple[bool, str]:
    """
    Borra todos los datos privados del usuario autenticado (RGPD):
    cartera, watchlist, preferencias_alertas, alertas_enviadas_log e ia_consumo_diario.
    """
    user = get_current_user()
    if not user:
        return False, "No hay ninguna sesión activa."
    uid = user["id"]
    token = user["access_token"]
    tablas = (
        "cartera",
        "watchlist",
        "preferencias_alertas",
        "alertas_enviadas_log",
        "ia_consumo_diario",
    )
    errores = []
    for tbl in tablas:
        ok, err = supabase_rest(
            "DELETE",
            tbl,
            access_token=token,
            params={"user_id": f"eq.{uid}"},
        )
        if not ok:
            errores.append(f"{tbl}: {err}")
    if errores:
        return False, "Error parcial al borrar datos: " + "; ".join(errores)
    return True, "Todos tus datos privados han sido eliminados."
