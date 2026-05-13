# =====================================================================
# Carga `.env` -> Key Vault (secretos) + App Settings (no-secretos).
#
# Convencion:
#   - Secretos (passwords, tokens, conn strings) -> Key Vault.
#     KV no permite `_` en nombres, asi que SQL_CONNECTION_STRING se
#     publica como SQL-CONNECTION-STRING. AzureSecretsClient hace la
#     conversion automatica al leer.
#   - Configuracion (URLs, IDs no-secretos, flags) -> App Settings de
#     la Function correspondiente.
#
# Uso:
#   .\50-load-secrets.ps1                       # lee ..\\.env
#   .\50-load-secrets.ps1 -EnvFile C:\path\.env # otro path
#   .\50-load-secrets.ps1 -Force                # sobreescribe en KV
# =====================================================================
[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [switch]$Force
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

if (-not (Test-Path $EnvFile)) {
    Fail "No existe el archivo: $EnvFile (copialo de unified_finance_sync.env.example)"
}

# Cargar .env como hashtable.
$envVars = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
        $key = $matches[1]
        $value = $matches[2].Trim('"').Trim("'")
        $envVars[$key] = $value
    }
}
Write-Step "$EnvFile parseado: $($envVars.Count) variables"

# ---------------------------------------------------------------------
# Clasificacion: secret vs config-no-secret.
# ---------------------------------------------------------------------

# Estos van a Key Vault (con conversion _ -> -).
$secretVars = @(
    "SQL_CONNECTION_STRING",
    "MP_ACCESS_TOKEN",
    "MP_WEBHOOK_SECRET",
    "IB_CLIENT_ID",
    "IB_CLIENT_SECRET",
    "IB_USERNAME",
    "IB_PASSWORD"
)

# Estas van como App Setting de la Function MP.
$mpAppSettings = @(
    # (todas las MP no-secretas estan ya cargadas en 30-create-function-mp.ps1)
)

# Estas van como App Setting de la Function IB.
$ibAppSettings = @(
    "IB_SERVICE_URL",
    "IB_CUSTOMER_ID",
    "IB_GRANT_TYPE",
    "IB_TOKEN_URL",
    "IB_API_BASE_URL",
    "IB_SCOPE",
    "IB_PAGE_SIZE",
    "IB_TIMEOUT_SECONDS"
)

# ---------------------------------------------------------------------
# Secretos -> Key Vault
# ---------------------------------------------------------------------
Write-Step "Cargando secretos en Key Vault"
foreach ($name in $secretVars) {
    if (-not $envVars.ContainsKey($name)) {
        Write-Host "    -- $name no esta en .env; saltando" -ForegroundColor DarkGray
        continue
    }
    $value = $envVars[$name]
    if (-not $value -or $value -match '^cambiar|^TODO|^tu_') {
        Write-Host "    -- $name parece placeholder; saltando" -ForegroundColor DarkGray
        continue
    }
    $kvName = $name.Replace("_", "-")
    if (-not $Force) {
        $existing = az keyvault secret show --vault-name $KeyVault --name $kvName `
            --query "id" -o tsv 2>$null
        if ($existing) {
            Write-Skip "$kvName ya esta en KV (usa -Force para sobrescribir)"
            continue
        }
    }
    az keyvault secret set --vault-name $KeyVault --name $kvName --value $value --output none
    Write-Ok "$kvName"
}

# ---------------------------------------------------------------------
# Config no-secreta -> App Settings de la Function correspondiente.
# ---------------------------------------------------------------------

function Set-AppSettings($functionName, $varList) {
    $settings = @()
    foreach ($name in $varList) {
        if ($envVars.ContainsKey($name) -and $envVars[$name]) {
            $settings += "$name=$($envVars[$name])"
        }
    }
    if ($settings.Count -gt 0) {
        Write-Step "App Settings -> $functionName ($($settings.Count) vars)"
        az functionapp config appsettings set `
            --name $functionName --resource-group $ResourceGroup `
            --settings $settings --output none
        Write-Ok "Aplicado"
    } else {
        Write-Host "    -- No hay App Settings non-secret para $functionName" -ForegroundColor DarkGray
    }
}

Set-AppSettings $FunctionAppMp $mpAppSettings
Set-AppSettings $FunctionAppIb $ibAppSettings

Write-Host ""
Write-Host "Secretos + App Settings OK." -ForegroundColor Green
Write-Host "Próximos pasos:" -ForegroundColor Green
Write-Host "  .\70-create-queue.ps1     # crea mp-payment-ids para el webhook async"
Write-Host "  .\60-deploy-code.ps1      # build + publish de ambas Functions"
