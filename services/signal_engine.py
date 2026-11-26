"""
Motor de Señales de IA — StockLab v5-frozen
===========================================
Genera señales direccionales (ALCISTA / LATERAL / BAJISTA) para los horizontes
1D / 3D / 1S / 1M usando arquitectura de 3 capas ortogonales enriquecida con
5 mejoras de rigor institucional:

  Capa 1 — Dirección: RSI, MACD, Tendencia (SMA+EMA), Momentum (ROC normalizado)
  Capa 2 — Confirmación: Volumen (OBV + RVOL) valida o penaliza la señal
  Capa 3 — Régimen, Filtros e Inteligencia Institucional:
           - ADX modula pesos dinámicos y ATR calibra bandas
           - Bollinger Squeeze ajusta confianza, S/R sesga el score final
           - 1. Marea de Mercado IBEX 35 (^IBEX): amortigua contratendencia y exige consenso n>=4
           - 2. Fuerza Relativa vs IBEX (Mansfield / Minervini RS 20d) para sesgo táctico
           - 3. Coherencia Multi-temporal (Triple Screen de Alexander Elder: 1D/3D no contradicen 1M)
           - 4. Sentimiento de Noticias en Horizontes Cortos (1D/3D vía titular scoring)
           - 5. Feedback Loop Adaptativo (calibración empírica según historial en base de datos)

Arquitectura congelada (v5-frozen):
  - Componentes normalizados a [−2, +2] con funciones continuas.
  - Consenso direccional adaptativo (n>=3 o n>=4 según marea o volatilidad histórica).
  - Confianza calibrada dinámicamente según predictibilidad empírica del activo.
  - Todas las funciones son puras o usan caché corto (5 min) coherente con la app.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

from services.data_provider import get_price_batch
from utils.market_calendar import bdate_range_bme

# ── Parámetros del modelo ───────────────────────────────────────────────────

MOTOR_VERSION = "v5-frozen"

HORIZONTES: Dict[str, int] = {"1D": 1, "3D": 3, "1S": 5, "1M": 20}

# Pesos DINÁMICOS según régimen de mercado detectado por ADX.
# Solo componentes direccionales; volumen actúa como multiplicador externo.
# Cada tabla suma 1.0 para los 4 componentes direccionales.

PESOS_TENDENCIA: Dict[str, Dict[str, float]] = {
    # ADX > 25: mercado en tendencia → momentum y tendencia dominan, RSI pierde peso.
    "1D": {"rsi": 0.05, "macd": 0.25, "tendencia": 0.30, "momentum": 0.40},
    "3D": {"rsi": 0.05, "macd": 0.20, "tendencia": 0.30, "momentum": 0.45},
    "1S": {"rsi": 0.05, "macd": 0.15, "tendencia": 0.35, "momentum": 0.45},
    "1M": {"rsi": 0.05, "macd": 0.10, "tendencia": 0.35, "momentum": 0.50},
}

PESOS_RANGO: Dict[str, Dict[str, float]] = {
    # ADX < 20: mercado lateral / rango → RSI reversión domina, momentum baja.
    "1D": {"rsi": 0.30, "macd": 0.30, "tendencia": 0.20, "momentum": 0.20},
    "3D": {"rsi": 0.25, "macd": 0.25, "tendencia": 0.25, "momentum": 0.25},
    "1S": {"rsi": 0.20, "macd": 0.20, "tendencia": 0.30, "momentum": 0.30},
    "1M": {"rsi": 0.15, "macd": 0.15, "tendencia": 0.35, "momentum": 0.35},
}

# Pesos legacy para interpolación (zona transicional ADX 20-25)
PESOS_TRANSICION: Dict[str, Dict[str, float]] = {
    "1D": {"rsi": 0.15, "macd": 0.28, "tendencia": 0.25, "momentum": 0.32},
    "3D": {"rsi": 0.12, "macd": 0.23, "tendencia": 0.27, "momentum": 0.38},
    "1S": {"rsi": 0.10, "macd": 0.18, "tendencia": 0.32, "momentum": 0.40},
    "1M": {"rsi": 0.08, "macd": 0.12, "tendencia": 0.35, "momentum": 0.45},
}

# Peso del tilt fundamental (solo horizonte 1M, se aplica externamente)
PESO_FUNDAMENTAL_1M = 0.20

UMBRAL_SENAL = 0.60       # |S| mínimo para señal direccional
MIN_CONSENSO = 3          # mín. componentes alineados para señal direccional
# Umbrales de zona lateral por horizonte (±%)
# Por encima del umbral → ALCISTA, por debajo → BAJISTA, dentro → LATERAL
UMBRALES_LATERAL = {"1D": 0.8, "3D": 1.2, "1S": 1.8, "1M": 3.0}
MIN_BARRAS = 35           # barras mínimas para MACD fiable (26+9)

# Umbrales de régimen de mercado (ADX)
ADX_TENDENCIA = 25.0      # ADX > 25 = mercado en tendencia fuerte
ADX_RANGO = 20.0          # ADX < 20 = mercado en rango / lateral

# Confirmación de volumen: multiplicadores aplicados al score
VOL_CONFIRM_BOOST = 1.10     # +10% si OBV confirma + RVOL alto
VOL_NO_CONFIRM_DAMP = 0.70   # -30% si volumen contradice la señal

SENAL_ALCISTA = "ALCISTA"
SENAL_LATERAL = "LATERAL"
SENAL_BAJISTA = "BAJISTA"

# ── Benchmark y Marea de Mercado (IBEX 35) ──────────────────────────────────
TICKER_BENCHMARK = "^IBEX"
MAREA_ALCISTA = "ALCISTA"
MAREA_NEUTRA = "NEUTRA"
MAREA_BAJISTA = "BAJISTA"

_PARAMS_V5_FROZEN = {
    "MOTOR_VERSION": MOTOR_VERSION,
    "HORIZONTES": HORIZONTES,
    "PESOS_TENDENCIA": PESOS_TENDENCIA,
    "PESOS_RANGO": PESOS_RANGO,
    "PESOS_TRANSICION": PESOS_TRANSICION,
    "PESO_FUNDAMENTAL_1M": PESO_FUNDAMENTAL_1M,
    "UMBRAL_SENAL": UMBRAL_SENAL,
    "MIN_CONSENSO": MIN_CONSENSO,
    "UMBRALES_LATERAL": UMBRALES_LATERAL,
    "MIN_BARRAS": MIN_BARRAS,
    "ADX_TENDENCIA": ADX_TENDENCIA,
    "ADX_RANGO": ADX_RANGO,
    "VOL_CONFIRM_BOOST": VOL_CONFIRM_BOOST,
    "VOL_NO_CONFIRM_DAMP": VOL_NO_CONFIRM_DAMP,
}

PARAMS_HASH: str = hashlib.sha256(
    json.dumps(_PARAMS_V5_FROZEN, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


FASE_1_CONGELADA: bool = True
FEEDBACK_LOOP_ACTIVO: bool = False


def get_params_hash() -> str:
    """Devuelve el hash SHA-256 determinista de los parametros y pesos de MOTOR_VERSION."""
    return PARAMS_HASH


def calcular_params_hash_motor() -> str:
    """Devuelve el hash SHA-256 determinista oficial de los parametros del motor v5-frozen."""
    return get_params_hash()


def verificar_estado_congelado_v5() -> Dict[str, Any]:
    """
    Devuelve el estado verificable del congelado de Fase 1 para el motor de senales.
    Garantiza que v5-frozen es 100% determinista y estacionario sin feedback loops.
    """
    return {
        "motor_version": MOTOR_VERSION,
        "fase_1_congelada": FASE_1_CONGELADA,
        "usar_calibracion_default": False,
        "params_hash_oficial": calcular_params_hash_motor(),
        "feedback_loop_activo": FEEDBACK_LOOP_ACTIVO,
    }


# ═══════════════════════════════════════════════════════════════════
# 📐 INDICADORES TÉCNICOS — CAPA 1: DIRECCIÓN
# ═══════════════════════════════════════════════════════════════════

def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI de Wilder (suavizado exponencial, alpha=1/n)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _macd_hist(close: pd.Series) -> pd.Series:
    """Histograma MACD (12,26,9) = MACD - señal."""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    senal = macd.ewm(span=9, adjust=False).mean()
    return macd - senal


def _ema_cross_bonus(close: pd.Series, fast: int = 9, slow: int = 21) -> float:
    """
    Bonus/penalty por cruce EMA reciente (últimas 3 sesiones).
    +0.3 cruce alcista reciente, -0.3 cruce bajista, 0 sin cruce.
    Actúa como sub-componente de la tendencia para horizontes cortos.
    """
    if len(close) < slow + 4:
        return 0.0
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    diff = ema_f - ema_s
    if len(diff) < 4:
        return 0.0
    # Cruce = cambio de signo en las últimas 3 sesiones
    curr_pos = float(diff.iloc[-1]) > 0
    prev_pos = float(diff.iloc[-4]) > 0
    if curr_pos and not prev_pos:
        return 0.3   # cruce alcista reciente
    if not curr_pos and prev_pos:
        return -0.3  # cruce bajista reciente
    return 0.0


# ═══════════════════════════════════════════════════════════════════
# 📐 INDICADORES TÉCNICOS — CAPA 2: CONFIRMACIÓN DE VOLUMEN
# ═══════════════════════════════════════════════════════════════════

def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume — acumulativo, confirma tendencias con participación."""
    direction = np.sign(close.diff()).fillna(0)
    return (volume * direction).cumsum()


def _rvol(volume: pd.Series, n: int = 20) -> float:
    """
    Volumen Relativo = volumen actual / media(n sesiones previas).
    RVOL > 1.5 = participación institucional activa / breakout real.
    RVOL < 0.5 = movimiento sin respaldo de volumen.
    """
    if len(volume) < n + 1 or volume.iloc[-1] <= 0:
        return 1.0
    avg = float(volume.iloc[-(n + 1):-1].mean())
    return float(volume.iloc[-1] / avg) if avg > 0 else 1.0


def _confirmar_volumen(close: pd.Series, volume: pd.Series,
                       score_dir: float) -> float:
    """
    Evalúa si el volumen confirma la dirección de la señal.

    Retorna un multiplicador ∈ [VOL_NO_CONFIRM_DAMP, VOL_CONFIRM_BOOST]:
      - OBV alineado con la señal + RVOL alto → boost
      - OBV divergente o RVOL bajo → dampening

    Lógica:
      1. Tendencia OBV: pendiente OBV últimas 10 sesiones
      2. RVOL: volumen relativo respecto a media 20d
      3. Score de confirmación: combina ambas señales
    """
    if len(close) < 12 or len(volume) < 12:
        return 1.0  # sin datos suficientes, neutral

    obv_s = _obv(close, volume)
    rvol = _rvol(volume, 20)

    # Tendencia OBV: regresión lineal simple sobre últimas 10 sesiones
    obv_tail = obv_s.iloc[-10:]
    if len(obv_tail) < 5:
        return 1.0
    x = np.arange(len(obv_tail), dtype=float)
    y = obv_tail.values.astype(float)
    # Normalizar por media de volumen para evitar escala absoluta
    vol_mean = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else 1.0
    if vol_mean <= 0:
        vol_mean = 1.0
    pendiente = float(np.polyfit(x, y, 1)[0]) / vol_mean

    # Alineación: pendiente OBV y score_dir deben tener el mismo signo
    if abs(score_dir) < 0.15:
        # Señal muy débil / lateral: volumen no modifica
        return 1.0

    misma_dir = (pendiente > 0 and score_dir > 0) or (pendiente < 0 and score_dir < 0)
    vol_factor = 1.0

    if misma_dir:
        # Volumen confirma la señal
        if rvol >= 1.5:
            vol_factor = VOL_CONFIRM_BOOST       # boost fuerte
        elif rvol >= 1.0:
            vol_factor = 1.0 + 0.05 * (rvol - 1.0)  # boost suave
        else:
            vol_factor = 1.0                     # confirma pero sin fuerza
    else:
        # Volumen contradice la señal
        if rvol >= 1.5:
            vol_factor = VOL_NO_CONFIRM_DAMP     # divergencia fuerte
        elif rvol >= 1.0:
            vol_factor = 0.85                    # divergencia suave
        else:
            vol_factor = 0.90                    # sin volumen = poca fiabilidad

    return float(np.clip(vol_factor, VOL_NO_CONFIRM_DAMP, VOL_CONFIRM_BOOST))


# ═══════════════════════════════════════════════════════════════════
# 📐 INDICADORES TÉCNICOS — CAPA 3: RÉGIMEN Y FILTROS
# ═══════════════════════════════════════════════════════════════════

def _atr(high: pd.Series, low: pd.Series, close: pd.Series,
         n: int = 14) -> pd.Series:
    """
    Average True Range — captura volatilidad real incluyendo gaps overnight.
    Reemplaza ret.std() para estimación de bandas más realista.
    """
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series,
         n: int = 14) -> float:
    """
    Average Directional Index — mide FUERZA de tendencia, NO dirección.
    ADX > 25 = tendencia fuerte (usar pesos de tendencia).
    ADX < 20 = mercado lateral / rango (usar pesos de rango / reversión).
    ADX 20-25 = zona transicional (interpolar pesos).

    Implementación Wilder: +DM/-DM → +DI/-DI → DX → ADX (suavizado).
    """
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_s = tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    safe_atr = atr_s.replace(0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / n, min_periods=n, adjust=False).mean() / safe_atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / n, min_periods=n, adjust=False).mean() / safe_atr

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx_s = dx.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()

    val = adx_s.iloc[-1] if len(adx_s) > 0 else 20.0
    return float(val) if pd.notna(val) and np.isfinite(val) else 20.0


def _bollinger_squeeze(close: pd.Series, n: int = 20,
                       k: float = 2.0) -> Tuple[float, float, bool]:
    """
    Detecta Bollinger Squeeze (contracción de volatilidad).
    Retorna (bandwidth%, %B, squeeze_activo).

    Squeeze activo = bandwidth actual < 75% de su media → indica que
    un movimiento explosivo es probable, pero NO indica dirección.
    Modula la CONFIANZA, no el SCORE.
    """
    sma = close.rolling(n).mean()
    std = close.rolling(n).std()
    upper = sma + k * std
    lower = sma - k * std
    bandwidth = ((upper - lower) / sma * 100.0).dropna()

    if len(bandwidth) < n:
        return 5.0, 0.5, False

    bw_val = float(bandwidth.iloc[-1])
    bw_avg = float(bandwidth.rolling(n).mean().iloc[-1])

    # %B: posición del precio dentro de las bandas [0, 1]
    ub = float(upper.iloc[-1])
    lb = float(lower.iloc[-1])
    rng = max(ub - lb, 0.001)
    pct_b = float(np.clip((close.iloc[-1] - lb) / rng, 0.0, 1.0))

    squeeze = bw_avg > 0 and bw_val < bw_avg * 0.75
    return bw_val, pct_b, squeeze


def _detectar_soportes_resistencias(high: pd.Series, low: pd.Series,
                                    close: pd.Series,
                                    ventana: int = 5) -> Dict:
    """
    Detecta niveles de soporte/resistencia como pivots locales (máx/mín relativos).

    Si el precio está a < 1% de una resistencia → sr_bias negativo (frena alcistas).
    Si el precio está a < 1% de un soporte → sr_bias positivo (frena bajistas).

    Retorna {resistencia, soporte, sr_bias ∈ [-0.5, +0.5]}.
    """
    precio = float(close.iloc[-1])
    h = high.values.astype(float)
    l = low.values.astype(float)
    n = len(h)

    resistencias: List[float] = []
    soportes: List[float] = []

    # Buscar pivots excluyendo las últimas 3 barras (ruido reciente)
    for i in range(ventana, max(0, n - ventana - 3)):
        seg_h = h[max(0, i - ventana):min(n, i + ventana + 1)]
        seg_l = l[max(0, i - ventana):min(n, i + ventana + 1)]
        if h[i] == seg_h.max():
            resistencias.append(float(h[i]))
        if l[i] == seg_l.min():
            soportes.append(float(l[i]))

    res_cercana = None
    sop_cercano = None

    # Filtrar niveles por encima / debajo del precio actual
    res_arriba = [r for r in resistencias if r > precio * 1.001]
    sop_abajo = [s for s in soportes if s < precio * 0.999]

    if res_arriba:
        res_cercana = min(res_arriba)
    if sop_abajo:
        sop_cercano = max(sop_abajo)

    # Calcular sesgo S/R
    sr_bias = 0.0
    if res_cercana and precio > 0:
        dist_res = (res_cercana - precio) / precio * 100.0
        if dist_res < 1.0:
            sr_bias -= 0.3 * max(0.0, 1.0 - dist_res)
    if sop_cercano and precio > 0:
        dist_sop = (precio - sop_cercano) / precio * 100.0
        if dist_sop < 1.0:
            sr_bias += 0.3 * max(0.0, 1.0 - dist_sop)

    return {
        "resistencia": res_cercana,
        "soporte": sop_cercano,
        "sr_bias": float(np.clip(sr_bias, -0.5, 0.5)),
    }


# ═══════════════════════════════════════════════════════════════════
# 🌊 MAREA DE MERCADO — BENCHMARK IBEX 35
# ═══════════════════════════════════════════════════════════════════

def calcular_marea_mercado(df_ibex: Optional[pd.DataFrame]) -> Dict:
    """
    Evalúa la salud y tendencia del índice de referencia (IBEX 35).
    Aproximadamente 3 de cada 4 acciones siguen la marea del mercado general.
    Si el IBEX está en fase correctiva (bajo SMA20 y SMA50 con MACD negativo),
    las roturas alcistas individuales tienen alta probabilidad de fallo (bull traps).

    Criterios cuantitativos:
      1. Precio vs SMA20 (impulso a corto plazo)
      2. Precio vs SMA50 (tendencia intermedia)
      3. Estructura SMA20 vs SMA50 (alineación tendencial)
      4. Histograma MACD (aceleración/desaceleración)
      5. Retorno 20 sesiones (momentum mensual)

    Retorna:
      {
        "estado": "ALCISTA" | "NEUTRA" | "BAJISTA",
        "score": float in [-1.0, +1.0],
        "precio": float,
        "sma20": float,
        "sma50": float,
        "var20d": float,
        "macd_h": float,
        "descripcion": str
      }
    """
    if df_ibex is None or df_ibex.empty or "Close" not in df_ibex.columns:
        return {
            "estado": MAREA_NEUTRA, "score": 0.0, "precio": 0.0,
            "sma20": 0.0, "sma50": 0.0, "var20d": 0.0, "macd_h": 0.0,
            "descripcion": "Índice de referencia no disponible (marea neutra)",
        }

    close = df_ibex["Close"].dropna()
    if len(close) < 20:
        p_val = float(close.iloc[-1]) if len(close) > 0 else 0.0
        return {
            "estado": MAREA_NEUTRA, "score": 0.0, "precio": p_val,
            "sma20": 0.0, "sma50": 0.0, "var20d": 0.0, "macd_h": 0.0,
            "descripcion": "Datos insuficientes del índice de referencia",
        }

    precio = float(close.iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else precio
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else sma20
    var20d = float((precio / float(close.iloc[-20]) - 1.0) * 100.0) if len(close) >= 20 else 0.0
    macd_h_s = _macd_hist(close)
    macd_h = float(macd_h_s.iloc[-1]) if len(macd_h_s) > 0 else 0.0

    pts = 0.0
    pts += 0.35 if precio > sma20 else -0.35
    pts += 0.35 if precio > sma50 else -0.35
    pts += 0.30 if sma20 > sma50 else -0.30
    pts += 0.30 if macd_h > 0 else -0.30
    pts += 0.20 if var20d > 0 else -0.20

    marea_score = float(np.clip(pts / 1.5, -1.0, 1.0))

    if marea_score >= 0.25:
        estado = MAREA_ALCISTA
        desc = f"Marea alcista en IBEX 35 (precio > medias, impulso positivo {var20d:+.1f}%)"
    elif marea_score <= -0.25:
        estado = MAREA_BAJISTA
        desc = f"Marea bajista en IBEX 35 (mercado en corrección {var20d:+.1f}%, precaución)"
    else:
        estado = MAREA_NEUTRA
        desc = "Marea neutra en IBEX 35 (mercado lateral o en consolidación)"

    return {
        "estado": estado,
        "score": round(marea_score, 3),
        "precio": round(precio, 2),
        "sma20": round(sma20, 2),
        "sma50": round(sma50, 2),
        "var20d": round(var20d, 2),
        "macd_h": round(macd_h, 4),
        "descripcion": desc,
    }


@st.cache_data(ttl=300, show_spinner=False)
def obtener_marea_mercado_actual() -> Dict:
    """Obtiene de forma cacheada el estado de la marea de mercado (IBEX 35)."""
    try:
        df_batch = get_price_batch((TICKER_BENCHMARK,))
        sub = obtener_serie_ticker(df_batch, TICKER_BENCHMARK)
        return calcular_marea_mercado(sub)
    except Exception:
        return {
            "estado": MAREA_NEUTRA, "score": 0.0, "precio": 0.0,
            "sma20": 0.0, "sma50": 0.0, "var20d": 0.0, "macd_h": 0.0,
            "descripcion": "Fallback: marea neutra",
        }


def obtener_score_sentimiento(ticker: str, nombre: str = "") -> float:
    """
    Obtiene el balance de sentimiento [-1.0, +1.0] a partir de titulares recientes.
    Usa caché de 1h de services.news_sentiment. Fallback seguro a 0.0 si la red falla.
    """
    try:
        from services.news_sentiment import obtener_noticias, resumen_sentimiento
        noticias = obtener_noticias(nombre or ticker, ticker, max_items=8)
        if not noticias:
            return 0.0
        res = resumen_sentimiento(noticias)
        pos = res.get("positivas", 0)
        neg = res.get("negativas", 0)
        if pos + neg == 0:
            return 0.0
        return float(np.clip((pos - neg) / float(pos + neg), -1.0, 1.0))
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════════
# 🧮 CÁLCULO INTEGRADO DE INDICADORES
# ═══════════════════════════════════════════════════════════════════

def calcular_indicadores(df_ticker: pd.DataFrame) -> Optional[Dict]:
    """
    Calcula el paquete completo de indicadores a partir de OHLCV.

    v3-enhanced: expande el paquete original (RSI, MACD, SMA, MOM) con
    ADX, ATR, OBV, RVOL, Bollinger, EMA cross y S/R para la arquitectura
    de 3 capas.

    Retorna None si hay menos de MIN_BARRAS barras útiles.
    """
    if df_ticker is None or df_ticker.empty or "Close" not in df_ticker.columns:
        return None

    close = df_ticker["Close"].dropna()
    if len(close) < MIN_BARRAS:
        return None

    # Extraer series OHLV (con fallbacks seguros)
    high = df_ticker["High"].dropna() if "High" in df_ticker.columns else close
    low = df_ticker["Low"].dropna() if "Low" in df_ticker.columns else close
    volume = df_ticker["Volume"].fillna(0) if "Volume" in df_ticker.columns else pd.Series(
        0, index=close.index)

    # Alinear índices (High/Low pueden tener NaN en fechas distintas a Close)
    idx = close.index
    high = high.reindex(idx).ffill().bfill()
    low = low.reindex(idx).ffill().bfill()
    volume = volume.reindex(idx).fillna(0)

    # ── Capa 1: Dirección ───────────────────────────────────────────
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    rsi_s = _rsi(close)
    macd_h = _macd_hist(close)

    precio = float(close.iloc[-1])
    previo = float(close.iloc[-2]) if len(close) > 1 else precio
    ret = close.pct_change().dropna()
    vol_d = float(ret.std()) if len(ret) > 5 else 0.01

    sma20_v = float(sma20.iloc[-1]) if pd.notna(sma20.iloc[-1]) else precio
    sma50_v = float(sma50.iloc[-1]) if pd.notna(sma50.iloc[-1]) else sma20_v

    def _mom(dias: int) -> float:
        if len(close) > dias and close.iloc[-1 - dias] > 0:
            return (precio / float(close.iloc[-1 - dias]) - 1.0) * 100.0
        return 0.0

    ema_cross = _ema_cross_bonus(close)

    # ── Capa 2: Confirmación de Volumen ─────────────────────────────
    rvol = _rvol(volume, 20)

    # ── Capa 3: Régimen y Filtros ───────────────────────────────────
    # ATR(14) normalizado como % del precio → volatilidad real diaria
    atr_s = _atr(high, low, close)
    atr_val = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else precio * vol_d
    atr_d = atr_val / precio if precio > 0 else vol_d  # ATR diario como fracción

    # ADX para detección de régimen
    adx_val = _adx(high, low, close) if len(close) >= 30 else 20.0

    # Bollinger Squeeze
    bb_bw, bb_pctb, bb_squeeze = _bollinger_squeeze(close)

    # Soporte / Resistencia
    sr = _detectar_soportes_resistencias(high, low, close)

    return {
        # Capa 1: Dirección
        "close": precio,
        "previo": previo,
        "var_dia": (precio / previo - 1.0) * 100.0 if previo > 0 else 0.0,
        "rsi": float(rsi_s.iloc[-1]) if pd.notna(rsi_s.iloc[-1]) else 50.0,
        "macd_h": float(macd_h.iloc[-1]) if pd.notna(macd_h.iloc[-1]) else 0.0,
        "macd_h_prev": float(macd_h.iloc[-2]) if len(macd_h) > 1 and pd.notna(macd_h.iloc[-2]) else 0.0,
        "sma20": sma20_v,
        "sma50": sma50_v,
        "mom5": _mom(5),
        "mom20": _mom(20),
        "ema_cross": ema_cross,

        # Capa 2: Volumen
        "rvol": round(rvol, 2),
        "_close_series": close,      # series internas para _confirmar_volumen
        "_volume_series": volume,

        # Capa 3: Régimen y Filtros
        "vol_d": vol_d,              # σ diaria (retornos) — legacy
        "atr_d": atr_d,              # ATR diaria como fracción del precio
        "adx": round(adx_val, 1),
        "bb_bandwidth": round(bb_bw, 2),
        "bb_pctb": round(bb_pctb, 3),
        "bb_squeeze": bb_squeeze,
        "sr_resistencia": sr["resistencia"],
        "sr_soporte": sr["soporte"],
        "sr_bias": sr["sr_bias"],
        "ultima_fecha": close.index[-1],
    }


# ═══════════════════════════════════════════════════════════════════
# 🔧 COMPONENTES DIRECCIONALES NORMALIZADOS [-2, +2]
# ═══════════════════════════════════════════════════════════════════

def _componentes(ind: Dict) -> Dict[str, float]:
    """
    Traduce los indicadores crudos a componentes normalizados [−2, +2].

    v3-enhanced:
      - RSI: momentum contextual con reversión suave en extremos
      - MACD: normalizado por volatilidad (ATR) para comparabilidad cross-asset
      - Tendencia: distancia a SMAs + estructura + bonus EMA 9/21
      - Momentum: ROC normalizado por volatilidad (z-score) en lugar de magnitud absoluta
    """
    close = ind["close"]
    # Usar ATR como volatilidad base (captura gaps), con floor de 0.5%
    atr_d = max(ind.get("atr_d", ind["vol_d"]), 0.005)
    vol_d = max(ind["vol_d"], 0.005)

    # ── RSI: Momentum contextual (NO contrarian puro) ──────────────
    rsi = ind["rsi"]
    c_rsi = float(np.clip((rsi - 50.0) / 20.0, -2.0, 2.0))
    # Reversión suave solo en extremos (±0.4 máx)
    if rsi < 20:
        c_rsi += 0.4 * (20.0 - rsi) / 20.0
    elif rsi > 80:
        c_rsi -= 0.4 * (rsi - 80.0) / 20.0
    c_rsi = float(np.clip(c_rsi, -2.0, 2.0))

    # ── MACD: Normalizado por ATR del activo ─────────────────────
    h = ind["macd_h"]
    norm = close * atr_d  # escala natural basada en ATR
    c_macd = float(np.clip(h / norm * 5.0, -2.0, 2.0)) if norm > 0 else 0.0

    # ── Tendencia: Distancia proporcional + estructura + EMA cross ──
    sma20, sma50 = ind["sma20"], ind["sma50"]
    dist20 = (close - sma20) / sma20 * 100.0 if sma20 > 0 else 0.0
    dist50 = (close - sma50) / sma50 * 100.0 if sma50 > 0 else 0.0
    sma_struct = (sma20 - sma50) / sma50 * 100.0 if sma50 > 0 else 0.0
    raw_tend = dist20 * 0.35 + dist50 * 0.25 + sma_struct * 0.25
    # Bonus EMA 9/21 (±0.3): potencia la tendencia cuando hay cruce reciente
    raw_tend += ind.get("ema_cross", 0.0) * 3.0  # escalado a rango raw_tend
    c_tendencia = float(np.clip(raw_tend / 3.0, -2.0, 2.0))

    # ── Momentum: ROC normalizado por volatilidad (z-score) ───────
    # En lugar de clippear magnitud absoluta, normalizamos por σ√t
    # para que activos de alta vol no saturen el componente.
    mom5, mom20 = ind["mom5"], ind["mom20"]
    sigma5 = vol_d * np.sqrt(5) * 100.0
    sigma20 = vol_d * np.sqrt(20) * 100.0
    z_mom5 = mom5 / sigma5 if sigma5 > 0 else 0.0
    z_mom20 = mom20 / sigma20 if sigma20 > 0 else 0.0
    c_mom5 = float(np.clip(z_mom5, -2.0, 2.0))
    c_mom20 = float(np.clip(z_mom20, -2.0, 2.0))
    c_momentum = 0.6 * c_mom5 + 0.4 * c_mom20

    return {"rsi": c_rsi, "macd": c_macd,
            "tendencia": c_tendencia, "momentum": c_momentum}


def _pesos_dinamicos(horizonte: str, adx: float) -> Dict[str, float]:
    """
    Selecciona los pesos óptimos para cada horizonte según el régimen
    de mercado detectado por el ADX.

    ADX > 25:     pesos de tendencia (momentum/tendencia dominan)
    ADX < 20:     pesos de rango (RSI reversión domina)
    ADX 20-25:    interpolación lineal entre rango y tendencia
    """
    if adx >= ADX_TENDENCIA:
        return PESOS_TENDENCIA[horizonte]
    elif adx <= ADX_RANGO:
        return PESOS_RANGO[horizonte]
    else:
        # Zona transicional: interpolar linealmente
        alpha = (adx - ADX_RANGO) / (ADX_TENDENCIA - ADX_RANGO)
        w_rango = PESOS_RANGO[horizonte]
        w_trend = PESOS_TENDENCIA[horizonte]
        return {
            k: round(w_rango[k] * (1 - alpha) + w_trend[k] * alpha, 4)
            for k in w_rango
        }


# ═══════════════════════════════════════════════════════════════════
# 🎯 SEÑAL POR HORIZONTE — ARQUITECTURA 3 CAPAS
# ═══════════════════════════════════════════════════════════════════

def calcular_senal(comp: Dict[str, float], vol_d: float,
                   horizonte: str, tilt_fundamental: float = 0.0,
                   adx: float = 22.0, vol_confirm: float = 1.0,
                   sr_bias: float = 0.0, bb_squeeze: bool = False,
                   atr_d: float = 0.0, marea_score: float = 0.0,
                   rs_score: float = 0.0,
                   sentiment_score: float = 0.0,
                   factor_calibracion: float = 1.0,
                   ajuste_consenso: int = 0) -> Dict:
    """
    Combina componentes con la arquitectura de 3 capas y devuelve:
      {senal, score, pct, banda, confianza, conviccion, regimen, marea, factor_calibracion}

    Capa 1 — Dirección:
      Score bruto S = Σ(peso_i × comp_i) con pesos dinámicos (ADX)

    Capa 2 — Confirmación de Volumen:
      S_ajustado = S × vol_confirm (boost/damp según OBV+RVOL)

    Capa 3 — Régimen, Filtros y Marea:
      - ADX modula pesos (ya integrado en Capa 1)
      - ATR(14) para bandas en lugar de σ clásica
      - S/R bias ajusta score final
      - Marea IBEX 35 modula viento a favor / en contra y consenso
      - Fuerza Relativa vs IBEX (RS Minervini/Mansfield) modula horizontes tácticos
      - Sentimiento de Noticias modula horizontes ultra-cortos (1D/3D)
      - Bollinger Squeeze modula confianza
      - Feedback Loop: Calibración adaptativa por historial empírico
    """
    dias = HORIZONTES[horizonte]

    # Blindaje Fase 1: v5-frozen no permite modulacion de confianza ni consenso heuristico
    if FASE_1_CONGELADA:
        factor_calibracion = 1.0
        ajuste_consenso = 0

    # ── CAPA 1: Score direccional con pesos dinámicos ───────────────
    w = _pesos_dinamicos(horizonte, adx)
    s = (w["rsi"] * comp["rsi"]
         + w["macd"] * comp["macd"]
         + w["tendencia"] * comp["tendencia"]
         + w["momentum"] * comp["momentum"])

    # Tilt fundamental (solo 1M)
    if horizonte == "1M" and abs(tilt_fundamental) > 0.01:
        s = s * (1.0 - PESO_FUNDAMENTAL_1M) + tilt_fundamental * PESO_FUNDAMENTAL_1M

    # ── CAPA 2: Multiplicador de volumen ────────────────────────────
    s_vol = s * vol_confirm

    # ── CAPA 3: Sesgo Soporte/Resistencia ───────────────────────────
    # El sesgo S/R empuja el score hacia 0 si el precio está cerca
    # de un nivel que contradice la señal (no cambia dirección, reduce magnitud).
    s_final = s_vol + sr_bias

    # ── CAPA 3: Marea de Mercado (Benchmark IBEX 35) ────────────────
    # La marea general actúa como viento a favor o en contra.
    # Si la marea es bajista, las señales alcistas se amortiguan y requieren mayor consenso.
    # Si la marea es alcista, las señales bajistas se amortiguan y requieren mayor consenso.
    if marea_score <= -0.25:
        if s_final > 0:
            s_final *= (1.0 + 0.15 * marea_score)  # frena señal alcista contraria a la marea
        min_consenso_alcista = 4                    # consenso más estricto contra corriente
        min_consenso_bajista = MIN_CONSENSO
    elif marea_score >= 0.25:
        if s_final < 0:
            s_final *= (1.0 - 0.15 * marea_score)  # frena señal bajista contraria a la marea
        min_consenso_alcista = MIN_CONSENSO
        min_consenso_bajista = 4                    # consenso más estricto contra corriente
    else:
        min_consenso_alcista = MIN_CONSENSO
        min_consenso_bajista = MIN_CONSENSO

    # Ajuste adaptativo de consenso por historial (activos erráticos exigen consenso unánime)
    if ajuste_consenso > 0:
        min_consenso_alcista = min(4, min_consenso_alcista + ajuste_consenso)
        min_consenso_bajista = min(4, min_consenso_bajista + ajuste_consenso)

    # ── CAPA 3: Fuerza Relativa vs IBEX (Mansfield / Minervini RS) ───
    # En horizontes tácticos (3D y 1S), el outperformance (+RS) confirma
    # la inercia ganadora, mientras que el underperformance (-RS) debilita
    # el impulso o refuerza señales bajistas.
    if horizonte in ("3D", "1S") and abs(rs_score) >= 0.20:
        s_final += 0.15 * rs_score

    # ── CAPA 3: Sentimiento de Noticias (Titulares Recientes) ───────
    # Afecta exclusivamente a horizontes ultra-cortos (1D y 3D), donde
    # los catalizadores de prensa tienen mayor capacidad predictiva.
    if abs(sentiment_score) >= 0.25:
        if horizonte == "1D":
            s_final += 0.15 * sentiment_score
        elif horizonte == "3D":
            s_final += 0.10 * sentiment_score

    # ── Bandas con ATR (si disponible) o σ clásica ──────────────────
    # ATR captura gaps overnight que σ de retornos subestima.
    volatilidad = atr_d if atr_d > 0 else vol_d
    banda_pct = volatilidad * np.sqrt(dias) * 100.0
    pct = (s_final / 2.0) * banda_pct
    pct = float(np.clip(pct, -2.0 * banda_pct, 2.0 * banda_pct))
    # Salida cruda del modelo ANTES de la armonización etiqueta↔umbral (solo auditoría).
    pct_raw = pct

    # ── Confianza base + modulación ─────────────────────────────────
    confianza = 50.0 + 45.0 * abs(s_final) / 2.0
    # Bollinger Squeeze: un squeeze activo indica que la volatilidad está
    # comprimida y un movimiento explosivo es probable → mayor confianza
    # si la señal ya es direccional.
    if bb_squeeze and abs(s_final) >= UMBRAL_SENAL:
        confianza = min(99.0, confianza + 5.0)

    # Calibración adaptativa por historial empírico (Feedback Loop)
    if factor_calibracion != 1.0:
        confianza = float(np.clip(confianza * factor_calibracion, 50.0, 99.0))

    # ── Consenso direccional ────────────────────────────────────────
    comp_vals = [comp["rsi"], comp["macd"], comp["tendencia"], comp["momentum"]]
    if horizonte == "1M" and abs(tilt_fundamental) > 0.01:
        comp_vals.append(tilt_fundamental)
    n_alcista = sum(1 for c in comp_vals if c > 0.15)
    n_bajista = sum(1 for c in comp_vals if c < -0.15)

    if s_final >= UMBRAL_SENAL and n_alcista >= min_consenso_alcista:
        senal = SENAL_ALCISTA
    elif s_final <= -UMBRAL_SENAL and n_bajista >= min_consenso_bajista:
        senal = SENAL_BAJISTA
    else:
        senal = SENAL_LATERAL

    # ── Coherencia etiqueta ↔ estimación (umbrales por horizonte) ────
    # La etiqueta y el porcentaje estimado deben ser mutuamente coherentes.
    # El umbral escala según el horizonte (1D: ±0.8%, 3D: ±1.2%, 1S: ±1.8%, 1M: ±3.0%).
    umbral_lat = UMBRALES_LATERAL.get((horizonte or "").upper().strip(), 0.8)
    if senal == SENAL_LATERAL:
        pct = round(float(np.clip(pct, -umbral_lat, umbral_lat)), 2)
    elif senal == SENAL_ALCISTA and pct < umbral_lat:
        pct = umbral_lat
    elif senal == SENAL_BAJISTA and pct > -umbral_lat:
        pct = -umbral_lat

    # ── Nivel de convicción ─────────────────────────────────────────
    # ALTA: señal direccional + volumen confirma + consenso fuerte
    # MEDIA: señal direccional pero sin confirmación plena
    # BAJA: lateral o señal débil
    if senal != SENAL_LATERAL:
        n_alineados = n_alcista if senal == SENAL_ALCISTA else n_bajista
        if n_alineados >= 4 and vol_confirm >= 1.0:
            conviccion = "ALTA"
        elif n_alineados >= 3 and vol_confirm >= 0.85:
            conviccion = "MEDIA"
        else:
            conviccion = "BAJA"
    else:
        conviccion = "BAJA"

    # Régimen descriptivo
    if adx >= ADX_TENDENCIA:
        regimen = "tendencia"
    elif adx <= ADX_RANGO:
        regimen = "rango"
    else:
        regimen = "mixto"

    marea_txt = "alcista" if marea_score >= 0.25 else ("bajista" if marea_score <= -0.25 else "neutra")

    return {
        "senal": senal,
        "score": round(float(s_final), 3),
        "score_bruto": float(s_final),
        "pct": round(pct, 2),
        "pct_raw": round(float(pct_raw), 4),
        "banda": round(float(banda_pct), 2),
        "vol_banda": float(volatilidad),
        "umbral_zona": float(umbral_lat),
        "confianza": round(confianza, 1),
        "conviccion": conviccion,
        "regimen": regimen,
        "marea": marea_txt,
        "factor_calibracion": round(factor_calibracion, 2),
    }


# ═══════════════════════════════════════════════════════════════════
# ⏳ COHERENCIA MULTI-TEMPORAL (TRIPLE SCREEN DE ALEXANDER ELDER)
# ═══════════════════════════════════════════════════════════════════

def aplicar_coherencia_multitemporal(senales: Dict[str, Dict], rsi: float) -> Dict[str, Dict]:
    """
    Filtro de Coherencia Multi-Temporal (Triple Screen de Alexander Elder):
    Valida la alineación entre horizontes. La tendencia de orden superior (1M)
    marca la dirección maestra; las señales de corto plazo (1D y 3D) no deben
    operar contra ella salvo condiciones de sobreventa/sobrecompra extrema.

    Reglas:
      1. Si 1M es BAJISTA: 1D y 3D no pueden ser ALCISTAS salvo si RSI < 25
         (rebote técnico extremo). Si RSI >= 25, se degradan a LATERAL.
      2. Si 1M es ALCISTA: 1D y 3D no pueden ser BAJISTAS salvo si RSI > 75
         (agotamiento extremo). Si RSI <= 75, se degradan a LATERAL.
      3. Si 1D, 1S y 1M están plenamente alineados en dirección (todas ALCISTA
         o todas BAJISTA): bonus de +5% de confianza por alineación perfecta.
    """
    if not senales or "1M" not in senales:
        return senales

    s_1m = senales["1M"]["senal"]

    # Regla 1: Contra-tendencia bajista macro
    if s_1m == SENAL_BAJISTA:
        for hz in ("1D", "3D"):
            if hz in senales and senales[hz]["senal"] == SENAL_ALCISTA:
                if rsi >= 25.0:
                    senales[hz]["senal"] = SENAL_LATERAL
                    senales[hz]["filtro_elder"] = "bloqueo_bajista_macro"
                    senales[hz]["conviccion"] = "BAJA"
                    _ulat = UMBRALES_LATERAL.get((hz or "").upper().strip(), 0.8)
                    senales[hz]["pct"] = round(float(np.clip(senales[hz]["pct"], -_ulat, _ulat)), 2)

    # Regla 2: Contra-tendencia alcista macro
    elif s_1m == SENAL_ALCISTA:
        for hz in ("1D", "3D"):
            if hz in senales and senales[hz]["senal"] == SENAL_BAJISTA:
                if rsi <= 75.0:
                    senales[hz]["senal"] = SENAL_LATERAL
                    senales[hz]["filtro_elder"] = "bloqueo_alcista_macro"
                    senales[hz]["conviccion"] = "BAJA"
                    _ulat = UMBRALES_LATERAL.get((hz or "").upper().strip(), 0.8)
                    senales[hz]["pct"] = round(float(np.clip(senales[hz]["pct"], -_ulat, _ulat)), 2)

    # Regla 3: Alineación multi-temporal perfecta (1D, 1S, 1M)
    if "1D" in senales and "1S" in senales and "1M" in senales:
        dir_1d = senales["1D"]["senal"]
        dir_1s = senales["1S"]["senal"]
        dir_1m = senales["1M"]["senal"]
        if dir_1d == dir_1s == dir_1m and dir_1m in (SENAL_ALCISTA, SENAL_BAJISTA):
            for hz in ("1D", "3D", "1S", "1M"):
                if hz in senales:
                    senales[hz]["confianza"] = min(99.0, senales[hz]["confianza"] + 5.0)
                    senales[hz]["alineacion_macro"] = True

    return senales


# ═══════════════════════════════════════════════════════════════════
# 📦 EXTRACCIÓN DE SERIES DEL BATCH
# ═══════════════════════════════════════════════════════════════════

def obtener_serie_ticker(df_batch: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """Extrae el sub-DataFrame OHLCV de un ticker del batch MultiIndex."""
    if df_batch is None or df_batch.empty:
        return None
    try:
        if isinstance(df_batch.columns, pd.MultiIndex):
            if ticker in df_batch.columns.get_level_values(0):
                sub = df_batch[ticker]
            elif ticker in df_batch.columns.get_level_values(-1):
                sub = df_batch.xs(ticker, level=-1, axis=1)
            else:
                return None
        else:
            sub = df_batch
        sub = sub.dropna(subset=["Close"])
        return sub if len(sub) >= MIN_BARRAS else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# 🚀 RADAR COMPLETO (cacheado 5 min, coherente con get_price_batch)
# ═══════════════════════════════════════════════════════════════════

def calcular_fila_ticker(sub: pd.DataFrame, marea: Dict, tilt: float = 0.0,
                         sent_val: float = 0.0,
                         calib_ticker: Optional[Dict[str, Dict]] = None,
                         ticker: str = "", nombre: str = "") -> Optional[Dict]:
    """
    Función PURA: señales de los 4 horizontes para UNA serie OHLCV.

    Extraída literalmente del bucle de calcular_radar (misma lógica, mismos
    parámetros) para que la reutilicen la emisión congelada al cierre y el
    backtester walk-forward. Solo depende de `sub` (datos ≤ t), `marea`
    (IBEX ≤ t), tilt, sentimiento y calibración, que llegan como argumentos.

    Además de las columnas históricas del radar, añade campos crudos de
    auditoría (prefijo comp_/raw) que no alteran ningún cálculo.
    """
    ind = calcular_indicadores(sub)
    if ind is None:
        return None
    ind["marea"] = marea
    ind["sentiment_score"] = sent_val

    comp = _componentes(ind)

    # Capa 2: confirmación de volumen (una vez por ticker, reutilizar)
    close_s = ind.get("_close_series")
    vol_s = ind.get("_volume_series")
    # Score promedio para evaluar dirección global
    w_ref = _pesos_dinamicos("1S", ind["adx"])
    score_ref = (w_ref["rsi"] * comp["rsi"]
                 + w_ref["macd"] * comp["macd"]
                 + w_ref["tendencia"] * comp["tendencia"]
                 + w_ref["momentum"] * comp["momentum"])
    vol_confirm = _confirmar_volumen(close_s, vol_s, score_ref) if (
        close_s is not None and vol_s is not None) else 1.0

    # Fuerza Relativa vs IBEX (20 sesiones)
    var20_ibex = marea.get("var20d", 0.0)
    rs_diff = ind["mom20"] - var20_ibex
    rs_score = float(np.clip(rs_diff / 10.0, -1.0, 1.0))
    ind["rs_diff"] = rs_diff

    fila: Dict = {
        "ticker": ticker,
        "empresa": nombre or ticker,
        "precio": ind["close"],
        "var_dia": round(ind["var_dia"], 2),
        "rsi": round(ind["rsi"], 1),
        "mom5": round(ind["mom5"], 2),
        "mom20": round(ind["mom20"], 2),
        "macd_h": round(ind["macd_h"], 4),
        "vol_d": round(ind["vol_d"] * 100.0, 2),
        "adx": ind["adx"],
        "rvol": ind["rvol"],
        "marea": marea["estado"],
        "rs_ibex": round(rs_diff, 1),
        # ── Campos crudos de auditoría (no intervienen en la UI) ──
        "ultima_fecha": ind["ultima_fecha"],
        "comp_rsi": float(comp["rsi"]),
        "comp_macd": float(comp["macd"]),
        "comp_tendencia": float(comp["tendencia"]),
        "comp_momentum": float(comp["momentum"]),
        "vol_d_frac": float(ind["vol_d"]),
        "atr_d": float(ind["atr_d"]),
        "vol_confirm": float(vol_confirm),
        "sr_bias": float(ind["sr_bias"]),
        "bb_squeeze": bool(ind["bb_squeeze"]),
        "marea_score": float(marea.get("score", 0.0)),
        "rs_ibex_raw": float(rs_diff),
        "rs_score": float(rs_score),
        "sentimiento": float(sent_val),
        "tilt": float(tilt),
    }
    calib_ticker = calib_ticker or {}
    senales_ticker = {}
    for hz in HORIZONTES:
        calib = calib_ticker.get(hz, {})
        f_conf = float(calib.get("factor_confianza", 1.0))
        a_cons = int(calib.get("ajuste_consenso", 0))
        senales_ticker[hz] = calcular_senal(
            comp, ind["vol_d"], hz, tilt,
            adx=ind["adx"],
            vol_confirm=vol_confirm,
            sr_bias=ind["sr_bias"],
            bb_squeeze=ind["bb_squeeze"],
            atr_d=ind["atr_d"],
            marea_score=marea["score"],
            rs_score=rs_score,
            sentiment_score=sent_val,
            factor_calibracion=f_conf,
            ajuste_consenso=a_cons,
        )
    senales_ticker = aplicar_coherencia_multitemporal(senales_ticker, ind["rsi"])

    regimen_set = None
    for hz in HORIZONTES:
        sig = senales_ticker[hz]
        fila[f"senal_{hz}"] = sig["senal"]
        fila[f"score_{hz}"] = sig["score"]
        fila[f"score_bruto_{hz}"] = sig.get("score_bruto", sig["score"])
        fila[f"pct_{hz}"] = sig["pct"]
        fila[f"conf_{hz}"] = sig["confianza"]
        fila[f"banda_{hz}"] = sig["banda"]
        fila[f"conv_{hz}"] = sig["conviccion"]
        fila[f"pct_raw_{hz}"] = sig.get("pct_raw")
        fila[f"umbral_{hz}"] = sig.get("umbral_zona")
        fila[f"elder_{hz}"] = sig.get("filtro_elder") or ""
        if regimen_set is None:
            regimen_set = sig["regimen"]
    fila["score_bruto"] = senales_ticker["1D"].get("score_bruto", senales_ticker["1D"]["score"])
    fila["regimen"] = regimen_set or "mixto"
    return fila


def calcular_radar_desde_batch(df_batch: pd.DataFrame,
                               tickers: Tuple[str, ...], nombres: Tuple[str, ...],
                               tilts: Tuple[float, ...],
                               sentimientos: Optional[Dict[str, float]] = None,
                               usar_calibracion: bool = False) -> pd.DataFrame:
    """
    Versión PURA (sin caché ni descarga) de calcular_radar: recibe el batch
    OHLCV ya preparado (p. ej. truncado a la última barra completa).
    """
    if FASE_1_CONGELADA:
        usar_calibracion = False

    sub_ibex = obtener_serie_ticker(df_batch, TICKER_BENCHMARK)
    marea = calcular_marea_mercado(sub_ibex)

    # Paso 5: Feedback Loop - Calibracion historica en lote
    mapa_calibracion: Dict = {}
    if usar_calibracion and not FASE_1_CONGELADA:
        try:
            from database.predicciones import obtener_calibracion_historica
            mapa_calibracion = obtener_calibracion_historica()
        except Exception:
            mapa_calibracion = {}

    filas: List[Dict] = []
    for i, ticker in enumerate(tickers):
        sub = obtener_serie_ticker(df_batch, ticker)
        if sub is None:
            continue
        sent_val = float(sentimientos.get(ticker, 0.0)) if sentimientos else 0.0
        tilt = float(tilts[i]) if i < len(tilts) else 0.0
        calib_ticker = {hz: mapa_calibracion.get((ticker, hz), {}) for hz in HORIZONTES}
        fila = calcular_fila_ticker(
            sub, marea, tilt=tilt, sent_val=sent_val, calib_ticker=calib_ticker,
            ticker=ticker, nombre=nombres[i] if i < len(nombres) else ticker,
        )
        if fila is not None:
            filas.append(fila)

    return pd.DataFrame(filas)


@st.cache_data(ttl=300, show_spinner=False)
def calcular_radar(tickers: Tuple[str, ...], nombres: Tuple[str, ...],
                   tilts: Tuple[float, ...],
                   sentimientos: Optional[Dict[str, float]] = None,
                   usar_calibracion: bool = False) -> pd.DataFrame:
    """
    Calcula las 4 señales (1D/3D/1S/1M) para todos los tickers (vista EN VIVO).

    IMPORTANTE: esta salida puede incluir la barra intradía incompleta y NO
    debe persistirse. La persistencia la hace services/emision.py con barras
    completas.

    Parámetros (tuplas alineadas, para cacheabilidad):
      tickers      — símbolos yfinance ('BBVA.MC', ...)
      nombres      — nombre visible de cada empresa
      tilts        — tilt fundamental ∈ [-2, 2] (solo afecta al horizonte 1M)
      sentimientos — dict opcional {ticker: score [-1, 1]} de noticias recientes
      usar_calibracion - bool (fijado a False en Fase 1 v5-frozen)

    Retorna DataFrame con una fila por ticker:
      ticker, empresa, precio, var_dia, rsi, mom5, mom20, macd_h, vol_d,
      adx, rvol, marea, rs_ibex, regimen,
      y por horizonte: senal_XX, score_XX, pct_XX, conf_XX, banda_XX, conv_XX
    """
    batch_symbols = list(tickers)
    if TICKER_BENCHMARK not in batch_symbols:
        batch_symbols.append(TICKER_BENCHMARK)
    df_batch = get_price_batch(tuple(batch_symbols))
    return calcular_radar_desde_batch(df_batch, tickers, nombres, tilts, sentimientos, usar_calibracion=usar_calibracion)


def calcular_senales_ticker(sub: pd.DataFrame, tilt: float = 0.0,
                            marea: Optional[Dict] = None,
                            ticker: str = "",
                            nombre: str = "",
                            usar_calibracion: bool = False) -> Optional[Dict]:
    """
    Señales de los 4 horizontes para UNA serie OHLCV (panel de detalle).
    Retorna {indicadores, componentes, senales: {hz: {...}}} o None.
    """
    ind = calcular_indicadores(sub)
    if ind is None:
        return None

    if FASE_1_CONGELADA:
        usar_calibracion = False

    if marea is None:
        marea = obtener_marea_mercado_actual()
    ind["marea"] = marea

    var20_ibex = marea.get("var20d", 0.0)
    rs_diff = ind["mom20"] - var20_ibex
    rs_score = float(np.clip(rs_diff / 10.0, -1.0, 1.0))
    ind["rs_diff"] = rs_diff

    sent_val = 0.0
    if ticker or nombre:
        sent_val = obtener_score_sentimiento(ticker, nombre)
    ind["sentiment_score"] = sent_val

    comp = _componentes(ind)

    # Confirmación de volumen
    close_s = ind.get("_close_series")
    vol_s = ind.get("_volume_series")
    w_ref = _pesos_dinamicos("1S", ind["adx"])
    score_ref = (w_ref["rsi"] * comp["rsi"]
                 + w_ref["macd"] * comp["macd"]
                 + w_ref["tendencia"] * comp["tendencia"]
                 + w_ref["momentum"] * comp["momentum"])
    vol_confirm = _confirmar_volumen(close_s, vol_s, score_ref) if (
        close_s is not None and vol_s is not None) else 1.0

    obtener_calibracion_historica = None
    if usar_calibracion and not FASE_1_CONGELADA:
        try:
            from database.predicciones import obtener_calibracion_historica
        except Exception:
            obtener_calibracion_historica = None

    senales = {}
    calibs_hz = {}
    for hz in HORIZONTES:
        calib = obtener_calibracion_historica(ticker, hz) if (obtener_calibracion_historica and ticker) else {}
        calibs_hz[hz] = calib
        f_conf = float(calib.get("factor_confianza", 1.0))
        a_cons = int(calib.get("ajuste_consenso", 0))
        senales[hz] = calcular_senal(
            comp, ind["vol_d"], hz, tilt,
            adx=ind["adx"],
            vol_confirm=vol_confirm,
            sr_bias=ind["sr_bias"],
            bb_squeeze=ind["bb_squeeze"],
            atr_d=ind["atr_d"],
            marea_score=marea["score"],
            rs_score=rs_score,
            sentiment_score=sent_val,
            factor_calibracion=f_conf,
            ajuste_consenso=a_cons,
        )
    senales = aplicar_coherencia_multitemporal(senales, ind["rsi"])
    ind["filtro_elder"] = any(s.get("filtro_elder") for s in senales.values())
    ind["alineacion_macro"] = any(s.get("alineacion_macro") for s in senales.values())
    ind["calibraciones"] = calibs_hz
    return {"indicadores": ind, "componentes": comp, "senales": senales}


# ═══════════════════════════════════════════════════════════════════
# 🔍 EXPLICABILIDAD Y PROYECCIÓN (panel de detalle)
# ═══════════════════════════════════════════════════════════════════

def explicar_senal(ind: Dict) -> Tuple[List[str], List[str]]:
    """
    Genera las listas 'a favor' / 'en contra' a partir de los indicadores.
    v3-enhanced: incluye diagnóstico de volumen, régimen, S/R y marea IBEX.
    """
    a_favor, en_contra = [], []

    # RSI como momentum (v2+)
    rsi = ind["rsi"]
    if rsi >= 60:
        a_favor.append(f"RSI {rsi:.0f} con momentum positivo")
    elif rsi >= 50:
        a_favor.append(f"RSI {rsi:.0f} en zona constructiva")
    elif rsi >= 40:
        en_contra.append(f"RSI {rsi:.0f} pierde impulso")
    else:
        en_contra.append(f"RSI {rsi:.0f} con momentum negativo")
    if rsi < 20:
        a_favor.append(f"RSI {rsi:.0f} en sobreventa extrema (posible rebote técnico)")
    elif rsi > 80:
        en_contra.append(f"RSI {rsi:.0f} en sobrecompra extrema (posible agotamiento)")

    # MACD
    h, hp = ind["macd_h"], ind["macd_h_prev"]
    if h > 0 and h > hp:
        a_favor.append("MACD positivo y acelerando")
    elif h > 0:
        a_favor.append("MACD positivo, aunque perdiendo fuelle")
    elif h < 0 and h < hp:
        en_contra.append("MACD negativo y cayendo")
    else:
        en_contra.append("MACD negativo (aunque mejorando)")

    # Tendencia
    close, sma20, sma50 = ind["close"], ind["sma20"], ind["sma50"]
    dist20 = (close - sma20) / sma20 * 100.0 if sma20 > 0 else 0.0
    dist50 = (close - sma50) / sma50 * 100.0 if sma50 > 0 else 0.0
    if close > sma20:
        a_favor.append(f"Precio +{dist20:.1f}% sobre SMA20")
    else:
        en_contra.append(f"Precio {dist20:.1f}% bajo SMA20")
    if close > sma50:
        a_favor.append(f"Precio +{dist50:.1f}% sobre SMA50")
    else:
        en_contra.append(f"Precio {dist50:.1f}% bajo SMA50")
    if sma20 > sma50:
        a_favor.append("Estructura alcista (SMA20 > SMA50)")
    else:
        en_contra.append("Estructura bajista (SMA20 < SMA50)")

    # EMA cross bonus
    ema_cross = ind.get("ema_cross", 0.0)
    if ema_cross > 0:
        a_favor.append("Cruce alcista EMA9/EMA21 reciente")
    elif ema_cross < 0:
        en_contra.append("Cruce bajista EMA9/EMA21 reciente")

    # Momentum (con magnitud)
    mom5, mom20 = ind["mom5"], ind["mom20"]
    if mom5 > 0:
        a_favor.append(f"Momentum 5d {mom5:+.1f}%")
    else:
        en_contra.append(f"Momentum 5d {mom5:+.1f}%")
    if mom20 > 0:
        a_favor.append(f"Momentum 20d {mom20:+.1f}%")
    else:
        en_contra.append(f"Momentum 20d {mom20:+.1f}%")

    # ── v3-enhanced: Volumen ────────────────────────────────────────
    rvol = ind.get("rvol", 1.0)
    if rvol >= 1.5:
        a_favor.append(f"Volumen relativo {rvol:.1f}× (alta participación)")
    elif rvol >= 1.0:
        a_favor.append(f"Volumen relativo {rvol:.1f}× (normal)")
    elif rvol >= 0.5:
        en_contra.append(f"Volumen relativo {rvol:.1f}× (bajo)")
    else:
        en_contra.append(f"Volumen relativo {rvol:.1f}× (muy bajo, señal débil)")

    # ── v3-enhanced: Régimen ADX ────────────────────────────────────
    adx = ind.get("adx", 20.0)
    if adx >= 30:
        a_favor.append(f"ADX {adx:.0f}: tendencia fuerte (señales de momentum fiables)")
    elif adx >= ADX_TENDENCIA:
        a_favor.append(f"ADX {adx:.0f}: mercado tendencial")
    elif adx <= 15:
        en_contra.append(f"ADX {adx:.0f}: mercado muy lateral (señales poco fiables)")
    elif adx <= ADX_RANGO:
        en_contra.append(f"ADX {adx:.0f}: mercado en rango (momentum reducido)")

    # ── v3-enhanced: Bollinger Squeeze ──────────────────────────────
    if ind.get("bb_squeeze", False):
        a_favor.append(f"Bollinger Squeeze activo (volatilidad comprimida → movimiento inminente)")

    # ── v3-enhanced: Soporte/Resistencia ────────────────────────────
    sr_bias = ind.get("sr_bias", 0.0)
    if sr_bias < -0.15:
        en_contra.append("Precio cerca de resistencia significativa")
    elif sr_bias > 0.15:
        a_favor.append("Precio apoyado sobre soporte significativo")

    # ── Marea de Mercado (Benchmark IBEX 35) ────────────────────────
    marea = ind.get("marea")
    if isinstance(marea, dict):
        est_marea = marea.get("estado")
        if est_marea == MAREA_BAJISTA:
            en_contra.append("Marea bajista en IBEX 35 (mercado general en corrección, viento en contra)")
        elif est_marea == MAREA_ALCISTA:
            a_favor.append("Marea alcista en IBEX 35 (mercado general en expansión, viento de cola)")

    # ── Fuerza Relativa vs IBEX (RS 20d) ────────────────────────────
    rs_diff = ind.get("rs_diff", 0.0)
    if rs_diff >= 4.0:
        a_favor.append(f"Fuerza relativa vs IBEX +{rs_diff:.1f}% (comportamiento superior al mercado)")
    elif rs_diff <= -4.0:
        en_contra.append(f"Fuerza relativa vs IBEX {rs_diff:.1f}% (comportamiento inferior al mercado)")

    # ── Coherencia Multi-temporal (Triple Screen) ───────────────────
    if ind.get("alineacion_macro"):
        a_favor.append("Alineación multi-temporal perfecta (1D, 1S y 1M apuntan en la misma dirección)")
    if ind.get("filtro_elder"):
        en_contra.append("Señal de corto plazo frenada por contratendencia macro mensual (Triple Screen)")

    # ── Sentimiento de Noticias ──────────────────────────────────────
    score_sent = ind.get("sentiment_score", 0.0)
    if score_sent >= 0.35:
        a_favor.append(f"Sentimiento favorable en noticias (+{score_sent*100:.0f}% neto en titulares)")
    elif score_sent <= -0.35:
        en_contra.append(f"Sentimiento adverso en noticias ({score_sent*100:.0f}% neto en titulares)")

    # ── Feedback Loop: Calibración Histórica ────────────────────────
    calibs = ind.get("calibraciones", {})
    for hz, c in calibs.items():
        n = c.get("muestras", 0)
        tasa = c.get("tasa_acierto")
        if n >= 5 and tasa is not None:
            if tasa >= 75.0:
                a_favor.append(f"Alta predictibilidad histórica en {hz} ({tasa:.0f}% acierto en {n} señales)")
            elif tasa <= 40.0:
                en_contra.append(f"Baja predictibilidad histórica en {hz} ({tasa:.0f}% acierto en {n} señales; consenso reforzado)")

    return a_favor, en_contra


def proyectar_cono(precio: float, score: float, vol_d: float,
                   ultima_fecha, dias: int = 20) -> Optional[Dict]:
    """
    Cono de proyección a `dias` sesiones:
      base      — deriva según la señal 1M (score/2 · σ√d)
      optimista — base + 1σ acumulada
      pesimista — base − 1σ acumulada
    """
    try:
        start_ts = pd.Timestamp(ultima_fecha) + pd.Timedelta(days=1)
        fechas = bdate_range_bme(start_ts, periods=dias)
        t = np.arange(1, dias + 1)
        drift = (score / 2.0) * vol_d * np.sqrt(dias)
        base = precio * (1.0 + drift * t / dias)
        banda = precio * vol_d * np.sqrt(t)
        return {
            "fechas": fechas,
            "base": base,
            "optimista": base + banda,
            "pesimista": base - banda,
        }
    except Exception:
        return None


def generar_conclusion(nombre: str, ind: Dict, senales: Dict[str, Dict]) -> str:
    """Texto de conclusión generado a partir de las señales 1S y 1M."""
    s1s, s1m = senales["1S"], senales["1M"]
    iconos = {SENAL_ALCISTA: "🟢", SENAL_LATERAL: "🟡", SENAL_BAJISTA: "🔴"}

    if s1s["senal"] == s1m["senal"]:
        coherencia = "Los horizontes semanal y mensual están alineados, lo que refuerza la lectura."
    elif SENAL_LATERAL in (s1s["senal"], s1m["senal"]):
        coherencia = "Hay divergencia parcial entre horizontes: conviene vigilar la evolución antes de actuar."
    else:
        coherencia = "Corto y medio plazo apuntan en direcciones opuestas: escenario de alta incertidumbre."

    vol_anual = ind["vol_d"] * np.sqrt(252) * 100.0

    # v3-enhanced: añadir info de régimen y convicción
    adx = ind.get("adx", 0)
    regimen_txt = ""
    if adx >= ADX_TENDENCIA:
        regimen_txt = f"Régimen tendencial (ADX {adx:.0f}). "
    elif adx <= ADX_RANGO:
        regimen_txt = f"Régimen lateral (ADX {adx:.0f}). "

    conviccion_1m = s1m.get("conviccion", "—")
    conv_txt = f"Convicción: {conviccion_1m}. " if conviccion_1m != "—" else ""

    return (
        f"{iconos[s1m['senal']]} **{nombre}** presenta señal **{s1m['senal']}** "
        f"a 1 mes ({s1m['pct']:+.1f}% esperado, confianza {s1m['confianza']:.0f}%) "
        f"y **{s1s['senal']}** a 1 semana ({s1s['pct']:+.1f}%). {coherencia} "
        f"{regimen_txt}{conv_txt}"
        f"Volatilidad anualizada ≈ {vol_anual:.0f}%. "
        f"Recuerda: es una estimación probabilística, no una garantía."
    )
