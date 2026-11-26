# Emision Automatica de Senales Predictivas — StockLab IA

Este documento describe la arquitectura, planificacion, codigos de operacion y mantenimiento de la tarea automatica de emision diaria de senales predictivas (`StockLab_Emision_Senales`).

---

## 1. Horarios y Ventana Institucional de Emision

La emision diaria de senales requiere que la sesion bursatil del BME / Mercado Continuo espanol este consolidada.

- **Cierre oficial BME:** 17:30 CET (subasta de cierre hasta 17:35 CET).
- **Consolidacion de datos de proveedores:** Entre 18:00 y 18:30 CET.
- **Ventana de emision valida:** Desde las **18:30 CET** del dia de la sesion $S$ hasta las **08:30 CET** de la manana del dia habil siguiente $S+1$ (antes de la apertura de mercado de las 09:00 CET).
- **Idempotencia:** La primera ejecucion valida dentro de la ventana emite las senales (`status=success`). Cualquier ejecucion posterior para la misma sesion no duplica registros (`status=already_emitted` o `topped_up` si algun valor faltante recibio barra tardia). Si se intenta emitir despues de las 08:30 CET, el sistema aborta sin registrar filas con estado `late` (codigo de salida 10).

### Triggers Programados (Lunes a Viernes)

La tarea programada incluye 4 disparos diarios de lunes a viernes:
1. **18:40 CET (Principal):** Disparo primario inmediatamente posterior al corte oficial de mercado (18:30 CET). Si los proveedores tienen los datos frescos listos, las senales se emiten aqui.
2. **20:00 CET (Reintento de tarde):** Si algun proveedor tuvo retardo en publicar el cierre consolidado, este reintento cubre los valores pendientes (`topped_up`).
3. **22:00 CET (Cierre de noche):** Captura cualquier cotizacion rezagada antes del final de la jornada.
4. **07:45 CET (Matutino de rescate):** Se ejecuta antes de la apertura del dia siguiente. Cubre el escenario en el que el ordenador estuvo apagado durante toda la tarde/noche anterior, emitiendo antes de la campana de apertura de las 09:00 CET. El trigger del lunes a las 07:45 cubre la sesion del viernes previo.

---

## 2. Codigos de Salida de `emitir_senales.py`

El script devuelve codigos de salida semanticos para integracion con el Programador de tareas de Windows y monitorizacion:

| Codigo | Estado / Significado | Descripcion |
| :---: | :--- | :--- |
| **0** | `success` / `topped_up` / `already_emitted` | Emision completada correctamente, o completada parcialmente con reintento, o sesion ya cubierta (idempotente). |
| **10** | `late` | Ejecucion fuera de la ventana de emision permitida (posterior a las 08:30 CET de $S+1$). No se insertan predicciones. |
| **11** | `no_data` / `error_descarga` | Menos del 50 % de los valores tienen datos de cierre o fallo en la descarga de datos de mercado. |
| **12** | `lock_in_use` | Otra instancia de `emitir_senales.py` se encuentra en ejecucion activa (bloqueo mediante `emision.lock`). |
| **1** | `error` | Excepcion no controlada durante la ejecucion (ver traza completa en log). |

---

## 3. Log Rotativo y Monitorizacion

La tarea registra cada intento en un archivo de registro rotativo (1 MB por archivo, 5 rotaciones historicas):
- **Ruta del log:** `../stocklab_logs/emision.log` (ubicado fuera del repositorio, junto a `stocklab_backups/`). Configurable mediante la variable de entorno `STOCKLAB_LOG_DIR`.
- **Linea de resultado institucional:** Cada corrida escribe al final de la ejecucion:
  ```text
  RESULT status=<status> sesion=<YYYY-MM-DD> insertadas=<N> omitidos=<M> resueltas=<R> backup=<ok|fail|skip>
  ```

### Distincion entre Lineas RESULT Reales y de Prueba

- **Lineas de prueba historicas (anteriores a C-24d):** Se identifican por aparecer en rafagas de 3 o mas lineas `RESULT` en el mismo segundo (o separadas por escasos milisegundos), frecuentemente con fechas pasadas sinteticas (p. ej. `sesion=2026-10-14 insertadas=10`, `insertadas=2`, `insertadas=35` consecutivas) y alternando estados de prueba (`success`, `topped_up`, `already_emitted`, `late`, `no_data`, `error_descarga`, `error`).
- **Lineas reales de emision:** Presentan una unica linea `RESULT` por ejecucion, separada por horas o dias, precedida por su correspondiente cabecera `INICIO EMISION` cuya hora coincide con un disparador programado (18:40, 20:00, 22:00, 07:45), una recuperacion `StartWhenAvailable` al arrancar el equipo, o una ejecucion manual aislada.
- **Aislamiento a partir de C-24d:** La suite de tests ya no escribe en produccion. Toda ejecucion de pruebas redirige el log a carpetas temporales mediante `STOCKLAB_LOG_DIR` y el bloqueo mediante `STOCKLAB_LOCK_DIR`.

### Consultar el Estado y Ultimo Resultado de la Tarea
En PowerShell:
```powershell
Get-ScheduledTaskInfo -TaskName "StockLab_Emision_Senales" | Format-List LastRunTime, LastTaskResult, NextRunTime
```
Un `LastTaskResult` igual a `0` (o `10` si se lanzo a deshora) indica que el proceso finalizo de forma esperada.

### Ver las Ultimas Lineas del Registro
```powershell
Get-Content -Tail 20 "..\stocklab_logs\emision.log"
```

---

## 4. Ejecucion Manual

Para lanzar la emision manualmente desde la linea de comandos:
```powershell
python -B -X utf8 emitir_senales.py
```
Para forzar el recalculo incluso si ya existen senales registradas para la sesion:
```powershell
python -B -X utf8 emitir_senales.py --force
```

---

## 5. Administracion de la Tarea Programada

### Re-registrar o Actualizar la Tarea
Ejecutar el script automatizado desde la raiz del repositorio:
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\registrar_tarea_emision.ps1
```
*(Opcional: anadir `-WakeToRun` si se desea que Windows reactive el equipo del modo suspension para emitir).*

### Desactivar Temporalmente la Tarea
```powershell
Disable-ScheduledTask -TaskName "StockLab_Emision_Senales"
```
Para reactivarla:
```powershell
Enable-ScheduledTask -TaskName "StockLab_Emision_Senales"
```

### Restaurar la Tarea desde un XML Exportado
Cada vez que se ejecuta `registrar_tarea_emision.ps1`, la definicion anterior se respalda automaticamente en `../stocklab_backups/tarea_StockLab_Emision_Senales_<fecha>.xml`.

Para restaurar una version anterior:
```powershell
Register-ScheduledTask -TaskName "StockLab_Emision_Senales" -Xml (Get-Content "..\stocklab_backups\tarea_StockLab_Emision_Senales_YYYYMMDD_HHMMSS.xml" -Raw) -Force
```

---

## 6. Limitaciones Conocidas y Consideraciones Operativas

1. **Equipo Apagado:** Si el ordenador se encuentra apagado durante un disparo, el ajuste `StartWhenAvailable` hara que la tarea se ejecute tan pronto como el equipo se inicie. Si el encendido ocurre despues de las 08:30 CET de la sesion siguiente, el proceso finalizara de forma segura con codigo `10` (`late`), protegiendo la inmutabilidad de la serie viva.
2. **Bloqueo Atomico (`emision.lock`):** Se almacena en la carpeta temporal del sistema (`%TEMP%\emision.lock`, configurable via `STOCKLAB_LOCK_DIR`). Si un proceso previo se cierra abruptamente, cualquier intento posterior comprueba si el PID sigue existiendo y si tiene mas de 60 minutos; en caso de proceso huerfano, se reemplaza automaticamente sin requerir intervencion manual.
3. **Resolucion Estricta y Estado `sin_cotizacion` (C-25f):** `resolver_predicciones` liquida las senales vencidas exclusivamente contra la barra exacta de `fecha_objetivo` (nunca utiliza una barra posterior). Si un valor no presenta barra en su `fecha_objetivo`, permanece pendiente durante un periodo de gracia de hasta `SESIONES_SIN_COTIZACION = 5` sesiones habiles BME cerradas posteriores; si transcurridas 5 sesiones cerradas sigue sin barra exacta y el lote de mercado esta sano (al menos el 50 % de los valores con cierre valido en la ultima sesion cerrada), la prediccion pasa a `resuelta = 2` (`motivo_resolucion = 'sin_cotizacion'`) con los campos de resultado en `NULL`, quedando excluida tanto del calculo de acierto/fallo como de la lista de pendientes vivas.
4. **Variables de Entorno Opcionales (C-24d):**
   - `STOCKLAB_LOG_DIR`: Permite redirigir el directorio donde se escribe `emision.log` (por defecto `../stocklab_logs`). Leida en tiempo de ejecucion en `setup_emision_logger()`.
   - `STOCKLAB_LOCK_DIR`: Permite redirigir el directorio del fichero de bloqueo `emision.lock` (por defecto `tempfile.gettempdir()`, es decir `%TEMP%`). Leida en tiempo de ejecucion en `get_lock_file()`.
   - En produccion no se requiere definir ninguna de las dos variables; su proposito principal es el aislamiento estricto de pruebas unitarias y entornos CI.
5. **Politica de Retencion de Copias y Etiquetas Protegidas (C-24e):** `tools/backup_db.py` conserva la union de las 5 copias mas recientes (`keep_last=5`), la mas reciente de cada dia UTC de los ultimos 30 dias (`keep_daily=30`) y la mas reciente de cada mes UTC de los ultimos 12 meses (`keep_monthly=12`). La opcion CLI `--tag <etiqueta>` genera archivos nombrados `stocklab_YYYYmmdd_HHMMSS_<tag>.db` con `protected: true` en su manifest, blindandolos indefinidamente frente a cualquier rotacion automatica.
