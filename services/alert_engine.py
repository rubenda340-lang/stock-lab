"""
services/alert_engine.py — Motor Central de Alertas de StockLab

Este módulo es la ÚNICA fuente de verdad para la generación de alertas del sistema.
Ningún otro módulo debe recalcular alertas por su cuenta.

Fuentes:
    - Watchlist     (objetivo alcanzado / cerca del objetivo)
    - Cartera Real  (rentabilidad elevada / pérdidas / stop loss)
    - Ranking       (empresa de watchlist en Top 10 de calidad)
    - Sistema       (volatilidad alta / moderada en el mercado)
    - Eventos       (eventos importantes mañana)
    - Cartera Simulada (reservado para uso futuro)

Sistema de prioridades:
    100  Objetivo alcanzado
     90  Stop Loss / Pérdidas importantes
     80  Evento importante mañana
     70  Cerca del objetivo (< 3%)
     60  Rentabilidad elevada (> 15%)
     50  Entrada Top Ranking
     40  Volatilidad alta en mercado
     30  Volatilidad moderada / Info general
"""

from __future__ import annotations

import datetime as _dt
import math as _math
from typing import Any

# Línea base diaria en memoria de ranking_estado para evitar que las alertas
# de ranking desaparezcan en el siguiente rerun de Streamlit el mismo día.
_DAILY_RANKING_BASELINE: dict[str, dict[str, dict[str, float]]] = {}


# ────────────────────────────────────────────────────────────────────────────
# CONSTANTES
# ────────────────────────────────────────────────────────────────────────────

_UMBRAL_CERCA_PCT       = 3.0    # % de distancia al objetivo → alerta amarilla
_UMBRAL_RENT_POSITIVA   = 15.0   # % de rentabilidad → alerta verde positiva
_UMBRAL_PERDIDAS        = -8.0   # % de pérdidas → alerta roja stop loss
_UMBRAL_VOLATILIDAD_ALT = 5.0    # % de variación direccional (|var| ≥ 5%) → alerta crítica/positiva
_UMBRAL_VOLATILIDAD_MOD = 2.5    # % de variación moderada (|var| ≥ 2.5%) → alerta amarilla
_TOP_RANKING_N          = 10     # número de posiciones del top ranking


# ────────────────────────────────────────────────────────────────────────────
# HELPERS INTERNOS
# ────────────────────────────────────────────────────────────────────────────

def _hoy_str() -> str:
    """Devuelve la etiqueta de fecha para la alerta."""
    return "Hoy"


def _alerta(
    tipo: str,
    origen: str,
    prioridad: int,
    ticker: str,
    empresa: str,
    mensaje: str,
    color: str,
    bg: str,
    estado: str,
    icono: str,
) -> dict:
    """Construye el diccionario estándar de una alerta."""
    return {
        "tipo":      tipo,
        "origen":    origen,
        "prioridad": prioridad,
        "ticker":    ticker,
        "empresa":   empresa,
        "mensaje":   mensaje,
        "fecha":     _hoy_str(),
        "color":     color,
        "bg":        bg,
        "estado":    estado,
        "icono":     icono,
    }


# ────────────────────────────────────────────────────────────────────────────
# GENERADORES POR FUENTE
# ────────────────────────────────────────────────────────────────────────────

def _alertas_watchlist(df_watchlist, precios_dict: dict) -> list[dict]:
    """
    Genera alertas de Watchlist respetando el tipo_objetivo
    (COMPRA_EN_CAIDA o COMPRA_EN_RUPTURA) de cada empresa.

    Para COMPRA_EN_CAIDA:
        - Alcanzado:    precio_actual <= precio_objetivo
        - Cerca:        distancia <= UMBRAL_CERCA_PCT %

    Para COMPRA_EN_RUPTURA:
        - Alcanzado:    precio_actual >= precio_objetivo
        - Cerca:        distancia <= UMBRAL_CERCA_PCT %
    """
    alertas = []
    if df_watchlist is None or df_watchlist.empty:
        return alertas

    for _, row in df_watchlist.iterrows():
        ticker    = str(row.get("ticker", "")).strip()
        ticker_k  = ticker.upper()
        objetivo  = row.get("precio_objetivo")
        tipo_obj  = row.get("tipo_objetivo", "COMPRA_EN_CAIDA")
        empresa   = row.get("_nombre", ticker.replace(".MC", ""))

        try:
            precio_obj_f = float(objetivo)
        except (TypeError, ValueError):
            continue
        if _math.isnan(precio_obj_f) or precio_obj_f <= 0:
            continue

        precio_actual = precios_dict.get(ticker_k)
        if precio_actual is None:
            continue

        try:
            precio_actual_f = float(precio_actual)
        except (TypeError, ValueError):
            continue
        if _math.isnan(precio_actual_f) or precio_actual_f <= 0:
            continue

        if tipo_obj == "COMPRA_EN_CAIDA":
            # Objetivo: el precio BAJA hasta el nivel fijado → oportunidad de compra
            if precio_actual_f <= precio_obj_f:
                alertas.append(_alerta(
                    tipo="objetivo_alcanzado",
                    origen="Watchlist",
                    prioridad=100,
                    ticker=ticker,
                    empresa=empresa,
                    mensaje=(
                        f"Ha caído hasta {precio_actual_f:.2f}€ alcanzando tu precio objetivo "
                        f"de {precio_obj_f:.2f}€. Posible oportunidad de compra."
                    ),
                    color="#10b981",
                    bg="rgba(16,185,129,0.08)",
                    estado="positiva",
                    icono="🟢",
                ))
            elif precio_actual_f > 0:
                distancia_pct = ((precio_actual_f - precio_obj_f) / precio_actual_f) * 100
                if 0 < distancia_pct <= _UMBRAL_CERCA_PCT:
                    alertas.append(_alerta(
                        tipo="cerca_objetivo",
                        origen="Watchlist",
                        prioridad=70,
                        ticker=ticker,
                        empresa=empresa,
                        mensaje=(
                            f"Está a solo {distancia_pct:.1f}% de tu precio objetivo "
                            f"de {precio_obj_f:.2f}€ (Actual: {precio_actual_f:.2f}€)."
                        ),
                        color="#f59e0b",
                        bg="rgba(245,158,11,0.06)",
                        estado="advertencia",
                        icono="🟡",
                    ))

        else:  # COMPRA_EN_RUPTURA
            # Objetivo: el precio SUBE y rompe el nivel fijado → confirmación de ruptura
            if precio_actual_f >= precio_obj_f:
                alertas.append(_alerta(
                    tipo="objetivo_alcanzado",
                    origen="Watchlist",
                    prioridad=100,
                    ticker=ticker,
                    empresa=empresa,
                    mensaje=(
                        f"Ha superado {precio_actual_f:.2f}€ confirmando la ruptura de tu "
                        f"objetivo de {precio_obj_f:.2f}€."
                    ),
                    color="#10b981",
                    bg="rgba(16,185,129,0.08)",
                    estado="positiva",
                    icono="🟢",
                ))
            elif precio_obj_f > 0:
                distancia_pct = ((precio_obj_f - precio_actual_f) / precio_obj_f) * 100
                if 0 < distancia_pct <= _UMBRAL_CERCA_PCT:
                    alertas.append(_alerta(
                        tipo="cerca_objetivo",
                        origen="Watchlist",
                        prioridad=70,
                        ticker=ticker,
                        empresa=empresa,
                        mensaje=(
                            f"Está a solo {distancia_pct:.1f}% de romper tu objetivo "
                            f"de {precio_obj_f:.2f}€ (Actual: {precio_actual_f:.2f}€)."
                        ),
                        color="#f59e0b",
                        bg="rgba(245,158,11,0.06)",
                        estado="advertencia",
                        icono="🟡",
                    ))

    return alertas


def _alertas_cartera(
    posiciones_abiertas: list,
    precios_dict: dict,
    origen: str = "Cartera Real",
) -> list[dict]:
    """
    Genera alertas de Cartera (Real o Simulada según `origen`):
    - Rentabilidad >= 15%  → positiva (prioridad 60)
    - Pérdidas    <= -8%   → stop loss crítico (prioridad 90)
    """
    alertas = []
    if not posiciones_abiertas:
        return alertas

    for pos in posiciones_abiertas:
        ticker    = str(pos.get("ticker", "")).strip()
        ticker_k  = ticker.upper()
        empresa   = pos.get("nombre", ticker.replace(".MC", ""))
        acciones  = pos.get("acciones", 0)
        coste_pos = pos.get("coste_total", 0)

        precio_act = precios_dict.get(ticker_k)
        if precio_act is None or coste_pos <= 0 or acciones <= 0:
            continue

        valor_act = acciones * float(precio_act)
        rent_pct  = ((valor_act - coste_pos) / coste_pos) * 100

        if rent_pct >= _UMBRAL_RENT_POSITIVA:
            alertas.append(_alerta(
                tipo="rentabilidad_elevada",
                origen=origen,
                prioridad=60,
                ticker=ticker,
                empresa=empresa,
                mensaje=(
                    f"Tu posición acumula una rentabilidad de +{rent_pct:.1f}%. "
                    f"Excelente resultado."
                ),
                color="#10b981",
                bg="rgba(16,185,129,0.06)",
                estado="positiva",
                icono="🟢",
            ))
        elif rent_pct <= _UMBRAL_PERDIDAS:
            alertas.append(_alerta(
                tipo="stop_loss",
                origen=origen,
                prioridad=90,
                ticker=ticker,
                empresa=empresa,
                mensaje=(
                    f"Tu posición acumula pérdidas de {rent_pct:.1f}%. "
                    f"Revisa tu stop loss."
                ),
                color="#ef4444",
                bg="rgba(239,68,68,0.06)",
                estado="critica",
                icono="🔴",
            ))

    return alertas


def _alertas_ranking(
    datos_variacion: list,
    df_watchlist,
    empresas_dict: dict,
) -> list[dict]:
    """
    Genera alertas basadas en cambios de posición y score de calidad en el Ranking.
    Utiliza la tabla `ranking_estado` para comparar contra el último cálculo guardado.
    Evita falsos positivos al añadir a la Watchlist o recalcular.
    """
    alertas = []
    if not datos_variacion or df_watchlist is None or df_watchlist.empty:
        return alertas

    try:
        import pandas as _pd
        from database import get_connection

        df_rank = _pd.DataFrame(datos_variacion)
        if "score_calidad" not in df_rank.columns or df_rank.empty:
            return alertas

        df_rank = df_rank.copy()
        df_rank["score_calidad"] = _pd.to_numeric(df_rank["score_calidad"], errors="coerce").fillna(0.0)

        # Protección ante fallback (obtener_variaciones_rapidas devuelve score_calidad=0.0)
        # y ante subconjuntos parciales de mercado (< 50 activos): nunca emitir alertas
        # falsas ni sobrescribir ranking_estado con datos incompletos o ceros.
        if df_rank["score_calidad"].max() <= 0 or len(df_rank) < 50:
            return alertas

        # Ordenar de forma determinista por score de calidad desc y ticker asc
        df_rank["_tk_norm"] = df_rank["ticker"].astype(str).str.strip().str.upper()
        df_rank = df_rank.sort_values(
            ["score_calidad", "_tk_norm"],
            ascending=[False, True],
            kind="mergesort",
        ).copy()
        df_rank["posicion"] = range(1, len(df_rank) + 1)

        hoy_iso = _dt.date.today().isoformat()

        # Leer línea base del día anterior (sobrevive reinicios vía ranking_historico)
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # Asegurar existencia de la tabla
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ranking_estado (
                    ticker          TEXT PRIMARY KEY,
                    posicion        INTEGER NOT NULL,
                    score           REAL NOT NULL,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            ya_en_memoria = hoy_iso in _DAILY_RANKING_BASELINE
            if ya_en_memoria:
                prev_state = _DAILY_RANKING_BASELINE[hoy_iso]
            else:
                prev_state = {}
                try:
                    row_fecha = cursor.execute(
                        "SELECT MAX(fecha) FROM ranking_historico WHERE fecha < ?",
                        (hoy_iso,),
                    ).fetchone()
                    fecha_prev = row_fecha[0] if row_fecha else None
                    if fecha_prev:
                        rows_h = cursor.execute(
                            "SELECT ticker, posicion, score FROM ranking_historico WHERE fecha = ?",
                            (fecha_prev,),
                        ).fetchall()
                        prev_state = {
                            str(r["ticker"]).strip().upper(): {
                                "posicion": int(r["posicion"]),
                                "score": float(r["score"]),
                            }
                            for r in rows_h
                        }
                except Exception:
                    prev_state = {}

                if len(prev_state) < 50:
                    rows = cursor.execute("SELECT ticker, posicion, score FROM ranking_estado").fetchall()
                    prev_state = {
                        str(row["ticker"]).strip().upper(): {
                            "posicion": int(row["posicion"]),
                            "score": float(row["score"]),
                        }
                        for row in rows
                    }
                if len(prev_state) >= 50:
                    _DAILY_RANKING_BASELINE.clear()
                    _DAILY_RANKING_BASELINE[hoy_iso] = prev_state

            wl_tickers = set(str(t).strip().upper() for t in df_watchlist["ticker"].tolist())
            
            # Si no hay suficientes registros históricos (cold start), evitamos alertar
            # para no saturar al usuario en el primer inicio de la tabla.
            historico_suficiente = len(prev_state) >= 50
            
            # Detectar transiciones para los tickers en la watchlist
            for _, row in df_rank.iterrows():
                ticker_upper = str(row["ticker"]).strip().upper()
                if ticker_upper not in wl_tickers:
                    continue
                
                if ticker_upper in prev_state and historico_suficiente:
                    prev = prev_state[ticker_upper]
                    prev_pos = prev["posicion"]
                    prev_score = prev["score"]
                    
                    cur_pos = row["posicion"]
                    cur_score = row["score_calidad"]
                    
                    empresa = empresas_dict.get(ticker_upper, str(row["ticker"]).replace(".MC", ""))
                    
                    # 1. Entra en el Top 10
                    if prev_pos > 10 and cur_pos <= 10:
                        alertas.append(_alerta(
                            tipo="top_ranking_entrado_10",
                            origen="Ranking",
                            prioridad=50,
                            ticker=row["ticker"],
                            empresa=empresa,
                            mensaje=f"Ha entrado en el Top 10 del Ranking de Calidad (Posición actual: {cur_pos}º).",
                            color="#10b981",
                            bg="rgba(16,185,129,0.06)",
                            estado="positiva",
                            icono="🟢"
                        ))
                    # 2. Sale del Top 10
                    elif prev_pos <= 10 and cur_pos > 10:
                        alertas.append(_alerta(
                            tipo="top_ranking_salido_10",
                            origen="Ranking",
                            prioridad=45,
                            ticker=row["ticker"],
                            empresa=empresa,
                            mensaje=f"Ha salido del Top 10 del Ranking de Calidad (Posición actual: {cur_pos}º).",
                            color="#f59e0b",
                            bg="rgba(245,158,11,0.06)",
                            estado="advertencia",
                            icono="🟡"
                        ))
                    
                    # 3. Entra en el Top 5
                    if prev_pos > 5 and cur_pos <= 5:
                        alertas.append(_alerta(
                            tipo="top_ranking_entrado_5",
                            origen="Ranking",
                            prioridad=55,
                            ticker=row["ticker"],
                            empresa=empresa,
                            mensaje=f"Ha entrado en el Top 5 del Ranking de Calidad (Posición actual: {cur_pos}º).",
                            color="#10b981",
                            bg="rgba(16,185,129,0.06)",
                            estado="positiva",
                            icono="🟢"
                        ))
                    
                    # 4. Mejora muchas posiciones (>= 10 posiciones)
                    if prev_pos - cur_pos >= 10:
                        alertas.append(_alerta(
                            tipo="top_ranking_mejora",
                            origen="Ranking",
                            prioridad=40,
                            ticker=row["ticker"],
                            empresa=empresa,
                            mensaje=f"Ha subido {prev_pos - cur_pos} posiciones en el Ranking de Calidad (Posición actual: {cur_pos}º).",
                            color="#10b981",
                            bg="rgba(16,185,129,0.06)",
                            estado="positiva",
                            icono="🟢"
                        ))
                    # 5. Empeora muchas posiciones (>= 10 posiciones)
                    elif cur_pos - prev_pos >= 10:
                        alertas.append(_alerta(
                            tipo="top_ranking_empeora",
                            origen="Ranking",
                            prioridad=35,
                            ticker=row["ticker"],
                            empresa=empresa,
                            mensaje=f"Ha bajado {cur_pos - prev_pos} posiciones en el Ranking de Calidad (Posición actual: {cur_pos}º).",
                            color="#f59e0b",
                            bg="rgba(245,158,11,0.06)",
                            estado="advertencia",
                            icono="🟡"
                        ))
                    
                    # 6. Entra en zona excelente (score >= 80)
                    if prev_score < 80.0 and cur_score >= 80.0:
                        alertas.append(_alerta(
                            tipo="top_ranking_excelente",
                            origen="Ranking",
                            prioridad=65,
                            ticker=row["ticker"],
                            empresa=empresa,
                            mensaje=f"Ha entrado en la Zona Excelente del Ranking con un score de {cur_score:.1f}.",
                            color="#10b981",
                            bg="rgba(16,185,129,0.06)",
                            estado="positiva",
                            icono="🟢"
                        ))

            # Persistir el estado actual una única vez por día (al fijar la línea base diaria)
            # o en cold start, manteniendo intacta la línea base en memoria para reruns.
            if not ya_en_memoria:
                tickers_activos = [str(r["ticker"]).strip().upper() for _, r in df_rank.iterrows()]
                if tickers_activos:
                    placeholders = ",".join("?" for _ in tickers_activos)
                    cursor.execute(
                        f"DELETE FROM ranking_estado WHERE UPPER(ticker) NOT IN ({placeholders})",
                        tickers_activos,
                    )
                for _, row in df_rank.iterrows():
                    cursor.execute("""
                        INSERT OR REPLACE INTO ranking_estado (ticker, posicion, score, updated_at)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    """, (str(row["ticker"]).strip().upper(), int(row["posicion"]), float(row["score_calidad"])))
                conn.commit()
                if not historico_suficiente:
                    _DAILY_RANKING_BASELINE[hoy_iso] = {
                        str(row["ticker"]).strip().upper(): {
                            "posicion": int(row["posicion"]),
                            "score": float(row["score_calidad"]),
                        }
                        for _, row in df_rank.iterrows()
                    }
            
        finally:
            conn.close()
            
    except Exception as e:
        print(f"Error en alertas de ranking: {e}")

    return alertas


def obtener_ranking_baseline_diario() -> dict[str, int]:
    """
    Devuelve el mapa {TICKER: posicion} del día anterior para calcular deltas 'Cambio'
    en Ranking de forma coherente con las alertas (sin comparar hoy contra hoy).
    """
    import datetime as _dt
    hoy_iso = _dt.date.today().isoformat()
    if hoy_iso in _DAILY_RANKING_BASELINE:
        return {k: int(v["posicion"]) for k, v in _DAILY_RANKING_BASELINE[hoy_iso].items()}

    try:
        from database import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            try:
                row_f = cur.execute(
                    "SELECT MAX(fecha) FROM ranking_historico WHERE fecha < ?",
                    (hoy_iso,),
                ).fetchone()
                if row_f and row_f[0]:
                    rows_h = cur.execute(
                        "SELECT ticker, posicion FROM ranking_historico WHERE fecha = ?",
                        (row_f[0],),
                    ).fetchall()
                    if len(rows_h) >= 50:
                        return {str(r["ticker"]).strip().upper(): int(r["posicion"]) for r in rows_h}
            except Exception:
                pass
            rows = cur.execute("SELECT ticker, posicion FROM ranking_estado").fetchall()
            if rows:
                return {str(r["ticker"]).strip().upper(): int(r["posicion"]) for r in rows}
        finally:
            conn.close()
    except Exception:
        pass
    return {}


def _alertas_sistema(datos_variacion: list) -> list[dict]:
    """
    Genera alertas de volatilidad de mercado **direccionales**.

    Reglas (por orden de prioridad, se añaden todas las que apliquen):

    1. Caída crítica (Rojo):   la peor variación diaria <= -UMBRAL_VOLATILIDAD_ALT
       → "Alta volatilidad detectada. Caída de hasta {var:.1f}% en algún activo."

    2. Subida explosiva (Verde): la mejor variación diaria >= +UMBRAL_VOLATILIDAD_ALT
       → "Alta volatilidad detectada. Movimiento alcista de hasta +{var:.1f}% en algún activo."

    3. Volatilidad moderada (Amarillo): |var_max| >= UMBRAL_VOLATILIDAD_MOD
       pero ninguna de las condiciones anteriores se cumple.
    """
    alertas = []
    if not datos_variacion:
        return alertas

    try:
        items_validos = []
        for d in datos_variacion:
            v = d.get("variacion")
            if v is None:
                continue
            vf = float(v)
            if _math.isnan(vf) or _math.isinf(vf):
                continue
            tkr = str(d.get("ticker", "")).strip()
            nom = str(d.get("nombre") or tkr.replace(".MC", "")).strip()
            etq = f"{nom} ({tkr})" if (nom and tkr and nom != tkr) else (tkr or nom or "el mercado")
            items_validos.append((vf, etq))
        if not items_validos:
            return alertas

        peor_var, peor_etq   = min(items_validos, key=lambda x: x[0])
        mejor_var, mejor_etq = max(items_validos, key=lambda x: x[0])
        max_abs, max_etq     = max(items_validos, key=lambda x: abs(x[0]))
        max_abs = abs(max_abs)
    except Exception:
        return alertas

    alerta_critica_generada  = False
    alerta_positiva_generada = False

    # 1. Caída crítica—cualquier activo ha caído >= UMBRAL_VOLATILIDAD_ALT %
    if peor_var <= -_UMBRAL_VOLATILIDAD_ALT:
        alertas.append(_alerta(
            tipo="volatilidad_caida_critica",
            origen="Sistema",
            prioridad=42,
            ticker="MERCADO",
            empresa="Mercado",
            mensaje=(
                f"Alta volatilidad detectada. "
                f"Caída de hasta {abs(peor_var):.1f}% liderada por {peor_etq}."
            ),
            color="#ef4444",
            bg="rgba(239,68,68,0.06)",
            estado="critica",
            icono="🔴",
        ))
        alerta_critica_generada = True

    # 2. Subida explosiva—cualquier activo ha subido >= UMBRAL_VOLATILIDAD_ALT %
    if mejor_var >= _UMBRAL_VOLATILIDAD_ALT:
        alertas.append(_alerta(
            tipo="volatilidad_subida_explosiva",
            origen="Sistema",
            prioridad=41,
            ticker="MERCADO",
            empresa="Mercado",
            mensaje=(
                f"Alta volatilidad detectada. "
                f"Movimiento alcista de hasta +{mejor_var:.1f}% liderado por {mejor_etq}."
            ),
            color="#10b981",
            bg="rgba(16,185,129,0.06)",
            estado="positiva",
            icono="🟢",
        ))
        alerta_positiva_generada = True

    # 3. Volatilidad moderada (solo si no hay alertas extremas)
    if not alerta_critica_generada and not alerta_positiva_generada:
        if max_abs >= _UMBRAL_VOLATILIDAD_MOD:
            alertas.append(_alerta(
                tipo="volatilidad_moderada",
                origen="Sistema",
                prioridad=30,
                ticker="MERCADO",
                empresa="Mercado",
                mensaje=(
                    f"Volatilidad moderada. Variación de hasta {max_abs:.1f}% "
                    f"en {max_etq}. Mantén el plan."
                ),
                color="#f59e0b",
                bg="rgba(245,158,11,0.06)",
                estado="advertencia",
                icono="🟡",
            ))

    return alertas


def _alertas_eventos(eventos_db: list) -> list[dict]:
    """
    Genera alertas de eventos próximos de alta importancia.
    Los eventos de importancia 'alta' que ocurran hoy (prioridad 85) o mañana
    (prioridad 80) se convierten en alertas para el Centro de Alertas.
    """
    alertas = []
    if not eventos_db:
        return alertas

    hoy   = _dt.date.today()
    manana = hoy + _dt.timedelta(days=1)

    for ev in eventos_db:
        if ev.get("importancia") != "alta":
            continue
        fecha_ev = ev.get("fecha")
        if not fecha_ev:
            continue
        # fecha_ev puede llegar como date o string
        if isinstance(fecha_ev, str):
            try:
                fecha_ev = _dt.date.fromisoformat(fecha_ev)
            except Exception:
                continue

        if fecha_ev in (hoy, manana):
            empresa = ev.get("empresa", "")
            ticker  = ev.get("ticker", "")
            evento  = ev.get("evento", "Evento corporativo")
            es_hoy  = fecha_ev == hoy
            alertas.append(_alerta(
                tipo="evento_importante",
                origen="Eventos",
                prioridad=85 if es_hoy else 80,
                ticker=ticker,
                empresa=empresa,
                mensaje=f"Evento {'hoy' if es_hoy else 'mañana'}: {evento}.",
                color="#a78bfa",
                bg="rgba(167,139,250,0.06)",
                estado="informativa",
                icono="🔵",
            ))

    return alertas


# ────────────────────────────────────────────────────────────────────────────
# DEDUPLICACIÓN Y ORDENACIÓN
# ────────────────────────────────────────────────────────────────────────────

def _deduplicar(alertas: list[dict]) -> list[dict]:
    """
    Elimina duplicados: si para el mismo (ticker, tipo) existen varias alertas,
    conserva únicamente la de mayor prioridad.
    """
    mejor: dict[tuple, dict] = {}
    for a in alertas:
        clave = (a["ticker"], a["tipo"])
        if clave not in mejor or a["prioridad"] > mejor[clave]["prioridad"]:
            mejor[clave] = a
    return list(mejor.values())


def _ordenar(alertas: list[dict]) -> list[dict]:
    """Ordena las alertas por prioridad descendente (mayor prioridad primero)."""
    return sorted(alertas, key=lambda x: x["prioridad"], reverse=True)


# ────────────────────────────────────────────────────────────────────────────
# FUNCIÓN PÚBLICA PRINCIPAL
# ────────────────────────────────────────────────────────────────────────────

def obtener_alertas(
    df_watchlist=None,
    posiciones_abiertas: list | None = None,
    datos_variacion: list | None = None,
    empresas_rows: list | None = None,
    eventos_db: list | None = None,
    origen_cartera: str = "Cartera Real",
) -> list[dict]:
    """
    Motor Central de Alertas — único punto de entrada para obtener alertas.

    Parámetros:
        df_watchlist       DataFrame de la watchlist (con columnas ticker, precio_objetivo,
                           tipo_objetivo y opcionalmente _nombre).
        posiciones_abiertas Lista de dicts de posiciones abiertas de la Cartera Real.
        datos_variacion    Lista de dicts con ticker, precio, variacion, score_calidad.
        empresas_rows      Lista de dicts con ticker y nombre (para resolver nombres).
        eventos_db         Lista de dicts con ticker, empresa, evento, fecha, importancia.

    Retorna:
        Lista de alertas ordenadas por prioridad descendente, sin duplicados.
        Cada alerta es un dict con las claves:
            tipo, origen, prioridad, ticker, empresa, mensaje,
            fecha, color, bg, estado, icono.
    """
    # ── 1. Construir diccionario de precios actuales ─────────────────────────
    precios_dict: dict[str, float] = {}
    if datos_variacion:
        for d in datos_variacion:
            t = str(d.get("ticker", "")).strip().upper()
            p = d.get("precio")
            if t and p is not None:
                try:
                    pf = float(p)
                    if not _math.isnan(pf):
                        precios_dict[t] = pf
                except (TypeError, ValueError):
                    pass

    # ── 2. Construir diccionario de nombres de empresa ───────────────────────
    empresas_dict: dict[str, str] = {}
    if empresas_rows:
        for e in empresas_rows:
            t = str(e.get("ticker", "")).strip().upper()
            n = e.get("nombre", "")
            if t and n:
                empresas_dict[t] = n

    # ── 3. Enriquecer df_watchlist con nombres resueltos ─────────────────────
    if df_watchlist is not None and not df_watchlist.empty:
        df_watchlist = df_watchlist.copy()
        if "_nombre" not in df_watchlist.columns:
            df_watchlist["_nombre"] = df_watchlist["ticker"].apply(
                lambda t: empresas_dict.get(str(t).strip().upper(), str(t).strip().replace(".MC", ""))
            )

    # ── 4. Normalizar ticker en precios_dict (algunos tickers llegan en minúsculas) ─
    precios_dict_norm = {k.strip().upper(): v for k, v in precios_dict.items()}

    # ── 5. Recolectar alertas de cada fuente ─────────────────────────────────
    todas: list[dict] = []

    todas.extend(_alertas_watchlist(df_watchlist, precios_dict_norm))
    todas.extend(_alertas_cartera(posiciones_abiertas or [], precios_dict_norm, origen=origen_cartera))
    todas.extend(_alertas_ranking(datos_variacion or [], df_watchlist, empresas_dict))
    todas.extend(_alertas_sistema(datos_variacion or []))
    todas.extend(_alertas_eventos(eventos_db or []))

    # Cartera Simulada — RESERVADO PARA USO FUTURO
    # todas.extend(_alertas_cartera_simulada(...))

    # ── 6. Deduplicar y ordenar ───────────────────────────────────────────────
    resultado = _ordenar(_deduplicar(todas))

    return resultado
