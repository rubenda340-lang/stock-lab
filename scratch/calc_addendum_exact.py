"""
Script de cálculo riguroso para el Addendum v3.
Obtiene los números reales de los puntos 1 a 8 sin modelos nuevos.
"""

import os
import sys
import numpy as np
import pandas as pd
from scipy import stats

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
CACHE_PRECIOS = os.path.join(BASE_DIR, "data", "backtest_cache_6y.parquet")
PANEL_SENALES = os.path.join(BASE_DIR, "data", "backtest_signals_panel.parquet")

from database.connection import get_connection

def newey_west(series: pd.Series, lag: int = 0):
    s = series.dropna().values
    n = len(s)
    if n < 5:
        return 0.0, 0.0, 1.0, 0.0
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
    se_nw = float(np.sqrt(max(1e-12, v_nw) / n))
    t_stat = mean_val / se_nw
    p_val = float(2.0 * stats.t.sf(abs(t_stat), df=n - 1))
    return mean_val, float(t_stat), p_val, se_nw

def calc_daily_spearman(df_sub, col_x, col_y, min_obs=6):
    v = df_sub[[col_x, col_y, "date"]].dropna()
    if len(v) < min_obs:
        return pd.Series(dtype=float)
    v["rx"] = v.groupby("date")[col_x].rank()
    v["ry"] = v.groupby("date")[col_y].rank()
    def _c(g):
        if len(g) < min_obs:
            return np.nan
        c = np.corrcoef(g["rx"].values, g["ry"].values)[0, 1]
        return float(c) if np.isfinite(c) else np.nan
    return v.groupby("date").apply(_c, include_groups=False).dropna()

print("Cargando datos...", flush=True)
df_panel = pd.read_parquet(PANEL_SENALES)
df_cache = pd.read_parquet(CACHE_PRECIOS)

closes = df_cache.xs("Close", level=1, axis=1)
opens = df_cache.xs("Open", level=1, axis=1)
highs = df_cache.xs("High", level=1, axis=1)
lows = df_cache.xs("Low", level=1, axis=1)
vols = df_cache.xs("Volume", level=1, axis=1)

ret_on_mat = (opens.shift(-1) - closes) / closes
ret_id_mat = (closes.shift(-1) - opens.shift(-1)) / opens.shift(-1)
ret_tot_mat = (closes.shift(-1) - closes) / closes
vol_eur_mat = (closes * vols).rolling(20).mean()

s_on = ret_on_mat.stack().rename("ret_on")
s_id = ret_id_mat.stack().rename("ret_id")
s_tot = ret_tot_mat.stack().rename("ret_tot")
s_veur = vol_eur_mat.stack().rename("vol_eur")

df_base_1d = pd.concat([s_on, s_id, s_tot, s_veur], axis=1).reset_index()
df_base_1d.columns = ["date", "ticker", "ret_on", "ret_id", "ret_tot", "vol_eur"]

df_merged = df_panel.merge(df_base_1d, on=["date", "ticker"], how="left")

conn = get_connection()
try:
    conn.row_factory = None
    ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
finally:
    conn.close()

df_merged["es_ibex"] = df_merged["ticker"].isin(ibex_set)

q2 = df_merged.groupby(["date", "horizon"])["vol_eur"].transform(lambda s: s.quantile(0.666))
q1 = df_merged.groupby(["date", "horizon"])["vol_eur"].transform(lambda s: s.quantile(0.333))
df_merged["es_tercil_alto"] = df_merged["vol_eur"] >= q2
df_merged["es_tercil_bajo"] = df_merged["vol_eur"] <= q1

SPLIT_DESIGN = pd.Timestamp("2025-03-31")
START_OOS = pd.Timestamp("2025-05-02")

print("\n=== PUNTO 1: SCORE INTRADÍA Y OVERNIGHT POR MITADES Y CONTRASTE ===")
res_p1 = []
for nom_univ, mask_u in [("IBEX 35", df_merged["es_ibex"]), ("Tercil ALTO", df_merged["es_tercil_alto"])]:
    for tramo, col_ret in [("Intradía", "ret_id"), ("Overnight", "ret_on")]:
        sub_1d = df_merged[mask_u & (df_merged["horizon"] == "1D")].copy()
        dis = sub_1d[sub_1d["date"] <= SPLIT_DESIGN]
        oos = sub_1d[sub_1d["date"] >= START_OOS]

        ic_dis = calc_daily_spearman(dis, "pct_raw", col_ret)
        ic_oos = calc_daily_spearman(oos, "pct_raw", col_ret)

        m_dis, t_dis, p_dis, se_dis = newey_west(ic_dis, lag=0)
        m_oos, t_oos, p_oos, se_oos = newey_west(ic_oos, lag=0)

        diff = m_dis - m_oos
        se_diff = np.sqrt(se_dis**2 + se_oos**2)
        z_contrast = diff / se_diff if se_diff > 0 else np.nan
        p_contrast = float(2.0 * stats.norm.sf(abs(z_contrast)))

        res_p1.append({
            "universo": nom_univ,
            "tramo": tramo,
            "ic_diseno": m_dis,
            "se_diseno": se_dis,
            "t_diseno": t_dis,
            "n_fechas_diseno": len(ic_dis),
            "ic_oos": m_oos,
            "se_oos": se_oos,
            "t_oos": t_oos,
            "n_fechas_oos": len(ic_oos),
            "diff_diseno_menos_oos": diff,
            "se_diff": se_diff,
            "z_contraste": z_contrast,
            "p_valor_contraste": p_contrast,
            "interpretacion": "Inconcluyente (no significativo)" if p_contrast > 0.05 else "Diferencia significativa"
        })

df_p1 = pd.DataFrame(res_p1)
print(df_p1.to_string(index=False))
df_p1.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_p1_oos_contrastes.csv"), index=False)

print("\n=== PUNTO 2: COMPROBACIÓN DE TABLA 3 Y t=3.62 ===")
# Exact t-stats and overlap
for row in res_p1[:2]:
    print(f"Univ: {row['universo']}, IC: {row['ic_diseno']:.6f}, SE: {row['se_diseno']:.6f}, t: {row['t_diseno']:.6f}")

# Overlap between IBEX 35 and Tercil Alto in design
sub_dis_1d = df_merged[(df_merged["date"] <= SPLIT_DESIGN) & (df_merged["horizon"] == "1D")]
tk_ibex = set(sub_dis_1d[sub_dis_1d["es_ibex"]]["ticker"].unique())
tk_t_alto = set(sub_dis_1d[sub_dis_1d["es_tercil_alto"]]["ticker"].unique())
inter_tk = tk_ibex.intersection(tk_t_alto)
print(f"Tickers únicos en diseño 1D: IBEX={len(tk_ibex)}, Tercil Alto={len(tk_t_alto)}, Intersección={len(inter_tk)}")

# Daily correlation between IC series of IBEX and Tercil Alto
ic_ibex_dis = calc_daily_spearman(sub_dis_1d[sub_dis_1d["es_ibex"]], "pct_raw", "ret_id")
ic_talto_dis = calc_daily_spearman(sub_dis_1d[sub_dis_1d["es_tercil_alto"]], "pct_raw", "ret_id")
idx_comun = ic_ibex_dis.index.intersection(ic_talto_dis.index)
corr_ic = np.corrcoef(ic_ibex_dis.loc[idx_comun], ic_talto_dis.loc[idx_comun])[0, 1]
print(f"Correlación entre series diarias de IC de IBEX 35 y Tercil Alto en diseño: {corr_ic:.4f}")

print("\n=== PUNTO 5: VALIDACIÓN DE OPEN (Open == Close previo) ===")
# Check fraction of Open == prev Close by liquidity group
cl_prev = closes.shift(1)
es_open_igual_cl_prev = (opens == cl_prev)

# Filter for IBEX, Tercil Alto, Tercil Medio, Tercil Bajo
# Using average daily liquidity
veur_med = vol_eur_mat.median()
q_low = veur_med.quantile(0.333)
q_high = veur_med.quantile(0.666)

tk_ibex_all = [t for t in closes.columns if t in ibex_set and t != "^IBEX"]
tk_talto_all = [t for t in closes.columns if t not in ["^IBEX"] and veur_med.get(t, 0) >= q_high]
tk_tmedio_all = [t for t in closes.columns if t not in ["^IBEX"] and q_low <= veur_med.get(t, 0) < q_high]
tk_tbajo_all = [t for t in closes.columns if t not in ["^IBEX"] and veur_med.get(t, 0) < q_low]

def pct_eq(cols):
    v = es_open_igual_cl_prev[cols].dropna(how="all")
    tot = v.count().sum()
    eq = v.sum().sum()
    return eq, tot, eq / tot if tot > 0 else 0

eq_ib, tot_ib, p_ib = pct_eq(tk_ibex_all)
eq_ta, tot_ta, p_ta = pct_eq(tk_talto_all)
eq_tm, tot_tm, p_tm = pct_eq(tk_tmedio_all)
eq_tb, tot_tb, p_tb = pct_eq(tk_tbajo_all)

print(f"Open == Close previo:")
print(f"  IBEX 35: {eq_ib}/{tot_ib} ({p_ib*100:.2f}%)")
print(f"  Tercil Alto: {eq_ta}/{tot_ta} ({p_ta*100:.2f}%)")
print(f"  Tercil Medio: {eq_tm}/{tot_tm} ({p_tm*100:.2f}%)")
print(f"  Tercil Bajo: {eq_tb}/{tot_tb} ({p_tb*100:.2f}%)")
if "^IBEX" in closes.columns:
    eq_idx, tot_idx, p_idx = pct_eq(["^IBEX"])
    print(f"  ^IBEX (Índice): {eq_idx}/{tot_idx} ({p_idx*100:.2f}%)")

print("\nScript preliminar completado exitosamente.")
