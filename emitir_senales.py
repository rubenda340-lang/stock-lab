"""
Script de Emision Oficial de Senales Predictivas - StockLab IA
=============================================================
Ejecutar diariamente tras el cierre oficial de mercado (18:30 CET o posterior).
Apto para Programador de tareas de Windows y ejecucion manual.

Uso:
    python emitir_senales.py          # Emision normal (idempotente)
    python emitir_senales.py --force  # Forzar emision
"""

from datetime import datetime
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile
import time
import traceback

# 1. Reconfigurar stdout y stderr para tolerancia en consolas Windows (cp1252)
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 2. Silenciar warnings ruidosos de streamlit en ejecucion batch de consola
for _name in (
    "streamlit",
    "streamlit.runtime",
    "streamlit.runtime.caching",
    "streamlit.runtime.caching.cache_data_api",
    "streamlit.runtime.scriptrunner_utils.script_run_context",
    "streamlit.runtime.state.session_state_proxy",
):
    logging.getLogger(_name).setLevel(logging.ERROR)

# 3. Asegurar importacion del directorio raiz del proyecto
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def get_lock_file() -> str:
    """
    Ruta del fichero de bloqueo, resuelta en tiempo de ejecucion (no al importar).
    Directorio: variable de entorno STOCKLAB_LOCK_DIR si esta definida y no vacia;
    en caso contrario tempfile.gettempdir() (comportamiento historico).
    """
    lock_dir = os.environ.get("STOCKLAB_LOCK_DIR") or tempfile.gettempdir()
    return os.path.join(lock_dir, "emision.lock")


def _is_pid_alive(pid: int) -> bool:
    """Comprueba si un proceso sigue vivo de forma segura en Windows y Unix."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h_proc:
                return False
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code))
            kernel32.CloseHandle(h_proc)
            return exit_code.value == 259  # STILL_ACTIVE
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def acquire_lock(lock_path: str = None) -> bool:
    """
    Adquiere bloqueo de instancia unica mediante creacion atomica O_CREAT | O_EXCL.
    Si el fichero existe pero el PID no vive o tiene mas de 60 min, se reemplaza.
    Si lock_path es None se usa get_lock_file().
    """
    if lock_path is None:
        lock_path = get_lock_file()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for _ in range(2):
        try:
            fd = os.open(lock_path, flags)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(f"{os.getpid()},{time.time():.3f}\n")
            return True
        except FileExistsError:
            is_stale = False
            try:
                with open(lock_path, "r", encoding="ascii") as f:
                    content = f.read().strip()
                parts = content.split(",")
                pid = int(parts[0])
                ts = float(parts[1]) if len(parts) > 1 else 0.0
                age = time.time() - ts
                if not _is_pid_alive(pid) or age > 3600.0:
                    is_stale = True
            except Exception:
                is_stale = True

            if is_stale:
                try:
                    os.remove(lock_path)
                    continue
                except Exception:
                    pass
            return False
    return False


def release_lock(lock_path: str = None):
    """Libera el fichero de bloqueo solo si pertenece al proceso actual."""
    if lock_path is None:
        lock_path = get_lock_file()
    try:
        if os.path.exists(lock_path):
            with open(lock_path, "r", encoding="ascii") as f:
                content = f.read().strip()
            parts = content.split(",")
            if parts and int(parts[0]) == os.getpid():
                os.remove(lock_path)
    except Exception:
        pass


def setup_emision_logger(repo_root: str):
    """
    Configura logger rotativo (1 MB x 5) en <repo>/../stocklab_logs/emision.log.
    El directorio se puede sustituir con la variable de entorno STOCKLAB_LOG_DIR,
    leida en tiempo de ejecucion (si no esta definida o esta vacia, ruta historica).
    """
    log_dir = os.environ.get("STOCKLAB_LOG_DIR") or os.path.join(repo_root, "..", "stocklab_logs")
    log_dir = os.path.abspath(log_dir)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "emision.log")

    logger = logging.getLogger("stocklab.emision_runner")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        h.close()
    logger.handlers.clear()

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1024 * 1024, backupCount=5, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def get_madrid_time_iso() -> str:
    """Devuelve la fecha/hora actual en zona horaria Europe/Madrid."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M:%S CET")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M:%S")


def ejecutar_backup_tras_emision(repo_root: str, logger) -> str:
    """Invoca tools/backup_db.py de forma desacoplada y devuelve ok/fail."""
    backup_script = os.path.join(repo_root, "tools", "backup_db.py")
    cmd = [sys.executable, "-B", backup_script]
    try:
        res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            logger.info("Backup verificado completado con exito tras emision.")
            return "ok"
        else:
            err_msg = res.stderr.strip() or res.stdout.strip()
            logger.warning("Fallo en backup tras emision (codigo %d): %s", res.returncode, err_msg)
            return "fail"
    except Exception as e:
        logger.warning("Excepcion al ejecutar backup tras emision: %s", e)
        return "fail"


def main() -> int:
    logger = setup_emision_logger(REPO_ROOT)
    madrid_str = get_madrid_time_iso()
    py_ver = sys.version.replace("\n", " ")

    logger.info("-----------------------------------------------------------------")
    logger.info("INICIO EMISION - Madrid: %s - Python: %s", madrid_str, py_ver)

    lock_file = get_lock_file()
    if not acquire_lock(lock_file):
        msg = "Bloqueo activo: otra instancia de emitir_senales.py esta en ejecucion."
        print(f"INFO: {msg}")
        logger.warning("Instancia duplicada detectada. Salida codigo 12.")
        return 12

    status = "error"
    sesion = "n/a"
    insertadas = 0
    omitidos = 0
    resueltas = 0
    backup_status = "skip"
    exit_code = 1

    try:
        force_flag = "--force" in sys.argv
        print("=================================================================")
        print("  STOCKLAB IA - EMISION DIARIA CONGELADA AL CIERRE")
        print("=================================================================")

        from services.emision import ejecutar_emision_diaria

        res = ejecutar_emision_diaria(force=force_flag, verbose=True)

        status = str(res.get("status", "unknown"))
        sesion = str(res.get("fecha", "n/a"))
        insertadas = int(res.get("insertadas", res.get("emitidas", 0)))
        resueltas = int(res.get("resueltas", 0))
        omitidos_list = res.get("omitidos_sin_barra", [])
        omitidos = len(omitidos_list) if isinstance(omitidos_list, list) else 0

        print("=================================================================")
        print(f"  Resultado: {status} | Sesion: {sesion} | Insertadas: {insertadas}")
        print("=================================================================")

        # Si status es success o topped_up, o resueltas > 0, ejecuta copia de seguridad
        if status in ("success", "topped_up") or resueltas > 0:
            print("Iniciando copia de seguridad de la base de datos tras la emision...")
            backup_status = ejecutar_backup_tras_emision(REPO_ROOT, logger)
        else:
            backup_status = "skip"

        # Mapeo oficial de codigos de salida:
        # 0 = success / topped_up / already_emitted
        # 10 = late
        # 11 = no_data / error_descarga
        # 1 = error inesperado
        if status in ("success", "topped_up", "already_emitted"):
            exit_code = 0
        elif status == "late":
            exit_code = 10
        elif status in ("no_data", "error_descarga"):
            exit_code = 11
        else:
            exit_code = 1

    except Exception as e:
        exit_code = 1
        status = "error"
        backup_status = "skip"
        tb_str = traceback.format_exc()
        logger.error("EXCEPCION NO CONTROLADA EN EMISION:\n%s", tb_str)
        print("ERROR INESPERADO EN EMISION:")
        print(tb_str)

    finally:
        release_lock(lock_file)

    result_line = (
        f"RESULT status={status} sesion={sesion} insertadas={insertadas} "
        f"omitidos={omitidos} resueltas={resueltas} backup={backup_status}"
    )
    logger.info(result_line)
    print(result_line)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
