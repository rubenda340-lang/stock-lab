"""
Tests unitarios para Paso C-14: Tabla Completa de Referencias Ingenuas
======================================================================
Verifica sobre BD temporal sintetica, sin red y con reloj parcheado en
utils.market_calendar, services.emision y database.predicciones:
  (a) Identidad algebraica de las 3 referencias constantes:
      tasa(Siempre alcista) + tasa(Siempre bajista) + tasa(Siempre lateral) == 100.0%
      y en signo (sin variaciones 0.0): tasa(alc) + tasa(baj) == 100.0%,
      mientras Siempre lateral tiene n_dir == 0 y tasa_signo is None.
  (b) Cero look-ahead en "Continuacion del ultimo movimiento" y "Direccion del IBEX":
      alterar precios en fechas posteriores (fecha > fecha_prediccion) NO modifica
      en absoluto las predicciones emitidas ni las metricas de la referencia.
  (c) Comparacion exacta contra el Motor en escenario alcista (+2.0% diario):
      Motor, Siempre alcista, Continuacion e IBEX obtienen 100.0%,
      Siempre bajista y Siempre lateral obtienen 0.0%,
      y deltas vs motor calculados con signo exacto.
  (d) Integracion con C-09 y C-13:
      cada fila de la tabla C-14 incluye etiqueta_muestra e inferencia_3clases /
      inferencia_signo agregada por fecha_prediccion.
  (e) Comprobacion en solo lectura contra stocklab.db:
      obtener_tabla_referencias_ingenuas devuelve las 6 filas completas
      (Motor + 5 referencias) para incluir_legacy=False y True sin modificar
      stocklab.db (total_changes == 0 y hashes intactos).
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
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


def _sha256_archivo(ruta: str) -> str:
    with open(ruta, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TestC14ReferenciasIngenuas(unittest.TestCase):
    """Bateria de pruebas unitarias para la Tabla Completa de Referencias Ingenuas [C-14]."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_c14.db")
        self.patcher = patch.object(db_conn, "DB_PATH", self.db_path)
        self.patcher.start()
        init_db()
        db_pred.init_predicciones_table()
        db_pred.init_evaluaciones_table()
        self.fijo_dt = datetime(2026, 10, 30, 10, 0, tzinfo=MADRID_TZ)

    def tearDown(self):
        self.patcher.stop()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    def _insertar_fila(
        self,
        conn: sqlite3.Connection,
        ticker: str = "SAN.MC",
        fecha_prediccion: str = "2026-10-15",
        horizonte: str = "1D",
        dias: int = 1,
        senal: str = "ALCISTA",
        variacion_real: float = 1.5,
        acierto: int = 1,
        modelo: str = "v5-frozen",
        comp_momentum: float = 0.5,
        marea: str = "ALCISTA",
        clase_real_v1: str = "ALCISTA",
        clase_real_v2: str = "ALCISTA",
        umbral_v2: float = 0.8,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO predicciones (
                ticker, fecha_prediccion, horizonte, dias, fecha_objetivo,
                senal, precio_entrada, precio_real, variacion_real, acierto,
                resuelta, modelo, comp_momentum, marea, sigma
            ) VALUES (?, ?, ?, ?, ?, ?, 10.0, 10.15, ?, ?, 1, ?, ?, ?, 0.015)
            """,
            (ticker, fecha_prediccion, horizonte, dias, "2026-10-16", senal, variacion_real, acierto, modelo, comp_momentum, marea),
        )
        pid = cur.lastrowid
        conn.execute(
            """
            INSERT INTO evaluaciones (
                prediccion_id, regla, precio_entrada_eval, precio_salida_eval,
                variacion_pct, umbral_aplicado, clase_real, acierto, acierto_signo, evaluada_en
            ) VALUES (?, 'c10_precio_cierre', 10.0, 10.15, ?, 0.8, ?, ?, ?, '2026-10-16T18:30:00')
            """,
            (pid, variacion_real, clase_real_v1, acierto, 1 if variacion_real > 0 else 0),
        )
        conn.execute(
            """
            INSERT INTO evaluaciones (
                prediccion_id, regla, precio_entrada_eval, precio_salida_eval,
                variacion_pct, umbral_aplicado, clase_real, acierto, acierto_signo, evaluada_en
            ) VALUES (?, 'c12_vol_precio_cierre', 10.0, 10.15, ?, ?, ?, ?, ?, '2026-10-16T18:30:00')
            """,
            (pid, variacion_real, umbral_v2, clase_real_v2, acierto, 1 if variacion_real > 0 else 0),
        )
        return pid

    def test_a_identidad_algebraica_referencias_constantes(self):
        """
        (a) Identidad algebraica de las 3 referencias constantes:
        Suma de acierto en 3 clases (Siempre alcista + Siempre bajista + Siempre lateral) == 100.0%.
        Y en signo (sin variaciones 0.0): Siempre alcista + Siempre bajista == 100.0%,
        mientras Siempre lateral tiene n_dir == 0 y tasa_signo is None.
        """
        with _reloj_madrid(self.fijo_dt):
            conn = get_connection()
            # Insertar 50 filas con distribucion variada:
            # 15 ALCISTAS (var > +0.8%), 15 BAJISTAS (var < -0.8%), 20 LATERALES (abs(var) <= 0.8%)
            # Ninguna variacion exactamente 0.0
            for i in range(15):
                self._insertar_fila(conn, ticker=f"T{i}.MC", fecha_prediccion="2026-10-15",
                                    variacion_real=1.5, clase_real_v1="ALCISTA", clase_real_v2="ALCISTA")
            for i in range(15):
                self._insertar_fila(conn, ticker=f"T{i+15}.MC", fecha_prediccion="2026-10-15",
                                    variacion_real=-1.2, clase_real_v1="BAJISTA", clase_real_v2="BAJISTA")
            for i in range(10):
                self._insertar_fila(conn, ticker=f"T{i+30}.MC", fecha_prediccion="2026-10-15",
                                    variacion_real=0.3, clase_real_v1="LATERAL", clase_real_v2="LATERAL")
            for i in range(10):
                self._insertar_fila(conn, ticker=f"T{i+40}.MC", fecha_prediccion="2026-10-15",
                                    variacion_real=-0.4, clase_real_v1="LATERAL", clase_real_v2="LATERAL")
            conn.commit()

            res = db_pred.obtener_tabla_referencias_ingenuas(conn=conn)
            conn.close()
            tabla = {r["codigo"]: r for r in res["tabla"]}

            t_alc_3c = tabla["siempre_alcista"]["tasa_acierto_3clases"]
            t_baj_3c = tabla["siempre_bajista"]["tasa_acierto_3clases"]
            t_lat_3c = tabla["siempre_lateral"]["tasa_acierto_3clases"]

            # Suma de 3 clases == 100%
            suma_3c = round(t_alc_3c + t_baj_3c + t_lat_3c, 1)
            self.assertAlmostEqual(suma_3c, 100.0, delta=0.2)
            self.assertEqual(tabla["siempre_alcista"]["aciertos_3clases"], 15)
            self.assertEqual(tabla["siempre_bajista"]["aciertos_3clases"], 15)
            self.assertEqual(tabla["siempre_lateral"]["aciertos_3clases"], 20)
            self.assertEqual(res["n_senales"], 50)

            # En signo:
            # Positivas: 15 + 10 = 25 subidas (50.0%)
            # Negativas: 15 + 10 = 25 bajadas (50.0%)
            t_alc_sig = tabla["siempre_alcista"]["tasa_acierto_signo"]
            t_baj_sig = tabla["siempre_bajista"]["tasa_acierto_signo"]
            self.assertAlmostEqual(t_alc_sig + t_baj_sig, 100.0, delta=0.2)
            self.assertEqual(t_alc_sig, 50.0)
            self.assertEqual(t_baj_sig, 50.0)

            # Siempre lateral en signo tiene n_dir == 0 y tasa is None
            self.assertEqual(tabla["siempre_lateral"]["n_direccionales"], 0)
            self.assertIsNone(tabla["siempre_lateral"]["tasa_acierto_signo"])
            self.assertIsNone(tabla["siempre_lateral"]["delta_vs_motor_signo_pp"])

    def test_b_cero_look_ahead_continuacion_e_ibex(self):
        """
        (b) Cero look-ahead en "Continuacion del ultimo movimiento" y "Direccion del IBEX":
        Al modificar precios en fechas posteriores a fecha_prediccion (t > fecha_prediccion),
        las predicciones y metricas de Continuacion e IBEX son 100% identicas.
        """
        with _reloj_madrid(self.fijo_dt):
            conn = get_connection()
            # Emision en 2026-10-15
            self._insertar_fila(conn, ticker="SAN.MC", fecha_prediccion="2026-10-15",
                                dias=1, horizonte="1D", variacion_real=2.0, clase_real_v1="ALCISTA")
            conn.commit()

            # Precios historicos validos <= 2026-10-15
            fechas_pre = pd.date_range("2026-10-01", "2026-10-15", freq="B")
            df_base = pd.DataFrame(
                {
                    ("SAN.MC", "Close"): np.linspace(10.0, 11.0, len(fechas_pre)),
                    ("^IBEX", "Close"): np.linspace(11000.0, 11500.0, len(fechas_pre)),
                },
                index=fechas_pre,
            )

            res_antes = db_pred.obtener_tabla_referencias_ingenuas(conn=conn, df_precios_previos=df_base)
            tabla_antes = {r["codigo"]: r for r in res_antes["tabla"]}

            # Anadir fechas futuras (t > 2026-10-15) con variaciones extremas simuladas
            fechas_post = pd.date_range("2026-10-16", "2026-10-25", freq="B")
            df_futuro = pd.DataFrame(
                {
                    ("SAN.MC", "Close"): [2.0] * len(fechas_post),       # Crash post-emision
                    ("^IBEX", "Close"): [50000.0] * len(fechas_post),   # Explosion post-emision
                },
                index=fechas_post,
            )
            df_contaminado = pd.concat([df_base, df_futuro])

            res_despues = db_pred.obtener_tabla_referencias_ingenuas(conn=conn, df_precios_previos=df_contaminado)
            conn.close()
            tabla_despues = {r["codigo"]: r for r in res_despues["tabla"]}

            # Verificar que Continuacion y Direccion IBEX NO cambiaron
            for code in ("continuacion_ultimo_movimiento", "direccion_ibex"):
                self.assertEqual(
                    tabla_antes[code]["tasa_acierto_3clases"],
                    tabla_despues[code]["tasa_acierto_3clases"],
                    f"Look-ahead detectado en 3clases para {code}",
                )
                self.assertEqual(
                    tabla_antes[code]["tasa_acierto_signo"],
                    tabla_despues[code]["tasa_acierto_signo"],
                    f"Look-ahead detectado en signo para {code}",
                )
                self.assertEqual(
                    tabla_antes[code]["n_direccionales"],
                    tabla_despues[code]["n_direccionales"],
                )

    def test_c_comparacion_exacta_mercado_alcista(self):
        """
        (c) Comparacion exacta contra el Motor en escenario alcista (+2.0% diario):
        Motor, Siempre alcista, Continuacion e IBEX obtienen 100.0%,
        Siempre bajista y Siempre lateral obtienen 0.0%,
        y los deltas vs motor calculados con signo exacto.
        """
        with _reloj_madrid(self.fijo_dt):
            conn = get_connection()
            # Escenario alcista: todo sube +2.0% todos los dias
            # 5 fechas x 5 tickers = 25 filas
            fechas = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
            for f in fechas:
                for i in range(5):
                    self._insertar_fila(
                        conn,
                        ticker=f"T{i}.MC",
                        fecha_prediccion=f,
                        senal="ALCISTA",
                        variacion_real=2.0,
                        acierto=1,
                        comp_momentum=1.5,
                        marea="ALCISTA",
                        clase_real_v1="ALCISTA",
                        clase_real_v2="ALCISTA",
                    )
            conn.commit()

            # Precios que suben +2% diario
            idx_p = pd.date_range("2026-10-01", "2026-10-16", freq="B")
            precios_dict = {}
            for i in range(5):
                precios_dict[(f"T{i}.MC", "Close")] = [10.0 * (1.02 ** k) for k in range(len(idx_p))]
            precios_dict[("^IBEX", "Close")] = [10000.0 * (1.02 ** k) for k in range(len(idx_p))]
            df_alcista = pd.DataFrame(precios_dict, index=idx_p)

            res = db_pred.obtener_tabla_referencias_ingenuas(conn=conn, df_precios_previos=df_alcista)
            conn.close()
            tabla = {r["codigo"]: r for r in res["tabla"]}

            # Motor
            self.assertEqual(tabla["motor"]["tasa_acierto_3clases"], 100.0)
            self.assertEqual(tabla["motor"]["tasa_acierto_signo"], 100.0)
            self.assertEqual(tabla["motor"]["delta_vs_motor_3clases_pp"], 0.0)
            self.assertEqual(tabla["motor"]["delta_vs_motor_signo_pp"], 0.0)

            # Siempre alcista
            self.assertEqual(tabla["siempre_alcista"]["tasa_acierto_3clases"], 100.0)
            self.assertEqual(tabla["siempre_alcista"]["tasa_acierto_signo"], 100.0)
            self.assertEqual(tabla["siempre_alcista"]["delta_vs_motor_3clases_pp"], 0.0)
            self.assertEqual(tabla["siempre_alcista"]["delta_vs_motor_signo_pp"], 0.0)

            # Continuacion
            self.assertEqual(tabla["continuacion_ultimo_movimiento"]["tasa_acierto_3clases"], 100.0)
            self.assertEqual(tabla["continuacion_ultimo_movimiento"]["tasa_acierto_signo"], 100.0)
            self.assertEqual(tabla["continuacion_ultimo_movimiento"]["delta_vs_motor_3clases_pp"], 0.0)
            self.assertEqual(tabla["continuacion_ultimo_movimiento"]["delta_vs_motor_signo_pp"], 0.0)

            # Direccion del IBEX
            self.assertEqual(tabla["direccion_ibex"]["tasa_acierto_3clases"], 100.0)
            self.assertEqual(tabla["direccion_ibex"]["tasa_acierto_signo"], 100.0)
            self.assertEqual(tabla["direccion_ibex"]["delta_vs_motor_3clases_pp"], 0.0)
            self.assertEqual(tabla["direccion_ibex"]["delta_vs_motor_signo_pp"], 0.0)

            # Siempre bajista
            self.assertEqual(tabla["siempre_bajista"]["tasa_acierto_3clases"], 0.0)
            self.assertEqual(tabla["siempre_bajista"]["tasa_acierto_signo"], 0.0)
            self.assertEqual(tabla["siempre_bajista"]["delta_vs_motor_3clases_pp"], -100.0)
            self.assertEqual(tabla["siempre_bajista"]["delta_vs_motor_signo_pp"], -100.0)

            # Siempre lateral
            self.assertEqual(tabla["siempre_lateral"]["tasa_acierto_3clases"], 0.0)
            self.assertEqual(tabla["siempre_lateral"]["delta_vs_motor_3clases_pp"], -100.0)
            self.assertIsNone(tabla["siempre_lateral"]["tasa_acierto_signo"])
            self.assertIsNone(tabla["siempre_lateral"]["delta_vs_motor_signo_pp"])

            # Motor supera o empata a todas
            self.assertTrue(res["motor_supera_todas_3clases"])
            self.assertTrue(res["motor_supera_todas_signo"])

    def test_d_integracion_c09_y_c13(self):
        """
        (d) Integracion con C-09 y C-13:
        Cada fila incluye etiqueta_muestra e inferencia_3clases / inferencia_signo
        agregada por fecha_prediccion.
        """
        with _reloj_madrid(self.fijo_dt):
            conn = get_connection()
            for i, f in enumerate(["2026-10-12", "2026-10-13", "2026-10-14"]):
                self._insertar_fila(conn, ticker="TEF.MC", fecha_prediccion=f,
                                    variacion_real=1.0 if i % 2 == 0 else -1.0)
            conn.commit()

            res = db_pred.obtener_tabla_referencias_ingenuas(conn=conn)
            conn.close()
            self.assertIn("etiqueta_muestra", res)
            self.assertIn("3 señales", res["etiqueta_muestra"])
            self.assertIn("3 fechas", res["etiqueta_muestra"])

            for item in res["tabla"]:
                self.assertIn("etiqueta_muestra", item)
                self.assertIn("inferencia_3clases", item)
                inf_3c = item["inferencia_3clases"]
                self.assertIn("n_fechas", inf_3c)
                self.assertEqual(inf_3c["n_fechas"], 3)
                self.assertIn("muestra_suficiente", inf_3c)
                self.assertFalse(inf_3c["muestra_suficiente"])  # 3 < 30 fechas
                if item["n_direccionales"] > 0:
                    self.assertIn("inferencia_signo", item)
                    inf_sig = item["inferencia_signo"]
                    self.assertIn("n_fechas", inf_sig)

    def test_e_solo_lectura_stocklab_db(self):
        """
        (e) Comprobacion en solo lectura contra stocklab.db:
        Verifica que obtener_tabla_referencias_ingenuas devuelve las 6 filas completas
        para incluir_legacy=False y True sin modificar stocklab.db (total_changes == 0
        y hashes intactos).
        """
        if not os.path.exists(REAL_DB_PATH):
            self.skipTest("stocklab.db no encontrada en la raiz del proyecto")

        hash_db_antes = _sha256_archivo(REAL_DB_PATH)

        conn_ro = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        hash_inmut_antes = db_pred._calcular_sha256_inmutables(conn_ro)
        hash_res_antes = db_pred._calcular_sha256_resueltas(conn_ro)

        changes_antes = conn_ro.total_changes

        exp_n_v5 = conn_ro.execute(
            "SELECT COUNT(*) FROM predicciones WHERE resuelta = 1 AND modelo != 'legacy-pre-freeze'"
        ).fetchone()[0]
        exp_fechas_v5 = conn_ro.execute(
            "SELECT COUNT(DISTINCT fecha_prediccion) FROM predicciones WHERE resuelta = 1 AND modelo != 'legacy-pre-freeze'"
        ).fetchone()[0]
        exp_n_all = conn_ro.execute(
            "SELECT COUNT(*) FROM predicciones WHERE resuelta = 1"
        ).fetchone()[0]
        self.assertGreaterEqual(exp_n_v5, 2794)
        self.assertGreaterEqual(exp_fechas_v5, 10)
        self.assertGreaterEqual(exp_n_all, 3826)

        # 1. Ejecutar para v5-frozen (incluir_legacy=False)
        res_v5 = db_pred.obtener_tabla_referencias_ingenuas(incluir_legacy=False, conn=conn_ro)
        self.assertEqual(len(res_v5["tabla"]), 6)
        codigos_v5 = [x["codigo"] for x in res_v5["tabla"]]
        self.assertEqual(
            codigos_v5,
            ["motor", "siempre_alcista", "siempre_bajista", "siempre_lateral", "continuacion_ultimo_movimiento", "direccion_ibex"]
        )
        self.assertEqual(res_v5["n_senales"], exp_n_v5)
        self.assertEqual(res_v5["n_fechas"], exp_fechas_v5)

        # 2. Ejecutar con legacy (incluir_legacy=True)
        res_all = db_pred.obtener_tabla_referencias_ingenuas(incluir_legacy=True, conn=conn_ro)
        self.assertEqual(len(res_all["tabla"]), 6)
        self.assertEqual(res_all["n_senales"], exp_n_all)

        # 3. Comprobar que obtener_metricas tambien expone referencias_ingenuas_c14
        m_v5 = db_pred.obtener_metricas(incluir_legacy=False, conn=conn_ro)
        self.assertIn("referencias_ingenuas_c14", m_v5)
        self.assertEqual(len(m_v5["referencias_ingenuas_c14"]["tabla"]), 6)

        # 4. Verificar integridad y cero cambios
        changes_despues = conn_ro.total_changes
        self.assertEqual(changes_antes, changes_despues, "La BD recibio escrituras en ejecucion de solo lectura")

        hash_inmut_despues = db_pred._calcular_sha256_inmutables(conn_ro)
        hash_res_despues = db_pred._calcular_sha256_resueltas(conn_ro)
        conn_ro.close()

        hash_db_despues = _sha256_archivo(REAL_DB_PATH)

        self.assertEqual(hash_db_antes, hash_db_despues, "SHA256 de stocklab.db se modifico!")
        self.assertEqual(hash_inmut_antes, hash_inmut_despues, "SHA256_INMUTABLES_11 cambio!")
        self.assertEqual(hash_res_antes, hash_res_despues, "SHA256_RESUELTAS cambio!")


if __name__ == "__main__":
    unittest.main()
