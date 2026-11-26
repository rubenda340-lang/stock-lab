"""
Tracking de Predicciones — StockLab IA
======================================
Registro y resolución automática de las señales generadas por el motor
de IA (services/signal_engine.py).

Diseño no-intrusivo:
  - Crea su propia tabla `predicciones` de forma perezosa (lazy), sin
    modificar el esquema gestionado por database/connection.py.
  - Una fila por (ticker, horizonte, fecha_prediccion): UNIQUE con
    ON CONFLICT IGNORE → nunca duplica señales del mismo día.
  - Las columnas de resultado se rellenan al resolver (precio_real,
    variacion_real, acierto, error_pct).

Flujo diario (invocado desde views/ia.py):
  1. init_predicciones_table()
  2. resolver_predicciones(precios)   — cierra las vencidas contra precio real
  3. registrar_predicciones(radar_df) — inserta las señales de hoy
"""

import logging
import os
import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List, Any

import numpy as np
import pandas as pd

from database.connection import get_connection
from services.signal_engine import MOTOR_VERSION, get_params_hash, calcular_params_hash_motor, FASE_1_CONGELADA
from utils.market_calendar import (
    fecha_habil_bme,
    festivos_bme_rango,
    sesion_cerrada_hoy,
    es_fecha_resoluble,
    HORA_CORTE_BME,
    MINUTO_CORTE_BME,
    barra_hoy_completa,
    get_ahora_madrid,
    limite_emision,
    en_ventana_emision,
)

logger = logging.getLogger(__name__)

MODELO_VERSION = MOTOR_VERSION
REGLA_EVAL_VERSION = "v1-dynamic-zone"

SESIONES_SIN_COTIZACION = 5
RESUELTA_SIN_COTIZACION = 2
MOTIVO_SIN_COTIZACION = "sin_cotizacion"

# Flag para el Feedback Loop: DESACTIVADO según Fase 1 para evitar amplificación
# de ruido con n < 30 y sesgo de baja volatilidad hasta rediseño bayesiano formal.
FEEDBACK_ACTIVO = False

# Umbrales de zona lateral por horizonte (±%) — DEBE coincidir con signal_engine.UMBRALES_LATERAL
# Por encima del umbral → zona ALCISTA, por debajo → zona BAJISTA, dentro → zona LATERAL
UMBRALES_LATERAL = {"1D": 0.8, "3D": 1.2, "1S": 1.8, "1M": 3.0}

# Días de negociación que representa cada horizonte (debe coincidir con
# services/signal_engine.HORIZONTES; se importa perezosamente para evitar
# dependencias circulares).
_HORIZONTES_DEFAULT = {"1D": 1, "3D": 3, "1S": 5, "1M": 20}

# Factor de invalidación anticipada por horizonte (mantenido solo para monitorización,
# NO resuelve anticipadamente como fallo en Fase 1).
_INVALIDACION_FACTOR = {
    "1D": None,
    "3D": 2.0,
    "1S": 1.5,
    "1M": 1.2,
}

_DDL = """
CREATE TABLE IF NOT EXISTS predicciones (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker             TEXT    NOT NULL,
    empresa            TEXT,
    fecha_prediccion   TEXT    NOT NULL,
    horizonte          TEXT    NOT NULL,
    dias               INTEGER NOT NULL,
    fecha_objetivo     TEXT    NOT NULL,
    senal              TEXT    NOT NULL,
    pct_estimado       REAL,
    banda_pct          REAL,
    confianza          REAL,
    precio_entrada     REAL    NOT NULL,
    score_motor        REAL,
    modelo             TEXT    NOT NULL DEFAULT 'v5-frozen',
    resuelta           INTEGER NOT NULL DEFAULT 0,
    fecha_resolucion   TEXT,
    precio_real        REAL,
    variacion_real     REAL,
    acierto            INTEGER,
    error_pct          REAL,
    motivo_resolucion  TEXT,
    score_calidad      REAL,
    -- Columnas crudas y de auditoría institucional (Fase 1) --
    pct_estimado_raw   REAL,
    umbral_zona        REAL,
    sigma              REAL,
    comp_rsi           REAL,
    comp_macd          REAL,
    comp_tendencia     REAL,
    comp_momentum      REAL,
    adx                REAL,
    rvol               REAL,
    marea              TEXT,
    rs_ibex            REAL,
    sentimiento        REAL,
    tilt               REAL,
    hora_calculo       TEXT,
    regla_eval         TEXT    DEFAULT 'v1-dynamic-zone',
    archivada          INTEGER NOT NULL DEFAULT 0,
    -- Columnas de esquema v2 --
    emitida_en         TEXT,
    params_hash        TEXT,
    score_bruto        REAL,
    features_json      TEXT,
    UNIQUE(ticker, horizonte, fecha_prediccion, modelo) ON CONFLICT IGNORE
);

CREATE TABLE IF NOT EXISTS ranking_historico (
    fecha              TEXT    NOT NULL,
    ticker             TEXT    NOT NULL,
    posicion           INTEGER NOT NULL,
    score              REAL    NOT NULL,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (fecha, ticker)
);

CREATE INDEX IF NOT EXISTS idx_pred_fecha_modelo ON predicciones(fecha_prediccion, modelo);
CREATE INDEX IF NOT EXISTS idx_pred_resuelta_fecha_obj ON predicciones(resuelta, fecha_objetivo);
CREATE INDEX IF NOT EXISTS idx_pred_ticker_hz_fecha ON predicciones(ticker, horizonte, fecha_prediccion);

CREATE TRIGGER IF NOT EXISTS trg_predicciones_no_delete
BEFORE DELETE ON predicciones
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'Inmutabilidad: prohibido borrar filas de predicciones');
END;

CREATE TRIGGER IF NOT EXISTS trg_predicciones_inmutables_update
BEFORE UPDATE ON predicciones
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN OLD.id IS NOT NEW.id
          OR OLD.ticker IS NOT NEW.ticker
          OR OLD.empresa IS NOT NEW.empresa
          OR OLD.fecha_prediccion IS NOT NEW.fecha_prediccion
          OR OLD.horizonte IS NOT NEW.horizonte
          OR OLD.dias IS NOT NEW.dias
          OR OLD.fecha_objetivo IS NOT NEW.fecha_objetivo
          OR OLD.senal IS NOT NEW.senal
          OR OLD.pct_estimado IS NOT NEW.pct_estimado
          OR OLD.banda_pct IS NOT NEW.banda_pct
          OR OLD.confianza IS NOT NEW.confianza
          OR OLD.precio_entrada IS NOT NEW.precio_entrada
          OR OLD.score_motor IS NOT NEW.score_motor
          OR OLD.modelo IS NOT NEW.modelo
          OR OLD.pct_estimado_raw IS NOT NEW.pct_estimado_raw
          OR OLD.umbral_zona IS NOT NEW.umbral_zona
          OR OLD.sigma IS NOT NEW.sigma
          OR OLD.comp_rsi IS NOT NEW.comp_rsi
          OR OLD.comp_macd IS NOT NEW.comp_macd
          OR OLD.comp_tendencia IS NOT NEW.comp_tendencia
          OR OLD.comp_momentum IS NOT NEW.comp_momentum
          OR OLD.adx IS NOT NEW.adx
          OR OLD.rvol IS NOT NEW.rvol
          OR OLD.marea IS NOT NEW.marea
          OR OLD.rs_ibex IS NOT NEW.rs_ibex
          OR OLD.sentimiento IS NOT NEW.sentimiento
          OR OLD.tilt IS NOT NEW.tilt
          OR OLD.hora_calculo IS NOT NEW.hora_calculo
          OR OLD.regla_eval IS NOT NEW.regla_eval
          OR OLD.emitida_en IS NOT NEW.emitida_en
          OR OLD.params_hash IS NOT NEW.params_hash
          OR OLD.score_bruto IS NOT NEW.score_bruto
          OR OLD.features_json IS NOT NEW.features_json
        THEN RAISE(ABORT, 'Inmutabilidad: prohibido modificar columnas de emision')
    END;
    SELECT CASE
        WHEN (
            OLD.resuelta IS NOT NEW.resuelta
            OR OLD.fecha_resolucion IS NOT NEW.fecha_resolucion
            OR OLD.precio_real IS NOT NEW.precio_real
            OR OLD.variacion_real IS NOT NEW.variacion_real
            OR OLD.acierto IS NOT NEW.acierto
            OR OLD.error_pct IS NOT NEW.error_pct
            OR OLD.motivo_resolucion IS NOT NEW.motivo_resolucion
            OR OLD.score_calidad IS NOT NEW.score_calidad
        )
        AND NOT COALESCE(OLD.resuelta = 0 AND NEW.resuelta IN (1, 2), 0)
        THEN RAISE(ABORT, 'Inmutabilidad: transicion de resolucion no permitida')
    END;
END;
"""

_DDL_RANKING_HISTORICO = """
CREATE TABLE IF NOT EXISTS ranking_historico (
    fecha              TEXT    NOT NULL,
    ticker             TEXT    NOT NULL,
    posicion           INTEGER NOT NULL,
    score              REAL    NOT NULL,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (fecha, ticker)
);
"""

_DDL_PREDICCIONES_V2_FMT = """
CREATE TABLE {table_name} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker             TEXT    NOT NULL,
    empresa            TEXT,
    fecha_prediccion   TEXT    NOT NULL,
    horizonte          TEXT    NOT NULL,
    dias               INTEGER NOT NULL,
    fecha_objetivo     TEXT    NOT NULL,
    senal              TEXT    NOT NULL,
    pct_estimado       REAL,
    banda_pct          REAL,
    confianza          REAL,
    precio_entrada     REAL    NOT NULL,
    score_motor        REAL,
    modelo             TEXT    NOT NULL DEFAULT 'v5-frozen',
    resuelta           INTEGER NOT NULL DEFAULT 0,
    fecha_resolucion   TEXT,
    precio_real        REAL,
    variacion_real     REAL,
    acierto            INTEGER,
    error_pct          REAL,
    motivo_resolucion  TEXT,
    score_calidad      REAL,
    pct_estimado_raw   REAL,
    umbral_zona        REAL,
    sigma              REAL,
    comp_rsi           REAL,
    comp_macd          REAL,
    comp_tendencia     REAL,
    comp_momentum      REAL,
    adx                REAL,
    rvol               REAL,
    marea              TEXT,
    rs_ibex            REAL,
    sentimiento        REAL,
    tilt               REAL,
    hora_calculo       TEXT,
    regla_eval         TEXT    DEFAULT 'v1-dynamic-zone',
    archivada          INTEGER NOT NULL DEFAULT 0,
    emitida_en         TEXT,
    params_hash        TEXT,
    score_bruto        REAL,
    features_json      TEXT,
    UNIQUE(ticker, horizonte, fecha_prediccion, modelo) ON CONFLICT IGNORE
)
"""

_COLUMNAS_V1_38 = (
    "id", "ticker", "empresa", "fecha_prediccion", "horizonte", "dias",
    "fecha_objetivo", "senal", "pct_estimado", "banda_pct", "confianza",
    "precio_entrada", "score_motor", "modelo", "resuelta", "fecha_resolucion",
    "precio_real", "variacion_real", "acierto", "error_pct",
    "motivo_resolucion", "score_calidad", "pct_estimado_raw", "umbral_zona",
    "sigma", "comp_rsi", "comp_macd", "comp_tendencia", "comp_momentum",
    "adx", "rvol", "marea", "rs_ibex", "sentimiento", "tilt", "hora_calculo",
    "regla_eval", "archivada",
)

_COLUMNAS_V2_NUEVAS = ("emitida_en", "params_hash", "score_bruto", "features_json")

_COLUMNAS_INMUTABLES_EMISION_33 = (
    "id", "ticker", "empresa", "fecha_prediccion", "horizonte", "dias",
    "fecha_objetivo", "senal", "pct_estimado", "banda_pct", "confianza",
    "precio_entrada", "score_motor", "modelo", "pct_estimado_raw", "umbral_zona",
    "sigma", "comp_rsi", "comp_macd", "comp_tendencia", "comp_momentum",
    "adx", "rvol", "marea", "rs_ibex", "sentimiento", "tilt", "hora_calculo",
    "regla_eval", "emitida_en", "params_hash", "score_bruto", "features_json",
)

_COLUMNAS_RESOLUCION_8 = (
    "resuelta", "fecha_resolucion", "precio_real", "variacion_real",
    "acierto", "error_pct", "motivo_resolucion", "score_calidad",
)

_INDICES_V2_SQL = (
    ("idx_pred_fecha_modelo", "CREATE INDEX IF NOT EXISTS idx_pred_fecha_modelo ON predicciones(fecha_prediccion, modelo)"),
    ("idx_pred_resuelta_fecha_obj", "CREATE INDEX IF NOT EXISTS idx_pred_resuelta_fecha_obj ON predicciones(resuelta, fecha_objetivo)"),
    ("idx_pred_ticker_hz_fecha", "CREATE INDEX IF NOT EXISTS idx_pred_ticker_hz_fecha ON predicciones(ticker, horizonte, fecha_prediccion)"),
)

_DDL_TRIGGER_NO_DELETE = """
CREATE TRIGGER IF NOT EXISTS trg_predicciones_no_delete
BEFORE DELETE ON predicciones
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'Inmutabilidad: prohibido borrar filas de predicciones');
END;
"""

_DDL_TRIGGER_INMUTABLES_UPDATE = """
CREATE TRIGGER IF NOT EXISTS trg_predicciones_inmutables_update
BEFORE UPDATE ON predicciones
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN OLD.id IS NOT NEW.id
          OR OLD.ticker IS NOT NEW.ticker
          OR OLD.empresa IS NOT NEW.empresa
          OR OLD.fecha_prediccion IS NOT NEW.fecha_prediccion
          OR OLD.horizonte IS NOT NEW.horizonte
          OR OLD.dias IS NOT NEW.dias
          OR OLD.fecha_objetivo IS NOT NEW.fecha_objetivo
          OR OLD.senal IS NOT NEW.senal
          OR OLD.pct_estimado IS NOT NEW.pct_estimado
          OR OLD.banda_pct IS NOT NEW.banda_pct
          OR OLD.confianza IS NOT NEW.confianza
          OR OLD.precio_entrada IS NOT NEW.precio_entrada
          OR OLD.score_motor IS NOT NEW.score_motor
          OR OLD.modelo IS NOT NEW.modelo
          OR OLD.pct_estimado_raw IS NOT NEW.pct_estimado_raw
          OR OLD.umbral_zona IS NOT NEW.umbral_zona
          OR OLD.sigma IS NOT NEW.sigma
          OR OLD.comp_rsi IS NOT NEW.comp_rsi
          OR OLD.comp_macd IS NOT NEW.comp_macd
          OR OLD.comp_tendencia IS NOT NEW.comp_tendencia
          OR OLD.comp_momentum IS NOT NEW.comp_momentum
          OR OLD.adx IS NOT NEW.adx
          OR OLD.rvol IS NOT NEW.rvol
          OR OLD.marea IS NOT NEW.marea
          OR OLD.rs_ibex IS NOT NEW.rs_ibex
          OR OLD.sentimiento IS NOT NEW.sentimiento
          OR OLD.tilt IS NOT NEW.tilt
          OR OLD.hora_calculo IS NOT NEW.hora_calculo
          OR OLD.regla_eval IS NOT NEW.regla_eval
          OR OLD.emitida_en IS NOT NEW.emitida_en
          OR OLD.params_hash IS NOT NEW.params_hash
          OR OLD.score_bruto IS NOT NEW.score_bruto
          OR OLD.features_json IS NOT NEW.features_json
        THEN RAISE(ABORT, 'Inmutabilidad: prohibido modificar columnas de emision')
    END;
    SELECT CASE
        WHEN (
            OLD.resuelta IS NOT NEW.resuelta
            OR OLD.fecha_resolucion IS NOT NEW.fecha_resolucion
            OR OLD.precio_real IS NOT NEW.precio_real
            OR OLD.variacion_real IS NOT NEW.variacion_real
            OR OLD.acierto IS NOT NEW.acierto
            OR OLD.error_pct IS NOT NEW.error_pct
            OR OLD.motivo_resolucion IS NOT NEW.motivo_resolucion
            OR OLD.score_calidad IS NOT NEW.score_calidad
        )
        AND NOT COALESCE(OLD.resuelta = 0 AND NEW.resuelta IN (1, 2), 0)
        THEN RAISE(ABORT, 'Inmutabilidad: transicion de resolucion no permitida')
    END;
END;
"""

_TRIGGERS_V2_SQL = (
    ("trg_predicciones_no_delete", _DDL_TRIGGER_NO_DELETE),
    ("trg_predicciones_inmutables_update", _DDL_TRIGGER_INMUTABLES_UPDATE),
)

# Fecha de corte a partir de la cual se activaron las guardas estrictas (ventana e integridad).
# En la migracion a esquema v2 (C-06b / D-1), las filas con fecha_prediccion < '2026-10-12'
# reciben modelo = 'legacy-pre-freeze' preservando las demas columnas inmutables.
FECHA_CORTE_GUARDAS = "2026-10-12"


def _calcular_sha256_inmutables(conn: sqlite3.Connection, tabla: str = "predicciones") -> str:
    """Calcula SHA256_INMUTABLES_11 con la formula oficial sobre la tabla indicada."""
    rows = conn.execute(
        f"SELECT id, ticker, fecha_prediccion, horizonte, senal, pct_estimado, "
        f"precio_entrada, modelo, score_motor, tilt, hora_calculo "
        f"FROM {tabla} ORDER BY id ASC"
    ).fetchall()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _calcular_sha256_inmutables_10_sin_modelo(conn: sqlite3.Connection, tabla: str = "predicciones") -> str:
    """Calcula SHA256_INMUTABLES_10_SIN_MODELO con la formula oficial sobre la tabla indicada."""
    rows = conn.execute(
        f"SELECT id, ticker, fecha_prediccion, horizonte, senal, pct_estimado, "
        f"precio_entrada, score_motor, tilt, hora_calculo "
        f"FROM {tabla} ORDER BY id ASC"
    ).fetchall()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _calcular_sha256_inmutables_11_desde_corte(conn: sqlite3.Connection, tabla: str = "predicciones") -> str:
    """Calcula el hash de las 11 columnas inmutables para las filas con fecha_prediccion >= '2026-10-12'."""
    rows = conn.execute(
        f"SELECT id, ticker, fecha_prediccion, horizonte, senal, pct_estimado, "
        f"precio_entrada, modelo, score_motor, tilt, hora_calculo "
        f"FROM {tabla} WHERE fecha_prediccion >= '2026-10-12' ORDER BY id ASC"
    ).fetchall()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _calcular_sha256_resueltas(conn: sqlite3.Connection, tabla: str = "predicciones") -> str:
    """Calcula SHA256_RESUELTAS con la formula oficial sobre la tabla indicada."""
    rows = conn.execute(
        f"SELECT id, resuelta, precio_real, variacion_real, acierto "
        f"FROM {tabla} WHERE resuelta != 0 ORDER BY id ASC"
    ).fetchall()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _es_tabla_v2(conn: sqlite3.Connection) -> bool:
    """
    Comprueba en solo lectura si la tabla predicciones tiene la estructura e indices de v2:
      1. Tiene las 4 columnas nuevas (emitida_en, params_hash, score_bruto, features_json).
      2. Tiene UNIQUE(ticker, horizonte, fecha_prediccion, modelo) en sqlite_master.
      3. Tiene los 3 indices de esquema v2 en sqlite_master.
    """
    row_tbl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'predicciones'"
    ).fetchone()
    if not row_tbl or not row_tbl[0]:
        return False

    sql_norm = "".join(str(row_tbl[0]).lower().split())
    if "unique(ticker,horizonte,fecha_prediccion,modelo)" not in sql_norm:
        return False

    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(predicciones)").fetchall()
    }
    if not all(col in existing_cols for col in _COLUMNAS_V2_NUEVAS):
        return False

    existing_idx = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'predicciones'"
        ).fetchall()
    }
    if not all(idx_name in existing_idx for idx_name, _ in _INDICES_V2_SQL):
        return False

    return True


def _tiene_triggers_inmutabilidad(conn: sqlite3.Connection) -> bool:
    """Comprueba en solo lectura si existen los 2 triggers de inmutabilidad en sqlite_master."""
    existing_trigs = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'predicciones'"
        ).fetchall()
    }
    return all(trig_name in existing_trigs for trig_name, _ in _TRIGGERS_V2_SQL)


def _es_esquema_v2(conn: sqlite3.Connection) -> bool:
    """
    Comprueba en solo lectura si la tabla predicciones ya tiene el esquema v2:
      1. Tiene las 4 columnas nuevas (emitida_en, params_hash, score_bruto, features_json).
      2. Tiene UNIQUE(ticker, horizonte, fecha_prediccion, modelo) en sqlite_master.
      3. Tiene los 3 indices de esquema v2 en sqlite_master.
      4. Tiene los 2 triggers de inmutabilidad en sqlite_master.
    """
    if not _es_tabla_v2(conn):
        return False
    return _tiene_triggers_inmutabilidad(conn)


REGLA_C10_PRECIO_CIERRE = "c10_precio_cierre"
REGLA_C10_RETORNO_TOTAL = "c10_retorno_total"
REGLA_C11_APERTURA_PRECIO = "c11_apertura_precio"
REGLA_C11_APERTURA_RETORNO_TOTAL = "c11_apertura_retorno_total"

import math

K_UMBRAL_SIGMA_V2 = 0.50
SIGMA_MIN_DIARIA_PCT = 0.20
SIGMA_MAX_DIARIA_PCT = 8.00
SIGMA_VENTANA_SESIONES_C12 = 20
SIGMA_RET_SALTO_MAX_FRAC = 0.10
SIGMA_ANCLA_DIARIA_PCT_C12 = 1.65
SIGMA_PESO_EMPIRICO_C12 = 0.50
REGLA_C12_VOL_PRECIO_CIERRE = "c12_vol_precio_cierre"
REGLA_C12_VOL_RETORNO_TOTAL = "c12_vol_retorno_total"
REGLA_C12_VOL_APERTURA_PRECIO = "c12_vol_apertura_precio"


def _normalizar_sigma_diaria_pct(sigma: Optional[float]) -> Optional[float]:
    """
    Normaliza sigma a porcentaje diario acotado en [0.20%, 8.00%] (p. ej. 1.5 para 1.5%).
    En predicciones, sigma se guarda SIEMPRE como fraccion en tanto por uno
    (p. ej. 0.018 es 1.80%); valores absurdos en fraccion (> 0.15, como 45.65 de un
    ticker con split/dato corrupto) no son porcentajes sino outliers de datos que se
    acotan al tope de 8.0% diario (y suelo de 0.20% diario).
    Por compatibilidad con invocaciones directas en porcentaje moderado (0.20 <= s <= 8.0),
    un valor en [0.20, 8.0] que supere el rango de fraccion normal (> 0.30) se interpreta
    en porcentaje y se acota igualmente en [0.20%, 8.00%].
    Devuelve None si sigma es None, NaN, no finito o <= 0.
    """
    if sigma is None:
        return None
    try:
        s_val = float(sigma)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(s_val) or s_val <= 0.0:
        return None
    if s_val <= 0.30:
        s_pct = s_val * 100.0
    elif s_val <= SIGMA_MAX_DIARIA_PCT:
        s_pct = s_val
    else:
        s_pct = SIGMA_MAX_DIARIA_PCT
    return float(min(max(s_pct, SIGMA_MIN_DIARIA_PCT), SIGMA_MAX_DIARIA_PCT))


def _calcular_sigma_cierre_hasta_emision_pct(
    sub: Optional[pd.DataFrame],
    fecha_prediccion_iso: str,
    sigma_fallback: Optional[float] = None,
    ventana: int = SIGMA_VENTANA_SESIONES_C12,
) -> Optional[float]:
    """
    Calcula la volatilidad diaria (%) de retornos de cierre hasta fecha_prediccion
    inclusive (fecha <= fecha_prediccion, cero look-ahead) sobre las ultimas `ventana`
    sesiones (estandar de 20 sesiones de signal_engine), acotando saltos extremos de
    datos y regularizando/acotando outliers dentro de [0.20%, 8.00%].
    Si la serie previa tiene menos de 5 retornos validos, aplica fallback a
    _normalizar_sigma_diaria_pct(sigma_fallback).
    """
    if sub is not None and not sub.empty and "Close" in sub.columns and fecha_prediccion_iso:
        serie_c = pd.to_numeric(sub["Close"], errors="coerce").dropna()
        serie_c = serie_c[serie_c > 0.0]
        if len(serie_c) >= 6:
            dt_idx = pd.to_datetime(serie_c.index)
            if getattr(dt_idx, "tz", None) is not None:
                dt_idx = dt_idx.tz_convert("Europe/Madrid")
            idx_str = dt_idx.strftime("%Y-%m-%d")
            close_hasta = serie_c[idx_str <= str(fecha_prediccion_iso)[:10]]
            if len(close_hasta) >= 6:
                ret = close_hasta.pct_change().dropna().tail(max(int(ventana), 5))
                if len(ret) >= 5:
                    ret_clean = ret.clip(
                        -SIGMA_RET_SALTO_MAX_FRAC, SIGMA_RET_SALTO_MAX_FRAC
                    )
                    std_val = float(ret_clean.std(ddof=1))
                    if math.isfinite(std_val) and std_val > 0.0:
                        raw_pct = float(
                            min(
                                max(std_val * 100.0, SIGMA_MIN_DIARIA_PCT),
                                SIGMA_MAX_DIARIA_PCT,
                            )
                        )
                        eff_pct = (
                            SIGMA_PESO_EMPIRICO_C12 * raw_pct
                            + (1.0 - SIGMA_PESO_EMPIRICO_C12) * SIGMA_ANCLA_DIARIA_PCT_C12
                        )
                        return float(
                            min(
                                max(eff_pct, SIGMA_MIN_DIARIA_PCT),
                                SIGMA_MAX_DIARIA_PCT,
                            )
                        )
    return _normalizar_sigma_diaria_pct(sigma_fallback)


def calcular_umbral_volatilidad_v2(
    sigma: Optional[float],
    dias: int,
    horizonte: str = "1D",
    k: float = K_UMBRAL_SIGMA_V2,
    sigma_ya_en_pct: bool = False,
) -> float:
    """
    Calcula el umbral de zona muerta lateral escalado por volatilidad (Regla v2, C-12):
      umbral = round(float(k) * sigma_pct * sqrt(max(int(dias), 1)), 4), con suelo minimo 0.10.
    Si sigma es None, NaN o <= 0, devuelve como fallback el umbral fijo del horizonte.
    """
    hz_norm = (horizonte or "1D").upper().strip()
    if sigma_ya_en_pct:
        if sigma is None:
            sigma_pct = None
        else:
            try:
                s_f = float(sigma)
                sigma_pct = (
                    float(min(max(s_f, SIGMA_MIN_DIARIA_PCT), SIGMA_MAX_DIARIA_PCT))
                    if (math.isfinite(s_f) and s_f > 0.0)
                    else None
                )
            except (TypeError, ValueError):
                sigma_pct = None
    else:
        sigma_pct = _normalizar_sigma_diaria_pct(sigma)
    if sigma_pct is None:
        return float(UMBRALES_LATERAL.get(hz_norm, 0.8))
    try:
        dias_int = max(int(dias), 1)
    except (TypeError, ValueError):
        dias_int = max(int(_HORIZONTES_DEFAULT.get(hz_norm, 1)), 1)
    umbral = round(float(k) * sigma_pct * math.sqrt(dias_int), 4)
    return max(umbral, 0.10)

_DDL_EVALUACIONES_TABLE = """
CREATE TABLE IF NOT EXISTS evaluaciones (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    prediccion_id            INTEGER NOT NULL REFERENCES predicciones(id),
    regla                    TEXT    NOT NULL,
    precio_entrada_eval      REAL    NOT NULL,
    precio_salida_eval       REAL    NOT NULL,
    variacion_pct            REAL    NOT NULL,
    umbral_aplicado          REAL    NOT NULL,
    clase_real               TEXT    NOT NULL,
    acierto                  INTEGER NOT NULL,
    acierto_signo            INTEGER,
    error_pct                REAL,
    diferencia_dividendo_pct REAL,
    evaluada_en              TEXT    NOT NULL,
    UNIQUE(prediccion_id, regla) ON CONFLICT IGNORE
);
"""

_INDICES_EVALUACIONES_SQL = (
    ("idx_eval_prediccion_id", "CREATE INDEX IF NOT EXISTS idx_eval_prediccion_id ON evaluaciones(prediccion_id)"),
    ("idx_eval_regla_acierto", "CREATE INDEX IF NOT EXISTS idx_eval_regla_acierto ON evaluaciones(regla, acierto)"),
)

_DDL_EVALUACIONES = (
    _DDL_EVALUACIONES_TABLE
    + "\n"
    + "\n".join(f"{sql};" for _, sql in _INDICES_EVALUACIONES_SQL)
    + "\n"
)


def _existe_esquema_evaluaciones(conn: sqlite3.Connection) -> bool:
    """
    Comprueba en solo lectura si la tabla evaluaciones y sus 2 indices
    (idx_eval_prediccion_id, idx_eval_regla_acierto) ya existen en sqlite_master.
    """
    row_tbl = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
    ).fetchone()
    if not row_tbl:
        return False

    existing_idx = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'evaluaciones'"
        ).fetchall()
    }
    return all(idx_name in existing_idx for idx_name, _ in _INDICES_EVALUACIONES_SQL)


def init_evaluaciones_table(
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
) -> bool:
    """
    Crea de forma idempotente la tabla evaluaciones y sus 2 indices sin modificar predicciones.
    Comprueba primero en solo lectura si evaluaciones y sus 2 indices ya existen en sqlite_master;
    si ya existen, retorna False sin hacer escrituras (total_changes == 0).
    Retorna True si creo la tabla o alguno de sus indices.
    """
    own_conn = False
    if conn is None:
        own_conn = True
        if db_path is not None:
            conn = sqlite3.connect(db_path, timeout=30)
        else:
            conn = get_connection()
    try:
        if _existe_esquema_evaluaciones(conn):
            return False

        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
        ).fetchone()
        if not row_tbl:
            conn.execute(_DDL_EVALUACIONES_TABLE)

        existing_idx = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'evaluaciones'"
            ).fetchall()
        }
        for idx_name, idx_sql in _INDICES_EVALUACIONES_SQL:
            if idx_name not in existing_idx:
                conn.execute(idx_sql)

        conn.commit()
        return True
    finally:
        if own_conn:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
# 🗄️ ESQUEMA Y MIGRACIONES
# ═══════════════════════════════════════════════════════════════════

def asegurar_triggers_inmutabilidad(
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
) -> Dict:
    """
    Instala de forma idempotente los 2 triggers SQLite de inmutabilidad en predicciones:
      - trg_predicciones_no_delete
      - trg_predicciones_inmutables_update

    Si ambos triggers ya existen en sqlite_master, retorna en solo lectura sin ejecutar
    DDL ni commit() (produciendo total_changes == 0).
    """
    own_conn = False
    if conn is None:
        own_conn = True
        if db_path is not None:
            conn = sqlite3.connect(db_path, timeout=30)
        else:
            conn = get_connection()

    try:
        existing_trigs = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'predicciones'"
            ).fetchall()
        }
        esperados = [name for name, _ in _TRIGGERS_V2_SQL]
        faltantes = [(name, sql) for name, sql in _TRIGGERS_V2_SQL if name not in existing_trigs]

        if not faltantes:
            return {
                "creados": False,
                "instalados": [],
                "ya_existian": esperados,
                "triggers_activos": sorted(existing_trigs),
            }

        ya_existian = [name for name in esperados if name in existing_trigs]
        instalados = []
        for name, trig_sql in faltantes:
            conn.execute(trig_sql)
            instalados.append(name)
        conn.commit()

        activos_despues = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'predicciones' ORDER BY name"
            ).fetchall()
        ]
        return {
            "creados": True,
            "instalados": instalados,
            "ya_existian": ya_existian,
            "triggers_activos": activos_despues,
        }
    finally:
        if own_conn:
            conn.close()


def migrar_predicciones_v2(
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
) -> Dict:
    """
    Migra la tabla predicciones de esquema v1 a esquema v2 de forma atomica e idempotente.

    - Si la tabla ya esta en esquema v2, retorna sin escrituras:
      {"migrada": False, "filas_antes": N, "filas_despues": N}
    - Si esta en v1, ejecuta dentro de una unica transaccion atomica (BEGIN IMMEDIATE ... COMMIT):
      (i)   Calcula filas_antes y los hashes de integridad antes de migrar.
      (ii)  Crea predicciones_v2 con el DDL v2 completo.
      (iii) Copia todas las filas preservando los id exactos y todas las demas columnas,
            asignando en modelo CASE WHEN fecha_prediccion < '2026-10-12' THEN 'legacy-pre-freeze' ELSE modelo END,
            y dejando emitida_en, params_hash, score_bruto y features_json en NULL.
      (iv)  Verifica dentro de la transaccion que:
            1) filas_antes == filas_despues,
            2) SHA256_INMUTABLES_10_SIN_MODELO antes y despues son identicos,
            3) SHA256_INMUTABLES_11 de las filas con fecha_prediccion >= '2026-10-12' es identico antes y despues,
            4) SHA256_RESUELTAS es identico antes y despues.
            Si cualquiera falla, lanza RuntimeError y ejecuta ROLLBACK.
      (v)   Elimina predicciones, renombra predicciones_v2 a predicciones y crea los 3 indices v2.
    """
    own_conn = False
    if conn is None:
        own_conn = True
        if db_path is not None:
            conn = sqlite3.connect(db_path, timeout=30)
        else:
            conn = get_connection()

    orig_row_factory = conn.row_factory
    orig_isolation = conn.isolation_level
    try:
        conn.row_factory = None

        if _es_esquema_v2(conn):
            n_actual = int(conn.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0])
            return {
                "migrada": False,
                "filas_antes": n_actual,
                "filas_despues": n_actual,
            }

        if _es_tabla_v2(conn):
            asegurar_triggers_inmutabilidad(conn=conn)
            n_actual = int(conn.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0])
            return {
                "migrada": False,
                "filas_antes": n_actual,
                "filas_despues": n_actual,
            }

        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'predicciones'"
        ).fetchone()
        if not row_tbl:
            conn.executescript(_DDL)
            conn.commit()
            h_inm_empty = _calcular_sha256_inmutables(conn, "predicciones")
            h_inm10_empty = _calcular_sha256_inmutables_10_sin_modelo(conn, "predicciones")
            h_inm11_ge_empty = _calcular_sha256_inmutables_11_desde_corte(conn, "predicciones")
            h_res_empty = _calcular_sha256_resueltas(conn, "predicciones")
            return {
                "migrada": True,
                "filas_antes": 0,
                "filas_despues": 0,
                "filas_legacy_marcadas": 0,
                "hash_inmutables_antes": h_inm_empty,
                "hash_inmutables_despues": h_inm_empty,
                "hash_inmutables_10_sin_modelo_antes": h_inm10_empty,
                "hash_inmutables_10_sin_modelo_despues": h_inm10_empty,
                "hash_inmutables_11_desde_12oct_antes": h_inm11_ge_empty,
                "hash_inmutables_11_desde_12oct_despues": h_inm11_ge_empty,
                "hash_resueltas_antes": h_res_empty,
                "hash_resueltas_despues": h_res_empty,
            }

        if conn.in_transaction:
            conn.commit()

        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Asegurar que las 38 columnas de v1 existen si se parte de un esquema v1 temprano
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(predicciones)").fetchall()
            }
            columnas_v1_extra = {
                "motivo_resolucion": "TEXT",
                "score_calidad": "REAL",
                "pct_estimado_raw": "REAL",
                "umbral_zona": "REAL",
                "sigma": "REAL",
                "comp_rsi": "REAL",
                "comp_macd": "REAL",
                "comp_tendencia": "REAL",
                "comp_momentum": "REAL",
                "adx": "REAL",
                "rvol": "REAL",
                "marea": "TEXT",
                "rs_ibex": "REAL",
                "sentimiento": "REAL",
                "tilt": "REAL",
                "hora_calculo": "TEXT",
                "regla_eval": "TEXT DEFAULT 'v1-dynamic-zone'",
                "archivada": "INTEGER NOT NULL DEFAULT 0",
            }
            for c_name, c_type in columnas_v1_extra.items():
                if c_name not in existing_cols:
                    conn.execute(f"ALTER TABLE predicciones ADD COLUMN {c_name} {c_type}")

            # (i) Calcular filas_antes y hashes de integridad antes de migrar
            filas_antes = int(conn.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0])
            hash_inm_antes = _calcular_sha256_inmutables(conn, "predicciones")
            hash_inm_10_antes = _calcular_sha256_inmutables_10_sin_modelo(conn, "predicciones")
            hash_inm_11_ge_antes = _calcular_sha256_inmutables_11_desde_corte(conn, "predicciones")
            hash_res_antes = _calcular_sha256_resueltas(conn, "predicciones")

            # (ii) Crear predicciones_v2 con el DDL v2 completo
            conn.execute("DROP TABLE IF EXISTS predicciones_v2")
            conn.execute(_DDL_PREDICCIONES_V2_FMT.format(table_name="predicciones_v2"))

            # (iii) Copiar todas las filas preservando id exactos y todas las demas columnas,
            #       asignando 'legacy-pre-freeze' a las filas con fecha_prediccion < '2026-10-12'
            #       y dejando emitida_en, params_hash, score_bruto y features_json en NULL
            select_38_exprs = [
                "CASE WHEN fecha_prediccion < '2026-10-12' THEN 'legacy-pre-freeze' ELSE modelo END AS modelo"
                if col == "modelo"
                else col
                for col in _COLUMNAS_V1_38
            ]
            select_38_csv = ", ".join(select_38_exprs)
            cols_42_csv = ", ".join(_COLUMNAS_V1_38 + _COLUMNAS_V2_NUEVAS)
            conn.execute(
                f"INSERT INTO predicciones_v2 ({cols_42_csv}) "
                f"SELECT {select_38_csv}, NULL, NULL, NULL, NULL "
                f"FROM predicciones ORDER BY id ASC"
            )

            # (iv) Verificar dentro de la transaccion recuento y hashes sobre predicciones_v2
            filas_v2 = int(conn.execute("SELECT COUNT(*) FROM predicciones_v2").fetchone()[0])
            hash_inm_10_v2 = _calcular_sha256_inmutables_10_sin_modelo(conn, "predicciones_v2")
            hash_inm_11_ge_v2 = _calcular_sha256_inmutables_11_desde_corte(conn, "predicciones_v2")
            hash_res_v2 = _calcular_sha256_resueltas(conn, "predicciones_v2")

            if (
                filas_v2 != filas_antes
                or hash_inm_10_v2 != hash_inm_10_antes
                or hash_inm_11_ge_v2 != hash_inm_11_ge_antes
                or hash_res_v2 != hash_res_antes
            ):
                raise RuntimeError(
                    f"Verificacion de integridad fallida en predicciones_v2: "
                    f"filas ({filas_antes} -> {filas_v2}), "
                    f"inmutables_10_sin_modelo ({hash_inm_10_antes} -> {hash_inm_10_v2}), "
                    f"inmutables_11_desde_12oct ({hash_inm_11_ge_antes} -> {hash_inm_11_ge_v2}), "
                    f"resueltas ({hash_res_antes} -> {hash_res_v2})."
                )

            # (v) Eliminar predicciones, renombrar predicciones_v2 a predicciones, crear indices y triggers v2
            conn.execute("DROP TABLE predicciones")
            conn.execute("ALTER TABLE predicciones_v2 RENAME TO predicciones")
            for _, idx_sql in _INDICES_V2_SQL:
                conn.execute(idx_sql)
            for _, trig_sql in _TRIGGERS_V2_SQL:
                conn.execute(trig_sql)

            filas_despues = int(conn.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0])
            hash_inm_despues = _calcular_sha256_inmutables(conn, "predicciones")
            hash_inm_10_despues = _calcular_sha256_inmutables_10_sin_modelo(conn, "predicciones")
            hash_inm_11_ge_despues = _calcular_sha256_inmutables_11_desde_corte(conn, "predicciones")
            hash_res_despues = _calcular_sha256_resueltas(conn, "predicciones")
            filas_legacy_marcadas = int(
                conn.execute(
                    "SELECT COUNT(*) FROM predicciones WHERE fecha_prediccion < '2026-10-12' AND modelo = 'legacy-pre-freeze'"
                ).fetchone()[0]
            )

            if (
                filas_despues != filas_antes
                or hash_inm_10_despues != hash_inm_10_antes
                or hash_inm_11_ge_despues != hash_inm_11_ge_antes
                or hash_res_despues != hash_res_antes
            ):
                raise RuntimeError(
                    f"Verificacion final de integridad fallida tras renombrar predicciones: "
                    f"filas ({filas_antes} -> {filas_despues}), "
                    f"inmutables_10_sin_modelo ({hash_inm_10_antes} -> {hash_inm_10_despues}), "
                    f"inmutables_11_desde_12oct ({hash_inm_11_ge_antes} -> {hash_inm_11_ge_despues}), "
                    f"resueltas ({hash_res_antes} -> {hash_res_despues})."
                )

            conn.execute("COMMIT")
            return {
                "migrada": True,
                "filas_antes": filas_antes,
                "filas_despues": filas_despues,
                "filas_legacy_marcadas": filas_legacy_marcadas,
                "hash_inmutables_antes": hash_inm_antes,
                "hash_inmutables_despues": hash_inm_despues,
                "hash_inmutables_10_sin_modelo_antes": hash_inm_10_antes,
                "hash_inmutables_10_sin_modelo_despues": hash_inm_10_despues,
                "hash_inmutables_11_desde_12oct_antes": hash_inm_11_ge_antes,
                "hash_inmutables_11_desde_12oct_despues": hash_inm_11_ge_despues,
                "hash_resueltas_antes": hash_res_antes,
                "hash_resueltas_despues": hash_res_despues,
            }
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = orig_isolation
        conn.row_factory = orig_row_factory
        if own_conn:
            conn.close()


def init_predicciones_table(conn: Optional[sqlite3.Connection] = None) -> None:
    """
    Crea las tablas de tracking y ranking_historico si no existen. Idempotente.

    Optimizacion v2:
      - Comprueba primero en solo lectura si la tabla `predicciones` ya tiene el
        esquema v2 (las 4 columnas nuevas, UNIQUE(ticker, horizonte, fecha_prediccion, modelo)
        en sqlite_master y los 3 indices). Si ya esta en v2, retorna inmediatamente
        sin ejecutar ningun CREATE, ALTER, UPDATE ni commit().
      - Si la tabla no existe, la crea directamente en esquema v2 junto con
        `ranking_historico` y los 3 indices.
      - Si existe en esquema v1, migra transaccionalmente via `migrar_predicciones_v2`
        preservando intactos los IDs y las 11 columnas inmutables.
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    try:
        if _es_esquema_v2(conn):
            return

        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'predicciones'"
        ).fetchone()
        if not row_tbl:
            conn.executescript(_DDL)
            conn.commit()
            return

        conn.execute(_DDL_RANKING_HISTORICO)
        conn.commit()
        migrar_predicciones_v2(conn=conn)
    finally:
        if own_conn:
            conn.close()


def _horizontes() -> Dict[str, int]:
    """Obtiene el mapa horizonte→días del motor de señales (import perezoso)."""
    try:
        from services.signal_engine import HORIZONTES
        return dict(HORIZONTES)
    except Exception:
        return dict(_HORIZONTES_DEFAULT)


def fecha_objetivo_habil(dias: int, desde: Optional[date] = None) -> str:
    """Fecha objetivo = desde + N días hábiles bursátiles (ISO YYYY-MM-DD), respetando festivos oficiales BME."""
    return fecha_habil_bme(dias, desde)


# ═══════════════════════════════════════════════════════════════════
# 📝 REGISTRO
# ═══════════════════════════════════════════════════════════════════

def guardar_snapshot_ranking(fecha: str) -> int:
    """
    Guarda un snapshot del ranking fundamental actual en la tabla ranking_historico.
    Se ejecuta al emitir señales con barras completas (Fase 1).
    """
    conn = get_connection()
    try:
        rows = conn.execute("SELECT ticker, posicion, score FROM ranking_estado").fetchall()
        if not rows:
            return 0
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO ranking_historico (fecha, ticker, posicion, score)
            VALUES (?, ?, ?, ?)
            """,
            [(fecha, str(r[0]), int(r[1]), float(r[2])) for r in rows],
        )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    except Exception:
        return 0
    finally:
        conn.close()


def registrar_predicciones(df_radar: pd.DataFrame,
                           fecha_prediccion: Optional[str] = None,
                           modelo: str = MOTOR_VERSION,
                           params_hash: Optional[str] = None) -> int:
    """
    Registra las señales del radar para una fecha de sesión específica.

    Fase 1 (Higiene e Inmutabilidad):
      - `fecha_prediccion`: fecha obligatoria de la sesión bursátil (YYYY-MM-DD).
      - Guarda intradía: si fecha_prediccion es hoy (hora de Madrid) y la barra
        no está completa (antes del corte BME 18:30 CET), no inserta y retorna 0.
      - `ON CONFLICT DO NOTHING`: las señales emitidas son inmutables; jamás
        se sobreescriben precios de entrada ni estimaciones.
      - Almacena la salida cruda (pct_estimado_raw, sigma, umbral_zona, etc.)
        para auditoría cuantitativa sin distorsión.

    Parámetros:
    -----------
    df_radar : DataFrame
        Salida de signal_engine con señales y componentes crudos.
    fecha_prediccion : str
        Fecha obligatoria en formato ISO (YYYY-MM-DD). Si falta o es inválida, lanza ValueError.
    modelo : str
        Versión del modelo emisor (por defecto 'v5-frozen').

    Retorna:
    --------
    int — número de filas nuevas insertadas (0 si ya existían o barra incompleta).
    """
    if not fecha_prediccion or not isinstance(fecha_prediccion, str) or not fecha_prediccion.strip():
        raise ValueError("fecha_prediccion es obligatoria (debe ser una cadena con formato YYYY-MM-DD).")

    fecha_prediccion_limpia = fecha_prediccion.strip()
    try:
        f_date = date.fromisoformat(fecha_prediccion_limpia[:10])
        f_pred = f_date.isoformat()
    except (ValueError, TypeError) as e:
        raise ValueError(f"fecha_prediccion inválida: '{fecha_prediccion}'. Debe tener formato YYYY-MM-DD.") from e

    # Guarda: si fecha_prediccion es hoy (hora de Madrid) y la barra de hoy no está completa, no insertar nada
    ahora_madrid = get_ahora_madrid()
    hoy_madrid = ahora_madrid.date().isoformat()
    if f_pred == hoy_madrid and not barra_hoy_completa():
        logger.warning(
            f"registrar_predicciones abortado: fecha_prediccion ({f_pred}) corresponde a la sesión "
            f"en curso de hoy y la barra aún no está consolidada (corte BME {HORA_CORTE_BME}:{MINUTO_CORTE_BME:02d} Madrid). "
            f"0 filas insertadas."
        )
        return 0

    # Guarda de ventana: una señal de la sesión f_pred solo es válida si se emite en [18:30, límite S+1 08:30)
    if not en_ventana_emision(f_pred, ahora=ahora_madrid):
        limite_iso = limite_emision(f_pred).isoformat()
        logger.warning(
            f"emisión tardía: fecha_prediccion ({f_pred}) está fuera de la ventana válida "
            f"[corte 18:30, límite {limite_iso}) para la hora actual ({ahora_madrid.isoformat()}). "
            f"0 filas insertadas."
        )
        return 0

    if df_radar is None or df_radar.empty:
        return 0

    # Defensa en profundidad: si df_radar contiene 'ultima_fecha', omitir filas con fecha distinta a f_pred
    if "ultima_fecha" in df_radar.columns:
        def _norm_uf_dp(val) -> str:
            if val is None or pd.isna(val):
                return ""
            try:
                return pd.to_datetime(val).strftime("%Y-%m-%d")
            except Exception:
                return str(val)[:10]

        fechas_filas = df_radar["ultima_fecha"].apply(_norm_uf_dp)
        desalineadas = (fechas_filas != f_pred)
        n_desalineadas = int(desalineadas.sum())
        if n_desalineadas > 0:
            tickers_omitidos = sorted(df_radar.loc[desalineadas, "ticker"].dropna().unique().tolist())
            logger.warning(
                f"registrar_predicciones: {n_desalineadas} filas omitidas por tener ultima_fecha "
                f"distinta a fecha_prediccion ({f_pred}): {tickers_omitidos}"
            )
            df_radar = df_radar.loc[~desalineadas].copy()

    if df_radar.empty:
        return 0

    ahora_iso = datetime.now().isoformat()
    if getattr(ahora_madrid, "tzinfo", None) is not None:
        ahora_utc = ahora_madrid.astimezone(timezone.utc)
    else:
        ahora_utc = ahora_madrid.replace(tzinfo=timezone.utc)
    emitida_en_iso = ahora_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Blindaje Fase 1: para MOTOR_VERSION ("v5-frozen") params_hash es estrictamente invariante
    params_hash_default = get_params_hash() if (modelo == MOTOR_VERSION or params_hash is None) else params_hash

    horizontes = _horizontes()
    filas = []

    for _, r in df_radar.iterrows():
        precio = r.get("precio")
        if precio is None or not np.isfinite(precio) or precio <= 0:
            continue

        # Componentes y metadatos comunes al ticker
        c_rsi = _f(r.get("comp_rsi"))
        c_macd = _f(r.get("comp_macd"))
        c_tend = _f(r.get("comp_tendencia"))
        c_mom = _f(r.get("comp_momentum"))
        adx_val = _f(r.get("adx"))
        rvol_val = _f(r.get("rvol"))
        marea_raw = r.get("marea", "")
        marea_val = "" if (marea_raw is None or (isinstance(marea_raw, float) and np.isnan(marea_raw))) else str(marea_raw)
        rs_ibex_val = _f(r.get("rs_ibex"))
        sent_val = _f(r.get("sentimiento"))
        tilt_val = _f(r.get("tilt"))

        row_ph = r.get("params_hash")
        if modelo == MOTOR_VERSION:
            ph_val = get_params_hash()
        elif row_ph is None or (isinstance(row_ph, float) and np.isnan(row_ph)):
            ph_val = str(params_hash_default)
        else:
            ph_val = str(row_ph)

        for hz, dias in horizontes.items():
            senal = r.get(f"senal_{hz}")
            if senal is None or (isinstance(senal, float) and np.isnan(senal)):
                continue
            senal_str = str(senal).upper().strip()
            pct_val = _f(r.get(f"pct_{hz}"))
            pct_raw_val = _f(r.get(f"pct_raw_{hz}"))
            if pct_raw_val is None:
                pct_raw_val = pct_val
            umbral_val = _f(r.get(f"umbral_{hz}")) or UMBRALES_LATERAL.get(hz, 0.8)
            vol_val = _f(r.get("vol_banda")) or _f(r.get("vol_d_frac")) or _f(r.get(f"sigma_{hz}"))

            score_motor_val = _f(r.get(f"score_{hz}"))
            score_bruto_val = _f(r.get(f"score_bruto_{hz}"))
            if score_bruto_val is None:
                score_bruto_val = _f(r.get("score_bruto"))
            if score_bruto_val is None:
                score_bruto_val = score_motor_val

            banda_val = _f(r.get(f"banda_{hz}"))
            conf_val = _f(r.get(f"conf_{hz}"))

            raw_feat_override = r.get("features_json")
            if isinstance(raw_feat_override, str) and raw_feat_override.strip():
                try:
                    parsed_feat = json.loads(raw_feat_override)
                    features_json_str = json.dumps(parsed_feat, sort_keys=True, separators=(",", ":"))
                except Exception:
                    features_json_str = raw_feat_override.strip()
            elif isinstance(raw_feat_override, dict):
                features_json_str = json.dumps(raw_feat_override, sort_keys=True, separators=(",", ":"))
            else:
                feat_dict = {
                    "adx": adx_val,
                    "banda_pct": banda_val,
                    "comp_macd": c_macd,
                    "comp_momentum": c_mom,
                    "comp_rsi": c_rsi,
                    "comp_tendencia": c_tend,
                    "confianza": conf_val,
                    "marea": marea_val,
                    "pct_estimado": pct_val,
                    "pct_estimado_raw": pct_raw_val,
                    "rs_ibex": rs_ibex_val,
                    "rvol": rvol_val,
                    "score_bruto": score_bruto_val,
                    "score_motor": score_motor_val,
                    "sentimiento": sent_val,
                    "sigma": vol_val,
                    "tilt": tilt_val,
                    "umbral_zona": umbral_val,
                }
                for opt_k in ("rsi", "macd_h", "mom5", "mom20", "vol_d", "atr_d", "vol_confirm", "sr_bias"):
                    if opt_k in r:
                        v_opt = _f(r.get(opt_k))
                        if v_opt is not None:
                            feat_dict[opt_k] = v_opt
                features_json_str = json.dumps(feat_dict, sort_keys=True, separators=(",", ":"))

            f_obj = fecha_objetivo_habil(dias, desde=f_date)

            filas.append((
                r["ticker"],
                r.get("empresa", ""),
                f_pred,
                hz,
                dias,
                f_obj,
                senal_str,
                pct_val,
                banda_val,
                conf_val,
                float(precio),
                score_motor_val,
                modelo,
                pct_raw_val,
                umbral_val,
                vol_val,
                c_rsi,
                c_macd,
                c_tend,
                c_mom,
                adx_val,
                rvol_val,
                marea_val,
                rs_ibex_val,
                sent_val,
                tilt_val,
                ahora_iso,
                REGLA_EVAL_VERSION,
                emitida_en_iso,
                ph_val,
                score_bruto_val,
                features_json_str,
            ))

    if not filas:
        return 0

    conn = get_connection()
    try:
        changes_before = conn.total_changes
        cur = conn.executemany(
            """
            INSERT INTO predicciones
            (ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
             senal, pct_estimado, banda_pct, confianza, precio_entrada,
             score_motor, modelo, pct_estimado_raw, umbral_zona, sigma,
             comp_rsi, comp_macd, comp_tendencia, comp_momentum, adx,
             rvol, marea, rs_ibex, sentimiento, tilt, hora_calculo, regla_eval,
             emitida_en, params_hash, score_bruto, features_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker, horizonte, fecha_prediccion, modelo) DO NOTHING
            """,
            filas,
        )
        conn.commit()
        insertadas = cur.rowcount if (cur.rowcount is not None and cur.rowcount >= 0) else (conn.total_changes - changes_before)
        ignoradas = len(filas) - insertadas
        logger.info(
            f"registrar_predicciones: {insertadas} insertadas, {ignoradas} ignoradas por conflicto "
            f"(fecha={f_pred}, modelo={modelo}, total={len(filas)})."
        )
        return insertadas
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════
# ✅ EVALUACIÓN Y RESOLUCIÓN
# ═══════════════════════════════════════════════════════════════════

def _evaluar_acierto_direccion(senal: str, variacion_real: float, banda_pct: float = 0.0,
                               horizonte: str = "1D") -> int:
    """
    Evaluación binaria por zonas (Capa 1 del sistema de scoring).
    Es el criterio PRINCIPAL que alimenta la Tasa de Acierto.

    El umbral lateral define tres zonas según el horizonte:
      1D: ±0.8%  |  3D: ±1.2%  |  1S: ±1.8%  |  1M: ±3.0%

    Regla:
      - ALCISTA → acierto si variación real > +umbral
      - BAJISTA → acierto si variación real < -umbral
      - LATERAL → acierto si |variación| ≤ umbral

    Retorna: 1 (acierto) o 0 (fallo).
    """
    if variacion_real is None or not np.isfinite(variacion_real):
        return 0
    senal = (senal or "").upper().strip()
    hz = (horizonte or "").upper().strip()
    umbral = UMBRALES_LATERAL.get(hz, 0.8)
    if senal == "ALCISTA":
        return int(variacion_real > umbral)
    if senal == "BAJISTA":
        return int(variacion_real < -umbral)
    # LATERAL: acierto si el precio se mantuvo dentro del margen
    return int(abs(variacion_real) <= umbral)


# Alias retrocompatible
_evaluar_acierto = _evaluar_acierto_direccion


def _calcular_score_calidad(
    senal: str,
    pct_estimado: float,
    variacion_real: float,
    banda_pct: float = 0.0,
    horizonte: str = "1D",
) -> float:
    """
    Score de Calidad 0-100 (Capa 2 del sistema de scoring).

    Composición:
      - Dirección correcta: 60 puntos (0 si falla)
      - Precisión de magnitud: 0-40 puntos (gradual)
        Fórmula: 40 × max(0, 1 - |error| / (3 × referencia))
        donde error = |predicho% - real%|, referencia = max(banda, 1.5%)

    Ejemplos (predicción ALCISTA +3.0%, banda ±2.0%):
      Real +0.1% → 60 + 20.7 = 80.7   (acierto técnico, impreciso)
      Real -0.5% → 0 + 16.7  = 16.7   (fallo, dirección incorrecta)
      Real -5.0% → 0 + 0.0   = 0.0    (fallo total)
    """
    pct_est = float(pct_estimado) if pct_estimado is not None and np.isfinite(pct_estimado) else 0.0
    var_real = float(variacion_real) if variacion_real is not None and np.isfinite(variacion_real) else 0.0
    banda = float(banda_pct) if banda_pct is not None and np.isfinite(banda_pct) else 0.0
    hz = (horizonte or "").upper().strip()

    # Puntos de dirección (0 o 60)
    acierto = _evaluar_acierto_direccion(senal, var_real, banda, hz)
    pts_dir = 60.0 if acierto else 0.0

    # Puntos de precisión (0-40)
    error = abs(pct_est - var_real)
    referencia = max(abs(banda), 1.5)  # Floor en 1.5% para evitar div/0
    pts_precision = 40.0 * max(0.0, 1.0 - error / (3.0 * referencia))

    return round(pts_dir + pts_precision, 1)


def _ultima_sesion_bme_cerrada(hoy_date: date, mercado_cerrado: bool, festivos: set) -> date:
    """
    Devuelve la fecha de la ultima sesion habil bursatil BME ya cerrada:
    hoy si sesion_cerrada_hoy() y hoy es habil BME, o el dia habil BME anterior en caso contrario.
    """
    cand = hoy_date if mercado_cerrado else (hoy_date - timedelta(days=1))
    while cand.weekday() >= 5 or cand.isoformat() in festivos:
        cand -= timedelta(days=1)
    return cand


def _sesiones_bme_transcurridas(fecha_objetivo_iso: str, ult_cerrada: date, festivos: set) -> int:
    """
    Cuenta el numero de dias habiles BME d tales que fecha_objetivo < d <= ult_cerrada.
    """
    try:
        d_obj = date.fromisoformat(str(fecha_objetivo_iso)[:10])
    except (ValueError, TypeError):
        return 0
    if d_obj >= ult_cerrada:
        return 0
    cnt = 0
    cur = d_obj + timedelta(days=1)
    while cur <= ult_cerrada:
        if cur.weekday() < 5 and cur.isoformat() not in festivos:
            cnt += 1
        cur += timedelta(days=1)
    return cnt


def _evaluar_salud_lote(df_precios: Optional[pd.DataFrame], fecha_iso: str):
    """
    Verifica si el lote esta sano en fecha_iso: al menos el 50 % de los valores
    del lote tienen barra Close finita y > 0 en fecha_iso.
    Retorna (es_sano, n_validos, n_totales).
    """
    if df_precios is None or df_precios.empty:
        return False, 0, 0

    if isinstance(df_precios.columns, pd.MultiIndex):
        if "Close" in df_precios.columns.get_level_values(-1):
            sub_closes = df_precios.xs("Close", level=-1, axis=1)
        elif "Close" in df_precios.columns.get_level_values(0):
            sub_closes = df_precios.xs("Close", level=0, axis=1)
        elif "Close" in df_precios.columns.get_level_values(1):
            sub_closes = df_precios.xs("Close", level=1, axis=1)
        else:
            return False, 0, 0
        if isinstance(sub_closes, pd.Series):
            sub_closes = sub_closes.to_frame()
    else:
        if "Close" in df_precios.columns:
            sub_closes = pd.DataFrame({"SINGLE": df_precios["Close"]}, index=df_precios.index)
        else:
            return False, 0, 0

    n_totales = int(sub_closes.shape[1])
    if n_totales == 0:
        return False, 0, 0

    dt_idx = pd.to_datetime(sub_closes.index)
    if getattr(dt_idx, "tz", None) is not None:
        dt_idx = dt_idx.tz_convert("Europe/Madrid")
    idx_str = dt_idx.strftime("%Y-%m-%d")

    matching_rows = sub_closes.loc[idx_str == fecha_iso]
    if matching_rows.empty:
        return False, 0, n_totales

    ultima_fila = matching_rows.iloc[-1]
    vals = pd.to_numeric(ultima_fila, errors="coerce")
    validos_mask = vals.notna() & np.isfinite(vals) & (vals > 0)
    n_validos = int(validos_mask.sum())
    es_sano = (n_validos * 2) >= n_totales
    return es_sano, n_validos, n_totales


def resolver_predicciones(precios: Optional[Dict] = None,
                          df_batch: Optional[pd.DataFrame] = None) -> int:
    """
    Resuelve predicciones vencidas buscando el precio de cierre oficial
    en la fecha_objetivo EXACTA en la serie historica (Fase 1 - Higiene, C-25f).

    El reloj de referencia es Madrid (CET/CEST) via get_ahora_madrid().date(),
    alineado con sesion_cerrada_hoy().

    El parametro `precios` esta obsoleto y se ignora (se mantiene solo por
    compatibilidad de firma). La resolucion se efectua exclusivamente contra
    barras historicas consolidadas (df_batch o descarga batch).

    Reglas institucionales:
      1. Solo se evaluan predicciones con resuelta = 0 cuya fecha_objetivo sea
         resoluble (fecha_objetivo < hoy, o fecha_objetivo <= hoy con sesion cerrada).
      2. Barra exacta: precio_real = Close (finito y > 0) de la barra con fecha
         EXACTAMENTE igual a fecha_objetivo. Nunca se usa una barra posterior.
      3. Periodo de gracia y estado sin_cotizacion: si no existe barra exacta en
         fecha_objetivo, la fila permanece pendiente (resuelta = 0) mientras hayan
         transcurrido menos de SESIONES_SIN_COTIZACION (5) sesiones habiles BME
         cerradas posteriores a fecha_objetivo (si la barra exacta aparece dentro
         del periodo de gracia, se resuelve normalmente como resuelta = 1).
      4. Cuando han transcurrido >= SESIONES_SIN_COTIZACION sesiones BME cerradas
         sin barra exacta y el lote esta sano (>= 50 % de los valores del lote
         tienen barra valida en la ultima sesion BME cerrada), la fila pasa a
         resuelta = RESUELTA_SIN_COTIZACION (2), motivo_resolucion = 'sin_cotizacion',
         fecha_resolucion = hoy Madrid (ISO), dejando precio_real, variacion_real,
         acierto, error_pct y score_calidad en NULL. Si el lote no esta sano, no se
         marca ninguna fila como sin_cotizacion.
      5. Retorno: numero de predicciones resueltas normalmente (resuelta = 1).
    """
    if precios:
        logger.warning("parámetro precios obsoleto e ignorado")

    ahora_madrid = get_ahora_madrid()
    hoy_date = ahora_madrid.date()
    hoy = hoy_date.isoformat()
    mercado_cerrado = sesion_cerrada_hoy()
    conn = get_connection()
    resueltas = 0
    siguen_pendientes = 0
    marcadas_sin_cotizacion = 0
    tickers_sin_cotizacion = []
    candidatas_bloqueadas_lote = 0

    try:
        conn.row_factory = None
        op_venc = "<=" if mercado_cerrado else "<"

        cur = conn.execute(
            f"""
            SELECT id, ticker, horizonte, senal, pct_estimado, banda_pct,
                   precio_entrada, fecha_objetivo
            FROM predicciones
            WHERE resuelta = 0 AND fecha_objetivo {op_venc} ?
            ORDER BY fecha_objetivo ASC
            """,
            (hoy,),
        )
        pendientes = cur.fetchall()
        if not pendientes:
            return 0

        # Obtener datos de precios para buscar el cierre exacto por fecha
        from services.data_provider import descargar_price_batch
        from services.signal_engine import obtener_serie_ticker

        tickers_pendientes = tuple({r[1] for r in pendientes})
        if df_batch is None or df_batch.empty:
            df_precios = descargar_price_batch(tickers_pendientes, period="1y")
        else:
            df_precios = df_batch

        min_year = min(
            (int(str(r[7])[:4]) for r in pendientes if r[7] and len(str(r[7])) >= 4),
            default=hoy_date.year,
        )
        festivos_set = set(festivos_bme_rango(min(min_year, hoy_date.year) - 1, hoy_date.year + 2))
        ult_cerrada_date = _ultima_sesion_bme_cerrada(hoy_date, mercado_cerrado, festivos_set)
        ult_cerrada_iso = ult_cerrada_date.isoformat()
        lote_sano, n_val_lote, n_tot_lote = _evaluar_salud_lote(df_precios, ult_cerrada_iso)

        for pid, ticker, horizonte, senal, pct_est, banda, precio_entrada, f_obj in pendientes:
            sub = obtener_serie_ticker(df_precios, ticker)
            precio_real = None

            if sub is not None and not sub.empty and "Close" in sub.columns:
                closes = pd.to_numeric(sub["Close"], errors="coerce").dropna()
                if not closes.empty:
                    dt_sub = pd.to_datetime(closes.index)
                    if getattr(dt_sub, "tz", None) is not None:
                        dt_sub = dt_sub.tz_convert("Europe/Madrid")
                    idx_str = dt_sub.strftime("%Y-%m-%d")
                    # Buscar exclusivamente la barra exacta en fecha_objetivo
                    matching = closes[idx_str == f_obj]
                    if not matching.empty:
                        val_cierre = float(matching.iloc[-1])
                        if np.isfinite(val_cierre) and val_cierre > 0:
                            precio_real = val_cierre

            if precio_real is None or not np.isfinite(precio_real) or precio_real <= 0:
                transcurridas = _sesiones_bme_transcurridas(f_obj, ult_cerrada_date, festivos_set)
                if transcurridas >= SESIONES_SIN_COTIZACION:
                    if lote_sano:
                        conn.execute(
                            """
                            UPDATE predicciones
                            SET resuelta = ?, motivo_resolucion = ?, fecha_resolucion = ?,
                                precio_real = NULL, variacion_real = NULL, acierto = NULL,
                                error_pct = NULL, score_calidad = NULL
                            WHERE id = ? AND resuelta = 0
                            """,
                            (RESUELTA_SIN_COTIZACION, MOTIVO_SIN_COTIZACION, hoy, pid),
                        )
                        marcadas_sin_cotizacion += 1
                        tickers_sin_cotizacion.append(ticker)
                    else:
                        candidatas_bloqueadas_lote += 1
                        siguen_pendientes += 1
                else:
                    siguen_pendientes += 1
                continue

            if not precio_entrada or not np.isfinite(float(precio_entrada)) or float(precio_entrada) <= 0:
                siguen_pendientes += 1
                continue

            variacion = (precio_real - precio_entrada) / precio_entrada * 100.0
            acierto = _evaluar_acierto_direccion(senal, variacion, banda or 0.0, horizonte)
            error = abs((pct_est or 0.0) - variacion)
            score = _calcular_score_calidad(senal, pct_est, variacion, banda or 0.0, horizonte)

            conn.execute(
                """
                UPDATE predicciones
                SET resuelta = 1, fecha_resolucion = ?, precio_real = ?,
                    variacion_real = ?, acierto = ?, error_pct = ?,
                    motivo_resolucion = 'vencida', score_calidad = ?
                WHERE id = ? AND resuelta = 0
                """,
                (f_obj, float(precio_real), round(variacion, 4),
                 acierto, round(error, 4), score, pid),
            )
            resueltas += 1

        conn.commit()
        if candidatas_bloqueadas_lote > 0:
            logger.warning(
                "resolver_predicciones: lote no sano en %s (%d/%d valores validos < 50%%); "
                "no se marcan %d filas vencidas como sin_cotizacion.",
                ult_cerrada_iso, n_val_lote, n_tot_lote, candidatas_bloqueadas_lote,
            )
        logger.info(
            "resolver_predicciones: resueltas=%d, siguen_pendientes=%d, "
            "sin_cotizacion=%d (tickers=%s).",
            resueltas, siguen_pendientes, marcadas_sin_cotizacion,
            sorted(set(tickers_sin_cotizacion)),
        )
        return resueltas
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════
# 📊 LECTURA — HISTORIAL Y MÉTRICAS
# ═══════════════════════════════════════════════════════════════════

def obtener_resueltas(horizonte: Optional[str] = None, limite: int = 300,
                      incluir_legacy: bool = False) -> pd.DataFrame:
    """Devuelve las predicciones resueltas activas (ordenadas por fecha_resolucion DESC)."""
    conn = get_connection()
    try:
        where_clauses = ["p.resuelta = 1"]
        params: list = []
        if not incluir_legacy:
            where_clauses.append("p.modelo != 'legacy-pre-freeze'")
        if horizonte and horizonte != "Todos":
            where_clauses.append("p.horizonte = ?")
            params.append(horizonte)
        where = f"WHERE {' AND '.join(where_clauses)}"
        order = "ORDER BY p.fecha_resolucion DESC, p.id DESC"
        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
        ).fetchone()
        if row_tbl:
            sql = f"""
            SELECT p.*,
                   ep.variacion_pct AS var_precio_c10,
                   ep.acierto AS acierto_precio_c10,
                   et.variacion_pct AS var_retorno_total_c10,
                   et.acierto AS acierto_retorno_total_c10,
                   et.diferencia_dividendo_pct AS diferencia_dividendo_pct,
                   ea.precio_entrada_eval AS precio_apertura_c11,
                   ea.variacion_pct AS var_apertura_precio_c11,
                   ea.acierto AS acierto_apertura_precio_c11,
                   eat.variacion_pct AS var_apertura_total_c11,
                   eat.acierto AS acierto_apertura_total_c11,
                   ev2.umbral_aplicado AS umbral_v2_c12,
                   ev2.clase_real AS clase_real_v2_c12,
                   ev2.acierto AS acierto_v2_c12
            FROM predicciones p
            LEFT JOIN evaluaciones ep
              ON ep.prediccion_id = p.id AND ep.regla = ?
            LEFT JOIN evaluaciones et
              ON et.prediccion_id = p.id AND et.regla = ?
            LEFT JOIN evaluaciones ea
              ON ea.prediccion_id = p.id AND ea.regla = ?
            LEFT JOIN evaluaciones eat
              ON eat.prediccion_id = p.id AND eat.regla = ?
            LEFT JOIN evaluaciones ev2
              ON ev2.prediccion_id = p.id AND ev2.regla = ?
            {where}
            {order}
            LIMIT ?
            """
            full_params = [
                REGLA_C10_PRECIO_CIERRE,
                REGLA_C10_RETORNO_TOTAL,
                REGLA_C11_APERTURA_PRECIO,
                REGLA_C11_APERTURA_RETORNO_TOTAL,
                REGLA_C12_VOL_PRECIO_CIERRE,
            ] + params + [int(limite)]
        else:
            sql = f"""
            SELECT p.*,
                   CAST(NULL AS REAL) AS var_precio_c10,
                   CAST(NULL AS INTEGER) AS acierto_precio_c10,
                   CAST(NULL AS REAL) AS var_retorno_total_c10,
                   CAST(NULL AS INTEGER) AS acierto_retorno_total_c10,
                   CAST(NULL AS REAL) AS diferencia_dividendo_pct,
                   CAST(NULL AS REAL) AS precio_apertura_c11,
                   CAST(NULL AS REAL) AS var_apertura_precio_c11,
                   CAST(NULL AS INTEGER) AS acierto_apertura_precio_c11,
                   CAST(NULL AS REAL) AS var_apertura_total_c11,
                   CAST(NULL AS INTEGER) AS acierto_apertura_total_c11,
                   CAST(NULL AS REAL) AS umbral_v2_c12,
                   CAST(NULL AS TEXT) AS clase_real_v2_c12,
                   CAST(NULL AS INTEGER) AS acierto_v2_c12
            FROM predicciones p
            {where}
            {order}
            LIMIT ?
            """
            full_params = params + [int(limite)]
        return pd.read_sql_query(sql, conn, params=tuple(full_params))
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def obtener_pendientes(horizonte: Optional[str] = None, solo_vigentes: bool = True, limite: int = 1500) -> pd.DataFrame:
    """
    Devuelve las predicciones en seguimiento activo (no resueltas y no archivadas).
    Si solo_vigentes=True, devuelve la emisión más reciente por cada par (ticker, horizonte),
    garantizando que aparezcan todas las empresas activas sin truncamiento (límite ampliado a 1500).
    """
    conn = get_connection()
    try:
        params: list = []
        if solo_vigentes:
            where_hz = ""
            if horizonte and horizonte != "Todos":
                where_hz = "AND p.horizonte = ?"
                params.append(horizonte)
            sql = f"""
            SELECT p.*
            FROM predicciones p
            INNER JOIN (
                SELECT ticker, horizonte, MAX(fecha_prediccion) as max_fecha
                FROM predicciones
                WHERE resuelta = 0
                GROUP BY ticker, horizonte
            ) ult ON p.ticker = ult.ticker 
                 AND p.horizonte = ult.horizonte 
                 AND p.fecha_prediccion = ult.max_fecha
            WHERE p.resuelta = 0 {where_hz}
            ORDER BY p.fecha_objetivo ASC, p.empresa ASC
            LIMIT ?
            """
        else:
            where_hz_simple = "AND horizonte = ?" if (horizonte and horizonte != "Todos") else ""
            if horizonte and horizonte != "Todos":
                params.append(horizonte)
            sql = f"""
            SELECT * FROM predicciones
            WHERE resuelta = 0 {where_hz_simple}
            ORDER BY fecha_objetivo ASC, id DESC
            LIMIT ?
            """
        params.append(int(limite))
        return pd.read_sql_query(sql, conn, params=tuple(params))
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def obtener_historial(limite: int = 300, solo_resueltas: bool = False,
                      incluir_legacy: bool = False) -> pd.DataFrame:
    """Devuelve el historial de predicciones resueltas o general (útil para auditoría y exportación)."""
    if solo_resueltas:
        return obtener_resueltas(limite=limite, incluir_legacy=incluir_legacy)
    conn = get_connection()
    try:
        where = "" if incluir_legacy else "WHERE p.modelo != 'legacy-pre-freeze'"
        order = "ORDER BY COALESCE(p.fecha_resolucion, p.fecha_prediccion) DESC, p.id DESC"
        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
        ).fetchone()
        if row_tbl:
            sql = f"""
            SELECT p.*,
                   ep.variacion_pct AS var_precio_c10,
                   ep.acierto AS acierto_precio_c10,
                   et.variacion_pct AS var_retorno_total_c10,
                   et.acierto AS acierto_retorno_total_c10,
                   et.diferencia_dividendo_pct AS diferencia_dividendo_pct,
                   ea.precio_entrada_eval AS precio_apertura_c11,
                   ea.variacion_pct AS var_apertura_precio_c11,
                   ea.acierto AS acierto_apertura_precio_c11,
                   eat.variacion_pct AS var_apertura_total_c11,
                   eat.acierto AS acierto_apertura_total_c11,
                   ev2.umbral_aplicado AS umbral_v2_c12,
                   ev2.clase_real AS clase_real_v2_c12,
                   ev2.acierto AS acierto_v2_c12
            FROM predicciones p
            LEFT JOIN evaluaciones ep
              ON ep.prediccion_id = p.id AND ep.regla = ?
            LEFT JOIN evaluaciones et
              ON et.prediccion_id = p.id AND et.regla = ?
            LEFT JOIN evaluaciones ea
              ON ea.prediccion_id = p.id AND ea.regla = ?
            LEFT JOIN evaluaciones eat
              ON eat.prediccion_id = p.id AND eat.regla = ?
            LEFT JOIN evaluaciones ev2
              ON ev2.prediccion_id = p.id AND ev2.regla = ?
            {where}
            {order}
            LIMIT ?
            """
            full_params = (
                REGLA_C10_PRECIO_CIERRE,
                REGLA_C10_RETORNO_TOTAL,
                REGLA_C11_APERTURA_PRECIO,
                REGLA_C11_APERTURA_RETORNO_TOTAL,
                REGLA_C12_VOL_PRECIO_CIERRE,
                int(limite),
            )
        else:
            sql = f"""
            SELECT p.*,
                   CAST(NULL AS REAL) AS var_precio_c10,
                   CAST(NULL AS INTEGER) AS acierto_precio_c10,
                   CAST(NULL AS REAL) AS var_retorno_total_c10,
                   CAST(NULL AS INTEGER) AS acierto_retorno_total_c10,
                   CAST(NULL AS REAL) AS diferencia_dividendo_pct,
                   CAST(NULL AS REAL) AS precio_apertura_c11,
                   CAST(NULL AS REAL) AS var_apertura_precio_c11,
                   CAST(NULL AS INTEGER) AS acierto_apertura_precio_c11,
                   CAST(NULL AS REAL) AS var_apertura_total_c11,
                   CAST(NULL AS INTEGER) AS acierto_apertura_total_c11,
                   CAST(NULL AS REAL) AS umbral_v2_c12,
                   CAST(NULL AS TEXT) AS clase_real_v2_c12,
                   CAST(NULL AS INTEGER) AS acierto_v2_c12
            FROM predicciones p
            {where}
            {order}
            LIMIT ?
            """
            full_params = (int(limite),)
        return pd.read_sql_query(sql, conn, params=full_params)
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


# ===================================================================
# C-09: UNA DEFINICION DE ACIERTO POR METRICA Y MUESTRA VISIBLE
# ===================================================================
# Metrica 1 - Acierto en 3 clases (ALCISTA / LATERAL / BAJISTA):
#   referencia ingenua = "siempre lateral" (% de observaciones con clase real LATERAL).
# Metrica 2 - Acierto de signo (solo senales direccionales ALCISTA / BAJISTA):
#   acierto si (ALCISTA y variacion > 0) o (BAJISTA y variacion < 0);
#   referencias = azar 50.0% y frecuencia base de subidas (% variacion > 0).
# Toda tasa va acompanada de la etiqueta de muestra:
#   "n senales . n fechas . periodo . horizonte" (con separador U+00B7).

REF_AZAR_SIGNO_PCT = 50.0
HORIZONTES_C09 = ("1D", "3D", "1S", "1M")
_SEP_MUESTRA = " \u00b7 "
_TXT_SENALES = "se\u00f1ales"

CLAVES_MUESTRA_C09 = (
    "n_senales",
    "n_fechas",
    "fecha_min",
    "fecha_max",
    "periodo_str",
    "horizonte_label",
    "etiqueta_muestra",
)

CLAVES_METRICAS_C09 = (
    "tasa_acierto_3clases",
    "ref_siempre_lateral_pct",
    "dist_clases_reales",
    "dist_senales_emitidas",
    "tasa_alcista_3c",
    "tasa_bajista_3c",
    "tasa_lateral_3c",
    "ret_alcista_medio",
    "ret_bajista_medio",
    "n_direccionales",
    "aciertos_signo",
    "tasa_acierto_signo",
    "ref_azar_signo_pct",
    "ref_frecuencia_subida_pct",
)

# C-13: Inferencia honesta agregada por bloques de fecha de emision
MIN_FECHAS_INFERENCIA = 30
MIN_FECHAS_IC_BASICO = 2
CLAVES_INFERENCIA_C13 = (
    "inferencia_3clases",
    "inferencia_signo",
)


def _normalizar_hz_label(horizonte: Optional[str]) -> str:
    """Devuelve la etiqueta de horizonte ('Todos' si es None o vacio)."""
    if horizonte is None:
        return "Todos"
    h = str(horizonte).strip()
    return h if h else "Todos"


def _periodo_muestra(n_senales: int, fecha_min: Optional[str], fecha_max: Optional[str]) -> str:
    """Devuelve 'fecha_min a fecha_max' o 'Sin muestra' si no hay senales."""
    if int(n_senales or 0) <= 0:
        return "Sin muestra"
    f_min = str(fecha_min)[:10] if fecha_min else ""
    f_max = str(fecha_max)[:10] if fecha_max else ""
    if f_min and f_max:
        return f"{f_min} a {f_max}"
    return f_min or f_max or "Sin muestra"


def formatear_etiqueta_muestra(
    n_senales: int,
    n_fechas: int,
    fecha_min: Optional[str],
    fecha_max: Optional[str],
    horizonte: Optional[str] = None,
) -> str:
    """
    Etiqueta canonica de muestra (C-09):
      "{n_senales} senales . {n_fechas} fechas . {periodo} . {hz_label}"
    (con la palabra 'senales' escrita con enye y separador punto medio U+00B7).
    Con n_senales == 0 devuelve "0 senales . 0 fechas . Sin muestra . {hz_label}".
    """
    try:
        n_s = max(int(n_senales or 0), 0)
    except (TypeError, ValueError):
        n_s = 0
    try:
        n_f = max(int(n_fechas or 0), 0)
    except (TypeError, ValueError):
        n_f = 0
    if n_s == 0:
        n_f = 0
    hz_label = _normalizar_hz_label(horizonte)
    periodo = _periodo_muestra(n_s, fecha_min, fecha_max)
    return (
        f"{n_s} {_TXT_SENALES}{_SEP_MUESTRA}{n_f} fechas"
        f"{_SEP_MUESTRA}{periodo}{_SEP_MUESTRA}{hz_label}"
    )


def _metadatos_muestra(
    n_senales: int,
    n_fechas: int,
    fecha_min: Optional[str],
    fecha_max: Optional[str],
    horizonte: Optional[str] = None,
) -> Dict:
    """Subdiccionario estandar de metadatos de muestra (C-09)."""
    n_s = max(int(n_senales or 0), 0)
    n_f = max(int(n_fechas or 0), 0) if n_s > 0 else 0
    f_min = str(fecha_min)[:10] if (n_s > 0 and fecha_min) else None
    f_max = str(fecha_max)[:10] if (n_s > 0 and fecha_max) else None
    hz_label = _normalizar_hz_label(horizonte)
    return {
        "n_senales": n_s,
        "n_fechas": n_f,
        "fecha_min": f_min,
        "fecha_max": f_max,
        "periodo_str": _periodo_muestra(n_s, f_min, f_max),
        "horizonte_label": hz_label,
        "etiqueta_muestra": formatear_etiqueta_muestra(n_s, n_f, f_min, f_max, hz_label),
    }


def _pct_c09(num: int, den: int) -> Optional[float]:
    """Porcentaje redondeado a 1 decimal o None si den <= 0."""
    return round(100.0 * num / den, 1) if den > 0 else None


def calcular_inferencia_por_fecha(
    filas_o_tasas_por_fecha,
    referencia_pct: float = 50.0,
    dias_horizonte: int = 1,
    min_fechas_inferencia: int = MIN_FECHAS_INFERENCIA,
) -> Dict:
    """
    Inferencia honesta agregada por bloques de fecha de emision (C-13).
    Agrupa siempre primero por fecha_prediccion para evitar tratar senales
    del mismo dia como independientes.

    Parametros:
      filas_o_tasas_por_fecha: lista de dicts (con 'fecha', 'n', 'aciertos'/'k'),
        o dict {fecha: {'n': ..., 'aciertos': ...}} / {fecha: (k, n)},
        o lista de tuplas/filas (fecha, acierto) o (fecha, hz, senal, var, acierto).
      referencia_pct: tasa de referencia para el contraste (p. ej. 50.0% para azar,
        o % siempre lateral para 3 clases).
      dias_horizonte: dias de horizonte (1 para 1D, 3 para 3D, 5 para 1S, 20 para 1M)
        para la correccion de solapamiento Newey-West / Bartlett.
      min_fechas_inferencia: minimo de fechas para considerar potencia estadistica
        suficiente (por defecto MIN_FECHAS_INFERENCIA = 30).
    """
    fechas_dict = {}
    if isinstance(filas_o_tasas_por_fecha, dict):
        for k, v in filas_o_tasas_por_fecha.items():
            f_str = str(k)
            if isinstance(v, dict):
                n_val = int(v.get("n", v.get("total", 0)))
                ac_val = int(v.get("aciertos", v.get("k", 0)))
            elif isinstance(v, (list, tuple)) and len(v) >= 2:
                v0, v1 = int(v[0]), int(v[1])
                if v0 <= v1:
                    ac_val, n_val = v0, v1
                else:
                    n_val, ac_val = v0, v1
            else:
                continue
            if n_val > 0:
                fechas_dict[f_str] = {"fecha": f_str, "n": n_val, "aciertos": ac_val}
    elif isinstance(filas_o_tasas_por_fecha, (list, tuple)):
        for item in filas_o_tasas_por_fecha:
            if isinstance(item, dict):
                f_str = str(item.get("fecha") or item.get("fecha_prediccion") or "")
                n_val = int(item.get("n", item.get("total", 0)))
                ac_val = int(item.get("aciertos", item.get("k", 0)))
                if f_str and n_val > 0:
                    if f_str not in fechas_dict:
                        fechas_dict[f_str] = {"fecha": f_str, "n": 0, "aciertos": 0}
                    fechas_dict[f_str]["n"] += n_val
                    fechas_dict[f_str]["aciertos"] += ac_val
            elif isinstance(item, (list, tuple)):
                if len(item) == 2:
                    f_str, ac_val = str(item[0]), int(item[1])
                    if f_str not in fechas_dict:
                        fechas_dict[f_str] = {"fecha": f_str, "n": 0, "aciertos": 0}
                    fechas_dict[f_str]["n"] += 1
                    fechas_dict[f_str]["aciertos"] += (1 if ac_val == 1 else 0)
                elif len(item) >= 5:
                    f_str, ac_val = str(item[0]), int(item[4])
                    if f_str not in fechas_dict:
                        fechas_dict[f_str] = {"fecha": f_str, "n": 0, "aciertos": 0}
                    fechas_dict[f_str]["n"] += 1
                    fechas_dict[f_str]["aciertos"] += (1 if ac_val == 1 else 0)

    fechas_ordenadas = sorted(
        [v for v in fechas_dict.values() if v["n"] > 0],
        key=lambda x: str(x["fecha"])
    )
    for d in fechas_ordenadas:
        d["tasa"] = round((d["aciertos"] / d["n"]) * 100.0, 4)

    T = len(fechas_ordenadas)
    N_total = sum(d["n"] for d in fechas_ordenadas)
    K_total = sum(d["aciertos"] for d in fechas_ordenadas)

    if T == 0 or N_total == 0:
        return {
            "n_fechas": 0,
            "n_total": 0,
            "tasa_global_pct": None,
            "tasa_media_fechas_pct": None,
            "se_cluster_pct": None,
            "se_iid_ingenuo_pct": None,
            "design_effect": None,
            "n_efectivo": 0.0,
            "ic95_inf": None,
            "ic95_sup": None,
            "ic95_semiancho_pct": None,
            "t_stat": None,
            "p_valor_aprox": None,
            "referencia_pct": float(referencia_pct),
            "muestra_suficiente": False,
            "estado_muestra": "sin_muestra",
            "aviso_muestra": "Sin muestra (0 fechas)",
            "significativo_95": False,
            "dias_horizonte": int(dias_horizonte),
            "tasas_por_fecha": [],
        }

    p_hat = (K_total / N_total) * 100.0
    tasas = np.array([d["tasa"] for d in fechas_ordenadas], dtype=float)
    p_bar = float(np.mean(tasas))
    se_iid = math.sqrt(p_hat * (100.0 - p_hat) / N_total) if N_total > 0 and 0.0 <= p_hat <= 100.0 else 0.0

    if T < MIN_FECHAS_IC_BASICO:
        n_efectivo = 1.0
        return {
            "n_fechas": T,
            "n_total": N_total,
            "tasa_global_pct": round(p_hat, 2),
            "tasa_media_fechas_pct": round(p_bar, 2),
            "se_cluster_pct": None,
            "se_iid_ingenuo_pct": round(se_iid, 4),
            "design_effect": None,
            "n_efectivo": n_efectivo,
            "ic95_inf": None,
            "ic95_sup": None,
            "ic95_semiancho_pct": None,
            "t_stat": None,
            "p_valor_aprox": None,
            "referencia_pct": float(referencia_pct),
            "muestra_suficiente": False,
            "estado_muestra": "muestra_insuficiente",
            "aviso_muestra": f"Muestra insuficiente ({T}/{min_fechas_inferencia} fechas de emision; N_ef={n_efectivo:.1f})",
            "significativo_95": False,
            "dias_horizonte": int(dias_horizonte),
            "tasas_por_fecha": fechas_ordenadas,
        }

    w = np.array([d["n"] / N_total for d in fechas_ordenadas], dtype=float)
    sum_w2 = float(np.sum(w**2))
    if 1.0 - sum_w2 > 1e-12:
        s_p2_weighted = float(np.sum(w * (tasas - p_hat)**2) / (1.0 - sum_w2))
    else:
        s_p2_weighted = float(np.sum((tasas - p_bar)**2) / (T - 1))
    s_p2_unweighted = float(np.sum((tasas - p_bar)**2) / (T - 1))
    var_base = s_p2_unweighted / T

    dias_h = max(int(dias_horizonte), 1)
    L = min(dias_h - 1, T - 2) if T >= 3 else 0
    var_hac = var_base
    if L > 0:
        u = tasas - p_bar
        gamma_0 = float(np.sum(u**2) / T)
        gamma_sum = 0.0
        for j in range(1, L + 1):
            w_j = 1.0 - (j / (L + 1.0))
            cov_j = float(np.sum(u[:-j] * u[j:]) / T)
            gamma_sum += 2.0 * w_j * cov_j
        omega = gamma_0 + gamma_sum
        var_hac = max(0.0, (T / (T - 1.0)) * (omega / T))

    var_cluster = max(var_base, var_hac)
    se_cluster = math.sqrt(var_cluster)

    if se_cluster > 1e-12:
        deff = (se_cluster**2) / (se_iid**2) if se_iid > 1e-12 else 1.0
        n_ef_raw = (p_hat * (100.0 - p_hat)) / (se_cluster**2) if (0.0 < p_hat < 100.0) else 1.0
        n_efectivo = min(float(N_total), max(1.0, float(n_ef_raw)))
    else:
        deff = 1.0
        n_efectivo = float(min(N_total, T))

    df = T - 1
    try:
        import scipy.stats as stats
        t_crit = float(stats.t.ppf(0.975, df=df))
    except Exception:
        t_crit = 1.96 if df >= 30 else 2.262

    semiancho = t_crit * se_cluster
    ic95_inf = max(0.0, p_hat - semiancho)
    ic95_sup = min(100.0, p_hat + semiancho)

    if se_cluster > 1e-12:
        t_stat = (p_hat - float(referencia_pct)) / se_cluster
        try:
            import scipy.stats as stats
            p_valor = float(stats.t.sf(abs(t_stat), df=df) * 2.0)
        except Exception:
            p_valor = None
    else:
        if abs(p_hat - float(referencia_pct)) > 1e-6:
            t_stat = float("inf") if p_hat > float(referencia_pct) else float("-inf")
            p_valor = 0.0
        else:
            t_stat = 0.0
            p_valor = 1.0

    if T < min_fechas_inferencia:
        muestra_suficiente = False
        estado_muestra = "muestra_insuficiente"
        aviso_muestra = f"Muestra insuficiente ({T}/{min_fechas_inferencia} fechas de emision; N_ef={n_efectivo:.1f})"
        significativo_95 = False
    else:
        muestra_suficiente = True
        estado_muestra = "muestra_suficiente"
        aviso_muestra = f"Muestra de {T} fechas (N_ef={n_efectivo:.1f})"
        significativo_95 = bool(abs(t_stat) >= t_crit)

    return {
        "n_fechas": T,
        "n_total": N_total,
        "tasa_global_pct": round(p_hat, 2),
        "tasa_media_fechas_pct": round(p_bar, 2),
        "se_cluster_pct": round(se_cluster, 4),
        "se_iid_ingenuo_pct": round(se_iid, 4),
        "design_effect": round(deff, 3) if deff is not None else None,
        "n_efectivo": round(n_efectivo, 1),
        "ic95_inf": round(ic95_inf, 2),
        "ic95_sup": round(ic95_sup, 2),
        "ic95_semiancho_pct": round(semiancho, 2),
        "t_stat": round(t_stat, 3) if (t_stat is not None and not math.isinf(t_stat)) else t_stat,
        "p_valor_aprox": round(p_valor, 4) if p_valor is not None else None,
        "referencia_pct": float(referencia_pct),
        "muestra_suficiente": muestra_suficiente,
        "estado_muestra": estado_muestra,
        "aviso_muestra": aviso_muestra,
        "significativo_95": significativo_95,
        "dias_horizonte": int(dias_horizonte),
        "tasas_por_fecha": fechas_ordenadas,
    }


def _calcular_bloque_acierto_c09(rows: list, horizonte: Optional[str] = None) -> Dict:
    """
    Calcula, sobre la lista COMPLETA de observaciones resueltas (sin LIMIT), las dos
    metricas separadas de C-09 y sus metadatos de muestra. Cada fila es una tupla:
      (fecha_prediccion, horizonte, senal, variacion_pct, acierto[, clase_real])
    Si clase_real viene informada (tabla evaluaciones) se usa para la distribucion real
    y para la referencia 'siempre lateral'; si no, la clase real se deriva con los
    umbrales fijos v1 por horizonte (abs(variacion) <= UMBRALES_LATERAL[hz] -> LATERAL).
    """
    n = len(rows)
    fechas = sorted({str(r[0])[:10] for r in rows if r[0]})
    meta = _metadatos_muestra(
        n, len(fechas), fechas[0] if fechas else None, fechas[-1] if fechas else None, horizonte
    )

    aciertos_3c = 0
    n_alc_em = n_lat_em = n_baj_em = 0
    ok_alc = ok_lat = ok_baj = 0
    rets_alc: list = []
    rets_baj: list = []
    n_real = n_alc_real = n_lat_real = n_baj_real = 0
    n_subidas = 0
    n_dir = 0
    ok_signo = 0
    fechas_3c: Dict[str, Dict] = {}
    fechas_signo: Dict[str, Dict] = {}

    for r in rows:
        hz_r = str(r[1] or "1D").upper().strip()
        senal_r = str(r[2] or "").upper().strip()
        var_f = _f(r[3])
        ac_i = 1 if (r[4] is not None and int(r[4]) == 1) else 0
        clase_r = str(r[5]).upper().strip() if (len(r) > 5 and r[5] is not None) else None
        aciertos_3c += ac_i
        fp_r = str(r[0])[:10] if r[0] else ""
        if fp_r:
            if fp_r not in fechas_3c:
                fechas_3c[fp_r] = {"fecha": fp_r, "n": 0, "aciertos": 0}
            fechas_3c[fp_r]["n"] += 1
            if ac_i == 1:
                fechas_3c[fp_r]["aciertos"] += 1

        if senal_r == "ALCISTA":
            n_alc_em += 1
            ok_alc += ac_i
            if var_f is not None:
                rets_alc.append(var_f)
        elif senal_r == "BAJISTA":
            n_baj_em += 1
            ok_baj += ac_i
            if var_f is not None:
                rets_baj.append(-var_f)
        else:
            n_lat_em += 1
            ok_lat += ac_i

        if senal_r in ("ALCISTA", "BAJISTA"):
            n_dir += 1
            if var_f is not None and (
                (senal_r == "ALCISTA" and var_f > 0.0) or (senal_r == "BAJISTA" and var_f < 0.0)
            ):
                ok_signo += 1
                ac_sig_i = 1
            else:
                ac_sig_i = 0
            if fp_r:
                if fp_r not in fechas_signo:
                    fechas_signo[fp_r] = {"fecha": fp_r, "n": 0, "aciertos": 0}
                fechas_signo[fp_r]["n"] += 1
                fechas_signo[fp_r]["aciertos"] += ac_sig_i

        if clase_r is None and var_f is not None:
            clase_r = _clasificar_variacion_c10(var_f, float(UMBRALES_LATERAL.get(hz_r, 0.8)))
        if clase_r is not None:
            n_real += 1
            if clase_r == "ALCISTA":
                n_alc_real += 1
            elif clase_r == "BAJISTA":
                n_baj_real += 1
            else:
                n_lat_real += 1
        if var_f is not None and var_f > 0.0:
            n_subidas += 1

    n_var_valid = sum(1 for r in rows if _f(r[3]) is not None)

    bloque = dict(meta)
    bloque.update(
        {
            "n": n,
            "aciertos": aciertos_3c,
            "tasa": _pct_c09(aciertos_3c, n),
            "tasa_acierto_3clases": _pct_c09(aciertos_3c, n),
            "ref_siempre_lateral_pct": _pct_c09(n_lat_real, n_real),
            "dist_clases_reales": {
                "n_alcista_real": n_alc_real,
                "n_lateral_real": n_lat_real,
                "n_bajista_real": n_baj_real,
                "pct_alcista_real": _pct_c09(n_alc_real, n_real),
                "pct_lateral_real": _pct_c09(n_lat_real, n_real),
                "pct_bajista_real": _pct_c09(n_baj_real, n_real),
            },
            "dist_senales_emitidas": {
                "n_alcista": n_alc_em,
                "n_lateral": n_lat_em,
                "n_bajista": n_baj_em,
            },
            "tasa_alcista_3c": _pct_c09(ok_alc, n_alc_em),
            "tasa_bajista_3c": _pct_c09(ok_baj, n_baj_em),
            "tasa_lateral_3c": _pct_c09(ok_lat, n_lat_em),
            "ret_alcista_medio": round(float(np.mean(rets_alc)), 2) if rets_alc else None,
            "ret_bajista_medio": round(float(np.mean(rets_baj)), 2) if rets_baj else None,
            "n_direccionales": n_dir,
            "aciertos_signo": ok_signo,
            "tasa_acierto_signo": _pct_c09(ok_signo, n_dir),
            "ref_azar_signo_pct": REF_AZAR_SIGNO_PCT,
            "ref_frecuencia_subida_pct": _pct_c09(n_subidas, n_var_valid),
        }
    )
    hz_norm = _normalizar_hz_label(horizonte)
    dias_hz = max(int(_HORIZONTES_DEFAULT.get(hz_norm, 1)), 1)
    ref_3c_val = bloque.get("ref_siempre_lateral_pct")
    ref_3c_num = float(ref_3c_val) if ref_3c_val is not None else 33.33

    inf_3c = calcular_inferencia_por_fecha(
        list(fechas_3c.values()),
        referencia_pct=ref_3c_num,
        dias_horizonte=dias_hz,
    )

    inf_signo = calcular_inferencia_por_fecha(
        list(fechas_signo.values()),
        referencia_pct=REF_AZAR_SIGNO_PCT,
        dias_horizonte=dias_hz,
    )

    ref_sub = bloque.get("ref_frecuencia_subida_pct")
    if ref_sub is not None:
        inf_s_sub = calcular_inferencia_por_fecha(
            list(fechas_signo.values()),
            referencia_pct=float(ref_sub),
            dias_horizonte=dias_hz,
        )
        inf_signo["referencia_subidas_pct"] = float(ref_sub)
        inf_signo["t_stat_vs_subidas"] = inf_s_sub.get("t_stat")
        inf_signo["p_valor_vs_subidas"] = inf_s_sub.get("p_valor_aprox")
        inf_signo["significativo_95_vs_subidas"] = inf_s_sub.get("significativo_95")
    else:
        inf_signo["referencia_subidas_pct"] = None
        inf_signo["t_stat_vs_subidas"] = None
        inf_signo["p_valor_vs_subidas"] = None
        inf_signo["significativo_95_vs_subidas"] = False

    bloque["inferencia_3clases"] = inf_3c
    bloque["inferencia_signo"] = inf_signo
    return bloque


def _bloques_global_y_por_horizonte_c09(rows: list, horizonte: Optional[str]) -> tuple:
    """
    Reparte las filas (fecha, hz, senal, var, acierto[, clase_real]) en el bloque global
    (filtrado por horizonte si se indica) y en los 4 bloques por horizonte (1D, 3D, 1S, 1M).
    """
    hz_label = _normalizar_hz_label(horizonte)
    por_hz_rows: Dict[str, list] = {h: [] for h in HORIZONTES_C09}
    sel: list = []
    for r in rows:
        h_r = str(r[1] or "").strip()
        por_hz_rows.setdefault(h_r, []).append(r)
        if hz_label == "Todos" or h_r == hz_label:
            sel.append(r)
    glob = _calcular_bloque_acierto_c09(sel, hz_label)
    por_hz = {h: _calcular_bloque_acierto_c09(por_hz_rows.get(h, []), h) for h in HORIZONTES_C09}
    for h_k, lst in por_hz_rows.items():
        if h_k and h_k not in por_hz and lst:
            por_hz[h_k] = _calcular_bloque_acierto_c09(lst, h_k)
    return glob, por_hz


def _filas_muestra_evaluacion_c09(
    conn: sqlite3.Connection,
    regla_metrica: str,
    reglas_requeridas: tuple = (),
    incluir_legacy: bool = False,
) -> list:
    """
    Lee en solo lectura (SELECT) todas las observaciones resueltas evaluadas bajo
    regla_metrica, exigiendo ademas (INNER JOIN) las reglas_requeridas para reproducir
    exactamente la misma poblacion que el resumen C-10/C-11/C-12 correspondiente.
    Devuelve tuplas (fecha_prediccion, horizonte, senal, variacion_pct, acierto, clase_real).
    """
    joins = ["INNER JOIN evaluaciones e0 ON e0.prediccion_id = p.id AND e0.regla = ?"]
    params: list = [regla_metrica]
    for i, regla in enumerate(reglas_requeridas, start=1):
        joins.append(
            f"INNER JOIN evaluaciones e{i} ON e{i}.prediccion_id = p.id AND e{i}.regla = ?"
        )
        params.append(regla)
    where = ["p.resuelta = 1"]
    if not incluir_legacy:
        where.append("p.modelo != 'legacy-pre-freeze'")
    sql = (
        "SELECT p.fecha_prediccion, p.horizonte, p.senal, e0.variacion_pct, e0.acierto, e0.clase_real "
        "FROM predicciones p " + " ".join(joins) + " WHERE " + " AND ".join(where)
        + " ORDER BY p.id ASC"
    )
    return conn.execute(sql, tuple(params)).fetchall()


def _fusionar_claves_c09(destino: Dict, bloque: Dict) -> None:
    """Anade a destino las claves de muestra y de metricas C-09 sin tocar las existentes."""
    for k in CLAVES_MUESTRA_C09 + CLAVES_METRICAS_C09 + CLAVES_INFERENCIA_C13:
        destino[k] = bloque.get(k)


def _enriquecer_resumen_c09(
    res: Dict,
    conn: sqlite3.Connection,
    regla_metrica: str,
    reglas_requeridas: tuple,
    horizonte: Optional[str],
    incluir_legacy: bool,
) -> Dict:
    """
    Enriquece de forma retrocompatible un resumen C-10/C-11/C-12 con las claves de
    muestra y de metricas C-09 (global y en cada entrada de por_horizonte).
    Devuelve el bloque global calculado.
    """
    try:
        tiene_eval = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
        ).fetchone()
        rows = (
            _filas_muestra_evaluacion_c09(conn, regla_metrica, reglas_requeridas, incluir_legacy)
            if tiene_eval
            else []
        )
    except Exception:
        rows = []
    glob, por_hz_bloques = _bloques_global_y_por_horizonte_c09(rows, horizonte)
    _fusionar_claves_c09(res, glob)
    res["regla_metrica_c09"] = regla_metrica
    por_hz = res.get("por_horizonte")
    if not isinstance(por_hz, dict):
        por_hz = {}
    for h_k, bloque_h in por_hz_bloques.items():
        entrada = por_hz.get(h_k)
        if not isinstance(entrada, dict):
            entrada = {}
        _fusionar_claves_c09(entrada, bloque_h)
        por_hz[h_k] = entrada
    res["por_horizonte"] = por_hz
    return glob


def obtener_metricas(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """
    Metricas agregadas del tracking para un horizonte especifico o global:
      total, resueltas, pendientes (total filas en seguimiento), vigentes (pares unicos vivos),
      tasa_acierto (%), score_medio (0-100), error_medio (p.p.), invalidadas,
      legacy_resueltas (conteo de senales anteriores al freeze),
      por_horizonte: {hz: {n, aciertos, tasa, ...}}
    C-09 (claves anadidas, calculadas sobre el total SQL de resueltas, nunca truncado):
      - Muestra: n_senales, n_fechas, fecha_min, fecha_max, periodo_str, horizonte_label,
        etiqueta_muestra.
      - Acierto en 3 clases: tasa_acierto_3clases (== tasa_acierto), ref_siempre_lateral_pct,
        dist_clases_reales, dist_senales_emitidas, tasa_alcista_3c, tasa_bajista_3c,
        tasa_lateral_3c.
      - Acierto de signo: n_direccionales, aciertos_signo, tasa_acierto_signo,
        ref_azar_signo_pct (50.0), ref_frecuencia_subida_pct.
    Si se pasa conn (p. ej. en solo lectura mode=ro), no se cierra al terminar.
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    hz_label = _normalizar_hz_label(horizonte)
    filtra_hz = hz_label != "Todos"
    try:
        where_hz = "WHERE horizonte = ?" if filtra_hz else ""
        params_hz = (hz_label,) if filtra_hz else ()
        and_hz = "AND horizonte = ?" if filtra_hz else ""

        total = conn.execute(f"SELECT COUNT(*) FROM predicciones {where_hz}", params_hz).fetchone()[0]

        # Resueltas principales (excluye legacy por defecto)
        filtro_leg = "" if incluir_legacy else "AND modelo != 'legacy-pre-freeze'"
        where_res = f"WHERE resuelta = 1 {filtro_leg} {and_hz}"
        res = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(acierto),0), "
            f"COALESCE(AVG(error_pct),0), COALESCE(AVG(score_calidad), 0) "
            f"FROM predicciones {where_res}",
            params_hz,
        ).fetchone()
        resueltas = int(res[0])
        aciertos = int(res[1])
        error_medio = float(res[2])
        score_medio_raw = float(res[3])

        # Conteo especifico de legacy para mostrar advertencia/badge en la UI
        where_leg = f"WHERE resuelta = 1 AND modelo = 'legacy-pre-freeze' {and_hz}"
        legacy_cnt = conn.execute(f"SELECT COUNT(*) FROM predicciones {where_leg}", params_hz).fetchone()[0]

        # Predicciones invalidadas anticipadamente
        where_inv = f"WHERE resuelta = 1 AND motivo_resolucion = 'invalidada' {filtro_leg} {and_hz}"
        invalidadas = conn.execute(
            f"SELECT COUNT(*) FROM predicciones {where_inv}",
            params_hz,
        ).fetchone()[0]

        # Predicciones cerradas sin cotizacion (resuelta = 2)
        where_sc = f"WHERE resuelta = {RESUELTA_SIN_COTIZACION} {filtro_leg} {and_hz}"
        sin_cotizacion = conn.execute(
            f"SELECT COUNT(*) FROM predicciones {where_sc}",
            params_hz,
        ).fetchone()[0]

        # Senales vivas no resueltas (conteo total de filas en seguimiento)
        where_pend = f"WHERE resuelta = 0 {and_hz}"
        pendientes_totales = conn.execute(
            f"SELECT COUNT(*) FROM predicciones {where_pend}",
            params_hz,
        ).fetchone()[0]

        # Senales vigentes activas (pares unicos ticker-horizonte en seguimiento)
        vigentes = conn.execute(
            f"SELECT COUNT(DISTINCT ticker || '-' || horizonte) FROM predicciones {where_pend}",
            params_hz,
        ).fetchone()[0]

        # C-09: todas las resueltas (sin LIMIT) para separar 3 clases / signo y muestra
        rows_res = conn.execute(
            f"SELECT fecha_prediccion, horizonte, senal, variacion_real, acierto "
            f"FROM predicciones WHERE resuelta = 1 {filtro_leg} ORDER BY id ASC"
        ).fetchall()
        glob, por_hz = _bloques_global_y_por_horizonte_c09(rows_res, hz_label)

        salida = {
            "total": resueltas + int(vigentes),
            "total_db": int(total),
            "resueltas": resueltas,
            "pendientes": int(vigentes),
            "pendientes_totales": int(pendientes_totales),
            "vigentes": int(vigentes),
            "tasa_acierto": round(100.0 * aciertos / resueltas, 1) if resueltas else None,
            "score_medio": round(score_medio_raw, 1) if resueltas else None,
            "error_medio": round(error_medio, 2) if resueltas else None,
            "invalidadas": int(invalidadas),
            "sin_cotizacion": int(sin_cotizacion),
            "legacy_resueltas": int(legacy_cnt),
            "por_horizonte": por_hz,
        }
        _fusionar_claves_c09(salida, glob)
        salida["tasa_acierto_3clases"] = salida["tasa_acierto"]
        try:
            salida["referencias_ingenuas_c14"] = obtener_tabla_referencias_ingenuas(
                horizonte=horizonte, incluir_legacy=incluir_legacy, conn=conn
            )
        except Exception:
            salida["referencias_ingenuas_c14"] = _tabla_referencias_ingenuas_vacia(hz_label)
        return salida
    except Exception:
        glob_v, por_hz_v = _bloques_global_y_por_horizonte_c09([], hz_label)
        salida = {"total": 0, "resueltas": 0, "pendientes": 0, "vigentes": 0,
                  "tasa_acierto": None, "score_medio": None,
                  "error_medio": None, "invalidadas": 0, "sin_cotizacion": 0,
                  "legacy_resueltas": 0, "por_horizonte": por_hz_v,
                  "referencias_ingenuas_c14": _tabla_referencias_ingenuas_vacia(hz_label)}
        _fusionar_claves_c09(salida, glob_v)
        return salida
    finally:
        if own_conn:
            conn.close()



def limpiar_predicciones(horizonte: Optional[str] = None, solo_resueltas: bool = True) -> int:
    """
    Archiva (soft-delete) registros de predicciones en vez de eliminarlos físicamente.
    Marca archivada = 1, preservando intacta la auditoría histórica e impidiendo
    pérdida accidental de datos desde la UI.

    - Si solo_resueltas=True: archiva únicamente las resueltas (resuelta = 1).
    - Si solo_resueltas=False: archiva todas las predicciones (resueltas y pendientes).
    - Si horizonte se especifica y != 'Todos': filtra por ese horizonte.
    Retorna el número de registros archivados.
    """
    if os.environ.get("STOCKLAB_DEV") != "1":
        raise PermissionError("Historial inmutable: operacion deshabilitada salvo STOCKLAB_DEV=1")
    conn = get_connection()
    try:
        where_clauses = ["COALESCE(archivada, 0) = 0"]
        params = []
        if solo_resueltas:
            where_clauses.append("resuelta = 1")
        if horizonte and horizonte != "Todos":
            where_clauses.append("horizonte = ?")
            params.append(horizonte)

        where_str = f"WHERE {' AND '.join(where_clauses)}"
        cur = conn.execute(f"UPDATE predicciones SET archivada = 1 {where_str}", tuple(params))
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    except Exception:
        return 0
    finally:
        conn.close()


def limpiar_resueltas(horizonte: Optional[str] = None) -> int:
    """Alias para archivar (soft-delete) únicamente predicciones resueltas."""
    if os.environ.get("STOCKLAB_DEV") != "1":
        raise PermissionError("Historial inmutable: operacion deshabilitada salvo STOCKLAB_DEV=1")
    return limpiar_predicciones(horizonte=horizonte, solo_resueltas=True)


def recalcular_predicciones(horizonte: Optional[str] = None) -> int:
    """
    Elimina las predicciones PENDIENTES (resuelta = 0) para forzar un
    recálculo con el modelo actual en la próxima carga de página.

    Flujo esperado:
      1. Se eliminan las pendientes del horizonte indicado (o todas).
      2. Al recargar la vista IA, ``registrar_predicciones(radar)`` inserta
         nuevas señales calculadas con el modelo vigente (v2-continuous).
      3. Las predicciones resueltas del historial NO se tocan.

    Retorna el número de predicciones pendientes eliminadas.
    """
    if os.environ.get("STOCKLAB_DEV") != "1":
        raise PermissionError("Historial inmutable: operacion deshabilitada salvo STOCKLAB_DEV=1")
    conn = get_connection()
    try:
        # FASE 1: Las predicciones con modelo 'v5-frozen' son inmutables y NUNCA se eliminan.
        # Solo se permite purgar pendientes de modelos legacy si existieran.
        where_clauses = ["resuelta = 0", "modelo != 'v5-frozen'"]
        params: list = []
        if horizonte and horizonte != "Todos":
            where_clauses.append("horizonte = ?")
            params.append(horizonte)

        where_str = f"WHERE {' AND '.join(where_clauses)}"
        cur = conn.execute(f"DELETE FROM predicciones {where_str}", tuple(params))
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    except Exception:
        return 0
    finally:
        conn.close()


def obtener_calibracion_historica(ticker: Optional[str] = None,
                                  horizonte: Optional[str] = None) -> Dict:
    """
    Consulta la tasa de acierto empírica para calibrar el motor de predicciones (Feedback Loop).

    FASE 1 (Higiene):
      El feedback loop heurístico está DESACTIVADO (FEEDBACK_ACTIVO = False).
      Con n < 30 y mezcla de clases con volatilidad asimétrica, el bucle
      solo amplificaba ruido. Devuelve factor_confianza = 1.0 y ajuste_consenso = 0.
    """
    if not FEEDBACK_ACTIVO:
        if ticker and horizonte:
            return {
                "muestras": 0,
                "tasa_acierto": None,
                "factor_confianza": 1.0,
                "ajuste_consenso": 0,
            }
        else:
            return {}

    conn = get_connection()
    try:
        if ticker and horizonte:
            row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(acierto), 0)
                FROM predicciones
                WHERE resuelta = 1 AND modelo != 'legacy-pre-freeze' AND ticker = ? AND horizonte = ?
                """,
                (ticker, horizonte),
            ).fetchone()
            n = int(row[0]) if row else 0
            ok = int(row[1]) if row else 0
            tasa = round(100.0 * ok / n, 1) if n >= 30 else None

            factor_conf = 1.0
            ajuste_con = 0
            return {
                "muestras": n,
                "tasa_acierto": tasa,
                "factor_confianza": factor_conf,
                "ajuste_consenso": ajuste_con,
            }
        else:
            return {}
    except Exception:
        return {} if not (ticker and horizonte) else {
            "muestras": 0, "tasa_acierto": None, "factor_confianza": 1.0, "ajuste_consenso": 0
        }
    finally:
        conn.close()


def _f(v) -> Optional[float]:
    """Conversión segura a float (None si no es finito)."""
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None



def _extraer_serie_raw_ticker(df_raw_ohlcv, ticker: str) -> Optional[pd.DataFrame]:
    """
    Extrae el DataFrame de barras de un ticker desde un lote descargado con auto_adjust=False
    (DataFrame MultiIndex por ticker o por metrica, DataFrame simple o diccionario).
    """
    if df_raw_ohlcv is None:
        return None
    t_norm = (ticker or "").strip().upper()
    if isinstance(df_raw_ohlcv, dict):
        sub = df_raw_ohlcv.get(ticker)
        if sub is None:
            sub = df_raw_ohlcv.get(t_norm)
        if isinstance(sub, pd.DataFrame) and not sub.empty:
            return sub
        return None

    if not isinstance(df_raw_ohlcv, pd.DataFrame) or df_raw_ohlcv.empty:
        return None

    try:
        if isinstance(df_raw_ohlcv.columns, pd.MultiIndex):
            lvl0 = [str(x).strip().upper() for x in df_raw_ohlcv.columns.get_level_values(0)]
            lvl_last = [str(x).strip().upper() for x in df_raw_ohlcv.columns.get_level_values(-1)]
            if ticker in df_raw_ohlcv.columns.get_level_values(0):
                sub = df_raw_ohlcv[ticker]
            elif t_norm in lvl0:
                orig_k = df_raw_ohlcv.columns.get_level_values(0)[lvl0.index(t_norm)]
                sub = df_raw_ohlcv[orig_k]
            elif ticker in df_raw_ohlcv.columns.get_level_values(-1):
                sub = df_raw_ohlcv.xs(ticker, level=-1, axis=1)
            elif t_norm in lvl_last:
                orig_k = df_raw_ohlcv.columns.get_level_values(-1)[lvl_last.index(t_norm)]
                sub = df_raw_ohlcv.xs(orig_k, level=-1, axis=1)
            else:
                return None
            if isinstance(sub, pd.DataFrame) and not sub.empty:
                return sub
            return None
        else:
            if "Close" in df_raw_ohlcv.columns:
                return df_raw_ohlcv
            return None
    except Exception:
        return None


def _extraer_precio_en_fecha(
    sub: pd.DataFrame, col_name: str, fecha_iso: str
) -> Optional[float]:
    """
    Extrae el valor finito y > 0 de col_name en la barra con fecha exacta fecha_iso.
    """
    if sub is None or sub.empty or col_name not in sub.columns:
        return None
    serie = pd.to_numeric(sub[col_name], errors="coerce").dropna()
    if serie.empty:
        return None
    dt_idx = pd.to_datetime(serie.index)
    if getattr(dt_idx, "tz", None) is not None:
        dt_idx = dt_idx.tz_convert("Europe/Madrid")
    idx_str = dt_idx.strftime("%Y-%m-%d")
    matching = serie[idx_str == fecha_iso]
    if matching.empty:
        return None
    val = float(matching.iloc[-1])
    if np.isfinite(val) and val > 0.0:
        return val
    return None


def _clasificar_variacion_c10(variacion_pct: float, umbral: float) -> str:
    """Clasifica la variacion porcentual segun el umbral lateral simetrico."""
    if variacion_pct > umbral:
        return "ALCISTA"
    if variacion_pct < -umbral:
        return "BAJISTA"
    return "LATERAL"


def _calcular_acierto_signo_c10(senal: str, variacion_pct: float) -> Optional[int]:
    """
    Calcula acierto_signo para evaluaciones C-10:
      - 1 si (senal == 'ALCISTA' and variacion_pct > 0) or (senal == 'BAJISTA' and variacion_pct < 0)
      - 0 si senal in ('ALCISTA', 'BAJISTA') y no acierta el signo
      - None (NULL en SQLite) si senal == 'LATERAL'
    """
    s = (senal or "").upper().strip()
    if s == "ALCISTA":
        return 1 if variacion_pct > 0.0 else 0
    if s == "BAJISTA":
        return 1 if variacion_pct < 0.0 else 0
    return None


def _determinar_fecha_entrada_operable(
    sub: pd.DataFrame, fecha_prediccion_iso: str, fecha_objetivo_iso: str
) -> Optional[str]:
    """
    Determina la fecha de entrada operable (S+1): la primera fecha disponible
    del ticker en la serie con fecha > fecha_prediccion y fecha <= fecha_objetivo
    que tenga Open > 0 y Close > 0 (preferiblemente la primera sesion habil BME
    posterior a fecha_prediccion).
    """
    if sub is None or sub.empty or "Close" not in sub.columns:
        return None

    try:
        d_pred = date.fromisoformat(fecha_prediccion_iso[:10])
        s1_bme = fecha_habil_bme(1, desde=d_pred)
    except Exception:
        s1_bme = ""

    if s1_bme and fecha_prediccion_iso < s1_bme <= fecha_objetivo_iso:
        c_s1 = _extraer_precio_en_fecha(sub, "Close", s1_bme)
        o_s1 = (
            _extraer_precio_en_fecha(sub, "Open", s1_bme)
            if "Open" in sub.columns
            else c_s1
        )
        if o_s1 is not None and c_s1 is not None and o_s1 > 0.0 and c_s1 > 0.0:
            return s1_bme

    dt_idx = pd.to_datetime(sub.index)
    if getattr(dt_idx, "tz", None) is not None:
        dt_idx = dt_idx.tz_convert("Europe/Madrid")
    fechas_ordenadas = sorted(set(dt_idx.strftime("%Y-%m-%d")))
    for f_cand in fechas_ordenadas:
        if fecha_prediccion_iso < f_cand <= fecha_objetivo_iso:
            c_val = _extraer_precio_en_fecha(sub, "Close", f_cand)
            o_val = (
                _extraer_precio_en_fecha(sub, "Open", f_cand)
                if "Open" in sub.columns
                else c_val
            )
            if o_val is not None and c_val is not None and o_val > 0.0 and c_val > 0.0:
                return f_cand
    return None


def _cargar_cache_ohlcv_local() -> Optional[pd.DataFrame]:
    """
    Carga en solo lectura el cache local OHLCV (data/backtest_cache_6y.parquet)
    si existe en disco, para calcular la volatilidad de retornos de cierre hasta
    fecha_prediccion en backfills sin requerir red.
    """
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel_p in ("data/backtest_cache_6y.parquet", "data/backtest_cache_2y.parquet"):
            full_p = os.path.join(base_dir, rel_p)
            if os.path.exists(full_p):
                return pd.read_parquet(full_p)
    except Exception:
        return None
    return None


def evaluar_predicciones_c10_c11(
    df_raw_ohlcv: Optional[pd.DataFrame] = None,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
    incluir_c11: bool = True,
    incluir_c12: bool = False,
    recalcular_existentes: bool = False,
) -> Dict:
    """
    Evalua en paralelo las predicciones resueltas (resuelta = 1) bajo las reglas v1:
      - 'c10_precio_cierre' (Close[fecha_objetivo] / Close[fecha_prediccion] - 1)
      - 'c10_retorno_total' (AdjClose[fecha_objetivo] / AdjClose[fecha_prediccion] - 1)
      - 'c11_apertura_precio' (Close[fecha_objetivo] / Open[S+1] - 1)
      - 'c11_apertura_retorno_total' (AdjClose[fecha_objetivo] / Open_Adj[S+1] - 1)
    y cuando incluir_c12=True registra tambien en paralelo las 3 reglas v2 (C-12,
    umbral escalado por volatilidad k * sigma * sqrt(dias)):
      - 'c12_vol_precio_cierre'
      - 'c12_vol_retorno_total'
      - 'c12_vol_apertura_precio'
    Cuando recalcular_existentes=True, actualiza (INSERT OR REPLACE) unicamente
    las 3 reglas 'c12_vol_*' en evaluaciones sin tocar predicciones ni las reglas c10_*/c11_*.
    Nunca modifica la tabla predicciones.
    """
    own_conn = False
    if conn is None:
        own_conn = True
        if db_path is not None:
            conn = sqlite3.connect(db_path, timeout=30)
        else:
            conn = get_connection()

    orig_rf = conn.row_factory
    try:
        conn.row_factory = None
        init_evaluaciones_table(conn=conn)

        ahora_madrid = get_ahora_madrid()
        if getattr(ahora_madrid, "tzinfo", None) is not None:
            ahora_utc = ahora_madrid.astimezone(timezone.utc)
        else:
            ahora_utc = ahora_madrid.replace(tzinfo=timezone.utc)
        evaluada_en_iso = ahora_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        evaluadas_precio = 0
        evaluadas_total = 0
        evaluadas_apertura_precio = 0
        evaluadas_apertura_total = 0
        evaluadas_vol_precio_cierre = 0
        evaluadas_vol_retorno_total = 0
        evaluadas_vol_apertura_precio = 0
        siguen_pendientes = 0

        sql_insert = """
        INSERT INTO evaluaciones (
            prediccion_id, regla, precio_entrada_eval, precio_salida_eval,
            variacion_pct, umbral_aplicado, clase_real, acierto,
            acierto_signo, error_pct, diferencia_dividendo_pct, evaluada_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(prediccion_id, regla) DO NOTHING
        """

        sql_insert_c12 = (
            """
            INSERT OR REPLACE INTO evaluaciones (
                prediccion_id, regla, precio_entrada_eval, precio_salida_eval,
                variacion_pct, umbral_aplicado, clase_real, acierto,
                acierto_signo, error_pct, diferencia_dividendo_pct, evaluada_en
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            if recalcular_existentes
            else sql_insert
        )

        # Paso 1 (ruta rapida sin red): si incluir_c12=True y las filas v1 base
        # (c10_precio_cierre, c10_retorno_total, c11_apertura_precio) ya existen en
        # evaluaciones, reutilizar sus extremos y variaciones para insertar/recalcular C-12.
        if incluir_c12:
            cond_c12_backfill = (
                "1 = 1"
                if recalcular_existentes
                else "(ev2p.id IS NULL OR ev2t.id IS NULL OR ev2a.id IS NULL)"
            )
            rows_backfill_c12 = conn.execute(
                f"""
                SELECT
                    p.id, p.ticker, p.fecha_prediccion, p.horizonte, p.dias, p.senal, p.pct_estimado, p.sigma,
                    ep.precio_entrada_eval, ep.precio_salida_eval, ep.variacion_pct, ep.diferencia_dividendo_pct,
                    et.precio_entrada_eval, et.precio_salida_eval, et.variacion_pct, et.diferencia_dividendo_pct,
                    ea.precio_entrada_eval, ea.precio_salida_eval, ea.variacion_pct, ea.diferencia_dividendo_pct
                FROM predicciones p
                INNER JOIN evaluaciones ep
                  ON ep.prediccion_id = p.id AND ep.regla = ?
                INNER JOIN evaluaciones et
                  ON et.prediccion_id = p.id AND et.regla = ?
                INNER JOIN evaluaciones ea
                  ON ea.prediccion_id = p.id AND ea.regla = ?
                LEFT JOIN evaluaciones ev2p
                  ON ev2p.prediccion_id = p.id AND ev2p.regla = ?
                LEFT JOIN evaluaciones ev2t
                  ON ev2t.prediccion_id = p.id AND ev2t.regla = ?
                LEFT JOIN evaluaciones ev2a
                  ON ev2a.prediccion_id = p.id AND ev2a.regla = ?
                WHERE p.resuelta = 1
                  AND {cond_c12_backfill}
                ORDER BY p.id ASC
                """,
                (
                    REGLA_C10_PRECIO_CIERRE,
                    REGLA_C10_RETORNO_TOTAL,
                    REGLA_C11_APERTURA_PRECIO,
                    REGLA_C12_VOL_PRECIO_CIERRE,
                    REGLA_C12_VOL_RETORNO_TOTAL,
                    REGLA_C12_VOL_APERTURA_PRECIO,
                ),
            ).fetchall()

            fuente_backfill = df_raw_ohlcv
            if fuente_backfill is None and rows_backfill_c12:
                fuente_backfill = _cargar_cache_ohlcv_local()

            cache_sub_backfill: Dict[str, Optional[pd.DataFrame]] = {}
            cache_sigma_backfill: Dict[tuple, Optional[float]] = {}

            for (
                pid_b,
                tk_b,
                f_pred_b,
                hz_b,
                dias_b,
                senal_b,
                pct_est_b,
                sigma_b,
                ep_in,
                ep_out,
                var_precio_b,
                dif_p_b,
                et_in,
                et_out,
                var_total_b,
                dif_t_b,
                ea_in,
                ea_out,
                var_ap_b,
                dif_ap_b,
            ) in rows_backfill_c12:
                hz_norm_b = (hz_b or "1D").upper().strip()
                senal_norm_b = (senal_b or "").upper().strip()
                pct_est_fb = _f(pct_est_b)
                tk_str_b = str(tk_b or "").strip()
                f_pred_str_b = str(f_pred_b or "")[:10]
                key_sig = (tk_str_b, f_pred_str_b, sigma_b)
                if key_sig not in cache_sigma_backfill:
                    if tk_str_b not in cache_sub_backfill:
                        cache_sub_backfill[tk_str_b] = (
                            _extraer_serie_raw_ticker(fuente_backfill, tk_str_b)
                            if fuente_backfill is not None
                            else None
                        )
                    cache_sigma_backfill[key_sig] = _calcular_sigma_cierre_hasta_emision_pct(
                        cache_sub_backfill[tk_str_b],
                        f_pred_str_b,
                        sigma_fallback=sigma_b,
                    )
                sigma_cierre_pct_b = cache_sigma_backfill[key_sig]
                umbral_v2_b = calcular_umbral_volatilidad_v2(
                    sigma=sigma_cierre_pct_b,
                    dias=dias_b,
                    horizonte=hz_norm_b,
                    sigma_ya_en_pct=True,
                )

                var_p_f = float(var_precio_b)
                clase_v2_p = _clasificar_variacion_c10(var_p_f, umbral_v2_b)
                acierto_v2_p = 1 if senal_norm_b == clase_v2_p else 0
                signo_v2_p = _calcular_acierto_signo_c10(senal_norm_b, var_p_f)
                err_v2_p = (
                    round(abs(var_p_f - pct_est_fb), 4) if pct_est_fb is not None else None
                )

                chg0 = conn.total_changes
                conn.execute(
                    sql_insert_c12,
                    (
                        int(pid_b),
                        REGLA_C12_VOL_PRECIO_CIERRE,
                        float(ep_in),
                        float(ep_out),
                        var_p_f,
                        float(umbral_v2_b),
                        clase_v2_p,
                        int(acierto_v2_p),
                        signo_v2_p,
                        err_v2_p,
                        float(dif_p_b or 0.0),
                        evaluada_en_iso,
                    ),
                )
                if conn.total_changes > chg0:
                    evaluadas_vol_precio_cierre += 1

                var_t_f = float(var_total_b)
                clase_v2_t = _clasificar_variacion_c10(var_t_f, umbral_v2_b)
                acierto_v2_t = 1 if senal_norm_b == clase_v2_t else 0
                signo_v2_t = _calcular_acierto_signo_c10(senal_norm_b, var_t_f)
                err_v2_t = (
                    round(abs(var_t_f - pct_est_fb), 4) if pct_est_fb is not None else None
                )

                chg1 = conn.total_changes
                conn.execute(
                    sql_insert_c12,
                    (
                        int(pid_b),
                        REGLA_C12_VOL_RETORNO_TOTAL,
                        float(et_in),
                        float(et_out),
                        var_t_f,
                        float(umbral_v2_b),
                        clase_v2_t,
                        int(acierto_v2_t),
                        signo_v2_t,
                        err_v2_t,
                        float(dif_t_b or 0.0),
                        evaluada_en_iso,
                    ),
                )
                if conn.total_changes > chg1:
                    evaluadas_vol_retorno_total += 1

                var_ap_f = float(var_ap_b)
                clase_v2_ap = _clasificar_variacion_c10(var_ap_f, umbral_v2_b)
                acierto_v2_ap = 1 if senal_norm_b == clase_v2_ap else 0
                signo_v2_ap = _calcular_acierto_signo_c10(senal_norm_b, var_ap_f)
                err_v2_ap = (
                    round(abs(var_ap_f - pct_est_fb), 4) if pct_est_fb is not None else None
                )

                chg2 = conn.total_changes
                conn.execute(
                    sql_insert_c12,
                    (
                        int(pid_b),
                        REGLA_C12_VOL_APERTURA_PRECIO,
                        float(ea_in),
                        float(ea_out),
                        var_ap_f,
                        float(umbral_v2_b),
                        clase_v2_ap,
                        int(acierto_v2_ap),
                        signo_v2_ap,
                        err_v2_ap,
                        float(dif_ap_b or 0.0),
                        evaluada_en_iso,
                    ),
                )
                if conn.total_changes > chg2:
                    evaluadas_vol_apertura_precio += 1

        if incluir_c12:
            pendientes = conn.execute(
                """
                SELECT p.id, p.ticker, p.horizonte, p.dias, p.senal, p.pct_estimado,
                       p.sigma, p.fecha_prediccion, p.fecha_objetivo
                FROM predicciones p
                LEFT JOIN evaluaciones ep
                  ON ep.prediccion_id = p.id AND ep.regla = ?
                LEFT JOIN evaluaciones et
                  ON et.prediccion_id = p.id AND et.regla = ?
                LEFT JOIN evaluaciones ea
                  ON ea.prediccion_id = p.id AND ea.regla = ?
                LEFT JOIN evaluaciones eat
                  ON eat.prediccion_id = p.id AND eat.regla = ?
                LEFT JOIN evaluaciones ev2p
                  ON ev2p.prediccion_id = p.id AND ev2p.regla = ?
                LEFT JOIN evaluaciones ev2t
                  ON ev2t.prediccion_id = p.id AND ev2t.regla = ?
                LEFT JOIN evaluaciones ev2a
                  ON ev2a.prediccion_id = p.id AND ev2a.regla = ?
                WHERE p.resuelta = 1
                  AND (
                      ep.id IS NULL OR et.id IS NULL
                      OR ea.id IS NULL OR eat.id IS NULL
                      OR ev2p.id IS NULL OR ev2t.id IS NULL OR ev2a.id IS NULL
                  )
                ORDER BY p.id ASC
                """,
                (
                    REGLA_C10_PRECIO_CIERRE,
                    REGLA_C10_RETORNO_TOTAL,
                    REGLA_C11_APERTURA_PRECIO,
                    REGLA_C11_APERTURA_RETORNO_TOTAL,
                    REGLA_C12_VOL_PRECIO_CIERRE,
                    REGLA_C12_VOL_RETORNO_TOTAL,
                    REGLA_C12_VOL_APERTURA_PRECIO,
                ),
            ).fetchall()
        elif incluir_c11:
            pendientes = conn.execute(
                """
                SELECT p.id, p.ticker, p.horizonte, p.dias, p.senal, p.pct_estimado,
                       p.sigma, p.fecha_prediccion, p.fecha_objetivo
                FROM predicciones p
                LEFT JOIN evaluaciones ep
                  ON ep.prediccion_id = p.id AND ep.regla = ?
                LEFT JOIN evaluaciones et
                  ON et.prediccion_id = p.id AND et.regla = ?
                LEFT JOIN evaluaciones ea
                  ON ea.prediccion_id = p.id AND ea.regla = ?
                LEFT JOIN evaluaciones eat
                  ON eat.prediccion_id = p.id AND eat.regla = ?
                WHERE p.resuelta = 1
                  AND (ep.id IS NULL OR et.id IS NULL OR ea.id IS NULL OR eat.id IS NULL)
                ORDER BY p.id ASC
                """,
                (
                    REGLA_C10_PRECIO_CIERRE,
                    REGLA_C10_RETORNO_TOTAL,
                    REGLA_C11_APERTURA_PRECIO,
                    REGLA_C11_APERTURA_RETORNO_TOTAL,
                ),
            ).fetchall()
        else:
            pendientes = conn.execute(
                """
                SELECT p.id, p.ticker, p.horizonte, p.dias, p.senal, p.pct_estimado,
                       p.sigma, p.fecha_prediccion, p.fecha_objetivo
                FROM predicciones p
                LEFT JOIN evaluaciones ep
                  ON ep.prediccion_id = p.id AND ep.regla = ?
                LEFT JOIN evaluaciones et
                  ON et.prediccion_id = p.id AND et.regla = ?
                WHERE p.resuelta = 1
                  AND (ep.id IS NULL OR et.id IS NULL)
                ORDER BY p.id ASC
                """,
                (REGLA_C10_PRECIO_CIERRE, REGLA_C10_RETORNO_TOTAL),
            ).fetchall()

        if not pendientes:
            if (
                evaluadas_vol_precio_cierre > 0
                or evaluadas_vol_retorno_total > 0
                or evaluadas_vol_apertura_precio > 0
            ):
                conn.commit()
            return {
                "evaluadas_precio": evaluadas_precio,
                "evaluadas_total": evaluadas_total,
                "evaluadas_apertura_precio": evaluadas_apertura_precio,
                "evaluadas_apertura_total": evaluadas_apertura_total,
                "evaluadas_vol_precio_cierre": evaluadas_vol_precio_cierre,
                "evaluadas_vol_retorno_total": evaluadas_vol_retorno_total,
                "evaluadas_vol_apertura_precio": evaluadas_vol_apertura_precio,
                "pendientes": 0,
            }

        fuente_ohlcv = df_raw_ohlcv
        if fuente_ohlcv is None:
            import yfinance as yf

            tickers_unicos = sorted({str(r[1]).strip().upper() for r in pendientes if r[1]})
            fechas_pred = [str(r[7])[:10] for r in pendientes if r[7]]
            fechas_obj = [str(r[8])[:10] for r in pendientes if r[8]]
            if not tickers_unicos or not fechas_pred or not fechas_obj:
                if (
                    evaluadas_vol_precio_cierre > 0
                    or evaluadas_vol_retorno_total > 0
                    or evaluadas_vol_apertura_precio > 0
                ):
                    conn.commit()
                return {
                    "evaluadas_precio": evaluadas_precio,
                    "evaluadas_total": evaluadas_total,
                    "evaluadas_apertura_precio": evaluadas_apertura_precio,
                    "evaluadas_apertura_total": evaluadas_apertura_total,
                    "evaluadas_vol_precio_cierre": evaluadas_vol_precio_cierre,
                    "evaluadas_vol_retorno_total": evaluadas_vol_retorno_total,
                    "evaluadas_vol_apertura_precio": evaluadas_vol_apertura_precio,
                    "pendientes": len(pendientes),
                }
            min_f_pred = min(fechas_pred)
            start_hist = (date.fromisoformat(min_f_pred) - timedelta(days=50)).isoformat()
            max_f_obj = max(fechas_obj)
            end_excl = (date.fromisoformat(max_f_obj) + timedelta(days=5)).isoformat()
            fuente_ohlcv = yf.download(
                tickers=tickers_unicos,
                start=start_hist,
                end=end_excl,
                auto_adjust=False,
                group_by="ticker",
                progress=False,
                threads=True,
            )

        for pid, ticker, horizonte, dias, senal, pct_est, sigma_val, f_pred_raw, f_obj_raw in pendientes:
            f_pred = str(f_pred_raw)[:10] if f_pred_raw else ""
            f_obj = str(f_obj_raw)[:10] if f_obj_raw else ""
            if not f_pred or not f_obj:
                siguen_pendientes += 1
                continue

            sub = _extraer_serie_raw_ticker(fuente_ohlcv, str(ticker))
            if sub is None or sub.empty:
                siguen_pendientes += 1
                continue

            close_in = _extraer_precio_en_fecha(sub, "Close", f_pred)
            close_out = _extraer_precio_en_fecha(sub, "Close", f_obj)
            if close_in is None or close_out is None:
                siguen_pendientes += 1
                continue

            if "Adj Close" in sub.columns:
                adj_in = _extraer_precio_en_fecha(sub, "Adj Close", f_pred)
                adj_out = _extraer_precio_en_fecha(sub, "Adj Close", f_obj)
            else:
                adj_in = close_in
                adj_out = close_out

            if adj_in is None or adj_out is None:
                siguen_pendientes += 1
                continue

            hz_norm = (horizonte or "").upper().strip()
            senal_norm = (senal or "").upper().strip()
            umbral_aplicado = float(UMBRALES_LATERAL.get(hz_norm, 0.8))
            sigma_cierre_pct = _calcular_sigma_cierre_hasta_emision_pct(
                sub, f_pred, sigma_fallback=sigma_val
            )
            umbral_v2 = calcular_umbral_volatilidad_v2(
                sigma=sigma_cierre_pct,
                dias=dias,
                horizonte=hz_norm,
                sigma_ya_en_pct=True,
            )
            pct_est_f = _f(pct_est)

            var_precio = round((close_out / close_in - 1.0) * 100.0, 4)
            var_total = round((adj_out / adj_in - 1.0) * 100.0, 4)
            dif_div = round(var_total - var_precio, 4)

            clase_precio = _clasificar_variacion_c10(var_precio, umbral_aplicado)
            acierto_precio = 1 if senal_norm == clase_precio else 0
            signo_precio = _calcular_acierto_signo_c10(senal_norm, var_precio)
            err_precio = (
                round(abs(var_precio - pct_est_f), 4) if pct_est_f is not None else None
            )

            clase_total = _clasificar_variacion_c10(var_total, umbral_aplicado)
            acierto_total = 1 if senal_norm == clase_total else 0
            signo_total = _calcular_acierto_signo_c10(senal_norm, var_total)
            err_total = (
                round(abs(var_total - pct_est_f), 4) if pct_est_f is not None else None
            )

            chg_before_p = conn.total_changes
            conn.execute(
                sql_insert,
                (
                    int(pid),
                    REGLA_C10_PRECIO_CIERRE,
                    float(close_in),
                    float(close_out),
                    float(var_precio),
                    float(umbral_aplicado),
                    clase_precio,
                    int(acierto_precio),
                    signo_precio,
                    err_precio,
                    0.0,
                    evaluada_en_iso,
                ),
            )
            if conn.total_changes > chg_before_p:
                evaluadas_precio += 1

            chg_before_t = conn.total_changes
            conn.execute(
                sql_insert,
                (
                    int(pid),
                    REGLA_C10_RETORNO_TOTAL,
                    float(adj_in),
                    float(adj_out),
                    float(var_total),
                    float(umbral_aplicado),
                    clase_total,
                    int(acierto_total),
                    signo_total,
                    err_total,
                    float(dif_div),
                    evaluada_en_iso,
                ),
            )
            if conn.total_changes > chg_before_t:
                evaluadas_total += 1

            if incluir_c12:
                clase_v2_precio = _clasificar_variacion_c10(var_precio, umbral_v2)
                acierto_v2_precio = 1 if senal_norm == clase_v2_precio else 0
                chg_before_v2p = conn.total_changes
                conn.execute(
                    sql_insert_c12,
                    (
                        int(pid),
                        REGLA_C12_VOL_PRECIO_CIERRE,
                        float(close_in),
                        float(close_out),
                        float(var_precio),
                        float(umbral_v2),
                        clase_v2_precio,
                        int(acierto_v2_precio),
                        signo_precio,
                        err_precio,
                        0.0,
                        evaluada_en_iso,
                    ),
                )
                if conn.total_changes > chg_before_v2p:
                    evaluadas_vol_precio_cierre += 1

                clase_v2_total = _clasificar_variacion_c10(var_total, umbral_v2)
                acierto_v2_total = 1 if senal_norm == clase_v2_total else 0
                chg_before_v2t = conn.total_changes
                conn.execute(
                    sql_insert_c12,
                    (
                        int(pid),
                        REGLA_C12_VOL_RETORNO_TOTAL,
                        float(adj_in),
                        float(adj_out),
                        float(var_total),
                        float(umbral_v2),
                        clase_v2_total,
                        int(acierto_v2_total),
                        signo_total,
                        err_total,
                        float(dif_div),
                        evaluada_en_iso,
                    ),
                )
                if conn.total_changes > chg_before_v2t:
                    evaluadas_vol_retorno_total += 1

            if not (incluir_c11 or incluir_c12):
                continue

            fecha_entrada_op = _determinar_fecha_entrada_operable(sub, f_pred, f_obj)
            if fecha_entrada_op is None:
                siguen_pendientes += 1
                continue

            close_s1 = _extraer_precio_en_fecha(sub, "Close", fecha_entrada_op)
            open_s1 = (
                _extraer_precio_en_fecha(sub, "Open", fecha_entrada_op)
                if "Open" in sub.columns
                else close_s1
            )
            adj_s1 = (
                _extraer_precio_en_fecha(sub, "Adj Close", fecha_entrada_op)
                if "Adj Close" in sub.columns
                else close_s1
            )
            if (
                open_s1 is None
                or close_s1 is None
                or adj_s1 is None
                or open_s1 <= 0.0
                or close_s1 <= 0.0
                or adj_s1 <= 0.0
            ):
                siguen_pendientes += 1
                continue

            open_adj_s1 = open_s1 * (adj_s1 / close_s1)
            var_ap_precio = round((close_out / open_s1 - 1.0) * 100.0, 4)
            var_ap_total = round((adj_out / open_adj_s1 - 1.0) * 100.0, 4)
            gap_ap_pct = round(var_ap_precio - var_precio, 4)
            dif_div_ap = round(var_ap_total - var_ap_precio, 4)

            signo_ap_precio = _calcular_acierto_signo_c10(senal_norm, var_ap_precio)
            err_ap_precio = (
                round(abs(var_ap_precio - pct_est_f), 4) if pct_est_f is not None else None
            )

            if incluir_c11:
                clase_ap_precio = _clasificar_variacion_c10(var_ap_precio, umbral_aplicado)
                acierto_ap_precio = 1 if senal_norm == clase_ap_precio else 0

                clase_ap_total = _clasificar_variacion_c10(var_ap_total, umbral_aplicado)
                acierto_ap_total = 1 if senal_norm == clase_ap_total else 0
                signo_ap_total = _calcular_acierto_signo_c10(senal_norm, var_ap_total)
                err_ap_total = (
                    round(abs(var_ap_total - pct_est_f), 4) if pct_est_f is not None else None
                )

                chg_before_ap = conn.total_changes
                conn.execute(
                    sql_insert,
                    (
                        int(pid),
                        REGLA_C11_APERTURA_PRECIO,
                        float(open_s1),
                        float(close_out),
                        float(var_ap_precio),
                        float(umbral_aplicado),
                        clase_ap_precio,
                        int(acierto_ap_precio),
                        signo_ap_precio,
                        err_ap_precio,
                        float(gap_ap_pct),
                        evaluada_en_iso,
                    ),
                )
                if conn.total_changes > chg_before_ap:
                    evaluadas_apertura_precio += 1

                chg_before_apt = conn.total_changes
                conn.execute(
                    sql_insert,
                    (
                        int(pid),
                        REGLA_C11_APERTURA_RETORNO_TOTAL,
                        float(open_adj_s1),
                        float(adj_out),
                        float(var_ap_total),
                        float(umbral_aplicado),
                        clase_ap_total,
                        int(acierto_ap_total),
                        signo_ap_total,
                        err_ap_total,
                        float(dif_div_ap),
                        evaluada_en_iso,
                    ),
                )
                if conn.total_changes > chg_before_apt:
                    evaluadas_apertura_total += 1

            if incluir_c12:
                clase_v2_ap = _clasificar_variacion_c10(var_ap_precio, umbral_v2)
                acierto_v2_ap = 1 if senal_norm == clase_v2_ap else 0
                chg_before_v2ap = conn.total_changes
                conn.execute(
                    sql_insert_c12,
                    (
                        int(pid),
                        REGLA_C12_VOL_APERTURA_PRECIO,
                        float(open_s1),
                        float(close_out),
                        float(var_ap_precio),
                        float(umbral_v2),
                        clase_v2_ap,
                        int(acierto_v2_ap),
                        signo_ap_precio,
                        err_ap_precio,
                        float(gap_ap_pct),
                        evaluada_en_iso,
                    ),
                )
                if conn.total_changes > chg_before_v2ap:
                    evaluadas_vol_apertura_precio += 1

        if (
            evaluadas_precio > 0
            or evaluadas_total > 0
            or evaluadas_apertura_precio > 0
            or evaluadas_apertura_total > 0
            or evaluadas_vol_precio_cierre > 0
            or evaluadas_vol_retorno_total > 0
            or evaluadas_vol_apertura_precio > 0
        ):
            conn.commit()

        return {
            "evaluadas_precio": evaluadas_precio,
            "evaluadas_total": evaluadas_total,
            "evaluadas_apertura_precio": evaluadas_apertura_precio,
            "evaluadas_apertura_total": evaluadas_apertura_total,
            "evaluadas_vol_precio_cierre": evaluadas_vol_precio_cierre,
            "evaluadas_vol_retorno_total": evaluadas_vol_retorno_total,
            "evaluadas_vol_apertura_precio": evaluadas_vol_apertura_precio,
            "pendientes": siguen_pendientes,
        }
    finally:
        conn.row_factory = orig_rf
        if own_conn:
            conn.close()


def evaluar_predicciones_paralelas(
    df_raw_ohlcv: Optional[pd.DataFrame] = None,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
    incluir_c11: bool = True,
    incluir_c12: bool = True,
    recalcular_existentes: bool = False,
) -> Dict:
    """
    Evalua en paralelo las reglas v1 (C-10 y C-11) y v2 (C-12, zona muerta escalada
    por volatilidad) sobre las predicciones resueltas, sin modificar predicciones.
    """
    return evaluar_predicciones_c10_c11(
        df_raw_ohlcv=df_raw_ohlcv,
        conn=conn,
        db_path=db_path,
        incluir_c11=incluir_c11,
        incluir_c12=incluir_c12,
        recalcular_existentes=recalcular_existentes,
    )


def evaluar_predicciones_c12(
    df_raw_ohlcv: Optional[pd.DataFrame] = None,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
    recalcular_existentes: bool = False,
) -> Dict:
    """
    Evalua las reglas C-12 (junto con C-10 y C-11 si faltasen) sobre predicciones resueltas.
    Cuando recalcular_existentes=True, recalcula (INSERT OR REPLACE) las 3 filas
    'c12_vol_*' en evaluaciones sin tocar predicciones ni las reglas c10_*/c11_*.
    """
    return evaluar_predicciones_c10_c11(
        df_raw_ohlcv=df_raw_ohlcv,
        conn=conn,
        db_path=db_path,
        incluir_c11=True,
        incluir_c12=True,
        recalcular_existentes=recalcular_existentes,
    )


def evaluar_predicciones_retorno_total(
    df_raw_ohlcv: Optional[pd.DataFrame] = None,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[str] = None,
    incluir_c11: bool = False,
    incluir_c12: bool = False,
) -> Dict:
    """
    Evalua en paralelo las predicciones resueltas (resuelta = 1) bajo las reglas
    C-10 (y C-11/C-12 cuando se activan) invocando evaluar_predicciones_c10_c11
    para aprovechar la misma descarga auto_adjust=False. Nunca modifica la tabla predicciones.
    """
    return evaluar_predicciones_c10_c11(
        df_raw_ohlcv=df_raw_ohlcv,
        conn=conn,
        db_path=db_path,
        incluir_c11=incluir_c11,
        incluir_c12=incluir_c12,
    )


def obtener_evaluaciones_comparadas(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    solo_con_dividendo: bool = False,
    limite: int = 5000,
    conn: Optional[sqlite3.Connection] = None,
) -> pd.DataFrame:
    """
    Realiza JOIN entre predicciones (resuelta = 1) y sus filas en evaluaciones
    ('c10_precio_cierre' y 'c10_retorno_total'), devolviendo para cada senal tanto
    la variacion/acierto sellados como var_precio_c10, acierto_precio_c10,
    var_retorno_total_c10, acierto_retorno_total_c10 y diferencia_dividendo_pct.
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    try:
        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
        ).fetchone()
        if not row_tbl:
            return pd.DataFrame()

        where_clauses = ["p.resuelta = 1"]
        params: list = [REGLA_C10_PRECIO_CIERRE, REGLA_C10_RETORNO_TOTAL]

        if not incluir_legacy:
            where_clauses.append("p.modelo != 'legacy-pre-freeze'")
        if horizonte and horizonte != "Todos":
            where_clauses.append("p.horizonte = ?")
            params.append(horizonte)
        if solo_con_dividendo:
            where_clauses.append("ABS(COALESCE(et.diferencia_dividendo_pct, 0.0)) > 0.0")

        where_sql = " AND ".join(where_clauses)
        params.append(int(limite))

        sql = f"""
        SELECT
            p.id AS id,
            p.id AS prediccion_id,
            p.ticker,
            p.empresa,
            p.fecha_prediccion,
            p.horizonte,
            p.dias,
            p.fecha_objetivo,
            p.fecha_resolucion,
            p.senal,
            p.pct_estimado,
            p.banda_pct,
            p.confianza,
            p.modelo,
            p.precio_entrada,
            p.precio_real,
            p.variacion_real,
            p.acierto,
            p.error_pct,
            p.score_calidad,
            ep.precio_entrada_eval AS precio_entrada_precio_c10,
            ep.precio_salida_eval AS precio_salida_precio_c10,
            ep.variacion_pct AS var_precio_c10,
            ep.umbral_aplicado AS umbral_precio_c10,
            ep.clase_real AS clase_real_precio_c10,
            ep.acierto AS acierto_precio_c10,
            ep.acierto_signo AS acierto_signo_precio_c10,
            ep.error_pct AS error_precio_c10,
            et.precio_entrada_eval AS precio_entrada_total_c10,
            et.precio_salida_eval AS precio_salida_total_c10,
            et.variacion_pct AS var_retorno_total_c10,
            et.umbral_aplicado AS umbral_retorno_total_c10,
            et.clase_real AS clase_real_retorno_total_c10,
            et.acierto AS acierto_retorno_total_c10,
            et.acierto_signo AS acierto_signo_retorno_total_c10,
            et.error_pct AS error_retorno_total_c10,
            et.diferencia_dividendo_pct AS diferencia_dividendo_pct,
            et.evaluada_en AS evaluada_en
        FROM predicciones p
        INNER JOIN evaluaciones ep
            ON ep.prediccion_id = p.id AND ep.regla = ?
        INNER JOIN evaluaciones et
            ON et.prediccion_id = p.id AND et.regla = ?
        WHERE {where_sql}
        ORDER BY COALESCE(p.fecha_resolucion, p.fecha_objetivo) DESC, p.id DESC
        LIMIT ?
        """
        return pd.read_sql_query(sql, conn, params=tuple(params))
    except Exception:
        return pd.DataFrame()
    finally:
        if own_conn:
            conn.close()


def _obtener_resumen_c10_base(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """
    Devuelve metricas comparadas C-10 sobre predicciones resueltas evaluadas:
      - n_evaluadas
      - tasa_acierto_sellada
      - tasa_acierto_precio_c10
      - tasa_acierto_retorno_total_c10
      - n_con_dividendo (filas con abs(diferencia_dividendo_pct) >= 0.01)
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    try:
        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
        ).fetchone()
        if not row_tbl:
            return {
                "n_evaluadas": 0,
                "tasa_acierto_sellada": None,
                "tasa_acierto_precio_c10": None,
                "tasa_acierto_retorno_total_c10": None,
                "n_con_dividendo": 0,
            }

        where_clauses = ["p.resuelta = 1"]
        params: list = [REGLA_C10_PRECIO_CIERRE, REGLA_C10_RETORNO_TOTAL]
        if not incluir_legacy:
            where_clauses.append("p.modelo != 'legacy-pre-freeze'")
        if horizonte and horizonte != "Todos":
            where_clauses.append("p.horizonte = ?")
            params.append(horizonte)

        where_sql = " AND ".join(where_clauses)
        row = conn.execute(
            f"""
            SELECT
                COUNT(*),
                COALESCE(SUM(p.acierto), 0),
                COALESCE(SUM(ep.acierto), 0),
                COALESCE(SUM(et.acierto), 0),
                COALESCE(
                    SUM(
                        CASE
                            WHEN ABS(COALESCE(et.diferencia_dividendo_pct, 0.0)) >= 0.01
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                )
            FROM predicciones p
            INNER JOIN evaluaciones ep
                ON ep.prediccion_id = p.id AND ep.regla = ?
            INNER JOIN evaluaciones et
                ON et.prediccion_id = p.id AND et.regla = ?
            WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()

        n_eval = int(row[0]) if row and row[0] is not None else 0
        ok_sellada = int(row[1]) if row and row[1] is not None else 0
        ok_precio = int(row[2]) if row and row[2] is not None else 0
        ok_total = int(row[3]) if row and row[3] is not None else 0
        n_div = int(row[4]) if row and row[4] is not None else 0

        if n_eval == 0:
            return {
                "n_evaluadas": 0,
                "tasa_acierto_sellada": None,
                "tasa_acierto_precio_c10": None,
                "tasa_acierto_retorno_total_c10": None,
                "n_con_dividendo": 0,
            }

        return {
            "n_evaluadas": n_eval,
            "tasa_acierto_sellada": round(100.0 * ok_sellada / n_eval, 2),
            "tasa_acierto_precio_c10": round(100.0 * ok_precio / n_eval, 2),
            "tasa_acierto_retorno_total_c10": round(100.0 * ok_total / n_eval, 2),
            "n_con_dividendo": n_div,
        }
    except Exception:
        return {
            "n_evaluadas": 0,
            "tasa_acierto_sellada": None,
            "tasa_acierto_precio_c10": None,
            "tasa_acierto_retorno_total_c10": None,
            "n_con_dividendo": 0,
        }
    finally:
        if own_conn:
            conn.close()


def _construir_metricas_resumen_c11(
    n_eval: int,
    ok_cp: int,
    ok_ap: int,
    ok_ct: int,
    ok_at: int,
) -> Dict:
    """Construye el diccionario de metricas C-11 para un conteo dado."""
    if n_eval <= 0:
        return {
            "n_evaluadas": 0,
            "tasa_cierre_precio": None,
            "tasa_apertura_precio": None,
            "delta_apertura_vs_cierre_pp": None,
            "tasa_cierre_retorno_total": None,
            "tasa_apertura_retorno_total": None,
            "delta_apertura_vs_cierre_total_pp": None,
        }
    tasa_cp = round(100.0 * ok_cp / n_eval, 2)
    tasa_ap = round(100.0 * ok_ap / n_eval, 2)
    delta_p = round(tasa_ap - tasa_cp, 2)
    tasa_ct = round(100.0 * ok_ct / n_eval, 2)
    tasa_at = round(100.0 * ok_at / n_eval, 2)
    delta_t = round(tasa_at - tasa_ct, 2)
    return {
        "n_evaluadas": n_eval,
        "tasa_cierre_precio": tasa_cp,
        "tasa_apertura_precio": tasa_ap,
        "delta_apertura_vs_cierre_pp": delta_p,
        "tasa_cierre_retorno_total": tasa_ct,
        "tasa_apertura_retorno_total": tasa_at,
        "delta_apertura_vs_cierre_total_pp": delta_t,
    }


def _obtener_resumen_c11_base(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """
    Devuelve el resumen comparativo C-11 (Cierre S vs Apertura S+1) para el filtro
    elegido (global y desglosado por horizonte 1D, 3D, 1S, 1M):
      - n_evaluadas
      - tasa_cierre_precio
      - tasa_apertura_precio
      - delta_apertura_vs_cierre_pp
      - tasa_cierre_retorno_total
      - tasa_apertura_retorno_total
      - delta_apertura_vs_cierre_total_pp
      - por_horizonte: {"1D": {...}, "3D": {...}, "1S": {...}, "1M": {...}}
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    vacio_hz = {
        hz: _construir_metricas_resumen_c11(0, 0, 0, 0, 0)
        for hz in ("1D", "3D", "1S", "1M")
    }
    vacio = _construir_metricas_resumen_c11(0, 0, 0, 0, 0)
    vacio["por_horizonte"] = vacio_hz
    try:
        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
        ).fetchone()
        if not row_tbl:
            return vacio

        base_rules = [
            REGLA_C10_PRECIO_CIERRE,
            REGLA_C10_RETORNO_TOTAL,
            REGLA_C11_APERTURA_PRECIO,
            REGLA_C11_APERTURA_RETORNO_TOTAL,
        ]
        where_base = ["p.resuelta = 1"]
        if not incluir_legacy:
            where_base.append("p.modelo != 'legacy-pre-freeze'")

        where_global = list(where_base)
        params_global = list(base_rules)
        if horizonte and horizonte != "Todos":
            where_global.append("p.horizonte = ?")
            params_global.append(horizonte)

        row_g = conn.execute(
            f"""
            SELECT
                COUNT(*),
                COALESCE(SUM(ep.acierto), 0),
                COALESCE(SUM(ea.acierto), 0),
                COALESCE(SUM(et.acierto), 0),
                COALESCE(SUM(eat.acierto), 0)
            FROM predicciones p
            INNER JOIN evaluaciones ep
                ON ep.prediccion_id = p.id AND ep.regla = ?
            INNER JOIN evaluaciones et
                ON et.prediccion_id = p.id AND et.regla = ?
            INNER JOIN evaluaciones ea
                ON ea.prediccion_id = p.id AND ea.regla = ?
            INNER JOIN evaluaciones eat
                ON eat.prediccion_id = p.id AND eat.regla = ?
            WHERE {' AND '.join(where_global)}
            """,
            tuple(params_global),
        ).fetchone()

        n_g = int(row_g[0]) if row_g and row_g[0] is not None else 0
        ok_cp_g = int(row_g[1]) if row_g and row_g[1] is not None else 0
        ok_ap_g = int(row_g[2]) if row_g and row_g[2] is not None else 0
        ok_ct_g = int(row_g[3]) if row_g and row_g[3] is not None else 0
        ok_at_g = int(row_g[4]) if row_g and row_g[4] is not None else 0

        res = _construir_metricas_resumen_c11(n_g, ok_cp_g, ok_ap_g, ok_ct_g, ok_at_g)

        rows_hz = conn.execute(
            f"""
            SELECT
                p.horizonte,
                COUNT(*),
                COALESCE(SUM(ep.acierto), 0),
                COALESCE(SUM(ea.acierto), 0),
                COALESCE(SUM(et.acierto), 0),
                COALESCE(SUM(eat.acierto), 0)
            FROM predicciones p
            INNER JOIN evaluaciones ep
                ON ep.prediccion_id = p.id AND ep.regla = ?
            INNER JOIN evaluaciones et
                ON et.prediccion_id = p.id AND et.regla = ?
            INNER JOIN evaluaciones ea
                ON ea.prediccion_id = p.id AND ea.regla = ?
            INNER JOIN evaluaciones eat
                ON eat.prediccion_id = p.id AND eat.regla = ?
            WHERE {' AND '.join(where_base)}
            GROUP BY p.horizonte
            """,
            tuple(base_rules),
        ).fetchall()

        por_hz = {
            hz: _construir_metricas_resumen_c11(0, 0, 0, 0, 0)
            for hz in ("1D", "3D", "1S", "1M")
        }
        for r_hz in rows_hz:
            hz_k = str(r_hz[0])
            por_hz[hz_k] = _construir_metricas_resumen_c11(
                int(r_hz[1] or 0),
                int(r_hz[2] or 0),
                int(r_hz[3] or 0),
                int(r_hz[4] or 0),
                int(r_hz[5] or 0),
            )

        res["por_horizonte"] = por_hz
        return res
    except Exception:
        return vacio
    finally:
        if own_conn:
            conn.close()


def _construir_bloque_c12(rows_eval: list) -> Dict:
    """
    Construye las metricas C-12 (globales y por terciles de volatilidad efectiva)
    a partir de una lista de tuplas:
      (sigma_raw, clase_v1_p, ok_v1_p, ok_v1_t, ok_v1_a, clase_v2_p, ok_v2_p, ok_v2_t, ok_v2_a[, umbral_v2, dias])
    La volatilidad efectiva diaria (%) del valor se calcula como:
      umbral_aplicado / (K_UMBRAL_SIGMA_V2 * sqrt(dias))
    con fallback a _normalizar_sigma_diaria_pct(sigma_raw).
    """
    etiquetas_terciles = [
        ("T1", "T1 (Baja vol)"),
        ("T2", "T2 (Media vol)"),
        ("T3", "T3 (Alta vol)"),
    ]
    n_eval = len(rows_eval)
    if n_eval <= 0:
        terciles_vacios = [
            {
                "codigo": cod,
                "tercil": nom,
                "n": 0,
                "sigma_min": None,
                "sigma_max": None,
                "sigma_min_pct": None,
                "sigma_max_pct": None,
                "pct_lateral_v1": None,
                "pct_lateral_v2": None,
                "tasa_acierto_v1": None,
                "tasa_acierto_v2": None,
            }
            for cod, nom in etiquetas_terciles
        ]
        return {
            "k_sigma": K_UMBRAL_SIGMA_V2,
            "n_evaluadas": 0,
            "tasa_acierto_v1_precio": None,
            "tasa_acierto_v1_retorno_total": None,
            "tasa_acierto_v1_apertura": None,
            "tasa_acierto_v2_precio": None,
            "tasa_acierto_v2_retorno_total": None,
            "tasa_acierto_v2_apertura": None,
            "pct_lateral_real_v1": None,
            "pct_lateral_real_v2": None,
            "terciles_sigma": terciles_vacios,
            "rango_intertercil_lateral_v1_pp": None,
            "rango_intertercil_lateral_v2_pp": None,
        }

    ok_v1_p = sum(int(r[2] or 0) for r in rows_eval)
    ok_v1_t = sum(int(r[3] or 0) for r in rows_eval)
    ok_v1_a = sum(int(r[4] or 0) for r in rows_eval)
    ok_v2_p = sum(int(r[6] or 0) for r in rows_eval)
    ok_v2_t = sum(int(r[7] or 0) for r in rows_eval)
    ok_v2_a = sum(int(r[8] or 0) for r in rows_eval)

    lat_v1 = sum(1 for r in rows_eval if str(r[1] or "").upper().strip() == "LATERAL")
    lat_v2 = sum(1 for r in rows_eval if str(r[5] or "").upper().strip() == "LATERAL")

    tasa_v1_p = round(100.0 * ok_v1_p / n_eval, 2)
    tasa_v1_t = round(100.0 * ok_v1_t / n_eval, 2)
    tasa_v1_a = round(100.0 * ok_v1_a / n_eval, 2)
    tasa_v2_p = round(100.0 * ok_v2_p / n_eval, 2)
    tasa_v2_t = round(100.0 * ok_v2_t / n_eval, 2)
    tasa_v2_a = round(100.0 * ok_v2_a / n_eval, 2)
    pct_lat_v1 = round(100.0 * lat_v1 / n_eval, 2)
    pct_lat_v2 = round(100.0 * lat_v2 / n_eval, 2)

    # Construir terciles sobre la volatilidad efectiva del valor:
    # umbral_aplicado / (K_UMBRAL_SIGMA_V2 * sqrt(dias))
    valid_sigma_rows = []
    for r in rows_eval:
        s_pct = None
        if len(r) >= 11 and r[9] is not None:
            try:
                umb_v2_f = float(r[9])
                dias_f = max(int(r[10] or 1), 1)
                denom = float(K_UMBRAL_SIGMA_V2) * math.sqrt(dias_f)
                if math.isfinite(umb_v2_f) and umb_v2_f > 0.0 and denom > 0.0:
                    s_pct = round(umb_v2_f / denom, 6)
            except (TypeError, ValueError):
                s_pct = None
        if s_pct is None:
            s_pct = _normalizar_sigma_diaria_pct(r[0])
        if s_pct is not None and s_pct > 0.0:
            s_frac = round(s_pct / 100.0, 6)
            valid_sigma_rows.append((s_frac, float(s_pct), r))

    grupos_terciles = [[], [], []]
    if valid_sigma_rows:
        sigmas_pct_arr = np.array([x[1] for x in valid_sigma_rows], dtype=float)
        q1 = float(np.quantile(sigmas_pct_arr, 1.0 / 3.0))
        q2 = float(np.quantile(sigmas_pct_arr, 2.0 / 3.0))
        g1 = [x for x in valid_sigma_rows if x[1] <= q1]
        g2 = [x for x in valid_sigma_rows if q1 < x[1] <= q2]
        g3 = [x for x in valid_sigma_rows if x[1] > q2]
        if g1 and g2 and g3:
            grupos_terciles = [g1, g2, g3]
        else:
            ordenados = sorted(valid_sigma_rows, key=lambda item: item[1])
            n_tot_s = len(ordenados)
            c1 = n_tot_s // 3
            c2 = (2 * n_tot_s) // 3
            grupos_terciles = [ordenados[:c1], ordenados[c1:c2], ordenados[c2:]]

    terciles_lista = []
    for idx_t, (cod, nom) in enumerate(etiquetas_terciles):
        grp = grupos_terciles[idx_t]
        n_t = len(grp)
        if n_t <= 0:
            terciles_lista.append(
                {
                    "codigo": cod,
                    "tercil": nom,
                    "n": 0,
                    "sigma_min": None,
                    "sigma_max": None,
                    "sigma_min_pct": None,
                    "sigma_max_pct": None,
                    "pct_lateral_v1": None,
                    "pct_lateral_v2": None,
                    "tasa_acierto_v1": None,
                    "tasa_acierto_v2": None,
                }
            )
            continue
        raw_vals = [x[0] for x in grp]
        pct_vals = [x[1] for x in grp]
        lat1_t = sum(1 for x in grp if str(x[2][1] or "").upper().strip() == "LATERAL")
        lat2_t = sum(1 for x in grp if str(x[2][5] or "").upper().strip() == "LATERAL")
        ok1_t = sum(int(x[2][2] or 0) for x in grp)
        ok2_t = sum(int(x[2][6] or 0) for x in grp)
        terciles_lista.append(
            {
                "codigo": cod,
                "tercil": nom,
                "n": n_t,
                "sigma_min": round(min(raw_vals), 6),
                "sigma_max": round(max(raw_vals), 6),
                "sigma_min_pct": round(min(pct_vals), 4),
                "sigma_max_pct": round(max(pct_vals), 4),
                "pct_lateral_v1": round(100.0 * lat1_t / n_t, 2),
                "pct_lateral_v2": round(100.0 * lat2_t / n_t, 2),
                "tasa_acierto_v1": round(100.0 * ok1_t / n_t, 2),
                "tasa_acierto_v2": round(100.0 * ok2_t / n_t, 2),
            }
        )

    p_lat_t1_v1 = terciles_lista[0]["pct_lateral_v1"]
    p_lat_t3_v1 = terciles_lista[2]["pct_lateral_v1"]
    p_lat_t1_v2 = terciles_lista[0]["pct_lateral_v2"]
    p_lat_t3_v2 = terciles_lista[2]["pct_lateral_v2"]

    rango_v1 = (
        round(abs(p_lat_t1_v1 - p_lat_t3_v1), 2)
        if p_lat_t1_v1 is not None and p_lat_t3_v1 is not None
        else None
    )
    rango_v2 = (
        round(abs(p_lat_t1_v2 - p_lat_t3_v2), 2)
        if p_lat_t1_v2 is not None and p_lat_t3_v2 is not None
        else None
    )

    return {
        "k_sigma": K_UMBRAL_SIGMA_V2,
        "n_evaluadas": n_eval,
        "tasa_acierto_v1_precio": tasa_v1_p,
        "tasa_acierto_v1_retorno_total": tasa_v1_t,
        "tasa_acierto_v1_apertura": tasa_v1_a,
        "tasa_acierto_v2_precio": tasa_v2_p,
        "tasa_acierto_v2_retorno_total": tasa_v2_t,
        "tasa_acierto_v2_apertura": tasa_v2_a,
        "pct_lateral_real_v1": pct_lat_v1,
        "pct_lateral_real_v2": pct_lat_v2,
        "terciles_sigma": terciles_lista,
        "rango_intertercil_lateral_v1_pp": rango_v1,
        "rango_intertercil_lateral_v2_pp": rango_v2,
    }


def _obtener_resumen_c12_base(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """
    Devuelve el resumen comparativo C-12 entre la Regla v1 (umbrales fijos 0.8/1.2/1.8/3.0)
    y la Regla v2 (zona muerta escalada por volatilidad k * sigma * sqrt(dias), k = 0.50):
      - k_sigma: K_UMBRAL_SIGMA_V2
      - n_evaluadas, tasa_acierto_v1_precio, tasa_acierto_v2_precio,
        tasa_acierto_v2_retorno_total, tasa_acierto_v2_apertura
      - pct_lateral_real_v1, pct_lateral_real_v2
      - terciles_sigma: lista de 3 diccionarios para 'T1 (Baja vol)', 'T2 (Media vol)'
        y 'T3 (Alta vol)' con n, sigma_min, sigma_max, pct_lateral_v1, pct_lateral_v2,
        tasa_acierto_v1, tasa_acierto_v2
      - rango_intertercil_lateral_v1_pp y rango_intertercil_lateral_v2_pp
      - por_horizonte: desglose para '1D', '3D', '1S', '1M'
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    vacio = _construir_bloque_c12([])
    vacio["por_horizonte"] = {
        hz: _construir_bloque_c12([]) for hz in ("1D", "3D", "1S", "1M")
    }
    try:
        row_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
        ).fetchone()
        if not row_tbl:
            return vacio

        reglas_join = (
            REGLA_C10_PRECIO_CIERRE,
            REGLA_C10_RETORNO_TOTAL,
            REGLA_C11_APERTURA_PRECIO,
            REGLA_C12_VOL_PRECIO_CIERRE,
            REGLA_C12_VOL_RETORNO_TOTAL,
            REGLA_C12_VOL_APERTURA_PRECIO,
        )
        where_base = ["p.resuelta = 1"]
        if not incluir_legacy:
            where_base.append("p.modelo != 'legacy-pre-freeze'")

        rows_all = conn.execute(
            f"""
            SELECT
                p.horizonte,
                p.sigma,
                ep.clase_real,
                ep.acierto,
                et.acierto,
                ea.acierto,
                ev2p.clase_real,
                ev2p.acierto,
                ev2t.acierto,
                ev2a.acierto,
                ev2p.umbral_aplicado,
                p.dias
            FROM predicciones p
            INNER JOIN evaluaciones ep
                ON ep.prediccion_id = p.id AND ep.regla = ?
            LEFT JOIN evaluaciones et
                ON et.prediccion_id = p.id AND et.regla = ?
            LEFT JOIN evaluaciones ea
                ON ea.prediccion_id = p.id AND ea.regla = ?
            INNER JOIN evaluaciones ev2p
                ON ev2p.prediccion_id = p.id AND ev2p.regla = ?
            LEFT JOIN evaluaciones ev2t
                ON ev2t.prediccion_id = p.id AND ev2t.regla = ?
            LEFT JOIN evaluaciones ev2a
                ON ev2a.prediccion_id = p.id AND ev2a.regla = ?
            WHERE {' AND '.join(where_base)}
            ORDER BY p.id ASC
            """,
            reglas_join,
        ).fetchall()

        por_hz_rows: Dict[str, list] = {hz: [] for hz in ("1D", "3D", "1S", "1M")}
        global_rows: list = []
        hz_filtro = (
            horizonte.strip()
            if (horizonte and horizonte != "Todos")
            else None
        )

        for r in rows_all:
            hz_r = str(r[0] or "")
            tupla_m = r[1:]
            if hz_r in por_hz_rows:
                por_hz_rows[hz_r].append(tupla_m)
            if hz_filtro is None or hz_r == hz_filtro:
                global_rows.append(tupla_m)

        res = _construir_bloque_c12(global_rows)
        res["por_horizonte"] = {
            hz: _construir_bloque_c12(por_hz_rows.get(hz, []))
            for hz in ("1D", "3D", "1S", "1M")
        }
        return res
    except Exception:
        return vacio
    finally:
        if own_conn:
            conn.close()


# ===================================================================
# C-09: envoltorios retrocompatibles de los resumenes C-10 / C-11 / C-12
# con metadatos de muestra y separacion 3 clases / signo
# ===================================================================

def obtener_resumen_c10(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """
    Resumen C-10 (claves originales intactas: n_evaluadas, tasa_acierto_sellada,
    tasa_acierto_precio_c10, tasa_acierto_retorno_total_c10, n_con_dividendo) mas,
    por C-09, metadatos de muestra y metricas de 3 clases / signo calculadas sobre la
    regla 'c10_precio_cierre' (global y en por_horizonte), y el acierto de signo en
    retorno total (tasa_acierto_signo_retorno_total_c10).
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    try:
        res = _obtener_resumen_c10_base(horizonte=horizonte, incluir_legacy=incluir_legacy, conn=conn)
        _enriquecer_resumen_c09(
            res, conn, REGLA_C10_PRECIO_CIERRE, (REGLA_C10_RETORNO_TOTAL,), horizonte, incluir_legacy
        )
        aux: Dict = {}
        glob_rt = _enriquecer_resumen_c09(
            aux, conn, REGLA_C10_RETORNO_TOTAL, (REGLA_C10_PRECIO_CIERRE,), horizonte, incluir_legacy
        )
        res["tasa_acierto_signo_retorno_total_c10"] = glob_rt.get("tasa_acierto_signo")
        res["ref_frecuencia_subida_retorno_total_pct"] = glob_rt.get("ref_frecuencia_subida_pct")
        try:
            res["referencias_ingenuas_c14"] = obtener_tabla_referencias_ingenuas(
                horizonte=horizonte, incluir_legacy=incluir_legacy, regla_umbral="v1", conn=conn
            )
        except Exception:
            pass
        return res
    finally:
        if own_conn:
            conn.close()


def obtener_resumen_c11(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """
    Resumen C-11 (claves originales intactas) mas, por C-09, metadatos de muestra y
    metricas de 3 clases / signo desde Cierre S (regla 'c10_precio_cierre') global y por
    horizonte, y el acierto de signo desde Apertura S+1 ('c11_apertura_precio'):
    tasa_acierto_signo_apertura y ref_frecuencia_subida_apertura_pct (global y por horizonte).
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    try:
        res = _obtener_resumen_c11_base(horizonte=horizonte, incluir_legacy=incluir_legacy, conn=conn)
        reglas_c11 = (
            REGLA_C10_PRECIO_CIERRE,
            REGLA_C10_RETORNO_TOTAL,
            REGLA_C11_APERTURA_PRECIO,
            REGLA_C11_APERTURA_RETORNO_TOTAL,
        )
        _enriquecer_resumen_c09(
            res, conn, REGLA_C10_PRECIO_CIERRE, reglas_c11[1:], horizonte, incluir_legacy
        )
        aux: Dict = {}
        glob_ap = _enriquecer_resumen_c09(
            aux,
            conn,
            REGLA_C11_APERTURA_PRECIO,
            (REGLA_C10_PRECIO_CIERRE, REGLA_C10_RETORNO_TOTAL, REGLA_C11_APERTURA_RETORNO_TOTAL),
            horizonte,
            incluir_legacy,
        )
        res["tasa_acierto_signo_apertura"] = glob_ap.get("tasa_acierto_signo")
        res["ref_frecuencia_subida_apertura_pct"] = glob_ap.get("ref_frecuencia_subida_pct")
        res["ref_siempre_lateral_apertura_pct"] = glob_ap.get("ref_siempre_lateral_pct")
        por_hz = res.get("por_horizonte") or {}
        for h_k, entrada_aux in (aux.get("por_horizonte") or {}).items():
            if isinstance(por_hz.get(h_k), dict):
                por_hz[h_k]["tasa_acierto_signo_apertura"] = entrada_aux.get("tasa_acierto_signo")
                por_hz[h_k]["ref_frecuencia_subida_apertura_pct"] = entrada_aux.get(
                    "ref_frecuencia_subida_pct"
                )
        try:
            res["referencias_ingenuas_c14"] = obtener_tabla_referencias_ingenuas(
                horizonte=horizonte, incluir_legacy=incluir_legacy, regla_umbral="v1", conn=conn
            )
        except Exception:
            pass
        return res
    finally:
        if own_conn:
            conn.close()


def obtener_resumen_c12(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict:
    """
    Resumen C-12 (claves originales intactas) mas, por C-09, metadatos de muestra y
    metricas de 3 clases / signo bajo la Regla v2 ('c12_vol_precio_cierre', con
    ref_siempre_lateral_pct = % clase real LATERAL v2) global y por horizonte, y las
    equivalentes de la Regla v1 sobre la misma poblacion: tasa_acierto_3clases_v1 y
    ref_siempre_lateral_v1_pct.
    """
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    try:
        res = _obtener_resumen_c12_base(horizonte=horizonte, incluir_legacy=incluir_legacy, conn=conn)
        _enriquecer_resumen_c09(
            res, conn, REGLA_C12_VOL_PRECIO_CIERRE, (REGLA_C10_PRECIO_CIERRE,), horizonte, incluir_legacy
        )
        aux: Dict = {}
        glob_v1 = _enriquecer_resumen_c09(
            aux, conn, REGLA_C10_PRECIO_CIERRE, (REGLA_C12_VOL_PRECIO_CIERRE,), horizonte, incluir_legacy
        )
        res["tasa_acierto_3clases_v1"] = glob_v1.get("tasa_acierto_3clases")
        res["ref_siempre_lateral_v1_pct"] = glob_v1.get("ref_siempre_lateral_pct")
        res["tasa_acierto_3clases_v2"] = res.get("tasa_acierto_3clases")
        res["ref_siempre_lateral_v2_pct"] = res.get("ref_siempre_lateral_pct")
        por_hz = res.get("por_horizonte") or {}
        for h_k, entrada_aux in (aux.get("por_horizonte") or {}).items():
            if isinstance(por_hz.get(h_k), dict):
                por_hz[h_k]["tasa_acierto_3clases_v1"] = entrada_aux.get("tasa_acierto_3clases")
                por_hz[h_k]["ref_siempre_lateral_v1_pct"] = entrada_aux.get("ref_siempre_lateral_pct")
        try:
            res["referencias_ingenuas_c14"] = obtener_tabla_referencias_ingenuas(
                horizonte=horizonte, incluir_legacy=incluir_legacy, regla_umbral="v2", conn=conn
            )
        except Exception:
            pass
        return res
    finally:
        if own_conn:
            conn.close()



# ===================================================================
# C-14: TABLA COMPLETA DE REFERENCIAS INGENUAS (MOTOR VS 5 REFERENCIAS)
# ===================================================================

_CACHE_PARQUET_PRECIOS = None


def _tabla_referencias_ingenuas_vacia(hz_label: str = "Todos") -> Dict:
    """Estructura vacía de fallback para C-14."""
    et = _metadatos_muestra(0, 0, None, None, hz_label)["etiqueta_muestra"]
    return {
        "etiqueta_muestra": et,
        "n_senales": 0,
        "n_fechas": 0,
        "regla_umbral": "v1",
        "horizonte": hz_label,
        "tabla": [],
        "por_horizonte": {h: [] for h in ("1D", "3D", "1S", "1M")},
        "mejor_estrategia_3clases": "motor",
        "mejor_estrategia_signo": "motor",
        "motor_supera_todas_3clases": False,
        "motor_supera_todas_signo": False,
    }


def _extraer_retorno_previo_serie(
    df_precios: Optional[pd.DataFrame],
    ticker: str,
    fecha_prediccion_str: str,
    dias: int,
) -> Optional[float]:
    """
    Calcula el retorno previo de 'dias' sesiones finalizando en fecha_prediccion (inclusive):
        ret_prev_pct = (Close[t] / Close[t - dias] - 1) * 100
    usando EXCLUSIVAMENTE datos con fecha <= fecha_prediccion (cero look-ahead).
    """
    if df_precios is None or dias <= 0:
        return None
    try:
        if isinstance(df_precios, pd.DataFrame) and df_precios.empty:
            return None
        fp_str = str(fecha_prediccion_str)[:10]
        series = None
        # Caso 1: MultiIndex columns (ticker, 'Close')
        if hasattr(df_precios, "columns") and isinstance(df_precios.columns, pd.MultiIndex):
            col_candidates = [
                (ticker, "Close"), (ticker, "close"),
                (ticker.upper(), "Close"), (ticker.upper(), "close"),
            ]
            if ticker == "^IBEX":
                col_candidates.extend([("^IBEX", "Close"), ("^IBEX", "close"), ("IBEX", "Close"), ("IBEX", "close")])
            for c in col_candidates:
                if c in df_precios.columns:
                    series = df_precios[c]
                    break
        elif hasattr(df_precios, "columns"):
            cols = df_precios.columns
            if ticker in cols:
                series = df_precios[ticker]
            elif ticker.upper() in cols:
                series = df_precios[ticker.upper()]
            elif "Close" in cols or "close" in cols:
                c_name = "Close" if "Close" in cols else "close"
                if "ticker" in cols:
                    sub = df_precios[df_precios["ticker"].astype(str).str.upper() == ticker.upper()]
                    if not sub.empty:
                        series = sub[c_name]
                        if "fecha" in sub.columns:
                            series.index = pd.to_datetime(sub["fecha"])
                        elif "Date" in sub.columns:
                            series.index = pd.to_datetime(sub["Date"])
                else:
                    series = df_precios[c_name]
        elif isinstance(df_precios, pd.Series):
            series = df_precios

        if series is None or len(series) == 0:
            return None

        # Truncar serie estrictamente a fecha <= fp_str (Cero Look-Ahead)
        idx = series.index
        if isinstance(idx, pd.DatetimeIndex):
            if idx.tz is not None:
                idx = idx.tz_convert("Europe/Madrid").tz_localize(None)
                series = series.copy()
                series.index = idx
            fp_dt = pd.to_datetime(fp_str)
            sub_series = series[series.index <= fp_dt].dropna()
        else:
            str_idx = [str(x)[:10] for x in idx]
            mask = [x <= fp_str for x in str_idx]
            sub_series = series.iloc[mask].dropna()

        n_pts = len(sub_series)
        if n_pts < dias + 1:
            return None

        c_t = float(sub_series.iloc[-1])
        c_prev = float(sub_series.iloc[-1 - dias])
        if c_prev <= 0.0 or not np.isfinite(c_t) or not np.isfinite(c_prev):
            return None
        return (c_t / c_prev - 1.0) * 100.0
    except Exception:
        return None


def obtener_tabla_referencias_ingenuas(
    horizonte: Optional[str] = None,
    incluir_legacy: bool = False,
    regla_umbral: str = "v1",
    conn: Optional[sqlite3.Connection] = None,
    df_precios_previos: Optional[pd.DataFrame] = None,
) -> Dict:
    """
    Tabla Completa de Referencias Ingenuas (C-14).
    Compara en paralelo al Motor (v5-frozen) contra 5 referencias ingenuas sin look-ahead
    sobre exactamente las mismas filas resueltas:
      1) "Siempre alcista" (emite siempre ALCISTA)
      2) "Siempre bajista" (emite siempre BAJISTA)
      3) "Siempre lateral" (emite siempre LATERAL, signo n_dir = 0)
      4) "Continuacion del ultimo movimiento" (retorno previo del propio activo hasta t)
      5) "Direccion del IBEX" (retorno previo del IBEX / marea hasta t)

    Devuelve para cada una de las 6 filas:
      - 3 clases (v1 y v2): aciertos, tasa (%), delta vs motor (pp), inferencia C-13
      - Signo: n_direccionales, aciertos, tasa (%), delta vs motor (pp), inferencia C-13
    Tanto global como por horizonte (1D, 3D, 1S, 1M).
    """
    global _CACHE_PARQUET_PRECIOS
    own_conn = False
    if conn is None:
        own_conn = True
        conn = get_connection()
    hz_label = _normalizar_hz_label(horizonte)
    try:
        # Resolver df_precios_previos si no fue suministrado
        if df_precios_previos is None:
            if _CACHE_PARQUET_PRECIOS is not None:
                df_precios_previos = _CACHE_PARQUET_PRECIOS
            else:
                p_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "backtest_cache_6y.parquet")
                if not os.path.exists(p_path):
                    p_path = os.path.join("data", "backtest_cache_6y.parquet")
                if os.path.exists(p_path):
                    try:
                        _CACHE_PARQUET_PRECIOS = pd.read_parquet(p_path)
                        df_precios_previos = _CACHE_PARQUET_PRECIOS
                    except Exception:
                        df_precios_previos = None

        # Comprobar si existe tabla evaluaciones
        tiene_eval = False
        try:
            r = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evaluaciones'"
            ).fetchone()
            tiene_eval = bool(r)
        except Exception:
            tiene_eval = False

        where_conds = ["p.resuelta = 1"]
        params: List[Any] = []
        if not incluir_legacy:
            where_conds.append("p.modelo != 'legacy-pre-freeze'")

        if tiene_eval:
            sql = f"""
            SELECT 
                p.id, p.ticker, p.fecha_prediccion, p.horizonte, p.dias,
                p.senal, p.variacion_real, p.comp_momentum, p.marea, p.sigma,
                ep.variacion_pct AS var_v1, ep.clase_real AS clase_v1,
                ev2p.variacion_pct AS var_v2, ev2p.clase_real AS clase_v2, ev2p.umbral_aplicado AS umbral_v2
            FROM predicciones p
            LEFT JOIN evaluaciones ep ON ep.prediccion_id = p.id AND ep.regla = ?
            LEFT JOIN evaluaciones ev2p ON ev2p.prediccion_id = p.id AND ev2p.regla = ?
            WHERE {' AND '.join(where_conds)}
            ORDER BY p.id ASC
            """
            params.extend([REGLA_C10_PRECIO_CIERRE, REGLA_C12_VOL_PRECIO_CIERRE])
        else:
            sql = f"""
            SELECT 
                p.id, p.ticker, p.fecha_prediccion, p.horizonte, p.dias,
                p.senal, p.variacion_real, p.comp_momentum, p.marea, p.sigma,
                p.variacion_real AS var_v1, NULL AS clase_v1,
                p.variacion_real AS var_v2, NULL AS clase_v2, NULL AS umbral_v2
            FROM predicciones p
            WHERE {' AND '.join(where_conds)}
            ORDER BY p.id ASC
            """

        raw_rows = conn.execute(sql, tuple(params)).fetchall()
        if not raw_rows:
            return _tabla_referencias_ingenuas_vacia(hz_label)

        parsed_rows = []
        for row in raw_rows:
            (
                pid, tk, f_pred, hz, dias, senal, var_real, comp_mom, marea_val, sigma,
                var_v1_raw, clase_v1_raw, var_v2_raw, clase_v2_raw, umbral_v2_raw
            ) = row
            hz_norm = str(hz or "1D").upper().strip()
            dias_val = int(dias or _HORIZONTES_DEFAULT.get(hz_norm, 1))
            fp_str = str(f_pred or "")[:10]

            var_v1 = _f(var_v1_raw) if var_v1_raw is not None else _f(var_real)
            var_v2 = _f(var_v2_raw) if var_v2_raw is not None else var_v1

            u_v1 = float(UMBRALES_LATERAL.get(hz_norm, 0.8))
            clase_v1 = str(clase_v1_raw).upper().strip() if clase_v1_raw is not None else (
                _clasificar_variacion_c10(var_v1, u_v1) if var_v1 is not None else "LATERAL"
            )

            if umbral_v2_raw is not None:
                u_v2 = float(umbral_v2_raw)
            else:
                sig_val = _f(sigma)
                u_v2 = calcular_umbral_volatilidad_v2(
                    sigma=sig_val, dias=dias_val, horizonte=hz_norm
                ) if sig_val is not None else u_v1

            clase_v2 = str(clase_v2_raw).upper().strip() if clase_v2_raw is not None else (
                _clasificar_variacion_c10(var_v2, u_v2) if var_v2 is not None else "LATERAL"
            )

            # 0. Motor
            s_motor = str(senal or "").upper().strip()
            # 1. Siempre alcista
            s_alc = "ALCISTA"
            # 2. Siempre bajista
            s_baj = "BAJISTA"
            # 3. Siempre lateral
            s_lat = "LATERAL"

            # 4. Continuacion del ultimo movimiento (sin look-ahead: datos <= fp_str)
            ret_prev_tk = _extraer_retorno_previo_serie(df_precios_previos, tk, fp_str, dias_val)
            if ret_prev_tk is not None:
                s_cont_v1 = _clasificar_variacion_c10(ret_prev_tk, u_v1)
                s_cont_v2 = _clasificar_variacion_c10(ret_prev_tk, u_v2)
                s_cont_sig = "ALCISTA" if ret_prev_tk > 0.0 else ("BAJISTA" if ret_prev_tk < 0.0 else None)
            else:
                mom_f = _f(comp_mom) or 0.0
                u_mom = 0.15
                s_cont_v1 = "ALCISTA" if mom_f > u_mom else ("BAJISTA" if mom_f < -u_mom else "LATERAL")
                s_cont_v2 = s_cont_v1
                s_cont_sig = "ALCISTA" if mom_f > 0.0 else ("BAJISTA" if mom_f < 0.0 else None)

            # 5. Direccion del IBEX (sin look-ahead: datos <= fp_str)
            ret_prev_ib = _extraer_retorno_previo_serie(df_precios_previos, "^IBEX", fp_str, dias_val)
            if ret_prev_ib is not None:
                s_ib_v1 = _clasificar_variacion_c10(ret_prev_ib, u_v1)
                s_ib_v2 = _clasificar_variacion_c10(ret_prev_ib, u_v2)
                s_ib_sig = "ALCISTA" if ret_prev_ib > 0.0 else ("BAJISTA" if ret_prev_ib < 0.0 else None)
            else:
                if isinstance(marea_val, (int, float)):
                    m_num = float(marea_val)
                    s_ib_v1 = "ALCISTA" if m_num > 0.25 else ("BAJISTA" if m_num < -0.25 else "LATERAL")
                    s_ib_sig = "ALCISTA" if m_num > 0.0 else ("BAJISTA" if m_num < 0.0 else None)
                else:
                    m_str = str(marea_val or "").upper().strip()
                    if "ALCISTA" in m_str:
                        s_ib_v1 = "ALCISTA"
                        s_ib_sig = "ALCISTA"
                    elif "BAJISTA" in m_str:
                        s_ib_v1 = "BAJISTA"
                        s_ib_sig = "BAJISTA"
                    else:
                        s_ib_v1 = "LATERAL"
                        s_ib_sig = None
                s_ib_v2 = s_ib_v1

            parsed_rows.append({
                "id": pid,
                "ticker": tk,
                "fecha": fp_str,
                "horizonte": hz_norm,
                "dias": dias_val,
                "var_v1": var_v1,
                "clase_v1": clase_v1,
                "var_v2": var_v2,
                "clase_v2": clase_v2,
                "senales": {
                    "motor": {"3c_v1": s_motor, "3c_v2": s_motor, "signo": s_motor if s_motor in ("ALCISTA", "BAJISTA") else None},
                    "siempre_alcista": {"3c_v1": s_alc, "3c_v2": s_alc, "signo": s_alc},
                    "siempre_bajista": {"3c_v1": s_baj, "3c_v2": s_baj, "signo": s_baj},
                    "siempre_lateral": {"3c_v1": s_lat, "3c_v2": s_lat, "signo": None},
                    "continuacion_ultimo_movimiento": {"3c_v1": s_cont_v1, "3c_v2": s_cont_v2, "signo": s_cont_sig},
                    "direccion_ibex": {"3c_v1": s_ib_v1, "3c_v2": s_ib_v2, "signo": s_ib_sig},
                },
            })

        ESTRATEGIAS_DEF = [
            ("motor", "Motor (v5-frozen)"),
            ("siempre_alcista", "Siempre alcista"),
            ("siempre_bajista", "Siempre bajista"),
            ("siempre_lateral", "Siempre lateral"),
            ("continuacion_ultimo_movimiento", "Continuacion del ultimo movimiento"),
            ("direccion_ibex", "Direccion del IBEX"),
        ]

        def _computar_tabla_est(filas_subset: List[Dict], hz_label_sub: Optional[str]) -> List[Dict]:
            n_sub = len(filas_subset)
            fechas_sub = sorted({r["fecha"] for r in filas_subset if r["fecha"]})
            et_sub = _metadatos_muestra(
                n_sub, len(fechas_sub),
                fechas_sub[0] if fechas_sub else None,
                fechas_sub[-1] if fechas_sub else None,
                hz_label_sub
            )["etiqueta_muestra"]

            dias_hz_sub = max(int(_HORIZONTES_DEFAULT.get(hz_label_sub, 1)), 1)

            tabla_res: List[Dict] = []
            tasa_motor_3c = None
            tasa_motor_3c_v1 = None
            tasa_motor_3c_v2 = None
            tasa_motor_signo = None

            for code, nombre in ESTRATEGIAS_DEF:
                ac_3c_v1 = 0
                ac_3c_v2 = 0
                fechas_3c_v1: Dict[str, Dict] = {}
                fechas_3c_v2: Dict[str, Dict] = {}

                n_dir = 0
                ac_signo = 0
                fechas_signo: Dict[str, Dict] = {}

                for r in filas_subset:
                    fp = r["fecha"]
                    s_info = r["senales"][code]
                    s_v1 = s_info["3c_v1"]
                    s_v2 = s_info["3c_v2"]
                    s_sig = s_info["signo"]

                    # 3 clases v1
                    ok_v1 = 1 if s_v1 == r["clase_v1"] else 0
                    ac_3c_v1 += ok_v1
                    if fp not in fechas_3c_v1:
                        fechas_3c_v1[fp] = {"fecha": fp, "n": 0, "aciertos": 0}
                    fechas_3c_v1[fp]["n"] += 1
                    fechas_3c_v1[fp]["aciertos"] += ok_v1

                    # 3 clases v2
                    ok_v2 = 1 if s_v2 == r["clase_v2"] else 0
                    ac_3c_v2 += ok_v2
                    if fp not in fechas_3c_v2:
                        fechas_3c_v2[fp] = {"fecha": fp, "n": 0, "aciertos": 0}
                    fechas_3c_v2[fp]["n"] += 1
                    fechas_3c_v2[fp]["aciertos"] += ok_v2

                    # Signo
                    if s_sig in ("ALCISTA", "BAJISTA"):
                        n_dir += 1
                        var_val = r["var_v1"]
                        ok_sig = 1 if (
                            var_val is not None and (
                                (s_sig == "ALCISTA" and var_val > 0.0) or
                                (s_sig == "BAJISTA" and var_val < 0.0)
                            )
                        ) else 0
                        ac_signo += ok_sig
                        if fp not in fechas_signo:
                            fechas_signo[fp] = {"fecha": fp, "n": 0, "aciertos": 0}
                        fechas_signo[fp]["n"] += 1
                        fechas_signo[fp]["aciertos"] += ok_sig

                t_3c_v1 = _pct_c09(ac_3c_v1, n_sub)
                t_3c_v2 = _pct_c09(ac_3c_v2, n_sub)
                t_sig = _pct_c09(ac_signo, n_dir) if n_dir > 0 else None

                inf_3c_v1 = calcular_inferencia_por_fecha(
                    list(fechas_3c_v1.values()),
                    referencia_pct=33.33,
                    dias_horizonte=dias_hz_sub,
                )
                inf_3c_v2 = calcular_inferencia_por_fecha(
                    list(fechas_3c_v2.values()),
                    referencia_pct=33.33,
                    dias_horizonte=dias_hz_sub,
                )
                inf_sig = (
                    calcular_inferencia_por_fecha(
                        list(fechas_signo.values()),
                        referencia_pct=50.0,
                        dias_horizonte=dias_hz_sub,
                    )
                    if n_dir > 0
                    else {}
                )

                t_3c_activa = t_3c_v1 if regla_umbral == "v1" else t_3c_v2
                ac_3c_activa = ac_3c_v1 if regla_umbral == "v1" else ac_3c_v2
                inf_3c_activa = inf_3c_v1 if regla_umbral == "v1" else inf_3c_v2

                if code == "motor":
                    tasa_motor_3c = t_3c_activa
                    tasa_motor_3c_v1 = t_3c_v1
                    tasa_motor_3c_v2 = t_3c_v2
                    tasa_motor_signo = t_sig

                row_dict = {
                    "codigo": code,
                    "nombre": nombre,
                    "n_senales": n_sub,
                    "n_fechas": len(fechas_sub),
                    "etiqueta_muestra": et_sub,
                    # 3 clases regla activa
                    "aciertos_3clases": ac_3c_activa,
                    "tasa_acierto_3clases": t_3c_activa,
                    "inferencia_3clases": inf_3c_activa,
                    # 3 clases v1
                    "aciertos_3clases_v1": ac_3c_v1,
                    "tasa_acierto_3clases_v1": t_3c_v1,
                    "inferencia_3clases_v1": inf_3c_v1,
                    # 3 clases v2
                    "aciertos_3clases_v2": ac_3c_v2,
                    "tasa_acierto_3clases_v2": t_3c_v2,
                    "inferencia_3clases_v2": inf_3c_v2,
                    # Signo
                    "n_direccionales": n_dir,
                    "aciertos_signo": ac_signo,
                    "tasa_acierto_signo": t_sig,
                    "inferencia_signo": inf_sig,
                }
                tabla_res.append(row_dict)

            # Calcular diferenciales frente al motor (pp)
            for item in tabla_res:
                if item["codigo"] == "motor":
                    item["delta_vs_motor_3clases_pp"] = 0.0
                    item["delta_vs_motor_3clases_v1_pp"] = 0.0
                    item["delta_vs_motor_3clases_v2_pp"] = 0.0
                    item["delta_vs_motor_signo_pp"] = 0.0
                else:
                    t_item_3c = item["tasa_acierto_3clases"]
                    item["delta_vs_motor_3clases_pp"] = (
                        round(t_item_3c - tasa_motor_3c, 2)
                        if (t_item_3c is not None and tasa_motor_3c is not None)
                        else None
                    )
                    t_item_v1 = item["tasa_acierto_3clases_v1"]
                    item["delta_vs_motor_3clases_v1_pp"] = (
                        round(t_item_v1 - tasa_motor_3c_v1, 2)
                        if (t_item_v1 is not None and tasa_motor_3c_v1 is not None)
                        else None
                    )
                    t_item_v2 = item["tasa_acierto_3clases_v2"]
                    item["delta_vs_motor_3clases_v2_pp"] = (
                        round(t_item_v2 - tasa_motor_3c_v2, 2)
                        if (t_item_v2 is not None and tasa_motor_3c_v2 is not None)
                        else None
                    )
                    t_item_sig = item["tasa_acierto_signo"]
                    item["delta_vs_motor_signo_pp"] = (
                        round(t_item_sig - tasa_motor_signo, 2)
                        if (t_item_sig is not None and tasa_motor_signo is not None)
                        else None
                    )

            return tabla_res

        hz_req = hz_label if hz_label != "Todos" else None
        filas_glob = [r for r in parsed_rows if hz_req is None or r["horizonte"] == hz_req]

        tabla_global = _computar_tabla_est(filas_glob, hz_label)

        por_hz = {}
        for hz_c in ("1D", "3D", "1S", "1M"):
            filas_h = [r for r in parsed_rows if r["horizonte"] == hz_c]
            por_hz[hz_c] = _computar_tabla_est(filas_h, hz_c)

        t_motor_3c = next((x["tasa_acierto_3clases"] for x in tabla_global if x["codigo"] == "motor"), 0.0) or 0.0
        t_motor_sig = next((x["tasa_acierto_signo"] for x in tabla_global if x["codigo"] == "motor"), 0.0) or 0.0

        mejores_3c = sorted(
            tabla_global,
            key=lambda x: (x["tasa_acierto_3clases"] is not None, x["tasa_acierto_3clases"] or -999.0),
            reverse=True,
        )
        mejor_est_3c = mejores_3c[0]["codigo"] if mejores_3c else "motor"

        estrats_con_signo = [x for x in tabla_global if x["tasa_acierto_signo"] is not None]
        mejores_sig = (
            sorted(estrats_con_signo, key=lambda x: x["tasa_acierto_signo"], reverse=True)
            if estrats_con_signo
            else []
        )
        mejor_est_sig = mejores_sig[0]["codigo"] if mejores_sig else "motor"

        max_otras_3c = max(
            [x["tasa_acierto_3clases"] for x in tabla_global if x["codigo"] != "motor" and x["tasa_acierto_3clases"] is not None]
            or [0.0]
        )
        max_otras_sig = max(
            [x["tasa_acierto_signo"] for x in tabla_global if x["codigo"] != "motor" and x["tasa_acierto_signo"] is not None]
            or [0.0]
        )

        n_tot = len(filas_glob)
        f_todas = sorted({r["fecha"] for r in filas_glob if r["fecha"]})
        et_tot = _metadatos_muestra(
            n_tot, len(f_todas), f_todas[0] if f_todas else None, f_todas[-1] if f_todas else None, hz_label
        )["etiqueta_muestra"]

        return {
            "etiqueta_muestra": et_tot,
            "n_senales": n_tot,
            "n_fechas": len(f_todas),
            "regla_umbral": regla_umbral,
            "horizonte": hz_label,
            "tabla": tabla_global,
            "por_horizonte": por_hz,
            "mejor_estrategia_3clases": mejor_est_3c,
            "mejor_estrategia_signo": mejor_est_sig,
            "motor_supera_todas_3clases": bool(t_motor_3c >= max_otras_3c),
            "motor_supera_todas_signo": bool(t_motor_sig >= max_otras_sig),
        }
    finally:
        if own_conn:
            conn.close()
