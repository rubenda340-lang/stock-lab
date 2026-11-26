"""
Tests para emitir_senales.py y tools/registrar_tarea_emision.ps1 - StockLab
=============================================================================
Pruebas unitarias aisladas sin red ni escrituras en BD real (Paso C-24c):
  (i)   Mapeo de status devueltos por ejecutar_emision_diaria a codigos de salida.
  (ii)  Bloqueo de instancia unica (emision.lock): segunda instancia -> 12;
        bloqueo huerfano (PID muerto o > 60 min) -> se reemplaza y adquiere.
  (iii) Subprocess con PYTHONIOENCODING=cp1252: sin UnicodeEncodeError y salida solo ASCII.
  (iv)  Linea RESULT presente, bien formada y con todos los campos requeridos.
  (v)   Invocacion de backup: solo si success/topped_up o resueltas > 0;
        su fallo (backup=fail) no altera el codigo de salida de la emision.
  (vi)  El fichero registrar_tarea_emision.ps1 pasa ParseFile sin errores y es solo ASCII.
  (vii) Las variables STOCKLAB_LOG_DIR y STOCKLAB_LOCK_DIR se leen en tiempo de ejecucion
        y, sin ellas, las rutas por defecto son las historicas (Paso C-24d).

Aislamiento de produccion (Paso C-24d): todos los tests usan un directorio temporal
para el log y el bloqueo mediante STOCKLAB_LOG_DIR / STOCKLAB_LOCK_DIR (patch.dict de
os.environ, tambien en el subprocess). Ademas, ejecutar_emision_diaria y
ejecutar_backup_tras_emision quedan sustituidas por defecto por funciones que fallan
si se invocan: si un parche concreto no entrase en vigor, el test falla en lugar de
tocar la emision o el backup reales.
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import emitir_senales

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _emision_real_prohibida(*args, **kwargs):
    raise AssertionError("ejecutar_emision_diaria real invocada desde un test")


def _backup_real_prohibido(*args, **kwargs):
    raise AssertionError("ejecutar_backup_tras_emision real invocada desde un test")


class TestEmitirSenales(unittest.TestCase):
    def setUp(self):
        # Orden de limpieza LIFO: primero se cierra el logger, despues se borra el directorio.
        self._tmp = tempfile.TemporaryDirectory(prefix="test_emision_")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._cerrar_logger_emision)

        self.tmp_dir = self._tmp.name
        self.log_dir = os.path.join(self.tmp_dir, "logs")
        self.lock_dir = os.path.join(self.tmp_dir, "lock")
        os.makedirs(self.lock_dir)
        self.test_lock = os.path.join(self.lock_dir, "emision.lock")

        env_patch = patch.dict(os.environ, {
            "STOCKLAB_LOG_DIR": self.log_dir,
            "STOCKLAB_LOCK_DIR": self.lock_dir,
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

        # Valores por defecto a prueba de fallos: la emision y el backup reales no pueden ejecutarse.
        for destino, funcion in (
            ("services.emision.ejecutar_emision_diaria", _emision_real_prohibida),
            ("emitir_senales.ejecutar_backup_tras_emision", _backup_real_prohibido),
        ):
            p = patch(destino, side_effect=funcion)
            p.start()
            self.addCleanup(p.stop)

        # Comprobacion previa: ninguna ruta de produccion queda activa en este test.
        self.assertTrue(emitir_senales.get_lock_file().startswith(self.tmp_dir))

    @staticmethod
    def _cerrar_logger_emision():
        lg = logging.getLogger("stocklab.emision_runner")
        for h in list(lg.handlers):
            h.close()
        lg.handlers.clear()

    def tearDown(self):
        emitir_senales.release_lock(self.test_lock)

    def test_01_mapeo_status_a_codigos_de_salida(self):
        """(i) Mapeo status -> codigo de salida, incluida excepcion -> 1."""
        casos = [
            ({"status": "success", "fecha": "2026-10-14", "insertadas": 10}, 0),
            ({"status": "topped_up", "fecha": "2026-10-14", "insertadas": 2}, 0),
            ({"status": "already_emitted", "fecha": "2026-10-14", "emitidas": 10, "insertadas": 0}, 0),
            ({"status": "late", "fecha": "2026-10-14", "insertadas": 0}, 10),
            ({"status": "no_data", "count": 0, "insertadas": 0}, 11),
            ({"status": "error_descarga", "count": 0}, 11),
        ]

        for mock_res, expected_code in casos:
            emitir_senales.release_lock(self.test_lock)
            with patch("services.emision.ejecutar_emision_diaria", return_value=mock_res), \
                 patch("emitir_senales.ejecutar_backup_tras_emision", return_value="ok"):
                code = emitir_senales.main()
                self.assertEqual(
                    code, expected_code,
                    f"Status {mock_res.get('status')} devolvio {code}, esperado {expected_code}"
                )

        # Caso excepcion inesperada -> codigo 1
        emitir_senales.release_lock(self.test_lock)
        with patch("services.emision.ejecutar_emision_diaria", side_effect=RuntimeError("Fallo simulado")):
            code = emitir_senales.main()
            self.assertEqual(code, 1, "Una excepcion debe devolver codigo de salida 1")

    def test_02_bloqueo_segunda_instancia_y_reemplazo_huerfano(self):
        """(ii) Bloqueo: segunda instancia -> 12; bloqueo huerfano -> se reemplaza."""
        # 1. Adquirir lock legitimo
        adquirido = emitir_senales.acquire_lock(self.test_lock)
        self.assertTrue(adquirido)

        # Intentar segunda instancia: debe devolver 12 sin llamar a ejecutar_emision_diaria
        mock_emision = MagicMock()
        with patch("services.emision.ejecutar_emision_diaria", mock_emision):
            code = emitir_senales.main()
            self.assertEqual(code, 12)
            mock_emision.assert_not_called()

        emitir_senales.release_lock(self.test_lock)

        # 2. Simular lock huerfano por PID muerto
        with open(self.test_lock, "w", encoding="ascii") as f:
            f.write("99999999,1000.0\n")

        with patch("services.emision.ejecutar_emision_diaria", return_value={"status": "already_emitted", "resueltas": 0}):
            code = emitir_senales.main()
            self.assertEqual(code, 0, "Un lock con PID inexistente debe ser reemplazado automaticamente")

        # 3. Simular lock huerfano por antiguedad > 60 min (> 3600 s)
        emitir_senales.release_lock(self.test_lock)
        ancient_time = time.time() - 4000.0
        with open(self.test_lock, "w", encoding="ascii") as f:
            f.write(f"{os.getpid()},{ancient_time:.3f}\n")

        with patch("services.emision.ejecutar_emision_diaria", return_value={"status": "already_emitted", "resueltas": 0}):
            code = emitir_senales.main()
            self.assertEqual(code, 0, "Un lock con mas de 60 min de antiguedad debe ser reemplazado")

    def test_03_subprocess_cp1252_sin_unicode_error_y_salida_ascii(self):
        """(iii) subprocess con PYTHONIOENCODING=cp1252: sin UnicodeEncodeError y salida solo ASCII."""
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp1252"
        env["PYTHONUTF8"] = "0"
        # El subprocess hereda el aislamiento: log y bloqueo en el directorio temporal del test.
        env["STOCKLAB_LOG_DIR"] = self.log_dir
        env["STOCKLAB_LOCK_DIR"] = self.lock_dir

        # Ejecutar emitir_senales.py usando subprocess aislando servicios con mock en linea.
        # El backup real queda prohibido (si se invocase, main devolveria 1 y el test fallaria).
        test_code = (
            "import os, sys, unittest.mock, emitir_senales;"
            "assert emitir_senales.get_lock_file().startswith(os.environ['STOCKLAB_LOCK_DIR']);"
            "unittest.mock.patch('services.emision.ejecutar_emision_diaria', "
            "return_value={'status':'already_emitted','fecha':'2026-10-14','emitidas':10,'insertadas':0,'resueltas':0}).start();"
            "unittest.mock.patch('emitir_senales.ejecutar_backup_tras_emision', "
            "side_effect=AssertionError('backup real invocado')).start();"
            "sys.exit(emitir_senales.main())"
        )
        cmd = [sys.executable, "-B", "-c", test_code]
        res = subprocess.run(cmd, env=env, cwd=REPO_ROOT, capture_output=True, text=False)

        self.assertEqual(res.returncode, 0, f"Error: {res.stderr.decode('latin1', errors='replace')}")

        # El log del subprocess debe haberse escrito en el directorio temporal (no en produccion)
        log_temporal = os.path.join(self.log_dir, "emision.log")
        self.assertTrue(os.path.exists(log_temporal), "El subprocess no escribio en STOCKLAB_LOG_DIR")
        with open(log_temporal, "r", encoding="utf-8") as f:
            self.assertIn("RESULT status=already_emitted", f.read())

        # Comprobar que no hay bytes > 127 en la salida de stdout
        non_ascii_out = [b for b in res.stdout if b > 127]
        self.assertEqual(len(non_ascii_out), 0, f"Salida contiene caracteres no-ASCII: {non_ascii_out}")

        non_ascii_err = [b for b in res.stderr if b > 127]
        self.assertEqual(len(non_ascii_err), 0, f"Stderr contiene caracteres no-ASCII: {non_ascii_err}")

    def test_04_linea_result_bien_formada(self):
        """(iv) Linea RESULT presente y bien formada."""
        mock_ret = {
            "status": "success",
            "fecha": "2026-10-14",
            "insertadas": 35,
            "resueltas": 4,
            "omitidos_sin_barra": ["ABC.MC", "XYZ.MC"],
        }
        with patch("services.emision.ejecutar_emision_diaria", return_value=mock_ret), \
             patch("emitir_senales.ejecutar_backup_tras_emision", return_value="ok"):
            import io
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                emitir_senales.release_lock(self.test_lock)
                code = emitir_senales.main()

            output = buf.getvalue()
            pattern = re.compile(
                r"^RESULT status=(\S+) sesion=(\S+) insertadas=(\d+) omitidos=(\d+) resueltas=(\d+) backup=(\S+)$",
                re.MULTILINE
            )
            m = pattern.search(output)
            self.assertIsNotNone(m, f"Linea RESULT no encontrada o mal formada en: {output}")
            self.assertEqual(m.group(1), "success")
            self.assertEqual(m.group(2), "2026-10-14")
            self.assertEqual(m.group(3), "35")
            self.assertEqual(m.group(4), "2")
            self.assertEqual(m.group(5), "4")
            self.assertEqual(m.group(6), "ok")

    def test_05_invocacion_backup_segun_condiciones(self):
        """(v) La copia solo se invoca si success/topped_up o resueltas > 0; su fallo no cambia el codigo."""
        mock_backup = MagicMock(return_value="fail")

        # Caso 1: status="success" -> se invoca backup; aunque devuelva fail, exit_code sigue siendo 0
        emitir_senales.release_lock(self.test_lock)
        with patch("services.emision.ejecutar_emision_diaria", return_value={"status": "success", "insertadas": 1, "resueltas": 0}), \
             patch("emitir_senales.ejecutar_backup_tras_emision", mock_backup):
            code = emitir_senales.main()
            mock_backup.assert_called_once()
            self.assertEqual(code, 0, "Fallo de backup no debe alterar el exit_code de la emision")

        mock_backup.reset_mock()

        # Caso 2: status="already_emitted" pero resueltas > 0 -> se invoca backup
        emitir_senales.release_lock(self.test_lock)
        with patch("services.emision.ejecutar_emision_diaria", return_value={"status": "already_emitted", "insertadas": 0, "resueltas": 3}), \
             patch("emitir_senales.ejecutar_backup_tras_emision", mock_backup):
            code = emitir_senales.main()
            mock_backup.assert_called_once()
            self.assertEqual(code, 0)

        mock_backup.reset_mock()

        # Caso 3: status="already_emitted" y resueltas == 0 -> NO se invoca backup
        emitir_senales.release_lock(self.test_lock)
        with patch("services.emision.ejecutar_emision_diaria", return_value={"status": "already_emitted", "insertadas": 0, "resueltas": 0}), \
             patch("emitir_senales.ejecutar_backup_tras_emision", mock_backup):
            code = emitir_senales.main()
            mock_backup.assert_not_called()
            self.assertEqual(code, 0)

        # Caso 4: status="late" -> NO se invoca backup
        emitir_senales.release_lock(self.test_lock)
        with patch("services.emision.ejecutar_emision_diaria", return_value={"status": "late", "insertadas": 0, "resueltas": 0}), \
             patch("emitir_senales.ejecutar_backup_tras_emision", mock_backup):
            code = emitir_senales.main()
            mock_backup.assert_not_called()
            self.assertEqual(code, 10)

    def test_06_powershell_script_parse_y_ascii(self):
        """(vi) El .ps1 pasa Parser::ParseFile sin errores y no contiene caracteres no-ASCII."""
        ps1_path = os.path.join(REPO_ROOT, "tools", "registrar_tarea_emision.ps1")
        self.assertTrue(os.path.exists(ps1_path))

        # 1. Comprobar que no hay bytes no-ASCII
        with open(ps1_path, "rb") as f:
            raw_bytes = f.read()
        non_ascii = [b for b in raw_bytes if b > 127]
        self.assertEqual(len(non_ascii), 0, f"El fichero contiene {len(non_ascii)} bytes no-ASCII")

        # 2. Comprobar que PowerShell lo parsea sin errores de sintaxis
        ps_cmd = (
            f"$errors = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{ps1_path}', [ref]$null, [ref]$errors); "
            f"if ($errors.Count -gt 0) {{ exit 1 }} else {{ exit 0 }}"
        )
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"PowerShell ParseFile fallo: {res.stderr}")

    def test_07_variables_de_entorno_en_tiempo_de_ejecucion_y_rutas_por_defecto(self):
        """(vii) STOCKLAB_LOCK_DIR y STOCKLAB_LOG_DIR se leen al ejecutar; sin ellas rige la ruta historica."""
        otro_dir = os.path.join(self.tmp_dir, "otro")
        os.makedirs(otro_dir)

        # Bloqueo: cambio de la variable DESPUES de importar emitir_senales -> se respeta
        self.assertEqual(emitir_senales.get_lock_file(), os.path.join(self.lock_dir, "emision.lock"))
        with patch.dict(os.environ, {"STOCKLAB_LOCK_DIR": otro_dir}):
            self.assertEqual(emitir_senales.get_lock_file(), os.path.join(otro_dir, "emision.lock"))

        # Sin variable (o vacia): tempfile.gettempdir() (solo se calcula la ruta, no se crea nada)
        esperado_lock = os.path.join(tempfile.gettempdir(), "emision.lock")
        with patch.dict(os.environ):
            os.environ.pop("STOCKLAB_LOCK_DIR", None)
            self.assertEqual(emitir_senales.get_lock_file(), esperado_lock)
            os.environ["STOCKLAB_LOCK_DIR"] = ""
            self.assertEqual(emitir_senales.get_lock_file(), esperado_lock)

        # Log: se sustituye RotatingFileHandler y os.makedirs para no crear nada en la ruta por defecto
        esperado_log_defecto = os.path.join(
            os.path.abspath(os.path.join(REPO_ROOT, "..", "stocklab_logs")), "emision.log"
        )
        with patch("emitir_senales.logging.handlers.RotatingFileHandler") as mock_handler, \
             patch("emitir_senales.os.makedirs") as mock_makedirs:
            with patch.dict(os.environ):
                os.environ.pop("STOCKLAB_LOG_DIR", None)
                emitir_senales.setup_emision_logger(REPO_ROOT)
                self.assertEqual(mock_handler.call_args[0][0], esperado_log_defecto)
                mock_makedirs.assert_called_with(os.path.dirname(esperado_log_defecto), exist_ok=True)

            with patch.dict(os.environ, {"STOCKLAB_LOG_DIR": otro_dir}):
                emitir_senales.setup_emision_logger(REPO_ROOT)
                self.assertEqual(mock_handler.call_args[0][0], os.path.join(otro_dir, "emision.log"))
                mock_makedirs.assert_called_with(otro_dir, exist_ok=True)


if __name__ == "__main__":
    unittest.main()
