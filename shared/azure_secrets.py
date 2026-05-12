"""SEC-04 — Wrapper de Azure Key Vault con fallback a variables de entorno.

Comportamiento:
    1. Si la env var `AZURE_KEY_VAULT_URI` está definida, intenta resolver secretos
       contra el Key Vault usando `DefaultAzureCredential` (Managed Identity en Azure,
       Azure CLI / VS Code en dev local).
    2. Si Key Vault falla o no está configurado, lee la variable de entorno con el
       mismo nombre del secreto (uppercase, guiones medios reemplazados por `_`).

Esto permite:
    - En Azure: nada de secretos en variables de entorno; todo en Key Vault.
    - En dev local: seguir usando .env hasta que se configure el Vault.
    - Mismo código en ambos entornos.

Convención de nombres de secreto (Key Vault):
    Key Vault NO permite `_` en nombres de secreto, sí permite `-`.
    Por eso convertimos:
        env var:        SQL_CONNECTION_STRING
        Key Vault key:  SQL-CONNECTION-STRING

Uso:
    >>> secrets = AzureSecretsClient()
    >>> sql_conn = secrets.get("SQL_CONNECTION_STRING")     # SecretString
    >>> mp_token = secrets.get("MP_ACCESS_TOKEN")           # SecretString
    >>> # Para revelar:
    >>> connection = sql_conn.reveal()
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .secret_string import SecretString

logger = logging.getLogger(__name__)


class SecretNotFoundError(RuntimeError):
    """El secreto no existe ni en Key Vault ni en variables de entorno."""


class AzureSecretsClient:
    """Cliente unificado de secretos: Key Vault con fallback a env vars."""

    def __init__(self, vault_uri: Optional[str] = None) -> None:
        self.vault_uri = vault_uri or os.getenv("AZURE_KEY_VAULT_URI")
        self._client = None
        if self.vault_uri:
            try:
                self._client = self._build_client(self.vault_uri)
                logger.info("Azure Key Vault habilitado: %s", self.vault_uri)
            except Exception as exc:
                # No abortamos: caemos a env vars. Loguear pero sin secretos.
                logger.warning(
                    "No se pudo inicializar Key Vault (%s); usando variables de entorno como fallback",
                    type(exc).__name__,
                )
                self._client = None

    @staticmethod
    def _build_client(vault_uri: str):
        # Import perezoso: en local sin Azure no queremos forzar la dependencia.
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as exc:
            raise RuntimeError(
                "Faltan dependencias Azure. Instala con: "
                "pip install azure-identity azure-keyvault-secrets"
            ) from exc

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
        return SecretClient(vault_url=vault_uri, credential=credential)

    @staticmethod
    def _to_kv_name(name: str) -> str:
        """Convierte SQL_CONNECTION_STRING -> SQL-CONNECTION-STRING."""
        return name.replace("_", "-").upper()

    def get(self, name: str, default: Optional[str] = None) -> SecretString:
        """Obtiene un secreto. Prioriza Key Vault; cae a env var; usa default si nada.

        Args:
            name: Nombre canónico (ej: "SQL_CONNECTION_STRING").
            default: Valor a usar si no se encuentra en ningún lado. None = error.

        Returns:
            SecretString envolviendo el valor.

        Raises:
            SecretNotFoundError: si no se encuentra y no hay default.
        """
        # 1. Key Vault.
        if self._client is not None:
            kv_name = self._to_kv_name(name)
            try:
                kv_secret = self._client.get_secret(kv_name)
                if kv_secret and kv_secret.value:
                    return SecretString(kv_secret.value)
            except Exception as exc:
                # Loguear sin exponer ni el nombre completo del secreto en errores HTTP.
                logger.debug(
                    "Key Vault sin secreto '%s' (%s); intentando env var",
                    kv_name, type(exc).__name__,
                )

        # 2. Variable de entorno.
        env_value = os.getenv(name)
        if env_value:
            return SecretString(env_value)

        # 3. Default explícito.
        if default is not None:
            return SecretString(default)

        raise SecretNotFoundError(
            f"Secreto '{name}' no encontrado en Key Vault ni en variables de entorno"
        )

    def get_optional(self, name: str) -> Optional[SecretString]:
        """Como get() pero devuelve None en vez de levantar excepción."""
        try:
            return self.get(name)
        except SecretNotFoundError:
            return None


# Instancia global perezosa para no inicializar Key Vault en cada import.
_default_client: Optional[AzureSecretsClient] = None


def default_secrets_client() -> AzureSecretsClient:
    """Singleton del cliente. Útil para no instanciar múltiples veces."""
    global _default_client
    if _default_client is None:
        _default_client = AzureSecretsClient()
    return _default_client
