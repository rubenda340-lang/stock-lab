"""
Tests para el Paso C-25f: Resolver estricto (barra exacta) y estado sin_cotizacion
==================================================================================
Pruebas sobre base de datos SQLite temporal sintetica con reloj inyectado
(parcheando get_ahora_madrid en utils.market_calendar, services.emision y
database.predicciones), sin red y sin depender de la fecha real:
  (i)    Barra exacta: resuelve con el precio de esa barra (precio_real, variacion_real, acierto).
  (ii)   Sin barra exacta pero con una posterior y < 5 sesiones: sigue en resuelta = 0.
  (iii)  Con 4 sesiones cerradas sigue pendiente; con 5 pasa a resuelta = 2 con motivo,
         fecha_resolucion = hoy y campos de resultado NULL.
  (iv)   El recuento salta fines de semana y un festivo BME y no cuenta la sesion de hoy
         si no esta cerrada.
  (v)    Una fila con resuelta = 1 no se modifica nunca; el hash de las columnas de emision
         no cambia.
  (vi)   Metricas, resueltas, pendientes e historial tratan resuelta = 2 segun Paso 0b
         (ni acierto/fallo ni pendiente).
  (vii)  Idempotencia: una segunda ejecucion no cambia nada.
  (viii) Si la barra exacta aparece el dia 3, se resuelve normal (periodo de gracia).
  (ix)   Lote no sano (< 50 % con barra valida): no se marca nada.
"""

from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import database.connection
from database import predicciones as db_pred
from database.connection import get_connection
import services.emision as se
import utils.market_calendar as mc

MADRID_TZ = ZoneInfo("Europe/Madrid")

COLS_EMISION = (
    "id, ticker, fecha_prediccion, horizonte, senal, pct_estimado, "
    "precio_entrada, modelo, score_motor, tilt, hora_calculo"
)


@contextmanager
def _reloj_madrid(dt_madrid: datetime):
    """Inyecta get_ahora_madrid en los tres espacios de nombres requeridos."""
    with patch.object(mc, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(se, "get_ahora_madrid", return_value=dt_madrid), \
         patch.object(db_pred, "get_ahora_madrid", return_value=dt_madrid):
        yield


def _crear_batch_sintetico(tickers, fechas_por_ticker, precios_por_ticker=None):
    """
    Construye un DataFrame MultiIndex (ticker, campo) con las barras indicadas
    por ticker, anteponiendo 40 sesiones historicas previas para superar
    MIN_BARRAS de obtener_serie_ticker. Las fechas del rango de prueba ausentes
    en fechas_por_ticker[t] quedan como NaN.
    """
    fechas_test = sorted({f for flist in fechas_por_ticker.values() for f in flist})
    min_dt = pd.to_datetime(fechas_test[0])
    fechas_previas = [
        d.strftime("%Y-%m-%d")
        for d in pd.bdate_range(end=min_dt - pd.Timedelta(days=1), periods=40)
    ]
    todas_fechas = fechas_previas + fechas_test
    idx = pd.to_datetime(todas_fechas)
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(np.nan, index=idx, columns=cols, dtype=float)
    precios_por_ticker = precios_por_ticker or {}

    for t in tickers:
        fechas_t = list(fechas_previas) + list(fechas_por_ticker.get(t, []))
        mapa_p = precios_por_ticker.get(t, {})
        for f in fechas_t:
            f_dt = pd.to_datetime(f)
            p = float(mapa_p.get(f, 10.0))
            df.loc[f_dt, (t, "Open")] = p
            df.loc[f_dt, (t, "High")] = p * 1.01
            df.loc[f_dt, (t, "Low")] = p * 0.99
            df.loc[f_dt, (t, "Close")] = p
            df.loc[f_dt, (t, "Volume")] = 1000.0
    return df


def _hash_columnas_emision(conn) -> str:
    rows = conn.execute(f"SELECT {COLS_EMISION} FROM predicciones ORDER BY id ASC").fetchall()
    payload = repr([tuple(r) for r in rows]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class TestResolverEstricto(unittest.TestCase):
    """Suite de pruebas para C-25f sobre BD temporal sintetica y reloj inyectado."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_c25f_")
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

    def _insertar_prediccion(
        self,
        ticker="SAN.MC",
        empresa="Banco Santander",
        fecha_prediccion="2026-10-01",
        horizonte="1D",
        dias=1,
        fecha_objetivo="2026-10-02",
        senal="ALCISTA",
        pct_estimado=2.0,
        banda_pct=1.5,
        precio_entrada=10.0,
        modelo="v5-frozen",
        resuelta=0,
        fecha_resolucion=None,
        precio_real=None,
        variacion_real=None,
        acierto=None,
        error_pct=None,
        motivo_resolucion=None,
        score_calidad=None,
        score_motor=70.0,
        tilt=0.5,
        hora_calculo="2026-10-01T19:00:00",
    ) -> int:
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO predicciones (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, pct_estimado, banda_pct, precio_entrada, modelo,
                    resuelta, fecha_resolucion, precio_real, variacion_real, acierto,
                    error_pct, motivo_resolucion, score_calidad, score_motor, tilt, hora_calculo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker, empresa, fecha_prediccion, horizonte, dias, fecha_objetivo,
                    senal, pct_estimado, banda_pct, precio_entrada, modelo,
                    resuelta, fecha_resolucion, precio_real, variacion_real, acierto,
                    error_pct, motivo_resolucion, score_calidad, score_motor, tilt, hora_calculo,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_i_barra_exacta_resuelve_normal(self):
        """(i) Barra exacta: resuelve con el precio de esa barra (precio_real, variacion_real y acierto)."""
        pid = self._insertar_prediccion(
            ticker="SAN.MC",
            fecha_prediccion="2026-10-01",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-02",
            senal="ALCISTA",
            pct_estimado=3.0,
            banda_pct=1.5,
            precio_entrada=10.0,
        )
        df_batch = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC"],
            fechas_por_ticker={
                "SAN.MC": ["2026-10-01", "2026-10-02", "2026-10-05"],
                "BBVA.MC": ["2026-10-01", "2026-10-02", "2026-10-05"],
            },
            precios_por_ticker={
                "SAN.MC": {"2026-10-01": 10.0, "2026-10-02": 10.5, "2026-10-05": 12.0},
            },
        )
        dt_now = datetime(2026, 10, 5, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_now):
            n_res = db_pred.resolver_predicciones(df_batch=df_batch)

        self.assertEqual(n_res, 1)
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT resuelta, fecha_resolucion, precio_real, variacion_real, acierto, motivo_resolucion "
                "FROM predicciones WHERE id = ?",
                (pid,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "2026-10-02")
        self.assertAlmostEqual(row[2], 10.5)
        self.assertAlmostEqual(row[3], 5.0)
        self.assertEqual(row[4], 1)
        self.assertEqual(row[5], "vencida")

    def test_ii_sin_barra_exacta_con_posterior_menor_5_sesiones_sigue_pendiente(self):
        """(ii) Sin barra exacta pero con una posterior y < 5 sesiones: sigue en resuelta = 0."""
        pid = self._insertar_prediccion(
            ticker="SAN.MC",
            fecha_prediccion="2026-10-01",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-02",
            senal="ALCISTA",
            precio_entrada=10.0,
        )
        # Falta 2026-10-02 en SAN.MC, pero existe barra posterior 2026-10-05 con precio 11.0
        df_batch = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC"],
            fechas_por_ticker={
                "SAN.MC": ["2026-10-01", "2026-10-05"],
                "BBVA.MC": ["2026-10-01", "2026-10-02", "2026-10-05"],
            },
            precios_por_ticker={
                "SAN.MC": {"2026-10-01": 10.0, "2026-10-05": 11.0},
            },
        )
        # El lunes 2026-10-05 a las 19:00 solo ha transcurrido 1 sesion BME cerrada tras 2026-10-02
        dt_now = datetime(2026, 10, 5, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_now):
            n_res = db_pred.resolver_predicciones(df_batch=df_batch)

        self.assertEqual(n_res, 0)
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT resuelta, fecha_resolucion, precio_real, variacion_real, acierto, motivo_resolucion "
                "FROM predicciones WHERE id = ?",
                (pid,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row[0], 0)
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertIsNone(row[4])
        self.assertIsNone(row[5])

    def test_iii_cuatro_sesiones_pendiente_cinco_pasa_a_sin_cotizacion(self):
        """(iii) Con 4 sesiones cerradas sigue pendiente; con 5 pasa a resuelta = 2 y campos NULL."""
        # fecha_objetivo = viernes 2026-10-02.
        # Sesiones BME posteriores:
        #   1: lun 2026-10-05
        #   2: mar 2026-10-06
        #   3: mie 2026-10-07
        #   4: jue 2026-10-08
        #   5: vie 2026-10-09
        pid = self._insertar_prediccion(
            ticker="ECR.MC",
            empresa="Ercros",
            fecha_prediccion="2026-10-01",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-02",
            senal="ALCISTA",
            precio_entrada=3.50,
        )
        fechas_sanas = [
            "2026-10-01", "2026-10-02", "2026-10-05",
            "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
        ]
        df_batch = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC", "ECR.MC"],
            fechas_por_ticker={
                "SAN.MC": fechas_sanas,
                "BBVA.MC": fechas_sanas,
                "ECR.MC": ["2026-10-01"],
            },
        )

        # Jueves 2026-10-08 19:00 -> 4 sesiones BME cerradas -> sigue resuelta = 0
        dt_4 = datetime(2026, 10, 8, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_4):
            n_res_4 = db_pred.resolver_predicciones(df_batch=df_batch)
        self.assertEqual(n_res_4, 0)

        conn = get_connection()
        try:
            row_4 = conn.execute(
                "SELECT resuelta, motivo_resolucion, fecha_resolucion FROM predicciones WHERE id = ?",
                (pid,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row_4[0], 0)
        self.assertIsNone(row_4[1])
        self.assertIsNone(row_4[2])

        # Viernes 2026-10-09 19:00 -> 5 sesiones BME cerradas -> pasa a resuelta = 2
        dt_5 = datetime(2026, 10, 9, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_5):
            n_res_5 = db_pred.resolver_predicciones(df_batch=df_batch)
        self.assertEqual(n_res_5, 0)

        conn = get_connection()
        try:
            row_5 = conn.execute(
                "SELECT resuelta, motivo_resolucion, fecha_resolucion, precio_real, "
                "variacion_real, acierto, error_pct, score_calidad "
                "FROM predicciones WHERE id = ?",
                (pid,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row_5[0], db_pred.RESUELTA_SIN_COTIZACION)
        self.assertEqual(row_5[1], db_pred.MOTIVO_SIN_COTIZACION)
        self.assertEqual(row_5[2], "2026-10-09")
        self.assertIsNone(row_5[3])
        self.assertIsNone(row_5[4])
        self.assertIsNone(row_5[5])
        self.assertIsNone(row_5[6])
        self.assertIsNone(row_5[7])

    def test_iv_recuento_salta_fines_de_semana_festivo_bme_y_sesion_abierta(self):
        """(iv) Salta fines de semana y festivo BME (2026-05-01) y no cuenta hoy si no ha cerrado."""
        # fecha_objetivo = miercoles 2026-04-29.
        # Jueves 2026-04-30 -> sesion 1
        # Viernes 2026-05-01 -> FESTIVO BME (Dia del Trabajador) -> no cuenta
        # Sab 02 y Dom 03 -> fin de semana -> no cuentan
        # Lunes 2026-05-04 -> sesion 2
        # Martes 2026-05-05 -> sesion 3
        # Miercoles 2026-05-06 -> sesion 4
        # Jueves 2026-05-07 a las 12:00 (sesion abierta) -> siguen siendo 4 sesiones cerradas
        # Jueves 2026-05-07 a las 19:00 (sesion cerrada) -> 5 sesiones cerradas
        pid = self._insertar_prediccion(
            ticker="ECR.MC",
            fecha_prediccion="2026-04-28",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-04-29",
            senal="BAJISTA",
            precio_entrada=3.50,
        )
        fechas_sanas = [
            "2026-04-28", "2026-04-29", "2026-04-30",
            "2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07",
        ]
        df_batch = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC", "ECR.MC"],
            fechas_por_ticker={
                "SAN.MC": fechas_sanas,
                "BBVA.MC": fechas_sanas,
                "ECR.MC": ["2026-04-28"],
            },
        )

        # Jueves 2026-05-07 a las 12:00 (mercado abierto, sesion de hoy no cerrada) -> 4 sesiones
        dt_abierto = datetime(2026, 5, 7, 12, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_abierto):
            db_pred.resolver_predicciones(df_batch=df_batch)

        conn = get_connection()
        try:
            est_abierto = conn.execute("SELECT resuelta FROM predicciones WHERE id = ?", (pid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(est_abierto, 0, "Con sesion de hoy abierta solo hay 4 sesiones cerradas")

        # Jueves 2026-05-07 a las 19:00 (sesion cerrada) -> 5 sesiones
        dt_cerrado = datetime(2026, 5, 7, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_cerrado):
            db_pred.resolver_predicciones(df_batch=df_batch)

        conn = get_connection()
        try:
            row_cerrado = conn.execute(
                "SELECT resuelta, motivo_resolucion, fecha_resolucion FROM predicciones WHERE id = ?",
                (pid,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row_cerrado[0], 2)
        self.assertEqual(row_cerrado[1], "sin_cotizacion")
        self.assertEqual(row_cerrado[2], "2026-05-07")

    def test_v_resuelta_1_intacta_y_hash_columnas_emision_inmutable(self):
        """(v) Una fila con resuelta = 1 no se modifica nunca; el hash de emision no cambia."""
        pid_res1 = self._insertar_prediccion(
            ticker="SAN.MC",
            fecha_prediccion="2026-09-20",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-09-21",
            senal="ALCISTA",
            pct_estimado=1.8,
            precio_entrada=4.00,
            resuelta=1,
            fecha_resolucion="2026-09-21",
            precio_real=4.10,
            variacion_real=2.5,
            acierto=1,
            error_pct=0.7,
            motivo_resolucion="vencida",
            score_calidad=92.0,
        )
        pid_pend_exacta = self._insertar_prediccion(
            ticker="BBVA.MC",
            fecha_prediccion="2026-10-08",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-09",
            senal="ALCISTA",
            pct_estimado=2.0,
            precio_entrada=9.00,
            resuelta=0,
        )
        pid_pend_sc = self._insertar_prediccion(
            ticker="ECR.MC",
            fecha_prediccion="2026-10-01",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-02",
            senal="LATERAL",
            pct_estimado=0.1,
            precio_entrada=3.50,
            resuelta=0,
        )

        conn = get_connection()
        try:
            hash_emision_antes = _hash_columnas_emision(conn)
            snap_res1_antes = conn.execute("SELECT * FROM predicciones WHERE id = ?", (pid_res1,)).fetchone()
        finally:
            conn.close()

        fechas_sanas = [
            "2026-10-01", "2026-10-02", "2026-10-05",
            "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
        ]
        df_batch = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC", "ECR.MC"],
            fechas_por_ticker={
                "SAN.MC": fechas_sanas,
                "BBVA.MC": fechas_sanas,
                "ECR.MC": ["2026-10-01"],
            },
            precios_por_ticker={
                "BBVA.MC": {"2026-10-09": 9.30},
            },
        )

        dt_now = datetime(2026, 10, 9, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_now):
            n_res = db_pred.resolver_predicciones(df_batch=df_batch)
        self.assertEqual(n_res, 1)

        conn = get_connection()
        try:
            hash_emision_despues = _hash_columnas_emision(conn)
            snap_res1_despues = conn.execute("SELECT * FROM predicciones WHERE id = ?", (pid_res1,)).fetchone()
            est_bbva = conn.execute("SELECT resuelta FROM predicciones WHERE id = ?", (pid_pend_exacta,)).fetchone()[0]
            est_ecr = conn.execute("SELECT resuelta FROM predicciones WHERE id = ?", (pid_pend_sc,)).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(hash_emision_antes, hash_emision_despues)
        self.assertEqual(tuple(snap_res1_antes), tuple(snap_res1_despues))
        self.assertEqual(est_bbva, 1)
        self.assertEqual(est_ecr, 2)

    def test_vi_consumidores_metricas_resueltas_pendientes_historial(self):
        """(vi) Metricas, resueltas, pendientes e historial tratan resuelta = 2 correctamente."""
        # 1 resuelta con acierto, 1 pendiente viva, 1 cerrada sin_cotizacion
        self._insertar_prediccion(
            ticker="SAN.MC",
            fecha_prediccion="2026-10-08",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-09",
            senal="ALCISTA",
            precio_entrada=10.0,
            resuelta=1,
            fecha_resolucion="2026-10-09",
            precio_real=10.5,
            variacion_real=5.0,
            acierto=1,
            error_pct=1.0,
            motivo_resolucion="vencida",
            score_calidad=85.0,
        )
        self._insertar_prediccion(
            ticker="BBVA.MC",
            fecha_prediccion="2026-10-09",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-12",
            senal="BAJISTA",
            precio_entrada=9.0,
            resuelta=0,
        )
        self._insertar_prediccion(
            ticker="ECR.MC",
            fecha_prediccion="2026-09-22",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-09-23",
            senal="LATERAL",
            precio_entrada=3.5,
            resuelta=2,
            fecha_resolucion="2026-10-09",
            motivo_resolucion="sin_cotizacion",
        )

        m = db_pred.obtener_metricas("Todos")
        self.assertEqual(m["resueltas"], 1)
        self.assertEqual(m["pendientes"], 1)
        self.assertEqual(m["pendientes_totales"], 1)
        self.assertEqual(m["vigentes"], 1)
        self.assertEqual(m["total"], 2)
        self.assertEqual(m["total_db"], 3)
        self.assertEqual(m["sin_cotizacion"], 1)
        self.assertEqual(m["tasa_acierto"], 100.0)
        self.assertEqual(m["score_medio"], 85.0)
        self.assertEqual(m["error_medio"], 1.0)
        self.assertEqual(m["por_horizonte"]["1D"]["n"], 1)

        df_res = db_pred.obtener_resueltas("Todos")
        self.assertEqual(len(df_res), 1)
        self.assertEqual(df_res.iloc[0]["ticker"], "SAN.MC")

        df_pend_vig = db_pred.obtener_pendientes("Todos", solo_vigentes=True)
        self.assertEqual(len(df_pend_vig), 1)
        self.assertEqual(df_pend_vig.iloc[0]["ticker"], "BBVA.MC")

        df_pend_all = db_pred.obtener_pendientes("Todos", solo_vigentes=False)
        self.assertEqual(len(df_pend_all), 1)
        self.assertEqual(df_pend_all.iloc[0]["ticker"], "BBVA.MC")

        df_hist_all = db_pred.obtener_historial(solo_resueltas=False)
        self.assertEqual(len(df_hist_all), 3)
        df_hist_res = db_pred.obtener_historial(solo_resueltas=True)
        self.assertEqual(len(df_hist_res), 1)

    def test_vii_idempotencia_segunda_ejecucion(self):
        """(vii) Idempotencia: una segunda ejecucion no cambia nada."""
        self._insertar_prediccion(
            ticker="SAN.MC",
            fecha_prediccion="2026-10-08",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-09",
            senal="ALCISTA",
            precio_entrada=10.0,
        )
        self._insertar_prediccion(
            ticker="ECR.MC",
            fecha_prediccion="2026-10-01",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-02",
            senal="ALCISTA",
            precio_entrada=3.5,
        )
        fechas_sanas = [
            "2026-10-01", "2026-10-02", "2026-10-05",
            "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
        ]
        df_batch = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC", "ECR.MC"],
            fechas_por_ticker={
                "SAN.MC": fechas_sanas,
                "BBVA.MC": fechas_sanas,
                "ECR.MC": ["2026-10-01"],
            },
            precios_por_ticker={"SAN.MC": {"2026-10-09": 10.4}},
        )

        dt_now = datetime(2026, 10, 9, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_now):
            n1 = db_pred.resolver_predicciones(df_batch=df_batch)
            conn = get_connection()
            snap1 = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
            conn.close()

            n2 = db_pred.resolver_predicciones(df_batch=df_batch)
            conn = get_connection()
            snap2 = conn.execute("SELECT * FROM predicciones ORDER BY id ASC").fetchall()
            conn.close()

        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)
        self.assertEqual([tuple(r) for r in snap1], [tuple(r) for r in snap2])

    def test_viii_barra_exacta_aparece_dia_3_resuelve_normal(self):
        """(viii) Si la barra exacta aparece el dia 3, se resuelve normal (periodo de gracia)."""
        pid = self._insertar_prediccion(
            ticker="SLOW.MC",
            fecha_prediccion="2026-10-01",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-02",
            senal="ALCISTA",
            pct_estimado=2.0,
            banda_pct=1.0,
            precio_entrada=20.0,
        )
        fechas_sanas = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]
        # En el dia 2 habil posterior (martes 2026-10-06), SLOW.MC aun no tiene barra del 2026-10-02
        df_batch_d2 = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC", "SLOW.MC"],
            fechas_por_ticker={
                "SAN.MC": fechas_sanas[:4],
                "BBVA.MC": fechas_sanas[:4],
                "SLOW.MC": ["2026-10-01"],
            },
        )
        dt_d2 = datetime(2026, 10, 6, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_d2):
            n_d2 = db_pred.resolver_predicciones(df_batch=df_batch_d2)
        self.assertEqual(n_d2, 0)

        # En el dia 3 habil posterior (miercoles 2026-10-07), el proveedor publica la barra de 2026-10-02
        df_batch_d3 = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC", "SLOW.MC"],
            fechas_por_ticker={
                "SAN.MC": fechas_sanas,
                "BBVA.MC": fechas_sanas,
                "SLOW.MC": ["2026-10-01", "2026-10-02", "2026-10-07"],
            },
            precios_por_ticker={
                "SLOW.MC": {"2026-10-02": 20.6, "2026-10-07": 25.0},
            },
        )
        dt_d3 = datetime(2026, 10, 7, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_d3):
            n_d3 = db_pred.resolver_predicciones(df_batch=df_batch_d3)
        self.assertEqual(n_d3, 1)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT resuelta, fecha_resolucion, precio_real, motivo_resolucion, acierto "
                "FROM predicciones WHERE id = ?",
                (pid,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "2026-10-02")
        self.assertAlmostEqual(row[2], 20.6)
        self.assertEqual(row[3], "vencida")
        self.assertEqual(row[4], 1)

    def test_ix_lote_no_sano_no_marca_sin_cotizacion(self):
        """(ix) Lote no sano (< 50 % con barra valida en la ultima sesion cerrada): no se marca nada."""
        pid = self._insertar_prediccion(
            ticker="ECR.MC",
            fecha_prediccion="2026-10-01",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-02",
            senal="ALCISTA",
            precio_entrada=3.50,
        )
        # En la ultima sesion cerrada (2026-10-09), solo 1 de 3 tickers (33.3% < 50%) tiene barra valida
        df_batch_malo = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC", "ECR.MC"],
            fechas_por_ticker={
                "SAN.MC": ["2026-10-01", "2026-10-02", "2026-10-09"],
                "BBVA.MC": ["2026-10-01", "2026-10-02"],
                "ECR.MC": ["2026-10-01"],
            },
        )
        dt_5 = datetime(2026, 10, 9, 19, 0, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_5), self.assertLogs("database.predicciones", level="WARNING") as cm:
            n_res = db_pred.resolver_predicciones(df_batch=df_batch_malo)

        self.assertEqual(n_res, 0)
        self.assertTrue(any("lote no sano" in m for m in cm.output))

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT resuelta, motivo_resolucion, fecha_resolucion FROM predicciones WHERE id = ?",
                (pid,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row[0], 0)
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])

    def test_x_quinta_sesion_no_cuenta_a_las_17_34_y_si_a_las_17_36(self):
        """(x) La 5.a sesion posterior a fecha_objetivo no cuenta a las 17:34 y si a las 17:36 de un dia habil."""
        # fecha_objetivo = viernes 2026-10-02; su 5.a sesion posterior es el viernes 2026-10-09.
        pid = self._insertar_prediccion(
            ticker="ECR.MC",
            empresa="Ercros",
            fecha_prediccion="2026-10-01",
            horizonte="1D",
            dias=1,
            fecha_objetivo="2026-10-02",
            senal="ALCISTA",
            precio_entrada=3.50,
        )
        fechas_sanas = [
            "2026-10-01", "2026-10-02", "2026-10-05",
            "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
        ]
        df_batch = _crear_batch_sintetico(
            tickers=["SAN.MC", "BBVA.MC", "ECR.MC"],
            fechas_por_ticker={
                "SAN.MC": fechas_sanas,
                "BBVA.MC": fechas_sanas,
                "ECR.MC": ["2026-10-01"],
            },
        )

        # Condicion previa: el lote esta sano en la ultima sesion cerrada de ambos instantes,
        # de modo que el unico factor que distingue 17:34 de 17:36 es el cierre de la sesion de hoy.
        for f in ("2026-10-08", "2026-10-09"):
            sano, _, _ = db_pred._evaluar_salud_lote(df_batch, f)
            self.assertTrue(sano, f"El lote sintetico debe estar sano el {f}")

        def estado():
            conn = get_connection()
            try:
                return conn.execute(
                    "SELECT resuelta, motivo_resolucion, fecha_resolucion FROM predicciones WHERE id = ?",
                    (pid,),
                ).fetchone()
            finally:
                conn.close()

        # Viernes 2026-10-09 17:34: la sesion de hoy sigue abierta -> 4 sesiones cerradas -> pendiente
        dt_1734 = datetime(2026, 10, 9, 17, 34, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_1734):
            self.assertFalse(mc.sesion_cerrada_hoy())
            n_1734 = db_pred.resolver_predicciones(df_batch=df_batch)
        self.assertEqual(n_1734, 0)
        r_1734 = estado()
        self.assertEqual(r_1734[0], 0)
        self.assertIsNone(r_1734[1])
        self.assertIsNone(r_1734[2])

        # Viernes 2026-10-09 17:36: la sesion de hoy esta cerrada -> 5 sesiones cerradas -> sin_cotizacion
        dt_1736 = datetime(2026, 10, 9, 17, 36, tzinfo=MADRID_TZ)
        with _reloj_madrid(dt_1736):
            self.assertTrue(mc.sesion_cerrada_hoy())
            n_1736 = db_pred.resolver_predicciones(df_batch=df_batch)
        self.assertEqual(n_1736, 0)
        r_1736 = estado()
        self.assertEqual(r_1736[0], db_pred.RESUELTA_SIN_COTIZACION)
        self.assertEqual(r_1736[1], db_pred.MOTIVO_SIN_COTIZACION)
        self.assertEqual(r_1736[2], "2026-10-09")

    def test_xi_ultima_sesion_bme_cerrada_en_sabado_domingo_y_festivo(self):
        """(xi) _ultima_sesion_bme_cerrada devuelve la sesion habil anterior en sabado, domingo y festivo BME."""
        casos = [
            # (nombre, instante Madrid, mercado_cerrado esperado, sesion esperada)
            ("sabado 12:00", datetime(2026, 10, 10, 12, 0, tzinfo=MADRID_TZ), True, date(2026, 10, 9)),
            ("sabado 22:00", datetime(2026, 10, 10, 22, 0, tzinfo=MADRID_TZ), True, date(2026, 10, 9)),
            ("domingo 12:00", datetime(2026, 10, 11, 12, 0, tzinfo=MADRID_TZ), True, date(2026, 10, 9)),
            ("domingo 00:07", datetime(2026, 10, 11, 0, 7, tzinfo=MADRID_TZ), True, date(2026, 10, 9)),
            ("festivo BME 12:00 (viernes 1-may-2026)", datetime(2026, 5, 1, 12, 0, tzinfo=MADRID_TZ), True, date(2026, 4, 30)),
            ("festivo BME 19:00 (viernes 1-may-2026)", datetime(2026, 5, 1, 19, 0, tzinfo=MADRID_TZ), True, date(2026, 4, 30)),
            ("Viernes Santo 12:00 (3-abr-2026)", datetime(2026, 4, 3, 12, 0, tzinfo=MADRID_TZ), True, date(2026, 4, 2)),
            ("Lunes de Pascua 12:00 (6-abr-2026)", datetime(2026, 4, 6, 12, 0, tzinfo=MADRID_TZ), True, date(2026, 4, 2)),
            # Controles en dia habil: antes del cierre 17:35 es la sesion anterior; despues, la de hoy
            ("miercoles habil 17:34", datetime(2026, 10, 7, 17, 34, tzinfo=MADRID_TZ), False, date(2026, 10, 6)),
            ("miercoles habil 17:36", datetime(2026, 10, 7, 17, 36, tzinfo=MADRID_TZ), True, date(2026, 10, 7)),
            ("lunes habil 00:07", datetime(2026, 10, 5, 0, 7, tzinfo=MADRID_TZ), False, date(2026, 10, 2)),
        ]
        festivos = set(mc.festivos_bme_rango(2025, 2028))
        for nombre, dt, cerrado_esperado, esperado in casos:
            with self.subTest(caso=nombre):
                with _reloj_madrid(dt):
                    cerrado = mc.sesion_cerrada_hoy()
                    self.assertEqual(cerrado, cerrado_esperado)
                    res = db_pred._ultima_sesion_bme_cerrada(dt.date(), cerrado, festivos)
                self.assertEqual(res, esperado)
                # La sesion devuelta es siempre habil BME
                self.assertLess(res.weekday(), 5)
                self.assertNotIn(res.isoformat(), festivos)


if __name__ == "__main__":
    unittest.main()
