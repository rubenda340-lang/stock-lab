"""
Herramienta de Copia de Seguridad Automatica y Verificada — StockLab
===================================================================
Realiza backups consistentes y verificados de la base de datos SQLite
utilizando sqlite3.Connection.backup sobre una conexion de solo lectura.

Codigos de salida:
  0: Operacion correcta.
  2: Fallo de verificacion (integridad, esquema o sha256).
  3: Base de datos origen no encontrada o bloqueada tras 30 s de reintentos.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

# Reconfigurar stdout y stderr para tolerancia en consolas Windows (cp1252)
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PATTERN_BACKUP = re.compile(r"^stocklab_\d{8}_\d{6}\.db$")
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def validate_tag(tag: str) -> bool:
    """Valida que la etiqueta solo contenga [A-Za-z0-9_-] con longitud de 1 a 40 caracteres."""
    if not tag or not isinstance(tag, str):
        return False
    return bool(TAG_PATTERN.match(tag))


def get_default_paths():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    default_db = os.path.join(repo_root, "stocklab.db")
    default_dest = os.path.abspath(os.path.join(repo_root, "..", "stocklab_backups"))
    return default_db, default_dest


def setup_logger(dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    log_file = os.path.join(dest_dir, "backup.log")
    logger = logging.getLogger("backup_db")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def calculate_sha256(filepath):
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_git_head(repo_dir=None):
    try:
        cmd = ["git", "rev-parse", "HEAD"]
        res = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_tables_and_schema(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name;"
    )
    schema_rows = cur.fetchall()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;")
    tables = [r[0] for r in cur.fetchall()]

    counts = {}
    for t in tables:
        try:
            cur.execute(f'SELECT count(*) FROM "{t}";')
            counts[t] = cur.fetchone()[0]
        except Exception:
            counts[t] = -1
    return schema_rows, counts


def open_ro_connection(db_path, timeout=30.0, retry_interval=None):
    start = time.time()
    abs_path = os.path.abspath(db_path)
    if retry_interval is None:
        retry_interval = 0.1 if timeout <= 3.0 else 1.0

    if not os.path.exists(abs_path):
        while time.time() - start < timeout:
            time.sleep(min(retry_interval, max(0.01, timeout - (time.time() - start))))
            if os.path.exists(abs_path):
                break
        if not os.path.exists(abs_path):
            return None, "Archivo de base de datos no existe tras reintentos."

    uri = f"file:///{abs_path.replace(os.sep, '/')}?mode=ro"
    while time.time() - start < timeout:
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.execute("PRAGMA schema_version;")
            return conn, None
        except sqlite3.OperationalError:
            time.sleep(min(retry_interval, max(0.01, timeout - (time.time() - start))))
        except Exception as e:
            return None, str(e)

    # Último intento si timeout fue 0 o muy pequeño
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.execute("PRAGMA schema_version;")
        return conn, None
    except Exception:
        pass
    return None, f"Base de datos ocupada o bloqueada tras {timeout:.1f} s de reintentos."


def verify_backup(backup_path, logger=None):
    abs_backup = os.path.abspath(backup_path)
    manifest_path = abs_backup + ".manifest.json"

    if not os.path.exists(abs_backup):
        msg = f"ERROR: Archivo de copia no existe: {abs_backup}"
        print(msg)
        if logger:
            logger.error(msg)
        return False, 2

    if not os.path.exists(manifest_path):
        msg = f"ERROR: Manifest no encontrado para: {abs_backup}"
        print(msg)
        if logger:
            logger.error(msg)
        return False, 2

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        msg = f"ERROR: Error al leer manifest {manifest_path}: {e}"
        print(msg)
        if logger:
            logger.error(msg)
        return False, 2

    actual_sha = calculate_sha256(abs_backup)
    expected_sha = manifest.get("sha256")
    if actual_sha != expected_sha:
        msg = f"ERROR: SHA-256 no coincide. Esperado: {expected_sha}, Actual: {actual_sha}"
        print(msg)
        if logger:
            logger.error(msg)
        return False, 2

    # Verificar integridad con SQLite (abrir en modo solo lectura e immutable para no crear -wal/-shm)
    uri = f"file:///{abs_backup.replace(os.sep, '/')}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        res = cur.fetchall()
        conn.close()
        if res != [("ok",)]:
            msg = f"ERROR: PRAGMA integrity_check fallo: {res}"
            print(msg)
            if logger:
                logger.error(msg)
            return False, 2
    except Exception as e:
        msg = f"ERROR: Fallo al abrir copia para integrity_check: {e}"
        print(msg)
        if logger:
            logger.error(msg)
        return False, 2

    print(f"Verificacion OK: {os.path.basename(abs_backup)}")
    print("Integridad: ok")
    print(f"SHA-256: {actual_sha}")
    print(f"Tamano: {os.path.getsize(abs_backup)} bytes")
    print(f"Fecha UTC: {manifest.get('fecha_utc')}")
    print(f"Fecha Madrid: {manifest.get('fecha_madrid')}")
    if logger:
        logger.info("Verificacion exitosa para %s", abs_backup)
    return True, 0


def rotate_backups(dest_dir, keep_last=5, keep_daily=30, keep_monthly=12, logger=None):
    if not os.path.isdir(dest_dir):
        return

    all_files = os.listdir(dest_dir)
    managed_backups = []

    for fname in all_files:
        if not PATTERN_BACKUP.match(fname):
            continue
        db_file = os.path.join(dest_dir, fname)
        man_file = db_file + ".manifest.json"
        if not os.path.exists(man_file):
            continue
        try:
            with open(man_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("sha256"):
                continue
            if data.get("protected") is True or data.get("tag"):
                continue
        except Exception:
            continue
        managed_backups.append(fname)

    if not managed_backups:
        return

    # Orden descendente por nombre (equivale a orden temporal YYYYmmdd_HHMMSS)
    managed_backups.sort(reverse=True)

    to_keep = set()
    # 1. Conservar siempre las keep_last copias mas recientes sin importar el dia
    for b in managed_backups[:keep_last]:
        to_keep.add(b)

    # 2. Conservar hasta keep_daily copias diarias (la mas reciente de cada dia para los ultimos N dias)
    day_map = {}
    for b in managed_backups:
        # stocklab_YYYYmmdd_HHMMSS.db
        day_str = b.split("_")[1]
        if day_str not in day_map:
            day_map[day_str] = []
        day_map[day_str].append(b)

    sorted_days = sorted(day_map.keys(), reverse=True)
    daily_days = sorted_days[:keep_daily]
    for d in daily_days:
        to_keep.add(day_map[d][0])

    # 3. Conservar la mas reciente de cada mes (hasta keep_monthly meses)
    month_map = {}
    for b in managed_backups:
        month_str = b.split("_")[1][:6]  # YYYYmm
        if month_str not in month_map:
            month_map[month_str] = []
        month_map[month_str].append(b)

    sorted_months = sorted(month_map.keys(), reverse=True)
    monthly_months = sorted_months[:keep_monthly]
    for m in monthly_months:
        to_keep.add(month_map[m][0])

    # Rotar las no conservadas
    for b in managed_backups:
        if b not in to_keep:
            db_path = os.path.join(dest_dir, b)
            man_path = db_path + ".manifest.json"
            alt_json = db_path[:-3] + ".json"
            for p in (db_path, man_path, alt_json):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            msg = f"Rotada copia antigua: {b}"
            print(msg)
            if logger:
                logger.info(msg)


def list_backups(dest_dir):
    if not os.path.isdir(dest_dir):
        print(f"Directorio de destino no existe: {dest_dir}")
        return 0

    all_entries = sorted(os.listdir(dest_dir))
    db_files = [f for f in all_entries if f.endswith(".db")]

    if not db_files:
        print(f"No se encontraron copias en {dest_dir}")
        return 0

    print(f"{'Fecha':<20}  {'Tamano':<12}  {'Estado':<30}  {'Fichero'}")
    print(f"{'-'*20}  {'-'*12}  {'-'*30}  {'-'*35}")

    for fname in db_files:
        fpath = os.path.join(dest_dir, fname)
        size_str = f"{os.path.getsize(fpath)} B"
        mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M:%S")

        man_path = fpath + ".manifest.json"
        if os.path.exists(man_path):
            try:
                with open(man_path, "r", encoding="utf-8") as mf:
                    mdata = json.load(mf)
                date_display = mdata.get("fecha_utc", mtime)[:19].replace("T", " ")
                tag = mdata.get("tag")
                protected = mdata.get("protected", False)
                if tag or protected:
                    tag_info = f"tag={tag}" if tag else "protegida"
                    status = f"Verificada ({tag_info})"
                elif PATTERN_BACKUP.match(fname):
                    status = "Verificada"
                else:
                    status = "Verificada (manual)"
            except Exception:
                status = "Manifest corrupto"
                date_display = mtime
        else:
            date_display = mtime
            if PATTERN_BACKUP.match(fname):
                status = "Invalida (sin manifest)"
            else:
                status = "manual, no gestionada"

        print(f"{date_display:<20}  {size_str:<12}  {status:<30}  {fname}")
    return 0


def create_backup(
    db_path,
    dest_dir,
    tag=None,
    keep_last=5,
    keep_daily=30,
    keep_monthly=12,
    logger=None,
    retry_seconds=30.0,
):
    if tag is not None:
        if not validate_tag(tag):
            raise ValueError(
                f"Etiqueta de copia invalida: '{tag}'. Solo se permiten 1 a 40 caracteres alfanumericos, guion y guion bajo."
            )

    start_time = time.time()
    os.makedirs(dest_dir, exist_ok=True)

    # 1. Abrir origen en modo ro
    src_conn, err = open_ro_connection(db_path, timeout=retry_seconds)
    if err:
        msg = f"ERROR: {err}"
        print(msg)
        if logger:
            logger.error(msg)
        return False, 3, None

    # Obtener journal_mode, esquema y recuentos de origen antes de backup
    try:
        cur_src = src_conn.cursor()
        cur_src.execute("PRAGMA journal_mode;")
        jm_row = cur_src.fetchone()
        origen_journal_mode = str(jm_row[0]).lower() if jm_row else "unknown"
    except Exception:
        origen_journal_mode = "unknown"

    src_schema, src_counts = get_tables_and_schema(src_conn)

    # 2. Generar nombre estricto stocklab_YYYYmmdd_HHMMSS.db (o con sufijo _<tag>.db)
    now_utc = datetime.now(timezone.utc)
    ts_str = now_utc.strftime("%Y%m%d_%H%M%S")
    dest_filename = f"stocklab_{ts_str}_{tag}.db" if tag else f"stocklab_{ts_str}.db"
    dest_filepath = os.path.join(dest_dir, dest_filename)

    # Si ya existiese (mismo segundo), esperar un segundo
    if os.path.exists(dest_filepath):
        time.sleep(1.0)
        now_utc = datetime.now(timezone.utc)
        ts_str = now_utc.strftime("%Y%m%d_%H%M%S")
        dest_filename = f"stocklab_{ts_str}_{tag}.db" if tag else f"stocklab_{ts_str}.db"
        dest_filepath = os.path.join(dest_dir, dest_filename)

    print(f"Iniciando copia de seguridad de {db_path} -> {dest_filepath}...")
    if logger:
        logger.info("Iniciando copia de %s a %s", db_path, dest_filepath)

    try:
        dest_conn = sqlite3.connect(dest_filepath)
        src_conn.backup(dest_conn)
        # (i) Poner copia en journal_mode=DELETE para que quede un fichero único autosuficiente
        cur_dest = dest_conn.cursor()
        cur_dest.execute("PRAGMA journal_mode = DELETE;")
        copia_jm_row = cur_dest.fetchone()
        copia_journal_mode = str(copia_jm_row[0]).lower() if copia_jm_row else "delete"
        dest_conn.close()
    except Exception as e:
        msg = f"ERROR durante backup sqlite: {e}"
        print(msg)
        if logger:
            logger.error(msg)
        if os.path.exists(dest_filepath):
            try:
                os.remove(dest_filepath)
            except Exception:
                pass
        src_conn.close()
        return False, 2, None

    # 3. Verificacion de la copia
    try:
        uri_dest = f"file:///{dest_filepath.replace(os.sep, '/')}?mode=ro&immutable=1"
        verify_conn = sqlite3.connect(uri_dest, uri=True)
        cur = verify_conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        int_res = cur.fetchall()
        if int_res != [("ok",)]:
            raise RuntimeError(f"PRAGMA integrity_check fallo: {int_res}")

        dest_schema, dest_counts = get_tables_and_schema(verify_conn)
        verify_conn.close()
    except Exception as e:
        msg = f"ERROR en verificacion de la copia: {e}"
        print(msg)
        if logger:
            logger.error(msg)
        if os.path.exists(dest_filepath):
            os.remove(dest_filepath)
        src_conn.close()
        return False, 2, None

    schema_matches = (src_schema == dest_schema)
    if not schema_matches:
        msg = "ERROR: El esquema de la copia no coincide con el origen."
        print(msg)
        if logger:
            logger.error(msg)
        if os.path.exists(dest_filepath):
            os.remove(dest_filepath)
        src_conn.close()
        return False, 2, None

    count_diffs = {}
    for tbl, cnt in dest_counts.items():
        src_c = src_counts.get(tbl, 0)
        if cnt != src_c:
            count_diffs[tbl] = {"origen": src_c, "copia": cnt}

    if count_diffs:
        msg_diff = f"INFO: Diferencias de recuento detectadas por escritura concurrente: {count_diffs}"
        print(msg_diff)
        if logger:
            logger.info(msg_diff)

    src_conn.close()

    # 4. SHA-256 y Manifest
    file_sha256 = calculate_sha256(dest_filepath)
    now_madrid_str = None
    try:
        from zoneinfo import ZoneInfo
        now_madrid_str = now_utc.astimezone(ZoneInfo("Europe/Madrid")).isoformat()
    except Exception:
        pass

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    git_head = get_git_head(repo_dir)

    manifest_data = {
        "fecha_utc": now_utc.isoformat(),
        "fecha_madrid": now_madrid_str,
        "ruta_origen": os.path.abspath(db_path),
        "tamano_bytes": os.path.getsize(dest_filepath),
        "sha256": file_sha256,
        "origen_journal_mode": origen_journal_mode,
        "copia_journal_mode": copia_journal_mode,
        "recuentos_tablas": dest_counts,
        "recuentos_origen": src_counts,
        "esquema_coincide": schema_matches,
        "diferencias_recuentos": count_diffs,
        "sqlite_version": sqlite3.sqlite_version,
        "git_head": git_head,
        "tag": str(tag) if tag else None,
        "protected": bool(tag),
    }

    manifest_path = dest_filepath + ".manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest_data, mf, indent=2, ensure_ascii=True)

    elapsed = time.time() - start_time
    print(f"Copia completada con exito: {dest_filename}")
    print(f"Tamano: {manifest_data['tamano_bytes']} bytes")
    print(f"SHA-256: {file_sha256}")
    print(f"Tiempo: {elapsed:.3f} s")
    print(f"Manifest: {manifest_path}")
    if logger:
        logger.info(
            "Copia verificada completada: %s (tamano=%d bytes, sha=%s, tiempo=%.3fs)",
            dest_filename,
            manifest_data["tamano_bytes"],
            file_sha256,
            elapsed,
        )

    # 5. Rotacion
    rotate_backups(
        dest_dir,
        keep_last=keep_last,
        keep_daily=keep_daily,
        keep_monthly=keep_monthly,
        logger=logger,
    )

    return True, 0, manifest_data


def main():
    default_db, default_dest = get_default_paths()

    parser = argparse.ArgumentParser(description="Copia de seguridad automatica y verificada de stocklab.db")
    parser.add_argument("--db", default=default_db, help=f"Ruta a la base de datos origen (defecto: {default_db})")
    parser.add_argument("--dest", default=default_dest, help=f"Carpeta de destino para copias (defecto: {default_dest})")
    parser.add_argument("--tag", default=None, help="Etiqueta opcional para identificar y proteger la copia (solo [A-Za-z0-9_-], 1-40 caracteres)")
    parser.add_argument("--keep-last", type=int, default=5, help="Numero de copias mas recientes a conservar sin importar el dia (defecto: 5)")
    parser.add_argument("--keep-daily", type=int, default=30, help="Numero de copias diarias a conservar (defecto: 30)")
    parser.add_argument("--keep-monthly", type=int, default=12, help="Numero de copias mensuales a conservar (defecto: 12)")
    parser.add_argument("--retry-seconds", type=float, default=30.0, help="Segundos maximos de reintento si la BD no existe o esta bloqueada (defecto: 30.0)")
    parser.add_argument("--verify", help="Ruta a una copia .db para verificar integridad y SHA-256")
    parser.add_argument("--list", action="store_true", help="Listar copias en la carpeta de destino")

    args = parser.parse_args()
    logger = setup_logger(args.dest)

    if args.verify:
        success, code = verify_backup(args.verify, logger=logger)
        sys.exit(code)

    if args.list:
        code = list_backups(args.dest)
        sys.exit(code)

    if args.tag is not None:
        if not validate_tag(args.tag):
            msg = f"ERROR: Etiqueta invalida: '{args.tag}'. Solo se permiten 1 a 40 caracteres alfanumericos, guion y guion bajo."
            print(msg)
            if logger:
                logger.error(msg)
            sys.exit(2)

    success, code, manifest = create_backup(
        args.db,
        args.dest,
        tag=args.tag,
        keep_last=args.keep_last,
        keep_daily=args.keep_daily,
        keep_monthly=args.keep_monthly,
        logger=logger,
        retry_seconds=args.retry_seconds,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
