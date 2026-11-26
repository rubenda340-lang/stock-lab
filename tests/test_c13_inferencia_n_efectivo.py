"""
Tests unitarios para Paso C-13: Inferencia honesta, N_efectivo e intervalos de confianza
========================================================================================
Verifica sobre BD temporal sintetica, sin red y con reloj parcheado en
utils.market_calendar, services.emision y database.predicciones:
  (a) Las 116 senales del mismo dia NO se tratan como independientes:
      con 116 senales en una sola fecha con 80% de acierto, n_fechas == 1,
      muestra_suficiente == False, estado_muestra == "muestra_insuficiente",
      significativo_95 == False y el sistema no declara significacion ni usa
      sqrt(p(1-p)/116) como error del cluster.
  (b) Diferencia entre varianza por fecha (clusters) e i.i.d. ingenuo:
      con 10 fechas de 100 senales cada una (N = 1000) donde 5 fechas tienen
      acierto del 80% y 5 fechas tienen acierto del 20% (media 50%):
      el error estandar i.i.d. ingenuo seria ~1.58% (N=1000), pero la inferencia
      por bloques detecta un error estandar robusto de ~10.0%, n_efectivo <= 25 (<< 1000)
      y un IC 95% ancho (~[27%, 73%]).
  (c) Aviso de "Muestra insuficiente" con pocas fechas (< 30) vs "Muestra suficiente" con >= 30:
      con 10 fechas devuelve muestra_suficiente == False y "Muestra insuficiente" en aviso_muestra;
      con 35 fechas devuelve muestra_suficiente == True y "Muestra de 35 fechas".
  (d) Penalizacion por solapamiento en horizontes multi-dia (3D, 1S, 1M):
      ante una serie de tasas diarias positivamente autocorrelacionada, el error
      estandar robusto con dias_horizonte = 5 (1S) es mayor o igual que con dias_horizonte = 1 (1D).
  (e) Comprobacion en solo lectura contra stocklab.db de que obtener_metricas,
      obtener_resumen_c10, obtener_resumen_c11 y obtener_resumen_c12 exponen
      inferencia_3clases e inferencia_signo con estado_muestra == "muestra_insuficiente"
      (para las 10 fechas actuales de v5-frozen) sin modificar stocklab.db.
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


class TestC13InferenciaNEfectivo(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_c13.db")
        self.p_db = patch.object(db_conn, "DB_PATH", self.db_path)
        self.p_db.start()
        init_db()
        db_pred.init_predicciones_table()
        db_pred.init_evaluaciones_table()

    def tearDown(self):
        self.p_db.stop()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    def _insertar_prediccion_resuelta(
        self,
        *,
        ticker: str,
        empresa: str,
        fecha_prediccion: str,
        horizonte: str,
        dias: int,
        fecha_objetivo: str,
        senal: str,
        precio_entrada: float,
        precio_real: float,
        variacion_real: float,
        acierto: int,
        modelo: str = "v5-frozen",
    ) -> int:
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias,
                    fecha_objetivo, precio_entrada, senal, pct_estimado,
                    banda_pct, confianza, modelo, resuelta,
                    fecha_resolucion, precio_real, variacion_real, acierto,
                    error_pct, motivo_resolucion, score_calidad
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, 1.5,
                    2.0, 75.0, ?, 1,
                    ?, ?, ?, ?,
                    0.5, 'cierre_horizonte', 80.0
                )
                """,
                (
                    ticker,
                    empresa,
                    fecha_prediccion,
                    horizonte,
                    dias,
                    fecha_objetivo,
                    precio_entrada,
                    senal,
                    modelo,
                    fecha_objetivo,
                    precio_real,
                    variacion_real,
                    acierto,
                ),
            )
            pid = cur.lastrowid
            conn.commit()
            return pid
        finally:
            conn.close()

    def test_a_senales_mismo_dia_no_son_independientes(self):
        """
        Las 116 senales del mismo dia NO se tratan como independientes:
        con 116 senales en 1 sola fecha que aciertan el 80%, n_fechas == 1,
        muestra_suficiente == False, estado_muestra == 'muestra_insuficiente',
        significativo_95 == False y el IC no puede estimarse (ic95_inf is None).
        """
        # 1. Comprobacion con funcion pura
        n_senales = 116
        n_aciertos = round(116 * 0.8)  # 93
        inf_pura = db_pred.calcular_inferencia_por_fecha(
            [{"fecha": "2026-10-14", "n": n_senales, "aciertos": n_aciertos}],
            referencia_pct=50.0,
            dias_horizonte=1,
        )
        self.assertEqual(inf_pura["n_fechas"], 1)
        self.assertEqual(inf_pura["n_total"], 116)
        self.assertFalse(inf_pura["muestra_suficiente"])
        self.assertEqual(inf_pura["estado_muestra"], "muestra_insuficiente")
        self.assertFalse(inf_pura["significativo_95"])
        self.assertIsNone(inf_pura["se_cluster_pct"])
        self.assertIsNone(inf_pura["ic95_inf"])
        self.assertIsNone(inf_pura["ic95_sup"])
        self.assertEqual(inf_pura["n_efectivo"], 1.0)
        self.assertIn("Muestra insuficiente (1/30 fechas de emision", inf_pura["aviso_muestra"])

        # 2. Comprobacion en pipeline de BD sintetica
        dt = datetime(2026, 10, 15, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt):
            for i in range(116):
                es_acierto = 1 if i < n_aciertos else 0
                self._insertar_prediccion_resuelta(
                    ticker=f"TK{i:03d}.MC",
                    empresa=f"Empresa {i}",
                    fecha_prediccion="2026-10-14",
                    horizonte="1D",
                    dias=1,
                    fecha_objetivo="2026-10-15",
                    senal="ALCISTA",
                    precio_entrada=10.0,
                    precio_real=10.5 if es_acierto else 9.5,
                    variacion_real=5.0 if es_acierto else -5.0,
                    acierto=es_acierto,
                )

            m = db_pred.obtener_metricas("1D", incluir_legacy=False)
            self.assertEqual(m["resueltas"], 116)
            self.assertEqual(m["n_fechas"], 1)
            inf_3c = m["inferencia_3clases"]
            self.assertEqual(inf_3c["n_fechas"], 1)
            self.assertFalse(inf_3c["muestra_suficiente"])
            self.assertEqual(inf_3c["estado_muestra"], "muestra_insuficiente")
            self.assertFalse(inf_3c["significativo_95"])
            self.assertIsNone(inf_3c["ic95_inf"])
            self.assertIsNone(inf_3c["se_cluster_pct"])
            self.assertIn("Muestra insuficiente (1/30 fechas de emision", inf_3c["aviso_muestra"])

    def test_b_diferencia_varianza_clusters_vs_iid_ingenuo(self):
        """
        Diferencia entre varianza por fecha (clusters) e i.i.d. ingenuo:
        con 10 fechas de 100 senales cada una (N = 1000) donde 5 fechas tienen acierto
        del 80% y 5 fechas tienen acierto del 20% (media 50%):
        - Error estandar i.i.d. ingenuo: ~1.58% (N=1000).
        - Error estandar robusto por bloques de fecha: ~10.0%.
        - N_efectivo <= 25 (<< 1000).
        - IC 95% ancho: ~[27%, 73%].
        """
        fechas_b = [
            {"fecha": f"2026-10-1{i}", "n": 100, "aciertos": 80} for i in range(5)
        ] + [
            {"fecha": f"2026-10-2{i}", "n": 100, "aciertos": 20} for i in range(5)
        ]

        res = db_pred.calcular_inferencia_por_fecha(fechas_b, referencia_pct=50.0, dias_horizonte=1)

        self.assertEqual(res["n_fechas"], 10)
        self.assertEqual(res["n_total"], 1000)
        self.assertEqual(res["tasa_global_pct"], 50.0)

        # SE iid ingenuo = sqrt(50*50 / 1000) = sqrt(2.5) = 1.5811%
        self.assertAlmostEqual(res["se_iid_ingenuo_pct"], 1.5811, places=2)

        # SE cluster robusto sobre las 10 tasas diarias = 10.0%
        self.assertAlmostEqual(res["se_cluster_pct"], 10.0, places=2)

        # Ratio entre cluster e i.i.d. >= 6x de varianza (design effect >= 6.0)
        self.assertGreater(res["design_effect"], 35.0)

        # N_efectivo <= 25 (en lugar de N=1000)
        self.assertLessEqual(res["n_efectivo"], 25.0)
        self.assertGreaterEqual(res["n_efectivo"], 1.0)

        # IC 95% con t-Student (df=9, t_crit=2.262) da ~[27.38%, 72.62%]
        self.assertAlmostEqual(res["ic95_inf"], 27.38, delta=1.0)
        self.assertAlmostEqual(res["ic95_sup"], 72.62, delta=1.0)

        # Pocas fechas (< 30) -> muestra_insuficiente
        self.assertFalse(res["muestra_suficiente"])
        self.assertEqual(res["estado_muestra"], "muestra_insuficiente")
        self.assertIn("Muestra insuficiente (10/30 fechas de emision", res["aviso_muestra"])

    def test_c_aviso_muestra_insuficiente_vs_suficiente(self):
        """
        Aviso de 'Muestra insuficiente' con pocas fechas (< 30) vs
        'Muestra suficiente' con >= 30 fechas.
        """
        # Caso 1: 10 fechas -> Muestra insuficiente
        fechas_10 = [{"fecha": f"2026-10-{i:02d}", "n": 50, "aciertos": 30} for i in range(1, 11)]
        res_10 = db_pred.calcular_inferencia_por_fecha(fechas_10, referencia_pct=50.0)
        self.assertFalse(res_10["muestra_suficiente"])
        self.assertEqual(res_10["estado_muestra"], "muestra_insuficiente")
        self.assertIn("Muestra insuficiente (10/30 fechas de emision", res_10["aviso_muestra"])
        self.assertFalse(res_10["significativo_95"])

        # Caso 2: 35 fechas consistentes -> Muestra suficiente
        fechas_35 = [{"fecha": f"2026-09-{i:02d}", "n": 50, "aciertos": 35} for i in range(1, 36)]
        res_35 = db_pred.calcular_inferencia_por_fecha(fechas_35, referencia_pct=50.0)
        self.assertTrue(res_35["muestra_suficiente"])
        self.assertEqual(res_35["estado_muestra"], "muestra_suficiente")
        self.assertIn("Muestra de 35 fechas", res_35["aviso_muestra"])
        # Con tasa constante 70% vs referencia 50% y varianza nula, es significativo
        self.assertTrue(res_35["significativo_95"])

    def test_d_penalizacion_solapamiento_horizontes_multidia(self):
        """
        Penalizacion por solapamiento en horizontes multi-dia (3D, 1S, 1M):
        ante una serie de tasas diarias positivamente autocorrelacionada,
        el error estandar con dias_horizonte = 5 (1S) es mayor o igual que con dias_horizonte = 1 (1D).
        """
        serie_autocorr = [20, 25, 30, 40, 50, 60, 70, 80, 85, 90]
        fechas_d = [
            {"fecha": f"2026-10-{i:02d}", "n": 100, "aciertos": val}
            for i, val in enumerate(serie_autocorr, start=1)
        ]

        res_1d = db_pred.calcular_inferencia_por_fecha(fechas_d, dias_horizonte=1)
        res_1s = db_pred.calcular_inferencia_por_fecha(fechas_d, dias_horizonte=5)

        self.assertIsNotNone(res_1d["se_cluster_pct"])
        self.assertIsNotNone(res_1s["se_cluster_pct"])
        # Con autocorrelacion positiva, la varianza Newey-West con lag > 0 debe ser mayor o igual
        self.assertGreaterEqual(res_1s["se_cluster_pct"], res_1d["se_cluster_pct"])
        self.assertLessEqual(res_1s["n_efectivo"], res_1d["n_efectivo"])

    def test_e_comprobacion_solo_lectura_contra_stocklab_db(self):
        """
        Verifica en solo lectura (mode=ro, uri=True) sobre stocklab.db real que
        obtener_metricas, obtener_resumen_c10, obtener_resumen_c11 y obtener_resumen_c12
        devuelven inferencia_3clases e inferencia_signo con estado_muestra == 'muestra_insuficiente'
        (para las 10 fechas actuales de v5-frozen) sin modificar stocklab.db en absoluto.
        """
        self.assertTrue(os.path.exists(REAL_DB_PATH), "stocklab.db debe existir")
        sha_db_antes = _sha256_archivo(REAL_DB_PATH)

        uri_ro = f"file:{os.path.abspath(REAL_DB_PATH)}?mode=ro"
        conn = sqlite3.connect(uri_ro, uri=True)
        try:
            sha_inm_antes = db_pred._calcular_sha256_inmutables(conn, "predicciones")
            sha_res_antes = db_pred._calcular_sha256_resueltas(conn, "predicciones")

            exp_n_fechas = conn.execute(
                "SELECT COUNT(DISTINCT fecha_prediccion) FROM predicciones "
                "WHERE resuelta = 1 AND modelo != 'legacy-pre-freeze'"
            ).fetchone()[0]
            exp_n_fechas_dir = conn.execute(
                "SELECT COUNT(DISTINCT fecha_prediccion) FROM predicciones "
                "WHERE resuelta = 1 AND modelo != 'legacy-pre-freeze' "
                "AND senal IN ('ALCISTA', 'BAJISTA')"
            ).fetchone()[0]
            self.assertGreaterEqual(exp_n_fechas, 10)
            self.assertGreaterEqual(exp_n_fechas_dir, 10)

            # 1. obtener_metricas
            m_vivo = db_pred.obtener_metricas("Todos", incluir_legacy=False, conn=conn)
            self.assertIn("inferencia_3clases", m_vivo)
            self.assertIn("inferencia_signo", m_vivo)
            inf_3c = m_vivo["inferencia_3clases"]
            inf_sig = m_vivo["inferencia_signo"]
            self.assertEqual(inf_3c["n_fechas"], exp_n_fechas)
            self.assertEqual(inf_3c["estado_muestra"], "muestra_insuficiente")
            self.assertFalse(inf_3c["muestra_suficiente"])
            self.assertFalse(inf_3c["significativo_95"])
            self.assertIn(f"Muestra insuficiente ({exp_n_fechas}/30 fechas de emision", inf_3c["aviso_muestra"])
            self.assertGreater(inf_3c["se_cluster_pct"], inf_3c["se_iid_ingenuo_pct"])

            self.assertEqual(inf_sig["n_fechas"], exp_n_fechas_dir)
            self.assertEqual(inf_sig["estado_muestra"], "muestra_insuficiente")
            self.assertFalse(inf_sig["muestra_suficiente"])
            self.assertFalse(inf_sig["significativo_95"])

            # Por horizonte
            for hz in ("1D", "3D", "1S", "1M"):
                sub_hz = m_vivo["por_horizonte"][hz]
                self.assertIn("inferencia_3clases", sub_hz)
                self.assertIn("inferencia_signo", sub_hz)

            # 2. obtener_resumen_c10
            r10 = db_pred.obtener_resumen_c10(incluir_legacy=False, conn=conn)
            self.assertIn("inferencia_3clases", r10)
            self.assertIn("inferencia_signo", r10)
            self.assertEqual(r10["inferencia_3clases"]["estado_muestra"], "muestra_insuficiente")
            self.assertEqual(r10["inferencia_signo"]["estado_muestra"], "muestra_insuficiente")

            # 3. obtener_resumen_c11
            r11 = db_pred.obtener_resumen_c11(incluir_legacy=False, conn=conn)
            self.assertIn("inferencia_3clases", r11)
            self.assertIn("inferencia_signo", r11)
            self.assertEqual(r11["inferencia_3clases"]["estado_muestra"], "muestra_insuficiente")
            self.assertEqual(r11["inferencia_signo"]["estado_muestra"], "muestra_insuficiente")

            # 4. obtener_resumen_c12
            r12 = db_pred.obtener_resumen_c12(incluir_legacy=False, conn=conn)
            self.assertIn("inferencia_3clases", r12)
            self.assertIn("inferencia_signo", r12)
            self.assertEqual(r12["inferencia_3clases"]["estado_muestra"], "muestra_insuficiente")
            self.assertEqual(r12["inferencia_signo"]["estado_muestra"], "muestra_insuficiente")

            # Inmutabilidad estricta
            self.assertEqual(conn.total_changes, 0)
            sha_inm_despues = db_pred._calcular_sha256_inmutables(conn, "predicciones")
            sha_res_despues = db_pred._calcular_sha256_resueltas(conn, "predicciones")
            self.assertEqual(sha_inm_despues, sha_inm_antes)
            self.assertEqual(sha_res_despues, sha_res_antes)
        finally:
            conn.close()

        sha_db_despues = _sha256_archivo(REAL_DB_PATH)
        self.assertEqual(sha_db_despues, sha_db_antes)


if __name__ == "__main__":
    unittest.main()
