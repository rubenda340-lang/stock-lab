[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [switch]$WakeToRun
)

$ErrorActionPreference = "Stop"

# 1. Determinar raiz del repositorio de forma dinamica
$RepoRoot = Split-Path -Path $PSScriptRoot -Parent

# 2. Resolver interprete de Python si no se especifico
if (-not $PythonExe) {
    $PythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($PythonCmd) {
        $PythonExe = $PythonCmd.Source
    } else {
        $PythonExe = "C:\Users\ruben\AppData\Local\Programs\Python\Python314\python.exe"
    }
}

if (-not (Test-Path $PythonExe)) {
    Write-Error "No se encontro el interprete de Python en: $PythonExe"
    exit 1
}

# 3. Exportar tarea previa si existe a stocklab_backups
$TaskName = "StockLab_Emision_Senales"
$BackupDir = Join-Path (Split-Path $RepoRoot -Parent) "stocklab_backups"
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    $DateStr = Get-Date -Format "yyyyMMdd_HHmmss"
    $ExportXmlPath = Join-Path $BackupDir "tarea_${TaskName}_${DateStr}.xml"
    Export-ScheduledTask -TaskName $TaskName | Out-File -FilePath $ExportXmlPath -Encoding ascii
    Write-Host "Tarea previa exportada a: $ExportXmlPath"
}

# 4. Definir accion con ruta absoluta y argumentos UTF-8
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "-B -X utf8 emitir_senales.py" -WorkingDirectory $RepoRoot

# 5. Definir 4 triggers semanales (Lunes a Viernes)
$Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
$Trigger1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At 18:40
$Trigger2 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At 20:00
$Trigger3 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At 22:00
$Trigger4 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At 07:45
$Triggers = @($Trigger1, $Trigger2, $Trigger3, $Trigger4)

# 6. Definir ajustes de ejecucion
$SettingsParams = @{
    StartWhenAvailable         = $true
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    MultipleInstances          = "IgnoreNew"
    ExecutionTimeLimit         = (New-TimeSpan -Minutes 30)
    RestartCount               = 2
    RestartInterval            = (New-TimeSpan -Minutes 10)
    RunOnlyIfNetworkAvailable  = $true
}
if ($WakeToRun) {
    $SettingsParams["WakeToRun"] = $true
}
$Settings = New-ScheduledTaskSettingsSet @SettingsParams

# 7. Registrar o reemplazar tarea (asigna al usuario actual con LogonType Interactive)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers -Settings $Settings -Description "Emision diaria automatica de senales predictivas StockLab" -Force | Out-Null

Write-Host "Tarea programada $TaskName registrada con exito."
Write-Host "Interprete: $PythonExe"
Write-Host "Directorio: $RepoRoot"
Write-Host "Horarios: 18:40, 20:00, 22:00 y 07:45 (Lunes a Viernes)"
