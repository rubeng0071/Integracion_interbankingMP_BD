"""Procesador Interbanking para el Timer Trigger.

Reemplaza la clase InterbankingSync del servicio monolítico, pero usando
shared.db_helpers.execute_upsert para evitar los 5 bloques MERGE manuales
duplicados (CAL-02 aplicado).

Cada método corresponde a un sub-proceso del sync, con su propio sync_run
en finance.sync_runs y su propio bookkeeping en finance.sync_control.

El entrypoint público es `run_full_sync(config)`, llamado desde function_app.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, Optional, Tuple

import pandas as pd
import pyodbc
from dateutil import parser as dtparser

from shared.config import IbPollerConfig
from shared.db_helpers import execute_upsert, sanitize_to_json, to_str
from shared.interbanking_client import InterbankingClient

logger = logging.getLogger(__name__)


# =====================================================================
# Helpers de tipos / parsing
# =====================================================================

def _utcnow_naive() -> datetime:
    return datetime.utcnow().replace(tzinfo=None)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return dtparser.parse(str(value)).replace(tzinfo=None)
    except (ValueError, TypeError) as exc:
        logger.warning("_parse_dt: valor no parseable %r (%s)", value, exc)
        return None


def _clean(value: Any) -> Any:
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


def _to_bool(value: Any) -> Optional[bool]:
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    txt = str(value).strip().lower()
    if txt in {"1", "true", "t", "yes", "si", "sí"}:
        return True
    if txt in {"0", "false", "f", "no"}:
        return False
    return None


def _sha256(*parts: Any) -> str:
    normalized = ["" if p is None else str(p).strip() for p in parts]
    return hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()


# =====================================================================
# Database (minimal — solo lo que necesita el poller)
# =====================================================================

class Database:
    """Wrapper de pyodbc con conexión persistente durante el ciclo del poller.

    Antes cada operación (`get_last_successful_sync`, `update_sync_control`,
    `start_sync_run`, `finish_sync_run` y los `with db.connect()` de los
    sub-procesos) abría y cerraba una conexión propia. En un ciclo completo
    eso significaba ~30 conexiones a Azure SQL, lo que en serverless cuesta
    DTU y agrega latencia de handshake en cada operación.

    Ahora mantenemos UNA sola conexión durante todo el run_full_sync():
        - Si todavía no hay conexión abierta, la creamos al primer uso.
        - Si la última operación lanzó excepción, hacemos rollback antes de
          ceder la conexión otra vez (evita "current transaction is aborted"
          residual en la próxima query).
        - Si la conexión se murió (idle timeout de Azure SQL, network blip),
          la siguiente operación detecta el error y reabre.

    Cerramos explícitamente al final del ciclo (close()) para no dejar
    sockets colgados entre invocaciones warm de la Function.
    """

    def __init__(self, conn_str: str) -> None:
        self.conn_str = conn_str
        self._conn: Optional[pyodbc.Connection] = None

    def _open(self) -> pyodbc.Connection:
        return pyodbc.connect(self.conn_str, autocommit=False)

    def _ensure_alive(self) -> pyodbc.Connection:
        if self._conn is None:
            self._conn = self._open()
            return self._conn
        try:
            # Ping liviano para detectar conexiones zombi (idle timeout, etc.).
            self._conn.cursor().execute("SELECT 1").fetchone()
            return self._conn
        except pyodbc.Error:
            logger.warning("Conexión SQL muerta; reabriendo")
            try:
                self._conn.close()
            except pyodbc.Error:
                pass
            self._conn = self._open()
            return self._conn

    @contextmanager
    def connect(self) -> Iterator[pyodbc.Connection]:
        conn = self._ensure_alive()
        try:
            yield conn
        except Exception:
            # Rollback defensivo: si el caller no commiteó por una excepción,
            # la próxima query empezaría con la transacción abortada.
            try:
                conn.rollback()
            except pyodbc.Error:
                pass
            raise

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except pyodbc.Error:
            pass
        self._conn = None

    def get_last_successful_sync(self, process_name: str) -> Optional[datetime]:
        with self.connect() as conn:
            row = conn.cursor().execute(
                "SELECT last_successful_sync FROM finance.sync_control WHERE process_name = ?",
                process_name,
            ).fetchone()
            return row[0] if row and row[0] else None

    def update_sync_control(
        self,
        process_name: str,
        status: str,
        begin_date: Optional[datetime],
        end_date: Optional[datetime],
        error: Optional[str] = None,
        success: bool = False,
    ) -> None:
        now = _utcnow_naive()
        with self.connect() as conn:
            cur = conn.cursor()
            if success:
                cur.execute(
                    """
                    UPDATE finance.sync_control
                    SET last_attempt_sync = ?, last_successful_sync = ?,
                        last_begin_date_used = ?, last_end_date_used = ?,
                        last_status = ?, last_error = ?, updated_at = ?
                    WHERE process_name = ?
                    """,
                    now, now, begin_date, end_date, status, error, now, process_name,
                )
            else:
                cur.execute(
                    """
                    UPDATE finance.sync_control
                    SET last_attempt_sync = ?, last_begin_date_used = ?, last_end_date_used = ?,
                        last_status = ?, last_error = ?, updated_at = ?
                    WHERE process_name = ?
                    """,
                    now, begin_date, end_date, status, error, now, process_name,
                )
            conn.commit()

    def start_sync_run(self, process_name: str) -> int:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO finance.sync_runs (source_system, process_name, status) "
                "OUTPUT INSERTED.sync_run_id VALUES (?, ?, ?)",
                "INTERBANKING", process_name, "RUNNING",
            )
            sync_run_id = cur.fetchone()[0]
            conn.commit()
            return sync_run_id

    def finish_sync_run(
        self,
        sync_run_id: int,
        status: str,
        rows_read: int = 0,
        rows_upserted: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        with self.connect() as conn:
            conn.cursor().execute(
                """
                UPDATE finance.sync_runs
                SET finished_at = SYSUTCDATETIME(), status = ?,
                    rows_read = ?, rows_upserted = ?, error_message = ?
                WHERE sync_run_id = ?
                """,
                status, rows_read, rows_upserted, error_message, sync_run_id,
            )
            conn.commit()


# =====================================================================
# CAL-02 — listas de columnas declarativas para cada tabla
# =====================================================================

_IB_ACCOUNTS_KEYS = ("account_cbu",)
_IB_ACCOUNTS_UPDATE = (
    "bank_number", "account_cuit", "account_label", "currency",
    "bank_name", "account_number", "account_type", "raw_json",
)

_IB_BALANCES_KEYS = ("balance_hash",)
_IB_BALANCES_UPDATE = (
    "bank_number", "account_number", "account_type", "currency",
    "account_label", "account_name", "row_date", "message",
    "countable_balance", "initial_operating_balance", "current_operating_balance",
    "projected_balance_24hs", "projected_balance_48hs",
    "operation_date", "day_balance", "total_debits", "total_credits",
    "is_historical", "raw_json",
)

_IB_MOVEMENTS_KEYS = ("movement_hash",)
_IB_MOVEMENTS_UPDATE = (
    "account_cbu", "depositor_code", "operation_code_ib", "operation_code_bank",
    "code_description_ib", "customer_cuit", "depositor_description", "code_description_bank",
    "amount", "voucher_number", "grouping_code_ib", "branch_office_activity",
    "process_date", "debit_credit_type", "movement_type", "source_account",
    "associated_voucher", "real_date_activity", "movement_date", "value_date",
    "correlative_number", "raw_json",
)

_IB_TRANSFERS_KEYS = ("transfer_id",)
_IB_TRANSFERS_UPDATE = (
    "transaction_number", "request_date", "transfer_type_code", "transfer_type_description",
    "account_label", "amount", "currency", "reference_number", "lot_number", "payment_number",
    "status", "client", "statement_consolidated", "unified_send", "direct_import",
    "same_owner", "internal_client_id", "addenda", "transfer_comments",
    "credit_account_customer_cuit", "credit_account_account_cbu", "credit_account_account_number",
    "credit_account_currency", "credit_account_account_type", "credit_account_bank_number",
    "credit_account_bank_name", "credit_account_account_label",
    "debit_account_customer_cuit", "debit_account_account_cbu", "debit_account_account_number",
    "debit_account_currency", "debit_account_account_type", "debit_account_bank_number",
    "debit_account_bank_name", "debit_account_account_label", "raw_json",
)

_IB_VOUCHERS_KEYS = ("transfer_id",)
_IB_VOUCHERS_UPDATE = (
    "request_date", "transfer_type_description", "transfer_type_code", "network_number",
    "amount", "currency", "validation_code", "total_amount", "comments",
    "billing_company", "paying_customer",
    "debit_account_customer_cuit", "debit_account_account_cbu", "debit_account_taxpayer_cuit",
    "debit_account_bank_number", "debit_account_bank_name", "debit_account_account_label",
    "afip_concept_description", "afip_control_code", "afip_nro_formulario",
    "afip_tax_description", "afip_fee_number", "afip_pago_desc",
    "afip_provider_name", "afip_concept_code", "afip_tax_code",
    "afip_vep_number", "afip_fiscal_period", "afip_provider_code",
    "credit_account_customer_cuit", "credit_account_account_cbu",
    "credit_account_bank_number", "credit_account_bank_name", "credit_account_account_label",
    "raw_json",
)

_IB_EXTRACTS_KEYS = ("extract_hash",)
_IB_EXTRACTS_UPDATE = (
    "statement_number", "operation_date", "total_movements", "opening_balance",
    "ending_balance", "operation_code_ib", "operation_code_bank", "code_description_ib",
    "customer_cuit", "depositor_description", "code_description_bank",
    "movement_date", "real_date_activity", "amount", "voucher_number",
    "branch_office_activity", "process_date", "value_date", "debit_credit_type",
    "correlative_number", "source_account", "code_description_standard",
    "operation_code_bank_standard", "raw_json",
)


# =====================================================================
# Procesador
# =====================================================================

@dataclass
class SyncStats:
    process_name: str
    rows_read: int
    rows_upserted: int
    duration_ms: int


class IBProcessor:
    """Sincroniza Interbanking (cuentas + balances + movimientos + transfers + extractos).

    Comparado con InterbankingSync del monolítico:
      - Usa execute_upsert(), eliminando ~600 líneas de SQL repetido.
      - Sin run_forever(): el Timer Trigger maneja el ciclo (AZ-11).
      - Operaciones declarativas: cada tabla se describe con keys + update_cols
        en constantes al tope del archivo.
    """

    def __init__(self, config: IbPollerConfig) -> None:
        self.config = config
        self.db = Database(config.sql_connection_string.reveal())
        # Inyectamos las credenciales desde el config en vez de que el cliente
        # las re-lea de os.environ. Asi Key Vault sigue siendo la fuente unica
        # de verdad: si un secret se rota en KV, basta con reciclar la Function
        # (no hay un segundo lugar donde se haya cacheado el valor).
        self.client = InterbankingClient(
            client_id=config.ib_client_id.reveal(),
            client_secret=config.ib_client_secret,
            service_url=config.ib_service_url,
            customer_id=config.ib_customer_id,
            token_url=config.ib_token_url,
            api_base_url=config.ib_api_base_url,
            grant_type=config.ib_grant_type,
            username=config.ib_username,
            password=config.ib_password,
            scope=config.ib_scope,
            page_size=config.ib_page_size,
            timeout=config.ib_timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _window(self, process_name: str) -> Tuple[str, str]:
        end_date = _utcnow_naive()
        last = self.db.get_last_successful_sync(process_name)
        lookback = self.config.ib_incremental_lookback_days
        if last is None:
            begin_date = end_date - timedelta(days=lookback)
        else:
            begin_date = last - timedelta(days=lookback)
        return begin_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")

    def _normalize_row(self, row: pd.Series) -> Dict[str, Any]:
        """Convierte una fila del DataFrame en dict con valores limpios."""
        return {k: _clean(v) for k, v in row.to_dict().items()}

    def _row_with_account(self, r: Dict[str, Any]) -> Dict[str, Any]:
        """Patch común: campos que necesitan to_str."""
        return {
            **r,
            "account_number": to_str(r.get("account_number")),
            "voucher_number": to_str(r.get("voucher_number")),
            "branch_office_activity": to_str(r.get("branch_office_activity")),
            "source_account": to_str(r.get("source_account")),
            "associated_voucher": to_str(r.get("associated_voucher")),
            "correlative_number": to_str(r.get("correlative_number")),
        }

    @contextmanager
    def _sync_context(self, process_name: str, begin: Optional[datetime], end: Optional[datetime]):
        """Wrapping uniforme: start_sync_run + update_sync_control RUNNING/SUCCESS/ERROR."""
        sync_run_id = self.db.start_sync_run(process_name)
        self.db.update_sync_control(process_name, "RUNNING", begin, end)
        started_at = _utcnow_naive()
        counter = {"read": 0, "upserted": 0}
        try:
            yield counter
        except Exception as exc:
            duration_ms = int((_utcnow_naive() - started_at).total_seconds() * 1000)
            logger.exception("sync %s falló tras %dms", process_name, duration_ms)
            self.db.update_sync_control(process_name, "ERROR", begin, end, str(exc), success=False)
            self.db.finish_sync_run(sync_run_id, "ERROR", counter["read"], counter["upserted"], str(exc))
            raise
        else:
            duration_ms = int((_utcnow_naive() - started_at).total_seconds() * 1000)
            self.db.update_sync_control(process_name, "SUCCESS", begin, end, success=True)
            self.db.finish_sync_run(sync_run_id, "SUCCESS", counter["read"], counter["upserted"])
            logger.info(
                "sync %s OK: read=%d upserted=%d duration_ms=%d",
                process_name, counter["read"], counter["upserted"], duration_ms,
            )

    # ------------------------------------------------------------------
    # Sub-procesos
    # ------------------------------------------------------------------

    def _process_accounts(self) -> SyncStats:
        process = "interbanking_accounts"
        started = _utcnow_naive()
        with self._sync_context(process, None, None) as counter:
            accounts_df, _ = self.client.get_cuentas()
            counter["read"] = len(accounts_df)
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, row in accounts_df.iterrows():
                    r = self._normalize_row(row)
                    r["account_number"] = to_str(r.get("account_number"))
                    r["raw_json"] = sanitize_to_json(r, source="ib")
                    execute_upsert(cur, "finance.ib_accounts", _IB_ACCOUNTS_KEYS, _IB_ACCOUNTS_UPDATE, r)
                    counter["upserted"] += 1
                conn.commit()
        return SyncStats(process, counter["read"], counter["upserted"],
                         int((_utcnow_naive() - started).total_seconds() * 1000))

    def _process_balances(self, begin: str, end: str) -> SyncStats:
        process = "interbanking_balances"
        started = _utcnow_naive()
        b_dt, e_dt = _parse_dt(begin), _parse_dt(end)
        with self._sync_context(process, b_dt, e_dt) as counter:
            balances_df, _ = self.client.get_saldos(date_since=begin, date_until=end)
            counter["read"] = len(balances_df)
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, row in balances_df.iterrows():
                    r = self._normalize_row(row)
                    r["balance_hash"] = _sha256(
                        r.get("bank_number"), r.get("account_number"), r.get("account_type"),
                        r.get("currency"), r.get("row_date"), r.get("operation_date"),
                        r.get("is_historical"), r.get("day_balance"),
                    )
                    r["account_number"] = to_str(r.get("account_number"))
                    r["row_date"] = _parse_dt(r.get("row_date"))
                    r["operation_date"] = _parse_dt(r.get("operation_date"))
                    r["is_historical"] = _to_bool(r.get("is_historical"))
                    r["raw_json"] = sanitize_to_json(r, source="ib")
                    execute_upsert(
                        cur, "finance.ib_balances",
                        _IB_BALANCES_KEYS, _IB_BALANCES_UPDATE,
                        r, extra_set=None,
                    )
                    counter["upserted"] += 1
                conn.commit()
        return SyncStats(process, counter["read"], counter["upserted"],
                         int((_utcnow_naive() - started).total_seconds() * 1000))

    def _process_movements(self, begin: str, end: str) -> SyncStats:
        process = "interbanking_movements"
        started = _utcnow_naive()
        b_dt, e_dt = _parse_dt(begin), _parse_dt(end)
        with self._sync_context(process, b_dt, e_dt) as counter:
            accounts_df, _ = self.client.get_cuentas()
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, acc in accounts_df.iterrows():
                    movements_df, _ = self.client.get_movimientos(
                        account_number=str(acc["account_number"]),
                        bank_number=acc["bank_number"],
                        date_since=begin, date_until=end,
                        movement_type="anteriores", version="v2",
                    )
                    counter["read"] += len(movements_df)
                    for _, row in movements_df.iterrows():
                        r = self._normalize_row(row)
                        r["movement_hash"] = _sha256(
                            r.get("source_account"), r.get("voucher_number"), r.get("process_date"),
                            r.get("amount"), r.get("debit_credit_type"), r.get("operation_code_ib"),
                            r.get("branch_office_activity"), r.get("correlative_number"),
                        )
                        r = self._row_with_account(r)
                        r["process_date"] = _parse_dt(r.get("process_date"))
                        r["real_date_activity"] = _parse_dt(r.get("real_date_activity"))
                        r["movement_date"] = _parse_dt(r.get("movement_date"))
                        r["value_date"] = _parse_dt(r.get("value_date"))
                        r["raw_json"] = sanitize_to_json(r, source="ib")
                        execute_upsert(
                            cur, "finance.ib_movements",
                            _IB_MOVEMENTS_KEYS, _IB_MOVEMENTS_UPDATE,
                            r, extra_set=None,
                        )
                        counter["upserted"] += 1
                conn.commit()
        return SyncStats(process, counter["read"], counter["upserted"],
                         int((_utcnow_naive() - started).total_seconds() * 1000))

    def _process_transfers(self, begin: str, end: str) -> SyncStats:
        process = "interbanking_transfers"
        started = _utcnow_naive()
        b_dt, e_dt = _parse_dt(begin), _parse_dt(end)
        with self._sync_context(process, b_dt, e_dt) as counter:
            transfers_df, _ = self.client.get_transferencias_detalle(date_since=begin, date_until=end)
            counter["read"] = len(transfers_df)
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, row in transfers_df.iterrows():
                    r = self._normalize_row(row)
                    r["transaction_number"] = to_str(r.get("transaction_number"))
                    r["reference_number"] = to_str(r.get("reference_number"))
                    r["lot_number"] = to_str(r.get("lot_number"))
                    r["payment_number"] = to_str(r.get("payment_number"))
                    r["request_date"] = _parse_dt(r.get("request_date"))
                    r["credit_account_account_number"] = to_str(r.get("credit_account_account_number"))
                    r["debit_account_account_number"] = to_str(r.get("debit_account_account_number"))
                    r["raw_json"] = sanitize_to_json(r, source="ib")
                    execute_upsert(
                        cur, "finance.ib_transfers",
                        _IB_TRANSFERS_KEYS, _IB_TRANSFERS_UPDATE,
                        r,
                    )
                    counter["upserted"] += 1
                conn.commit()
        return SyncStats(process, counter["read"], counter["upserted"],
                         int((_utcnow_naive() - started).total_seconds() * 1000))

    def _process_vouchers(self, begin: str, end: str) -> SyncStats:
        process = "interbanking_vouchers"
        started = _utcnow_naive()
        b_dt, e_dt = _parse_dt(begin), _parse_dt(end)
        # Voucher endpoint puede no estar disponible: lo verificamos.
        if not hasattr(self.client, "get_comprobantes"):
            logger.info("Cliente IB sin get_comprobantes; skip vouchers")
            return SyncStats(process, 0, 0, 0)
        with self._sync_context(process, b_dt, e_dt) as counter:
            vouchers_df, _ = self.client.get_comprobantes(date_since=begin, date_until=end)
            counter["read"] = len(vouchers_df)
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, row in vouchers_df.iterrows():
                    r = self._normalize_row(row)
                    r["request_date"] = _parse_dt(r.get("request_date"))
                    for col in ("network_number", "afip_control_code", "afip_nro_formulario",
                                "afip_fee_number", "afip_concept_code", "afip_tax_code",
                                "afip_vep_number", "afip_fiscal_period", "afip_provider_code"):
                        r[col] = to_str(r.get(col))
                    r["raw_json"] = sanitize_to_json(r, source="ib")
                    execute_upsert(
                        cur, "finance.ib_vouchers",
                        _IB_VOUCHERS_KEYS, _IB_VOUCHERS_UPDATE,
                        r, extra_set=None,
                    )
                    counter["upserted"] += 1
                conn.commit()
        return SyncStats(process, counter["read"], counter["upserted"],
                         int((_utcnow_naive() - started).total_seconds() * 1000))

    def _process_extracts(self, begin: str, end: str) -> SyncStats:
        process = "interbanking_extracts"
        started = _utcnow_naive()
        b_dt, e_dt = _parse_dt(begin), _parse_dt(end)
        with self._sync_context(process, b_dt, e_dt) as counter:
            accounts_df, _ = self.client.get_cuentas()
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, acc in accounts_df.iterrows():
                    extracts_df, _ = self.client.get_extractos(
                        account_number=str(acc["account_number"]),
                        bank_number=acc["bank_number"],
                        date_since=begin, date_until=end,
                    )
                    counter["read"] += len(extracts_df)
                    for _, row in extracts_df.iterrows():
                        r = self._normalize_row(row)
                        r["extract_hash"] = _sha256(
                            r.get("statement_number"), r.get("source_account"), r.get("voucher_number"),
                            r.get("process_date"), r.get("amount"), r.get("debit_credit_type"),
                            r.get("correlative_number"),
                        )
                        r["statement_number"] = to_str(r.get("statement_number"))
                        r["voucher_number"] = to_str(r.get("voucher_number"))
                        r["branch_office_activity"] = to_str(r.get("branch_office_activity"))
                        r["correlative_number"] = to_str(r.get("correlative_number"))
                        r["source_account"] = to_str(r.get("source_account"))
                        r["operation_date"] = _parse_dt(r.get("operation_date"))
                        r["movement_date"] = _parse_dt(r.get("movement_date"))
                        r["real_date_activity"] = _parse_dt(r.get("real_date_activity"))
                        r["process_date"] = _parse_dt(r.get("process_date"))
                        r["value_date"] = _parse_dt(r.get("value_date"))
                        r["raw_json"] = sanitize_to_json(r, source="ib")
                        execute_upsert(
                            cur, "finance.ib_extracts",
                            _IB_EXTRACTS_KEYS, _IB_EXTRACTS_UPDATE,
                            r, extra_set=None,
                        )
                        counter["upserted"] += 1
                conn.commit()
        return SyncStats(process, counter["read"], counter["upserted"],
                         int((_utcnow_naive() - started).total_seconds() * 1000))

    # ------------------------------------------------------------------
    # Entrypoint
    # ------------------------------------------------------------------

    def run_full_sync(self) -> Dict[str, SyncStats]:
        """Corre todos los sub-procesos en orden y devuelve estadísticas por proceso.

        No re-levanta excepciones de sub-procesos individuales: cada uno ya las
        loggea y persiste en finance.sync_runs / sync_control. Los errores
        parciales no impiden que los demás corran.

        Al terminar cerramos la conexión SQL persistente; entre invocaciones
        warm de la Function abrimos una nueva (es barato si el worker está
        caliente, y evita arrastrar conexiones zombi por horas).
        """
        results: Dict[str, SyncStats] = {}

        steps = [
            ("accounts",      lambda: self._process_accounts()),
            ("balances",      lambda: self._process_balances(*self._window("interbanking_balances"))),
            ("movements",     lambda: self._process_movements(*self._window("interbanking_movements"))),
            ("transfers",     lambda: self._process_transfers(*self._window("interbanking_transfers"))),
            ("vouchers",      lambda: self._process_vouchers(*self._window("interbanking_vouchers"))),
            ("extracts",      lambda: self._process_extracts(*self._window("interbanking_extracts"))),
        ]

        try:
            for label, fn in steps:
                try:
                    results[label] = fn()
                except Exception as exc:
                    logger.error("Sub-proceso '%s' falló: %s; continuando con los demás", label, exc)
                    results[label] = SyncStats(label, 0, 0, 0)
            return results
        finally:
            self.db.close()


def run_full_sync(config: IbPollerConfig) -> Dict[str, SyncStats]:
    """Atajo functional para llamar desde function_app.py."""
    return IBProcessor(config).run_full_sync()
