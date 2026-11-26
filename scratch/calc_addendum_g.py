"""
Cálculo de G.1, G.2 y G.3 para el Addendum v3:
(a) Con observaciones originales (replicando auditoría v2)
(b) Con outliers (|r| > 25%) excluidos según regla documentada
(c) Medias winsorizadas y medianas
(d) Corrección de sesgos metodológicos:
    - G.2: excluir NaNs en lugar de tratarlos como fallos de cobertura; excluir ^IBEX de Resto
    - G.3: block bootstrap por bloques de fechas (20 días de mercado), no filas apiladas;
           pendiente de calibración por regresión logística exacta (IRLS con numpy).
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
from database.connection import get_connection

print("Cargando datos para G...", flush=True)
df_cache = pd.read_parquet(CACHE_PRECIOS)
closes = df_cache.xs("Close", level=1, axis=1)
highs = df_cache.xs("High", level=1, axis=1)
lows = df_cache.xs("Low", level=1, axis=1)

ret_daily = closes.pct_change()
abs_ret = ret_daily.abs()

conn = get_connection()
try:
    conn.row_factory = None
    ibex_set = set(r[0] for r in conn.execute("SELECT ticker FROM empresas WHERE es_ibex35 = 1").fetchall())
finally:
    conn.close()

SPLIT_DESIGN = pd.Timestamp("2025-03-31")
START_OOS = pd.Timestamp("2025-05-02")
HORIZONTE_BARS = {"1D": 1, "3D": 3, "1S": 5, "1M": 20}

# 1. Crear máscara de outliers (|r_diario| > 0.25)
mask_outlier_daily = (ret_daily.abs() > 0.25)
print(f"Total barras con |r_diario| > 25%: {mask_outlier_daily.sum().sum()}")

# Retornos diarios limpios
ret_daily_clean = ret_daily.copy()
ret_daily_clean[mask_outlier_daily] = np.nan
abs_ret_clean = ret_daily_clean.abs()

# Modelos de volatilidad (calculados sobre retornos brutos y limpios)
def get_vol_models(r_d, c, h, l):
    sig_base = r_d.rolling(252).std()
    sig_94 = np.sqrt((r_d**2).ewm(alpha=1.0 - 0.94, adjust=False).mean())
    sig_97 = np.sqrt((r_d**2).ewm(alpha=1.0 - 0.97, adjust=False).mean())
    cl_p = c.shift(1)
    tr = np.maximum(h - l, np.maximum((h - cl_p).abs(), (l - cl_p).abs()))
    sig_atr = (tr.rolling(14).mean() / c) / 1.6
    return sig_base, sig_94, sig_97, sig_atr

sig_base_raw, sig_94_raw, sig_97_raw, sig_atr_raw = get_vol_models(ret_daily, closes, highs, lows)
sig_base_cln, sig_94_cln, sig_97_cln, sig_atr_cln = get_vol_models(ret_daily_clean, closes, highs, lows)

ibex_tickers = [t for t in closes.columns if t in ibex_set and t != "^IBEX"]
resto_tickers = [t for t in closes.columns if t not in ibex_set and t != "^IBEX"]

# ═══════════════════════════════════════════════════════════════════
# G.1 WITHIN-TICKER EVALUATION (Raw vs Outliers Excluidos)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("G.1 WITHIN-TICKER (Raw vs Sin Outliers)")
print("="*60)

def eval_g1(models_dict, r_abs_df, label):
    filas = []
    base_m = models_dict["Baseline 252d"]
    for nom, df_sig in models_dict.items():
        ics = []
        qlike_diffs = []
        for tk in ibex_tickers:
            sig = df_sig[tk].shift(1).dropna()
            r_real = r_abs_df[tk].reindex(sig.index).dropna()
            idx = sig.index.intersection(r_real.index)
            if len(idx) < 100:
                continue
            rs, _ = stats.spearmanr(sig.loc[idx], r_real.loc[idx])
            ics.append(rs)

            sig_b = base_m[tk].shift(1).loc[idx]
            r2 = r_real.loc[idx]**2
            s2 = sig.loc[idx]**2
            sb2 = sig_b**2
            # QLIKE loss = ln(s^2) + r^2/s^2
            ql_m = np.log(s2) + (r2 / s2)
            ql_b = np.log(sb2) + (r2 / sb2)
            qlike_diffs.append(float(np.nanmean(ql_b - ql_m)))

        filas.append({
            "version": label,
            "estimador": nom,
            "n_tickers": len(ics),
            "mediana_spearman": round(float(np.median(ics)), 4),
            "media_spearman": round(float(np.mean(ics)), 4),
            "pct_pos": round(float((np.array(ics) > 0).mean() * 100.0), 1),
            "qlike_mejora_vs_base": round(float(np.mean(qlike_diffs)), 4)
        })
    return pd.DataFrame(filas)

df_g1_raw = eval_g1({
    "Baseline 252d": sig_base_raw, "EWMA 0.94": sig_94_raw, "EWMA 0.97": sig_97_raw, "ATR / 1.6": sig_atr_raw
}, abs_ret, "Con Outliers (v2)")

df_g1_cln = eval_g1({
    "Baseline 252d": sig_base_cln, "EWMA 0.94": sig_94_cln, "EWMA 0.97": sig_97_cln, "ATR / 1.6": sig_atr_cln
}, abs_ret_clean, "Sin Outliers (|r|>25%)")

df_g1_all = pd.concat([df_g1_raw, df_g1_cln], ignore_index=True)
print(df_g1_all.to_string(index=False))
df_g1_all.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_pG1_volatilidad_within.csv"), index=False)

# ═══════════════════════════════════════════════════════════════════
# G.2 COBERTURAS EMPÍRICAS (CORREGIDO: Sin NaNs, Sin ^IBEX en Resto)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("G.2 COBERTURAS EMPÍRICAS CORREGIDAS")
print("="*60)

def run_coberturas(r_df, sig_df, label):
    # Grados de libertad nu ajustados solo en diseño en IBEX
    r_dis = r_df.loc[:SPLIT_DESIGN, ibex_tickers].dropna()
    res_std = (r_dis / sig_df.loc[:SPLIT_DESIGN, ibex_tickers]).dropna().values.flatten()
    nu_dis = round(float(stats.t.fit(res_std)[0]), 2)

    crit_1s_t = stats.t.ppf(1.0 - (1.0 - 0.6827)/2.0, df=nu_dis)
    crit_2s_t = stats.t.ppf(1.0 - (1.0 - 0.9545)/2.0, df=nu_dis)

    filas = []
    for hz, h in HORIZONTE_BARS.items():
        ret_h = closes.pct_change(h)
        if label == "Sin Outliers":
            # Mask if any daily return in the holding window was > 25%
            # Or if ret_h itself is contaminated
            mask_out_h = mask_outlier_daily.rolling(h).max() > 0
            ret_h = ret_h.copy()
            ret_h[mask_out_h] = np.nan

        for nom_u, tk_list in [("IBEX 35", ibex_tickers), ("Resto (sin ^IBEX)", resto_tickers)]:
            for tramo, d_cut in [("Diseño", closes.index <= SPLIT_DESIGN), ("OOS", closes.index >= START_OOS)]:
                r_sub = ret_h.loc[d_cut, tk_list]
                s_sub = sig_df.loc[d_cut, tk_list].shift(h) * np.sqrt(h)

                # CORRECCIÓN VITAL: Alinear y evaluar solo donde AMBOS son válidos (sin contar NaN como fallo)
                valid = r_sub.notna() & s_sub.notna()
                r_v = r_sub.values[valid.values]
                s_v = s_sub.values[valid.values]

                n_obs = len(r_v)
                if n_obs == 0:
                    continue

                cob_g1 = float((np.abs(r_v) <= s_v).mean() * 100.0)
                cob_g2 = float((np.abs(r_v) <= 2.0 * s_v).mean() * 100.0)
                cola_g2 = 100.0 - cob_g2

                cob_t1 = float((np.abs(r_v) <= crit_1s_t * s_v).mean() * 100.0)
                cob_t2 = float((np.abs(r_v) <= crit_2s_t * s_v).mean() * 100.0)
                cola_t2 = 100.0 - cob_t2

                filas.append({
                    "version": label,
                    "horizonte": hz,
                    "universo": nom_u,
                    "tramo": tramo,
                    "n_obs_validas": n_obs,
                    "nu_diseno": nu_dis,
                    "cob_gauss_1sigma": round(cob_g1, 2),
                    "cob_gauss_2sigma": round(cob_g2, 2),
                    "frec_cola_gauss_2s": round(cola_g2, 2),
                    "cob_student_1sigma": round(cob_t1, 2),
                    "cob_student_2sigma": round(cob_t2, 2),
                    "frec_cola_student_2s": round(cola_t2, 2),
                })
    return pd.DataFrame(filas)

df_g2_raw = run_coberturas(ret_daily, sig_94_raw, "Con Outliers (v2 corregido)")
df_g2_cln = run_coberturas(ret_daily_clean, sig_94_cln, "Sin Outliers")
df_g2_all = pd.concat([df_g2_raw, df_g2_cln], ignore_index=True)
print(df_g2_all[df_g2_all["horizonte"] == "1D"].to_string(index=False))
df_g2_all.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_pG2_cobertura.csv"), index=False)

# ═══════════════════════════════════════════════════════════════════
# G.3 BRIER SKILL SCORE Y CALIBRACIÓN (LOGÍSTICA IRLS Y DATE-BLOCK BOOTSTRAP)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("G.3 BRIER SKILL SCORE (Date-Block Bootstrap & Regresión Logística)")
print("="*60)

def logistic_fit_irls(x, y, max_iter=25, tol=1e-6):
    """Ajusta regresión logística binaria y = sigmoid(alpha + beta * x) usando IRLS exacto."""
    N = len(y)
    X = np.column_stack([np.ones(N), x])
    # Init beta
    beta = np.zeros(2)
    for _ in range(max_iter):
        eta = X @ beta
        eta = np.clip(eta, -20.0, 20.0)
        p = 1.0 / (1.0 + np.exp(-eta))
        p = np.clip(p, 1e-6, 1.0 - 1e-6)
        W = p * (1.0 - p)
        z = eta + (y - p) / W
        # WLS: (X^T W X)^-1 X^T W z
        XW = X.T * W
        A = XW @ X
        b = XW @ z
        try:
            beta_new = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            break
        if np.max(np.abs(beta_new - beta)) < tol:
            beta = beta_new
            break
        beta = beta_new
    return beta[0], beta[1]  # intercept, slope

def eval_g3(abs_r_df, sig_df, label):
    # Umbral de evento: percentil 66.6% móvil 252d de retornos absolutos
    q_u = abs_r_df.rolling(252).quantile(0.666).shift(1)
    evento_u = (abs_r_df > q_u).astype(float)
    # Marcar NaN donde no haya datos
    evento_u[abs_r_df.isna() | q_u.isna()] = np.nan

    prob_ewma = pd.DataFrame(
        2.0 * (1.0 - stats.norm.cdf(q_u.values / np.maximum(1e-6, sig_df.shift(1).values))),
        index=closes.index,
        columns=closes.columns
    )
    prob_base = evento_u.rolling(252).mean().shift(1)

    filas = []
    for nom_u, tk_list in [("IBEX 35", ibex_tickers), ("Resto (sin ^IBEX)", resto_tickers)]:
        for tramo, d_cut in [("Diseño", closes.index <= SPLIT_DESIGN), ("OOS", closes.index >= START_OOS)]:
            ev_sub = evento_u.loc[d_cut, tk_list]
            pr_sub = prob_ewma.loc[d_cut, tk_list]
            pb_sub = prob_base.loc[d_cut, tk_list]

            # Fechas comunes
            valid_dates = ev_sub.dropna(how="all").index.intersection(pr_sub.dropna(how="all").index).intersection(pb_sub.dropna(how="all").index)
            
            # Stack data
            df_stacked = pd.DataFrame({
                "date": np.repeat(valid_dates, len(tk_list)),
                "y_true": ev_sub.loc[valid_dates].values.flatten(),
                "y_pred": pr_sub.loc[valid_dates].values.flatten(),
                "y_base": pb_sub.loc[valid_dates].values.flatten(),
            }).dropna()

            y_t = df_stacked["y_true"].values
            y_p = df_stacked["y_pred"].values
            y_b = df_stacked["y_base"].values

            bs_m = float(np.mean((y_p - y_t)**2))
            bs_b = float(np.mean((y_b - y_t)**2))
            bss = 1.0 - (bs_m / bs_b) if bs_b > 0 else 0.0

            # Pendiente de calibración logística exacta
            p_clip = np.clip(y_p, 1e-4, 1.0 - 1e-4)
            logit_p = np.log(p_clip / (1.0 - p_clip))
            _, slope_logistic = logistic_fit_irls(logit_p, y_t)

            # Block bootstrap por BLOQUES DE FECHAS (20 fechas consecutivas de mercado)
            unique_dates = np.array(valid_dates)
            n_d = len(unique_dates)
            block_len = 20
            n_blocks = max(1, n_d // block_len)

            # Pre-agrupar errores cuadráticos por fecha
            df_stacked["se_m"] = (y_p - y_t)**2
            df_stacked["se_b"] = (y_b - y_t)**2
            daily_err = df_stacked.groupby("date")[["se_m", "se_b"]].mean().loc[unique_dates]
            err_m_d = daily_err["se_m"].values
            err_b_d = daily_err["se_b"].values

            np.random.seed(42)
            bss_boot = []
            for _ in range(500):
                starts = np.random.randint(0, n_d - block_len, size=n_blocks)
                idx_b = np.concatenate([np.arange(s, s + block_len) for s in starts])
                b_m = np.mean(err_m_d[idx_b])
                b_b = np.mean(err_b_d[idx_b])
                if b_b > 0:
                    bss_boot.append(1.0 - (b_m / b_b))

            ci_low = float(np.percentile(bss_boot, 2.5) * 100.0)
            ci_high = float(np.percentile(bss_boot, 97.5) * 100.0)

            # Criterio G: bss > 0, ci_low > 0, 0.9 <= slope <= 1.1
            cumple_g = (bss > 0) and (ci_low > 0) and (0.9 <= slope_logistic <= 1.1)

            filas.append({
                "version": label,
                "universo": nom_u,
                "tramo": tramo,
                "n_obs": len(y_t),
                "n_fechas": n_d,
                "brier_score_modelo": round(bs_m, 4),
                "brier_score_base": round(bs_b, 4),
                "bss_pct": round(bss * 100.0, 2),
                "ci95_low_bss_pct": round(ci_low, 2),
                "ci95_high_bss_pct": round(ci_high, 2),
                "pendiente_calibracion_logistica": round(float(slope_logistic), 2),
                "cumple_criterio_G": "SÍ" if cumple_g else "NO"
            })
    return pd.DataFrame(filas)

df_g3_raw = eval_g3(abs_ret, sig_94_raw, "Con Outliers (v2 corregido)")
df_g3_cln = eval_g3(abs_ret_clean, sig_94_cln, "Sin Outliers")
df_g3_all = pd.concat([df_g3_raw, df_g3_cln], ignore_index=True)
print(df_g3_all.to_string(index=False))
df_g3_all.to_csv(os.path.join(BASE_DIR, "data", "addendum_v3_pG3_brier_calibracion.csv"), index=False)

print("\nCálculos de G.1, G.2 y G.3 completados exitosamente.")
