"""
Tests para el Paso C-04: Historial inmutable y lectura unificada de archivada
=============================================================================
Pruebas sobre base de datos SQLite temporal sintetica, sin red y con reloj
parcheado en utils.market_calendar, services.emision y database.predicciones:
  (a) Con filas sinteticas con archivada = 0 y archivada = 1, obtener_resueltas,
      obtener_pendientes (con solo_vigentes=True y False), obtener_historial
      (con incluir_legacy=True y False), obtener_metricas y
      obtener_calibracion_historica devuelven y contabilizan ambos conjuntos
      de forma identica y coherente.
  (b) Llamar a limpiar_predicciones, limpiar_resueltas y recalcular_predicciones
      sin STOCKLAB_DEV=1 lanza PermissionError y no modifica ni borra ninguna
      fila en la BD temporal.
  (c) Con unittest.mock.patch.dict(os.environ, {"STOCKLAB_DEV": "1"}), la guarda
      de desarrollo permite la llamada en la BD temporal.
  (d) En views/ia.py, _dialog_limpiar_historial y los botones de limpiar/archivar
      historial estan ocultos salvo STOCKLAB_DEV=1.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database.connection
from database import predicciones as db_pred
from database.connection import get_connection
import services.emision as se
import utils.market_calendar as mc
import views.ia as via

MADRID_TZ = ZoneInfo("Europe/Madrid")


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Parchea get_ahora_madrid en los tres espacios de nombres requeridos."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


class TestC04HistorialInmutable(unittest.TestCase):
    """Suite de pruebas para C-04 sobre BD temporal sintetica."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_c04_")
        self.temp_db = os.path.join(self.temp_dir, "test_synthetic.db")
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

    def _poblar_escenario_mixto(self):
        """
        Inserta un conjunto controlado de filas con archivada = 0 y archivada = 1
        cubriendo resueltas (v5-frozen y legacy-pre-freeze, normales e invalidadas),
        pendientes (varias fechas para probar solo_vigentes=True/False) y sin_cotizacion.
        """
        conn = get_connection()
        try:
            filas = [
                # 1. Resueltas v5-frozen 1D: una archivada=0 (acierto=1) y una archivada=1 (acierto=0)
                ("SAN.MC", "Santander", "2026-10-01", "1D", 1, "2026-10-02", "ALCISTA",
                 1.5, 2.0, 10.0, "v5-frozen", 1, "2026-10-02", 10.2, 2.0, 1, 0.5, "vencida", 80.0, 0),
                ("BBVA.MC", "BBVA", "2026-10-02", "1D", 1, "2026-10-05", "ALCISTA",
                 1.5, 2.0, 8.0, "v5-frozen", 1, "2026-10-05", 7.8, -2.5, 0, 4.0, "vencida", 10.0, 1),
                # 2. Resueltas invalidada v5-frozen 3D: archivada=1
                ("ITX.MC", "Inditex", "2026-10-01", "3D", 3, "2026-10-06", "BAJISTA",
                 -2.0, 2.5, 40.0, "v5-frozen", 1, "2026-10-06", 41.0, 2.5, 0, 4.5, "invalidada", 5.0, 1),
                # 3. Resueltas legacy-pre-freeze 1D: una archivada=0 y una archivada=1
                ("IBE.MC", "Iberdrola", "2026-09-10", "1D", 1, "2026-09-11", "ALCISTA",
                 1.0, 1.5, 11.0, "legacy-pre-freeze", 1, "2026-09-11", 11.2, 1.8, 1, 0.8, "vencida", 75.0, 0),
                ("REP.MC", "Repsol", "2026-09-11", "1D", 1, "2026-09-14", "BAJISTA",
                 -1.0, 1.5, 13.0, "legacy-pre-freeze", 1, "2026-09-14", 12.7, -2.3, 1, 1.3, "vencida", 70.0, 1),
                # 4. Sin cotizacion (resuelta = 2) 1D: una archivada=0 y una archivada=1
                ("SC0.MC", "SinCot0", "2026-10-01", "1D", 1, "2026-10-02", "LATERAL",
                 0.0, 1.0, 5.0, "v5-frozen", 2, "2026-10-09", None, None, None, None, "sin_cotizacion", None, 0),
                ("SC1.MC", "SinCot1", "2026-10-01", "1D", 1, "2026-10-02", "LATERAL",
                 0.0, 1.0, 5.0, "v5-frozen", 2, "2026-10-09", None, None, None, None, "sin_cotizacion", None, 1),
                # 5. Pendientes (resuelta = 0) 1D:
                #    - P_A.MC tiene 2 emisiones (2026-10-05 archivada=0 y 2026-10-06 archivada=1 -> la vigente es archivada=1)
                #    - P_B.MC tiene 1 emision (2026-10-06 archivada=0)
                ("P_A.MC", "Pendiente A", "2026-10-05", "1D", 1, "2026-10-06", "ALCISTA",
                 1.2, 1.5, 20.0, "v5-frozen", 0, None, None, None, None, None, None, None, 0),
                ("P_A.MC", "Pendiente A", "2026-10-06", "1D", 1, "2026-10-07", "BAJISTA",
                 -1.5, 1.5, 21.0, "v5-frozen", 0, None, None, None, None, None, None, None, 1),
                ("P_B.MC", "Pendiente B", "2026-10-06", "1D", 1, "2026-10-07", "LATERAL",
                 0.1, 1.2, 15.0, "v5-frozen", 0, None, None, None, None, None, None, None, 0),
            ]
            conn.executemany(
                """
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, pct_estimado, banda_pct, precio_entrada, modelo, resuelta,
                    fecha_resolucion, precio_real, variacion_real, acierto, error_pct,
                    motivo_resolucion, score_calidad, archivada
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                filas,
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_lecturas_y_metricas_unifican_archivada_0_y_1(self):
        """
        (a) Verifica que obtener_resueltas, obtener_pendientes (solo_vigentes=True/False),
        obtener_historial (incluir_legacy=True/False), obtener_metricas y
        obtener_calibracion_historica tratan archivada=0 y archivada=1 de forma identica.
        """
        dt_fijo = datetime(2026, 10, 10, 12, 0, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_fijo):
            self._poblar_escenario_mixto()

            # 1. obtener_resueltas (sin legacy y con legacy)
            df_res_noleg = db_pred.obtener_resueltas(horizonte="Todos", limite=100, incluir_legacy=False)
            self.assertEqual(len(df_res_noleg), 3)
            self.assertEqual(set(df_res_noleg["ticker"]), {"SAN.MC", "BBVA.MC", "ITX.MC"})
            self.assertEqual(set(df_res_noleg["archivada"]), {0, 1})

            df_res_1d = db_pred.obtener_resueltas(horizonte="1D", limite=100, incluir_legacy=False)
            self.assertEqual(len(df_res_1d), 2)
            self.assertEqual(set(df_res_1d["ticker"]), {"SAN.MC", "BBVA.MC"})

            df_res_leg = db_pred.obtener_resueltas(horizonte="Todos", limite=100, incluir_legacy=True)
            self.assertEqual(len(df_res_leg), 5)
            self.assertEqual(set(df_res_leg["ticker"]), {"SAN.MC", "BBVA.MC", "ITX.MC", "IBE.MC", "REP.MC"})

            # 2. obtener_pendientes con solo_vigentes=True y solo_vigentes=False
            df_pend_vig = db_pred.obtener_pendientes(horizonte="1D", solo_vigentes=True, limite=100)
            self.assertEqual(len(df_pend_vig), 2)
            mapa_vig = dict(zip(df_pend_vig["ticker"], df_pend_vig["fecha_prediccion"]))
            self.assertEqual(mapa_vig["P_A.MC"], "2026-10-06", "La emision mas reciente (archivada=1) debe ser la vigente")
            self.assertEqual(mapa_vig["P_B.MC"], "2026-10-06")

            df_pend_todas = db_pred.obtener_pendientes(horizonte="1D", solo_vigentes=False, limite=100)
            self.assertEqual(len(df_pend_todas), 3, "Con solo_vigentes=False debe devolver las 3 pendientes (2 con archivada=0 y 1 con archivada=1)")

            # 3. obtener_historial con incluir_legacy=False y True
            df_hist_noleg = db_pred.obtener_historial(limite=100, solo_resueltas=False, incluir_legacy=False)
            self.assertEqual(len(df_hist_noleg), 8, "Debe incluir las 8 filas no-legacy (archivada=0 y archivada=1)")

            df_hist_leg = db_pred.obtener_historial(limite=100, solo_resueltas=False, incluir_legacy=True)
            self.assertEqual(len(df_hist_leg), 10, "Debe incluir las 10 filas totales de la tabla")

            # 4. obtener_metricas y equivalencia exacta si todas tuvieran archivada=0
            m_con_arch = db_pred.obtener_metricas(horizonte="Todos", incluir_legacy=False)
            m_1d_con_arch = db_pred.obtener_metricas(horizonte="1D", incluir_legacy=True)

            self.assertEqual(m_con_arch["total_db"], 10)
            self.assertEqual(m_con_arch["resueltas"], 3)
            self.assertEqual(m_con_arch["invalidadas"], 1)
            self.assertEqual(m_con_arch["sin_cotizacion"], 2)
            self.assertEqual(m_con_arch["legacy_resueltas"], 2)
            self.assertEqual(m_con_arch["pendientes_totales"], 3)
            self.assertEqual(m_con_arch["vigentes"], 2)
            self.assertEqual(m_con_arch["por_horizonte"]["1D"]["n"], 2)
            self.assertEqual(m_con_arch["por_horizonte"]["3D"]["n"], 1)

            # Comprobar que poner archivada=0 en todas las filas produce exactamente las mismas metricas
            conn = get_connection()
            try:
                conn.execute("UPDATE predicciones SET archivada = 0")
                conn.commit()
            finally:
                conn.close()

            m_sin_arch = db_pred.obtener_metricas(horizonte="Todos", incluir_legacy=False)
            m_1d_sin_arch = db_pred.obtener_metricas(horizonte="1D", incluir_legacy=True)
            self.assertEqual(m_con_arch, m_sin_arch)
            self.assertEqual(m_1d_con_arch, m_1d_sin_arch)

    def test_a_calibracion_historica_coherente_archivada_0_y_1(self):
        """
        Verifica que obtener_calibracion_historica devuelve resultados identicos
        para filas con archivada = 0 y archivada = 1 tanto con FEEDBACK_ACTIVO=False
        como con FEEDBACK_ACTIVO=True.
        """
        conn = get_connection()
        try:
            for i in range(35):
                arch = 1 if i % 2 == 0 else 0
                acierto = 1 if i < 21 else 0
                conn.execute(
                    """
                    INSERT INTO predicciones (
                        ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                        senal, precio_entrada, modelo, resuelta, acierto, archivada
                    ) VALUES (?, 'Cal Corp', ?, '1D', 1, '2026-10-20', 'ALCISTA', 10.0, 'v5-frozen', 1, ?, ?)
                    """,
                    ("CAL.MC", f"2026-08-{(i % 28) + 1:02d}-{i}", acierto, arch),
                )
            conn.commit()
        finally:
            conn.close()

        # Con FEEDBACK_ACTIVO = False (por defecto)
        cal_off = db_pred.obtener_calibracion_historica("CAL.MC", "1D")
        self.assertEqual(cal_off["muestras"], 0)
        self.assertEqual(cal_off["factor_confianza"], 1.0)

        # Con FEEDBACK_ACTIVO = True (verifica la consulta SQL interna)
        with patch.object(db_pred, "FEEDBACK_ACTIVO", True):
            cal_mixto = db_pred.obtener_calibracion_historica("CAL.MC", "1D")
            self.assertEqual(cal_mixto["muestras"], 35)
            self.assertEqual(cal_mixto["tasa_acierto"], round(100.0 * 21 / 35, 1))

            conn = get_connection()
            try:
                conn.execute("UPDATE predicciones SET archivada = 0 WHERE ticker = 'CAL.MC'")
                conn.commit()
            finally:
                conn.close()

            cal_todo_0 = db_pred.obtener_calibracion_historica("CAL.MC", "1D")
            self.assertEqual(cal_mixto, cal_todo_0)

    def test_b_funciones_purga_bloqueadas_sin_stocklab_dev(self):
        """
        (b) Llamar a limpiar_predicciones, limpiar_resueltas y recalcular_predicciones
        sin STOCKLAB_DEV=1 lanza PermissionError y no modifica ni borra ninguna fila.
        """
        self._poblar_escenario_mixto()
        conn = get_connection()
        try:
            # Anadir tambien una pendiente legacy para tentar a recalcular_predicciones
            conn.execute(
                """
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, precio_entrada, modelo, resuelta, archivada
                ) VALUES ('LEG_PEND.MC', 'Legacy Pend', '2026-09-01', '1D', 1, '2026-09-02', 'ALCISTA', 10.0, 'legacy-pre-freeze', 0, 0)
                """
            )
            conn.commit()
            rows_antes = [tuple(r) for r in conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()]
            hash_antes = hashlib.sha256(repr(rows_antes).encode("utf-8")).hexdigest()
        finally:
            conn.close()

        for env_val in (None, "", "0", "true", "2"):
            env_patch = {} if env_val is None else {"STOCKLAB_DEV": env_val}
            with patch.dict(os.environ, env_patch, clear=False):
                if env_val is None:
                    os.environ.pop("STOCKLAB_DEV", None)

                with self.assertRaises(PermissionError):
                    db_pred.limpiar_predicciones(horizonte="1D", solo_resueltas=True)
                with self.assertRaises(PermissionError):
                    db_pred.limpiar_predicciones(horizonte=None, solo_resueltas=False)
                with self.assertRaises(PermissionError):
                    db_pred.limpiar_resueltas(horizonte="1D")
                with self.assertRaises(PermissionError):
                    db_pred.recalcular_predicciones(horizonte="1D")

        conn = get_connection()
        try:
            rows_despues = [tuple(r) for r in conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()]
            hash_despues = hashlib.sha256(repr(rows_despues).encode("utf-8")).hexdigest()
        finally:
            conn.close()

        self.assertEqual(len(rows_despues), len(rows_antes))
        self.assertEqual(hash_despues, hash_antes, "Ninguna fila debe modificarse ni borrarse sin STOCKLAB_DEV=1")

    def test_c_guarda_desarrollo_permite_llamada_con_stocklab_dev_1(self):
        """
        (c) Con unittest.mock.patch.dict(os.environ, {"STOCKLAB_DEV": "1"}),
        la guarda de desarrollo permite la llamada en la BD temporal.
        """
        self._poblar_escenario_mixto()
        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, precio_entrada, modelo, resuelta, archivada
                ) VALUES ('LEG_PEND.MC', 'Legacy Pend', '2026-09-01', '1D', 1, '2026-09-02', 'ALCISTA', 10.0, 'legacy-pre-freeze', 0, 0)
                """
            )
            conn.commit()
        finally:
            conn.close()

        with patch.dict(os.environ, {"STOCKLAB_DEV": "1"}):
            n_res_arch = db_pred.limpiar_resueltas(horizonte="1D")
            self.assertGreaterEqual(n_res_arch, 1)

            n_all_arch = db_pred.limpiar_predicciones(horizonte="Todos", solo_resueltas=False)
            self.assertGreaterEqual(n_all_arch, 1)

            conn = get_connection()
            try:
                rows_antes_recalc = [
                    tuple(r) for r in conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
                ]
            finally:
                conn.close()

            n_recalc = db_pred.recalcular_predicciones(horizonte="1D")
            self.assertEqual(
                n_recalc,
                0,
                "Con C-08 activo, trg_predicciones_no_delete impide borrar filas fisicas incluso con STOCKLAB_DEV=1",
            )

            conn = get_connection()
            try:
                rows_despues_recalc = [
                    tuple(r) for r in conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
                ]
            finally:
                conn.close()
            self.assertEqual(rows_despues_recalc, rows_antes_recalc)

    def test_d_ui_views_ia_oculta_botones_y_dialogo_salvo_stocklab_dev_1(self):
        """
        Verifica que en views/ia.py _dialog_limpiar_historial retorna de inmediato
        sin renderizar ni llamar a limpiar_resueltas cuando STOCKLAB_DEV != '1'.
        """
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCKLAB_DEV", None)
            with patch("views.ia.st") as mock_st, \
                 patch.object(db_pred, "limpiar_resueltas") as mock_limpiar:
                fn = getattr(via._dialog_limpiar_historial, "__wrapped__", via._dialog_limpiar_historial)
                fn("1D", "1 Dia", 5, 10)
                mock_limpiar.assert_not_called()
                mock_st.markdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
