"""Tests para shared.observability.

Cubrimos:
    - configure_logging instala un handler en el root.
    - Sin connection_string no se agrega AzureLogHandler.
    - Llamadas repetidas son idempotentes (no duplican handlers).
    - Si python-json-logger no está, cae a formatter plano.
    - Si opencensus-ext-azure no está y hay connection_string, no rompe.
    - El filter inyecta service en record.custom_dimensions.
"""
from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock

import pytest

from shared.observability import (
    _ServiceDimensionFilter,
    _reset_for_tests,
    configure_logging,
)
from shared.secret_string import SecretString


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Cada test arranca con root logger limpio."""
    _reset_for_tests()
    yield
    _reset_for_tests()


class TestConfigureLogging:
    def test_instala_stream_handler(self) -> None:
        configure_logging(service_name="test")
        root = logging.getLogger()
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)

    def test_idempotente(self) -> None:
        configure_logging(service_name="test")
        n1 = len(logging.getLogger().handlers)
        configure_logging(service_name="test")
        n2 = len(logging.getLogger().handlers)
        assert n1 == n2, "segunda llamada agrego handlers extra"

    def test_sin_connection_string_no_agrega_appinsights(self) -> None:
        configure_logging(connection_string=None, service_name="test")
        root = logging.getLogger()
        # Solo el StreamHandler; no debe haber AzureLogHandler.
        non_stream = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)]
        assert non_stream == []

    def test_connection_string_vacio_no_agrega_appinsights(self) -> None:
        configure_logging(connection_string=SecretString(""), service_name="test")
        non_stream = [h for h in logging.getLogger().handlers if not isinstance(h, logging.StreamHandler)]
        assert non_stream == []

    def test_appinsights_falla_no_aborta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si AzureLogHandler(...) lanza, configure_logging sigue con stdout."""
        import shared.observability as obs

        fake_module = MagicMock()
        fake_module.AzureLogHandler = MagicMock(side_effect=RuntimeError("invalid conn str"))
        monkeypatch.setitem(
            sys.modules,
            "opencensus.ext.azure.log_exporter",
            fake_module,
        )
        configure_logging(
            connection_string=SecretString("InstrumentationKey=fake"),
            service_name="test",
        )
        # El stream handler igual quedó instalado.
        assert any(
            isinstance(h, logging.StreamHandler)
            for h in logging.getLogger().handlers
        )

    def test_appinsights_se_agrega_si_se_puede(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mockeamos AzureLogHandler y verificamos que se agregue al root."""
        import shared.observability as obs

        added_handler = MagicMock(spec=logging.Handler)
        # Logger.addHandler chequea isinstance(h, logging.Handler).
        # Devolvemos un handler real para que pase.
        real_handler = logging.NullHandler()
        fake_AzureLogHandler = MagicMock(return_value=real_handler)

        fake_module = MagicMock()
        fake_module.AzureLogHandler = fake_AzureLogHandler
        monkeypatch.setitem(
            sys.modules,
            "opencensus.ext.azure.log_exporter",
            fake_module,
        )

        configure_logging(
            connection_string=SecretString("InstrumentationKey=fake"),
            service_name="mp_webhook",
        )

        # AzureLogHandler fue invocado con el conn string revelado.
        fake_AzureLogHandler.assert_called_once_with(connection_string="InstrumentationKey=fake")
        # El handler fue agregado al root.
        assert real_handler in logging.getLogger().handlers


class TestServiceDimensionFilter:
    def test_inyecta_service(self) -> None:
        f = _ServiceDimensionFilter("ib_poller")
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="hola", args=(), exc_info=None,
        )
        assert f.filter(record) is True
        assert record.custom_dimensions == {"service": "ib_poller"}  # type: ignore[attr-defined]

    def test_preserva_dimensions_existentes(self) -> None:
        f = _ServiceDimensionFilter("ib_poller")
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="hola", args=(), exc_info=None,
        )
        # Si alguien ya seteo custom_dimensions, solo le agregamos service.
        record.custom_dimensions = {"otra": "cosa"}  # type: ignore[attr-defined]
        f.filter(record)
        assert record.custom_dimensions == {"otra": "cosa", "service": "ib_poller"}  # type: ignore[attr-defined]

    def test_no_pisa_service_existente(self) -> None:
        f = _ServiceDimensionFilter("ib_poller")
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="hola", args=(), exc_info=None,
        )
        record.custom_dimensions = {"service": "ya_tenia"}  # type: ignore[attr-defined]
        f.filter(record)
        # setdefault no pisa.
        assert record.custom_dimensions["service"] == "ya_tenia"  # type: ignore[attr-defined]


class TestJsonFormatterFallback:
    def test_sin_pythonjsonlogger_cae_a_plano(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si python-json-logger no se puede importar, no debe romperse."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pythonjsonlogger" or name.startswith("pythonjsonlogger."):
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        configure_logging(service_name="test")
        # Logger queda funcional con formatter plano.
        stream = [h for h in logging.getLogger().handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream) >= 1
        # Verificamos que el formatter es el plain (no JSON).
        fmt = stream[0].formatter
        assert fmt is not None
        # JsonFormatter tiene formato distinto al Formatter base.
        assert type(fmt).__name__ == "Formatter"
