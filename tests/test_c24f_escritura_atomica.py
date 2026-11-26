"""
Tests unitarios para el Paso C-24f: Escritura atomica de Parquet y CSV de cache
===============================================================================
Verifica sobre tempfile.TemporaryDirectory (sin tocar ficheros reales):
  (a) Escritura atomica exitosa de Parquet (atomic_write_parquet) y de CSV
      (atomic_append_or_write_csv, tanto en creacion inicial con cabecera como
      en segundo append sin duplicar cabecera): contenido exacto y 0 ficheros
      .tmp residuales.
  (b) Fallo a mitad de escritura en Parquet y en CSV: si existe un fichero previo
      valido y la escritura nueva lanza una excepcion antes de reemplazar, el
      fichero previo permanece 100% intacto (mismo SHA-256 y bytes) y el .tmp se
      limpia en finally.
  (c) Reintentos en Windows ante PermissionError: mockeando os.replace para que
      falle las primeras 2 veces con PermissionError("WinError 32") y tenga exito
      a la 3.a llamada, la escritura completa con exito, reemplaza el destino y
      limpia el temporal.
  (d) Agotamiento de reintentos: si os.replace falla en todos los intentos con
      PermissionError, se propaga PermissionError, el fichero destino previo
      permanece intacto y el .tmp es eliminado.
"""

import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import services.emision as se
from services.emision import (
    atomic_append_or_write_csv,
    atomic_replace_with_retry,
    atomic_write_parquet,
    sincronizar_cache_panel_historico,
)


def _listar_temporales(dirpath: str):
    return [
        f
        for f in os.listdir(dirpath)
        if f.endswith(".tmp") or f.startswith(".tmp_")
    ]


class TestC24fEscrituraAtomica(unittest.TestCase):
    """Suite de pruebas para C-24f: escritura atomica con reintentos."""

    def _crear_df_parquet(self, valores):
        cols = pd.MultiIndex.from_tuples(
            [("SAN.MC", "Close"), ("SAN.MC", "Volume")],
            names=["Ticker", "Price"],
        )
        idx = pd.date_range("2026-10-01", periods=len(valores), freq="B")
        return pd.DataFrame(valores, index=idx, columns=cols)

    def test_a_escritura_atomica_exitosa_parquet_y_csv(self):
        """
        (a) Escritura atomica exitosa de Parquet (atomic_write_parquet) y de CSV
        (atomic_append_or_write_csv, en creacion inicial con cabecera y en segundo
        append sin duplicar cabecera): contenido exacto y 0 ficheros .tmp residuales.
        """
        with tempfile.TemporaryDirectory(prefix="test_c24f_a_") as tmp_dir:
            parquet_path = os.path.join(tmp_dir, "backtest_cache_6y.parquet")
            csv_path = os.path.join(tmp_dir, "registro_ajustes_cache.csv")

            # 1. Parquet: creacion inicial y sobrescritura posterior
            df_p1 = self._crear_df_parquet([[4.50, 100000.0], [4.55, 110000.0]])
            atomic_write_parquet(df_p1, parquet_path)
            self.assertTrue(os.path.exists(parquet_path))
            self.assertEqual(_listar_temporales(tmp_dir), [])
            df_read_1 = pd.read_parquet(parquet_path)
            pd.testing.assert_frame_equal(df_read_1, df_p1, check_freq=False)

            df_p2 = self._crear_df_parquet(
                [[4.50, 100000.0], [4.55, 110000.0], [4.62, 125000.0]]
            )
            atomic_write_parquet(df_p2, parquet_path)
            self.assertEqual(_listar_temporales(tmp_dir), [])
            df_read_2 = pd.read_parquet(parquet_path)
            pd.testing.assert_frame_equal(df_read_2, df_p2, check_freq=False)

            # 2. CSV: creacion inicial con cabecera
            df_c1 = pd.DataFrame(
                [
                    {
                        "timestamp_deteccion": "2026-10-20T18:40:00",
                        "ticker": "SAN.MC",
                        "fecha_ajuste": "2026-10-15",
                        "ratio": 0.95,
                        "accion": "reemplazo_historico_completo",
                    }
                ]
            )
            atomic_append_or_write_csv(df_c1, csv_path)
            self.assertTrue(os.path.exists(csv_path))
            self.assertEqual(_listar_temporales(tmp_dir), [])

            with open(csv_path, "r", encoding="utf-8") as f:
                lineas_1 = [ln.strip() for ln in f.readlines() if ln.strip()]
            self.assertEqual(len(lineas_1), 2)
            self.assertEqual(
                lineas_1[0],
                "timestamp_deteccion,ticker,fecha_ajuste,ratio,accion",
            )

            # 3. CSV: segundo append sin duplicar cabecera
            df_c2 = pd.DataFrame(
                [
                    {
                        "timestamp_deteccion": "2026-10-21T18:40:00",
                        "ticker": "BBVA.MC",
                        "fecha_ajuste": "2026-10-16",
                        "ratio": 0.50,
                        "accion": "reemplazo_historico_completo",
                    },
                    {
                        "timestamp_deteccion": "2026-10-21T18:40:01",
                        "ticker": "ITX.MC",
                        "fecha_ajuste": "2026-10-17",
                        "ratio": 0.98,
                        "accion": "reemplazo_historico_completo",
                    },
                ]
            )
            atomic_append_or_write_csv(df_c2, csv_path)
            self.assertEqual(_listar_temporales(tmp_dir), [])

            with open(csv_path, "r", encoding="utf-8") as f:
                lineas_2 = [ln.strip() for ln in f.readlines() if ln.strip()]
            self.assertEqual(len(lineas_2), 4)
            self.assertEqual(
                lineas_2[0],
                "timestamp_deteccion,ticker,fecha_ajuste,ratio,accion",
            )
            # La cabecera aparece exactamente una vez
            self.assertEqual(
                sum(1 for ln in lineas_2 if ln.startswith("timestamp_deteccion,")),
                1,
            )

            df_csv_read = pd.read_csv(csv_path)
            df_expected = pd.concat([df_c1, df_c2], ignore_index=True)
            pd.testing.assert_frame_equal(df_csv_read, df_expected)

    def test_b_fallo_mitad_escritura_preserva_fichero_previo_y_limpia_tmp(self):
        """
        (b) Fallo a mitad de escritura en Parquet y en CSV: si existe un fichero
        previo valido y la escritura nueva lanza una excepcion antes de reemplazar,
        el fichero previo permanece 100% intacto (mismo SHA-256 y bytes) y el .tmp
        se limpia en finally.
        """
        with tempfile.TemporaryDirectory(prefix="test_c24f_b_") as tmp_dir:
            parquet_path = os.path.join(tmp_dir, "backtest_cache_6y.parquet")
            csv_path = os.path.join(tmp_dir, "registro_ajustes_cache.csv")

            # Preparar ficheros previos validos
            df_p_orig = self._crear_df_parquet([[4.50, 100000.0], [4.55, 110000.0]])
            atomic_write_parquet(df_p_orig, parquet_path)
            with open(parquet_path, "rb") as f:
                bytes_p_antes = f.read()
            sha_p_antes = hashlib.sha256(bytes_p_antes).hexdigest()

            df_c_orig = pd.DataFrame(
                [
                    {
                        "timestamp_deteccion": "2026-10-20T18:40:00",
                        "ticker": "SAN.MC",
                        "fecha_ajuste": "2026-10-15",
                        "ratio": 0.95,
                        "accion": "reemplazo_historico_completo",
                    }
                ]
            )
            atomic_append_or_write_csv(df_c_orig, csv_path)
            with open(csv_path, "rb") as f:
                bytes_c_antes = f.read()
            sha_c_antes = hashlib.sha256(bytes_c_antes).hexdigest()

            # Simular fallo a mitad de escritura de Parquet (escribiendo bytes corruptos al tmp y lanzando error)
            df_p_new = self._crear_df_parquet([[9.99, 999999.0]])

            def _fallo_to_parquet(path_arg, *args, **kwargs):
                with open(path_arg, "wb") as f_bad:
                    f_bad.write(b"PAR1_CORRUPT_PARTIAL_WRITE")
                raise RuntimeError("Fallo simulado a mitad de to_parquet")

            with patch.object(pd.DataFrame, "to_parquet", side_effect=_fallo_to_parquet):
                with self.assertRaises(RuntimeError):
                    atomic_write_parquet(df_p_new, parquet_path)

            with open(parquet_path, "rb") as f:
                bytes_p_despues = f.read()
            sha_p_despues = hashlib.sha256(bytes_p_despues).hexdigest()
            self.assertEqual(sha_p_despues, sha_p_antes)
            self.assertEqual(bytes_p_despues, bytes_p_antes)
            self.assertEqual(_listar_temporales(tmp_dir), [])
            pd.testing.assert_frame_equal(
                pd.read_parquet(parquet_path), df_p_orig, check_freq=False
            )

            # Simular fallo a mitad de escritura de CSV (escribiendo linea truncada al tmp y lanzando error)
            df_c_new = pd.DataFrame(
                [
                    {
                        "timestamp_deteccion": "2026-10-21T18:40:00",
                        "ticker": "BBVA.MC",
                        "fecha_ajuste": "2026-10-16",
                        "ratio": 0.50,
                        "accion": "reemplazo_historico_completo",
                    }
                ]
            )

            def _fallo_to_csv(path_arg, *args, **kwargs):
                with open(path_arg, "ab") as f_bad:
                    f_bad.write(b"2026-10-21T18:40:00,BBVA.MC,TRUNCATED")
                raise OSError("Fallo simulado a mitad de to_csv")

            with patch.object(pd.DataFrame, "to_csv", side_effect=_fallo_to_csv):
                with self.assertRaises(OSError):
                    atomic_append_or_write_csv(df_c_new, csv_path)

            with open(csv_path, "rb") as f:
                bytes_c_despues = f.read()
            sha_c_despues = hashlib.sha256(bytes_c_despues).hexdigest()
            self.assertEqual(sha_c_despues, sha_c_antes)
            self.assertEqual(bytes_c_despues, bytes_c_antes)
            self.assertEqual(_listar_temporales(tmp_dir), [])
            pd.testing.assert_frame_equal(pd.read_csv(csv_path), df_c_orig)

    def test_c_reintentos_windows_permission_error_exito_al_tercer_intento(self):
        """
        (c) Reintentos en Windows ante PermissionError: mockeando os.replace para
        que falle las primeras 2 veces con PermissionError("WinError 32") y tenga
        exito a la 3.a llamada, la escritura completa con exito, reemplaza el
        destino y limpia el temporal.
        """
        with tempfile.TemporaryDirectory(prefix="test_c24f_c_") as tmp_dir:
            parquet_path = os.path.join(tmp_dir, "backtest_cache_6y.parquet")
            csv_path = os.path.join(tmp_dir, "registro_ajustes_cache.csv")

            df_p_ini = self._crear_df_parquet([[4.50, 100000.0]])
            atomic_write_parquet(df_p_ini, parquet_path)

            df_p_new = self._crear_df_parquet([[4.50, 100000.0], [4.75, 130000.0]])
            real_replace = os.replace
            call_count_p = {"n": 0}

            def _flaky_replace_parquet(src, dst):
                call_count_p["n"] += 1
                if call_count_p["n"] <= 2:
                    raise PermissionError("WinError 32")
                return real_replace(src, dst)

            with patch("services.emision.os.replace", side_effect=_flaky_replace_parquet), \
                 patch("services.emision.time.sleep") as mock_sleep_p:
                atomic_write_parquet(df_p_new, parquet_path)

            self.assertEqual(call_count_p["n"], 3)
            self.assertEqual(mock_sleep_p.call_count, 2)
            self.assertEqual(_listar_temporales(tmp_dir), [])
            pd.testing.assert_frame_equal(
                pd.read_parquet(parquet_path), df_p_new, check_freq=False
            )

            # Verificar tambien sobre CSV (append con 2 fallos iniciales de PermissionError)
            df_c1 = pd.DataFrame(
                [{"timestamp_deteccion": "t1", "ticker": "SAN.MC", "fecha_ajuste": "2026-10-15", "ratio": 0.95, "accion": "a"}]
            )
            atomic_append_or_write_csv(df_c1, csv_path)

            df_c2 = pd.DataFrame(
                [{"timestamp_deteccion": "t2", "ticker": "BBVA.MC", "fecha_ajuste": "2026-10-16", "ratio": 0.50, "accion": "a"}]
            )
            call_count_c = {"n": 0}

            def _flaky_replace_csv(src, dst):
                call_count_c["n"] += 1
                if call_count_c["n"] <= 2:
                    raise PermissionError("WinError 32")
                return real_replace(src, dst)

            with patch("services.emision.os.replace", side_effect=_flaky_replace_csv), \
                 patch("services.emision.time.sleep") as mock_sleep_c:
                atomic_append_or_write_csv(df_c2, csv_path)

            self.assertEqual(call_count_c["n"], 3)
            self.assertEqual(mock_sleep_c.call_count, 2)
            self.assertEqual(_listar_temporales(tmp_dir), [])
            pd.testing.assert_frame_equal(
                pd.read_csv(csv_path),
                pd.concat([df_c1, df_c2], ignore_index=True),
            )

    def test_d_agotamiento_reintentos_propaga_permission_error_y_limpia_tmp(self):
        """
        (d) Agotamiento de reintentos: si os.replace falla en todos los intentos
        con PermissionError, se propaga PermissionError, el fichero destino previo
        permanece intacto y el .tmp es eliminado.
        """
        with tempfile.TemporaryDirectory(prefix="test_c24f_d_") as tmp_dir:
            parquet_path = os.path.join(tmp_dir, "backtest_cache_6y.parquet")
            csv_path = os.path.join(tmp_dir, "registro_ajustes_cache.csv")

            # Fichero Parquet previo
            df_p_ini = self._crear_df_parquet([[4.50, 100000.0], [4.55, 110000.0]])
            atomic_write_parquet(df_p_ini, parquet_path)
            with open(parquet_path, "rb") as f:
                bytes_p_antes = f.read()
            sha_p_antes = hashlib.sha256(bytes_p_antes).hexdigest()

            # Fichero CSV previo
            df_c_ini = pd.DataFrame(
                [{"timestamp_deteccion": "t1", "ticker": "SAN.MC", "fecha_ajuste": "2026-10-15", "ratio": 0.95, "accion": "a"}]
            )
            atomic_append_or_write_csv(df_c_ini, csv_path)
            with open(csv_path, "rb") as f:
                bytes_c_antes = f.read()
            sha_c_antes = hashlib.sha256(bytes_c_antes).hexdigest()

            # Fallo permanente en Parquet
            df_p_new = self._crear_df_parquet([[9.99, 999999.0]])
            with patch(
                "services.emision.os.replace",
                side_effect=PermissionError("WinError 32: archivo bloqueado"),
            ) as mock_rep_p, patch("services.emision.time.sleep") as mock_sleep_p:
                with self.assertRaises(PermissionError):
                    atomic_write_parquet(df_p_new, parquet_path)

            self.assertEqual(mock_rep_p.call_count, 5)
            self.assertEqual(mock_sleep_p.call_count, 4)
            self.assertEqual(_listar_temporales(tmp_dir), [])
            with open(parquet_path, "rb") as f:
                bytes_p_despues = f.read()
            self.assertEqual(hashlib.sha256(bytes_p_despues).hexdigest(), sha_p_antes)
            self.assertEqual(bytes_p_despues, bytes_p_antes)

            # Fallo permanente en CSV
            df_c_new = pd.DataFrame(
                [{"timestamp_deteccion": "t2", "ticker": "BBVA.MC", "fecha_ajuste": "2026-10-16", "ratio": 0.50, "accion": "a"}]
            )
            with patch(
                "services.emision.os.replace",
                side_effect=PermissionError("WinError 32: archivo bloqueado"),
            ) as mock_rep_c, patch("services.emision.time.sleep") as mock_sleep_c:
                with self.assertRaises(PermissionError):
                    atomic_append_or_write_csv(df_c_new, csv_path)

            self.assertEqual(mock_rep_c.call_count, 5)
            self.assertEqual(mock_sleep_c.call_count, 4)
            self.assertEqual(_listar_temporales(tmp_dir), [])
            with open(csv_path, "rb") as f:
                bytes_c_despues = f.read()
            self.assertEqual(hashlib.sha256(bytes_c_despues).hexdigest(), sha_c_antes)
            self.assertEqual(bytes_c_despues, bytes_c_antes)

    def test_e_integracion_sincronizar_cache_panel_historico_usa_helpers_atomicos(self):
        """
        Verifica que sincronizar_cache_panel_historico utiliza atomic_append_or_write_csv
        y atomic_write_parquet sobre un directorio temporal sin dejar ficheros .tmp.
        """
        with tempfile.TemporaryDirectory(prefix="test_c24f_e_") as tmp_dir:
            parquet_path = os.path.join(tmp_dir, "backtest_cache_6y.parquet")
            csv_path = os.path.join(tmp_dir, "registro_ajustes_cache.csv")

            cols = pd.MultiIndex.from_tuples(
                [
                    ("SAN.MC", "Open"),
                    ("SAN.MC", "High"),
                    ("SAN.MC", "Low"),
                    ("SAN.MC", "Close"),
                    ("SAN.MC", "Volume"),
                ],
                names=["Ticker", "Price"],
            )
            fechas_ini = pd.date_range("2026-10-01", periods=3, freq="B")
            df_ini = pd.DataFrame(
                [
                    [10.0, 10.1, 9.9, 10.0, 100000.0],
                    [10.1, 10.2, 10.0, 10.1, 100000.0],
                    [10.2, 10.3, 10.1, 10.2, 100000.0],
                ],
                index=fechas_ini,
                columns=cols,
            )
            atomic_write_parquet(df_ini, parquet_path)

            # Lote con ajuste retrospectivo (factor 0.5 en solape) y una sesion nueva
            fechas_batch = pd.date_range("2026-10-02", periods=3, freq="B")
            df_batch = pd.DataFrame(
                [
                    [5.05, 5.10, 5.00, 5.05, 200000.0],
                    [5.10, 5.15, 5.05, 5.10, 200000.0],
                    [5.20, 5.25, 5.15, 5.20, 210000.0],
                ],
                index=fechas_batch,
                columns=cols,
            )
            fechas_full = pd.date_range("2026-10-01", periods=4, freq="B")
            df_full = pd.DataFrame(
                [
                    [5.00, 5.05, 4.95, 5.00, 200000.0],
                    [5.05, 5.10, 5.00, 5.05, 200000.0],
                    [5.10, 5.15, 5.05, 5.10, 200000.0],
                    [5.20, 5.25, 5.15, 5.20, 210000.0],
                ],
                index=fechas_full,
                columns=cols,
            )

            with patch(
                "services.emision.atomic_append_or_write_csv",
                wraps=se.atomic_append_or_write_csv,
            ) as spy_csv, patch(
                "services.emision.atomic_write_parquet",
                wraps=se.atomic_write_parquet,
            ) as spy_parquet:
                ok = sincronizar_cache_panel_historico(
                    df_batch=df_batch,
                    verbose=False,
                    path_cache=parquet_path,
                    fn_descargar_full=lambda symbols, period="6y": df_full,
                )

            self.assertTrue(ok)
            self.assertEqual(spy_csv.call_count, 1)
            self.assertEqual(spy_parquet.call_count, 1)
            self.assertTrue(os.path.exists(parquet_path))
            self.assertTrue(os.path.exists(csv_path))
            self.assertEqual(_listar_temporales(tmp_dir), [])


if __name__ == "__main__":
    unittest.main()
