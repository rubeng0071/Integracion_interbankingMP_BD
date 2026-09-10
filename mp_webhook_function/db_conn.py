"""Conexión SQL reutilizada por hilo para el worker mp_process_payment.

pyodbc tiene threadsafety=1: una conexión no se comparte entre hilos. El worker
de Python corre las funciones síncronas en un thread pool, así que cada hilo guarda
la suya y la reutiliza entre invocaciones warm.

Mismo criterio que ib_poller.ib_processor.Database: ping liviano antes de usar y
reapertura si la conexión quedó zombi (idle timeout de Azure SQL, corte de red).

Garantía de no perder datos: transaction() hace commit solo si el bloque sale bien;
ante cualquier error hace rollback y re-lanza, de modo que el runtime de Functions
reintenta el mensaje (el pago sigue en la cola). Nunca commitea a medias.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

import pyodbc

logger = logging.getLogger(__name__)

# El default del ODBC Driver 18 es 15 s, exactamente donde hoy se corta.
LOGIN_TIMEOUT_SECONDS = 30


class ThreadLocalConnection:
    def __init__(self, conn_str: str, login_timeout: int = LOGIN_TIMEOUT_SECONDS) -> None:
        self._conn_str = conn_str
        self._login_timeout = login_timeout
        self._local = threading.local()

    def _open(self) -> pyodbc.Connection:
        # `timeout=` en pyodbc.connect es el login timeout (SQL_ATTR_LOGIN_TIMEOUT).
        return pyodbc.connect(self._conn_str, autocommit=False, timeout=self._login_timeout)

    def _discard(self) -> None:
        conn: Optional[pyodbc.Connection] = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except pyodbc.Error:
                pass

    def _ensure_alive(self) -> pyodbc.Connection:
        conn: Optional[pyodbc.Connection] = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.cursor().execute("SELECT 1").fetchone()
                return conn
            except pyodbc.Error:
                logger.warning("Conexión SQL muerta en este hilo; reabriendo")
                self._discard()
        conn = self._open()
        self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[pyodbc.Connection]:
        """Entrega la conexión del hilo; commit al salir bien, rollback si falla.

        Ante un pyodbc.Error la conexión se descarta: puede haber quedado
        inutilizable, y el próximo mensaje de este hilo abre una nueva.
        """
        conn = self._ensure_alive()
        try:
            yield conn
            conn.commit()
        except pyodbc.Error:
            try:
                conn.rollback()
            except pyodbc.Error:
                pass
            self._discard()
            raise
        except Exception:
            try:
                conn.rollback()
            except pyodbc.Error:
                pass
            raise
