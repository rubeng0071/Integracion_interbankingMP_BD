# =====================================================================
# Function App: mp_webhook_function (HTTP trigger).
#
# Configuracion clave:
#   - Linux Consumption Plan (Y1), Python 3.11.
#   - System-assigned Managed Identity para leer Key Vault sin secretos.
#   - App Settings minimas (no-secretas); los secretos viven en KV y los
#     resuelve AzureSecretsClient via AZURE_KEY_VAULT_URI.
#   - HTTPS only + min TLS 1.2.
# =====================================================================
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

$Name = $FunctionAppMp
Write-Step "Function App: $Name (MP webhook + queue worker)"

# ---------- Crear (idempotente) ----------
$exists = az functionapp show --name $Name --resource-group $ResourceGroup `
    --query "id" -o tsv 2>$null
if (-not $exists) {
    az functionapp create `
        --name $Name --resource-group $ResourceGroup `
        --consumption-plan-location $Location `
        --runtime python --runtime-version $PythonVersion `
        --functions-version $FunctionsExt `
        --os-type Linux `
        --storage-account $StorageAccount `
        --output none
    Write-Ok "Creada"
} else {
    Write-Skip $Name
}

# ---------- HTTPS only + TLS minimo ----------
Write-Step "HTTPS only + TLS 1.2"
az functionapp update --name $Name --resource-group $ResourceGroup `
    --set httpsOnly=true --output none
az functionapp config set --name $Name --resource-group $ResourceGroup `
    --min-tls-version 1.2 --output none
Write-Ok "Aplicado"

# ---------- System-assigned MI ----------
Write-Step "System-assigned Managed Identity"
$principalId = az functionapp identity show `
    --name $Name --resource-group $ResourceGroup `
    --query "principalId" -o tsv 2>$null
if (-not $principalId) {
    $principalId = az functionapp identity assign `
        --name $Name --resource-group $ResourceGroup `
        --query "principalId" -o tsv
    Write-Ok "Asignada ($principalId)"
} else {
    Write-Skip "MI ya asignada ($principalId)"
}

# ---------- RBAC: MI -> Key Vault Secrets User ----------
Write-Step "RBAC: $Name MI lee secretos del Key Vault"
$scope = az keyvault show --name $KeyVault --query "id" -o tsv
$existingRole = az role assignment list --assignee $principalId --scope $scope `
    --role "Key Vault Secrets User" --query "[0].id" -o tsv 2>$null
if (-not $existingRole) {
    # El rol no se aplica inmediatamente; reintentamos 5 veces con backoff.
    $attempt = 0
    do {
        az role assignment create --assignee $principalId --scope $scope `
            --role "Key Vault Secrets User" --output none 2>$null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 5
        $attempt++
    } while ($attempt -lt 5)
    if ($LASTEXITCODE -eq 0) { Write-Ok "Asignado" } else { Fail "No pudo asignarse RBAC" }
} else {
    Write-Skip "ya asignado"
}

# ---------- App Settings ----------
# Las que viven aca son no-secretas + URI del KV. Los secretos los lee
# AzureSecretsClient via DefaultAzureCredential -> MI.
$aiConn = az monitor app-insights component show `
    --app $AppInsights --resource-group $ResourceGroup `
    --query "connectionString" -o tsv
$kvUri = "https://$KeyVault.vault.azure.net/"

Write-Step "Configurando App Settings"
# MP_POLLER_SCHEDULE: NCRONTAB cada 30 min para el batch poller que complementa el webhook.
# MP_INCREMENTAL_LOOKBACK_HOURS: ventana incremental por ciclo (4h cubre webhooks perdidos
#   sin pelearse con la idempotencia del upsert).
# MP_INITIAL_LOAD: poner "true" UNA VEZ para forzar carga histórica de MP_INITIAL_LOOKBACK_DAYS;
#   despues volver a "false" o el poller hace 365 dias en cada ciclo.
# MP_SEARCH_PAGE_DELAY_MS: respeta rate limit MP (~10 req/s segun doc Rapanui).
az functionapp config appsettings set `
    --name $Name --resource-group $ResourceGroup `
    --settings `
        "APPLICATIONINSIGHTS_CONNECTION_STRING=$aiConn" `
        "AZURE_KEY_VAULT_URI=$kvUri" `
        "MP_PAYMENT_QUEUE_NAME=$PaymentQueue" `
        "MP_POLLER_SCHEDULE=0 */30 * * * *" `
        "MP_INCREMENTAL_LOOKBACK_HOURS=4" `
        "MP_INITIAL_LOAD=false" `
        "MP_INITIAL_LOOKBACK_DAYS=365" `
        "MP_SEARCH_PAGE_DELAY_MS=200" `
        "LOG_LEVEL=INFO" `
        "PYTHON_ENABLE_WORKER_EXTENSIONS=1" `
    --output none
Write-Ok "Aplicado"

Write-Host ""
Write-Host "Function App MP OK. URL: https://$Name.azurewebsites.net" -ForegroundColor Green
Write-Host "Próximo paso: .\40-create-function-ib.ps1" -ForegroundColor Green
