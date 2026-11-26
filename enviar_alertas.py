"""
enviar_alertas.py — Motor 24/7 de Alertas por Correo Multiusuario de StockLab (Fase 3)

Se ejecuta automáticamente vía GitHub Actions Cron (cada 20 min en horario BME)
o manualmente en modo prueba (`--dry-run`):

  1. Lee todos los usuarios con `email_activo = true` en Supabase (`preferencias_alertas`).
  2. Descarga UNA SOLA VEZ precios actuales y variación diaria de los tickers implicados.
  3. Evalúa por usuario sus reglas personalizadas:
     - Watchlist: precio objetivo alcanzado y proximidad configurable (±X%).
     - Cartera Real / Simulada: ganancia acumulada (>= +X%), caída (<= -Y%) y
       movimiento intradía fuerte (>= ±Z%).
  4. Filtra contra `alertas_enviadas_log` (anti-spam: máx. 1 aviso por usuario/ticker/tipo/día).
  5. Envía un correo HTML limpio vía API de Resend y registra los envíos.

Variables de entorno requeridas (GitHub Actions Secrets o .env):
  - SUPABASE_URL
  - SUPABASE_SERVICE_ROLE_KEY
  - RESEND_API_KEY
  - ALERT_FROM_EMAIL   (ej. "StockLab Alertas <alertas@tudominio.com>")
  - APP_PUBLIC_URL     (ej. "https://stocklab.streamlit.app")
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html as _html
import os
import sys
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

_MADRID_TZ = ZoneInfo("Europe/Madrid")
_HTTP_TIMEOUT = 15


def _env(clave: str, default: str = "") -> str:
    return (os.getenv(clave) or default).strip()


def _en_horario_bme(ahora: dt.datetime) -> bool:
    """True si es lunes-viernes entre 09:00 y 17:45 hora de Madrid."""
    if ahora.weekday() >= 5:
        return False
    hm = (ahora.hour, ahora.minute)
    return (9, 0) <= hm <= (17, 45)


def _sb_admin_headers(service_key: str) -> dict[str, str]:
    hdrs = {
        "apikey": service_key,
        "Content-Type": "application/json",
    }
    if service_key.startswith("eyJ"):
        hdrs["Authorization"] = f"Bearer {service_key}"
    return hdrs


def _sb_get(base_url: str, service_key: str, table: str, params: dict[str, Any]) -> list[dict]:
    resp = requests.get(
        f"{base_url}/rest/v1/{table}",
        headers=_sb_admin_headers(service_key),
        params=params,
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _sb_insert(base_url: str, service_key: str, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    headers = _sb_admin_headers(service_key)
    headers["Prefer"] = "resolution=ignore-duplicates,return=minimal"
    resp = requests.post(
        f"{base_url}/rest/v1/{table}",
        headers=headers,
        json=rows,
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()


def _descargar_precios(tickers: set[str]) -> dict[str, tuple[float, float]]:
    """Descarga (precio_actual, variacion_diaria_pct) en paralelo una única vez."""
    from services.data_provider import get_intraday_variation

    resultados: dict[str, tuple[float, float]] = {}
    if not tickers:
        return resultados

    def _fetch(tk: str):
        try:
            p, v = get_intraday_variation(tk)
            if p is not None and p > 0:
                return tk, float(p), float(v or 0.0)
        except Exception:
            pass
        return tk, None, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_fetch, t) for t in sorted(tickers)]
        for f in concurrent.futures.as_completed(futs):
            tk, p, v = f.result()
            if p is not None:
                resultados[tk] = (p, v)
    return resultados


def _calcular_posiciones_abiertas_usuario(ops: list[dict]) -> list[dict]:
    """Reconstruye posiciones abiertas con coste medio a partir de filas de `public.cartera`."""
    pos_map: dict[str, dict] = {}
    ops_ord = sorted(ops, key=lambda r: (str(r.get("fecha") or ""), int(r.get("id") or 0)))
    for op in ops_ord:
        tipo = str(op.get("tipo") or "").upper()
        tk = str(op.get("ticker") or "").strip().upper()
        if not tk or tipo not in ("COMPRA", "VENTA"):
            continue
        cant = float(op.get("cantidad") or 0.0)
        prec = float(op.get("precio_unitario") or 0.0)
        com = float(op.get("comision") or 0.0)
        p = pos_map.setdefault(tk, {
            "ticker": tk,
            "nombre": str(op.get("empresa_nombre") or tk.replace(".MC", "")),
            "acciones": 0.0,
            "coste": 0.0,
            "comisiones": 0.0,
        })
        if tipo == "COMPRA":
            p["acciones"] += cant
            p["coste"] += cant * prec
            p["comisiones"] += com
        elif tipo == "VENTA" and p["acciones"] > 0:
            acc_prev = p["acciones"]
            coste_u = p["coste"] / acc_prev
            p["acciones"] -= cant
            p["coste"] -= cant * coste_u
            if p["acciones"] <= 0.001:
                p["acciones"] = 0.0
                p["coste"] = 0.0
                p["comisiones"] = 0.0
            else:
                p["comisiones"] *= max(0.0, p["acciones"] / acc_prev)

    abiertas = []
    for p in pos_map.values():
        if p["acciones"] > 0.001 and (p["coste"] + p["comisiones"]) > 0:
            abiertas.append(p)
    return abiertas


def _evaluar_alertas_usuario(
    pref: dict,
    wl_rows: list[dict],
    cart_rows: list[dict],
    precios: dict[str, tuple[float, float]],
) -> list[dict]:
    """Evalúa todas las reglas configuradas para un usuario y devuelve lista de avisos."""
    avisos: list[dict] = []

    # ── 1. Watchlist ─────────────────────────────────────────────────────
    if pref.get("avisar_wl_alcanzado") or pref.get("avisar_wl_cerca"):
        umbral_cerca = float(pref.get("umbral_wl_cerca_pct") or 3.0)
        for w in wl_rows:
            tk = str(w.get("ticker") or "").strip().upper()
            obj_raw = w.get("precio_objetivo")
            if not tk or obj_raw is None or tk not in precios:
                continue
            obj = float(obj_raw)
            if obj <= 0:
                continue
            precio_act, var_hoy = precios[tk]
            tipo_obj = str(w.get("tipo_objetivo") or "COMPRA_EN_CAIDA").upper()

            if tipo_obj == "COMPRA_EN_RUPTURA":
                if precio_act >= obj and pref.get("avisar_wl_alcanzado"):
                    avisos.append({
                        "ticker": tk,
                        "tipo_alerta": "wl_ruptura_alcanzada",
                        "bloque": "Watchlist",
                        "nivel": "positiva",
                        "titulo": f"{tk} ha superado tu objetivo de ruptura",
                        "detalle": f"Cotiza a {precio_act:.2f} € ({var_hoy:+.2f}% hoy), por encima de tu objetivo de {obj:.2f} €.",
                    })
                elif pref.get("avisar_wl_cerca"):
                    dist = ((obj - precio_act) / obj) * 100.0
                    if 0 < dist <= umbral_cerca:
                        avisos.append({
                            "ticker": tk,
                            "tipo_alerta": "wl_cerca_ruptura",
                            "bloque": "Watchlist",
                            "nivel": "aviso",
                            "titulo": f"{tk} está a {dist:.1f}% de romper tu objetivo",
                            "detalle": f"Precio actual: {precio_act:.2f} € | Objetivo ruptura: {obj:.2f} €.",
                        })
            else:
                if precio_act <= obj and pref.get("avisar_wl_alcanzado"):
                    avisos.append({
                        "ticker": tk,
                        "tipo_alerta": "wl_caida_alcanzada",
                        "bloque": "Watchlist",
                        "nivel": "positiva",
                        "titulo": f"{tk} ha alcanzado tu precio objetivo de entrada",
                        "detalle": f"Cotiza a {precio_act:.2f} € ({var_hoy:+.2f}% hoy), en o por debajo de tu objetivo de {obj:.2f} €.",
                    })
                elif pref.get("avisar_wl_cerca"):
                    dist = ((precio_act - obj) / precio_act) * 100.0
                    if 0 < dist <= umbral_cerca:
                        avisos.append({
                            "ticker": tk,
                            "tipo_alerta": "wl_cerca_caida",
                            "bloque": "Watchlist",
                            "nivel": "aviso",
                            "titulo": f"{tk} se acerca a tu objetivo ({dist:.1f}%)",
                            "detalle": f"Precio actual: {precio_act:.2f} € | Objetivo entrada: {obj:.2f} €.",
                        })

    # ── 2. Cartera Real / Simulada ───────────────────────────────────────
    modos_activos = []
    if pref.get("avisar_cartera_real"):
        modos_activos.append("REAL")
    if pref.get("avisar_cartera_simulada"):
        modos_activos.append("SIMULADA")

    u_gan = float(pref.get("umbral_ganancia_pct") or 15.0)
    u_per = float(pref.get("umbral_perdida_pct") or -8.0)
    u_intra = float(pref.get("umbral_intradia_pct") or 4.0)

    for modo in modos_activos:
        ops_modo = [o for o in cart_rows if str(o.get("tipo_cartera") or "SIMULADA").upper() == modo]
        posiciones = _calcular_posiciones_abiertas_usuario(ops_modo)
        etq_modo = "Cartera Real" if modo == "REAL" else "Cartera Simulada"

        for pos in posiciones:
            tk = pos["ticker"]
            if tk not in precios:
                continue
            precio_act, var_hoy = precios[tk]
            valor_act = pos["acciones"] * precio_act
            coste_tot = pos["coste"] + pos["comisiones"]
            rent_pct = ((valor_act - coste_tot) / coste_tot) * 100.0

            if rent_pct >= u_gan:
                avisos.append({
                    "ticker": tk,
                    "tipo_alerta": f"cart_{modo.lower()}_ganancia",
                    "bloque": etq_modo,
                    "nivel": "positiva",
                    "titulo": f"{pos['nombre']} ({tk}) acumula +{rent_pct:.1f}%",
                    "detalle": f"Posición de {pos['acciones']:.2f} acc. valorada en {valor_act:,.2f} € (supera tu umbral +{u_gan:.0f}%).",
                })
            elif rent_pct <= u_per:
                avisos.append({
                    "ticker": tk,
                    "tipo_alerta": f"cart_{modo.lower()}_perdida",
                    "bloque": etq_modo,
                    "nivel": "critica",
                    "titulo": f"Alerta de caída en {pos['nombre']} ({tk}): {rent_pct:.1f}%",
                    "detalle": f"Posición de {pos['acciones']:.2f} acc. valorada en {valor_act:,.2f} € (por debajo de tu límite {u_per:.0f}%).",
                })

            if abs(var_hoy) >= u_intra:
                dir_txt = "sube" if var_hoy >= 0 else "cae"
                avisos.append({
                    "ticker": tk,
                    "tipo_alerta": f"cart_{modo.lower()}_intradia_{'up' if var_hoy >= 0 else 'dn'}",
                    "bloque": etq_modo,
                    "nivel": "positiva" if var_hoy >= 0 else "critica",
                    "titulo": f"{pos['nombre']} ({tk}) {dir_txt} {var_hoy:+.2f}% hoy",
                    "detalle": f"Movimiento intradía superior a tu aviso de ±{u_intra:.1f}%. Precio actual: {precio_act:.2f} €.",
                })

    return avisos


def _render_email_html(nombre: str, avisos: list[dict], app_url: str, es_resumen: bool = False) -> str:
    color_map = {"positiva": "#34d399", "critica": "#f87171", "aviso": "#fbbf24"}
    bloques_html = []
    for a in avisos:
        col = color_map.get(a.get("nivel"), "#38bdf8")
        bloques_html.append(
            f'<div style="background:#111a2c;border:1px solid #1e293b;border-left:4px solid {col};'
            f'border-radius:10px;padding:14px 16px;margin-bottom:10px;">'
            f'<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;'
            f'color:{col};margin-bottom:4px;">{_html.escape(a["bloque"])}</div>'
            f'<div style="font-size:15px;font-weight:700;color:#f8fafc;margin-bottom:4px;">'
            f'{_html.escape(a["titulo"])}</div>'
            f'<div style="font-size:13px;color:#cbd5e1;line-height:1.5;">'
            f'{_html.escape(a["detalle"])}</div>'
            f'</div>'
        )
    subtitulo = (
        "Resumen diario del estado de tus inversiones al cierre de mercado."
        if es_resumen
        else f"Hemos detectado {len(avisos)} aviso(s) importante(s) en tu terminal privado."
    )
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:24px;background:#080c15;font-family:Inter,Segoe UI,Arial,sans-serif;color:#eef2f8;">
  <div style="max-width:600px;margin:0 auto;background:#0f1726;border:1px solid #1e293b;border-radius:16px;padding:28px;">
    <div style="font-size:22px;font-weight:800;color:#ffffff;letter-spacing:-0.03em;">StockLab</div>
    <div style="font-size:12px;color:#38bdf8;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:18px;">
      INVESTMENT TERMINAL · ALERTAS
    </div>
    <p style="font-size:14px;color:#cbd5e1;margin:0 0 18px 0;">
      Hola {_html.escape(nombre or 'inversor')}, {subtitulo}
    </p>
    {''.join(bloques_html)}
    <div style="margin-top:22px;text-align:center;">
      <a href="{_html.escape(app_url)}" style="display:inline-block;background:#38bdf8;color:#080c15;
         font-weight:700;font-size:14px;text-decoration:none;padding:11px 24px;border-radius:10px;">
        Abrir mi StockLab
      </a>
    </div>
    <p style="margin-top:24px;padding-top:14px;border-top:1px solid #1e293b;font-size:11px;color:#64748b;line-height:1.5;">
      Puedes modificar o desactivar estos avisos en cualquier momento desde <b>Cuenta → Alertas Automáticas</b> dentro de StockLab.<br>
      Aviso informativo: este correo no constituye asesoramiento financiero ni recomendación de inversión.
    </p>
  </div>
</body>
</html>"""


def _enviar_resend(api_key: str, from_email: str, to_email: str, subject: str, html_body: str) -> None:
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        },
        timeout=_HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Resend HTTP {resp.status_code}: {resp.text[:300]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Motor 24/7 de alertas por correo de StockLab.")
    parser.add_argument("--dry-run", action="store_true", help="Evalúa y muestra avisos sin enviar correos ni escribir log")
    parser.add_argument("--force", action="store_true", help="Ignora la ventana de horario BME (útil para pruebas)")
    parser.add_argument("--resumen-diario", action="store_true", help="Envía resumen diario al cierre BME")
    parser.add_argument("--test-email", action="store_true", help="Envía un correo de prueba de conectividad a los usuarios activos sin escribir en el log anti-spam")
    args = parser.parse_args()

    ahora_mad = dt.datetime.now(_MADRID_TZ)
    hoy_iso = ahora_mad.date().isoformat()

    if not args.force and not args.resumen_diario and not args.test_email and not _en_horario_bme(ahora_mad):
        print(f"[SKIP] Fuera de horario BME ({ahora_mad.strftime('%Y-%m-%d %H:%M %Z')}).")
        return 0

    sb_url = _env("SUPABASE_URL").rstrip("/")
    sb_key = _env("SUPABASE_SERVICE_ROLE_KEY")
    resend_key = _env("RESEND_API_KEY")
    from_email = _env("ALERT_FROM_EMAIL", "StockLab Alertas <onboarding@resend.dev>")
    app_url = _env("APP_PUBLIC_URL", "https://stocklab.streamlit.app")

    if not sb_url or not sb_key:
        print("[SKIP] Faltan SUPABASE_URL y/o SUPABASE_SERVICE_ROLE_KEY.")
        return 0

    prefs = _sb_get(sb_url, sb_key, "preferencias_alertas", {"email_activo": "eq.true"})
    if not prefs:
        print("[OK] Sin usuarios con alertas por correo activadas.")
        return 0

    profiles = {
        p["id"]: p
        for p in _sb_get(sb_url, sb_key, "profiles", {"select": "id,email,full_name"})
    }
    all_wl = _sb_get(sb_url, sb_key, "watchlist", {"select": "*"})
    all_cart = _sb_get(sb_url, sb_key, "cartera", {"select": "*"})
    sent_today = _sb_get(
        sb_url, sb_key, "alertas_enviadas_log",
        {"fecha_dia": f"eq.{hoy_iso}", "select": "user_id,ticker,tipo_alerta"},
    )
    enviados_set = {
        (r["user_id"], str(r["ticker"]).upper(), str(r["tipo_alerta"]))
        for r in sent_today
    }

    # Unir todos los tickers necesarios y descargar precios 1 única vez
    tickers_unicos: set[str] = set()
    for w in all_wl:
        if w.get("ticker"):
            tickers_unicos.add(str(w["ticker"]).strip().upper())
    for c in all_cart:
        if c.get("ticker"):
            tickers_unicos.add(str(c["ticker"]).strip().upper())

    precios = _descargar_precios(tickers_unicos)
    print(f"[INFO] Precios descargados: {len(precios)}/{len(tickers_unicos)} tickers | Usuarios activos: {len(prefs)}")

    correos_enviados = 0
    for pref in prefs:
        uid = pref["user_id"]
        prof = profiles.get(uid, {})
        to_email = (pref.get("email_destino") or prof.get("email") or "").strip()
        if not to_email or "@" not in to_email:
            continue

        wl_u = [w for w in all_wl if w.get("user_id") == uid]
        cart_u = [c for c in all_cart if c.get("user_id") == uid]

        if args.test_email:
            avisos_test = [{
                "ticker": "TEST",
                "tipo_alerta": "test",
                "bloque": "Prueba de Conexión · Sistema Activo",
                "nivel": "positiva",
                "titulo": "Alertas automáticas de StockLab conectadas correctamente",
                "detalle": (
                    f"Tu motor 24/7 está operativo en la nube. "
                    f"Actualmente vigilando {len(wl_u)} valor(es) en Watchlist y {len(_calcular_posiciones_abiertas_usuario(cart_u))} posición(es) abierta(s) en cartera."
                ),
            }]
            subject = f"StockLab · Confirmación de alertas activas ({hoy_iso})"
            html_body = _render_email_html(prof.get("full_name", ""), avisos_test, app_url, es_resumen=False)
            log_rows = []
        elif args.resumen_diario:
            if not pref.get("resumen_diario"):
                continue
            if (uid, "RESUMEN", "resumen_diario") in enviados_set:
                continue
            avisos_raw = _evaluar_alertas_usuario(pref, wl_u, cart_u, precios)
            if not avisos_raw:
                avisos_raw = [{
                    "ticker": "RESUMEN",
                    "tipo_alerta": "resumen_diario",
                    "bloque": "Resumen de Cierre",
                    "nivel": "aviso",
                    "titulo": "Todas tus posiciones y watchlist bajo control",
                    "detalle": f"Hoy no se ha disparado ningún umbral. Activos en Watchlist: {len(wl_u)}.",
                }]
            subject = f"StockLab · Resumen de cierre ({hoy_iso})"
            html_body = _render_email_html(prof.get("full_name", ""), avisos_raw, app_url, es_resumen=True)
            log_rows = [{"user_id": uid, "ticker": "RESUMEN", "tipo_alerta": "resumen_diario", "fecha_dia": hoy_iso, "detalle": subject}]
        else:
            avisos_all = _evaluar_alertas_usuario(pref, wl_u, cart_u, precios)
            avisos_nuevos = [
                a for a in avisos_all
                if (uid, a["ticker"].upper(), a["tipo_alerta"]) not in enviados_set
            ]
            if not avisos_nuevos:
                continue
            subject = (
                f"StockLab · {avisos_nuevos[0]['titulo']}"
                if len(avisos_nuevos) == 1
                else f"StockLab · {len(avisos_nuevos)} alertas en tu cartera/watchlist"
            )
            html_body = _render_email_html(prof.get("full_name", ""), avisos_nuevos, app_url, es_resumen=False)
            log_rows = [
                {
                    "user_id": uid,
                    "ticker": a["ticker"].upper(),
                    "tipo_alerta": a["tipo_alerta"],
                    "fecha_dia": hoy_iso,
                    "detalle": a["titulo"][:240],
                }
                for a in avisos_nuevos
            ]

        if args.dry_run:
            print(f"[DRY-RUN] -> {to_email} | {subject} ({len(log_rows)} aviso(s))")
            continue

        if not resend_key:
            print(f"[WARN] Falta RESEND_API_KEY; omitiendo envío a {to_email}.")
            continue

        try:
            _enviar_resend(resend_key, from_email, to_email, subject, html_body)
            _sb_insert(sb_url, sb_key, "alertas_enviadas_log", log_rows)
            correos_enviados += 1
            print(f"[SENT] {to_email} | {subject}")
        except Exception as e:
            print(f"[ERROR] Envío fallido a {to_email}: {e}")

    print(f"[OK] Proceso completado. Correos enviados: {correos_enviados}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
