# Protocolo de Copia de Seguridad y Restauracion de Base de Datos (StockLab)

Este documento describe la operativa de respaldos automaticos y el procedimiento manual de recuperacion de la base de datos principal (`stocklab.db`).

---

## 1. Copias de Seguridad Automaticas (`tools/backup_db.py`)

StockLab incluye una herramienta oficial de emision y verificacion de respaldos en caliente:

```bash
python tools/backup_db.py [--db RUTA_BD] [--dest RUTA_DESTINO] [--tag ETIQUETA] [--keep-last 5] [--keep-daily 30] [--keep-monthly 12]
```

### Parametros y Valores por Defecto
- `--db`: Ruta a la base de datos de origen (defecto: `stocklab.db` en la raiz del proyecto).
- `--dest`: Carpeta de destino (defecto: `../stocklab_backups/` fuera del repositorio).
- `--tag <etiqueta>`: Etiqueta opcional (`[A-Za-z0-9_-]`, 1 a 40 caracteres) para nombrar y blindar la copia (`stocklab_YYYYmmdd_HHMMSS_<tag>.db`) con `"protected": true`.
- `--keep-last`: Numero de copias mas recientes a conservar sin importar el dia (defecto: `5`).
- `--keep-daily`: Numero de copias diarias a conservar (defecto: `30`).
- `--keep-monthly`: Numero de copias mensuales a conservar (defecto: `12`).
- `--verify <fichero_db>`: Recalcula integridad y SHA-256 contra su manifest.
- `--list`: Muestra el inventario de copias en el directorio de respaldo.

### Caracteristicas de Seguridad
1. **Conexion en Solo Lectura**: Abre la base de datos con URI `mode=ro`, permitiendo realizar copias consistentes mientras la aplicacion y las vistas de usuario estan activas.
2. **API Oficial de Backup**: Utiliza `sqlite3.Connection.backup()`, garantizando transaccionalidad sin riesgo de corrupcion por escrituras concurrentes.
3. **Nombre Estricto**: Formato `stocklab_YYYYmmdd_HHMMSS.db` (o `stocklab_YYYYmmdd_HHMMSS_<tag>.db` si se especifica `--tag`).
4. **Manifest Criptografico**: Cada copia va acompanada de `stocklab_YYYYmmdd_HHMMSS[_<tag>].db.manifest.json` con:
   - Timestamps en UTC y Hora de Madrid.
   - Hash SHA-256 del archivo.
   - Etiqueta (`tag`) y estado de proteccion (`protected`).
   - Recuentos de filas por tabla.
   - Comprobacion de coincidencia de esquema (`sqlite_master`).
   - Version de SQLite y commit actual de Git (`HEAD`).
5. **Autoverificacion Inmediata**: Tras la copia se ejecuta `PRAGMA integrity_check` y se verifica el hash. Si falla, el archivo incompleto se elimina de inmediato y el proceso termina con codigo de salida `2`.
6. **Politica de Retencion Segura (C-24e)**:
   - Conserva la **union** de:
     * Las `keep_last` copias mas recientes (por defecto 5), evitando que una copia de la tarde elimine la de la manana del mismo dia.
     * La copia mas reciente de cada dia UTC de los ultimos 30 dias (`keep_daily=30`).
     * La copia mas reciente de cada mes UTC de los ultimos 12 meses (`keep_monthly=12`).
   - **Copias protegidas**: Nunca se borran ni rotan copias con etiqueta (`--tag`), con `"protected": true`, ni copias manuales no gestionadas (como `stocklab_*_pre_emision*.db`). Al rotar una copia no conservada, se elimina tanto el `.db` como su `.manifest.json`.

---

## 2. Inventario y Verificacion de Copias

### Listar respaldos existentes
```bash
python tools/backup_db.py --list
```

Salida esperada:
```text
Fecha                 Tamano        Estado                          Fichero
--------------------  ------------  ------------------------------  -----------------------------------
2026-10-12 21:44:14   851968 B      Verificada                      stocklab_20261012_234414.db
2026-10-21 18:40:00   1929216 B     Verificada (tag=pre_c06)        stocklab_20261021_184000_pre_c06.db
2026-10-12 22:44:20   851968 B      manual, no gestionada           stocklab_2026-10-12_pre_emision.db
```

### Verificar una copia especifica
```bash
python tools/backup_db.py --verify D:\Escritorio\stocklab_backups\stocklab_20261012_234414.db
```

Codigos de salida:
- `0`: Verificacion correcta.
- `2`: Fallo de integridad o hash.

---

## 3. Protocolo Manual de Restauracion (Desastre o Corrupcion)

> [!WARNING]
> **Advertencia de modo WAL (Write-Ahead Logging):**
> Con la base de datos en modo WAL, **NO se debe copiar `stocklab.db` a mano** (por ejemplo mediante el Explorador de Windows o `Copy-Item`) mientras la aplicación Streamlit o el proceso de emisión estén en marcha: en caliente, las transacciones recientes residen en `stocklab.db-wal` y una copia a mano quedará incompleta o desfasada. Debe usarse **siempre** `python tools/backup_db.py`, que realiza una instantánea transaccional consistente y produce un archivo único autosuficiente en modo `DELETE`.

> [!CAUTION]
> **NO existe restauracion automatica.** La restauracion de la base de datos debe ser siempre un acto consciente y supervisado por un operador humano para evitar sobreescrituras accidentales de la serie historica.

Siga estrictamente los siguientes pasos:

### Paso 1: Detener la aplicacion
Asegurese de que ningun proceso de Streamlit o tarea de emision programada este ejecutandose:
```powershell
Get-Process | Where-Object { $_.ProcessName -match "python|streamlit" }
```
Si existen procesos activos, detengalos ordenadamente.

### Paso 2: Aislar la base de datos danada y purgar archivos WAL
**Nunca borre directamente la base de datos existente.** Renombre el archivo actual con el sufijo `.roto`:
```powershell
Rename-Item stocklab.db stocklab.db.roto
```
**Importante al restaurar:** Borre antes cualquier archivo `stocklab.db-wal` o `stocklab.db-shm` antiguo para evitar inconsistencias con la base restaurada:
```powershell
Remove-Item stocklab.db-wal, stocklab.db-shm -ErrorAction SilentlyContinue
```

### Paso 3: Seleccionar y verificar la copia de respaldo
Identifique la copia verificada mas reciente en el directorio de copias y compruebe su integridad antes de moverla:
```powershell
python tools/backup_db.py --verify D:\Escritorio\stocklab_backups\stocklab_YYYYmmdd_HHMMSS.db
```
Compruebe que devuelve `Integridad: ok`, `SHA-256` coincidente y codigo de salida `0`.

### Paso 4: Copiar la copia seleccionada al repositorio
Copie el archivo restaurado a la ubicacion oficial:
```powershell
Copy-Item D:\Escritorio\stocklab_backups\stocklab_YYYYmmdd_HHMMSS.db stocklab.db
```

### Paso 5: Comprobacion final en la ubicacion destino
Verifique que la nueva `stocklab.db` responde correctamente en modo lectura:
```powershell
python -c "import sqlite3; conn = sqlite3.connect('file:stocklab.db?mode=ro', uri=True); print('Integridad:', conn.execute('PRAGMA integrity_check;').fetchall())"
```

### Paso 6: Reiniciar los servicios
Reinicie la aplicacion Streamlit o el servicio correspondiente:
```powershell
streamlit run app.py
```

---

## 4. Recomendacion de Respaldo Secundario (Regla 3-2-1)

- **Copia fuera del disco D:**: La carpeta `D:\Escritorio\stocklab_backups\` se encuentra en el mismo disco fisico que el proyecto. Ante un fallo de hardware del disco D:, ambas copias se perderian.
- Se recomienda configurar una sincronizacion periodica o copia secundaria hacia una unidad externa (ej. disco USB, NAS local) o almacenamiento en la nube cifrado.
