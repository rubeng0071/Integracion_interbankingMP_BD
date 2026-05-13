# Puesta en marcha en producción

Guía paso a paso para deployar el servicio a Azure desde cero. Pensada
para ser ejecutable de principio a fin sin saltar a otros docs salvo
para detalle de fondo.

> **Tiempo estimado**: 45-60 minutos la primera vez, ~5 minutos para
> deploys posteriores de código.

---

## Tabla de contenidos

1. [Pre-requisitos](#1-pre-requisitos)
2. [Configuración inicial](#2-configuración-inicial)
3. [Crear recursos en Azure](#3-crear-recursos-en-azure)
4. [Aplicar esquema SQL](#4-aplicar-esquema-sql)
5. [Cargar secretos y deploy del código](#5-cargar-secretos-y-deploy-del-código)
6. [Smoke tests end-to-end](#6-smoke-tests-end-to-end)
7. [Configurar webhook en panel de Mercado Pago](#7-configurar-webhook-en-panel-de-mercado-pago)
8. [Alertas y observabilidad](#8-alertas-y-observabilidad)
9. [Pre-flight check final](#9-pre-flight-check-final)
10. [Rollback](#10-rollback)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Pre-requisitos

### 1.1 Cuentas y permisos

- **Suscripción Azure** con permisos para crear recursos en un Resource
  Group. Idealmente rol `Contributor` + `User Access Administrator`
  sobre la sub (el segundo es necesario para asignar RBAC al Key Vault
  y a las Managed Identities).
- **Tenant Azure AD**: el usuario que corre los scripts debe poder
  consultar `az ad signed-in-user show` (la mayoría de los users sí
  pueden). El AAD admin de SQL es opcional pero recomendado.
- **Credenciales Mercado Pago**:
  - `MP_ACCESS_TOKEN` (panel MP → Tus integraciones → Credenciales).
  - `MP_WEBHOOK_SECRET` (panel MP → Webhooks → Configurar
    notificaciones; se muestra **una sola vez** al crear el webhook).
- **Credenciales Interbanking**:
  - `IB_CLIENT_ID`, `IB_CLIENT_SECRET` (developers.interbanking.com.ar).
  - `IB_SERVICE_URL` (URL de redirección OAuth registrada).
  - `IB_CUSTOMER_ID` (código de abonado, en Administración del portal IB).

### 1.2 Herramientas locales

| Herramienta | Versión | Verificación |
|---|---|---|
| Windows 10/11 con PowerShell 5.1+ | — | `$PSVersionTable` |
| Azure CLI | 2.55+ | `az version` |
| Azure Functions Core Tools | 4.x | `func --version` |
| Python | 3.11.x | `python --version` |
| ODBC Driver 18 for SQL Server | última | `odbcad32.exe` |
| `sqlcmd` (opcional) o SSMS | última | `sqlcmd -?` |
| git | reciente | `git --version` |

Instalación rápida en Windows:

```powershell
winget install Microsoft.AzureCLI
winget install Microsoft.AzureFunctionsCoreTools
winget install Python.Python.3.11
winget install Microsoft.ODBCDriver18.SQLServer
winget install Microsoft.Sqlcmd
```

### 1.3 Clonado y venv

```powershell
git clone <repo-url> servicio_interbankingMP_toBD
cd servicio_interbankingMP_toBD

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
pip install -e ".[dev]"
```

Validación:

```powershell
pytest                  # debe pasar 120 tests en <2 seg
python -c "from shared.config import IbPollerConfig; print('imports OK')"
```

---

## 2. Configuración inicial

### 2.1 `.env` (raíz del repo)

```powershell
Copy-Item unified_finance_sync.env.example .env
notepad .env
```

Completá **todos** los valores reales (sin placeholders `cambiar`, `tu_`,
`TODO`). Mínimo necesario:

```
SQL_CONNECTION_STRING=...              # opcional aquí; lo carga 20-create-sql
MP_ACCESS_TOKEN=APP_USR_xxx
MP_WEBHOOK_SECRET=xxx                  # del panel MP
IB_CLIENT_ID=xxx
IB_CLIENT_SECRET=xxx
IB_SERVICE_URL=https://tu-empresa.com.ar/callback
IB_CUSTOMER_ID=12345678
```

> `.env` está en `.gitignore`. Nunca lo comitees.

### 2.2 `infra/_config.ps1`

```powershell
notepad infra\_config.ps1
```

Cambiar:

- Línea 12 (`$Subscription`): tu nombre o ID de suscripción.
  Para verlo: `az account list -o table`.
- Línea 16 (`$Env`): `"prod"` por default. Si querés crear staging
  primero, ponelo en `"staging"`.
- Línea 19 (`$Location`): región Azure (`"eastus"`, `"westus2"`,
  `"brazilsouth"`, etc.). **Importante**: las dos Functions y SQL deben
  estar en la misma región para minimizar latencia.
- Línea 22 (`$Suffix`): default `"finance-sync"`. Si chocás con otro
  tenant en algún nombre global (Storage, Key Vault, SQL Server),
  cambialo a algo único.
- Línea 47 (`$SqlAadAdmin`) — **opcional**: UPN AAD del usuario o grupo
  que será admin SQL via AAD (además del SQL auth). Si lo dejás vacío,
  el script lo salta sin error.

### 2.3 Validar pre-requisitos contra Azure

```powershell
cd infra
.\00-prereqs.ps1
```

Output esperado:

```
==> Verificando az CLI
    OK az 2.62.0
==> Verificando login
    OK Suscripción: Mi-Sub-Prod (xxxxxxxx-...)
==> Verificando Functions Core Tools
    OK func 4.0.5455
==> Verificando Python
    OK Python 3.11.9
==> Verificando registro de providers
    OK Microsoft.Storage
    OK Microsoft.KeyVault
    OK Microsoft.Web
    OK Microsoft.Sql
    OK Microsoft.OperationalInsights
    OK Microsoft.Insights

Pre-requisitos OK. Próximo paso: .\10-create-foundation.ps1
```

Si falla `az login`, corré `az login` con browser y volvé.

---

## 3. Crear recursos en Azure

### 3.1 Foundation (RG, Key Vault, Storage, Log Analytics, AppInsights)

```powershell
.\10-create-foundation.ps1
```

Tiempo: ~3-5 minutos. Crea **6 recursos**.

Verificación:

```powershell
az resource list --resource-group rg-finance-sync-prod -o table
```

Deberías ver: `kv-finance-sync-prod`, `stfinancesyncprod`,
`log-finance-sync-prod`, `appi-finance-sync-prod` + dependencias.

### 3.2 SQL Server + Database

```powershell
.\20-create-sql.ps1
# Pide el password admin SQL interactivo. Mínimo 12 chars.
# Guardalo a mano por ahora; el script lo persiste en Key Vault como
# SQL-CONNECTION-STRING al final.
```

Tiempo: ~5-8 minutos (la creación del SQL Server es lo más lento).

Verificación:

```powershell
az sql db show --server sql-finance-sync-prod --resource-group rg-finance-sync-prod --name finance -o table
```

`status` debe ser `Online`. `currentServiceObjectiveName` debe ser
`GP_S_Gen5_2` (serverless, 2 vCores).

### 3.3 Function Apps

```powershell
.\30-create-function-mp.ps1
.\40-create-function-ib.ps1
```

Tiempo: ~2-3 minutos cada una.

Verificación:

```powershell
az functionapp show --name func-mp-webhook-prod --resource-group rg-finance-sync-prod --query "{state:state, kind:kind, identity:identity.principalId}"
az functionapp show --name func-ib-poller-prod --resource-group rg-finance-sync-prod --query "{state:state, kind:kind, identity:identity.principalId}"
```

`state` debe ser `Running` y `identity.principalId` debe estar populado
(GUID de la Managed Identity).

### 3.4 Queue del webhook async

```powershell
.\70-create-queue.ps1
```

Tiempo: <1 min. Verificación:

```powershell
$ctx = az storage account show-connection-string --name stfinancesyncprod --resource-group rg-finance-sync-prod --query connectionString -o tsv
az storage queue list --connection-string $ctx -o table
```

Debe aparecer `mp-payment-ids`.

---

## 4. Aplicar esquema SQL

> Los scripts SQL **no se corren automáticamente**: requieren credenciales
> que el script de PowerShell no debería manejar. Hacelo manualmente con
> `sqlcmd` o SSMS.

### 4.1 Esquema base

```powershell
$server = "sql-finance-sync-prod.database.windows.net"
$db = "finance"
$pwd = Read-Host -AsSecureString "Password admin SQL"
$pwdPlain = [System.Net.NetworkCredential]::new("", $pwd).Password

sqlcmd -S $server -d $db -U sqladmin -P $pwdPlain `
    -i ..\script\unified_finance_schema.sql
```

Verificación:

```powershell
sqlcmd -S $server -d $db -U sqladmin -P $pwdPlain `
    -Q "SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('finance') ORDER BY name"
```

Deberías ver: `ib_accounts`, `ib_balances`, `ib_movements`,
`ib_transfers`, `ib_vouchers`, `ib_extracts`, `mp_payments`,
`mp_payment_charges`, `mp_payment_items`, `sync_runs`, `sync_control`.

### 4.2 Usuario `finance_svc` con grants mínimos

**Antes de correr**, editá `script\create_db_user.sql` y reemplazá el
password placeholder (línea con `CambiarEsto-Min16-Chars#2026`).

```powershell
sqlcmd -S $server -d $db -U sqladmin -P $pwdPlain `
    -i ..\script\create_db_user.sql
```

Verificación (debe listar SELECT/INSERT/UPDATE/DELETE/EXECUTE sobre
`finance`, y DENY ALTER/CONTROL/REFERENCES):

```powershell
sqlcmd -S $server -d $db -U sqladmin -P $pwdPlain `
    -Q "SELECT pr.name, pe.permission_name, pe.state_desc FROM sys.database_permissions pe JOIN sys.database_principals pr ON pr.principal_id = pe.grantee_principal_id WHERE pr.name = 'finance_svc'"
```

### 4.3 Indices + page compression

```powershell
# Editá el USE [...] del top del archivo si tu DB no se llama 'finance':
notepad ..\script\unified_finance_schema_security_v2.sql

sqlcmd -S $server -d $db -U sqladmin -P $pwdPlain `
    -i ..\script\unified_finance_schema_security_v2.sql
```

### 4.4 Rotar la conn string del Key Vault a `finance_svc`

El paso 3.2 cargó la connection string usando `sqladmin`. Para producción
es mejor que la app use `finance_svc` (menos privilegios):

```powershell
$financeSvcPwd = Read-Host -AsSecureString "Password finance_svc (el que pusiste en create_db_user.sql)"
$financeSvcPwdPlain = [System.Net.NetworkCredential]::new("", $financeSvcPwd).Password
$conn = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=tcp:$server,1433;DATABASE=$db;UID=finance_svc;PWD=$financeSvcPwdPlain;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"

az keyvault secret set --vault-name kv-finance-sync-prod `
    --name SQL-CONNECTION-STRING --value $conn
```

---

## 5. Cargar secretos y deploy del código

### 5.1 Secretos a Key Vault + App Settings non-secret

```powershell
.\50-load-secrets.ps1
# Lee ..\.env automáticamente.
```

Output esperado:

```
==> .env parseado: 15 variables
==> Cargando secretos en Key Vault
    OK MP-ACCESS-TOKEN
    OK MP-WEBHOOK-SECRET
    OK IB-CLIENT-ID
    OK IB-CLIENT-SECRET
    -- SQL-CONNECTION-STRING ya está en KV (usa -Force para sobrescribir)
==> App Settings -> func-ib-poller-prod (5 vars)
    OK Aplicado
```

Si querés re-cargar valores rotados:

```powershell
.\50-load-secrets.ps1 -Force
```

Verificación de que las Functions ven los secretos:

```powershell
# Debería listar las MIs de las dos Functions como Reader del KV.
az role assignment list --scope (az keyvault show --name kv-finance-sync-prod --query id -o tsv) --query "[?roleDefinitionName=='Key Vault Secrets User'].{principalName: principalName, principalId: principalId}" -o table
```

### 5.2 Build del wheel y publish

```powershell
.\60-deploy-code.ps1
```

Tiempo: ~3-5 minutos (build del wheel + publish a cada Function).

Verificación de que cada Function ve sus funciones:

```powershell
az functionapp function list --name func-mp-webhook-prod --resource-group rg-finance-sync-prod --query "[].name" -o tsv
# Debe imprimir: mp_webhook  mp_process_payment

az functionapp function list --name func-ib-poller-prod --resource-group rg-finance-sync-prod --query "[].name" -o tsv
# Debe imprimir: ib_poller_run
```

Si una Function no lista sus funciones, el deploy falló por algún
import error. Ver logs:

```powershell
az functionapp logs tail --name func-mp-webhook-prod --resource-group rg-finance-sync-prod
```

---

## 6. Smoke tests end-to-end

### 6.1 Function MP responde

```powershell
$url = "https://func-mp-webhook-prod.azurewebsites.net/api/mp/webhook"
# Sin HMAC: debe rechazar con 401.
$code = (Invoke-WebRequest -Uri $url -Method POST -Body '{}' -ContentType 'application/json' -SkipHttpErrorCheck).StatusCode
if ($code -ne 401) { Write-Error "Esperado 401, obtenido $code" } else { Write-Host "OK 401 sin HMAC" -ForegroundColor Green }
```

### 6.2 Function MP con HMAC válido encola el mensaje

Replicá el bloque de la sección 8.3.1 de `PROYECTO.md` (genera HMAC y
manda POST). El response esperado ahora es **202 Accepted** (no 200,
porque el procesamiento es async).

Después de unos segundos, verificá que se procesó:

```powershell
sqlcmd -S sql-finance-sync-prod.database.windows.net -d finance -U finance_svc -P '...' `
    -Q "SELECT TOP 1 payment_id, status, date_last_updated FROM finance.mp_payments ORDER BY date_last_updated DESC"
```

### 6.3 Function IB ejecuta el cron

El cron es cada 10 min; podés esperar o forzar una ejecución:

```powershell
$subId = az account show --query id -o tsv
az rest --method post `
    --url "https://management.azure.com/subscriptions/$subId/resourceGroups/rg-finance-sync-prod/providers/Microsoft.Web/sites/func-ib-poller-prod/functions/ib_poller_run/invoke?api-version=2022-03-01"
```

Verificación SQL:

```powershell
sqlcmd -S sql-finance-sync-prod.database.windows.net -d finance -U finance_svc -P '...' `
    -Q "SELECT process_name, last_status, last_successful_sync FROM finance.sync_control ORDER BY process_name"
```

Esperás 6 rows con `last_status = 'SUCCESS'` y `last_successful_sync`
reciente.

### 6.4 Logs estructurados llegan a AppInsights

```powershell
# Esperá ~3 min para que AppInsights ingiera. Después:
az monitor app-insights events show --app appi-finance-sync-prod `
    --resource-group rg-finance-sync-prod --type traces `
    --start-time (Get-Date).AddMinutes(-10).ToString("o")
```

O en el portal: Application Insights → Logs → KQL:

```kql
traces
| where timestamp > ago(15m)
| where customDimensions.service in ("mp_webhook", "ib_poller")
| project timestamp, customDimensions.service, severityLevel, message
| order by timestamp desc
```

Si ves `customDimensions.service` populado, el bootstrap de
`observability.configure_logging` está funcionando.

---

## 7. Configurar webhook en panel de Mercado Pago

1. Panel MP → Tus integraciones → Tu aplicación → **Webhooks**.
2. Configurar notificaciones → **Modo productivo**.
3. URL: `https://func-mp-webhook-prod.azurewebsites.net/api/mp/webhook?code=<function-key>`
   - El `code` lo obtenés con:
     ```powershell
     az functionapp keys list --name func-mp-webhook-prod --resource-group rg-finance-sync-prod --query "functionKeys.default" -o tsv
     ```
4. Eventos: tildá **Pagos** (`payment.updated`, `payment.created`).
5. **Copiá el secret que muestra MP** — solo se ve una vez. Si lo
   perdiste, regenerar el webhook (te dará uno nuevo).
6. Actualizar el secret en Key Vault:
   ```powershell
   az keyvault secret set --vault-name kv-finance-sync-prod `
       --name MP-WEBHOOK-SECRET --value "<el-secret-de-MP>"
   az functionapp restart --name func-mp-webhook-prod `
       --resource-group rg-finance-sync-prod
   ```
7. Disparar un pago de prueba desde el sandbox MP y verificar que llega
   (Live Metrics → Function MP).

---

## 8. Alertas y observabilidad

```powershell
cd infra
.\80-create-alerts.ps1 -NotifyEmail ops@tu-empresa.com
```

Esto crea un Action Group con email + 4 alertas. Detalle en
[`OBSERVABILITY.md`](OBSERVABILITY.md).

Validación: en el portal → Monitor → Alerts → Alert rules, deberías ver
las 4 alertas habilitadas.

---

## 9. Pre-flight check final

Antes de declarar "vivo en producción":

```powershell
# 1. Tests locales OK
pytest

# 2. Functions corriendo
az functionapp show --name func-mp-webhook-prod --resource-group rg-finance-sync-prod --query state -o tsv
az functionapp show --name func-ib-poller-prod --resource-group rg-finance-sync-prod --query state -o tsv
# Ambos deben decir: Running

# 3. Secretos completos en KV
az keyvault secret list --vault-name kv-finance-sync-prod --query "[].name" -o tsv | Sort-Object
# Esperado mínimo:
#   APPLICATIONINSIGHTS-CONNECTION-STRING (opcional, si lo cargaste)
#   IB-CLIENT-ID
#   IB-CLIENT-SECRET
#   MP-ACCESS-TOKEN
#   MP-WEBHOOK-SECRET
#   SQL-CONNECTION-STRING

# 4. Schema SQL completo
sqlcmd -S sql-finance-sync-prod.database.windows.net -d finance -U finance_svc -P '...' `
    -Q "SELECT COUNT(*) AS tables FROM sys.tables WHERE schema_id = SCHEMA_ID('finance')"
# Esperado: 11

# 5. Webhook MP responde 401 sin HMAC (eso es bueno)
Invoke-WebRequest -Uri "https://func-mp-webhook-prod.azurewebsites.net/api/mp/webhook" -Method POST -Body '{}' -ContentType 'application/json' -SkipHttpErrorCheck | Select-Object StatusCode

# 6. Cron del poller en sync_control reciente
sqlcmd -S ... -Q "SELECT MAX(last_successful_sync) FROM finance.sync_control WHERE process_name LIKE 'interbanking_%'"
# Esperado: dentro de los últimos 15 minutos

# 7. Alertas creadas
az monitor metrics alert list --resource-group rg-finance-sync-prod -o table
# Esperado: 4 alertas (alert-mp-5xx, alert-ib-no-runs, alert-sql-cpu-high, alert-queue-backlog)
```

Si los 7 puntos pasan: estás en producción.

---

## 10. Rollback

### 10.1 Rollback de código (no afecta datos ni infra)

Cada `func azure functionapp publish` reemplaza el deployment slot
default. Para revertir a la versión anterior:

```powershell
# Ver los deployments recientes:
az functionapp deployment list --name func-mp-webhook-prod --resource-group rg-finance-sync-prod -o table

# Hacer redeploy desde un commit anterior:
git checkout <commit-anterior>
.\build_shared_wheel.ps1
cd infra
.\60-deploy-code.ps1
git checkout main
```

### 10.2 Pausar el servicio

```powershell
# Frena el webhook MP (devuelve 503 a MP, que reintenta hasta 24h):
az functionapp stop --name func-mp-webhook-prod --resource-group rg-finance-sync-prod

# Frena el poller IB:
az functionapp stop --name func-ib-poller-prod --resource-group rg-finance-sync-prod

# Volver a arrancar:
az functionapp start --name func-mp-webhook-prod --resource-group rg-finance-sync-prod
az functionapp start --name func-ib-poller-prod --resource-group rg-finance-sync-prod
```

Si el webhook está parado >24h, MP eventualmente desconfigura el webhook
y tenés que rearmarlo en el panel.

### 10.3 Teardown total

```powershell
cd infra
.\99-teardown.ps1
# Te pide el nombre del RG para confirmar; escribilo tal cual.
```

Borra **TODO** (incluye DB con todos los datos). El Key Vault queda en
soft-delete por 7 días; para recrear con el mismo nombre antes:

```powershell
az keyvault purge --name kv-finance-sync-prod --location eastus
```

---

## 11. Troubleshooting

### "El deploy de la Function falla con 'ModuleNotFoundError: shared'"

El wheel no se copió a la carpeta de la Function antes del publish.

```powershell
cd ..\
.\build_shared_wheel.ps1
ls mp_webhook_function\*.whl    # debe estar el wheel ahí
ls ib_poller\*.whl              # idem
cd infra
.\60-deploy-code.ps1
```

### "Function arranca pero al primer request devuelve 'config_error' (500)"

Falta algún secreto en Key Vault o la MI no tiene acceso. Verificá:

```powershell
# 1. Que la MI tenga rol "Key Vault Secrets User":
$miId = az functionapp identity show --name func-mp-webhook-prod --resource-group rg-finance-sync-prod --query principalId -o tsv
$kvId = az keyvault show --name kv-finance-sync-prod --query id -o tsv
az role assignment list --assignee $miId --scope $kvId -o table

# 2. Que todos los secretos esperados estén:
az keyvault secret list --vault-name kv-finance-sync-prod --query "[].name" -o tsv | Sort-Object
```

Si la MI no tiene el rol, re-correr `.\30-create-function-mp.ps1`
(es idempotente y reaplica el RBAC).

### "HMAC siempre rechazado aunque el secret esté bien"

Causa más común: el secret tiene espacios o saltos de línea ocultos
al copiarlo del panel MP.

```powershell
$kvSecret = az keyvault secret show --vault-name kv-finance-sync-prod --name MP-WEBHOOK-SECRET --query value -o tsv
$kvSecret.Length     # comparar contra la longitud que muestra el panel MP
```

Si la longitud no coincide, re-set sin espacios.

### "El poller IB se cuelga sin terminar"

El `functionTimeout` de `host.json` es 10 min. Si tu volumen IB es
mayor, sube a 20 min editando `ib_poller/host.json` y re-deploy.

Alternativa: bajar `IB_INCREMENTAL_LOOKBACK_DAYS=1` para procesar menos
historia por ciclo.

### "Webhook devuelve 202 pero el upsert nunca ocurre"

El mensaje quedó en la queue pero el worker no lo procesa. Posibles
causas:

1. **Permiso de queue**: la MI necesita "Storage Queue Data Contributor"
   sobre el Storage Account. Esto no está en los scripts (Functions
   usa la connection string de AzureWebJobsStorage, que ya tiene
   acceso full). Si lo cambiaste a MI auth, agregá el rol.
2. **Worker en error**: ver logs.
   ```powershell
   az functionapp logs tail --name func-mp-webhook-prod --resource-group rg-finance-sync-prod
   ```
3. **Poison queue tiene mensajes**: alguno falló >5 veces. Ver
   `mp-payment-ids-poison` desde el portal y reprocesar manualmente.

### "Las alertas se disparan en falsos positivos al arrancar"

`alert-ib-no-runs` (sin ejecuciones en 30 min) puede fallar la primera
vez antes de que el cron arranque. Forzá la primera ejecución manual
(ver 6.3) y esperá 5 minutos para que se silencie.

---

## Para más detalle

- Decisiones de diseño y por qué: [`PROYECTO.md`](PROYECTO.md)
- Queries KQL y debugging: [`OBSERVABILITY.md`](OBSERVABILITY.md)
- Convenciones para devs / agentes: [`../CLAUDE.md`](../CLAUDE.md)
- Scripts de infra detalle: [`../infra/README.md`](../infra/README.md)
