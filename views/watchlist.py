"""
Módulo Watchlist — StockLab v3.0 (Rediseño Premium)

Panel de seguimiento de inversiones con diseño de plataforma profesional.
Inspirado en TradingView / Bloomberg Terminal / Google Finance.
Tarjetas premium con sparklines, barras de progreso, prioridad automática
y centro de actividad en tiempo real.
"""

import html as _html
import math
import concurrent.futures
import streamlit as st
from database import (
    obtener_watchlist,
    eliminar_de_watchlist,
    agregar_a_watchlist,
    actualizar_watchlist,
    calcular_posiciones_abiertas_filtradas,
    query_all,
)
import market_data
import pandas as pd
from datetime import datetime, timedelta


# ═══════════════════════════════════════════════════════════════════════════
# 🔧 HELPERS — Lógica de datos
# ═══════════════════════════════════════════════════════════════════════════

def _to_float(val, positivo: bool = False) -> float | None:
    """Convierte a float finito o devuelve None si es None/NaN/inf (o <= 0 si positivo=True)."""
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    if positivo and num <= 0:
        return None
    return num

def _obtener_nombres_empresas() -> dict:
    """Crea un diccionario de mapeo Ticker -> Nombre Real."""
    try:
        empresas = query_all("SELECT ticker, nombre FROM empresas")
        return {emp['ticker']: emp['nombre'] for emp in empresas}
    except Exception:
        return {}


def _obtener_sectores_empresas() -> dict:
    """Crea un diccionario de mapeo Ticker -> Sector."""
    try:
        empresas = query_all("SELECT ticker, sector FROM empresas")
        return {emp['ticker']: (emp.get('sector') or 'General') for emp in empresas}
    except Exception:
        return {}


def _obtener_clase_sector(sec: str) -> tuple[str, str]:
    """Retorna la clase CSS de la pastilla y el nombre de sector normalizado."""
    sec_str = sec or "General"
    sec_lower = sec_str.lower()
    if any(k in sec_lower for k in ["tecno", "software", "comunic", "tech"]):
        return "sec-tech", sec_str
    if any(k in sec_lower for k in ["finan", "banc", "segur", "bank"]):
        return "sec-fin", sec_str
    if any(k in sec_lower for k in ["salud", "farma", "biotec", "health"]):
        return "sec-health", sec_str
    if any(k in sec_lower for k in ["energ", "petr", "gas", "renov"]):
        return "sec-energy", sec_str
    if any(k in sec_lower for k in ["consum", "retail", "lujo", "aliment"]):
        return "sec-consume", sec_str
    return "sec-default", sec_str


def _calcular_prioridad(precio_actual, objetivo, tipo_objetivo) -> tuple:
    """
    Calcula la prioridad de seguimiento según la distancia al objetivo.
    Retorna (nivel, label, color_css, glow_css, icon).
    """
    p_act = _to_float(precio_actual, positivo=True)
    p_obj = _to_float(objetivo, positivo=True)
    if p_act is None or p_obj is None:
        return ("baja", "Sin Objetivo", "#64748b", "rgba(75,85,99,0.15)", "")

    if tipo_objetivo == "COMPRA_EN_CAIDA":
        if p_act <= p_obj:
            return ("alcanzado", "Objetivo Alcanzado", "#34d399", "rgba(16,185,129,0.22)", "")
        distancia = ((p_act - p_obj) / p_act) * 100
    else:  # COMPRA_EN_RUPTURA
        if p_act >= p_obj:
            return ("alcanzado", "Objetivo Alcanzado", "#34d399", "rgba(16,185,129,0.22)", "")
        distancia = ((p_obj - p_act) / p_obj) * 100

    if distancia <= 3:
        return ("alta", "Alta Prioridad", "#fbbf24", "rgba(250,204,21,0.18)", "")
    elif distancia <= 10:
        return ("media", "Media Prioridad", "#f59e0b", "rgba(245,158,11,0.15)", "")
    else:
        return ("baja", "Baja Prioridad", "#64748b", "rgba(100,116,139,0.12)", "")


def _calcular_progreso_objetivo(precio_actual, precio_adicion, objetivo, tipo_objetivo) -> dict:
    """
    Calcula la barra de progreso hacia el objetivo basada en la proximidad.
    Retorna dict con pct_progreso, distancia_restante, texto.
    """
    resultado = {"pct_progreso": 0.0, "distancia_restante": None, "texto": "", "alcanzado": False}

    p_act = _to_float(precio_actual, positivo=True)
    p_obj = _to_float(objetivo, positivo=True)
    if p_act is None or p_obj is None:
        return resultado

    if tipo_objetivo == "COMPRA_EN_CAIDA":
        distancia = ((p_act - p_obj) / p_act) * 100
        if distancia <= 0:
            resultado["alcanzado"] = True
            resultado["pct_progreso"] = 100.0
            resultado["texto"] = "Objetivo alcanzado"
        else:
            resultado["distancia_restante"] = distancia
            resultado["pct_progreso"] = max(0.0, min(100.0, 100.0 - distancia))
            resultado["texto"] = f"Faltan {distancia:.1f}% para bajar al objetivo"
    else:  # COMPRA_EN_RUPTURA
        distancia = ((p_obj - p_act) / p_obj) * 100
        if distancia <= 0:
            resultado["alcanzado"] = True
            resultado["pct_progreso"] = 100.0
            resultado["texto"] = "Objetivo de ruptura alcanzado"
        else:
            resultado["distancia_restante"] = distancia
            resultado["pct_progreso"] = max(0.0, min(100.0, 100.0 - distancia))
            resultado["texto"] = f"Faltan {distancia:.1f}% para romper el objetivo"

    return resultado



def _calcular_metricas_dashboard(df: pd.DataFrame) -> dict:
    """Calcula las métricas del panel superior con rentabilidad media incluida."""
    metricas = {
        "total": len(df),
        "objetivos_tiro": 0,
        "objetivos_alcanzados": 0,
        "añadidas_mes": 0,
        "rentabilidad_media": None,
        "alta_prioridad": 0,
    }

    if df.empty:
        return metricas

    fecha_limite = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if "fecha_adicion" in df.columns:
        metricas["añadidas_mes"] = len(df[df["fecha_adicion"] >= fecha_limite])
    else:
        metricas["añadidas_mes"] = 0

    rentabilidades = []
    for _, row in df.iterrows():
        p_act = _to_float(row.get("precio_actual"), positivo=True)
        p_add = _to_float(row.get("precio_adicion"), positivo=True)
        p_obj = _to_float(row.get("precio_objetivo"), positivo=True)
        tipo_obj = row.get("tipo_objetivo", "COMPRA_EN_CAIDA")

        if p_act is not None and p_add is not None:
            rent = ((p_act - p_add) / p_add) * 100
            rentabilidades.append(rent)

        if p_act is not None and p_obj is not None:
            nivel, *_ = _calcular_prioridad(p_act, p_obj, tipo_obj)
            if nivel == "alcanzado":
                metricas["objetivos_alcanzados"] += 1
            elif nivel == "alta":
                metricas["objetivos_tiro"] += 1
                metricas["alta_prioridad"] += 1

    if rentabilidades:
        metricas["rentabilidad_media"] = sum(rentabilidades) / len(rentabilidades)

    return metricas


def _obtener_precios_actuales(df_watchlist: pd.DataFrame) -> pd.DataFrame:
    """Descarga precios actuales y variación diaria en paralelo preservando el orden."""
    tickers = df_watchlist["ticker"].tolist()
    resultados = {}

    def _fetch(tk):
        try:
            datos = market_data.get_stock_data(tk) or {}
            return tk, datos.get("precio_actual"), datos.get("variacion_diaria")
        except Exception:
            return tk, None, None

    if len(tickers) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(tickers))) as ex:
            for tk, p, v in ex.map(_fetch, tickers):
                resultados[tk] = (p, v)
    else:
        for tk in tickers:
            _, p, v = _fetch(tk)
            resultados[tk] = (p, v)

    df_result = df_watchlist.copy()
    df_result["precio_actual"] = [resultados.get(t, (None, None))[0] for t in tickers]
    df_result["variacion_diaria"] = [resultados.get(t, (None, None))[1] for t in tickers]
    return df_result


def _obtener_contexto_integrado() -> tuple[dict, set]:
    """
    Lee en local (0 llamadas de red):
    - mapa_calidad: {TICKER_UPPER: score_calidad_v85} desde `ranking_estado`
    - tickers_cartera: conjunto de tickers con posición abierta en SIMULADA o REAL
    """
    mapa_calidad = {}
    try:
        rows = query_all("SELECT UPPER(ticker) AS tk, score FROM ranking_estado")
        mapa_calidad = {str(r["tk"]): float(r["score"]) for r in rows if r.get("score") is not None}
    except Exception:
        mapa_calidad = {}

    tickers_cartera = set()
    for tipo_c in ("SIMULADA", "REAL"):
        try:
            for pos in calcular_posiciones_abiertas_filtradas(tipo_c) or []:
                tk = str(pos.get("ticker") or "").strip().upper()
                if tk:
                    tickers_cartera.add(tk)
        except Exception:
            pass

    return mapa_calidad, tickers_cartera


def _validar_coherencia_objetivo(precio_actual, objetivo, tipo_objetivo: str) -> tuple[str | None, str | None]:
    """
    Evalúa la coherencia entre el precio objetivo, la cotización actual y la estrategia.
    Retorna `(aviso_coherencia, texto_distancia)`.
    """
    p_act = _to_float(precio_actual, positivo=True)
    p_obj = _to_float(objetivo, positivo=True)
    if p_act is None or p_obj is None:
        return None, None

    if tipo_objetivo == "COMPRA_EN_CAIDA":
        if p_obj >= p_act:
            return (
                f"En «Compra en caída» el objetivo suele ser inferior al precio actual ({p_act:.2f} €). "
                f"Con {p_obj:.2f} € se marcará inmediatamente como Objetivo Alcanzado.",
                None,
            )
        dist = ((p_act - p_obj) / p_act) * 100.0
        return None, f"Distancia actual: -{dist:.1f}% (bajando desde {p_act:.2f} € hasta {p_obj:.2f} €)"
    else:
        if p_obj <= p_act:
            return (
                f"En «Compra por ruptura» el objetivo suele ser superior al precio actual ({p_act:.2f} €). "
                f"Con {p_obj:.2f} € se marcará inmediatamente como Objetivo Alcanzado.",
                None,
            )
        dist = ((p_obj - p_act) / p_obj) * 100.0
        return None, f"Distancia actual: +{dist:.1f}% (subiendo desde {p_act:.2f} € hasta {p_obj:.2f} €)"


def _limpiar_modal_wl():
    st.session_state.pop("wl_editar_item", None)


@st.dialog("Editar Seguimiento", width="medium", on_dismiss=_limpiar_modal_wl)
def _dialog_editar_watchlist():
    """Modal centrado para editar precio objetivo, estrategia y motivo sin perder el histórico."""
    item = st.session_state.get("wl_editar_item")
    if not item:
        return

    st.markdown(
        """
        <style>
            div[data-testid="stDialog"] > div {
                background: #111723 !important;
                border: 1px solid rgba(148, 163, 184, 0.14) !important;
                border-radius: 16px !important;
            }
            div[data-testid="stDialog"] h2 {
                color: #ffffff !important;
                font-family: 'Inter', sans-serif !important;
                font-weight: 700 !important;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    tk = item["ticker"]
    nombre = item["nombre"]
    p_act = _to_float(item.get("precio_actual"), positivo=True)
    p_add = _to_float(item.get("precio_adicion"), positivo=True)
    p_obj_orig = _to_float(item.get("precio_objetivo"), positivo=True) or 0.0
    tipo_orig = item.get("tipo_objetivo") or "COMPRA_EN_CAIDA"
    motivo_orig = item.get("motivo") or ""

    p_act_txt = f"{p_act:.2f} €" if p_act is not None else "N/A"
    p_add_txt = f"{p_add:.2f} €" if p_add is not None else "N/A"
    st.info(f"**{tk} - {nombre}** · Cotización actual: **{p_act_txt}** · Añadida a: **{p_add_txt}**")

    nuevo_obj = st.number_input(
        "PRECIO OBJETIVO (€ · 0 PARA SIN OBJETIVO)",
        min_value=0.0,
        value=float(round(p_obj_orig, 4)),
        step=0.25,
        format="%.2f",
        key=f"wl_edit_obj_{tk}",
    )

    opciones_estrat = ["COMPRA_EN_CAIDA", "COMPRA_EN_RUPTURA"]
    idx_est = 0 if tipo_orig == "COMPRA_EN_CAIDA" else 1
    nueva_estrat = st.radio(
        "ESTRATEGIA DE OBJETIVO",
        options=opciones_estrat,
        index=idx_est,
        format_func=lambda x: "Comprar cuando BAJE hasta este precio" if x == "COMPRA_EN_CAIDA" else "Comprar cuando SUBA (Ruptura)",
        horizontal=True,
        key=f"wl_edit_estrat_{tk}",
    )

    aviso_coh, dist_coh = _validar_coherencia_objetivo(p_act, nuevo_obj if nuevo_obj > 0 else None, nueva_estrat)
    if aviso_coh:
        st.warning(aviso_coh)
    elif dist_coh:
        st.caption(dist_coh)

    nuevo_motivo = st.text_input(
        "MOTIVO DE SEGUIMIENTO",
        value=motivo_orig,
        placeholder="Ej: Esperar corrección, seguimiento de dividendos...",
        key=f"wl_edit_motivo_{tk}",
    )

    c_save, c_cancel = st.columns(2)
    with c_save:
        if st.button("Guardar cambios", type="primary", use_container_width=True, key=f"wl_edit_save_{tk}"):
            val_obj = float(nuevo_obj) if nuevo_obj > 0 else None
            motivo_final = (nuevo_motivo or "").strip() or "Sin motivo especificado"
            if actualizar_watchlist(tk, val_obj, nueva_estrat, motivo_final):
                st.toast(f"Watchlist actualizada: **{tk}**")
                _limpiar_modal_wl()
                st.rerun()
            else:
                st.error("No se pudo actualizar el seguimiento en la base de datos.")
    with c_cancel:
        if st.button("Cancelar", use_container_width=True, key=f"wl_edit_cancel_{tk}"):
            _limpiar_modal_wl()
            st.rerun()




# ═══════════════════════════════════════════════════════════════════════════
# 🎨 RENDER COMPONENTS — Secciones HTML premium
# ═══════════════════════════════════════════════════════════════════════════


def _render_header_kpis(metricas: dict):
    """Renderiza la cabecera de KPIs como dashboard HTML premium."""
    rent = metricas.get("rentabilidad_media")
    rent_str = f"{'+' if rent >= 0 else ''}{rent:.1f}%" if rent is not None else "—"
    rent_color = "#10b981" if (rent or 0) >= 0 else "#ef4444"

    st.markdown(f"""
<div class="sl-kpi-grid" style="--sl-columns:4;">

  <div class="sl-kpi-card" style="--sl-kpi-accent:#38bdf8;">
    <div class="sl-kpi-value">
      {metricas['total']}
    </div>
    <div class="sl-kpi-label">
      Empresas en Radar
    </div>
    <div class="sl-kpi-caption">Monitorizadas activamente</div>
  </div>

  <div class="sl-kpi-card" style="--sl-kpi-accent:#fbbf24;">
    <div class="sl-kpi-value">{metricas['objetivos_tiro']}</div>
    <div class="sl-kpi-label">
      Objetivos a Tiro
    </div>
    <div class="sl-kpi-caption">Distancia &lt; 3%</div>
  </div>

  <div class="sl-kpi-card" style="--sl-kpi-accent:#34d399;">
    <div class="sl-kpi-value">
      {metricas['objetivos_alcanzados']}
    </div>
    <div class="sl-kpi-label">
      Objetivos Alcanzados
    </div>
    <div class="sl-kpi-caption">Precio objetivo superado</div>
  </div>

  <div class="sl-kpi-card" style="--sl-kpi-accent:{rent_color};">
    <div class="sl-kpi-value">
      {rent_str}
    </div>
    <div class="sl-kpi-label">
      Rentabilidad Media
    </div>
    <div class="sl-kpi-caption">Desde precio de entrada</div>
  </div>

</div>
""", unsafe_allow_html=True)


def _render_activity_feed(df: pd.DataFrame, mapa_nombres: dict):
    """Renderiza el centro de actividad reciente tipo TradingView sin indentación para evitar bugs de markdown."""
    items = []

    for _, row in df.iterrows():
        ticker = row["ticker"]
        nombre = _html.escape(str(mapa_nombres.get(ticker, ticker)))
        p_act = _to_float(row.get("precio_actual"), positivo=True)
        p_obj = _to_float(row.get("precio_objetivo"), positivo=True)
        tipo_obj = row.get("tipo_objetivo", "COMPRA_EN_CAIDA")
        var_dia = _to_float(row.get("variacion_diaria"))

        if p_act is None:
            continue

        nivel, label, color, _, icon = _calcular_prioridad(p_act, p_obj, tipo_obj)

        if nivel == "alcanzado" and p_obj is not None:
            items.append({
                "icon": "",
                "color": "#10b981",
                "bg": "rgba(16,185,129,0.06)",
                "border": "rgba(16,185,129,0.2)",
                "mensaje": f"<b style='color:#e8ecf1'>{nombre}</b> ({ticker}) ha alcanzado el precio objetivo de <b style='color:#34d399'>{p_obj:.2f}€</b>",
                "prioridad": 0,
            })
        elif nivel == "alta" and p_obj is not None:
            prog = _calcular_progreso_objetivo(p_act, row.get("precio_adicion"), p_obj, tipo_obj)
            dist = prog.get("distancia_restante")
            if dist is not None:
                items.append({
                    "icon": "",
                    "color": "#facc15",
                    "bg": "rgba(250,204,21,0.05)",
                    "border": "rgba(250,204,21,0.2)",
                    "mensaje": f"<b style='color:#e8ecf1'>{nombre}</b> ({ticker}) está a sólo <b style='color:#fde047'>{dist:.1f}%</b> del objetivo — Alta prioridad",
                    "prioridad": 1,
                })
        elif nivel == "media" and p_obj is not None:
            prog = _calcular_progreso_objetivo(p_act, row.get("precio_adicion"), p_obj, tipo_obj)
            dist = prog.get("distancia_restante")
            if dist is not None and dist <= 5.0:
                items.append({
                    "icon": "",
                    "color": "#f59e0b",
                    "bg": "rgba(245,158,11,0.05)",
                    "border": "rgba(245,158,11,0.2)",
                    "mensaje": f"<b style='color:#e8ecf1'>{nombre}</b> ({ticker}) se acerca al objetivo ({dist:.1f}% restante)",
                    "prioridad": 2,
                })

        if var_dia is not None and abs(var_dia) >= 2.0:
            dir_color = "#34d399" if var_dia > 0 else "#f87171"
            items.append({
                "icon": "",
                "color": dir_color,
                "bg": f"rgba({'16,185,129' if var_dia > 0 else '239,68,68'},0.04)",
                "border": f"rgba({'16,185,129' if var_dia > 0 else '239,68,68'},0.12)",
                "mensaje": f"<b style='color:#e8ecf1'>{ticker}</b> con variación significativa de <b style='color:{dir_color}'>{var_dia:+.2f}%</b> en la sesión de hoy",
                "prioridad": 3,
            })

    if not items:
        return

    items_sorted = sorted(items, key=lambda x: x["prioridad"])[:6]

    items_html = ""
    for item in items_sorted:
        items_html += (
            f'<div class="wl-activity-row" style="display:flex;align-items:center;gap:12px;padding:13px 16px;'
            f'background:rgba(11, 17, 30, 0.72);border:1px solid {item["border"]};border-left:3px solid {item["color"]};border-radius:10px;'
            f'margin-bottom:8px;transition:transform 200ms var(--sl-ease), border-color 200ms var(--sl-ease), background 200ms var(--sl-ease);">'
            f'<span style="width:7px;height:7px;border-radius:50%;background:{item["color"]};box-shadow:0 0 8px {item["color"]}88;flex-shrink:0;display:inline-block;"></span>'
            f'<span style="font-size:0.86rem;color:#d6e0ee;line-height:1.45;font-family:var(--sl-font-body);padding-top:1px;">{item["mensaje"]}</span>'
            f'</div>'
        )

    activity_html = (
        f'<div class="wl-activity-panel" style="background:var(--sl-surface-glass);border:1px solid var(--sl-border-default);border-radius:var(--sl-radius-lg);padding:22px 24px 16px;margin-bottom:28px;box-shadow:var(--sl-shadow-card);position:relative;overflow:hidden;">'
        f'<div style="position:absolute;top:0;left:16%;right:16%;height:1px;background:linear-gradient(90deg,transparent,rgba(56,189,248,0.48),transparent);"></div>'
        f'<div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.10em;color:#7dd3fc;margin-bottom:14px;display:flex;align-items:center;gap:8px;font-family:var(--sl-font-display);">'
        f'<span style="width:7px;height:7px;background:#38bdf8;border-radius:50%;display:inline-block;box-shadow:0 0 10px rgba(56,189,248,0.85);"></span>'
        f'Actividad Reciente'
        f'</div>'
        f'{items_html}'
        f'</div>'
    )
    st.markdown(activity_html, unsafe_allow_html=True)


def _render_tarjeta_empresa(
    row,
    nombre_real: str,
    mapa_nombres: dict,
    idx: int,
    sector: str = "General",
    score_calidad: float | None = None,
    en_cartera: bool = False,
):
    """
    Renderiza una tarjeta premium para una empresa de la watchlist.
    Incluye barra de progreso, badges de estado/prioridad y contexto integrado (Calidad V8.5 + En Cartera).
    """
    ticker = row["ticker"]
    sec_cls, sec_txt = _obtener_clase_sector(sector)
    sec_txt_esc = _html.escape(str(sec_txt))
    nombre_esc = _html.escape(str(nombre_real))
    precio_adicion = _to_float(row.get("precio_adicion"), positivo=True)
    precio_actual = _to_float(row.get("precio_actual"), positivo=True)
    objetivo = _to_float(row.get("precio_objetivo"), positivo=True)
    motivo = _html.escape(str(row.get("motivo") or "Sin motivo especificado"))
    tipo_obj = row.get("tipo_objetivo", "COMPRA_EN_CAIDA")
    fecha_adicion = row.get("fecha_adicion", "")
    variacion = _to_float(row.get("variacion_diaria"))

    # ── Cálculos de prioridad y estado ──────────────────────────────────
    nivel, label_prioridad, color_prioridad, glow_prioridad, icon_prioridad = _calcular_prioridad(
        precio_actual, objetivo, tipo_obj
    )

    # ── Variación desde adición ──────────────────────────────────────────
    if precio_actual is not None and precio_adicion is not None:
        var_adicion = ((precio_actual - precio_adicion) / precio_adicion) * 100
        var_str = f"{'+' if var_adicion >= 0 else ''}{var_adicion:.2f}%"
        var_color = "#10b981" if var_adicion >= 0 else "#ef4444"
    else:
        var_str, var_color = "N/A", "#64748b"

    # ── Variación diaria ─────────────────────────────────────────────────
    if variacion is not None:
        var_dia_str = f"{'+' if variacion >= 0 else ''}{variacion:.2f}%"
        var_dia_color = "#10b981" if variacion >= 0 else "#ef4444"
    else:
        var_dia_str, var_dia_color = "—", "#64748b"

    # ── Precios formateados (protegidos contra None/NaN) ─────────────────
    precio_str = f"{precio_actual:.2f} €" if precio_actual is not None else "—"
    objetivo_str = f"{objetivo:.2f} €" if objetivo is not None else "—"
    precio_adicion_str = f"{precio_adicion:.2f} &#8364;" if precio_adicion is not None else "—"

    # ── Barra de progreso ────────────────────────────────────────────────
    progreso = _calcular_progreso_objetivo(precio_actual, precio_adicion, objetivo, tipo_obj)
    pct = progreso["pct_progreso"]
    texto_distancia = progreso["texto"]

    # Color de la barra según estado
    if nivel == "alcanzado":
        barra_color = "linear-gradient(90deg, #10b981, #34d399)"
    elif nivel == "alta":
        barra_color = "linear-gradient(90deg, #facc15, #fde047)"
    elif nivel == "media":
        barra_color = "linear-gradient(90deg, #f59e0b, #fcd34d)"
    else:
        barra_color = "linear-gradient(90deg, #4b5563, #6b7280)"

    # ── Badge de estrategia ──────────────────────────────────────────────
    if nivel == "alcanzado":
        badge_texto = "Objetivo Alcanzado"
        badge_bg = "rgba(16,185,129,0.12)"
        badge_border = "rgba(16,185,129,0.35)"
        badge_color = "#34d399"
    elif tipo_obj == "COMPRA_EN_CAIDA":
        badge_texto = "Esperando corrección"
        badge_bg = "rgba(56,189,248,0.10)"
        badge_border = "rgba(56,189,248,0.24)"
        badge_color = "#7dd3fc"
    else:
        badge_texto = "Compra por ruptura"
        badge_bg = "rgba(139,92,246,0.10)"
        badge_border = "rgba(139,92,246,0.24)"
        badge_color = "#a78bfa"

    # ── Días en seguimiento ──────────────────────────────────────────────
    # ── Días en seguimiento ──────────────────────────────────────────────
    try:
        try:
            fecha_dt = datetime.strptime(fecha_adicion, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            fecha_dt = datetime.strptime(fecha_adicion, "%Y-%m-%d")
        
        diff = datetime.now() - fecha_dt
        dias = diff.days
        segundos = diff.seconds
        horas = segundos // 3600
        minutos = (segundos % 3600) // 60
        
        if dias > 0:
            dias_str = f"Hace {dias} día{'s' if dias != 1 else ''}"
        elif horas > 0:
            dias_str = f"Hace {horas} hora{'s' if horas != 1 else ''}"
        else:
            dias_str = f"Hace {minutos} minuto{'s' if minutos != 1 else ''}"
    except Exception:
        dias_str = str(fecha_adicion).capitalize()

    # ── Borde sutil dinámico según prioridad ─────────────────────────────
    if nivel == "alcanzado":
        border_color = "rgba(16,185,129,0.28)"
        card_glow = "0 1px 2px rgba(0,0,0,0.24), 0 8px 24px -8px rgba(0,0,0,0.34), inset 0 1px 0 rgba(255,255,255,0.04)"
        hover_glow = "0 4px 14px rgba(0,0,0,0.32), 0 14px 30px -10px rgba(0,0,0,0.42)"
        accent_gradient = "linear-gradient(180deg, #10b981, #34d399)"
    elif nivel == "alta":
        border_color = "rgba(251,191,36,0.26)"
        card_glow = "0 1px 2px rgba(0,0,0,0.24), 0 8px 24px -8px rgba(0,0,0,0.34), inset 0 1px 0 rgba(255,255,255,0.04)"
        hover_glow = "0 4px 14px rgba(0,0,0,0.32), 0 14px 30px -10px rgba(0,0,0,0.42)"
        accent_gradient = "linear-gradient(180deg, #fbbf24, #fde047)"
    elif nivel == "media":
        border_color = "rgba(245,158,11,0.22)"
        card_glow = "0 1px 2px rgba(0,0,0,0.24), 0 8px 24px -8px rgba(0,0,0,0.34), inset 0 1px 0 rgba(255,255,255,0.04)"
        hover_glow = "0 4px 14px rgba(0,0,0,0.32), 0 14px 30px -10px rgba(0,0,0,0.42)"
        accent_gradient = "linear-gradient(180deg, #f59e0b, #fbbf24)"
    else:
        border_color = "rgba(148,163,184,0.12)"
        card_glow = "0 1px 2px rgba(0,0,0,0.24), 0 8px 24px -8px rgba(0,0,0,0.34), inset 0 1px 0 rgba(255,255,255,0.04)"
        hover_glow = "0 4px 14px rgba(0,0,0,0.32), 0 14px 30px -10px rgba(0,0,0,0.42)"
        accent_gradient = "linear-gradient(180deg, rgba(56,189,248,0.45), rgba(96,165,250,0.20))"

    # ── Renderizado de la tarjeta HTML ───────────────────────────────────
    barra_html = ""
    if objetivo is not None and precio_actual is not None:
        barra_html = f"""
      <div style="margin-top:20px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
          <span style="font-size:0.70rem; color:#78879c; text-transform:uppercase; letter-spacing:0.06em; font-weight:600;">
            Progreso al objetivo
          </span>
          <span style="font-size:0.78rem; font-weight:700; color:{color_prioridad}; font-family:'JetBrains Mono',monospace; font-variant-numeric:tabular-nums;">
            {pct:.1f}%
          </span>
        </div>
        <div style="
          background: rgba(148,163,184,0.10);
          border-radius: 999px;
          height: 5px;
          overflow: hidden;
          position: relative;
        ">
          <div style="
            width: {pct:.1f}%;
            height: 100%;
            background: {barra_color};
            border-radius: 999px;
            transition: width 0.5s cubic-bezier(0.2,0.8,0.2,1);
          "></div>
        </div>
        <div style="font-size:0.76rem; color:#78879c; margin-top:8px;">{texto_distancia}</div>
      </div>"""

    # Micro-píldoras de contexto integrado (Calidad Ranking V8.5 + En Cartera)
    meta_pills_html = ""
    if score_calidad is not None:
        if score_calidad >= 70.0:
            q_cls = "wl-q-high"
        elif score_calidad >= 50.0:
            q_cls = "wl-q-mid"
        else:
            q_cls = "wl-q-low"
        meta_pills_html += f'<span class="wl-meta-pill {q_cls}" title="Puntuación de Calidad Empresarial (Ranking StockLab V8.5)">Calidad {score_calidad:.1f}</span>'
    if en_cartera:
        meta_pills_html += '<span class="wl-meta-pill wl-in-port" title="Tienes una posición abierta de este activo en Mi Cartera">En Cartera</span>'

    card_html = (
        f'<div class="wl-asset-card" style="background:var(--sl-surface-glass);border:1px solid {border_color};border-radius:var(--sl-radius-lg);padding:0;margin-bottom:16px;box-shadow:var(--sl-shadow-card);transition:transform 240ms var(--sl-ease), border-color 240ms var(--sl-ease), box-shadow 240ms var(--sl-ease);position:relative;overflow:hidden;font-family:var(--sl-font-body);">'
        f'<div style="position:absolute;top:0;left:16%;right:16%;height:1px;background:linear-gradient(90deg,transparent,{color_prioridad}88,transparent);"></div>'
        f'<div style="position:absolute;top:12%;bottom:12%;left:0;width:3px;background:{accent_gradient};border-radius:0 3px 3px 0;box-shadow:0 0 12px {color_prioridad}66;"></div>'
        f'<div style="padding:24px 26px 24px 30px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px;">'
        f'<div>'
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap;">'
        f'<span style="font-size:1.30rem;font-weight:800;color:#f8fafc;font-family:var(--sl-font-mono);letter-spacing:-0.02em;">{ticker.split(".")[0]}</span>'
        f'<span class="wl-sec-pill {sec_cls}">{sec_txt_esc}</span>'
        f'<span style="background:{badge_bg};border:1px solid {badge_border};color:{badge_color};font-size:0.68rem;font-weight:600;padding:3px 10px;border-radius:999px;letter-spacing:0.01em;">{badge_texto}</span>'
        f'<span style="background:{glow_prioridad};border:1px solid {color_prioridad}38;color:{color_prioridad};font-size:0.62rem;font-weight:700;padding:2px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:0.06em;">{label_prioridad}</span>'
        f'{meta_pills_html}'
        f'</div>'
        f'<div style="font-size:0.85rem;color:var(--sl-text-secondary);font-weight:500;margin-top:2px;">{nombre_esc}</div>'
        f'</div>'
        f'</div>'
        f'<div style="display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px 24px;margin-bottom:20px;">'
        f'<div>'
        f'<div style="font-size:0.66rem;color:var(--sl-text-muted);text-transform:uppercase;letter-spacing:0.08em;font-weight:600;margin-bottom:4px;">Precio Actual</div>'
        f'<div style="font-size:1.95rem;font-weight:700;color:#f8fafc;font-family:var(--sl-font-mono);font-variant-numeric:tabular-nums;letter-spacing:-0.03em;line-height:1;">{precio_str}</div>'
        f'</div>'
        f'<div style="padding-bottom:3px;">'
        f'<div style="font-size:1.02rem;font-weight:600;color:{var_dia_color};font-family:var(--sl-font-mono);font-variant-numeric:tabular-nums;margin-bottom:2px;">{var_dia_str} <span style="font-size:0.74rem;color:var(--sl-text-muted);font-weight:500;">Hoy</span></div>'
        f'<div style="font-size:0.84rem;font-weight:600;color:{var_color};font-family:var(--sl-font-mono);font-variant-numeric:tabular-nums;">{var_str} <span style="font-size:0.70rem;color:var(--sl-text-muted);font-weight:400;">Desde adici&#243;n</span></div>'
        f'</div>'
        f'</div>'
        f'<div class="wl-card-details">'
        f'<div><div style="font-size:0.64rem;color:var(--sl-text-muted);text-transform:uppercase;letter-spacing:0.08em;font-weight:600;margin-bottom:5px;">Objetivo</div><div style="font-size:1.02rem;font-weight:600;color:#e2e8f0;font-family:var(--sl-font-mono);font-variant-numeric:tabular-nums;">{objetivo_str}</div></div>'
        f'<div><div style="font-size:0.64rem;color:var(--sl-text-muted);text-transform:uppercase;letter-spacing:0.08em;font-weight:600;margin-bottom:5px;">A&#241;adida a</div><div style="font-size:1.02rem;font-weight:600;color:#e2e8f0;font-family:var(--sl-font-mono);font-variant-numeric:tabular-nums;">{precio_adicion_str}</div></div>'
        f'<div><div style="font-size:0.64rem;color:var(--sl-text-muted);text-transform:uppercase;letter-spacing:0.08em;font-weight:600;margin-bottom:5px;">Motivo</div><div style="font-size:0.85rem;color:var(--sl-text-secondary);font-style:italic;line-height:1.4;">{motivo}</div></div>'
        f'<div style="text-align:right;"><div style="font-size:0.64rem;color:var(--sl-text-muted);text-transform:uppercase;letter-spacing:0.08em;font-weight:600;margin-bottom:5px;">En seguimiento</div><div style="font-size:0.80rem;color:var(--sl-text-muted);">{dias_str}</div></div>'
        f'</div>'
        f'{barra_html}'
        f'</div>'
        f'</div>'
    )
    st.markdown(card_html, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# 🖥️ SHOW — Función principal de la vista
# ═══════════════════════════════════════════════════════════════════════════

def show():
    # ── CSS Premium Unificado ────────────────────────────────────────────
    st.markdown("""
<style>
    /* ═══════════════════════════════════════════════════════════════════
       🎨 STOCKLAB WATCHLIST v3.0 — Premium Dark Trading Terminal
       Unified with Empresas Design System
       Inspired by TradingView Pro / Bloomberg / Google Finance
       ═══════════════════════════════════════════════════════════════════ */

    /* ── Google Font: cargada centralizadamente en app.py ──────────── */

    /* ── Keyframe Animations ────────────────────────────────────────── */
    @keyframes fade-in-up {
        0%   { opacity: 0; transform: translateY(6px); }
        100% { opacity: 1; transform: translateY(0); }
    }

    @keyframes gradient-shift {
        0%   { background-position: 0% 50%; }
        50%  { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }

    @keyframes pulse-dot {
        0%, 100% { opacity: 1; transform: scale(1); }
        50%       { opacity: 0.5; transform: scale(0.75); }
    }

    /* ── Global Foundation ──────────────────────────────────────────── */
    .block-container {
        padding-top: 1.6rem !important;
        padding-bottom: 4rem !important;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
        max-width: 100% !important;
    }

    /* ── Custom Scrollbar ───────────────────────────────────────────── */
    ::-webkit-scrollbar { width: 6px !important; height: 6px !important; }
    ::-webkit-scrollbar-track { background: #0d121c !important; border-radius: 3px !important; }
    ::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.20) !important; border-radius: 999px !important; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(148, 163, 184, 0.36) !important; }

    /* ═══════════════════════════════════════════════════════════════════
       ✍️ GRADIENT HEADINGS — Bloomberg Premium Typography
       ═══════════════════════════════════════════════════════════════════ */
    h1, .stMarkdown h1 {
        background: none !important;
        -webkit-background-clip: initial !important;
        -webkit-text-fill-color: initial !important;
        background-clip: initial !important;
        color: #f8fafc !important;
        font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
        font-weight: 800 !important;
        letter-spacing: -0.025em !important;
        text-shadow: none !important;
    }

    h2, .stMarkdown h2 {
        background: none !important;
        -webkit-background-clip: initial !important;
        -webkit-text-fill-color: initial !important;
        background-clip: initial !important;
        color: #f1f5f9 !important;
        font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
        font-weight: 700 !important;
        letter-spacing: -0.025em !important;
        text-shadow: none !important;
    }

    h3, .stMarkdown h3 {
        background: none !important;
        -webkit-background-clip: initial !important;
        -webkit-text-fill-color: initial !important;
        background-clip: initial !important;
        color: #e2e8f0 !important;
        font-family: 'Plus Jakarta Sans', 'Inter', sans-serif !important;
        font-weight: 600 !important;
        letter-spacing: -0.025em !important;
        text-shadow: none !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       🔘 BUTTONS — Gradient Action Buttons
       ═══════════════════════════════════════════════════════════════════ */
    section[data-testid="stMain"] div[data-testid="stButton"]:not(.st-key-topbar_nav *) button {
        background: linear-gradient(180deg, #131a29 0%, #101623 100%) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        color: #cbd5e1 !important;
        border-radius: 10px !important;
        padding: 10px 16px !important;
        font-weight: 600 !important;
        font-size: 0.85rem !important;
        font-family: 'Inter', sans-serif !important;
        transition: all 0.2s cubic-bezier(0.25,0.46,0.45,0.94) !important;
        position: relative !important;
        overflow: hidden !important;
        width: 100% !important;
    }

    section[data-testid="stMain"] div[data-testid="stButton"]:not(.st-key-topbar_nav *) button::after {
        content: '' !important;
        position: absolute !important;
        top: 0 !important; left: -100% !important;
        width: 100% !important; height: 100% !important;
        background: linear-gradient(90deg, transparent, rgba(255,255,255,0.04), transparent) !important;
        transition: left 0.5s ease !important;
    }

    section[data-testid="stMain"] div[data-testid="stButton"]:not(.st-key-topbar_nav *) button:hover::after { left: 100% !important; }

    section[data-testid="stMain"] div[data-testid="stButton"]:not(.st-key-topbar_nav *) button:hover {
        background: linear-gradient(180deg, #182133 0%, #131b2b 100%) !important;
        border-color: rgba(56, 189, 248, 0.3) !important;
        color: #ffffff !important;
        box-shadow: 0 4px 16px rgba(0,0,0,0.3) !important;
        transform: translateY(-1px) !important;
    }

    section[data-testid="stMain"] div[data-testid="stButton"]:not(.st-key-topbar_nav *) button:active {
        transform: translateY(0px) !important;
    }

    /* Primary / Buy buttons — Emerald */
    section[data-testid="stMain"] div[data-testid="stButton"]:not(.st-key-topbar_nav *) button[kind="primary"] {
        background: linear-gradient(135deg, #10b981 0%, #059669 50%, #047857 100%) !important;
        background-size: 200% 200% !important;
        border: 1px solid rgba(16,185,129,0.4) !important;
        color: #ffffff !important;
        font-weight: 700 !important;
        box-shadow: 0 4px 16px rgba(0,0,0,0.3) !important;
        animation: none !important;
    }

    section[data-testid="stMain"] div[data-testid="stButton"]:not(.st-key-topbar_nav *) button[kind="primary"]:hover {
        background: linear-gradient(135deg, #34d399 0%, #10b981 50%, #059669 100%) !important;
        background-size: 200% 200% !important;
        border-color: rgba(52,211,153,0.6) !important;
        box-shadow: 0 8px 32px rgba(0,0,0,0.35) !important;
        transform: translateY(-2px) !important;
    }

    /* ═══════════════════════════════════════════════════════════════════
       📑 EXPANDER — Glassmorphic Add Form Panel
       ═══════════════════════════════════════════════════════════════════ */
    div[data-testid="stExpander"] {
        background: linear-gradient(180deg, #131a29 0%, #101623 100%) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 14px !important;
        overflow: hidden !important;
        transition: all 0.3s ease !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
        margin-bottom: 12px !important;
        position: relative !important;
    }

    div[data-testid="stExpander"]:hover {
        border-color: rgba(56, 189, 248, 0.28) !important;
        box-shadow: 0 6px 24px rgba(0,0,0,0.5) !important;
    }

    div[data-testid="stExpander"] summary {
        color: #cbd5e1 !important;
        font-weight: 600 !important;
        font-family: 'Inter', sans-serif !important;
        padding: 16px 20px !important;
    }

    div[data-testid="stExpander"] summary:hover { color: #38bdf8 !important; }

    /* ═══════════════════════════════════════════════════════════════════
       📝 FORM INPUTS — Dark Terminal Style
       ═══════════════════════════════════════════════════════════════════ */
    .stSelectbox [data-baseweb="select"],
    .stMultiSelect [data-baseweb="select"],
    div[data-testid="stNumberInput"] input,
    div[data-testid="stTextInput"] input,
    div[data-testid="stTextArea"] textarea {
        background-color: #0d121c !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 10px !important;
        color: #f1f5f9 !important;
        font-family: 'Inter', sans-serif !important;
        transition: border-color 0.3s ease, box-shadow 0.3s ease !important;
    }

    div[data-testid="stNumberInput"] input:focus,
    div[data-testid="stTextInput"] input:focus,
    div[data-testid="stTextArea"] textarea:focus {
        border-color: #38bdf8 !important;
        box-shadow: none !important;
        outline: none !important;
    }

    [data-baseweb="popover"] {
        background-color: #111723 !important;
        border: 1px solid rgba(56, 189, 248, 0.18) !important;
        border-radius: 12px !important;
        box-shadow: 0 16px 40px rgba(0,0,0,0.85) !important;
    }

    [data-baseweb="menu"] { background-color: #111723 !important; }
    [data-baseweb="menu"] li {
        color: #cbd5e1 !important;
        transition: all 0.2s ease !important;
        font-family: 'Inter', sans-serif !important;
    }
    [data-baseweb="menu"] li:hover {
        background-color: rgba(56, 189, 248, 0.08) !important;
        color: #38bdf8 !important;
    }

    /* Form & Selectbox label typography - 100% harmonized with Ranking & Cartera */
    section[data-testid="stMain"] div[data-testid="stSelectbox"] label[data-testid="stWidgetLabel"],
    .stSelectbox label, .stMultiSelect label,
    div[data-testid="stNumberInput"] label,
    div[data-testid="stTextInput"] label,
    div[data-testid="stTextArea"] label,
    div[data-testid="stCheckbox"] label {
        min-height: 1.5rem !important;
        margin-bottom: 0.25rem !important;
        display: flex !important;
        align-items: center !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.76rem !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
    }

    section[data-testid="stMain"] div[data-testid="stSelectbox"] label[data-testid="stWidgetLabel"] p,
    .stSelectbox label p, .stMultiSelect label p,
    div[data-testid="stNumberInput"] label p,
    div[data-testid="stTextInput"] label p,
    div[data-testid="stTextArea"] label p,
    div[data-testid="stCheckbox"] label p {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.76rem !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
        margin: 0 !important;
        line-height: 1.4 !important;
    }

    div[data-testid="stRadio"] label {
        color: #cbd5e1 !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 500 !important;
    }

    div[data-testid="stRadio"] label[data-baseweb="radio"] {
        text-transform: none !important;
        font-size: 0.88rem !important;
        letter-spacing: normal !important;
    }

    /* ── Separadores — Gradient Dividers ─────────────────────────────── */
    hr {
        border: none !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(148, 163, 184, 0.16), transparent) !important;
        margin: 2.5rem 0 !important;
    }

    /* ── Global Text ─────────────────────────────────────────────────── */
    section[data-testid="stMain"] div[data-testid="stMarkdownContainer"] p {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
        letter-spacing: -0.2px !important;
    }

    .stMarkdown strong { color: #f1f5f9 !important; }

    section[data-testid="stMain"] .stCaption,
    section[data-testid="stMain"] [data-testid="stCaptionContainer"] {
        color: #78879c !important;
        font-family: 'Inter', sans-serif !important;
    }

    /* ── Alerts ──────────────────────────────────────────────────────── */
    div[data-testid="stAlert"] {
        border-radius: 12px !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 500 !important;
    }

    .wl-card-details {
        display: grid;
        grid-template-columns: auto auto minmax(0, 1fr) auto;
        gap: 14px 32px;
        margin-top: 18px;
        padding-top: 18px;
        border-top: 1px solid var(--sl-border-subtle);
        overflow-wrap: anywhere;
    }
    .active-counter-badge {
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        color: #38bdf8 !important;
        background: rgba(56, 189, 248, 0.08) !important;
        border: 1px solid rgba(56, 189, 248, 0.25) !important;
        border-radius: 6px !important;
        padding: 2px 8px !important;
        letter-spacing: 0.5px !important;
        margin-left: 6px !important;
    }

    /* ── Chromatic Sector Pills ───────────────────────────────────────── */
    .wl-sec-pill {
        font-family: 'Inter', sans-serif;
        font-size: 0.58rem;
        font-weight: 700;
        letter-spacing: 0.4px;
        text-transform: uppercase;
        padding: 2px 8px;
        border-radius: 12px;
        display: inline-flex;
        align-items: center;
        white-space: nowrap;
    }
    .sec-tech    { background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }
    .sec-fin     { background: rgba(251, 191, 36, 0.12); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.3); }
    .sec-health  { background: rgba(52, 211, 153, 0.12); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.3); }
    .sec-energy  { background: rgba(251, 146, 60, 0.12); color: #fb923c; border: 1px solid rgba(251, 146, 60, 0.3); }
    .sec-consume { background: rgba(192, 132, 252, 0.12); color: #c084fc; border: 1px solid rgba(192, 132, 252, 0.3); }
    .sec-default { background: rgba(148, 163, 184, 0.10); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.25); }

    /* ── Glowing Gradient Divider Line ─────────────────────────────────── */
    .wl-divider {
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(148, 163, 184, 0.16), transparent);
        margin: 24px 0 20px;
    }

    /* ── Terminal Empty State Shell ──────────────────────────────────── */
    .wl-empty-shell {
        background: radial-gradient(ellipse at top, var(--sl-accent-soft), transparent 60%), var(--sl-bg-surface);
        border: 1px solid var(--sl-border-default);
        border-radius: 16px;
        padding: 48px 32px;
        text-align: center;
        margin: 24px 0;
        position: relative;
        overflow: hidden;
    }
    .wl-empty-icon {
        font-size: 2.6rem;
        margin-bottom: 12px;
    }
    .wl-empty-title {
        font-family: 'Inter', sans-serif;
        font-size: 1.15rem;
        font-weight: 700;
        color: #e2e8f0;
        margin-bottom: 8px;
    }
    .wl-empty-desc {
        font-family: 'Inter', sans-serif;
        font-size: 0.90rem;
        color: var(--sl-text-secondary);
        max-width: 520px;
        margin: 0 auto;
        line-height: 1.5;
    }

    /* ── Watchlist Activity & Asset Card Hover Micro-Interactions ────── */
    .wl-activity-row:hover {
        transform: translateX(3px);
        background: rgba(20, 31, 50, 0.85) !important;
        border-color: rgba(56, 189, 248, 0.28) !important;
    }
    .wl-asset-card:hover {
        transform: translateY(-2px);
        border-color: var(--sl-border-hover) !important;
        box-shadow: var(--sl-shadow-hover) !important;
    }

    /* ── Trading Console Action Buttons ──────────────────────────────── */
    div[class*="st-key-btn_ficha_"] button,
    section[data-testid="stMain"] div[class*="st-key-btn_ficha_"] div.stButton button {
        background: var(--sl-surface-glass) !important;
        border: 1px solid rgba(56, 189, 248, 0.30) !important;
        color: #7dd3fc !important;
        font-weight: 600 !important;
        font-size: 0.80rem !important;
        border-radius: 9px !important;
        padding: 8px 14px !important;
        transition: all 200ms var(--sl-ease) !important;
    }
    div[class*="st-key-btn_ficha_"] button:hover,
    section[data-testid="stMain"] div[class*="st-key-btn_ficha_"] div.stButton button:hover {
        background: rgba(56, 189, 248, 0.14) !important;
        border-color: rgba(56, 189, 248, 0.60) !important;
        box-shadow: 0 0 18px -5px rgba(56, 189, 248, 0.45) !important;
        color: #ffffff !important;
        transform: translateY(-1px) !important;
    }
    div[class*="st-key-btn_comprar_"] button,
    section[data-testid="stMain"] div[class*="st-key-btn_comprar_"] div.stButton button {
        background: linear-gradient(135deg, rgba(16,185,129,0.18) 0%, rgba(5,150,105,0.25) 100%) !important;
        border: 1px solid rgba(52, 211, 153, 0.45) !important;
        color: #34d399 !important;
        font-weight: 700 !important;
        font-size: 0.80rem !important;
        border-radius: 9px !important;
        padding: 8px 14px !important;
        transition: all 200ms var(--sl-ease) !important;
    }
    div[class*="st-key-btn_comprar_"] button:hover,
    section[data-testid="stMain"] div[class*="st-key-btn_comprar_"] div.stButton button:hover {
        background: linear-gradient(135deg, #10b981 0%, #059669 100%) !important;
        border-color: #34d399 !important;
        box-shadow: 0 0 18px -4px rgba(52, 211, 153, 0.50) !important;
        color: #ffffff !important;
        transform: translateY(-1px) !important;
    }
    div[class*="st-key-btn_quitar_"] button,
    section[data-testid="stMain"] div[class*="st-key-btn_quitar_"] div.stButton button {
        background: linear-gradient(145deg, rgba(28, 21, 31, 0.9), rgba(19, 16, 25, 0.95)) !important;
        border: 1px solid rgba(248, 113, 113, 0.28) !important;
        color: #f87171 !important;
        font-weight: 600 !important;
        font-size: 0.80rem !important;
        border-radius: 9px !important;
        padding: 8px 14px !important;
        transition: all 200ms var(--sl-ease) !important;
    }
    div[class*="st-key-btn_quitar_"] button:hover,
    section[data-testid="stMain"] div[class*="st-key-btn_quitar_"] div.stButton button:hover {
        background: rgba(239, 68, 68, 0.16) !important;
        border-color: rgba(248, 113, 113, 0.65) !important;
        box-shadow: 0 0 18px -5px rgba(248, 113, 113, 0.45) !important;
        color: #ffffff !important;
        transform: translateY(-1px) !important;
    }

    div[class*="st-key-btn_editar_wl_"] button,
    section[data-testid="stMain"] div[class*="st-key-btn_editar_wl_"] div.stButton button {
        background: var(--sl-surface-glass) !important;
        border: 1px solid rgba(251, 191, 36, 0.30) !important;
        color: #fbbf24 !important;
        font-weight: 600 !important;
        font-size: 0.80rem !important;
        border-radius: 9px !important;
        padding: 8px 14px !important;
        transition: all 200ms var(--sl-ease) !important;
    }
    div[class*="st-key-btn_editar_wl_"] button:hover,
    section[data-testid="stMain"] div[class*="st-key-btn_editar_wl_"] div.stButton button:hover {
        background: rgba(251, 191, 36, 0.14) !important;
        border-color: rgba(251, 191, 36, 0.60) !important;
        box-shadow: 0 0 18px -5px rgba(251, 191, 36, 0.42) !important;
        color: #ffffff !important;
        transform: translateY(-1px) !important;
    }

    /* ── Integrated Context Micro-Pills (Calidad V8.5 + En Cartera) ──── */
    .wl-meta-pill {
        display: inline-flex;
        align-items: center;
        font-family: var(--sl-font-mono);
        font-size: 0.62rem;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 999px;
        letter-spacing: 0.04em;
    }
    .wl-q-high { background: rgba(16,185,129,0.10); color: #34d399; border: 1px solid rgba(16,185,129,0.28); }
    .wl-q-mid  { background: rgba(56,189,248,0.10); color: #38bdf8; border: 1px solid rgba(56,189,248,0.28); }
    .wl-q-low  { background: rgba(245,158,11,0.10); color: #fbbf24; border: 1px solid rgba(245,158,11,0.28); }
    .wl-in-port {
        background: rgba(99,102,241,0.14);
        color: #a5b4fc;
        border: 1px solid rgba(129,140,248,0.34);
        font-family: 'Inter', sans-serif;
        text-transform: uppercase;
    }

    .st-key-wl_export_csv button {
        height: 40px !important;
        min-height: 40px !important;
        border-radius: 10px !important;
        background: rgba(56, 189, 248, 0.08) !important;
        border: 1px solid rgba(56, 189, 248, 0.28) !important;
        color: #38bdf8 !important;
        font-weight: 600 !important;
        font-size: 0.80rem !important;
    }
    .st-key-wl_export_csv button:hover {
        background: rgba(56, 189, 248, 0.16) !important;
        border-color: rgba(56, 189, 248, 0.55) !important;
        color: #ffffff !important;
    }

    /* ── Tooltip ─────────────────────────────────────────────────────── */
    div[data-testid="stTooltipContent"] {
        background-color: #111723 !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 8px !important;
        color: #cbd5e1 !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.32), 0 12px 28px -10px rgba(0, 0, 0, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
    }

    @media (max-width: 768px) {
        .wl-card-details { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .wl-card-details > :last-child { text-align: left !important; }
        .block-container { padding-top: 1rem !important; }
    }
</style>
""", unsafe_allow_html=True)

    # ── Cabecera de página premium institucional ─────────────────────────
    st.html("""
    <div class="sl-page-header">
        <h1 class="sl-page-title">Watchlist</h1>
        <p class="sl-page-subtitle">Radar de oportunidades y seguimiento táctico en tiempo real</p>
    </div>
    """)

    # Modal centrado de edición (si hay activo seleccionado para editar)
    if "wl_editar_item" in st.session_state:
        _dialog_editar_watchlist()

    # ── 1. FORMULARIO DE ADICIÓN ─────────────────────────────────────────
    with st.expander("Añadir Nueva Empresa a Watchlist", expanded=False):
        try:
            empresas_db = query_all("SELECT ticker, nombre FROM empresas WHERE activa = 1 ORDER BY nombre")
            opciones = [f"{emp['ticker']} - {emp['nombre']}" for emp in empresas_db]
        except Exception:
            opciones = []
            st.warning("No se pudieron cargar las empresas disponibles")

        if opciones:
            col_f1, col_f2 = st.columns([2, 1])
            with col_f1:
                seleccion = st.selectbox("SELECCIONA UNA EMPRESA", options=opciones, index=None, placeholder="Buscar ticker o nombre...")
            with col_f2:
                precio_obj_input = st.number_input("PRECIO OBJETIVO (OPCIONAL)", min_value=0.0, step=0.5, format="%.2f")

            # Mostrar cotización actual en vivo al seleccionar empresa para no fijar objetivos a ciegas
            precio_live_sel = None
            if seleccion:
                tk_preview = seleccion.split(" - ")[0].strip().upper()
                try:
                    d_prev = market_data.get_stock_data(tk_preview) or {}
                    precio_live_sel = _to_float(d_prev.get("precio_actual"), positivo=True)
                    var_live_sel = _to_float(d_prev.get("variacion_diaria"))
                    if precio_live_sel is not None:
                        var_s = f" ({var_live_sel:+.2f}% hoy)" if var_live_sel is not None else ""
                        st.caption(f"Cotización actual de **{tk_preview}**: **{precio_live_sel:.2f} €**{var_s}")
                except Exception:
                    precio_live_sel = None

            motivo_opciones = ["Esperar corrección", "Seguimiento de dividendos", "Ruptura técnica", "Noticias positivas", "Otro..."]
            motivo_sel = st.selectbox("MOTIVO DE SEGUIMIENTO", options=motivo_opciones)
            motivo_texto = st.text_input("ESPECIFICA EL MOTIVO") if motivo_sel == "Otro..." else motivo_sel

            tipo_estrat_label = st.radio(
                "ESTRATEGIA DE OBJETIVO",
                ["📉 Comprar cuando BAJE hasta este precio", "📈 Comprar cuando SUBA (Ruptura)"],
                format_func=lambda x: x.replace("📉 ", "").replace("📈 ", ""),
                index=0,
                horizontal=True
            )
            tipo_estrat = "COMPRA_EN_CAIDA" if "BAJE" in tipo_estrat_label else "COMPRA_EN_RUPTURA"

            if seleccion and precio_live_sel is not None and precio_obj_input > 0:
                aviso_add, dist_add = _validar_coherencia_objetivo(precio_live_sel, precio_obj_input, tipo_estrat)
                if aviso_add:
                    st.warning(aviso_add)
                elif dist_add:
                    st.caption(dist_add)

            if st.button("Añadir a Watchlist", type="primary"):
                motivo_limpio = (motivo_texto or "").strip()
                if not seleccion:
                    st.warning("Selecciona una empresa primero.")
                elif motivo_sel == "Otro..." and not motivo_limpio:
                    st.warning("Por favor, especifica un motivo personalizado.")
                else:
                    ticker_limpio = seleccion.split(" - ")[0].strip().upper()
                    val_obj = precio_obj_input if precio_obj_input > 0 else None
                    try:
                        precio_act = precio_live_sel
                        if precio_act is None:
                            datos = market_data.get_stock_data(ticker_limpio) or {}
                            precio_act = _to_float(datos.get("precio_actual"), positivo=True)
                        if precio_act is not None:
                            if agregar_a_watchlist(ticker_limpio, precio_act, motivo_limpio, val_obj, tipo_estrat):
                                st.success(f"{ticker_limpio} guardado correctamente en tu Watchlist.")
                                st.rerun()
                            else:
                                st.error("Error al guardar en DB")
                        else:
                            st.warning("No se pudo obtener el precio actual.")
                    except Exception as e:
                        st.error(f"Error técnico: {e}")
        else:
            st.info("No hay empresas registradas en la base de datos.")

    # ── 2. CARGAR DATOS ──────────────────────────────────────────────────
    df_raw = obtener_watchlist()

    if df_raw.empty:
        st.markdown("""
        <div class="wl-empty-shell">
            <div class="wl-empty-title">Radar de Watchlist en Espera</div>
            <div class="wl-empty-desc">
                Actualmente no tienes empresas añadidas a tu radar de seguimiento. Despliega el formulario superior para añadir tu primer activo y monitorizar precios objetivo en tiempo real.
            </div>
        </div>
        """, unsafe_allow_html=True)
        return

    mapa_nombres = _obtener_nombres_empresas()
    mapa_sectores = _obtener_sectores_empresas()
    mapa_calidad, tickers_en_cartera = _obtener_contexto_integrado()

    with st.spinner("Sincronizando cotizaciones y objetivos de la Watchlist..."):
        df = _obtener_precios_actuales(df_raw)

    # ── 3. DASHBOARD KPIs ────────────────────────────────────────────────
    metricas = _calcular_metricas_dashboard(df)
    _render_header_kpis(metricas)

    # ── 4. ACTIVIDAD RECIENTE ────────────────────────────────────────────
    _render_activity_feed(df, mapa_nombres)

    st.markdown('<div class="wl-divider"></div>', unsafe_allow_html=True)

    # ── 5. ENRIQUECIMIENTO, FILTROS RÁPIDOS Y ORDENACIÓN PRECISA ─────────
    orden_prioridad = {"alcanzado": 0, "alta": 1, "media": 2, "baja": 3}

    def _enriquecer_fila(r):
        p_act = _to_float(r.get("precio_actual"), positivo=True)
        p_add = _to_float(r.get("precio_adicion"), positivo=True)
        p_obj = _to_float(r.get("precio_objetivo"), positivo=True)
        t_obj = r.get("tipo_objetivo", "COMPRA_EN_CAIDA")
        nivel = _calcular_prioridad(p_act, p_obj, t_obj)[0]
        prog = _calcular_progreso_objetivo(p_act, p_add, p_obj, t_obj)
        dist = 0.0 if prog["alcanzado"] else (prog["distancia_restante"] if prog["distancia_restante"] is not None else 9999.0)
        rent = ((p_act - p_add) / p_add * 100.0) if (p_act is not None and p_add is not None) else -9999.0
        var_d = _to_float(r.get("variacion_diaria"))
        tk_u = str(r.get("ticker") or "").strip().upper()
        cal = mapa_calidad.get(tk_u, -1.0)
        sin_obj = p_obj is None
        return pd.Series({
            "_prioridad_nivel": nivel,
            "_prioridad_orden": orden_prioridad.get(nivel, 3),
            "_distancia": dist,
            "_rent_entrada": rent,
            "_var_dia_sort": var_d if var_d is not None else -9999.0,
            "_calidad": cal,
            "_sin_objetivo": sin_obj,
        })

    df = pd.concat([df.reset_index(drop=True), df.apply(_enriquecer_fila, axis=1)], axis=1)

    # Barra compacta de filtrado, ordenación y exportación CSV
    ctrl1, ctrl2, ctrl3 = st.columns([1.4, 1.5, 1.0], vertical_alignment="bottom")
    with ctrl1:
        filtro_estado = st.selectbox(
            "FILTRAR POR ESTADO",
            options=["Todos", "Objetivo Alcanzado", "Alta Prioridad (<3%)", "En Seguimiento", "Sin Objetivo"],
            index=0,
            key="wl_filtro_estado",
        )
    with ctrl2:
        orden_sel = st.selectbox(
            "ORDENAR POR",
            options=[
                "Prioridad y cercanía",
                "Rentabilidad desde entrada",
                "Variación hoy",
                "Calidad StockLab",
                "Más recientes",
            ],
            index=0,
            key="wl_orden_sel",
        )

    df_view = df.copy()
    if filtro_estado == "Objetivo Alcanzado":
        df_view = df_view[df_view["_prioridad_nivel"] == "alcanzado"]
    elif filtro_estado == "Alta Prioridad (<3%)":
        df_view = df_view[df_view["_prioridad_nivel"] == "alta"]
    elif filtro_estado == "En Seguimiento":
        df_view = df_view[(~df_view["_sin_objetivo"]) & (df_view["_prioridad_nivel"] != "alcanzado")]
    elif filtro_estado == "Sin Objetivo":
        df_view = df_view[df_view["_sin_objetivo"]]

    if orden_sel == "Prioridad y cercanía":
        df_view = df_view.sort_values(["_prioridad_orden", "_distancia", "ticker"], ascending=[True, True, True])
    elif orden_sel == "Rentabilidad desde entrada":
        df_view = df_view.sort_values(["_rent_entrada", "ticker"], ascending=[False, True])
    elif orden_sel == "Variación hoy":
        df_view = df_view.sort_values(["_var_dia_sort", "ticker"], ascending=[False, True])
    elif orden_sel == "Calidad StockLab":
        df_view = df_view.sort_values(["_calidad", "ticker"], ascending=[False, True])
    else:
        df_view = df_view.sort_values(["fecha_adicion", "ticker"], ascending=[False, True])

    df_view = df_view.reset_index(drop=True)

    with ctrl3:
        with st.container(key="wl_export_csv"):
            csv_wl = pd.DataFrame([
                {
                    "Ticker": r["ticker"],
                    "Empresa": mapa_nombres.get(r["ticker"], r["ticker"]),
                    "Sector": mapa_sectores.get(r["ticker"], "General"),
                    "Precio_Actual_EUR": round(float(r["precio_actual"]), 4) if _to_float(r.get("precio_actual"), positivo=True) else "",
                    "Var_Hoy_Pct": round(float(r["variacion_diaria"]), 2) if _to_float(r.get("variacion_diaria")) is not None else "",
                    "Precio_Entrada_EUR": round(float(r["precio_adicion"]), 4) if _to_float(r.get("precio_adicion"), positivo=True) else "",
                    "Precio_Objetivo_EUR": round(float(r["precio_objetivo"]), 4) if _to_float(r.get("precio_objetivo"), positivo=True) else "",
                    "Estrategia": r.get("tipo_objetivo") or "",
                    "Prioridad": r.get("_prioridad_nivel") or "",
                    "Calidad_V85": round(float(r["_calidad"]), 1) if float(r.get("_calidad", -1)) >= 0 else "",
                    "En_Cartera": "Sí" if str(r["ticker"]).strip().upper() in tickers_en_cartera else "No",
                    "Motivo": r.get("motivo") or "",
                    "Fecha_Adicion": r.get("fecha_adicion") or "",
                }
                for _, r in df_view.iterrows()
            ]).to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label=f"Exportar CSV ({len(df_view)})",
                data=csv_wl,
                file_name="stocklab_watchlist.csv",
                mime="text/csv",
                use_container_width=True,
                disabled=(len(df_view) == 0),
                key="wl_btn_download_csv",
            )

    # ── 6. TARJETAS PREMIUM ──────────────────────────────────────────────
    cant_wl = len(df_view)
    badge_txt = f"{cant_wl} {'activa' if cant_wl == 1 else 'activas'}"
    st.markdown(f'<div class="sl-section-title" style="margin-top:12px;">Radar de Seguimiento <span class="active-counter-badge">[ {badge_txt} ]</span> <span style="font-family:var(--sl-font-body);font-size:0.70rem;font-weight:600;color:var(--sl-text-muted);text-transform:uppercase;letter-spacing:0.06em;">{orden_sel}</span></div>', unsafe_allow_html=True)

    if df_view.empty:
        st.markdown("""
        <div class="wl-empty-shell" style="padding:32px 24px;">
            <div class="wl-empty-title" style="font-size:1.0rem;">Sin activos en este filtro</div>
            <div class="wl-empty-desc" style="font-size:0.84rem;">Cambia el filtro de estado superior para ver el resto de empresas de tu Watchlist.</div>
        </div>
        """, unsafe_allow_html=True)
        return

    for idx, row in df_view.iterrows():
        ticker = row["ticker"]
        tk_u = str(ticker).strip().upper()
        nombre_real = mapa_nombres.get(ticker, ticker)
        sector = mapa_sectores.get(ticker, "General")
        sc_cal = mapa_calidad.get(tk_u)
        en_cart = tk_u in tickers_en_cartera

        # Renderizar la tarjeta HTML premium
        _render_tarjeta_empresa(
            row,
            nombre_real,
            mapa_nombres,
            idx,
            sector=sector,
            score_calidad=sc_cal,
            en_cartera=en_cart,
        )

        # Botones de acción (4 controles alineados: Ver Empresa, Editar, Comprar, Quitar)
        col_b1, col_b2, col_b3, col_b4 = st.columns([1, 1, 1, 1])

        def ir_a_detalle_callback(ticker_empresa, nombre_empresa):
            st.session_state["selector_global_empresas"] = f"{ticker_empresa} - {nombre_empresa}"
            st.session_state["menu_activo"] = "🏢 Empresas"

        def comprar_callback(ticker_empresa, precio_emp, fecha_emp):
            st.session_state["compra_preseleccionada"] = ticker_empresa
            st.session_state["accion_activa"] = "comprar"
            precio_f = _to_float(precio_emp, positivo=True)
            if precio_f is not None:
                st.session_state["precio_operacion_input"] = precio_f
            st.session_state["menu_activo"] = "Mi Cartera"

        with col_b1:
            st.button(
                "Ver Empresa",
                key=f"btn_ficha_{ticker}_{idx}",
                use_container_width=True,
                on_click=ir_a_detalle_callback,
                args=(ticker, nombre_real)
            )
        with col_b2:
            if st.button("Editar", key=f"btn_editar_wl_{ticker}_{idx}", use_container_width=True):
                st.session_state["wl_editar_item"] = {
                    "ticker": ticker,
                    "nombre": nombre_real,
                    "precio_actual": row.get("precio_actual"),
                    "precio_adicion": row.get("precio_adicion"),
                    "precio_objetivo": row.get("precio_objetivo"),
                    "tipo_objetivo": row.get("tipo_objetivo") or "COMPRA_EN_CAIDA",
                    "motivo": row.get("motivo") or "",
                }
                st.rerun()
        with col_b3:
            precio_actual_btn = row.get("precio_actual")
            st.button(
                "Comprar",
                key=f"btn_comprar_{ticker}_{idx}",
                use_container_width=True,
                type="primary",
                on_click=comprar_callback,
                args=(ticker, precio_actual_btn, datetime.now().strftime("%Y-%m-%d"))
            )
        with col_b4:
            if st.button("Quitar", key=f"btn_quitar_{ticker}_{idx}", use_container_width=True):
                eliminar_de_watchlist(ticker)
                st.rerun()

        # Separador entre tarjetas (excepto la última)
        if idx < len(df_view) - 1:
            st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)