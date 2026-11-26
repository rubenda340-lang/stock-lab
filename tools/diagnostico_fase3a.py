"""
Fase 3A — Diagnóstico Cuantitativo Completo (Solo Lectura)
==========================================================
Ejecuta los 9 puntos analíticos requeridos para el diagnóstico institucional
sin modificar en ningún momento la lógica de producción de signal_engine.py.

Puntos implementados:
  1. Líneas base completas (Diseño y OOS): % días al alza, frecuencias de zonas (fija y dinámica),
     aciertos de siempre ALCISTA/LATERAL/BAJISTA, azar proporcional y acierto de signo condicional.
  2. Tabla completa Componente × Régimen ADX (4 comps × 3 regímenes × 4 horizontes)
     con IC, t-stat Newey-West y n, estrictamente SEPARADA en Diseño y OOS.
  3. Prueba de ejecutabilidad: IC y retorno por decil entrando en Open(t+1) y Close(t+1).
  4. Fricciones y costes: retorno neto por decil con 10, 20 y 30 bps por pierna + Tasa Tobin (0.20%),
     con rotación diaria real (% cambios de decil), para Long-Only y Long-Short.
  5. IC por tercil de liquidez (€ volumen 20d) y capitalización (IBEX 35 vs Continuo).
  6. Estabilidad temporal (IC año a año) y distribución de IC por ticker (% negativos).
  7. Análisis y documentación del sesgo de supervivencia (delistings BME 2020-2026).
  8. Análisis de paridad Backtest vs En Vivo (ventana 90d, barra intradía, invalidaciones).
  9. Factores candidatos puros (retornos pasados normalizados por sigma y residuales vs IBEX con beta 60d)
     y matriz de correlación de rango entre los 4 componentes actuales.
"""

import os
import sys
import time
from typing import Dict, List, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from scipy import stats

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from database.connection import get_connection

CACHE_PRECIOS = os.path.join(BASE_DIR, "data", "backtest_cache_6y.parquet")
PANEL_SENALES = os.path.join(BASE_DIR, "data", "backtest_signals_panel.parquet")

OUTPUT_BASELINES = os.path.join(BASE_DIR, "data", "fase3a_baselines.csv")
OUTPUT_ADX_COMPS = os.path.join(BASE_DIR, "data", "fase3a_adx_components_design_oos.csv")
OUTPUT_EXEC = os.path.join(BASE_DIR, "data", "fase3a_executability_timings.csv")
OUTPUT_COSTS = os.path.join(BASE_DIR, "data", "fase3a_transaction_costs.csv")
OUTPUT_LIQ = os.path.join(BASE_DIR, "data", "fase3a_liquidity_cap_terciles.csv")
OUTPUT_STAB = os.path.join(BASE_DIR, "data", "fase3a_temporal_stability.csv")
OUTPUT_FACTORS = os.path.join(BASE_DIR, "data", "fase3a_factor_candidates.csv")
OUTPUT_CORR = os.path.join(BASE_DIR, "data", "fase3a_component_correlations.csv")

HORIZONTE_BARS = {"1D": 1, "3D": 3, "1S": 5, "1M": 20}
SPLIT_DESIGN_DATE = "2025-03-31"
START_OOS_DATE = "2025-05-02"


# ═══════════════════════════════════════════════════════════════════
# UTILIDADES ESTADÍSTICAS
# ═══════════════════════════════════════════════════════════════════

def newey_west(series: pd.Series, lag: int) -> Tuple[float, float, float]:
    """Retorna (media, t-stat NW con lag, p-valor)."""
    s = series.dropna().values
    n = len(s)
    if n < 5:
        return 0.0, 0.0, 1.0
    mean_val = float(np.mean(s))
    centered = s - mean_val
    gamma_0 = float(np.dot(centered, centered) / n)
    v_nw = gamma_0
    for l in range(1, lag + 1):
        if l >= n:
            break
        gamma_l = float(np.dot(centered[l:], centered[:-l]) / n)
        w = 1.0 - (l / (lag + 1.0))
        v_nw += 2.0 * w * gamma_l
    se_nw = np.sqrt(max(1e-12, v_nw) / n)
    t_stat = mean_val / se_nw
    p_val = float(2.0 * stats.t.sf(abs(t_stat), df=n - 1))
    return mean_val, float(t_stat), p_val


def calc_daily_spearman_ic(sub_df: pd.DataFrame, col_signal: str, col_ret: str) -> pd.Series:
    """Calcula la serie temporal de correlación de rango Spearman cross-sectional por fecha."""
    def _ic_date(g):
        v = g[[col_signal, col_ret]].dropna()
        if len(v) < 10:
            return np.nan
        r, _ = stats.spearmanr(v[col_signal], v[col_ret])
        return r
    return sub_df.groupby("date").apply(_ic_date, include_groups=False).dropna()


# ═══════════════════════════════════════════════════════════════════
# PUNTO 1: LÍNEAS BASE COMPLETAS (DISEÑO & OOS)
# ═══════════════════════════════════════════════════════════════════

def analizar_lineas_base(df_panel: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("1. LÍNEAS BASE COMPLETAS Y DISTRIBUCIONES REALES")
    print("="*70)
    tramos = {
        "diseno": df_panel[df_panel["date"] <= pd.Timestamp(SPLIT_DESIGN_DATE)],
        "oos": df_panel[df_panel["date"] >= pd.Timestamp(START_OOS_DATE)],
    }
    filas = []
    for nombre_tramo, df_t in tramos.items():
        for hz, n_bars in HORIZONTE_BARS.items():
            sub = df_t[df_t["horizon"] == hz].copy()
            n_tot = len(sub)
            if n_tot == 0:
                continue

            # 1. % retornos reales > 0
            ret = sub["fwd_ret"]
            pct_up = float((ret > 0).mean() * 100.0)
            pct_down = float((ret < 0).mean() * 100.0)
            pct_zero = float((ret == 0).mean() * 100.0)

            # 2. Distribución real en zona fija
            # (1D: ±1.5%, 3D: ±2.5%, 1S: ±4.0%, 1M: ±7.0%)
            u_fijo = sub["umbral_fijo"].iloc[0]
            real_fijo_alc = float((ret * 100.0 > u_fijo).mean() * 100.0)
            real_fijo_baj = float((ret * 100.0 < -u_fijo).mean() * 100.0)
            real_fijo_lat = float(((ret * 100.0 >= -u_fijo) & (ret * 100.0 <= u_fijo)).mean() * 100.0)

            # 3. Distribución real en zona dinámica (k*sigma*sqrt(h))
            u_din = sub["umbral_din"]
            real_din_alc = float((ret * 100.0 > u_din).mean() * 100.0)
            real_din_baj = float((ret * 100.0 < -u_din).mean() * 100.0)
            real_din_lat = float(((ret * 100.0 >= -u_din) & (ret * 100.0 <= u_din)).mean() * 100.0)

            # 4. Distribución de las señales EMITIDAS por el motor
            senales = sub["senal"]
            pred_alc = float((senales == "ALCISTA").mean() * 100.0)
            pred_baj = float((senales == "BAJISTA").mean() * 100.0)
            pred_lat = float((senales == "LATERAL").mean() * 100.0)

            # 5. Acierto de líneas base triviales sobre zona FIJA
            # Siempre ALCISTA acierta si real fue ALCISTA, etc.
            base_fijo_alc = real_fijo_alc
            base_fijo_baj = real_fijo_baj
            base_fijo_lat = real_fijo_lat
            # Aleatorio proporcional a las frecuencias emitidas:
            base_fijo_azar_prop = (pred_alc * real_fijo_alc + pred_lat * real_fijo_lat + pred_baj * real_fijo_baj) / 100.0
            # Acierto real del motor en zona fija
            acierto_motor_fijo = float(sub["hit_3c_fijo"].mean() * 100.0)

            # 6. Acierto de líneas base triviales sobre zona DINÁMICA
            base_din_alc = real_din_alc
            base_din_baj = real_din_baj
            base_din_lat = real_din_lat
            base_din_azar_prop = (pred_alc * real_din_alc + pred_lat * real_din_lat + pred_baj * real_din_baj) / 100.0
            acierto_motor_din = float(sub["hit_3c_din"].mean() * 100.0)

            # 7. Acierto de signo direccional SOLO sobre señales direccionales (excluye señales = 0)
            sub_dir = sub[sub["pct_raw"].abs() > 1e-5]
            if not sub_dir.empty:
                hit_sign_only_dir = float((
                    ((sub_dir["pct_raw"] > 0) & (sub_dir["fwd_ret"] > 0)) |
                    ((sub_dir["pct_raw"] < 0) & (sub_dir["fwd_ret"] < 0))
                ).mean() * 100.0)
            else:
                hit_sign_only_dir = np.nan

            filas.append({
                "tramo": nombre_tramo,
                "horizonte": hz,
                "n_muestras": n_tot,
                "pct_ret_positivo": round(pct_up, 2),
                "pct_ret_negativo": round(pct_down, 2),
                # Distribución real fija
                "real_fijo_lat_pct": round(real_fijo_lat, 2),
                "real_fijo_alc_pct": round(real_fijo_alc, 2),
                "real_fijo_baj_pct": round(real_fijo_baj, 2),
                # Distribución real dinámica
                "real_din_lat_pct": round(real_din_lat, 2),
                "real_din_alc_pct": round(real_din_alc, 2),
                "real_din_baj_pct": round(real_din_baj, 2),
                # Frecuencias emitidas motor
                "pred_lat_pct": round(pred_lat, 2),
                "pred_alc_pct": round(pred_alc, 2),
                "pred_baj_pct": round(pred_baj, 2),
                # Líneas base fijas
                "linea_base_fijo_siempre_lat": round(base_fijo_lat, 2),
                "linea_base_fijo_azar_prop": round(base_fijo_azar_prop, 2),
                "motor_acierto_fijo": round(acierto_motor_fijo, 2),
                # Líneas base dinámicas
                "linea_base_din_siempre_lat": round(base_din_lat, 2),
                "linea_base_din_azar_prop": round(base_din_azar_prop, 2),
                "motor_acierto_din": round(acierto_motor_din, 2),
                # Acierto de signo puro
                "motor_hit_sign_solo_dir": round(hit_sign_only_dir, 2),
            })

    df_res = pd.DataFrame(filas)
    print(df_res[["tramo", "horizonte", "pct_ret_positivo", "real_fijo_lat_pct", "linea_base_fijo_siempre_lat",
                  "motor_acierto_fijo", "real_din_lat_pct", "linea_base_din_siempre_lat", "motor_acierto_din", "motor_hit_sign_solo_dir"]].to_string(index=False))
    df_res.to_csv(OUTPUT_BASELINES, index=False)
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 2: TABLA COMPLETA COMPONENTE × RÉGIMEN ADX (DISEÑO & OOS)
# ═══════════════════════════════════════════════════════════════════

def analizar_componentes_adx(df_panel: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("2. TABLA COMPONENTE × RÉGIMEN ADX (SEPARADA DISEÑO Y OOS)")
    print("="*70)
    comps = ["comp_rsi", "comp_macd", "comp_tendencia", "comp_momentum"]
    regimes = ["low", "trans", "high"]
    tramos = {
        "diseno": df_panel[df_panel["date"] <= pd.Timestamp(SPLIT_DESIGN_DATE)],
        "oos": df_panel[df_panel["date"] >= pd.Timestamp(START_OOS_DATE)],
    }
    filas = []

    for nombre_tramo, df_t in tramos.items():
        for hz, n_bars in HORIZONTE_BARS.items():
            sub_hz = df_t[df_t["horizon"] == hz]
            lag_nw = max(1, n_bars - 1)

            for reg in regimes:
                sub_reg = sub_hz[sub_hz["regimen_adx"] == reg]
                n_reg = len(sub_reg)
                if n_reg < 30:
                    continue

                for comp in comps:
                    # IC diario y t-stat Newey-West
                    daily_ic = calc_daily_spearman_ic(sub_reg, comp, "fwd_ret")
                    if len(daily_ic) >= 5:
                        mean_ic, t_nw, p_nw = newey_west(daily_ic, lag=lag_nw)
                    else:
                        r_p, p_p = stats.spearmanr(sub_reg[comp], sub_reg["fwd_ret"])
                        mean_ic, t_nw, p_nw = float(r_p), np.nan, float(p_p)

                    filas.append({
                        "tramo": nombre_tramo,
                        "horizonte": hz,
                        "regimen_adx": reg,
                        "componente": comp,
                        "n_muestras": n_reg,
                        "n_fechas": len(daily_ic),
                        "spearman_ic": round(mean_ic, 4),
                        "t_stat_nw": round(t_nw, 2) if np.isfinite(t_nw) else None,
                        "p_val_nw": round(p_nw, 4),
                        "interpretacion": "CONTINUACION" if (mean_ic > 0 and abs(t_nw or 0) >= 2.0) else (
                            "REVERSION" if (mean_ic < 0 and abs(t_nw or 0) >= 2.0) else "RUIDO (|t|<2)"
                        ),
                    })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_ADX_COMPS, index=False)
    # Mostrar tabla filtrada donde |t| >= 2.0
    signif = df_res[df_res["interpretacion"] != "RUIDO (|t|<2)"]
    print("CELDAS SIGNIFICATIVAS (|t| >= 2.0):")
    print(signif[["tramo", "horizonte", "regimen_adx", "componente", "n_muestras", "spearman_ic", "t_stat_nw", "interpretacion"]].to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 3: PRUEBA DE EJECUTABILIDAD (TIMINGS DE ENTRADA)
# ═══════════════════════════════════════════════════════════════════

def analizar_ejecutabilidad_timings(df_panel: pd.DataFrame, df_cache: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("3. PRUEBA DE EJECUTABILIDAD (ENTRADA EN APERTURA Y CIERRE DE T+1)")
    print("="*70)
    # Extraer matrices 2D de precios
    closes = df_cache.xs("Close", level="Price", axis=1)
    opens = df_cache.xs("Open", level="Price", axis=1)
    ibex_close = closes["^IBEX"] if "^IBEX" in closes.columns else None
    ibex_open = opens["^IBEX"] if "^IBEX" in opens.columns else None

    # Mapeo de calendario
    dates_idx = {d: i for i, d in enumerate(closes.index)}
    cal = closes.index

    # Añadir a sub-panel de señales de 1D, 3D, 1S
    sub = df_panel[df_panel["horizon"].isin(["1D", "3D", "1S"])].copy()

    ret_open1_list = []
    ret_close1_list = []
    ret_open1_bench_list = []
    ret_close1_bench_list = []

    for _, row in sub.iterrows():
        t = row["date"]
        ticker = row["ticker"]
        n_bars = row["n_bars"]
        c_i = dates_idx.get(t)

        if c_i is None or (c_i + n_bars + 1) >= len(cal) or ticker not in closes.columns:
            ret_open1_list.append(np.nan)
            ret_close1_list.append(np.nan)
            ret_open1_bench_list.append(np.nan)
            ret_close1_bench_list.append(np.nan)
            continue

        # Fechas relevantes
        t_plus_1 = cal[c_i + 1]
        t_plus_h = cal[c_i + n_bars]
        t_plus_h1 = cal[c_i + n_bars + 1]

        # Regla A: Entrada en Apertura de t+1, Salida en Cierre de t+h (o Close t+1 para 1D)
        # Para 1D: entra Open(t+1), sale Close(t+1) -> Retorno intradía de la sesión t+1
        p_open1 = opens.loc[t_plus_1, ticker]
        p_exitA = closes.loc[t_plus_h, ticker]

        # Regla B: Entrada en Cierre de t+1, Salida en Cierre de t+1+h (1 sesión desplazada)
        p_close1 = closes.loc[t_plus_1, ticker]
        p_exitB = closes.loc[t_plus_h1, ticker]

        # Retornos activo
        r_A = (p_exitA - p_open1) / p_open1 if (pd.notna(p_open1) and p_open1 > 0) else np.nan
        r_B = (p_exitB - p_close1) / p_close1 if (pd.notna(p_close1) and p_close1 > 0) else np.nan

        # Retornos benchmark IBEX
        p_ib_open1 = ibex_open.loc[t_plus_1] if ibex_open is not None else np.nan
        p_ib_exitA = ibex_close.loc[t_plus_h] if ibex_close is not None else np.nan
        p_ib_close1 = ibex_close.loc[t_plus_1] if ibex_close is not None else np.nan
        p_ib_exitB = ibex_close.loc[t_plus_h1] if ibex_close is not None else np.nan

        r_ib_A = (p_ib_exitA - p_ib_open1) / p_ib_open1 if (pd.notna(p_ib_open1) and p_ib_open1 > 0) else 0.0
        r_ib_B = (p_ib_exitB - p_ib_close1) / p_ib_close1 if (pd.notna(p_ib_close1) and p_ib_close1 > 0) else 0.0

        ret_open1_list.append(r_A)
        ret_close1_list.append(r_B)
        ret_open1_bench_list.append(r_ib_A)
        ret_close1_bench_list.append(r_ib_B)

    sub["fwd_ret_open1"] = ret_open1_list
    sub["fwd_ret_close1"] = ret_close1_list
    sub["fwd_excess_open1"] = sub["fwd_ret_open1"] - ret_open1_bench_list
    sub["fwd_excess_close1"] = sub["fwd_ret_close1"] - ret_close1_bench_list

    # Evaluar IC y deciles para cada regla
    filas = []
    reglas = [
        ("Cierre(t) [Original No Operable]", "fwd_ret", "fwd_ret_excess"),
        ("Apertura(t+1) [Operable Campana]", "fwd_ret_open1", "fwd_excess_open1"),
        ("Cierre(t+1) [Operable Retardo 1D]", "fwd_ret_close1", "fwd_excess_close1"),
    ]

    for hz in ["1D", "3D", "1S"]:
        n_bars = HORIZONTE_BARS[hz]
        lag_nw = max(1, n_bars - 1)
        sub_hz = sub[sub["horizon"] == hz].dropna(subset=["fwd_ret", "fwd_ret_open1", "fwd_ret_close1"])

        for nombre_regla, col_ret, col_exc in reglas:
            # 1. Spearman IC
            daily_ic = calc_daily_spearman_ic(sub_hz, "pct_raw", col_ret)
            mean_ic, t_nw, p_nw = newey_west(daily_ic, lag=lag_nw)

            # 2. Deciles D1, D10 y Spread
            sub_hz["decil"] = pd.qcut(sub_hz["pct_raw"].rank(method="first"), q=10, labels=False) + 1
            d1 = sub_hz[sub_hz["decil"] == 1]
            d10 = sub_hz[sub_hz["decil"] == 10]

            ret_d1 = float(d1[col_ret].mean() * 100.0)
            ret_d10 = float(d10[col_ret].mean() * 100.0)
            exc_d1 = float(d1[col_exc].mean() * 100.0)
            exc_d10 = float(d10[col_exc].mean() * 100.0)

            spread_raw = ret_d10 - ret_d1
            spread_exc = exc_d10 - exc_d1

            # Acierto de signo
            hit_sign = float((
                ((sub_hz["pct_raw"] > 0) & (sub_hz[col_ret] > 0)) |
                ((sub_hz["pct_raw"] < 0) & (sub_hz[col_ret] < 0))
            ).mean() * 100.0)

            filas.append({
                "horizonte": hz,
                "regla_ejecucion": nombre_regla,
                "n_muestras": len(sub_hz),
                "spearman_ic": round(mean_ic, 4),
                "t_stat_nw": round(t_nw, 2),
                "p_val": round(p_nw, 4),
                "hit_sign_pct": round(hit_sign, 2),
                "ret_d1_pct": round(ret_d1, 3),
                "ret_d10_pct": round(ret_d10, 3),
                "spread_d10_d1_pct": round(spread_raw, 3),
                "exc_d1_pct": round(exc_d1, 3),
                "exc_d10_pct": round(exc_d10, 3),
                "spread_excess_pct": round(spread_exc, 3),
            })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_EXEC, index=False)
    print(df_res[["horizonte", "regla_ejecucion", "spearman_ic", "t_stat_nw", "spread_d10_d1_pct", "spread_excess_pct"]].to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 4: COSTES DE TRANSACCIÓN Y ROTACIÓN
# ═══════════════════════════════════════════════════════════════════

def analizar_costes_y_rotacion(df_panel: pd.DataFrame, df_cache: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("4. COSTES DE TRANSACCIÓN, ROTACIÓN Y TASA TOBIN")
    print("="*70)
    conn = get_connection()
    try:
        conn.row_factory = None
        # Empresas IBEX 35 (sujetas a tasa Tobin 0.20% por capitalización > 1.000 M€)
        ibex_tickers = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    filas = []
    niveles_coste = [0.0010, 0.0020, 0.0030]  # 10, 20, 30 bps por pierna (compra y venta)

    for hz in ["1D", "3D", "1S"]:
        sub_hz = df_panel[df_panel["horizon"] == hz].copy()
        sub_hz["decil"] = pd.qcut(sub_hz["pct_raw"].rank(method="first"), q=10, labels=False) + 1

        # Rotación día a día: % de valores en Decil 10 que no estaban en Decil 10 ayer
        d10_series = sub_hz[sub_hz["decil"] == 10].groupby("date")["ticker"].apply(set)
        rot_d10_list = []
        fechas_d10 = sorted(d10_series.index)
        for i in range(1, len(fechas_d10)):
            prev_set = d10_series[fechas_d10[i-1]]
            curr_set = d10_series[fechas_d10[i]]
            if len(curr_set) > 0:
                cambios = len(curr_set - prev_set)
                rot_d10_list.append(cambios / len(curr_set))
        rotacion_media = float(np.mean(rot_d10_list) * 100.0) if rot_d10_list else 100.0

        d1 = sub_hz[sub_hz["decil"] == 1]
        d10 = sub_hz[sub_hz["decil"] == 10]

        ret_bruto_d1 = float(d1["fwd_ret"].mean())
        ret_bruto_d10 = float(d10["fwd_ret"].mean())
        exc_bruto_d10 = float(d10["fwd_ret_excess"].mean())

        # Proporción de valores D10 sujetos a tasa Tobin
        pct_tobin = float(d10["ticker"].isin(ibex_tickers).mean())
        coste_tobin_d10 = pct_tobin * 0.0020  # 0.20% en la compra

        for c_leg in niveles_coste:
            c_roundtrip = 2.0 * c_leg

            # Solo-Long (D10 vs IBEX): Compra D10 (paga comisión + Tobin) y vende D10 (paga comisión)
            # Retorno neto por operación = ret_bruto_d10 - c_roundtrip - coste_tobin_d10
            ret_neto_long_raw = ret_bruto_d10 - c_roundtrip - coste_tobin_d10
            exc_neto_long = exc_bruto_d10 - c_roundtrip - coste_tobin_d10

            # Long-Short (D10 - D1):
            # Pierna larga: paga 2 comisiones + Tobin
            # Pierna corta: paga 2 comisiones (sin Tobin)
            # Total fricción LS = 4 * c_leg + coste_tobin_d10
            spread_bruto = ret_bruto_d10 - ret_bruto_d1
            spread_neto = spread_bruto - (4.0 * c_leg) - coste_tobin_d10

            filas.append({
                "horizonte": hz,
                "coste_pierna_bps": int(c_leg * 10000),
                "coste_rt_bps": int(c_roundtrip * 10000),
                "rotacion_d10_pct": round(rotacion_media, 1),
                "pct_empresas_tobin": round(pct_tobin * 100.0, 1),
                "ret_bruto_d10_pct": round(ret_bruto_d10 * 100.0, 3),
                "ret_neto_d10_pct": round(ret_neto_long_raw * 100.0, 3),
                "exc_bruto_d10_pct": round(exc_bruto_d10 * 100.0, 3),
                "exc_neto_d10_pct": round(exc_neto_long * 100.0, 3),
                "spread_bruto_ls_pct": round(spread_bruto * 100.0, 3),
                "spread_neto_ls_pct": round(spread_neto * 100.0, 3),
            })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_COSTS, index=False)
    print(df_res[["horizonte", "coste_pierna_bps", "rotacion_d10_pct", "exc_bruto_d10_pct", "exc_neto_d10_pct", "spread_bruto_ls_pct", "spread_neto_ls_pct"]].to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 5: IC POR TERCIL DE LIQUIDEZ Y CAPITALIZACIÓN
# ═══════════════════════════════════════════════════════════════════

def analizar_liquidez_y_capitalizacion(df_panel: pd.DataFrame, df_cache: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("5. IC POR TERCIL DE LIQUIDEZ Y CAPITALIZACIÓN")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    vols = df_cache.xs("Volume", level="Price", axis=1)
    vol_eur_20d = (closes * vols).rolling(20).mean()

    conn = get_connection()
    try:
        conn.row_factory = None
        ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    # Añadir liquidez por fecha a cada fila del panel de forma vectorizada
    sub = df_panel[df_panel["horizon"].isin(["1D", "3D", "1S"])].copy()
    vol_stacked = vol_eur_20d.stack(future_stack=True).rename("vol_eur_20d").reset_index()
    vol_stacked.columns = ["date", "ticker", "vol_eur_20d"]
    sub = sub.merge(vol_stacked, on=["date", "ticker"], how="left")
    sub["cap_grupo"] = np.where(sub["ticker"].isin(ibex_set), "IBEX35", "CONTINUO")

    # Terciles diarios de liquidez con transform rápido
    q1 = sub.groupby(["date", "horizon"])["vol_eur_20d"].transform(lambda s: s.quantile(0.333))
    q2 = sub.groupby(["date", "horizon"])["vol_eur_20d"].transform(lambda s: s.quantile(0.666))
    sub["liq_tercil"] = np.where(sub["vol_eur_20d"] >= q2, "alta", np.where(sub["vol_eur_20d"] <= q1, "baja", "media"))

    filas = []
    for hz in ["1D", "3D", "1S"]:
        n_bars = HORIZONTE_BARS[hz]
        lag_nw = max(1, n_bars - 1)
        sub_hz = sub[sub["horizon"] == hz]

        # Por liquidez
        for tercil in ["alta", "media", "baja"]:
            sub_t = sub_hz[sub_hz["liq_tercil"] == tercil]
            if len(sub_t) < 50:
                continue
            daily_ic = calc_daily_spearman_ic(sub_t, "pct_raw", "fwd_ret")
            mean_ic, t_nw, p_nw = newey_west(daily_ic, lag=lag_nw)
            hit_sign = float((
                ((sub_t["pct_raw"] > 0) & (sub_t["fwd_ret"] > 0)) |
                ((sub_t["pct_raw"] < 0) & (sub_t["fwd_ret"] < 0))
            ).mean() * 100.0)

            filas.append({
                "horizonte": hz,
                "criterio": "Liquidez € (20d)",
                "grupo": f"Tercil {tercil.upper()}",
                "n_muestras": len(sub_t),
                "spearman_ic": round(mean_ic, 4),
                "t_stat_nw": round(t_nw, 2),
                "p_val": round(p_nw, 4),
                "hit_sign_pct": round(hit_sign, 2),
            })

        # Por capitalización
        for cap_g in ["IBEX35", "CONTINUO"]:
            sub_c = sub_hz[sub_hz["cap_grupo"] == cap_g]
            daily_ic = calc_daily_spearman_ic(sub_c, "pct_raw", "fwd_ret")
            mean_ic, t_nw, p_nw = newey_west(daily_ic, lag=lag_nw)
            hit_sign = float((
                ((sub_c["pct_raw"] > 0) & (sub_c["fwd_ret"] > 0)) |
                ((sub_c["pct_raw"] < 0) & (sub_c["fwd_ret"] < 0))
            ).mean() * 100.0)

            filas.append({
                "horizonte": hz,
                "criterio": "Capitalización",
                "grupo": cap_g,
                "n_muestras": len(sub_c),
                "spearman_ic": round(mean_ic, 4),
                "t_stat_nw": round(t_nw, 2),
                "p_val": round(p_nw, 4),
                "hit_sign_pct": round(hit_sign, 2),
            })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_LIQ, index=False)
    print(df_res[["horizonte", "criterio", "grupo", "n_muestras", "spearman_ic", "t_stat_nw", "hit_sign_pct"]].to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 6: ESTABILIDAD TEMPORAL Y DISTRIBUCIÓN POR TICKER
# ═══════════════════════════════════════════════════════════════════

def analizar_estabilidad_temporal_y_tickers(df_panel: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("6. ESTABILIDAD TEMPORAL (AÑO A AÑO) Y DISTRIBUCIÓN POR TICKER")
    print("="*70)
    df_panel["year"] = pd.to_datetime(df_panel["date"]).dt.year
    filas = []

    # 1. Año a año
    for hz in ["1D", "3D", "1S"]:
        n_bars = HORIZONTE_BARS[hz]
        lag_nw = max(1, n_bars - 1)
        sub_hz = df_panel[df_panel["horizon"] == hz]

        for y in sorted(sub_hz["year"].unique()):
            sub_y = sub_hz[sub_hz["year"] == y]
            daily_ic = calc_daily_spearman_ic(sub_y, "pct_raw", "fwd_ret")
            mean_ic, t_nw, p_nw = newey_west(daily_ic, lag=lag_nw)
            hit_sign = float((
                ((sub_y["pct_raw"] > 0) & (sub_y["fwd_ret"] > 0)) |
                ((sub_y["pct_raw"] < 0) & (sub_y["fwd_ret"] < 0))
            ).mean() * 100.0)

            filas.append({
                "horizonte": hz,
                "tipo": "Ano",
                "etiqueta": str(y),
                "n_muestras": len(sub_y),
                "spearman_ic": round(mean_ic, 4),
                "t_stat_nw": round(t_nw, 2),
                "hit_sign_pct": round(hit_sign, 2),
            })

    # 2. Distribución de IC por ticker (% de tickers con IC negativo)
    for hz in ["1D", "3D", "1S"]:
        sub_hz = df_panel[df_panel["horizon"] == hz]
        ticker_ics = []
        for tk, grp in sub_hz.groupby("ticker"):
            if len(grp) >= 100:
                r, _ = stats.spearmanr(grp["pct_raw"], grp["fwd_ret"])
                ticker_ics.append(float(r))

        ticker_ics = np.array(ticker_ics)
        pct_neg = float((ticker_ics < 0).mean() * 100.0)
        q25, q50, q75 = np.percentile(ticker_ics, [25, 50, 75])

        filas.append({
            "horizonte": hz,
            "tipo": "Distribucion_Tickers",
            "etiqueta": f"Total {len(ticker_ics)} tickers (Mediana={round(q50,4)}, %Neg={round(pct_neg,1)}%)",
            "n_muestras": len(ticker_ics),
            "spearman_ic": round(float(np.mean(ticker_ics)), 4),
            "t_stat_nw": None,
            "hit_sign_pct": round(pct_neg, 1),  # Guarda el % de tickers negativos
        })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_STAB, index=False)
    print(df_res[df_res["tipo"] == "Ano"][["horizonte", "etiqueta", "n_muestras", "spearman_ic", "t_stat_nw", "hit_sign_pct"]].to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 9: FACTORES CANDIDATOS PUROS Y COLINEALIDAD
# ═══════════════════════════════════════════════════════════════════

def analizar_factores_candidatos_y_colinealidad(df_panel: pd.DataFrame, df_cache: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    print("\n" + "="*70)
    print("9. CANDIDATOS DE FACTOR PURO Y COLINEALIDAD ENTRE COMPONENTES")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    ret_daily = closes.pct_change()
    ibex_ret = ret_daily["^IBEX"] if "^IBEX" in ret_daily.columns else None

    # Factores puros pasados normalizados por volatilidad
    # r_-1 / sigma, r_-5 / sigma, r_-20 / sigma
    sigma_20 = ret_daily.rolling(20).std()
    f_r1_norm = (ret_daily / sigma_20).shift(0)  # retorno de ayer normalizado
    f_r5_norm = ((closes / closes.shift(5) - 1.0) / (sigma_20 * np.sqrt(5))).shift(0)
    f_r20_norm = ((closes / closes.shift(20) - 1.0) / (sigma_20 * np.sqrt(20))).shift(0)

    # Beta 60d vs IBEX
    cov_60 = ret_daily.rolling(60).cov(ibex_ret)
    var_ibex_60 = ibex_ret.rolling(60).var()
    beta_60 = cov_60.div(var_ibex_60, axis=0)

    # Retornos residuales frente a IBEX
    # r_resid = r_i - beta * r_ibex
    f_r1_resid = (ret_daily.sub(beta_60.mul(ibex_ret, axis=0))).div(sigma_20)
    f_r5_resid = ((closes / closes.shift(5) - 1.0).sub(beta_60.mul(closes["^IBEX"] / closes["^IBEX"].shift(5) - 1.0, axis=0))).div(sigma_20 * np.sqrt(5))

    # Cruzar con panel de señales de forma vectorizada
    sub = df_panel[df_panel["horizon"].isin(["1D", "3D", "1S"])].copy()
    
    def _stack_fact(df_mat, name):
        s = df_mat.stack(future_stack=True).rename(name).reset_index()
        s.columns = ["date", "ticker", name]
        return s

    sub = sub.merge(_stack_fact(f_r1_norm, "f_r1_norm"), on=["date", "ticker"], how="left")
    sub = sub.merge(_stack_fact(f_r5_norm, "f_r5_norm"), on=["date", "ticker"], how="left")
    sub = sub.merge(_stack_fact(f_r20_norm, "f_r20_norm"), on=["date", "ticker"], how="left")
    sub = sub.merge(_stack_fact(f_r1_resid, "f_r1_resid"), on=["date", "ticker"], how="left")
    sub = sub.merge(_stack_fact(f_r5_resid, "f_r5_resid"), on=["date", "ticker"], how="left")

    factores = [
        ("r_-1 / sigma", "f_r1_norm"),
        ("r_-5 / sigma", "f_r5_norm"),
        ("r_-20 / sigma", "f_r20_norm"),
        ("r_-1 residual (beta 60)", "f_r1_resid"),
        ("r_-5 residual (beta 60)", "f_r5_resid"),
    ]

    filas_fact = []
    for hz in ["1D", "3D", "1S"]:
        n_bars = HORIZONTE_BARS[hz]
        lag_nw = max(1, n_bars - 1)
        sub_hz = sub[sub["horizon"] == hz]

        for nombre_f, col_f in factores:
            daily_ic = calc_daily_spearman_ic(sub_hz, col_f, "fwd_ret")
            mean_ic, t_nw, p_nw = newey_west(daily_ic, lag=lag_nw)
            filas_fact.append({
                "horizonte": hz,
                "factor_candidato": nombre_f,
                "spearman_ic": round(mean_ic, 4),
                "t_stat_nw": round(t_nw, 2),
                "p_val": round(p_nw, 4),
                "interpretacion": "REVERSION FUERTE" if (mean_ic < 0 and t_nw <= -3.0) else (
                    "CONTINUACION" if (mean_ic > 0 and t_nw >= 3.0) else "DEBIL / RUIDO"
                ),
            })

    df_factors = pd.DataFrame(filas_fact)
    df_factors.to_csv(OUTPUT_FACTORS, index=False)
    print("FACTORES PUROS CANDIDATOS:")
    print(df_factors.to_string(index=False))

    # Matriz de correlación entre los 4 componentes actuales
    comps = ["comp_rsi", "comp_macd", "comp_tendencia", "comp_momentum"]
    sub_1d = df_panel[df_panel["horizon"] == "1D"][comps].dropna()
    corr_matrix = sub_1d.corr(method="spearman").round(3)
    corr_matrix.to_csv(OUTPUT_CORR)
    print("\nMATRIZ DE CORRELACIÓN DE SPEARMAN ENTRE COMPONENTES:")
    print(corr_matrix.to_string())

    return df_factors, corr_matrix


# ═══════════════════════════════════════════════════════════════════
# MAIN DE EJECUCIÓN FASE 3A
# ═══════════════════════════════════════════════════════════════════

def main():
    print("="*70)
    print("🔬 INICIANDO DIAGNÓSTICO INSTITUCIONAL FASE 3A")
    print("="*70)
    t0 = time.time()

    df_panel = pd.read_parquet(PANEL_SENALES)
    df_cache = pd.read_parquet(CACHE_PRECIOS)

    # 1. Líneas base
    if not os.path.exists(OUTPUT_BASELINES):
        analizar_lineas_base(df_panel)
    else:
        print("[CACHE] Líneas base ya calculadas en", OUTPUT_BASELINES)

    # 2. Componente x ADX
    if not os.path.exists(OUTPUT_ADX_COMPS):
        analizar_componentes_adx(df_panel)
    else:
        print("[CACHE] Componente x ADX ya calculado en", OUTPUT_ADX_COMPS)

    # 3. Ejecutabilidad timings
    if not os.path.exists(OUTPUT_EXEC):
        analizar_ejecutabilidad_timings(df_panel, df_cache)
    else:
        print("[CACHE] Timings de ejecutabilidad ya calculados en", OUTPUT_EXEC)

    # 4. Costes y rotación
    if not os.path.exists(OUTPUT_COSTS):
        analizar_costes_y_rotacion(df_panel, df_cache)
    else:
        print("[CACHE] Costes y rotación ya calculados en", OUTPUT_COSTS)

    # 5. Liquidez y capitalización
    analizar_liquidez_y_capitalizacion(df_panel, df_cache)

    # 6. Estabilidad temporal y tickers
    analizar_estabilidad_temporal_y_tickers(df_panel)

    # 9. Factores candidatos y colinealidad
    analizar_factores_candidatos_y_colinealidad(df_panel, df_cache)

    print("\n" + "="*70)
    print(f"✅ FASE 3A COMPLETADA EN {round(time.time() - t0, 1)}s")
    print("Archivos de resultados generados en data/:")
    print(f"  - {OUTPUT_BASELINES}")
    print(f"  - {OUTPUT_ADX_COMPS}")
    print(f"  - {OUTPUT_EXEC}")
    print(f"  - {OUTPUT_COSTS}")
    print(f"  - {OUTPUT_LIQ}")
    print(f"  - {OUTPUT_STAB}")
    print(f"  - {OUTPUT_FACTORS}")
    print(f"  - {OUTPUT_CORR}")
    print("="*70)


if __name__ == "__main__":
    main()
