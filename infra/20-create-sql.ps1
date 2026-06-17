# =====================================================================
# SQL Server + Database serverless + firewall rules.
#
# Soporta DOS escenarios:
#   A. SQL Server NUEVO: si no existe, lo crea (pide password admin).
#   B. SQL Server EXISTENTE: si ya existe, lo respeta y solo crea la DB +
#      agrega firewall rules + guarda la conn string en Key Vault.
#
# Para usar un server existente, exportar antes:
#     $env:SqlServer = "nombre-corto-del-server"           # SIN .database.windows.net
#     $env:SqlServerResourceGroup = "rg-del-server"        # opcional, si vive en otro RG
#     $env:SqlAdminUser = "rapanuisa"                      # default 'sqladmin'
#
# Decision: si lo creamos nosotros, SKU serverless General Purpose Gen5_2.
# Pausa automatica despues de 1 hora idle.
# =====================================================================
[CmdletBinding()]
param(
    [SecureString]$SqlAdminPassword
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

# ---------- Determinar si el server existe ----------
# Se busca primero en SqlServerResourceGroup (puede ser distinto al RG nuestro).
Write-Step "Buscando SQL Server '$SqlServer' en RG '$SqlServerResourceGroup'"
$srvId = az sql server show `
    --name $SqlServer --resource-group $SqlServerResourceGroup `
    --query "id" -o tsv 2>$null

$serverIsNew = $false
if (-not $srvId) {
    Write-Host "    No existe; lo vamos a crear en $ResourceGroup" -ForegroundColor Yellow
    $serverIsNew = $true
    # Cuando lo creamos nosotros, el server vive en NUESTRO RG.
    $SqlServerResourceGroup = $ResourceGroup
} else {
    Write-Skip "$SqlServer (existente en $SqlServerResourceGroup)"
}

# ---------- Password admin ----------
# Solo necesario si vamos a crear el server (CREATE SERVER pide --admin-password).
# Si el server existe, no la usamos para crear, pero la necesitamos abajo
# para componer la conn string que persistimos temporalmente en KV.
if (-not $SqlAdminPassword) {
    if ($serverIsNew) {
        Write-Step "Password para el admin SQL ($SqlAdminUser)"
        $SqlAdminPassword = Read-Host -AsSecureString "Password (min 16 chars, mayus+minus+digitos+simbolos)"
    } else {
        Write-Step "Password del admin existente ($SqlAdminUser) para la conn string inicial"
        $SqlAdminPassword = Read-Host -AsSecureString "Password de $SqlAdminUser en $SqlServer"
    }
}
$plainPwd = [System.Net.NetworkCredential]::new("", $SqlAdminPassword).Password
if ($plainPwd.Length -lt 8) {
    Fail "Password muy corto (Azure SQL exige >= 8; recomendamos 16+)."
}

# ---------- Crear server si era nuevo ----------
if ($serverIsNew) {
    Write-Step "Creando SQL Server: $SqlServer"
    az sql server create `
        --name $SqlServer --resource-group $SqlServerResourceGroup `
        --location $Location `
        --admin-user $SqlAdminUser --admin-password $plainPwd `
        --minimal-tls-version "1.2" `
        --output none
    Write-Ok "Creado"
}

# ---------- AAD admin (opcional pero recomendado) ----------
if ($SqlAadAdmin) {
    Write-Step "AAD admin SQL: $SqlAadAdmin"
    az sql server ad-admin create `
        --server $SqlServer --resource-group $SqlServerResourceGroup `
        --display-name $SqlAadAdmin --object-id (
            az ad user show --id $SqlAadAdmin --query "id" -o tsv 2>$null
        ) --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Asignado"
    } else {
        Write-Host "    No se pudo resolver $SqlAadAdmin como AAD user; saltando." -ForegroundColor Yellow
    }
} else {
    Write-Host "(`$SqlAadAdmin vacio en _config.ps1; saltando AAD admin)" -ForegroundColor DarkGray
}

# ---------- Firewall: permitir Azure Services ----------
# Necesario para que las Function Apps (Consumption Plan, IPs dinamicas) lleguen
# a SQL. La regla "AllowAllWindowsAzureIps" (start=0.0.0.0, end=0.0.0.0) permite
# cualquier recurso Azure dentro de cualquier tenant. Si querés mas seguridad:
# migrar a Private Endpoint o restringir por outbound IPs de las Function Apps.
Write-Step "Firewall: permitir servicios Azure"
$ruleExists = az sql server firewall-rule show `
    --server $SqlServer --resource-group $SqlServerResourceGroup `
    --name "AllowAzureServices" --query "id" -o tsv 2>$null
if (-not $ruleExists) {
    az sql server firewall-rule create `
        --server $SqlServer --resource-group $SqlServerResourceGroup `
        --name "AllowAzureServices" `
        --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 `
        --output none
    Write-Ok "Regla creada"
} else {
    Write-Skip "AllowAzureServices"
}

# ---------- IP del usuario actual (para sqlcmd local) ----------
$myIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -ErrorAction SilentlyContinue) 2>$null
if ($myIp) {
    Write-Step "Firewall: agregar tu IP ($myIp) para sqlcmd local"
    $ruleName = "client-$($myIp.Replace('.','-'))"
    $exists = az sql server firewall-rule show `
        --server $SqlServer --resource-group $SqlServerResourceGroup `
        --name $ruleName --query "id" -o tsv 2>$null
    if (-not $exists) {
        az sql server firewall-rule create `
            --server $SqlServer --resource-group $SqlServerResourceGroup `
            --name $ruleName `
            --start-ip-address $myIp --end-ip-address $myIp `
            --output none
        Write-Ok "Agregada"
    } else {
        Write-Skip $ruleName
    }
}

# ---------- Database serverless ----------
Write-Step "Database: $SqlDatabase (serverless Gen5_2)"
$dbId = az sql db show `
    --name $SqlDatabase --server $SqlServer --resource-group $SqlServerResourceGroup `
    --query "id" -o tsv 2>$null
if (-not $dbId) {
    az sql db create `
        --name $SqlDatabase --server $SqlServer --resource-group $SqlServerResourceGroup `
        --edition GeneralPurpose --family Gen5 --capacity 2 --compute-model Serverless `
        --auto-pause-delay 60 `
        --backup-storage-redundancy Local `
        --output none
    Write-Ok "Creada"
} else {
    Write-Skip $SqlDatabase
}

# ---------- Connection string (lo guardamos en Key Vault) ----------
# Inicial: usa el admin para que sqlcmd pueda aplicar los SQL del paso 4.
# Despues de crear finance_svc con create_db_user.sql, rotamos manualmente
# (ver docs/DEPLOY.md seccion 4.4).
$connString = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=tcp:$SqlServer.database.windows.net,1433;DATABASE=$SqlDatabase;UID=$SqlAdminUser;PWD=$plainPwd;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"

Write-Step "Guardando connection string en Key Vault"
$existing = az keyvault secret show --vault-name $KeyVault `
    --name "SQL-CONNECTION-STRING" --query "id" -o tsv 2>$null
if ($existing) {
    Write-Skip "SQL-CONNECTION-STRING ya existe; usar 50-load-secrets.ps1 -Force para rotar"
} else {
    az keyvault secret set --vault-name $KeyVault `
        --name "SQL-CONNECTION-STRING" --value $connString `
        --output none
    Write-Ok "Cargada en KV (usa user '$SqlAdminUser' temporalmente)"
}

# ---------- Recordatorio ----------
Write-Host ""
Write-Host "SQL OK. Después corré los scripts de esquema:" -ForegroundColor Green
Write-Host "  sqlcmd -S $SqlServer.database.windows.net -d $SqlDatabase -U $SqlAdminUser -P '...' -i ..\script\unified_finance_schema.sql"
Write-Host "  sqlcmd -S $SqlServer.database.windows.net -d $SqlDatabase -U $SqlAdminUser -P '...' -i ..\script\create_db_user.sql"
Write-Host "  sqlcmd -S $SqlServer.database.windows.net -d $SqlDatabase -U $SqlAdminUser -P '...' -i ..\script\unified_finance_schema_security_v2.sql"
Write-Host ""
Write-Host "Y editá create_db_user.sql para reemplazar el password placeholder antes de correrlo." -ForegroundColor Yellow
Write-Host ""
Write-Host "Próximo paso: .\30-create-function-mp.ps1" -ForegroundColor Green
