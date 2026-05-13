"""Tests para shared.config (CAL-11).

El punto principal: `AppConfig.from_env()` debe acumular TODOS los errores
y reportarlos juntos. Antes el código fallaba en el primer KeyError y había
que correrlo N veces para descubrir N variables faltantes.
"""
from __future__ import annotations

import pytest

from shared.azure_secrets import AzureSecretsClient
from shared.config import AppConfig, ConfigError
from shared.secret_string import SecretString


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sete las env vars mínimas requeridas para que from_env() pase."""
    monkeypatch.setenv("SQL_CONNECTION_STRING", "Driver=...;Server=test;")
    monkeypatch.setenv("MP_ACCESS_TOKEN", "APP_USR_xyz")
    monkeypatch.setenv("IB_CLIENT_ID", "client_xyz")
    monkeypatch.setenv("IB_CLIENT_SECRET", "secret_xyz")
    monkeypatch.setenv("IB_SERVICE_URL", "https://example.com/callback")
    monkeypatch.setenv("IB_CUSTOMER_ID", "12345678")


@pytest.fixture
def secrets_env() -> AzureSecretsClient:
    """Cliente de secretos en modo env-only (sin vault)."""
    return AzureSecretsClient(vault_uri=None)


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_minimal_env_construye_config(
        self, minimal_env: None, secrets_env: AzureSecretsClient
    ) -> None:
        cfg = AppConfig.from_env(secrets=secrets_env)
        assert isinstance(cfg.sql_connection_string, SecretString)
        assert cfg.sql_connection_string.reveal() == "Driver=...;Server=test;"
        assert cfg.mp_access_token.reveal() == "APP_USR_xyz"
        assert cfg.ib_client_id.reveal() == "client_xyz"
        assert cfg.ib_client_secret.reveal() == "secret_xyz"
        assert cfg.ib_service_url == "https://example.com/callback"
        assert cfg.ib_customer_id == "12345678"

    def test_defaults_se_aplican(
        self, minimal_env: None, secrets_env: AzureSecretsClient
    ) -> None:
        cfg = AppConfig.from_env(secrets=secrets_env)
        assert cfg.ib_grant_type == "client_credentials"
        assert cfg.ib_scope == "info-financiera"
        assert cfg.ib_page_size == 100
        assert cfg.ib_timeout_seconds == 60
        assert cfg.poll_interval_seconds == 600
        assert cfg.log_level == "INFO"
        assert cfg.output_dir == "."
        # Secrets opcionales no presentes -> None.
        assert cfg.mp_webhook_secret is None
        assert cfg.azure_key_vault_uri is None
        assert cfg.application_insights_connection_string is None

    def test_optionales_se_cargan(
        self,
        minimal_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MP_WEBHOOK_SECRET", "wh_secret_xxx")
        monkeypatch.setenv("AZURE_KEY_VAULT_URI", "https://kv.vault.azure.net/")
        cfg = AppConfig.from_env(secrets=secrets_env)
        assert cfg.mp_webhook_secret is not None
        assert cfg.mp_webhook_secret.reveal() == "wh_secret_xxx"
        assert cfg.azure_key_vault_uri == "https://kv.vault.azure.net/"


# ---------------------------------------------------------------------------
# Reporte agregado de errores
# ---------------------------------------------------------------------------


class TestAggregateErrors:
    def test_falta_todo_lista_todo(self, secrets_env: AzureSecretsClient) -> None:
        with pytest.raises(ConfigError) as exc_info:
            AppConfig.from_env(secrets=secrets_env)
        msg = str(exc_info.value)
        # Todas las requeridas deben aparecer en el único mensaje.
        for var in (
            "SQL_CONNECTION_STRING",
            "MP_ACCESS_TOKEN",
            "IB_CLIENT_ID",
            "IB_CLIENT_SECRET",
            "IB_SERVICE_URL",
            "IB_CUSTOMER_ID",
        ):
            assert var in msg, f"falta {var} en el mensaje agregado"

    def test_solo_falta_una(
        self,
        minimal_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MP_ACCESS_TOKEN")
        with pytest.raises(ConfigError) as exc_info:
            AppConfig.from_env(secrets=secrets_env)
        msg = str(exc_info.value)
        assert "MP_ACCESS_TOKEN" in msg
        # No deberían aparecer las que sí están.
        assert "SQL_CONNECTION_STRING" not in msg
        assert "IB_CLIENT_ID" not in msg


# ---------------------------------------------------------------------------
# grant_type=password
# ---------------------------------------------------------------------------


class TestGrantTypePassword:
    def test_password_sin_credenciales_falla(
        self,
        minimal_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_GRANT_TYPE", "password")
        with pytest.raises(ConfigError) as exc_info:
            AppConfig.from_env(secrets=secrets_env)
        msg = str(exc_info.value)
        assert "IB_USERNAME" in msg
        assert "IB_PASSWORD" in msg

    def test_password_con_credenciales_ok(
        self,
        minimal_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_GRANT_TYPE", "password")
        monkeypatch.setenv("IB_USERNAME", "-3|123456|user")
        monkeypatch.setenv("IB_PASSWORD", "passw0rd")
        cfg = AppConfig.from_env(secrets=secrets_env)
        assert cfg.ib_grant_type == "password"
        assert cfg.ib_username is not None
        assert cfg.ib_username.reveal() == "-3|123456|user"
        assert cfg.ib_password is not None
        assert cfg.ib_password.reveal() == "passw0rd"

    def test_client_credentials_sin_user_y_pass_ok(
        self, minimal_env: None, secrets_env: AzureSecretsClient
    ) -> None:
        """En el flujo default, IB_USERNAME/IB_PASSWORD son opcionales."""
        cfg = AppConfig.from_env(secrets=secrets_env)
        assert cfg.ib_username is None
        assert cfg.ib_password is None


# ---------------------------------------------------------------------------
# Parsing de enteros
# ---------------------------------------------------------------------------


class TestIntParsing:
    def test_int_valido_se_parsea(
        self,
        minimal_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_PAGE_SIZE", "250")
        monkeypatch.setenv("POLL_INTERVAL_SECONDS", "300")
        cfg = AppConfig.from_env(secrets=secrets_env)
        assert cfg.ib_page_size == 250
        assert cfg.poll_interval_seconds == 300

    def test_int_invalido_reporta_error(
        self,
        minimal_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_PAGE_SIZE", "no_es_int")
        with pytest.raises(ConfigError) as exc_info:
            AppConfig.from_env(secrets=secrets_env)
        msg = str(exc_info.value)
        assert "IB_PAGE_SIZE" in msg
        assert "no_es_int" in msg

    def test_int_vacio_aplica_default(
        self,
        minimal_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_PAGE_SIZE", "")
        cfg = AppConfig.from_env(secrets=secrets_env)
        assert cfg.ib_page_size == 100  # default


# ---------------------------------------------------------------------------
# Normalizaciones específicas
# ---------------------------------------------------------------------------


class TestNormalizations:
    def test_ib_api_base_url_strip_trailing_slash(
        self,
        minimal_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_API_BASE_URL", "https://api.example.com/v1/")
        cfg = AppConfig.from_env(secrets=secrets_env)
        assert cfg.ib_api_base_url == "https://api.example.com/v1"

    def test_config_no_filtra_secretos_en_repr(
        self, minimal_env: None, secrets_env: AzureSecretsClient
    ) -> None:
        """Defensa en profundidad: el repr de AppConfig no debe contener secretos."""
        cfg = AppConfig.from_env(secrets=secrets_env)
        rendered = repr(cfg)
        assert "APP_USR_xyz" not in rendered
        assert "secret_xyz" not in rendered
        # El placeholder debe aparecer en su lugar.
        assert SecretString.PLACEHOLDER in rendered
