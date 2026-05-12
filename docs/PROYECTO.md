# Servicio Interbanking + Mercado Pago → Azure SQL

> Guía maestra del proyecto: arquitectura, decisiones, pruebas locales,
> deploy a Azure y operación del día a día.
> Última actualización: post-Bloque 2 (refactor a Functions + shared wheel).

---

## Índice

1. [Resumen ejecutivo](#1-resumen-ejecutivo)
2. [Glosario y conceptos clave](#2-glosario-y-conceptos-clave)
3. [Arquitectura objetivo](#3-arquitectura-objetivo)
4. [Estructura del repositorio](#4-estructura-del-repositorio)
5. [Catálogo completo de cambios (Bloque 1 + 2)](#5-catálogo-completo-de-cambios-bloque-1--2)
6. [Decisiones de diseño y por qué](#6-decisiones-de-diseño-y-por-qué)
7. [Setup local de desarrollo](#7-setup-local-de-desarrollo)
8. [Pruebas locales paso a paso](#8-pruebas-locales-paso-a-paso)
9. [Deploy a Azure (Bloque 4 — pendiente)](#9-deploy-a-azure-bloque-4--pendiente)
10. [Operación, observabilidad y mantenimiento](#10-operación-observabilidad-y-mantenimiento)
11. [Troubleshooting frecuente](#11-troubleshooting-frecuente)
12. [Roadmap pendiente](#12-roadmap-pendiente)
13. [Apéndices](#13-apéndices)

---

## 1. Resumen ejecutivo

### Qué hace este servicio

Sincroniza dos fuentes financieras hacia una base **Azure SQL** (`schema finance`):

- **Mercado Pago (MP)**: pagos individuales, cobros, items, refunds → tablas
  `mp_payments`, `mp_charges_details`, `mp_payment_items`, etc.
- **Interbanking (IB)**: cuentas bancarias, saldos, movimientos, transferencias,
  comprobantes, extractos → tablas `ib_accounts`, `ib_balances`, `ib_movements`,
  `ib_transfers`, `ib_vouchers`, `ib_extracts`.

Ambas fuentes terminan en el mismo esquema, listas para consumo por reportes,
conciliaciones, integración contable, etc.

### De dónde venimos (estado pre-refactor)

- **Un solo proceso monolítico** (`unified_finance_sync_service.py`) que corría
  como `systemd unit` en una VM, con `time.sleep()` y `run_forever()`.
- Polling de MP cada N minutos (latencia alta, riesgo de perder pagos si la VM
  estaba caída).
- Secretos en `.env` plano, sin gestión de rotación.
- TLS desactivado en la conexión SQL (`Encrypt=no`).
- Datos sensibles (números de tarjeta, emails, CUITs) volcados crudos al campo
  `raw_json` de la DB.
- ~700 líneas de código `MERGE` SQL repetido para cada tabla.
- Sin alertas, sin logs estructurados, sin Application Insights.

### A dónde vamos (estado actual + objetivo)

| Aspecto | Antes | Ahora (Bloques 1+2) | Objetivo final (Bloques 3+) |
|---|---|---|---|
| **Runtime MP** | Polling cada N min | Webhook HTTP (Function v2) | Idem |
| **Runtime IB** | `run_forever()` en VM | Timer Trigger Function (cron */10 min) | Idem |
| **Secretos** | `.env` en disco | `SecretString` + Azure Key Vault | Rotación automática |
| **TLS SQL** | Off | `Encrypt=yes` obligatorio | Always Encrypted opcional |
| **PII en DB** | Crudo | Sanitizado (`***REDACTED***`) | Idem |
| **Código MERGE** | ~700 líneas duplicadas | `execute_upsert()` declarativo | Idem |
| **Validación webhook** | N/A | HMAC-SHA256 + anti-replay 5min | Idem |
| **Idempotencia MP** | DB-only via PK | Doble: app-level + DB MERGE | Idem |
| **Observabilidad** | `logging` plano | JSON structured + AppInsights ready | Alertas + dashboards |
| **Infra** | VM manual | (pendiente Bloque 4) | Scripts `az CLI` idempotentes |

### Bloques del plan

| Bloque | Descripción | Estado |
|---|---|---|
| **1** | Hardening de seguridad + helpers + paquete `shared/` | ✅ Completo |
| **2** | Split en `mp_webhook_function/` + `ib_poller/` Function Apps | ✅ Completo |
| **3** | Tests unitarios (pytest) y de integración | Pendiente |
| **4** | Infraestructura Azure (scripts `az CLI` idempotentes) | Pendiente |
| **5** | Observabilidad: AppInsights, alertas, dashboards | Pendiente |

---

## 2. Glosario y conceptos clave

| Término | Significado en este proyecto |
|---|---|
| **Webhook** | Endpoint HTTP que MP llama cuando ocurre un evento (pago creado/aprobado). Reemplaza al polling. |
| **HMAC** | Hash criptográfico con clave secreta. MP firma cada webhook para que verifiquemos que es legítimo (AZ-02). |
| **Anti-replay** | Rechazar mensajes con timestamp viejo (>5 min) para evitar que un atacante reenvíe un webhook capturado. |
| **Idempotencia** | Si MP envía el mismo evento 2 veces (cosa común), no duplicamos ni rompemos nada. |
| **Timer Trigger** | Tipo de Azure Function que dispara por cron (en vez de por HTTP). Lo usamos para IB. |
| **Singleton** | Configuración del Timer que garantiza que solo una instancia corra a la vez (evita race conditions). |
| **MERGE** | Sentencia SQL Server que hace "UPDATE si existe, INSERT si no" en una sola operación atómica. Equivalente a UPSERT. |
| **Managed Identity (MI)** | Identidad de Azure asignada a un recurso (ej: Function App) para que pueda autenticarse contra otros recursos sin guardar credenciales. |
| **Key Vault** | Servicio de Azure para guardar secretos cifrados, accesible vía Managed Identity. |
| **`SecretString`** | Wrapper que escondemos los secretos del `repr()` y `str()` para que no terminen en logs accidentalmente (SEC-07). |
| **Application Insights** | Servicio de telemetría de Azure: logs, métricas, trazas, alertas. |
| **NCRONTAB** | Sintaxis cron de Azure Functions. Tiene 6 campos: `seg min hora día mes día-semana`. |
| **PII** | Personally Identifiable Information. Datos personales sensibles (DNI, CUIT, tarjeta, email, etc.). |
| **`raw_json`** | Columna de auditoría en cada tabla, guarda el payload crudo de la API por si se necesita reprocesar. Sanitizada (SEC-03). |

---

## 3. Arquitectura objetivo

### Diagrama de alto nivel

```
                           ┌──────────────────────┐
                           │   Azure Key Vault    │
                           │  (secretos: tokens,  │
                           │   conn string, etc.) │
                           └──────────┬───────────┘
                                      │
              Managed Identity        │   Managed Identity
              (lectura de secretos)   │   (lectura de secretos)
                       ┌──────────────┼──────────────┐
                       │              │              │
                       ▼                             ▼
       ┌────────────────────────┐         ┌─────────────────────────┐
 MP →  │  mp_webhook_function   │         │      ib_poller          │
 (HTTP)│  ─────────────────     │         │  ─────────────────      │
 POST  │  Trigger: HTTP /api/   │         │  Trigger: Timer cron    │
       │           mp/webhook   │         │           */10 min      │
       │                        │         │                         │
       │  1. Verifica HMAC      │         │  1. Lee config          │
       │  2. Anti-replay 5min   │         │  2. Por cada subproceso:│
       │  3. Llama MP API       │         │     - accounts          │
       │  4. Sanitiza PII       │         │     - balances          │
       │  5. Upsert idempotente │         │     - movements         │
       │                        │         │     - transfers         │
       │                        │         │     - vouchers          │
       │                        │         │     - extracts          │
       │                        │         │  3. Sanitiza PII        │
       │                        │         │  4. Upsert + sync_runs  │
       └──────────┬─────────────┘         └────────────┬────────────┘
                  │                                    │
                  │       ┌────────────────────┐       │
                  └──────▶│   Azure SQL        │◀──────┘
                          │   schema finance   │
                          │   (serverless)     │
                          └──────────┬─────────┘
                                     │
                                     ▼
                       ┌──────────────────────────┐
                       │  Application Insights    │
                       │  (logs, métricas, alertas)│
                       └──────────────────────────┘
```

### Por qué dos Function Apps separadas (no una sola)

Tomamos esta decisión arquitectónica explícitamente. Razones:

1. **Blast radius**: si el código del poller IB tiene un bug que crashea, el
   webhook MP sigue recibiendo eventos. Si fuera una sola app, un crash las
   afecta a las dos.
2. **Escalado independiente**: el webhook MP escala según tráfico HTTP (puede
   recibir picos en horarios de venta); el poller IB es una carga predecible
   (un job cada 10 min). Mezclarlas tira el plan de auto-scaling al tacho.
3. **Permisos granulares**: la Managed Identity del webhook solo necesita el
   secreto `MP_ACCESS_TOKEN`; la del poller solo `IB_*`. Separarlas permite
   aplicar el principio de menor privilegio en Key Vault.
4. **Alertas y SLOs distintos**: la latencia objetivo del webhook es <2s
   (response time del HTTP); la del poller es por ciclo completo (~minutos).
   Separar las apps separa también las métricas y alertas.
5. **Deploy independiente**: actualizar la lógica MP no requiere redeployar el
   poller IB (que tiene una ventana de mantenimiento más estrecha).

### Por qué Timer Trigger en vez de Container App Job para IB

Estuvo sobre la mesa la opción de usar Azure Container Apps Jobs para IB.
Optamos por Timer Trigger porque:

- **Mismo runtime que MP**: una sola tecnología (Functions) para mantener.
- **Sin Dockerfile**: el código se publica con `func azure functionapp publish`,
  no hay registry container que mantener.
- **Singleton built-in**: el extension de timer ya garantiza no-overlap.
- **`functionTimeout=10min`** alcanza para un ciclo completo de IB.
- **Si en el futuro un ciclo tarda >10min**, migramos a Container Apps Jobs (es
  el plan B documentado).

---

## 4. Estructura del repositorio

```
servicio_interbankingMP_toBD/
│
├── pyproject.toml                              [B2] packaging del wheel `shared`
├── build_shared_wheel.ps1                      [B2] build + distribución del wheel
├── requirements.txt                            [B1] deps del monolítico legacy
├── .gitignore                                  [B1] SEC-05
├── unified_finance_sync.env.example            [B1] template de env vars
├── unified_finance_sync.service                LEGACY systemd (sin tocar)
│
├── docs/
│   └── PROYECTO.md                             ← este archivo
│
├── shared/                                     [B1+B2] paquete wheel-able
│   ├── __init__.py
│   ├── README.md
│   ├── py.typed                                [B2] marker de tipos
│   ├── secret_string.py                        [B1] SEC-07 wrapper anti-leak
│   ├── azure_secrets.py                        [B1] SEC-04 Key Vault client
│   ├── config.py                               [B1] CAL-11 AppConfig dataclass
│   ├── db_helpers.py                           [B1] CAL-02/03 + SEC-03 sanitización
│   └── interbanking_client.py                  [B2] cliente IB (movido de la raíz)
│
├── mp_webhook_function/                        [B2] Azure Function HTTP v2
│   ├── function_app.py                         AZ-02 HMAC + AZ-03 idempotencia
│   ├── mp_client.py                            cliente MP con SecretString
│   ├── mp_processor.py                         transform + upsert (CAL-02)
│   ├── host.json                               AZ-04 timeout + retries
│   ├── local.settings.json.example             template para `func start`
│   ├── requirements.txt                        deps + wheel local
│   └── .funcignore                             qué excluir del deploy
│
├── ib_poller/                                  [B2] Azure Function Timer v2
│   ├── function_app.py                         cron */10 min, singleton
│   ├── ib_processor.py                         InterbankingSync refactoreado (CAL-02)
│   ├── host.json                               timeout 10min, singleton lock
│   ├── local.settings.json.example
│   ├── requirements.txt
│   └── .funcignore
│
├── script/
│   ├── unified_finance_schema.sql              esquema base de finance.*
│   ├── create_db_user.sql                      [B1] SEC-06 user con menos privilegios
│   └── unified_finance_schema_security_v2.sql  [B1] OPS-05/06 + SEC-08 plantillas
│
├── unified_finance_sync_service.py             LEGACY monolítico (parchado, funciona)
└── main_interactive.py                         LEGACY CLI interactiva (parchado)
```

**Convenciones de etiquetado en este doc:**

- `[B1]` = creado/modificado en el Bloque 1 (Hardening + helpers).
- `[B2]` = creado/modificado en el Bloque 2 (Split en Functions).
- `LEGACY` = código previo, mantenido funcionando para no romper la operación
  actual durante la migración.

---

## 5. Catálogo completo de cambios (Bloque 1 + 2)

Cada cambio tiene un ID estable que se referencia desde código y commits.

### Categorías

- **SEC-XX**: Security hardening
- **CAL-XX**: Calidad de código y mantenibilidad
- **AZ-XX**: Adopción Azure / refactor a Functions
- **OPS-XX**: Operación / observabilidad

### 5.1 Seguridad (SEC-XX)

| ID | Cambio | Archivo(s) | Por qué |
|---|---|---|---|
| **SEC-01** | TLS habilitado en conexión SQL (`Encrypt=yes;TrustServerCertificate=no`) | `unified_finance_sync.env.example` | Antes la conexión iba en claro o con cert sin validar. Riesgo de MITM en redes corporativas. |
| **SEC-02** | Credenciales OAuth IB en `body` (no en `query string`) según RFC 6749 | `shared/interbanking_client.py` `_get_token()` | El query string queda en logs de proxies/balanceadores. RFC obliga a usar body. |
| **SEC-03** | Sanitización PII antes de guardar `raw_json` (números de tarjeta, email, CUIT) | `shared/db_helpers.py` `sanitize_to_json()` + uso en `mp_processor.py` y `ib_processor.py` y `unified_finance_sync_service.py` | Compliance con PCI-DSS / GDPR. Aunque la DB sea privada, no hay razón para guardar PAN completo. |
| **SEC-04** | Cliente unificado para Azure Key Vault con fallback a env vars | `shared/azure_secrets.py` | Permite migrar a Key Vault sin cambiar el código de las apps; el switch es por config. |
| **SEC-05** | `.gitignore` excluye `.env`, `*.key`, builds, etc. | `.gitignore` | Evita commits accidentales de secretos. |
| **SEC-06** | Usuario SQL `finance_svc` con permisos mínimos (no ALTER/CONTROL) | `script/create_db_user.sql` | Si la app es comprometida, el atacante no puede dropear tablas ni leer otras DBs. |
| **SEC-07** | Wrapper `SecretString` que oculta valor en `repr()`, `str()`, `__format__` | `shared/secret_string.py` | Protege contra leaks accidentales en logs (`logger.info(f"config={config}")` ya no expone tokens). |
| **SEC-08** | Plantilla SQL para Always Encrypted en columnas con PII | `script/unified_finance_schema_security_v2.sql` | Cifrado a nivel de columna; ni siquiera DBAs ven los valores. Opcional, comentado. |

### 5.2 Calidad / Mantenibilidad (CAL-XX)

| ID | Cambio | Archivo(s) | Por qué |
|---|---|---|---|
| **CAL-01** | `requirements.txt` con versiones fijas (pinned) | `requirements.txt` | Build reproducible. Sin pin, un `pip install` en 6 meses puede traer breaking changes. |
| **CAL-02** | Helper `execute_upsert(cur, table, keys, update_cols, row)` reemplaza ~700 líneas de MERGE manual | `shared/db_helpers.py` + uso en `mp_processor.py` y `ib_processor.py` | Era código copy-paste con alto riesgo de bugs por desincronización. Ahora la definición es declarativa (listas de columnas). |
| **CAL-03** | Helper `to_str(value)` para normalizar valores que vienen como int/float/None | `shared/db_helpers.py` | Mismo patrón se repetía 30+ veces (`str(x).strip() if x else None`). |
| **CAL-06** | `except:` bare reemplazados por `except Exception as exc:` | `main_interactive.py` | El bare `except:` traga `KeyboardInterrupt` y `SystemExit`, hace imposible matar el proceso con Ctrl+C. |
| **CAL-07** | Fechas hardcoded reemplazadas por ventana relativa (90 días) | `main_interactive.py` `probar_disponibilidad_datos()` | El script fallaba al pasar el tiempo porque las fechas quedaban viejas. |
| **CAL-08** | `output_dir` parametrizable (env `OUTPUT_DIR` o cwd) | `shared/interbanking_client.py` `export_to_excel()` | Antes generaba el xlsx en cwd, lo que rompía cuando se corría desde un dir distinto al esperado. |
| **CAL-10** | `assert` reemplazado por `RuntimeError` explícito | `unified_finance_sync_service.py` `compute_window()` | `assert` se desactiva con `python -O`, dejando el chequeo silenciosamente roto en producción. |
| **CAL-11** | `AppConfig` dataclass con validación agregada (todos los errores en un solo `ConfigError`) | `shared/config.py` | Antes el script fallaba en la primera env var faltante; con muchas faltantes había que correrlo N veces. |

### 5.3 Adopción Azure (AZ-XX)

| ID | Cambio | Archivo(s) | Por qué |
|---|---|---|---|
| **AZ-01** | Programming model Functions v2 (decoradores `@app.route`, `@app.timer_trigger`) | `mp_webhook_function/function_app.py`, `ib_poller/function_app.py` | Modelo recomendado actual; `function.json` por carpeta es legacy. |
| **AZ-02** | Validación HMAC-SHA256 del webhook MP con template oficial `id:{X};request-id:{Y};ts:{Z};` + `hmac.compare_digest()` (timing-safe) + ventana anti-replay 5min | `mp_webhook_function/function_app.py` | Sin esto cualquiera puede falsificar webhooks y inyectar pagos falsos. |
| **AZ-03** | Idempotencia doble: (a) chequeo `date_last_updated` antes del DB hit, (b) MERGE como red de seguridad | `mp_webhook_function/mp_processor.py` `_is_already_current()` + `upsert_payment()` | MP reentrega webhooks con frecuencia. (a) ahorra DB writes; (b) garantiza correctitud aunque (a) falle. |
| **AZ-04** | `host.json` con `functionTimeout`, `retry.exponentialBackoff`, sampling de AppInsights | `mp_webhook_function/host.json`, `ib_poller/host.json` | Defaults de Functions son demasiado conservadores; calibramos a nuestro workload. |
| **AZ-05** | Managed Identity para acceso a Key Vault | `shared/azure_secrets.py` con `DefaultAzureCredential` | Sin credenciales en disco. Rotación de secretos automática. |
| **AZ-06** | `requirements.txt` por componente, mínimo necesario, con wheel local de `shared` | `mp_webhook_function/requirements.txt`, `ib_poller/requirements.txt` | Cold start más rápido (menos paquetes). Wheel local evita publicar `shared/` en PyPI. |
| **AZ-07** | Componente IB separado en `ib_poller/` con sus propios `host.json`, `requirements.txt`, etc. | `ib_poller/*` | Ver razones en sección [3. Arquitectura objetivo](#3-arquitectura-objetivo). |
| **AZ-08** | Decisión: NO usar Docker / Container Apps para IB | (decisión, no archivo) | Ver razones en sección [3](#3-arquitectura-objetivo). |
| **AZ-09** | Cron NCRONTAB `0 */10 * * * *` configurable vía env var | `ib_poller/function_app.py` con `IB_POLLER_SCHEDULE` | Permite acelerar/frenar sin redeploy. |
| **AZ-10** | Misma Managed Identity pattern para el poller | `ib_poller/function_app.py` (vía `AppConfig`) | Consistencia. |
| **AZ-11** | Eliminado `run_forever()` y `time.sleep()` del flujo Azure | `ib_poller/function_app.py` (no llama a `run_forever`) | Azure Functions ya orquesta el ciclo de vida. Mantener `run_forever` rompía el modelo serverless. El monolítico LEGACY conserva `run_forever` para la VM. |

### 5.4 Operación / Observabilidad (OPS-XX)

| ID | Cambio | Archivo(s) | Por qué | Estado |
|---|---|---|---|---|
| **OPS-01** | Conexión a Application Insights vía env var | `unified_finance_sync.env.example` | Telemetría centralizada. | Variable definida; integración full en Bloque 5. |
| **OPS-04** | `IBProcessor` mide `duration_ms` por sub-proceso y lo loggea | `ib_poller/ib_processor.py` `_sync_context()` | Sin esto no podés alertar por procesos lentos. | Implementado. |
| **OPS-05** | Plantilla de índices no-clustered para queries frecuentes | `script/unified_finance_schema_security_v2.sql` | Sin índices, los reportes lloran cuando la DB crece. | Pendiente aplicar en SQL real. |
| **OPS-06** | `DATA_COMPRESSION = PAGE` en tablas con `raw_json` | `script/unified_finance_schema_security_v2.sql` | `raw_json` comprime ~70%. Ahorra storage Y mejora I/O. | Pendiente aplicar en SQL real. |

---

## 6. Decisiones de diseño y por qué

### 6.1 Distribución del código compartido como wheel local

**El problema**: `mp_webhook_function/` y `ib_poller/` necesitan ambos el código
de `shared/` (config, secret_string, db_helpers, etc.). ¿Cómo se lo
distribuimos?

**Opciones que evaluamos**:

| Opción | Pro | Con |
|---|---|---|
| Copiar `shared/` en cada Function Folder | Simple | Drift garantizado entre copias |
| Symlinks | Sin duplicación | No funcionan en Windows ni en `func azure functionapp publish` |
| Publicar a PyPI privado | "Profesional" | Requiere infra adicional (Artifacts/Nexus), overkill para un proyecto interno |
| **Wheel local en cada Function** | Sin drift, sin infra extra | Hay que correr `build_shared_wheel.ps1` antes de cada deploy |

**Decidimos wheel local** porque es el sweet spot: una sola fuente de verdad
(`shared/*.py`), sin infraestructura extra, y el script `build_shared_wheel.ps1`
automatiza la distribución.

### 6.2 `interbanking_client.py` movido a `shared/`

Originalmente vivía en la raíz como módulo top-level. Lo movimos a
`shared/interbanking_client.py` porque:

- Lo usan **el monolítico legacy** (`unified_finance_sync_service.py`) **y**
  **el poller nuevo** (`ib_poller/ib_processor.py`).
- Tener una sola copia en `shared/` (que se empaqueta en el wheel) elimina el
  problema de mantener dos copias sincronizadas.
- Los imports en el monolítico cambiaron de
  `from interbanking_client import` a `from shared.interbanking_client import`.

### 6.3 Por qué `SecretString` en vez de simplemente `str`

```python
config = AppConfig.from_env()
logger.info(f"Conectado con config: {config}")
# Sin SecretString:  config=AppConfig(mp_token='APP_USR_abc123...', ...)
# Con SecretString:  config=AppConfig(mp_token=***SECRET***, ...)
```

Es una capa de defensa en profundidad. **No reemplaza** a Key Vault ni a `.gitignore`,
pero es la última red contra el accidente de loguear un objeto entero.

Para usar el valor real:

```python
token_real = config.mp_access_token.reveal()
client = MercadoPagoClient(access_token=token_real)
```

### 6.4 Doble idempotencia en MP webhook

```python
# Capa 1 — App level (mp_processor.py _is_already_current):
if existing_date_last_updated == incoming_date_last_updated:
    return  # No tocamos la DB. Ahorro de I/O.

# Capa 2 — DB level (MERGE):
MERGE INTO finance.mp_payments USING ... WHEN MATCHED THEN UPDATE ...
```

Capa 1 ahorra writes y locks innecesarios cuando MP reenvía el mismo evento
(cosa muy frecuente: MP reenvía webhooks ante el menor 5xx).

Capa 2 garantiza correctitud: si dos webhooks idénticos pasan capa 1
simultáneamente (race condition), el MERGE resuelve el conflicto.

### 6.5 HMAC + anti-replay

El header `x-signature` que MP envía tiene el formato:

```
ts=1700000000000,v1=abc123def456...
```

El proceso es:

1. Tomamos `ts` y `v1` del header, `request-id` de otro header, `id` del payload.
2. Construimos `manifest = "id:{id};request-id:{rid};ts:{ts};"`.
3. Calculamos `expected = HMAC_SHA256(secret, manifest).hexdigest()`.
4. Comparamos `v1 == expected` con `hmac.compare_digest()` (timing-safe, no
   vulnerable a side-channel attacks).
5. **Anti-replay**: si `now - ts > 5min`, rechazamos aunque el HMAC sea válido
   (alguien capturó el webhook y lo reenvía).

### 6.6 Por qué Azure Functions Consumption (no Premium)

- **Costo**: Consumption cobra por ejecución. Para volúmenes bajos/medianos es
  prácticamente gratis (1M ejecuciones/mes incluidas).
- **Cold start**: aceptable para un webhook cuyo SLA es <2s; el cold start de
  Python Functions ronda 1.5-3s.
- **Escalado automático**: hasta 200 instancias, sin tunning.
- **Plan B**: si el cold start molesta, migrar a Premium con `alwaysReady=1`
  cuesta unos USD 30-50/mes y elimina cold starts.

---

## 7. Setup local de desarrollo

### 7.1 Pre-requisitos en tu máquina

| Software | Versión recomendada | Cómo instalar |
|---|---|---|
| **Python** | 3.10 o 3.11 (NO 3.12 todavía, `azure-functions` aún no lo soporta full) | https://www.python.org/downloads/ |
| **Azure Functions Core Tools** | v4.x | `winget install Microsoft.AzureFunctionsCoreTools` |
| **Azure CLI** | Última | `winget install Microsoft.AzureCLI` |
| **ODBC Driver 18 for SQL Server** | Última | https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server |
| **PowerShell** | 5.1+ (ya viene en Windows) | — |
| **VS Code** + extensión Azure Functions | Última | https://code.visualstudio.com/ |

Verificá:

```powershell
python --version            # 3.10.x o 3.11.x
func --version              # 4.x
az --version                # 2.x
```

### 7.2 Clonar y preparar el repo

```powershell
cd c:\UserData\Rapanui\Python\servicio_interbankingMP_toBD

# Crear venv aislado
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Instalar deps del monolítico legacy (sirve también para correr scripts CLI)
pip install -r requirements.txt

# Instalar el paquete shared en modo editable (cambios en shared/*.py se reflejan al instante)
pip install -e .
```

Verificá que el paquete está importable:

```powershell
python -c "from shared.config import AppConfig; print('OK')"
python -c "from shared.interbanking_client import InterbankingClient; print('OK')"
```

### 7.3 Construir el wheel `interbanking_mp_shared`

Cada vez que **modifiques algo en `shared/`**, regenerá el wheel para que las
Functions lo recojan:

```powershell
.\build_shared_wheel.ps1
```

Esto:

1. Construye `dist/interbanking_mp_shared-0.1.0-py3-none-any.whl`.
2. Copia ese wheel a `mp_webhook_function/` y `ib_poller/`.

Si modificás `shared/` y **no** corrés esto, las Functions van a usar la versión
vieja del wheel ya copiado.

### 7.4 Configurar variables de entorno locales

#### 7.4.1 Para el monolítico legacy

```powershell
Copy-Item unified_finance_sync.env.example .env
# Editá .env con valores reales (NUNCA commitearlo: .gitignore lo excluye)
```

Variables críticas:

| Variable | Ejemplo | Notas |
|---|---|---|
| `MP_ACCESS_TOKEN` | `APP_USR_abc...` | Token de prod o sandbox de MP |
| `IB_CLIENT_ID` | `xyz` | Provisto por Interbanking |
| `IB_CLIENT_SECRET` | `***` | Idem |
| `IB_USERNAME` | `usuario_ib` | Idem |
| `IB_PASSWORD` | `***` | Idem |
| `SQL_CONNECTION_STRING` | `Driver={ODBC Driver 18 for SQL Server};Server=tcp:srv.database.windows.net,1433;Database=finance;Uid=finance_svc;Pwd=***;Encrypt=yes;TrustServerCertificate=no;` | TLS obligatorio (SEC-01) |

#### 7.4.2 Para el webhook MP (Function local)

```powershell
cd mp_webhook_function
Copy-Item local.settings.json.example local.settings.json
# Editar local.settings.json con valores reales
```

Variable adicional clave: **`MP_WEBHOOK_SECRET`**. Este es el HMAC secret que
MP te muestra UNA SOLA VEZ al configurar el webhook en su panel
(https://www.mercadopago.com.ar/developers/panel/app → tu app → Webhooks).

Si lo perdiste: regenerá el webhook (te dará un secret nuevo) y actualizá la
variable.

#### 7.4.3 Para el poller IB (Function local)

```powershell
cd ib_poller
Copy-Item local.settings.json.example local.settings.json
# Editar local.settings.json con valores reales
```

Variables clave:

- `SQL_CONNECTION_STRING`
- `IB_CLIENT_ID`, `IB_CLIENT_SECRET`, `IB_USERNAME`, `IB_PASSWORD`
- `IB_INCREMENTAL_LOOKBACK_DAYS` (default 1)
- `IB_POLLER_SCHEDULE` (default `0 */10 * * * *` = cada 10 min). Para testing
  podés ponerlo en `*/30 * * * * *` (cada 30 segundos) y volver a `0 */10 * * * *`
  cuando termines.

### 7.5 Preparar la base de datos

Si todavía no tenés el esquema:

```powershell
# Conectate a la DB con sqlcmd o SSMS y corré, en orden:
# 1. Esquema base
sqlcmd -S srv.database.windows.net -d finance -U admin -P '***' -i script\unified_finance_schema.sql

# 2. Usuario con menos privilegios (SEC-06)
# Editar primero create_db_user.sql cambiando el placeholder de password
sqlcmd -S srv.database.windows.net -d finance -U admin -P '***' -i script\create_db_user.sql

# 3. Índices y compresión (OPS-05/06)
sqlcmd -S srv.database.windows.net -d finance -U admin -P '***' -i script\unified_finance_schema_security_v2.sql
```

---

## 8. Pruebas locales paso a paso

### 8.1 Test 1: importar el paquete shared (smoke test)

```powershell
python -c "from shared.config import AppConfig; c = AppConfig.from_env(); print('Config OK:', c)"
```

Debería imprimir `AppConfig(...)` con los **secretos enmascarados como
`***SECRET***`** (eso es SEC-07 funcionando).

Si dice `ConfigError: missing variables...`, te falta exportar variables.
Cargalas en la sesión:

```powershell
# Cargar el .env en la sesión actual
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
    }
}
```

### 8.2 Test 2: correr el monolítico legacy una vez

```powershell
python unified_finance_sync_service.py --once
```

Debería:
1. Conectarse a MP API y traer pagos.
2. Conectarse a IB API y traer cuentas/movimientos/etc.
3. Hacer upserts a Azure SQL.
4. Loggear stats por sub-proceso.
5. Salir limpio.

Verificá en la DB:

```sql
SELECT TOP 10 * FROM finance.sync_runs ORDER BY started_at DESC;
SELECT TOP 10 * FROM finance.mp_payments ORDER BY date_last_updated DESC;
SELECT TOP 10 * FROM finance.ib_movements ORDER BY process_date DESC;
```

### 8.3 Test 3: correr el webhook MP localmente

```powershell
# Regenerá el wheel por si tocaste shared/
.\build_shared_wheel.ps1

cd mp_webhook_function
pip install -r requirements.txt
func start
```

Debería arrancar y mostrar:

```
Functions:
        mp_webhook: [POST] http://localhost:7071/api/mp/webhook
```

#### 8.3.1 Disparar un webhook de prueba con HMAC válido

En **otra terminal PowerShell**:

```powershell
# 1. Definir variables (reemplazá $secret por tu MP_WEBHOOK_SECRET real)
$secret = "tu_webhook_secret_aqui"
$ts = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$reqId = [guid]::NewGuid().ToString()
$paymentId = "1234567890"

# 2. Construir el manifest según el formato MP
$manifest = "id:$paymentId;request-id:$reqId;ts:$ts;"

# 3. Calcular HMAC-SHA256
$keyBytes = [Text.Encoding]::UTF8.GetBytes($secret)
$msgBytes = [Text.Encoding]::UTF8.GetBytes($manifest)
$hmac = New-Object System.Security.Cryptography.HMACSHA256 (,$keyBytes)
$hashBytes = $hmac.ComputeHash($msgBytes)
$sig = -join ($hashBytes | ForEach-Object { $_.ToString("x2") })

# 4. Disparar el POST
$body = @{type="payment"; data=@{id=$paymentId}} | ConvertTo-Json -Compress
$headers = @{
    "Content-Type" = "application/json"
    "x-request-id" = $reqId
    "x-signature"  = "ts=$ts,v1=$sig"
}

Invoke-RestMethod -Uri http://localhost:7071/api/mp/webhook -Method POST -Headers $headers -Body $body
```

**Esperás:**
- Status 200, response `{"status":"ok"}`.
- En la consola de `func start`, logs JSON con `mp_webhook: payment processed id=1234567890`.
- En la DB, una row nueva o actualizada en `finance.mp_payments`.

#### 8.3.2 Verificar que el HMAC inválido es rechazado

Cambiá `$secret` a `"secret_falso"` y re-ejecutá el bloque. Esperás:
- Status 401, response `{"error":"invalid signature"}`.

#### 8.3.3 Verificar anti-replay

Restablecé el secret correcto pero usá un `$ts` viejo:

```powershell
$ts = ([DateTimeOffset]::UtcNow.AddMinutes(-10)).ToUnixTimeMilliseconds()
# ... resto igual
```

Esperás:
- Status 401, response `{"error":"signature timestamp too old"}`.

#### 8.3.4 Verificar idempotencia (AZ-03)

Disparar el mismo webhook 2 veces con el mismo `$paymentId` y un `$ts` fresco
en cada ejecución. La segunda ejecución debería loggear:

```
mp_webhook: payment 1234567890 already current, skipping DB write
```

### 8.4 Test 4: correr el poller IB localmente

```powershell
cd ib_poller

# Opcional: bajar el cron a 30 segundos para no esperar 10 min
# Editá local.settings.json y poné: "IB_POLLER_SCHEDULE": "*/30 * * * * *"

func start
```

Esperás (cada 30 seg si bajaste el cron):

```
[2026-04-17T...] Executing 'Functions.ib_poller_run' (Reason='Timer fired ...')
[2026-04-17T...] sync interbanking_accounts OK: read=12 upserted=12 duration_ms=1240
[2026-04-17T...] sync interbanking_balances OK: read=84 upserted=84 duration_ms=2310
[2026-04-17T...] sync interbanking_movements OK: read=1320 upserted=1320 duration_ms=18430
[2026-04-17T...] sync interbanking_transfers OK: ...
[2026-04-17T...] sync interbanking_extracts OK: ...
[2026-04-17T...] ib_poller: ciclo completo. summary={'accounts': {'read': 12, 'upserted': 12, 'duration_ms': 1240}, ...}
[2026-04-17T...] Executed 'Functions.ib_poller_run' (Succeeded, ...)
```

Verificá:

```sql
SELECT process_name, last_status, last_successful_sync, last_error
FROM finance.sync_control
WHERE process_name LIKE 'interbanking_%'
ORDER BY process_name;

SELECT TOP 20 * FROM finance.sync_runs
WHERE source_system = 'INTERBANKING'
ORDER BY started_at DESC;
```

**Importante**: cuando termines, **restaurá el cron a `0 */10 * * * *`** en
`local.settings.json` (sino te va a martillar la API de IB).

### 8.5 Test 5: probar HMAC en serio con `ngrok` (opcional)

Si querés que MP te dispare webhooks reales (sandbox) contra tu máquina:

```powershell
# Instalar ngrok (https://ngrok.com)
ngrok http 7071

# Te da una URL pública tipo https://abc123.ngrok-free.app
# Configurar esa URL + /api/mp/webhook en el panel de MP
```

MP enviará webhooks reales y vas a poder verificar el flow end-to-end con HMAC
real.

---

## 9. Deploy a Azure (Bloque 4 — pendiente)

> Esta sección quedará completa cuando ejecutemos el Bloque 4. Por ahora dejo
> el outline para que sepas qué se va a generar.

### 9.1 Recursos Azure que vamos a crear

| Recurso | Nombre tentativo | Para qué |
|---|---|---|
| Resource Group | `rg-finance-sync-prod` | Contenedor lógico |
| Log Analytics Workspace | `log-finance-sync-prod` | Backend de Application Insights |
| Application Insights | `appi-finance-sync-prod` | Telemetría |
| Key Vault | `kv-finance-sync-prod` | Secretos |
| Storage Account | `stfinancesyncprod` | Required por Functions (timer locks, etc.) |
| SQL Server | `sql-finance-sync-prod` | Servidor lógico |
| SQL Database | `finance` | DB serverless |
| Function App (HTTP) | `func-mp-webhook-prod` | Webhook MP |
| Function App (Timer) | `func-ib-poller-prod` | Poller IB |
| Managed Identity | (system-assigned por cada Function) | Auth contra Key Vault y SQL |

### 9.2 Estructura prevista

```
infra/
├── 00-prereqs.ps1              chequea az login, suscripción, etc.
├── 10-create-foundation.ps1    RG + Log Analytics + AppInsights + KeyVault + Storage
├── 20-create-sql.ps1           SQL Server + DB + firewall + admin AAD
├── 30-create-function-mp.ps1   Function App MP webhook + MI + AppSettings
├── 40-create-function-ib.ps1   Function App IB poller + MI + AppSettings
├── 50-load-secrets.ps1         carga .env → Key Vault, otorga acceso a las MI
├── 60-deploy-code.ps1          build wheel + func azure functionapp publish (ambas)
└── 99-teardown.ps1             borra todo (cuidado: irreversible)
```

Cada script será **idempotente**: podés correrlo N veces sin romper nada.

### 9.3 Flow esperado del primer deploy

```powershell
cd infra
.\00-prereqs.ps1                 # Verifica que tenés az CLI, login, etc.
az login
az account set --subscription "<tu_sub>"

.\10-create-foundation.ps1
.\20-create-sql.ps1
.\30-create-function-mp.ps1
.\40-create-function-ib.ps1
.\50-load-secrets.ps1            # Lee .env y publica a Key Vault
.\60-deploy-code.ps1             # Build wheel + publish ambas functions
```

Tiempo estimado: 15-20 minutos la primera vez.

---

## 10. Operación, observabilidad y mantenimiento

### 10.1 Cambios en código

```
1. Editás archivo (ej: shared/db_helpers.py o mp_webhook_function/mp_processor.py).
2. Si tocaste algo en shared/: corré .\build_shared_wheel.ps1
3. Probás localmente con func start (ver sección 8).
4. Commit con conventional commit (feat:, fix:, chore:, etc.).
5. Deploy a Azure: cd infra; .\60-deploy-code.ps1
6. Verificás logs en Application Insights.
```

### 10.2 Cambios en secretos

- **NUNCA editar `.env` en producción**. Los secretos viven en Key Vault.

```powershell
# Rotar el MP_ACCESS_TOKEN (ejemplo)
az keyvault secret set --vault-name kv-finance-sync-prod --name MP-ACCESS-TOKEN --value "APP_USR_nuevo_token"

# Reiniciar la Function para que lo recoja (sino usa el cacheado en _cached_config)
az functionapp restart --name func-mp-webhook-prod --resource-group rg-finance-sync-prod
```

> **Nota sobre nombres**: env var `MP_ACCESS_TOKEN` ↔ Key Vault secret `MP-ACCESS-TOKEN`.
> El `_` se traduce a `-` (regla de Key Vault) automáticamente por
> `AzureSecretsClient`.

### 10.3 Monitoreo del día a día

Mientras no tengamos el Bloque 5 (alertas), inspeccioná manualmente:

#### Application Insights — Live Metrics

```powershell
# Streaming en tiempo real
az monitor app-insights events show --app appi-finance-sync-prod --resource-group rg-finance-sync-prod --type traces
```

#### Queries útiles en App Insights (KQL)

```kql
// Webhooks MP de la última hora
traces
| where timestamp > ago(1h)
| where cloud_RoleName == "func-mp-webhook-prod"
| where message contains "mp_webhook"
| order by timestamp desc

// Errores en el poller IB
traces
| where timestamp > ago(24h)
| where cloud_RoleName == "func-ib-poller-prod"
| where severityLevel >= 3
| order by timestamp desc

// Duración por sub-proceso del poller IB (OPS-04)
traces
| where timestamp > ago(24h)
| where message has "duration_ms"
| extend duration_ms = toint(extract("duration_ms=([0-9]+)", 1, message))
| extend process = extract("sync (interbanking_[a-z]+)", 1, message)
| where isnotempty(process)
| summarize avg(duration_ms), max(duration_ms), count() by process
| order by avg_duration_ms desc
```

#### SQL — health checks rápidos

```sql
-- ¿Cuándo fue el último sync exitoso de cada proceso?
SELECT process_name, last_status, last_successful_sync,
       DATEDIFF(MINUTE, last_successful_sync, SYSUTCDATETIME()) AS minutes_since_last_success
FROM finance.sync_control
ORDER BY last_successful_sync DESC;

-- Errores recientes
SELECT TOP 20 sync_run_id, process_name, started_at, finished_at, status, error_message
FROM finance.sync_runs
WHERE status = 'ERROR'
ORDER BY started_at DESC;

-- Volumen de datos por día
SELECT CAST(date_last_updated AS DATE) AS dia, COUNT(*) AS pagos
FROM finance.mp_payments
WHERE date_last_updated > DATEADD(DAY, -30, SYSUTCDATETIME())
GROUP BY CAST(date_last_updated AS DATE)
ORDER BY dia DESC;
```

### 10.4 Mantenimiento periódico

| Frecuencia | Tarea |
|---|---|
| **Diario** (automático cuando esté el Bloque 5) | Revisar alertas de errores |
| **Semanal** | Verificar `sync_control.last_status` de los 6 procesos IB y MP |
| **Mensual** | Revisar growth de la DB; ejecutar `sp_estimate_data_compression_savings` por si vale ajustar |
| **Trimestral** | Rotar `MP_ACCESS_TOKEN` e `IB_PASSWORD` |
| **Semestral** | `pip list --outdated` y actualizar pinning con criterio (no breaking) |
| **Anual** | Revisar y purgar `raw_json` de >12 meses si no se usa |

---

## 11. Troubleshooting frecuente

### 11.1 `ImportError: cannot import name 'SecretString' from 'shared.secret_string'`

**Causa**: el venv no instaló el paquete editable o el wheel está viejo.

**Solución**:
```powershell
pip install -e .
.\build_shared_wheel.ps1
```

### 11.2 `pyodbc.InterfaceError: ('IM002', '[IM002] [Microsoft][ODBC Driver Manager] Data source name not found...`

**Causa**: falta el ODBC Driver 18.

**Solución**: instalá desde
https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server.

### 11.3 `func start` arranca pero el webhook devuelve 404

**Causa**: `function_app.py` no se está cargando (probablemente import error).

**Solución**: revisá la consola al arranque. Si hay un `ModuleNotFoundError`,
el wheel no se instaló o el `requirements.txt` está desactualizado:

```powershell
cd mp_webhook_function
pip install -r requirements.txt --force-reinstall
```

### 11.4 HMAC siempre rechazado aunque el secret esté bien

**Causa más común**: el `MP_WEBHOOK_SECRET` tiene espacios o saltos de línea
ocultos al copiarlo del panel de MP.

**Solución**: re-copiarlo con cuidado, sin trailing spaces. Verificá con:

```powershell
$env:MP_WEBHOOK_SECRET.Length
# Comparar contra la longitud que muestra MP
```

### 11.5 El poller IB se cuelga sin terminar

**Causa más probable**: la API de IB está respondiendo lento y el timeout (10 min)
no alcanza para procesar todas las cuentas.

**Diagnóstico**:
```sql
SELECT process_name, started_at, finished_at, status,
       DATEDIFF(SECOND, started_at, finished_at) AS duration_sec
FROM finance.sync_runs
WHERE source_system = 'INTERBANKING'
ORDER BY started_at DESC;
```

**Soluciones**:
1. Aumentar `IB_INCREMENTAL_LOOKBACK_DAYS=0` para reducir ventana.
2. Subir `functionTimeout` en `host.json` a `00:30:00` (max plan Consumption).
3. Si persiste: migrar a Container Apps Job (Bloque 4 alt).

### 11.6 `ConfigError: Missing required environment variables: ...`

**Causa**: `local.settings.json` no carga, o algunas variables están en `.env`
pero no en `local.settings.json`.

**Solución**: cada Function lee de SU `local.settings.json` (no del `.env` raíz).
Asegurate de tener un `local.settings.json` por carpeta (`mp_webhook_function/`
y `ib_poller/`).

---

## 12. Roadmap pendiente

### Bloque 3 — Tests (próximo a definir)

- Unit tests para `shared/db_helpers.py` (`build_merge_sql`, `sanitize_to_json`)
- Unit tests para `shared/secret_string.py` (`__repr__`, `reveal()`)
- Unit tests para `shared/config.py` (validación de errores)
- Unit tests para `mp_webhook_function/function_app.py` con HMAC mock
- Tests de integración con Azure SQL (con un container `mssql` local)
- Coverage objetivo: >80% en `shared/`, >70% en `mp_processor.py` y `ib_processor.py`

### Bloque 4 — Infraestructura Azure (siguiente bloque a ejecutar)

Ver outline en sección [9. Deploy a Azure](#9-deploy-a-azure-bloque-4--pendiente).

### Bloque 5 — Observabilidad

- Conexión completa a Application Insights (`opencensus-ext-azure` ya está en
  requirements)
- Logging estructurado JSON con `python-json-logger`
- Alertas de Azure Monitor:
  - Webhook MP con error rate >1% en 5 min
  - Poller IB sin sync exitoso por >30 min
  - SQL DTU >80% sostenido
  - Disponibilidad de las Function Apps
- Dashboard de Azure con widgets de:
  - Volumen de pagos MP por hora
  - Latencia p50/p95/p99 del webhook
  - Duración de cada sub-proceso IB
  - Conteo de errores por categoría

---

## 13. Apéndices

### 13.1 Convenciones de commits

Seguimos **Conventional Commits**, mensaje <60 caracteres:

```
feat: add HMAC validation to MP webhook
fix: handle null amount in IB movements
chore: bump pyodbc to 5.1.1
docs: update PROYECTO.md with deploy section
refactor: extract execute_upsert helper
test: add coverage for sanitize_to_json
```

### 13.2 Referencias externas

- [Azure Functions Python v2 model](https://learn.microsoft.com/azure/azure-functions/functions-reference-python?pivots=python-mode-decorators)
- [MP Webhooks docs](https://www.mercadopago.com.ar/developers/es/docs/notifications/webhooks)
- [MP signature validation](https://www.mercadopago.com.ar/developers/es/docs/your-integrations/notifications/webhooks#editor_5)
- [Interbanking API docs](https://www.interbanking.com.ar/desarrolladores) (acceso restringido)
- [NCRONTAB syntax](https://learn.microsoft.com/azure/azure-functions/functions-bindings-timer?pivots=programming-language-python#ncrontab-expressions)
- [pyodbc connection strings](https://github.com/mkleehammer/pyodbc/wiki/Connection-Strings)
- [Azure Key Vault Python SDK](https://learn.microsoft.com/python/api/overview/azure/keyvault-secrets-readme)
- [SOLID principles refresher](https://en.wikipedia.org/wiki/SOLID)

### 13.3 IDs de cambio referenciados desde código

Buscar en el código una etiqueta como `SEC-02` o `CAL-02` te lleva al lugar
exacto donde se aplicó el cambio. Útil para entender el "por qué" de cada decisión.

```powershell
# Ver todos los IDs en uso en el código:
Select-String -Path "**/*.py","**/*.sql","**/*.md" -Pattern "(SEC|CAL|AZ|OPS)-\d+" -AllMatches |
    ForEach-Object { $_.Matches.Value } | Sort-Object -Unique
```

### 13.4 Tabla de archivos por componente

| Componente | Archivos clave | Línea más importante |
|---|---|---|
| `shared/secret_string.py` | toda la clase | `__repr__` que devuelve `***SECRET***` |
| `shared/azure_secrets.py` | `AzureSecretsClient.get_secret()` | Fallback a env var si Key Vault no configurado |
| `shared/config.py` | `AppConfig.from_env()` | Validación agregada (todos los errores juntos) |
| `shared/db_helpers.py` | `execute_upsert()` y `sanitize_to_json()` | Reemplaza ~700 líneas de MERGE manual |
| `shared/interbanking_client.py` | `_get_token()` | Body en lugar de query string (SEC-02) |
| `mp_webhook_function/function_app.py` | `_verify_signature()` | HMAC + anti-replay |
| `mp_webhook_function/mp_processor.py` | `_is_already_current()` | Capa 1 de idempotencia |
| `ib_poller/function_app.py` | decorador `@app.timer_trigger` | Cron + singleton |
| `ib_poller/ib_processor.py` | `_sync_context()` y `run_full_sync()` | Encapsula sync_runs + sync_control |

---

**Fin del documento.**

Si encontrás algo confuso o desactualizado mientras trabajás con este proyecto,
abrí un PR actualizando este `PROYECTO.md`. La doc desactualizada es peor que
no tener doc.
