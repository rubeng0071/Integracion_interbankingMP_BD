# =====================================================================
# Crea la queue del Storage Account que conecta el HTTP webhook con el
# worker async (refactor J).
#
# El runtime de Functions crea la queue al primer mensaje encolado si no
# existe, pero crearla explicitamente permite ver la cola en el portal
# desde dia 0 y validar permisos.
# =====================================================================
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

Write-Step "Storage Queue: $PaymentQueue en $StorageAccount"

# Tomamos el connection string del storage para usar `az storage queue`.
$connStr = az storage account show-connection-string `
    --name $StorageAccount --resource-group $ResourceGroup `
    --query "connectionString" -o tsv

$exists = az storage queue exists --name $PaymentQueue --connection-string $connStr `
    --query "exists" -o tsv 2>$null
if ($exists -eq "true") {
    Write-Skip $PaymentQueue
} else {
    az storage queue create --name $PaymentQueue --connection-string $connStr `
        --output none
    Write-Ok "Creada"
}

# Bonus: la queue de poison se crea automaticamente cuando un mensaje
# falla N veces. Si querés crearla manualmente para observarla desde el
# inicio, descomentar:
# $poison = "$PaymentQueue-poison"
# az storage queue create --name $poison --connection-string $connStr --output none

Write-Host ""
Write-Host "Queue OK. Próximo paso: .\60-deploy-code.ps1" -ForegroundColor Green
