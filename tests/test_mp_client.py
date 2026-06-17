"""Tests para mp_webhook_function/mp_client.py — OAuth2 client_credentials.

Lo que validamos:
    - Construcción: requiere (client_id, client_secret) o access_token_override.
    - OAuth flow: cachea token tras POST /oauth/token y lo reusa.
    - Refresh: cuando expira (o se invalida tras 401), llama de nuevo /oauth/token.
    - Override: si hay access_token_override, NO se llama /oauth/token nunca.
    - search_payments: arma params correctos y valida límites.

No usamos `requests-mock` para no agregar dependencia: parcheamos
`requests.post` (refresh) y `Session.get` (data endpoints) con monkeypatch.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from shared.secret_string import SecretString

# Importar mp_client desde mp_webhook_function/ (no está en shared/).
_FN_DIR = Path(__file__).resolve().parent.parent / "mp_webhook_function"
if str(_FN_DIR) not in sys.path:
    sys.path.insert(0, str(_FN_DIR))

import mp_client  # noqa: E402
from mp_client import (  # noqa: E402
    MercadoPagoAuthError,
    MercadoPagoClient,
    MercadoPagoError,
)


# =====================================================================
# Helpers
# =====================================================================


class _FakeResponse:
    """Stub de requests.Response suficiente para nuestros casos."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Optional[Dict[str, Any]] = None,
        raise_on_status: bool = True,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self._raise_on_status = raise_on_status

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self._raise_on_status and self.status_code >= 400:
            import requests
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self  # type: ignore[assignment]
            raise err


@pytest.fixture
def fake_oauth_response() -> _FakeResponse:
    return _FakeResponse(
        status_code=200,
        json_data={
            "access_token": "APP_USR-token-1",
            "token_type": "bearer",
            "expires_in": 21600,
            "scope": "offline_access read write",
            "user_id": 425625653,
        },
    )


# =====================================================================
# Construcción
# =====================================================================


class TestConstructor:
    def test_requiere_credenciales_u_override(self) -> None:
        with pytest.raises(ValueError, match="client_id"):
            MercadoPagoClient()

    def test_con_oauth_credentials_ok(self) -> None:
        c = MercadoPagoClient(
            client_id=SecretString("cid"),
            client_secret=SecretString("csec"),
        )
        assert c._cached_token is None  # No refresca hasta el primer request
        assert c._access_token_override is None

    def test_con_override_no_requiere_credenciales(self) -> None:
        c = MercadoPagoClient(access_token_override=SecretString("APP_USR-static"))
        assert c._access_token_override is not None

    def test_refresh_safety_factor_invalido(self) -> None:
        with pytest.raises(ValueError, match="refresh_safety_factor"):
            MercadoPagoClient(
                client_id=SecretString("a"),
                client_secret=SecretString("b"),
                refresh_safety_factor=1.5,
            )


# =====================================================================
# OAuth flow: cache + refresh
# =====================================================================


class TestOAuthFlow:
    def test_primer_request_dispara_oauth(
        self, monkeypatch: pytest.MonkeyPatch, fake_oauth_response: _FakeResponse
    ) -> None:
        post_mock = MagicMock(return_value=fake_oauth_response)
        monkeypatch.setattr(mp_client.requests, "post", post_mock)

        client = MercadoPagoClient(
            client_id=SecretString("cid"),
            client_secret=SecretString("csec"),
        )
        token = client._get_access_token()

        assert token == "APP_USR-token-1"
        assert post_mock.call_count == 1
        # Body con grant_type=client_credentials.
        kwargs = post_mock.call_args.kwargs
        assert kwargs["json"]["grant_type"] == "client_credentials"
        assert kwargs["json"]["client_id"] == "cid"
        assert kwargs["json"]["client_secret"] == "csec"

    def test_segundo_request_usa_cache(
        self, monkeypatch: pytest.MonkeyPatch, fake_oauth_response: _FakeResponse
    ) -> None:
        post_mock = MagicMock(return_value=fake_oauth_response)
        monkeypatch.setattr(mp_client.requests, "post", post_mock)

        client = MercadoPagoClient(
            client_id=SecretString("cid"),
            client_secret=SecretString("csec"),
        )
        t1 = client._get_access_token()
        t2 = client._get_access_token()
        t3 = client._get_access_token()

        assert t1 == t2 == t3 == "APP_USR-token-1"
        assert post_mock.call_count == 1  # Solo una llamada a /oauth/token

    def test_refresh_cuando_expira(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Forzamos expires_in muy chico para no esperar.
        first = _FakeResponse(200, {"access_token": "t1", "expires_in": 100})
        second = _FakeResponse(200, {"access_token": "t2", "expires_in": 100})
        post_mock = MagicMock(side_effect=[first, second])
        monkeypatch.setattr(mp_client.requests, "post", post_mock)

        client = MercadoPagoClient(
            client_id=SecretString("cid"),
            client_secret=SecretString("csec"),
            refresh_safety_factor=0.5,  # refresca al 50% (50s)
        )
        assert client._get_access_token() == "t1"

        # Simulamos el paso del tiempo monkey-patcheando time.monotonic.
        real_monotonic = mp_client.time.monotonic
        offset = 60.0  # > 50s del safety factor
        monkeypatch.setattr(mp_client.time, "monotonic", lambda: real_monotonic() + offset)

        assert client._get_access_token() == "t2"
        assert post_mock.call_count == 2

    def test_override_nunca_llama_oauth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        post_mock = MagicMock()
        monkeypatch.setattr(mp_client.requests, "post", post_mock)

        client = MercadoPagoClient(access_token_override=SecretString("APP_USR-fixed"))
        for _ in range(5):
            assert client._get_access_token() == "APP_USR-fixed"
        assert post_mock.call_count == 0

    def test_oauth_500_propaga_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = _FakeResponse(500, {"error": "internal"}, raise_on_status=False)
        monkeypatch.setattr(mp_client.requests, "post", MagicMock(return_value=bad))

        client = MercadoPagoClient(
            client_id=SecretString("cid"), client_secret=SecretString("csec")
        )
        with pytest.raises(MercadoPagoAuthError, match="500"):
            client._get_access_token()

    def test_oauth_sin_access_token_falla(self, monkeypatch: pytest.MonkeyPatch) -> None:
        weird = _FakeResponse(200, {"expires_in": 3600})  # falta access_token
        monkeypatch.setattr(mp_client.requests, "post", MagicMock(return_value=weird))

        client = MercadoPagoClient(
            client_id=SecretString("cid"), client_secret=SecretString("csec")
        )
        with pytest.raises(MercadoPagoAuthError, match="access_token"):
            client._get_access_token()

    def test_invalidate_token_fuerza_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _FakeResponse(200, {"access_token": "t1", "expires_in": 21600})
        second = _FakeResponse(200, {"access_token": "t2", "expires_in": 21600})
        post_mock = MagicMock(side_effect=[first, second])
        monkeypatch.setattr(mp_client.requests, "post", post_mock)

        client = MercadoPagoClient(
            client_id=SecretString("cid"), client_secret=SecretString("csec")
        )
        assert client._get_access_token() == "t1"
        client._invalidate_token()
        assert client._get_access_token() == "t2"
        assert post_mock.call_count == 2


# =====================================================================
# GET con manejo de 401
# =====================================================================


class TestGetWith401Retry:
    def _build_client(self, monkeypatch: pytest.MonkeyPatch, session_get: MagicMock) -> MercadoPagoClient:
        """Construye un cliente con token cacheado para skipear el primer OAuth."""
        oauth_resp = _FakeResponse(200, {"access_token": "t1", "expires_in": 21600})
        monkeypatch.setattr(mp_client.requests, "post", MagicMock(return_value=oauth_resp))
        client = MercadoPagoClient(
            client_id=SecretString("cid"), client_secret=SecretString("csec")
        )
        # Forzamos un refresh para tener token cacheado.
        client._get_access_token()
        # Parcheamos el get de la sesión.
        client.session.get = session_get  # type: ignore[assignment]
        return client

    def test_401_invalida_token_y_reintenta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Primer GET devuelve 401 (token expirado server-side). Segundo GET (con token nuevo) devuelve 200.
        unauthorized = _FakeResponse(401, {"error": "unauthorized"}, raise_on_status=False)
        ok = _FakeResponse(200, {"id": 123, "status": "approved"})
        session_get = MagicMock(side_effect=[unauthorized, ok])

        # Tras el invalidate, _get_access_token vuelve a postear /oauth/token.
        second_oauth = _FakeResponse(200, {"access_token": "t2", "expires_in": 21600})
        post_mock = MagicMock(side_effect=[
            _FakeResponse(200, {"access_token": "t1", "expires_in": 21600}),
            second_oauth,
        ])
        monkeypatch.setattr(mp_client.requests, "post", post_mock)

        client = MercadoPagoClient(
            client_id=SecretString("cid"), client_secret=SecretString("csec")
        )
        client._get_access_token()  # primer OAuth → t1
        client.session.get = session_get  # type: ignore[assignment]

        result = client._get("/v1/payments/123")
        assert result == {"id": 123, "status": "approved"}
        assert session_get.call_count == 2
        assert post_mock.call_count == 2  # OAuth original + refresh tras 401

    def test_401_con_override_no_reintenta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # raise_on_status=True: el cliente NO se reintenta (override no permite refresh)
        # y deja que raise_for_status escale a HTTPError → MercadoPagoError.
        unauthorized = _FakeResponse(401, {"error": "unauthorized"}, raise_on_status=True)
        session_get = MagicMock(return_value=unauthorized)

        client = MercadoPagoClient(access_token_override=SecretString("APP_USR-fixed"))
        client.session.get = session_get  # type: ignore[assignment]

        with pytest.raises(MercadoPagoError):
            client._get("/v1/payments/123")
        # Con override no hay refresh posible: una sola llamada.
        assert session_get.call_count == 1

    def test_get_propaga_error_de_red(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import requests as req
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise req.exceptions.ConnectionError("boom")
        session_get = MagicMock(side_effect=boom)
        client = self._build_client(monkeypatch, session_get)
        with pytest.raises(MercadoPagoError, match="ConnectionError"):
            client._get("/v1/payments/123")


# =====================================================================
# search_payments: params + validaciones
# =====================================================================


class TestSearchPayments:
    def _client_with_get(self, monkeypatch: pytest.MonkeyPatch, return_value: Dict[str, Any]) -> MercadoPagoClient:
        client = MercadoPagoClient(access_token_override=SecretString("APP_USR-fixed"))
        ok = _FakeResponse(200, return_value)
        get_mock = MagicMock(return_value=ok)
        client.session.get = get_mock  # type: ignore[assignment]
        client._search_get_mock = get_mock  # type: ignore[attr-defined]
        return client

    def test_limit_invalido(self) -> None:
        from datetime import datetime
        c = MercadoPagoClient(access_token_override=SecretString("t"))
        with pytest.raises(ValueError, match="limit"):
            c.search_payments(datetime(2026, 1, 1), datetime(2026, 1, 2), limit=51)
        with pytest.raises(ValueError, match="limit"):
            c.search_payments(datetime(2026, 1, 1), datetime(2026, 1, 2), limit=0)

    def test_offset_negativo(self) -> None:
        from datetime import datetime
        c = MercadoPagoClient(access_token_override=SecretString("t"))
        with pytest.raises(ValueError, match="offset"):
            c.search_payments(datetime(2026, 1, 1), datetime(2026, 1, 2), offset=-1)

    def test_params_armados_correctamente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import datetime
        client = self._client_with_get(monkeypatch, {"paging": {"total": 0}, "results": []})
        client.search_payments(
            begin_date=datetime(2026, 1, 1, 0, 0, 0),
            end_date=datetime(2026, 1, 2, 23, 59, 59),
            limit=50,
            offset=100,
            status="approved",
        )
        get_mock: MagicMock = client._search_get_mock  # type: ignore[attr-defined]
        params = get_mock.call_args.kwargs["params"]
        assert params["sort"] == "date_last_updated"
        assert params["criteria"] == "desc"
        assert params["range"] == "date_last_updated"
        assert params["limit"] == 50
        assert params["offset"] == 100
        assert params["status"] == "approved"
        assert params["begin_date"] == "2026-01-01T00:00:00.000Z"
        assert params["end_date"] == "2026-01-02T23:59:59.000Z"

    def test_status_opcional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from datetime import datetime
        client = self._client_with_get(monkeypatch, {"paging": {"total": 0}, "results": []})
        client.search_payments(datetime(2026, 1, 1), datetime(2026, 1, 2))
        get_mock: MagicMock = client._search_get_mock  # type: ignore[attr-defined]
        params = get_mock.call_args.kwargs["params"]
        assert "status" not in params

    def test_range_field_y_sort_custom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Para el backfill se busca por date_created ascendente."""
        from datetime import datetime
        client = self._client_with_get(monkeypatch, {"paging": {"total": 0}, "results": []})
        client.search_payments(
            begin_date=datetime(2026, 5, 1),
            end_date=datetime(2026, 6, 1),
            range_field="date_created",
            criteria="asc",
        )
        params = client._search_get_mock.call_args.kwargs["params"]  # type: ignore[attr-defined]
        assert params["range"] == "date_created"
        assert params["sort"] == "date_created"   # sort default = range_field
        assert params["criteria"] == "asc"


# =====================================================================
# iter_all_payments: slicing por fecha para esquivar el cap de offset 10k
# =====================================================================


class TestIterAllPayments:
    def _client(self) -> MercadoPagoClient:
        return MercadoPagoClient(access_token_override=SecretString("t"))

    @staticmethod
    def _fake_search(dataset: List[Dict[str, Any]]):
        """Fake de search_payments que sirve desde un dataset en memoria, honrando
        begin_date/end_date (inclusive), offset, limit y orden ascendente."""
        calls = {"n": 0}

        def fake(begin_date, end_date, limit=50, offset=0, status=None,
                 range_field="date_created", sort=None, criteria="asc", **_):
            calls["n"] += 1
            sel = [p for p in dataset if begin_date <= p["_dt"] <= end_date]
            sel.sort(key=lambda p: p["_dt"])
            page = sel[offset:offset + limit]
            return {
                "paging": {"total": len(sel), "limit": limit, "offset": offset},
                "results": [{"id": p["id"], "date_created": p["date_created"]} for p in page],
            }
        fake.calls = calls  # type: ignore[attr-defined]
        return fake

    @staticmethod
    def _dataset(n: int):
        from datetime import datetime, timedelta
        base = datetime(2026, 5, 1, 0, 0, 0)
        ds = []
        for i in range(n):
            dt = base + timedelta(seconds=i)
            ds.append({"id": i, "_dt": dt, "date_created": dt.isoformat()})
        return ds, base, base + timedelta(days=2)

    def test_una_ventana_sin_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ds, begin, end = self._dataset(120)
        client = self._client()
        client.search_payments = self._fake_search(ds)  # type: ignore[assignment]
        got = list(client.iter_all_payments(begin, end, range_field="date_created",
                                            page_size=50, page_delay_seconds=0))
        ids = [p["id"] for p in got]
        assert ids == list(range(120))            # todos, en orden
        assert len(set(ids)) == 120               # sin duplicados

    def test_slicing_supera_cap_de_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """25_000 pagos > cap de 10k: el iterador parte la ventana y los trae TODOS
        sin perder ni duplicar (dedup en los bordes de ventana)."""
        ds, begin, end = self._dataset(25_000)
        client = self._client()
        fake = self._fake_search(ds)
        client.search_payments = fake  # type: ignore[assignment]

        got = list(client.iter_all_payments(begin, end, range_field="date_created",
                                            page_size=50, page_delay_seconds=0))
        ids = [p["id"] for p in got]
        assert len(ids) == 25_000
        assert len(set(ids)) == 25_000           # sin duplicados pese a los bordes
        assert sorted(ids) == list(range(25_000))  # no se perdió ninguno
        # Tuvo que partir en >1 ventana (25k / 9.9k ≈ 3 ventanas → muchas páginas).
        assert fake.calls["n"] > 200  # type: ignore[attr-defined]

    def test_ventana_vacia_corta_limpio(self) -> None:
        client = self._client()
        client.search_payments = self._fake_search([])  # type: ignore[assignment]
        from datetime import datetime
        got = list(client.iter_all_payments(datetime(2026, 5, 1), datetime(2026, 5, 2),
                                            range_field="date_created", page_delay_seconds=0))
        assert got == []

    def test_corte_si_cursor_no_avanza(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si una ventana se llena pero todos los pagos comparten la MISMA fecha
        (cursor no puede avanzar), corta en vez de loopear infinito."""
        from datetime import datetime, timedelta
        same = datetime(2026, 5, 1, 12, 0, 0)
        # 12_000 pagos con la MISMA fecha: supera el cap y el cursor no avanza.
        ds = [{"id": i, "_dt": same, "date_created": same.isoformat()} for i in range(12_000)]
        client = self._client()
        client.search_payments = self._fake_search(ds)  # type: ignore[assignment]
        got = list(client.iter_all_payments(datetime(2026, 5, 1), datetime(2026, 5, 2),
                                            range_field="date_created", page_size=50,
                                            page_delay_seconds=0))
        # No loopea infinito: trae lo que pudo bajo el cap (~9900) y corta.
        assert 0 < len(got) <= 10_000
