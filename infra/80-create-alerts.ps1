# =====================================================================
# Alertas de Azure Monitor (4 minimas + Action Group con email).
#
# Lo que cubre:
#   1. Function App MP: Http5xx > 5 en 5 min  (webhook degradado).
#   2. Function App IB: ejecuciones con Result=Failed > 1 en 1 hora.
#   3. SQL DB: CPU > 80% sostenido 15 min.
#   4. Queue mp-payment-ids: > 100 mensajes pending (worker atrasado).
#
# Dashboards y alerta sobre "sync IB sin SUCCESS en 30 min" (que requiere
# KQL log query) quedan como mejora futura: portal de Azure es mejor
# canal para armarlos visualmente.
#
# Uso:
#   .\80-create-alerts.ps1 -NotifyEmail you@example.com
# =====================================================================
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$NotifyEmail
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

if (-not ($NotifyEmail -match '^[^@]+@[^@]+\.[^@]+$')) {
    Fail "Email invalido: $NotifyEmail"
}

# ---------------------------------------------------------------------
# Action Group (destinos de notificacion)
# ---------------------------------------------------------------------
$ActionGroup = "ag-$Suffix-$Env"
Write-Step "Action Group: $ActionGroup -> $NotifyEmail"

$agId = az monitor action-group show `
    --resource-group $ResourceGroup --name $ActionGroup `
    --query "id" -o tsv 2>$null
if (-not $agId) {
    # Short name <= 12 chars; deriva de Suffix.
    $shortName = ($Suffix.Replace("-","") + $Env)
    if ($shortName.Length -gt 12) { $shortName = $shortName.Substring(0,12) }
    $agId = az monitor action-group create `
        --resource-group $ResourceGroup --name $ActionGroup `
        --short-name $shortName `
        --action email "ops-email" $NotifyEmail `
        --query "id" -o tsv
    Write-Ok "Creado"
} else {
    Write-Skip $ActionGroup
}

# ---------------------------------------------------------------------
# IDs de los recursos que vamos a monitorear
# ---------------------------------------------------------------------
$mpId  = az functionapp show --name $FunctionAppMp --resource-group $ResourceGroup --query "id" -o tsv
$ibId  = az functionapp show --name $FunctionAppIb --resource-group $ResourceGroup --query "id" -o tsv
$sqlId = az sql db show --name $SqlDatabase --server $SqlServer --resource-group $ResourceGroup --query "id" -o tsv
$stId  = az storage account show --name $StorageAccount --resource-group $ResourceGroup --query "id" -o tsv

# Helper: crea alerta solo si no existe.
function New-AlertIfMissing($name, $resourceId, $condition, $severity, $description) {
    $exists = az monitor metrics alert show `
        --resource-group $ResourceGroup --name $name `
        --query "id" -o tsv 2>$null
    if ($exists) {
        Write-Skip $name
        return
    }
    az monitor metrics alert create `
        --name $name --resource-group $ResourceGroup `
        --scopes $resourceId `
        --condition $condition `
        --description $description `
        --severity $severity `
        --action $agId `
        --evaluation-frequency "1m" `
        --window-size "5m" `
        --output none
    Write-Ok $name
}

# ---------------------------------------------------------------------
# Alerta 1: MP webhook con muchos 5xx
# ---------------------------------------------------------------------
Write-Step "Alerta 1: $FunctionAppMp Http5xx > 5 en 5 min"
New-AlertIfMissing `
    -name "alert-mp-5xx" `
    -resourceId $mpId `
    -condition "total Http5xx > 5" `
    -severity 2 `
    -description "Webhook MP devolvio mas de 5 errores 5xx en los ultimos 5 minutos"

# ---------------------------------------------------------------------
# Alerta 2: IB poller con ejecuciones fallidas
# Metric: FunctionExecutionCount con dimension Result=Failed.
# Como la dim filter en az CLI v2 metrics alert es engorrosa, usamos la
# metric simple "FunctionExecutionUnits" como proxy: si hay 0 unidades
# en 30min, el cron no esta corriendo. Como alerta complementaria,
# manejaremos los Failed via log query desde portal (queda como mejora).
# ---------------------------------------------------------------------
Write-Step "Alerta 2: $FunctionAppIb sin ejecuciones en 30 min"
$exists = az monitor metrics alert show --resource-group $ResourceGroup `
    --name "alert-ib-no-runs" --query "id" -o tsv 2>$null
if ($exists) {
    Write-Skip "alert-ib-no-runs"
} else {
    az monitor metrics alert create `
        --name "alert-ib-no-runs" --resource-group $ResourceGroup `
        --scopes $ibId `
        --condition "total FunctionExecutionCount < 1" `
        --description "Poller IB no ejecuto ninguna corrida en los ultimos 30 min (cron deberia disparar c/10min)" `
        --severity 1 `
        --action $agId `
        --evaluation-frequency "5m" `
        --window-size "30m" `
        --output none
    Write-Ok "alert-ib-no-runs"
}

# ---------------------------------------------------------------------
# Alerta 3: SQL CPU > 80% sostenido
# En serverless usamos cpu_percent.
# ---------------------------------------------------------------------
Write-Step "Alerta 3: SQL DB CPU > 80%"
$exists = az monitor metrics alert show --resource-group $ResourceGroup `
    --name "alert-sql-cpu-high" --query "id" -o tsv 2>$null
if ($exists) {
    Write-Skip "alert-sql-cpu-high"
} else {
    az monitor metrics alert create `
        --name "alert-sql-cpu-high" --resource-group $ResourceGroup `
        --scopes $sqlId `
        --condition "avg cpu_percent > 80" `
        --description "SQL DB con CPU >80% sostenido 15 minutos" `
        --severity 2 `
        --action $agId `
        --evaluation-frequency "5m" `
        --window-size "15m" `
        --output none
    Write-Ok "alert-sql-cpu-high"
}

# ---------------------------------------------------------------------
# Alerta 4: Queue del webhook acumulando mensajes
# Metric: QueueMessageCount. La metrica viene del Storage Account, no
# de la Function. Filtramos por nombre de queue como dimension.
# ---------------------------------------------------------------------
Write-Step "Alerta 4: Queue $PaymentQueue con backlog > 100"
$exists = az monitor metrics alert show --resource-group $ResourceGroup `
    --name "alert-queue-backlog" --query "id" -o tsv 2>$null
if ($exists) {
    Write-Skip "alert-queue-backlog"
} else {
    # Scope al servicio queue del storage; la metrica QueueMessageCount
    # se reporta por queue via dimension.
    $queueServiceId = "$stId/queueServices/default"
    az monitor metrics alert create `
        --name "alert-queue-backlog" --resource-group $ResourceGroup `
        --scopes $queueServiceId `
        --condition "max QueueMessageCount > 100 where QueueName includes $PaymentQueue" `
        --description "Queue del webhook async con >100 mensajes pending; worker atrasado" `
        --severity 2 `
        --action $agId `
        --evaluation-frequency "5m" `
        --window-size "15m" `
        --output none
    Write-Ok "alert-queue-backlog"
}

Write-Host ""
Write-Host "Alertas creadas. Notificacion a: $NotifyEmail" -ForegroundColor Green
Write-Host ""
Write-Host "Para deshabilitar temporalmente una alerta:" -ForegroundColor DarkCyan
Write-Host "  az monitor metrics alert update --name <alert> --resource-group $ResourceGroup --enabled false"
Write-Host ""
Write-Host "Para ver el historial de disparos:" -ForegroundColor DarkCyan
Write-Host "  Portal -> Monitor -> Alerts -> Fired alerts"
