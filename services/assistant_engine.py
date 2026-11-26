"""
Asistente StockLab — Motor de Intenciones (Opción A)
====================================================
Asistente de preguntas sobre los datos reales de la web, sin LLM externo:
detecta la intención de la pregunta (patrones en español), ejecuta la
consulta correspondiente contra los datos vivos de StockLab y devuelve una
respuesta estructurada para que la vista la renderice.

Contexto esperado (lo construye views/ia.py):
  ctx = {
    'radar':          DataFrame de services.signal_engine.calcular_radar()
    'tickers':        tuple de tickers activos
    'nombre2ticker':  {nombre_visible: ticker}
    'ticker2nombre':  {ticker: nombre_visible}
    'fundamentales':  {ticker: dict fundamental (solo caché en disco)}
  }

Respuesta devuelta por responder():
  {
    'titulo': str,            # cabecera de la respuesta
    'texto':  str,            # cuerpo en Markdown
    'df':     DataFrame|None, # tabla opcional
  }

Arquitectura preparada para enchufar un LLM gratuito (Opción B) como
intérprete: bastaría reemplazar _detectar_intencion manteniendo los handlers.
"""

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from database.connection import query_all

# ── Preguntas sugeridas (chips de la vista) ─────────────────────────────────
PREGUNTAS_SUGERIDAS = [
    "¿Qué empresa tiene mejor momentum?",
    "¿Qué señales alcistas hay hoy?",
    "¿Qué empresas están en sobreventa?",
    "¿Cuáles tienen PER bajo y buen dividendo?",
    "¿Qué próximos dividendos hay?",
    "Resume BBVA",
]

# Pool ampliado: la vista rota una selección de 6 cada día
_POOL_SUGERIDAS = PREGUNTAS_SUGERIDAS + [
    "¿Cómo va mi cartera?",
    "¿Qué tengo en mi watchlist?",
    "¿Qué noticias hay del mercado?",
    "¿Qué empresas son más volátiles?",
    "¿Cuáles son las mejores del ranking?",
    "¿Qué señales bajistas hay hoy?",
]


def preguntas_sugeridas_dia(n: int = 6) -> List[str]:
    """Selección determinista por fecha: rota el pool de sugerencias cada día."""
    from datetime import date
    inicio = date.today().toordinal() % len(_POOL_SUGERIDAS)
    rotado = _POOL_SUGERIDAS[inicio:] + _POOL_SUGERIDAS[:inicio]
    return rotado[:n]


# ═══════════════════════════════════════════════════════════════════
# 🧰 UTILIDADES DE TEXTO Y CONTEXTO
# ═══════════════════════════════════════════════════════════════════

def _norm(texto: str) -> str:
    """Minúsculas, sin acentos, sin signos — para matching robusto."""
    t = (texto or "").lower().strip()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t)


@st.cache_data(ttl=43200, show_spinner=False)
def cargar_fundamentales_disco(tickers: Tuple[str, ...]) -> Dict[str, Dict]:
    """
    Lee los fundamentales cacheados en disco (TTL ignorado, solo lectura
    local — nunca dispara red). Ideal para respuestas instantáneas.
    """
    out: Dict[str, Dict] = {}
    try:
        from services.data_provider import _dp_leer_cache_disco
        for t in tickers:
            try:
                d = _dp_leer_cache_disco(t, ignore_ttl=True)
                if d:
                    out[t] = d
            except Exception:
                continue
    except Exception:
        pass
    return out


# Palabras genéricas de razones sociales que no identifican a una empresa
_STOP_NOMBRES = {
    "banco", "grupo", "sociedad", "corporacion", "compania", "espanola",
    "espanol", "empresa", "inversiones", "internacional", "global",
    "holdings", "europe", "europa", "industria", "industrias", "diseno",
    "servicios", "energia", "activos", "hotels", "hoteles",
}


def _detectar_empresas(pregunta_norm: str, ctx: Dict) -> List[str]:
    """
    Detecta tickers mencionados por:
      1. Nombre completo ('banco santander')
      2. Palabra distintiva del nombre ('santander', 'iberdrola', 'inditex')
      3. Símbolo explícito ('BBVA', 'SAN.MC', 'IBE')
    """
    encontrados: List[str] = []
    nombre2ticker = ctx.get("nombre2ticker", {}) if ctx else {}
    for nombre, ticker in nombre2ticker.items():
        n = _norm(nombre)
        if len(n) >= 4 and n in pregunta_norm:
            if ticker not in encontrados:
                encontrados.append(ticker)
            continue
        for w in n.split():
            if (len(w) >= 5 and w not in _STOP_NOMBRES
                    and re.search(rf"\b{re.escape(w)}\b", pregunta_norm)):
                if ticker not in encontrados:
                    encontrados.append(ticker)
                break
    # Símbolo explícito (BBVA, SAN.MC, IBE...)
    tickers_list = ctx.get("tickers", ()) if ctx else ()
    for simbolo in re.findall(r"\b([a-z0-9]{2,8}(?:\.mc)?)\b", pregunta_norm):
        for ticker in tickers_list:
            base = ticker.lower().replace(".mc", "")
            if simbolo == base or simbolo == ticker.lower():
                if ticker not in encontrados:
                    encontrados.append(ticker)
                break

    # Memoria de contexto: si no hay empresa explícita pero hay pronombres/referencias
    # anafóricas ("su dividendo", "su per", "esta empresa", "y sus noticias", etc.).
    # Evita falsos positivos con el verbo "está" (normalizado a "esta"), expresiones
    # temporales ("esta semana"/"este mes"), preguntas conceptuales y consultas de mercado.
    if not encontrados and hasattr(st, "session_state"):
        ultimo = st.session_state.get("ia_ultimo_ticker")
        if ultimo and (not tickers_list or ultimo in tickers_list):
            p_clean = pregunta_norm.lstrip("¿¡?.,;:! \t\n")
            es_conceptual = p_clean.startswith((
                "que es ", "que son ", "que significa ", "explica ", "explicame ",
                "definicion de ", "como funciona ", "como se calcula ",
                "como leer ", "como interpretar ", "diferencia entre ",
                "como esta definid",
            ))
            tiene_posesivo_claro = bool(re.search(
                r"\b(su|sus|ella|dicha)\b|"
                r"\b(esta\s+(empresa|accion|compania|valor|cotizada)|este\s+(valor|titulo|banco|activo)|la\s+misma\s+empresa|el\s+mismo\s+valor)\b",
                p_clean,
            ))
            es_alcance_general = bool(re.search(
                r"\b(mercado|ibex|empresas|acciones|cuales|mejores|peores|hay|proximos|todas|todos|ranking|cartera|watchlist)\b",
                p_clean,
            ))
            tiene_termino_anaf = bool(re.search(
                r"\b(dividendo|dividendos|per|noticia|noticias|proyeccion|prevision|ficha|grafico|objetivo)\b"
                r"|cuanto cotiza|a cuanto cotiza",
                p_clean,
            ))
            if tiene_posesivo_claro or (not es_conceptual and not es_alcance_general and tiene_termino_anaf):
                encontrados.append(ultimo)

    # Actualizar memoria del último ticker activo consultado
    if encontrados and hasattr(st, "session_state"):
        st.session_state["ia_ultimo_ticker"] = encontrados[0]

    return encontrados


def _f_num(v, dec: int = 2) -> Optional[float]:
    try:
        f = float(v)
        return round(f, dec) if pd.notna(f) else None
    except (TypeError, ValueError):
        return None


def _pct_decimal(v, es_dividendo: bool = False) -> Optional[float]:
    """Normaliza ratios decimales de fundamentales a porcentaje (p. ej. 0.18 -> 18.0, 1.41 -> 141.0)."""
    f = _f_num(v, 4)
    if f is None:
        return None
    if es_dividendo:
        # En algunos feeds de dividendYield puede venir ya en % (ej. 4.56) o decimal (0.0456)
        return round(f * 100.0, 2) if abs(f) < 0.35 else round(f, 2)
    return round(f * 100.0, 2)


def _limpiar_texto_link(texto: str) -> str:
    """Evita que corchetes en titulares de prensa rompan los enlaces Markdown."""
    return (texto or "").replace("[", "(").replace("]", ")").strip()


def _radar_invalido(radar) -> bool:
    return radar is None or not isinstance(radar, pd.DataFrame) or radar.empty or "ticker" not in radar.columns


# ═══════════════════════════════════════════════════════════════════
# 🧠 HANDLERS DE INTENCIÓN
# ═══════════════════════════════════════════════════════════════════

def _ayuda() -> Dict:
    return {
        "titulo": "Esto es lo que puedo hacer por ti",
        "texto": (
            "Soy el agente de IA de StockLab. Consulto **tus datos reales** "
            "(radar, fundamentales, perfil de riesgo, sectores, alertas, cartera, "
            "watchlist y eventos) y **actualidad económica en la web**. Puedes preguntarme:\n\n"
            "- *¿Qué empresa tiene mejor momentum?*\n"
            "- *¿Qué señales alcistas/bajistas hay hoy?*\n"
            "- *¿Qué empresas están en sobreventa?*\n"
            "- *¿Cuáles tienen PER bajo y buen dividendo?*\n"
            "- *¿Cómo está el sector bancario?*\n"
            "- *Analiza el riesgo y volatilidad de BBVA*\n"
            "- *¿Qué alertas tengo activas hoy?*\n"
            "- *¿Cómo va mi cartera real / simulada?*\n"
            "- *¿Qué noticias recientes hay del BCE o del IBEX 35?*"
        ),
        "df": None,
    }


def _top_momentum(ctx: Dict) -> Dict:
    radar = ctx.get("radar")
    if _radar_invalido(radar):
        return {"titulo": "Empresas con mejor momentum", "texto": "Sin datos de mercado en el radar ahora mismo.", "df": None}
    top = radar.nlargest(5, "mom20")
    df = pd.DataFrame({
        "Empresa": top["empresa"].values,
        "Mom. 20d (%)": top["mom20"].values,
        "Mom. 5d (%)": top["mom5"].values,
        "Señal 1S": top["senal_1S"].values,
        "Est. 1S (%)": top["pct_1S"].values,
    })
    mejor = top.iloc[0]
    return {
        "titulo": "Empresas con mejor momentum",
        "texto": (f"La de mayor momentum a 20 días es **{mejor['empresa']}** "
                  f"({mejor['mom20']:+.1f}%). Estas son las 5 primeras:"),
        "df": df,
    }


def _sobreventa(ctx: Dict) -> Dict:
    radar = ctx.get("radar")
    if _radar_invalido(radar):
        return {"titulo": "Sobreventa (RSI < 35)", "texto": "Sin datos de mercado en el radar ahora mismo.", "df": None}
    sel = radar[radar["rsi"] < 35].nsmallest(8, "rsi")
    if sel.empty:
        return {"titulo": "Sobreventa (RSI < 35)",
                "texto": "Ahora mismo **ninguna empresa está en sobreventa** "
                         "(RSI < 35). El mercado no ofrece rebotes técnicos claros.",
                "df": None}
    df = pd.DataFrame({
        "Empresa": sel["empresa"].values,
        "RSI": sel["rsi"].values,
        "Var. día (%)": sel["var_dia"].values,
        "Señal 1D": sel["senal_1D"].values,
    })
    return {
        "titulo": "Empresas en sobreventa (RSI < 35)",
        "texto": (f"Hay **{len(sel)} empresa(s)** con RSI bajo. Un RSI < 30 "
                  f"suele anticipar rebotes técnicos a 1-3 días:"),
        "df": df,
    }


def _sobrecompra(ctx: Dict) -> Dict:
    radar = ctx.get("radar")
    if _radar_invalido(radar):
        return {"titulo": "Sobrecompra (RSI > 65)", "texto": "Sin datos de mercado en el radar ahora mismo.", "df": None}
    sel = radar[radar["rsi"] > 65].nlargest(8, "rsi")
    if sel.empty:
        return {"titulo": "Sobrecompra (RSI > 65)",
                "texto": "No hay empresas en sobrecompra (RSI > 65) en este momento.",
                "df": None}
    df = pd.DataFrame({
        "Empresa": sel["empresa"].values,
        "RSI": sel["rsi"].values,
        "Mom. 20d (%)": sel["mom20"].values,
        "Señal 1D": sel["senal_1D"].values,
    })
    return {
        "titulo": "Empresas en sobrecompra (RSI > 65)",
        "texto": "Estas empresas muestran RSI alto; riesgo de pausa o recorte a corto plazo:",
        "df": df,
    }


def _senales(ctx: Dict, tipo: str) -> Dict:
    radar = ctx.get("radar")
    if _radar_invalido(radar):
        return {"titulo": f"Señales {tipo.lower()}s", "texto": "Sin datos de mercado en el radar ahora mismo.", "df": None}
    sel = radar[radar["senal_1S"] == tipo].copy()
    sel = sel.reindex(sel["score_1S"].abs().sort_values(ascending=False).index).head(12)
    if sel.empty:
        return {"titulo": f"Señales {tipo.lower()}s",
                "texto": f"Hoy no hay señales **{tipo.lower()}s** a 1 semana en el radar.",
                "df": None}
    df = pd.DataFrame({
        "Empresa": sel["empresa"].values,
        "Precio (€)": sel["precio"].round(2).values,
        "Est. 1D (%)": sel["pct_1D"].values,
        "Est. 1S (%)": sel["pct_1S"].values,
        "Est. 1M (%)": sel["pct_1M"].values,
        "Confianza (%)": sel["conf_1S"].values,
    })
    return {
        "titulo": f"Señales {tipo.lower()}s a 1 semana",
        "texto": (f"El radar detecta **{len(sel)} señal(es) {tipo.lower()}s** "
                  f"con horizonte semanal. Ordenadas por fuerza de señal:"),
        "df": df,
    }


def _value(ctx: Dict) -> Dict:
    fund = ctx["fundamentales"]
    filas = []
    for ticker, d in fund.items():
        pe = _f_num(d.get("trailingPE") or d.get("forwardPE"))
        yld = _pct_decimal(d.get("dividendYield"), es_dividendo=True)
        roe = _pct_decimal(d.get("returnOnEquity"))
        if pe and 0 < pe < 18 and (yld or 0) >= 2.0:
            filas.append((ctx["ticker2nombre"].get(ticker, ticker),
                          pe, yld, roe))
    if not filas:
        return {"titulo": "Value (PER bajo + dividendo)",
                "texto": ("No encuentro empresas con PER < 18 y dividendo ≥ 2% "
                          "en los fundamentales cacheados. Visita las vistas "
                          "Empresas/Ranking para ampliar la caché."),
                "df": None}
    filas.sort(key=lambda x: x[1])
    df = pd.DataFrame(filas[:10], columns=["Empresa", "PER", "Div. (%)", "ROE (%)"])
    return {
        "titulo": "PER bajo con buen dividendo",
        "texto": "Empresas con **PER < 18** y **rentabilidad por dividendo ≥ 2%**, ordenadas por PER:",
        "df": df,
    }


def _top_dividendos(ctx: Dict) -> Dict:
    fund = ctx["fundamentales"]
    filas = []
    for ticker, d in fund.items():
        yld = _pct_decimal(d.get("dividendYield"), es_dividendo=True)
        if yld and yld > 0:
            filas.append((ctx["ticker2nombre"].get(ticker, ticker),
                          yld, _f_num(d.get("trailingPE"))))
    if not filas:
        return {"titulo": "Mayores dividendos",
                "texto": "No hay datos de dividendo en caché todavía.",
                "df": None}
    filas.sort(key=lambda x: -x[1])
    df = pd.DataFrame(filas[:10], columns=["Empresa", "Div. (%)", "PER"])
    return {
        "titulo": "Mayores rentabilidades por dividendo",
        "texto": "Top 10 por rentabilidad por dividendo (según fundamentales en caché):",
        "df": df,
    }


def _proximos_eventos() -> Dict:
    filas = query_all(
        "SELECT empresa, ticker, evento, fecha, tipo FROM eventos "
        "WHERE fecha >= date('now') ORDER BY fecha ASC LIMIT 12"
    )
    titulo = "Próximos eventos y dividendos"
    if not filas:
        filas = query_all(
            "SELECT empresa, ticker, evento, fecha, tipo FROM eventos "
            "ORDER BY fecha DESC LIMIT 12"
        )
        titulo = "Últimos eventos registrados"
    if not filas:
        return {"titulo": titulo,
                "texto": "No hay eventos registrados todavía en el calendario.",
                "df": None}
    df = pd.DataFrame(filas)[["fecha", "empresa", "evento", "tipo"]]
    df.columns = ["Fecha", "Empresa", "Evento", "Tipo"]
    return {"titulo": titulo,
            "texto": "Estos son los eventos que tengo en el calendario:",
            "df": df}


def _top_ranking() -> Dict:
    filas = query_all(
        "SELECT r.posicion, e.nombre, r.ticker, r.score "
        "FROM ranking_estado r "
        "INNER JOIN empresas e ON UPPER(e.ticker) = UPPER(r.ticker) AND e.activa = 1 "
        "ORDER BY r.posicion ASC, r.score DESC LIMIT 10"
    )
    if not filas:
        return {"titulo": "Ranking de calidad",
                "texto": ("El ranking aún no se ha calculado en esta sesión. "
                          "Visita la vista **Ranking** y vuelve a preguntarme."),
                "df": None}
    df = pd.DataFrame(filas)
    df.columns = ["Pos.", "Empresa", "Ticker", "Score"]
    return {"titulo": "Top 10 del ranking de calidad",
            "texto": "Las mejor posicionadas del último ranking calculado:",
            "df": df}


def _volatiles(ctx: Dict) -> Dict:
    radar = ctx.get("radar")
    if _radar_invalido(radar):
        return {"titulo": "Empresas más volátiles", "texto": "Sin datos de mercado en el radar ahora mismo.", "df": None}
    top = radar.nlargest(8, "vol_d")
    df = pd.DataFrame({
        "Empresa": top["empresa"].values,
        "Vol. diaria (%)": top["vol_d"].values,
        "Banda 1M ± (%)": top["banda_1M"].values,
        "Señal 1M": top["senal_1M"].values,
    })
    return {
        "titulo": "Empresas más volátiles",
        "texto": ("Mayor volatilidad diaria (σ). Su banda de movimiento esperado "
                  "a 1 mes es más ancha: más riesgo y más incertidumbre:"),
        "df": df,
    }


def _ficha(ticker: str, ctx: Dict) -> Dict:
    radar = ctx.get("radar")
    nombre = ctx["ticker2nombre"].get(ticker, ticker)
    if _radar_invalido(radar):
        return {"titulo": f"{nombre}", "texto": "Sin datos de mercado en el radar ahora mismo.", "df": None}
    fila = radar[radar["ticker"] == ticker]
    if fila.empty:
        return {"titulo": f"{nombre}",
                "texto": "No tengo datos de mercado suficientes para esta empresa ahora mismo.",
                "df": None}
    r = fila.iloc[0]
    d = ctx["fundamentales"].get(ticker, {})
    pe = _f_num(d.get("trailingPE"))
    pe_fwd = _f_num(d.get("forwardPE"))
    pb = _f_num(d.get("priceToBook"))
    ev_eb = _f_num(d.get("enterpriseToEbitda"))
    yld = _pct_decimal(d.get("dividendYield"), es_dividendo=True)
    roe = _pct_decimal(d.get("returnOnEquity"))
    op_m = _pct_decimal(d.get("operatingMargins"))
    de = _f_num(d.get("debtToEquity"))

    lineas_fund = []
    if pe is not None:
        lineas_fund.append(f"PER **{pe}**" + (f" (fwd {pe_fwd})" if pe_fwd is not None else ""))
    if pb is not None:
        lineas_fund.append(f"P/B **{pb}**")
    if ev_eb is not None:
        lineas_fund.append(f"EV/EBITDA **{ev_eb}**")
    if yld is not None:
        lineas_fund.append(f"dividendo **{yld}%**")
    if roe is not None:
        lineas_fund.append(f"ROE **{roe}%**")
    if op_m is not None:
        lineas_fund.append(f"margen op. **{op_m}%**")
    if de is not None:
        lineas_fund.append(f"deuda/fondos **{de}**")
    extra = (" · ".join(lineas_fund)) if lineas_fund else "fundamentales no cacheados"

    # Enriquecer con rango 52s y volatilidad si están en caché local del Panel de Análisis
    riesgo_txt = ""
    try:
        from services.descriptive_metrics import (
            cargar_datos_cache_historico,
            calcular_extremos_y_drawdown,
            calcular_metricas_volatilidad,
        )
        closes, highs, lows, vols = cargar_datos_cache_historico()
        if not closes.empty and ticker in closes.columns:
            p_ser = closes[ticker].dropna()
            if len(p_ser) >= 60:
                v_ser = vols[ticker].reindex(p_ser.index) if ticker in vols.columns else None
                h_ser = highs[ticker].reindex(p_ser.index) if ticker in highs.columns else None
                l_ser = lows[ticker].reindex(p_ser.index) if ticker in lows.columns else None
                ext = calcular_extremos_y_drawdown(p_ser, highs=h_ser, lows=l_ser, volumen=v_ser)
                mv = calcular_metricas_volatilidad(p_ser, volumen=v_ser, highs=h_ser, lows=l_ser)
                v20 = mv.get("vol_20d_anual")
                v20_s = f" · Vol. 20d **{v20:.1f}%**" if v20 is not None and pd.notna(v20) else ""
                riesgo_txt = (
                    f"\nRango 52s: **{ext['min_52w']:.2f} € – {ext['max_52w']:.2f} €** "
                    f"(posición **{ext['posicion_rango_52w_pct']:.0f}%**, "
                    f"Max DD **{ext['max_drawdown_252d_pct']:.1f}%**){v20_s}."
                )
    except Exception:
        pass

    df = pd.DataFrame({
        "Horizonte": ["1 Día", "3 Días", "1 Semana", "1 Mes"],
        "Señal": [r.get("senal_1D", "—"), r.get("senal_3D", "—"), r.get("senal_1S", "—"), r.get("senal_1M", "—")],
        "Estimado (%)": [r.get("pct_1D"), r.get("pct_3D"), r.get("pct_1S"), r.get("pct_1M")],
        "Confianza (%)": [r.get("conf_1D"), r.get("conf_3D"), r.get("conf_1S"), r.get("conf_1M")],
    })
    precio_val = float(r.get("precio", 0.0))
    var_val = float(r.get("var_dia", 0.0))
    rsi_val = float(r.get("rsi", 50.0))
    mom_val = float(r.get("mom20", 0.0))
    s1s_val = r.get("senal_1S", "—")
    pct1s_val = float(r.get("pct_1S", 0.0))

    return {
        "titulo": f"{nombre} ({ticker})",
        "texto": (
            f"Precio **{precio_val:.2f} €** ({var_val:+.2f}% hoy) · "
            f"RSI **{rsi_val:.0f}** · Mom. 20d **{mom_val:+.1f}%** · {extra}.{riesgo_txt}\n\n"
            f"Señal principal (1S): **{s1s_val}** ({pct1s_val:+.1f}% esperado)."
        ),
        "df": df,
    }


def _proyeccion(ticker: str, ctx: Dict, dias: int = 20) -> Dict:
    """Proyección cuantitativa con cono de volatilidad (base, optimista +1σ, pesimista -1σ)."""
    radar = ctx.get("radar")
    nombre = ctx.get("ticker2nombre", {}).get(ticker, ticker)
    if _radar_invalido(radar):
        return {"titulo": f"Proyección de {nombre}", "texto": "Sin datos de mercado en el radar.", "df": None}

    fila = radar[radar["ticker"] == ticker]
    if fila.empty:
        return {"titulo": f"Proyección de {nombre}", "texto": f"No se encontró información para {nombre}.", "df": None}

    r = fila.iloc[0]
    precio = float(r.get("precio") or 0.0)
    if precio <= 0:
        return {"titulo": f"Proyección de {nombre}", "texto": f"Precio actual no válido para proyectar {nombre}.", "df": None}
    dias = max(1, min(int(dias or 20), 60))
    score_1m = float(r.get("score_1M", 0.0))
    vol_d = float(r.get("vol_d", 1.5)) / 100.0

    from datetime import date
    import numpy as np
    from services import signal_engine
    cono = signal_engine.proyectar_cono(precio, score_1m, vol_d, date.today().isoformat(), dias=dias)
    if not cono:
        return {"titulo": f"Proyección de {nombre}", "texto": "No se pudo calcular el cono de proyección.", "df": None}

    fechas = [pd.Timestamp(f).strftime("%d/%m") for f in cono["fechas"]]
    base = cono["base"]
    opt = cono["optimista"]
    pes = cono["pesimista"]

    hitos_idx = [min(len(fechas)-1, idx) for idx in (0, 4, 9, len(fechas)-1)]
    hitos_idx = sorted(list(dict.fromkeys(hitos_idx)))

    filas = []
    for idx in hitos_idx:
        filas.append((
            fechas[idx],
            f"{base[idx]:.2f} €",
            f"{opt[idx]:.2f} €",
            f"{pes[idx]:.2f} €",
            f"{((base[idx]/precio)-1.0)*100.0:+.2f}%",
        ))
    df = pd.DataFrame(filas, columns=["Sesión", "Base (€)", "Techo +1σ (€)", "Suelo -1σ (€)", "Retorno Base (%)"])

    s_1m = r.get("senal_1M", "LATERAL")
    pct_1m = r.get("pct_1M", 0.0)
    conf_1m = r.get("conf_1M", 50.0)
    vol_anual = vol_d * np.sqrt(252) * 100.0

    texto = (
        f"**{nombre}** ({ticker}) cotiza actualmente a **{precio:.2f} €**. "
        f"Señal a 1 mes: **{s_1m}** ({pct_1m:+.2f}% esperado, confianza **{conf_1m:.0f}%**).\n\n"
        f"Cono probabilístico a **{dias} sesiones hábiles** (volatilidad anualizada ≈ {vol_anual:.1f}%):\n"
        f"- **Escenario base (deriva):** {base[-1]:.2f} € ({((base[-1]/precio)-1.0)*100.0:+.2f}%)\n"
        f"- **Techo estadístico (+1σ):** {opt[-1]:.2f} € ({((opt[-1]/precio)-1.0)*100.0:+.2f}%)\n"
        f"- **Suelo estadístico (-1σ):** {pes[-1]:.2f} € ({((pes[-1]/precio)-1.0)*100.0:+.2f}%)\n\n"
        f"*Intervalo de confianza 1σ: el precio tiene un ~68% de probabilidad matemática de situarse dentro de esta banda.*"
    )
    return {
        "titulo": f"Proyección Cuantitativa — {nombre} ({ticker})",
        "texto": texto,
        "df": df,
    }


def _comparar(t1: str, t2: str, ctx: Dict) -> Dict:
    radar = ctx.get("radar")
    if _radar_invalido(radar):
        return {"titulo": "Comparativa", "texto": "Sin datos de mercado en el radar ahora mismo.", "df": None}
    f1 = radar[radar["ticker"] == t1]
    f2 = radar[radar["ticker"] == t2]
    n1 = ctx["ticker2nombre"].get(t1, t1)
    n2 = ctx["ticker2nombre"].get(t2, t2)
    if f1.empty or f2.empty:
        return {"titulo": f"⚖️ {n1} vs {n2}",
                "texto": "No tengo datos suficientes de alguna de las dos empresas.",
                "df": None}
    r1, r2 = f1.iloc[0], f2.iloc[0]
    df = pd.DataFrame({
        "Métrica": ["Precio (€)", "Var. día (%)", "RSI", "Mom. 20d (%)",
                    "Señal 1S", "Est. 1S (%)", "Señal 1M", "Est. 1M (%)"],
        n1: [r1["precio"], r1["var_dia"], r1["rsi"], r1["mom20"],
             r1["senal_1S"], r1["pct_1S"], r1["senal_1M"], r1["pct_1M"]],
        n2: [r2["precio"], r2["var_dia"], r2["rsi"], r2["mom20"],
             r2["senal_1S"], r2["pct_1S"], r2["senal_1M"], r2["pct_1M"]],
    })
    ganador = n1 if r1["score_1S"] >= r2["score_1S"] else n2
    return {
        "titulo": f"{n1} vs {n2}",
        "texto": f"Comparativa directa. Por fuerza de señal semanal, hoy destaca **{ganador}**:",
        "df": df,
    }


def _noticias(ticker: Optional[str], ctx: Dict) -> Dict:
    """Titulares recientes + sentimiento de una empresa, o del mercado (IBEX) si ticker=None."""
    from services import news_sentiment
    if ticker:
        nombre = ctx["ticker2nombre"].get(ticker, ticker)
        query_nombre, query_ticker = nombre, ticker
    else:
        nombre = "el mercado (IBEX 35)"
        query_nombre, query_ticker = "IBEX 35", "^IBEX"
    noticias = news_sentiment.obtener_noticias(query_nombre, query_ticker, max_items=8)
    if not noticias:
        return {"titulo": f"Noticias de {nombre}",
                "texto": f"No he encontrado titulares recientes de **{nombre}** ahora mismo.",
                "df": None,
                "fuentes": []}
    resumen = news_sentiment.resumen_sentimiento(noticias)
    df = pd.DataFrame({
        "Fecha": [n["fecha"] for n in noticias],
        "Titular": [n["titulo"] for n in noticias],
        "Fuente": [n["fuente"] for n in noticias],
        "Sentimiento": [n["sentimiento"] for n in noticias],
    })
    fuentes = [
        f"- [{_limpiar_texto_link(n['titulo'])} ({_limpiar_texto_link(n['fuente'])})]({n['link']})"
        for n in noticias[:5] if n.get("link")
    ]
    return {
        "titulo": f"Noticias de {nombre}",
        "texto": (f"Balance de titulares: **{resumen['etiqueta']}** "
                  f"({resumen['positivas']} positivas · {resumen['negativas']} negativas · "
                  f"{resumen['neutrales']} neutrales). Estos son los más recientes:"),
        "df": df,
        "fuentes": fuentes,
    }


def _buscar_actualidad_web(consulta: str) -> Dict:
    """Búsqueda abierta de actualidad económica/financiera en Google News RSS (gratuita)."""
    from services import news_sentiment
    q = (consulta or "").strip()
    noticias = news_sentiment.buscar_actualidad_web(q, max_items=8)
    if not noticias:
        return {
            "titulo": f"Actualidad web: {q}",
            "texto": f"No se han encontrado titulares recientes en la web para **{q}**.",
            "df": None,
            "fuentes": [],
        }
    resumen = news_sentiment.resumen_sentimiento(noticias)
    df = pd.DataFrame({
        "Fecha": [n["fecha"] for n in noticias],
        "Titular": [n["titulo"] for n in noticias],
        "Fuente": [n["fuente"] for n in noticias],
        "Sentimiento": [n["sentimiento"] for n in noticias],
    })
    fuentes = [
        f"- [{_limpiar_texto_link(n['titulo'])} ({_limpiar_texto_link(n['fuente'])})]({n['link']})"
        for n in noticias[:5] if n.get("link")
    ]
    return {
        "titulo": f"Actualidad web: {q}",
        "texto": (
            f"Resultados recientes en prensa financiera para **{q}** "
            f"(tono: **{resumen['etiqueta']}**, {resumen['positivas']} pos. / "
            f"{resumen['negativas']} neg. / {resumen['neutrales']} neut.):"
        ),
        "df": df,
        "fuentes": fuentes,
    }


def _riesgo_empresa(ticker: str, ctx: Dict) -> Dict:
    """Perfil de riesgo cuantitativo descriptivo (Panel de Análisis) para un ticker."""
    from services.descriptive_metrics import (
        cargar_datos_cache_historico,
        calcular_metricas_volatilidad,
        calcular_extremos_y_drawdown,
        calcular_metricas_liquidez,
        calcular_sensibilidad_benchmark,
    )
    nombre = ctx["ticker2nombre"].get(ticker, ticker)
    closes, highs, lows, vols = cargar_datos_cache_historico()
    if closes.empty or ticker not in closes.columns:
        return {
            "titulo": f"Perfil de riesgo: {nombre}",
            "texto": f"No hay serie histórica suficiente en caché para **{nombre}** ({ticker}).",
            "df": None,
        }
    p = closes[ticker].dropna()
    if len(p) < 60:
        return {
            "titulo": f"Perfil de riesgo: {nombre}",
            "texto": f"Serie histórica insuficiente (< 60 sesiones) para **{nombre}**.",
            "df": None,
        }
    v = vols[ticker].reindex(p.index) if ticker in vols.columns else None
    h = highs[ticker].reindex(p.index) if ticker in highs.columns else None
    l = lows[ticker].reindex(p.index) if ticker in lows.columns else None
    vol_eur_mat = (closes * vols).rolling(60).mean().iloc[-1] if not vols.empty else None

    m_vol = calcular_metricas_volatilidad(p, volumen=v, highs=h, lows=l)
    m_ext = calcular_extremos_y_drawdown(p, highs=h, lows=l, volumen=v, ventana_dias=252)
    m_liq = calcular_metricas_liquidez(p, volumen=v, volumenes_mercado_medios=vol_eur_mat, ventana_dias=252)
    p_ibex = closes["^IBEX"].dropna() if "^IBEX" in closes.columns else pd.Series(dtype=float)
    m_sens = calcular_sensibilidad_benchmark(p, p_ibex, ventana_corr=60)

    def _fv(val, spec=".1f", suf=""):
        return f"{format(float(val), spec)}{suf}" if (val is not None and pd.notna(val)) else "N/D"

    veur20 = m_liq.get("volumen_eur_20d")
    liq_txt = (
        f"{veur20 / 1e6:.2f} M€/día" if (veur20 is not None and pd.notna(veur20) and veur20 >= 1e6)
        else (f"{veur20 / 1e3:.1f} k€/día" if (veur20 is not None and pd.notna(veur20)) else "N/D")
    )

    df = pd.DataFrame([
        {"Métrica": "Volatilidad anualizada (20d / 60d / 252d)",
         "Valor": f"{_fv(m_vol['vol_20d_anual'], '.1f', '%')} / {_fv(m_vol['vol_60d_anual'], '.1f', '%')} / {_fv(m_vol['vol_252d_anual'], '.1f', '%')}"},
        {"Métrica": "Percentil volatilidad (1A / 3A)",
         "Valor": f"{_fv(m_vol['percentil_1y'], '.1f', '%')} / {_fv(m_vol['percentil_3y'], '.1f', '%')}"},
        {"Métrica": "Rango 52 semanas (mín – máx)",
         "Valor": f"{m_ext['min_52w']:.3f} € – {m_ext['max_52w']:.3f} € (pos. {m_ext['posicion_rango_52w_pct']:.1f}%)"},
        {"Métrica": "Max Drawdown (252d) / Actual",
         "Valor": f"{m_ext['max_drawdown_252d_pct']:.2f}% / {m_ext['drawdown_actual_pct']:.2f}%"},
        {"Métrica": "Liquidez media 20d / Tercil",
         "Valor": f"{liq_txt} ({m_liq['tercil_liquidez']})"},
        {"Métrica": "Beta / Correlación 60d vs IBEX 35",
         "Valor": f"{_fv(m_sens.get('beta_ibex_60d'), '.2f')} / {_fv(m_sens.get('correlacion_ibex_60d'), '.2f')}"},
        {"Métrica": "Retorno relativo vs IBEX (1M / 6M / 1A)",
         "Valor": f"{_fv(m_sens.get('retorno_relativo_1m_pct'), '+.1f', '%')} / {_fv(m_sens.get('retorno_relativo_6m_pct'), '+.1f', '%')} / {_fv(m_sens.get('retorno_relativo_1a_pct'), '+.1f', '%')}"},
    ])
    return {
        "titulo": f"Perfil de riesgo y liquidez: {nombre} ({ticker})",
        "texto": (
            f"Régimen actual de volatilidad: **{m_vol['regimen_descriptivo']}** "
            f"(20d: **{_fv(m_vol['vol_20d_anual'], '.1f', '%')}**, Max DD 1A: **{m_ext['max_drawdown_252d_pct']:.1f}%**). "
            f"Beta 60d vs IBEX 35: **{_fv(m_sens.get('beta_ibex_60d'), '.2f')}** · Liquidez: **{liq_txt}**."
        ),
        "df": df,
    }


def _resumen_sector(sector_query: str, ctx: Dict) -> Dict:
    """Resumen de empresas activas de un sector (o listado de sectores si no se especifica)."""
    filas_db = query_all(
        "SELECT e.ticker, e.nombre, e.sector, r.score AS score_calidad "
        "FROM empresas e LEFT JOIN ranking_estado r ON UPPER(r.ticker) = UPPER(e.ticker) "
        "WHERE e.activa = 1 AND e.sector IS NOT NULL AND TRIM(e.sector) != '' "
        "ORDER BY e.nombre"
    )
    if not filas_db:
        return {"titulo": "Sectores BME", "texto": "No hay clasificación sectorial disponible.", "df": None}

    q_norm = _norm(sector_query or "")
    # Alias habituales en español (incluye raíces de adjetivos masculino/femenino)
    alias_sector = {
        "banc": ["banc", "financ"],
        "financier": ["banc", "financ", "segur"],
        "energ": ["energ", "renovab", "electr", "petrol", "utilit"],
        "electr": ["electr", "energ", "utilit"],
        "construc": ["construc", "infraestruct"],
        "salud": ["salud", "farma", "biotec"],
        "farmac": ["farma", "salud"],
        "inmobil": ["inmobil", "socimi", "real estate"],
        "tecnolog": ["tecnolog", "software", "telecom", "informatic", "semiconduct"],
        "telecom": ["telecom", "tecnolog"],
        "turis": ["turism", "hotel", "aerolin", "viaj", "ocio"],
        "industr": ["industr", "metal", "acero", "papel"],
        "consumo": ["consumo", "textil", "aliment", "distrib"],
    }
    raices = [q_norm] if q_norm else []
    for k, vals in alias_sector.items():
        if k in q_norm:
            raices.extend(vals)

    radar = ctx.get("radar")
    fund = ctx.get("fundamentales", {})
    radar_map = {str(r["ticker"]).upper(): r for _, r in radar.iterrows()} if (radar is not None and not radar.empty) else {}

    if not raices:
        # Resumen agregado por sector (top 12 sectores con más empresas)
        por_sec: Dict[str, List] = {}
        for f in filas_db:
            por_sec.setdefault(f["sector"], []).append(f)
        resumen_s = []
        for sec, lista in sorted(por_sec.items(), key=lambda kv: -len(kv[1]))[:12]:
            vars_d = [float(radar_map[f["ticker"].upper()]["var_dia"]) for f in lista if f["ticker"].upper() in radar_map]
            cal_s = [float(f["score_calidad"]) for f in lista if f.get("score_calidad") is not None]
            resumen_s.append({
                "Sector": sec,
                "Empresas": len(lista),
                "Var. día media (%)": round(sum(vars_d) / len(vars_d), 2) if vars_d else None,
                "Calidad media": round(sum(cal_s) / len(cal_s), 1) if cal_s else None,
            })
        return {
            "titulo": "Resumen de sectores BME",
            "texto": "Principales sectores en cobertura ordenados por número de empresas:",
            "df": pd.DataFrame(resumen_s),
        }

    coinc = [f for f in filas_db if any(raiz and raiz in _norm(f["sector"]) for raiz in raices)]
    if not coinc:
        sectores_disp = sorted({f["sector"] for f in filas_db})[:15]
        return {
            "titulo": f"Sector: {sector_query}",
            "texto": (
                f"No encontré empresas para **{sector_query}**. Algunos sectores disponibles son: "
                + ", ".join(f"**{s}**" for s in sectores_disp) + "."
            ),
            "df": None,
        }

    out = []
    for f in coinc:
        tk = f["ticker"].upper()
        r = radar_map.get(tk)
        d = fund.get(f["ticker"], fund.get(tk, {}))
        out.append({
            "Empresa": f["nombre"],
            "Sector": f["sector"],
            "Precio (€)": round(float(r["precio"]), 2) if r is not None else None,
            "Var. día (%)": round(float(r["var_dia"]), 2) if r is not None else None,
            "Mom. 20d (%)": round(float(r["mom20"]), 1) if r is not None else None,
            "PER": _f_num(d.get("trailingPE")),
            "Div. (%)": _pct_decimal(d.get("dividendYield"), es_dividendo=True),
            "Calidad": round(float(f["score_calidad"]), 1) if f.get("score_calidad") is not None else None,
        })
    out.sort(key=lambda x: -(x["Calidad"] if x["Calidad"] is not None else -1.0))
    df = pd.DataFrame(out[:15])
    vars_ok = [x["Var. día (%)"] for x in out if x["Var. día (%)"] is not None]
    media_var = (sum(vars_ok) / len(vars_ok)) if vars_ok else 0.0
    return {
        "titulo": f"Sector {sector_query.strip().capitalize()}",
        "texto": (
            f"Encontré **{len(out)} empresa(s)** en el sector **{sector_query}** "
            f"(variación diaria media: **{media_var:+.2f}%**). Ordenadas por calidad:"
        ),
        "df": df,
    }


def _mis_alertas(ctx: Dict) -> Dict:
    """Consulta las alertas activas del motor del Dashboard (Watchlist, Cartera, Ranking y Eventos)."""
    from services import alert_engine
    from database import cartera as db_cartera, watchlist as db_wl

    radar = ctx.get("radar")
    datos_var = []
    try:
        scores_db = {
            str(r["ticker"]).strip().upper(): float(r["score"])
            for r in query_all("SELECT ticker, score FROM ranking_estado")
        }
    except Exception:
        scores_db = {}

    if not _radar_invalido(radar):
        for _, r in radar.iterrows():
            tk = str(r["ticker"]).strip().upper()
            datos_var.append({
                "ticker": tk,
                "nombre": r["empresa"],
                "precio": float(r["precio"]),
                "variacion": float(r["var_dia"]),
                "score_calidad": scores_db.get(tk, 0.0),
                "score_oportunidad": 0.0,
            })

    try:
        df_wl = db_wl.obtener_watchlist()
    except Exception:
        df_wl = None

    try:
        tipo_act = st.session_state.get("tipo_cartera_activa", "SIMULADA") if hasattr(st, "session_state") else "SIMULADA"
        pos_abiertas = db_cartera.calcular_posiciones_abiertas_filtradas(tipo_act)
    except Exception:
        pos_abiertas = None

    try:
        empresas_rows = query_all("SELECT ticker, nombre FROM empresas WHERE activa = 1")
    except Exception:
        empresas_rows = None

    try:
        eventos_db = query_all("SELECT empresa, ticker, evento, fecha, tipo FROM eventos WHERE fecha >= date('now') ORDER BY fecha ASC LIMIT 20")
    except Exception:
        eventos_db = None

    try:
        alertas = alert_engine.obtener_alertas(
            df_watchlist=df_wl,
            posiciones_abiertas=pos_abiertas,
            datos_variacion=datos_var,
            empresas_rows=empresas_rows,
            eventos_db=eventos_db,
        )[:15]
    except Exception:
        alertas = []

    if not alertas:
        return {
            "titulo": "Alertas activas",
            "texto": "No hay alertas activas en este momento en Watchlist, Cartera ni Ranking.",
            "df": None,
        }

    etiqueta_est = {"critica": "Crítica", "negativa": "Crítica", "positiva": "Positiva", "informativa": "Info"}
    df = pd.DataFrame([
        {
            "Origen": a.get("origen", "Sistema"),
            "Empresa": a.get("empresa") or a.get("ticker") or "Mercado",
            "Estado": etiqueta_est.get(str(a.get("estado", "")).lower(), str(a.get("estado", "Info")).capitalize()),
            "Detalle": a.get("mensaje", ""),
        }
        for a in alertas
    ])
    n_crit = sum(1 for a in alertas if str(a.get("estado", "")).lower() in ("critica", "negativa"))
    n_pos = sum(1 for a in alertas if str(a.get("estado", "")).lower() == "positiva")
    n_info = len(alertas) - n_crit - n_pos
    return {
        "titulo": "Alertas activas de StockLab",
        "texto": (
            f"Hay **{len(alertas)} alerta(s) activa(s)** "
            f"(**{n_crit}** crítica(s) · **{n_pos}** positiva(s) · **{n_info}** informativa(s)):"
        ),
        "df": df,
    }


def _mi_cartera(ctx: Dict, tipo: Optional[str] = None) -> Dict:
    """Posiciones abiertas + valoración de la cartera (ACTIVA, SIMULADA o REAL)."""
    from database import cartera as db_cartera
    tipo_norm = (tipo or "").strip().upper()
    if tipo_norm not in ("SIMULADA", "REAL"):
        tipo_norm = st.session_state.get("tipo_cartera_activa", "SIMULADA")
    try:
        pat = db_cartera.calcular_patrimonio_total_filtrado(tipo_norm)
        posiciones = db_cartera.calcular_posiciones_abiertas_filtradas(tipo_norm)
    except Exception:
        return {"titulo": "Mi cartera",
                "texto": "No he podido leer tu cartera ahora mismo. Inténtalo de nuevo en unos segundos.",
                "df": None}
    if not posiciones:
        return {"titulo": f"Mi cartera ({tipo_norm.lower()})",
                "texto": (f"Tu cartera **{tipo_norm}** no tiene posiciones abiertas. "
                          f"Liquidez disponible: **{pat['liquidez']:,.2f} €**."),
                "df": None}
    radar = ctx.get("radar")
    radar_ok = not _radar_invalido(radar)
    filas = []
    for pos in posiciones:
        fila = radar[radar["ticker"] == pos["ticker"]] if radar_ok else pd.DataFrame()
        if not fila.empty:
            r = fila.iloc[0]
            precio_actual, senal = float(r["precio"]), r["senal_1S"]
        else:
            precio_actual, senal = float(pos["precio_medio"]), "—"
        valor = pos["acciones"] * precio_actual
        pnl_pct = ((precio_actual / pos["precio_medio"]) - 1.0) * 100.0 if pos["precio_medio"] else 0.0
        filas.append((pos["nombre"], round(pos["acciones"], 2), round(pos["precio_medio"], 2),
                      round(precio_actual, 2), round(valor, 2), round(pnl_pct, 2), senal))
    df = pd.DataFrame(filas, columns=["Empresa", "Acciones", "P. medio (€)", "P. actual (€)",
                                      "Valor (€)", "P&L (%)", "Señal 1S"])
    return {
        "titulo": f"Mi cartera ({tipo_norm.lower()})",
        "texto": (f"Patrimonio total: **{pat['patrimonio_total']:,.2f} €** "
                  f"(liquidez {pat['liquidez']:,.2f} € · invertido {pat['capital_invertido']:,.2f} €). "
                  f"Rentabilidad de las posiciones: **{pat['rentabilidad_pct']:+.2f}%** "
                  f"({pat['rentabilidad_absoluta']:+,.2f} €) en {pat['num_posiciones']} posición(es)."),
        "df": df,
    }


def _mi_watchlist(ctx: Dict) -> Dict:
    """Empresas en seguimiento con su estado actual de mercado."""
    from database import watchlist as db_watchlist
    try:
        wl = db_watchlist.obtener_watchlist()
    except Exception:
        wl = pd.DataFrame()
    if wl.empty:
        return {"titulo": "Mi watchlist",
                "texto": "Tu watchlist está vacía. Añade empresas desde la vista **Watchlist**.",
                "df": None}
    radar = ctx.get("radar")
    radar_ok = not _radar_invalido(radar)
    filas = []
    for _, w in wl.iterrows():
        fila = radar[radar["ticker"] == w["ticker"]] if radar_ok else pd.DataFrame()
        nombre = ctx["ticker2nombre"].get(w["ticker"], w["ticker"])
        base = float(w["precio_adicion"]) if (w.get("precio_adicion") is not None and pd.notna(w["precio_adicion"])) else 0.0
        if not fila.empty:
            r = fila.iloc[0]
            precio, var_dia, senal = float(r["precio"]), r["var_dia"], r["senal_1S"]
        else:
            precio, var_dia, senal = base, None, "—"
        desde = round(((precio / base) - 1.0) * 100.0, 2) if base > 0 else None
        filas.append((nombre, round(precio, 2), var_dia, desde, senal, (w["motivo"] or "")[:60]))
    df = pd.DataFrame(filas, columns=["Empresa", "Precio (€)", "Var. día (%)",
                                      "Desde alta (%)", "Señal 1S", "Motivo"])
    return {
        "titulo": "Mi watchlist",
        "texto": (f"Tienes **{len(df)} empresa(s)** en seguimiento. "
                  f"'Desde alta' es la variación desde que la añadiste:"),
        "df": df,
    }


@st.cache_data(ttl=300, show_spinner=False)
def _obtener_metricas_pred_cacheadas(horizonte: str = "Todos") -> Dict:
    from database import predicciones as db_pred
    return db_pred.obtener_metricas(horizonte)


def _metricas_predicciones(horizonte: Optional[str] = None) -> Dict:
    """Métricas y estadísticas de acierto del motor predictivo de IA (cacheadas 5 min)."""
    hz_norm = horizonte if horizonte in ("1D", "3D", "1S", "1M") else "Todos"
    m = _obtener_metricas_pred_cacheadas(hz_norm)
    total = m.get("total", 0)
    if total == 0:
        return {
            "titulo": "Auditoría de Predicciones",
            "texto": "El historial de predicciones está en fase de acumulación y aún no registra señales resueltas.",
            "df": None,
        }
    resueltas = m.get("resueltas", 0)
    tasa = m.get("tasa_acierto_3clases", m.get("tasa_acierto"))
    tasa_signo = m.get("tasa_acierto_signo")
    ref_lat = m.get("ref_siempre_lateral_pct")
    score = m.get("score_medio")
    err = m.get("error_medio")
    pend = m.get("pendientes", 0)
    inv = m.get("invalidadas", 0)

    tasa_txt = f"**{tasa:.1f}%**" if tasa is not None else "en cálculo"
    lat_txt = f" (ref. siempre lateral: {ref_lat:.1f}%)" if ref_lat is not None else ""
    signo_txt = f"**{tasa_signo:.1f}%**" if tasa_signo is not None else "s/d"
    score_txt = f"**{score:.0f}/100**" if score is not None else "en cálculo"
    err_txt = f"**{err:.2f} p.p.**" if err is not None else "en cálculo"

    hz_data = m.get("por_horizonte", {})
    filas = []
    hz_nombres = {"1D": "1 Día", "3D": "3 Días", "1S": "1 Semana", "1M": "1 Mes"}
    for hz in ("1D", "3D", "1S", "1M"):
        d = hz_data.get(hz, {})
        t3 = d.get("tasa_acierto_3clases", d.get("tasa"))
        ts = d.get("tasa_acierto_signo")
        filas.append((
            hz_nombres.get(hz, hz),
            d.get("n", 0),
            f"{t3:.1f}%" if t3 is not None else "—",
            f"{ts:.1f}%" if ts is not None else "—",
        ))
    df = pd.DataFrame(filas, columns=["Horizonte", "Resueltas", "3 Clases (%)", "Signo (%)"]) if filas else None

    sub_hz = f" ({horizonte})" if horizonte and horizonte != "Todos" else ""
    texto = (
        f"El motor de IA registra **{total} señales**{sub_hz}, con **{resueltas} resueltas** "
        f"contra precio real y **{pend} en seguimiento activo**.\n\n"
        f"- **Acierto en 3 clases:** {tasa_txt}{lat_txt}\n"
        f"- **Acierto de signo (ALC/BAJ):** {signo_txt} (ref. azar: 50.0%)\n"
        f"- **Score medio / Error medio:** {score_txt} · {err_txt}\n"
        f"- **Invalidadas por stop:** **{inv}**"
    )
    return {
        "titulo": f"Auditoría y Métricas del Motor Predictivo{sub_hz}",
        "texto": texto,
        "df": df,
    }


def _fallback() -> Dict:
    return {
        "titulo": "No he entendido del todo la pregunta",
        "texto": (
            "Puedo analizar tus datos si me preguntas cosas como:\n\n"
            "- *¿Qué empresa tiene mejor momentum?*\n"
            "- *¿Qué señales alcistas hay hoy?*\n"
            "- *¿Qué empresas están en sobreventa?*\n"
            "- *¿Cuáles tienen PER bajo y buen dividendo?*\n"
            "- *¿Cómo va mi cartera?* · *¿Qué tengo en mi watchlist?*\n"
            "- *¿Cuál es la tasa de acierto de las predicciones?*\n"
            "- *¿Qué noticias hay de BBVA?* · *Resume BBVA*"
        ),
        "df": None,
    }


# ═══════════════════════════════════════════════════════════════════
# 🚦 ROUTER PRINCIPAL
# ═══════════════════════════════════════════════════════════════════

def responder(pregunta: str, ctx: Dict) -> Dict:
    """Punto de entrada: detecta intención → handler → respuesta estructurada."""
    p = _norm(pregunta)
    if not p:
        return _ayuda()

    radar = ctx.get("radar")
    if radar is None or radar.empty:
        return {"titulo": "Sin datos de mercado",
                "texto": "El radar no tiene datos ahora mismo (posible fallo de "
                         "conexión con Yahoo Finance). Inténtalo de nuevo en unos minutos.",
                "df": None}

    # 0) Ayuda / saludo
    if re.search(r"\b(hola|buenas|ayuda|help|que puedes|como funcionas)\b", p):
        return _ayuda()

    entidades = _detectar_empresas(p, ctx)

    # 1) Comparativa explícita entre dos empresas
    if re.search(r"\b(compara|comparar|versus|vs)\b", p) and len(entidades) >= 2:
        return _comparar(entidades[0], entidades[1], ctx)

    # 1b) Noticias / actualidad web: empresa concreta, IBEX o consulta macro/externa
    if re.search(r"noticia|titular|prensa|actualidad|que ha pasado|que pasa|bce|fed|tipos de interes|inflacion|petroleo|wall street|opa", p):
        if entidades:
            return _noticias(entidades[0], ctx)
        if re.search(r"bce|fed|tipos de interes|inflacion|petroleo|wall street|opa|europa|eeuu|banca europea|economia", p):
            return _buscar_actualidad_web(pregunta)
        return _noticias(None, ctx)

    # 1c) Alertas, cartera (simulada/real) y watchlist personales
    if re.search(r"\balerta|\bavisos\b|notificacion", p):
        return _mis_alertas(ctx)
    if re.search(r"\bcartera\b|portafolio|mis posiciones|mis acciones|patrimonio", p):
        tipo_c = "REAL" if "real" in p else ("SIMULADA" if "simulada" in p else None)
        return _mi_cartera(ctx, tipo=tipo_c)
    if re.search(r"watchlist|en seguimiento|favoritas|mis listas", p):
        return _mi_watchlist(ctx)

    # 1d) Auditoría y acierto de predicciones
    if re.search(r"acierto|precision|tasa de acierto|score medio|como van las predicciones|historial.*prediccion|auditoria.*prediccion|fiabilidad", p):
        hz_target = None
        if "1d" in p or "1 dia" in p:
            hz_target = "1D"
        elif "3d" in p or "3 dia" in p:
            hz_target = "3D"
        elif "semana" in p or "1s" in p:
            hz_target = "1S"
        elif "mes" in p or "1m" in p:
            hz_target = "1M"
        return _metricas_predicciones(hz_target)

    # 1e) Perfil de riesgo descriptivo de una empresa concreta
    if entidades and re.search(r"riesgo|volatilidad|drawdown|beta|correlacion|liquidez|52 semanas|rango", p):
        return _riesgo_empresa(entidades[0], ctx)

    # 1f) Análisis por sector
    if re.search(r"\bsector|\bsectores\b|\bbanca\b|\bbancos\b|\benerget|\belectricas\b|\bfarmaceut|\binmobiliar|\bconstruccion\b|\bturismo\b", p) and not entidades:
        m_sec = re.search(r"sector(?:es)?\s+(?:de\s+|del\s+)?([a-z0-9 ]{3,24})", p)
        if m_sec:
            return _resumen_sector(m_sec.group(1).strip(), ctx)
        for kw_s in ("banca", "bancos", "energia", "electricas", "salud", "farmaceutico", "inmobiliaria", "construccion", "turismo", "tecnologia", "industria", "consumo"):
            if kw_s in p:
                return _resumen_sector(kw_s, ctx)
        return _resumen_sector("", ctx)

    # 2) Proyección cuantitativa de una empresa
    if entidades and re.search(r"proyeccion|prevision|objetivo|hacia donde|cono|cuanto subira|cuanto caera|escenario", p):
        return _proyeccion(entidades[0], ctx)

    # 2b) Ficha / diagnóstico general de una empresa concreta
    if entidades and (re.search(
            r"(resume|analiza|analisis|prediccion|pronostico|como esta|que tal|"
            r"senal de|informe|veo|cuentame)", p) or len(p.split()) <= 4):
        return _ficha(entidades[0], ctx)

    # 3) Intenciones temáticas
    if re.search(r"momentum|inercia|impulso", p):
        return _top_momentum(ctx)
    if re.search(r"sobreventa|rsi bajo|castigad|rebote", p):
        return _sobreventa(ctx)
    if re.search(r"sobrecompra", p):
        return _sobrecompra(ctx)
    if re.search(r"per bajo|barat|infravalorad|value|per y dividendo|per.*dividendo", p):
        return _value(ctx)
    if re.search(r"dividendo", p):
        if re.search(r"proxim|calendario|cuando|evento|fecha", p):
            return _proximos_eventos()
        return _top_dividendos(ctx)
    if re.search(r"proxim.*(evento|resultado)|calendario", p):
        return _proximos_eventos()
    if re.search(r"ranking|mejores empresas|mejor calidad|top|solidas|líderes|lideres", p):
        return _top_ranking()
    if re.search(r"volatil|riesgo|agitad", p):
        return _volatiles(ctx)
    if re.search(r"alcista|subir|subida|comprar|positiv|oportunidad", p):
        return _senales(ctx, "ALCISTA")
    if re.search(r"bajista|bajar|caer|caida|vender|negativ", p):
        return _senales(ctx, "BAJISTA")
    if re.search(r"lateral|estancad|sin tendencia", p):
        return _senales(ctx, "LATERAL")

    # 4) Si solo menciona una empresa (sin palabra clave), dar su ficha
    if entidades:
        return _ficha(entidades[0], ctx)

    return _fallback()
