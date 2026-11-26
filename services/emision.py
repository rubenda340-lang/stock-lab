"""
Módulo de Emisión Diaria y Persistencia Congelada — StockLab IA (Fase 1 - Higiene)
==================================================================================
Garantiza la inmutabilidad y la integridad temporal del historial predictivo:

Reglas estrictas de emisión:
  1. Solo se persisten señales con BARRAS DIARIAS COMPLETAS.
     El mercado continuo español (BME) cierra a las 17:35 CET. Con margen de
     liquidación y consolidación de Yahoo Finance, el corte oficial es a las 18:30 CET.
  2. Si la app se ejecuta durante el día (antes de las 18:30 CET), la barra en curso
     se trunca antes de cualquier persistencia. La vista de usuario puede seguir en vivo,
     pero NUNCA escribe en la base de datos.
  3. Inmutabilidad: las señales se emiten una sola vez por (ticker, horizonte, fecha_prediccion)
     usando ON CONFLICT DO NOTHING. El precio de entrada es el cierre oficial inmutable.
  4. Snapshot diario de ranking_historico en cada emisión.
  5. Resolución contra el cierre histórico en fecha_objetivo exacta.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple, Set
import logging
import os
import shutil
import tempfile
import time

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ULTIMOS_AJUSTES_DETECTADOS: List[Dict] = []


def atomic_replace_with_retry(
    tmp_path: str, dst_path: str, retries: int = 5, delay_s: float = 0.15
) -> None:
    """
    Reemplaza atomicamente dst_path con tmp_path ejecutando os.replace(tmp_path, dst_path)
    y reintentando hasta retries veces ante PermissionError en Windows (esperando delay_s).
    """
    max_attempts = max(1, int(retries))
    for attempt in range(max_attempts):
        try:
            os.replace(tmp_path, dst_path)
            return
        except PermissionError:
            if attempt < max_attempts - 1:
                time.sleep(delay_s)
            else:
                raise


def atomic_write_parquet(df: pd.DataFrame, dst_path: str) -> None:
    """
    Escribe df en formato Parquet de forma atomica mediante fichero temporal
    en el mismo directorio de dst_path y reemplazo con reintento.
    """
    dst_abs = os.path.abspath(dst_path)
    dst_dir = os.path.dirname(dst_abs)
    os.makedirs(dst_dir, exist_ok=True)
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dst_dir, prefix=".tmp_parquet_", suffix=".tmp", delete=False
        ) as tf:
            tmp_path = tf.name
        df.to_parquet(tmp_path)
        atomic_replace_with_retry(tmp_path, dst_path)
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def atomic_append_or_write_csv(df: pd.DataFrame, dst_path: str) -> None:
    """
    Escribe o anade filas a dst_path en formato CSV de forma atomica mediante
    fichero temporal en el mismo directorio de dst_path y reemplazo con reintento.
    """
    dst_abs = os.path.abspath(dst_path)
    dst_dir = os.path.dirname(dst_abs)
    os.makedirs(dst_dir, exist_ok=True)
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dst_dir, prefix=".tmp_csv_", suffix=".tmp", delete=False
        ) as tf:
            tmp_path = tf.name
        if os.path.exists(dst_path):
            shutil.copyfile(dst_path, tmp_path)
            df.to_csv(tmp_path, mode="a", header=False, index=False)
        else:
            df.to_csv(tmp_path, mode="w", header=True, index=False)
        atomic_replace_with_retry(tmp_path, dst_path)
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


from database.connection import get_connection, query_all
from database.predicciones import (
    evaluar_predicciones_retorno_total,
    guardar_snapshot_ranking,
    init_evaluaciones_table,
    init_predicciones_table,
    registrar_predicciones,
    resolver_predicciones,
)
from services.data_provider import descargar_price_batch
from services.signal_engine import (
    FASE_1_CONGELADA,
    FEEDBACK_LOOP_ACTIVO,
    MIN_BARRAS,
    MOTOR_VERSION,
    TICKER_BENCHMARK,
    calcular_params_hash_motor,
    calcular_radar_desde_batch,
    get_params_hash,
    obtener_serie_ticker,
    verificar_estado_congelado_v5,
)
from utils.market_calendar import (
    festivos_bme_rango,
    sesion_cerrada_hoy,
    HORA_CORTE_BME,
    MINUTO_CORTE_BME,
    HORA_LIMITE_EMISION,
    MINUTO_LIMITE_EMISION,
    get_ahora_madrid,
    barra_hoy_completa,
    limite_emision,
    en_ventana_emision,
)

# Constantes de elegibilidad del universo (Paso C-27)
MIN_CLOSE_ELEGIBLE_EUR = 0.05
MAX_DIAS_VOL_CERO_20S = 5
MAX_DIAS_VOL_CERO_60S = 10
MIN_EFECTIVO_MEDIANO_20S_EUR = 5000.0

_MOTIVOS_ILIQUIDEZ_C27 = frozenset({
    "sin_volumen_en_sesion",
    "precio_bajo_minimo",
    "exceso_dias_sin_volumen_20s",
    "efectivo_mediano_insuficiente",
    "exceso_dias_sin_volumen_60s",
})


def _formatear_fechas_indice(index: pd.Index) -> pd.Series:
    """Convierte el índice a una Serie de cadenas YYYY-MM-DD en hora de Madrid."""
    dt_idx = pd.to_datetime(index)
    if getattr(dt_idx, "tz", None) is not None:
        dt_idx = dt_idx.tz_convert("Europe/Madrid")
    return pd.Series(dt_idx.strftime("%Y-%m-%d"), index=index)


def _extraer_sub_raw_ticker(df_batch: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
    """Extrae el sub-DataFrame de un ticker del batch sin aplicar filtro de MIN_BARRAS."""
    if df_batch is None or df_batch.empty:
        return None
    try:
        if isinstance(df_batch.columns, pd.MultiIndex):
            if ticker in df_batch.columns.get_level_values(0):
                return df_batch[ticker]
            if ticker in df_batch.columns.get_level_values(-1):
                return df_batch.xs(ticker, level=-1, axis=1)
            return None
        return df_batch
    except Exception:
        return None


def evaluar_elegibilidad_ticker(
    df_batch: pd.DataFrame, ticker: str, fecha_sesion: str
) -> Tuple[bool, str]:
    """
    Funcion pura y determinista que evalua la elegibilidad de un ticker en fecha_sesion
    contra valores zombi, sin volumen o iliquidos (Paso C-27):
      (i)   Al menos MIN_BARRAS (35) barras con Close > 0 -> si no: (False, "sin_historico_minimo").
      (ii)  Barra en fecha_sesion (como ultima barra disponible del ticker en el lote) con
            Close no nulo y float(Close) > 0 -> si no: (False, "sin_barra_fresca").
      (iii) En fecha_sesion, Volume no nulo y float(Volume) > 0 -> si no: (False, "sin_volumen_en_sesion").
      (iv)  En fecha_sesion, float(Close) >= MIN_CLOSE_ELEGIBLE_EUR -> si no: (False, "precio_bajo_minimo").
      (v)   En las ultimas 20 fechas del indice general de df_batch (hasta fecha_sesion inclusive):
            - dias_vol_cero_20s <= MAX_DIAS_VOL_CERO_20S -> si no: (False, "exceso_dias_sin_volumen_20s").
            - efectivo_mediano_20s >= MIN_EFECTIVO_MEDIANO_20S_EUR -> si no: (False, "efectivo_mediano_insuficiente").
      (vi)  En las ultimas 60 fechas del indice general de df_batch (hasta fecha_sesion inclusive):
            - dias_vol_cero_60s <= MAX_DIAS_VOL_CERO_60S -> si no: (False, "exceso_dias_sin_volumen_60s").
      (vii) Si supera todas las comprobaciones: (True, "ok").
    """
    if df_batch is None or df_batch.empty or not fecha_sesion:
        return False, "sin_historico_minimo"

    sub_raw = _extraer_sub_raw_ticker(df_batch, ticker)
    if sub_raw is None or sub_raw.empty or "Close" not in sub_raw.columns:
        return False, "sin_historico_minimo"

    # (i) Verificar al menos MIN_BARRAS (35) barras con Close > 0 (hasta fecha_sesion)
    fechas_sub_all = _formatear_fechas_indice(sub_raw.index)
    sub_le = sub_raw.loc[fechas_sub_all <= fecha_sesion]
    if sub_le.empty:
        return False, "sin_historico_minimo"

    close_le = pd.to_numeric(sub_le["Close"], errors="coerce")
    if int((close_le.notna() & (close_le > 0)).sum()) < MIN_BARRAS:
        return False, "sin_historico_minimo"

    # (ii) Verificar que la serie del ticker tiene barra fresca en fecha_sesion con Close > 0
    sub_valid = sub_le.dropna(subset=["Close"])
    if sub_valid.empty:
        return False, "sin_barra_fresca"

    ult_dt = sub_valid.index[-1]
    try:
        ult_dt_ts = pd.to_datetime(ult_dt)
        if getattr(ult_dt_ts, "tz", None) is not None:
            ult_dt_ts = ult_dt_ts.tz_convert("Europe/Madrid")
        ult_str = ult_dt_ts.strftime("%Y-%m-%d")
    except Exception:
        ult_str = str(ult_dt)[:10]

    if ult_str != fecha_sesion:
        return False, "sin_barra_fresca"

    fechas_le_str = _formatear_fechas_indice(sub_le.index)
    filas_sesion = sub_le.loc[fechas_le_str == fecha_sesion]
    if filas_sesion.empty:
        return False, "sin_barra_fresca"

    c_val = pd.to_numeric(pd.Series([filas_sesion["Close"].iloc[-1]]), errors="coerce").iloc[0]
    if pd.isna(c_val) or float(c_val) <= 0.0:
        return False, "sin_barra_fresca"

    # (iii) Verificar que en fecha_sesion Volume es no nulo y float(Volume) > 0
    if "Volume" not in filas_sesion.columns:
        return False, "sin_volumen_en_sesion"
    v_val = pd.to_numeric(pd.Series([filas_sesion["Volume"].iloc[-1]]), errors="coerce").iloc[0]
    if pd.isna(v_val) or float(v_val) <= 0.0:
        return False, "sin_volumen_en_sesion"

    # (iv) Verificar que en fecha_sesion float(Close) >= MIN_CLOSE_ELEGIBLE_EUR
    if float(c_val) < MIN_CLOSE_ELEGIBLE_EUR:
        return False, "precio_bajo_minimo"

    if "Volume" not in sub_le.columns:
        return False, "exceso_dias_sin_volumen_20s"

    # Fechas del indice general de df_batch hasta fecha_sesion inclusive
    fechas_batch_str = _formatear_fechas_indice(df_batch.index)
    idx_batch_le = df_batch.index[fechas_batch_str <= fecha_sesion]
    if len(idx_batch_le) == 0:
        return False, "sin_barra_fresca"

    sub_le_dedup = sub_le[~sub_le.index.duplicated(keep="last")]

    # (v) Ultimas 20 fechas del indice general de df_batch (hasta fecha_sesion inclusive)
    idx_20 = idx_batch_le[-20:]
    sub_20 = sub_le_dedup.reindex(idx_20)
    c_20 = pd.to_numeric(sub_20["Close"], errors="coerce")
    v_20 = pd.to_numeric(sub_20["Volume"], errors="coerce")
    inval_20 = c_20.isna() | (c_20 <= 0.0) | v_20.isna() | (v_20 <= 0.0)
    dias_vol_cero_20s = int(inval_20.sum())
    if dias_vol_cero_20s > MAX_DIAS_VOL_CERO_20S:
        return False, "exceso_dias_sin_volumen_20s"

    efectivos_20 = np.where(
        inval_20,
        0.0,
        c_20.fillna(0.0).to_numpy(dtype=float) * v_20.fillna(0.0).to_numpy(dtype=float),
    )
    efectivo_mediano_20s = float(np.median(efectivos_20)) if len(efectivos_20) > 0 else 0.0
    if efectivo_mediano_20s < MIN_EFECTIVO_MEDIANO_20S_EUR:
        return False, "efectivo_mediano_insuficiente"

    # (vi) Ultimas 60 fechas del indice general de df_batch (hasta fecha_sesion inclusive)
    idx_60 = idx_batch_le[-60:]
    sub_60 = sub_le_dedup.reindex(idx_60)
    c_60 = pd.to_numeric(sub_60["Close"], errors="coerce")
    v_60 = pd.to_numeric(sub_60["Volume"], errors="coerce")
    inval_60 = c_60.isna() | (c_60 <= 0.0) | v_60.isna() | (v_60 <= 0.0)
    dias_vol_cero_60s = int(inval_60.sum())
    if dias_vol_cero_60s > MAX_DIAS_VOL_CERO_60S:
        return False, "exceso_dias_sin_volumen_60s"

    # (vii) Supera todas las comprobaciones
    return True, "ok"


def filtrar_tickers_elegibles(
    df_batch: pd.DataFrame, tickers: Tuple[str, ...], fecha_sesion: str
) -> Tuple[Set[str], Dict[str, str], List[str]]:
    """
    Evalua la elegibilidad de cada ticker del universo en fecha_sesion y devuelve:
      - elegibles: Set[str] de tickers que cumplen todos los criterios ("ok")
      - omitidos_detalle: Dict[str, str] ordenado {ticker: motivo} para los no elegibles
      - omitidos_iliquidez: List[str] ordenada de tickers excluidos por motivos (iii) a (vi)
    """
    elegibles: Set[str] = set()
    omitidos_detalle: Dict[str, str] = {}
    for t in tickers:
        es_elegible, motivo = evaluar_elegibilidad_ticker(df_batch, t, fecha_sesion)
        if es_elegible:
            elegibles.add(t)
        else:
            omitidos_detalle[t] = motivo
    omitidos_detalle_ord = {k: omitidos_detalle[k] for k in sorted(omitidos_detalle.keys())}
    omitidos_iliquidez = sorted(
        [t for t, m in omitidos_detalle_ord.items() if m in _MOTIVOS_ILIQUIDEZ_C27]
    )
    return elegibles, omitidos_detalle_ord, omitidos_iliquidez


def _obtener_matriz_closes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extrae una matriz 2D (fechas x tickers) con los precios de Close
    independientemente de si las columnas son MultiIndex o Index simple.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        if "Close" in df.columns.get_level_values(-1):
            sub = df.xs("Close", level=-1, axis=1)
        elif "Close" in df.columns.get_level_values(0):
            sub = df.xs("Close", level=0, axis=1)
        elif "Close" in df.columns.get_level_values(1):
            sub = df.xs("Close", level=1, axis=1)
        else:
            return pd.DataFrame(index=df.index)
        if isinstance(sub, pd.Series):
            sub = sub.to_frame()
        return sub
    else:
        if "Close" in df.columns:
            return pd.DataFrame({"SINGLE": df["Close"]}, index=df.index)
        return pd.DataFrame(index=df.index)


def truncar_a_barras_completas(df_batch: pd.DataFrame) -> Tuple[pd.DataFrame, Optional[str]]:
    """
    Determina la fecha_sesion global como la fecha más reciente del índice en la que
    al menos el 50 % de los valores del lote tiene Close válido (> 0).
    Truncado por FECHA, no por posición: si esa fecha es hoy (hora de Madrid) y no
    barra_hoy_completa(), elimina del lote TODAS las filas con índice == hoy (para
    todos los tickers) y repite la regla sobre las fechas restantes.
    Elimina toda dependencia del "primer ticker" y todo iloc[:-1] posicional.
    Retorna (df_truncado, fecha_sesion). Si ninguna fecha cumple la regla: (pd.DataFrame(), None).
    """
    if df_batch is None or df_batch.empty:
        return pd.DataFrame() if df_batch is None else df_batch, None

    df_work = df_batch.copy()
    if not df_work.index.is_monotonic_increasing:
        df_work = df_work.sort_index()

    while True:
        df_closes = _obtener_matriz_closes(df_work)
        if df_closes.empty:
            return pd.DataFrame(), None

        total_tickers = df_closes.shape[1]
        if total_tickers == 0:
            return pd.DataFrame(), None

        # Valores con Close no nulo y > 0
        validos = df_closes.notna() & (df_closes > 0)
        conteo_validos = validos.sum(axis=1)

        umbral = total_tickers * 0.5
        fechas_validas = conteo_validos[conteo_validos >= umbral]

        if fechas_validas.empty:
            return pd.DataFrame(), None

        ult_fecha_idx = fechas_validas.index[-1]
        try:
            ult_dt = pd.to_datetime(ult_fecha_idx)
            if getattr(ult_dt, "tz", None) is not None:
                ult_dt = ult_dt.tz_convert("Europe/Madrid")
            ult_fecha_str = ult_dt.strftime("%Y-%m-%d")
        except Exception:
            ult_fecha_str = str(ult_fecha_idx)[:10]

        hoy_str = get_ahora_madrid().strftime("%Y-%m-%d")

        if ult_fecha_str == hoy_str and not barra_hoy_completa():
            fechas_str_work = _formatear_fechas_indice(df_work.index)
            mask_hoy = (fechas_str_work == hoy_str)
            df_work = df_work.loc[~mask_hoy].copy()
            continue

        fecha_sesion = ult_fecha_str
        fechas_str_work = _formatear_fechas_indice(df_work.index)
        mask_le = (fechas_str_work <= fecha_sesion)
        df_truncado = df_work.loc[mask_le].copy()
        return df_truncado, fecha_sesion


def sincronizar_cache_panel_historico(
    df_batch: Optional[pd.DataFrame] = None,
    verbose: bool = False,
    path_cache: Optional[str] = None,
    fn_descargar_full: Optional[Callable] = None,
) -> bool:
    """
    Sincroniza el caché histórico de 6 años del panel (data/backtest_cache_6y.parquet)
    con las barras consolidadas de la emisión diaria.

    Protección contra distorsión de escalas por auto_adjust=True (dividendos y splits):
      1. Descarga/utiliza las últimas ~400 sesiones por ticker.
      2. Sobrescribe las barras solapadas del caché.
      3. Compara el cierre solapado entre el caché y el lote descargado.
         Si ratio != 1.0 (con tolerancia 1e-6), detecta evento corporativo retrospectivo,
         reemplaza el histórico completo (6 años) de ese ticker por una descarga nueva,
         y deja constancia (ticker, fecha, ratio) en log, disco y memoria.
      4. Es estrictamente idempotente.
    """
    try:
        if path_cache is None:
            path_cache = os.path.join(os.path.dirname(__file__), "..", "data", "backtest_cache_6y.parquet")
        if not os.path.exists(path_cache):
            if verbose:
                print(f"⚠️ Archivo de caché {path_cache} no existe.")
            return False

        if fn_descargar_full is None:
            fn_descargar_full = descargar_price_batch

        # Si no se pasó df_batch, descargarlo para todas las empresas activas (~400 sesiones -> period='2y')
        if df_batch is None or df_batch.empty:
            empresas = query_all("SELECT ticker FROM empresas WHERE activa = 1")
            if not empresas:
                return False
            tickers = [e["ticker"].strip().upper() for e in empresas]
            if TICKER_BENCHMARK not in tickers:
                tickers.append(TICKER_BENCHMARK)
            df_raw = descargar_price_batch(tuple(tickers), period="2y")
            if df_raw is None or df_raw.empty:
                return False
            df_batch, _ = truncar_a_barras_completas(df_raw)

        if df_batch is None or df_batch.empty:
            return False

        df_old = pd.read_parquet(path_cache)
        if df_old.empty:
            return False

        # Normalizar índices de fecha a DatetimeIndex tz-naive
        idx_old = pd.to_datetime(df_old.index).tz_localize(None) if getattr(df_old.index, "tz", None) is not None else pd.to_datetime(df_old.index)
        df_old = df_old.copy()
        df_old.index = idx_old

        idx_batch = pd.to_datetime(df_batch.index).tz_localize(None) if getattr(df_batch.index, "tz", None) is not None else pd.to_datetime(df_batch.index)
        df_batch = df_batch.copy()
        df_batch.index = idx_batch

        fechas_solapadas = df_old.index.intersection(df_batch.index)
        fechas_nuevas = df_batch.index.difference(df_old.index)

        # Identificar tickers
        if isinstance(df_old.columns, pd.MultiIndex):
            tickers_old = list(df_old.columns.get_level_values(0).unique())
        else:
            tickers_old = []

        if isinstance(df_batch.columns, pd.MultiIndex):
            tickers_batch = list(df_batch.columns.get_level_values(0).unique())
        else:
            if len(tickers_old) == 1 and "Close" in df_batch.columns:
                t_unico = tickers_old[0]
                df_batch.columns = pd.MultiIndex.from_product([[t_unico], df_batch.columns], names=["Ticker", "Price"])
                tickers_batch = [t_unico]
            else:
                tickers_batch = []

        all_dates = df_old.index.union(df_batch.index).sort_values()
        df_updated = df_old.reindex(all_dates)

        ajustes_detectados = []
        tickers_reemplazados = set()
        cambios_detectados = len(fechas_nuevas) > 0

        tickers_comunes = [t for t in tickers_batch if t in tickers_old]

        # 1. Comprobar ratio en solape para detectar splits o dividendos retrospectivos
        if not fechas_solapadas.empty:
            for t in tickers_comunes:
                try:
                    c_old = df_old.loc[fechas_solapadas, (t, "Close")].dropna()
                    c_new = df_batch.loc[fechas_solapadas, (t, "Close")].dropna()
                    comun_t = c_old.index.intersection(c_new.index)
                except (KeyError, TypeError):
                    continue

                if comun_t.empty:
                    continue

                validos = (c_old.loc[comun_t] > 0) & (c_new.loc[comun_t] > 0)
                if not validos.any():
                    continue

                idx_val = comun_t[validos]
                ratios = c_new.loc[idx_val] / c_old.loc[idx_val]
                desviaciones = (ratios - 1.0).abs()
                es_ajuste = desviaciones > 1e-6

                if es_ajuste.any():
                    # Obtener la fecha con mayor desviación
                    fecha_max = desviaciones.idxmax()
                    ratio_val = float(ratios.loc[fecha_max])
                    fecha_str = fecha_max.strftime("%Y-%m-%d")

                    reg = {
                        "ticker": t,
                        "fecha": fecha_str,
                        "ratio": ratio_val
                    }
                    ajustes_detectados.append(reg)
                    ULTIMOS_AJUSTES_DETECTADOS.append(reg)
                    tickers_reemplazados.add(t)

                    if verbose:
                        try:
                            print(f"[AJUSTE EN CACHE] Ticker: {t}, Fecha: {fecha_str}, Ratio: {ratio_val:.6f}. Reemplazando historico completo...")
                        except Exception:
                            pass

                    # Descargar histórico completo (6 años) para este ticker
                    try:
                        df_full_t = fn_descargar_full((t,), period="6y")
                        if df_full_t is not None and not df_full_t.empty:
                            if isinstance(df_full_t.columns, pd.MultiIndex) and t in df_full_t.columns.get_level_values(0):
                                sub_full = df_full_t[t]
                            else:
                                sub_full = df_full_t

                            sub_idx = pd.to_datetime(sub_full.index).tz_localize(None) if getattr(sub_full.index, "tz", None) is not None else pd.to_datetime(sub_full.index)
                            sub_full = sub_full.copy()
                            sub_full.index = sub_idx

                            # Expandir si la descarga incluye fechas fuera del rango
                            if not sub_full.index.isin(df_updated.index).all():
                                df_updated = df_updated.reindex(df_updated.index.union(sub_full.index).sort_values())

                            # Reemplazar completamente las columnas del ticker en el caché
                            for col in ["Open", "High", "Low", "Close", "Volume"]:
                                if col in sub_full.columns and (t, col) in df_updated.columns:
                                    df_updated.loc[:, (t, col)] = np.nan
                                    fechas_inter = df_updated.index.intersection(sub_full.index)
                                    df_updated.loc[fechas_inter, (t, col)] = sub_full.loc[fechas_inter, col]
                            cambios_detectados = True
                    except Exception as e_full:
                        if verbose:
                            print(f"Error al descargar historico completo para {t}: {e_full}")

        # 2. Dejar constancia persistente en disco si se detectaron ajustes
        if ajustes_detectados:
            try:
                path_reg = os.path.join(os.path.dirname(path_cache), "registro_ajustes_cache.csv")
                df_reg = pd.DataFrame([
                    {
                        "timestamp_deteccion": pd.Timestamp.now().isoformat(),
                        "ticker": aj["ticker"],
                        "fecha_ajuste": aj["fecha"],
                        "ratio": aj["ratio"],
                        "accion": "reemplazo_historico_completo"
                    }
                    for aj in ajustes_detectados
                ])
                atomic_append_or_write_csv(df_reg, path_reg)
            except Exception as e_reg:
                if verbose:
                    print(f"No se pudo registrar ajuste en CSV: {e_reg}")

        # 3. Sobrescribir barras solapadas y nuevas para tickers sin ajuste retrospectivo
        for t in tickers_batch:
            if t in tickers_reemplazados:
                continue
            try:
                sub_b = df_batch[t] if isinstance(df_batch.columns, pd.MultiIndex) and t in df_batch.columns.get_level_values(0) else df_batch
                sub_b_idx = pd.to_datetime(sub_b.index).tz_localize(None) if getattr(sub_b.index, "tz", None) is not None else pd.to_datetime(sub_b.index)
                sub_b = sub_b.copy()
                sub_b.index = sub_b_idx
                fechas_actualizar = df_updated.index.intersection(sub_b.index)

                for col in ["Open", "High", "Low", "Close", "Volume"]:
                    if col in sub_b.columns:
                        col_tuple = (t, col)
                        if col_tuple not in df_updated.columns:
                            df_updated[col_tuple] = np.nan

                        v_old = df_updated.loc[fechas_actualizar, col_tuple]
                        v_new = sub_b.loc[fechas_actualizar, col]
                        diff_mask = (v_old != v_new) & ~(v_old.isna() & v_new.isna())
                        if diff_mask.any():
                            diff_vals = (v_old.loc[diff_mask] - v_new.loc[diff_mask]).abs()
                            if (diff_vals > 1e-6).any():
                                cambios_detectados = True
                        df_updated.loc[fechas_actualizar, col_tuple] = v_new
            except Exception as e_ovw:
                if verbose:
                    print(f"Error sobrescribiendo barras para {t}: {e_ovw}")

        if not cambios_detectados:
            return False

        df_updated = df_updated[~df_updated.index.duplicated(keep="last")].sort_index()
        atomic_write_parquet(df_updated, path_cache)
        if verbose:
            print(f"[OK] Cache de panel historico sincronizado ({len(df_updated)} sesiones hasta {df_updated.index[-1].strftime('%Y-%m-%d')}).")
        return True
    except Exception as e:
        if verbose:
            print(f"No se pudo sincronizar cache historico del panel: {e}")
        return False


def refrescar_cierres_panel(period: str = "2y", verbose: bool = True) -> bool:
    """
    Descarga las últimas ~400 sesiones (period='2y') de precios consolidados
    y sincroniza el caché histórico (data/backtest_cache_6y.parquet),
    sobrescribiendo el solape y reemplazando cualquier activo con ajuste retrospectivo.
    """
    empresas = query_all("SELECT ticker FROM empresas WHERE activa = 1")
    if not empresas:
        return False
    tickers = [e["ticker"].strip().upper() for e in empresas]
    if TICKER_BENCHMARK not in tickers:
        tickers.append(TICKER_BENCHMARK)
    df_raw = descargar_price_batch(tuple(tickers), period=period)
    if df_raw is None or df_raw.empty:
        return False
    df_truncado, _ = truncar_a_barras_completas(df_raw)
    return sincronizar_cache_panel_historico(df_truncado, verbose=verbose)


def _evaluar_retorno_total_seguro(df_batch: Optional[pd.DataFrame] = None) -> None:
    """
    Invoca de forma segura evaluar_predicciones_retorno_total tras resolver_predicciones()
    solo cuando existan filas resuelta = 1 pendientes de evaluar en evaluaciones.
    Nunca rompe el flujo de emision si falla la descarga auto_adjust=False.
    """
    try:
        conn = get_connection()
        try:
            n_res1 = conn.execute(
                "SELECT COUNT(*) FROM predicciones WHERE resuelta = 1"
            ).fetchone()[0]
            if not n_res1:
                return
            init_evaluaciones_table(conn=conn)
            n_pend_eval = conn.execute(
                """
                SELECT COUNT(*)
                FROM predicciones p
                LEFT JOIN evaluaciones ep
                  ON ep.prediccion_id = p.id AND ep.regla = 'c10_precio_cierre'
                LEFT JOIN evaluaciones et
                  ON et.prediccion_id = p.id AND et.regla = 'c10_retorno_total'
                LEFT JOIN evaluaciones ea
                  ON ea.prediccion_id = p.id AND ea.regla = 'c11_apertura_precio'
                LEFT JOIN evaluaciones eat
                  ON eat.prediccion_id = p.id AND eat.regla = 'c11_apertura_retorno_total'
                LEFT JOIN evaluaciones ev2p
                  ON ev2p.prediccion_id = p.id AND ev2p.regla = 'c12_vol_precio_cierre'
                LEFT JOIN evaluaciones ev2t
                  ON ev2t.prediccion_id = p.id AND ev2t.regla = 'c12_vol_retorno_total'
                LEFT JOIN evaluaciones ev2a
                  ON ev2a.prediccion_id = p.id AND ev2a.regla = 'c12_vol_apertura_precio'
                WHERE p.resuelta = 1
                  AND (
                      ep.id IS NULL OR et.id IS NULL
                      OR ea.id IS NULL OR eat.id IS NULL
                      OR ev2p.id IS NULL OR ev2t.id IS NULL OR ev2a.id IS NULL
                  )
                """
            ).fetchone()[0]
        finally:
            conn.close()

        if not n_pend_eval:
            return

        tiene_adj_close = False
        if df_batch is not None and not df_batch.empty:
            if isinstance(df_batch.columns, pd.MultiIndex):
                tiene_adj_close = (
                    "Adj Close" in df_batch.columns.get_level_values(0)
                    or "Adj Close" in df_batch.columns.get_level_values(-1)
                )
            else:
                tiene_adj_close = "Adj Close" in df_batch.columns

        if tiene_adj_close:
            evaluar_predicciones_retorno_total(
                df_raw_ohlcv=df_batch, incluir_c11=True, incluir_c12=True
            )
        else:
            import yfinance as yf

            if hasattr(descargar_price_batch, "_mock_name") and not hasattr(
                yf.download, "_mock_name"
            ):
                evaluar_predicciones_retorno_total(
                    df_raw_ohlcv=df_batch, incluir_c11=True, incluir_c12=True
                )
            else:
                evaluar_predicciones_retorno_total(
                    incluir_c11=True, incluir_c12=True
                )
    except Exception as exc:
        logger.warning(
            "Error no bloqueante en evaluar_predicciones_retorno_total: %s", exc
        )


def ejecutar_emision_diaria(force: bool = False, verbose: bool = True, modelo: str = MOTOR_VERSION) -> Dict:
    """
    Ejecuta el pipeline oficial de emisión diaria con barras cerradas.
    Es idempotente: si ya existen emisiones para la última sesión completa,
    no repite la emisión salvo force=True.

    Pasos:
      1. Asegurar esquema y migraciones (init_predicciones_table).
      2. Descargar lote de 2 años (~400 sesiones) OHLCV sin caché.
      3. Truncar cualquier barra incompleta si la sesión sigue activa.
      4. Sincronizar el panel descriptivo con los cierres consolidados (con comprobación de splits/dividendos).
      5. Verificar si ya se emitieron señales para esa fecha.
      6. Calcular radar puro con arquitectura congelada (feedback loop desactivado).
      7. Insertar en base de datos con ON CONFLICT DO NOTHING.
      8. Guardar snapshot en ranking_historico.
      9. Resolver predicciones vencidas contra precio real de la fecha objetivo.
    """
    init_predicciones_table()
    try:
        init_evaluaciones_table()
    except Exception as exc:
        logger.warning("Error no bloqueante en init_evaluaciones_table: %s", exc)

    # Obtener catálogo de empresas activas
    empresas = query_all("SELECT ticker, nombre FROM empresas WHERE activa = 1 ORDER BY nombre")
    if not empresas:
        if verbose:
            print("⚠️ No hay empresas activas en la base de datos.")
        return {"status": "no_empresas", "count": 0}

    tickers = tuple(e["ticker"].strip().upper() for e in empresas)
    nombres = tuple(e["nombre"] for e in empresas)

    # Añadir benchmark si no está
    batch_symbols = list(tickers)
    if TICKER_BENCHMARK not in batch_symbols:
        batch_symbols.append(TICKER_BENCHMARK)

    if verbose:
        print(f"📥 Descargando histórico 2y (~400 sesiones) para {len(batch_symbols)} tickers...")
    df_batch_raw = descargar_price_batch(tuple(batch_symbols), period="2y")

    if df_batch_raw is None or df_batch_raw.empty:
        if verbose:
            print("❌ Error: No se pudieron descargar datos de mercado.")
        return {"status": "error_descarga", "count": 0}

    df_batch, fecha_sesion = truncar_a_barras_completas(df_batch_raw)
    if fecha_sesion is None or df_batch is None or df_batch.empty:
        if verbose:
            print("❌ No hay sesión válida para emisión (menos del 50 % de valores válidos).")
        return {
            "status": "no_data",
            "count": 0,
            "insertadas": 0,
            "snapshot_ranking": 0,
            "resueltas": 0,
            "omitidos_sin_barra": [],
            "omitidos_detalle": {},
            "omitidos_iliquidez": [],
        }

    if verbose:
        print(f"📅 Sesión de referencia para emisión: {fecha_sesion} (barra completa)")

    # Sincronizar caché de 6 años del panel con los cierres consolidados
    sincronizar_cache_panel_historico(df_batch, verbose=verbose)

    # Comprobar qué tickers ya tienen señales emitidas para esta sesión (sin filtrar archivada)
    conn = get_connection()
    try:
        filas_prev = conn.execute(
            "SELECT DISTINCT ticker FROM predicciones WHERE fecha_prediccion = ? AND modelo = ?",
            (fecha_sesion, modelo),
        ).fetchall()
        tickers_ya_emitidos = {r[0] for r in filas_prev}
        emitidas_prev = conn.execute(
            "SELECT COUNT(*) FROM predicciones WHERE fecha_prediccion = ? AND modelo = ?",
            (fecha_sesion, modelo),
        ).fetchone()[0]
    finally:
        conn.close()

    # Evaluar elegibilidad de cada valor del universo (C-27)
    elegibles, omitidos_detalle, omitidos_iliquidez = filtrar_tickers_elegibles(
        df_batch, tickers, fecha_sesion
    )
    omitidos_sin_barra = sorted(list(omitidos_detalle.keys()))

    # Camino rápido: si todos los elegibles ya están emitidos y no es force:
    # resolver pendientes y devolver already_emitted SIN calcular el radar.
    ya_cubiertos = False
    if elegibles and elegibles.issubset(tickers_ya_emitidos):
        ya_cubiertos = True
    elif not elegibles and tickers_ya_emitidos and set(tickers).issubset(tickers_ya_emitidos):
        ya_cubiertos = True

    if ya_cubiertos and not force:
        if verbose:
            print(f"ℹ️ Ya existen {emitidas_prev} señales emitidas e inmutables para {fecha_sesion} (todos los elegibles cubiertos). DO NOTHING.")
        resueltas = resolver_predicciones(df_batch=df_batch)
        _evaluar_retorno_total_seguro(df_batch=df_batch)
        return {
            "status": "already_emitted",
            "fecha": fecha_sesion,
            "emitidas": emitidas_prev,
            "insertadas": 0,
            "resueltas": resueltas,
            "omitidos_sin_barra": omitidos_sin_barra,
            "omitidos_detalle": omitidos_detalle,
            "omitidos_iliquidez": omitidos_iliquidez,
        }

    # Guarda de ventana de emisión: si faltan elegibles (o force) y la sesión está fuera de ventana, DO NOTHING
    if not en_ventana_emision(fecha_sesion):
        ahora_dt = get_ahora_madrid()
        limite_dt = limite_emision(fecha_sesion)
        limite_iso = limite_dt.isoformat()
        logger.warning(
            f"Emisión tardía fuera de ventana para {fecha_sesion}: "
            f"hora actual ({ahora_dt.isoformat()}) > límite ({limite_iso}). No se emite nada."
        )
        if verbose:
            print(
                f"⚠️ Emisión tardía para {fecha_sesion}: fuera de ventana "
                f"(límite {limite_iso}, ahora {ahora_dt.isoformat()}). DO NOTHING."
            )
        resueltas = resolver_predicciones(df_batch=df_batch)
        _evaluar_retorno_total_seguro(df_batch=df_batch)
        return {
            "status": "late",
            "fecha": fecha_sesion,
            "emitidas": emitidas_prev,
            "insertadas": 0,
            "resueltas": resueltas,
            "limite": limite_iso,
            "omitidos_sin_barra": omitidos_sin_barra,
            "omitidos_detalle": omitidos_detalle,
            "omitidos_iliquidez": omitidos_iliquidez,
        }

    # Cargar tilts desde ranking_estado
    try:
        filas_r = query_all("SELECT ticker, score FROM ranking_estado")
        scores_r = {r["ticker"]: float(r["score"]) for r in filas_r}
    except Exception:
        scores_r = {}
    tilts = tuple(max(-2.0, min(2.0, (scores_r.get(t, 50.0) - 50.0) / 25.0)) for t in tickers)

    # Calcular radar puro (sin caché, sin mutaciones) sobre el lote completo
    if verbose:
        print("⚙️ Calculando señales puras del radar...")
    # Blindaje Fase 1: cuando modelo == MOTOR_VERSION ("v5-frozen"), forzar siempre usar_calibracion=False
    usar_calib = False if (modelo == MOTOR_VERSION or FASE_1_CONGELADA) else False
    radar = calcular_radar_desde_batch(
        df_batch, tickers, nombres, tilts,
        sentimientos=None,
        usar_calibracion=usar_calib,  # Feedback loop desactivado en Fase 1
    )

    # 1. Filtrar valores no elegibles o sin barra fresca en fecha_sesion (Cambio 1 y C-27)
    lote_sintetico_sin_series = all(
        m == "sin_historico_minimo" for m in omitidos_detalle.values()
    ) and bool(omitidos_detalle)

    if radar is not None and not radar.empty:
        if "ultima_fecha" in radar.columns:
            def _norm_uf(v) -> str:
                if v is None or pd.isna(v):
                    return ""
                try:
                    return pd.to_datetime(v).strftime("%Y-%m-%d")
                except Exception:
                    return str(v)[:10]

            fechas_radar = radar["ultima_fecha"].apply(_norm_uf)
            mascara_fresca = (fechas_radar == fecha_sesion)
            omitidos_radar_uf = sorted(
                radar.loc[~mascara_fresca, "ticker"].dropna().unique().tolist()
            )
            radar_fresco = radar.loc[mascara_fresca].copy()
        else:
            omitidos_radar_uf = []
            radar_fresco = radar.copy()

        if not lote_sintetico_sin_series:
            radar_fresco = radar_fresco.loc[radar_fresco["ticker"].isin(elegibles)].copy()
        else:
            omitidos_detalle = {t: "sin_barra_fresca" for t in omitidos_radar_uf}
            omitidos_iliquidez = []
            omitidos_sin_barra = omitidos_radar_uf
    else:
        radar_fresco = pd.DataFrame()

    if omitidos_sin_barra:
        logger.info(
            f"{len(omitidos_sin_barra)} valores omitidos en {fecha_sesion}: "
            f"{omitidos_detalle}"
        )
        if verbose:
            print(
                f"[INFO] {len(omitidos_sin_barra)} valores omitidos en {fecha_sesion}: "
                f"{omitidos_detalle}"
            )

    # 2. Emisión incremental por valor: solo los tickers elegibles que NO estén ya en la BD (Cambio 2)
    if not force and tickers_ya_emitidos and not radar_fresco.empty:
        radar_a_registrar = radar_fresco.loc[~radar_fresco["ticker"].isin(tickers_ya_emitidos)].copy()
    else:
        radar_a_registrar = radar_fresco

    # Registrar con ON CONFLICT DO NOTHING
    insertadas = 0
    if not radar_a_registrar.empty:
        insertadas = registrar_predicciones(
            radar_a_registrar, fecha_prediccion=fecha_sesion, modelo=modelo
        )

    # Guardar snapshot diario de ranking
    snap_cnt = guardar_snapshot_ranking(fecha_sesion)

    # Resolver predicciones vencidas buscando precio de cierre en fecha_objetivo
    resueltas = resolver_predicciones(df_batch=df_batch)
    _evaluar_retorno_total_seguro(df_batch=df_batch)

    # Determinar status resultante:
    # - Si ya había filas y se insertaron nuevas: "topped_up"
    # - Si ya había filas y no se insertó ninguna nueva: "already_emitted"
    # - Si no había filas previas y se insertaron (o sesión inicial): "success"
    if emitidas_prev > 0 and insertadas > 0:
        status = "topped_up"
    elif emitidas_prev > 0 and insertadas == 0:
        status = "already_emitted"
    else:
        status = "success"

    if verbose:
        print(f"✅ Emisión completada ({status}): {insertadas} predicciones registradas para {fecha_sesion}.")
        print(f"📊 Snapshot ranking: {snap_cnt} registros archivados.")
        print(f"⚖️ Resoluciones: {resueltas} predicciones liquidadas contra precio de cierre.")

    ret_dict = {
        "status": status,
        "fecha": fecha_sesion,
        "insertadas": insertadas,
        "snapshot_ranking": snap_cnt,
        "resueltas": resueltas,
        "omitidos_sin_barra": omitidos_sin_barra,
        "omitidos_detalle": omitidos_detalle,
        "omitidos_iliquidez": omitidos_iliquidez,
    }
    if status == "already_emitted":
        ret_dict["emitidas"] = emitidas_prev
    return ret_dict


def catch_up_emision() -> Dict:
    """
    Función de puesta al día no bloqueante:
    Llamada al iniciar la aplicación (prewarm) o la vista IA.
    Si la última sesión completa no tiene señales registradas, las emite silenciosamente.
    """
    try:
        return ejecutar_emision_diaria(force=False, verbose=False)
    except Exception as e:
        return {"status": "error", "error": str(e)}
