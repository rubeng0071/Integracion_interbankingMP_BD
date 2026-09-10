# Login timeouts en `mp_process_payment`: cambios a implementar

**Repo:** `servicio_interbankingMP_toBD` · **Function App:** `func-mp-webhook-prod` (`rg-rapanui-finance-prod`, Y1 Consumption, Python 3.11) · **Base:** `cobrosconciliaciones/finance` (Standard S3, 100 DTU)

Diagnóstico hecho el 10-sep-2026 con Application Insights, métricas de Azure SQL y lectura del código. No se aplicó ningún cambio: este documento es la especificación.

---

## 1. Qué está pasando

### El síntoma

El worker `mp_process_payment` falla miles de veces por día con **un único error**:

```
pyodbc.OperationalError: ('HYT00', '[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]
Login timeout expired (0) (SQLDriverConnect)')
  File "/home/site/wwwroot/function_app.py", line 308, in mp_process_payment
```

- **24.781 errores en 3 días, el 100 % son `Login timeout expired`.** Cero timeouts de consulta. Falla al *establecer* la conexión, antes de ejecutar SQL.
- Viene de **al menos el 24-ago** (es hasta donde llega la retención del workspace).
- **No se pierden pagos.** Cada pago distinto falla ~25 veces antes de entrar, pero entra: cero mensajes en `mp-payment-ids-poison` en los últimos 5 días.
- Sigue la curva de ventas: el pico es de 00 a 06 UTC (21 a 03 hora argentina) y los fines de semana.

### La causa

`mp_webhook_function/function_app.py:308`:

```python
with pyodbc.connect(config.sql_connection_string.reveal(), autocommit=False) as conn:
```

**Cada mensaje de la cola abre una conexión nueva a Azure SQL.** El `with` de pyodbc hace commit o rollback al salir pero no cierra ni reutiliza: la conexión muere al terminar la invocación y la próxima vuelve a hacer handshake TCP + TLS + login.

El login consume CPU del servidor, y la base tiene ~1 vCore (S3). En los picos, `cpu_percent` llega al **100 % todos los días**; con la CPU saturada los logins se encolan, superan los **15 segundos** de login timeout por defecto del Driver 18, y el mensaje se reintenta, lo que suma más logins.

### El amplificador: el 88 % de las ejecuciones no escribe nada

De las ejecuciones exitosas de los últimos 3 días, **el 88,2 % terminan en `skipped=True`**: el pago ya estaba al día en SQL.

El origen es el poller. Corre cada 30 minutos (`MP_POLLER_SCHEDULE = 0 */30 * * * *`) sobre una ventana de 4 horas (`MP_INCREMENTAL_LOOKBACK_HOURS = 4`), así que **cada pago se encola unas 8 veces**. Cada una de esas ejecuciones hace un GET a MercadoPago, **abre un login nuevo a SQL**, lee `date_last_updated` por clave primaria y descubre que no hay nada que escribir.

### Lo que se descartó con datos

| Hipótesis | Por qué no |
|---|---|
| Consulta lenta | 0 timeouts de query. `_is_already_current` busca por `payment_id`, que es la clave primaria. |
| Red | Función y SQL en East US 2, política de conexión `Default` (redirect), sin private endpoint ni VNet. |
| Falta de sesiones o workers | `sessions_percent` ≈ 1 %, `workers_percent` 4–7 %. |
| El cambio de SKU del 2-sep | Los timeouts ya ocurrían con la base en serverless de 2 vCore. |
| `ib_poller` | **No tiene el problema.** Su clase `Database` (`ib_poller/ib_processor.py`) ya mantiene una conexión por ciclo, con tests en `tests/test_database_pool.py`. **No hay que tocarlo.** |

---

## 2. Cambio 1: reusar la conexión por hilo en el worker (arreglo principal)

### Por qué una conexión por hilo, no una global

pyodbc tiene `threadsafety = 1`: una conexión **no se puede compartir entre hilos**. El worker de Python ejecuta las funciones síncronas en un thread pool y `host.json` permite hasta 16 mensajes en paralelo por instancia (`batchSize: 16`). Una conexión global única rompería con dos mensajes concurrentes.

Con `threading.local()` cada hilo guarda su propia conexión y la reutiliza en las invocaciones *warm* siguientes. Pasamos de **un login por mensaje** a **un login por hilo** mientras la instancia esté viva.

El criterio es el mismo que ya usa `ib_poller.ib_processor.Database`: ping liviano antes de usar, y reapertura si la conexión quedó zombi (Azure SQL corta las conexiones inactivas).

### Código propuesto

Nuevo módulo `mp_webhook_function/db_conn.py`:

```python
"""Conexión SQL reutilizada por hilo para el worker mp_process_payment.

pyodbc tiene threadsafety=1: una conexión no se comparte entre hilos. El worker
de Python corre las funciones síncronas en un thread pool, así que cada hilo guarda
la suya y la reutiliza entre invocaciones warm.

Mismo criterio que ib_poller.ib_processor.Database: ping liviano antes de usar y
reapertura si la conexión quedó zombi (idle timeout de Azure SQL, corte de red).
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

import pyodbc

logger = logging.getLogger(__name__)

# El default del ODBC Driver 18 es 15 s, exactamente donde hoy se corta.
LOGIN_TIMEOUT_SECONDS = 30


class ThreadLocalConnection:
    def __init__(self, conn_str: str, login_timeout: int = LOGIN_TIMEOUT_SECONDS) -> None:
        self._conn_str = conn_str
        self._login_timeout = login_timeout
        self._local = threading.local()

    def _open(self) -> pyodbc.Connection:
        # `timeout=` en pyodbc.connect es el login timeout (SQL_ATTR_LOGIN_TIMEOUT).
        return pyodbc.connect(self._conn_str, autocommit=False, timeout=self._login_timeout)

    def _discard(self) -> None:
        conn: Optional[pyodbc.Connection] = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except pyodbc.Error:
                pass

    def _ensure_alive(self) -> pyodbc.Connection:
        conn: Optional[pyodbc.Connection] = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.cursor().execute("SELECT 1").fetchone()
                return conn
            except pyodbc.Error:
                logger.warning("Conexión SQL muerta en este hilo; reabriendo")
                self._discard()
        conn = self._open()
        self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[pyodbc.Connection]:
        """Entrega la conexión del hilo; commit al salir bien, rollback si falla.

        Ante un pyodbc.Error la conexión se descarta: puede haber quedado
        inutilizable, y el próximo mensaje de este hilo abre una nueva.
        """
        conn = self._ensure_alive()
        try:
            yield conn
            conn.commit()
        except pyodbc.Error:
            try:
                conn.rollback()
            except pyodbc.Error:
                pass
            self._discard()
            raise
        except Exception:
            try:
                conn.rollback()
            except pyodbc.Error:
                pass
            raise
```

En `mp_webhook_function/function_app.py`, junto a `_cached_config` y `_cached_mp_client` (mismo patrón):

```python
import threading

from db_conn import ThreadLocalConnection

_cached_db: Optional[ThreadLocalConnection] = None
_db_init_lock = threading.Lock()


def _get_db() -> ThreadLocalConnection:
    """Wrapper de conexión único, reutilizado entre invocaciones warm."""
    global _cached_db
    if _cached_db is None:
        with _db_init_lock:
            if _cached_db is None:
                _cached_db = ThreadLocalConnection(_get_config().sql_connection_string.reveal())
    return _cached_db
```

Y en `mp_process_payment` se reemplazan las líneas 307 a 317:

```python
    try:
        with _get_db().transaction() as conn:
            result = upsert_payment(conn, payment)
    except pyodbc.Error:
        logger.exception("[%s] Error de DB procesando payment %s", invocation_id, payment_id)
        raise
```

El commit y el rollback pasan a vivir en `transaction()`. `upsert_payment` no cambia: ya espera una conexión abierta con `autocommit=False` y no commitea por su cuenta.

### Tests: `tests/test_mp_db_conn.py`

Modelados sobre `tests/test_database_pool.py` (mismo `_make_mock_conn`, `monkeypatch` de `pyodbc.connect`, y el mismo `sys.path` que usa `test_mp_poller.py` para importar desde `mp_webhook_function/`):

- Dos transacciones seguidas en el mismo hilo abren **una sola** conexión.
- Dos hilos distintos obtienen conexiones **distintas**.
- Conexión zombi (el ping lanza `pyodbc.Error`) → se cierra y se reabre.
- `pyodbc.Error` dentro de la transacción → rollback, descarte, y la próxima transacción abre una conexión nueva.
- Excepción que no es de pyodbc → rollback, y la conexión **se conserva**.
- Salida normal → `commit()` llamado una vez.
- `pyodbc.connect` recibe `timeout=30`.

### Efecto esperado

Los logins pasan de uno por mensaje a uno por hilo mientras viva la instancia. Con eso los `Login timeout expired` deberían desaparecer, y con ellos los reintentos y el volumen de excepciones que hoy se factura como ingesta en Log Analytics.

---

## 3. Cambio 2: que el poller no encole pagos que ya están al día

Este cambio ataca el 88 % de ejecuciones inútiles. Es independiente del cambio 1 y se puede hacer después.

### La idea

`collect_payment_ids` recorre los resultados de `GET /v1/payments/search`, y cada resultado es el recurso de pago completo, con su `date_last_updated`. El poller puede leer de SQL los `date_last_updated` guardados para esos IDs **en una sola consulta por lote**, y encolar sólo los que cambiaron. Hoy encola todos y deja que el worker lo descubra de a uno, con un GET a MercadoPago y un login por pago.

**La regla tiene que ser la misma que `_is_already_current`** (`mp_processor.py`): `parse_dt(...)` sobre el valor de MercadoPago, el valor de SQL con `.replace(tzinfo=None)`, y comparación con `==`. Ante cualquier duda (fecha ausente en el resultado, pago inexistente en SQL, valor nulo), **se encola**.

### Diseño

`collect_payment_ids` está escrita como función pura a propósito, para poder testearla. Hay que mantener eso:

1. **`collect_payment_candidates(...)`**: igual que hoy, pero devuelve `List[Tuple[str, Optional[datetime]]]` (id + `parse_dt(payment.get("date_last_updated"))`).
2. **`select_changed(candidates, stored: Dict[str, datetime]) -> List[str]`**: función pura. Decide qué encolar aplicando la regla de arriba.
3. **`load_stored_last_updated(conn, ids) -> Dict[str, datetime]`**: la única parte que toca SQL.
   ```sql
   SELECT payment_id, date_last_updated FROM finance.mp_payments WHERE payment_id IN (?, ?, ...)
   ```
   En lotes de **500 IDs** (SQL Server admite hasta 2.100 parámetros por sentencia). `payment_id` es `BIGINT` y la clave primaria: cada lote es un seek.
4. **`mp_poller_run`** usa **una conexión para todo el ciclo** (sirve `_get_db()` del cambio 1).

### Una regla no negociable

El poller es la red de seguridad. **Si la consulta a SQL falla, se encola todo, igual que hoy**, con un `logger.warning`. Un error de base nunca puede hacer que el poller encole *menos*.

### Antes de confiar en el campo

Hoy los fixtures de `tests/test_mp_client.py` y `tests/test_mp_poller.py` no traen `date_last_updated` en los resultados de la búsqueda (sólo verifican que el parámetro `range` sea ese). Hay que **confirmar con una respuesta real de `/v1/payments/search`** que el campo viene en cada resultado, y agregarlo a los fixtures. Si falta en algún resultado, ese pago se encola.

### Tests: ampliar `tests/test_mp_poller.py`

- `select_changed`: ID nuevo → encola · misma fecha → no encola · fecha distinta → encola · fecha ausente en el resultado → encola · fecha nula en SQL → encola · fecha con zona horaria igual a la naive guardada → no encola.
- `load_stored_last_updated`: parte en lotes de 500 y nunca pasa de 2.100 parámetros.
- `mp_poller_run` con `pyodbc.Error` en la carga → encola la lista completa.

### Si esto tarda: alternativa sólo de configuración

Bajar `MP_INCREMENTAL_LOOKBACK_HOURS` de 4 a 1 reduce el solapamiento de ~8 veces a ~2, sin tocar código. **Tiene un costo:** hoy el poller tolera caídas propias de hasta ~3,5 h sin perder pagos; con 1 h, una caída de más de media hora deja un hueco que después no se recorre. Es una decisión del dueño del servicio, no del desarrollador.

---

## 4. Cambio 3: techo de escalado (configuración, no código)

`functionAppScaleLimit` está en **200**, el máximo por defecto. En los picos se observaron 6 instancias, así que un techo de 10 no frena la carga actual y sí corta un escalado desbocado como el del incidente de `dataxprt-fnprocess` en agosto.

```bash
az resource update -g rg-rapanui-finance-prod \
  -n func-mp-webhook-prod/config/web --resource-type Microsoft.Web/sites \
  --set properties.functionAppScaleLimit=10
```

Hoy este valor no está en ningún script de `infra/`. Conviene agregarlo ahí para que un redeploy no lo pise.

---

## 5. Índices: aparte, en otro repo, y verificando primero

Azure SQL tiene dos recomendaciones **activas** de índices y tres de índices sin uso. **No son la causa de los login timeouts**: las lecturas del worker son seeks por clave primaria. Pueden sí contribuir a los picos de CPU de la base por otras consultas, y por eso se mencionan. El *automatic tuning* está en `Disabled` para crear y borrar índices.

### Crear: revisar antes contra lo que ya existe

| Recomendación de Azure | Lo que ya existe (migración `028_indices_performance.sql` de cobros) |
|---|---|
| `mp_payments (status, date_approved) INCLUDE (...)` | `IX_mpp_approved_fecha` sobre `(date_approved) ... WHERE status = N'approved'` |
| `ib_movements (debit_credit_type, amount) INCLUDE (...)` | `IX_ibm_credit_fecha` sobre `(real_date_activity) ... WHERE debit_credit_type = N'C'` |

Lo más probable es que las recomendaciones vengan de consultas que **no calzan con el predicado de los índices filtrados**, por ejemplo con `status` o `debit_credit_type` parametrizados. Antes de crear nada:

1. Buscar en **Query Performance Insight** (portal) o en `sys.query_store_*` las consultas que motivan cada recomendación.
2. Si se confirman, van como migración **`038_*.sql`** en `app-cobros-ventas/app-cobros-ventas-backend/db/sql/`, con el mismo estilo idempotente que la `028` (`IF NOT EXISTS ... EXEC`) y `WITH (ONLINE = ON)`.

### Borrar: esperar a tener historial

| Índice | Tabla |
|---|---|
| `IX_mp_payments_date_last_updated` | `finance.mp_payments` (la tabla que más se escribe) |
| `IX_ib_balances_account_row_date` | `finance.ib_balances` |
| `IX_ib_extracts_source_account_operation_date` | `finance.ib_extracts` |

En ninguno de los dos repos hay consultas que filtren por esas columnas, lo que coincide con "sin uso". Pero **las estadísticas de uso se reinician cuando se cambia el SKU de la base**, y eso pasó el 2-sep. Antes de borrar, confirmar con 2 a 4 semanas de historial:

```sql
SELECT OBJECT_NAME(s.object_id) AS tabla, i.name AS indice,
       s.user_seeks, s.user_scans, s.user_lookups, s.user_updates,
       s.last_user_seek, s.last_user_scan
FROM sys.dm_db_index_usage_stats s
JOIN sys.indexes i ON i.object_id = s.object_id AND i.index_id = s.index_id
WHERE s.database_id = DB_ID()
  AND i.name IN (N'IX_mp_payments_date_last_updated',
                 N'IX_ib_balances_account_row_date',
                 N'IX_ib_extracts_source_account_operation_date');
```

Si `user_seeks + user_scans + user_lookups` sigue en 0 y `user_updates` crece, el índice sólo cuesta.

---

## 6. Lo que NO hay que hacer

- **No tocar `ib_poller`.** Ya tiene el arreglo.
- **No bajar `maxDequeueCount`** (hoy 5). Es lo que garantiza que ningún pago termine en poison.
- **No subir el SKU como primera medida.** S3 → S4 son +$147/mes y no cambian el patrón de un login por mensaje.
- **No tocar `unified_finance_sync_service.py` ni `main_interactive.py`** (legacy, según el `CLAUDE.md` del repo).

---

## 7. Orden y commits sugeridos

Según las convenciones del repo: español, conventional commits, un commit por idea, tests incluidos.

1. `perf(mp_webhook): reusar la conexión SQL por hilo en mp_process_payment` · `db_conn.py` + cambio en `function_app.py` + `tests/test_mp_db_conn.py`
2. `perf(mp_webhook): el poller no encola pagos que ya están al día` · refactor de `collect_payment_ids` + `select_changed` + `load_stored_last_updated` + tests
3. *(infra)* `functionAppScaleLimit = 10` y agregarlo a `infra/`
4. *(repo cobros, aparte)* índices, sólo después de la verificación de la sección 5

Antes de cada commit: `pytest` verde, incluidos `test_database_pool.py`, `test_db_helpers.py` y `test_mp_poller.py`.

---

## 8. Cómo verificar en producción

Workspace `log-rapanui-finance-prod`.

**Login timeouts por día.** Esperado tras el cambio 1: prácticamente cero.

```kql
AppExceptions
| where TimeGenerated > ago(7d) and ProblemId startswith "OperationalError"
| where tostring(parse_json(OuterMessage).exc_info) has "Login timeout expired"
| summarize n = count() by bin(TimeGenerated, 1d)
```

**Proporción de ejecuciones que no escriben nada.** Hoy ~88 % `True`. Esperado tras el cambio 2: una minoría.

```kql
AppTraces
| where TimeGenerated > ago(3d) and Message has "procesado: skipped="
| extend skip = extract(@"skipped=(True|False)", 1, Message)
| summarize n = count() by skip
```

**Ejecuciones del worker por día.** Esperado tras el cambio 2: una caída del orden del 80 %.

```kql
AppRequests
| where TimeGenerated > ago(14d) and Name has "mp_process_payment"
| summarize ejecuciones = count(), fallidas = countif(Success == false) by bin(TimeGenerated, 1d)
```

**Cola poison.** Tiene que seguir en cero.

```kql
AppTraces
| where TimeGenerated > ago(7d) and Message has "poison"
| summarize n = count() by bin(TimeGenerated, 1d)
```

Además, en Azure Monitor, la métrica `cpu_percent` (máximo diario) de `cobrosconciliaciones/finance`: hoy toca 100 % todos los días.

### Criterios de aceptación

- `pytest` verde.
- 48 horas en producción sin `Login timeout expired`, o menos del 1 % de lo actual.
- Cola poison en cero.
- Tras el cambio 2, las ejecuciones de `mp_process_payment` bajan alrededor de un 80 %.

---

## Observación aparte (fuera de este alcance)

En los mismos 3 días hay más líneas de traza `procesado: skipped=` que ejecuciones registradas en `AppRequests`. Probablemente el mismo log se emite dos veces (handler propio de `configure_logging` más la captura del worker). No afecta a este arreglo, pero sí a lo que se paga de ingesta en Log Analytics, que hoy es el 99 % nivel *Information*. Vale una revisión separada.
