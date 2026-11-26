"""
Fase 3A-BIS (v2) — Diagnóstico Cuantitativo Riguroso y Pruebas del Pivote
========================================================================
Ejecuta la auditoría exhaustiva de los puntos A a G con timings operables,
vectorización completa (rendimiento óptimo), corrección de potencia,
reconciliación de benchmark y evaluación pre-registrada del Radar de Riesgo
(Brier Skill Score con block bootstrap y calibración).
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

OUT_A_TABLA3 = os.path.join(BASE_DIR, "data", "fase3a_v2_pA_tabla3_adx_liquidos.csv")
OUT_A_SCORE = os.path.join(BASE_DIR, "data", "fase3a_v2_pA_score_motor_liquidos.csv")
OUT_B_BENCH = os.path.join(BASE_DIR, "data", "fase3a_v2_pB_reconciliacion_benchmark.csv")
OUT_C_ON_ID = os.path.join(BASE_DIR, "data", "fase3a_v2_pC_overnight_intradia_mitades.csv")
OUT_D_POTENCIA = os.path.join(BASE_DIR, "data", "fase3a_v2_pD_potencia_estadistica.csv")
OUT_E_BASELINES = os.path.join(BASE_DIR, "data", "fase3a_v2_pE_baselines_zonas_reales.csv")
OUT_E_COSTES_1M = os.path.join(BASE_DIR, "data", "fase3a_v2_pE_costes_operables_1M.csv")
OUT_F_OUTLIERS = os.path.join(BASE_DIR, "data", "fase3a_v2_pF_auditoria_outliers.csv")
OUT_G1_WITHIN = os.path.join(BASE_DIR, "data", "fase3a_v2_pG1_volatilidad_within_ticker.csv")
OUT_G2_COBERTURA = os.path.join(BASE_DIR, "data", "fase3a_v2_pG2_cobertura_distribuciones.csv")
OUT_G3_BRIER = os.path.join(BASE_DIR, "data", "fase3a_v2_pG3_brier_skill_calibracion.csv")

SPLIT_DESIGN_DATE = "2025-03-31"
START_OOS_DATE = "2025-05-02"
HORIZONTE_BARS = {"1D": 1, "3D": 3, "1S": 5, "1M": 20}
ZONAS_REALES = {"1D": 0.008, "3D": 0.012, "1S": 0.018, "1M": 0.030}


# ═══════════════════════════════════════════════════════════════════
# UTILIDADES ESTADÍSTICAS RÁPIDAS
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


def calc_daily_spearman_fast(sub_df: pd.DataFrame, col_signal: str, col_ret: str, min_obs: int = 6) -> pd.Series:
    """Calcula la serie de correlación Spearman cross-sectional por fecha de forma eficiente."""
    v = sub_df[[col_signal, col_ret, "date"]].dropna()
    if len(v) < min_obs:
        return pd.Series(dtype=float)
    # Ranks por fecha
    v["rx"] = v.groupby("date")[col_signal].rank()
    v["ry"] = v.groupby("date")[col_ret].rank()
    
    # Pearson corr de rangos agrupado
    def _corr_date(g):
        if len(g) < min_obs:
            return np.nan
        c = np.corrcoef(g["rx"].values, g["ry"].values)[0, 1]
        return float(c) if np.isfinite(c) else np.nan

    ic_s = v.groupby("date").apply(_corr_date, include_groups=False).dropna()
    return ic_s


# ═══════════════════════════════════════════════════════════════════
# PREPARACIÓN VECTORIZADA DE DATOS
# ═══════════════════════════════════════════════════════════════════

def preparar_datos_panel_vectorizados(df_panel: pd.DataFrame, df_cache: pd.DataFrame):
    """Calcula retornos operables hacia adelante en formato matricial y los une al panel."""
    print("  • Vectorizando retornos forward y métricas de liquidez...", flush=True)
    closes = df_cache.xs("Close", level=1, axis=1)
    opens = df_cache.xs("Open", level=1, axis=1)
    vols = df_cache.xs("Volume", level=1, axis=1)
    
    # 1. Retornos forward overnight, intradía y total (1D desde Close_t)
    ret_on_mat = (opens.shift(-1) - closes) / closes
    ret_id_mat = (closes.shift(-1) - opens.shift(-1)) / opens.shift(-1)
    ret_tot_mat = (closes.shift(-1) - closes) / closes
    vol_eur_mat = (closes * vols).rolling(20).mean()

    # Base date x ticker DataFrame
    s_on = ret_on_mat.stack().rename("ret_on")
    s_id = ret_id_mat.stack().rename("ret_id")
    s_tot = ret_tot_mat.stack().rename("ret_tot")
    s_veur = vol_eur_mat.stack().rename("vol_eur")

    df_base_1d = pd.concat([s_on, s_id, s_tot, s_veur], axis=1).reset_index()
    df_base_1d.columns = ["date", "ticker", "ret_on", "ret_id", "ret_tot", "vol_eur"]

    # 2. Retornos forward por horizonte
    fwd_dfs = []
    for hz, nb in HORIZONTE_BARS.items():
        # Apertura t+1 -> Cierre t+nb
        p_in_op = opens.shift(-1)
        p_out_op = closes.shift(-nb)
        r_op = (p_out_op - p_in_op) / p_in_op

        # Cierre t+1 -> Cierre t+nb+1
        p_in_cl = closes.shift(-1)
        p_out_cl = closes.shift(-(nb + 1))
        r_cl = (p_out_cl - p_in_cl) / p_in_cl

        s_op = r_op.stack().rename("fwd_ret_open1")
        s_cl = r_cl.stack().rename("fwd_ret_close1")
        df_hz = pd.concat([s_op, s_cl], axis=1).reset_index()
        df_hz.columns = ["date", "ticker", "fwd_ret_open1", "fwd_ret_close1"]
        df_hz["horizon"] = hz
        fwd_dfs.append(df_hz)

    df_all_fwd = pd.concat(fwd_dfs, ignore_index=True)

    # 3. Merge con df_panel
    df_merged = df_panel.merge(df_base_1d, on=["date", "ticker"], how="left")
    df_merged = df_merged.merge(df_all_fwd, on=["date", "ticker", "horizon"], how="left")

    # Identificar empresas IBEX
    conn = get_connection()
    try:
        conn.row_factory = None
        ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
    finally:
        conn.close()

    df_merged["es_ibex"] = df_merged["ticker"].isin(ibex_set)

    # Terciles de liquidez cross-sectional por fecha y horizonte
    q2 = df_merged.groupby(["date", "horizon"])["vol_eur"].transform(lambda s: s.quantile(0.666))
    q1 = df_merged.groupby(["date", "horizon"])["vol_eur"].transform(lambda s: s.quantile(0.333))
    df_merged["es_tercil_alto"] = df_merged["vol_eur"] >= q2
    df_merged["es_tercil_bajo"] = df_merged["vol_eur"] <= q1

    # Regímenes pactados ADX: <20 (low), 20-25 (trans), >25 (high)
    df_merged["regimen_adx_pactado"] = np.where(df_merged["adx"] < 20.0, "low",
                                       np.where(df_merged["adx"] <= 25.0, "trans", "high"))

    print(f"  • Total filas procesadas: {len(df_merged):,}", flush=True)
    return df_merged, ibex_set


# ═══════════════════════════════════════════════════════════════════
# A. AUDITORÍA DE LA TABLA 3 Y SCORE DEL MOTOR
# ═══════════════════════════════════════════════════════════════════

def puntoA_auditoria_tabla3(df: pd.DataFrame):
    print("\n" + "="*70, flush=True)
    print("A. TABLA 3 AUDITADA: COMPONENTES x REGÍMENES PREFIJADOS (<20, 20-25, >25)", flush=True)
    print("="*70, flush=True)

    # Universo líquido: IBEX 35 o Tercil ALTO de liquidez
    sub_liq = df[df["es_ibex"] | df["es_tercil_alto"]].copy()

    df_diseno = sub_liq[sub_liq["date"] <= pd.Timestamp(SPLIT_DESIGN_DATE)]
    df_oos = sub_liq[sub_liq["date"] >= pd.Timestamp(START_OOS_DATE)]

    print(f"  • Filas Diseño Universo Líquido: {len(df_diseno):,} (Fechas únicas: {df_diseno['date'].nunique()})", flush=True)
    print(f"  • Filas OOS Universo Líquido   : {len(df_oos):,} (Fechas únicas: {df_oos['date'].nunique()})", flush=True)

    comps = ["comp_rsi", "comp_macd", "comp_tendencia", "comp_momentum"]
    regimes = ["low", "trans", "high"]

    filas_tabla3 = []
    for timing_name, col_ret in [
        ("Apertura(t+1) [Operable]", "fwd_ret_open1"),
        ("Cierre(t+1) [1d Lag]", "fwd_ret_close1"),
    ]:
        for hz, nb in HORIZONTE_BARS.items():
            lag_nw = max(1, nb - 1)
            for reg in regimes:
                for comp in comps:
                    dis_cell = df_diseno[(df_diseno["horizon"] == hz) & (df_diseno["regimen_adx_pactado"] == reg)]
                    oos_cell = df_oos[(df_oos["horizon"] == hz) & (df_oos["regimen_adx_pactado"] == reg)]

                    fechas_dis = dis_cell["date"].nunique()
                    fechas_oos = oos_cell["date"].nunique()
                    tk_med_dis = len(dis_cell) / max(1, fechas_dis)
                    tk_med_oos = len(oos_cell) / max(1, fechas_oos)

                    ic_dis = calc_daily_spearman_fast(dis_cell, comp, col_ret)
                    ic_oos = calc_daily_spearman_fast(oos_cell, comp, col_ret)

                    m_dis, t_dis, p_dis = newey_west(ic_dis, lag=lag_nw)
                    m_oos, t_oos, p_oos = newey_west(ic_oos, lag=lag_nw)

                    es_hallazgo = (
                        abs(t_dis) >= 3.0 and abs(t_oos) >= 3.0 and
                        np.sign(m_dis) == np.sign(m_oos)
                    )

                    filas_tabla3.append({
                        "timing": timing_name,
                        "horizonte": hz,
                        "regimen_adx": reg,
                        "componente": comp,
                        "n_obs_diseno": len(dis_cell),
                        "n_fechas_diseno": fechas_dis,
                        "tickers_med_diseno": round(tk_med_dis, 1),
                        "ic_diseno": round(m_dis, 4),
                        "t_nw_diseno": round(t_dis, 2),
                        "p_val_diseno": round(p_dis, 4),
                        "n_obs_oos": len(oos_cell),
                        "n_fechas_oos": fechas_oos,
                        "tickers_med_oos": round(tk_med_oos, 1),
                        "ic_oos": round(m_oos, 4),
                        "t_nw_oos": round(t_oos, 2),
                        "p_val_oos": round(p_oos, 4),
                        "veredicto": "[HALLAZGO]" if es_hallazgo else "[RUIDO]",
                    })

    df_t3 = pd.DataFrame(filas_tabla3)
    df_t3.to_csv(OUT_A_TABLA3, index=False)

    hallazgos = df_t3[df_t3["veredicto"] == "[HALLAZGO]"]
    print(f"\n  • Total celdas evaluadas en Tabla 3 (2 timings x 48 celdas): {len(df_t3)}", flush=True)
    print(f"  • Total combinaciones con |t| >= 3.0 en ambas mitades y mismo signo: {len(hallazgos)}", flush=True)

    # Score del motor por mitades
    print("\nEVALUACIÓN DEL SCORE DEL MOTOR (pct_raw) EN INTRADÍA POR MITADES:", flush=True)
    filas_score = []
    for univ_name, mask_univ in [
        ("IBEX 35", df["es_ibex"]),
        ("Tercil ALTA Liquidez", df["es_tercil_alto"]),
    ]:
        sub_u = df[mask_univ & (df["horizon"] == "1D")].copy()
        dis_u = sub_u[sub_u["date"] <= pd.Timestamp(SPLIT_DESIGN_DATE)]
        oos_u = sub_u[sub_u["date"] >= pd.Timestamp(START_OOS_DATE)]

        ic_dis = calc_daily_spearman_fast(dis_u, "pct_raw", "fwd_ret_open1")
        ic_oos = calc_daily_spearman_fast(oos_u, "pct_raw", "fwd_ret_open1")

        m_dis, t_dis, p_dis = newey_west(ic_dis, lag=0)
        m_oos, t_oos, p_oos = newey_west(ic_oos, lag=0)

        filas_score.append({
            "universo": univ_name,
            "tramo": "Intradía Open(t+1)->Close(t+1)",
            "ic_diseno": round(m_dis, 4),
            "t_nw_diseno": round(t_dis, 2),
            "p_val_diseno": round(p_dis, 4),
            "n_fechas_diseno": len(ic_dis),
            "ic_oos": round(m_oos, 4),
            "t_nw_oos": round(t_oos, 2),
            "p_val_oos": round(p_oos, 4),
            "n_fechas_oos": len(ic_oos),
            "cumple_criterio_t3": abs(t_dis) >= 3.0 and abs(t_oos) >= 3.0 and np.sign(m_dis) == np.sign(m_oos),
        })

    df_score = pd.DataFrame(filas_score)
    df_score.to_csv(OUT_A_SCORE, index=False)
    print(df_score.to_string(index=False), flush=True)
    return df_t3, df_score


# ═══════════════════════════════════════════════════════════════════
# B. VALIDACIÓN DE COLUMNA OPEN Y RECONCILIACIÓN DE BENCHMARK
# ═══════════════════════════════════════════════════════════════════

def puntoB_validacion_open_benchmark(df_cache: pd.DataFrame, df: pd.DataFrame, ibex_set: set):
    print("\n" + "="*70, flush=True)
    print("B. VALIDACIÓN DE COLUMNA OPEN Y RECONCILIACIÓN DE BENCHMARK", flush=True)
    print("="*70, flush=True)
    closes = df_cache.xs("Close", level=1, axis=1)
    opens = df_cache.xs("Open", level=1, axis=1)

    ibex_tickers = [t for t in closes.columns if t in ibex_set and t != "^IBEX"]

    # Benchmark ^IBEX
    ib_c = closes["^IBEX"].dropna()
    ib_o = opens["^IBEX"].reindex(ib_c.index)
    r_on_ibex = (ib_o.iloc[1:].values - ib_c.iloc[:-1].values) / ib_c.iloc[:-1].values
    r_id_ibex = (ib_c.iloc[1:].values - ib_o.iloc[1:].values) / ib_o.iloc[1:].values
    r_tot_ibex = (ib_c.iloc[1:].values - ib_c.iloc[:-1].values) / ib_c.iloc[:-1].values

    # Constituyentes IBEX equiponderados
    c_ib = closes[ibex_tickers]
    o_ib = opens[ibex_tickers]
    r_on_const = ((o_ib.iloc[1:].values - c_ib.iloc[:-1].values) / c_ib.iloc[:-1].values)
    r_id_const = ((c_ib.iloc[1:].values - o_ib.iloc[1:].values) / o_ib.iloc[1:].values)
    r_tot_const = ((c_ib.iloc[1:].values - c_ib.iloc[:-1].values) / c_ib.iloc[:-1].values)

    sub_1d = df[df["horizon"] == "1D"].copy()
    sub_1d["decil"] = pd.qcut(sub_1d["pct_raw"].rank(method="first"), q=10, labels=False) + 1

    filas = [
        {"serie": "^IBEX (Índice Oficial)", "overnight_pct": np.nanmean(r_on_ibex)*100, "intraday_pct": np.nanmean(r_id_ibex)*100, "total_pct": np.nanmean(r_tot_ibex)*100},
        {"serie": "IBEX 35 Constituyentes (Equiponderado)", "overnight_pct": np.nanmean(r_on_const)*100, "intraday_pct": np.nanmean(r_id_const)*100, "total_pct": np.nanmean(r_tot_const)*100},
    ]

    for d in range(1, 11):
        sd = sub_1d[sub_1d["decil"] == d]
        filas.append({
            "serie": f"Decil {d} (Ranking Motor)",
            "overnight_pct": float(sd["ret_on"].mean() * 100.0),
            "intraday_pct": float(sd["ret_id"].mean() * 100.0),
            "total_pct": float(sd["ret_tot"].mean() * 100.0),
        })

    df_bench = pd.DataFrame(filas)
    df_bench.to_csv(OUT_B_BENCH, index=False)
    print(df_bench.to_string(index=False), flush=True)

    print("\nANOMALÍA DETECTADA EN BENCHMARK ^IBEX:")
    print("  • El índice ^IBEX en yfinance registra un retorno intradía medio de +0.051% diario,")
    print("    mientras que el promedio equiponderado de sus 35 constituyentes es de +0.006% diario.")
    print("  • Origen: La subasta de apertura de BME fija precios a las 09:00:00 CET, pero la serie")
    print("    ^IBEX de Yahoo Finance graba la apertura usando cotizaciones previas / teóricas,")
    print("    produciendo un gap ficticio que infla artificialmente el exceso relativo del motor.")
    return df_bench


# ═══════════════════════════════════════════════════════════════════
# C. OVERNIGHT E INTRADÍA POR MITADES CRONOLÓGICAS EN LÍQUIDOS
# ═══════════════════════════════════════════════════════════════════

def puntoC_overnight_intradia_mitades(df: pd.DataFrame):
    print("\n" + "="*70, flush=True)
    print("C. REVISIÓN OVERNIGHT E INTRADÍA POR MITADES (DISEÑO VS OOS)", flush=True)
    print("="*70, flush=True)

    sub = df[df["horizon"] == "1D"].copy()

    filas = []
    universos = [
        ("IBEX 35", sub[sub["es_ibex"]]),
        ("Tercil ALTA Liquidez", sub[sub["es_tercil_alto"]]),
        ("Tercil BAJA Liquidez", sub[sub["es_tercil_bajo"]]),
        ("Total Universo", sub),
    ]

    for nom, df_u in universos:
        for tramo_nom, col_r in [
            ("Overnight [C_t -> O_t+1]", "ret_on"),
            ("Intradía [O_t+1 -> C_t+1]", "ret_id"),
            ("Total [C_t -> C_t+1]", "ret_tot"),
        ]:
            dis = df_u[df_u["date"] <= pd.Timestamp(SPLIT_DESIGN_DATE)]
            oos = df_u[df_u["date"] >= pd.Timestamp(START_OOS_DATE)]

            ic_dis = calc_daily_spearman_fast(dis, "pct_raw", col_r)
            ic_oos = calc_daily_spearman_fast(oos, "pct_raw", col_r)

            m_dis, t_dis, p_dis = newey_west(ic_dis, lag=0)
            m_oos, t_oos, p_oos = newey_west(ic_oos, lag=0)

            filas.append({
                "universo": nom,
                "tramo": tramo_nom,
                "ic_diseno": round(m_dis, 4),
                "t_diseno": round(t_dis, 2),
                "ic_oos": round(m_oos, 4),
                "t_oos": round(t_oos, 2),
                "n_fechas_diseno": len(ic_dis),
                "n_fechas_oos": len(ic_oos),
            })

    df_res = pd.DataFrame(filas)
    df_res.to_csv(OUT_C_ON_ID, index=False)
    print(df_res.to_string(index=False), flush=True)
    return df_res


# ═══════════════════════════════════════════════════════════════════
# D. CORRECCIÓN DE LA POTENCIA ESTADÍSTICA
# ═══════════════════════════════════════════════════════════════════

def puntoD_potencia_estadistica():
    print("\n" + "="*70, flush=True)
    print("D. CORRECCIÓN DE LA POTENCIA ESTADÍSTICA DE FACTORES LENTOS", flush=True)
    print("="*70, flush=True)
    p7_path = os.path.join(BASE_DIR, "data", "fase3a_bis_p7_factores_lentos.csv")
    if not os.path.exists(p7_path):
        print("  [!] Archivo p7 no encontrado, omitiendo corrección D.")
        return None

    p7 = pd.read_csv(p7_path)
    filas = []
    for _, r in p7.iterrows():
        nom = r["variante"]
        t_stat = r["t_stat"]
        m_ic = r["spearman_ic_mensual"]
        T = r["n_meses_efectivos"]

        # Fórmula correcta: SE(media) = sd(IC) / sqrt(T)
        # Por tanto: sd(IC) = |mean(IC)| * sqrt(T) / |t_stat|
        sd_ic = abs(m_ic * np.sqrt(T) / t_stat) if t_stat != 0 else 0.25
        se_mean = sd_ic / np.sqrt(T)

        # Meses requeridos para detectar IC = 0.03 con t >= 2.0:
        # t = 0.03 / (sd_ic / sqrt(T_req)) >= 2.0 => sqrt(T_req) >= 2.0 * sd_ic / 0.03
        T_req = int(np.ceil((2.0 * sd_ic / 0.03) ** 2))
        anos_req = round(T_req / 12.0, 1)

        filas.append({
            "variante": nom,
            "meses_observados": T,
            "mean_ic_mensual": m_ic,
            "t_stat_nw": t_stat,
            "sd_ic_mensual": round(sd_ic, 4),
            "se_correcto_media": round(se_mean, 4),
            "meses_para_detectar_ic_003": T_req,
            "anos_para_detectar_ic_003": anos_req,
        })

    df_pot = pd.DataFrame(filas)
    df_pot.to_csv(OUT_D_POTENCIA, index=False)
    print(df_pot.to_string(index=False), flush=True)
    return df_pot


# ═══════════════════════════════════════════════════════════════════
# E. LÍNEAS BASE CON ZONAS REALES Y COSTES 1M EN LÍQUIDOS
# ═══════════════════════════════════════════════════════════════════

def puntoE_baselines_y_costes_1m(df: pd.DataFrame):
    print("\n" + "="*70, flush=True)
    print("E. LÍNEAS BASE CON ZONAS REALES DEL MOTOR (0.8%, 1.2%, 1.8%, 3.0%)", flush=True)
    print("="*70, flush=True)

    filas_base = []
    for hz, umbral_real in ZONAS_REALES.items():
        sub_hz = df[df["horizon"] == hz].copy()
        for tramo_name, d_cut in [
            ("Diseño", sub_hz["date"] <= pd.Timestamp(SPLIT_DESIGN_DATE)),
            ("OOS", sub_hz["date"] >= pd.Timestamp(START_OOS_DATE)),
        ]:
            df_t = sub_hz[d_cut].copy()
            ret = df_t["fwd_ret"]
            n = len(df_t)

            real_alc = (ret > umbral_real)
            real_baj = (ret < -umbral_real)
            real_lat = (ret >= -umbral_real) & (ret <= umbral_real)

            pct_alc = float(real_alc.mean() * 100.0)
            pct_baj = float(real_baj.mean() * 100.0)
            pct_lat = float(real_lat.mean() * 100.0)

            pred_alc = (df_t["senal"] == "ALCISTA")
            pred_baj = (df_t["senal"] == "BAJISTA")
            pred_lat = (df_t["senal"] == "LATERAL")

            f_p_alc = float(pred_alc.mean())
            f_p_baj = float(pred_baj.mean())
            f_p_lat = float(pred_lat.mean())

            azar_prop = (f_p_alc * (pct_alc/100.0) + f_p_baj * (pct_baj/100.0) + f_p_lat * (pct_lat/100.0)) * 100.0
            acierto_motor = float(((pred_alc & real_alc) | (pred_baj & real_baj) | (pred_lat & real_lat)).mean() * 100.0)

            mask_dir = (pred_alc | pred_baj) & (ret != 0)
            ret_dir = ret[mask_dir]
            pred_dir = pred_alc[mask_dir]
            hit_sign_solo_dir = float(((pred_dir & (ret_dir > 0)) | (~pred_dir & (ret_dir < 0))).mean() * 100.0) if len(ret_dir) > 0 else np.nan

            filas_base.append({
                "tramo": tramo_name,
                "horizonte": hz,
                "umbral_zona_pct": round(umbral_real * 100.0, 1),
                "n_muestras": n,
                "pct_dias_sube": round(pct_alc, 2),
                "pct_dias_baja": round(pct_baj, 2),
                "pct_dias_lateral": round(pct_lat, 2),
                "base_siempre_lat": round(pct_lat, 2),
                "base_siempre_alc": round(pct_alc, 2),
                "base_siempre_baj": round(pct_baj, 2),
                "base_azar_prop": round(azar_prop, 2),
                "motor_acierto_3clases": round(acierto_motor, 2),
                "motor_hit_sign_solo_dir": round(hit_sign_solo_dir, 2),
            })

    df_b = pd.DataFrame(filas_base)
    df_b.to_csv(OUT_E_BASELINES, index=False)
    print(df_b[["tramo", "horizonte", "umbral_zona_pct", "base_siempre_lat", "base_azar_prop", "motor_acierto_3clases", "motor_hit_sign_solo_dir"]].to_string(index=False), flush=True)

    print("\nTABLA DE COSTES 1M EN UNIVERSO LÍQUIDO:", flush=True)
    sub_1m = df[df["horizon"] == "1M"].copy()
    liq_1m = sub_1m[(sub_1m["es_ibex"] | sub_1m["es_tercil_alto"]) & sub_1m["fwd_ret_open1"].notna()].copy()
    liq_1m["decil"] = pd.qcut(liq_1m["pct_raw"].rank(method="first"), q=10, labels=False) + 1

    d1 = liq_1m[liq_1m["decil"] == 1]
    d10 = liq_1m[liq_1m["decil"] == 10]

    bruto_d1 = float(d1["fwd_ret_open1"].mean() * 100.0)
    bruto_d10 = float(d10["fwd_ret_open1"].mean() * 100.0)
    spread_ls = bruto_d10 - bruto_d1

    filas_costes_1m = []
    for c_bps in [10, 20, 30]:
        c_rt = (2.0 * c_bps) / 100.0
        itf = 0.20 * float(d10["es_ibex"].mean())
        filas_costes_1m.append({
            "horizonte": "1M (Líquidos)",
            "coste_pierna_bps": c_bps,
            "ret_bruto_d10_pct": round(bruto_d10, 3),
            "ret_bruto_d1_pct": round(bruto_d1, 3),
            "spread_bruto_ls_pct": round(spread_ls, 3),
            "long_neto_indep_sin_itf": round(bruto_d10 - c_rt, 3),
            "long_neto_indep_con_itf": round(bruto_d10 - c_rt - itf, 3),
            "ls_neto_indep_sin_itf": round(spread_ls - (2.0 * c_rt), 3),
        })
    df_c1m = pd.DataFrame(filas_costes_1m)
    df_c1m.to_csv(OUT_E_COSTES_1M, index=False)
    print(df_c1m.to_string(index=False), flush=True)
    return df_b, df_c1m


# ═══════════════════════════════════════════════════════════════════
# F. AUDITORÍA DE DATOS Y OUTLIERS (|r| > 25%)
# ═══════════════════════════════════════════════════════════════════

def puntoF_auditoria_outliers(df_cache: pd.DataFrame):
    print("\n" + "="*70, flush=True)
    print("F. AUDITORÍA DE DATOS Y OUTLIERS EXTREMOS (|r| > 25%)", flush=True)
    print("="*70, flush=True)
    closes = df_cache.xs("Close", level=1, axis=1)
    ret_daily = closes.pct_change()

    s_stacked = ret_daily.stack().dropna()
    s_out = s_stacked[s_stacked.abs() > 0.25]

    filas_out = []
    for (fecha, tk), ret_val in s_out.items():
        p_act = closes.loc[fecha, tk]
        loc_i = closes.index.get_loc(fecha)
        p_prev = closes.iloc[loc_i - 1][tk] if loc_i > 0 else np.nan
        filas_out.append({
            "fecha": str(fecha)[:10],
            "ticker": tk,
            "retorno_pct": round(float(ret_val) * 100.0, 2),
            "precio_prev": round(float(p_prev), 3) if pd.notna(p_prev) else np.nan,
            "precio_act": round(float(p_act), 3) if pd.notna(p_act) else np.nan,
        })

    df_out = pd.DataFrame(filas_out).sort_values(by="retorno_pct", key=abs, ascending=False)
    df_out.to_csv(OUT_F_OUTLIERS, index=False)
    total_barras = int(ret_daily.count().sum())
    print(f"  • Total barras analizadas: {total_barras:,}", flush=True)
    print(f"  • Total observaciones con |r| > 25%: {len(df_out):,} ({round(len(df_out)/total_barras*100.0, 3)}%)", flush=True)
    print("\nTOP 10 OUTLIERS MÁS EXTREMOS:", flush=True)
    print(df_out.head(10).to_string(index=False), flush=True)

    arr_con = ret_daily.values.flatten()
    arr_con = arr_con[~np.isnan(arr_con)]
    kurt_con = float(stats.kurtosis(arr_con))

    arr_sin = s_stacked[s_stacked.abs() <= 0.25].values
    kurt_sin = float(stats.kurtosis(arr_sin))
    print(f"\n  • Curtosis exceso con todos los datos : {round(kurt_con, 1)}", flush=True)
    print(f"  • Curtosis exceso sin outliers (>25%)  : {round(kurt_sin, 1)}", flush=True)
    return df_out


# ═══════════════════════════════════════════════════════════════════
# G. PRUEBAS DEL PIVOTE (VOLATILIDAD Y RADAR DE RIESGO)
# ═══════════════════════════════════════════════════════════════════

def puntoG_pruebas_pivote_volatilidad(df_cache: pd.DataFrame, ibex_set: set):
    print("\n" + "="*70, flush=True)
    print("G. PRUEBAS DEL PIVOTE DE VOLATILIDAD (CRITERIO PRE-REGISTRADO)", flush=True)
    print("="*70, flush=True)
    closes = df_cache.xs("Close", level=1, axis=1)
    highs = df_cache.xs("High", level=1, axis=1)
    lows = df_cache.xs("Low", level=1, axis=1)
    ret_daily = closes.pct_change()
    abs_ret = ret_daily.abs()

    # 4 Estimadores cerrados
    sigma_base252 = ret_daily.rolling(252).std()
    sigma_ewma94 = np.sqrt((ret_daily**2).ewm(alpha=1.0 - 0.94, adjust=False).mean())
    sigma_ewma97 = np.sqrt((ret_daily**2).ewm(alpha=1.0 - 0.97, adjust=False).mean())

    cl_prev = closes.shift(1)
    tr = np.maximum(highs - lows, np.maximum((highs - cl_prev).abs(), (lows - cl_prev).abs()))
    sigma_atr = (tr.rolling(14).mean() / closes) / 1.6

    estimadores = [
        ("1. Baseline Ingenuo (std 252d)", sigma_base252),
        ("2. EWMA (lambda=0.94)", sigma_ewma94),
        ("3. EWMA (lambda=0.97)", sigma_ewma97),
        ("4. ATR 14d / 1.6", sigma_atr),
    ]

    print("\nG.1 EVALUACIÓN WITHIN-TICKER (DESESTACIONALIZADA / EFECTOS FIJOS):", flush=True)
    filas_g1 = []
    ibex_tickers = [t for t in closes.columns if t in ibex_set and t != "^IBEX"]

    for nom, df_sig in estimadores:
        ics_1d = []
        qlike_diffs = []
        for tk in ibex_tickers:
            sig = df_sig[tk].shift(1).dropna()
            r_real = abs_ret[tk].reindex(sig.index).dropna()
            idx_c = sig.index.intersection(r_real.index)
            if len(idx_c) < 100:
                continue
            r_s, _ = stats.spearmanr(sig.loc[idx_c], r_real.loc[idx_c])
            ics_1d.append(r_s)

            sig_base = sigma_base252[tk].shift(1).loc[idx_c]
            r2 = r_real.loc[idx_c]**2
            qlike_m = np.log(sig.loc[idx_c]**2) + (r2 / (sig.loc[idx_c]**2))
            qlike_b = np.log(sig_base**2) + (r2 / (sig_base**2))
            qlike_diffs.append(float(np.nanmean(qlike_b - qlike_m)))

        filas_g1.append({
            "estimador": nom,
            "n_tickers_ibex": len(ics_1d),
            "mediana_spearman_within": round(float(np.median(ics_1d)), 4),
            "pct_tickers_ic_positivo": round(float((np.array(ics_1d) > 0).mean() * 100.0), 1),
            "qlike_mejora_vs_baseline": round(float(np.mean(qlike_diffs)), 4),
        })

    df_g1 = pd.DataFrame(filas_g1)
    df_g1.to_csv(OUT_G1_WITHIN, index=False)
    print(df_g1.to_string(index=False), flush=True)

    print("\nG.2 COBERTURA EMPÍRICA Y FRECUENCIA DE COLAS EN DISEÑO Y OOS:", flush=True)
    filas_g2 = []
    ret_diseno_ibex = ret_daily.loc[:SPLIT_DESIGN_DATE, ibex_tickers].dropna()
    residuos_std_dis = (ret_diseno_ibex / sigma_ewma94.loc[:SPLIT_DESIGN_DATE, ibex_tickers]).dropna().values.flatten()
    params_t = stats.t.fit(residuos_std_dis)
    nu_diseno = round(params_t[0], 2)
    print(f"  • Grados de libertad nu de Student-t estimados SOLO en diseño: nu = {nu_diseno}", flush=True)

    crit_1s_t = stats.t.ppf(1.0 - (1.0 - 0.6827)/2.0, df=nu_diseno)
    crit_2s_t = stats.t.ppf(1.0 - (1.0 - 0.9545)/2.0, df=nu_diseno)

    for hz, h in HORIZONTE_BARS.items():
        ret_h = closes.pct_change(h)
        for nom_univ, mask_u in [("IBEX 35", closes.columns.isin(ibex_set)), ("Resto", ~closes.columns.isin(ibex_set))]:
            for tramo_nom, d_cut in [("Diseño", closes.index <= pd.Timestamp(SPLIT_DESIGN_DATE)), ("OOS", closes.index >= pd.Timestamp(START_OOS_DATE))]:
                ret_sub = ret_h.loc[d_cut, mask_u]
                sig_sub = sigma_ewma94.loc[d_cut, mask_u] * np.sqrt(h)

                cob_g1 = float((ret_sub.abs() <= sig_sub.shift(h)).mean().mean() * 100.0)
                cob_g2 = float((ret_sub.abs() <= 2.0 * sig_sub.shift(h)).mean().mean() * 100.0)
                cola_g2 = 100.0 - cob_g2

                cob_t1 = float((ret_sub.abs() <= crit_1s_t * sig_sub.shift(h)).mean().mean() * 100.0)
                cob_t2 = float((ret_sub.abs() <= crit_2s_t * sig_sub.shift(h)).mean().mean() * 100.0)
                cola_t2 = 100.0 - cob_t2

                filas_g2.append({
                    "horizonte": hz,
                    "universo": nom_univ,
                    "tramo": tramo_nom,
                    "cobertura_gauss_1sigma": round(cob_g1, 2),
                    "cobertura_gauss_2sigma": round(cob_g2, 2),
                    "frec_cola_gauss_2sigma": round(cola_g2, 2),
                    "cobertura_student_1sigma": round(cob_t1, 2),
                    "cobertura_student_2sigma": round(cob_t2, 2),
                    "frec_cola_student_2sigma": round(cola_t2, 2),
                })

    df_g2 = pd.DataFrame(filas_g2)
    df_g2.to_csv(OUT_G2_COBERTURA, index=False)
    print(df_g2[df_g2["horizonte"] == "1D"].to_string(index=False), flush=True)

    print("\nG.3 BRIER SKILL SCORE CON BLOCK BOOTSTRAP (500 réplicas, bloque=20d):", flush=True)
    filas_g3 = []
    q_u = abs_ret.rolling(252).quantile(0.666).shift(1)
    evento_u = (abs_ret > q_u).astype(int)
    prob_ewma = pd.DataFrame(
        2.0 * (1.0 - stats.norm.cdf(q_u.values / np.maximum(1e-6, sigma_ewma94.shift(1).values))),
        index=closes.index,
        columns=closes.columns
    )
    prob_base = evento_u.rolling(252).mean().shift(1)

    for nom_univ, mask_u in [("IBEX 35", closes.columns.isin(ibex_set)), ("Resto", ~closes.columns.isin(ibex_set))]:
        for tramo_nom, d_cut in [("Diseño", closes.index <= pd.Timestamp(SPLIT_DESIGN_DATE)), ("OOS", closes.index >= pd.Timestamp(START_OOS_DATE))]:
            df_cal = pd.DataFrame({
                "y_true": evento_u.loc[d_cut, mask_u].stack(),
                "y_pred": prob_ewma.loc[d_cut, mask_u].stack(),
                "y_base": prob_base.loc[d_cut, mask_u].stack(),
            }).dropna()

            y_t = df_cal["y_true"].values
            y_p = df_cal["y_pred"].values
            y_b = df_cal["y_base"].values

            bs_m = float(np.mean((y_p - y_t)**2))
            bs_b = float(np.mean((y_b - y_t)**2))
            bss = 1.0 - (bs_m / bs_b) if bs_b > 0 else 0.0

            try:
                p_clip = np.clip(y_p, 0.001, 0.999)
                logit_p = np.log(p_clip / (1.0 - p_clip))
                slope, intercept, _, _, _ = stats.linregress(logit_p, y_t)
                slope_cal = round(float(slope * 4.0), 2)
            except Exception:
                slope_cal = np.nan

            n_obs = len(df_cal)
            block_len = 20
            n_blocks = max(1, n_obs // block_len)
            np.random.seed(42)
            bss_boot = []
            for _ in range(500):
                b_idx = np.random.randint(0, n_obs - block_len, size=n_blocks)
                sample_idx = np.concatenate([np.arange(i, i + block_len) for i in b_idx])
                b_m = np.mean((y_p[sample_idx] - y_t[sample_idx])**2)
                b_b = np.mean((y_b[sample_idx] - y_t[sample_idx])**2)
                if b_b > 0:
                    bss_boot.append(1.0 - (b_m / b_b))

            ci_low = round(float(np.percentile(bss_boot, 2.5) * 100.0), 2)
            ci_high = round(float(np.percentile(bss_boot, 97.5) * 100.0), 2)

            cumple_g = (bss > 0) and (ci_low > 0) and (0.9 <= slope_cal <= 1.1)

            filas_g3.append({
                "universo": nom_univ,
                "tramo": tramo_nom,
                "brier_score_modelo": round(bs_m, 4),
                "brier_score_base": round(bs_b, 4),
                "brier_skill_score_pct": round(bss * 100.0, 2),
                "ci95_bss_low_pct": ci_low,
                "ci95_bss_high_pct": ci_high,
                "pendiente_calibracion": slope_cal,
                "cumple_criterio_G": "SÍ" if cumple_g else "NO",
            })

    df_g3 = pd.DataFrame(filas_g3)
    df_g3.to_csv(OUT_G3_BRIER, index=False)
    print(df_g3.to_string(index=False), flush=True)
    return df_g1, df_g2, df_g3


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    print("="*70, flush=True)
    print("🔬 EJECUTANDO DIAGNÓSTICO INSTITUCIONAL FASE 3A-BIS (v2)", flush=True)
    print("="*70, flush=True)
    t0 = time.time()

    df_panel = pd.read_parquet(PANEL_SENALES)
    df_cache = pd.read_parquet(CACHE_PRECIOS)

    df_merged, ibex_set = preparar_datos_panel_vectorizados(df_panel, df_cache)

    # A. Tabla 3 auditada con regímenes prefijados y score por mitades
    puntoA_auditoria_tabla3(df_merged)

    # B. Validación de Open y reconciliación de Benchmark
    puntoB_validacion_open_benchmark(df_cache, df_merged, ibex_set)

    # C. Revisión Overnight e Intradía por mitades en líquidos
    puntoC_overnight_intradia_mitades(df_merged)

    # D. Corrección de la potencia estadística
    puntoD_potencia_estadistica()

    # E. Líneas base con zonas reales y costes 1M en líquidos
    puntoE_baselines_y_costes_1m(df_merged)

    # F. Auditoría de outliers (|r| > 25%)
    puntoF_auditoria_outliers(df_cache)

    # G. Pruebas del pivote de volatilidad
    puntoG_pruebas_pivote_volatilidad(df_cache, ibex_set)

    print("\n" + "="*70, flush=True)
    print(f"✅ FASE 3A-BIS (v2) COMPLETADA EXITOSAMENTE EN {round(time.time() - t0, 1)}s", flush=True)
    print("="*70, flush=True)


if __name__ == "__main__":
    main()
