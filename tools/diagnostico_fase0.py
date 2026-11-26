"""
Fase 0 — Diagnóstico de solo lectura del historial de predicciones.

Uso:
    python tools/diagnostico_fase0.py                 # usa ./stocklab.db
    python tools/diagnostico_fase0.py ruta/otra.db    # cualquier copia de la BD

Abre la BD en modo solo lectura (URI mode=ro). No modifica nada.

Tablas que produce (sobre resueltas con motivo 'vencida', por horizonte):
  (a) Línea base: % de resultados reales por zona (sube / lateral / baja)
      con los umbrales fijos vigentes (±0.8 / ±1.2 / ±1.8 / ±3.0 %).
  (b) Acierto por señal y horizonte.
  (c) Resoluciones tardías (fecha_resolucion > fecha_objetivo) e invalidadas.
  (d) IC de Spearman entre score_motor y variacion_real.
"""

import os
import sqlite3
import sys

import pandas as pd

UMBRALES = {"1D": 0.8, "3D": 1.2, "1S": 1.8, "1M": 3.0}


def main(db_path: str) -> None:
    if not os.path.exists(db_path):
        print(f"No existe: {db_path}")
        return
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    pd.set_option("display.width", 200)

    n_total = conn.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='predicciones'").fetchone()
    print(f"BD: {db_path}")
    print(f"Filas en predicciones: {n_total} | ids asignados históricamente (sqlite_sequence): {seq[0] if seq else 'n/d'}")
    print(pd.read_sql(
        "SELECT modelo, resuelta, COALESCE(motivo_resolucion,'-') motivo, COUNT(*) n "
        "FROM predicciones GROUP BY 1,2,3 ORDER BY 1,2,3", conn).to_string(index=False))

    df = pd.read_sql(
        "SELECT * FROM predicciones WHERE resuelta = 1", conn)
    if df.empty:
        print("\nNo hay predicciones resueltas: las tablas (a)-(d) no se pueden calcular.")
        return

    venc = df[df["motivo_resolucion"].fillna("vencida") == "vencida"].copy()
    venc["u"] = venc["horizonte"].map(UMBRALES)
    v = venc["variacion_real"]

    print("\n(a) LÍNEA BASE — % de resultados reales por zona (vencidas)")
    a = venc.assign(sube=v > venc["u"], lateral=v.abs() <= venc["u"], baja=v < -venc["u"]) \
        .groupby("horizonte").agg(n=("id", "size"), pct_sube=("sube", "mean"),
                                  pct_lateral=("lateral", "mean"), pct_baja=("baja", "mean"))
    print((a.assign(**{c: (a[c] * 100).round(1) for c in ["pct_sube", "pct_lateral", "pct_baja"]})).to_string())

    print("\n(b) ACIERTO POR SEÑAL Y HORIZONTE (vencidas)")
    b = venc.groupby(["horizonte", "senal"]).agg(n=("id", "size"), acierto=("acierto", "mean"))
    b["acierto"] = (b["acierto"] * 100).round(1)
    print(b.to_string())
    tot = venc["acierto"].mean() * 100
    print(f"Global vencidas: n={len(venc)}  acierto={tot:.1f}%")

    print("\n(c) RESOLUCIÓN TARDÍA E INVALIDADAS (todas las resueltas)")
    c = df.assign(
        tarde=(df["motivo_resolucion"].fillna("vencida") == "vencida") & (df["fecha_resolucion"] > df["fecha_objetivo"]),
        invalidada=df["motivo_resolucion"] == "invalidada",
    ).groupby("horizonte").agg(n=("id", "size"), tarde=("tarde", "sum"), invalidadas=("invalidada", "sum"))
    print(c.to_string())

    print("\n(d) IC SPEARMAN score_motor vs variacion_real (vencidas)")
    for hz, g in venc.groupby("horizonte"):
        g = g.dropna(subset=["score_motor", "variacion_real"])
        ic = g["score_motor"].corr(g["variacion_real"], method="spearman") if len(g) > 2 else float("nan")
        print(f"  {hz}: n={len(g):4d}  IC={ic:+.3f}")


if __name__ == "__main__":
    ruta = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stocklab.db")
    main(ruta)
