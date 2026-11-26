"""
Tests para el Paso C-05: Constante unica MOTOR_VERSION
======================================================
Verifica que:
  (a) MOTOR_VERSION vale exactamente "v5-frozen" y es el mismo objeto/valor
      importado en services.signal_engine, services.emision, database.predicciones
      y views.ia.
  (b) No queda ningun literal "v4-institutional" activo como identificador de
      modelo en services/signal_engine.py, services/emision.py ni
      database/predicciones.py.
  (c) En una BD temporal sintetica, registrar_predicciones sin especificar
      modelo (y a traves del flujo de emision) sella las filas con
      modelo == MOTOR_VERSION ("v5-frozen").
"""

import ast
from contextlib import contextmanager
from datetime import datetime
import inspect
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import database.connection
import database.predicciones as db_pred
from database.connection import get_connection
import services.emision as se
import services.signal_engine as sig_eng
import utils.market_calendar as mc
import views.ia as via

MADRID_TZ = ZoneInfo("Europe/Madrid")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Inyecta get_ahora_madrid en los tres espacios de nombres requeridos."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _crear_batch_emision_sintetico(tickers, fecha_sesion: str):
    """Crea un DataFrame MultiIndex (ticker, campo) con 45 barras hasta fecha_sesion."""
    end_dt = pd.to_datetime(fecha_sesion)
    fechas = pd.bdate_range(end=end_dt, periods=45)
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(np.nan, index=fechas, columns=cols, dtype=float)
    for idx_i, f_dt in enumerate(fechas):
        p = 10.0 + idx_i * 0.05
        for t in tickers:
            df.loc[f_dt, (t, "Open")] = p
            df.loc[f_dt, (t, "High")] = p * 1.01
            df.loc[f_dt, (t, "Low")] = p * 0.99
            df.loc[f_dt, (t, "Close")] = p
            df.loc[f_dt, (t, "Volume")] = 50000.0
    return df


class TestC05MotorVersion(unittest.TestCase):
    """Suite de pruebas para C-05 sobre BD temporal sintetica y sin red."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_c05_")
        self.temp_db = os.path.join(self.temp_dir, "test_synthetic_c05.db")
        self.orig_db_path = database.connection.DB_PATH
        database.connection.DB_PATH = self.temp_db
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass
        db_pred.init_predicciones_table()

    def tearDown(self):
        database.connection.DB_PATH = self.orig_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

    def test_a_motor_version_identica_en_todos_los_modulos(self):
        """
        (a) MOTOR_VERSION vale exactamente "v5-frozen" y es el mismo objeto/valor
        importado en services.signal_engine, services.emision, database.predicciones
        y views.ia.
        """
        self.assertEqual(sig_eng.MOTOR_VERSION, "v5-frozen")
        self.assertEqual(se.MOTOR_VERSION, "v5-frozen")
        self.assertEqual(db_pred.MOTOR_VERSION, "v5-frozen")
        self.assertEqual(via.MOTOR_VERSION, "v5-frozen")

        self.assertIs(se.MOTOR_VERSION, sig_eng.MOTOR_VERSION)
        self.assertIs(db_pred.MOTOR_VERSION, sig_eng.MOTOR_VERSION)
        self.assertIs(via.MOTOR_VERSION, sig_eng.MOTOR_VERSION)
        self.assertEqual(db_pred.MODELO_VERSION, sig_eng.MOTOR_VERSION)

        sig_reg = inspect.signature(db_pred.registrar_predicciones)
        self.assertEqual(sig_reg.parameters["modelo"].default, sig_eng.MOTOR_VERSION)

    def test_b_sin_literales_v4_institutional(self):
        """
        (b) No queda ningun literal "v4-institutional" activo como identificador
        de modelo en services/signal_engine.py, services/emision.py ni
        database/predicciones.py.
        """
        archivos = [
            "services/signal_engine.py",
            "services/emision.py",
            "database/predicciones.py",
        ]
        for rel_path in archivos:
            abs_path = os.path.join(REPO_ROOT, rel_path)
            with open(abs_path, "r", encoding="utf-8") as f:
                src = f.read()

            self.assertNotIn(
                "v4-institutional",
                src,
                f"El archivo {rel_path} aun contiene la cadena 'v4-institutional'",
            )

            tree = ast.parse(src, filename=rel_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotEqual(
                        node.value,
                        "v4-institutional",
                        f"Literal AST 'v4-institutional' encontrado en {rel_path}",
                    )

    def test_c_registro_y_emision_sellan_motor_version_en_bd_temporal(self):
        """
        (c) En una BD temporal sintetica, registrar_predicciones sin especificar
        modelo (y a traves del flujo de emision) sella las filas con
        modelo == MOTOR_VERSION ("v5-frozen").
        """
        df_radar = pd.DataFrame([
            {
                "ticker": "SAN.MC",
                "empresa": "Banco Santander",
                "precio": 4.50,
                "senal_1D": "ALCISTA",
                "pct_1D": 1.2,
                "banda_1D": 1.5,
                "conf_1D": 65.0,
                "score_1D": 0.85,
                "pct_raw_1D": 1.2,
                "sigma_1D": 1.5,
                "senal_3D": "LATERAL",
                "pct_3D": 0.2,
                "banda_3D": 2.0,
                "conf_3D": 55.0,
                "score_3D": 0.10,
                "pct_raw_3D": 0.2,
                "sigma_3D": 2.0,
                "senal_1S": "BAJISTA",
                "pct_1S": -2.1,
                "banda_1S": 2.8,
                "conf_1S": 60.0,
                "score_1S": -0.75,
                "pct_raw_1S": -2.1,
                "sigma_1S": 2.8,
                "senal_1M": "ALCISTA",
                "pct_1M": 3.5,
                "banda_1M": 4.2,
                "conf_1M": 70.0,
                "score_1M": 0.95,
                "pct_raw_1M": 3.5,
                "sigma_1M": 4.2,
            }
        ])

        dt_emision = datetime(2026, 10, 15, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_emision):
            n_ins = db_pred.registrar_predicciones(df_radar, fecha_prediccion="2026-10-15")

        self.assertEqual(n_ins, 4)

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT horizonte, modelo, regla_eval FROM predicciones WHERE ticker = 'SAN.MC' ORDER BY dias ASC"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 4)
        for r in rows:
            self.assertEqual(r[1], sig_eng.MOTOR_VERSION)
            self.assertEqual(r[1], "v5-frozen")
            self.assertEqual(r[2], db_pred.REGLA_EVAL_VERSION)

        # Verificar tambien a traves del flujo de ejecutar_emision_diaria
        conn = get_connection()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS empresas (ticker TEXT PRIMARY KEY, nombre TEXT, activa INTEGER)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO empresas (ticker, nombre, activa) VALUES ('BBVA.MC', 'BBVA', 1)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ranking_estado (ticker TEXT PRIMARY KEY, posicion INTEGER, score REAL)"
            )
            conn.commit()
        finally:
            conn.close()

        df_batch = _crear_batch_emision_sintetico(["BBVA.MC", "^IBEX"], "2026-10-16")
        dt_emision_2 = datetime(2026, 10, 16, 19, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_emision_2), \
             patch("services.emision.descargar_price_batch", return_value=df_batch), \
             patch("services.emision.sincronizar_cache_panel_historico"):
            res = se.ejecutar_emision_diaria(verbose=False)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["insertadas"], 4)

        conn = get_connection()
        try:
            modelos_emision = [
                r[0]
                for r in conn.execute(
                    "SELECT modelo FROM predicciones WHERE ticker = 'BBVA.MC' AND fecha_prediccion = '2026-10-16'"
                ).fetchall()
            ]
        finally:
            conn.close()

        self.assertEqual(modelos_emision, [sig_eng.MOTOR_VERSION] * 4)


if __name__ == "__main__":
    unittest.main()
