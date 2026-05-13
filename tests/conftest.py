"""Configuración común de pytest para los tests del paquete shared/.

Sirve para dos cosas:
    1. Garantizar que el repo root está en sys.path aunque shared/ no esté
       instalado en modo editable. Esto permite correr `pytest` sin
       `pip install -e .` previo.
    2. Limpiar variables de entorno entre tests para evitar contaminación
       cruzada (AppConfig.from_env() y AzureSecretsClient leen de os.environ).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


_RELEVANT_ENV_VARS = (
    "AZURE_KEY_VAULT_URI",
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
    "SQL_CONNECTION_STRING",
    "MP_ACCESS_TOKEN",
    "MP_WEBHOOK_SECRET",
    "IB_CLIENT_ID",
    "IB_CLIENT_SECRET",
    "IB_USERNAME",
    "IB_PASSWORD",
    "IB_SERVICE_URL",
    "IB_CUSTOMER_ID",
    "IB_GRANT_TYPE",
    "IB_TOKEN_URL",
    "IB_API_BASE_URL",
    "IB_SCOPE",
    "IB_PAGE_SIZE",
    "IB_TIMEOUT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "MP_INITIAL_LOOKBACK_DAYS",
    "MP_INCREMENTAL_LOOKBACK_HOURS",
    "IB_INCREMENTAL_LOOKBACK_DAYS",
    "LOG_LEVEL",
    "OUTPUT_DIR",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Borra del entorno toda var que pueda interferir con la config bajo test."""
    for name in _RELEVANT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _reset_default_secrets_client() -> None:
    """Resetea el singleton de AzureSecretsClient para que cada test arranque limpio."""
    from shared import azure_secrets

    azure_secrets._default_client = None
    yield
    azure_secrets._default_client = None
