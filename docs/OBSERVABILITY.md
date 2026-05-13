# Observabilidad — guía de KQL queries

Esta guía complementa la sección 10.3 de `PROYECTO.md`. Las queries acá
asumen que el bootstrap de `shared/observability.py` ya corre en ambas
Functions (refactor de Tanda 5), por lo que los logs llegan a
Application Insights en **formato JSON estructurado** con
`customDimensions.service` distinguiendo `mp_webhook` de `ib_poller`.

## Tabla de queries

Pegá cualquiera de éstas en **Application Insights → Logs**.

### Logs MP webhook de la última hora

```kql
traces
| where timestamp > ago(1h)
| where customDimensions.service == "mp_webhook"
| project timestamp, severityLevel, message, customDimensions
| order by timestamp desc
```

### Errores del poller IB en 24h

```kql
traces
| where timestamp > ago(24h)
| where customDimensions.service == "ib_poller"
| where severityLevel >= 3   // 3 = Error
| order by timestamp desc
```

### Duración por sub-proceso del poller IB

Antes era un `extract()` con regex porque los logs eran texto. Ahora cada
log estructurado tiene campos JSON:

```kql
traces
| where timestamp > ago(24h)
| where customDimensions.service == "ib_poller"
| where message has "sync " and message has "OK: read="
// extraemos process, read, upserted, duration_ms del mensaje
| parse message with "sync " process " OK: read=" read_count:int " upserted=" upserted:int " duration_ms=" duration_ms:int
| where isnotempty(process)
| summarize avg(duration_ms), max(duration_ms), count() by process
| order by avg_duration_ms desc
```

### Latencia del webhook MP (p50, p95, p99)

```kql
requests
| where timestamp > ago(24h)
| where cloud_RoleName has "mp-webhook"
| where url has "/api/mp/webhook"
| summarize
    p50 = percentile(duration, 50),
    p95 = percentile(duration, 95),
    p99 = percentile(duration, 99),
    count = count()
  by bin(timestamp, 1h)
| order by timestamp desc
```

### Webhook MP por status code

```kql
requests
| where timestamp > ago(24h)
| where cloud_RoleName has "mp-webhook"
| summarize count() by resultCode, bin(timestamp, 1h)
| order by timestamp desc
```

### Webhooks rechazados por HMAC inválido

```kql
traces
| where timestamp > ago(24h)
| where customDimensions.service == "mp_webhook"
| where message has "HMAC inválido"
// el mensaje incluye el reason: ts_too_old, hmac_mismatch, etc.
| parse message with * "HMAC inválido (" reason ") para event_type=" event_type
| summarize count() by reason
| order by count_ desc
```

### Procesamiento async del payment (Queue worker)

```kql
traces
| where timestamp > ago(24h)
| where customDimensions.service == "mp_webhook"
| where message has "payment " and message has " procesado:"
| count
```

### Backlog actual de la queue mp-payment-ids

```kql
AzureMetrics
| where TimeGenerated > ago(1h)
| where ResourceProvider == "MICROSOFT.STORAGE"
| where MetricName == "QueueMessageCount"
| where Dimensions has "mp-payment-ids"
| summarize avg(Average) by bin(TimeGenerated, 5m)
| order by TimeGenerated desc
```

## Comparación con queries pre-refactor

Las queries de PROYECTO.md sec. 10.3 hacían `extract("duration_ms=([0-9]+)", 1, message)`
porque los logs eran texto plano:

```kql
// Antes (texto plano)
| extend duration_ms = toint(extract("duration_ms=([0-9]+)", 1, message))
| extend process = extract("sync (interbanking_[a-z]+)", 1, message)
```

Con JSON structured pueden usar `parse` que es más legible y más rápido:

```kql
// Ahora (JSON)
| parse message with "sync " process " OK: read=" read_count:int " upserted=" upserted:int " duration_ms=" duration_ms:int
```

## Cómo se conecta esto con las alertas de `infra/80-create-alerts.ps1`

| Alerta | Cubre | Cómo investigar cuando dispara |
|---|---|---|
| `alert-mp-5xx` | webhook con 5xx en últimos 5min | "Webhooks rechazados por HMAC" o filtrar `requests` con resultCode >= 500 |
| `alert-ib-no-runs` | IB poller no ejecutó en 30min | Buscar logs de `ib_poller_run`; revisar `sync_runs` SQL |
| `alert-sql-cpu-high` | SQL CPU > 80% | Portal SQL → Query Performance Insight |
| `alert-queue-backlog` | Queue > 100 mensajes pendientes | Logs del worker `mp_process_payment`; revisar si MP API está caído |

## Futuras mejoras

1. **Log query alert** que dispara cuando no aparece `sync .* OK` para un
   proceso específico en >30min. Hoy `alert-ib-no-runs` solo detecta
   "ninguna ejecución"; podría correr el cron pero fallar todos los
   sub-procesos sin disparar la alerta.
2. **Workbook** combinando las 6 queries de arriba en un dashboard
   visual. El portal Azure Monitor lo permite con UI; armarlo via
   `az monitor workbook` es engorroso, por eso queda fuera de los
   scripts `infra/`.
3. **Migración a OpenTelemetry** (`azure-monitor-opentelemetry`) cuando
   se reescriba el bootstrap. OpenCensus está deprecating.
