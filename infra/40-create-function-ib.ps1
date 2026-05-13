# =====================================================================
# Function App: ib_poller (Timer trigger).
#
# Misma plataforma que la MP. App Settings agregan IB_POLLER_SCHEDULE
# e IB_INCREMENTAL_LOOKBACK_DAYS; los demas IB_* vienen de Key Vault.
# =====================================================================
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

$Name = $FunctionAppIb
Write-Step "Function App: $Name (IB poller)"

# ---------- Crear ----------
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

# ---------- HTTPS only + TLS ----------
Write-Step "HTTPS only + TLS 1.2"
az functionapp update --name $Name --resource-group $ResourceGroup `
    --set httpsOnly=true --output none
az functionapp config set --name $Name --resource-group $ResourceGroup `
    --min-tls-version 1.2 --output none
Write-Ok "Aplicado"

# ---------- Managed Identity ----------
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
    Write-Skip "ya asignada ($principalId)"
}

# ---------- RBAC ----------
Write-Step "RBAC: $Name MI lee secretos del Key Vault"
$scope = az keyvault show --name $KeyVault --query "id" -o tsv
$existingRole = az role assignment list --assignee $principalId --scope $scope `
    --role "Key Vault Secrets User" --query "[0].id" -o tsv 2>$null
if (-not $existingRole) {
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
$aiConn = az monitor app-insights component show `
    --app $AppInsights --resource-group $ResourceGroup `
    --query "connectionString" -o tsv
$kvUri = "https://$KeyVault.vault.azure.net/"

Write-Step "Configurando App Settings"
az functionapp config appsettings set `
    --name $Name --resource-group $ResourceGroup `
    --settings `
        "APPLICATIONINSIGHTS_CONNECTION_STRING=$aiConn" `
        "AZURE_KEY_VAULT_URI=$kvUri" `
        "IB_POLLER_SCHEDULE=0 */10 * * * *" `
        "IB_INCREMENTAL_LOOKBACK_DAYS=7" `
        "LOG_LEVEL=INFO" `
        "PYTHON_ENABLE_WORKER_EXTENSIONS=1" `
    --output none
Write-Ok "Aplicado"

Write-Host ""
Write-Host "Function App IB OK." -ForegroundColor Green
Write-Host "Próximo paso: .\50-load-secrets.ps1" -ForegroundColor Green
