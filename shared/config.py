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

        Args:
            secrets: Cliente de secretos. Si None, usa el singleton global.

        Raises:
            ConfigError: si falta cualquier variable requerida (con lista completa).
        """
        secrets = secrets or default_secrets_client()
        errors: List[str] = []

        def _required_secret(name: str) -> Optional[SecretString]:
            try:
                return secrets.get(name)
            except SecretNotFoundError:
                errors.append(f"  - {name} (requerido, no encontrado en Key Vault ni en env)")
                return None

        def _optional_secret(name: str) -> Optional[SecretString]:
            return secrets.get_optional(name)

        def _required_str(name: str) -> Optional[str]:
            value = os.getenv(name)
            if not value:
                errors.append(f"  - {name} (variable de entorno requerida, vacía o ausente)")
                return None
            return value

        def _int_env(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                errors.append(f"  - {name} debe ser entero, recibido: '{raw}'")
                return default

        sql_conn = _required_secret("SQL_CONNECTION_STRING")
        mp_token = _required_secret("MP_ACCESS_TOKEN")
        ib_id = _required_secret("IB_CLIENT_ID")
        ib_secret = _required_secret("IB_CLIENT_SECRET")
        ib_service_url = _required_str("IB_SERVICE_URL")
        ib_customer_id = _required_str("IB_CUSTOMER_ID")

        # Validación condicional: si grant_type=password, username/password son obligatorios.
        grant_type = os.getenv("IB_GRANT_TYPE", "client_credentials")
        ib_username: Optional[SecretString] = None
        ib_password: Optional[SecretString] = None
        if grant_type == "password":
            ib_username = _required_secret("IB_USERNAME")
            ib_password = _required_secret("IB_PASSWORD")
        else:
            ib_username = _optional_secret("IB_USERNAME")
            ib_password = _optional_secret("IB_PASSWORD")

        # Parseo de enteros ANTES del check de errores: _int_env appendea a
        # `errors` cuando el valor no es entero, así que tienen que evaluarse
        # antes de la condición que dispara ConfigError. (Antes vivían dentro
        # del `return cls(...)`, lo que dejaba los errores silenciosamente
        # descartados y la config caía al default sin avisar.)
        ib_page_size = _int_env("IB_PAGE_SIZE", cls.ib_page_size)
        ib_timeout_seconds = _int_env("IB_TIMEOUT_SECONDS", cls.ib_timeout_seconds)
        poll_interval_seconds = _int_env("POLL_INTERVAL_SECONDS", cls.poll_interval_seconds)
        mp_initial_lookback_days = _int_env("MP_INITIAL_LOOKBACK_DAYS", cls.mp_initial_lookback_days)
        mp_incremental_lookback_hours = _int_env(
            "MP_INCREMENTAL_LOOKBACK_HOURS", cls.mp_incremental_lookback_hours
        )
        ib_incremental_lookback_days = _int_env(
            "IB_INCREMENTAL_LOOKBACK_DAYS", cls.ib_incremental_lookback_days
        )

        if errors:
            header = "Configuración inválida. Variables faltantes o erróneas:\n"
            tip = (
                "\n\nRevisá unified_finance_sync.env.example para ver el formato esperado, "
                "o configurá Azure Key Vault con AZURE_KEY_VAULT_URI."
            )
            raise ConfigError(header + "\n".join(errors) + tip)

        return cls(
            sql_connection_string=sql_conn,            # type: ignore[arg-type]  validado arriba
            mp_access_token=mp_token,                  # type: ignore[arg-type]
            ib_client_id=ib_id,                        # type: ignore[arg-type]
            ib_client_secret=ib_secret,                # type: ignore[arg-type]
            mp_webhook_secret=_optional_secret("MP_WEBHOOK_SECRET"),
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
            application_insights_connection_string=_optional_secret("APPLICATIONINSIGHTS_CONNECTION_STRING"),
        )
