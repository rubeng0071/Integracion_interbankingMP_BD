# =====================================================================
# Borra el Resource Group entero.
# IRREVERSIBLE: incluye SQL DB, secretos del Key Vault, logs, etc.
# =====================================================================
[CmdletBinding()]
param(
    [switch]$Yes
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

$rg = az group show --name $ResourceGroup --query "name" -o tsv 2>$null
if (-not $rg) {
    Write-Host "El Resource Group $ResourceGroup no existe." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "⚠️  TEARDOWN" -ForegroundColor Red
Write-Host "Va a borrarse TODO el Resource Group:" -ForegroundColor Red
Write-Host "    $ResourceGroup" -ForegroundColor Red
Write-Host ""
Write-Host "Incluye:" -ForegroundColor Yellow
az resource list --resource-group $ResourceGroup `
    --query "[].{Type:type, Name:name}" -o table

Write-Host ""
if (-not $Yes) {
    $confirm = Read-Host "Para confirmar, escribí el nombre del RG ($ResourceGroup)"
    if ($confirm -ne $ResourceGroup) {
        Write-Host "Nombre no coincide. Cancelado." -ForegroundColor Yellow
        exit 0
    }
}

Write-Step "Borrando $ResourceGroup en background"
az group delete --name $ResourceGroup --yes --no-wait
Write-Ok "Eliminacion en curso (~5-10 min)"
Write-Host ""
Write-Host "Verificar con: az group show --name $ResourceGroup" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "NOTA: Key Vault queda en 'soft-delete' por 7 dias (retention-days en 10-create-foundation)." -ForegroundColor Yellow
Write-Host "Para purgar definitivamente:"
Write-Host "    az keyvault purge --name $KeyVault --location $Location"
