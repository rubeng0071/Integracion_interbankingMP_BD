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
# collect_payment_candidates
# =====================================================================


def _payments(ids: List[int]) -> List[Dict[str, Any]]:
    return [{"id": pid, "status": "approved"} for pid in ids]


class TestCollectPaymentCandidates:
    """collect_payment_candidates delega el search+slicing en client.iter_all_payments
    (testeado a fondo en test_mp_client.py) y devuelve (id_str, date_last_updated parseada),
    con un cap defensivo por ciclo."""

    def test_mapea_a_tuplas_id_fecha(self, fn_app_module) -> None:
        client = MagicMock()
        client.iter_all_payments.return_value = iter(_payments([1, 2, 3]))

        result = fn_app_module.collect_payment_candidates(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
            page_size=50,
        )
        # Sin date_last_updated en el fixture -> parse_dt(None) == None.
        assert result == [("1", None), ("2", None), ("3", None)]
        # El poller incremental busca por date_last_updated.
        assert client.iter_all_payments.call_count == 1
        assert client.iter_all_payments.call_args.kwargs["range_field"] == "date_last_updated"

    def test_parsea_date_last_updated(self, fn_app_module) -> None:
        client = MagicMock()
        client.iter_all_payments.return_value = iter(
            [{"id": 1, "date_last_updated": "2026-09-10T08:00:59.000-04:00"}]
        )
        result = fn_app_module.collect_payment_candidates(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
        )
        # parse_dt normaliza a naive (tz descartada), igual que el worker.
        assert result == [("1", datetime(2026, 9, 10, 8, 0, 59))]

    def test_skipea_ids_none(self, fn_app_module) -> None:
        client = MagicMock()
        client.iter_all_payments.return_value = iter(
            [{"id": 1}, {"id": None}, {"id": 2}, {"status": "x"}]
        )
        result = fn_app_module.collect_payment_candidates(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
        )
        assert result == [("1", None), ("2", None)]

    def test_cap_defensivo_por_ciclo(self, fn_app_module) -> None:
        """Aunque el iterador devuelva un universo enorme, cortamos en max_pages*page_size."""
        import itertools

        client = MagicMock()
        client.iter_all_payments.return_value = ({"id": i} for i in itertools.count(1))

        result = fn_app_module.collect_payment_candidates(
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

        result = fn_app_module.collect_payment_candidates(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
        )
        # Devuelve lo recolectado antes del error, sin propagar.
        assert result == [("1", None), ("2", None)]

    def test_resultado_vacio(self, fn_app_module) -> None:
        client = MagicMock()
        client.iter_all_payments.return_value = iter([])
        result = fn_app_module.collect_payment_candidates(
            client=client,
            begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            page_delay_seconds=0,
        )
        assert result == []


# =====================================================================
# select_changed  (función pura: qué encolar)
# =====================================================================


class TestSelectChanged:
    def test_id_nuevo_se_encola(self, fn_app_module) -> None:
        cand = [("1", datetime(2026, 9, 10, 8, 0, 0))]
        assert fn_app_module.select_changed(cand, stored={}) == ["1"]

    def test_misma_fecha_no_se_encola(self, fn_app_module) -> None:
        dt = datetime(2026, 9, 10, 8, 0, 0)
        assert fn_app_module.select_changed([("1", dt)], stored={"1": dt}) == []

    def test_fecha_distinta_se_encola(self, fn_app_module) -> None:
        cand = [("1", datetime(2026, 9, 10, 8, 0, 5))]
        stored = {"1": datetime(2026, 9, 10, 8, 0, 0)}
        assert fn_app_module.select_changed(cand, stored) == ["1"]

    def test_fecha_ausente_en_el_candidato_se_encola(self, fn_app_module) -> None:
        # parse_dt no pudo parsear -> None -> ante la duda, encolar.
        stored = {"1": datetime(2026, 9, 10, 8, 0, 0)}
        assert fn_app_module.select_changed([("1", None)], stored) == ["1"]

    def test_fecha_nula_en_sql_se_encola(self, fn_app_module) -> None:
        # stored no trae la clave (load_stored_last_updated omite los nulos) -> encolar.
        cand = [("1", datetime(2026, 9, 10, 8, 0, 0))]
        assert fn_app_module.select_changed(cand, stored={}) == ["1"]

    def test_fecha_con_tz_igual_a_la_naive_guardada_no_se_encola(self, fn_app_module) -> None:
        from mp_processor import parse_dt

        # El candidato viene con tz -04:00; parse_dt lo deja naive, igual que lo guardado.
        dlu = parse_dt("2026-09-10T08:00:59.000-04:00")
        stored = {"1": datetime(2026, 9, 10, 8, 0, 59)}
        assert fn_app_module.select_changed([("1", dlu)], stored) == []

    def test_mezcla_conserva_orden(self, fn_app_module) -> None:
        dt = datetime(2026, 9, 10, 8, 0, 0)
        cand = [("1", dt), ("2", datetime(2026, 9, 10, 9, 0, 0)), ("3", None)]
        stored = {"1": dt, "2": datetime(2026, 9, 10, 8, 0, 0)}  # 2 cambió, 1 igual, 3 sin fecha
        assert fn_app_module.select_changed(cand, stored) == ["2", "3"]


# =====================================================================
# load_stored_last_updated  (lotes de 500, <= 2100 params)
# =====================================================================


class _RecordingCursor:
    def __init__(self, rows_by_call: List[List[Any]]) -> None:
        self._rows_by_call = rows_by_call
        self.calls: List[tuple] = []
        self._i = 0

    def execute(self, sql: str, *params: Any):
        self.calls.append((sql, params))
        self._rows = self._rows_by_call[self._i] if self._i < len(self._rows_by_call) else []
        self._i += 1
        return self

    def fetchall(self) -> List[Any]:
        return self._rows


class _RecordingConn:
    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _RecordingCursor:
        return self._cursor


class TestLoadStoredLastUpdated:
    def test_parte_en_lotes_de_500_sin_pasar_2100_params(self, fn_app_module) -> None:
        cur = _RecordingCursor(rows_by_call=[[], [], []])
        conn = _RecordingConn(cur)
        ids = [str(i) for i in range(1200)]

        fn_app_module.load_stored_last_updated(conn, ids)

        assert len(cur.calls) == 3  # 500 + 500 + 200
        tamanos = [len(params) for _, params in cur.calls]
        assert tamanos == [500, 500, 200]
        assert all(t <= 2100 for t in tamanos)
        # Los placeholders del SQL coinciden con la cantidad de params.
        for sql, params in cur.calls:
            assert sql.count("?") == len(params)

    def test_omite_nulos_y_normaliza_naive(self, fn_app_module) -> None:
        # payment_id vuelve como int de SQL; la clave del dict debe ser str.
        rows = [[(1, datetime(2026, 9, 10, 8, 0, 0)), (2, None)]]
        cur = _RecordingCursor(rows_by_call=rows)
        conn = _RecordingConn(cur)

        stored = fn_app_module.load_stored_last_updated(conn, ["1", "2"])

        assert stored == {"1": datetime(2026, 9, 10, 8, 0, 0)}  # el 2 (nulo) se omite

    def test_lista_vacia_no_consulta(self, fn_app_module) -> None:
        cur = _RecordingCursor(rows_by_call=[])
        conn = _RecordingConn(cur)
        assert fn_app_module.load_stored_last_updated(conn, []) == {}
        assert cur.calls == []


# =====================================================================
# mp_poller_run: red de seguridad ante fallo de SQL
# =====================================================================


class TestMpPollerRunSafetyNet:
    def _cfg(self):
        from shared.config import MpWebhookConfig
        from shared.secret_string import SecretString

        return MpWebhookConfig(
            sql_connection_string=SecretString("sql"),
            mp_client_id=SecretString("cid"),
            mp_client_secret=SecretString("csec"),
            mp_webhook_secret=SecretString("wh"),
        )

    def test_si_falla_la_lectura_de_sql_encola_todo(
        self, fn_app_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pyodbc
        from contextlib import contextmanager

        fa = fn_app_module
        monkeypatch.setattr(fa, "_get_config", lambda: self._cfg())

        client = MagicMock()
        client.iter_all_payments.return_value = iter(
            [
                {"id": 1, "date_last_updated": "2026-09-10T08:00:00.000-04:00"},
                {"id": 2, "date_last_updated": "2026-09-10T08:00:01.000-04:00"},
            ]
        )
        monkeypatch.setattr(fa, "_get_mp_client", lambda: client)

        class _FailDB:
            def transaction(self):
                @contextmanager
                def _cm():
                    raise pyodbc.Error("login timeout")
                    yield  # pragma: no cover
                return _cm()

        monkeypatch.setattr(fa, "_get_db", lambda: _FailDB())

        captured: Dict[str, Any] = {}

        class _Out:
            def set(self, value: Any) -> None:
                captured["value"] = value

        timer = MagicMock()
        timer.past_due = False

        fa.mp_poller_run(timer, _Out())

        # Red de seguridad: ante el error de SQL, se encola TODO.
        assert captured["value"] == ["1", "2"]

    def test_pre_filtra_los_que_ya_estan_al_dia(
        self, fn_app_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from contextlib import contextmanager

        fa = fn_app_module
        monkeypatch.setattr(fa, "_get_config", lambda: self._cfg())

        client = MagicMock()
        client.iter_all_payments.return_value = iter(
            [
                {"id": 1, "date_last_updated": "2026-09-10T08:00:00.000-04:00"},
                {"id": 2, "date_last_updated": "2026-09-10T09:00:00.000-04:00"},
            ]
        )
        monkeypatch.setattr(fa, "_get_mp_client", lambda: client)

        # SQL dice que el 1 ya está al día (misma fecha); el 2 no está.
        stored = {"1": datetime(2026, 9, 10, 8, 0, 0)}

        class _OkDB:
            def transaction(self):
                @contextmanager
                def _cm():
                    yield MagicMock()  # la conexión no se usa (load está mockeado)
                return _cm()

        monkeypatch.setattr(fa, "_get_db", lambda: _OkDB())
        monkeypatch.setattr(fa, "load_stored_last_updated", lambda conn, ids: stored)

        captured: Dict[str, Any] = {}

        class _Out:
            def set(self, value: Any) -> None:
                captured["value"] = value

        timer = MagicMock()
        timer.past_due = False

        fa.mp_poller_run(timer, _Out())

        assert captured["value"] == ["2"]  # solo el que cambió
