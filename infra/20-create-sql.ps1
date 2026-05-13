# =====================================================================
# SQL Server + Database serverless + AAD admin + firewall rules.
#
# Decision: SKU serverless General Purpose Gen5_2. Pausa automatica
# despues de 1 hora idle. Perfecto para nuestro patron de carga
# (webhook eventual + poller cada 10 min, mucho idle por la noche).
# =====================================================================
[CmdletBinding()]
param(
    [SecureString]$SqlAdminPassword
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_config.ps1"
Assert-AzReady

# Admin SQL (login + password). Lo guardamos en Key Vault al final.
$sqlAdminUser = "sqladmin"

if (-not $SqlAdminPassword) {
    Write-Step "Pedir password para el admin SQL (sqladmin)"
    $SqlAdminPassword = Read-Host -AsSecureString "Password (min 16 chars, mayus+minus+digitos+simbolos)"
}
$plainPwd = [System.Net.NetworkCredential]::new("", $SqlAdminPassword).Password
if ($plainPwd.Length -lt 12) {
    Fail "Password muy corto. SQL Server exige >= 8; recomendamos 16+."
}

# ---------- SQL Server logico ----------
Write-Step "SQL Server: $SqlServer"
$srvId = az sql server show `
    --name $SqlServer --resource-group $ResourceGroup `
    --query "id" -o tsv 2>$null
if (-not $srvId) {
    az sql server create `
        --name $SqlServer --resource-group $ResourceGroup `
        --location $Location `
        --admin-user $sqlAdminUser --admin-password $plainPwd `
        --minimal-tls-version "1.2" `
        --output none
    Write-Ok "Creado"
} else {
    Write-Skip $SqlServer
}

# ---------- AAD admin (opcional pero recomendado) ----------
if ($SqlAadAdmin) {
    Write-Step "AAD admin SQL: $SqlAadAdmin"
    az sql server ad-admin create `
        --server $SqlServer --resource-group $ResourceGroup `
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
# Necesario para que las Function Apps (que tienen IPs dinamicas en
# Consumption Plan) lleguen a SQL. La regla "AllowAllWindowsAzureIps"
# (start=0.0.0.0, end=0.0.0.0) permite cualquier recurso Azure dentro
# de cualquier tenant. Si querés mas seguridad: migrar a Private Endpoint
# o restringir por outbound IPs de la Function App.
Write-Step "Firewall: permitir servicios Azure"
$ruleExists = az sql server firewall-rule show `
    --server $SqlServer --resource-group $ResourceGroup `
    --name "AllowAzureServices" --query "id" -o tsv 2>$null
if (-not $ruleExists) {
    az sql server firewall-rule create `
        --server $SqlServer --resource-group $ResourceGroup `
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
        --server $SqlServer --resource-group $ResourceGroup `
        --name $ruleName --query "id" -o tsv 2>$null
    if (-not $exists) {
        az sql server firewall-rule create `
            --server $SqlServer --resource-group $ResourceGroup `
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
    --name $SqlDatabase --server $SqlServer --resource-group $ResourceGroup `
    --query "id" -o tsv 2>$null
if (-not $dbId) {
    az sql db create `
        --name $SqlDatabase --server $SqlServer --resource-group $ResourceGroup `
        --edition GeneralPurpose --family Gen5 --capacity 2 --compute-model Serverless `
        --auto-pause-delay 60 `
        --backup-storage-redundancy Local `
        --output none
    Write-Ok "Creada"
} else {
    Write-Skip $SqlDatabase
}

# ---------- Connection string (lo guardamos en Key Vault) ----------
# El user finance_svc se crea aparte con `create_db_user.sql`. Acá la
# conn string usa al admin para el bootstrap inicial; despues de aplicar
# create_db_user.sql, conviene rotar a finance_svc.
$connString = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=tcp:$SqlServer.database.windows.net,1433;DATABASE=$SqlDatabase;UID=$sqlAdminUser;PWD=$plainPwd;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"

Write-Step "Guardando connection string en Key Vault"
$existing = az keyvault secret show --vault-name $KeyVault `
    --name "SQL-CONNECTION-STRING" --query "id" -o tsv 2>$null
if ($existing) {
    Write-Skip "SQL-CONNECTION-STRING ya existe; usar 50-load-secrets.ps1 para rotar"
} else {
    az keyvault secret set --vault-name $KeyVault `
        --name "SQL-CONNECTION-STRING" --value $connString `
        --output none
    Write-Ok "Cargada en KV (usa user 'sqladmin' temporalmente)"
}

# ---------- Recordatorio ----------
Write-Host ""
Write-Host "SQL OK. Después corré los scripts de esquema:" -ForegroundColor Green
Write-Host "  sqlcmd -S $SqlServer.database.windows.net -d $SqlDatabase -U $sqlAdminUser -P '...' -i ..\script\unified_finance_schema.sql"
Write-Host "  sqlcmd -S $SqlServer.database.windows.net -d $SqlDatabase -U $sqlAdminUser -P '...' -i ..\script\create_db_user.sql"
Write-Host "  sqlcmd -S $SqlServer.database.windows.net -d $SqlDatabase -U $sqlAdminUser -P '...' -i ..\script\unified_finance_schema_security_v2.sql"
Write-Host ""
Write-Host "Y editá create_db_user.sql para reemplazar el password placeholder antes de correrlo." -ForegroundColor Yellow
Write-Host ""
Write-Host "Próximo paso: .\30-create-function-mp.ps1" -ForegroundColor Green
