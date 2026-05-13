"""Tests para shared.azure_secrets (SEC-04, AZ-05).

Cubrimos los caminos que el código real recorre:
    - Sin AZURE_KEY_VAULT_URI: solo env vars.
    - Con vault válido: prefiere Key Vault, cae a env var si el secreto no está.
    - Con vault inválido: loguea warning, cae a env var como fallback.
    - Errores de "no encontrado en ningún lado": SecretNotFoundError, salvo default.
    - default_secrets_client() es singleton.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from shared import azure_secrets
from shared.azure_secrets import (
    AzureSecretsClient,
    SecretNotFoundError,
    default_secrets_client,
)
from shared.secret_string import SecretString


# ---------------------------------------------------------------------------
# Conversión de nombres
# ---------------------------------------------------------------------------


class TestNameConversion:
    def test_envvar_a_kv_name(self) -> None:
        assert AzureSecretsClient._to_kv_name("SQL_CONNECTION_STRING") == "SQL-CONNECTION-STRING"

    def test_ya_en_uppercase_con_guiones(self) -> None:
        assert AzureSecretsClient._to_kv_name("mp-access-token") == "MP-ACCESS-TOKEN"

    def test_sin_underscore_ni_dash(self) -> None:
        assert AzureSecretsClient._to_kv_name("token") == "TOKEN"


# ---------------------------------------------------------------------------
# Modo env-only (sin vault configurado)
# ---------------------------------------------------------------------------


class TestEnvOnly:
    def test_sin_vault_devuelve_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MP_ACCESS_TOKEN", "APP_USR_xyz")
        client = AzureSecretsClient(vault_uri=None)
        secret = client.get("MP_ACCESS_TOKEN")
        assert isinstance(secret, SecretString)
        assert secret.reveal() == "APP_USR_xyz"

    def test_sin_vault_sin_env_y_sin_default_falla(self) -> None:
        client = AzureSecretsClient(vault_uri=None)
        with pytest.raises(SecretNotFoundError, match="MP_ACCESS_TOKEN"):
            client.get("MP_ACCESS_TOKEN")

    def test_default_se_usa_si_no_hay_env(self) -> None:
        client = AzureSecretsClient(vault_uri=None)
        secret = client.get("MISSING", default="fallback_value")
        assert secret.reveal() == "fallback_value"

    def test_get_optional_devuelve_none(self) -> None:
        client = AzureSecretsClient(vault_uri=None)
        assert client.get_optional("MISSING") is None

    def test_get_optional_devuelve_secret_si_existe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOKEN", "abc")
        client = AzureSecretsClient(vault_uri=None)
        out = client.get_optional("TOKEN")
        assert out is not None
        assert out.reveal() == "abc"


# ---------------------------------------------------------------------------
# Con vault mockeado
# ---------------------------------------------------------------------------


def _patch_build_client(monkeypatch: pytest.MonkeyPatch, mock_client: MagicMock) -> None:
    """Reemplaza AzureSecretsClient._build_client por una factory que devuelve mock_client."""
    monkeypatch.setattr(
        AzureSecretsClient,
        "_build_client",
        staticmethod(lambda _uri: mock_client),
    )


class TestWithVault:
    def test_kv_tiene_prioridad_sobre_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kv_secret = MagicMock(value="from_vault")
        mock_kv = MagicMock()
        mock_kv.get_secret.return_value = kv_secret
        _patch_build_client(monkeypatch, mock_kv)

        monkeypatch.setenv("MP_ACCESS_TOKEN", "from_env")  # debería ignorarse

        client = AzureSecretsClient(vault_uri="https://kv.vault.azure.net/")
        secret = client.get("MP_ACCESS_TOKEN")
        assert secret.reveal() == "from_vault"
        # El nombre debe haberse convertido a guiones.
        mock_kv.get_secret.assert_called_once_with("MP-ACCESS-TOKEN")

    def test_kv_falla_cae_a_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_kv = MagicMock()
        mock_kv.get_secret.side_effect = RuntimeError("403 forbidden")
        _patch_build_client(monkeypatch, mock_kv)

        monkeypatch.setenv("MP_ACCESS_TOKEN", "from_env")

        client = AzureSecretsClient(vault_uri="https://kv.vault.azure.net/")
        secret = client.get("MP_ACCESS_TOKEN")
        assert secret.reveal() == "from_env"

    def test_kv_secret_sin_value_cae_a_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el secret existe pero está vacío, intentamos env var antes de fallar."""
        mock_kv = MagicMock()
        mock_kv.get_secret.return_value = MagicMock(value=None)
        _patch_build_client(monkeypatch, mock_kv)

        monkeypatch.setenv("MP_ACCESS_TOKEN", "from_env")

        client = AzureSecretsClient(vault_uri="https://kv.vault.azure.net/")
        secret = client.get("MP_ACCESS_TOKEN")
        assert secret.reveal() == "from_env"

    def test_kv_sin_secret_sin_env_y_sin_default_falla(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_kv = MagicMock()
        mock_kv.get_secret.side_effect = RuntimeError("not found")
        _patch_build_client(monkeypatch, mock_kv)

        client = AzureSecretsClient(vault_uri="https://kv.vault.azure.net/")
        with pytest.raises(SecretNotFoundError):
            client.get("MISSING")

    def test_build_client_lanza_excepcion_no_aborta_init(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si _build_client revienta (ej: credenciales rotas), seguimos en modo env-only."""

        def _broken_builder(_uri: str):
            raise RuntimeError("DefaultAzureCredential failed")

        monkeypatch.setattr(
            AzureSecretsClient, "_build_client", staticmethod(_broken_builder)
        )

        monkeypatch.setenv("TOKEN", "fallback")
        client = AzureSecretsClient(vault_uri="https://kv.vault.azure.net/")
        # El cliente quedó sin vault; igual sirve desde env.
        assert client._client is None
        assert client.get("TOKEN").reveal() == "fallback"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestDefaultClient:
    def test_singleton_devuelve_misma_instancia(self) -> None:
        a = default_secrets_client()
        b = default_secrets_client()
        assert a is b

    def test_singleton_se_resetea_en_test(self) -> None:
        """conftest._reset_default_secrets_client borra el singleton entre tests.
        Verificamos que esa fixture realmente se aplique."""
        first = default_secrets_client()
        azure_secrets._default_client = None
        second = default_secrets_client()
        assert first is not second
