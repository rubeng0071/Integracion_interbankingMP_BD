"""Tests para los configs especializados MpWebhookConfig e IbPollerConfig.

El objetivo de los configs modulares: cada Function valida SOLO lo que usa.
mp_webhook_function no debe necesitar IB_CLIENT_ID; ib_poller no debe
necesitar MP_ACCESS_TOKEN. Y mp_webhook_secret se vuelve OBLIGATORIO en
MpWebhookConfig (sin él, no podemos validar HMAC).
"""
from __future__ import annotations

import pytest

from shared.azure_secrets import AzureSecretsClient
from shared.config import ConfigError, IbPollerConfig, MpWebhookConfig
from shared.secret_string import SecretString


@pytest.fixture
def secrets_env() -> AzureSecretsClient:
    return AzureSecretsClient(vault_uri=None)


# =====================================================================
# MpWebhookConfig
# =====================================================================


class TestMpWebhookConfig:
    def test_minimal_env_construye_config(
        self, secrets_env: AzureSecretsClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQL_CONNECTION_STRING", "Driver=...;")
        monkeypatch.setenv("MP_ACCESS_TOKEN", "APP_USR_xyz")
        monkeypatch.setenv("MP_WEBHOOK_SECRET", "wh_secret")

        cfg = MpWebhookConfig.from_env(secrets=secrets_env)
        assert cfg.sql_connection_string.reveal() == "Driver=...;"
        assert cfg.mp_access_token.reveal() == "APP_USR_xyz"
        assert cfg.mp_webhook_secret.reveal() == "wh_secret"
        assert cfg.log_level == "INFO"

    def test_no_requiere_credenciales_ib(
        self, secrets_env: AzureSecretsClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin IB_* en el entorno, MpWebhookConfig.from_env() funciona."""
        monkeypatch.setenv("SQL_CONNECTION_STRING", "X")
        monkeypatch.setenv("MP_ACCESS_TOKEN", "Y")
        monkeypatch.setenv("MP_WEBHOOK_SECRET", "Z")
        # IB_* deliberadamente ausentes.
        cfg = MpWebhookConfig.from_env(secrets=secrets_env)
        assert isinstance(cfg, MpWebhookConfig)

    def test_webhook_secret_es_obligatorio(
        self, secrets_env: AzureSecretsClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQL_CONNECTION_STRING", "X")
        monkeypatch.setenv("MP_ACCESS_TOKEN", "Y")
        # MP_WEBHOOK_SECRET ausente: debe fallar.
        with pytest.raises(ConfigError, match="MP_WEBHOOK_SECRET"):
            MpWebhookConfig.from_env(secrets=secrets_env)

    def test_reporta_todas_las_faltantes_juntas(
        self, secrets_env: AzureSecretsClient
    ) -> None:
        with pytest.raises(ConfigError) as exc_info:
            MpWebhookConfig.from_env(secrets=secrets_env)
        msg = str(exc_info.value)
        for var in ("SQL_CONNECTION_STRING", "MP_ACCESS_TOKEN", "MP_WEBHOOK_SECRET"):
            assert var in msg

    def test_repr_no_filtra_secretos(
        self, secrets_env: AzureSecretsClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SQL_CONNECTION_STRING", "supersecret_conn")
        monkeypatch.setenv("MP_ACCESS_TOKEN", "APP_USR_xyz_real")
        monkeypatch.setenv("MP_WEBHOOK_SECRET", "wh_real_secret")
        cfg = MpWebhookConfig.from_env(secrets=secrets_env)
        rendered = repr(cfg)
        assert "supersecret_conn" not in rendered
        assert "APP_USR_xyz_real" not in rendered
        assert "wh_real_secret" not in rendered
        assert SecretString.PLACEHOLDER in rendered


# =====================================================================
# IbPollerConfig
# =====================================================================


@pytest.fixture
def minimal_ib_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQL_CONNECTION_STRING", "Driver=...;")
    monkeypatch.setenv("IB_CLIENT_ID", "client_xyz")
    monkeypatch.setenv("IB_CLIENT_SECRET", "secret_xyz")
    monkeypatch.setenv("IB_SERVICE_URL", "https://example.com/callback")
    monkeypatch.setenv("IB_CUSTOMER_ID", "12345678")


class TestIbPollerConfig:
    def test_minimal_env_construye_config(
        self, minimal_ib_env: None, secrets_env: AzureSecretsClient
    ) -> None:
        cfg = IbPollerConfig.from_env(secrets=secrets_env)
        assert cfg.ib_client_id.reveal() == "client_xyz"
        assert cfg.ib_grant_type == "client_credentials"
        assert cfg.ib_page_size == 100
        assert cfg.ib_incremental_lookback_days == 7

    def test_no_requiere_credenciales_mp(
        self, minimal_ib_env: None, secrets_env: AzureSecretsClient
    ) -> None:
        """Sin MP_ACCESS_TOKEN ni MP_WEBHOOK_SECRET, IbPollerConfig funciona."""
        cfg = IbPollerConfig.from_env(secrets=secrets_env)
        assert isinstance(cfg, IbPollerConfig)
        assert not hasattr(cfg, "mp_access_token")

    def test_password_flow_obliga_credenciales(
        self,
        minimal_ib_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_GRANT_TYPE", "password")
        with pytest.raises(ConfigError) as exc_info:
            IbPollerConfig.from_env(secrets=secrets_env)
        msg = str(exc_info.value)
        assert "IB_USERNAME" in msg
        assert "IB_PASSWORD" in msg

    def test_password_flow_con_credenciales_ok(
        self,
        minimal_ib_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_GRANT_TYPE", "password")
        monkeypatch.setenv("IB_USERNAME", "-3|123|u")
        monkeypatch.setenv("IB_PASSWORD", "p")
        cfg = IbPollerConfig.from_env(secrets=secrets_env)
        assert cfg.ib_grant_type == "password"
        assert cfg.ib_username is not None and cfg.ib_username.reveal() == "-3|123|u"
        assert cfg.ib_password is not None and cfg.ib_password.reveal() == "p"

    def test_int_invalido_reporta_error(
        self,
        minimal_ib_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_PAGE_SIZE", "abc")
        with pytest.raises(ConfigError, match="IB_PAGE_SIZE"):
            IbPollerConfig.from_env(secrets=secrets_env)

    def test_api_base_url_strip_trailing_slash(
        self,
        minimal_ib_env: None,
        secrets_env: AzureSecretsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("IB_API_BASE_URL", "https://api.example.com/v1/")
        cfg = IbPollerConfig.from_env(secrets=secrets_env)
        assert cfg.ib_api_base_url == "https://api.example.com/v1"

    def test_reporta_todas_las_faltantes_juntas(
        self, secrets_env: AzureSecretsClient
    ) -> None:
        with pytest.raises(ConfigError) as exc_info:
            IbPollerConfig.from_env(secrets=secrets_env)
        msg = str(exc_info.value)
        for var in (
            "SQL_CONNECTION_STRING",
            "IB_CLIENT_ID",
            "IB_CLIENT_SECRET",
            "IB_SERVICE_URL",
            "IB_CUSTOMER_ID",
        ):
            assert var in msg
