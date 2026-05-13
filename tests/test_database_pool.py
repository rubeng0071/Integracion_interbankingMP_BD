"""Tests para ib_poller.ib_processor.Database (conexión persistente).

Verifican que:
    - Una secuencia de operaciones reusa la misma conexión.
    - Una conexión zombi se detecta vía ping y se reabre.
    - Excepción dentro de connect() dispara rollback.
    - close() libera la conexión y la próxima operación abre una nueva.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pyodbc
import pytest

# ib_poller no es un paquete instalable; lo agregamos al path para importarlo aquí.
_IB_POLLER_DIR = Path(__file__).resolve().parent.parent / "ib_poller"
if str(_IB_POLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_IB_POLLER_DIR))

from ib_processor import Database  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_conn(alive: bool = True) -> MagicMock:
    """Construye un mock de pyodbc.Connection.

    Si alive=False, el cursor.execute('SELECT 1') lanza pyodbc.Error
    (simula conexión zombi).
    """
    conn = MagicMock()
    cur = MagicMock()
    if alive:
        cur.execute.return_value = cur
        cur.fetchone.return_value = (1,)
    else:
        cur.execute.side_effect = pyodbc.Error("zombie")
    conn.cursor.return_value = cur
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDatabasePersistence:
    def test_reusa_la_misma_conexion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _make_mock_conn()
        opens: list[MagicMock] = []

        def _fake_open(self_: Database) -> MagicMock:
            opens.append(conn)
            return conn

        monkeypatch.setattr(Database, "_open", _fake_open)

        db = Database("dummy")
        with db.connect() as c1:
            assert c1 is conn
        with db.connect() as c2:
            assert c2 is conn
        with db.connect() as c3:
            assert c3 is conn

        assert len(opens) == 1, "esperabamos una sola apertura para 3 usos"

    def test_close_libera_y_proxima_operacion_abre_nueva(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conns = [_make_mock_conn(), _make_mock_conn()]
        idx = {"i": 0}

        def _fake_open(self_: Database) -> MagicMock:
            c = conns[idx["i"]]
            idx["i"] += 1
            return c

        monkeypatch.setattr(Database, "_open", _fake_open)

        db = Database("dummy")
        with db.connect():
            pass
        db.close()
        # close() debe haber cerrado la primera conexión.
        conns[0].close.assert_called_once()

        with db.connect() as c:
            assert c is conns[1]

    def test_excepcion_dispara_rollback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _make_mock_conn()
        monkeypatch.setattr(Database, "_open", lambda self_: conn)
        db = Database("dummy")

        with pytest.raises(ValueError):
            with db.connect():
                raise ValueError("boom")

        conn.rollback.assert_called_once()

    def test_conexion_zombi_se_reabre(self, monkeypatch: pytest.MonkeyPatch) -> None:
        zombie = _make_mock_conn(alive=False)
        fresh = _make_mock_conn(alive=True)
        opens = iter([zombie, fresh])
        monkeypatch.setattr(Database, "_open", lambda self_: next(opens))

        db = Database("dummy")
        # Primera apertura -> recibimos zombie.
        with db.connect() as c1:
            assert c1 is zombie

        # En la siguiente operación el ping falla; debe cerrar zombi y abrir fresh.
        with db.connect() as c2:
            assert c2 is fresh

        zombie.close.assert_called_once()

    def test_close_sin_conexion_abierta_no_explota(self) -> None:
        db = Database("dummy")
        db.close()  # no-op, no debería levantar.
