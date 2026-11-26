"""
Tests unitarios para el Paso C-27: Elegibilidad del universo contra valores zombi e iliquidos
=============================================================================================
Verifica sobre base de datos SQLite temporal sintetica y sin red, con el reloj
parcheado en utils.market_calendar, services.emision y database.predicciones:
  (a) Pseudo-barra en sesion S (Close = 10.0, Volume = 0 en S, aunque las 59 sesiones
      previas tengan volumen alto) es rechazada con motivo "sin_volumen_en_sesion"
      y NO se registra en predicciones.
  (b) Precio inferior al minimo en S (Close = 0.04 < 0.05, con volumen alto) es
      rechazado con "precio_bajo_minimo".
  (c) Exceso de dias sin volumen en 20 sesiones (6 dias con Volume = 0 en las ultimas
      20 sesiones, aunque en S tenga volumen y mediano >= 5000) es rechazado con
      "exceso_dias_sin_volumen_20s".
  (d) Exceso de dias sin volumen en 60 sesiones (11 dias con Volume = 0 entre las
      sesiones -60 y -21 y 0 en las ultimas 20) es rechazado con
      "exceso_dias_sin_volumen_60s".
  (e) Efectivo mediano 20s insuficiente (Close = 2.0, Volume = 1000 -> 2000 EUR < 5000 EUR)
      es rechazado con "efectivo_mediano_insuficiente", mientras que con 5000 EUR
      exactos es aceptado ("ok").
  (f) Coherencia entre camino completo (success) y segunda ejecucion (already_emitted):
      si un universo tiene 2 tickers liquidos y 2 tickers zombi/iliquidos, la primera
      ejecucion emite solo los 2 liquidos (8 filas) y la segunda ejecucion devuelve
      already_emitted por el camino rapido sin recalcular el radar ni intentar emitir
      los 2 zombis.
  (g) Comprobacion en solo lectura contra data/backtest_cache_6y.parquet (en la sesion
      '2026-10-27'): verifica que evaluar_elegibilidad_ticker excluye exactamente los
      11 tickers identificados en el Paso 0d del Perfil 1 (['ARM.MC', 'CEV.MC',
      'DESA.MC', 'ECR.MC', 'GAM.MC', 'LIB.MC', 'MTB.MC', 'NYE.MC', 'TRG.MC',
      'UBS.MC', 'VID.MC'], con VID.MC por "sin_barra_fresca" en esa fecha y los
      zombis por sus motivos respectivos) y acepta los 105 restantes.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database.connection
from database import predicciones as db_pred
from database.connection import get_connection
import services.emision as se
import utils.market_calendar as mc

MADRID_TZ = ZoneInfo("Europe/Madrid")
PARQUET_PATH = os.path.join(BASE_DIR, "data", "backtest_cache_6y.parquet")
REAL_DB_PATH = os.path.join(BASE_DIR, "stocklab.db")


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Parchea get_ahora_madrid en los tres espacios de nombres requeridos."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _crear_batch_60_sesiones(
    tickers,
    fecha_sesion: str = "2026-10-15",
    n_sesiones: int = 65,
    close_default: float = 10.0,
    vol_default: float = 10000.0,
) -> pd.DataFrame:
    """
    Construye un DataFrame MultiIndex (ticker, campo) con n_sesiones habiles
    terminando en fecha_sesion, con valores por defecto liquidos.
    """
    end_dt = pd.to_datetime(fecha_sesion)
    fechas = pd.bdate_range(end=end_dt, periods=n_sesiones)
    cols = pd.MultiIndex.from_product(
        [list(tickers), ["Open", "High", "Low", "Close", "Volume"]],
        names=["Ticker", "Price"],
    )
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)
    for idx_i, f_dt in enumerate(fechas):
        drift = 1.0 + 0.001 * (idx_i % 7)
        for t in tickers:
            p = close_default * drift if t == "^IBEX" else close_default
            df.loc[f_dt, (t, "Open")] = p
            df.loc[f_dt, (t, "High")] = p * 1.01
            df.loc[f_dt, (t, "Low")] = p * 0.99
            df.loc[f_dt, (t, "Close")] = p
            df.loc[f_dt, (t, "Volume")] = vol_default
    return df


class TestC27ElegibilidadUniverso(unittest.TestCase):
    """Suite unitaria para el filtro de elegibilidad del universo (Paso C-27)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_c27_")
        self.temp_db = os.path.join(self.temp_dir, "test_synthetic_c27.db")
        self.orig_db_path = database.connection.DB_PATH
        database.connection.DB_PATH = self.temp_db
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass
        db_pred.init_predicciones_table()

        conn = get_connection()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS empresas (
                    ticker TEXT PRIMARY KEY,
                    nombre TEXT NOT NULL,
                    activa INTEGER DEFAULT 1,
                    es_ibex35 INTEGER DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ranking_estado (
                    ticker TEXT PRIMARY KEY,
                    posicion INTEGER DEFAULT 1,
                    score REAL DEFAULT 50.0
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        database.connection.DB_PATH = self.orig_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

    def _registrar_empresas(self, tickers):
        conn = get_connection()
        try:
            conn.execute("DELETE FROM empresas")
            conn.execute("DELETE FROM ranking_estado")
            for idx, t in enumerate(tickers, start=1):
                conn.execute(
                    "INSERT INTO empresas (ticker, nombre, activa, es_ibex35) VALUES (?, ?, 1, 1)",
                    (t, f"Empresa {t}"),
                )
                conn.execute(
                    "INSERT INTO ranking_estado (ticker, posicion, score) VALUES (?, ?, 50.0)",
                    (t, idx),
                )
            conn.commit()
        finally:
            conn.close()
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

    def test_a_pseudobarra_en_sesion_rechazada_y_no_emitida(self):
        """
        (a) Pseudo-barra en sesion S (Close = 10.0, Volume = 0 en S, aunque las 59
        sesiones previas tengan volumen alto) es rechazada con motivo
        "sin_volumen_en_sesion" y NO se registra en predicciones.
        """
        fecha_s = "2026-10-15"
        df = _crear_batch_60_sesiones(
            ["LIQ.MC", "PSEUDO.MC", "^IBEX"],
            fecha_sesion=fecha_s,
            n_sesiones=65,
            close_default=10.0,
            vol_default=50000.0,
        )
        df.loc[pd.to_datetime(fecha_s), ("PSEUDO.MC", "Close")] = 10.0
        df.loc[pd.to_datetime(fecha_s), ("PSEUDO.MC", "Volume")] = 0.0

        ok_liq, mot_liq = se.evaluar_elegibilidad_ticker(df, "LIQ.MC", fecha_s)
        ok_ps, mot_ps = se.evaluar_elegibilidad_ticker(df, "PSEUDO.MC", fecha_s)
        self.assertEqual((ok_liq, mot_liq), (True, "ok"))
        self.assertEqual((ok_ps, mot_ps), (False, "sin_volumen_en_sesion"))

        self._registrar_empresas(["LIQ.MC", "PSEUDO.MC"])
        dt_emision = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_emision), \
             patch.object(se, "descargar_price_batch", return_value=df), \
             patch.object(se, "sincronizar_cache_panel_historico", return_value=False), \
             patch.object(se, "guardar_snapshot_ranking", return_value=1), \
             patch.object(se, "resolver_predicciones", return_value=0):
            res = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["insertadas"], 4)
        self.assertIn("PSEUDO.MC", res["omitidos_sin_barra"])
        self.assertEqual(res["omitidos_detalle"].get("PSEUDO.MC"), "sin_volumen_en_sesion")
        self.assertEqual(res["omitidos_iliquidez"], ["PSEUDO.MC"])

        conn = get_connection()
        try:
            cnt_ps = conn.execute(
                "SELECT COUNT(*) FROM predicciones WHERE ticker = 'PSEUDO.MC'"
            ).fetchone()[0]
            cnt_liq = conn.execute(
                "SELECT COUNT(*) FROM predicciones WHERE ticker = 'LIQ.MC'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(cnt_ps, 0)
        self.assertEqual(cnt_liq, 4)

    def test_b_precio_bajo_minimo_rechazado(self):
        """
        (b) Precio inferior al minimo en S (Close = 0.04 < 0.05, con volumen alto)
        es rechazado con "precio_bajo_minimo".
        """
        fecha_s = "2026-10-15"
        df = _crear_batch_60_sesiones(
            ["PENNY.MC", "BORDE.MC"],
            fecha_sesion=fecha_s,
            n_sesiones=60,
            close_default=0.04,
            vol_default=1000000.0,
        )
        df[("BORDE.MC", "Close")] = 0.05
        df[("BORDE.MC", "Open")] = 0.05
        df[("BORDE.MC", "High")] = 0.051
        df[("BORDE.MC", "Low")] = 0.049

        ok_penny, mot_penny = se.evaluar_elegibilidad_ticker(df, "PENNY.MC", fecha_s)
        ok_borde, mot_borde = se.evaluar_elegibilidad_ticker(df, "BORDE.MC", fecha_s)
        self.assertEqual((ok_penny, mot_penny), (False, "precio_bajo_minimo"))
        self.assertEqual((ok_borde, mot_borde), (True, "ok"))

    def test_c_exceso_dias_sin_volumen_20s_rechazado(self):
        """
        (c) Exceso de dias sin volumen en 20 sesiones (6 dias con Volume = 0 en las
        ultimas 20 sesiones, aunque en S tenga volumen y mediano >= 5000) es
        rechazado con "exceso_dias_sin_volumen_20s". Con 5 dias es aceptado ("ok").
        """
        fecha_s = "2026-10-15"
        df = _crear_batch_60_sesiones(
            ["VOL6_20.MC", "VOL5_20.MC"],
            fecha_sesion=fecha_s,
            n_sesiones=60,
            close_default=10.0,
            vol_default=5000.0,  # 50000 EUR en los dias con volumen
        )
        ult_20 = df.index[-20:]
        # Poner 6 dias con Volume = 0 en las ultimas 20 sesiones (pero dejando S con volumen > 0)
        for d in ult_20[:6]:
            df.loc[d, ("VOL6_20.MC", "Volume")] = 0.0
        # Y 5 dias con Volume = 0 para VOL5_20.MC
        for d in ult_20[:5]:
            df.loc[d, ("VOL5_20.MC", "Volume")] = 0.0

        ok_6, mot_6 = se.evaluar_elegibilidad_ticker(df, "VOL6_20.MC", fecha_s)
        ok_5, mot_5 = se.evaluar_elegibilidad_ticker(df, "VOL5_20.MC", fecha_s)
        self.assertEqual((ok_6, mot_6), (False, "exceso_dias_sin_volumen_20s"))
        self.assertEqual((ok_5, mot_5), (True, "ok"))

    def test_d_exceso_dias_sin_volumen_60s_rechazado(self):
        """
        (d) Exceso de dias sin volumen en 60 sesiones (11 dias con Volume = 0 entre
        las sesiones -60 y -21 y 0 en las ultimas 20) es rechazado con
        "exceso_dias_sin_volumen_60s". Con 10 dias es aceptado ("ok").
        """
        fecha_s = "2026-10-15"
        df = _crear_batch_60_sesiones(
            ["VOL11_60.MC", "VOL10_60.MC"],
            fecha_sesion=fecha_s,
            n_sesiones=65,
            close_default=10.0,
            vol_default=5000.0,
        )
        tramo_60_a_21 = df.index[-60:-20]
        self.assertEqual(len(tramo_60_a_21), 40)

        for d in tramo_60_a_21[:11]:
            df.loc[d, ("VOL11_60.MC", "Volume")] = 0.0
        for d in tramo_60_a_21[:10]:
            df.loc[d, ("VOL10_60.MC", "Volume")] = 0.0

        ok_11, mot_11 = se.evaluar_elegibilidad_ticker(df, "VOL11_60.MC", fecha_s)
        ok_10, mot_10 = se.evaluar_elegibilidad_ticker(df, "VOL10_60.MC", fecha_s)
        self.assertEqual((ok_11, mot_11), (False, "exceso_dias_sin_volumen_60s"))
        self.assertEqual((ok_10, mot_10), (True, "ok"))

    def test_e_efectivo_mediano_20s_insuficiente_vs_exacto_5000(self):
        """
        (e) Efectivo mediano 20s insuficiente (Close = 2.0, Volume = 1000 ->
        2000 EUR < 5000 EUR) es rechazado con "efectivo_mediano_insuficiente",
        mientras que con 5000 EUR exactos es aceptado ("ok").
        """
        fecha_s = "2026-10-15"
        df = _crear_batch_60_sesiones(
            ["BAJO_EF.MC", "EXACTO_EF.MC"],
            fecha_sesion=fecha_s,
            n_sesiones=60,
            close_default=2.0,
            vol_default=1000.0,  # 2.0 * 1000 = 2000.0 EUR < 5000.0 EUR
        )
        df[("EXACTO_EF.MC", "Close")] = 2.0
        df[("EXACTO_EF.MC", "Volume")] = 2500.0  # 2.0 * 2500 = 5000.0 EUR exactos

        ok_bajo, mot_bajo = se.evaluar_elegibilidad_ticker(df, "BAJO_EF.MC", fecha_s)
        ok_exacto, mot_exacto = se.evaluar_elegibilidad_ticker(df, "EXACTO_EF.MC", fecha_s)
        self.assertEqual((ok_bajo, mot_bajo), (False, "efectivo_mediano_insuficiente"))
        self.assertEqual((ok_exacto, mot_exacto), (True, "ok"))

    def test_f_coherencia_camino_completo_y_segunda_ejecucion_already_emitted(self):
        """
        (f) Coherencia entre camino completo (success) y segunda ejecucion
        (already_emitted): si un universo tiene 2 tickers liquidos y 2 tickers
        zombi/iliquidos, la primera ejecucion emite solo los 2 liquidos (8 filas)
        y la segunda ejecucion devuelve already_emitted por el camino rapido sin
        recalcular el radar ni intentar emitir los 2 zombis.
        """
        fecha_s = "2026-10-15"
        tickers_univ = ["LIQ1.MC", "LIQ2.MC", "ZOMBI_VOL0.MC", "ILIQ_EF.MC"]
        df = _crear_batch_60_sesiones(
            tickers_univ + ["^IBEX"],
            fecha_sesion=fecha_s,
            n_sesiones=65,
            close_default=10.0,
            vol_default=20000.0,
        )
        # ZOMBI_VOL0.MC: pseudo-barra en fecha_s con Volume = 0
        df.loc[pd.to_datetime(fecha_s), ("ZOMBI_VOL0.MC", "Volume")] = 0.0
        # ILIQ_EF.MC: efectivo mediano 20s = 2.0 * 500 = 1000 EUR < 5000 EUR
        df[("ILIQ_EF.MC", "Close")] = 2.0
        df[("ILIQ_EF.MC", "Volume")] = 500.0

        self._registrar_empresas(tickers_univ)

        dt_emision = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        spy_radar = MagicMock(side_effect=se.calcular_radar_desde_batch)

        with _reloj_madrid(dt_emision), \
             patch.object(se, "descargar_price_batch", return_value=df), \
             patch.object(se, "sincronizar_cache_panel_historico", return_value=False), \
             patch.object(se, "guardar_snapshot_ranking", return_value=2), \
             patch.object(se, "resolver_predicciones", return_value=0), \
             patch.object(se, "calcular_radar_desde_batch", spy_radar):
            # 1a ejecucion: camino completo -> emite solo los 2 liquidos (8 filas)
            res1 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res1["status"], "success")
            self.assertEqual(res1["insertadas"], 8)
            self.assertEqual(spy_radar.call_count, 1)
            self.assertEqual(res1["omitidos_sin_barra"], ["ILIQ_EF.MC", "ZOMBI_VOL0.MC"])
            self.assertEqual(res1["omitidos_iliquidez"], ["ILIQ_EF.MC", "ZOMBI_VOL0.MC"])
            self.assertEqual(
                res1["omitidos_detalle"],
                {
                    "ILIQ_EF.MC": "efectivo_mediano_insuficiente",
                    "ZOMBI_VOL0.MC": "sin_volumen_en_sesion",
                },
            )

            # 2a ejecucion: camino rapido -> already_emitted sin recalcular radar
            res2 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res2["status"], "already_emitted")
            self.assertEqual(res2["insertadas"], 0)
            self.assertEqual(res2["emitidas"], 8)
            self.assertEqual(
                spy_radar.call_count,
                1,
                "La segunda ejecucion NO debe recalcular el radar",
            )
            self.assertEqual(res2["omitidos_sin_barra"], ["ILIQ_EF.MC", "ZOMBI_VOL0.MC"])
            self.assertEqual(res2["omitidos_iliquidez"], ["ILIQ_EF.MC", "ZOMBI_VOL0.MC"])
            self.assertEqual(res2["omitidos_detalle"], res1["omitidos_detalle"])

        conn = get_connection()
        try:
            filas = [
                tuple(r)
                for r in conn.execute(
                    "SELECT ticker, COUNT(*) FROM predicciones WHERE fecha_prediccion = ? GROUP BY ticker ORDER BY ticker",
                    (fecha_s,),
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertEqual(filas, [("LIQ1.MC", 4), ("LIQ2.MC", 4)])

    @unittest.skipUnless(
        os.path.exists(PARQUET_PATH) and os.path.exists(REAL_DB_PATH),
        "data/backtest_cache_6y.parquet o stocklab.db no disponible",
    )
    def test_g_comprobacion_solo_lectura_contra_backtest_cache_6y_parquet_2026_10_27(self):
        """
        (g) Comprobacion en solo lectura contra data/backtest_cache_6y.parquet
        (en la sesion '2026-10-27'): verifica que evaluar_elegibilidad_ticker
        excluye exactamente los 11 tickers identificados en el Paso 0d del Perfil 1
        (['ARM.MC', 'CEV.MC', 'DESA.MC', 'ECR.MC', 'GAM.MC', 'LIB.MC', 'MTB.MC',
        'NYE.MC', 'TRG.MC', 'UBS.MC', 'VID.MC'], con VID.MC por "sin_barra_fresca"
        en esa fecha y los zombis por sus motivos respectivos) y acepta los 105 restantes.
        """
        with open(REAL_DB_PATH, "rb") as f:
            sha_db_antes = hashlib.sha256(f.read()).hexdigest()

        conn_ro = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            tickers_activos = tuple(
                r[0].strip().upper()
                for r in conn_ro.execute(
                    "SELECT ticker FROM empresas WHERE activa = 1 ORDER BY ticker"
                ).fetchall()
            )
        finally:
            conn_ro.close()

        self.assertEqual(len(tickers_activos), 116)

        df_cache = pd.read_parquet(PARQUET_PATH)
        elegibles, omitidos_detalle, omitidos_iliquidez = se.filtrar_tickers_elegibles(
            df_cache, tickers_activos, "2026-10-27"
        )

        esperados_excluidos_10 = [
            "ARM.MC",
            "CEV.MC",
            "DESA.MC",
            "ECR.MC",
            "GAM.MC",
            "LIB.MC",
            "MTB.MC",
            "NYE.MC",
            "TRG.MC",
            "UBS.MC",
        ]
        motivos_esperados = {
            "ARM.MC": "sin_volumen_en_sesion",
            "CEV.MC": "exceso_dias_sin_volumen_20s",
            "DESA.MC": "sin_volumen_en_sesion",
            "ECR.MC": "sin_historico_minimo",
            "GAM.MC": "efectivo_mediano_insuficiente",
            "LIB.MC": "efectivo_mediano_insuficiente",
            "MTB.MC": "efectivo_mediano_insuficiente",
            "NYE.MC": "precio_bajo_minimo",
            "TRG.MC": "sin_volumen_en_sesion",
            "UBS.MC": "sin_volumen_en_sesion",
        }
        if omitidos_detalle.get("VID.MC") == "sin_barra_fresca":
            esperados_excluidos = sorted(esperados_excluidos_10 + ["VID.MC"])
            motivos_esperados["VID.MC"] = "sin_barra_fresca"
            esperados_elegibles = 105
        else:
            esperados_excluidos = esperados_excluidos_10
            esperados_elegibles = 106

        self.assertEqual(sorted(omitidos_detalle.keys()), esperados_excluidos)
        self.assertEqual(len(elegibles), esperados_elegibles)
        self.assertEqual(len(omitidos_detalle), len(esperados_excluidos))
        self.assertEqual(omitidos_detalle, motivos_esperados)
        self.assertEqual(
            omitidos_iliquidez,
            [
                "ARM.MC",
                "CEV.MC",
                "DESA.MC",
                "GAM.MC",
                "LIB.MC",
                "MTB.MC",
                "NYE.MC",
                "TRG.MC",
                "UBS.MC",
            ],
        )

        with open(REAL_DB_PATH, "rb") as f:
            sha_db_despues = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(sha_db_despues, sha_db_antes)


if __name__ == "__main__":
    unittest.main()
