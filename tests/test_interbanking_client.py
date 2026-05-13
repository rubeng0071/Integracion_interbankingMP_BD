"""Tests para shared.interbanking_client.

Cubrimos lo que el refactor agrega: constructor con inyección explícita,
factory from_env() para retrocompatibilidad y normalizaciones que ya estaban
(strip de trailing slash en api_base_url).

NO testeamos los endpoints REST: requieren creds reales o un mock de toda la
API IB. El valor está en validar el contrato de construcción, no en
re-validar requests.
"""
from __future__ import annotations

import pytest

from shared.interbanking_client import InterbankingClient
from shared.secret_string import SecretString


# ---------------------------------------------------------------------------
# Constructor explícito
# ---------------------------------------------------------------------------


def _build(**overrides) -> InterbankingClient:
    """Helper: construye un cliente con defaults sensatos para tests."""
    base = dict(
        client_id="cid",
        client_secret=SecretString("csec"),
        service_url="https://callback.example.com",
        customer_id="12345678",
    )
    base.update(overrides)
    return InterbankingClient(**base)


class TestConstructor:
    def test_args_explicitos_se_aplican(self) -> None:
        c = _build(
            grant_type="password",
            username=SecretString("u"),
            password=SecretString("p"),
            scope="custom-scope",
            page_size=250,
            timeout=120,
        )
        assert c.client_id == "cid"
        assert c.client_secret.reveal() == "csec"
        assert c.service_url == "https://callback.example.com"
        assert c.customer_id == "12345678"
        assert c.grant_type == "password"
        assert c.username is not None and c.username.reveal() == "u"
        assert c.password is not None and c.password.reveal() == "p"
        assert c.scope == "custom-scope"
        assert c.page_size == 250
        assert c.timeout == 120

    def test_defaults(self) -> None:
        c = _build()
        assert c.grant_type == "client_credentials"
        assert c.username is None
        assert c.password is None
        assert c.scope == "info-financiera"
        assert c.page_size == 100
        assert c.timeout == 60
        assert c.token_url.startswith("https://auth.interbanking.com.ar")
        assert c.api_base_url == "https://api-gw.interbanking.com.ar/api/prod/v1"

    def test_api_base_url_strip_trailing_slash(self) -> None:
        c = _build(api_base_url="https://api.example.com/v1/")
        assert c.api_base_url == "https://api.example.com/v1"

    def test_client_secret_debe_ser_secret_string(self) -> None:
        """str crudo debe rechazarse: SEC-07 exige envoltura explícita."""
        with pytest.raises(TypeError, match="SecretString"):
            InterbankingClient(
                client_id="cid",
                client_secret="csec",  # type: ignore[arg-type]
                service_url="https://x",
                customer_id="1",
            )


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


class TestLazyPandasImport:
    """Verifica que pandas no se cargue al importar el cliente.

    El test se hace en un subproceso para no contaminar sys.modules: si pytest
    o un test previo ya importaron pandas (por ejemplo TestPandasInDataFrame),
    el assert in-process daría falso positivo.
    """

    def test_modulo_no_carga_pandas_al_importarse(self) -> None:
        import subprocess
        import sys

        script = (
            "import sys; "
            "assert 'pandas' not in sys.modules; "
            "from shared.interbanking_client import InterbankingClient; "
            "assert 'pandas' not in sys.modules, 'pandas se cargo al importar el cliente'; "
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "ok" in result.stdout


class TestFromEnv:
    def test_construye_desde_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IB_CLIENT_ID", "envcid")
        monkeypatch.setenv("IB_CLIENT_SECRET", "envcsec")
        monkeypatch.setenv("IB_SERVICE_URL", "https://env.example.com")
        monkeypatch.setenv("IB_CUSTOMER_ID", "99")
        c = InterbankingClient.from_env()
        assert c.client_id == "envcid"
        assert c.client_secret.reveal() == "envcsec"
        assert c.service_url == "https://env.example.com"
        assert c.customer_id == "99"

    def test_falla_si_falta_obligatoria(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() debe propagar KeyError; mantener fail-fast del comportamiento legacy."""
        monkeypatch.setenv("IB_CLIENT_ID", "x")
        # Sin IB_CLIENT_SECRET, IB_SERVICE_URL, IB_CUSTOMER_ID.
        with pytest.raises(KeyError):
            InterbankingClient.from_env()

    def test_username_password_opcionales(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IB_CLIENT_ID", "x")
        monkeypatch.setenv("IB_CLIENT_SECRET", "y")
        monkeypatch.setenv("IB_SERVICE_URL", "https://x")
        monkeypatch.setenv("IB_CUSTOMER_ID", "1")
        c = InterbankingClient.from_env()
        assert c.username is None
        assert c.password is None

    def test_username_password_se_envuelven_en_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IB_CLIENT_ID", "x")
        monkeypatch.setenv("IB_CLIENT_SECRET", "y")
        monkeypatch.setenv("IB_SERVICE_URL", "https://x")
        monkeypatch.setenv("IB_CUSTOMER_ID", "1")
        monkeypatch.setenv("IB_USERNAME", "-3|123|u")
        monkeypatch.setenv("IB_PASSWORD", "passw0rd")
        c = InterbankingClient.from_env()
        assert isinstance(c.username, SecretString)
        assert isinstance(c.password, SecretString)
        assert c.username.reveal() == "-3|123|u"
        assert c.password.reveal() == "passw0rd"
