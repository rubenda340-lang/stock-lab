"""
Noticias & Sentimiento — StockLab IA
====================================
Obtención de titulares recientes y clasificación de sentimiento.

Fuentes (ambas gratuitas, sin API keys nuevas):
  1. Google News RSS  — endpoint público, parseo XML con librería estándar
  2. yfinance .news   — fallback si el RSS no devuelve resultados

Sentimiento v1: léxico financiero en español (~150 términos). Cada titular
se puntúa como (#positivos − #negativos) y se etiqueta:
  Positivo / Neutral / Negativo

El balance agregado puede usarse como factor adicional en las señales del
Radar (peso moderado) — ver views/ia.py.

Caché: 1 hora por empresa (TTL coherente con la cadencia de publicación).
"""

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Dict, List, Tuple
from urllib.parse import quote

import requests
import streamlit as st

# ── Léxico financiero español ───────────────────────────────────────────────

_POSITIVOS = [
    "beneficio", "beneficios récord", "récord", "ganancia", "ganancias",
    "crece", "crecimiento", "aumenta", "sube", "subida", "alza", "repunta",
    "rebota", "mejora", "supera", "expectativas", "recompra", "dividendo",
    "dividendos", "eleva", "revisa alza", "upgrade", "sobreponderar",
    "comprar", "acuerdo", "expansión", "inversión", "contrato", "fusiona",
    "opera", "opv", "salida a bolsa", "recuperación", "optimista",
    "positivo", "solidez", "rentabilidad", "impulsa", "dispara", "máximo",
    "máximos", "histórico", "aprueba", "refuerza", "lidera", "gana",
]
_NEGATIVOS = [
    "pérdida", "pérdidas", "caída", "cae", "caen", "baja", "bajada",
    "desploma", "hunde", "recorte", "recorta", "rebaja", "downgrade",
    "infraponderar", "vender", "multa", "sanción", "demanda", "juicio",
    "crisis", "quiebra", "concurso", "despido", "despidos", "ere",
    "profit warning", "alerta", "riesgo", "deuda", "impago", "suspende",
    "suspensión", "negativo", "pesimista", "débil", "debilidad", "fraude",
    "investigación", "investiga", "cnmv", "rescisión", "cancela",
    "retrasa", "incumple", "mínimo", "mínimos", "castiga", "presión",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
}


_POSITIVOS_EN = [
    "profit", "record", "beat", "beats", "surge", "surges", "rally", "rallies",
    "growth", "upgrade", "buyback", "dividend", "rise", "rises", "gain", "gains",
    "strong", "outperform", "rebound", "expansion", "deal", "approval",
]
_NEGATIVOS_EN = [
    "loss", "losses", "fall", "falls", "drop", "drops", "plunge", "slump",
    "cut", "cuts", "downgrade", "fine", "lawsuit", "crisis", "bankruptcy",
    "layoff", "layoffs", "warning", "debt", "default", "fraud", "probe",
    "miss", "misses", "weak", "decline", "selloff",
]

_POS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in (_POSITIVOS + _POSITIVOS_EN)) + r")\b"
)
_NEG_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in (_NEGATIVOS + _NEGATIVOS_EN)) + r")\b"
)


# ═══════════════════════════════════════════════════════════════════
# 🏷️ CLASIFICADOR DE SENTIMIENTO (léxico)
# ═══════════════════════════════════════════════════════════════════

def clasificar_sentimiento(texto: str) -> Tuple[str, int]:
    """
    Puntúa un titular: +1 por término positivo, −1 por negativo (con límites de palabra).

    Retorna (etiqueta, score): etiqueta ∈ {Positivo, Neutral, Negativo}.
    """
    t = (texto or "").lower()
    t = re.sub(r"[^a-záéíóúüñ0-9 %]", " ", t)
    score = len(_POS_RE.findall(t)) - len(_NEG_RE.findall(t))
    if score > 0:
        return "Positivo", score
    if score < 0:
        return "Negativo", score
    return "Neutral", score


def resumen_sentimiento(noticias: List[Dict]) -> Dict:
    """Agrega el balance de sentimiento de una lista de noticias."""
    pos = sum(1 for n in noticias if n.get("sentimiento") == "Positivo")
    neg = sum(1 for n in noticias if n.get("sentimiento") == "Negativo")
    neu = sum(1 for n in noticias if n.get("sentimiento") == "Neutral")
    total = max(1, len(noticias))
    if pos > neg:
        etiqueta, icono = "Favorable", "🟢"
    elif neg > pos:
        etiqueta, icono = "Desfavorable", "🔴"
    else:
        etiqueta, icono = "Mixto", "🟡"
    return {
        "positivas": pos, "negativas": neg, "neutrales": neu,
        "total": len(noticias),
        "pct_pos": round(100.0 * pos / total, 1),
        "pct_neg": round(100.0 * neg / total, 1),
        "etiqueta": etiqueta, "icono": icono,
    }


# ═══════════════════════════════════════════════════════════════════
# 📰 OBTENCIÓN DE NOTICIAS
# ═══════════════════════════════════════════════════════════════════

def _fmt_fecha_rfc822(raw: str) -> str:
    """Convierte pubDate RFC-822 a 'DD/MM/YYYY'."""
    try:
        return parsedate_to_datetime(raw).strftime("%d/%m/%Y")
    except Exception:
        return (raw or "")[:16]


def _google_news_rss(nombre: str, ticker: str, max_items: int) -> List[Dict]:
    """Titulares de Google News RSS (español, España)."""
    base = ticker.replace(".MC", "").strip()
    consulta = quote(f'"{nombre}" {base} bolsa')
    url = (f"https://news.google.com/rss/search?q={consulta}"
           f"&hl=es&gl=ES&ceid=ES:es")
    resp = requests.get(url, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    noticias = []
    for item in root.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        if not titulo:
            continue
        fuente = (item.findtext("source") or "").strip()
        fecha = _fmt_fecha_rfc822(item.findtext("pubDate") or "")
        link = (item.findtext("link") or "").strip()
        # El formato de Google suele ser "Titular - Fuente": limpiar sufijo
        if fuente and titulo.endswith(f" - {fuente}"):
            titulo = titulo[: -len(f" - {fuente}")].strip()
        sentimiento, score = clasificar_sentimiento(titulo)
        noticias.append({
            "titulo": titulo, "fuente": fuente or "Google News",
            "fecha": fecha, "link": link,
            "sentimiento": sentimiento, "score": score,
        })
        if len(noticias) >= max_items:
            break
    return noticias


def _yfinance_news(ticker: str, max_items: int) -> List[Dict]:
    """Fallback: noticias vía yfinance (sin clave)."""
    import yfinance as yf

    noticias = []
    for n in (yf.Ticker(ticker).news or [])[:max_items]:
        contenido = n.get("content", n)  # yfinance >= 0.2.40 anida en content
        titulo = (contenido.get("title") or "").strip()
        if not titulo:
            continue
        fuente = (contenido.get("provider", {}) or {}).get("displayName", "Yahoo")
        fecha_raw = contenido.get("pubDate") or contenido.get("displayTime") or ""
        try:
            fecha = parsedate_to_datetime(fecha_raw).strftime("%d/%m/%Y")
        except Exception:
            fecha = str(fecha_raw)[:10]
        link = (contenido.get("canonicalUrl", {}) or {}).get("url", "")
        sentimiento, score = clasificar_sentimiento(titulo)
        noticias.append({
            "titulo": titulo, "fuente": fuente, "fecha": fecha,
            "link": link, "sentimiento": sentimiento, "score": score,
        })
    return noticias


@st.cache_data(ttl=3600, show_spinner=False)
def obtener_noticias(nombre: str, ticker: str, max_items: int = 12) -> List[Dict]:
    """
    Titulares recientes de una empresa con sentimiento clasificado.
    Cascada: Google News RSS → yfinance. Nunca lanza excepción.
    """
    try:
        noticias = _google_news_rss(nombre, ticker, max_items)
        if noticias:
            return noticias
    except Exception:
        pass
    try:
        return _yfinance_news(ticker, max_items)
    except Exception:
        return []


def _google_news_rss_libre(consulta: str, max_items: int, lang: str = "es") -> List[Dict]:
    """Búsqueda abierta en Google News RSS (español o inglés) sin API key."""
    q = quote((consulta or "").strip())
    if not q:
        return []
    if lang == "en":
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    else:
        url = f"https://news.google.com/rss/search?q={q}&hl=es&gl=ES&ceid=ES:es"
    resp = requests.get(url, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    noticias: List[Dict] = []
    for item in root.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        if not titulo:
            continue
        fuente = (item.findtext("source") or "").strip()
        fecha = _fmt_fecha_rfc822(item.findtext("pubDate") or "")
        link = (item.findtext("link") or "").strip()
        if fuente and titulo.endswith(f" - {fuente}"):
            titulo = titulo[: -len(f" - {fuente}")].strip()
        sentimiento, score = clasificar_sentimiento(titulo)
        noticias.append({
            "titulo": titulo,
            "fuente": fuente or "Google News",
            "fecha": fecha,
            "link": link,
            "sentimiento": sentimiento,
            "score": score,
        })
        if len(noticias) >= max_items:
            break
    return noticias


@st.cache_data(ttl=1800, show_spinner=False)
def buscar_actualidad_web(consulta: str, max_items: int = 8) -> List[Dict]:
    """
    Busca noticias y actualidad económica/financiera abierta en la web (gratis,
    sin clave ni facturación) vía Google News RSS en español con respaldo global.
    """
    q_clean = (consulta or "").strip()
    if not q_clean:
        return []
    resultados: List[Dict] = []
    vistos: set = set()
    for lang in ("es", "en"):
        if len(resultados) >= max_items:
            break
        if lang == "en" and len(resultados) >= 3:
            break
        try:
            items = _google_news_rss_libre(q_clean, max_items=max_items, lang=lang)
            for it in items:
                clave = it["titulo"].lower()
                if clave not in vistos:
                    vistos.add(clave)
                    resultados.append(it)
                    if len(resultados) >= max_items:
                        break
        except Exception:
            continue
    return resultados
