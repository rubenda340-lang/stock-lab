"""
Cálculos para Puntos 3, 4, 7 y 8 del Addendum v3.
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
df_panel = pd.read_parquet(PANEL_SENALES)
df_cache = pd.read_parquet(CACHE_PRECIOS)

closes = df_cache.xs("Close", level=1, axis=1)
opens = df_cache.xs("Open", level=1, axis=1)
highs = df_cache.xs("High", level=1, axis=1)
lows = df_cache.xs("Low", level=1, axis=1)
vols = df_cache.xs("Volume", level=1, axis=1)

SPLIT_DESIGN = pd.Timestamp("2025-03-31")
START_OOS = pd.Timestamp("2025-05-02")

ret_daily = closes.pct_change()

conn = get_connection()
try:
    conn.row_factory = None
    ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
finally:
    conn.close()

# ═══════════════════════════════════════════════════════════════════
# PUNTO 3: AUDITORÍA DE OUTLIERS (|r| > 25%) Y DECILES 1M
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("PUNTO 3: OUTLIERS Y DECILES 1M")
print("="*60)

# Detectar los 78 outliers
s_stacked = ret_daily.stack().dropna()
s_out = s_stacked[s_stacked.abs() > 0.25]
outlier_pairs = set(s_out.index)
print(f"Total barras con |r_diario| > 25%: {len(outlier_pairs)}")

# Comprobar en qué análisis de la v2 entraron los outliers:
# 1. Deciles 1M: v2 usó fwd_ret_open1 en sub_1m sin filtrar outliers -> SÍ INCLUYE
# 2. Coberturas (G.2): v2 calculó closes.pct_change(h) directamente de df_cache sin filtrar -> SÍ INCLUYE
# 3. Volatilidad Within-ticker (G.1): v2 usó abs_ret sin filtrar -> SÍ INCLUYE
# 4. G.3 (Brier): v2 usó abs_ret sin filtrar -> SÍ INCLUYE

# Análisis 1M en Líquidos:
# Apertura(t+1) -> Cierre(t+20)
cal = closes.index
dates_idx = {d: i for i, d in enumerate(cal)}
nb = 20

# Vectorización de 1M
p_in = opens.shift(-1)
p_out = closes.shift(-nb)
r_1m = (p_out - p_in) / p_in

# Vol_eur
vol_eur = (closes * vols).rolling(20).mean()

# Panel 1M
sub_1m = df_panel[df_panel["horizon"] == "1M"].copy()
# Merge p_in, p_out, r_1m, vol_eur
s_r1m = r_1m.stack().rename("fwd_ret_open1")
s_veur = vol_eur.stack().rename("vol_eur")
df_fwd_1m = pd.concat([s_r1m, s_veur], axis=1).reset_index()
df_fwd_1m.columns = ["date", "ticker", "fwd_ret_open1", "vol_eur"]

sub_1m = sub_1m.merge(df_fwd_1m, on=["date", "ticker"], how="left")
sub_1m["es_ibex"] = sub_1m["ticker"].isin(ibex_set)
q2_1m = sub_1m.groupby("date")["vol_eur"].transform(lambda s: s.quantile(0.666))
sub_1m["es_tercil_alto"] = sub_1m["vol_eur"] >= q2_1m

# Líquidos
liq_1m = sub_1m[(sub_1m["es_ibex"] | sub_1m["es_tercil_alto"]) & sub_1m["fwd_ret_open1"].notna()].copy()

# Comprobar si hay barras con |r_diario| > 25% dentro de la ventana de 20 días o en el retorno 1M
# Una regla documentada de exclusión de outliers:
# Excluir observaciones de 1M donde durante el holding period [t+1, t+20] o en t ocurrió un |r_diario| > 25%
# O bien donde |fwd_ret_open1| > 1.0 (100%) o retorno diario > 25%.
def has_daily_outlier_in_window(tk, t):
    idx = dates_idx.get(t)
    if idx is None or idx + nb >= len(cal):
        return False
    window_dates = cal[idx : idx + nb + 1]
    rets = ret_daily.loc[window_dates, tk]
    return (rets.abs() > 0.25).any()

print("Etiquetando outliers en ventana 1M...", flush=True)
liq_1m["has_outlier"] = [has_daily_outlier_in_window(tk, t) for tk, t in zip(liq_1m["ticker"], liq_1m["date"])]
print(f"Observaciones 1M líquidos con outlier en ventana: {liq_1m['has_outlier'].sum()} de {len(liq_1m)}")

# Deciles agrupados en pooled (como en v2) vs cross-sectional (por fecha)
# En v2: pd.qcut(liq_1m["pct_raw"].rank(method="first"), q=10)
liq_1m["decil_pooled"] = pd.qcut(liq_1m["pct_raw"].rank(method="first"), q=10, labels=False) + 1
# Cross-sectional deciles per date:
def get_cs_deciles(g):
    if len(g) < 10:
        return pd.Series(np.nan, index=g.index)
    return pd.qcut(g["pct_raw"].rank(method="first"), q=10, labels=False) + 1
liq_1m["decil_cs"] = liq_1m.groupby("date", group_keys=False).apply(get_cs_deciles)

print("\n--- A. Deciles 1M Pooled (como v2) ---")
res_pooled = []
for d in range(1, 11):
    sd = liq_1m[liq_1m["decil_pooled"] == d]
    r = sd["fwd_ret_open1"]
    r_sin = sd[~sd["has_outlier"]]["fwd_ret_open1"]
    
    # Winsorized 1% (both tails)
    p1, p99 = r.quantile(0.01), r.quantile(0.99)
    r_win = r.clip(p1, p99)

    res_pooled.append({
        "decil": d,
        "n_original": len(r),
        "media_original_pct": r.mean() * 100,
        "n_sin_outliers": len(r_sin),
        "media_sin_outliers_pct": r_sin.mean() * 100,
        "media_winsorizada_1pct": r_win.mean() * 100,
        "mediana_pct": r.median() * 100,
    })
df_dec_pooled = pd.DataFrame(res_pooled)
print(df_dec_pooled.to_string(index=False))

print("\n--- B. Deciles 1M Cross-Sectional (por fecha) ---")
res_cs = []
for d in range(1, 11):
    sd = liq_1m[liq_1m["decil_cs"] == d]
    r = sd["fwd_ret_open1"]
    r_sin = sd[~sd["has_outlier"]]["fwd_ret_open1"]
    p1, p99 = r.quantile(0.01), r.quantile(0.99)
    r_win = r.clip(p1, p99)

    res_cs.append({
        "decil": d,
        "n_original": len(r),
        "media_original_pct": r.mean() * 100,
        "n_sin_outliers": len(r_sin),
        "media_sin_outliers_pct": r_sin.mean() * 100,
        "media_winsorizada_1pct": r_win.mean() * 100,
        "mediana_pct": r.median() * 100,
    })
df_dec_cs = pd.DataFrame(res_cs)
print(df_dec_cs.to_string(index=False))

# Top 10 observaciones que más contribuyen a la media del Decil 1 (pooled)
d1_all = liq_1m[liq_1m["decil_pooled"] == 1].copy()
d1_all["contribucion_pct"] = (d1_all["fwd_ret_open1"] / len(d1_all)) * 100
top10_d1 = d1_all.sort_values(by="fwd_ret_open1", ascending=False).head(10)
print("\nTop 10 mayores retornos en Decil 1 1M:")
print(top10_d1[["date", "ticker", "pct_raw", "fwd_ret_open1", "contribucion_pct"]].to_string(index=False))

top10_d1[["date", "ticker", "pct_raw", "fwd_ret_open1", "contribucion_pct"]].to_csv(
    os.path.join(BASE_DIR, "data", "addendum_v3_p3_top10_decil1_1m.csv"), index=False
)
df_dec_pooled.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_p3_deciles_1m_pooled.csv"), index=False)
df_dec_cs.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_p3_deciles_1m_crosssectional.csv"), index=False)

# ═══════════════════════════════════════════════════════════════════
# PUNTO 4: RECONCILIACIÓN BENCHMARK (EQUIPONDERADO VS ^IBEX)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("PUNTO 4: RECONCILIACIÓN DE BENCHMARK")
print("="*60)
ibex_tickers = [t for t in closes.columns if t in ibex_set and t != "^IBEX"]
c_ib = closes[ibex_tickers]
o_ib = opens[ibex_tickers]

r_on_ibex = ((opens["^IBEX"].iloc[1:].values - closes["^IBEX"].iloc[:-1].values) / closes["^IBEX"].iloc[:-1].values)
r_id_ibex = ((closes["^IBEX"].iloc[1:].values - opens["^IBEX"].iloc[1:].values) / opens["^IBEX"].iloc[1:].values)
r_tot_ibex = ((closes["^IBEX"].iloc[1:].values - closes["^IBEX"].iloc[:-1].values) / closes["^IBEX"].iloc[:-1].values)

r_on_const = ((o_ib.iloc[1:].values - c_ib.iloc[:-1].values) / c_ib.iloc[:-1].values)
r_id_const = ((c_ib.iloc[1:].values - o_ib.iloc[1:].values) / o_ib.iloc[1:].values)
r_tot_const = ((c_ib.iloc[1:].values - c_ib.iloc[:-1].values) / c_ib.iloc[:-1].values)

# Daily equal weighted return series:
ew_on_daily = pd.Series(np.nanmean(r_on_const, axis=1), index=c_ib.index[1:])
ew_id_daily = pd.Series(np.nanmean(r_id_const, axis=1), index=c_ib.index[1:])
ew_tot_daily = pd.Series(np.nanmean(r_tot_const, axis=1), index=c_ib.index[1:])

print(f"^IBEX Oficial: ON={np.nanmean(r_on_ibex)*100:.3f}%, ID={np.nanmean(r_id_ibex)*100:.3f}%, TOT={np.nanmean(r_tot_ibex)*100:.3f}%")
print(f"IBEX Constituyentes EW: ON={ew_on_daily.mean()*100:.3f}%, ID={ew_id_daily.mean()*100:.3f}%, TOT={ew_tot_daily.mean()*100:.3f}%")

df_bench_rec = pd.DataFrame([
    {"serie": "^IBEX (Índice oficial precio)", "overnight_mean_pct": np.nanmean(r_on_ibex)*100, "intraday_mean_pct": np.nanmean(r_id_ibex)*100, "total_mean_pct": np.nanmean(r_tot_ibex)*100, "tipo": "Price Return"},
    {"serie": "IBEX 35 Constituyentes (Equiponderado)", "overnight_mean_pct": ew_on_daily.mean()*100, "intraday_mean_pct": ew_id_daily.mean()*100, "total_mean_pct": ew_tot_daily.mean()*100, "tipo": "Total Return (auto_adjust)"}
])
df_bench_rec.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_p4_benchmark_reconciliacion.csv"), index=False)

# ═══════════════════════════════════════════════════════════════════
# PUNTO 7: LÍNEAS BASE, ACIERTO DE SIGNO VS P(SUBE|R!=0) Y AZAR ESTRATIFICADO
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("PUNTO 7: LÍNEAS BASE Y ACIERTO DE SIGNO CONDICIONADO")
print("="*60)

ZONAS = {"1D": 0.008, "3D": 0.012, "1S": 0.018, "1M": 0.030}
filas_p7 = []

# Volatility bins: quantiles of vol_d within panel
vol_quantiles = df_panel.groupby("horizon")["vol_d"].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False))
df_panel["vol_bin"] = vol_quantiles

for hz, umbral_z in ZONAS.items():
    sub_h = df_panel[df_panel["horizon"] == hz].copy()
    for tramo, d_cut in [("Diseño", sub_h["date"] <= SPLIT_DESIGN), ("OOS", sub_h["date"] >= START_OOS)]:
        dh = sub_h[d_cut].copy()
        ret = dh["fwd_ret"]
        n_tot = len(dh)

        # Retornos no nulos
        mask_nonzero = (ret != 0)
        p_sube_cond = float((ret[mask_nonzero] > 0).mean() * 100.0)

        # Solo señales direccionales del motor
        mask_dir = (dh["senal"].isin(["ALCISTA", "BAJISTA"])) & mask_nonzero
        dh_dir = dh[mask_dir]
        hit_sign_dir = float(((dh_dir["senal"] == "ALCISTA") & (dh_dir["fwd_ret"] > 0) |
                               (dh_dir["senal"] == "BAJISTA") & (dh_dir["fwd_ret"] < 0)).mean() * 100.0)

        # 3 Clases real: ALCISTA (> +u), BAJISTA (< -u), LATERAL ([-u, +u])
        y_real_3c = np.where(ret > umbral_z, "ALCISTA", np.where(ret < -umbral_z, "BAJISTA", "LATERAL"))
        hit_3c_motor = float((dh["senal"] == y_real_3c).mean() * 100.0)

        # Siempre Lateral
        base_lateral = float((y_real_3c == "LATERAL").mean() * 100.0)

        # Azar empírico global
        p_alc = (y_real_3c == "ALCISTA").mean()
        p_baj = (y_real_3c == "BAJISTA").mean()
        p_lat = (y_real_3c == "LATERAL").mean()
        f_alc = (dh["senal"] == "ALCISTA").mean()
        f_baj = (dh["senal"] == "BAJISTA").mean()
        f_lat = (dh["senal"] == "LATERAL").mean()
        azar_global = (p_alc * f_alc + p_baj * f_baj + p_lat * f_lat) * 100.0

        # Azar estratificado dentro de ticker x vol_bin (analítico exacto):
        # En cada grupo g=(ticker, vol_bin), p_g = frecs reales, f_g = frecs motor
        # Acierto esperado = sum_g N_g * (p_alc_g * f_alc_g + p_baj_g * f_baj_g + p_lat_g * f_lat_g) / N
        dh["y_real_3c"] = y_real_3c
        def azar_cell(g):
            n_g = len(g)
            if n_g == 0:
                return 0.0
            p_a = (g["y_real_3c"] == "ALCISTA").mean()
            p_b = (g["y_real_3c"] == "BAJISTA").mean()
            p_l = (g["y_real_3c"] == "LATERAL").mean()
            f_a = (g["senal"] == "ALCISTA").mean()
            f_b = (g["senal"] == "BAJISTA").mean()
            f_l = (g["senal"] == "LATERAL").mean()
            return n_g * (p_a * f_a + p_b * f_b + p_l * f_l)
        
        aciertos_azar_estrat = dh.groupby(["ticker", "vol_bin"]).apply(azar_cell).sum()
        azar_estratificado = (aciertos_azar_estrat / n_tot) * 100.0

        # Contar ceros exactos vs dentro de banda
        pct_ceros_exactos = float((ret == 0).mean() * 100.0)
        pct_dentro_banda = float(((ret >= -umbral_z) & (ret <= umbral_z)).mean() * 100.0)

        filas_p7.append({
            "horizonte": hz,
            "tramo": tramo,
            "n_obs": n_tot,
            "pct_ceros_exactos": pct_ceros_exactos,
            "pct_dentro_banda_lateral": pct_dentro_banda,
            "p_sube_dado_no_cero": p_sube_cond,
            "motor_hit_signo_dir": hit_sign_dir,
            "diff_hit_signo_vs_cond": hit_sign_dir - p_sube_cond,
            "base_siempre_lateral": base_lateral,
            "base_azar_global": azar_global,
            "base_azar_estratificado_ticker_vol": azar_estratificado,
            "motor_acierto_3clases": hit_3c_motor,
        })

df_p7 = pd.DataFrame(filas_p7)
print(df_p7.to_string(index=False))
df_p7.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_p7_baselines.csv"), index=False)

# ═══════════════════════════════════════════════════════════════════
# PUNTO 8: QLIKE CON IC Y EXPLICACIÓN DE ATR
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("PUNTO 8: QLIKE CON IC Y ANÁLISIS DE ATR")
print("="*60)

sigma_base252 = ret_daily.rolling(252).std()
sigma_ewma94 = np.sqrt((ret_daily**2).ewm(alpha=1.0 - 0.94, adjust=False).mean())
sigma_ewma97 = np.sqrt((ret_daily**2).ewm(alpha=1.0 - 0.97, adjust=False).mean())

cl_prev = closes.shift(1)
tr = np.maximum(highs - lows, np.maximum((highs - cl_prev).abs(), (lows - cl_prev).abs()))
sigma_atr = (tr.rolling(14).mean() / closes) / 1.6

estimadores = [
    ("Baseline 252d", sigma_base252),
    ("EWMA 0.94", sigma_ewma94),
    ("EWMA 0.97", sigma_ewma97),
    ("ATR / 1.6", sigma_atr),
]

# Diebold-Mariano test / Block Bootstrap de QLIKE en IBEX 35
# QLIKE loss por fecha y ticker: L(r^2, s^2) = ln(s^2) + r^2 / s^2
# Calculamos la serie de pérdida agregada diaria en IBEX 35
ibex_tickers = [t for t in closes.columns if t in ibex_set and t != "^IBEX"]
r2_ibex = (ret_daily[ibex_tickers]**2)

loss_series = {}
for nom, df_sig in estimadores:
    s2 = (df_sig[ibex_tickers].shift(1))**2
    loss_mat = np.log(s2) + (r2_ibex / s2)
    # Media diaria cross-sectional
    loss_series[nom] = loss_mat.mean(axis=1).dropna()

idx_valid = loss_series["Baseline 252d"].index
for nom in loss_series:
    idx_valid = idx_valid.intersection(loss_series[nom].index)

print(f"Fechas válidas para comparación QLIKE: {len(idx_valid)}")

filas_p8 = []
base_loss = loss_series["Baseline 252d"].loc[idx_valid]

np.random.seed(42)
n_dates = len(idx_valid)
block_len = 20
n_blocks = max(1, n_dates // block_len)

for nom, s_loss in loss_series.items():
    l_cur = s_loss.loc[idx_valid]
    diff_loss = base_loss - l_cur  # Positivo = modelo mejora a baseline
    mean_diff = diff_loss.mean()

    # Diebold-Mariano t-test usando Newey-West con lag 5
    m_dm, t_dm, p_dm, se_dm = newey_west(diff_loss, lag=5)

    # Block bootstrap del diferencial de pérdida
    boot_diffs = []
    diff_vals = diff_loss.values
    for _ in range(500):
        b_starts = np.random.randint(0, n_dates - block_len, size=n_blocks)
        boot_idx = np.concatenate([np.arange(b, b + block_len) for b in b_starts])
        boot_diffs.append(np.mean(diff_vals[boot_idx]))
    
    ci_low = float(np.percentile(boot_diffs, 2.5))
    ci_high = float(np.percentile(boot_diffs, 97.5))

    # Nivel medio de sesgo de escala: b = mean(r^2 / s^2)
    s2_cur = (estimadores[[x[0] for x in estimadores].index(nom)][1][ibex_tickers].shift(1))**2
    ratio_scale = (r2_ibex.loc[idx_valid] / s2_cur.loc[idx_valid]).mean().mean()

    filas_p8.append({
        "estimador": nom,
        "qlike_medio": round(float(l_cur.mean()), 4),
        "mejora_vs_base": round(float(mean_diff), 4),
        "se_mejora": round(float(se_dm), 4),
        "t_diebold_mariano": round(float(t_dm), 2),
        "p_val_dm": round(float(p_dm), 4),
        "ci95_low_boot": round(ci_low, 4),
        "ci95_high_boot": round(ci_high, 4),
        "scale_ratio_b": round(float(ratio_scale), 3),
        "penalizacion_escala_b_1_ln_b": round(float(ratio_scale - 1.0 - np.log(ratio_scale)), 4) if ratio_scale > 0 else np.nan
    })

df_p8 = pd.DataFrame(filas_p8)
print(df_p8.to_string(index=False))
df_p8.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_p8_qlike_bootstrap.csv"), index=False)

print("\nCálculos de Puntos 3, 4, 7 y 8 completados con éxito.")
