"""Tests para mp_webhook_function.db_conn.ThreadLocalConnection.

Verifican (spec fix-login-timeout-mp-webhook, sección 2):
    - Dos transacciones en el mismo hilo abren UNA sola conexión.
    - Dos hilos distintos obtienen conexiones distintas.
    - Conexión zombi (ping lanza pyodbc.Error) -> se cierra y se reabre.
    - pyodbc.Error dentro de la transacción -> rollback, descarte, y la próxima abre nueva.
    - Excepción no-pyodbc -> rollback, y la conexión se CONSERVA.
    - Salida normal -> commit() una vez.
    - pyodbc.connect recibe timeout=30 (login timeout) y autocommit=False.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pyodbc
import pytest

# mp_webhook_function no es un paquete instalable; lo agregamos al path (igual que test_mp_poller).
_MP_DIR = Path(__file__).resolve().parent.parent / "mp_webhook_function"
if str(_MP_DIR) not in sys.path:
    sys.path.insert(0, str(_MP_DIR))

import db_conn  # noqa: E402
from db_conn import LOGIN_TIMEOUT_SECONDS, ThreadLocalConnection  # noqa: E402


def _make_mock_conn(alive: bool = True) -> MagicMock:
    """Mock de pyodbc.Connection. alive=False -> el ping SELECT 1 lanza pyodbc.Error."""
    conn = MagicMock()
    cur = MagicMock()
    if alive:
        cur.execute.return_value = cur
        cur.fetchone.return_value = (1,)
    else:
        cur.execute.side_effect = pyodbc.Error("zombie")
    conn.cursor.return_value = cur
    return conn


class TestThreadLocalConnection:
    def test_reusa_la_conexion_en_el_mismo_hilo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _make_mock_conn()
        opens: list[int] = []

        def _fake_open(self_: ThreadLocalConnection) -> MagicMock:
            opens.append(1)
            return conn

        monkeypatch.setattr(ThreadLocalConnection, "_open", _fake_open)

        tlc = ThreadLocalConnection("dummy")
        with tlc.transaction() as c1:
            assert c1 is conn
        with tlc.transaction() as c2:
            assert c2 is conn

        assert len(opens) == 1, "esperábamos una sola apertura para 2 transacciones"
        assert conn.commit.call_count == 2

    def test_hilos_distintos_obtienen_conexiones_distintas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_open(self_: ThreadLocalConnection) -> MagicMock:
            return _make_mock_conn()

        monkeypatch.setattr(ThreadLocalConnection, "_open", _fake_open)

        tlc = ThreadLocalConnection("dummy")
        seen: dict[str, MagicMock] = {}

        def _work(name: str) -> None:
            with tlc.transaction() as c:
                seen[name] = c

        t1 = threading.Thread(target=_work, args=("a",))
        t2 = threading.Thread(target=_work, args=("b",))
        t1.start()
        t1.join()
        t2.start()
        t2.join()

        assert seen["a"] is not seen["b"]

    def test_conexion_zombi_se_reabre(self, monkeypatch: pytest.MonkeyPatch) -> None:
        zombie = _make_mock_conn(alive=False)
        fresh = _make_mock_conn(alive=True)
        opens = iter([zombie, fresh])
        monkeypatch.setattr(ThreadLocalConnection, "_open", lambda self_: next(opens))

        tlc = ThreadLocalConnection("dummy")
        # Primera transacción: no hay ping (conexión recién abierta) -> usa zombie.
        with tlc.transaction() as c1:
            assert c1 is zombie
        # Segunda: el ping falla -> descarta zombie y abre fresh.
        with tlc.transaction() as c2:
            assert c2 is fresh

        zombie.close.assert_called_once()

    def test_pyodbc_error_descarta_y_la_proxima_abre_nueva(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conns = [_make_mock_conn(), _make_mock_conn()]
        opens = iter(conns)
        monkeypatch.setattr(ThreadLocalConnection, "_open", lambda self_: next(opens))

        tlc = ThreadLocalConnection("dummy")
        with pytest.raises(pyodbc.Error):
            with tlc.transaction():
                raise pyodbc.Error("boom en el upsert")

        conns[0].rollback.assert_called_once()
        conns[0].close.assert_called_once()  # el descarte cierra la conexión

        # La próxima transacción del hilo abre una conexión nueva.
        with tlc.transaction() as c:
            assert c is conns[1]

    def test_excepcion_no_pyodbc_conserva_la_conexion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _make_mock_conn()
        opens: list[int] = []

        def _fake_open(self_: ThreadLocalConnection) -> MagicMock:
            opens.append(1)
            return conn

        monkeypatch.setattr(ThreadLocalConnection, "_open", _fake_open)

        tlc = ThreadLocalConnection("dummy")
        with pytest.raises(ValueError):
            with tlc.transaction():
                raise ValueError("error de datos, no de DB")

        conn.rollback.assert_called_once()
        conn.close.assert_not_called()  # NO se descarta

        # Se reutiliza la misma conexión (no se reabre).
        with tlc.transaction():
            pass
        assert len(opens) == 1

    def test_salida_normal_commitea_una_vez(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _make_mock_conn()
        monkeypatch.setattr(ThreadLocalConnection, "_open", lambda self_: conn)

        tlc = ThreadLocalConnection("dummy")
        with tlc.transaction():
            pass

        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()

    def test_connect_usa_login_timeout_30_y_autocommit_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def _fake_connect(conn_str: str, **kwargs: object) -> MagicMock:
            captured["conn_str"] = conn_str
            captured.update(kwargs)
            return _make_mock_conn()

        monkeypatch.setattr(db_conn.pyodbc, "connect", _fake_connect)

        tlc = ThreadLocalConnection("cadena-de-conexion")
        with tlc.transaction():
            pass

        assert captured["conn_str"] == "cadena-de-conexion"
        assert captured["timeout"] == LOGIN_TIMEOUT_SECONDS == 30
        assert captured["autocommit"] is False
