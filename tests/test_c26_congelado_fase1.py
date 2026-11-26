"""
Tests unitarios para Paso C-26: Verificar y blindar congelado de Fase 1 (v5-frozen)
===================================================================================
Verifica sobre BD temporal sintetica, sin red y con reloj parcheado en
utils.market_calendar, services.emision y database.predicciones:
  (a) Invarianza total frente al historial de predicciones:
      Dado el mismo lote OHLCV sintetico, v5-frozen produce exactamente las
      mismas senales, pct_estimado, banda_pct, confianza, score_motor,
      score_bruto, params_hash y features_json tanto en una BD vacia como en
      una BD contaminada con 500 predicciones resueltas de fallo sistematico.
  (b) Consistencia de firmas y defaults:
      calcular_radar_desde_batch y calcular_senales_ticker tienen
      usar_calibracion=False por defecto; verificar_estado_congelado_v5()
      reporta feedback_loop_activo=False y params_hash_oficial determinista.
  (c) Unicidad e inmutabilidad de params_hash en produccion:
      stocklab.db en modo solo lectura contiene un unico valor de params_hash
      para todas las filas de v5-frozen, identico a calcular_params_hash_motor().
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import database.connection as db_conn
from database import predicciones as db_pred
from database.connection import get_connection, init_db
import services.emision as se
import services.signal_engine as sig_eng
from services.signal_engine import (
    FASE_1_CONGELADA,
    FEEDBACK_LOOP_ACTIVO,
    MOTOR_VERSION,
    calcular_params_hash_motor,
    verificar_estado_congelado_v5,
)
import utils.market_calendar as mc

MADRID_TZ = ZoneInfo("Europe/Madrid")
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REAL_DB_PATH = os.path.join(BASE_DIR, "stocklab.db")


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Parchea get_ahora_madrid en los tres espacios de nombres requeridos."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _crear_batch_ohlcv_sintetico(
    tickers,
    fecha_sesion: str = "2026-10-15",
    n_sesiones: int = 65,
    close_default: float = 12.5,
    vol_default: float = 80000.0,
) -> pd.DataFrame:
    """Construye un lote OHLCV sintetico determinista con n_sesiones habiles."""
    end_dt = pd.to_datetime(fecha_sesion)
    fechas = pd.bdate_range(end=end_dt, periods=n_sesiones)
    cols = pd.MultiIndex.from_product(
        [list(tickers), ["Open", "High", "Low", "Close", "Volume"]],
        names=["Ticker", "Price"],
    )
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)
    for idx_i, f_dt in enumerate(fechas):
        drift = 1.0 + 0.002 * (idx_i % 9)
        for t in tickers:
            p = close_default * drift if t == "^IBEX" else close_default * (1.0 + 0.001 * (idx_i % 5))
            df.loc[f_dt, (t, "Open")] = p
            df.loc[f_dt, (t, "High")] = p * 1.015
            df.loc[f_dt, (t, "Low")] = p * 0.985
            df.loc[f_dt, (t, "Close")] = p
            df.loc[f_dt, (t, "Volume")] = vol_default
    return df


class TestC26CongeladoFase1(unittest.TestCase):
    """Suite de validacion para el blindaje y congelado de Fase 1 (v5-frozen)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_db_path = db_conn.DB_PATH
        self.fijo_dt = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)

    def tearDown(self):
        db_conn.DB_PATH = self.orig_db_path
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    def _inicializar_bd(self, ruta_db: str, tickers_empresas):
        """Inicializa esquema v2 y tabla empresas en la base de datos indicada."""
        db_conn.DB_PATH = ruta_db
        init_db()
        db_pred.init_predicciones_table()
        db_pred.init_evaluaciones_table()
        conn = get_connection()
        try:
            conn.execute("DELETE FROM empresas")
            for t in tickers_empresas:
                conn.execute(
                    "INSERT INTO empresas (ticker, nombre, activa) VALUES (?, ?, 1)",
                    (t, f"Empresa {t}"),
                )
            conn.commit()
        finally:
            conn.close()

    def _precargar_500_predicciones_contaminadas(self, ruta_db: str, tickers_afectados):
        """Precarga 500 predicciones resueltas extremas con 100% de fallos para probar feedback loop."""
        conn = sqlite3.connect(ruta_db)
        try:
            horizontes = ["1D", "3D", "1S", "1M"]
            n_tickers = len(tickers_afectados)
            total_precargar = 500
            fecha_base = datetime(2026, 1, 1)

            for i in range(total_precargar):
                t = tickers_afectados[i % n_tickers]
                hz = horizontes[(i // n_tickers) % len(horizontes)]
                dia_offset = i // (n_tickers * len(horizontes))
                dt_p = fecha_base + timedelta(days=dia_offset)
                fecha_p = dt_p.strftime("%Y-%m-%d")
                dt_obj = dt_p + timedelta(days=5)
                fecha_obj = dt_obj.strftime("%Y-%m-%d")

                # Senal ALCISTA con caida catastrofica (-20%) y acierto 0
                cur = conn.execute(
                    """
                    INSERT INTO predicciones (
                        ticker, fecha_prediccion, horizonte, dias, fecha_objetivo,
                        senal, precio_entrada, precio_real, variacion_real, acierto,
                        resuelta, motivo_resolucion, fecha_resolucion, modelo,
                        comp_rsi, comp_macd, comp_tendencia, comp_momentum,
                        score_motor, score_bruto, params_hash
                    ) VALUES (
                        ?, ?, ?, 1, ?,
                        'ALCISTA', 10.0, 8.0, -20.0, 0,
                        1, 'cierre_oficial', ?, 'v5-frozen',
                        0.8, 0.7, 0.6, 0.9,
                        1.5, 1.5, ?
                    )
                    """,
                    (t, fecha_p, hz, fecha_obj, fecha_obj, sig_eng.get_params_hash()),
                )
                pid = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO evaluaciones (
                        prediccion_id, regla, precio_entrada_eval, precio_salida_eval,
                        variacion_pct, umbral_aplicado, clase_real, acierto, acierto_signo, evaluada_en
                    ) VALUES (?, 'c10_precio_cierre', 10.0, 8.0, -20.0, 0.8, 'BAJISTA', 0, 0, ?)
                    """,
                    (pid, f"{fecha_obj}T18:30:00"),
                )
            conn.commit()
            cnt_res = conn.execute("SELECT COUNT(*) FROM predicciones WHERE resuelta = 1").fetchone()[0]
            self.assertEqual(cnt_res, total_precargar)
        finally:
            conn.close()

    def test_a_invarianza_total_frente_al_historial_predicciones(self):
        """
        (a) Prueba reina de C-26:
        Verifica que ejecutar_emision_diaria y calcular_radar_desde_batch producen
        exactamente las mismas senales bit a bit en DB_vacia y DB_contaminada
        (senal, pct_estimado, banda_pct, confianza, score_motor, score_bruto,
         params_hash y features_json 100% identicos).
        """
        db_vacia_path = os.path.join(self.tmp_dir.name, "db_vacia.db")
        db_contaminada_path = os.path.join(self.tmp_dir.name, "db_contaminada.db")
        tickers_universo = ["SAN.MC", "BBVA.MC", "ITX.MC", "TEF.MC"]
        todos_simbolos = list(tickers_universo) + ["^IBEX"]

        df_batch = _crear_batch_ohlcv_sintetico(todos_simbolos, fecha_sesion="2026-10-15")
        nombres = tuple(f"Empresa {t}" for t in tickers_universo)
        tilts = tuple(0.1 * idx for idx in range(len(tickers_universo)))

        # 1. Ejecucion sobre DB_vacia
        self._inicializar_bd(db_vacia_path, tickers_universo)
        db_conn.DB_PATH = db_vacia_path

        radar_vacia = sig_eng.calcular_radar_desde_batch(
            df_batch, tuple(tickers_universo), nombres, tilts,
            sentimientos=None,
            usar_calibracion=False,
        )

        with _reloj_madrid(self.fijo_dt), \
             patch.object(se, "descargar_price_batch", return_value=df_batch), \
             patch.object(se, "sincronizar_cache_panel_historico", return_value=False), \
             patch.object(se, "guardar_snapshot_ranking", return_value=1), \
             patch.object(se, "resolver_predicciones", return_value=0):
            res_vacia = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res_vacia["status"], "success")
        self.assertEqual(res_vacia["insertadas"], len(tickers_universo) * 4)

        conn_vacia = sqlite3.connect(db_vacia_path)
        try:
            cols_query = (
                "ticker, horizonte, senal, pct_estimado, banda_pct, "
                "confianza, score_motor, score_bruto, params_hash, features_json"
            )
            rows_vacia = conn_vacia.execute(
                f"SELECT {cols_query} FROM predicciones WHERE fecha_prediccion = '2026-10-15' ORDER BY ticker, horizonte"
            ).fetchall()
        finally:
            conn_vacia.close()

        # 2. Ejecucion sobre DB_contaminada
        self._inicializar_bd(db_contaminada_path, tickers_universo)
        self._precargar_500_predicciones_contaminadas(db_contaminada_path, tickers_universo)
        db_conn.DB_PATH = db_contaminada_path

        radar_contaminada = sig_eng.calcular_radar_desde_batch(
            df_batch, tuple(tickers_universo), nombres, tilts,
            sentimientos=None,
            usar_calibracion=False,
        )

        with _reloj_madrid(self.fijo_dt), \
             patch.object(se, "descargar_price_batch", return_value=df_batch), \
             patch.object(se, "sincronizar_cache_panel_historico", return_value=False), \
             patch.object(se, "guardar_snapshot_ranking", return_value=1), \
             patch.object(se, "resolver_predicciones", return_value=0):
            res_contaminada = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res_contaminada["status"], "success")
        self.assertEqual(res_contaminada["insertadas"], len(tickers_universo) * 4)

        conn_contam = sqlite3.connect(db_contaminada_path)
        try:
            rows_contaminada = conn_contam.execute(
                f"SELECT {cols_query} FROM predicciones WHERE fecha_prediccion = '2026-10-15' ORDER BY ticker, horizonte"
            ).fetchall()
        finally:
            conn_contam.close()

        # 3. Comparacion bit a bit de DataFrames y filas en BD
        pd.testing.assert_frame_equal(radar_vacia, radar_contaminada)

        self.assertEqual(len(rows_vacia), len(rows_contaminada))
        self.assertGreater(len(rows_vacia), 0)

        for i, (fila_v, fila_c) in enumerate(zip(rows_vacia, rows_contaminada)):
            (t_v, hz_v, s_v, pct_v, bnd_v, conf_v, sm_v, sb_v, ph_v, fj_v) = fila_v
            (t_c, hz_c, s_c, pct_c, bnd_c, conf_c, sm_c, sb_c, ph_c, fj_c) = fila_c

            self.assertEqual(t_v, t_c, f"Ticker difiere en fila {i}")
            self.assertEqual(hz_v, hz_c, f"Horizonte difiere en fila {i}")
            self.assertEqual(s_v, s_c, f"Senal difiere en fila {i}")
            self.assertEqual(pct_v, pct_c, f"Pct estimado difiere en fila {i}")
            self.assertEqual(bnd_v, bnd_c, f"Banda pct difiere en fila {i}")
            self.assertEqual(conf_v, conf_c, f"Confianza difiere en fila {i}")
            self.assertEqual(sm_v, sm_c, f"Score motor difiere en fila {i}")
            self.assertEqual(sb_v, sb_c, f"Score bruto difiere en fila {i}")
            self.assertEqual(ph_v, ph_c, f"Params hash difiere en fila {i}")
            self.assertEqual(fj_v, fj_c, f"Features JSON difiere en fila {i}")

    def test_b_consistencia_de_firmas_defaults_y_verificar_estado(self):
        """
        (b) Consistencia de firmas y defaults:
        Inspecciona inspect.signature de calcular_radar_desde_batch y calcular_senales_ticker,
        y comprueba que verificar_estado_congelado_v5() devuelve el estado exacto.
        """
        sig_radar = inspect.signature(sig_eng.calcular_radar_desde_batch)
        self.assertIn("usar_calibracion", sig_radar.parameters)
        self.assertIs(sig_radar.parameters["usar_calibracion"].default, False)

        sig_ticker = inspect.signature(sig_eng.calcular_senales_ticker)
        self.assertIn("usar_calibracion", sig_ticker.parameters)
        self.assertIs(sig_ticker.parameters["usar_calibracion"].default, False)

        estado = verificar_estado_congelado_v5()
        self.assertEqual(estado["motor_version"], "v5-frozen")
        self.assertIs(estado["fase_1_congelada"], True)
        self.assertIs(estado["usar_calibracion_default"], False)
        self.assertIs(estado["feedback_loop_activo"], False)

        ph_oficial = estado["params_hash_oficial"]
        self.assertEqual(ph_oficial, calcular_params_hash_motor())
        self.assertEqual(ph_oficial, sig_eng.get_params_hash())
        self.assertEqual(len(ph_oficial), 64)
        # Verificacion determinista de digitos hexadecimales
        self.assertTrue(all(c in "0123456789abcdef" for c in ph_oficial))
        int(ph_oficial, 16)
        # Prefijo de 16 caracteres hexadecimales determinista
        self.assertEqual(len(ph_oficial[:16]), 16)

    def test_c_unicidad_params_hash_en_produccion_stocklab_db_ro(self):
        """
        (c) Unicidad e inmutabilidad de params_hash en produccion:
        Comprueba que todas las filas de stocklab.db con modelo='v5-frozen' y
        params_hash IS NOT NULL tienen exactamente el mismo params_hash igual a
        calcular_params_hash_motor() sin modificar la BD (total_changes == 0).
        """
        self.assertTrue(os.path.exists(REAL_DB_PATH), f"No se encuentra {REAL_DB_PATH}")
        conn = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            total_antes = conn.total_changes
            filas_hash = conn.execute(
                "SELECT DISTINCT params_hash, COUNT(*) "
                "FROM predicciones "
                "WHERE modelo = 'v5-frozen' AND params_hash IS NOT NULL "
                "GROUP BY params_hash"
            ).fetchall()

            self.assertEqual(len(filas_hash), 1, "Debe existir un unico params_hash en v5-frozen")
            unico_hash, cnt_filas = filas_hash[0]
            self.assertGreater(cnt_filas, 0, "Debe haber filas emitidas bajo v5-frozen")
            self.assertEqual(unico_hash, calcular_params_hash_motor())
            self.assertEqual(conn.total_changes, total_antes, "stocklab.db fue modificada en test!")
        finally:
            conn.close()

    def test_d_guarda_activa_fase_1_bloquea_intento_calibracion_explicita(self):
        """
        (d) Verifica que incluso si un llamador intenta invocar explicitamente
        calcular_radar_desde_batch(..., usar_calibracion=True), la constante
        FASE_1_CONGELADA suprime el feedback loop y produce exactamente el mismo
        resultado que con usar_calibracion=False.
        """
        db_path = os.path.join(self.tmp_dir.name, "db_guarda.db")
        tickers = ["SAN.MC", "BBVA.MC"]
        self._inicializar_bd(db_path, tickers)
        self._precargar_500_predicciones_contaminadas(db_path, tickers)
        db_conn.DB_PATH = db_path

        df_batch = _crear_batch_ohlcv_sintetico(tickers + ["^IBEX"])
        radar_sin_calib = sig_eng.calcular_radar_desde_batch(
            df_batch, tuple(tickers), ("Banco Santander", "BBVA"), (0.0, 0.0),
            usar_calibracion=False,
        )
        radar_intento_calib = sig_eng.calcular_radar_desde_batch(
            df_batch, tuple(tickers), ("Banco Santander", "BBVA"), (0.0, 0.0),
            usar_calibracion=True,
        )
        pd.testing.assert_frame_equal(radar_sin_calib, radar_intento_calib)


if __name__ == "__main__":
    unittest.main()
