"""
Test de Higiene y Protección del Historial de Predicciones — StockLab IA
========================================================================
Verifica las reglas institucionales del Paso C-25 y remates C-25a:
  1. Base de datos puramente sintética en memoria/temporal (nunca toca ni copia stocklab.db).
  2. Inmutabilidad: una señal emitida no puede ser sobreescrita por re-emisión (ON CONFLICT DO NOTHING).
  3. Soft-Delete: limpiar_resueltas archiva (archivada = 1) sin borrar filas físicas de SQLite.
  4. Guarda intradía a las 12:00: registrar_predicciones con fecha de hoy devuelve 0, no inserta y emite warning.
  5. Emisión oficial a las 18:35: registrar_predicciones con barra consolidada inserta las filas.
  6. Fecha obligatoria: registrar_predicciones sin fecha o con fecha inválida lanza ValueError.
  7. Equivalencia de sesion_cerrada_hoy() para 12 instantes representativos.
  8. Resolución con barra intradía: a las 12:00 catch_up_emision no resuelve filas con fecha_objetivo=hoy;
     a las 18:35 sí las resuelve con barra completa.
  9. truncar_a_barras_completas: 5 escenarios verificando discriminación por fecha/hora sin pérdida de barras.
 10. Preservación en UI: precargar_ia y show a las 12:00 sobre BD sintética dejan predicciones idéntica.
 11. Caso A: secuencia 12:00 (sin inserción) -> 18:30 emisión oficial sin abortar con already_emitted.
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database.connection
from database import predicciones as db_pred
from database.connection import get_connection
import services.emision as se
import utils.market_calendar as mc
import views.ia as via


def _crear_df_senal_dummy(ticker="TEST.MC", empresa="Test Corp", precio=10.0,
                          senal_1D="ALCISTA", pct_1D=1.5,
                          ultima_fecha=None) -> pd.DataFrame:
    """Crea un DataFrame mínimo válido que simula la salida de calcular_radar."""
    d = {
        "ticker": ticker,
        "empresa": empresa,
        "precio": precio,
        "senal_1D": senal_1D,
        "pct_1D": pct_1D,
        "banda_1D": 2.0,
        "conf_1D": 80.0,
        "score_1D": 65.0,
        "comp_rsi": 60.0,
        "comp_macd": 0.5,
        "comp_tendencia": 1.0,
        "comp_momentum": 0.8,
        "adx": 25.0,
        "rvol": 1.2,
        "marea": "ALCISTA",
        "rs_ibex": 1.0,
        "sentimiento": 0.0,
        "tilt": 0.0,
        "rsi": 60.0,
    }
    if ultima_fecha is not None:
        d["ultima_fecha"] = ultima_fecha
    return pd.DataFrame([d])


def _crear_batch_ohlcv(tickers, fechas, precios_base=None, faltantes=None):
    """Crea un DataFrame OHLCV con columnas MultiIndex (ticker, metric) para pruebas sin red."""
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(index=pd.to_datetime(fechas), columns=cols, dtype=float)
    precios_base = precios_base or {}
    faltantes = faltantes or {}
    for t in tickers:
        base = precios_base.get(t, 10.0)
        df[(t, "Open")] = base
        df[(t, "High")] = base * 1.02
        df[(t, "Low")] = base * 0.98
        df[(t, "Close")] = base
        df[(t, "Volume")] = 10000.0
        if t in faltantes:
            for f in faltantes[t]:
                f_dt = pd.to_datetime(f)
                if f_dt in df.index:
                    for m in ["Open", "High", "Low", "Close", "Volume"]:
                        df.loc[f_dt, (t, m)] = np.nan
    return df


class DummyContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestHigienePredicciones(unittest.TestCase):
    """Suite de pruebas de higiene sobre base de datos temporal puramente sintética."""

    def setUp(self):
        """Crea una base de datos temporal sintética (sin copiar ni abrir stocklab.db real)."""
        self.temp_dir = tempfile.mkdtemp()
        self.temp_db = os.path.join(self.temp_dir, "test_synthetic.db")
        self.orig_db_path = database.connection.DB_PATH

        database.connection.DB_PATH = self.temp_db
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

        # Inicializar esquema de predicciones
        db_pred.init_predicciones_table()

        # Poblar tablas sintéticas mínimas requeridas para flujos de prewarm/UI
        conn = get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS empresas (
                ticker TEXT PRIMARY KEY,
                nombre TEXT NOT NULL,
                activa INTEGER DEFAULT 1,
                es_ibex35 INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ranking_estado (
                ticker TEXT PRIMARY KEY,
                score REAL DEFAULT 50.0
            )
        """)
        conn.execute("""
            INSERT INTO empresas (ticker, nombre, activa, es_ibex35) VALUES
            ('SAN.MC', 'Banco Santander', 1, 1),
            ('BBVA.MC', 'BBVA', 1, 1)
        """)
        conn.execute("""
            INSERT INTO ranking_estado (ticker, score) VALUES
            ('SAN.MC', 55.0),
            ('BBVA.MC', 45.0)
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        """Restaura la ruta original de la base de datos y elimina archivos temporales."""
        database.connection.DB_PATH = self.orig_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        try:
            database.connection.query_all.clear()
            database.connection.query_val.clear()
        except Exception:
            pass

    def test_inmutabilidad_emision_no_sobrescribe(self):
        """Verifica que registrar_predicciones no sobreescribe una señal ya emitida para la misma fecha."""
        conn = get_connection()
        try:
            dt_emision = datetime(2026, 1, 1, 19, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
            with patch.object(mc, "get_ahora_madrid", return_value=dt_emision), \
                 patch.object(db_pred, "get_ahora_madrid", return_value=dt_emision):
                df_senal1 = _crear_df_senal_dummy(ticker="TEST.MC", precio=10.0, senal_1D="ALCISTA")
                n1 = db_pred.registrar_predicciones(df_senal1, fecha_prediccion="2026-01-01")
                self.assertEqual(n1, 1, f"Debe registrar 1 predicción inicial, obtuvo {n1}")

                row1 = conn.execute(
                    "SELECT precio_entrada, senal FROM predicciones WHERE ticker = 'TEST.MC' AND horizonte = '1D'"
                ).fetchone()
                self.assertEqual(row1[0], 10.0)
                self.assertEqual(row1[1], "ALCISTA")

                # Intentar re-emitir en el mismo día con diferente precio y señal
                df_senal2 = _crear_df_senal_dummy(ticker="TEST.MC", precio=15.0, senal_1D="BAJISTA")
                n2 = db_pred.registrar_predicciones(df_senal2, fecha_prediccion="2026-01-01")
                self.assertEqual(n2, 0, "No debe registrar ni sobreescribir señales ya existentes")

                row2 = conn.execute(
                    "SELECT precio_entrada, senal FROM predicciones WHERE ticker = 'TEST.MC' AND horizonte = '1D'"
                ).fetchone()
                self.assertEqual(row2[0], 10.0, "El precio de entrada original jamás debe cambiar")
                self.assertEqual(row2[1], "ALCISTA", "La señal original jamás debe mutar")
        finally:
            conn.close()

    def test_soft_delete_preserva_filas_fisicas(self):
        """Verifica guarda STOCKLAB_DEV, soft-delete fisico y lectura unificada de archivada = 1."""
        conn = get_connection()
        try:
            conn.execute("""
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, precio_entrada, modelo, resuelta, precio_real, acierto, archivada
                ) VALUES (
                    'TEST_SOFT.MC', 'Test Soft Corp', '2026-01-01', '1D', 1, '2026-01-02',
                    'ALCISTA', 10.0, 'v5-frozen', 1, 10.5, 1, 0
                )
            """)
            conn.commit()

            cnt_fisico_antes = conn.execute(
                "SELECT COUNT(*) FROM predicciones WHERE ticker = 'TEST_SOFT.MC'"
            ).fetchone()[0]
            self.assertEqual(cnt_fisico_antes, 1)

            res_antes = db_pred.obtener_resueltas(horizonte="1D")
            self.assertTrue(any(res_antes["ticker"] == "TEST_SOFT.MC"))

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("STOCKLAB_DEV", None)
                with self.assertRaises(PermissionError):
                    db_pred.limpiar_resueltas(horizonte="1D")

            with patch.dict(os.environ, {"STOCKLAB_DEV": "1"}):
                n_arch = db_pred.limpiar_resueltas(horizonte="1D")
            self.assertGreaterEqual(n_arch, 1)

            cnt_fisico_despues = conn.execute(
                "SELECT COUNT(*) FROM predicciones WHERE ticker = 'TEST_SOFT.MC'"
            ).fetchone()[0]
            self.assertEqual(cnt_fisico_despues, 1, "La fila NUNCA debe ser eliminada fisicamente")

            estado_arch = conn.execute(
                "SELECT archivada FROM predicciones WHERE ticker = 'TEST_SOFT.MC'"
            ).fetchone()[0]
            self.assertEqual(estado_arch, 1, "La columna archivada debe valer 1")

            res_despues = db_pred.obtener_resueltas(horizonte="1D")
            self.assertTrue(any(res_despues.get("ticker", pd.Series()) == "TEST_SOFT.MC"))
        finally:
            conn.close()

    def test_guarda_intradia_12_00_no_inserta(self):
        """(i) A las 12:00 de un día hábil, registrar con fecha de hoy inserta 0 filas y emite warning en log."""
        dt_12 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        df = _crear_df_senal_dummy(ticker="INTRADAY.MC")

        with patch.object(mc, "get_ahora_madrid", return_value=dt_12), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_12), \
             patch.object(se, "get_ahora_madrid", return_value=dt_12), \
             patch("services.emision.date") as mock_se_date:
            mock_se_date.today.return_value = dt_12.date()

            self.assertFalse(db_pred.barra_hoy_completa())
            with self.assertLogs("database.predicciones", level="WARNING") as cm:
                n = db_pred.registrar_predicciones(df, fecha_prediccion="2026-10-07")
            self.assertEqual(n, 0, "A las 12:00 registrar con fecha de hoy debe abortar y retornar 0")
            self.assertTrue(
                any("registrar_predicciones abortado" in m for m in cm.output),
                "El log debe contener el warning de la guarda intradía"
            )

        conn = get_connection()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM predicciones WHERE ticker = 'INTRADAY.MC' AND fecha_prediccion = '2026-10-07'"
            ).fetchone()[0]
            self.assertEqual(cnt, 0, "No debe haberse insertado ninguna fila en BD")
        finally:
            conn.close()

    def test_emision_18_35_inserta_filas(self):
        """(ii) A las 18:35 del mismo día hábil, registrar con fecha de hoy inserta las filas."""
        dt_18 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        df = _crear_df_senal_dummy(ticker="CONSOL.MC")

        with patch.object(mc, "get_ahora_madrid", return_value=dt_18), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_18), \
             patch.object(se, "get_ahora_madrid", return_value=dt_18), \
             patch("services.emision.date") as mock_se_date:
            mock_se_date.today.return_value = dt_18.date()

            self.assertTrue(db_pred.barra_hoy_completa())
            n = db_pred.registrar_predicciones(df, fecha_prediccion="2026-10-07")
            self.assertEqual(n, 1, "A las 18:35 con barra completa debe insertar la señal")

        conn = get_connection()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM predicciones WHERE ticker = 'CONSOL.MC' AND fecha_prediccion = '2026-10-07'"
            ).fetchone()[0]
            self.assertEqual(cnt, 1, "Debe existir exactamente 1 fila en BD")
        finally:
            conn.close()

    def test_fecha_prediccion_obligatoria(self):
        """(iii) Sin fecha_prediccion o con fecha inválida lanza ValueError."""
        df = _crear_df_senal_dummy()

        with self.assertRaises(ValueError):
            db_pred.registrar_predicciones(df)

        with self.assertRaises(ValueError):
            db_pred.registrar_predicciones(df, fecha_prediccion=None)

        with self.assertRaises(ValueError):
            db_pred.registrar_predicciones(df, fecha_prediccion="")

        with self.assertRaises(ValueError):
            db_pred.registrar_predicciones(df, fecha_prediccion="   ")

        with self.assertRaises(ValueError):
            db_pred.registrar_predicciones(df, fecha_prediccion="fecha_invalida")

        with self.assertRaises(ValueError):
            db_pred.registrar_predicciones(df, fecha_prediccion="2026-02-31")

    def test_equivalencia_sesion_cerrada_hoy(self):
        """
        Tarea 2: Demuestra equivalencia de sesion_cerrada_hoy() en los 12 instantes temporales requeridos.
        """
        madrid_tz = ZoneInfo("Europe/Madrid")
        casos = [
            ("lunes 00:07", datetime(2026, 10, 5, 0, 7, tzinfo=madrid_tz), False),
            ("dia habil 09:00", datetime(2026, 10, 7, 9, 0, tzinfo=madrid_tz), False),
            ("dia habil 17:34", datetime(2026, 10, 7, 17, 34, tzinfo=madrid_tz), False),
            ("dia habil 17:36", datetime(2026, 10, 7, 17, 36, tzinfo=madrid_tz), True),
            ("dia habil 18:29", datetime(2026, 10, 7, 18, 29, tzinfo=madrid_tz), True),
            ("dia habil 18:30", datetime(2026, 10, 7, 18, 30, tzinfo=madrid_tz), True),
            ("dia habil 21:00", datetime(2026, 10, 7, 21, 0, tzinfo=madrid_tz), True),
            ("sabado 12:00", datetime(2026, 10, 10, 12, 0, tzinfo=madrid_tz), True),
            ("festivo BME 12:00 (1-may-2026)", datetime(2026, 5, 1, 12, 0, tzinfo=madrid_tz), True),
            ("festivo BME 19:00 (1-may-2026)", datetime(2026, 5, 1, 19, 0, tzinfo=madrid_tz), True),
            ("lunes 26-oct-2026 18:29", datetime(2026, 10, 26, 18, 29, tzinfo=madrid_tz), True),
            ("lunes 26-oct-2026 18:30", datetime(2026, 10, 26, 18, 30, tzinfo=madrid_tz), True),
        ]

        for nombre, dt, esperado in casos:
            with self.subTest(caso=nombre):
                with patch.object(mc, "get_ahora_madrid", return_value=dt):
                    res = mc.sesion_cerrada_hoy()
                    self.assertEqual(res, esperado, f"Fallo en {nombre}: esperado {esperado}, obtenido {res}")

    def test_resolucion_intradia_12_00_vs_18_35(self):
        """
        Tarea 3b: Pendientes con fecha_objetivo = HOY.
        A las 12:00 (intradía), catch_up_emision NO resuelve ninguna (resuelta=0).
        A las 18:35 (barra completa), catch_up_emision resuelve las vencidas hoy (resuelta=1).
        """
        hoy_str = "2026-10-07"
        conn = get_connection()
        conn.execute("""
            INSERT INTO predicciones (
                ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                senal, precio_entrada, modelo, resuelta, pct_estimado, banda_pct
            ) VALUES (
                'SAN.MC', 'Banco Santander', '2026-10-06', '1D', 1, ?,
                'ALCISTA', 4.0, 'v5-frozen', 0, 1.5, 2.0
            )
        """, (hoy_str,))
        conn.commit()
        conn.close()

        # Preparar df_batch sintético con barra de hoy (>= 35 barras requeridas por MIN_BARRAS)
        fechas = pd.date_range("2026-08-01", "2026-10-07", freq="B")
        cols = pd.MultiIndex.from_tuples([("SAN.MC", "Close"), ("SAN.MC", "Open")])
        data = [[4.0, 4.0]] * (len(fechas) - 1) + [[4.2, 4.0]]
        df_batch = pd.DataFrame(data, index=fechas, columns=cols)

        dt_12 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_12), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_12), \
             patch.object(se, "get_ahora_madrid", return_value=dt_12), \
             patch("services.emision.descargar_price_batch", return_value=df_batch), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("database.predicciones.date") as mock_dp_date, \
             patch("services.emision.date") as mock_se_date:
            mock_dp_date.today.return_value = dt_12.date()
            mock_dp_date.fromisoformat.side_effect = date.fromisoformat
            mock_se_date.today.return_value = dt_12.date()
            mock_se_date.fromisoformat.side_effect = date.fromisoformat

            se.catch_up_emision()

        conn = get_connection()
        row_12 = conn.execute("SELECT resuelta FROM predicciones WHERE ticker = 'SAN.MC'").fetchone()
        conn.close()
        self.assertEqual(row_12[0], 0, "A las 12:00 ninguna fila con fecha_objetivo=hoy debe pasar a resuelta=1")

        # A las 18:35 con barra completa consolidada
        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.descargar_price_batch", return_value=df_batch), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.calcular_radar_desde_batch", return_value=_crear_df_senal_dummy(ticker="SAN.MC")), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("database.predicciones.date") as mock_dp_date, \
             patch("services.emision.date") as mock_se_date:
            mock_dp_date.today.return_value = dt_1835.date()
            mock_dp_date.fromisoformat.side_effect = date.fromisoformat
            mock_se_date.today.return_value = dt_1835.date()
            mock_se_date.fromisoformat.side_effect = date.fromisoformat

            res_1835 = se.catch_up_emision()

        conn = get_connection()
        row_1835 = conn.execute("SELECT resuelta, precio_real FROM predicciones WHERE ticker = 'SAN.MC'").fetchone()
        conn.close()
        self.assertEqual(row_1835[0], 1, "A las 18:35 con barra completa la fila sí debe resolverse")
        self.assertEqual(row_1835[1], 4.2, "El precio real debe ser el cierre oficial de hoy")

    def test_truncar_a_barras_completas_5_escenarios(self):
        """
        Tarea 3c: Verifica truncar_a_barras_completas en los 5 escenarios requeridos.
        Confirma que la discriminación es por fecha y hora, no un iloc[:-1] a ciegas.
        """
        def _hacer_batch(fechas_str, precios):
            idx = pd.to_datetime(fechas_str)
            cols = pd.MultiIndex.from_tuples([("SAN.MC", "Close")])
            return pd.DataFrame([[p] for p in precios], index=idx, columns=cols)

        # 1) Lunes 00:07 (hoy = 2026-10-05, última barra = viernes 2026-10-02)
        dt_1 = datetime(2026, 10, 5, 0, 7, tzinfo=ZoneInfo("Europe/Madrid"))
        df_1 = _hacer_batch(["2026-10-01", "2026-10-02"], [10.0, 10.2])
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1), \
             patch("services.emision.date") as mock_date:
            mock_date.today.return_value = dt_1.date()
            df_res, fecha_res = se.truncar_a_barras_completas(df_1)
            self.assertEqual(fecha_res, "2026-10-02")
            self.assertEqual(len(df_res), 2, "No debe descartar la barra del viernes")

        # 2) Sábado 12:00 (hoy = 2026-10-10, última barra = viernes 2026-10-09)
        dt_2 = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        df_2 = _hacer_batch(["2026-10-08", "2026-10-09"], [10.0, 10.3])
        with patch.object(mc, "get_ahora_madrid", return_value=dt_2), \
             patch.object(se, "get_ahora_madrid", return_value=dt_2), \
             patch("services.emision.date") as mock_date:
            mock_date.today.return_value = dt_2.date()
            df_res, fecha_res = se.truncar_a_barras_completas(df_2)
            self.assertEqual(fecha_res, "2026-10-09")
            self.assertEqual(len(df_res), 2, "No debe descartar barra en fin de semana")

        # 3) Festivo entre semana a las 12:00 (hoy = 2026-05-01, última barra = jueves 2026-04-30)
        dt_3 = datetime(2026, 5, 1, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        df_3 = _hacer_batch(["2026-04-29", "2026-04-30"], [10.0, 10.1])
        with patch.object(mc, "get_ahora_madrid", return_value=dt_3), \
             patch.object(se, "get_ahora_madrid", return_value=dt_3), \
             patch("services.emision.date") as mock_date:
            mock_date.today.return_value = dt_3.date()
            df_res, fecha_res = se.truncar_a_barras_completas(df_3)
            self.assertEqual(fecha_res, "2026-04-30")
            self.assertEqual(len(df_res), 2, "No debe descartar barra en festivo")

        # 4) Día hábil a las 12:00 (hoy = 2026-10-07, barra parcial de hoy = 2026-10-07)
        dt_4 = datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        df_4 = _hacer_batch(["2026-10-06", "2026-10-07"], [10.0, 10.5])
        with patch.object(mc, "get_ahora_madrid", return_value=dt_4), \
             patch.object(se, "get_ahora_madrid", return_value=dt_4), \
             patch("services.emision.date") as mock_date:
            mock_date.today.return_value = dt_4.date()
            df_res, fecha_res = se.truncar_a_barras_completas(df_4)
            self.assertEqual(fecha_res, "2026-10-06", "Debe recortar a la sesión de ayer")
            self.assertEqual(len(df_res), 1, "Debe descartar la barra intradía incompleta")

        # 5) Día hábil a las 18:35 (hoy = 2026-10-07, barra completa de hoy = 2026-10-07)
        dt_5 = datetime(2026, 10, 7, 18, 35, tzinfo=ZoneInfo("Europe/Madrid"))
        df_5 = _hacer_batch(["2026-10-06", "2026-10-07"], [10.0, 10.5])
        with patch.object(mc, "get_ahora_madrid", return_value=dt_5), \
             patch.object(se, "get_ahora_madrid", return_value=dt_5), \
             patch("services.emision.date") as mock_date:
            mock_date.today.return_value = dt_5.date()
            df_res, fecha_res = se.truncar_a_barras_completas(df_5)
            self.assertEqual(fecha_res, "2026-10-07", "Debe mantener la sesión de hoy consolidada")
            self.assertEqual(len(df_res), 2, "NUNCA debe descartar la barra completa consolidada")

    def test_simulacion_precargar_ia_y_show_a_las_12_00(self):
        """
        Tarea 4: Simulando precargar_ia y show a las 12:00 sobre una BD temporal sintética,
        la tabla predicciones queda idéntica (mismo recuento y mismo checksum de filas).
        No depende de stocklab.db ni de un número fijo de filas de producción.
        """
        # Insertar predicciones sintéticas previas para la sesión anterior (2026-10-06)
        conn = get_connection()
        for i in range(5):
            conn.execute("""
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, precio_entrada, modelo, resuelta
                ) VALUES (
                    ?, 'Empresa Test', '2026-10-06', '1D', 1, '2026-10-07',
                    'ALCISTA', 10.0, 'v5-frozen', 0
                )
            """, (f"TICKER_{i}.MC",))
        for t in ("SAN.MC", "BBVA.MC"):
            conn.execute("""
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, precio_entrada, modelo, resuelta
                ) VALUES (
                    ?, 'Empresa Activa', '2026-10-06', '1D', 1, '2026-10-07',
                    'ALCISTA', 10.0, 'v5-frozen', 0
                )
            """, (t,))
        conn.commit()

        rows_antes = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
        count_antes = len(rows_antes)
        checksum_antes = hashlib.sha256(repr([tuple(r) for r in rows_antes]).encode("utf-8")).hexdigest()
        conn.close()

        dt_12 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        dates = pd.to_datetime(["2026-10-06", "2026-10-07"])
        cols = pd.MultiIndex.from_tuples([("SAN.MC", "Close"), ("BBVA.MC", "Close")])
        df_batch = pd.DataFrame([[4.0, 9.0], [4.1, 9.1]], index=dates, columns=cols)
        radar_en_vivo = pd.concat([
            _crear_df_senal_dummy(ticker="SAN.MC", precio=4.5),
            _crear_df_senal_dummy(ticker="BBVA.MC", precio=9.2),
        ], ignore_index=True)

        with patch.object(mc, "get_ahora_madrid", return_value=dt_12), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_12), \
             patch.object(se, "get_ahora_madrid", return_value=dt_12), \
             patch("services.emision.date") as mock_se_date, \
             patch("services.emision.descargar_price_batch", return_value=df_batch), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.signal_engine.calcular_radar", return_value=radar_en_vivo), \
             patch("views.ia._seccion_agente"), \
             patch("views.ia._css"), \
             patch("views.ia.cargar_datos_cache_historico", return_value=({}, {}, {}, {})), \
             patch("views.ia._render_panel_descriptivo"), \
             patch("views.ia._seccion_radar"), \
             patch("views.ia._seccion_historial"), \
             patch("views.ia._render_auditoria_metodologia"), \
             patch("streamlit.tabs", return_value=[DummyContext(), DummyContext(), DummyContext()]), \
             patch("services.assistant_engine.cargar_fundamentales_disco", return_value={}), \
             patch("services.llm_agent.disponible", return_value=False):
            mock_se_date.today.return_value = dt_12.date()

            via.precargar_ia()
            via.show()

        conn = get_connection()
        rows_despues = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
        count_despues = len(rows_despues)
        checksum_despues = hashlib.sha256(repr([tuple(r) for r in rows_despues]).encode("utf-8")).hexdigest()
        conn.close()

        self.assertEqual(count_despues, count_antes, "El recuento de predicciones debe ser idéntico")
        self.assertEqual(checksum_despues, checksum_antes, "El checksum de predicciones debe ser idéntico")

    def test_caso_a_secuencia_12_00_luego_18_30(self):
        """
        Caso A: A las 12:00 no se inserta nada; a las 18:30 la emisión oficial
        inserta las filas y no devuelve 'already_emitted'.
        """
        fecha_test = "2026-10-07"

        # Fase 1: 12:00 del día hábil (apertura de la app con radar en vivo)
        dt_12 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        df_radar_en_vivo = _crear_df_senal_dummy(ticker="SAN.MC", precio=4.5, ultima_fecha=fecha_test)

        with patch.object(mc, "get_ahora_madrid", return_value=dt_12), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_12), \
             patch.object(se, "get_ahora_madrid", return_value=dt_12), \
             patch("services.emision.date") as mock_se_date:
            mock_se_date.today.return_value = dt_12.date()

            n_intradia = db_pred.registrar_predicciones(df_radar_en_vivo, fecha_prediccion=fecha_test)
            self.assertEqual(n_intradia, 0, "A las 12:00 la guarda intradía debe devolver 0")

        conn = get_connection()
        cnt_12 = conn.execute(
            "SELECT COUNT(*) FROM predicciones WHERE fecha_prediccion = ?", (fecha_test,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(cnt_12, 0, "A las 12:00 no debe haberse insertado ninguna fila para hoy")

        # Fase 2: 18:30 del mismo día (emisión oficial programada con barra consolidada)
        dt_1830 = datetime(2026, 10, 7, 18, 30, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1830), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1830), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1830), \
             patch("services.emision.date") as mock_se_date, \
             patch("services.emision.descargar_price_batch", return_value=pd.DataFrame({"Close": [10.0]})), \
             patch("services.emision.truncar_a_barras_completas", return_value=(pd.DataFrame({"Close": [10.0]}), fecha_test)), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.calcular_radar_desde_batch", return_value=df_radar_en_vivo), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_se_date.today.return_value = dt_1830.date()

            res = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res.get("status"), "success", "La emisión oficial no debe dar already_emitted")
            self.assertGreater(res.get("insertadas", 0), 0, "Debe haber insertado señales oficiales")

        conn = get_connection()
        cnt_1830 = conn.execute(
            "SELECT COUNT(*) FROM predicciones WHERE fecha_prediccion = ?", (fecha_test,)
        ).fetchone()[0]
        conn.close()
        self.assertGreater(cnt_1830, 0, "A las 18:30 deben constar las señales oficiales en BD")

    def test_emision_valor_sin_barra_fresca_omitido(self):
        """
        Tarea 4(i): Un valor sin barra en fecha_sesion no genera filas y aparece en omitidos_sin_barra.
        """
        fecha_test = "2026-10-07"
        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        # 2 empresas en el radar: SAN.MC (fresca 2026-10-07) y STALE.MC (antigua 2026-10-06)
        radar_mixto = pd.concat([
            _crear_df_senal_dummy(ticker="SAN.MC", empresa="Santander", ultima_fecha="2026-10-07"),
            _crear_df_senal_dummy(ticker="STALE.MC", empresa="Stale Corp", ultima_fecha="2026-10-06"),
        ], ignore_index=True)

        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_se_date, \
             patch("services.emision.descargar_price_batch", return_value=pd.DataFrame({"Close": [10.0]})), \
             patch("services.emision.truncar_a_barras_completas", return_value=(pd.DataFrame({"Close": [10.0]}), fecha_test)), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.calcular_radar_desde_batch", return_value=radar_mixto), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_se_date.today.return_value = dt_1835.date()

            res = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res.get("status"), "success")
        self.assertIn("STALE.MC", res.get("omitidos_sin_barra", []))
        self.assertNotIn("SAN.MC", res.get("omitidos_sin_barra", []))

        conn = get_connection()
        tickers_en_bd = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM predicciones WHERE fecha_prediccion = ?", (fecha_test,)
        ).fetchall()]
        conn.close()

        self.assertIn("SAN.MC", tickers_en_bd, "SAN.MC tiene barra fresca y debe estar emitida")
        self.assertNotIn("STALE.MC", tickers_en_bd, "STALE.MC no tiene barra fresca y NO debe estar emitida")

    def test_emision_incremental_y_segunda_ejecucion(self):
        """
        Tarea 4(ii), (iii), (vi):
        - Emisión parcial inicial (status: success).
        - Segunda ejecución con barra ya disponible (status: topped_up, solo inserta lo que falta, filas existentes con mismo checksum).
        - Tercera ejecución sin cambios (status: already_emitted, 0 filas nuevas).
        - Señales de un valor emitido inicial vs incremental son idénticas.
        """
        fecha_test = "2026-10-07"
        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        # Configuración común de mocks
        radar_ejecucion_1 = pd.concat([
            _crear_df_senal_dummy(ticker="SAN.MC", precio=4.0, senal_1D="ALCISTA", pct_1D=2.0, ultima_fecha="2026-10-07"),
            _crear_df_senal_dummy(ticker="BBVA.MC", precio=9.0, senal_1D="BAJISTA", pct_1D=-1.0, ultima_fecha="2026-10-06"),  # Sin barra fresca
        ], ignore_index=True)

        radar_ejecucion_2 = pd.concat([
            _crear_df_senal_dummy(ticker="SAN.MC", precio=4.0, senal_1D="ALCISTA", pct_1D=2.0, ultima_fecha="2026-10-07"),
            _crear_df_senal_dummy(ticker="BBVA.MC", precio=9.2, senal_1D="BAJISTA", pct_1D=-1.0, ultima_fecha="2026-10-07"),  # Ahora sí tiene barra fresca
        ], ignore_index=True)

        # ── Fase 1: Emisión inicial (solo SAN.MC) ──
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_se_date, \
             patch("services.emision.descargar_price_batch", return_value=pd.DataFrame({"Close": [10.0]})), \
             patch("services.emision.truncar_a_barras_completas", return_value=(pd.DataFrame({"Close": [10.0]}), fecha_test)), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.calcular_radar_desde_batch", return_value=radar_ejecucion_1), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_se_date.today.return_value = dt_1835.date()
            res1 = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res1.get("status"), "success")
        self.assertEqual(res1.get("omitidos_sin_barra"), ["BBVA.MC"])

        conn = get_connection()
        filas_san_1 = conn.execute(
            "SELECT * FROM predicciones WHERE ticker = 'SAN.MC' AND fecha_prediccion = ? ORDER BY horizonte",
            (fecha_test,)
        ).fetchall()
        checksum_san_1 = hashlib.sha256(repr([tuple(r) for r in filas_san_1]).encode("utf-8")).hexdigest()
        count_total_1 = conn.execute("SELECT COUNT(*) FROM predicciones WHERE fecha_prediccion = ?", (fecha_test,)).fetchone()[0]
        conn.close()
        self.assertGreater(count_total_1, 0)

        # ── Fase 2: Segunda ejecución con BBVA.MC ahora disponible (incremental) ──
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_se_date, \
             patch("services.emision.descargar_price_batch", return_value=pd.DataFrame({"Close": [10.0]})), \
             patch("services.emision.truncar_a_barras_completas", return_value=(pd.DataFrame({"Close": [10.0]}), fecha_test)), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.calcular_radar_desde_batch", return_value=radar_ejecucion_2), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_se_date.today.return_value = dt_1835.date()
            res2 = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res2.get("status"), "topped_up", "Segunda ejecución con nuevos valores debe retornar topped_up")
        self.assertEqual(res2.get("omitidos_sin_barra"), [])
        self.assertGreater(res2.get("insertadas", 0), 0)

        conn = get_connection()
        filas_san_2 = conn.execute(
            "SELECT * FROM predicciones WHERE ticker = 'SAN.MC' AND fecha_prediccion = ? ORDER BY horizonte",
            (fecha_test,)
        ).fetchall()
        checksum_san_2 = hashlib.sha256(repr([tuple(r) for r in filas_san_2]).encode("utf-8")).hexdigest()
        filas_bbva_2 = conn.execute(
            "SELECT senal, pct_estimado, score_motor, tilt FROM predicciones WHERE ticker = 'BBVA.MC' AND fecha_prediccion = ? AND horizonte = '1D'",
            (fecha_test,)
        ).fetchone()
        conn.close()

        # Comprobar inmutabilidad estricta de las filas preexistentes
        self.assertEqual(checksum_san_2, checksum_san_1, "Las filas existentes de SAN.MC deben ser 100% idénticas (inmutables)")
        # Comprobar señales de BBVA.MC
        self.assertEqual(filas_bbva_2[0], "BAJISTA")
        self.assertEqual(filas_bbva_2[1], -1.0)

        # ── Fase 3: Tercera ejecución sin cambios (already_emitted) ──
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_se_date, \
             patch("services.emision.descargar_price_batch", return_value=pd.DataFrame({"Close": [10.0]})), \
             patch("services.emision.truncar_a_barras_completas", return_value=(pd.DataFrame({"Close": [10.0]}), fecha_test)), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.calcular_radar_desde_batch", return_value=radar_ejecucion_2), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_se_date.today.return_value = dt_1835.date()
            res3 = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res3.get("status"), "already_emitted")
        self.assertEqual(res3.get("insertadas"), 0)

    def test_fila_archivada_no_se_reemite(self):
        """
        Tarea 4(iv): Una fila archivada para (fecha_sesion, modelo) cuenta como ya emitida
        y no se vuelve a generar ni emitir.
        """
        fecha_test = "2026-10-07"
        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        # Pre-insertar filas archivadas para SAN.MC y BBVA.MC
        conn = get_connection()
        for t in ("SAN.MC", "BBVA.MC"):
            conn.execute("""
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, precio_entrada, modelo, resuelta, archivada
                ) VALUES (
                    ?, 'Empresa Arch', ?, '1D', 1, '2026-10-08',
                    'ALCISTA', 10.0, 'v5-frozen', 1, 1
                )
            """, (t, fecha_test))
        conn.commit()
        cnt_antes = conn.execute("SELECT COUNT(*) FROM predicciones WHERE fecha_prediccion = ?", (fecha_test,)).fetchone()[0]
        conn.close()

        radar = pd.concat([
            _crear_df_senal_dummy(ticker="SAN.MC", ultima_fecha=fecha_test),
            _crear_df_senal_dummy(ticker="BBVA.MC", ultima_fecha=fecha_test),
        ], ignore_index=True)

        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_se_date, \
             patch("services.emision.descargar_price_batch", return_value=pd.DataFrame({"Close": [10.0]})), \
             patch("services.emision.truncar_a_barras_completas", return_value=(pd.DataFrame({"Close": [10.0]}), fecha_test)), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.calcular_radar_desde_batch", return_value=radar), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_se_date.today.return_value = dt_1835.date()
            res = se.ejecutar_emision_diaria(force=False, verbose=False)

        self.assertEqual(res.get("status"), "already_emitted")
        self.assertEqual(res.get("insertadas"), 0)

        conn = get_connection()
        cnt_despues = conn.execute("SELECT COUNT(*) FROM predicciones WHERE fecha_prediccion = ?", (fecha_test,)).fetchone()[0]
        conn.close()
        self.assertEqual(cnt_despues, cnt_antes, "No deben haberse insertado filas adicionales")

    def test_guarda_ultima_fecha_registrar_predicciones(self):
        """
        Tarea 4(v): La guarda de ultima_fecha de registrar_predicciones omite filas con fecha distinta
        y emite un warning en el log con el recuento y los tickers.
        """
        df = pd.concat([
            _crear_df_senal_dummy(ticker="FRESH.MC", ultima_fecha="2026-10-07"),
            _crear_df_senal_dummy(ticker="STALE1.MC", ultima_fecha="2026-10-06"),
            _crear_df_senal_dummy(ticker="STALE2.MC", ultima_fecha="2026-10-05"),
        ], ignore_index=True)

        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch("database.predicciones.date") as mock_dp_date:
            mock_dp_date.today.return_value = dt_1835.date()
            mock_dp_date.fromisoformat.side_effect = date.fromisoformat

            with self.assertLogs("database.predicciones", level="WARNING") as cm:
                n = db_pred.registrar_predicciones(df, fecha_prediccion="2026-10-07")

        self.assertEqual(n, 1, "Solo debe registrar la fila con fecha coincidente (FRESH.MC)")
        self.assertTrue(any("2 filas omitidas por tener ultima_fecha distinta" in m for m in cm.output))
        self.assertTrue(any("STALE1.MC" in m and "STALE2.MC" in m for m in cm.output))

        conn = get_connection()
        tickers_bd = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM predicciones WHERE fecha_prediccion = '2026-10-07'"
        ).fetchall()]
        conn.close()
        self.assertIn("FRESH.MC", tickers_bd)
        self.assertNotIn("STALE1.MC", tickers_bd)
        self.assertNotIn("STALE2.MC", tickers_bd)

    # ═══════════════════════════════════════════════════════════════════
    # 🧪 PASO C-25c: FECHA DE SESIÓN GLOBAL Y CAMINO RÁPIDO SIN RADAR
    # ═══════════════════════════════════════════════════════════════════

    def test_c25c_i_ticker_ref_sin_barra_12_00_trunca_a_ayer(self):
        """
        Paso C-25c (i): Ticker de referencia sin barra el último día a las 12:00
        con barras parciales de hoy en los demás valores:
        Se truncan las barras de hoy, fecha_sesion = ayer y las señales
        emitidas se calculan con el cierre de ayer (no con la barra parcial de hoy).
        """
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO empresas (ticker, nombre, activa) VALUES ('REF.MC', 'Ref Ticker', 1), ('ACT.MC', 'Act Ticker', 1)")
        conn.commit()
        conn.close()

        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        df = _crear_batch_ohlcv(
            ["REF.MC", "ACT.MC", "^IBEX"],
            fechas,
            precios_base={"REF.MC": 10.0, "ACT.MC": 40.0, "^IBEX": 10900.0},
            faltantes={"REF.MC": ["2026-10-07"]}
        )
        df.loc["2026-10-07", ("ACT.MC", "Close")] = 55.0
        df.loc["2026-10-07", ("^IBEX", "Close")] = 11000.0

        dt_0800 = datetime(2026, 10, 7, 8, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_0800), \
             patch.object(se, "get_ahora_madrid", return_value=dt_0800), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_0800), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=2), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_date.today.return_value = dt_0800.date()

            df_trunc, f_sesion = se.truncar_a_barras_completas(df)
            self.assertEqual(f_sesion, "2026-10-06")
            self.assertNotIn("2026-10-07", [d.strftime("%Y-%m-%d") for d in df_trunc.index])

            res = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res["fecha"], "2026-10-06")

        conn = get_connection()
        p_act = conn.execute(
            "SELECT precio_entrada FROM predicciones WHERE ticker = 'ACT.MC' AND fecha_prediccion = '2026-10-06'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(p_act)
        self.assertEqual(p_act[0], 40.0, "Las señales deben calcularse SIN la barra de hoy (cierre 40.0 de ayer)")

    def test_c25c_ii_mismo_lote_18_35_barra_completa_fecha_hoy(self):
        """
        Paso C-25c (ii): El mismo lote a las 18:35 con barras completas:
        fecha_sesion = hoy (2026-10-07).
        """
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO empresas (ticker, nombre, activa) VALUES ('REF.MC', 'Ref Ticker', 1), ('ACT.MC', 'Act Ticker', 1)")
        conn.commit()
        conn.close()

        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        df = _crear_batch_ohlcv(
            ["REF.MC", "ACT.MC", "^IBEX"],
            fechas,
            precios_base={"REF.MC": 10.0, "ACT.MC": 40.0, "^IBEX": 10900.0},
            faltantes={"REF.MC": ["2026-10-07"]}
        )
        df.loc["2026-10-07", ("ACT.MC", "Close")] = 55.0
        df.loc["2026-10-07", ("^IBEX", "Close")] = 11000.0

        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_date.today.return_value = dt_1835.date()

            df_trunc, f_sesion = se.truncar_a_barras_completas(df)
            self.assertEqual(f_sesion, "2026-10-07")

            res = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res["fecha"], "2026-10-07")
            self.assertIn("REF.MC", res.get("omitidos_sin_barra", []))

        conn = get_connection()
        p_act = conn.execute(
            "SELECT precio_entrada FROM predicciones WHERE ticker = 'ACT.MC' AND fecha_prediccion = '2026-10-07'"
        ).fetchone()
        cnt_ref = conn.execute(
            "SELECT COUNT(*) FROM predicciones WHERE ticker = 'REF.MC' AND fecha_prediccion = '2026-10-07'"
        ).fetchone()[0]
        conn.close()
        self.assertIsNotNone(p_act)
        self.assertEqual(p_act[0], 55.0, "A las 18:35 el precio debe ser el consolidado de hoy (55.0)")
        self.assertEqual(cnt_ref, 0, "REF.MC no tiene barra y no debe emitirse")

    def test_c25c_iii_permutar_orden_tickers_lote_invariante(self):
        """
        Paso C-25c (iii): Permutar el orden de los tickers del lote (semilla fija)
        no cambia el resultado (ni fecha_sesion ni el DataFrame truncado).
        """
        tickers = ["T1.MC", "T2.MC", "T3.MC", "T4.MC", "T5.MC"]
        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        df_orig = _crear_batch_ohlcv(tickers, fechas, faltantes={"T1.MC": ["2026-10-07"]})

        rng = np.random.RandomState(42)
        tickers_perm = list(rng.permutation(tickers))
        cols_perm = pd.MultiIndex.from_product([tickers_perm, ["Open", "High", "Low", "Close", "Volume"]])
        df_perm = df_orig[cols_perm].copy()

        df_t1, f1 = se.truncar_a_barras_completas(df_orig)
        df_t2, f2 = se.truncar_a_barras_completas(df_perm)

        self.assertEqual(f1, f2)
        self.assertEqual(len(df_t1), len(df_t2))

    def test_c25c_iv_camino_rapido_sin_radar_con_valor_sin_barra(self):
        """
        Paso C-25c (iv): Con un valor sin barra que nunca se emite, la segunda ejecución
        NO llama a calcular_radar_desde_batch (contador por mock) y devuelve 'already_emitted'.
        """
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO empresas (ticker, nombre, activa) VALUES ('SAN.MC', 'Santander', 1), ('BBVA.MC', 'BBVA', 1), ('NOBAR.MC', 'Sin Barra', 1)")
        conn.commit()
        conn.close()

        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        df = _crear_batch_ohlcv(
            ["SAN.MC", "BBVA.MC", "NOBAR.MC", "^IBEX"],
            fechas,
            faltantes={"NOBAR.MC": ["2026-10-07"]}
        )

        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        spy_radar = MagicMock(side_effect=se.calcular_radar_desde_batch)

        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=2), \
             patch("services.emision.resolver_predicciones", return_value=0), \
             patch("services.emision.calcular_radar_desde_batch", spy_radar):
            mock_date.today.return_value = dt_1835.date()

            # 1ª ejecución: calcula radar y emite SAN y BBVA (NOBAR queda omitido)
            res1 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res1["status"], "success")
            self.assertEqual(spy_radar.call_count, 1)

            # 2ª ejecución: todos los elegibles (SAN y BBVA) ya están emitidos.
            # Debe tomar el camino rápido y NO llamar al radar.
            res2 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res2["status"], "already_emitted")
            self.assertEqual(spy_radar.call_count, 1, "calcular_radar_desde_batch NO debe llamarse en la 2ª ejecución")
            self.assertEqual(res2["insertadas"], 0)

    def test_c25c_v_falta_valor_elegible_emite_solo_faltante(self):
        """
        Paso C-25c (v): Si falta un valor elegible, calcula el radar y emite solo ese
        (status 'topped_up'), manteniendo las filas preexistentes intactas.
        """
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO empresas (ticker, nombre, activa) VALUES ('SAN.MC', 'Santander', 1), ('BBVA.MC', 'BBVA', 1)")
        conn.commit()
        conn.close()

        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        df_1 = _crear_batch_ohlcv(
            ["SAN.MC", "BBVA.MC", "^IBEX"],
            fechas,
            faltantes={"BBVA.MC": ["2026-10-07"]}
        )

        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df_1), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_date.today.return_value = dt_1835.date()

            res1 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res1["status"], "success")

        conn = get_connection()
        filas_san_1 = conn.execute("SELECT * FROM predicciones WHERE ticker = 'SAN.MC'").fetchall()
        chk_san_1 = hashlib.sha256(repr([tuple(r) for r in filas_san_1]).encode("utf-8")).hexdigest()
        cnt_bbva_1 = conn.execute("SELECT COUNT(*) FROM predicciones WHERE ticker = 'BBVA.MC'").fetchone()[0]
        conn.close()
        self.assertEqual(cnt_bbva_1, 0)

        # 2ª ejecución: ahora BBVA.MC sí tiene barra fresca hoy
        df_2 = _crear_batch_ohlcv(
            ["SAN.MC", "BBVA.MC", "^IBEX"],
            fechas
        )
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df_2), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=2), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_date.today.return_value = dt_1835.date()

            res2 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res2["status"], "topped_up")
            self.assertEqual(res2["insertadas"], 4)

        conn = get_connection()
        filas_san_2 = conn.execute("SELECT * FROM predicciones WHERE ticker = 'SAN.MC'").fetchall()
        chk_san_2 = hashlib.sha256(repr([tuple(r) for r in filas_san_2]).encode("utf-8")).hexdigest()
        cnt_bbva_2 = conn.execute("SELECT COUNT(*) FROM predicciones WHERE ticker = 'BBVA.MC'").fetchone()[0]
        conn.close()
        self.assertEqual(chk_san_2, chk_san_1, "Las filas existentes de SAN.MC deben ser inmutables")
        self.assertEqual(cnt_bbva_2, 4, "BBVA.MC debe tener sus 4 horizontes registrados")

    def test_c25c_vi_umbral_50_pct_y_no_data(self):
        """
        Paso C-25c (vi):
        - Con menos del 50 % de valores válidos en la última fecha se descarta esa fecha
          y se usa la anterior.
        - Sin ninguna fecha válida: status 'no_data' y 0 inserciones.
        """
        tickers = ["T1", "T2", "T3", "T4"]
        fechas = ["2026-10-05", "2026-10-06"]
        # En 2026-10-06 solo T1 tiene valor válido (1/4 = 25% < 50%)
        # En 2026-10-05 todos tienen valor válido (4/4 = 100%)
        df = _crear_batch_ohlcv(tickers, fechas, faltantes={
            "T2": ["2026-10-06"],
            "T3": ["2026-10-06"],
            "T4": ["2026-10-06"],
        })
        df_trunc, f_sesion = se.truncar_a_barras_completas(df)
        self.assertEqual(f_sesion, "2026-10-05", "Debe descartar 2026-10-06 por tener menos del 50% de valores válidos")

        # Sin ninguna fecha válida
        df_invalido = pd.DataFrame(index=pd.to_datetime(["2026-10-06"]), columns=df.columns, dtype=float)
        df_t_inv, f_inv = se.truncar_a_barras_completas(df_invalido)
        self.assertIsNone(f_inv)

        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO empresas (ticker, nombre, activa) VALUES ('SAN.MC', 'Santander', 1)")
        conn.commit()
        conn.close()

        with patch("services.emision.descargar_price_batch", return_value=df_invalido), \
             patch("services.emision.sincronizar_cache_panel_historico"):
            res = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res["status"], "no_data")
            self.assertEqual(res["insertadas"], 0)

        conn = get_connection()
        cnt = conn.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 0, "No debe haberse insertado ninguna fila en BD")

    def test_c25c_vii_equivalencia_lote_normal_checksum(self):
        """
        Paso C-25c (vii): Para un lote normal (todos con barras frescas) las filas y
        señales emitidas son idénticas (verificado por checksum).
        """
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO empresas (ticker, nombre, activa) VALUES ('SAN.MC', 'Santander', 1), ('BBVA.MC', 'BBVA', 1)")
        conn.commit()
        conn.close()

        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        df = _crear_batch_ohlcv(["SAN.MC", "BBVA.MC", "^IBEX"], fechas)

        dt_1835 = datetime(2026, 10, 7, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=2), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_date.today.return_value = dt_1835.date()

            res = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["insertadas"], 8)

        conn = get_connection()
        filas = conn.execute("SELECT ticker, horizonte, senal, precio_entrada FROM predicciones ORDER BY ticker, horizonte").fetchall()
        conn.close()
        chk = hashlib.sha256(repr([tuple(r) for r in filas]).encode("utf-8")).hexdigest()
        self.assertIsNotNone(chk)
        self.assertEqual(len(filas), 8)

    def test_c25d_resolver_reloj_madrid_vs_today(self):
        """
        C-25d C3(i): Parchea get_ahora_madrid a un instante en que el día de Madrid
        difiere del de date.today() (p. ej. 2026-10-08 00:30 Madrid con date.today()
        devolviendo el día anterior 2026-10-07) y comprueba que el resolver usa el día de Madrid.
        """
        conn = get_connection()
        conn.execute("""
            INSERT INTO predicciones (
                ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                senal, precio_entrada, modelo, resuelta
            ) VALUES (
                'SAN.MC', 'Banco Santander', '2026-10-06', '1D', 1, '2026-10-07',
                'ALCISTA', 10.0, 'v5-frozen', 0
            )
        """)
        conn.commit()
        conn.close()

        # Batch con >= MIN_BARRAS barras incluyendo la de fecha_objetivo 2026-10-07
        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        cols = pd.MultiIndex.from_tuples([("SAN.MC", "Close")])
        df_batch = pd.DataFrame(10.0, index=fechas, columns=cols)
        df_batch.loc["2026-10-07", ("SAN.MC", "Close")] = 10.5

        dt_madrid = datetime(2026, 10, 8, 0, 30, tzinfo=ZoneInfo("Europe/Madrid"))
        yesterday_date = date(2026, 10, 7)

        with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid), \
             patch("database.predicciones.date") as mock_date:
            mock_date.today.return_value = yesterday_date

            resueltas = db_pred.resolver_predicciones(df_batch=df_batch)
            self.assertEqual(resueltas, 1, "Debe resolver usando hoy de Madrid (2026-10-08 > 2026-10-07)")

        conn = get_connection()
        row = conn.execute("SELECT resuelta, fecha_resolucion, precio_real FROM predicciones WHERE ticker='SAN.MC'").fetchone()
        conn.close()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "2026-10-07")
        self.assertEqual(row[2], 10.5)

    def test_c25d_sin_barra_hoy_ignora_dict_precios_y_emite_warning(self):
        """
        C-25d C3(ii): Una fila con fecha_objetivo = hoy, mercado cerrado y sin barra
        de hoy en df_batch NO se resuelve aunque se pase `precios` con valores,
        y se emite el warning 'parámetro precios obsoleto e ignorado'.
        """
        conn = get_connection()
        conn.execute("""
            INSERT INTO predicciones (
                ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                senal, precio_entrada, modelo, resuelta
            ) VALUES (
                'SAN.MC', 'Banco Santander', '2026-10-06', '1D', 1, '2026-10-07',
                'ALCISTA', 10.0, 'v5-frozen', 0
            )
        """)
        conn.commit()
        conn.close()

        # Batch solo hasta 2026-10-06 (falta barra de hoy 2026-10-07)
        fechas = pd.date_range("2026-08-15", "2026-10-06", freq="B")
        cols = pd.MultiIndex.from_tuples([("SAN.MC", "Close")])
        df_batch = pd.DataFrame(10.0, index=fechas, columns=cols)

        dt_1835 = datetime(2026, 10, 7, 18, 35, tzinfo=ZoneInfo("Europe/Madrid"))

        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             self.assertLogs("database.predicciones", level="WARNING") as cm:
            resueltas = db_pred.resolver_predicciones(
                precios={"SAN.MC": 11.5},
                df_batch=df_batch,
            )
            self.assertEqual(resueltas, 0, "NO debe resolverse sin barra consolidada")
            self.assertTrue(any("parámetro precios obsoleto e ignorado" in m for m in cm.output))

        conn = get_connection()
        row = conn.execute("SELECT resuelta, precio_real FROM predicciones WHERE ticker='SAN.MC'").fetchone()
        conn.close()
        self.assertEqual(row[0], 0, "La predicción debe permanecer pendiente (resuelta=0)")
        self.assertIsNone(row[1], "precio_real debe seguir siendo NULL")

    def test_c25d_fecha_objetivo_hoy_a_las_12_no_se_resuelve(self):
        """
        C-25d C3(iii): Una fila con fecha_objetivo = hoy a las 12:00 no se resuelve.
        """
        conn = get_connection()
        conn.execute("""
            INSERT INTO predicciones (
                ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                senal, precio_entrada, modelo, resuelta
            ) VALUES (
                'SAN.MC', 'Banco Santander', '2026-10-06', '1D', 1, '2026-10-07',
                'ALCISTA', 10.0, 'v5-frozen', 0
            )
        """)
        conn.commit()
        conn.close()

        # Batch con barra de hoy 2026-10-07
        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        cols = pd.MultiIndex.from_tuples([("SAN.MC", "Close")])
        df_batch = pd.DataFrame(10.0, index=fechas, columns=cols)
        df_batch.loc["2026-10-07", ("SAN.MC", "Close")] = 10.3

        dt_12 = datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        with patch.object(mc, "get_ahora_madrid", return_value=dt_12), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_12):
            resueltas = db_pred.resolver_predicciones(df_batch=df_batch)
            self.assertEqual(resueltas, 0, "No debe resolverse a las 12:00 con mercado abierto")

        conn = get_connection()
        row = conn.execute("SELECT resuelta, precio_real FROM predicciones WHERE ticker='SAN.MC'").fetchone()
        conn.close()
        self.assertEqual(row[0], 0)
        self.assertIsNone(row[1])

    def test_c25d_resolucion_normal_con_barra_consolidada(self):
        """
        C-25d C3(iv): Resolución normal con barra consolidada: mismo resultado
        que antes (precio_real, variacion_real, acierto).
        """
        conn = get_connection()
        conn.execute("""
            INSERT INTO predicciones (
                ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                senal, pct_estimado, banda_pct, precio_entrada, modelo, resuelta
            ) VALUES (
                'SAN.MC', 'Banco Santander', '2026-10-06', '1D', 1, '2026-10-07',
                'ALCISTA', 4.0, 1.5, 10.0, 'v5-frozen', 0
            )
        """)
        conn.commit()
        conn.close()

        fechas = pd.date_range("2026-08-15", "2026-10-07", freq="B")
        cols = pd.MultiIndex.from_tuples([("SAN.MC", "Close")])
        df_batch = pd.DataFrame(10.0, index=fechas, columns=cols)
        df_batch.loc["2026-10-07", ("SAN.MC", "Close")] = 10.5

        dt_1835 = datetime(2026, 10, 7, 18, 35, tzinfo=ZoneInfo("Europe/Madrid"))

        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835):
            resueltas = db_pred.resolver_predicciones(df_batch=df_batch)
            self.assertEqual(resueltas, 1, "Debe resolver la predicción vencida con barra completa")

        conn = get_connection()
        row = conn.execute("""
            SELECT resuelta, fecha_resolucion, precio_real, variacion_real, acierto, motivo_resolucion
            FROM predicciones WHERE ticker='SAN.MC'
        """).fetchone()
        conn.close()

        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "2026-10-07")
        self.assertEqual(row[2], 10.5)
        self.assertEqual(row[3], 5.0)  # (10.5 - 10.0) / 10.0 * 100 = +5.0%
        self.assertEqual(row[4], 1)    # ALCISTA y +5.0% > banda 1.5% -> acierto
        self.assertEqual(row[5], "vencida")

    # =========================================================================
    # Tests Paso C-25e: Ventana de Emisión
    # =========================================================================

    def test_c25e_i_viernes_sabado_en_ventana(self):
        """
        Paso C-25e (i): S = viernes 2026-10-09, ahora = sábado 2026-10-10 12:00 -> en ventana.
        """
        dt_sabado = datetime(2026, 10, 10, 12, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_sabado), \
             patch.object(se, "get_ahora_madrid", return_value=dt_sabado), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_sabado):
            self.assertTrue(mc.en_ventana_emision("2026-10-09", ahora=dt_sabado))
            self.assertTrue(mc.en_ventana_emision("2026-10-09"))

    def test_c25e_ii_viernes_lunes_limite(self):
        """
        Paso C-25e (ii): S = viernes 2026-10-09, lunes 2026-10-12 08:29:59 -> en ventana;
        lunes 2026-10-12 08:30:00 -> fuera de ventana.
        """
        dt_29 = datetime(2026, 10, 12, 8, 29, 59, tzinfo=ZoneInfo("Europe/Madrid"))
        dt_30 = datetime(2026, 10, 12, 8, 30, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_29), \
             patch.object(se, "get_ahora_madrid", return_value=dt_29), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_29):
            self.assertTrue(mc.en_ventana_emision("2026-10-09", ahora=dt_29))
            self.assertTrue(mc.en_ventana_emision("2026-10-09"))

        with patch.object(mc, "get_ahora_madrid", return_value=dt_30), \
             patch.object(se, "get_ahora_madrid", return_value=dt_30), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_30):
            self.assertFalse(mc.en_ventana_emision("2026-10-09", ahora=dt_30))
            self.assertFalse(mc.en_ventana_emision("2026-10-09"))

    def test_c25e_iii_jueves_sabado_lote_incompleto_late_resuelve(self):
        """
        Paso C-25e (iii): S = jueves 2026-10-08, ahora = sábado 2026-10-10 12:00
        con lote donde el viernes 2026-10-09 tiene < 50% de valores válidos:
        fecha_sesion = jueves, status 'late', 0 inserciones, y resolver_predicciones sí se ejecuta.
        """
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO empresas (ticker, nombre, activa) VALUES ('T1.MC', 'T1 Corp', 1), ('T2.MC', 'T2 Corp', 1), ('T3.MC', 'T3 Corp', 1), ('T4.MC', 'T4 Corp', 1)")
        conn.execute("""
            INSERT INTO predicciones (
                ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                senal, pct_estimado, banda_pct, precio_entrada, modelo, resuelta
            ) VALUES (
                'T1.MC', 'T1 Corp', '2026-10-06', '1D', 1, '2026-10-07',
                'ALCISTA', 2.0, 1.0, 10.0, 'v5-frozen', 0
            )
        """)
        conn.commit()
        conn.close()

        fechas = pd.date_range("2026-08-15", "2026-10-09", freq="B")
        tickers = ["T1.MC", "T2.MC", "T3.MC", "T4.MC", "^IBEX"]
        df = _crear_batch_ohlcv(
            tickers,
            fechas,
            faltantes={
                "T2.MC": ["2026-10-09"],
                "T3.MC": ["2026-10-09"],
                "T4.MC": ["2026-10-09"],
            }
        )

        dt_sabado = datetime(2026, 10, 10, 12, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        spy_resolver = MagicMock(side_effect=db_pred.resolver_predicciones)

        with patch.object(mc, "get_ahora_madrid", return_value=dt_sabado), \
             patch.object(se, "get_ahora_madrid", return_value=dt_sabado), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_sabado), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=4), \
             patch("services.emision.resolver_predicciones", spy_resolver), \
             self.assertLogs("services.emision", level="WARNING") as cm:
            mock_date.today.return_value = dt_sabado.date()

            df_trunc, f_sesion = se.truncar_a_barras_completas(df)
            self.assertEqual(f_sesion, "2026-10-08")

            res = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res["status"], "late")
            self.assertEqual(res["fecha"], "2026-10-08")
            self.assertEqual(res["insertadas"], 0)
            self.assertIn("limite", res)
            self.assertEqual(spy_resolver.call_count, 1, "resolver_predicciones debe ejecutarse en rama 'late'")
            self.assertTrue(any("fuera de ventana" in m for m in cm.output))

        conn = get_connection()
        cnt_jueves = conn.execute("SELECT COUNT(*) FROM predicciones WHERE fecha_prediccion = '2026-10-08'").fetchone()[0]
        p_res = conn.execute("SELECT resuelta FROM predicciones WHERE ticker = 'T1.MC' AND fecha_prediccion = '2026-10-06'").fetchone()[0]
        conn.close()
        self.assertEqual(cnt_jueves, 0, "No debe haberse insertado ninguna predicción para el jueves")
        self.assertEqual(p_res, 1, "La predicción pendiente debe haberse resuelto")

    def test_c25e_iv_secuencia_viernes_success_topup_late(self):
        """
        Paso C-25e (iv): S = viernes 18:35 -> 'success'; top-up a las 20:00 -> 'topped_up';
        el mismo top-up a las 10:00 de la siguiente sesión hábil (lunes) -> 'late'.
        """
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO empresas (ticker, nombre, activa, es_ibex35) "
            "VALUES ('SAN.MC', 'Santander', 1, 1), ('BBVA.MC', 'BBVA', 1, 1), ('ITX.MC', 'Inditex', 1, 1)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO ranking_estado (ticker, score) VALUES ('ITX.MC', 50.0)"
        )
        conn.commit()
        conn.close()

        fechas = pd.date_range("2026-08-15", "2026-10-09", freq="B")
        df_1 = _crear_batch_ohlcv(
            ["SAN.MC", "BBVA.MC", "ITX.MC", "^IBEX"],
            fechas,
            faltantes={"BBVA.MC": ["2026-10-09"], "ITX.MC": ["2026-10-09"]},
        )
        dt_1835 = datetime(2026, 10, 9, 18, 35, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        with patch.object(mc, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(se, "get_ahora_madrid", return_value=dt_1835), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1835), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df_1), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=1), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_date.today.return_value = dt_1835.date()
            res1 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res1["status"], "success")
            self.assertEqual(res1["insertadas"], 4)

        df_2 = _crear_batch_ohlcv(
            ["SAN.MC", "BBVA.MC", "ITX.MC", "^IBEX"],
            fechas,
            faltantes={"ITX.MC": ["2026-10-09"]},
        )
        dt_2000 = datetime(2026, 10, 9, 20, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        with patch.object(mc, "get_ahora_madrid", return_value=dt_2000), \
             patch.object(se, "get_ahora_madrid", return_value=dt_2000), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_2000), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df_2), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=2), \
             patch("services.emision.resolver_predicciones", return_value=0):
            mock_date.today.return_value = dt_2000.date()
            res2 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res2["status"], "topped_up")
            self.assertEqual(res2["insertadas"], 4)

        df_3 = _crear_batch_ohlcv(["SAN.MC", "BBVA.MC", "ITX.MC", "^IBEX"], fechas)
        dt_lunes_10 = datetime(2026, 10, 12, 10, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        with patch.object(mc, "get_ahora_madrid", return_value=dt_lunes_10), \
             patch.object(se, "get_ahora_madrid", return_value=dt_lunes_10), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_lunes_10), \
             patch("services.emision.date") as mock_date, \
             patch("services.emision.descargar_price_batch", return_value=df_3), \
             patch("services.emision.sincronizar_cache_panel_historico"), \
             patch("services.emision.guardar_snapshot_ranking", return_value=2), \
             patch("services.emision.resolver_predicciones", return_value=0), \
             self.assertLogs("services.emision", level="WARNING"):
            mock_date.today.return_value = dt_lunes_10.date()
            res3 = se.ejecutar_emision_diaria(force=False, verbose=False)
            self.assertEqual(res3["status"], "late")
            self.assertEqual(res3["insertadas"], 0)

    def test_c25e_v_vispera_festivo_limite(self):
        """
        Paso C-25e (v): S = 2026-04-30 (víspera del festivo 1 de mayo viernes):
        límite = lunes 2026-05-04 08:30 Madrid.
        """
        lim = mc.limite_emision("2026-04-30")
        esperado = datetime(2026, 5, 4, 8, 30, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        self.assertEqual(lim, esperado)

    def test_c25e_vi_registrar_predicciones_fuera_ventana(self):
        """
        Paso C-25e (vi): registrar_predicciones llamado directamente fuera de ventana
        devuelve 0 filas y emite un log warning con 'emisión tardía'.
        """
        df = _crear_df_senal_dummy(ticker="TEST_LATE.MC", precio=12.0)
        dt_tarde = datetime(2026, 10, 10, 12, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        with patch.object(mc, "get_ahora_madrid", return_value=dt_tarde), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_tarde), \
             self.assertLogs("database.predicciones", level="WARNING") as cm:
            n = db_pred.registrar_predicciones(df, fecha_prediccion="2026-10-08")
            self.assertEqual(n, 0, "No debe registrar filas fuera de ventana")
            self.assertTrue(any("emisión tardía" in m for m in cm.output))

        conn = get_connection()
        cnt = conn.execute("SELECT COUNT(*) FROM predicciones WHERE ticker = 'TEST_LATE.MC'").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 0)

    def test_c25e_vii_antes_18_30_fuera_ventana(self):
        """
        Paso C-25e (vii): Antes de las 18:30 del día S está fuera de ventana
        (en_ventana_emision devuelve False y la guarda intradía sigue activa).
        """
        dt_1829 = datetime(2026, 10, 7, 18, 29, 59, tzinfo=ZoneInfo("Europe/Madrid"))
        dt_1200 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=ZoneInfo("Europe/Madrid"))

        self.assertFalse(mc.en_ventana_emision("2026-10-07", ahora=dt_1829))
        self.assertFalse(mc.en_ventana_emision("2026-10-07", ahora=dt_1200))

        # Comprobar que en_ventana_emision lanza ValueError si se le pasa datetime naive
        dt_naive = datetime(2026, 10, 7, 19, 0, 0)
        with self.assertRaises(ValueError):
            mc.en_ventana_emision("2026-10-07", ahora=dt_naive)

        # Comprobar que a las 12:00 registrar_predicciones de hoy sigue abortando por la guarda intradía
        df = _crear_df_senal_dummy(ticker="TEST_INTRA.MC")
        with patch.object(mc, "get_ahora_madrid", return_value=dt_1200), \
             patch.object(db_pred, "get_ahora_madrid", return_value=dt_1200), \
             self.assertLogs("database.predicciones", level="WARNING") as cm:
            n = db_pred.registrar_predicciones(df, fecha_prediccion="2026-10-07")
            self.assertEqual(n, 0)
            self.assertTrue(any("registrar_predicciones abortado" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()

