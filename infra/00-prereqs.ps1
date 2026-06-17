# =====================================================================
# Prerequisitos: valida az CLI, login, providers registrados, herramientas
# locales (func CLI, build, pyodbc).
# =====================================================================
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"

Write-Step "Verificando az CLI"
# PowerShell pierde las comillas dobles internas que JMESPath necesita para
# claves con guion como 'azure-cli'. Mejor parsear el JSON completo.
$azJson = az version -o json 2>$null | ConvertFrom-Json
$azVersion = if ($azJson) { $azJson.'azure-cli' } else { $null }
if (-not $azVersion) { Fail "az CLI no encontrado. Instalá con: winget install Microsoft.AzureCLI" }
Write-Ok "az $azVersion"

Write-Step "Verificando login"
Assert-AzReady
$accountName = az account show --query "name" -o tsv
$accountId   = az account show --query "id" -o tsv
Write-Ok "Suscripción: $accountName ($accountId)"

Write-Step "Verificando Functions Core Tools"
$funcVersion = func --version 2>$null
if (-not $funcVersion) {
    Write-Host "WARNING: func CLI no encontrado. 60-deploy-code.ps1 va a fallar." -ForegroundColor Yellow
    Write-Host "Instalá con: winget install Microsoft.AzureFunctionsCoreTools" -ForegroundColor Yellow
} else {
    Write-Ok "func $funcVersion"
}

Write-Step "Verificando Python"
$pyVersion = python --version 2>$null
if (-not $pyVersion) {
    Fail "Python no encontrado en PATH. Necesario para construir el wheel."
}
Write-Ok "$pyVersion"

# Providers requeridos para los recursos que vamos a crear.
$providers = @(
    "Microsoft.Storage",
    "Microsoft.KeyVault",
    "Microsoft.Web",
    "Microsoft.Sql",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights"
)
Write-Step "Verificando registro de providers"
foreach ($p in $providers) {
    $state = az provider show --namespace $p --query "registrationState" -o tsv 2>$null
    if ($state -ne "Registered") {
        Write-Host "    Registrando $p..." -ForegroundColor Yellow
        az provider register --namespace $p --wait | Out-Null
    }
    Write-Ok $p
}

Write-Host ""
Write-Host "Pre-requisitos OK. Próximo paso: .\10-create-foundation.ps1" -ForegroundColor Green
