# =====================================================================
# Deploy de codigo a ambas Function Apps.
#
# Pasos:
#   1. Construye el wheel de shared/ con build_shared_wheel.ps1, que ya
#      copia el wheel a mp_webhook_function/ e ib_poller/.
#   2. `func azure functionapp publish` para cada Function.
#
# Pre-requisito local: Azure Functions Core Tools instalado.
# =====================================================================
[CmdletBinding()]
param(
    [switch]$SkipWheel
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

$RepoRoot = Split-Path -Parent $PSScriptRoot

# ---------- 1. Wheel ----------
if (-not $SkipWheel) {
    Write-Step "Construyendo wheel de shared/"
    & (Join-Path $RepoRoot "build_shared_wheel.ps1")
    if ($LASTEXITCODE -ne 0) {
        Fail "build_shared_wheel.ps1 fallo (exit $LASTEXITCODE)"
    }
    Write-Ok "Wheel construido y distribuido"
}

# ---------- 2. Publish MP webhook ----------
Write-Step "Publish: $FunctionAppMp"
Push-Location (Join-Path $RepoRoot "mp_webhook_function")
try {
    func azure functionapp publish $FunctionAppMp --python
    if ($LASTEXITCODE -ne 0) { Fail "func publish $FunctionAppMp fallo" }
    Write-Ok "Deploy OK"
} finally {
    Pop-Location
}

# ---------- 3. Publish IB poller ----------
Write-Step "Publish: $FunctionAppIb"
Push-Location (Join-Path $RepoRoot "ib_poller")
try {
    func azure functionapp publish $FunctionAppIb --python
    if ($LASTEXITCODE -ne 0) { Fail "func publish $FunctionAppIb fallo" }
    Write-Ok "Deploy OK"
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Deploy completo." -ForegroundColor Green
Write-Host ""
Write-Host "Validar:" -ForegroundColor DarkCyan
Write-Host "  curl https://$FunctionAppMp.azurewebsites.net/api/mp/webhook  # 401 sin HMAC, esperable"
Write-Host "  az functionapp logs tail --name $FunctionAppMp --resource-group $ResourceGroup"
Write-Host "  az functionapp logs tail --name $FunctionAppIb --resource-group $ResourceGroup"
