# CLAUDE.md

Guía para Claude y otros agentes IA que trabajen en este repo.

## Qué es este proyecto

Pipeline financiero que sincroniza **Mercado Pago** (webhook HTTP) y
**Interbanking** (timer cada 10 min) hacia **Azure SQL** (schema `finance`).
Dos Azure Functions independientes (`mp_webhook_function`, `ib_poller`)
que comparten un paquete Python (`shared/`) empaquetado como wheel local.

Documentación de fondo: `docs/PROYECTO.md` (decisiones, glosario, troubleshooting),
`docs/OBSERVABILITY.md` (queries KQL), `infra/README.md` (deploy Azure).

## Convenciones

- **Idioma**: código, comentarios, docs y commits **en español**.
- **Commits**: conventional commits — `fix:`, `feat:`, `refactor:`, `perf:`,
  `chore:`, `test:`, `docs:`. Body en español, una línea por idea.
- **Identificadores de cambio**: los IDs `SEC-XX`, `CAL-XX`, `AZ-XX`, `OPS-XX`
  vienen del plan original (sección 5 de `PROYECTO.md`). Si modificás algo
  ligado a uno, mencionalo en el commit y el comentario; no inventés IDs
  nuevos sin coordinar.
- **No usar emojis** en código ni docs (salvo que el usuario lo pida).
- **Tests obligatorios**: cualquier cambio en `shared/` debe venir con su
  test correspondiente. Tests viven en `tests/`.

## Estructura y dónde tocar qué

```
shared/                       paquete wheel-able (compartido entre Functions)
    secret_string.py          SecretString (no filtra en logs)
    azure_secrets.py          Key Vault + fallback env
    config.py                 MpWebhookConfig / IbPollerConfig / AppConfig
    db_helpers.py             execute_upsert, execute_upsert_batch, sanitize_*
    interbanking_client.py    cliente REST IB (lazy import de pandas)
    observability.py          configure_logging(): JSON + AppInsights

mp_webhook_function/          Function MP (HTTP webhook + Queue worker + Timer poller)
    function_app.py           mp_webhook (HMAC) + mp_process_payment (Queue) + mp_poller_run (Timer)
    mp_client.py              cliente MP con OAuth2 client_credentials (cache + refresh)
    mp_processor.py           transform + upsert_payment idempotente
    host.json                 functionTimeout 10min + singleton (para el poller)
    requirements.txt          deps + wheel local

ib_poller/                    Azure Function Timer
    function_app.py           timer trigger (cron */10 min)
    ib_processor.py           IBProcessor, Database, sub-procesos
    host.json                 timeout 10min + singleton
    requirements.txt

infra/                        deploy Azure (PowerShell + az CLI)
    _config.ps1               nombres derivados; Assert-AzReady
    00..99-*.ps1              scripts numerados, idempotentes

script/                       SQL schema + grants + indices
docs/                         documentación de fondo
tests/                        suite pytest (120 tests)

unified_finance_sync_service.py    LEGACY (monolítico systemd) — no tocar
main_interactive.py                LEGACY (CLI interactiva) — no tocar
```

## Comandos esenciales

### Setup local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e ".[dev]"            # incluye pytest + pytest-cov
```

### Tests

```powershell
pytest                              # todos
pytest tests/test_secret_string.py  # uno solo
pytest -k "TestMpWebhookConfig"     # por clase/nombre
pytest --cov=shared                 # con coverage
```

Si tu cambio toca `Database`, `InterbankingClient`, o función de
`db_helpers`, asegurate de que los tests de `test_database_pool.py`,
`test_interbanking_client.py` y `test_db_helpers.py` sigan pasando.

### Regenerar el wheel de `shared/`

Cada vez que toques algo en `shared/` y querás probarlo en las Functions:

```powershell
.\build_shared_wheel.ps1
```

Esto construye `dist/interbanking_mp_shared-*.whl` y lo **copia** a
`mp_webhook_function/` y `ib_poller/`. Si no corrés esto, las Functions
usan el wheel viejo.

### Levantar una Function localmente

```powershell
cd mp_webhook_function           # o ib_poller
Copy-Item local.settings.json.example local.settings.json
# editá local.settings.json con valores reales
func start
```

### Deploy a Azure

Ver `infra/README.md`. NO ejecutes los scripts vos: el usuario decide
cuándo correrlos. Si te piden modificarlos, mantené **idempotencia**
(`az X show ... 2>$null; if (-not $existing) { create }`).

## Lo que NO hacés

- **No comitear** `.env`, `local.settings.json`, `*.key`, `*.pfx`, wheels
  con build local. Ya están en `.gitignore` pero revisá `git status`.
- **No tocar `unified_finance_sync_service.py` ni `main_interactive.py`**
  salvo bug crítico. Son código legacy mantenido funcional durante la
  migración a Functions; cambios cosméticos no valen la pena.
- **No reabrir conexiones SQL por operación**. Usar `self.db.connect()`
  de `ib_poller/ib_processor.py:Database`, que es persistente.
- **No remover el lazy import de pandas** en `shared/interbanking_client.py`.
  Si necesitás pandas en una función nueva, llamá a `_pd()`.
- **No agregar `import pandas as pd` al top de ningún módulo de Functions**.
  El cold start es caro.
- **No reintroducir `os.environ["..."]` en `InterbankingClient.__init__`**.
  Inyectá desde `IbPollerConfig` o usá `from_env()` clasificado.
- **No instanciar `MercadoPagoClient` con un access_token directo** salvo que
  uses `access_token_override=` (modo dev local). En prod siempre pasale
  `client_id` + `client_secret` para que haga OAuth con cache + refresh.
- **No persistir el access_token MP en Key Vault como `MP_ACCESS_TOKEN` en prod**.
  El access_token vive en memoria del cliente, no en KV. En KV van
  `MP-CLIENT-ID` y `MP-CLIENT-SECRET`. `MP_ACCESS_TOKEN` es override opcional
  para dev local.
- **No dejar `MP_INITIAL_LOAD=true` permanentemente** en App Settings. Cada
  ciclo del poller volvería a paginar 365 días. Activalo manualmente, esperá
  al backfill, apagalo.
- **No comparar strings de password / token con `==`**. Usar
  `hmac.compare_digest(...)` (timing-safe). Ya se hace en
  `mp_webhook_function/function_app.py:_verify_signature`.
- **No loguear objetos config completos sin envolverlos en `SecretString`**.
  Si querés ver un secret real para debug, llamá explícitamente a
  `.reveal()`; el resto se renderiza como `***SECRET***`.
- **No agregar dependencias top-level a `pyproject.toml`** sin confirmar
  que se necesitan en `shared/` (no en las Functions individuales). Las
  deps del wheel son mínimas (`azure-identity`, `azure-keyvault-secrets`).
  Si una Function necesita algo más, va en su `requirements.txt`.

## Lo que SÍ hacés sin preguntar

- Corregir typos, comentarios inexactos.
- Agregar tests que cubran un código existente sin tests.
- Aplicar `_Validator` cuando agregás un campo nuevo a un config.
- Sumar campos a `MpWebhookConfig` o `IbPollerConfig` si el componente
  realmente los necesita (pero **no a `AppConfig`** salvo que sea
  para el monolítico legacy).

## Patrones repetidos del proyecto

### Agregar un secreto nuevo

1. Agregalo al `.env` local (gitignored).
2. Agregá el campo al config correspondiente
   (`MpWebhookConfig` o `IbPollerConfig`) como `Optional[SecretString]`.
3. Usá `v.required_secret("X")` o `v.optional_secret("X")` dentro de
   `from_env()`.
4. Para deploy: `50-load-secrets.ps1` ya lo carga si el nombre está en
   `$secretVars` de ese script. Si es nuevo, sumalo a la lista.

### Agregar una tabla SQL nueva

1. Schema en `script/unified_finance_schema.sql` (idempotente con
   `IF OBJECT_ID('...') IS NULL`).
2. Índices en `script/unified_finance_schema_security_v2.sql`.
3. Definición de `_KEYS` y `_UPDATE` en `ib_processor.py`.
4. Sub-proceso `_process_X()` usando `execute_upsert` (volúmenes chicos)
   o `execute_upsert_batch` (volúmenes >100 filas/ciclo).

### Agregar una alerta de Monitor

`infra/80-create-alerts.ps1` ya tiene el patrón (helper
`New-AlertIfMissing`). Para alerta de log query (KQL contra
AppInsights), ver "Futuras mejoras" en `docs/OBSERVABILITY.md`: no está
incluido en los scripts porque el flag `az monitor scheduled-query`
está en preview en algunas regiones.

## Antes de proponer un PR / cambio grande

1. `pytest` pasa todo.
2. Si tocaste `shared/`, regeneraste el wheel.
3. Si tocaste un `function_app.py`, importás el módulo localmente
   (`python -c "import sys; sys.path.insert(0, '.'); import mp_webhook_function.function_app"`)
   para asegurarte que no hay un import error.
4. Commits chicos, uno por idea.
5. Si rompiste algo en `unified_finance_sync_service.py` o
   `main_interactive.py` (legacy), avisá antes de comitear y proponé un
   plan.
