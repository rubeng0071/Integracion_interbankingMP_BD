"""OPS-01 + OPS-02 — Bootstrap de logging estructurado + Application Insights.

Diseño:
    1. Cada Function App llama a configure_logging() al arrancar.
    2. Se instala un formatter JSON (vía python-json-logger) en el root logger.
       Todas las llamadas existentes a logger.info/warning/exception/etc. heredan
       el formato sin cambios en el código.
    3. Si hay APPLICATIONINSIGHTS_CONNECTION_STRING en la config, agrega un
       AzureLogHandler para que los logs se ingieran en AppInsights con
       custom_dimensions (campo "service" para filtrar por Function App).

    Si las librerías opcionales (python-json-logger, opencensus-ext-azure) no
    están instaladas, configure_logging cae a logging plano y avisa con un
    warning. La Function NUNCA debe abortar por falta de observability.

Por qué opencensus y no azure-monitor-opentelemetry:
    Migrar a OT es la mejora futura recomendada (Microsoft lo está deprecando
    a opencensus). Por ahora usamos lo que ya está en requirements de ambas
    Functions y dejamos el cambio para una iteración posterior.

Uso:
    from shared.observability import configure_logging
    configure_logging(
        connection_string=config.application_insights_connection_string,
        service_name="mp_webhook",
        level=config.log_level,
    )
"""
from __future__ import annotations

import logging
from typing import Optional

from .secret_string import SecretString, reveal as _reveal


# Idempotencia: configure_logging no debe agregar handlers duplicados si se
# llama más de una vez (puede pasar en invocaciones warm de Azure Functions
# si el module scope se evalúa otra vez por algún path raro).
_configured: bool = False


def configure_logging(
    connection_string: Optional[SecretString] = None,
    level: str = "INFO",
    service_name: str = "",
) -> None:
    """Configura el root logger con JSON formatter y opcional AppInsights handler.

    Args:
        connection_string: SecretString con el APPLICATIONINSIGHTS_CONNECTION_STRING.
            Si es None o vacío, no se exporta a AppInsights (logs solo a stdout).
        level: Nivel del root logger ("DEBUG", "INFO", "WARNING", ...).
        service_name: Tag que se incluye como custom_dimension en cada log.
            Permite distinguir entre "mp_webhook" e "ib_poller" en una sola
            instancia de AppInsights compartida.
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Removemos cualquier handler default agregado por basicConfig() o por el
    # host de Functions; instalamos los nuestros para tener formato consistente.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = _build_formatter()

    # Stdout handler (siempre): el host de Functions lo captura y lo manda al
    # stream "Console" de AppInsights con sampling default.
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    # AppInsights handler (opcional): nos da control fino de severity, custom
    # dimensions y trace correlation, en vez de depender del host.
    conn = _reveal(connection_string) if connection_string else None
    if conn:
        ai = _build_appinsights_handler(conn, formatter, service_name)
        if ai is not None:
            root.addHandler(ai)

    _configured = True
    logging.getLogger(__name__).info(
        "observability.configure_logging: level=%s service=%s appinsights=%s",
        level, service_name or "(none)", "on" if conn else "off",
    )


def _build_formatter() -> logging.Formatter:
    """JSON formatter si python-json-logger está; plain si no."""
    try:
        from pythonjsonlogger import jsonlogger  # type: ignore[import-untyped]
    except ImportError:
        return logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # rename_fields traduce a nombres que KQL espera de Application Insights.
    return jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        json_default=str,
        rename_fields={"asctime": "timestamp", "levelname": "severity"},
    )


def _build_appinsights_handler(
    connection_string: str,
    formatter: logging.Formatter,
    service_name: str,
) -> Optional[logging.Handler]:
    """AzureLogHandler con un filter que inyecta custom_dimensions."""
    try:
        from opencensus.ext.azure.log_exporter import AzureLogHandler  # type: ignore[import-untyped]
    except ImportError:
        logging.getLogger(__name__).warning(
            "opencensus-ext-azure no instalado; logs solo a stdout"
        )
        return None
    try:
        handler = AzureLogHandler(connection_string=connection_string)
    except Exception as exc:
        # Una falla acá (conn string malformado, etc.) NO debe abortar la
        # Function. Mejor seguir con stdout logging y avisar.
        logging.getLogger(__name__).warning(
            "AzureLogHandler init falló (%s); logs solo a stdout",
            type(exc).__name__,
        )
        return None
    handler.setFormatter(formatter)
    handler.addFilter(_ServiceDimensionFilter(service_name or "unknown"))
    return handler


class _ServiceDimensionFilter(logging.Filter):
    """Inyecta record.custom_dimensions["service"]. AppInsights lo expone como customDimensions.service."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        existing = getattr(record, "custom_dimensions", None) or {}
        if not isinstance(existing, dict):
            existing = {}
        existing.setdefault("service", self.service)
        record.custom_dimensions = existing  # type: ignore[attr-defined]
        return True


# Reset utility para tests; no es API pública.
def _reset_for_tests() -> None:
    global _configured
    _configured = False
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
