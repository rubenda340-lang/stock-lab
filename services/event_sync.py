"""
Sincronización global de eventos corporativos (dividendos, resultados).

Extraído de views/dashboard.py como parte de CF-013 para desacoplar
la sincronización de eventos del Dashboard y hacerla disponible globalmente.

La función sincronizar_eventos_db() se ejecuta una vez por sesión de Streamlit
desde app.py, independientemente de cuál sea la primera vista que visite el usuario.

Mecanismo de protección de frecuencia:
  1. st.session_state["_eventos_sincronizados"] → máximo 1 vez por sesión.
  2. data/cache/last_event_sync.txt → máximo 1 sincronización completa por día.
"""

import streamlit as st
import os
import time
import datetime as _dt
import concurrent.futures
import pandas as pd
import yfinance as yf
import requests

from database import (
    query_all, obtener_watchlist,
    calcular_posiciones_abiertas_filtradas, get_connection,
)


# ═══════════════════════════════════════════════════════════════════
# Sesión HTTP compartida para Yahoo Finance
# ═══════════════════════════════════════════════════════════════════
_yf_session = requests.Session()
_yf_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0'
})


# ═══════════════════════════════════════════════════════════════════
# Sincronización global de eventos corporativos
# ═══════════════════════════════════════════════════════════════════

def sincronizar_eventos_db(force=False):
    """
    Sincroniza la tabla 'eventos' de SQLite con fuentes de datos corporativos.

    Niveles de fuentes:
      - Nivel 1 (confianza 100): data/dividendos_oficiales.json
      - Nivel 3 (confianza 70):  Yahoo Finance calendars

    Protección de frecuencia:
      - Fichero data/cache/last_event_sync.txt → 1 sync/día máximo.
      - Idempotencia vía UNIQUE(ticker, evento, fecha) ON CONFLICT REPLACE.
    """
    hoy = _dt.date.today()
    hoy_str = hoy.isoformat()
    cache_dir = "data/cache"
    cache_path = os.path.join(cache_dir, "last_event_sync.txt")
    oficiales_path = os.path.join("data", "dividendos_oficiales.json")

    # ── Cache check ──────────────────────────────────────────────────────
    if not force:
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    last_date = f.read().strip()
                if last_date == hoy_str:
                    # Limpiar eventos obsoletos y volver
                    conn = get_connection()
                    try:
                        cursor = conn.cursor()
                        cursor.execute(
                            "DELETE FROM eventos WHERE fecha < ?", (hoy_str,)
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    return
        except Exception:
            pass

    # ── Gather tickers ───────────────────────────────────────────────────
    try:
        df_wl = obtener_watchlist()
        wl_tickers = df_wl['ticker'].tolist() if not df_wl.empty else []
    except Exception:
        wl_tickers = []
    try:
        posiciones = calcular_posiciones_abiertas_filtradas("REAL")
        port_tickers = [p['ticker'] for p in posiciones]
    except Exception:
        port_tickers = []

    default_tickers = ["SAN.MC", "BBVA.MC", "REP.MC", "TEF.MC", "ITX.MC", "IBE.MC"]
    tickers = list(set(wl_tickers + port_tickers + default_tickers))

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 1. Limpiar eventos pasados (no los de confianza 100 futura)
        cursor.execute("DELETE FROM eventos WHERE fecha < ?", (hoy_str,))

        # Company name cache
        companies_rows = cursor.execute(
            "SELECT ticker, nombre FROM empresas WHERE activa = 1"
        ).fetchall()
        company_names = {r['ticker'].upper(): r['nombre'] for r in companies_rows}

        # ── NIVEL 1: Datos oficiales (confianza 100) ─────────────────────
        tickers_nivel1 = set()  # tickers cubiertos por la fuente oficial
        try:
            if os.path.exists(oficiales_path):
                with open(oficiales_path, "r", encoding="utf-8") as f:
                    import json as _json
                    datos_oficiales = _json.load(f)
                for div in datos_oficiales.get("dividendos", []):
                    try:
                        t = div["ticker"].upper()
                        nombre = div.get("empresa", company_names.get(t, t.replace(".MC", "")))
                        pago_str = div.get("pago_date")
                        ex_str = div.get("ex_dividend_date")
                        if not pago_str:
                            continue
                        pago_date = _dt.date.fromisoformat(pago_str)
                        if pago_date < hoy:
                            continue  # Dividendo ya cobrado
                        # Insertar evento de pago con confianza 100
                        cursor.execute("""
                            INSERT OR REPLACE INTO eventos
                                (ticker, empresa, evento, fecha, importancia, tipo,
                                 nivel_confianza, ex_dividend_date)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (t, nombre, "Pago Dividendo", pago_str,
                              "alta", "dividendo", 100, ex_str))
                        # También insertar el ex-div si es futuro
                        if ex_str:
                            ex_date = _dt.date.fromisoformat(ex_str)
                            if ex_date >= hoy:
                                cursor.execute("""
                                    INSERT OR REPLACE INTO eventos
                                        (ticker, empresa, evento, fecha, importancia, tipo,
                                         nivel_confianza, ex_dividend_date)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """, (t, nombre, "Descuento Dividendo (Ex-Div)", ex_str,
                                      "media", "dividendo", 100, ex_str))
                        tickers_nivel1.add(t)
                    except Exception:
                        pass
        except Exception:
            pass

        # ── NIVEL 3: Yahoo Finance (confianza 70) ────────────────────────
        # Solo procesar tickers SIN datos oficiales para dividendos
        def fetch_calendar(t_ticker):
            try:
                time.sleep(0.05)
                t_obj = yf.Ticker(t_ticker, session=_yf_session)
                cal = t_obj.calendar
                is_est = False
                if cal and 'Earnings Date' in cal and cal['Earnings Date']:
                    is_est = t_obj.info.get('isEarningsDateEstimate', False)
                return t_ticker, cal, is_est
            except Exception:
                return t_ticker, None, False

        calendars_dict = {}
        estimates_dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futuros = {executor.submit(fetch_calendar, t): t for t in tickers}
            for futuro in concurrent.futures.as_completed(futuros):
                t_ticker, cal_data, is_est = futuro.result()
                if cal_data:
                    calendars_dict[t_ticker] = cal_data
                    estimates_dict[t_ticker] = is_est

        for ticker_upper, cal in calendars_dict.items():
            if not cal:
                continue
            nombre_empresa = company_names.get(ticker_upper.upper(), ticker_upper.replace('.MC', ''))

            # Dividendo: omitir si ya cubierto por fuente oficial (nivel 1)
            if ticker_upper.upper() not in tickers_nivel1:
                ex_div_date = cal.get('Ex-Dividend Date')
                if ex_div_date:
                    try:
                        date_val = (ex_div_date if isinstance(ex_div_date, _dt.date)
                                    else pd.to_datetime(ex_div_date).date())
                        if date_val >= hoy:
                            # Yahoo sólo tiene ex-div: nunca lo llamamos "Pago Dividendo"
                            cursor.execute("""
                                INSERT OR REPLACE INTO eventos
                                    (ticker, empresa, evento, fecha, importancia, tipo,
                                     nivel_confianza, ex_dividend_date)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (ticker_upper, nombre_empresa,
                                  "Descuento Dividendo (Ex-Div)",
                                  date_val.isoformat(), "media", "dividendo",
                                  70, date_val.isoformat()))
                    except Exception:
                        pass

            # Earnings: solo si la fecha NO es estimada
            earnings_dates = cal.get('Earnings Date')
            if (earnings_dates and isinstance(earnings_dates, list)
                    and not estimates_dict.get(ticker_upper, False)):
                try:
                    earn_date = earnings_dates[0]
                    if earn_date:
                        date_val = (earn_date if isinstance(earn_date, _dt.date)
                                    else pd.to_datetime(earn_date).date())
                        if date_val >= hoy:
                            cursor.execute("""
                                INSERT OR REPLACE INTO eventos
                                    (ticker, empresa, evento, fecha, importancia, tipo,
                                     nivel_confianza, ex_dividend_date)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (ticker_upper, nombre_empresa,
                                  "Presentación Resultados",
                                  date_val.isoformat(), "alta", "resultados",
                                  70, None))
                except Exception:
                    pass

        conn.commit()

        # Actualizar caché
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(hoy_str)

    except Exception:
        pass  # Fallo silencioso: no debe impedir arrancar StockLab
    finally:
        conn.close()


def obtener_eventos_db():
    """
    Consulta los eventos futuros almacenados en SQLite.
    Devuelve lista de dicts con campo 'fecha' como datetime.date y 'dias' como int.
    """
    hoy_str = _dt.date.today().isoformat()
    eventos = query_all("""
        SELECT ticker, empresa, evento, fecha, importancia, tipo
        FROM eventos
        WHERE fecha >= ?
        ORDER BY fecha ASC
    """, (hoy_str,))

    # Map to dates
    resultado = []
    for ev in eventos:
        try:
            ev_fecha = _dt.date.fromisoformat(ev['fecha'])
            ev_copy = dict(ev)
            ev_copy['fecha'] = ev_fecha
            ev_copy['dias'] = (ev_fecha - _dt.date.today()).days
            resultado.append(ev_copy)
        except Exception:
            pass
    return resultado
