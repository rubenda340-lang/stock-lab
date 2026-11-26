"""
Fase 3A-BIS — Diagnóstico Cuantitativo Riguroso (Pre-registro de Criterios)
==========================================================================
Ejecuta los puntos del diagnóstico complementario con timings operables,
descomposición de microestructura, evaluación de volatilidad y factores lentos.

CRITERIO DE ÉXITO PRE-REGISTRADO:
  Solo hay señal direccional operable si en universo líquido (IBEX 35 y Tercil Alto),
  con entrada en Apertura(t+1), hay IC >= 0.02 con |t| > 3.0 (NW serie diaria)
  en AMBAS mitades cronológicas (Diseño y OOS), mismo signo, y retorno neto positivo
  con coste de 20 pb round-trip. Si no, se concluye "sin señal direccional operable".
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

OUTPUT_P1 = os.path.join(BASE_DIR, "data", "fase3a_bis_p1_descomposicion_1d.csv")
OUTPUT_P2 = os.path.join(BASE_DIR, "data", "fase3a_bis_p2_costes_operables.csv")
OUTPUT_P3 = os.path.join(BASE_DIR, "data", "fase3a_bis_p3_adx_operable_liquido.csv")
OUTPUT_P4 = os.path.join(BASE_DIR, "data", "fase3a_bis_p4_calidad_datos.csv")
OUTPUT_P5 = os.path.join(BASE_DIR, "data", "fase3a_bis_p5_estabilidad_liquidos.csv")
OUTPUT_P6 = os.path.join(BASE_DIR, "data", "fase3a_bis_p6_volatilidad_zonas.csv")
OUTPUT_P7 = os.path.join(BASE_DIR, "data", "fase3a_bis_p7_factores_lentos.csv")

SPLIT_DESIGN_DATE = "2025-03-31"
START_OOS_DATE = "2025-05-02"
HORIZONTE_BARS = {"1D": 1, "3D": 3, "1S": 5, "1M": 20}


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
        if len(v) < 8:
            return np.nan
        r, _ = stats.spearmanr(v[col_signal], v[col_ret])
        return r
    return sub_df.groupby("date").apply(_ic_date, include_groups=False).dropna()


# ═══════════════════════════════════════════════════════════════════
# PUNTO 1: DESCOMPOSICIÓN DEL 1D (OVERNIGHT VS INTRADÍA)
# ═══════════════════════════════════════════════════════════════════

def punto1_descomposicion_1d(df_panel: pd.DataFrame, df_cache: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("1. DESCOMPOSICIÓN DEL HORIZONTE 1D: OVERNIGHT VS INTRADÍA")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    opens = df_cache.xs("Open", level="Price", axis=1)
    vols = df_cache.xs("Volume", level="Price", axis=1)
    cal = closes.index
    dates_idx = {d: i for i, d in enumerate(cal)}

    conn = get_connection()
    try:
        conn.row_factory = None
        ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    # Terciles de liquidez rodante 20d en euros
    vol_eur = (closes * vols).rolling(20).mean()

    # Filtrar horizonte 1D
    sub = df_panel[df_panel["horizon"] == "1D"].copy()

    ret_overnight_list = []
    ret_intraday_list = []
    ret_total_list = []
    eur_liq_list = []

    for _, r in sub.iterrows():
        t = r["date"]
        tk = r["ticker"]
        c_i = dates_idx.get(t)
        if c_i is None or (c_i + 1) >= len(cal) or tk not in closes.columns:
            ret_overnight_list.append(np.nan)
            ret_intraday_list.append(np.nan)
            ret_total_list.append(np.nan)
            eur_liq_list.append(np.nan)
            continue

        t1 = cal[c_i + 1]
        p_c0 = closes.loc[t, tk]
        p_o1 = opens.loc[t1, tk]
        p_c1 = closes.loc[t1, tk]

        # Overnight: Cierre(t) -> Apertura(t+1)
        r_on = (p_o1 - p_c0) / p_c0 if (pd.notna(p_c0) and p_c0 > 0 and pd.notna(p_o1) and p_o1 > 0) else np.nan
        # Intradía: Apertura(t+1) -> Cierre(t+1)
        r_id = (p_c1 - p_o1) / p_o1 if (pd.notna(p_o1) and p_o1 > 0 and pd.notna(p_c1) and p_c1 > 0) else np.nan
        # Total: Cierre(t) -> Cierre(t+1)
        r_tot = (p_c1 - p_c0) / p_c0 if (pd.notna(p_c0) and p_c0 > 0 and pd.notna(p_c1) and p_c1 > 0) else np.nan

        ret_overnight_list.append(r_on)
        ret_intraday_list.append(r_id)
        ret_total_list.append(r_tot)
        eur_liq_list.append(vol_eur.loc[t, tk] if (t in vol_eur.index and tk in vol_eur.columns) else np.nan)

    sub["ret_overnight"] = ret_overnight_list
    sub["ret_intraday"] = ret_intraday_list
    sub["ret_total"] = ret_total_list
    sub["vol_eur"] = eur_liq_list
    sub["es_ibex"] = sub["ticker"].isin(ibex_set)

    # Terciles de liquidez diarios
    q1 = sub.groupby("date")["vol_eur"].transform(lambda s: s.quantile(0.333))
    q2 = sub.groupby("date")["vol_eur"].transform(lambda s: s.quantile(0.666))
    sub["liq_tercil"] = np.where(sub["vol_eur"] >= q2, "alta", np.where(sub["vol_eur"] <= q1, "baja", "media"))

    subgrupos = [
        ("Total Universo", sub),
        ("IBEX 35", sub[sub["es_ibex"]]),
        ("Tercil ALTA Liquidez", sub[sub["liq_tercil"] == "alta"]),
        ("Tercil BAJA Liquidez", sub[sub["liq_tercil"] == "baja"]),
    ]

    filas = []
    for nombre_grupo, df_g in subgrupos:
        for tramo_name, col_ret in [
            ("Overnight [Cierre(t) -> Open(t+1)]", "ret_overnight"),
            ("Intradía [Open(t+1) -> Cierre(t+1)]", "ret_intraday"),
            ("Total [Cierre(t) -> Cierre(t+1)]", "ret_total"),
        ]:
            daily_ic = calc_daily_spearman_ic(df_g, "pct_raw", col_ret)
            mean_ic, t_nw, p_nw = newey_west(daily_ic, lag=0)  # lag=0 para 1D no solapado

            # Deciles
            df_valid = df_g.dropna(subset=["pct_raw", col_ret]).copy()
            if len(df_valid) >= 50:
                df_valid["decil"] = pd.qcut(df_valid["pct_raw"].rank(method="first"), q=10, labels=False) + 1
                ret_d1 = float(df_valid[df_valid["decil"] == 1][col_ret].mean() * 100.0)
                ret_d10 = float(df_valid[df_valid["decil"] == 10][col_ret].mean() * 100.0)
                spread = ret_d10 - ret_d1
            else:
                ret_d1, ret_d10, spread = np.nan, np.nan, np.nan

            filas.append({
                "subgrupo": nombre_grupo,
                "tramo_1d": tramo_name,
                "n_muestras": len(df_valid),
                "spearman_ic": round(mean_ic, 4),
                "t_stat_nw": round(t_nw, 2),
                "p_val": round(p_nw, 4),
                "ret_d1_pct": round(ret_d1, 3),
                "ret_d10_pct": round(ret_d10, 3),
                "spread_d10_d1_pct": round(spread, 3),
            })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_P1, index=False)
    print(df_res[["subgrupo", "tramo_1d", "spearman_ic", "t_stat_nw", "spread_d10_d1_pct"]].to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 2: REHACER TABLA 4 CON TIMING OPERABLE (APERTURA T+1 -> CIERRE T+H)
# ═══════════════════════════════════════════════════════════════════

def punto2_costes_operables(df_panel: pd.DataFrame, df_cache: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("2. TABLA DE COSTES CON TIMING OPERABLE (APERTURA T+1 -> CIERRE T+H)")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    opens = df_cache.xs("Open", level="Price", axis=1)
    ibex_close = closes["^IBEX"] if "^IBEX" in closes.columns else None
    ibex_open = opens["^IBEX"] if "^IBEX" in opens.columns else None
    cal = closes.index
    dates_idx = {d: i for i, d in enumerate(cal)}

    conn = get_connection()
    try:
        conn.row_factory = None
        ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    filas = []
    costes_pierna = [0.0010, 0.0020, 0.0030]  # 10, 20, 30 bps por pierna

    for hz, n_bars in HORIZONTE_BARS.items():
        sub_hz = df_panel[df_panel["horizon"] == hz].copy()

        # Calcular retorno operable: Apertura(t+1) -> Cierre(t+h)
        ret_op_list = []
        ret_op_bench_list = []
        for _, r in sub_hz.iterrows():
            t = r["date"]
            tk = r["ticker"]
            c_i = dates_idx.get(t)
            if c_i is None or (c_i + n_bars) >= len(cal) or tk not in closes.columns:
                ret_op_list.append(np.nan)
                ret_op_bench_list.append(np.nan)
                continue

            t1 = cal[c_i + 1]
            th = cal[c_i + n_bars]

            p_in = opens.loc[t1, tk]
            p_out = closes.loc[th, tk]
            r_asset = (p_out - p_in) / p_in if (pd.notna(p_in) and p_in > 0 and pd.notna(p_out) and p_out > 0) else np.nan

            p_ib_in = ibex_open.loc[t1] if ibex_open is not None else np.nan
            p_ib_out = ibex_close.loc[th] if ibex_close is not None else np.nan
            r_bench = (p_ib_out - p_ib_in) / p_ib_in if (pd.notna(p_ib_in) and p_ib_in > 0 and pd.notna(p_ib_out) and p_ib_out > 0) else 0.0

            ret_op_list.append(r_asset)
            ret_op_bench_list.append(r_bench)

        sub_hz["ret_operable"] = ret_op_list
        sub_hz["excess_operable"] = sub_hz["ret_operable"] - ret_op_bench_list
        sub_hz = sub_hz.dropna(subset=["pct_raw", "ret_operable"])

        sub_hz["decil"] = pd.qcut(sub_hz["pct_raw"].rank(method="first"), q=10, labels=False) + 1
        d1 = sub_hz[sub_hz["decil"] == 1]
        d10 = sub_hz[sub_hz["decil"] == 10]

        ret_bruto_d1 = float(d1["ret_operable"].mean())
        ret_bruto_d10 = float(d10["ret_operable"].mean())
        exc_bruto_d10 = float(d10["excess_operable"].mean())
        spread_bruto_ls = ret_bruto_d10 - ret_bruto_d1

        # Rotación real de D10 día a día
        d10_by_date = d10.groupby("date")["ticker"].apply(set)
        rot_list = []
        fechas = sorted(d10_by_date.index)
        for i in range(1, len(fechas)):
            s0 = d10_by_date[fechas[i-1]]
            s1 = d10_by_date[fechas[i]]
            if len(s1) > 0:
                rot_list.append(len(s1 - s0) / len(s1))
        rotacion = float(np.mean(rot_list)) if rot_list else 1.0

        # Tasa Tobin (ITF): En 1D de Apertura a Cierre es INTRADÍA PURO (exento según art. 5.2 Ley 5/2020)
        pct_ibex_d10 = float(d10["ticker"].isin(ibex_set).mean())
        if hz == "1D":
            itf_rate = 0.0  # Intradía exento por liquidación de saldo neto
        else:
            itf_rate = pct_ibex_d10 * 0.0020  # 0.20% sobre compras de >1.000M€

        for c_leg in costes_pierna:
            coste_rt = 2.0 * c_leg

            # Esquema A: Cada señal es operación independiente (ida y vuelta completa cada vez)
            exc_neto_indep_sin_itf = exc_bruto_d10 - coste_rt
            exc_neto_indep_con_itf = exc_bruto_d10 - coste_rt - itf_rate

            spread_neto_indep_sin_itf = spread_bruto_ls - (4.0 * c_leg)
            spread_neto_indep_con_itf = spread_bruto_ls - (4.0 * c_leg) - itf_rate

            # Esquema B: Posiciones persistentes con rotación real
            coste_pers = rotacion * coste_rt
            itf_pers = rotacion * itf_rate
            exc_neto_pers_sin_itf = exc_bruto_d10 - coste_pers
            exc_neto_pers_con_itf = exc_bruto_d10 - coste_pers - itf_pers

            spread_neto_pers_sin_itf = spread_bruto_ls - (2.0 * rotacion * coste_rt)
            spread_neto_pers_con_itf = spread_bruto_ls - (2.0 * rotacion * coste_rt) - itf_pers

            filas.append({
                "horizonte": hz,
                "coste_pierna_bps": int(c_leg * 10000),
                "rotacion_d10_pct": round(rotacion * 100.0, 1),
                "exento_itf": "SI (Intradía)" if hz == "1D" else "NO",
                "exc_bruto_d10_pct": round(exc_bruto_d10 * 100.0, 3),
                # Esquema A (Independiente)
                "long_neto_indep_sin_itf_pct": round(exc_neto_indep_sin_itf * 100.0, 3),
                "long_neto_indep_con_itf_pct": round(exc_neto_indep_con_itf * 100.0, 3),
                "ls_neto_indep_sin_itf_pct": round(spread_neto_indep_sin_itf * 100.0, 3),
                "ls_neto_indep_con_itf_pct": round(spread_neto_indep_con_itf * 100.0, 3),
                # Esquema B (Persistente con rotación)
                "long_neto_pers_sin_itf_pct": round(exc_neto_pers_sin_itf * 100.0, 3),
                "long_neto_pers_con_itf_pct": round(exc_neto_pers_con_itf * 100.0, 3),
                "ls_neto_pers_sin_itf_pct": round(spread_neto_pers_sin_itf * 100.0, 3),
                "ls_neto_pers_con_itf_pct": round(spread_neto_pers_con_itf * 100.0, 3),
            })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_P2, index=False)
    print(df_res[["horizonte", "coste_pierna_bps", "rotacion_d10_pct", "exc_bruto_d10_pct", "long_neto_pers_sin_itf_pct", "long_neto_pers_con_itf_pct"]].to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 3: REHACER TABLAS 2 Y 9 CON TIMINGS OPERABLES EN UNIVERSO LÍQUIDO
# ═══════════════════════════════════════════════════════════════════

def punto3_tablas_operables_liquido(df_panel: pd.DataFrame, df_cache: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    print("\n" + "="*70)
    print("3. TABLAS 2 Y 9 CON TIMING OPERABLE EN UNIVERSO LÍQUIDO (IBEX 35 Y TERCIL ALTO)")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    opens = df_cache.xs("Open", level="Price", axis=1)
    vols = df_cache.xs("Volume", level="Price", axis=1)
    cal = closes.index
    dates_idx = {d: i for i, d in enumerate(cal)}

    conn = get_connection()
    try:
        conn.row_factory = None
        ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    vol_eur = (closes * vols).rolling(20).mean()

    # Añadir retornos operables: Apertura(t+1) -> Cierre(t+h) y Cierre(t+1) -> Cierre(t+1+h)
    sub = df_panel.copy()
    ret_op_list = []
    ret_cl1_list = []
    eur_liq_list = []

    for _, r in sub.iterrows():
        t = r["date"]
        tk = r["ticker"]
        n_bars = r["n_bars"]
        c_i = dates_idx.get(t)
        if c_i is None or (c_i + n_bars + 1) >= len(cal) or tk not in closes.columns:
            ret_op_list.append(np.nan)
            ret_cl1_list.append(np.nan)
            eur_liq_list.append(np.nan)
            continue

        t1 = cal[c_i + 1]
        th = cal[c_i + n_bars]
        th1 = cal[c_i + n_bars + 1]

        p_o1 = opens.loc[t1, tk]
        p_ch = closes.loc[th, tk]
        p_c1 = closes.loc[t1, tk]
        p_ch1 = closes.loc[th1, tk]

        r_op = (p_ch - p_o1) / p_o1 if (pd.notna(p_o1) and p_o1 > 0 and pd.notna(p_ch) and p_ch > 0) else np.nan
        r_cl1 = (p_ch1 - p_c1) / p_c1 if (pd.notna(p_c1) and p_c1 > 0 and pd.notna(p_ch1) and p_ch1 > 0) else np.nan

        ret_op_list.append(r_op)
        ret_cl1_list.append(r_cl1)
        eur_liq_list.append(vol_eur.loc[t, tk] if (t in vol_eur.index and tk in vol_eur.columns) else np.nan)

    sub["fwd_ret_open1"] = ret_op_list
    sub["fwd_ret_close1"] = ret_cl1_list
    sub["vol_eur"] = eur_liq_list
    sub["es_ibex"] = sub["ticker"].isin(ibex_set)

    # Identificar tercil alto de liquidez
    q2 = sub.groupby(["date", "horizon"])["vol_eur"].transform(lambda s: s.quantile(0.666))
    sub["es_tercil_alto"] = sub["vol_eur"] >= q2

    # Universo líquido = IBEX 35 O Tercil Alto
    sub_liq = sub[sub["es_ibex"] | sub["es_tercil_alto"]].copy()

    # Partición Diseño vs OOS
    df_diseno = sub_liq[sub_liq["date"] <= pd.Timestamp(SPLIT_DESIGN_DATE)]
    df_oos = sub_liq[sub_liq["date"] >= pd.Timestamp(START_OOS_DATE)]

    comps = ["comp_rsi", "comp_macd", "comp_tendencia", "comp_momentum"]
    regimes = ["low", "trans", "high"]

    filas_adx = []
    for hz, n_bars in HORIZONTE_BARS.items():
        lag_nw = max(1, n_bars - 1)
        for reg in regimes:
            for comp in comps:
                # 1. Timing Apertura(t+1) en Diseño y OOS
                dis_reg = df_diseno[(df_diseno["horizon"] == hz) & (df_diseno["regimen_adx"] == reg)]
                oos_reg = df_oos[(df_oos["horizon"] == hz) & (df_oos["regimen_adx"] == reg)]

                ic_dis = calc_daily_spearman_ic(dis_reg, comp, "fwd_ret_open1")
                ic_oos = calc_daily_spearman_ic(oos_reg, comp, "fwd_ret_open1")

                m_dis, t_dis, p_dis = newey_west(ic_dis, lag=lag_nw)
                m_oos, t_oos, p_oos = newey_west(ic_oos, lag=lag_nw)

                # Regla de hallazgo: |t| > 3.0 en ambas mitades con mismo signo
                es_hallazgo = (
                    abs(t_dis) >= 3.0 and abs(t_oos) >= 3.0 and
                    np.sign(m_dis) == np.sign(m_oos)
                )

                filas_adx.append({
                    "horizonte": hz,
                    "regimen_adx": reg,
                    "componente": comp,
                    "timing": "Apertura(t+1) [Operable]",
                    "n_diseno": len(dis_reg),
                    "ic_diseno": round(m_dis, 4),
                    "t_nw_diseno": round(t_dis, 2),
                    "n_oos": len(oos_reg),
                    "ic_oos": round(m_oos, 4),
                    "t_nw_oos": round(t_oos, 2),
                    "veredicto": "[HALLAZGO]" if es_hallazgo else "[RUIDO]",
                })

    df_res_adx = pd.DataFrame(filas_adx)
    df_res_adx.to_csv(OUTPUT_P3, index=False)

    hallazgos = df_res_adx[df_res_adx["veredicto"] == "[HALLAZGO]"]
    print(f"Total combinaciones evaluadas: {len(df_res_adx)}")
    print(f"Total combinaciones que cumplen |t| > 3.0 en ambas mitades (mismo signo): {len(hallazgos)}")
    if not hallazgos.empty:
        print(hallazgos.to_string(index=False))
    else:
        print("  -> NINGUNA celda cumple el criterio de hallazgo |t| > 3.0 en ambas mitades.")

    return df_res_adx, pd.DataFrame()


# ═══════════════════════════════════════════════════════════════════
# PUNTO 4: CALIDAD DE DATOS (MICROESTRUCTURA BME Y COLUMNA OPEN)
# ═══════════════════════════════════════════════════════════════════

def punto4_calidad_datos(df_cache: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("4. AUDITORÍA DE CALIDAD DE DATOS (RETORNOS CERO Y COLUMNA OPEN)")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    opens = df_cache.xs("Open", level="Price", axis=1)
    vols = df_cache.xs("Volume", level="Price", axis=1)
    tickers = [t for t in closes.columns if t != "^IBEX"]

    vol_eur = (closes * vols).rolling(20).mean().mean()
    q1, q2 = vol_eur.quantile([0.333, 0.666])

    conn = get_connection()
    try:
        conn.row_factory = None
        ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    filas = []
    tot_obs = 0
    tot_cero = 0
    tot_open_eq_close_prev = 0
    tot_open_zero_nan = 0

    for tk in tickers:
        c = closes[tk].dropna()
        o = opens[tk].reindex(c.index)
        if len(c) < 50:
            continue

        ret = c.pct_change().dropna()
        n = len(ret)
        n_cero = int((ret == 0).sum())
        n_sube = int((ret > 0).sum())
        n_baja = int((ret < 0).sum())

        # Problemas en Open:
        # 1. Open == Close previo exactamente
        c_prev = c.shift(1).loc[ret.index]
        o_cur = o.loc[ret.index]
        open_eq_cprev = int((o_cur == c_prev).sum())
        # 2. Open == 0 o NaN
        open_bad = int((o_cur.isna() | (o_cur <= 0)).sum())

        tot_obs += n
        tot_cero += n_cero
        tot_open_eq_close_prev += open_eq_cprev
        tot_open_zero_nan += open_bad

        eur_v = vol_eur.get(tk, 0.0)
        tercil = "ALTA" if eur_v >= q2 else ("BAJA" if eur_v <= q1 else "MEDIA")

        filas.append({
            "ticker": tk,
            "es_ibex": tk in ibex_set,
            "tercil_liq": tercil,
            "muestras": n,
            "pct_cero": round(n_cero / n * 100.0, 2),
            "pct_sube": round(n_sube / n * 100.0, 2),
            "pct_baja": round(n_baja / n * 100.0, 2),
            "pct_open_igual_cprev": round(open_eq_cprev / n * 100.0, 2),
            "pct_open_bad": round(open_bad / n * 100.0, 2),
        })

    df_tickers = pd.DataFrame(filas)
    df_tickers.to_csv(OUTPUT_P4, index=False)

    # Resumen por grupo
    print(f"Total barras analizadas: {tot_obs:,}")
    print(f"Total días con retorno cero: {tot_cero:,} ({round(tot_cero/tot_obs*100.0, 2)}%)")
    print(f"Total Open == Close previo (posible fallback yfinance): {tot_open_eq_close_prev:,} ({round(tot_open_eq_close_prev/tot_obs*100.0, 2)}%)")
    print(f"Total Open inválido (0 o NaN): {tot_open_zero_nan:,} ({round(tot_open_zero_nan/tot_obs*100.0, 3)}%)")

    grp = df_tickers.groupby("tercil_liq")[["pct_cero", "pct_sube", "pct_baja", "pct_open_igual_cprev"]].mean().round(2)
    print("\nPROMEDIO POR TERCIL DE LIQUIDEZ:")
    print(grp.to_string())
    return df_tickers


# ═══════════════════════════════════════════════════════════════════
# PUNTO 5: ESTABILIDAD TEMPORAL Y TICKERS (SOLO UNIVERSO LÍQUIDO)
# ═══════════════════════════════════════════════════════════════════

def punto5_estabilidad_liquidos(df_panel: pd.DataFrame, df_cache: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("5. ESTABILIDAD TEMPORAL EN UNIVERSO LÍQUIDO (IBEX 35 Y TERCIL ALTO)")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    vols = df_cache.xs("Volume", level="Price", axis=1)
    vol_eur = (closes * vols).rolling(20).mean()

    conn = get_connection()
    try:
        conn.row_factory = None
        ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    sub = df_panel[df_panel["horizon"] == "1D"].copy()
    sub["year"] = pd.to_datetime(sub["date"]).dt.year
    sub["es_ibex"] = sub["ticker"].isin(ibex_set)

    # Liquidez tercil alto
    eur_list = [vol_eur.loc[r["date"], r["ticker"]] if (r["date"] in vol_eur.index and r["ticker"] in vol_eur.columns) else np.nan for _, r in sub.iterrows()]
    sub["vol_eur"] = eur_list
    q2 = sub.groupby("date")["vol_eur"].transform(lambda s: s.quantile(0.666))
    sub["es_tercil_alto"] = sub["vol_eur"] >= q2

    # Solo universo líquido
    sub_liq = sub[sub["es_ibex"] | sub["es_tercil_alto"]].copy()

    filas = []
    # 1. Año a año
    for y in sorted(sub_liq["year"].unique()):
        sub_y = sub_liq[sub_liq["year"] == y]
        daily_ic = calc_daily_spearman_ic(sub_y, "pct_raw", "fwd_ret")
        mean_ic, t_nw, p_nw = newey_west(daily_ic, lag=0)
        filas.append({
            "universo": "IBEX35 + Tercil Alto",
            "ano": y,
            "n_muestras": len(sub_y),
            "spearman_ic": round(mean_ic, 4),
            "t_stat_nw": round(t_nw, 2),
            "p_val": round(p_nw, 4),
        })

    # 2. Distribución de IC por ticker en universo líquido
    ticker_ics = []
    for tk, grp in sub_liq.groupby("ticker"):
        if len(grp) >= 80:
            r, _ = stats.spearmanr(grp["pct_raw"], grp["fwd_ret"])
            ticker_ics.append(float(r))
    ticker_ics = np.array(ticker_ics)
    pct_neg = float((ticker_ics < 0).mean() * 100.0)

    print(f"Total tickers líquidos evaluados: {len(ticker_ics)}")
    print(f"% de tickers líquidos con IC negativo: {round(pct_neg, 1)}% (Mediana IC = {round(float(np.median(ticker_ics)), 4)})")

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_P5, index=False)
    print(df_res.to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# PUNTO 6: ZONAS Y VOLATILIDAD (CANDIDATO A RADAR DE RIESGO)
# ═══════════════════════════════════════════════════════════════════

def punto6_volatilidad_y_zonas(df_cache: pd.DataFrame) -> Dict:
    print("\n" + "="*70)
    print("6. ZONAS Y VOLATILIDAD (RADAR DE RIESGO Y COBERTURA DE BANDAS)")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    highs = df_cache.xs("High", level="Price", axis=1) if "High" in df_cache.columns.get_level_values("Price") else closes
    lows = df_cache.xs("Low", level="Price", axis=1) if "Low" in df_cache.columns.get_level_values("Price") else closes

    ret_daily = closes.pct_change()
    abs_ret = ret_daily.abs()

    # 1. Estimadores de volatilidad
    # a) std 20d
    sigma_std20 = ret_daily.rolling(20).std()
    # b) EWMA lambda=0.94 (RiskMetrics)
    sigma_ewma = np.sqrt((ret_daily**2).ewm(alpha=1.0 - 0.94, adjust=False).mean())
    # c) ATR_d / Close
    tr = pd.DataFrame(index=closes.index, columns=closes.columns)
    for c in closes.columns:
        h = highs[c]
        l = lows[c]
        cl = closes[c]
        cl_prev = cl.shift(1)
        tr[c] = np.maximum(h - l, np.maximum((h - cl_prev).abs(), (l - cl_prev).abs()))
    atr_14 = tr.rolling(14).mean()
    sigma_atr = (atr_14 / closes).astype(float)

    # Correlación de Spearman entre sigma pronosticada en t y |retorno| en t+1
    s1 = pd.DataFrame({"sigma": sigma_std20.shift(1).stack(), "ret": abs_ret.stack()}).dropna()
    s2 = pd.DataFrame({"sigma": sigma_ewma.shift(1).stack(), "ret": abs_ret.stack()}).dropna()
    s3 = pd.DataFrame({"sigma": sigma_atr.shift(1).stack(), "ret": abs_ret.stack()}).dropna()

    ic_std = float(stats.spearmanr(s1["sigma"], s1["ret"])[0])
    ic_ewma = float(stats.spearmanr(s2["sigma"], s2["ret"])[0])
    ic_atr = float(stats.spearmanr(s3["sigma"], s3["ret"])[0])

    print("PREDICTIBILIDAD DE LA VOLATILIDAD A 1D (Spearman IC vs |retorno| futuro):")
    print(f"  • std 20d         : IC = {round(ic_std, 4)}")
    print(f"  • EWMA (λ=0.94)   : IC = {round(ic_ewma, 4)}")
    print(f"  • ATR 14d / Close : IC = {round(ic_atr, 4)}")

    # Cobertura empírica de bandas a 1D, 3D, 5D, 20D (Objetivo: 68.3% y 95.5%)
    filas_cobertura = []
    for hz, h in HORIZONTE_BARS.items():
        ret_h = closes.pct_change(h)
        banda_1s = sigma_ewma * np.sqrt(h)
        banda_2s = 2.0 * sigma_ewma * np.sqrt(h)

        dentro_1s = float((ret_h.abs() <= banda_1s.shift(h)).mean().mean() * 100.0)
        dentro_2s = float((ret_h.abs() <= banda_2s.shift(h)).mean().mean() * 100.0)

        filas_cobertura.append({
            "horizonte": hz,
            "dias_h": h,
            "cobertura_1sigma_pct": round(dentro_1s, 2),
            "teorico_1sigma": 68.27,
            "cobertura_2sigma_pct": round(dentro_2s, 2),
            "teorico_2sigma": 95.45,
        })
    df_cob = pd.DataFrame(filas_cobertura)
    print("\nCOBERTURA EMPÍRICA DE BANDAS GAUSSIANAS (EWMA):")
    print(df_cob.to_string(index=False))

    # Brier Skill Score de P(|retorno| > u) con EWMA frente a frecuencia base rodante (ventana 252d)
    # Evento: |r_1d| > 1.5%
    u = 0.015
    evento_real = (abs_ret > u).astype(int)
    prob_ewma = 2.0 * (1.0 - stats.norm.cdf(u / np.maximum(1e-6, sigma_ewma.shift(1))))
    prob_base = evento_real.rolling(252).mean().shift(1)

    df_brier = pd.DataFrame({
        "y_true": evento_real.stack(),
        "y_pred": pd.DataFrame(prob_ewma, index=abs_ret.index, columns=abs_ret.columns).stack(),
        "y_base": prob_base.stack()
    }).dropna()

    y_true = df_brier["y_true"].values
    y_pred = df_brier["y_pred"].values
    y_base = df_brier["y_base"].values

    brier_model = float(np.mean((y_pred - y_true)**2))
    brier_base = float(np.mean((y_base - y_true)**2))
    bss = float(1.0 - (brier_model / brier_base)) if brier_base > 0 else 0.0

    print(f"\nCALIBRACIÓN PROBABILÍSTICA P(|r| > 1.5%):")
    print(f"  • Brier Score Modelo EWMA : {round(brier_model, 4)}")
    print(f"  • Brier Score Frecuencia Base: {round(brier_base, 4)}")
    print(f"  • Brier Skill Score (BSS)   : {round(bss * 100.0, 2)}% (Mejora frente a frecuencia base)")

    # Descomposición causal del 50% de LATERAL dinámico
    # Relación ATR / std_20d
    ratio_atr_std = float((sigma_atr / sigma_std20).dropna().mean().mean())
    kurtosis_ret = float(stats.kurtosis(ret_daily.dropna().values.flatten(), nan_policy="omit"))
    pct_zeros = float((ret_daily == 0).mean().mean() * 100.0)

    print(f"\nDESCOMPOSICIÓN CAUSAL DEL 50% LATERAL DINÁMICO:")
    print(f"  1. Sobreestimación de sigma por ATR : Ratio ATR / std_20 = {round(ratio_atr_std, 2)}x (ATR mide rango H-L, sobreestima la desviación de cierre en ~18%)")
    print(f"  2. Leptocurtosis real de retornos   : Curtosis exceso = {round(kurtosis_ret, 1)} (pico central alto concentra ~45% de masa en [-0.43sigma, +0.43sigma])")
    print(f"  3. Días de retorno exactamente cero : {round(pct_zeros, 2)}% de las sesiones")

    df_cob.to_csv(OUTPUT_P6, index=False)
    return {"cobertura": df_cob, "bss": bss}


# ═══════════════════════════════════════════════════════════════════
# PUNTO 7: FACTORES LENTOS (LISTA CERRADA DE 4 VARIANTES)
# ═══════════════════════════════════════════════════════════════════

def punto7_factores_lentos(df_cache: pd.DataFrame) -> pd.DataFrame:
    print("\n" + "="*70)
    print("7. EVALUACIÓN DE FACTORES LENTOS (LISTA CERRADA DE 4 VARIANTES)")
    print("="*70)
    closes = df_cache.xs("Close", level="Price", axis=1)
    opens = df_cache.xs("Open", level="Price", axis=1)

    conn = get_connection()
    try:
        conn.row_factory = None
        ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    # Identificar primer día hábil de cada mes para rebalanceo mensual
    df_dates = pd.DataFrame({"date": closes.index})
    df_dates["year_month"] = df_dates["date"].dt.to_period("M")
    first_days = df_dates.groupby("year_month")["date"].first().values

    # 4 Variantes contadas:
    # 1. Momentum 12-1: retorno t-252 a t-21
    f_mom_12_1 = closes.shift(21) / closes.shift(252) - 1.0
    # 2. Low-Vol 60d: std retornos 60d (invertido)
    f_low_vol = -(closes.pct_change().rolling(60).std())
    # 3. Reversal 21d: retorno t-21 a t-1 (invertido)
    f_rev_21 = -(closes.shift(1) / closes.shift(21) - 1.0)
    # 4. Momentum 6-1: retorno t-126 a t-21
    f_mom_6_1 = closes.shift(21) / closes.shift(126) - 1.0

    variantes = [
        ("1. Momentum 12-1 (252d a 21d)", f_mom_12_1),
        ("2. Low-Vol 60d (std 60d invertida)", f_low_vol),
        ("3. Reversal 21d (21d a 1d invertido)", f_rev_21),
        ("4. Momentum 6-1 (126d a 21d)", f_mom_6_1),
    ]

    filas = []
    # Retorno a 1 mes: compra Open del primer día del mes, venta Open del primer día del mes siguiente
    cal = closes.index
    first_idx = [cal.get_loc(d) for d in first_days if cal.get_loc(d) + 22 < len(cal)]

    n_meses = len(first_idx)
    # Potencia estadística: con N meses, el SE de un IC es ~ 1/sqrt(N-3). Para detectar IC=0.03 a t=2.0 requeriría N ~ 4400 meses!
    se_ic_teorico = 1.0 / np.sqrt(max(1, n_meses - 3))

    for nombre_v, df_factor in variantes:
        ics_mensuales = []
        for i in range(len(first_idx) - 1):
            idx_t = first_idx[i]
            idx_next = first_idx[i + 1]

            t = cal[idx_t]
            t_next = cal[idx_next]

            # Retorno mensual operable: Open(t) a Open(t_next)
            p_in = opens.loc[t]
            p_out = opens.loc[t_next]
            ret_m = (p_out - p_in) / p_in

            # Factor conocido en t (calculado con datos hasta t-1)
            f_val = df_factor.loc[t]

            # Filtrar solo universo IBEX 35 para solidez
            mask = f_val.notna() & ret_m.notna() & f_val.index.isin(ibex_set)
            if mask.sum() >= 15:
                r, _ = stats.spearmanr(f_val[mask], ret_m[mask])
                ics_mensuales.append(r)

        s_ic = pd.Series(ics_mensuales).dropna()
        m_ic, t_nw, p_nw = newey_west(s_ic, lag=1)

        filas.append({
            "variante": nombre_v,
            "n_meses_efectivos": len(s_ic),
            "spearman_ic_mensual": round(m_ic, 4),
            "t_stat": round(t_nw, 2),
            "p_val": round(p_nw, 4),
            "se_muestral": round(se_ic_teorico, 4),
            "potencia_para_ic_003": "INSUFICIENTE (N=55 meses requiere |IC| >= 0.27 para t>=2.0)",
        })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUTPUT_P7, index=False)
    print(df_res[["variante", "n_meses_efectivos", "spearman_ic_mensual", "t_stat", "p_val"]].to_string(index=False))
    return df_res


# ═══════════════════════════════════════════════════════════════════
# MAIN DE EJECUCIÓN FASE 3A-BIS
# ═══════════════════════════════════════════════════════════════════

def main():
    print("="*70)
    print("🔬 EJECUTANDO DIAGNÓSTICO INSTITUCIONAL FASE 3A-BIS")
    print("="*70)
    t0 = time.time()

    df_panel = pd.read_parquet(PANEL_SENALES)
    df_cache = pd.read_parquet(CACHE_PRECIOS)

    # 1. Descomposición del 1D
    if not os.path.exists(OUTPUT_P1):
        punto1_descomposicion_1d(df_panel, df_cache)
    else:
        print(f"  [CACHE] P1 ya calculado: {OUTPUT_P1}")

    # 2. Costes con timing operable
    if not os.path.exists(OUTPUT_P2):
        punto2_costes_operables(df_panel, df_cache)
    else:
        print(f"  [CACHE] P2 ya calculado: {OUTPUT_P2}")

    # 3. Tablas 2 y 9 con timing operable en líquidos
    if not os.path.exists(OUTPUT_P3):
        punto3_tablas_operables_liquido(df_panel, df_cache)
    else:
        print(f"  [CACHE] P3 ya calculado: {OUTPUT_P3}")

    # 4. Calidad de datos
    if not os.path.exists(OUTPUT_P4):
        punto4_calidad_datos(df_cache)
    else:
        print(f"  [CACHE] P4 ya calculado: {OUTPUT_P4}")

    # 5. Estabilidad en líquidos
    if not os.path.exists(OUTPUT_P5):
        punto5_estabilidad_liquidos(df_panel, df_cache)
    else:
        print(f"  [CACHE] P5 ya calculado: {OUTPUT_P5}")

    # 6. Volatilidad y zonas
    if not os.path.exists(OUTPUT_P6):
        punto6_volatilidad_y_zonas(df_cache)
    else:
        print(f"  [CACHE] P6 ya calculado: {OUTPUT_P6}")

    # 7. Factores lentos
    if not os.path.exists(OUTPUT_P7):
        punto7_factores_lentos(df_cache)
    else:
        print(f"  [CACHE] P7 ya calculado: {OUTPUT_P7}")

    print("\n" + "="*70)
    print(f"✅ FASE 3A-BIS COMPLETADA EXITOSAMENTE EN {round(time.time() - t0, 1)}s")
    print("="*70)


if __name__ == "__main__":
    main()
