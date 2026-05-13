# =====================================================================
# Foundation: Resource Group, Log Analytics, Application Insights,
# Key Vault (RBAC), Storage Account.
#
# Idempotente: cada `az X show` evita un re-create si el recurso existe.
# =====================================================================
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

# ---------- Resource Group ----------
Write-Step "Resource Group: $ResourceGroup"
$rg = az group show --name $ResourceGroup --query "id" -o tsv 2>$null
if (-not $rg) {
    az group create --name $ResourceGroup --location $Location --output none
    Write-Ok "Creado en $Location"
} else {
    Write-Skip $ResourceGroup
}

# ---------- Log Analytics Workspace ----------
# Backend de AppInsights; los logs/metricas se ingieren acá.
Write-Step "Log Analytics: $LogWorkspace"
$logId = az monitor log-analytics workspace show `
    --resource-group $ResourceGroup --workspace-name $LogWorkspace `
    --query "id" -o tsv 2>$null
if (-not $logId) {
    $logId = az monitor log-analytics workspace create `
        --resource-group $ResourceGroup --workspace-name $LogWorkspace `
        --location $Location --query "id" -o tsv
    Write-Ok "Creado"
} else {
    Write-Skip $LogWorkspace
}

# ---------- Application Insights ----------
# Usamos workspace-based (no classic). Conexion string requerido por las
# Functions; lo capturamos para que 30-/40- lo agreguen como app setting.
Write-Step "Application Insights: $AppInsights"
$aiResource = az monitor app-insights component show `
    --app $AppInsights --resource-group $ResourceGroup `
    --query "connectionString" -o tsv 2>$null
if (-not $aiResource) {
    $aiResource = az monitor app-insights component create `
        --app $AppInsights --resource-group $ResourceGroup `
        --location $Location --workspace $logId `
        --query "connectionString" -o tsv
    Write-Ok "Creado"
} else {
    Write-Skip $AppInsights
}

# ---------- Storage Account ----------
# AzureWebJobsStorage de ambas Functions + Queue del refactor J.
# StorageV2 + Standard_LRS es suficiente para Functions; no necesitamos
# georedundancia para datos efímeros (queue/blob de triggers).
Write-Step "Storage Account: $StorageAccount"
$stId = az storage account show `
    --name $StorageAccount --resource-group $ResourceGroup `
    --query "id" -o tsv 2>$null
if (-not $stId) {
    az storage account create `
        --name $StorageAccount --resource-group $ResourceGroup `
        --location $Location --sku Standard_LRS --kind StorageV2 `
        --min-tls-version TLS1_2 --allow-blob-public-access false `
        --output none
    Write-Ok "Creado"
} else {
    Write-Skip $StorageAccount
}

# ---------- Key Vault (RBAC) ----------
# enable-rbac-authorization=true: usamos RBAC roles, no Access Policies.
# soft-delete está ON por default desde 2020.
Write-Step "Key Vault: $KeyVault (RBAC mode)"
$kvId = az keyvault show `
    --name $KeyVault --resource-group $ResourceGroup `
    --query "id" -o tsv 2>$null
if (-not $kvId) {
    az keyvault create `
        --name $KeyVault --resource-group $ResourceGroup `
        --location $Location --enable-rbac-authorization true `
        --retention-days 7 `
        --output none
    Write-Ok "Creado"
} else {
    # Si fue creado en modo Access Policies, lo migramos a RBAC.
    $rbacEnabled = az keyvault show `
        --name $KeyVault --resource-group $ResourceGroup `
        --query "properties.enableRbacAuthorization" -o tsv 2>$null
    if ($rbacEnabled -ne "true") {
        Write-Step "Migrando $KeyVault a modo RBAC"
        az keyvault update --name $KeyVault `
            --enable-rbac-authorization true --output none
        Write-Ok "RBAC habilitado"
    } else {
        Write-Skip "$KeyVault (RBAC ya activo)"
    }
}

# Otorgamos al usuario actual el rol Key Vault Secrets Officer para que
# pueda cargar secretos desde 50-load-secrets.ps1.
$me = az ad signed-in-user show --query "id" -o tsv
$scope = az keyvault show --name $KeyVault --query "id" -o tsv
Write-Step "RBAC: Key Vault Secrets Officer al usuario actual"
$existing = az role assignment list --assignee $me --scope $scope `
    --role "Key Vault Secrets Officer" --query "[0].id" -o tsv 2>$null
if (-not $existing) {
    az role assignment create --assignee $me --scope $scope `
        --role "Key Vault Secrets Officer" --output none
    Write-Ok "Asignado"
} else {
    Write-Skip "ya asignado"
}

Write-Host ""
Write-Host "Foundation OK. Próximo paso: .\20-create-sql.ps1" -ForegroundColor Green
Write-Host ""
Write-Host "Output útil para los siguientes scripts:" -ForegroundColor DarkCyan
Write-Host "  ResourceGroup:   $ResourceGroup"
Write-Host "  Location:        $Location"
Write-Host "  Storage:         $StorageAccount"
Write-Host "  KeyVault:        $KeyVault"
Write-Host "  AppInsights:     $AppInsights"
