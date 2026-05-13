"""CAL-11 — Configuración tipada con validación al inicio.

Reemplaza los `os.environ["..."]` dispersos en el código por una única clase
`AppConfig` que se construye una vez al arranque y reporta TODOS los problemas
de configuración en un único mensaje (en lugar de fallar en el primer KeyError).

Ventajas:
    - Fail-fast: si falta algo, lo sabés antes de hacer una sola request.
    - Mensaje único: el usuario ve toda la lista de vars faltantes, no una a una.
    - Secretos envueltos en SecretString automáticamente.
    - Tipado: el resto del código recibe tipos correctos (int, bool, etc.).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from .azure_secrets import AzureSecretsClient, SecretNotFoundError, default_secrets_client
from .secret_string import SecretString

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Configuración inválida o incompleta. Mensaje incluye todos los errores."""


# =====================================================================
# Helper interno: validador agregado.
#
# Centraliza la lectura de secretos/env vars + la acumulación de errores
# para que MULTIPLES dataclasses de configuración compartan el patrón
# fail-fast con reporte único. Antes vivía como closures dentro de
# AppConfig.from_env(); al externalizarlo, MpWebhookConfig e IbPollerConfig
# pueden reusarlo sin duplicar lógica.
# =====================================================================


class _Validator:
    """Acumula errores durante la construcción de una config y los reporta al final."""

    def __init__(self, secrets: AzureSecretsClient) -> None:
        self.secrets = secrets
        self.errors: List[str] = []

    def required_secret(self, name: str) -> Optional[SecretString]:
        try:
            return self.secrets.get(name)
        except SecretNotFoundError:
            self.errors.append(f"  - {name} (requerido, no encontrado en Key Vault ni en env)")
            return None

    def optional_secret(self, name: str) -> Optional[SecretString]:
        return self.secrets.get_optional(name)

    def required_str(self, name: str) -> Optional[str]:
        value = os.getenv(name)
        if not value:
            self.errors.append(f"  - {name} (variable de entorno requerida, vacía o ausente)")
            return None
        return value

    def int_env(self, name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            self.errors.append(f"  - {name} debe ser entero, recibido: '{raw}'")
            return default

    def raise_if_errors(self) -> None:
        if not self.errors:
            return
        header = "Configuración inválida. Variables faltantes o erróneas:\n"
        tip = (
            "\n\nRevisá unified_finance_sync.env.example para ver el formato esperado, "
            "o configurá Azure Key Vault con AZURE_KEY_VAULT_URI."
        )
        raise ConfigError(header + "\n".join(self.errors) + tip)


@dataclass
class AppConfig:
    """Configuración completa de la aplicación.

    Los campos `SecretString` provienen de Key Vault o de env vars (fallback).
    Los campos primitivos (int/str/bool) provienen siempre de env vars.
    """

    # === Secretos (Key Vault o env var) ===
    sql_connection_string: SecretString
    mp_access_token: SecretString
    ib_client_id: SecretString
    ib_client_secret: SecretString
    mp_webhook_secret: Optional[SecretString]  # AZ-02 — para validación HMAC del webhook MP
    ib_username: Optional[SecretString]
    ib_password: Optional[SecretString]

    # === Config no-secreta (env vars) ===
    ib_service_url: str
    ib_customer_id: str
    ib_grant_type: str = "client_credentials"
    ib_token_url: str = "https://auth.interbanking.com.ar/cas/oidc/accessToken"
    ib_api_base_url: str = "https://api-gw.interbanking.com.ar/api/prod/v1"
    ib_scope: str = "info-financiera"
    ib_page_size: int = 100
    ib_timeout_seconds: int = 60

    poll_interval_seconds: int = 600
    mp_initial_lookback_days: int = 365
    mp_incremental_lookback_hours: int = 72
    ib_incremental_lookback_days: int = 7

    log_level: str = "INFO"
    output_dir: str = "."  # CAL-08 — directorio para exports Excel

    # === Azure (opcionales, solo en deploy) ===
    azure_key_vault_uri: Optional[str] = None
    application_insights_connection_string: Optional[SecretString] = None

    # Errores acumulados durante la construcción (uso interno).
    _errors: List[str] = field(default_factory=list, repr=False)

    @classmethod
    def from_env(cls, secrets: Optional[AzureSecretsClient] = None) -> "AppConfig":
        """Construye la config validando TODO antes de devolver. Falla con un único error.

        Pensado para el monolítico legacy y para tests que necesitan la config
        completa. Para Functions individuales preferí MpWebhookConfig o
        IbPollerConfig, que validan solo lo que cada componente realmente usa.

        Args:
            secrets: Cliente de secretos. Si None, usa el singleton global.

        Raises:
            ConfigError: si falta cualquier variable requerida (con lista completa).
        """
        v = _Validator(secrets or default_secrets_client())

        sql_conn = v.required_secret("SQL_CONNECTION_STRING")
        mp_token = v.required_secret("MP_ACCESS_TOKEN")
        ib_id = v.required_secret("IB_CLIENT_ID")
        ib_secret = v.required_secret("IB_CLIENT_SECRET")
        ib_service_url = v.required_str("IB_SERVICE_URL")
        ib_customer_id = v.required_str("IB_CUSTOMER_ID")

        grant_type = os.getenv("IB_GRANT_TYPE", "client_credentials")
        if grant_type == "password":
            ib_username = v.required_secret("IB_USERNAME")
            ib_password = v.required_secret("IB_PASSWORD")
        else:
            ib_username = v.optional_secret("IB_USERNAME")
            ib_password = v.optional_secret("IB_PASSWORD")

        ib_page_size = v.int_env("IB_PAGE_SIZE", cls.ib_page_size)
        ib_timeout_seconds = v.int_env("IB_TIMEOUT_SECONDS", cls.ib_timeout_seconds)
        poll_interval_seconds = v.int_env("POLL_INTERVAL_SECONDS", cls.poll_interval_seconds)
        mp_initial_lookback_days = v.int_env("MP_INITIAL_LOOKBACK_DAYS", cls.mp_initial_lookback_days)
        mp_incremental_lookback_hours = v.int_env(
            "MP_INCREMENTAL_LOOKBACK_HOURS", cls.mp_incremental_lookback_hours
        )
        ib_incremental_lookback_days = v.int_env(
            "IB_INCREMENTAL_LOOKBACK_DAYS", cls.ib_incremental_lookback_days
        )

        v.raise_if_errors()

        return cls(
            sql_connection_string=sql_conn,            # type: ignore[arg-type]  validado arriba
            mp_access_token=mp_token,                  # type: ignore[arg-type]
            ib_client_id=ib_id,                        # type: ignore[arg-type]
            ib_client_secret=ib_secret,                # type: ignore[arg-type]
            mp_webhook_secret=v.optional_secret("MP_WEBHOOK_SECRET"),
            ib_username=ib_username,
            ib_password=ib_password,
            ib_service_url=ib_service_url,             # type: ignore[arg-type]
            ib_customer_id=ib_customer_id,             # type: ignore[arg-type]
            ib_grant_type=grant_type,
            ib_token_url=os.getenv("IB_TOKEN_URL", cls.ib_token_url),
            ib_api_base_url=os.getenv("IB_API_BASE_URL", cls.ib_api_base_url).rstrip("/"),
            ib_scope=os.getenv("IB_SCOPE", cls.ib_scope),
            ib_page_size=ib_page_size,
            ib_timeout_seconds=ib_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            mp_initial_lookback_days=mp_initial_lookback_days,
            mp_incremental_lookback_hours=mp_incremental_lookback_hours,
            ib_incremental_lookback_days=ib_incremental_lookback_days,
            log_level=os.getenv("LOG_LEVEL", cls.log_level),
            output_dir=os.getenv("OUTPUT_DIR", cls.output_dir),
            azure_key_vault_uri=os.getenv("AZURE_KEY_VAULT_URI"),
            application_insights_connection_string=v.optional_secret("APPLICATIONINSIGHTS_CONNECTION_STRING"),
        )
