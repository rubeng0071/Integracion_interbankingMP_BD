"""Tests para la lógica de paginación del poller MP.

Probamos `collect_payment_ids` y `_poller_window` en aislamiento (sin Azure Functions
runtime, sin queue real). El timer trigger en sí es un wrapper fino que delega
en estas funciones; testear esas dos cubre el corazón de la lógica.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

# Importar function_app desde mp_webhook_function/.
_FN_DIR = Path(__file__).resolve().parent.parent / "mp_webhook_function"
if str(_FN_DIR) not in sys.path:
    sys.path.insert(0, str(_FN_DIR))


@pytest.fixture(autouse=True)
def _stub_azure_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub mínimo de azure.functions para poder importar function_app sin el SDK real.

    El SDK no está instalado en el venv del repo (vive solo dentro de las Functions).
    En tests solo nos interesan funciones puras del módulo, así que armamos un
    stub con el shape mínimo que el módulo necesita en import-time.
    """
    if "azure.functions" in sys.modules:
        return

    import types

    fake = types.ModuleType("azure.functions")

    class _AuthLevel:
        FUNCTION = "function"

    class _FunctionApp:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def route(self, *args: Any, **kwargs: Any):
            return lambda f: f

        def queue_output(self, *args: Any, **kwargs: Any):
            return lambda f: f

        def queue_trigger(self, *args: Any, **kwargs: Any):
            return lambda f: f

        def timer_trigger(self, *args: Any, **kwargs: Any):
            return lambda f: f

    fake.FunctionApp = _FunctionApp        # type: ignore[attr-defined]
    fake.AuthLevel = _AuthLevel            # type: ignore[attr-defined]
    fake.HttpRequest = object              # type: ignore[attr-defined]
    fake.HttpResponse = object             # type: ignore[attr-defined]
    fake.TimerRequest = object             # type: ignore[attr-defined]
    fake.QueueMessage = object             # type: ignore[attr-defined]
    fake.Out = object                      # type: ignore[attr-defined]

    azure_pkg = types.ModuleType("azure")
    azure_pkg.functions = fake             # type: ignore[attr-defined]
    sys.modules["azure"] = azure_pkg
    sys.modules["azure.functions"] = fake


@pytest.fixture(autouse=True)
def _stub_pyodbc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub pyodbc también: el import no lo necesitamos para los tests."""
    if "pyodbc" in sys.modules:
        return
    import types
    fake = types.ModuleType("pyodbc")
    fake.Error = type("Error", (Exception,), {})           # type: ignore[attr-defined]
    fake.Connection = type("Connection", (), {})           # type: ignore[attr-defined]
    fake.Cursor = type("Cursor", (), {})                   # type: ignore[attr-defined]
    fake.connect = lambda *a, **kw: None                   # type: ignore[attr-defined]
    sys.modules["pyodbc"] = fake


# Imports diferidos para que los stubs estén montados antes.
@pytest.fixture
def fn_app_module():
    import function_app
    return function_app


# =====================================================================
# _poller_window
# =====================================================================


class TestPollerWindow:
    def _cfg(self, **kwargs: Any):
        from shared.config import MpWebhookConfig
        from shared.secret_string import SecretString
        base = dict(
            sql_connection_string=SecretString("sql"),
            mp_client_id=SecretString("cid"),
            mp_client_secret=SecretString("csec"),
            mp_webhook_secret=SecretString("wh"),
        )
        base.update(kwargs)
        return MpWebhookConfig(**base)  # type: ignore[arg-type]

    def test_incremental_default_4h(self, fn_app_module) -> None:
        cfg = self._cfg()
        now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
        begin, end = fn_app_module._poller_window(cfg, now=now)
        assert end == now
        assert (end - begin) == timedelta(hours=4)

    def test_incremental_custom_lookback(self, fn_app_module) -> None:
        cfg = self._cfg(mp_incremental_lookback_hours=24)
        now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
        begin, end = fn_app_module._poller_window(cfg, now=now)
        assert (end - begin) == timedelta(hours=24)

    def test_initial_load_usa_lookback_days(self, fn_app_module) -> None:
        cfg = self._cfg(mp_initial_load=True, mp_initial_lookback_days=30)
        now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
        begin, end = fn_app_module._poller_window(cfg, now=now)
        assert (end - begin) == timedelta(days=30)


# =====================================================================
# collect_payment_ids
# =====================================================================


def _payments(ids: List[int]) -> List[Dict[str, Any]]:
    return [{"id": pid, "status": "approved"} for pid in ids]


class TestCollectPaymentIds:
    """collect_payment_ids ahora delega el search+slicing en client.iter_all_payments
    (testeado a fondo en test_mp_client.py) y solo mapea a ids, dedup vía el iterador,
    con un cap defensivo por ciclo."""

    def test_mapea_payment_ids_a_str(self, fn_app_module) -> None:
        client = MagicMock()
        client.iter_all_payments.return_value = iter(_payments([1, 2, 3]))

        result = fn_app_module.collect_payment_ids(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
            page_size=50,
        )
        assert result == ["1", "2", "3"]
        # El poller incremental busca por date_last_updated.
        assert client.iter_all_payments.call_count == 1
        assert client.iter_all_payments.call_args.kwargs["range_field"] == "date_last_updated"

    def test_skipea_ids_none(self, fn_app_module) -> None:
        client = MagicMock()
        client.iter_all_payments.return_value = iter(
            [{"id": 1}, {"id": None}, {"id": 2}, {"status": "x"}]
        )
        result = fn_app_module.collect_payment_ids(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
        )
        assert result == ["1", "2"]

    def test_cap_defensivo_por_ciclo(self, fn_app_module) -> None:
        """Aunque el iterador devuelva un universo enorme, cortamos en max_pages*page_size."""
        import itertools

        client = MagicMock()
        client.iter_all_payments.return_value = ({"id": i} for i in itertools.count(1))

        result = fn_app_module.collect_payment_ids(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
            max_pages=3,
            page_size=50,
        )
        assert len(result) == 150  # 3 * 50

    def test_error_mp_no_explota(self, fn_app_module) -> None:
        from mp_client import MercadoPagoError

        def _gen():
            yield {"id": 1}
            yield {"id": 2}
            raise MercadoPagoError("boom")

        client = MagicMock()
        client.iter_all_payments.return_value = _gen()

        result = fn_app_module.collect_payment_ids(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
        )
        # Devuelve lo recolectado antes del error, sin propagar.
        assert result == ["1", "2"]

    def test_resultado_vacio(self, fn_app_module) -> None:
        client = MagicMock()
        client.iter_all_payments.return_value = iter([])
        result = fn_app_module.collect_payment_ids(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
        )
        assert result == []
