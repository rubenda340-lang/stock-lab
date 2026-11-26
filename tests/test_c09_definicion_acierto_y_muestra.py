"""
Tests unitarios para Paso C-09: Definicion de acierto por metrica y muestra visible
===================================================================================
Verifica sobre BD temporal sintetica, sin red y con reloj parcheado en
utils.market_calendar, services.emision y database.predicciones:
  (a) Separacion matematica entre Acierto en 3 clases y Acierto de signo:
      construye 10 senales resueltas donde el acierto en 3 clases y el acierto de signo
      dan porcentajes distintos y verifica que obtener_metricas calcula exactamente
      ambos porcentajes y ambas referencias (ref_siempre_lateral_pct y ref_frecuencia_subida_pct).
  (b) Metadatos de muestra exactos: con senales repartidas en 3 fechas distintas
      (2026-10-14, 2026-10-15, 2026-10-16) y varios horizontes, verifica n_senales,
      n_fechas == 3, fecha_min == '2026-10-14', fecha_max == '2026-10-16' y etiqueta_muestra
      tanto en el global como en cada subdiccionario de por_horizonte, y comprueba que
      no depende del limite=250 de obtener_resueltas (insertando 300 senales sinteticas).
  (c) Caso de horizonte sin resueltas (1M con 0 resueltas): no lanza ZeroDivisionError,
      devuelve n_senales == 0, n_fechas == 0, tasas None y etiqueta limpia.
  (d) Comprobacion en solo lectura contra stocklab.db de que obtener_metricas,
      obtener_resumen_c10, obtener_resumen_c11 y obtener_resumen_c12 exponen
      etiqueta_muestra y las metricas de 3 clases y signo sin modificar stocklab.db.
"""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
import sqlite3
import sys
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


class TestC09DefinicionAciertoYMuestra(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_c09.db")
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0, 1.0, 60.0, ?, 1, ?, ?, ?, ?, 0.5, 'vencida', 70.0)
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
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_a_separacion_matematica_3clases_vs_signo(self):
        """
        (a) Verifica la separacion matematica entre Acierto en 3 clases y Acierto de signo:
        Construye 10 senales resueltas en 1D (umbral lateral 0.8%):
          1. ALCISTA, var = +0.4% -> clase real LATERAL (acierto 3c = 0), acierto signo = 1 (+0.4 > 0)
          2. ALCISTA, var = +1.2% -> clase real ALCISTA (acierto 3c = 1), acierto signo = 1
          3. BAJISTA, var = -0.3% -> clase real LATERAL (acierto 3c = 0), acierto signo = 1 (-0.3 < 0)
          4. BAJISTA, var = -1.5% -> clase real BAJISTA (acierto 3c = 1), acierto signo = 1
          5. LATERAL, var = +0.2% -> clase real LATERAL (acierto 3c = 1), no es direccional
          6. LATERAL, var = +2.0% -> clase real ALCISTA (acierto 3c = 0), no es direccional
          7. ALCISTA, var = -0.5% -> clase real LATERAL (acierto 3c = 0), acierto signo = 0
          8. BAJISTA, var = +0.5% -> clase real LATERAL (acierto 3c = 0), acierto signo = 0
          9. LATERAL, var = -0.1% -> clase real LATERAL (acierto 3c = 1), no es direccional
          10. ALCISTA, var = +1.0% -> clase real ALCISTA (acierto 3c = 1), acierto signo = 1
        Resultados esperados:
          - n_senales = 10
          - Acierto en 3 clases = 5 / 10 = 50.0% (casos 2, 4, 5, 9, 10)
          - n_direccionales = 7 (casos 1, 2, 3, 4, 7, 8, 10)
          - aciertos_signo = 5 / 7 = 71.4% (casos 1, 2, 3, 4, 10)
          - ref_siempre_lateral_pct = 6 / 10 = 60.0% (casos 1, 3, 5, 7, 8, 9)
          - ref_azar_signo_pct = 50.0%
          - ref_frecuencia_subida_pct = 6 / 10 = 60.0% (casos 1, 2, 5, 6, 8, 10)
        """
        datos = [
            ("SAN.MC", "Banco Santander", "ALCISTA", 10.0, 10.04, 0.40, 0),
            ("BBVA.MC", "BBVA", "ALCISTA", 10.0, 10.12, 1.20, 1),
            ("ITX.MC", "Inditex", "BAJISTA", 10.0, 9.97, -0.30, 0),
            ("IBE.MC", "Iberdrola", "BAJISTA", 10.0, 9.85, -1.50, 1),
            ("REP.MC", "Repsol", "LATERAL", 10.0, 10.02, 0.20, 1),
            ("TEF.MC", "Telefonica", "LATERAL", 10.0, 10.20, 2.00, 0),
            ("ACS.MC", "ACS", "ALCISTA", 10.0, 9.95, -0.50, 0),
            ("FER.MC", "Ferrovial", "BAJISTA", 10.0, 10.05, 0.50, 0),
            ("GRF.MC", "Grifols", "LATERAL", 10.0, 9.99, -0.10, 1),
            ("AMS.MC", "Amadeus", "ALCISTA", 10.0, 10.10, 1.00, 1),
        ]
        for tk, emp, senal, pin, pout, var, ok in datos:
            self._insertar_prediccion_resuelta(
                ticker=tk,
                empresa=emp,
                fecha_prediccion="2026-10-14",
                horizonte="1D",
                dias=1,
                fecha_objetivo="2026-10-15",
                senal=senal,
                precio_entrada=pin,
                precio_real=pout,
                variacion_real=var,
                acierto=ok,
            )

        m = db_pred.obtener_metricas("1D", incluir_legacy=False)
        self.assertEqual(m["n_senales"], 10)
        self.assertEqual(m["resueltas"], 10)
        self.assertEqual(m["tasa_acierto_3clases"], 50.0)
        self.assertEqual(m["tasa_acierto"], 50.0)
        self.assertEqual(m["ref_siempre_lateral_pct"], 60.0)

        self.assertEqual(m["n_direccionales"], 7)
        self.assertEqual(m["aciertos_signo"], 5)
        self.assertEqual(m["tasa_acierto_signo"], 71.4)
        self.assertEqual(m["ref_azar_signo_pct"], 50.0)
        self.assertEqual(m["ref_frecuencia_subida_pct"], 60.0)

        dist_em = m["dist_senales_emitidas"]
        self.assertEqual(dist_em["n_alcista"], 4)
        self.assertEqual(dist_em["n_bajista"], 3)
        self.assertEqual(dist_em["n_lateral"], 3)

        dist_re = m["dist_clases_reales"]
        self.assertEqual(dist_re["n_alcista_real"], 3)
        self.assertEqual(dist_re["n_bajista_real"], 1)
        self.assertEqual(dist_re["n_lateral_real"], 6)

        self.assertIn("10 se\u00f1ales", m["etiqueta_muestra"])
        self.assertIn("1 fechas", m["etiqueta_muestra"])
        self.assertIn("1D", m["etiqueta_muestra"])

    def test_b_metadatos_muestra_exactos_y_no_dependencia_limite_250(self):
        """
        (b) Verifica metadatos de muestra exactos con 3 fechas distintas
        (2026-10-14, 2026-10-15, 2026-10-16) y varios horizontes, e inserta 300
        senales sinteticas para comprobar que obtener_metricas calcula sobre el
        total SQL de resueltas (300) y no sobre el limite=250 de obtener_resueltas.
        """
        fechas = ["2026-10-14", "2026-10-15", "2026-10-16"]
        horizontes = ["1D", "3D", "1S"]

        # Insertar 300 senales: repartidas uniformemente en 3 fechas y 3 horizontes
        for i in range(300):
            hz = horizontes[i % 3]
            f_pred = fechas[(i // 3) % 3]
            self._insertar_prediccion_resuelta(
                ticker=f"TK{i:03d}.MC",
                empresa=f"Empresa {i}",
                fecha_prediccion=f_pred,
                horizonte=hz,
                dias=1 if hz == "1D" else (3 if hz == "3D" else 5),
                fecha_objetivo="2026-10-22",
                senal="ALCISTA" if i % 2 == 0 else "LATERAL",
                precio_entrada=10.0,
                precio_real=10.10,
                variacion_real=1.00,
                acierto=1 if i % 2 == 0 else 0,
            )

        # 1. Comprobacion de que obtener_resueltas trunca a 250
        df_res = db_pred.obtener_resueltas("Todos", limite=250)
        self.assertEqual(len(df_res), 250)

        # 2. Comprobacion de que obtener_metricas evalua sobre el total de 300
        m = db_pred.obtener_metricas("Todos", incluir_legacy=False)
        self.assertEqual(m["resueltas"], 300)
        self.assertEqual(m["n_senales"], 300)
        self.assertEqual(m["n_fechas"], 3)
        self.assertEqual(m["fecha_min"], "2026-10-14")
        self.assertEqual(m["fecha_max"], "2026-10-16")
        self.assertEqual(m["periodo_str"], "2026-10-14 a 2026-10-16")
        self.assertEqual(m["horizonte_label"], "Todos")

        etiq_esp = db_pred.formatear_etiqueta_muestra(300, 3, "2026-10-14", "2026-10-16", "Todos")
        self.assertEqual(m["etiqueta_muestra"], etiq_esp)
        self.assertIn("300 se\u00f1ales", m["etiqueta_muestra"])
        self.assertIn("3 fechas", m["etiqueta_muestra"])
        self.assertIn("2026-10-14 a 2026-10-16", m["etiqueta_muestra"])

        # 3. Comprobacion en cada subdiccionario de por_horizonte
        for hz in ("1D", "3D", "1S"):
            sub_m = m["por_horizonte"][hz]
            self.assertEqual(sub_m["n_senales"], 100)
            self.assertEqual(sub_m["n_fechas"], 3)
            self.assertEqual(sub_m["fecha_min"], "2026-10-14")
            self.assertEqual(sub_m["fecha_max"], "2026-10-16")
            self.assertIn(f"100 se\u00f1ales", sub_m["etiqueta_muestra"])
            self.assertIn(f"{hz}", sub_m["etiqueta_muestra"])

    def test_c_horizonte_sin_resueltas_limpio_sin_zerodivision(self):
        """
        (c) Caso de horizonte sin resueltas (como 1M con 0 resueltas):
        no lanza ZeroDivisionError y devuelve n_senales == 0, n_fechas == 0,
        tasas None y etiqueta limpia.
        """
        self._insertar_prediccion_resuelta(
            ticker="SAN.MC",
            empresa="Santander",
            fecha_prediccion="2026-10-14",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-15",
            senal="ALCISTA",
            precio_entrada=10.0,
            precio_real=10.10,
            variacion_real=1.00,
            acierto=1,
        )

        m_1m = db_pred.obtener_metricas("1M", incluir_legacy=False)
        self.assertEqual(m_1m["resueltas"], 0)
        self.assertEqual(m_1m["n_senales"], 0)
        self.assertEqual(m_1m["n_fechas"], 0)
        self.assertIsNone(m_1m["fecha_min"])
        self.assertIsNone(m_1m["fecha_max"])
        self.assertEqual(m_1m["periodo_str"], "Sin muestra")
        self.assertEqual(m_1m["horizonte_label"], "1M")
        self.assertIsNone(m_1m["tasa_acierto_3clases"])
        self.assertIsNone(m_1m["tasa_acierto_signo"])
        self.assertIsNone(m_1m["ref_siempre_lateral_pct"])
        self.assertIsNone(m_1m["ref_frecuencia_subida_pct"])
        self.assertEqual(m_1m["n_direccionales"], 0)
        self.assertEqual(m_1m["aciertos_signo"], 0)

        etiq_limpia = db_pred.formatear_etiqueta_muestra(0, 0, None, None, "1M")
        self.assertEqual(m_1m["etiqueta_muestra"], etiq_limpia)
        self.assertIn("0 se\u00f1ales", etiq_limpia)
        self.assertIn("Sin muestra", etiq_limpia)
        self.assertIn("1M", etiq_limpia)

        # En el desglose de Todos, 1M debe estar presente e igualmente limpio
        m_todos = db_pred.obtener_metricas("Todos", incluir_legacy=False)
        self.assertIn("1M", m_todos["por_horizonte"])
        sub_1m = m_todos["por_horizonte"]["1M"]
        self.assertEqual(sub_1m["n_senales"], 0)
        self.assertEqual(sub_1m["etiqueta_muestra"], etiq_limpia)

    @unittest.skipUnless(os.path.exists(REAL_DB_PATH), "stocklab.db no disponible")
    def test_d_comprobacion_solo_lectura_contra_stocklab_db(self):
        """
        (d) Comprobacion en solo lectura contra stocklab.db de que
        obtener_metricas, obtener_resumen_c10, obtener_resumen_c11 y
        obtener_resumen_c12 incluyen los campos de muestra y metricas C-09
        sin modificar stocklab.db.
        """
        sha_db_antes = _sha256_archivo(REAL_DB_PATH)
        conn = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            sha_inm_antes = db_pred._calcular_sha256_inmutables(conn, "predicciones")
            sha_res_antes = db_pred._calcular_sha256_resueltas(conn, "predicciones")

            # 1. obtener_metricas
            m_vivo = db_pred.obtener_metricas("Todos", incluir_legacy=False, conn=conn)
            self.assertGreater(m_vivo["resueltas"], 0)
            self.assertIn("tasa_acierto_3clases", m_vivo)
            self.assertIn("tasa_acierto_signo", m_vivo)
            self.assertIn("ref_siempre_lateral_pct", m_vivo)
            self.assertIn("ref_azar_signo_pct", m_vivo)
            self.assertIn("ref_frecuencia_subida_pct", m_vivo)
            self.assertIn("etiqueta_muestra", m_vivo)
            self.assertIn("se\u00f1ales", m_vivo["etiqueta_muestra"])
            for hz in ("1D", "3D", "1S", "1M"):
                self.assertIn(hz, m_vivo["por_horizonte"])
                self.assertIn("etiqueta_muestra", m_vivo["por_horizonte"][hz])

            # 2. obtener_resumen_c10
            r10 = db_pred.obtener_resumen_c10(incluir_legacy=False, conn=conn)
            self.assertIn("etiqueta_muestra", r10)
            self.assertIn("tasa_acierto_3clases", r10)
            self.assertIn("tasa_acierto_signo", r10)
            self.assertIn("ref_siempre_lateral_pct", r10)

            # 3. obtener_resumen_c11
            r11 = db_pred.obtener_resumen_c11(incluir_legacy=False, conn=conn)
            self.assertIn("etiqueta_muestra", r11)
            self.assertIn("tasa_acierto_3clases", r11)
            self.assertIn("tasa_acierto_signo", r11)
            self.assertIn("tasa_acierto_signo_apertura", r11)

            # 4. obtener_resumen_c12
            r12 = db_pred.obtener_resumen_c12(incluir_legacy=False, conn=conn)
            self.assertIn("etiqueta_muestra", r12)
            self.assertIn("tasa_acierto_3clases_v1", r12)
            self.assertIn("tasa_acierto_3clases_v2", r12)
            self.assertIn("ref_siempre_lateral_v1_pct", r12)
            self.assertIn("ref_siempre_lateral_v2_pct", r12)

            # Verificar que no hubo cambios en absoluto
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
