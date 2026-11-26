"""
Arnes Walk-Forward Honesto y Reproducible de Experimentos (Paso C-15)
=====================================================================
Evalua cualquier configuracion experimental (ConfigExperimentoMotor) sobre un
panel historico OHLCV aplicando:
  - Cero look-ahead estricto por construccion (recorte index <= fecha_prediccion).
  - Filtro de elegibilidad de universo y exclusion de valores zombi (C-27).
  - Resolucion en fecha_objetivo exacta (Cierre S -> Cierre obj, C-10) y desde
    apertura operable S+1 (Apertura S+1 -> Cierre obj, C-11).
  - Clasificacion bajo Regla v1 (umbrales fijos) y Regla v2 (zona muerta escalada
    por volatilidad k * sigma * sqrt(dias), C-12).
  - Separacion entre Acierto en 3 clases y Acierto de signo con etiqueta canonica
    de muestra (C-09).
  - Inferencia honesta agregada por bloques de fecha de emision (N_efectivo e
    intervalos de confianza con correccion Newey-West / Bartlett, C-13).
  - Comparativa contra las 5 referencias ingenuas sin look-ahead (C-14).
  - Diagnostico de monotonia de confianza (Spearman y terciles) y comparador
    pareado challenger vs baseline por fecha de emision.

100% ASCII, determinista y sin efectos secundarios en base de datos ni red.
"""

from dataclasses import dataclass, field
from datetime import date
import copy
import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from database.predicciones import (
    REF_AZAR_SIGNO_PCT,
    _calcular_acierto_signo_c10,
    _calcular_sigma_cierre_hasta_emision_pct,
    _clasificar_variacion_c10,
    _determinar_fecha_entrada_operable,
    _extraer_precio_en_fecha,
    _extraer_retorno_previo_serie,
    calcular_inferencia_por_fecha,
    calcular_umbral_volatilidad_v2,
    formatear_etiqueta_muestra,
)
from services.emision import (
    _extraer_sub_raw_ticker,
    _formatear_fechas_indice,
    evaluar_elegibilidad_ticker,
)
import services.signal_engine as sig_eng
from utils.market_calendar import fecha_habil_bme

PESOS_HORIZONTE: Dict[str, Dict[str, float]] = copy.deepcopy(sig_eng.PESOS_TRANSICION)
PESOS_TENDENCIA_DEFAULT: Dict[str, Dict[str, float]] = copy.deepcopy(sig_eng.PESOS_TENDENCIA)
PESOS_RANGO_DEFAULT: Dict[str, Dict[str, float]] = copy.deepcopy(sig_eng.PESOS_RANGO)
UMBRALES_LATERAL_DEFAULT: Dict[str, float] = copy.deepcopy(sig_eng.UMBRALES_LATERAL)
MULT_CONVICCION_DEFAULT: Dict[str, float] = {"1D": 1.0, "3D": 1.0, "1S": 1.0, "1M": 1.0}
PESO_TILT_DEFAULT: Dict[str, float] = {
    "1D": 0.0,
    "3D": 0.0,
    "1S": 0.0,
    "1M": float(sig_eng.PESO_FUNDAMENTAL_1M),
}
VOL_BOOST_POR_HZ_DEFAULT: Dict[str, float] = {
    "1D": float(sig_eng.VOL_CONFIRM_BOOST),
    "3D": float(sig_eng.VOL_CONFIRM_BOOST),
    "1S": float(sig_eng.VOL_CONFIRM_BOOST),
    "1M": float(sig_eng.VOL_CONFIRM_BOOST),
}
VOL_DAMP_POR_HZ_DEFAULT: Dict[str, float] = {
    "1D": float(sig_eng.VOL_NO_CONFIRM_DAMP),
    "3D": float(sig_eng.VOL_NO_CONFIRM_DAMP),
    "1S": float(sig_eng.VOL_NO_CONFIRM_DAMP),
    "1M": float(sig_eng.VOL_NO_CONFIRM_DAMP),
}
AMORTIGUACION_RVOL_PICO_DEFAULT: Dict[str, float] = {
    "1D": 1.0,
    "3D": 1.0,
    "1S": 1.0,
    "1M": 1.0,
}

# Pesos y parametros exactos de C-16 (fijados exclusivamente en ventana Train 2025):
# - 1D: reversion suave sobre extremos de momentum y RSI (|comp| >= 0.80), mayor peso
#   relativo a MACD (0.40) y tendencia moderada (0.30), y peso de tilt fundamental 0.30.
# - 3D: horizonte tactico intermedio con MACD (0.30), tendencia (0.28), momentum (0.30),
#   RSI (0.12) y peso de tilt fundamental 0.05.
# - 1S y 1M: mantienen continuacion tendencial dinamica por ADX de v5-frozen.
PESOS_HORIZONTE_C16: Dict[str, Dict[str, float]] = {
    "1D": {"rsi": 0.15, "macd": 0.40, "tendencia": 0.30, "momentum": 0.15},
    "3D": {"rsi": 0.12, "macd": 0.30, "tendencia": 0.28, "momentum": 0.30},
    "1S": copy.deepcopy(sig_eng.PESOS_TRANSICION["1S"]),
    "1M": copy.deepcopy(sig_eng.PESOS_TRANSICION["1M"]),
}
PESO_TILT_POR_HZ_C16: Dict[str, float] = {
    "1D": 0.30,
    "3D": 0.05,
    "1S": 0.0,
    "1M": float(sig_eng.PESO_FUNDAMENTAL_1M),
}
VOL_BOOST_POR_HZ_C16: Dict[str, float] = {
    "1D": 1.00,
    "3D": 1.00,
    "1S": float(sig_eng.VOL_CONFIRM_BOOST),
    "1M": float(sig_eng.VOL_CONFIRM_BOOST),
}
VOL_DAMP_POR_HZ_C16: Dict[str, float] = {
    "1D": 1.00,
    "3D": 0.90,
    "1S": float(sig_eng.VOL_NO_CONFIRM_DAMP),
    "1M": float(sig_eng.VOL_NO_CONFIRM_DAMP),
}
AMORTIGUACION_RVOL_PICO_C16: Dict[str, float] = {
    "1D": 0.75,
    "3D": 0.90,
    "1S": 1.00,
    "1M": 1.00,
}

# Multiplicadores de conviccion direccional por horizonte calibrados en Train 2025 (C-17)
# sobre challenger_c16, preservando 1M en 1.0 para no alterar el filtro macro Triple Screen:
# - Moderado (~20%-30% de senales direccionales): mejora simultanea de 3 clases y signo
#   en los tres horizontes (1D, 3D, 1S) con alta retencion de senales direccionales.
# - Optimo C-17 (~15%-20% de senales direccionales): frontera de Pareto en Train 2025 que
#   supera a "Siempre lateral" (v1) en 1D, 3D y 1S a la vez sin vaciar las direccionales.
MULT_CONVICCION_C17_MODERADO: Dict[str, float] = {
    "1D": 1.25,
    "3D": 1.25,
    "1S": 1.25,
    "1M": 1.00,
}
MULT_CONVICCION_C17: Dict[str, float] = {
    "1D": 1.35,
    "3D": 2.00,
    "1S": 1.75,
    "1M": 1.00,
}

# Tabla estatica de calibracion empirica 2D (Paso C-18b), fijada exclusivamente en
# la ventana TRAIN_2025 (255 sesiones, 2025-01-02..2025-12-31, 80673 senales resueltas
# de challenger_c17 sobre data/backtest_cache_6y.parquet; cero look-ahead):
# - Senales direccionales (DIR: ALCISTA/BAJISTA): tasa de acierto de signo C-10 (%)
#   en 2 bins de abs(pct_estimado)/umbral_v1 (Cerca < 1.50, Lejos >= 1.50) x
#   2 bins de RVOL (Bajo < 0.98, Alto >= 0.98). Las celdas con n < 5 (Cerca en 3D/1S
#   por el filtro de conviccion C-17) usan fallback marginal de RVOL del mismo horizonte.
# - Senales LATERAL: tasa de acierto en 3 clases v1 C-10 (%) en 2 bins de
#   abs(pct_estimado)/umbral_v1 (Centro < 0.50, Borde >= 0.50) x 2 bins de RVOL
#   (Bajo < 0.98, Alto >= 0.98).
UMBRAL_RVOL_BIN_C18: float = 0.98
UMBRAL_RATIO_DIR_BIN_C18: float = 1.50
UMBRAL_RATIO_LAT_BIN_C18: float = 0.50

TABLA_CALIBRACION_C18_TRAIN: Dict[str, Dict[str, Dict[Tuple[str, str], float]]] = {
    "DIR": {
        "1D": {
            ("Cerca", "Bajo"): 49.05,
            ("Cerca", "Alto"): 50.00,
            ("Lejos", "Bajo"): 49.67,
            ("Lejos", "Alto"): 53.89,
        },
        "3D": {
            ("Cerca", "Bajo"): 47.67,
            ("Cerca", "Alto"): 46.79,
            ("Lejos", "Bajo"): 47.67,
            ("Lejos", "Alto"): 46.79,
        },
        "1S": {
            ("Cerca", "Bajo"): 47.59,
            ("Cerca", "Alto"): 46.81,
            ("Lejos", "Bajo"): 47.59,
            ("Lejos", "Alto"): 46.81,
        },
    },
    "LATERAL": {
        "1D": {
            ("Centro", "Bajo"): 52.73,
            ("Centro", "Alto"): 47.85,
            ("Borde", "Bajo"): 45.10,
            ("Borde", "Alto"): 36.55,
        },
        "3D": {
            ("Centro", "Bajo"): 47.05,
            ("Centro", "Alto"): 45.88,
            ("Borde", "Bajo"): 39.80,
            ("Borde", "Alto"): 38.85,
        },
        "1S": {
            ("Centro", "Bajo"): 51.82,
            ("Centro", "Alto"): 49.74,
            ("Borde", "Bajo"): 43.26,
            ("Borde", "Alto"): 43.17,
        },
    },
}

# Parametros de filtro macro y fuerza relativa (Paso C-19), fijados exclusivamente
# en la ventana TRAIN_2025 (255 sesiones, 2025-01-02..2025-12-31, 80673 senales
# resueltas de challenger_c18b sobre data/backtest_cache_6y.parquet; cero look-ahead):
# - filtro_marea_estricto=True: bloquea ALCISTA bajo marea BAJISTA y BAJISTA bajo
#   marea ALCISTA, forzandolas a LATERAL.
# - umbral_rs_ibex=3.0: exige rs_ibex > -3.0 para ALCISTA y rs_ibex < 3.0 para BAJISTA.
FILTRO_MAREA_ESTRICTO_C19: bool = True
UMBRAL_RS_IBEX_C19: float = 3.0


def calcular_confianza_v6_calibrada(
    horizonte: str,
    senal: str,
    pct_estimado: float,
    umbral_v1: Optional[float],
    rvol: float,
    tabla: Optional[Dict[str, Dict[str, Dict[Tuple[str, str], float]]]] = None,
) -> Tuple[float, float, str, Tuple[str, str]]:
    """
    Calcula la confianza empirica calibrada (modo_confianza='v6_calibrada') mediante
    busqueda determinista en la matriz estatica 2D (cero look-ahead):
      - Clase emitida: 'LATERAL' si senal == 'LATERAL', en caso contrario 'DIR'.
      - Bin de magnitud relativa abs(pct_estimado) / umbral_v1:
          * DIR: 'Cerca' (< 1.50) vs 'Lejos' (>= 1.50)
          * LATERAL: 'Centro' (< 0.50) vs 'Borde' (>= 0.50)
      - Bin de volumen relativo RVOL: 'Bajo' (< 0.98) vs 'Alto' (>= 0.98).
    Escala linealmente la probabilidad empirica de la celda desde [min_prob, max_prob]
    de la tabla al rango visual [50.0, 95.0] (acotada en [50.0, 99.0]) preservando el
    orden estrictamente creciente.

    Retorna (confianza_visual, prob_empirica_pct, clase_bin, (bin_dist, bin_rvol)).
    """
    tabla_eff = tabla if tabla is not None else TABLA_CALIBRACION_C18_TRAIN
    hz_norm = str(horizonte or "1D").upper().strip()
    sen_norm = str(senal or sig_eng.SENAL_LATERAL).upper().strip()
    clase_bin = "LATERAL" if sen_norm == sig_eng.SENAL_LATERAL else "DIR"

    if umbral_v1 is not None and float(umbral_v1) > 1e-9:
        u_val = float(umbral_v1)
    else:
        u_val = float(UMBRALES_LATERAL_DEFAULT.get(hz_norm, 1.0))

    pct_val = float(pct_estimado) if (pct_estimado is not None and math.isfinite(float(pct_estimado))) else 0.0
    ratio = abs(pct_val) / u_val

    rvol_val = float(rvol) if (rvol is not None and math.isfinite(float(rvol))) else 1.0
    bin_rvol = "Bajo" if rvol_val < UMBRAL_RVOL_BIN_C18 - 1e-12 else "Alto"
    if clase_bin == "LATERAL":
        bin_dist = "Centro" if ratio < UMBRAL_RATIO_LAT_BIN_C18 - 1e-12 else "Borde"
    else:
        bin_dist = "Cerca" if ratio < UMBRAL_RATIO_DIR_BIN_C18 - 1e-12 else "Lejos"

    todas_probs = [
        float(p)
        for cls_d in tabla_eff.values()
        for hz_d in cls_d.values()
        for p in hz_d.values()
    ]
    min_prob = min(todas_probs) if todas_probs else 35.0
    max_prob = max(todas_probs) if todas_probs else 65.0

    cls_map = tabla_eff.get(clase_bin, {})
    hz_map = cls_map.get(hz_norm)
    if hz_map is None:
        hz_map = cls_map.get("1S") or (next(iter(cls_map.values())) if cls_map else {})

    celda_key = (bin_dist, bin_rvol)
    if celda_key in hz_map:
        prob = float(hz_map[celda_key])
    elif hz_map:
        prob = float(sum(float(v) for v in hz_map.values()) / len(hz_map))
    else:
        prob = 50.0

    if max_prob > min_prob + 1e-12:
        conf_raw = 50.0 + 45.0 * (prob - min_prob) / (max_prob - min_prob)
    else:
        conf_raw = 72.5
    conf_visual = round(float(np.clip(conf_raw, 50.0, 99.0)), 2)
    return conf_visual, round(prob, 2), clase_bin, celda_key


def _copiar_pesos_default() -> Dict[str, Dict[str, float]]:
    return copy.deepcopy(PESOS_HORIZONTE)


def _copiar_umbrales_default() -> Dict[str, float]:
    return copy.deepcopy(UMBRALES_LATERAL_DEFAULT)


def _copiar_mult_default() -> Dict[str, float]:
    return copy.deepcopy(MULT_CONVICCION_DEFAULT)


def _copiar_peso_tilt_default() -> Dict[str, float]:
    return copy.deepcopy(PESO_TILT_DEFAULT)


def _copiar_amort_rvol_default() -> Dict[str, float]:
    return copy.deepcopy(AMORTIGUACION_RVOL_PICO_DEFAULT)


@dataclass
class ConfigExperimentoMotor:
    """
    Configuracion parametrica e inmutable en ejecucion para experimentos walk-forward
    de los pasos C-16 a C-20, sin alterar las constantes globales de signal_engine.
    """

    nombre_modelo: str = "v5-frozen-wf"
    pesos_horizonte: Dict[str, Dict[str, float]] = field(default_factory=_copiar_pesos_default)
    pesos_tendencia: Optional[Dict[str, Dict[str, float]]] = None
    pesos_rango: Optional[Dict[str, Dict[str, float]]] = None
    peso_tilt_por_hz: Dict[str, float] = field(default_factory=_copiar_peso_tilt_default)
    reversion_sobreextension_1d: float = 0.0
    umbral_sobreextension_1d: float = 0.80
    umbrales_lateral_v1: Dict[str, float] = field(default_factory=_copiar_umbrales_default)
    multiplicador_conviccion_por_hz: Dict[str, float] = field(default_factory=_copiar_mult_default)
    vol_confirm_boost: float = sig_eng.VOL_CONFIRM_BOOST
    vol_no_confirm_damp: float = sig_eng.VOL_NO_CONFIRM_DAMP
    vol_confirm_boost_por_hz: Optional[Dict[str, float]] = None
    vol_no_confirm_damp_por_hz: Optional[Dict[str, float]] = None
    amortiguacion_rvol_pico_por_hz: Dict[str, float] = field(default_factory=_copiar_amort_rvol_default)
    umbral_rvol_pico: float = 0.98
    escala_1d_cierre_a_apertura: float = 1.0
    penalizar_bajista_1d_en_marea_bajista: bool = False
    modo_confianza: str = "v5_original"
    k_sigma_v2: float = 0.50
    tabla_calibracion_confianza: Optional[
        Dict[str, Dict[str, Dict[Tuple[str, str], float]]]
    ] = None
    filtro_marea_estricto: bool = False
    umbral_rs_ibex: float = 0.0

    def calcular_hash(self) -> str:
        """Atajo de instancia para calcular_hash_config(self)."""
        return calcular_hash_config(self)


def _es_cfg_c16_por_defecto(cfg: ConfigExperimentoMotor) -> bool:
    """Comprueba si los campos nuevos de C-16 conservan sus valores neutros por defecto."""
    if cfg.pesos_tendencia is not None or cfg.pesos_rango is not None:
        return False
    if cfg.vol_confirm_boost_por_hz is not None or cfg.vol_no_confirm_damp_por_hz is not None:
        return False
    if abs(float(cfg.reversion_sobreextension_1d)) > 1e-9:
        return False
    for hz, v_def in PESO_TILT_DEFAULT.items():
        if abs(float((cfg.peso_tilt_por_hz or {}).get(hz, v_def)) - float(v_def)) > 1e-6:
            return False
    for hz, v_def in AMORTIGUACION_RVOL_PICO_DEFAULT.items():
        if abs(float((cfg.amortiguacion_rvol_pico_por_hz or {}).get(hz, v_def)) - float(v_def)) > 1e-6:
            return False
    return True


def calcular_hash_config(cfg: ConfigExperimentoMotor) -> str:
    """
    Calcula el hash SHA-256 determinista (hexadecimal de 64 caracteres) de una
    instancia de ConfigExperimentoMotor.
    """
    if cfg is None:
        cfg = ConfigExperimentoMotor()
    payload: Dict[str, Any] = {
        "nombre_modelo": str(cfg.nombre_modelo),
        "pesos_horizonte": {
            str(hz): {str(k): round(float(v), 6) for k, v in sorted(d.items())}
            for hz, d in sorted((cfg.pesos_horizonte or {}).items())
        },
        "umbrales_lateral_v1": {
            str(hz): round(float(v), 6)
            for hz, v in sorted((cfg.umbrales_lateral_v1 or {}).items())
        },
        "multiplicador_conviccion_por_hz": {
            str(hz): round(float(v), 6)
            for hz, v in sorted((cfg.multiplicador_conviccion_por_hz or {}).items())
        },
        "vol_confirm_boost": round(float(cfg.vol_confirm_boost), 6),
        "vol_no_confirm_damp": round(float(cfg.vol_no_confirm_damp), 6),
        "escala_1d_cierre_a_apertura": round(float(cfg.escala_1d_cierre_a_apertura), 6),
        "penalizar_bajista_1d_en_marea_bajista": bool(cfg.penalizar_bajista_1d_en_marea_bajista),
        "modo_confianza": str(cfg.modo_confianza),
        "k_sigma_v2": round(float(cfg.k_sigma_v2), 6),
    }
    if not _es_cfg_c16_por_defecto(cfg):
        payload["peso_tilt_por_hz"] = {
            str(hz): round(float(v), 6)
            for hz, v in sorted((cfg.peso_tilt_por_hz or {}).items())
        }
        payload["reversion_sobreextension_1d"] = round(float(cfg.reversion_sobreextension_1d), 6)
        payload["umbral_sobreextension_1d"] = round(float(cfg.umbral_sobreextension_1d), 6)
        payload["vol_confirm_boost_por_hz"] = (
            {
                str(hz): round(float(v), 6)
                for hz, v in sorted(cfg.vol_confirm_boost_por_hz.items())
            }
            if cfg.vol_confirm_boost_por_hz is not None
            else None
        )
        payload["vol_no_confirm_damp_por_hz"] = (
            {
                str(hz): round(float(v), 6)
                for hz, v in sorted(cfg.vol_no_confirm_damp_por_hz.items())
            }
            if cfg.vol_no_confirm_damp_por_hz is not None
            else None
        )
        payload["amortiguacion_rvol_pico_por_hz"] = {
            str(hz): round(float(v), 6)
            for hz, v in sorted((cfg.amortiguacion_rvol_pico_por_hz or {}).items())
        }
        payload["umbral_rvol_pico"] = round(float(cfg.umbral_rvol_pico), 6)
        if cfg.pesos_tendencia is not None:
            payload["pesos_tendencia"] = {
                str(hz): {str(k): round(float(v), 6) for k, v in sorted(d.items())}
                for hz, d in sorted(cfg.pesos_tendencia.items())
            }
        if cfg.pesos_rango is not None:
            payload["pesos_rango"] = {
                str(hz): {str(k): round(float(v), 6) for k, v in sorted(d.items())}
                for hz, d in sorted(cfg.pesos_rango.items())
            }
    if cfg.tabla_calibracion_confianza is not None:
        payload["tabla_calibracion_confianza"] = {
            str(cls_k): {
                str(hz): {
                    f"{str(b_k[0])}|{str(b_k[1])}": round(float(p_val), 6)
                    for b_k, p_val in sorted(
                        hz_d.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))
                    )
                }
                for hz, hz_d in sorted(cls_d.items())
            }
            for cls_k, cls_d in sorted(cfg.tabla_calibracion_confianza.items())
        }
    if bool(cfg.filtro_marea_estricto) or float(cfg.umbral_rs_ibex or 0.0) > 0.0:
        payload["filtro_marea_estricto"] = bool(cfg.filtro_marea_estricto)
        payload["umbral_rs_ibex"] = round(float(cfg.umbral_rs_ibex or 0.0), 6)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _es_pesos_por_defecto(pesos: Optional[Dict[str, Dict[str, float]]]) -> bool:
    """Comprueba si el diccionario de pesos coincide con el por defecto de v5-frozen."""
    if not pesos:
        return True
    for hz, d_def in PESOS_HORIZONTE.items():
        if hz not in pesos:
            return False
        d_cur = pesos[hz]
        for k, v_def in d_def.items():
            if abs(float(d_cur.get(k, -999.0)) - float(v_def)) > 1e-6:
                return False
    return True


def _pesos_para_horizonte(
    horizonte: str, adx: float, config: ConfigExperimentoMotor
) -> Dict[str, float]:
    """
    Si config conserva los valores por defecto, aplica exactamente los pesos dinamicos
    por ADX de v5-frozen (_pesos_dinamicos). Si el experimento define pesos_tendencia /
    pesos_rango o personaliza pesos_horizonte para ese horizonte, usa los pesos
    experimentales.
    """
    if config.pesos_tendencia is not None or config.pesos_rango is not None:
        t_map = config.pesos_tendencia or PESOS_TENDENCIA_DEFAULT
        r_map = config.pesos_rango or PESOS_RANGO_DEFAULT
        if horizonte in t_map and horizonte in r_map:
            w_trend = t_map[horizonte]
            w_rango = r_map[horizonte]
            if adx >= sig_eng.ADX_TENDENCIA:
                return {k: float(w_trend.get(k, 0.0)) for k in ("rsi", "macd", "tendencia", "momentum")}
            elif adx <= sig_eng.ADX_RANGO:
                return {k: float(w_rango.get(k, 0.0)) for k in ("rsi", "macd", "tendencia", "momentum")}
            else:
                alpha = (adx - sig_eng.ADX_RANGO) / (sig_eng.ADX_TENDENCIA - sig_eng.ADX_RANGO)
                return {
                    k: round(float(w_rango.get(k, 0.0)) * (1.0 - alpha) + float(w_trend.get(k, 0.0)) * alpha, 4)
                    for k in ("rsi", "macd", "tendencia", "momentum")
                }

    if _es_pesos_por_defecto(config.pesos_horizonte):
        return sig_eng._pesos_dinamicos(horizonte, adx)
    custom = (config.pesos_horizonte or {}).get(horizonte)
    if not custom:
        return sig_eng._pesos_dinamicos(horizonte, adx)
    # Comprobar si este horizonte concreto fue modificado respecto a PESOS_HORIZONTE
    d_def = PESOS_HORIZONTE.get(horizonte, {})
    modificado = any(
        abs(float(custom.get(k, 0.0)) - float(d_def.get(k, 0.0))) > 1e-6
        for k in ("rsi", "macd", "tendencia", "momentum")
    )
    if not modificado:
        return sig_eng._pesos_dinamicos(horizonte, adx)
    return {
        "rsi": float(custom.get("rsi", 0.0)),
        "macd": float(custom.get("macd", 0.0)),
        "tendencia": float(custom.get("tendencia", 0.0)),
        "momentum": float(custom.get("momentum", 0.0)),
    }


def _confirmar_volumen_experimental(
    close: Optional[pd.Series],
    volume: Optional[pd.Series],
    score_dir: float,
    config: ConfigExperimentoMotor,
    horizonte: Optional[str] = None,
) -> float:
    """
    Calcula el factor de confirmacion de volumen usando los parametros de config
    (vol_confirm_boost, vol_no_confirm_damp o sus diccionarios por horizonte, mas
    amortiguacion de picos RVOL > umbral_rvol_pico) sin mutar el estado global de
    signal_engine. Cuando los parametros coinciden con VOL_CONFIRM_BOOST y
    VOL_NO_CONFIRM_DAMP, reproduce exactamente sig_eng._confirmar_volumen.
    """
    if close is None or volume is None or len(close) < 12 or len(volume) < 12:
        return 1.0

    hz_norm = (horizonte or "").upper().strip()
    if config.vol_confirm_boost_por_hz is not None and hz_norm in config.vol_confirm_boost_por_hz:
        boost = float(config.vol_confirm_boost_por_hz[hz_norm])
    else:
        boost = float(config.vol_confirm_boost)

    if config.vol_no_confirm_damp_por_hz is not None and hz_norm in config.vol_no_confirm_damp_por_hz:
        damp = float(config.vol_no_confirm_damp_por_hz[hz_norm])
    else:
        damp = float(config.vol_no_confirm_damp)

    obv_s = sig_eng._obv(close, volume)
    rvol = sig_eng._rvol(volume, 20)

    obv_tail = obv_s.iloc[-10:]
    if len(obv_tail) < 5:
        return 1.0
    x = np.arange(len(obv_tail), dtype=float)
    y = obv_tail.values.astype(float)
    vol_mean = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else 1.0
    if vol_mean <= 0:
        vol_mean = 1.0
    pendiente = float(np.polyfit(x, y, 1)[0]) / vol_mean

    if abs(score_dir) < 0.15:
        return 1.0

    misma_dir = (pendiente > 0 and score_dir > 0) or (pendiente < 0 and score_dir < 0)
    vol_factor = 1.0

    if misma_dir:
        if rvol >= 1.5:
            vol_factor = boost
        elif rvol >= 1.0:
            vol_factor = 1.0 + 0.05 * (rvol - 1.0) if abs(boost - 1.0) > 1e-6 else 1.0
        else:
            vol_factor = 1.0
    else:
        if rvol >= 1.5:
            vol_factor = damp
        elif rvol >= 1.0:
            vol_factor = 0.85 if abs(damp - 1.0) > 1e-6 else 1.0
        else:
            vol_factor = 0.90 if abs(damp - 1.0) > 1e-6 else 1.0

    lo = min(damp, boost)
    hi = max(damp, boost)
    vol_factor = float(np.clip(vol_factor, lo, hi))

    if hz_norm and config.amortiguacion_rvol_pico_por_hz:
        damp_pico = float(config.amortiguacion_rvol_pico_por_hz.get(hz_norm, 1.0))
        if rvol > float(config.umbral_rvol_pico) and abs(damp_pico - 1.0) > 1e-6:
            vol_factor *= damp_pico

    return float(vol_factor)


def _calcular_senal_experimental(
    comp: Dict[str, float],
    vol_d: float,
    horizonte: str,
    config: ConfigExperimentoMotor,
    tilt_fundamental: float = 0.0,
    adx: float = 22.0,
    vol_confirm: float = 1.0,
    sr_bias: float = 0.0,
    bb_squeeze: bool = False,
    atr_d: float = 0.0,
    marea_score: float = 0.0,
    rs_score: float = 0.0,
    sentiment_score: float = 0.0,
    rvol: float = 1.0,
) -> Dict[str, Any]:
    """
    Calcula la senal de un horizonte aplicando las palancas de ConfigExperimentoMotor.
    Con ConfigExperimentoMotor() por defecto reproduce bit a bit sig_eng.calcular_senal.
    """
    hz_norm = (horizonte or "1D").upper().strip()
    dias = sig_eng.HORIZONTES[hz_norm]

    w = _pesos_para_horizonte(hz_norm, adx, config)
    c_rsi = float(comp["rsi"])
    c_macd = float(comp["macd"])
    c_tend = float(comp["tendencia"])
    c_mom = float(comp["momentum"])

    rev_1d = float(getattr(config, "reversion_sobreextension_1d", 0.0) or 0.0)
    u_rev_1d = float(getattr(config, "umbral_sobreextension_1d", 0.80) or 0.80)
    if hz_norm == "1D" and rev_1d > 1e-9:
        if abs(c_mom) >= u_rev_1d:
            c_mom = -rev_1d * c_mom
        if abs(c_rsi) >= u_rev_1d:
            c_rsi = -rev_1d * c_rsi

    s = (
        w["rsi"] * c_rsi
        + w["macd"] * c_macd
        + w["tendencia"] * c_tend
        + w["momentum"] * c_mom
    )

    tilt_map = getattr(config, "peso_tilt_por_hz", None) or PESO_TILT_DEFAULT
    w_tilt = float(
        tilt_map.get(
            hz_norm,
            float(sig_eng.PESO_FUNDAMENTAL_1M) if hz_norm == "1M" else 0.0,
        )
    )
    if w_tilt > 1e-9 and abs(tilt_fundamental) > 0.01:
        s = s * (1.0 - w_tilt) + tilt_fundamental * w_tilt

    s_vol = s * vol_confirm
    s_final = s_vol + sr_bias

    if marea_score <= -0.25:
        if s_final > 0:
            s_final *= 1.0 + 0.15 * marea_score
        min_consenso_alcista = 4
        min_consenso_bajista = sig_eng.MIN_CONSENSO
        if (
            config.penalizar_bajista_1d_en_marea_bajista
            and hz_norm == "1D"
            and s_final < 0
        ):
            s_final *= 1.0 + 0.15 * marea_score
            min_consenso_bajista = 4
    elif marea_score >= 0.25:
        if s_final < 0:
            s_final *= 1.0 - 0.15 * marea_score
        min_consenso_alcista = sig_eng.MIN_CONSENSO
        min_consenso_bajista = 4
    else:
        min_consenso_alcista = sig_eng.MIN_CONSENSO
        min_consenso_bajista = sig_eng.MIN_CONSENSO

    if hz_norm in ("3D", "1S") and abs(rs_score) >= 0.20:
        s_final += 0.15 * rs_score

    if abs(sentiment_score) >= 0.25:
        if hz_norm == "1D":
            s_final += 0.15 * sentiment_score
        elif hz_norm == "3D":
            s_final += 0.10 * sentiment_score

    if hz_norm == "1D" and abs(float(config.escala_1d_cierre_a_apertura) - 1.0) > 1e-9:
        s_final *= float(config.escala_1d_cierre_a_apertura)

    volatilidad = atr_d if atr_d > 0 else vol_d
    banda_pct = volatilidad * np.sqrt(dias) * 100.0
    pct = (s_final / 2.0) * banda_pct
    pct = float(np.clip(pct, -2.0 * banda_pct, 2.0 * banda_pct))
    pct_raw = pct

    comp_vals = [
        c_rsi if w["rsi"] >= 0 else -c_rsi,
        c_macd if w["macd"] >= 0 else -c_macd,
        c_tend if w["tendencia"] >= 0 else -c_tend,
        c_mom if w["momentum"] >= 0 else -c_mom,
    ]
    if w_tilt > 1e-9 and abs(tilt_fundamental) > 0.01:
        comp_vals.append(tilt_fundamental)
    n_alcista = sum(1 for c in comp_vals if c > 0.15)
    n_bajista = sum(1 for c in comp_vals if c < -0.15)

    if s_final >= sig_eng.UMBRAL_SENAL and n_alcista >= min_consenso_alcista:
        senal = sig_eng.SENAL_ALCISTA
    elif s_final <= -sig_eng.UMBRAL_SENAL and n_bajista >= min_consenso_bajista:
        senal = sig_eng.SENAL_BAJISTA
    else:
        senal = sig_eng.SENAL_LATERAL

    umbrales_map = config.umbrales_lateral_v1 or UMBRALES_LATERAL_DEFAULT
    umbral_lat = float(umbrales_map.get(hz_norm, UMBRALES_LATERAL_DEFAULT.get(hz_norm, 0.8)))

    if senal == sig_eng.SENAL_LATERAL:
        pct = round(float(np.clip(pct, -umbral_lat, umbral_lat)), 2)
    elif senal == sig_eng.SENAL_ALCISTA and pct < umbral_lat:
        pct = umbral_lat
    elif senal == sig_eng.SENAL_BAJISTA and pct > -umbral_lat:
        pct = -umbral_lat

    # Filtro experimental de conviccion minima por horizonte (C-16):
    # si abs(pct_estimado) < umbral_zona * mult (o abs(pct_raw) < umbral_zona * mult cuando mult > 1),
    # la senal emitida se reclasifica a LATERAL.
    mult_map = config.multiplicador_conviccion_por_hz or MULT_CONVICCION_DEFAULT
    mult_hz = float(mult_map.get(hz_norm, 1.0))
    umbral_conv = umbral_lat * mult_hz
    if senal != sig_eng.SENAL_LATERAL and mult_hz > 1.0:
        if abs(pct_raw) < umbral_conv or abs(pct) < umbral_conv:
            senal = sig_eng.SENAL_LATERAL
            pct = round(float(np.clip(pct_raw, -umbral_lat, umbral_lat)), 2)
    elif senal != sig_eng.SENAL_LATERAL and abs(pct) < umbral_conv:
        senal = sig_eng.SENAL_LATERAL
        pct = round(float(np.clip(pct, -umbral_lat, umbral_lat)), 2)

    # Modo de calculo de confianza (v5_original vs alternativos para C-19)
    modo_conf = (config.modo_confianza or "v5_original").strip().lower()
    if modo_conf == "v5_original":
        confianza = 50.0 + 45.0 * abs(s_final) / 2.0
        if bb_squeeze and abs(s_final) >= sig_eng.UMBRAL_SENAL:
            confianza = min(99.0, confianza + 5.0)
    elif modo_conf in ("inversa_vol", "calibrada_consenso", "experimental_c19"):
        n_alin = n_alcista if s_final >= 0 else n_bajista
        cons_ratio = float(n_alin) / 4.0
        vol_pen = float(np.clip(0.02 / max(volatilidad, 0.005), 0.5, 1.3))
        rvol_bon = 1.05 if rvol >= 1.2 else 0.95
        base_c = 50.0 + 35.0 * (abs(s_final) / 2.0) * cons_ratio * vol_pen * rvol_bon
        confianza = float(np.clip(base_c, 50.0, 95.0))
    elif modo_conf == "constante":
        confianza = 60.0
    else:
        confianza = 50.0 + 45.0 * abs(s_final) / 2.0
        if bb_squeeze and abs(s_final) >= sig_eng.UMBRAL_SENAL:
            confianza = min(99.0, confianza + 5.0)

    if senal != sig_eng.SENAL_LATERAL:
        n_alineados = n_alcista if senal == sig_eng.SENAL_ALCISTA else n_bajista
        if n_alineados >= 4 and vol_confirm >= 1.0:
            conviccion = "ALTA"
        elif n_alineados >= 3 and vol_confirm >= 0.85:
            conviccion = "MEDIA"
        else:
            conviccion = "BAJA"
    else:
        conviccion = "BAJA"

    if adx >= sig_eng.ADX_TENDENCIA:
        regimen = "tendencia"
    elif adx <= sig_eng.ADX_RANGO:
        regimen = "rango"
    else:
        regimen = "mixto"

    marea_txt = (
        "alcista"
        if marea_score >= 0.25
        else ("bajista" if marea_score <= -0.25 else "neutra")
    )

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
        "factor_calibracion": 1.0,
    }


def _aplicar_coherencia_multitemporal_experimental(
    senales: Dict[str, Dict[str, Any]],
    rsi: float,
    config: ConfigExperimentoMotor,
) -> Dict[str, Dict[str, Any]]:
    """
    Aplica el filtro Triple Screen de Elder usando los umbrales de config.umbrales_lateral_v1.
    Si modo_confianza != 'v5_original' y es 'constante', no suma bonus de +5%.
    """
    if not senales or "1M" not in senales:
        return senales

    umbrales_map = config.umbrales_lateral_v1 or UMBRALES_LATERAL_DEFAULT
    s_1m = senales["1M"]["senal"]

    if s_1m == sig_eng.SENAL_BAJISTA:
        for hz in ("1D", "3D"):
            if hz in senales and senales[hz]["senal"] == sig_eng.SENAL_ALCISTA:
                if rsi >= 25.0:
                    senales[hz]["senal"] = sig_eng.SENAL_LATERAL
                    senales[hz]["filtro_elder"] = "bloqueo_bajista_macro"
                    senales[hz]["conviccion"] = "BAJA"
                    _ulat = float(umbrales_map.get(hz, UMBRALES_LATERAL_DEFAULT.get(hz, 0.8)))
                    senales[hz]["pct"] = round(
                        float(np.clip(senales[hz]["pct"], -_ulat, _ulat)), 2
                    )
    elif s_1m == sig_eng.SENAL_ALCISTA:
        for hz in ("1D", "3D"):
            if hz in senales and senales[hz]["senal"] == sig_eng.SENAL_BAJISTA:
                if rsi <= 75.0:
                    senales[hz]["senal"] = sig_eng.SENAL_LATERAL
                    senales[hz]["filtro_elder"] = "bloqueo_alcista_macro"
                    senales[hz]["conviccion"] = "BAJA"
                    _ulat = float(umbrales_map.get(hz, UMBRALES_LATERAL_DEFAULT.get(hz, 0.8)))
                    senales[hz]["pct"] = round(
                        float(np.clip(senales[hz]["pct"], -_ulat, _ulat)), 2
                    )

    if "1D" in senales and "1S" in senales and "1M" in senales:
        dir_1d = senales["1D"]["senal"]
        dir_1s = senales["1S"]["senal"]
        dir_1m = senales["1M"]["senal"]
        if dir_1d == dir_1s == dir_1m and dir_1m in (
            sig_eng.SENAL_ALCISTA,
            sig_eng.SENAL_BAJISTA,
        ):
            for hz in ("1D", "3D", "1S", "1M"):
                if hz in senales:
                    if (config.modo_confianza or "v5_original").strip().lower() not in (
                        "constante",
                        "v6_calibrada",
                    ):
                        senales[hz]["confianza"] = min(99.0, senales[hz]["confianza"] + 5.0)
                    senales[hz]["alineacion_macro"] = True

    return senales


def _calcular_fila_desde_indicadores_experimental(
    ind: Dict[str, Any],
    comp: Dict[str, float],
    marea: Dict[str, Any],
    config: ConfigExperimentoMotor,
    tilt: float = 0.0,
    sent_val: float = 0.0,
    ticker: str = "",
    nombre: str = "",
    vol_confirm_default: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Calcula la fila de emision de un ticker para una ConfigExperimentoMotor a partir
    de los indicadores y componentes ya extraidos de sub (evita recalcular RSI/MACD/ADX
    al evaluar multiples variantes de ablacion sobre la misma sesion).
    """
    close_s = ind.get("_close_series")
    vol_s = ind.get("_volume_series")
    w_ref = _pesos_para_horizonte("1S", ind["adx"], config)
    score_ref = (
        w_ref["rsi"] * comp["rsi"]
        + w_ref["macd"] * comp["macd"]
        + w_ref["tendencia"] * comp["tendencia"]
        + w_ref["momentum"] * comp["momentum"]
    )
    tiene_vol_por_hz = (
        config.vol_confirm_boost_por_hz is not None
        or config.vol_no_confirm_damp_por_hz is not None
        or any(
            abs(float((config.amortiguacion_rvol_pico_por_hz or {}).get(h, 1.0)) - 1.0) > 1e-6
            for h in sig_eng.HORIZONTES
        )
    )
    if (
        not tiene_vol_por_hz
        and abs(float(config.vol_confirm_boost) - sig_eng.VOL_CONFIRM_BOOST) <= 1e-9
        and abs(float(config.vol_no_confirm_damp) - sig_eng.VOL_NO_CONFIRM_DAMP) <= 1e-9
    ):
        if vol_confirm_default is not None and _es_pesos_por_defecto(config.pesos_horizonte) and config.pesos_tendencia is None and config.pesos_rango is None:
            vol_confirm_base = float(vol_confirm_default)
        else:
            vol_confirm_base = (
                sig_eng._confirmar_volumen(close_s, vol_s, score_ref)
                if (close_s is not None and vol_s is not None)
                else 1.0
            )
    else:
        vol_confirm_base = _confirmar_volumen_experimental(
            close_s, vol_s, score_ref, config
        )

    var20_ibex = marea.get("var20d", 0.0)
    rs_diff = ind["mom20"] - var20_ibex
    rs_score = float(np.clip(rs_diff / 10.0, -1.0, 1.0))
    ind["rs_diff"] = rs_diff

    fila: Dict[str, Any] = {
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
        "ultima_fecha": ind["ultima_fecha"],
        "comp_rsi": float(comp["rsi"]),
        "comp_macd": float(comp["macd"]),
        "comp_tendencia": float(comp["tendencia"]),
        "comp_momentum": float(comp["momentum"]),
        "vol_d_frac": float(ind["vol_d"]),
        "atr_d": float(ind["atr_d"]),
        "vol_confirm": float(vol_confirm_base),
        "sr_bias": float(ind["sr_bias"]),
        "bb_squeeze": bool(ind["bb_squeeze"]),
        "marea_score": float(marea.get("score", 0.0)),
        "rs_ibex_raw": float(rs_diff),
        "rs_score": float(rs_score),
        "sentimiento": float(sent_val),
        "tilt": float(tilt),
    }

    senales_ticker: Dict[str, Dict[str, Any]] = {}
    for hz in sig_eng.HORIZONTES:
        if tiene_vol_por_hz:
            vol_conf_hz = _confirmar_volumen_experimental(
                close_s, vol_s, score_ref, config, horizonte=hz
            )
        else:
            vol_conf_hz = vol_confirm_base
        senales_ticker[hz] = _calcular_senal_experimental(
            comp,
            ind["vol_d"],
            hz,
            config=config,
            tilt_fundamental=tilt,
            adx=ind["adx"],
            vol_confirm=vol_conf_hz,
            sr_bias=ind["sr_bias"],
            bb_squeeze=ind["bb_squeeze"],
            atr_d=ind["atr_d"],
            marea_score=marea["score"],
            rs_score=rs_score,
            sentiment_score=sent_val,
            rvol=ind["rvol"],
        )

    senales_ticker = _aplicar_coherencia_multitemporal_experimental(
        senales_ticker, ind["rsi"], config
    )

    if (config.modo_confianza or "v5_original").strip().lower() == "v6_calibrada":
        for hz in sig_eng.HORIZONTES:
            sig = senales_ticker[hz]
            conf_cal, _, _, _ = calcular_confianza_v6_calibrada(
                horizonte=hz,
                senal=sig["senal"],
                pct_estimado=sig["pct"],
                umbral_v1=sig.get("umbral_zona"),
                rvol=ind["rvol"],
                tabla=config.tabla_calibracion_confianza,
            )
            sig["confianza"] = conf_cal

    regimen_set = None
    for hz in sig_eng.HORIZONTES:
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
    fila["score_bruto"] = senales_ticker["1D"].get(
        "score_bruto", senales_ticker["1D"]["score"]
    )
    fila["regimen"] = regimen_set or "mixto"
    return fila


def calcular_fila_ticker_experimental(
    sub: pd.DataFrame,
    marea: Dict[str, Any],
    config: Optional[ConfigExperimentoMotor] = None,
    tilt: float = 0.0,
    sent_val: float = 0.0,
    ticker: str = "",
    nombre: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Calcula las senales de los 4 horizontes para un ticker bajo ConfigExperimentoMotor.
    Si config es la configuracion por defecto, delega o reproduce exactamente
    sig_eng.calcular_fila_ticker sin tocar ninguna variable global.
    """
    if config is None:
        config = ConfigExperimentoMotor()

    ind = sig_eng.calcular_indicadores(sub)
    if ind is None:
        return None
    ind["marea"] = marea
    ind["sentiment_score"] = sent_val

    comp = sig_eng._componentes(ind)
    return _calcular_fila_desde_indicadores_experimental(
        ind=ind,
        comp=comp,
        marea=marea,
        config=config,
        tilt=tilt,
        sent_val=sent_val,
        ticker=ticker,
        nombre=nombre,
    )


def _extraer_marea(
    fila_emision: Optional[Dict[str, Any]] = None,
    marea_s: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Extrae el estado normalizado de la marea del mercado ('ALCISTA', 'BAJISTA'
    o 'NEUTRA') a partir de marea_s (calculada del IBEX sin look-ahead) o, como
    respaldo, de la columna historica sellada 'marea' de la fila.
    """
    raw = None
    if isinstance(marea_s, dict) and marea_s.get("estado"):
        raw = marea_s.get("estado")
    elif isinstance(fila_emision, dict) and fila_emision.get("marea"):
        raw = fila_emision.get("marea")
    txt = str(raw or sig_eng.MAREA_NEUTRA).upper().strip()
    if "ALCISTA" in txt:
        return sig_eng.MAREA_ALCISTA
    if "BAJISTA" in txt:
        return sig_eng.MAREA_BAJISTA
    return sig_eng.MAREA_NEUTRA


def _aplicar_filtros_macro_rs_c19(
    senal: str,
    pct_est: float,
    pct_raw: float,
    conviccion: str,
    umbral_v1: float,
    marea_estado: str,
    rs_ibex: Optional[float],
    config: ConfigExperimentoMotor,
) -> Tuple[str, float, str, bool]:
    """
    Aplica los filtros opcionales de C-19 sobre una senal direccional emitida:
      - filtro_marea_estricto: fuerza a 'LATERAL' si ALCISTA con marea BAJISTA
        o BAJISTA con marea ALCISTA.
      - umbral_rs_ibex (> 0.0): exige rs_ibex > -umbral para ALCISTA y
        rs_ibex < umbral para BAJISTA; en caso contrario fuerza a 'LATERAL'.

    Retorna (senal_final, pct_final, conviccion_final, fue_filtrada).
    """
    sen_norm = str(senal or sig_eng.SENAL_LATERAL).upper().strip()
    if sen_norm not in (sig_eng.SENAL_ALCISTA, sig_eng.SENAL_BAJISTA):
        return sen_norm, float(pct_est), str(conviccion), False

    bloqueada = False
    if bool(getattr(config, "filtro_marea_estricto", False)):
        m_norm = str(marea_estado or sig_eng.MAREA_NEUTRA).upper().strip()
        if sen_norm == sig_eng.SENAL_ALCISTA and m_norm == sig_eng.MAREA_BAJISTA:
            bloqueada = True
        elif sen_norm == sig_eng.SENAL_BAJISTA and m_norm == sig_eng.MAREA_ALCISTA:
            bloqueada = True

    u_rs = float(getattr(config, "umbral_rs_ibex", 0.0) or 0.0)
    if not bloqueada and u_rs > 0.0 and rs_ibex is not None:
        try:
            rs_val = float(rs_ibex)
        except (TypeError, ValueError):
            rs_val = float("nan")
        if math.isfinite(rs_val):
            if sen_norm == sig_eng.SENAL_ALCISTA and rs_val <= -u_rs:
                bloqueada = True
            elif sen_norm == sig_eng.SENAL_BAJISTA and rs_val >= u_rs:
                bloqueada = True

    if not bloqueada:
        return sen_norm, float(pct_est), str(conviccion), False

    u_lat = (
        float(umbral_v1)
        if (umbral_v1 is not None and float(umbral_v1) > 0.0)
        else 0.8
    )
    pct_base = (
        float(pct_raw)
        if (pct_raw is not None and math.isfinite(float(pct_raw)))
        else float(pct_est)
    )
    pct_clip = round(float(np.clip(pct_base, -u_lat, u_lat)), 2)
    return sig_eng.SENAL_LATERAL, pct_clip, "BAJA", True


def _recortar_panel_hasta_fecha(
    df_panel: pd.DataFrame, fecha_corte_iso: str
) -> pd.DataFrame:
    """
    Recorta estrictamente df_panel a index <= fecha_corte_iso (cero look-ahead).
    """
    if df_panel is None or df_panel.empty:
        return pd.DataFrame()
    f_corte = str(fecha_corte_iso)[:10]
    fechas_str = _formatear_fechas_indice(df_panel.index)
    mask = (fechas_str <= f_corte).to_numpy(dtype=bool)
    return df_panel.iloc[mask]


def simular_emision_y_resolucion_wf(
    df_panel: pd.DataFrame,
    fechas_emision: List[str],
    tickers: List[str],
    config: Optional[ConfigExperimentoMotor] = None,
    horizontes: Tuple[str, ...] = ("1D", "3D", "1S"),
    aplicar_filtro_c27: bool = True,
    tilts: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """
    Simula walk-forward de emision y resolucion sobre df_panel sin look-ahead:
      - En cada fecha_prediccion en fechas_emision, recorta df_panel a index <= fecha_prediccion.
      - Filtra los tickers con evaluar_elegibilidad_ticker(df_hasta_s, ticker, fecha_prediccion)
        cuando aplicar_filtro_c27=True.
      - Calcula las senales de la sesion S para los tickers elegibles aplicando config.
      - Resuelve cada senal en fecha_objetivo = fecha_habil_bme(dias, desde=fecha_pred)
        y desde fecha_entrada_op (Apertura S+1), calculando todas las metricas C-09..C-12.
    """
    if config is None:
        config = ConfigExperimentoMotor()
    cfg_hash = calcular_hash_config(config)
    tilts_map = tilts or {}
    hz_tuple = tuple(str(h).upper().strip() for h in horizontes)
    umbrales_v1_map = config.umbrales_lateral_v1 or UMBRALES_LATERAL_DEFAULT

    columnas_salida = [
        "ticker",
        "fecha_prediccion",
        "horizonte",
        "dias",
        "fecha_objetivo",
        "fecha_entrada_op",
        "modelo",
        "params_hash",
        "senal",
        "pct_estimado",
        "pct_estimado_raw",
        "banda_pct",
        "confianza",
        "conviccion",
        "score_motor",
        "score_bruto",
        "precio_entrada",
        "precio_apertura_s1",
        "precio_real",
        "resuelta",
        "var_precio_c10",
        "var_apertura_c11",
        "umbral_v1",
        "sigma_cierre_pct",
        "umbral_v2",
        "clase_real_v1_c10",
        "acierto_3c_v1_c10",
        "clase_real_v2_c12",
        "acierto_3c_v2_c12",
        "clase_real_v1_c11",
        "acierto_3c_v1_c11",
        "clase_real_v2_c11",
        "acierto_3c_v2_c11",
        "acierto_signo_c10",
        "acierto_signo_c11",
        "comp_rsi",
        "comp_macd",
        "comp_tendencia",
        "comp_momentum",
        "adx",
        "rvol",
        "marea",
        "marea_score",
        "rs_ibex",
        "sigma",
        "tilt",
    ]

    if df_panel is None or df_panel.empty or not fechas_emision or not tickers:
        return pd.DataFrame(columns=columnas_salida)

    # Pre-extraer las series completas por ticker solo para la fase de resolucion post-emision
    series_completas: Dict[str, Optional[pd.DataFrame]] = {}
    for tk in tickers:
        series_completas[tk] = _extraer_sub_raw_ticker(df_panel, tk)

    registros: List[Dict[str, Any]] = []

    for f_emision_raw in fechas_emision:
        f_pred = str(f_emision_raw).strip()[:10]
        if not f_pred:
            continue
        try:
            d_pred = date.fromisoformat(f_pred)
        except ValueError:
            continue

        # 1. Recorte estricto sin look-ahead: solo barras <= f_pred
        df_hasta_s = _recortar_panel_hasta_fecha(df_panel, f_pred)
        if df_hasta_s.empty:
            continue

        # Marea del mercado calculada exclusivamente con datos <= f_pred
        sub_ibex_s = sig_eng.obtener_serie_ticker(df_hasta_s, sig_eng.TICKER_BENCHMARK)
        marea_s = sig_eng.calcular_marea_mercado(sub_ibex_s)

        for tk in tickers:
            if tk == sig_eng.TICKER_BENCHMARK:
                continue

            if aplicar_filtro_c27:
                es_elegible, _ = evaluar_elegibilidad_ticker(df_hasta_s, tk, f_pred)
                if not es_elegible:
                    continue

            sub_tk_s = sig_eng.obtener_serie_ticker(df_hasta_s, tk)
            if sub_tk_s is None or sub_tk_s.empty:
                continue

            # Verificar que la ultima barra disponible del ticker en df_hasta_s corresponde a f_pred
            fechas_tk_s = _formatear_fechas_indice(sub_tk_s.index)
            if fechas_tk_s.empty or str(fechas_tk_s.iloc[-1])[:10] != f_pred:
                continue

            tilt_tk = float(tilts_map.get(tk, 0.0))
            fila_emision = calcular_fila_ticker_experimental(
                sub_tk_s,
                marea_s,
                config=config,
                tilt=tilt_tk,
                sent_val=0.0,
                ticker=tk,
                nombre=tk,
            )
            if fila_emision is None:
                continue

            precio_entrada = float(fila_emision["precio"])
            sigma_motor_frac = float(
                fila_emision.get("atr_d") or fila_emision.get("vol_d_frac") or 0.01
            )

            # Volatilidad diaria de retornos de cierre hasta f_pred inclusive (cero look-ahead, C-12)
            sigma_cierre_pct = _calcular_sigma_cierre_hasta_emision_pct(
                sub_tk_s, f_pred, sigma_fallback=sigma_motor_frac
            )

            sub_full = series_completas.get(tk)

            for hz in hz_tuple:
                if hz not in sig_eng.HORIZONTES:
                    continue
                dias = int(sig_eng.HORIZONTES[hz])
                f_obj = fecha_habil_bme(dias, desde=d_pred)

                senal = str(fila_emision[f"senal_{hz}"]).upper().strip()
                pct_est = float(fila_emision[f"pct_{hz}"])
                pct_raw = float(fila_emision.get(f"pct_raw_{hz}", pct_est))
                banda_pct = float(fila_emision[f"banda_{hz}"])
                confianza = float(fila_emision[f"conf_{hz}"])
                conviccion = str(fila_emision.get(f"conv_{hz}", "BAJA"))
                score_motor = float(fila_emision[f"score_{hz}"])
                score_bruto = float(fila_emision.get(f"score_bruto_{hz}", score_motor))

                umbral_v1 = float(
                    umbrales_v1_map.get(hz, UMBRALES_LATERAL_DEFAULT.get(hz, 0.8))
                )
                marea_estado_s = _extraer_marea(fila_emision, marea_s)
                senal, pct_est, conviccion, _ = _aplicar_filtros_macro_rs_c19(
                    senal=senal,
                    pct_est=pct_est,
                    pct_raw=pct_raw,
                    conviccion=conviccion,
                    umbral_v1=umbral_v1,
                    marea_estado=marea_estado_s,
                    rs_ibex=float(fila_emision["rs_ibex"]),
                    config=config,
                )
                if (config.modo_confianza or "v5_original").strip().lower() == "v6_calibrada":
                    confianza, _, _, _ = calcular_confianza_v6_calibrada(
                        horizonte=hz,
                        senal=senal,
                        pct_estimado=pct_est,
                        umbral_v1=umbral_v1,
                        rvol=float(fila_emision["rvol"]),
                        tabla=config.tabla_calibracion_confianza,
                    )
                umbral_v2 = calcular_umbral_volatilidad_v2(
                    sigma=sigma_cierre_pct,
                    dias=dias,
                    horizonte=hz,
                    k=float(config.k_sigma_v2),
                    sigma_ya_en_pct=True,
                )

                # Resolucion en fecha_objetivo exacta y desde apertura operable S+1
                precio_real = (
                    _extraer_precio_en_fecha(sub_full, "Close", f_obj)
                    if sub_full is not None
                    else None
                )
                f_entrada_op = (
                    _determinar_fecha_entrada_operable(sub_full, f_pred, f_obj)
                    if sub_full is not None
                    else None
                )
                precio_ap_s1 = None
                if f_entrada_op is not None and sub_full is not None:
                    precio_ap_s1 = (
                        _extraer_precio_en_fecha(sub_full, "Open", f_entrada_op)
                        if "Open" in sub_full.columns
                        else _extraer_precio_en_fecha(sub_full, "Close", f_entrada_op)
                    )

                resuelta = 1 if (precio_real is not None and precio_entrada > 0.0) else 0
                var_c10 = (
                    round((precio_real / precio_entrada - 1.0) * 100.0, 4)
                    if resuelta == 1
                    else None
                )
                var_c11 = (
                    round((precio_real / precio_ap_s1 - 1.0) * 100.0, 4)
                    if (
                        resuelta == 1
                        and precio_ap_s1 is not None
                        and precio_ap_s1 > 0.0
                    )
                    else None
                )

                if var_c10 is not None:
                    clase_v1_c10 = _clasificar_variacion_c10(var_c10, umbral_v1)
                    acierto_v1_c10 = 1 if senal == clase_v1_c10 else 0
                    clase_v2_c12 = _clasificar_variacion_c10(var_c10, umbral_v2)
                    acierto_v2_c12 = 1 if senal == clase_v2_c12 else 0
                    acierto_sig_c10 = _calcular_acierto_signo_c10(senal, var_c10)
                else:
                    clase_v1_c10 = None
                    acierto_v1_c10 = None
                    clase_v2_c12 = None
                    acierto_v2_c12 = None
                    acierto_sig_c10 = None

                if var_c11 is not None:
                    clase_v1_c11 = _clasificar_variacion_c10(var_c11, umbral_v1)
                    acierto_v1_c11 = 1 if senal == clase_v1_c11 else 0
                    clase_v2_c11 = _clasificar_variacion_c10(var_c11, umbral_v2)
                    acierto_v2_c11 = 1 if senal == clase_v2_c11 else 0
                    acierto_sig_c11 = _calcular_acierto_signo_c10(senal, var_c11)
                else:
                    clase_v1_c11 = None
                    acierto_v1_c11 = None
                    clase_v2_c11 = None
                    acierto_v2_c11 = None
                    acierto_sig_c11 = None

                registros.append(
                    {
                        "ticker": tk,
                        "fecha_prediccion": f_pred,
                        "horizonte": hz,
                        "dias": dias,
                        "fecha_objetivo": f_obj,
                        "fecha_entrada_op": f_entrada_op,
                        "modelo": config.nombre_modelo,
                        "params_hash": cfg_hash,
                        "senal": senal,
                        "pct_estimado": pct_est,
                        "pct_estimado_raw": pct_raw,
                        "banda_pct": banda_pct,
                        "confianza": confianza,
                        "conviccion": conviccion,
                        "score_motor": score_motor,
                        "score_bruto": score_bruto,
                        "precio_entrada": precio_entrada,
                        "precio_apertura_s1": precio_ap_s1,
                        "precio_real": precio_real,
                        "resuelta": resuelta,
                        "var_precio_c10": var_c10,
                        "var_apertura_c11": var_c11,
                        "umbral_v1": umbral_v1,
                        "sigma_cierre_pct": sigma_cierre_pct,
                        "umbral_v2": umbral_v2,
                        "clase_real_v1_c10": clase_v1_c10,
                        "acierto_3c_v1_c10": acierto_v1_c10,
                        "clase_real_v2_c12": clase_v2_c12,
                        "acierto_3c_v2_c12": acierto_v2_c12,
                        "clase_real_v1_c11": clase_v1_c11,
                        "acierto_3c_v1_c11": acierto_v1_c11,
                        "clase_real_v2_c11": clase_v2_c11,
                        "acierto_3c_v2_c11": acierto_v2_c11,
                        "acierto_signo_c10": acierto_sig_c10,
                        "acierto_signo_c11": acierto_sig_c11,
                        "comp_rsi": float(fila_emision["comp_rsi"]),
                        "comp_macd": float(fila_emision["comp_macd"]),
                        "comp_tendencia": float(fila_emision["comp_tendencia"]),
                        "comp_momentum": float(fila_emision["comp_momentum"]),
                        "adx": float(fila_emision["adx"]),
                        "rvol": float(fila_emision["rvol"]),
                        "marea": str(fila_emision["marea"]),
                        "marea_score": float(fila_emision["marea_score"]),
                        "rs_ibex": float(fila_emision["rs_ibex"]),
                        "sigma": sigma_motor_frac,
                        "tilt": tilt_tk,
                    }
                )

    if not registros:
        return pd.DataFrame(columns=columnas_salida)
    return pd.DataFrame(registros, columns=columnas_salida)


def _pct_wf(num: int, den: int) -> Optional[float]:
    return round(100.0 * num / den, 2) if den > 0 else None


def _calcular_monotonia_confianza_wf(df_res: pd.DataFrame) -> Dict[str, Any]:
    """
    Calcula la correlacion de Spearman entre confianza y acierto (v1_c10, v2_c12,
    signo_c10 y signo_c11) y las tasas de acierto por terciles de confianza
    (T1_baja, T2_media, T3_alta).
    """
    vacio_tercil = {
        "n": 0,
        "conf_min": None,
        "conf_max": None,
        "tasa_3c_v1_c10": None,
        "tasa_3c_v2_c12": None,
        "n_direccionales": 0,
        "tasa_signo_c10": None,
        "n_direccionales_c11": 0,
        "tasa_signo_c11": None,
    }
    if df_res is None or df_res.empty or "confianza" not in df_res.columns:
        return {
            "spearman_3c_v1_c10": None,
            "spearman_3c_v2_c12": None,
            "spearman_signo_c10": None,
            "spearman_signo_c11": None,
            "monotona_creciente_3c_v1": False,
            "monotona_creciente_signo": False,
            "terciles": {
                "T1_baja": dict(vacio_tercil),
                "T2_media": dict(vacio_tercil),
                "T3_alta": dict(vacio_tercil),
            },
        }

    sub = df_res.dropna(subset=["confianza", "acierto_3c_v1_c10"]).copy()
    if sub.empty:
        return {
            "spearman_3c_v1_c10": None,
            "spearman_3c_v2_c12": None,
            "spearman_signo_c10": None,
            "spearman_signo_c11": None,
            "monotona_creciente_3c_v1": False,
            "monotona_creciente_signo": False,
            "terciles": {
                "T1_baja": dict(vacio_tercil),
                "T2_media": dict(vacio_tercil),
                "T3_alta": dict(vacio_tercil),
            },
        }

    def _spearman(x_s: pd.Series, y_s: pd.Series) -> Optional[float]:
        if len(x_s) < 3 or x_s.nunique() < 2 or y_s.nunique() < 2:
            return 0.0 if len(x_s) >= 3 else None
        rx = x_s.rank(method="average").to_numpy(dtype=float)
        ry = y_s.rank(method="average").to_numpy(dtype=float)
        sx = float(np.std(rx))
        sy = float(np.std(ry))
        if sx <= 1e-12 or sy <= 1e-12:
            return 0.0
        corr = float(np.corrcoef(rx, ry)[0, 1])
        return round(corr, 4) if math.isfinite(corr) else 0.0

    sp_v1 = _spearman(sub["confianza"], sub["acierto_3c_v1_c10"])
    sp_v2 = (
        _spearman(sub["confianza"], sub["acierto_3c_v2_c12"])
        if "acierto_3c_v2_c12" in sub.columns
        else None
    )
    sub_dir = sub.dropna(subset=["acierto_signo_c10"])
    sp_sig = (
        _spearman(sub_dir["confianza"], sub_dir["acierto_signo_c10"])
        if not sub_dir.empty
        else None
    )
    sub_dir_c11 = (
        sub.dropna(subset=["acierto_signo_c11"])
        if "acierto_signo_c11" in sub.columns
        else pd.DataFrame()
    )
    sp_sig_c11 = (
        _spearman(sub_dir_c11["confianza"], sub_dir_c11["acierto_signo_c11"])
        if not sub_dir_c11.empty
        else None
    )

    conf_arr = sub["confianza"].to_numpy(dtype=float)
    q1 = float(np.quantile(conf_arr, 1.0 / 3.0))
    q2 = float(np.quantile(conf_arr, 2.0 / 3.0))

    m1 = sub["confianza"] <= q1
    m2 = (sub["confianza"] > q1) & (sub["confianza"] <= q2)
    m3 = sub["confianza"] > q2

    if m1.sum() > 0 and m2.sum() > 0 and m3.sum() > 0:
        partes = [sub.loc[m1], sub.loc[m2], sub.loc[m3]]
    else:
        sub_ord = sub.sort_values("confianza", kind="mergesort")
        n_tot = len(sub_ord)
        c1 = n_tot // 3
        c2 = (2 * n_tot) // 3
        partes = [sub_ord.iloc[:c1], sub_ord.iloc[c1:c2], sub_ord.iloc[c2:]]

    nombres_t = ("T1_baja", "T2_media", "T3_alta")
    terciles_out: Dict[str, Dict[str, Any]] = {}
    for nom_t, df_t in zip(nombres_t, partes):
        n_t = len(df_t)
        if n_t == 0:
            terciles_out[nom_t] = dict(vacio_tercil)
            continue
        ok_v1 = int(df_t["acierto_3c_v1_c10"].sum())
        ok_v2 = (
            int(df_t["acierto_3c_v2_c12"].sum())
            if "acierto_3c_v2_c12" in df_t.columns
            else 0
        )
        df_t_dir = df_t.dropna(subset=["acierto_signo_c10"])
        n_dir_t = len(df_t_dir)
        ok_sig_t = int(df_t_dir["acierto_signo_c10"].sum()) if n_dir_t > 0 else 0
        df_t_dir_c11 = (
            df_t.dropna(subset=["acierto_signo_c11"])
            if "acierto_signo_c11" in df_t.columns
            else pd.DataFrame()
        )
        n_dir_t_c11 = len(df_t_dir_c11)
        ok_sig_t_c11 = (
            int(df_t_dir_c11["acierto_signo_c11"].sum()) if n_dir_t_c11 > 0 else 0
        )
        terciles_out[nom_t] = {
            "n": n_t,
            "conf_min": round(float(df_t["confianza"].min()), 2),
            "conf_max": round(float(df_t["confianza"].max()), 2),
            "tasa_3c_v1_c10": _pct_wf(ok_v1, n_t),
            "tasa_3c_v2_c12": _pct_wf(ok_v2, n_t),
            "n_direccionales": n_dir_t,
            "tasa_signo_c10": _pct_wf(ok_sig_t, n_dir_t) if n_dir_t > 0 else None,
            "n_direccionales_c11": n_dir_t_c11,
            "tasa_signo_c11": (
                _pct_wf(ok_sig_t_c11, n_dir_t_c11) if n_dir_t_c11 > 0 else None
            ),
        }

    t1_3c = terciles_out["T1_baja"]["tasa_3c_v1_c10"]
    t3_3c = terciles_out["T3_alta"]["tasa_3c_v1_c10"]
    t1_sig = terciles_out["T1_baja"]["tasa_signo_c10"]
    t3_sig = terciles_out["T3_alta"]["tasa_signo_c10"]

    return {
        "spearman_3c_v1_c10": sp_v1,
        "spearman_3c_v2_c12": sp_v2,
        "spearman_signo_c10": sp_sig,
        "spearman_signo_c11": sp_sig_c11,
        "monotona_creciente_3c_v1": bool(
            t1_3c is not None and t3_3c is not None and t3_3c >= t1_3c
        ),
        "monotona_creciente_signo": bool(
            t1_sig is not None and t3_sig is not None and t3_sig >= t1_sig
        ),
        "terciles": terciles_out,
    }


def _calcular_referencias_ingenuas_wf(
    df_res: pd.DataFrame,
    df_panel: Optional[pd.DataFrame],
    horizonte_label: str = "Todos",
) -> Dict[str, Any]:
    """
    Evalua las 5 referencias ingenuas de C-14 sobre las mismas observaciones del
    walk-forward (sin look-ahead):
      - siempre_alcista
      - siempre_bajista
      - siempre_lateral
      - continuacion_ultimo_movimiento
      - direccion_ibex
    """
    estrategias_def = [
        ("motor", "Motor"),
        ("siempre_alcista", "Siempre alcista"),
        ("siempre_bajista", "Siempre bajista"),
        ("siempre_lateral", "Siempre lateral"),
        ("continuacion_ultimo_movimiento", "Continuacion del ultimo movimiento"),
        ("direccion_ibex", "Direccion del IBEX"),
    ]
    if df_res is None or df_res.empty:
        return {
            "n_senales": 0,
            "n_fechas": 0,
            "etiqueta_muestra": formatear_etiqueta_muestra(0, 0, None, None, horizonte_label),
            "tabla": [],
            "por_codigo": {},
            "mejor_estrategia_3c_v1": "motor",
            "mejor_estrategia_3c_v2": "motor",
            "mejor_estrategia_signo": "motor",
            "motor_supera_todas_3c_v1": False,
            "motor_supera_todas_3c_v2": False,
            "motor_supera_todas_signo": False,
        }

    n_sub = len(df_res)
    fechas_sub = sorted(df_res["fecha_prediccion"].astype(str).str[:10].unique().tolist())
    et_sub = formatear_etiqueta_muestra(
        n_sub,
        len(fechas_sub),
        fechas_sub[0] if fechas_sub else None,
        fechas_sub[-1] if fechas_sub else None,
        horizonte_label,
    )
    dias_hz = int(sig_eng.HORIZONTES.get(horizonte_label, 1))

    # Cachear retornos previos por (ticker, fecha, dias) para evitar recomputos
    cache_ret_prev: Dict[Tuple[str, str, int], Optional[float]] = {}

    def _ret_prev(tk_q: str, fp_q: str, d_q: int) -> Optional[float]:
        key = (tk_q, fp_q, d_q)
        if key not in cache_ret_prev:
            cache_ret_prev[key] = _extraer_retorno_previo_serie(df_panel, tk_q, fp_q, d_q)
        return cache_ret_prev[key]

    filas_parsed: List[Dict[str, Any]] = []
    for row in df_res.itertuples(index=False):
        tk = str(getattr(row, "ticker", ""))
        fp = str(getattr(row, "fecha_prediccion", ""))[:10]
        hz = str(getattr(row, "horizonte", "1D")).upper().strip()
        dias_val = int(getattr(row, "dias", sig_eng.HORIZONTES.get(hz, 1)))
        s_motor = str(getattr(row, "senal", "LATERAL")).upper().strip()
        var_c10 = getattr(row, "var_precio_c10", None)
        u_v1 = float(getattr(row, "umbral_v1", UMBRALES_LATERAL_DEFAULT.get(hz, 0.8)))
        u_v2 = float(getattr(row, "umbral_v2", u_v1))
        clase_v1 = str(getattr(row, "clase_real_v1_c10", "LATERAL")).upper().strip()
        clase_v2 = str(getattr(row, "clase_real_v2_c12", "LATERAL")).upper().strip()
        comp_mom = getattr(row, "comp_momentum", 0.0)
        marea_val = getattr(row, "marea", "NEUTRA")

        ret_tk = _ret_prev(tk, fp, dias_val)
        if ret_tk is not None:
            s_cont_v1 = _clasificar_variacion_c10(ret_tk, u_v1)
            s_cont_v2 = _clasificar_variacion_c10(ret_tk, u_v2)
            s_cont_sig = "ALCISTA" if ret_tk > 0.0 else ("BAJISTA" if ret_tk < 0.0 else None)
        else:
            mom_f = float(comp_mom or 0.0)
            s_cont_v1 = "ALCISTA" if mom_f > 0.15 else ("BAJISTA" if mom_f < -0.15 else "LATERAL")
            s_cont_v2 = s_cont_v1
            s_cont_sig = "ALCISTA" if mom_f > 0.0 else ("BAJISTA" if mom_f < 0.0 else None)

        ret_ib = _ret_prev(sig_eng.TICKER_BENCHMARK, fp, dias_val)
        if ret_ib is not None:
            s_ib_v1 = _clasificar_variacion_c10(ret_ib, u_v1)
            s_ib_v2 = _clasificar_variacion_c10(ret_ib, u_v2)
            s_ib_sig = "ALCISTA" if ret_ib > 0.0 else ("BAJISTA" if ret_ib < 0.0 else None)
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

        filas_parsed.append(
            {
                "fecha": fp,
                "var_c10": float(var_c10) if var_c10 is not None else None,
                "clase_v1": clase_v1,
                "clase_v2": clase_v2,
                "senales": {
                    "motor": {
                        "3c_v1": s_motor,
                        "3c_v2": s_motor,
                        "signo": s_motor if s_motor in ("ALCISTA", "BAJISTA") else None,
                    },
                    "siempre_alcista": {
                        "3c_v1": "ALCISTA",
                        "3c_v2": "ALCISTA",
                        "signo": "ALCISTA",
                    },
                    "siempre_bajista": {
                        "3c_v1": "BAJISTA",
                        "3c_v2": "BAJISTA",
                        "signo": "BAJISTA",
                    },
                    "siempre_lateral": {
                        "3c_v1": "LATERAL",
                        "3c_v2": "LATERAL",
                        "signo": None,
                    },
                    "continuacion_ultimo_movimiento": {
                        "3c_v1": s_cont_v1,
                        "3c_v2": s_cont_v2,
                        "signo": s_cont_sig,
                    },
                    "direccion_ibex": {
                        "3c_v1": s_ib_v1,
                        "3c_v2": s_ib_v2,
                        "signo": s_ib_sig,
                    },
                },
            }
        )

    tabla: List[Dict[str, Any]] = []
    t_motor_v1 = None
    t_motor_v2 = None
    t_motor_sig = None

    for code, nombre in estrategias_def:
        ac_v1 = 0
        ac_v2 = 0
        n_dir = 0
        ac_sig = 0
        fechas_v1: Dict[str, Dict[str, int]] = {}
        fechas_v2: Dict[str, Dict[str, int]] = {}
        fechas_sig: Dict[str, Dict[str, int]] = {}

        for r in filas_parsed:
            fp = r["fecha"]
            s_info = r["senales"][code]
            ok_1 = 1 if s_info["3c_v1"] == r["clase_v1"] else 0
            ok_2 = 1 if s_info["3c_v2"] == r["clase_v2"] else 0
            ac_v1 += ok_1
            ac_v2 += ok_2

            if fp not in fechas_v1:
                fechas_v1[fp] = {"fecha": fp, "n": 0, "aciertos": 0}
                fechas_v2[fp] = {"fecha": fp, "n": 0, "aciertos": 0}
            fechas_v1[fp]["n"] += 1
            fechas_v1[fp]["aciertos"] += ok_1
            fechas_v2[fp]["n"] += 1
            fechas_v2[fp]["aciertos"] += ok_2

            s_sig = s_info["signo"]
            if s_sig in ("ALCISTA", "BAJISTA"):
                n_dir += 1
                v_c10 = r["var_c10"]
                ok_s = (
                    1
                    if (
                        v_c10 is not None
                        and (
                            (s_sig == "ALCISTA" and v_c10 > 0.0)
                            or (s_sig == "BAJISTA" and v_c10 < 0.0)
                        )
                    )
                    else 0
                )
                ac_sig += ok_s
                if fp not in fechas_sig:
                    fechas_sig[fp] = {"fecha": fp, "n": 0, "aciertos": 0}
                fechas_sig[fp]["n"] += 1
                fechas_sig[fp]["aciertos"] += ok_s

        t_v1 = _pct_wf(ac_v1, n_sub)
        t_v2 = _pct_wf(ac_v2, n_sub)
        t_sig = _pct_wf(ac_sig, n_dir) if n_dir > 0 else None

        inf_v1 = calcular_inferencia_por_fecha(
            list(fechas_v1.values()), referencia_pct=33.33, dias_horizonte=dias_hz
        )
        inf_v2 = calcular_inferencia_por_fecha(
            list(fechas_v2.values()), referencia_pct=33.33, dias_horizonte=dias_hz
        )
        inf_sig = (
            calcular_inferencia_por_fecha(
                list(fechas_sig.values()),
                referencia_pct=REF_AZAR_SIGNO_PCT,
                dias_horizonte=dias_hz,
            )
            if n_dir > 0
            else {}
        )

        if code == "motor":
            t_motor_v1 = t_v1
            t_motor_v2 = t_v2
            t_motor_sig = t_sig

        tabla.append(
            {
                "codigo": code,
                "nombre": nombre,
                "n_senales": n_sub,
                "n_fechas": len(fechas_sub),
                "etiqueta_muestra": et_sub,
                "aciertos_3clases": ac_v1,
                "tasa_acierto_3clases": t_v1,
                "inferencia_3clases": inf_v1,
                "aciertos_3clases_v1": ac_v1,
                "tasa_acierto_3clases_v1": t_v1,
                "inferencia_3clases_v1": inf_v1,
                "aciertos_3clases_v2": ac_v2,
                "tasa_acierto_3clases_v2": t_v2,
                "inferencia_3clases_v2": inf_v2,
                "n_direccionales": n_dir,
                "aciertos_signo": ac_sig,
                "tasa_acierto_signo": t_sig,
                "inferencia_signo": inf_sig,
            }
        )

    for item in tabla:
        if item["codigo"] == "motor":
            item["delta_vs_motor_3clases_pp"] = 0.0
            item["delta_vs_motor_3clases_v1_pp"] = 0.0
            item["delta_vs_motor_3clases_v2_pp"] = 0.0
            item["delta_vs_motor_signo_pp"] = 0.0
        else:
            tv1 = item["tasa_acierto_3clases_v1"]
            tv2 = item["tasa_acierto_3clases_v2"]
            ts = item["tasa_acierto_signo"]
            item["delta_vs_motor_3clases_v1_pp"] = (
                round(tv1 - t_motor_v1, 2)
                if (tv1 is not None and t_motor_v1 is not None)
                else None
            )
            item["delta_vs_motor_3clases_pp"] = item["delta_vs_motor_3clases_v1_pp"]
            item["delta_vs_motor_3clases_v2_pp"] = (
                round(tv2 - t_motor_v2, 2)
                if (tv2 is not None and t_motor_v2 is not None)
                else None
            )
            item["delta_vs_motor_signo_pp"] = (
                round(ts - t_motor_sig, 2)
                if (ts is not None and t_motor_sig is not None)
                else None
            )

    por_codigo = {x["codigo"]: x for x in tabla}
    otras_v1 = [
        x["tasa_acierto_3clases_v1"]
        for x in tabla
        if x["codigo"] != "motor" and x["tasa_acierto_3clases_v1"] is not None
    ]
    otras_v2 = [
        x["tasa_acierto_3clases_v2"]
        for x in tabla
        if x["codigo"] != "motor" and x["tasa_acierto_3clases_v2"] is not None
    ]
    otras_sig = [
        x["tasa_acierto_signo"]
        for x in tabla
        if x["codigo"] != "motor" and x["tasa_acierto_signo"] is not None
    ]

    mejor_v1 = max(
        tabla, key=lambda x: x["tasa_acierto_3clases_v1"] if x["tasa_acierto_3clases_v1"] is not None else -999.0
    )["codigo"]
    mejor_v2 = max(
        tabla, key=lambda x: x["tasa_acierto_3clases_v2"] if x["tasa_acierto_3clases_v2"] is not None else -999.0
    )["codigo"]
    con_sig = [x for x in tabla if x["tasa_acierto_signo"] is not None]
    mejor_sig = (
        max(con_sig, key=lambda x: x["tasa_acierto_signo"])["codigo"]
        if con_sig
        else "motor"
    )

    return {
        "n_senales": n_sub,
        "n_fechas": len(fechas_sub),
        "etiqueta_muestra": et_sub,
        "tabla": tabla,
        "por_codigo": por_codigo,
        "mejor_estrategia_3c_v1": mejor_v1,
        "mejor_estrategia_3c_v2": mejor_v2,
        "mejor_estrategia_signo": mejor_sig,
        "motor_supera_todas_3c_v1": bool(
            t_motor_v1 is not None and t_motor_v1 >= max(otras_v1 or [0.0])
        ),
        "motor_supera_todas_3c_v2": bool(
            t_motor_v2 is not None and t_motor_v2 >= max(otras_v2 or [0.0])
        ),
        "motor_supera_todas_signo": bool(
            t_motor_sig is not None and t_motor_sig >= max(otras_sig or [0.0])
        ),
    }


def _evaluar_bloque_wf(
    df_sub: pd.DataFrame,
    horizonte_label: str,
    df_panel: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    Calcula todas las metricas C-09..C-14 y monotonia de confianza para un subconjunto
    del DataFrame de simulacion walk-forward.
    """
    if df_sub is None or df_sub.empty:
        df_res = pd.DataFrame()
    else:
        df_res = df_sub[df_sub["resuelta"] == 1].copy()

    n_emitidas = int(len(df_sub)) if df_sub is not None else 0
    n_resueltas = int(len(df_res))
    fechas_res = (
        sorted(df_res["fecha_prediccion"].astype(str).str[:10].unique().tolist())
        if n_resueltas > 0
        else []
    )
    n_fechas = len(fechas_res)
    f_min = fechas_res[0] if fechas_res else None
    f_max = fechas_res[-1] if fechas_res else None
    etiqueta = formatear_etiqueta_muestra(
        n_resueltas, n_fechas, f_min, f_max, horizonte_label
    )
    dias_hz = int(sig_eng.HORIZONTES.get(horizonte_label, 1))

    if n_resueltas == 0:
        inf_vacia = calcular_inferencia_por_fecha([], referencia_pct=50.0, dias_horizonte=dias_hz)
        return {
            "horizonte": horizonte_label,
            "n_emitidas": n_emitidas,
            "n_senales": 0,
            "n_resueltas": 0,
            "n_fechas": 0,
            "fecha_min": None,
            "fecha_max": None,
            "etiqueta_muestra": etiqueta,
            "tasas_3clases": {
                "v1_c10": None,
                "v2_c12": None,
                "v1_c11": None,
                "v2_c11": None,
            },
            "tasas_signo": {
                "c10": None,
                "c11": None,
            },
            "tasa_3c_v1_c10": None,
            "tasa_3c_v2_c12": None,
            "tasa_3c_v1_c11": None,
            "tasa_3c_v2_c11": None,
            "tasa_signo_c10": None,
            "tasa_signo_c11": None,
            "n_direccionales_c10": 0,
            "n_direccionales_c11": 0,
            "ref_siempre_lateral_v1_c10_pct": None,
            "ref_siempre_lateral_v2_c12_pct": None,
            "ref_frecuencia_subida_c10_pct": None,
            "ref_frecuencia_subida_c11_pct": None,
            "dist_senales_emitidas": {"ALCISTA": 0, "LATERAL": 0, "BAJISTA": 0},
            "dist_clases_reales_v1_c10": {"ALCISTA": 0, "LATERAL": 0, "BAJISTA": 0},
            "dist_clases_reales_v2_c12": {"ALCISTA": 0, "LATERAL": 0, "BAJISTA": 0},
            "inferencia": {
                "v1_c10": inf_vacia,
                "v2_c12": inf_vacia,
                "v1_c11": inf_vacia,
                "v2_c11": inf_vacia,
                "signo_c10": inf_vacia,
                "signo_c11": inf_vacia,
            },
            "referencias_ingenuas_c14": _calcular_referencias_ingenuas_wf(
                df_res, df_panel, horizonte_label
            ),
            "monotonia_confianza": _calcular_monotonia_confianza_wf(df_res),
        }

    ok_v1_c10 = int(df_res["acierto_3c_v1_c10"].dropna().sum())
    ok_v2_c12 = int(df_res["acierto_3c_v2_c12"].dropna().sum())
    tasa_v1_c10 = _pct_wf(ok_v1_c10, n_resueltas)
    tasa_v2_c12 = _pct_wf(ok_v2_c12, n_resueltas)

    df_c11 = df_res.dropna(subset=["acierto_3c_v1_c11"]).copy()
    n_c11 = len(df_c11)
    ok_v1_c11 = int(df_c11["acierto_3c_v1_c11"].sum()) if n_c11 > 0 else 0
    ok_v2_c11 = int(df_c11["acierto_3c_v2_c11"].sum()) if n_c11 > 0 else 0
    tasa_v1_c11 = _pct_wf(ok_v1_c11, n_c11) if n_c11 > 0 else None
    tasa_v2_c11 = _pct_wf(ok_v2_c11, n_c11) if n_c11 > 0 else None

    df_dir_c10 = df_res.dropna(subset=["acierto_signo_c10"]).copy()
    n_dir_c10 = len(df_dir_c10)
    ok_sig_c10 = int(df_dir_c10["acierto_signo_c10"].sum()) if n_dir_c10 > 0 else 0
    tasa_sig_c10 = _pct_wf(ok_sig_c10, n_dir_c10) if n_dir_c10 > 0 else None

    df_dir_c11 = df_res.dropna(subset=["acierto_signo_c11"]).copy()
    n_dir_c11 = len(df_dir_c11)
    ok_sig_c11 = int(df_dir_c11["acierto_signo_c11"].sum()) if n_dir_c11 > 0 else 0
    tasa_sig_c11 = _pct_wf(ok_sig_c11, n_dir_c11) if n_dir_c11 > 0 else None

    n_lat_v1 = int((df_res["clase_real_v1_c10"] == "LATERAL").sum())
    n_lat_v2 = int((df_res["clase_real_v2_c12"] == "LATERAL").sum())
    ref_lat_v1 = _pct_wf(n_lat_v1, n_resueltas)
    ref_lat_v2 = _pct_wf(n_lat_v2, n_resueltas)

    n_sub_c10 = int((df_res["var_precio_c10"] > 0.0).sum())
    ref_sub_c10 = _pct_wf(n_sub_c10, n_resueltas)
    n_sub_c11 = int((df_c11["var_apertura_c11"] > 0.0).sum()) if n_c11 > 0 else 0
    ref_sub_c11 = _pct_wf(n_sub_c11, n_c11) if n_c11 > 0 else None

    def _agrupar_por_fecha(df_in: pd.DataFrame, col_ac: str) -> List[Dict[str, Any]]:
        if df_in.empty or col_ac not in df_in.columns:
            return []
        sub_f = df_in.dropna(subset=[col_ac])
        if sub_f.empty:
            return []
        grp = (
            sub_f.groupby("fecha_prediccion", as_index=False)[col_ac]
            .agg(n="count", aciertos="sum")
            .sort_values("fecha_prediccion")
        )
        return [
            {
                "fecha": str(r.fecha_prediccion)[:10],
                "n": int(r.n),
                "aciertos": int(r.aciertos),
            }
            for r in grp.itertuples(index=False)
        ]

    inf_v1_c10 = calcular_inferencia_por_fecha(
        _agrupar_por_fecha(df_res, "acierto_3c_v1_c10"),
        referencia_pct=ref_lat_v1 if ref_lat_v1 is not None else 33.33,
        dias_horizonte=dias_hz,
    )
    inf_v2_c12 = calcular_inferencia_por_fecha(
        _agrupar_por_fecha(df_res, "acierto_3c_v2_c12"),
        referencia_pct=ref_lat_v2 if ref_lat_v2 is not None else 33.33,
        dias_horizonte=dias_hz,
    )
    inf_v1_c11 = calcular_inferencia_por_fecha(
        _agrupar_por_fecha(df_c11, "acierto_3c_v1_c11"),
        referencia_pct=ref_lat_v1 if ref_lat_v1 is not None else 33.33,
        dias_horizonte=dias_hz,
    )
    inf_v2_c11 = calcular_inferencia_por_fecha(
        _agrupar_por_fecha(df_c11, "acierto_3c_v2_c11"),
        referencia_pct=ref_lat_v2 if ref_lat_v2 is not None else 33.33,
        dias_horizonte=dias_hz,
    )
    inf_sig_c10 = calcular_inferencia_por_fecha(
        _agrupar_por_fecha(df_dir_c10, "acierto_signo_c10"),
        referencia_pct=REF_AZAR_SIGNO_PCT,
        dias_horizonte=dias_hz,
    )
    inf_sig_c11 = calcular_inferencia_por_fecha(
        _agrupar_por_fecha(df_dir_c11, "acierto_signo_c11"),
        referencia_pct=REF_AZAR_SIGNO_PCT,
        dias_horizonte=dias_hz,
    )

    dist_em = {
        k: int((df_res["senal"] == k).sum()) for k in ("ALCISTA", "LATERAL", "BAJISTA")
    }
    dist_real_v1 = {
        k: int((df_res["clase_real_v1_c10"] == k).sum())
        for k in ("ALCISTA", "LATERAL", "BAJISTA")
    }
    dist_real_v2 = {
        k: int((df_res["clase_real_v2_c12"] == k).sum())
        for k in ("ALCISTA", "LATERAL", "BAJISTA")
    }

    return {
        "horizonte": horizonte_label,
        "n_emitidas": n_emitidas,
        "n_senales": n_resueltas,
        "n_resueltas": n_resueltas,
        "n_fechas": n_fechas,
        "fecha_min": f_min,
        "fecha_max": f_max,
        "etiqueta_muestra": etiqueta,
        "tasas_3clases": {
            "v1_c10": tasa_v1_c10,
            "v2_c12": tasa_v2_c12,
            "v1_c11": tasa_v1_c11,
            "v2_c11": tasa_v2_c11,
        },
        "tasas_signo": {
            "c10": tasa_sig_c10,
            "c11": tasa_sig_c11,
        },
        "tasa_3c_v1_c10": tasa_v1_c10,
        "tasa_3c_v2_c12": tasa_v2_c12,
        "tasa_3c_v1_c11": tasa_v1_c11,
        "tasa_3c_v2_c11": tasa_v2_c11,
        "tasa_signo_c10": tasa_sig_c10,
        "tasa_signo_c11": tasa_sig_c11,
        "n_direccionales_c10": n_dir_c10,
        "n_direccionales_c11": n_dir_c11,
        "ref_siempre_lateral_v1_c10_pct": ref_lat_v1,
        "ref_siempre_lateral_v2_c12_pct": ref_lat_v2,
        "ref_frecuencia_subida_c10_pct": ref_sub_c10,
        "ref_frecuencia_subida_c11_pct": ref_sub_c11,
        "dist_senales_emitidas": dist_em,
        "dist_clases_reales_v1_c10": dist_real_v1,
        "dist_clases_reales_v2_c12": dist_real_v2,
        "inferencia": {
            "v1_c10": inf_v1_c10,
            "v2_c12": inf_v2_c12,
            "v1_c11": inf_v1_c11,
            "v2_c11": inf_v2_c11,
            "signo_c10": inf_sig_c10,
            "signo_c11": inf_sig_c11,
        },
        "referencias_ingenuas_c14": _calcular_referencias_ingenuas_wf(
            df_res, df_panel, horizonte_label
        ),
        "monotonia_confianza": _calcular_monotonia_confianza_wf(df_res),
    }


def evaluar_experimento_wf(
    df_simulacion: pd.DataFrame,
    df_panel: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    Evalua el DataFrame resultante de simular_emision_y_resolucion_wf calculando:
      - Etiqueta de muestra C-09 (formatear_etiqueta_muestra),
      - Tasas de Acierto en 3 clases (v1_c10, v2_c12, v1_c11, v2_c11) y de Acierto
        de signo (c10 y c11), global y por horizonte,
      - Inferencia honesta por bloques de fecha_prediccion C-13 (calcular_inferencia_por_fecha),
      - Comparativa contra las 5 referencias ingenuas C-14 sobre las mismas observaciones,
      - Monotonia de confianza: correlacion de Spearman y tasas de acierto por terciles
        de confianza (T1_baja, T2_media, T3_alta).
    """
    glob = _evaluar_bloque_wf(df_simulacion, "Todos", df_panel=df_panel)
    por_hz: Dict[str, Dict[str, Any]] = {}
    if df_simulacion is not None and not df_simulacion.empty and "horizonte" in df_simulacion.columns:
        horizontes_presentes = [
            h for h in ("1D", "3D", "1S", "1M") if h in set(df_simulacion["horizonte"].unique())
        ]
        for h_extra in sorted(df_simulacion["horizonte"].dropna().unique()):
            if h_extra not in horizontes_presentes:
                horizontes_presentes.append(str(h_extra))
    else:
        horizontes_presentes = ["1D", "3D", "1S"]

    for hz in horizontes_presentes:
        sub_hz = (
            df_simulacion[df_simulacion["horizonte"] == hz]
            if (df_simulacion is not None and not df_simulacion.empty)
            else pd.DataFrame()
        )
        por_hz[hz] = _evaluar_bloque_wf(sub_hz, hz, df_panel=df_panel)

    modelo_str = "v5-frozen-wf"
    ph_str = None
    if df_simulacion is not None and not df_simulacion.empty:
        if "modelo" in df_simulacion.columns and not df_simulacion["modelo"].dropna().empty:
            modelo_str = str(df_simulacion["modelo"].dropna().iloc[0])
        if "params_hash" in df_simulacion.columns and not df_simulacion["params_hash"].dropna().empty:
            ph_str = str(df_simulacion["params_hash"].dropna().iloc[0])

    salida = dict(glob)
    salida["modelo"] = modelo_str
    salida["params_hash"] = ph_str
    salida["global"] = glob
    salida["por_horizonte"] = por_hz
    return salida


def _test_pareado_por_fecha_wf(
    df_base: pd.DataFrame,
    df_chall: pd.DataFrame,
    col_metrica: str = "acierto_3c_v1_c10",
) -> Dict[str, Any]:
    """
    Calcula el contraste pareado por fecha de emision entre challenger y baseline
    para la columna de acierto indicada:
      - t_stat_pareado_por_fecha
      - p_valor_pareado
      - media_diff_por_fecha_pp
      - se_diff_por_fecha_pp
      - n_fechas_comunes
      - n_fechas_ganadas_challenger
      - n_fechas_ganadas_baseline
      - n_fechas_empatadas
    """
    res_vacio = {
        "metrica": col_metrica,
        "n_fechas_comunes": 0,
        "media_diff_por_fecha_pp": None,
        "se_diff_por_fecha_pp": None,
        "t_stat_pareado_por_fecha": None,
        "p_valor_pareado": None,
        "n_fechas_ganadas_challenger": 0,
        "n_fechas_ganadas_baseline": 0,
        "n_fechas_empatadas": 0,
        "diferencias_por_fecha": [],
    }
    if (
        df_base is None
        or df_chall is None
        or df_base.empty
        or df_chall.empty
        or col_metrica not in df_base.columns
        or col_metrica not in df_chall.columns
    ):
        return res_vacio

    b_sub = df_base[df_base["resuelta"] == 1].dropna(subset=[col_metrica])
    c_sub = df_chall[df_chall["resuelta"] == 1].dropna(subset=[col_metrica])
    if b_sub.empty or c_sub.empty:
        return res_vacio

    b_grp = b_sub.groupby("fecha_prediccion")[col_metrica].mean() * 100.0
    c_grp = c_sub.groupby("fecha_prediccion")[col_metrica].mean() * 100.0
    fechas_comunes = sorted(set(b_grp.index).intersection(set(c_grp.index)))
    T = len(fechas_comunes)
    if T == 0:
        return res_vacio

    diffs: List[float] = []
    detalle: List[Dict[str, Any]] = []
    gana_c = 0
    gana_b = 0
    empata = 0

    for f in fechas_comunes:
        tb = float(b_grp.loc[f])
        tc = float(c_grp.loc[f])
        d = tc - tb
        diffs.append(d)
        if d > 1e-9:
            gana_c += 1
        elif d < -1e-9:
            gana_b += 1
        else:
            empata += 1
        detalle.append(
            {
                "fecha": str(f)[:10],
                "tasa_baseline_pct": round(tb, 4),
                "tasa_challenger_pct": round(tc, 4),
                "diff_pp": round(d, 4),
            }
        )

    arr_d = np.array(diffs, dtype=float)
    media_d = float(np.mean(arr_d))

    if T < 2:
        return {
            "metrica": col_metrica,
            "n_fechas_comunes": T,
            "media_diff_por_fecha_pp": round(media_d, 4),
            "se_diff_por_fecha_pp": None,
            "t_stat_pareado_por_fecha": None,
            "p_valor_pareado": None,
            "n_fechas_ganadas_challenger": gana_c,
            "n_fechas_ganadas_baseline": gana_b,
            "n_fechas_empatadas": empata,
            "diferencias_por_fecha": detalle,
        }

    sd_d = float(np.std(arr_d, ddof=1))
    se_d = sd_d / math.sqrt(T)
    if se_d > 1e-12:
        t_stat = media_d / se_d
        try:
            import scipy.stats as stats

            p_val = float(stats.t.sf(abs(t_stat), df=T - 1) * 2.0)
        except Exception:
            p_val = None
        t_stat_out: Optional[float] = round(float(t_stat), 4)
    else:
        if abs(media_d) <= 1e-12:
            t_stat_out = 0.0
            p_val = 1.0
        else:
            t_stat_out = float("inf") if media_d > 0 else float("-inf")
            p_val = 0.0

    return {
        "metrica": col_metrica,
        "n_fechas_comunes": T,
        "media_diff_por_fecha_pp": round(media_d, 4),
        "se_diff_por_fecha_pp": round(se_d, 4),
        "t_stat_pareado_por_fecha": t_stat_out,
        "p_valor_pareado": round(p_val, 4) if p_val is not None else None,
        "n_fechas_ganadas_challenger": gana_c,
        "n_fechas_ganadas_baseline": gana_b,
        "n_fechas_empatadas": empata,
        "diferencias_por_fecha": detalle,
    }


def _restar_pp(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 2)


def comparar_challenger_vs_baseline_wf(
    df_base: pd.DataFrame,
    df_challenger: pd.DataFrame,
    df_panel: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    Compara un experimento challenger frente al baseline sobre el mismo walk-forward:
      - Evalua ambos con evaluar_experimento_wf.
      - Calcula los diferenciales en puntos porcentuales (challenger - baseline)
        global y por horizonte para 3 clases (v1_c10, v2_c12, v1_c11, v2_c11) y
        signo (c10, c11).
      - Ejecuta el contraste pareado por fecha de emision (t_stat_pareado_por_fecha,
        n_fechas_ganadas_challenger, n_fechas_ganadas_baseline).
    """
    ev_base = evaluar_experimento_wf(df_base, df_panel=df_panel)
    ev_chall = evaluar_experimento_wf(df_challenger, df_panel=df_panel)

    pareado_v1_c10 = _test_pareado_por_fecha_wf(
        df_base, df_challenger, "acierto_3c_v1_c10"
    )
    pareado_v2_c12 = _test_pareado_por_fecha_wf(
        df_base, df_challenger, "acierto_3c_v2_c12"
    )
    pareado_v1_c11 = _test_pareado_por_fecha_wf(
        df_base, df_challenger, "acierto_3c_v1_c11"
    )
    pareado_v2_c11 = _test_pareado_por_fecha_wf(
        df_base, df_challenger, "acierto_3c_v2_c11"
    )
    pareado_sig_c10 = _test_pareado_por_fecha_wf(
        df_base, df_challenger, "acierto_signo_c10"
    )
    pareado_sig_c11 = _test_pareado_por_fecha_wf(
        df_base, df_challenger, "acierto_signo_c11"
    )

    deltas_globales = {
        "delta_3c_v1_c10_pp": _restar_pp(
            ev_chall["tasa_3c_v1_c10"], ev_base["tasa_3c_v1_c10"]
        ),
        "delta_3c_v2_c12_pp": _restar_pp(
            ev_chall["tasa_3c_v2_c12"], ev_base["tasa_3c_v2_c12"]
        ),
        "delta_3c_v1_c11_pp": _restar_pp(
            ev_chall["tasa_3c_v1_c11"], ev_base["tasa_3c_v1_c11"]
        ),
        "delta_3c_v2_c11_pp": _restar_pp(
            ev_chall["tasa_3c_v2_c11"], ev_base["tasa_3c_v2_c11"]
        ),
        "delta_signo_c10_pp": _restar_pp(
            ev_chall["tasa_signo_c10"], ev_base["tasa_signo_c10"]
        ),
        "delta_signo_c11_pp": _restar_pp(
            ev_chall["tasa_signo_c11"], ev_base["tasa_signo_c11"]
        ),
    }

    horizontes_comunes = sorted(
        set(ev_base.get("por_horizonte", {}).keys()).union(
            set(ev_chall.get("por_horizonte", {}).keys())
        )
    )
    por_hz_comp: Dict[str, Dict[str, Any]] = {}
    for hz in horizontes_comunes:
        b_hz = ev_base["por_horizonte"].get(hz, {})
        c_hz = ev_chall["por_horizonte"].get(hz, {})
        df_b_hz = (
            df_base[df_base["horizonte"] == hz]
            if (df_base is not None and not df_base.empty)
            else pd.DataFrame()
        )
        df_c_hz = (
            df_challenger[df_challenger["horizonte"] == hz]
            if (df_challenger is not None and not df_challenger.empty)
            else pd.DataFrame()
        )
        par_hz_v1 = _test_pareado_por_fecha_wf(
            df_b_hz, df_c_hz, "acierto_3c_v1_c10"
        )
        par_hz_v2 = _test_pareado_por_fecha_wf(
            df_b_hz, df_c_hz, "acierto_3c_v2_c12"
        )
        par_hz_sig = _test_pareado_por_fecha_wf(
            df_b_hz, df_c_hz, "acierto_signo_c10"
        )
        por_hz_comp[hz] = {
            "delta_3c_v1_c10_pp": _restar_pp(
                c_hz.get("tasa_3c_v1_c10"), b_hz.get("tasa_3c_v1_c10")
            ),
            "delta_3c_v2_c12_pp": _restar_pp(
                c_hz.get("tasa_3c_v2_c12"), b_hz.get("tasa_3c_v2_c12")
            ),
            "delta_3c_v1_c11_pp": _restar_pp(
                c_hz.get("tasa_3c_v1_c11"), b_hz.get("tasa_3c_v1_c11")
            ),
            "delta_3c_v2_c11_pp": _restar_pp(
                c_hz.get("tasa_3c_v2_c11"), b_hz.get("tasa_3c_v2_c11")
            ),
            "delta_signo_c10_pp": _restar_pp(
                c_hz.get("tasa_signo_c10"), b_hz.get("tasa_signo_c10")
            ),
            "delta_signo_c11_pp": _restar_pp(
                c_hz.get("tasa_signo_c11"), b_hz.get("tasa_signo_c11")
            ),
            "t_stat_pareado_por_fecha": par_hz_v1["t_stat_pareado_por_fecha"],
            "n_fechas_ganadas_challenger": par_hz_v1["n_fechas_ganadas_challenger"],
            "n_fechas_ganadas_baseline": par_hz_v1["n_fechas_ganadas_baseline"],
            "n_fechas_empatadas": par_hz_v1["n_fechas_empatadas"],
            "pareado_v1_c10": par_hz_v1,
            "pareado_v2_c12": par_hz_v2,
            "pareado_signo_c10": par_hz_sig,
        }

    return {
        "baseline_modelo": ev_base.get("modelo"),
        "challenger_modelo": ev_chall.get("modelo"),
        "baseline_hash": ev_base.get("params_hash"),
        "challenger_hash": ev_chall.get("params_hash"),
        "baseline": ev_base,
        "challenger": ev_chall,
        "diferenciales_pp": deltas_globales,
        **deltas_globales,
        "t_stat_pareado_por_fecha": pareado_v1_c10["t_stat_pareado_por_fecha"],
        "p_valor_pareado_por_fecha": pareado_v1_c10["p_valor_pareado"],
        "media_diff_por_fecha_pp": pareado_v1_c10["media_diff_por_fecha_pp"],
        "se_diff_por_fecha_pp": pareado_v1_c10["se_diff_por_fecha_pp"],
        "n_fechas_comunes": pareado_v1_c10["n_fechas_comunes"],
        "n_fechas_ganadas_challenger": pareado_v1_c10["n_fechas_ganadas_challenger"],
        "n_fechas_ganadas_baseline": pareado_v1_c10["n_fechas_ganadas_baseline"],
        "n_fechas_empatadas": pareado_v1_c10["n_fechas_empatadas"],
        "tests_pareados": {
            "v1_c10": pareado_v1_c10,
            "v2_c12": pareado_v2_c12,
            "v1_c11": pareado_v1_c11,
            "v2_c11": pareado_v2_c11,
            "signo_c10": pareado_sig_c10,
            "signo_c11": pareado_sig_c11,
        },
        "por_horizonte": por_hz_comp,
    }


def construir_config_ablacion_volumen_c16() -> ConfigExperimentoMotor:
    """
    Devuelve la configuracion de la variante V1 de ablacion de volumen (C-16):
    neutraliza el boost de volumen en 1D/3D (vol_confirm_boost_por_hz 1.0 en 1D/3D)
    y amortigua los picos de volumen (RVOL > 0.98) en 1D (0.75) y 3D (0.90),
    manteniendo los pesos direccionales de v5-frozen.
    """
    return ConfigExperimentoMotor(
        nombre_modelo="c16-ablacion-volumen",
        vol_confirm_boost_por_hz=copy.deepcopy(VOL_BOOST_POR_HZ_C16),
        vol_no_confirm_damp_por_hz=copy.deepcopy(VOL_DAMP_POR_HZ_C16),
        amortiguacion_rvol_pico_por_hz=copy.deepcopy(AMORTIGUACION_RVOL_PICO_C16),
        umbral_rvol_pico=0.98,
    )


def construir_config_ablacion_pesos_1d_c16() -> ConfigExperimentoMotor:
    """
    Devuelve la configuracion de la variante V2 de ablacion de pesos por horizonte (C-16):
    re-pondera 1D y 3D (con reversion suave sobre sobreextension de momentum y RSI
    en 1D cuando |comp| >= 0.80 e incorporacion de peso positivo de tilt fundamental:
    0.30 en 1D y 0.05 en 3D), manteniendo el multiplicador de volumen original de v5-frozen.
    """
    return ConfigExperimentoMotor(
        nombre_modelo="c16-ablacion-pesos-1d",
        pesos_horizonte=copy.deepcopy(PESOS_HORIZONTE_C16),
        peso_tilt_por_hz=copy.deepcopy(PESO_TILT_POR_HZ_C16),
        reversion_sobreextension_1d=1.0,
        umbral_sobreextension_1d=0.80,
    )


def construir_config_challenger_c16() -> ConfigExperimentoMotor:
    """
    Devuelve la configuracion completa del challenger C-16 (fijada exclusivamente con
    informacion de la ventana de entrenamiento 2025 anterior a 2026-10-12):
      - Pesos por horizonte:
        * 1D: {'rsi': 0.15, 'macd': 0.40, 'tendencia': 0.30, 'momentum': 0.15}
              con reversion de sobreextension en momentum y RSI (|comp| >= 0.80)
              y peso de tilt fundamental 0.30.
        * 3D: {'rsi': 0.12, 'macd': 0.30, 'tendencia': 0.28, 'momentum': 0.30}
              y peso de tilt fundamental 0.05.
        * 1S: {'rsi': 0.10, 'macd': 0.18, 'tendencia': 0.32, 'momentum': 0.40}
              (continuacion dinamica por ADX identica a v5-frozen, peso_tilt = 0.0).
        * 1M: {'rsi': 0.08, 'macd': 0.12, 'tendencia': 0.35, 'momentum': 0.45}
              (continuacion dinamica por ADX identica a v5-frozen, peso_tilt = 0.20).
      - Volumen por horizonte:
        * Desactiva el boost de volumen en 1D y 3D (vol_confirm_boost_por_hz = 1.0)
          y amortigua picos de agotamiento cuando RVOL > 0.98 (x0.75 en 1D, x0.90 en 3D),
          conservando intacto el comportamiento en 1S y 1M (1.10 / 0.70).
    """
    return ConfigExperimentoMotor(
        nombre_modelo="c16-challenger",
        pesos_horizonte=copy.deepcopy(PESOS_HORIZONTE_C16),
        peso_tilt_por_hz=copy.deepcopy(PESO_TILT_POR_HZ_C16),
        reversion_sobreextension_1d=1.0,
        umbral_sobreextension_1d=0.80,
        vol_confirm_boost_por_hz=copy.deepcopy(VOL_BOOST_POR_HZ_C16),
        vol_no_confirm_damp_por_hz=copy.deepcopy(VOL_DAMP_POR_HZ_C16),
        amortiguacion_rvol_pico_por_hz=copy.deepcopy(AMORTIGUACION_RVOL_PICO_C16),
        umbral_rvol_pico=0.98,
    )


def _simular_multiples_configs_wf(
    df_panel: pd.DataFrame,
    fechas_emision: List[str],
    tickers: List[str],
    configs_map: Dict[str, ConfigExperimentoMotor],
    horizontes: Tuple[str, ...] = ("1D", "3D", "1S"),
    aplicar_filtro_c27: bool = True,
    tilts: Optional[Dict[str, float]] = None,
) -> Dict[str, pd.DataFrame]:
    """
    Ejecuta simultaneamente multiples ConfigExperimentoMotor sobre las mismas fechas
    y tickers en un unico pase de indicadores sin look-ahead, garantizando resultados
    bit a bit identicos a llamar simular_emision_y_resolucion_wf por separado.
    """
    tilts_map = tilts or {}
    hz_tuple = tuple(str(h).upper().strip() for h in horizontes)
    hashes_map = {k: calcular_hash_config(cfg) for k, cfg in configs_map.items()}
    registros_map: Dict[str, List[Dict[str, Any]]] = {k: [] for k in configs_map}

    columnas_salida = [
        "ticker",
        "fecha_prediccion",
        "horizonte",
        "dias",
        "fecha_objetivo",
        "fecha_entrada_op",
        "modelo",
        "params_hash",
        "senal",
        "pct_estimado",
        "pct_estimado_raw",
        "banda_pct",
        "confianza",
        "conviccion",
        "score_motor",
        "score_bruto",
        "precio_entrada",
        "precio_apertura_s1",
        "precio_real",
        "resuelta",
        "var_precio_c10",
        "var_apertura_c11",
        "umbral_v1",
        "sigma_cierre_pct",
        "umbral_v2",
        "clase_real_v1_c10",
        "acierto_3c_v1_c10",
        "clase_real_v2_c12",
        "acierto_3c_v2_c12",
        "clase_real_v1_c11",
        "acierto_3c_v1_c11",
        "clase_real_v2_c11",
        "acierto_3c_v2_c11",
        "acierto_signo_c10",
        "acierto_signo_c11",
        "comp_rsi",
        "comp_macd",
        "comp_tendencia",
        "comp_momentum",
        "adx",
        "rvol",
        "marea",
        "marea_score",
        "rs_ibex",
        "sigma",
        "tilt",
    ]

    if df_panel is None or df_panel.empty or not fechas_emision or not tickers:
        return {k: pd.DataFrame(columns=columnas_salida) for k in configs_map}

    series_completas: Dict[str, Optional[pd.DataFrame]] = {
        tk: _extraer_sub_raw_ticker(df_panel, tk) for tk in tickers
    }

    for f_emision_raw in fechas_emision:
        f_pred = str(f_emision_raw).strip()[:10]
        if not f_pred:
            continue
        try:
            d_pred = date.fromisoformat(f_pred)
        except ValueError:
            continue

        df_hasta_s = _recortar_panel_hasta_fecha(df_panel, f_pred)
        if df_hasta_s.empty:
            continue

        sub_ibex_s = sig_eng.obtener_serie_ticker(df_hasta_s, sig_eng.TICKER_BENCHMARK)
        marea_s = sig_eng.calcular_marea_mercado(sub_ibex_s)

        for tk in tickers:
            if tk == sig_eng.TICKER_BENCHMARK:
                continue

            if aplicar_filtro_c27:
                es_elegible, _ = evaluar_elegibilidad_ticker(df_hasta_s, tk, f_pred)
                if not es_elegible:
                    continue

            sub_tk_s = sig_eng.obtener_serie_ticker(df_hasta_s, tk)
            if sub_tk_s is None or sub_tk_s.empty:
                continue

            fechas_tk_s = _formatear_fechas_indice(sub_tk_s.index)
            if fechas_tk_s.empty or str(fechas_tk_s.iloc[-1])[:10] != f_pred:
                continue

            ind = sig_eng.calcular_indicadores(sub_tk_s)
            if ind is None:
                continue
            ind["marea"] = marea_s
            ind["sentiment_score"] = 0.0
            comp = sig_eng._componentes(ind)

            close_s = ind.get("_close_series")
            vol_s = ind.get("_volume_series")
            w_ref_def = sig_eng._pesos_dinamicos("1S", ind["adx"])
            score_ref_def = (
                w_ref_def["rsi"] * comp["rsi"]
                + w_ref_def["macd"] * comp["macd"]
                + w_ref_def["tendencia"] * comp["tendencia"]
                + w_ref_def["momentum"] * comp["momentum"]
            )
            vol_conf_def = (
                sig_eng._confirmar_volumen(close_s, vol_s, score_ref_def)
                if (close_s is not None and vol_s is not None)
                else 1.0
            )

            tilt_tk = float(tilts_map.get(tk, 0.0))
            precio_entrada = float(ind["close"])
            sigma_motor_frac = float(ind.get("atr_d") or ind.get("vol_d") or 0.01)
            sigma_cierre_pct = _calcular_sigma_cierre_hasta_emision_pct(
                sub_tk_s, f_pred, sigma_fallback=sigma_motor_frac
            )
            sub_full = series_completas.get(tk)

            # Pre-resolver precios y variaciones por horizonte (comunes a las 4 variantes)
            res_por_hz: Dict[str, Dict[str, Any]] = {}
            for hz in hz_tuple:
                if hz not in sig_eng.HORIZONTES:
                    continue
                dias = int(sig_eng.HORIZONTES[hz])
                f_obj = fecha_habil_bme(dias, desde=d_pred)
                precio_real = (
                    _extraer_precio_en_fecha(sub_full, "Close", f_obj)
                    if sub_full is not None
                    else None
                )
                f_entrada_op = (
                    _determinar_fecha_entrada_operable(sub_full, f_pred, f_obj)
                    if sub_full is not None
                    else None
                )
                precio_ap_s1 = None
                if f_entrada_op is not None and sub_full is not None:
                    precio_ap_s1 = (
                        _extraer_precio_en_fecha(sub_full, "Open", f_entrada_op)
                        if "Open" in sub_full.columns
                        else _extraer_precio_en_fecha(sub_full, "Close", f_entrada_op)
                    )
                resuelta = 1 if (precio_real is not None and precio_entrada > 0.0) else 0
                var_c10 = (
                    round((precio_real / precio_entrada - 1.0) * 100.0, 4)
                    if resuelta == 1
                    else None
                )
                var_c11 = (
                    round((precio_real / precio_ap_s1 - 1.0) * 100.0, 4)
                    if (
                        resuelta == 1
                        and precio_ap_s1 is not None
                        and precio_ap_s1 > 0.0
                    )
                    else None
                )
                res_por_hz[hz] = {
                    "dias": dias,
                    "f_obj": f_obj,
                    "f_entrada_op": f_entrada_op,
                    "precio_real": precio_real,
                    "precio_ap_s1": precio_ap_s1,
                    "resuelta": resuelta,
                    "var_c10": var_c10,
                    "var_c11": var_c11,
                }

            for k_cfg, cfg in configs_map.items():
                fila_em = _calcular_fila_desde_indicadores_experimental(
                    ind=ind,
                    comp=comp,
                    marea=marea_s,
                    config=cfg,
                    tilt=tilt_tk,
                    sent_val=0.0,
                    ticker=tk,
                    nombre=tk,
                    vol_confirm_default=vol_conf_def,
                )
                umbrales_v1_map = cfg.umbrales_lateral_v1 or UMBRALES_LATERAL_DEFAULT
                cfg_hash = hashes_map[k_cfg]

                for hz in hz_tuple:
                    if hz not in res_por_hz:
                        continue
                    r_hz = res_por_hz[hz]
                    dias = r_hz["dias"]
                    var_c10 = r_hz["var_c10"]
                    var_c11 = r_hz["var_c11"]

                    senal = str(fila_em[f"senal_{hz}"]).upper().strip()
                    pct_est = float(fila_em[f"pct_{hz}"])
                    pct_raw = float(fila_em.get(f"pct_raw_{hz}", pct_est))
                    banda_pct = float(fila_em[f"banda_{hz}"])
                    confianza = float(fila_em[f"conf_{hz}"])
                    conviccion = str(fila_em.get(f"conv_{hz}", "BAJA"))
                    score_motor = float(fila_em[f"score_{hz}"])
                    score_bruto = float(fila_em.get(f"score_bruto_{hz}", score_motor))

                    umbral_v1 = float(
                        umbrales_v1_map.get(hz, UMBRALES_LATERAL_DEFAULT.get(hz, 0.8))
                    )
                    marea_estado_s = _extraer_marea(fila_em, marea_s)
                    senal, pct_est, conviccion, _ = _aplicar_filtros_macro_rs_c19(
                        senal=senal,
                        pct_est=pct_est,
                        pct_raw=pct_raw,
                        conviccion=conviccion,
                        umbral_v1=umbral_v1,
                        marea_estado=marea_estado_s,
                        rs_ibex=float(fila_em["rs_ibex"]),
                        config=cfg,
                    )
                    if (cfg.modo_confianza or "v5_original").strip().lower() == "v6_calibrada":
                        confianza, _, _, _ = calcular_confianza_v6_calibrada(
                            horizonte=hz,
                            senal=senal,
                            pct_estimado=pct_est,
                            umbral_v1=umbral_v1,
                            rvol=float(fila_em["rvol"]),
                            tabla=cfg.tabla_calibracion_confianza,
                        )
                    umbral_v2 = calcular_umbral_volatilidad_v2(
                        sigma=sigma_cierre_pct,
                        dias=dias,
                        horizonte=hz,
                        k=float(cfg.k_sigma_v2),
                        sigma_ya_en_pct=True,
                    )

                    if var_c10 is not None:
                        clase_v1_c10 = _clasificar_variacion_c10(var_c10, umbral_v1)
                        acierto_v1_c10 = 1 if senal == clase_v1_c10 else 0
                        clase_v2_c12 = _clasificar_variacion_c10(var_c10, umbral_v2)
                        acierto_v2_c12 = 1 if senal == clase_v2_c12 else 0
                        acierto_sig_c10 = _calcular_acierto_signo_c10(senal, var_c10)
                    else:
                        clase_v1_c10 = None
                        acierto_v1_c10 = None
                        clase_v2_c12 = None
                        acierto_v2_c12 = None
                        acierto_sig_c10 = None

                    if var_c11 is not None:
                        clase_v1_c11 = _clasificar_variacion_c10(var_c11, umbral_v1)
                        acierto_v1_c11 = 1 if senal == clase_v1_c11 else 0
                        clase_v2_c11 = _clasificar_variacion_c10(var_c11, umbral_v2)
                        acierto_v2_c11 = 1 if senal == clase_v2_c11 else 0
                        acierto_sig_c11 = _calcular_acierto_signo_c10(senal, var_c11)
                    else:
                        clase_v1_c11 = None
                        acierto_v1_c11 = None
                        clase_v2_c11 = None
                        acierto_v2_c11 = None
                        acierto_sig_c11 = None

                    registros_map[k_cfg].append(
                        {
                            "ticker": tk,
                            "fecha_prediccion": f_pred,
                            "horizonte": hz,
                            "dias": dias,
                            "fecha_objetivo": r_hz["f_obj"],
                            "fecha_entrada_op": r_hz["f_entrada_op"],
                            "modelo": cfg.nombre_modelo,
                            "params_hash": cfg_hash,
                            "senal": senal,
                            "pct_estimado": pct_est,
                            "pct_estimado_raw": pct_raw,
                            "banda_pct": banda_pct,
                            "confianza": confianza,
                            "conviccion": conviccion,
                            "score_motor": score_motor,
                            "score_bruto": score_bruto,
                            "precio_entrada": precio_entrada,
                            "precio_apertura_s1": r_hz["precio_ap_s1"],
                            "precio_real": r_hz["precio_real"],
                            "resuelta": r_hz["resuelta"],
                            "var_precio_c10": var_c10,
                            "var_apertura_c11": var_c11,
                            "umbral_v1": umbral_v1,
                            "sigma_cierre_pct": sigma_cierre_pct,
                            "umbral_v2": umbral_v2,
                            "clase_real_v1_c10": clase_v1_c10,
                            "acierto_3c_v1_c10": acierto_v1_c10,
                            "clase_real_v2_c12": clase_v2_c12,
                            "acierto_3c_v2_c12": acierto_v2_c12,
                            "clase_real_v1_c11": clase_v1_c11,
                            "acierto_3c_v1_c11": acierto_v1_c11,
                            "clase_real_v2_c11": clase_v2_c11,
                            "acierto_3c_v2_c11": acierto_v2_c11,
                            "acierto_signo_c10": acierto_sig_c10,
                            "acierto_signo_c11": acierto_sig_c11,
                            "comp_rsi": float(fila_em["comp_rsi"]),
                            "comp_macd": float(fila_em["comp_macd"]),
                            "comp_tendencia": float(fila_em["comp_tendencia"]),
                            "comp_momentum": float(fila_em["comp_momentum"]),
                            "adx": float(fila_em["adx"]),
                            "rvol": float(fila_em["rvol"]),
                            "marea": str(fila_em["marea"]),
                            "marea_score": float(fila_em["marea_score"]),
                            "rs_ibex": float(fila_em["rs_ibex"]),
                            "sigma": sigma_motor_frac,
                            "tilt": tilt_tk,
                        }
                    )

    out_dfs: Dict[str, pd.DataFrame] = {}
    for k_cfg, regs in registros_map.items():
        if not regs:
            out_dfs[k_cfg] = pd.DataFrame(columns=columnas_salida)
        else:
            out_dfs[k_cfg] = pd.DataFrame(regs, columns=columnas_salida)
    return out_dfs


def ejecutar_estudio_ablacion_c16(
    df_panel: pd.DataFrame,
    fechas_emision: List[str],
    tickers: List[str],
    horizontes: Tuple[str, ...] = ("1D", "3D", "1S"),
    tilts: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Ejecuta en paralelo sobre las mismas fechas y tickers las 4 configuraciones del
    estudio de ablacion C-16:
      - baseline_v5: ConfigExperimentoMotor() (clon exacto de v5-frozen),
      - ablacion_volumen: variante V1 de volumen corregido en corto plazo,
      - ablacion_pesos_1d: variante V2 de re-ponderacion de componentes en 1D/3D,
      - challenger_c16: construir_config_challenger_c16() (V3 combinada V1 + V2),
    y devuelve las metricas completas de las 4 configuraciones y las comparaciones
    pareadas por fecha (comparar_challenger_vs_baseline_wf) de cada variante contra
    baseline_v5.
    """
    configs_map: Dict[str, ConfigExperimentoMotor] = {
        "baseline_v5": ConfigExperimentoMotor(),
        "ablacion_volumen": construir_config_ablacion_volumen_c16(),
        "ablacion_pesos_1d": construir_config_ablacion_pesos_1d_c16(),
        "challenger_c16": construir_config_challenger_c16(),
    }

    dfs_map = _simular_multiples_configs_wf(
        df_panel=df_panel,
        fechas_emision=fechas_emision,
        tickers=tickers,
        configs_map=configs_map,
        horizontes=horizontes,
        aplicar_filtro_c27=True,
        tilts=tilts,
    )

    df_base = dfs_map["baseline_v5"]
    ev_base = evaluar_experimento_wf(df_base, df_panel=df_panel)

    comp_vol = comparar_challenger_vs_baseline_wf(
        df_base, dfs_map["ablacion_volumen"], df_panel=df_panel
    )
    comp_pesos = comparar_challenger_vs_baseline_wf(
        df_base, dfs_map["ablacion_pesos_1d"], df_panel=df_panel
    )
    comp_c16 = comparar_challenger_vs_baseline_wf(
        df_base, dfs_map["challenger_c16"], df_panel=df_panel
    )

    variantes_out: Dict[str, Dict[str, Any]] = {
        "baseline_v5": {
            "config": configs_map["baseline_v5"],
            "params_hash": calcular_hash_config(configs_map["baseline_v5"]),
            "df_simulacion": df_base,
            "evaluacion": ev_base,
            "comparacion_vs_baseline": None,
            **ev_base,
        },
        "ablacion_volumen": {
            "config": configs_map["ablacion_volumen"],
            "params_hash": calcular_hash_config(configs_map["ablacion_volumen"]),
            "df_simulacion": dfs_map["ablacion_volumen"],
            "evaluacion": comp_vol["challenger"],
            "comparacion_vs_baseline": comp_vol,
            **comp_vol["challenger"],
        },
        "ablacion_pesos_1d": {
            "config": configs_map["ablacion_pesos_1d"],
            "params_hash": calcular_hash_config(configs_map["ablacion_pesos_1d"]),
            "df_simulacion": dfs_map["ablacion_pesos_1d"],
            "evaluacion": comp_pesos["challenger"],
            "comparacion_vs_baseline": comp_pesos,
            **comp_pesos["challenger"],
        },
        "challenger_c16": {
            "config": configs_map["challenger_c16"],
            "params_hash": calcular_hash_config(configs_map["challenger_c16"]),
            "df_simulacion": dfs_map["challenger_c16"],
            "evaluacion": comp_c16["challenger"],
            "comparacion_vs_baseline": comp_c16,
            **comp_c16["challenger"],
        },
    }

    comparaciones_out: Dict[str, Dict[str, Any]] = {
        "ablacion_volumen": comp_vol,
        "ablacion_pesos_1d": comp_pesos,
        "challenger_c16": comp_c16,
    }

    return {
        "baseline_v5": variantes_out["baseline_v5"],
        "ablacion_volumen": variantes_out["ablacion_volumen"],
        "ablacion_pesos_1d": variantes_out["ablacion_pesos_1d"],
        "challenger_c16": variantes_out["challenger_c16"],
        "variantes": variantes_out,
        "comparaciones_vs_baseline": comparaciones_out,
        "comparaciones": comparaciones_out,
        "configuraciones": configs_map,
        "simulaciones": dfs_map,
    }


def construir_config_challenger_c17_moderado() -> ConfigExperimentoMotor:
    """
    Devuelve la configuracion equilibrada del challenger C-17 (calibrada en Train 2025):
    hereda 100% la re-ponderacion y amortiguacion de volumen de challenger_c16 y aplica
    un multiplicador moderado de conviccion direccional (1.25 en 1D, 3D y 1S; 1.0 en 1M),
    reteniendo ~20%-30% de senales direccionales activas.
    """
    cfg = construir_config_challenger_c16()
    cfg.nombre_modelo = "c17-challenger-moderado"
    cfg.multiplicador_conviccion_por_hz = copy.deepcopy(MULT_CONVICCION_C17_MODERADO)
    return cfg


def construir_config_challenger_c17() -> ConfigExperimentoMotor:
    """
    Devuelve la configuracion optima del challenger C-17 (calibrada exclusivamente en
    la ventana Train 2025 sobre challenger_c16):
      - Hereda los pesos por horizonte, reversion de sobreextension 1D, tilt fundamental
        y amortiguacion de volumen RVOL de challenger_c16.
      - Anade multiplicador_conviccion_por_hz = {'1D': 1.35, '3D': 2.00, '1S': 1.75, '1M': 1.00}
        para filtrar senales direccionales de baja conviccion que caen en la zona muerta
        LATERAL, manteniendo ~15%-20% de senales direccionales activas.
    """
    cfg = construir_config_challenger_c16()
    cfg.nombre_modelo = "c17-challenger"
    cfg.multiplicador_conviccion_por_hz = copy.deepcopy(MULT_CONVICCION_C17)
    return cfg


def _resumen_selectividad_bloque_wf(df_sub: pd.DataFrame) -> Dict[str, Any]:
    """Calcula n_emitidas, n_direccionales, n_lateral, % direccionales y % lateral."""
    if df_sub is None or df_sub.empty or "senal" not in df_sub.columns:
        return {
            "n_emitidas": 0,
            "n_direccionales": 0,
            "n_lateral": 0,
            "pct_direccionales": 0.0,
            "pct_lateral": 0.0,
        }
    senales = df_sub["senal"].astype(str).str.upper().str.strip()
    n_tot = int(len(senales))
    n_lat = int((senales == sig_eng.SENAL_LATERAL).sum())
    n_dir = n_tot - n_lat
    pct_dir = round(100.0 * n_dir / n_tot, 2) if n_tot > 0 else 0.0
    pct_lat = round(100.0 * n_lat / n_tot, 2) if n_tot > 0 else 0.0
    return {
        "n_emitidas": n_tot,
        "n_direccionales": n_dir,
        "n_lateral": n_lat,
        "pct_direccionales": pct_dir,
        "pct_lateral": pct_lat,
    }


def _resumen_selectividad_wf(df_sim: pd.DataFrame) -> Dict[str, Any]:
    """Resume selectividad direccional global y por horizonte de una simulacion WF."""
    glob = _resumen_selectividad_bloque_wf(df_sim)
    por_hz: Dict[str, Dict[str, Any]] = {}
    if df_sim is not None and not df_sim.empty and "horizonte" in df_sim.columns:
        horizontes_presentes = [
            h for h in ("1D", "3D", "1S", "1M") if h in set(df_sim["horizonte"].unique())
        ]
        for h_extra in sorted(df_sim["horizonte"].dropna().unique()):
            if h_extra not in horizontes_presentes:
                horizontes_presentes.append(str(h_extra))
        for hz in horizontes_presentes:
            por_hz[hz] = _resumen_selectividad_bloque_wf(
                df_sim[df_sim["horizonte"] == hz]
            )
    return {
        **glob,
        "por_horizonte": por_hz,
    }


def ejecutar_estudio_umbrales_c17(
    df_panel: pd.DataFrame,
    fechas_emision: List[str],
    tickers: List[str],
    horizontes: Tuple[str, ...] = ("1D", "3D", "1S"),
    tilts: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Ejecuta en una sola pasada sobre df_panel las 4 configuraciones del estudio de
    calibracion de umbrales de conviccion direccional C-17:
      - baseline_v5: ConfigExperimentoMotor() (clon exacto de v5-frozen),
      - challenger_c16: construir_config_challenger_c16(),
      - challenger_c17_moderado: construir_config_challenger_c17_moderado(),
      - challenger_c17: construir_config_challenger_c17(),
    devolviendo las metricas completas de las 4 configuraciones (incluyendo la tabla
    de referencias ingenuas C-14), el porcentaje de senales direccionales emitidas
    por cada variante y las comparaciones pareadas por fecha tanto contra baseline_v5
    como de challenger_c17 (y challenger_c17_moderado) contra challenger_c16.
    """
    configs_map: Dict[str, ConfigExperimentoMotor] = {
        "baseline_v5": ConfigExperimentoMotor(),
        "challenger_c16": construir_config_challenger_c16(),
        "challenger_c17_moderado": construir_config_challenger_c17_moderado(),
        "challenger_c17": construir_config_challenger_c17(),
    }

    dfs_map = _simular_multiples_configs_wf(
        df_panel=df_panel,
        fechas_emision=fechas_emision,
        tickers=tickers,
        configs_map=configs_map,
        horizontes=horizontes,
        aplicar_filtro_c27=True,
        tilts=tilts,
    )

    df_base = dfs_map["baseline_v5"]
    df_c16 = dfs_map["challenger_c16"]
    df_c17_mod = dfs_map["challenger_c17_moderado"]
    df_c17 = dfs_map["challenger_c17"]

    ev_base = evaluar_experimento_wf(df_base, df_panel=df_panel)
    comp_c16_vs_base = comparar_challenger_vs_baseline_wf(
        df_base, df_c16, df_panel=df_panel
    )
    comp_c17_mod_vs_base = comparar_challenger_vs_baseline_wf(
        df_base, df_c17_mod, df_panel=df_panel
    )
    comp_c17_vs_base = comparar_challenger_vs_baseline_wf(
        df_base, df_c17, df_panel=df_panel
    )
    comp_c17_mod_vs_c16 = comparar_challenger_vs_baseline_wf(
        df_c16, df_c17_mod, df_panel=df_panel
    )
    comp_c17_vs_c16 = comparar_challenger_vs_baseline_wf(
        df_c16, df_c17, df_panel=df_panel
    )

    sel_map = {k: _resumen_selectividad_wf(df_v) for k, df_v in dfs_map.items()}
    eval_map = {
        "baseline_v5": ev_base,
        "challenger_c16": comp_c16_vs_base["challenger"],
        "challenger_c17_moderado": comp_c17_mod_vs_base["challenger"],
        "challenger_c17": comp_c17_vs_base["challenger"],
    }
    comp_base_map = {
        "baseline_v5": None,
        "challenger_c16": comp_c16_vs_base,
        "challenger_c17_moderado": comp_c17_mod_vs_base,
        "challenger_c17": comp_c17_vs_base,
    }
    comp_c16_map = {
        "baseline_v5": None,
        "challenger_c16": None,
        "challenger_c17_moderado": comp_c17_mod_vs_c16,
        "challenger_c17": comp_c17_vs_c16,
    }

    variantes_out: Dict[str, Dict[str, Any]] = {}
    for k in (
        "baseline_v5",
        "challenger_c16",
        "challenger_c17_moderado",
        "challenger_c17",
    ):
        ev_k = eval_map[k]
        sel_k = sel_map[k]
        variantes_out[k] = {
            "config": configs_map[k],
            "params_hash": calcular_hash_config(configs_map[k]),
            "df_simulacion": dfs_map[k],
            "evaluacion": ev_k,
            "selectividad": sel_k,
            "pct_direccionales": sel_k["pct_direccionales"],
            "pct_lateral": sel_k["pct_lateral"],
            "n_direccionales": sel_k["n_direccionales"],
            "n_lateral": sel_k["n_lateral"],
            "pct_direccionales_por_horizonte": {
                hz: info["pct_direccionales"]
                for hz, info in sel_k["por_horizonte"].items()
            },
            "comparacion_vs_baseline": comp_base_map[k],
            "comparacion_vs_c16": comp_c16_map[k],
            **ev_k,
        }

    comparaciones_vs_baseline: Dict[str, Dict[str, Any]] = {
        "challenger_c16": comp_c16_vs_base,
        "challenger_c17_moderado": comp_c17_mod_vs_base,
        "challenger_c17": comp_c17_vs_base,
    }
    comparaciones_vs_c16: Dict[str, Dict[str, Any]] = {
        "challenger_c17_moderado": comp_c17_mod_vs_c16,
        "challenger_c17": comp_c17_vs_c16,
    }
    pct_dir_resumen: Dict[str, Dict[str, Any]] = {
        k: sel_map[k] for k in variantes_out
    }

    return {
        "baseline_v5": variantes_out["baseline_v5"],
        "challenger_c16": variantes_out["challenger_c16"],
        "challenger_c17_moderado": variantes_out["challenger_c17_moderado"],
        "challenger_c17": variantes_out["challenger_c17"],
        "variantes": variantes_out,
        "selectividad": pct_dir_resumen,
        "pct_direccionales": {
            k: sel_map[k]["pct_direccionales"] for k in variantes_out
        },
        "referencias_ingenuas_c14": ev_base.get("referencias_ingenuas_c14"),
        "comparaciones_vs_baseline": comparaciones_vs_baseline,
        "comparaciones_vs_c16": comparaciones_vs_c16,
        "comparacion_c17_vs_c16": comp_c17_vs_c16,
        "comparaciones": comparaciones_vs_baseline,
        "configuraciones": configs_map,
        "simulaciones": dfs_map,
    }


def construir_config_challenger_c18b() -> ConfigExperimentoMotor:
    """
    Devuelve la configuracion del challenger C-18b (calibracion empirica de confianza
    mediante binning estatico 2D entrenado exclusivamente en TRAIN_2025):
      - Hereda 100% los pesos por horizonte, reversion de sobreextension 1D, tilt
        fundamental y amortiguacion de volumen RVOL de challenger_c16, asi como los
        multiplicadores de conviccion direccional de challenger_c17.
      - Activa modo_confianza = 'v6_calibrada' con TABLA_CALIBRACION_C18_TRAIN,
        manteniendo intactas todas las senales y scores respecto a challenger_c17.
    """
    cfg = construir_config_challenger_c17()
    cfg.nombre_modelo = "c18b-challenger"
    cfg.modo_confianza = "v6_calibrada"
    cfg.tabla_calibracion_confianza = copy.deepcopy(TABLA_CALIBRACION_C18_TRAIN)
    return cfg


def ejecutar_estudio_confianza_c18b(
    df_panel: pd.DataFrame,
    fechas_emision: List[str],
    tickers: List[str],
    horizontes: Tuple[str, ...] = ("1D", "3D", "1S"),
    tilts: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Ejecuta en walk-forward paralelo (un unico pase de indicadores sin look-ahead)
    las 3 configuraciones del estudio de calibracion empirica de confianza C-18b:
      - baseline_v5 (modo_confianza='v5_original'),
      - challenger_c17 (modo_confianza='v5_original'),
      - challenger_c18b (modo_confianza='v6_calibrada').

    Reporta para las 3 variantes (global y por horizonte) las metricas de monotonia
    de confianza (spearman_3c_v1_c10, spearman_signo_c10, spearman_signo_c11) y las
    tasas de acierto por terciles de confianza (T1_baja, T2_media, T3_alta), junto
    con las evaluaciones completas y comparaciones pareadas.
    """
    configs_map: Dict[str, ConfigExperimentoMotor] = {
        "baseline_v5": ConfigExperimentoMotor(),
        "challenger_c17": construir_config_challenger_c17(),
        "challenger_c18b": construir_config_challenger_c18b(),
    }

    dfs_map = _simular_multiples_configs_wf(
        df_panel=df_panel,
        fechas_emision=fechas_emision,
        tickers=tickers,
        configs_map=configs_map,
        horizontes=horizontes,
        aplicar_filtro_c27=True,
        tilts=tilts,
    )

    df_base = dfs_map["baseline_v5"]
    df_c17 = dfs_map["challenger_c17"]
    df_c18b = dfs_map["challenger_c18b"]

    ev_base = evaluar_experimento_wf(df_base, df_panel=df_panel)
    comp_c17_vs_base = comparar_challenger_vs_baseline_wf(
        df_base, df_c17, df_panel=df_panel
    )
    comp_c18b_vs_base = comparar_challenger_vs_baseline_wf(
        df_base, df_c18b, df_panel=df_panel
    )
    comp_c18b_vs_c17 = comparar_challenger_vs_baseline_wf(
        df_c17, df_c18b, df_panel=df_panel
    )

    eval_map = {
        "baseline_v5": ev_base,
        "challenger_c17": comp_c17_vs_base["challenger"],
        "challenger_c18b": comp_c18b_vs_base["challenger"],
    }
    comp_base_map = {
        "baseline_v5": None,
        "challenger_c17": comp_c17_vs_base,
        "challenger_c18b": comp_c18b_vs_base,
    }
    comp_c17_map = {
        "baseline_v5": None,
        "challenger_c17": None,
        "challenger_c18b": comp_c18b_vs_c17,
    }

    variantes_out: Dict[str, Dict[str, Any]] = {}
    monotonia_resumen: Dict[str, Dict[str, Any]] = {}
    for k in ("baseline_v5", "challenger_c17", "challenger_c18b"):
        ev_k = eval_map[k]
        mono_glob = ev_k["monotonia_confianza"]
        mono_hz = {
            hz: info_hz["monotonia_confianza"]
            for hz, info_hz in ev_k.get("por_horizonte", {}).items()
        }
        monotonia_resumen[k] = {
            "modo_confianza": configs_map[k].modo_confianza,
            "global": mono_glob,
            "por_horizonte": mono_hz,
            "spearman_3c_v1_c10": mono_glob["spearman_3c_v1_c10"],
            "spearman_signo_c10": mono_glob["spearman_signo_c10"],
            "spearman_signo_c11": mono_glob["spearman_signo_c11"],
            "terciles": mono_glob["terciles"],
        }
        variantes_out[k] = {
            "config": configs_map[k],
            "params_hash": calcular_hash_config(configs_map[k]),
            "modo_confianza": configs_map[k].modo_confianza,
            "df_simulacion": dfs_map[k],
            "evaluacion": ev_k,
            "comparacion_vs_baseline": comp_base_map[k],
            "comparacion_vs_c17": comp_c17_map[k],
            **ev_k,
        }

    comparaciones_vs_baseline: Dict[str, Dict[str, Any]] = {
        "challenger_c17": comp_c17_vs_base,
        "challenger_c18b": comp_c18b_vs_base,
    }

    return {
        "baseline_v5": variantes_out["baseline_v5"],
        "challenger_c17": variantes_out["challenger_c17"],
        "challenger_c18b": variantes_out["challenger_c18b"],
        "variantes": variantes_out,
        "monotonia_confianza": monotonia_resumen,
        "comparaciones_vs_baseline": comparaciones_vs_baseline,
        "comparacion_c18b_vs_c17": comp_c18b_vs_c17,
        "comparaciones": comparaciones_vs_baseline,
        "configuraciones": configs_map,
        "simulaciones": dfs_map,
    }


def construir_config_challenger_c19() -> ConfigExperimentoMotor:
    """
    Devuelve la configuracion del challenger C-19 (filtros de regimen macro
    marea y fuerza relativa rs_ibex calibrados exclusivamente en TRAIN_2025):
      - Hereda 100% los pesos por horizonte, reversion de sobreextension 1D,
        tilt fundamental y amortiguacion de volumen RVOL de challenger_c16,
        los multiplicadores de conviccion direccional de challenger_c17 y la
        calibracion empirica de confianza v6_calibrada de challenger_c18b.
      - Activa filtro_marea_estricto=True (impide operar contra la marea
        principal del IBEX) y umbral_rs_ibex=3.0 (exige rs_ibex > -3.0 para
        ALCISTA y rs_ibex < 3.0 para BAJISTA), forzando a LATERAL las senales
        que no superen el filtro.
    """
    cfg = construir_config_challenger_c18b()
    cfg.nombre_modelo = "c19-challenger"
    cfg.filtro_marea_estricto = bool(FILTRO_MAREA_ESTRICTO_C19)
    cfg.umbral_rs_ibex = float(UMBRAL_RS_IBEX_C19)
    return cfg


def ejecutar_estudio_filtros_c19(
    df_panel: pd.DataFrame,
    fechas_emision: List[str],
    tickers: List[str],
    horizontes: Tuple[str, ...] = ("1D", "3D", "1S"),
    tilts: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Ejecuta en walk-forward paralelo (un unico pase de indicadores sin look-ahead)
    las 3 configuraciones del estudio de filtros macro y fuerza relativa C-19:
      - baseline_v5,
      - challenger_c18b,
      - challenger_c19 (con filtros macro marea y fuerza relativa rs_ibex).

    Reporta para las 3 variantes las metricas completas de 3 clases (v1_c10,
    v2_c12, v1_c11, v2_c11), signo (C10 y C11), selectividad (% direccionales),
    monotonia de confianza y las comparaciones pareadas de challenger_c19 vs
    challenger_c18b y vs baseline_v5.
    """
    configs_map: Dict[str, ConfigExperimentoMotor] = {
        "baseline_v5": ConfigExperimentoMotor(),
        "challenger_c18b": construir_config_challenger_c18b(),
        "challenger_c19": construir_config_challenger_c19(),
    }

    dfs_map = _simular_multiples_configs_wf(
        df_panel=df_panel,
        fechas_emision=fechas_emision,
        tickers=tickers,
        configs_map=configs_map,
        horizontes=horizontes,
        aplicar_filtro_c27=True,
        tilts=tilts,
    )

    df_base = dfs_map["baseline_v5"]
    df_c18b = dfs_map["challenger_c18b"]
    df_c19 = dfs_map["challenger_c19"]

    ev_base = evaluar_experimento_wf(df_base, df_panel=df_panel)
    comp_c18b_vs_base = comparar_challenger_vs_baseline_wf(
        df_base, df_c18b, df_panel=df_panel
    )
    comp_c19_vs_base = comparar_challenger_vs_baseline_wf(
        df_base, df_c19, df_panel=df_panel
    )
    comp_c19_vs_c18b = comparar_challenger_vs_baseline_wf(
        df_c18b, df_c19, df_panel=df_panel
    )

    sel_map = {k: _resumen_selectividad_wf(df_v) for k, df_v in dfs_map.items()}
    eval_map = {
        "baseline_v5": ev_base,
        "challenger_c18b": comp_c18b_vs_base["challenger"],
        "challenger_c19": comp_c19_vs_base["challenger"],
    }
    comp_base_map = {
        "baseline_v5": None,
        "challenger_c18b": comp_c18b_vs_base,
        "challenger_c19": comp_c19_vs_base,
    }
    comp_c18b_map = {
        "baseline_v5": None,
        "challenger_c18b": None,
        "challenger_c19": comp_c19_vs_c18b,
    }

    variantes_out: Dict[str, Dict[str, Any]] = {}
    monotonia_resumen: Dict[str, Dict[str, Any]] = {}
    for k in ("baseline_v5", "challenger_c18b", "challenger_c19"):
        ev_k = eval_map[k]
        sel_k = sel_map[k]
        mono_glob = ev_k["monotonia_confianza"]
        mono_hz = {
            hz: info_hz["monotonia_confianza"]
            for hz, info_hz in ev_k.get("por_horizonte", {}).items()
        }
        monotonia_resumen[k] = {
            "modo_confianza": configs_map[k].modo_confianza,
            "global": mono_glob,
            "por_horizonte": mono_hz,
            "spearman_3c_v1_c10": mono_glob["spearman_3c_v1_c10"],
            "spearman_signo_c10": mono_glob["spearman_signo_c10"],
            "spearman_signo_c11": mono_glob["spearman_signo_c11"],
            "terciles": mono_glob["terciles"],
        }
        variantes_out[k] = {
            "config": configs_map[k],
            "params_hash": calcular_hash_config(configs_map[k]),
            "df_simulacion": dfs_map[k],
            "evaluacion": ev_k,
            "selectividad": sel_k,
            "pct_direccionales": sel_k["pct_direccionales"],
            "pct_lateral": sel_k["pct_lateral"],
            "n_direccionales": sel_k["n_direccionales"],
            "n_lateral": sel_k["n_lateral"],
            "pct_direccionales_por_horizonte": {
                hz: info["pct_direccionales"]
                for hz, info in sel_k["por_horizonte"].items()
            },
            "comparacion_vs_baseline": comp_base_map[k],
            "comparacion_vs_c18b": comp_c18b_map[k],
            **ev_k,
        }

    comparaciones_vs_baseline: Dict[str, Dict[str, Any]] = {
        "challenger_c18b": comp_c18b_vs_base,
        "challenger_c19": comp_c19_vs_base,
    }

    return {
        "baseline_v5": variantes_out["baseline_v5"],
        "challenger_c18b": variantes_out["challenger_c18b"],
        "challenger_c19": variantes_out["challenger_c19"],
        "variantes": variantes_out,
        "selectividad": sel_map,
        "pct_direccionales": {
            k: sel_map[k]["pct_direccionales"] for k in variantes_out
        },
        "monotonia_confianza": monotonia_resumen,
        "comparaciones_vs_baseline": comparaciones_vs_baseline,
        "comparacion_c19_vs_c18b": comp_c19_vs_c18b,
        "comparacion_c19_vs_baseline": comp_c19_vs_base,
        "comparaciones": comparaciones_vs_baseline,
        "configuraciones": configs_map,
        "simulaciones": dfs_map,
    }
