"""Azure Function v2: Timer Trigger que sincroniza Interbanking → Azure SQL.

Schedule: NCRONTAB '0 */10 * * * *' = cada 10 minutos.
Configurable vía la app setting IB_POLLER_SCHEDULE.

Ciclo de cada ejecución (AZ-09 + AZ-11 — sin run_forever, sin systemd):
    1. Cargar AppConfig desde Key Vault o env vars (CAL-11).
    2. IBProcessor.run_full_sync() — itera los 6 sub-procesos, cada uno con
       su sync_run propio y su update de sync_control.
    3. Logging estructurado de stats por sub-proceso (OPS-04: duration_ms).
    4. Si el host.json marca singleton=true (lo está), Azure garantiza que
       no haya 2 ejecuciones solapadas aunque el cron dispare antes del
       fin de la anterior.

Concurrency:
    - functionTimeout = 10 min (host.json) — si tarda más, se aborta.
    - singleton=true (host.json) — solo una instancia activa a la vez.

Nota: el binding del Timer requiere AzureWebJobsStorage (cualquier tabla de
storage account funciona; el extension lo usa para distributed locks).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import azure.functions as func

from shared.config import ConfigError, IbPollerConfig

from ib_processor import IBProcessor

logger = logging.getLogger(__name__)


# =====================================================================
# Cache de config entre invocaciones warm
# =====================================================================

_cached_config: Optional[IbPollerConfig] = None


def _get_config() -> IbPollerConfig:
    global _cached_config
    if _cached_config is None:
        try:
            _cached_config = IbPollerConfig.from_env()
        except ConfigError as exc:
            logger.error("Config inválida al arrancar el poller: %s", exc)
            raise
    return _cached_config


# =====================================================================
# Schedule configurable
# Default: '0 */10 * * * *' = cada 10 minutos al segundo 0.
# Formato NCRONTAB: {sec} {min} {hour} {day} {month} {day-of-week}
# =====================================================================

_SCHEDULE = os.getenv("IB_POLLER_SCHEDULE", "0 */10 * * * *")


# =====================================================================
# Function App
# =====================================================================

app = func.FunctionApp()


@app.timer_trigger(
    schedule=_SCHEDULE,
    arg_name="timer",
    run_on_startup=False,    # NO disparar al iniciar el host (evita overlap en deploys)
    use_monitor=True,        # Persistir el último trigger en storage para evitar duplicados
)
def ib_poller_run(timer: func.TimerRequest) -> None:
    """Punto de entrada del Timer Trigger."""
    if timer.past_due:
        logger.warning("ib_poller: timer past_due (anterior tardó más de 10min); arrancando ahora")

    try:
        config = _get_config()
    except ConfigError:
        # El host de Functions reintenta automáticamente según host.json retry policy.
        raise

    processor = IBProcessor(config)
    try:
        results = processor.run_full_sync()
    except Exception as exc:
        # run_full_sync ya captura errores por sub-proceso. Si llegamos acá
        # algo catastrófico ocurrió (ej: no se pudo conectar a SQL).
        logger.exception("ib_poller: error catastrófico — %s", exc)
        raise

    summary = {
        label: {
            "read": stats.rows_read,
            "upserted": stats.rows_upserted,
            "duration_ms": stats.duration_ms,
        }
        for label, stats in results.items()
    }
    logger.info("ib_poller: ciclo completo. summary=%s", summary)
