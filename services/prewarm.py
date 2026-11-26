"""
StockLab — Pre-warming Global del Ecosistema
=============================================
Ejecuta una única vez al arrancar la sesión para calentar todas las cachés
de datos. Después de esta ejecución, la navegación entre pestañas es
instantánea (0 ms) porque @st.cache_data ya tiene los resultados en memoria.

Seguridad:
  - No modifica contratos existentes: solo llama a funciones públicas ya cacheadas.
  - Cada paso está envuelto en try/except: un fallo parcial NO impide el arranque.
  - Se invoca exclusivamente desde app.py, protegido por session_state.
"""

import concurrent.futures
import streamlit as st


def precargar_ecosistema_completo():
    """
    Orquesta la precarga completa del ecosistema StockLab.
    Calienta todas las cachés de Streamlit para que la navegación
    entre Dashboard, Ranking, Watchlist, Mi Cartera e IA sea instantánea.
    """

    # ── PASO 1: Sincronización de eventos corporativos ──────────────────
    try:
        from services.event_sync import sincronizar_eventos_db
        sincronizar_eventos_db()
    except Exception:
        pass  # Fallo silencioso: no impide el arranque

    # ── PASO 2: Obtener tickers activos y mercados del ecosistema ───────
    try:
        from database.connection import query_all
        empresas_rows = query_all(
            "SELECT ticker, mercado FROM empresas WHERE activa = 1 ORDER BY nombre"
        )
        tickers_bme = [emp["ticker"].strip().upper() for emp in empresas_rows] if empresas_rows else []
        mercados = list({emp["mercado"] for emp in empresas_rows if emp.get("mercado")})
    except Exception:
        tickers_bme = []
        mercados = ["BME"]

    if not tickers_bme:
        return  # Sin empresas, no hay nada que precargar

    # ── PASO 3: Pre-calentar lote de precios 60d (5 min caché) ──────────
    try:
        from services.data_provider import get_price_batch
        get_price_batch(tuple(tickers_bme))
    except Exception:
        pass

    # ── PASO 4: Pre-calentar fundamentales en paralelo (12h caché) ──────
    try:
        from views.ranking import cargar_todos_los_fundamentales_en_paralelo
        cargar_todos_los_fundamentales_en_paralelo(tickers_bme)
    except Exception:
        pass

    # ── PASO 4b: Pre-calentar Ranking completo procesado en memoria ─────
    try:
        from views.ranking import obtener_empresas_ranking_procesadas
        obtener_empresas_ranking_procesadas()
    except Exception:
        pass

    # ── PASO 5: Pre-calentar Dashboard (variaciones + scores + IBEX 35) ──
    for m in (mercados or ["BME"]):
        try:
            from views.dashboard import obtener_datos_pulso_mercado
            obtener_datos_pulso_mercado(m)
        except Exception:
            pass

        try:
            from views.dashboard import obtener_variaciones_rapidas
            obtener_variaciones_rapidas(m)
        except Exception:
            pass

    # Pre-calentar gráfico e índice IBEX 35 por defecto (periodo 6M)
    try:
        import market_data
        market_data.get_price_history("^IBEX", period="6mo", interval="1d")
        market_data.get_stock_data("^IBEX")
    except Exception:
        pass

    # ── PASO 6: Pre-calentar Watchlist (market_data por ticker) ─────────
    _precalentar_watchlist()

    # ── PASO 7: Pre-calentar Mi Cartera (market_data por ticker) ────────
    _precalentar_cartera()

    # ── PASO 8: Pre-calentar Área de IA (Radar, Tracking, Asistente) ────
    _precalentar_ia()


def _precalentar_watchlist():
    """Pre-calienta la caché de market_data.get_stock_data para los tickers de la Watchlist."""
    try:
        from database import obtener_watchlist
        import market_data

        df_wl = obtener_watchlist()
        if df_wl is None or df_wl.empty:
            return

        tickers_wl = df_wl["ticker"].dropna().unique().tolist()
        if not tickers_wl:
            return

        def _fetch_one(ticker):
            try:
                import time
                time.sleep(0.05)
                market_data.get_stock_data(ticker.strip().upper())
            except Exception:
                pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(_fetch_one, tickers_wl))

    except Exception:
        pass


def _precalentar_cartera():
    """Pre-calienta la caché de market_data.get_stock_data para los tickers en cartera abierta."""
    try:
        from database.cartera import (
            calcular_posiciones_abiertas,
            calcular_posiciones_abiertas_filtradas,
            obtener_precios_actuales_batch
        )
        import market_data

        posiciones = calcular_posiciones_abiertas()
        if not posiciones:
            return

        tickers_cartera = list({pos["ticker"].strip().upper() for pos in posiciones if pos.get("ticker")})
        if not tickers_cartera:
            return

        # 1. Calienta market_data.get_stock_data individual en paralelo
        def _fetch_one(ticker):
            try:
                import time
                time.sleep(0.05)
                market_data.get_stock_data(ticker)
            except Exception:
                pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(_fetch_one, tickers_cartera))

        # 2. Calienta la caché de obtener_precios_actuales_batch con las listas exactas de cada cartera
        for tipo in ("SIMULADA", "REAL"):
            pos_tipo = calcular_posiciones_abiertas_filtradas(tipo)
            if pos_tipo:
                t_list = [p['ticker'] for p in pos_tipo]
                obtener_precios_actuales_batch(t_list)

    except Exception:
        pass


def _precalentar_ia():
    """Pre-calienta el área de Inteligencia Artificial (Radar Predictivo, Tracking, Asistente e Historial)."""
    try:
        from views.ia import precargar_ia
        precargar_ia()
    except Exception as e:
        print(f"[PREWARM IA ERROR] {e}")

