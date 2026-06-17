# Servicio Interbanking + Mercado Pago → Azure SQL

Pipeline financiero serverless que ingiere pagos de **Mercado Pago** (vía
webhook HTTP) y movimientos / saldos / extractos de **Interbanking** (vía
timer cada 10 min), y los persiste en una base **Azure SQL** (schema
`finance`) lista para consumo por reportes, conciliaciones e integración
contable.

> **Estado**: bloques 1–5 del plan original completos. 120 tests pasando.
> Listo para deploy a Azure con los scripts de `infra/`. Ver
> [`docs/DEPLOY.md`](docs/DEPLOY.md) para puesta en marcha en producción.

## Arquitectura

```
                           ┌──────────────────────┐
                           │   Azure Key Vault    │
                           │  (tokens, conn str)  │
                           └──────────┬───────────┘
                                      │
                       ┌──── Managed Identity ────┐
                       │                          │
                       ▼                          ▼
       ┌────────────────────────┐   ┌─────────────────────────┐
 MP →  │  mp_webhook_function   │   │      ib_poller          │
 POST  │  HTTP /api/mp/webhook  │   │   Timer */10 min        │
       │   ├─ HMAC + replay     │   │   ├─ accounts           │
       │   ├─ encola payment_id │   │   ├─ balances           │
       │   └─ 202 Accepted      │   │   ├─ movements (bulk)   │
       │                        │   │   ├─ transfers          │
       │  Timer */30 min        │   │   ├─ vouchers           │
       │   ├─ search paginado   │   │   └─ extracts (bulk)    │
       │   └─ encola IDs        │   │                         │
       │                        │   │                         │
       │  Queue worker          │   │                         │
       │   ├─ OAuth /oauth/token│   │                         │
       │   ├─ GET MP API        │   │                         │
       │   └─ UPSERT idempotent │   │                         │
       └──────────┬─────────────┘   └────────────┬────────────┘
                  │                              │
                  │     ┌──────────────────┐     │
                  └────▶│   Azure SQL      │◀────┘
                        │  schema finance  │
                        └────────┬─────────┘
                                 │
                                 ▼
                       ┌──────────────────────┐
                       │ Application Insights │
                       │   (JSON structured)  │
                       └──────────────────────┘
```

### Decisiones clave

| Decisión | Por qué |
|---|---|
| Dos Function Apps separadas | Blast radius, escalado independiente, RBAC granular. |
| Webhook + Poller MP en la misma App | Comparten cliente OAuth, config y queue. El poller solo encola IDs y reusa el worker existente — cero código duplicado. |
| Webhook async via Queue | Blinda SLA de 2s del HTTP; el upsert puede tardar lo que necesite. |
| Poller MP como red de seguridad | Cada 30 min recorre la ventana reciente. Cubre webhooks perdidos / downtime de MP / carga histórica inicial. Idempotente por `date_last_updated`. |
| OAuth client_credentials con cache | Token MP de 6h, refrescado al 80% del expires_in. Lock para concurrencia. Override opcional para dev local con APP_USR. |
| Linux Consumption Plan | Costo bajo, escalado automático. Cold start aceptable (1.5-3s). |
| Key Vault con Managed Identity | Sin credenciales en disco, rotación sin redeploy. |
| Bulk upsert (staging + MERGE) | Movements/extracts row-by-row eran el cuello de botella. |
| Conexión SQL persistente | El poller hacía 30+ conexiones por ciclo a serverless DTU. |

Detalles en [`docs/PROYECTO.md`](docs/PROYECTO.md) (sección 6: "Decisiones
de diseño y por qué").

## Tabla de contenidos

- [Quickstart local](#quickstart-local)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Stack y dependencias](#stack-y-dependencias)
- [Desarrollo](#desarrollo)
- [Tests](#tests)
- [Deploy a Azure](#deploy-a-azure)
- [Observabilidad](#observabilidad)
- [Operación día a día](#operación-día-a-día)
- [Referencias](#referencias)

## Quickstart local

```powershell
# 1. Clonar
git clone <repo-url>
cd servicio_interbankingMP_toBD

# 2. Venv + deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e ".[dev]"

# 3. Tests
pytest                                  # 120 tests, <2s

# 4. Configurar .env
Copy-Item unified_finance_sync.env.example .env
# Editá .env con valores reales (NUNCA commitear)

# 5. Construir el wheel de shared
.\build_shared_wheel.ps1

# 6. Arrancar una Function localmente (ejemplo: MP webhook)
cd mp_webhook_function
Copy-Item local.settings.json.example local.settings.json
# Editá local.settings.json
func start
```

Disparar un webhook de prueba con HMAC válido: ver sección 8.3 de
[`docs/PROYECTO.md`](docs/PROYECTO.md).

## Estructura del repositorio

```
servicio_interbankingMP_toBD/
│
├── README.md                              ← este archivo
├── CLAUDE.md                              guía para agentes IA
├── pyproject.toml                         packaging del wheel `shared`
├── build_shared_wheel.ps1                 build + distribución del wheel
├── requirements.txt                       deps del monolítico legacy
├── .gitignore
├── unified_finance_sync.env.example       template de env vars
│
├── docs/
│   ├── PROYECTO.md                        guía maestra: decisiones, KQL, ops
│   ├── DEPLOY.md                          ★ puesta en marcha paso a paso
│   └── OBSERVABILITY.md                   queries KQL para AppInsights
│
├── shared/                                paquete wheel-able
│   ├── secret_string.py                   SEC-07 wrapper anti-leak
│   ├── azure_secrets.py                   SEC-04 Key Vault + fallback env
│   ├── config.py                          CAL-11 dataclasses tipadas
│   ├── db_helpers.py                      CAL-02/03 + SEC-03
│   ├── interbanking_client.py             cliente IB (lazy import pandas)
│   └── observability.py                   OPS-01 logging JSON + AppInsights
│
├── mp_webhook_function/                   Function MP (HTTP + Queue worker + Timer poller)
│   ├── function_app.py                    mp_webhook (HMAC), mp_process_payment (worker), mp_poller_run (timer)
│   ├── mp_client.py                       cliente MP con OAuth2 client_credentials
│   ├── mp_processor.py                    transform + upsert (CAL-02)
│   ├── host.json                          timeout 10min + singleton + retries
│   └── requirements.txt
│
├── ib_poller/                             Azure Function Timer
│   ├── function_app.py                    cron */10 min, singleton
│   ├── ib_processor.py                    InterbankingSync refactoreado
│   ├── host.json                          timeout 10min + singleton
│   └── requirements.txt
│
├── infra/                                 scripts PowerShell + az CLI
│   ├── README.md
│   ├── _config.ps1                        nombres derivados, helpers
│   ├── 00-prereqs.ps1                     valida az login, providers
│   ├── 10-create-foundation.ps1           RG + LogAnalytics + AI + KV + Storage
│   ├── 20-create-sql.ps1                  SQL Server + DB serverless
│   ├── 30-create-function-mp.ps1          Function MP + MI + AppSettings
│   ├── 40-create-function-ib.ps1          Function IB + MI + AppSettings
│   ├── 50-load-secrets.ps1                .env → Key Vault + AppSettings
│   ├── 60-deploy-code.ps1                 build wheel + func publish
│   ├── 70-create-queue.ps1                queue mp-payment-ids
│   ├── 80-create-alerts.ps1               Action Group + 4 alertas
│   └── 99-teardown.ps1                    borra el RG entero
│
├── script/                                SQL schema y grants
│   ├── unified_finance_schema.sql         tablas base
│   ├── create_db_user.sql                 user finance_svc con grants mínimos
│   └── unified_finance_schema_security_v2.sql   indices + page compression
│
├── tests/                                 suite pytest (120 tests)
│   ├── conftest.py                        aislamiento de env vars
│   ├── test_secret_string.py
│   ├── test_db_helpers.py
│   ├── test_azure_secrets.py
│   ├── test_config.py
│   ├── test_config_modular.py
│   ├── test_interbanking_client.py
│   ├── test_database_pool.py
│   └── test_observability.py
│
├── unified_finance_sync_service.py        LEGACY (monolítico systemd)
└── main_interactive.py                    LEGACY (CLI interactiva)
```

## Stack y dependencias

| Capa | Tecnología |
|---|---|
| Runtime | Python 3.11, Azure Functions v4 (Linux Consumption) |
| Datos | Azure SQL Database serverless (Gen5_2) |
| Secretos | Azure Key Vault (RBAC) + Managed Identity |
| Mensajería | Azure Storage Queue (`mp-payment-ids`) |
| Observabilidad | Application Insights + Log Analytics |
| Build / Deploy | PowerShell + `az CLI` + `func` CLI |
| Test | pytest 8 + pytest-cov |

### Dependencias Python

- **Paquete `shared`** (wheel): `azure-identity`, `azure-keyvault-secrets`.
- **`mp_webhook_function`**: + `azure-functions`, `requests`, `pyodbc`,
  `python-dateutil`, `opencensus-ext-azure`, `python-json-logger`.
- **`ib_poller`**: idem + `pandas` (usado por `interbanking_client`).

## Desarrollo

### Cambiar código de `shared/`

```powershell
# 1. Tocás shared/*.py
# 2. Tests
pytest tests/test_<archivo>.py
# 3. Regenerá el wheel para que las Functions lo recojan
.\build_shared_wheel.ps1
```

### Cambiar código de una Function

```powershell
cd mp_webhook_function          # o ib_poller
func start
```

Si tocaste algo de `shared/`, regenerá el wheel primero (paso 3 arriba)
o las Functions usan la versión vieja.

### Convenciones

Ver [`CLAUDE.md`](CLAUDE.md) para reglas detalladas. Resumen:

- Idioma: **español** (código, comentarios, commits).
- Commits: conventional commits (`fix:`, `feat:`, `refactor:`, etc.).
- Tests obligatorios para cambios en `shared/`.
- Secretos siempre en `SecretString`; nada de `os.environ["..."]` en el
  cliente IB (usar `from_env()` o inyectar desde config).

## Tests

```powershell
pytest                              # todos
pytest -k "TestMpWebhookConfig"     # por clase/nombre
pytest --cov=shared                 # con coverage
pytest --cov=shared --cov-report=html  # HTML en htmlcov/
```

Coverage actual de `shared/`: ~95%. Las Functions tienen tests dedicados:
`test_mp_client.py` (OAuth flow + 401 retry + search params),
`test_mp_poller.py` (paginación + dedup + corte por max_pages),
`test_database_pool.py`, `test_interbanking_client.py`.

## Deploy a Azure

Guía paso a paso: [`docs/DEPLOY.md`](docs/DEPLOY.md).

Resumen:

```powershell
cd infra
.\00-prereqs.ps1                              # valida tooling
.\10-create-foundation.ps1                    # RG + KV + AI + Storage
.\20-create-sql.ps1                           # SQL + firewall
.\30-create-function-mp.ps1                   # Function MP
.\40-create-function-ib.ps1                   # Function IB
.\50-load-secrets.ps1                         # .env → Key Vault
.\70-create-queue.ps1                         # queue del webhook async
.\60-deploy-code.ps1                          # build + publish
.\80-create-alerts.ps1 -NotifyEmail you@ex   # alertas Monitor
```

Después: aplicar `script/*.sql` con sqlcmd o SSMS (los detalles los
imprime `20-create-sql.ps1` al terminar).

## Observabilidad

Logs en formato **JSON estructurado** llegan a Application Insights con
`customDimensions.service` distinguiendo `mp_webhook` de `ib_poller`.
Queries KQL útiles en [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md):

- Latencia p50/p95/p99 del webhook.
- Duración por sub-proceso del poller (con `parse`, no regex).
- Webhooks rechazados por HMAC con motivo (`hmac_mismatch`, `ts_too_old`).
- Backlog de la queue `mp-payment-ids`.

Las **4 alertas básicas** que crea `80-create-alerts.ps1`:

| Alerta | Trigger | Severidad |
|---|---|---|
| `alert-mp-5xx` | webhook devuelve >5 5xx en 5min | Warning |
| `alert-ib-no-runs` | poller no ejecuta en 30min | Critical |
| `alert-sql-cpu-high` | SQL CPU >80% sostenido 15min | Warning |
| `alert-queue-backlog` | queue con >100 mensajes pending | Warning |

## Operación día a día

### Cambiar código en producción

```powershell
# 1. Editar archivos
# 2. Si tocaste shared/:
.\build_shared_wheel.ps1
# 3. Tests
pytest
# 4. Deploy
cd infra
.\60-deploy-code.ps1
# 5. Verificar logs en Live Metrics del AppInsights
```

### Rotar un secreto en Key Vault

```powershell
az keyvault secret set --vault-name kv-finance-sync-prod `
    --name MP-ACCESS-TOKEN --value "APP_USR_nuevo_token"

# Reiniciar la Function para limpiar el _cached_config:
az functionapp restart --name func-mp-webhook-prod `
    --resource-group rg-finance-sync-prod
```

### Forzar un sync incremental fuera de cron

```powershell
az rest --method post `
    --url "https://management.azure.com/subscriptions/<sub-id>/resourceGroups/rg-finance-sync-prod/providers/Microsoft.Web/sites/func-ib-poller-prod/functions/ib_poller_run/invoke?api-version=2022-03-01"
```

### Inspeccionar la queue del webhook

```powershell
az storage queue list --account-name stfinancesyncprod -o table
az storage message peek --account-name stfinancesyncprod --queue-name mp-payment-ids --num-messages 5
```

### Reprocesar mensajes en poison

Si `mp-payment-ids-poison` tiene mensajes, significa que un `payment_id`
falló N reintentos. Después de arreglar la causa:

```powershell
# Mover mensajes de poison de vuelta a la queue principal (PowerShell + az):
$ctx = az storage account show-connection-string --name stfinancesyncprod -o tsv
$messages = az storage message get --queue-name mp-payment-ids-poison --connection-string $ctx
# ... ver portal Azure → Storage → Queues → re-enqueue manual.
```

## Referencias

- [`docs/PROYECTO.md`](docs/PROYECTO.md) — guía maestra: decisiones,
  glosario, troubleshooting completo, queries KQL.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — puesta en marcha en producción
  paso a paso.
- [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) — queries KQL útiles.
- [`infra/README.md`](infra/README.md) — scripts de deploy.
- [`CLAUDE.md`](CLAUDE.md) — convenciones para agentes IA / nuevos devs.

## Licencia

Proprietary — Rapanui. Ver `pyproject.toml`.
