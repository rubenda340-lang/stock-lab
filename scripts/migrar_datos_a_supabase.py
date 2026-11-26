"""
scripts/migrar_datos_a_supabase.py — Migración opcional de datos locales a tu cuenta Supabase

Importa tu Watchlist y Cartera (Real + Simulada) desde `stocklab.db` local
a tu cuenta privada en Supabase (PostgreSQL con RLS).

Uso:
    python scripts/migrar_datos_a_supabase.py --email tu@correo.com --dry-run
    python scripts/migrar_datos_a_supabase.py --email tu@correo.com
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from database.connection import get_connection
from services.auth_service import (
    get_supabase_anon_key,
    get_supabase_url,
    supabase_rest,
)
import requests

load_dotenv()


def _autenticar(email: str, password: str) -> tuple[str, str]:
    base = get_supabase_url()
    anon = get_supabase_anon_key()
    if not base or not anon:
        raise RuntimeError("Faltan SUPABASE_URL y/o SUPABASE_ANON_KEY en .env")
    hdrs = {"apikey": anon, "Content-Type": "application/json"}
    if anon.startswith("eyJ"):
        hdrs["Authorization"] = f"Bearer {anon}"
    resp = requests.post(
        f"{base}/auth/v1/token?grant_type=password",
        headers=hdrs,
        json={"email": email.strip().lower(), "password": password},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Error de login ({resp.status_code}): {resp.text}")
    data = resp.json()
    return data["user"]["id"], data["access_token"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Migra watchlist y cartera locales a Supabase.")
    parser.add_argument("--email", required=True, help="Correo de tu cuenta ya registrada en Supabase")
    parser.add_argument("--dry-run", action="store_true", help="Muestra lo que se migraría sin escribir nada")
    args = parser.parse_args()

    conn = get_connection()
    try:
        wl_rows = [dict(r) for r in conn.execute(
            "SELECT ticker, fecha_adicion, precio_adicion, precio_objetivo, motivo, tipo_objetivo FROM watchlist"
        ).fetchall()]
        cart_rows = [dict(r) for r in conn.execute(
            """
            SELECT c.id, e.ticker, e.nombre AS empresa_nombre, c.tipo, c.cantidad,
                   c.precio_unitario, c.importe_total, c.comision, c.fecha, c.notas
            FROM cartera c
            LEFT JOIN empresas e ON c.empresa_id = e.id
            ORDER BY c.fecha ASC, c.id ASC
            """
        ).fetchall()]
    finally:
        conn.close()

    print(f"Encontrados en stocklab.db: {len(wl_rows)} filas en Watchlist, {len(cart_rows)} operaciones en Cartera.")
    if args.dry_run:
        for w in wl_rows:
            print(f"  [WL] {w['ticker']} obj={w['precio_objetivo']} ({w['tipo_objetivo']})")
        for c in cart_rows:
            tc = "REAL" if "[REAL]" in str(c.get("notas") or "") else "SIMULADA"
            print(f"  [CART-{tc}] {c['fecha']} {c['tipo']} {c.get('ticker') or '-'} cant={c['cantidad']} imp={c['importe_total']}")
        print("Modo --dry-run finalizado sin escribir en Supabase.")
        return

    pwd = getpass.getpass(f"Contraseña de Supabase para {args.email}: ")
    uid, token = _autenticar(args.email, pwd)

    # 1. Watchlist (idempotente por PK user_id + ticker)
    wl_ok = 0
    for w in wl_rows:
        payload = {
            "user_id": uid,
            "ticker": str(w["ticker"]).strip().upper(),
            "fecha_adicion": str(w["fecha_adicion"])[:10],
            "precio_adicion": float(w["precio_adicion"]),
            "precio_objetivo": float(w["precio_objetivo"]) if w.get("precio_objetivo") is not None else None,
            "motivo": w.get("motivo") or "",
            "tipo_objetivo": w.get("tipo_objetivo") or "COMPRA_EN_CAIDA",
        }
        ok, err = supabase_rest(
            "POST",
            "watchlist",
            access_token=token,
            params={"on_conflict": "user_id,ticker"},
            payload=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        if ok:
            wl_ok += 1
        else:
            print(f"[WARN] Watchlist {payload['ticker']}: {err}")

    # 2. Cartera (comprobar si el usuario ya tiene operaciones para evitar duplicados)
    ok_chk, existentes = supabase_rest(
        "GET", "cartera", access_token=token, params={"user_id": f"eq.{uid}", "select": "id", "limit": 1}
    )
    if ok_chk and isinstance(existentes, list) and existentes:
        print("[INFO] Tu cuenta de Supabase ya tiene operaciones en Cartera; se omite inserción para evitar duplicados.")
        cart_ok = 0
    else:
        cart_ok = 0
        for c in cart_rows:
            notas = str(c.get("notas") or "")
            tc = "REAL" if "[REAL]" in notas else "SIMULADA"
            payload = {
                "user_id": uid,
                "tipo_cartera": tc,
                "ticker": str(c["ticker"]).strip().upper() if c.get("ticker") else None,
                "empresa_nombre": c.get("empresa_nombre"),
                "tipo": str(c["tipo"]).upper(),
                "cantidad": float(c.get("cantidad") or 0.0),
                "precio_unitario": float(c.get("precio_unitario") or 0.0),
                "importe_total": float(c.get("importe_total") or 0.0),
                "comision": float(c.get("comision") or 0.0),
                "fecha": str(c["fecha"])[:10],
                "notas": notas,
            }
            ok, err = supabase_rest("POST", "cartera", access_token=token, payload=payload, prefer="return=minimal")
            if ok:
                cart_ok += 1
            else:
                print(f"[WARN] Cartera op #{c['id']}: {err}")

    print(f"[OK] Migración completada: {wl_ok}/{len(wl_rows)} Watchlist, {cart_ok}/{len(cart_rows)} Cartera.")


if __name__ == "__main__":
    main()
