# =====================================================================
# Configuración compartida entre todos los scripts de infra/.
# Dot-sourceá este archivo al inicio de cada script:  . .\_config.ps1
#
# Cualquier variable definida aquí se respeta si ya está seteada en
# el environment (útil para CI o para overrides puntuales por sesión).
# =====================================================================

# Identidad de la suscripción donde se crea todo.
# Reemplazá por el nombre o ID que ves con `az account list`.
if (-not $Subscription) { $Subscription = "517ff6e9-04fa-44ed-8c2e-06561f635035" }

# Slug de environment. Cambialo a "staging" o "dev" para crear un set
# paralelo de recursos sin colisionar con prod.
if (-not $Env) { $Env = "prod" }

# Región Azure. Las dos Functions y SQL deben estar en la misma región
# para minimizar latencia (el poller hace varias roundtrips a SQL).
if (-not $Location) { $Location = "eastus2" }

# Sufijo para nombres globales (Storage, Key Vault, SQL Server requieren
# unicidad global). Cambialo si chocás contra otro tenant.
if (-not $Suffix) { $Suffix = "rapanui-finance" }

# ---------------------------------------------------------------------
# Nombres derivados. NO suelen necesitar override.
# ---------------------------------------------------------------------

$ResourceGroup        = "rg-$Suffix-$Env"
$LogWorkspace         = "log-$Suffix-$Env"
$AppInsights          = "appi-$Suffix-$Env"
$KeyVault             = "kv-$Suffix-$Env"
# Storage requiere lowercase, alfanumérico, <=24 chars.
$StorageAccount       = ("st" + ($Suffix -replace '-','') + $Env).ToLower()
if ($StorageAccount.Length -gt 24) { $StorageAccount = $StorageAccount.Substring(0,24) }

# SQL Server: puede ser uno NUEVO (creado por 20-create-sql.ps1) o uno EXISTENTE.
# Para usar uno existente, exportar como env var antes de correr los scripts:
#   $env:SqlServer = "nombre-corto-sin-database-windows-net"
#   $env:SqlServerResourceGroup = "rg-donde-vive-el-server"  (si no esta en $ResourceGroup)
#   $env:SqlAdminUser = "rapanuisa"  (default es 'sqladmin')
# En PowerShell, $env:X y $X son namespaces distintos: leemos primero del env,
# despues caemos al default. Sin esto, las env vars exportadas se ignorarian.
if (-not $SqlServer)              { $SqlServer = if ($env:SqlServer) { $env:SqlServer } else { "sql-$Suffix-$Env" } }
if (-not $SqlServerResourceGroup) { $SqlServerResourceGroup = if ($env:SqlServerResourceGroup) { $env:SqlServerResourceGroup } else { $ResourceGroup } }
if (-not $SqlAdminUser)           { $SqlAdminUser = if ($env:SqlAdminUser) { $env:SqlAdminUser } else { "sqladmin" } }
if (-not $SqlDatabase)            { $SqlDatabase = if ($env:SqlDatabase) { $env:SqlDatabase } else { "finance" } }

$FunctionAppMp        = "func-mp-webhook-$Env"
$FunctionAppIb        = "func-ib-poller-$Env"

# Queue del refactor J (webhook async). El nombre debe coincidir con
# MP_PAYMENT_QUEUE_NAME en las App Settings de la Function MP.
$PaymentQueue         = "mp-payment-ids"

# SKU de Function: Consumption (Y1) por costo. Si necesitás eliminar
# cold start, cambiar a "EP1" (Premium) y configurar alwaysReady.
$FunctionSku          = "Y1"

# Versión de runtime. Functions v4 + Python 3.11 es el combo soportado
# actual (3.12 todavía no es full-supported para Python Workers).
$FunctionsExt         = "4"
$PythonVersion        = "3.11"

# AAD admin de SQL: usuario o grupo que va a tener acceso de DBA.
# Si lo dejás vacío, 20-create-sql.ps1 lo pide al runtime.
if (-not $SqlAadAdmin) { $SqlAadAdmin = "" }

# Función helper: muestra pasos con prefijo y color.
function Write-Step($message) {
    Write-Host "==> $message" -ForegroundColor Cyan
}
function Write-Ok($message) {
    Write-Host "    OK $message" -ForegroundColor Green
}
function Write-Skip($message) {
    Write-Host "    -- $message (ya existe)" -ForegroundColor DarkGray
}
function Fail($message) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}

# Garantiza que estamos logueados y apuntando a la sub correcta.
function Assert-AzReady {
    $current = az account show --query "id" -o tsv 2>$null
    if (-not $current) {
        Fail "No estás logueado. Corré: az login"
    }
    # Si Subscription parece un GUID, comparar contra id; si no, contra name.
    if ($Subscription -match '^[0-9a-fA-F-]{36}$') {
        if ($current -ne $Subscription) {
            Write-Step "Cambiando a suscripción $Subscription"
            az account set --subscription $Subscription | Out-Null
        }
    } else {
        $currentName = az account show --query "name" -o tsv
        if ($currentName -ne $Subscription) {
            Write-Step "Cambiando a suscripción $Subscription"
            az account set --subscription $Subscription | Out-Null
        }
    }
}
