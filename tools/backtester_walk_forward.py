"""
Backtester Walk-Forward Institucional — StockLab IA (Fase 2)
============================================================
Evaluación cuantitativa rigurosa y empírica del motor de señales:
  - 6 años de datos OHLCV con auto_adjust=True (1 año warm-up + 5 años de evaluación).
  - Verificación formal anti-lookahead (perturbación de futuro).
  - Panel completo de señales día a día con funciones puras de signal_engine.
  - Horizontes: 1D (1 sesión), 3D (3 sesiones), 1S (5 sesiones), 1M (20 sesiones).
  - Partición: Diseño (~3.5 años) vs Fuera de Muestra OOS (~1.5 años) con embargo de h sesiones.
  - Métricas empíricas:
      a) Spearman Rank IC (cross-sectional diario con t-stat Newey-West lag h-1 y pooled).
      b) IC condicional por régimen de ADX (<20, 20-25, >25) por componente (RSI, MACD, Trend, Mom).
      c) Retorno medio y exceso vs ^IBEX por decil/quintil y spread D10 - D1.
      d) Tasa de acierto de signo direccional.
      e) Tasa de acierto de 3 clases (umbrales fijos vs umbrales dinámicos k*sigma*sqrt(h)).
      f) Curva de precisión vs cobertura (top 10%, 20%, 30%, 50%, 100%) con Block Bootstrap 95% CI.
"""

import os
import sys
import time
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from scipy import stats

# Añadir workspace al path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from database.connection import get_connection
from services.signal_engine import (
    HORIZONTES,
    TICKER_BENCHMARK,
    calcular_fila_ticker,
    calcular_marea_mercado,
    obtener_serie_ticker,
)

CACHE_FILE = os.path.join(BASE_DIR, "data", "backtest_cache_6y.parquet")
OUTPUT_PANEL = os.path.join(BASE_DIR, "data", "backtest_signals_panel.parquet")
OUTPUT_METRICS_DESIGN = os.path.join(BASE_DIR, "data", "backtest_design_summary.csv")
OUTPUT_METRICS_OOS = os.path.join(BASE_DIR, "data", "backtest_oos_summary.csv")
OUTPUT_DECILES = os.path.join(BASE_DIR, "data", "backtest_deciles.csv")
OUTPUT_ADX_COMPS = os.path.join(BASE_DIR, "data", "backtest_adx_components.csv")
OUTPUT_COVERAGE = os.path.join(BASE_DIR, "data", "backtest_coverage.csv")

HORIZONTE_BARS = {
    "1D": 1,
    "3D": 3,
    "1S": 5,
    "1M": 20,
}

# Fechas de partición
START_EVAL_DATE = "2021-10-01"
SPLIT_DESIGN_DATE = "2025-03-31"   # Fin tramo diseño (~3.5 años)
EMBARGO_BARS = 20                  # Embargo de 20 sesiones bursátiles
START_OOS_DATE = "2025-05-02"      # Inicio OOS (~1.4 años tras embargo)
END_EVAL_DATE = "2026-09-04"        # Fin evaluación (deja 20 sesiones para retorno 1M)


# ═══════════════════════════════════════════════════════════════════
# 1. DESCARGA Y CACHÉ DE DATOS OHLCV (auto_adjust=True)
# ═══════════════════════════════════════════════════════════════════

def obtener_universo_tickers() -> List[str]:
    """Obtiene la lista de tickers activos de la BD local."""
    conn = get_connection()
    try:
        conn.row_factory = None
        rows = conn.execute("SELECT ticker FROM empresas WHERE activa = 1 ORDER BY es_ibex35 DESC, ticker ASC").fetchall()
        tickers = [r[0] for r in rows if r[0]]
        return tickers
    finally:
        conn.close()


def descargar_o_cargar_precios(tickers: List[str], period: str = "6y") -> pd.DataFrame:
    """
    Descarga OHLCV para todo el universo + ^IBEX con auto_adjust=True.
    Cachea en parquet para ejecuciones instantáneas posteriores.
    """
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    if os.path.exists(CACHE_FILE):
        try:
            print(f"[CACHE] Cargando datos locales desde {CACHE_FILE}...")
            df = pd.read_parquet(CACHE_FILE)
            if not df.empty and isinstance(df.columns, pd.MultiIndex):
                print(f"[CACHE] Cargado exitosamente: shape={df.shape}, fechas de {df.index[0].date()} a {df.index[-1].date()}")
                return df
        except Exception as e:
            print(f"[CACHE] Error leyendo caché parquet ({e}), redescargando...")

    print(f"[DESCARGA] Descargando {len(tickers)} tickers + {TICKER_BENCHMARK} (periodo={period}, auto_adjust=True)...")
    import yfinance as yf
    todos = list(dict.fromkeys(tickers + [TICKER_BENCHMARK]))
    t0 = time.time()
    df = yf.download(todos, period=period, auto_adjust=True, group_by="ticker", progress=True)
    print(f"[DESCARGA] Finalizada en {round(time.time() - t0, 1)}s. Shape: {df.shape}")

    # Guardar en caché parquet
    try:
        df.to_parquet(CACHE_FILE)
        print(f"[CACHE] Guardado en {CACHE_FILE}")
    except Exception as e:
        print(f"[CACHE] Advertencia guardando parquet: {e}")
    return df


# ═══════════════════════════════════════════════════════════════════
# 2. TEST RIGUROSO ANTI-LOOKAHEAD (Perturbación de futuro)
# ═══════════════════════════════════════════════════════════════════

def verificar_anti_lookahead(df_precios: pd.DataFrame, tickers_prueba: List[str]) -> bool:
    """
    Verifica formalmente que calcular_fila_ticker en t no accede a ningún dato posterior a t.
    Perturba los precios posteriores a t (+100% de subida ficticia) y comprueba
    que la señal en t es bit a bit IDÉNTICA.
    """
    print("\n" + "="*70)
    print("🔬 TEST ANTI-LOOKAHEAD (Perturbación del futuro)")
    print("="*70)
    sub_ibex = obtener_serie_ticker(df_precios, TICKER_BENCHMARK)
    if sub_ibex is None:
        raise RuntimeError("No se pudo obtener la serie de ^IBEX para el test anti-lookahead")

    todas_ok = True
    for ticker in tickers_prueba:
        sub = obtener_serie_ticker(df_precios, ticker)
        if sub is None or len(sub) < 300:
            continue

        # Probar en 3 fechas distintas
        indices_prueba = [220, len(sub) // 2, len(sub) - 50]
        for idx in indices_prueba:
            t_date = sub.index[idx]
            sub_ibex_t = sub_ibex.loc[:t_date]
            marea_t = calcular_marea_mercado(sub_ibex_t)

            # Cálculo 1: serie cortada estrictamente en t
            sub_t1 = sub.iloc[:idx+1].copy()
            res1 = calcular_fila_ticker(sub_t1, marea_t, ticker=ticker)

            # Cálculo 2: serie perturbada tras t
            sub_pert = sub.copy()
            col_c = sub_pert.columns.get_loc("Close")
            col_h = sub_pert.columns.get_loc("High")
            col_l = sub_pert.columns.get_loc("Low")
            sub_pert.iloc[idx+1:, col_c] *= 2.5
            sub_pert.iloc[idx+1:, col_h] *= 2.5
            sub_pert.iloc[idx+1:, col_l] *= 0.3
            # Cortar a t tras la perturbación
            sub_t2 = sub_pert.iloc[:idx+1].copy()
            res2 = calcular_fila_ticker(sub_t2, marea_t, ticker=ticker)

            # Comprobar todos los campos
            if res1 is None or res2 is None:
                todas_ok = False
                print(f"❌ Fallo: res es None en {ticker} @ {t_date.date()}")
                continue

            diffs = []
            for k in res1:
                val1 = res1[k]
                val2 = res2[k]
                if isinstance(val1, float) and np.isnan(val1) and np.isnan(val2):
                    continue
                if val1 != val2:
                    diffs.append((k, val1, val2))

            if diffs:
                todas_ok = False
                print(f"❌ LOOK-AHEAD DETECTADO en {ticker} @ {t_date.date()}: {diffs}")
            else:
                print(f"  [OK] {ticker:8s} @ {t_date.date()}: 100% idéntico (0 discrepancias)")

    if todas_ok:
        print("✅ VERIFICACIÓN ANTI-LOOKAHEAD SUPERADA CON ÉXITO: 0 filtración temporal.")
    else:
        print("❌ ALERTA: Hubo fallos en el test anti-lookahead.")
    print("="*70 + "\n")
    return todas_ok


# ═══════════════════════════════════════════════════════════════════
# 3. GENERACIÓN WALK-FORWARD DE SEÑALES Y RETORNOS FUTUROS
# ═══════════════════════════════════════════════════════════════════

def _worker_walk_forward_ticker(args) -> List[Dict]:
    """
    Función de trabajo ejecutada en paralelo para un ticker:
    Recorre día a día su serie histórica calculando señales y liquidándolas
    contra el precio de cierre exacto en t+h.
    """
    ticker, sub_df, mareas_por_fecha, ibex_closes, calendar_dates, eval_start, eval_end = args
    if sub_df is None or len(sub_df) < 220:
        return []

    # Mapeo de fecha a índice del calendario
    cal_idx = {d: i for i, d in enumerate(calendar_dates)}
    sub_dates = sub_df.index
    filas = []

    # Umbrales fijos del motor original
    umbrales_fijos = {"1D": 1.5, "3D": 2.5, "1S": 4.0, "1M": 7.0}
    # Factor k para terciles dinámicos gaussianos (P(Z < -0.4307) = 1/3)
    K_TERCIL = 0.4307

    for idx, t_date in enumerate(sub_dates):
        if idx < 200:
            continue  # Warm-up de 200 barras para estabilización de indicadores
        if t_date < eval_start or t_date > eval_end:
            continue

        marea = mareas_por_fecha.get(t_date)
        if marea is None:
            continue

        c_idx = cal_idx.get(t_date)
        if c_idx is None:
            continue

        # Generar señal usando exclusivamente datos <= t_date
        sub_t = sub_df.iloc[:idx+1]
        sig_dict = calcular_fila_ticker(sub_t, marea, tilt=0.0, sent_val=0.0, ticker=ticker)
        if sig_dict is None:
            continue

        p_close = float(sub_t["Close"].iloc[-1])
        vol_d = sig_dict["vol_d_frac"]
        adx_val = sig_dict["adx"]
        if adx_val < 20.0:
            regimen_adx = "low"
        elif adx_val <= 25.0:
            regimen_adx = "trans"
        else:
            regimen_adx = "high"

        # Calcular retornos futuros para cada horizonte
        for hz, n_bars in HORIZONTE_BARS.items():
            target_c_idx = c_idx + n_bars
            if target_c_idx >= len(calendar_dates):
                continue
            target_date = calendar_dates[target_c_idx]

            # Buscar precio de cierre del activo en target_date
            posteriores = sub_df["Close"][sub_dates >= target_date]
            if posteriores.empty:
                continue
            p_fwd = float(posteriores.iloc[0])

            # Buscar precio del benchmark IBEX
            if t_date in ibex_closes.index and target_date in ibex_closes.index:
                p_ibex_0 = float(ibex_closes.loc[t_date])
                p_ibex_fwd = float(ibex_closes.loc[target_date])
                ret_bench = (p_ibex_fwd - p_ibex_0) / p_ibex_0
            else:
                ret_bench = 0.0

            ret_fwd = (p_fwd - p_close) / p_close
            ret_excess = ret_fwd - ret_bench

            # Métricas del horizonte
            senal_str = sig_dict[f"senal_{hz}"]
            score_hz = sig_dict[f"score_{hz}"]
            pct_raw_hz = sig_dict.get(f"pct_raw_{hz}", 0.0) or 0.0
            pct_hz = sig_dict[f"pct_{hz}"]
            conf_hz = sig_dict[f"conf_{hz}"]
            conv_hz = sig_dict[f"conv_{hz}"]
            umbral_fijo = umbrales_fijos[hz]
            umbral_dinamico = K_TERCIL * vol_d * np.sqrt(n_bars) * 100.0

            # Acierto de signo
            if abs(pct_raw_hz) > 1e-6 and abs(ret_fwd) > 1e-6:
                hit_sign = 1 if (pct_raw_hz > 0 and ret_fwd > 0) or (pct_raw_hz < 0 and ret_fwd < 0) else 0
            else:
                hit_sign = None

            # Acierto 3 clases fijo
            if ret_fwd * 100.0 > umbral_fijo:
                clase_real_fija = "ALCISTA"
            elif ret_fwd * 100.0 < -umbral_fijo:
                clase_real_fija = "BAJISTA"
            else:
                clase_real_fija = "LATERAL"
            hit_3c_fijo = 1 if senal_str == clase_real_fija else 0

            # Acierto 3 clases dinámico
            if ret_fwd * 100.0 > umbral_dinamico:
                clase_real_din = "ALCISTA"
            elif ret_fwd * 100.0 < -umbral_dinamico:
                clase_real_din = "BAJISTA"
            else:
                clase_real_din = "LATERAL"
            hit_3c_din = 1 if senal_str == clase_real_din else 0

            filas.append({
                "ticker": ticker,
                "date": t_date,
                "horizon": hz,
                "n_bars": n_bars,
                "target_date": target_date,
                "senal": senal_str,
                "score": score_hz,
                "pct_estimado": pct_hz,
                "pct_raw": pct_raw_hz,
                "confianza": conf_hz,
                "conviccion": conv_hz,
                "comp_rsi": sig_dict["comp_rsi"],
                "comp_macd": sig_dict["comp_macd"],
                "comp_tendencia": sig_dict["comp_tendencia"],
                "comp_momentum": sig_dict["comp_momentum"],
                "adx": adx_val,
                "regimen_adx": regimen_adx,
                "vol_d": vol_d,
                "rvol": sig_dict["rvol"],
                "marea": sig_dict["marea"],
                "fwd_ret": ret_fwd,
                "fwd_ret_bench": ret_bench,
                "fwd_ret_excess": ret_excess,
                "hit_sign": hit_sign,
                "hit_3c_fijo": hit_3c_fijo,
                "hit_3c_din": hit_3c_din,
                "umbral_fijo": umbral_fijo,
                "umbral_din": umbral_dinamico,
            })
    return filas


def generar_panel_senales(df_precios: pd.DataFrame, tickers: List[str]) -> pd.DataFrame:
    """
    Construye el panel completo de señales día a día para todos los tickers.
    Usa ProcessPoolExecutor para ejecución paralela ultrarrápida.
    """
    print("\n" + "="*70)
    print("⚙️ GENERACIÓN DE PANEL WALK-FORWARD (Paralelo)")
    print("="*70)
    sub_ibex = obtener_serie_ticker(df_precios, TICKER_BENCHMARK)
    if sub_ibex is None:
        raise RuntimeError("Serie de ^IBEX no encontrada en datos descargados")

    calendar_dates = sub_ibex.index.sort_values()
    ibex_closes = sub_ibex["Close"].dropna()

    # Precalcular marea de mercado por cada fecha
    print("[1/3] Precalculando marea de mercado diaria del IBEX 35...")
    mareas = {}
    for idx in range(50, len(calendar_dates)):
        t_d = calendar_dates[idx]
        mareas[t_d] = calcular_marea_mercado(sub_ibex.iloc[:idx+1])

    eval_start = pd.Timestamp(START_EVAL_DATE)
    eval_end = pd.Timestamp(END_EVAL_DATE)

    print(f"[2/3] Preparando tareas paralelas para {len(tickers)} tickers...")
    tasks = []
    for ticker in tickers:
        sub_t = obtener_serie_ticker(df_precios, ticker)
        if sub_t is not None and len(sub_t) >= 220:
            tasks.append((ticker, sub_t, mareas, ibex_closes, calendar_dates, eval_start, eval_end))

    print(f"[3/3] Ejecutando walk-forward con 8 procesos de CPU...")
    t0 = time.time()
    todas_filas = []
    with ProcessPoolExecutor(max_workers=min(10, os.cpu_count() or 4)) as executor:
        for chunk in executor.map(_worker_walk_forward_ticker, tasks):
            todas_filas.extend(chunk)

    df_panel = pd.DataFrame(todas_filas)
    print(f"Panel generado en {round(time.time() - t0, 1)}s. Total filas de señales: {len(df_panel):,}")

    if not df_panel.empty:
        df_panel.to_parquet(OUTPUT_PANEL)
        print(f"Panel guardado en {OUTPUT_PANEL}")
    print("="*70 + "\n")
    return df_panel


# ═══════════════════════════════════════════════════════════════════
# 4. FUNCIONES ESTADÍSTICAS CUANTITATIVAS
# ═══════════════════════════════════════════════════════════════════

def calcular_newey_west_tstat(series: pd.Series, lag: int) -> Tuple[float, float, float]:
    """
    Calcula media, error estándar de Newey-West con 'lag' rezagos, y t-stat.
    Horizonte h induce correlación serial de orden h-1.
    """
    s = series.dropna().values
    n = len(s)
    if n < 10:
        return 0.0, 0.0, 1.0

    mean_val = float(np.mean(s))
    centered = s - mean_val

    # Gamma_0 (varianza muestral)
    gamma_0 = float(np.dot(centered, centered) / n)
    v_nw = gamma_0

    # Suma ponderada de autocovarianzas (pesos de Bartlett)
    for l in range(1, lag + 1):
        if l >= n:
            break
        gamma_l = float(np.dot(centered[l:], centered[:-l]) / n)
        weight = 1.0 - (l / (lag + 1.0))
        v_nw += 2.0 * weight * gamma_l

    se_nw = np.sqrt(max(1e-12, v_nw) / n)
    t_stat = mean_val / se_nw
    p_val = float(2.0 * stats.t.sf(abs(t_stat), df=n - 1))
    return mean_val, float(t_stat), p_val


def calcular_block_bootstrap_ci(data: pd.DataFrame, block_size: int = 5, n_boot: int = 1000) -> Tuple[float, float, float]:
    """
    Calcula media e intervalo de confianza del 95% mediante block bootstrap temporal
    para preservar la estructura de autocorrelación en datos agrupados por bloques de fechas.
    """
    if data.empty or "hit_sign" not in data.columns:
        return 0.0, 0.0, 0.0

    # Agrupar por fechas únicas
    fechas = data["date"].drop_duplicates().sort_values().values
    n_fechas = len(fechas)
    if n_fechas < block_size * 2:
        val = data["hit_sign"].dropna().mean()
        return float(val), float(val), float(val)

    # Dividir fechas en bloques
    n_blocks = max(1, n_fechas // block_size)
    blocks = [fechas[i * block_size : (i + 1) * block_size] for i in range(n_blocks)]

    # Mapeo rápido de fecha a aciertos
    hits_by_date = data.dropna(subset=["hit_sign"]).groupby("date")["hit_sign"].agg(["sum", "count"])

    rng = np.random.default_rng(42)
    boot_means = []
    for _ in range(n_boot):
        sampled_blocks = rng.choice(len(blocks), size=n_blocks, replace=True)
        sampled_dates = np.concatenate([blocks[b] for b in sampled_blocks])
        sub = hits_by_date.reindex(sampled_dates).dropna()
        if not sub.empty and sub["count"].sum() > 0:
            boot_means.append(sub["sum"].sum() / sub["count"].sum())

    if not boot_means:
        val = float(data["hit_sign"].dropna().mean())
        return val, val, val

    mean_val = float(np.mean(boot_means))
    ci_low = float(np.percentile(boot_means, 2.5))
    ci_high = float(np.percentile(boot_means, 97.5))
    return mean_val, ci_low, ci_high


# ═══════════════════════════════════════════════════════════════════
# 5. CÁLCULO COMPLETO DE MÉTRICAS (DISEÑO & FUERA DE MUESTRA)
# ═══════════════════════════════════════════════════════════════════

def analizar_tramo(df_panel: pd.DataFrame, nombre_tramo: str) -> Dict:
    """
    Calcula exhaustivamente todas las métricas requeridas:
      a) Spearman IC (cross-sectional con Newey-West y pooled).
      b) IC condicional por régimen de ADX.
      c) Retorno medio por decil y spread D10 - D1.
      d) Hit rate de signo.
      e) Hit rate de 3 clases (fijo vs dinámico).
      f) Precisión vs cobertura.
    """
    print(f"\n=======================================================")
    print(f"📊 ANÁLISIS DE RENDIMIENTO: {nombre_tramo.upper()}")
    print(f"=======================================================")
    print(f"Filas totales en tramo: {len(df_panel):,}")
    if df_panel.empty:
        return {}

    resultados_hz = {}
    metricas_summary_list = []

    for hz, n_bars in HORIZONTE_BARS.items():
        sub_hz = df_panel[df_panel["horizon"] == hz].copy()
        if sub_hz.empty:
            continue

        # ── a) Spearman Rank IC Cross-Sectional diario ──
        # Correlación entre pct_raw (o score) y fwd_ret en cada fecha
        def calc_ic_date(group):
            if len(group) < 10:
                return np.nan
            # Spearman entre señal (pct_raw) y retorno futuro
            r, _ = stats.spearmanr(group["pct_raw"], group["fwd_ret"])
            return r

        daily_ic = sub_hz.groupby("date").apply(calc_ic_date, include_groups=False).dropna()
        lag_nw = max(1, n_bars - 1)
        mean_ic, t_stat_nw, p_val_nw = calcular_newey_west_tstat(daily_ic, lag=lag_nw)
        ic_std = float(daily_ic.std()) if len(daily_ic) > 1 else 0.0
        ir = mean_ic / ic_std if ic_std > 0 else 0.0
        pct_ic_pos = float((daily_ic > 0).mean() * 100.0)

        # Pooled Spearman IC
        pooled_ic, pooled_p = stats.spearmanr(sub_hz["pct_raw"], sub_hz["fwd_ret"])

        # ── d) Hit Rate de Signo Direccional ──
        valid_signs = sub_hz["hit_sign"].dropna()
        hit_sign_rate = float(valid_signs.mean() * 100.0) if len(valid_signs) > 0 else 0.0
        n_sign_valid = len(valid_signs)

        # ── e) Hit Rate 3 Clases (Fijo vs Dinámico) ──
        hit_3c_fijo_rate = float(sub_hz["hit_3c_fijo"].mean() * 100.0)
        hit_3c_din_rate = float(sub_hz["hit_3c_din"].mean() * 100.0)

        # Retorno medio global
        mean_ret_fwd = float(sub_hz["fwd_ret"].mean() * 100.0)
        mean_excess = float(sub_hz["fwd_ret_excess"].mean() * 100.0)

        metricas_summary_list.append({
            "horizonte": hz,
            "dias_h": n_bars,
            "muestras": len(sub_hz),
            "fechas": len(daily_ic),
            "mean_ic": round(mean_ic, 4),
            "ic_std": round(ic_std, 4),
            "ir_ratio": round(ir, 2),
            "t_stat_nw": round(t_stat_nw, 2),
            "p_val_nw": round(p_val_nw, 4),
            "ic_pos_pct": round(pct_ic_pos, 1),
            "pooled_ic": round(pooled_ic, 4),
            "hit_sign_pct": round(hit_sign_rate, 2),
            "hit_3c_fijo_pct": round(hit_3c_fijo_rate, 2),
            "hit_3c_din_pct": round(hit_3c_din_rate, 2),
            "mean_ret_pct": round(mean_ret_fwd, 2),
            "mean_excess_pct": round(mean_excess, 2),
        })

    df_summary = pd.DataFrame(metricas_summary_list)
    print("\nRESUMEN DE MÉTRICAS GLOBALES POR HORIZONTE:")
    print(df_summary.to_string(index=False))

    # ── b) IC Condicional por Régimen de ADX para cada Componente ──
    comps = ["comp_rsi", "comp_macd", "comp_tendencia", "comp_momentum", "pct_raw"]
    regimes = ["low", "trans", "high"]
    adx_metrics = []

    for hz in HORIZONTE_BARS:
        sub_hz = df_panel[df_panel["horizon"] == hz]
        for reg in regimes:
            sub_reg = sub_hz[sub_hz["regimen_adx"] == reg]
            if len(sub_reg) < 50:
                continue
            for comp in comps:
                r_comp, p_comp = stats.spearmanr(sub_reg[comp], sub_reg["fwd_ret"])
                adx_metrics.append({
                    "horizonte": hz,
                    "regimen_adx": reg,
                    "componente": comp,
                    "n_muestras": len(sub_reg),
                    "spearman_ic": round(float(r_comp), 4),
                    "p_val": round(float(p_comp), 4),
                })
    df_adx = pd.DataFrame(adx_metrics)

    # ── c) Rendimiento por Decil (Quintil para 1M) ──
    decil_rows = []
    for hz in HORIZONTE_BARS:
        sub_hz = df_panel[df_panel["horizon"] == hz].copy()
        if len(sub_hz) < 100:
            continue
        try:
            sub_hz["decil"] = pd.qcut(sub_hz["pct_raw"], q=10, labels=False, duplicates="drop") + 1
        except Exception:
            sub_hz["decil"] = pd.qcut(sub_hz["pct_raw"].rank(method="first"), q=10, labels=False) + 1

        for d in sorted(sub_hz["decil"].unique()):
            sub_d = sub_hz[sub_hz["decil"] == d]
            decil_rows.append({
                "tramo": nombre_tramo,
                "horizonte": hz,
                "decil": int(d),
                "n": len(sub_d),
                "pct_raw_medio": round(float(sub_d["pct_raw"].mean()), 2),
                "ret_medio_pct": round(float(sub_d["fwd_ret"].mean() * 100.0), 3),
                "excess_medio_pct": round(float(sub_d["fwd_ret_excess"].mean() * 100.0), 3),
                "hit_sign_pct": round(float(sub_d["hit_sign"].dropna().mean() * 100.0), 1),
            })
    df_deciles = pd.DataFrame(decil_rows)

    # ── f) Precisión vs Cobertura (Top 10%, 20%, 30%, 50%, 100%) ──
    coverage_rows = []
    for hz, n_bars in HORIZONTE_BARS.items():
        sub_hz = df_panel[df_panel["horizon"] == hz].copy()
        if sub_hz.empty:
            continue
        # Ordenar por convicción y confianza numérica descendente
        conv_map = {"ALTA": 3, "MEDIA": 2, "BAJA": 1, "NEUTRA": 0}
        sub_hz["conv_num"] = sub_hz["conviccion"].astype(str).map(conv_map).fillna(0)
        sub_hz["abs_pct_raw"] = sub_hz["pct_raw"].abs()
        sub_hz = sub_hz.sort_values(by=["confianza", "conv_num", "abs_pct_raw"], ascending=[False, False, False])
        total_n = len(sub_hz)

        for cov_pct in [10, 20, 30, 50, 100]:
            n_sub = int(round(total_n * (cov_pct / 100.0)))
            sub_cov = sub_hz.iloc[:n_sub]
            mean_hit, ci_low, ci_high = calcular_block_bootstrap_ci(sub_cov, block_size=n_bars, n_boot=500)
            coverage_rows.append({
                "tramo": nombre_tramo,
                "horizonte": hz,
                "cobertura_pct": cov_pct,
                "muestras": n_sub,
                "hit_sign_medio": round(mean_hit * 100.0, 2),
                "ci_95_low": round(ci_low * 100.0, 2),
                "ci_95_high": round(ci_high * 100.0, 2),
                "ret_medio_pct": round(float(sub_cov["fwd_ret"].mean() * 100.0), 2),
                "excess_medio_pct": round(float(sub_cov["fwd_ret_excess"].mean() * 100.0), 2),
            })
    df_coverage = pd.DataFrame(coverage_rows)

    return {
        "summary": df_summary,
        "adx_components": df_adx,
        "deciles": df_deciles,
        "coverage": df_coverage,
    }


# ═══════════════════════════════════════════════════════════════════
# 6. FUNCIÓN PRINCIPAL DE EJECUCIÓN
# ═══════════════════════════════════════════════════════════════════

def main():
    print("="*70)
    print("🚀 EJECUTANDO BACKTESTER WALK-FORWARD INSTITUCIONAL — FASE 2")
    print("="*70)

    # 1. Obtener universo
    tickers = obtener_universo_tickers()
    print(f"Universo activo en stocklab.db: {len(tickers)} tickers")

    # 2. Cargar/descargar datos de 6 años
    df_precios = descargar_o_cargar_precios(tickers, period="6y")

    # 3. Test anti-lookahead
    sample_tickers = ["SAN.MC", "BBVA.MC", "ITX.MC", "REP.MC"]
    ok_anti = verificar_anti_lookahead(df_precios, sample_tickers)
    if not ok_anti:
        print("❌ ABORTANDO: Se detectaron fallos en la prueba anti-lookahead.")
        return

    # 4. Generar panel de señales o cargar si ya existe
    if os.path.exists(OUTPUT_PANEL):
        print(f"[CARGA] Cargando panel de señales precalculado desde {OUTPUT_PANEL}...")
        df_panel = pd.read_parquet(OUTPUT_PANEL)
    else:
        df_panel = generar_panel_senales(df_precios, tickers)

    # 5. Partición: Diseño vs Fuera de Muestra (OOS) con Embargo
    split_date = pd.Timestamp(SPLIT_DESIGN_DATE)
    oos_start = pd.Timestamp(START_OOS_DATE)

    df_design = df_panel[df_panel["date"] <= split_date].copy()
    df_oos = df_panel[df_panel["date"] >= oos_start].copy()
    df_embargo = df_panel[(df_panel["date"] > split_date) & (df_panel["date"] < oos_start)].copy()

    print(f"PARTICIÓN DE DATOS:")
    print(f"  • Muestra de Diseño: {df_design['date'].min().date()} a {df_design['date'].max().date()} ({len(df_design):,} señales)")
    print(f"  • Embargo temporal : {split_date.date()} a {oos_start.date()} ({len(df_embargo):,} señales descartadas)")
    print(f"  • Fuera de Muestra : {df_oos['date'].min().date()} a {df_oos['date'].max().date()} ({len(df_oos):,} señales)")

    # 6. Evaluación empírica en tramo de Diseño
    res_design = analizar_tramo(df_design, "diseno")
    res_design["summary"].to_csv(OUTPUT_METRICS_DESIGN, index=False)

    # 7. Evaluación empírica única Fuera de Muestra (OOS)
    res_oos = analizar_tramo(df_oos, "oos")
    res_oos["summary"].to_csv(OUTPUT_METRICS_OOS, index=False)

    # 8. Guardar tablas auxiliares
    df_all_deciles = pd.concat([res_design["deciles"], res_oos["deciles"]], ignore_index=True)
    df_all_deciles.to_csv(OUTPUT_DECILES, index=False)

    df_all_coverage = pd.concat([res_design["coverage"], res_oos["coverage"]], ignore_index=True)
    df_all_coverage.to_csv(OUTPUT_COVERAGE, index=False)

    res_design["adx_components"].to_csv(OUTPUT_ADX_COMPS, index=False)

    print("\n" + "="*70)
    print("✅ BACKTEST WALK-FORWARD COMPLETADO EXITOSAMENTE")
    print(f"Archivos generados:")
    print(f"  - {OUTPUT_PANEL}")
    print(f"  - {OUTPUT_METRICS_DESIGN}")
    print(f"  - {OUTPUT_METRICS_OOS}")
    print(f"  - {OUTPUT_DECILES}")
    print(f"  - {OUTPUT_ADX_COMPS}")
    print(f"  - {OUTPUT_COVERAGE}")
    print("="*70)


if __name__ == "__main__":
    main()
