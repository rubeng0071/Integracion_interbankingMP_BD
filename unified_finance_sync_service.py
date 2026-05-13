#!/usr/bin/env python3
"""Servicio integrado Mercado Pago + Interbanking.

- Carga inicial e incremental de Mercado Pago.
- Carga incremental de Interbanking usando el mismo cliente del proyecto.
- Upsert en SQL Server.
- Preparado para correr como daemon en Oracle Linux (systemd).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pyodbc
import requests
from dateutil import parser as dtparser

try:
    import pandas as pd
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Falta instalar pandas") from exc

try:
    # InterbankingClient ahora vive en shared/ junto con SecretString y los demás
    # helpers. Mismo módulo, una sola fuente de verdad para el monolítico y para
    # ib_poller/. La importación falla si `shared/` no está en el PYTHONPATH.
    from shared.interbanking_client import InterbankingClient
except Exception:
    InterbankingClient = None  # type: ignore

# SEC-03: sanitización de PII antes de persistir el raw_json.
# Import con fallback para mantener el archivo ejecutable aun si `shared/` no existe.
try:
    from shared.db_helpers import sanitize_to_json
except ImportError:  # pragma: no cover
    def sanitize_to_json(payload: Any, source: str = "mp") -> str:  # type: ignore[no-redef]
        """Fallback sin sanitización (mantiene compatibilidad si `shared/` falta).

        ATENCIÓN: con este fallback el raw_json contendrá PII completa.
        Asegurate de instalar el paquete `shared/` antes de producción.
        """
        return json.dumps(payload, ensure_ascii=False, default=str)


# =========================
# CONFIG
# =========================

@dataclass
class Config:
    sql_connection_string: str
    mp_access_token: str
    poll_interval_seconds: int = 600
    mp_initial_lookback_days: int = 365
    mp_incremental_lookback_hours: int = 72
    ib_incremental_lookback_days: int = 7
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        sql_connection_string = os.environ["SQL_CONNECTION_STRING"]
        mp_access_token = os.environ["MP_ACCESS_TOKEN"]
        return cls(
            sql_connection_string=sql_connection_string,
            mp_access_token=mp_access_token,
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "600")),
            mp_initial_lookback_days=int(os.getenv("MP_INITIAL_LOOKBACK_DAYS", "365")),
            mp_incremental_lookback_hours=int(os.getenv("MP_INCREMENTAL_LOOKBACK_HOURS", "72")),
            ib_incremental_lookback_days=int(os.getenv("IB_INCREMENTAL_LOOKBACK_DAYS", "7")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def utcnow_naive() -> datetime:
    return datetime.utcnow().replace(tzinfo=None)


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return dtparser.parse(str(value)).replace(tzinfo=None)


def safe_get(d: Dict[str, Any], *path: str, default=None):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def clean_value(value: Any) -> Any:
    if pd.isna(value) if isinstance(value, float) else False:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped != "" else None
    return value


def to_bool(value: Any) -> Optional[bool]:
    value = clean_value(value)
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


def sha256_of_parts(*parts: Any) -> str:
    normalized = ["" if p is None else str(p).strip() for p in parts]
    return hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()


# =========================
# DATABASE
# =========================

class Database:
    def __init__(self, conn_str: str):
        self.conn_str = conn_str

    @contextmanager
    def connect(self):
        conn = pyodbc.connect(self.conn_str)
        try:
            yield conn
        finally:
            conn.close()

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
        now = utcnow_naive()
        with self.connect() as conn:
            cur = conn.cursor()
            if success:
                cur.execute(
                    """
                    UPDATE finance.sync_control
                    SET last_attempt_sync = ?,
                        last_successful_sync = ?,
                        last_begin_date_used = ?,
                        last_end_date_used = ?,
                        last_status = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE process_name = ?
                    """,
                    now, now, begin_date, end_date, status, error, now, process_name,
                )
            else:
                cur.execute(
                    """
                    UPDATE finance.sync_control
                    SET last_attempt_sync = ?,
                        last_begin_date_used = ?,
                        last_end_date_used = ?,
                        last_status = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE process_name = ?
                    """,
                    now, begin_date, end_date, status, error, now, process_name,
                )
            conn.commit()

    def start_sync_run(self, source_system: str, process_name: str) -> int:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO finance.sync_runs (source_system, process_name, status) OUTPUT INSERTED.sync_run_id VALUES (?, ?, ?)",
                source_system, process_name, "RUNNING"
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
        rows_deleted_reloaded: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        with self.connect() as conn:
            conn.cursor().execute(
                """
                UPDATE finance.sync_runs
                SET finished_at = SYSUTCDATETIME(),
                    status = ?,
                    rows_read = ?,
                    rows_upserted = ?,
                    rows_deleted_reloaded = ?,
                    error_message = ?
                WHERE sync_run_id = ?
                """,
                status, rows_read, rows_upserted, rows_deleted_reloaded, error_message, sync_run_id,
            )
            conn.commit()


# =========================
# MERCADO PAGO
# =========================

class MercadoPagoClient:
    BASE_URL = "https://api.mercadopago.com/v1/payments/search"

    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def search_payments(self, begin_date: datetime, end_date: datetime, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        params = {
            "sort": "date_created",
            "criteria": "desc",
            "range": "date_created",
            "begin_date": begin_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_date": end_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "limit": limit,
            "offset": offset,
        }
        response = self.session.get(self.BASE_URL, params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def fetch_all_payments(self, begin_date: datetime, end_date: datetime, page_size: int = 50) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        offset = 0
        while True:
            payload = self.search_payments(begin_date, end_date, page_size, offset)
            page_results = payload.get("results", [])
            if not page_results:
                break
            results.extend(page_results)
            if len(page_results) < page_size:
                break
            offset += page_size
            time.sleep(0.15)
        return results


def transform_mp_payment(payment: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    payment_id = payment["id"]
    parent = {
        "payment_id": payment_id,
        "collector_id": safe_get(payment, "collector_id") or safe_get(payment, "collector", "id"),
        "payer_id": safe_get(payment, "payer_id") or safe_get(payment, "payer", "id"),
        "external_reference": payment.get("external_reference"),
        "order_id": safe_get(payment, "order", "id"),
        "order_type": safe_get(payment, "order", "type"),
        "status": payment.get("status"),
        "status_detail": payment.get("status_detail"),
        "operation_type": payment.get("operation_type"),
        "payment_method_id": payment.get("payment_method_id"),
        "payment_type_id": payment.get("payment_type_id"),
        "payment_method_type": safe_get(payment, "payment_method", "type"),
        "issuer_id": payment.get("issuer_id"),
        "currency_id": payment.get("currency_id"),
        "installments": payment.get("installments"),
        "transaction_amount": payment.get("transaction_amount"),
        "transaction_amount_refunded": payment.get("transaction_amount_refunded"),
        "shipping_amount": payment.get("shipping_amount"),
        "shipping_cost": payment.get("shipping_cost"),
        "taxes_amount": payment.get("taxes_amount"),
        "coupon_amount": payment.get("coupon_amount"),
        "net_received_amount": safe_get(payment, "transaction_details", "net_received_amount"),
        "total_paid_amount": safe_get(payment, "transaction_details", "total_paid_amount"),
        "installment_amount": safe_get(payment, "transaction_details", "installment_amount"),
        "overpaid_amount": safe_get(payment, "transaction_details", "overpaid_amount"),
        "description": payment.get("description"),
        "authorization_code": payment.get("authorization_code"),
        "money_release_status": payment.get("money_release_status"),
        "binary_mode": payment.get("binary_mode"),
        "captured": payment.get("captured"),
        "live_mode": payment.get("live_mode"),
        "store_id": payment.get("store_id"),
        "pos_id": payment.get("pos_id"),
        "notification_url": payment.get("notification_url"),
        "statement_descriptor": payment.get("statement_descriptor"),
        "processing_mode": payment.get("processing_mode"),
        "point_type": safe_get(payment, "point_of_interaction", "type"),
        "point_unit": safe_get(payment, "point_of_interaction", "business_info", "unit"),
        "point_sub_unit": safe_get(payment, "point_of_interaction", "business_info", "sub_unit"),
        "point_branch": safe_get(payment, "point_of_interaction", "business_info", "branch"),
        "point_source": safe_get(payment, "point_of_interaction", "location", "source"),
        "point_state_id": safe_get(payment, "point_of_interaction", "location", "state_id"),
        "payer_email": safe_get(payment, "payer", "email"),
        "payer_identification_type": safe_get(payment, "payer", "identification", "type"),
        "payer_identification_number": safe_get(payment, "payer", "identification", "number"),
        "card_first_six_digits": safe_get(payment, "card", "first_six_digits"),
        "card_last_four_digits": safe_get(payment, "card", "last_four_digits"),
        "cardholder_name": safe_get(payment, "card", "cardholder", "name"),
        "cardholder_ident_type": safe_get(payment, "card", "cardholder", "identification", "type"),
        "cardholder_ident_number": safe_get(payment, "card", "cardholder", "identification", "number"),
        "date_created": parse_dt(payment.get("date_created")),
        "date_approved": parse_dt(payment.get("date_approved")),
        "date_last_updated": parse_dt(payment.get("date_last_updated")),
        "money_release_date": parse_dt(payment.get("money_release_date")),
        # SEC-03: el blob raw_json se sanitiza para no duplicar PII fuera de las
        # columnas dedicadas (payer_email, card_*, cardholder_*, etc.). Esas
        # columnas dedicadas siguen guardándose; el dump JSON queda redactado.
        "raw_json": sanitize_to_json(payment, source="mp"),
    }
    charges: List[Dict[str, Any]] = []
    for ch in payment.get("charges_details", []) or []:
        charges.append({
            "payment_id": payment_id,
            "charge_id": ch.get("id"),
            "charge_type": ch.get("type"),
            "charge_name": ch.get("name"),
            "account_from": safe_get(ch, "accounts", "from"),
            "account_to": safe_get(ch, "accounts", "to"),
            "amount_original": safe_get(ch, "amounts", "original"),
            "amount_refunded": safe_get(ch, "amounts", "refunded"),
            "base_amount": ch.get("base_amount"),
            "rate": ch.get("rate"),
            "reserve_id": ch.get("reserve_id"),
            "client_id": ch.get("client_id"),
            "tax_id": safe_get(ch, "metadata", "tax_id"),
            "tax_status": safe_get(ch, "metadata", "tax_status"),
            "mov_detail": safe_get(ch, "metadata", "mov_detail"),
            "mov_financial_entity": safe_get(ch, "metadata", "mov_financial_entity"),
            "mov_type": safe_get(ch, "metadata", "mov_type"),
            "metadata_user_id": safe_get(ch, "metadata", "user_id"),
            "metadata_source": safe_get(ch, "metadata", "source"),
            "charge_date_created": parse_dt(ch.get("date_created")),
            "charge_last_updated": parse_dt(ch.get("last_updated")),
        })
    items: List[Dict[str, Any]] = []
    for item in (safe_get(payment, "additional_info", "items", default=[]) or []):
        items.append({
            "payment_id": payment_id,
            "item_id": item.get("id"),
            "category_id": item.get("category_id"),
            "title": item.get("title"),
            "description": item.get("description"),
            "quantity": item.get("quantity"),
            "unit_price": item.get("unit_price"),
            "picture_url": item.get("picture_url"),
        })
    return parent, charges, items


class MercadoPagoSync:
    PROCESS_NAME = "mercadopago_payments"

    def __init__(self, db: Database, client: MercadoPagoClient, config: Config):
        self.db = db
        self.client = client
        self.config = config

    def compute_window(self, initial_load: bool) -> Tuple[datetime, datetime]:
        end_date = utcnow_naive()
        last_sync = self.db.get_last_successful_sync(self.PROCESS_NAME)
        if initial_load or last_sync is None:
            return end_date - timedelta(days=self.config.mp_initial_lookback_days), end_date
        # CAL-10: reemplazo de `assert last_sync is not None`. El assert se elimina
        # cuando Python corre con -O, dejando la rama sin protección. Validamos
        # explícitamente con un error descriptivo.
        if last_sync is None:
            raise RuntimeError(
                f"compute_window({self.PROCESS_NAME}): last_successful_sync vino None "
                "en una rama incremental. Esto indica una race condition con sync_control."
            )
        return last_sync - timedelta(hours=self.config.mp_incremental_lookback_hours), end_date

    def run(self, initial_load: bool = False) -> None:
        begin_date, end_date = self.compute_window(initial_load)
        sync_run_id = self.db.start_sync_run("MERCADOPAGO", self.PROCESS_NAME)
        self.db.update_sync_control(self.PROCESS_NAME, "RUNNING", begin_date, end_date)
        rows_read = rows_upserted = rows_children = 0
        try:
            payments = self.client.fetch_all_payments(begin_date, end_date)
            rows_read = len(payments)
            with self.db.connect() as conn:
                conn.autocommit = False
                cur = conn.cursor()
                for payment in payments:
                    parent, charges, items = transform_mp_payment(payment)
                    cur.execute(
                        """
                        MERGE finance.mp_payments AS tgt
                        USING (SELECT ? AS payment_id) AS src
                        ON tgt.payment_id = src.payment_id
                        WHEN MATCHED THEN UPDATE SET
                            collector_id=?, payer_id=?, external_reference=?, order_id=?, order_type=?, status=?, status_detail=?, operation_type=?,
                            payment_method_id=?, payment_type_id=?, payment_method_type=?, issuer_id=?, currency_id=?, installments=?, transaction_amount=?,
                            transaction_amount_refunded=?, shipping_amount=?, shipping_cost=?, taxes_amount=?, coupon_amount=?, net_received_amount=?,
                            total_paid_amount=?, installment_amount=?, overpaid_amount=?, description=?, authorization_code=?, money_release_status=?,
                            binary_mode=?, captured=?, live_mode=?, store_id=?, pos_id=?, notification_url=?, statement_descriptor=?, processing_mode=?,
                            point_type=?, point_unit=?, point_sub_unit=?, point_branch=?, point_source=?, point_state_id=?, payer_email=?,
                            payer_identification_type=?, payer_identification_number=?, card_first_six_digits=?, card_last_four_digits=?,
                            cardholder_name=?, cardholder_ident_type=?, cardholder_ident_number=?, date_created=?, date_approved=?,
                            date_last_updated=?, money_release_date=?, raw_json=?, updated_at=SYSUTCDATETIME()
                        WHEN NOT MATCHED THEN INSERT (
                            payment_id, collector_id, payer_id, external_reference, order_id, order_type, status, status_detail, operation_type,
                            payment_method_id, payment_type_id, payment_method_type, issuer_id, currency_id, installments, transaction_amount,
                            transaction_amount_refunded, shipping_amount, shipping_cost, taxes_amount, coupon_amount, net_received_amount,
                            total_paid_amount, installment_amount, overpaid_amount, description, authorization_code, money_release_status,
                            binary_mode, captured, live_mode, store_id, pos_id, notification_url, statement_descriptor, processing_mode,
                            point_type, point_unit, point_sub_unit, point_branch, point_source, point_state_id, payer_email,
                            payer_identification_type, payer_identification_number, card_first_six_digits, card_last_four_digits,
                            cardholder_name, cardholder_ident_type, cardholder_ident_number, date_created, date_approved,
                            date_last_updated, money_release_date, raw_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                        """,
                        parent["payment_id"],
                        parent["collector_id"], parent["payer_id"], parent["external_reference"], parent["order_id"], parent["order_type"], parent["status"], parent["status_detail"], parent["operation_type"],
                        parent["payment_method_id"], parent["payment_type_id"], parent["payment_method_type"], parent["issuer_id"], parent["currency_id"], parent["installments"], parent["transaction_amount"],
                        parent["transaction_amount_refunded"], parent["shipping_amount"], parent["shipping_cost"], parent["taxes_amount"], parent["coupon_amount"], parent["net_received_amount"],
                        parent["total_paid_amount"], parent["installment_amount"], parent["overpaid_amount"], parent["description"], parent["authorization_code"], parent["money_release_status"],
                        parent["binary_mode"], parent["captured"], parent["live_mode"], parent["store_id"], parent["pos_id"], parent["notification_url"], parent["statement_descriptor"], parent["processing_mode"],
                        parent["point_type"], parent["point_unit"], parent["point_sub_unit"], parent["point_branch"], parent["point_source"], parent["point_state_id"], parent["payer_email"],
                        parent["payer_identification_type"], parent["payer_identification_number"], parent["card_first_six_digits"], parent["card_last_four_digits"], parent["cardholder_name"],
                        parent["cardholder_ident_type"], parent["cardholder_ident_number"], parent["date_created"], parent["date_approved"], parent["date_last_updated"], parent["money_release_date"], parent["raw_json"],
                        parent["payment_id"], parent["collector_id"], parent["payer_id"], parent["external_reference"], parent["order_id"], parent["order_type"], parent["status"], parent["status_detail"], parent["operation_type"],
                        parent["payment_method_id"], parent["payment_type_id"], parent["payment_method_type"], parent["issuer_id"], parent["currency_id"], parent["installments"], parent["transaction_amount"],
                        parent["transaction_amount_refunded"], parent["shipping_amount"], parent["shipping_cost"], parent["taxes_amount"], parent["coupon_amount"], parent["net_received_amount"],
                        parent["total_paid_amount"], parent["installment_amount"], parent["overpaid_amount"], parent["description"], parent["authorization_code"], parent["money_release_status"],
                        parent["binary_mode"], parent["captured"], parent["live_mode"], parent["store_id"], parent["pos_id"], parent["notification_url"], parent["statement_descriptor"], parent["processing_mode"],
                        parent["point_type"], parent["point_unit"], parent["point_sub_unit"], parent["point_branch"], parent["point_source"], parent["point_state_id"], parent["payer_email"],
                        parent["payer_identification_type"], parent["payer_identification_number"], parent["card_first_six_digits"], parent["card_last_four_digits"], parent["cardholder_name"],
                        parent["cardholder_ident_type"], parent["cardholder_ident_number"], parent["date_created"], parent["date_approved"], parent["date_last_updated"], parent["money_release_date"], parent["raw_json"],
                    )
                    rows_upserted += 1
                    cur.execute("DELETE FROM finance.mp_payment_charges WHERE payment_id = ?", parent["payment_id"])
                    cur.execute("DELETE FROM finance.mp_payment_items WHERE payment_id = ?", parent["payment_id"])
                    for ch in charges:
                        cur.execute(
                            """
                            INSERT INTO finance.mp_payment_charges (
                                payment_id, charge_id, charge_type, charge_name, account_from, account_to,
                                amount_original, amount_refunded, base_amount, rate, reserve_id, client_id,
                                tax_id, tax_status, mov_detail, mov_financial_entity, mov_type,
                                metadata_user_id, metadata_source, charge_date_created, charge_last_updated
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            ch["payment_id"], ch["charge_id"], ch["charge_type"], ch["charge_name"], ch["account_from"], ch["account_to"],
                            ch["amount_original"], ch["amount_refunded"], ch["base_amount"], ch["rate"], ch["reserve_id"], ch["client_id"],
                            ch["tax_id"], ch["tax_status"], ch["mov_detail"], ch["mov_financial_entity"], ch["mov_type"],
                            ch["metadata_user_id"], ch["metadata_source"], ch["charge_date_created"], ch["charge_last_updated"],
                        )
                        rows_children += 1
                    for item in items:
                        cur.execute(
                            "INSERT INTO finance.mp_payment_items (payment_id, item_id, category_id, title, description, quantity, unit_price, picture_url) VALUES (?,?,?,?,?,?,?,?)",
                            item["payment_id"], item["item_id"], item["category_id"], item["title"], item["description"], item["quantity"], item["unit_price"], item["picture_url"],
                        )
                        rows_children += 1
                conn.commit()
            self.db.update_sync_control(self.PROCESS_NAME, "SUCCESS", begin_date, end_date, success=True)
            self.db.finish_sync_run(sync_run_id, "SUCCESS", rows_read, rows_upserted, rows_children)
        except Exception as exc:
            logging.exception("Error Mercado Pago")
            self.db.update_sync_control(self.PROCESS_NAME, "ERROR", begin_date, end_date, str(exc), success=False)
            self.db.finish_sync_run(sync_run_id, "ERROR", rows_read, rows_upserted, rows_children, str(exc))
            raise


# =========================
# INTERBANKING
# =========================

class InterbankingSync:
    def __init__(self, db: Database, config: Config):
        if InterbankingClient is None:
            raise RuntimeError("No se pudo importar InterbankingClient. Copia este script dentro del proyecto donde ya funciona la conexión.")
        self.db = db
        self.client = InterbankingClient.from_env()
        self.config = config

    def _window(self, process_name: str) -> Tuple[str, str]:
        end_date = utcnow_naive()
        last = self.db.get_last_successful_sync(process_name)
        if last is None:
            begin_date = end_date - timedelta(days=self.config.ib_incremental_lookback_days)
        else:
            begin_date = last - timedelta(days=self.config.ib_incremental_lookback_days)
        return begin_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")

    def _upsert_accounts(self) -> Tuple[int, int]:
        process_name = "interbanking_accounts"
        sync_run_id = self.db.start_sync_run("INTERBANKING", process_name)
        self.db.update_sync_control(process_name, "RUNNING", None, None)
        rows_read = rows_upserted = 0
        try:
            accounts_df, _ = self.client.get_cuentas()
            rows_read = len(accounts_df)
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, row in accounts_df.iterrows():
                    r = {k: clean_value(v) for k, v in row.to_dict().items()}
                    raw_json = sanitize_to_json(r, source="ib")  # SEC-03
                    cur.execute(
                        """
                        MERGE finance.ib_accounts AS tgt
                        USING (SELECT ? AS account_cbu) AS src
                        ON tgt.account_cbu = src.account_cbu
                        WHEN MATCHED THEN UPDATE SET
                            bank_number=?, account_cuit=?, account_label=?, currency=?, bank_name=?, account_number=?, account_type=?, raw_json=?, updated_at=SYSUTCDATETIME()
                        WHEN NOT MATCHED THEN INSERT (account_cbu, bank_number, account_cuit, account_label, currency, bank_name, account_number, account_type, raw_json)
                        VALUES (?,?,?,?,?,?,?,?,?);
                        """,
                        r.get("account_cbu"),
                        r.get("bank_number"), r.get("account_cuit"), r.get("account_label"), r.get("currency"), r.get("bank_name"), str(r.get("account_number")) if r.get("account_number") is not None else None, r.get("account_type"), raw_json,
                        r.get("account_cbu"), r.get("bank_number"), r.get("account_cuit"), r.get("account_label"), r.get("currency"), r.get("bank_name"), str(r.get("account_number")) if r.get("account_number") is not None else None, r.get("account_type"), raw_json,
                    )
                    rows_upserted += 1
                conn.commit()
            self.db.update_sync_control(process_name, "SUCCESS", None, None, success=True)
            self.db.finish_sync_run(sync_run_id, "SUCCESS", rows_read, rows_upserted)
            return rows_read, rows_upserted
        except Exception as exc:
            self.db.update_sync_control(process_name, "ERROR", None, None, str(exc), success=False)
            self.db.finish_sync_run(sync_run_id, "ERROR", rows_read, rows_upserted, 0, str(exc))
            raise

    def _load_balances(self, begin_date: str, end_date: str) -> Tuple[int, int]:
        process_name = "interbanking_balances"
        sync_run_id = self.db.start_sync_run("INTERBANKING", process_name)
        self.db.update_sync_control(process_name, "RUNNING", parse_dt(begin_date), parse_dt(end_date))
        rows_read = rows_upserted = 0
        try:
            balances_df, _ = self.client.get_saldos(date_since=begin_date, date_until=end_date)
            rows_read = len(balances_df)
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, row in balances_df.iterrows():
                    r = {k: clean_value(v) for k, v in row.to_dict().items()}
                    balance_hash = sha256_of_parts(
                        r.get("bank_number"), r.get("account_number"), r.get("account_type"), r.get("currency"),
                        r.get("row_date"), r.get("operation_date"), r.get("is_historical"), r.get("day_balance"),
                    )
                    raw_json = sanitize_to_json(r, source="ib")  # SEC-03
                    cur.execute(
                        """
                        MERGE finance.ib_balances AS tgt
                        USING (SELECT ? AS balance_hash) AS src
                        ON tgt.balance_hash = src.balance_hash
                        WHEN MATCHED THEN UPDATE SET
                            bank_number=?, account_number=?, account_type=?, currency=?, account_label=?, account_name=?, row_date=?, message=?,
                            countable_balance=?, initial_operating_balance=?, current_operating_balance=?, projected_balance_24hs=?, projected_balance_48hs=?,
                            operation_date=?, day_balance=?, total_debits=?, total_credits=?, is_historical=?, raw_json=?
                        WHEN NOT MATCHED THEN INSERT (
                            balance_hash, bank_number, account_number, account_type, currency, account_label, account_name, row_date, message,
                            countable_balance, initial_operating_balance, current_operating_balance, projected_balance_24hs, projected_balance_48hs,
                            operation_date, day_balance, total_debits, total_credits, is_historical, raw_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                        """,
                        balance_hash,
                        r.get("bank_number"), str(r.get("account_number")) if r.get("account_number") is not None else None, r.get("account_type"), r.get("currency"), r.get("account_label"), r.get("account_name"), parse_dt(r.get("row_date")), r.get("message"),
                        r.get("countable_balance"), r.get("initial_operating_balance"), r.get("current_operating_balance"), r.get("projected_balance_24hs"), r.get("projected_balance_48hs"),
                        parse_dt(r.get("operation_date")), r.get("day_balance"), r.get("total_debits"), r.get("total_credits"), to_bool(r.get("is_historical")), raw_json,
                        balance_hash, r.get("bank_number"), str(r.get("account_number")) if r.get("account_number") is not None else None, r.get("account_type"), r.get("currency"), r.get("account_label"), r.get("account_name"), parse_dt(r.get("row_date")), r.get("message"),
                        r.get("countable_balance"), r.get("initial_operating_balance"), r.get("current_operating_balance"), r.get("projected_balance_24hs"), r.get("projected_balance_48hs"),
                        parse_dt(r.get("operation_date")), r.get("day_balance"), r.get("total_debits"), r.get("total_credits"), to_bool(r.get("is_historical")), raw_json,
                    )
                    rows_upserted += 1
                conn.commit()
            self.db.update_sync_control(process_name, "SUCCESS", parse_dt(begin_date), parse_dt(end_date), success=True)
            self.db.finish_sync_run(sync_run_id, "SUCCESS", rows_read, rows_upserted)
            return rows_read, rows_upserted
        except Exception as exc:
            self.db.update_sync_control(process_name, "ERROR", parse_dt(begin_date), parse_dt(end_date), str(exc), success=False)
            self.db.finish_sync_run(sync_run_id, "ERROR", rows_read, rows_upserted, 0, str(exc))
            raise

    def _iter_accounts(self) -> pd.DataFrame:
        accounts_df, _ = self.client.get_cuentas()
        return accounts_df

    def _load_movements_for_accounts(self, begin_date: str, end_date: str) -> Tuple[int, int]:
        process_name = "interbanking_movements"
        sync_run_id = self.db.start_sync_run("INTERBANKING", process_name)
        self.db.update_sync_control(process_name, "RUNNING", parse_dt(begin_date), parse_dt(end_date))
        rows_read = rows_upserted = 0
        try:
            accounts_df = self._iter_accounts()
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, acc in accounts_df.iterrows():
                    movements_df, _ = self.client.get_movimientos(
                        account_number=str(acc["account_number"]),
                        bank_number=acc["bank_number"],
                        date_since=begin_date,
                        date_until=end_date,
                        movement_type="anteriores",
                        version="v2",
                    )
                    rows_read += len(movements_df)
                    for _, row in movements_df.iterrows():
                        r = {k: clean_value(v) for k, v in row.to_dict().items()}
                        movement_hash = sha256_of_parts(
                            r.get("source_account"), r.get("voucher_number"), r.get("process_date"), r.get("amount"),
                            r.get("debit_credit_type"), r.get("operation_code_ib"), r.get("branch_office_activity"), r.get("correlative_number"),
                        )
                        raw_json = sanitize_to_json(r, source="ib")  # SEC-03
                        cur.execute(
                            """
                            MERGE finance.ib_movements AS tgt
                            USING (SELECT ? AS movement_hash) AS src
                            ON tgt.movement_hash = src.movement_hash
                            WHEN MATCHED THEN UPDATE SET
                                account_cbu=?, depositor_code=?, operation_code_ib=?, operation_code_bank=?, code_description_ib=?, customer_cuit=?,
                                depositor_description=?, code_description_bank=?, amount=?, voucher_number=?, grouping_code_ib=?, branch_office_activity=?,
                                process_date=?, debit_credit_type=?, movement_type=?, source_account=?, associated_voucher=?, real_date_activity=?,
                                movement_date=?, value_date=?, correlative_number=?, raw_json=?
                            WHEN NOT MATCHED THEN INSERT (
                                movement_hash, account_cbu, depositor_code, operation_code_ib, operation_code_bank, code_description_ib, customer_cuit,
                                depositor_description, code_description_bank, amount, voucher_number, grouping_code_ib, branch_office_activity,
                                process_date, debit_credit_type, movement_type, source_account, associated_voucher, real_date_activity,
                                movement_date, value_date, correlative_number, raw_json
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            movement_hash,
                            r.get("account_cbu"), r.get("depositor_code"), r.get("operation_code_ib"), r.get("operation_code_bank"), r.get("code_description_ib"), r.get("customer_cuit"),
                            r.get("depositor_description"), r.get("code_description_bank"), r.get("amount"), str(r.get("voucher_number")) if r.get("voucher_number") is not None else None,
                            r.get("grouping_code_ib"), str(r.get("branch_office_activity")) if r.get("branch_office_activity") is not None else None, parse_dt(r.get("process_date")),
                            r.get("debit_credit_type"), r.get("movement_type"), str(r.get("source_account")) if r.get("source_account") is not None else None,
                            str(r.get("associated_voucher")) if r.get("associated_voucher") is not None else None, parse_dt(r.get("real_date_activity")), parse_dt(r.get("movement_date")),
                            parse_dt(r.get("value_date")), str(r.get("correlative_number")) if r.get("correlative_number") is not None else None, raw_json,
                            movement_hash,
                            r.get("account_cbu"), r.get("depositor_code"), r.get("operation_code_ib"), r.get("operation_code_bank"), r.get("code_description_ib"), r.get("customer_cuit"),
                            r.get("depositor_description"), r.get("code_description_bank"), r.get("amount"), str(r.get("voucher_number")) if r.get("voucher_number") is not None else None,
                            r.get("grouping_code_ib"), str(r.get("branch_office_activity")) if r.get("branch_office_activity") is not None else None, parse_dt(r.get("process_date")),
                            r.get("debit_credit_type"), r.get("movement_type"), str(r.get("source_account")) if r.get("source_account") is not None else None,
                            str(r.get("associated_voucher")) if r.get("associated_voucher") is not None else None, parse_dt(r.get("real_date_activity")), parse_dt(r.get("movement_date")),
                            parse_dt(r.get("value_date")), str(r.get("correlative_number")) if r.get("correlative_number") is not None else None, raw_json,
                        )
                        rows_upserted += 1
                conn.commit()
            self.db.update_sync_control(process_name, "SUCCESS", parse_dt(begin_date), parse_dt(end_date), success=True)
            self.db.finish_sync_run(sync_run_id, "SUCCESS", rows_read, rows_upserted)
            return rows_read, rows_upserted
        except Exception as exc:
            self.db.update_sync_control(process_name, "ERROR", parse_dt(begin_date), parse_dt(end_date), str(exc), success=False)
            self.db.finish_sync_run(sync_run_id, "ERROR", rows_read, rows_upserted, 0, str(exc))
            raise

    def _load_transfers_and_vouchers(self, begin_date: str, end_date: str) -> Tuple[int, int]:
        process_name = "interbanking_transfers"
        sync_run_id = self.db.start_sync_run("INTERBANKING", process_name)
        self.db.update_sync_control(process_name, "RUNNING", parse_dt(begin_date), parse_dt(end_date))
        rows_read = rows_upserted = 0
        try:
            transfers_df, _ = self.client.get_transferencias_detalle(date_since=begin_date, date_until=end_date)
            rows_read = len(transfers_df)
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, row in transfers_df.iterrows():
                    r = {k: clean_value(v) for k, v in row.to_dict().items()}
                    raw_json = sanitize_to_json(r, source="ib")  # SEC-03
                    cur.execute(
                        """
                        MERGE finance.ib_transfers AS tgt
                        USING (SELECT ? AS transfer_id) AS src
                        ON tgt.transfer_id = src.transfer_id
                        WHEN MATCHED THEN UPDATE SET
                            transaction_number=?, request_date=?, transfer_type_code=?, transfer_type_description=?, account_label=?, amount=?, currency=?,
                            reference_number=?, lot_number=?, payment_number=?, status=?, client=?, statement_consolidated=?, unified_send=?, direct_import=?,
                            same_owner=?, internal_client_id=?, addenda=?, transfer_comments=?, credit_account_customer_cuit=?, credit_account_account_cbu=?,
                            credit_account_account_number=?, credit_account_currency=?, credit_account_account_type=?, credit_account_bank_number=?, credit_account_bank_name=?,
                            credit_account_account_label=?, debit_account_customer_cuit=?, debit_account_account_cbu=?, debit_account_account_number=?, debit_account_currency=?,
                            debit_account_account_type=?, debit_account_bank_number=?, debit_account_bank_name=?, debit_account_account_label=?, raw_json=?, updated_at=SYSUTCDATETIME()
                        WHEN NOT MATCHED THEN INSERT (
                            transfer_id, transaction_number, request_date, transfer_type_code, transfer_type_description, account_label, amount, currency,
                            reference_number, lot_number, payment_number, status, client, statement_consolidated, unified_send, direct_import,
                            same_owner, internal_client_id, addenda, transfer_comments, credit_account_customer_cuit, credit_account_account_cbu,
                            credit_account_account_number, credit_account_currency, credit_account_account_type, credit_account_bank_number, credit_account_bank_name,
                            credit_account_account_label, debit_account_customer_cuit, debit_account_account_cbu, debit_account_account_number, debit_account_currency,
                            debit_account_account_type, debit_account_bank_number, debit_account_bank_name, debit_account_account_label, raw_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                        """,
                        r.get("transfer_id"),
                        str(r.get("transaction_number")) if r.get("transaction_number") is not None else None, parse_dt(r.get("request_date")), r.get("transfer_type_code"), r.get("transfer_type_description"),
                        r.get("account_label"), r.get("amount"), r.get("currency"), str(r.get("reference_number")) if r.get("reference_number") is not None else None,
                        str(r.get("lot_number")) if r.get("lot_number") is not None else None, str(r.get("payment_number")) if r.get("payment_number") is not None else None,
                        r.get("status"), r.get("client"), r.get("statement_consolidated"), r.get("unified_send"), r.get("direct_import"), r.get("same_owner"),
                        r.get("internal_client_id"), r.get("addenda"), r.get("transfer_comments"), r.get("credit_account_customer_cuit"), r.get("credit_account_account_cbu"),
                        str(r.get("credit_account_account_number")) if r.get("credit_account_account_number") is not None else None, r.get("credit_account_currency"), r.get("credit_account_account_type"),
                        r.get("credit_account_bank_number"), r.get("credit_account_bank_name"), r.get("credit_account_account_label"), r.get("debit_account_customer_cuit"),
                        r.get("debit_account_account_cbu"), str(r.get("debit_account_account_number")) if r.get("debit_account_account_number") is not None else None,
                        r.get("debit_account_currency"), r.get("debit_account_account_type"), r.get("debit_account_bank_number"), r.get("debit_account_bank_name"),
                        r.get("debit_account_account_label"), raw_json,
                        r.get("transfer_id"), str(r.get("transaction_number")) if r.get("transaction_number") is not None else None, parse_dt(r.get("request_date")), r.get("transfer_type_code"), r.get("transfer_type_description"),
                        r.get("account_label"), r.get("amount"), r.get("currency"), str(r.get("reference_number")) if r.get("reference_number") is not None else None,
                        str(r.get("lot_number")) if r.get("lot_number") is not None else None, str(r.get("payment_number")) if r.get("payment_number") is not None else None,
                        r.get("status"), r.get("client"), r.get("statement_consolidated"), r.get("unified_send"), r.get("direct_import"), r.get("same_owner"),
                        r.get("internal_client_id"), r.get("addenda"), r.get("transfer_comments"), r.get("credit_account_customer_cuit"), r.get("credit_account_account_cbu"),
                        str(r.get("credit_account_account_number")) if r.get("credit_account_account_number") is not None else None, r.get("credit_account_currency"), r.get("credit_account_account_type"),
                        r.get("credit_account_bank_number"), r.get("credit_account_bank_name"), r.get("credit_account_account_label"), r.get("debit_account_customer_cuit"),
                        r.get("debit_account_account_cbu"), str(r.get("debit_account_account_number")) if r.get("debit_account_account_number") is not None else None,
                        r.get("debit_account_currency"), r.get("debit_account_account_type"), r.get("debit_account_bank_number"), r.get("debit_account_bank_name"),
                        r.get("debit_account_account_label"), raw_json,
                    )
                    rows_upserted += 1
                conn.commit()

            # vouchers, si el cliente ya tiene el método.
            if hasattr(self.client, "get_comprobantes"):
                voucher_process = "interbanking_vouchers"
                voucher_run_id = self.db.start_sync_run("INTERBANKING", voucher_process)
                self.db.update_sync_control(voucher_process, "RUNNING", parse_dt(begin_date), parse_dt(end_date))
                v_rows_read = v_rows_upserted = 0
                try:
                    vouchers_df, _ = self.client.get_comprobantes(date_since=begin_date, date_until=end_date)
                    v_rows_read = len(vouchers_df)
                    with self.db.connect() as conn:
                        cur = conn.cursor()
                        for _, row in vouchers_df.iterrows():
                            r = {k: clean_value(v) for k, v in row.to_dict().items()}
                            raw_json = sanitize_to_json(r, source="ib")  # SEC-03
                            cur.execute(
                                """
                                MERGE finance.ib_vouchers AS tgt
                                USING (SELECT ? AS transfer_id) AS src
                                ON tgt.transfer_id = src.transfer_id
                                WHEN MATCHED THEN UPDATE SET
                                    request_date=?, transfer_type_description=?, transfer_type_code=?, network_number=?, amount=?, currency=?, validation_code=?,
                                    total_amount=?, comments=?, billing_company=?, paying_customer=?, debit_account_customer_cuit=?, debit_account_account_cbu=?,
                                    debit_account_taxpayer_cuit=?, debit_account_bank_number=?, debit_account_bank_name=?, debit_account_account_label=?,
                                    afip_concept_description=?, afip_control_code=?, afip_nro_formulario=?, afip_tax_description=?, afip_fee_number=?, afip_pago_desc=?,
                                    afip_provider_name=?, afip_concept_code=?, afip_tax_code=?, afip_vep_number=?, afip_fiscal_period=?, afip_provider_code=?,
                                    credit_account_customer_cuit=?, credit_account_account_cbu=?, credit_account_bank_number=?, credit_account_bank_name=?, credit_account_account_label=?, raw_json=?
                                WHEN NOT MATCHED THEN INSERT (
                                    transfer_id, request_date, transfer_type_description, transfer_type_code, network_number, amount, currency, validation_code,
                                    total_amount, comments, billing_company, paying_customer, debit_account_customer_cuit, debit_account_account_cbu,
                                    debit_account_taxpayer_cuit, debit_account_bank_number, debit_account_bank_name, debit_account_account_label,
                                    afip_concept_description, afip_control_code, afip_nro_formulario, afip_tax_description, afip_fee_number, afip_pago_desc,
                                    afip_provider_name, afip_concept_code, afip_tax_code, afip_vep_number, afip_fiscal_period, afip_provider_code,
                                    credit_account_customer_cuit, credit_account_account_cbu, credit_account_bank_number, credit_account_bank_name, credit_account_account_label, raw_json
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                r.get("transfer_id"),
                                parse_dt(r.get("request_date")), r.get("transfer_type_description"), r.get("transfer_type_code"), str(r.get("network_number")) if r.get("network_number") is not None else None,
                                r.get("amount"), r.get("currency"), r.get("validation_code"), r.get("total_amount"), r.get("comments"), r.get("billing_company"), r.get("paying_customer"),
                                r.get("debit_account_customer_cuit"), r.get("debit_account_account_cbu"), r.get("debit_account_taxpayer_cuit"), r.get("debit_account_bank_number"),
                                r.get("debit_account_bank_name"), r.get("debit_account_account_label"), r.get("afip_concept_description"), str(r.get("afip_control_code")) if r.get("afip_control_code") is not None else None,
                                str(r.get("afip_nro_formulario")) if r.get("afip_nro_formulario") is not None else None, r.get("afip_tax_description"), str(r.get("afip_fee_number")) if r.get("afip_fee_number") is not None else None,
                                r.get("afip_pago_desc"), r.get("afip_provider_name"), str(r.get("afip_concept_code")) if r.get("afip_concept_code") is not None else None,
                                str(r.get("afip_tax_code")) if r.get("afip_tax_code") is not None else None, str(r.get("afip_vep_number")) if r.get("afip_vep_number") is not None else None,
                                str(r.get("afip_fiscal_period")) if r.get("afip_fiscal_period") is not None else None, str(r.get("afip_provider_code")) if r.get("afip_provider_code") is not None else None,
                                r.get("credit_account_customer_cuit"), r.get("credit_account_account_cbu"), r.get("credit_account_bank_number"), r.get("credit_account_bank_name"), r.get("credit_account_account_label"), raw_json,
                                r.get("transfer_id"), parse_dt(r.get("request_date")), r.get("transfer_type_description"), r.get("transfer_type_code"), str(r.get("network_number")) if r.get("network_number") is not None else None,
                                r.get("amount"), r.get("currency"), r.get("validation_code"), r.get("total_amount"), r.get("comments"), r.get("billing_company"), r.get("paying_customer"),
                                r.get("debit_account_customer_cuit"), r.get("debit_account_account_cbu"), r.get("debit_account_taxpayer_cuit"), r.get("debit_account_bank_number"),
                                r.get("debit_account_bank_name"), r.get("debit_account_account_label"), r.get("afip_concept_description"), str(r.get("afip_control_code")) if r.get("afip_control_code") is not None else None,
                                str(r.get("afip_nro_formulario")) if r.get("afip_nro_formulario") is not None else None, r.get("afip_tax_description"), str(r.get("afip_fee_number")) if r.get("afip_fee_number") is not None else None,
                                r.get("afip_pago_desc"), r.get("afip_provider_name"), str(r.get("afip_concept_code")) if r.get("afip_concept_code") is not None else None,
                                str(r.get("afip_tax_code")) if r.get("afip_tax_code") is not None else None, str(r.get("afip_vep_number")) if r.get("afip_vep_number") is not None else None,
                                str(r.get("afip_fiscal_period")) if r.get("afip_fiscal_period") is not None else None, str(r.get("afip_provider_code")) if r.get("afip_provider_code") is not None else None,
                                r.get("credit_account_customer_cuit"), r.get("credit_account_account_cbu"), r.get("credit_account_bank_number"), r.get("credit_account_bank_name"), r.get("credit_account_account_label"), raw_json,
                            )
                            v_rows_upserted += 1
                        conn.commit()
                    self.db.update_sync_control(voucher_process, "SUCCESS", parse_dt(begin_date), parse_dt(end_date), success=True)
                    self.db.finish_sync_run(voucher_run_id, "SUCCESS", v_rows_read, v_rows_upserted)
                except Exception as voucher_exc:
                    self.db.update_sync_control(voucher_process, "ERROR", parse_dt(begin_date), parse_dt(end_date), str(voucher_exc), success=False)
                    self.db.finish_sync_run(voucher_run_id, "ERROR", v_rows_read, v_rows_upserted, 0, str(voucher_exc))
                    raise

            self.db.update_sync_control(process_name, "SUCCESS", parse_dt(begin_date), parse_dt(end_date), success=True)
            self.db.finish_sync_run(sync_run_id, "SUCCESS", rows_read, rows_upserted)
            return rows_read, rows_upserted
        except Exception as exc:
            self.db.update_sync_control(process_name, "ERROR", parse_dt(begin_date), parse_dt(end_date), str(exc), success=False)
            self.db.finish_sync_run(sync_run_id, "ERROR", rows_read, rows_upserted, 0, str(exc))
            raise

    def _load_extracts_for_accounts(self, begin_date: str, end_date: str) -> Tuple[int, int]:
        process_name = "interbanking_extracts"
        sync_run_id = self.db.start_sync_run("INTERBANKING", process_name)
        self.db.update_sync_control(process_name, "RUNNING", parse_dt(begin_date), parse_dt(end_date))
        rows_read = rows_upserted = 0
        try:
            accounts_df = self._iter_accounts()
            with self.db.connect() as conn:
                cur = conn.cursor()
                for _, acc in accounts_df.iterrows():
                    extracts_df, _ = self.client.get_extractos(
                        account_number=str(acc["account_number"]),
                        bank_number=acc["bank_number"],
                        date_since=begin_date,
                        date_until=end_date,
                    )
                    rows_read += len(extracts_df)
                    for _, row in extracts_df.iterrows():
                        r = {k: clean_value(v) for k, v in row.to_dict().items()}
                        extract_hash = sha256_of_parts(
                            r.get("statement_number"), r.get("source_account"), r.get("voucher_number"), r.get("process_date"),
                            r.get("amount"), r.get("debit_credit_type"), r.get("correlative_number"),
                        )
                        raw_json = sanitize_to_json(r, source="ib")  # SEC-03
                        cur.execute(
                            """
                            MERGE finance.ib_extracts AS tgt
                            USING (SELECT ? AS extract_hash) AS src
                            ON tgt.extract_hash = src.extract_hash
                            WHEN MATCHED THEN UPDATE SET
                                statement_number=?, operation_date=?, total_movements=?, opening_balance=?, ending_balance=?, operation_code_ib=?,
                                operation_code_bank=?, code_description_ib=?, customer_cuit=?, depositor_description=?, code_description_bank=?,
                                movement_date=?, real_date_activity=?, amount=?, voucher_number=?, branch_office_activity=?, process_date=?, value_date=?,
                                debit_credit_type=?, correlative_number=?, source_account=?, code_description_standard=?, operation_code_bank_standard=?, raw_json=?
                            WHEN NOT MATCHED THEN INSERT (
                                extract_hash, statement_number, operation_date, total_movements, opening_balance, ending_balance, operation_code_ib,
                                operation_code_bank, code_description_ib, customer_cuit, depositor_description, code_description_bank, movement_date,
                                real_date_activity, amount, voucher_number, branch_office_activity, process_date, value_date, debit_credit_type,
                                correlative_number, source_account, code_description_standard, operation_code_bank_standard, raw_json
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            extract_hash,
                            str(r.get("statement_number")) if r.get("statement_number") is not None else None, parse_dt(r.get("operation_date")), r.get("total_movements"),
                            r.get("opening_balance"), r.get("ending_balance"), r.get("operation_code_ib"), r.get("operation_code_bank"), r.get("code_description_ib"),
                            r.get("customer_cuit"), r.get("depositor_description"), r.get("code_description_bank"), parse_dt(r.get("movement_date")), parse_dt(r.get("real_date_activity")),
                            r.get("amount"), str(r.get("voucher_number")) if r.get("voucher_number") is not None else None, str(r.get("branch_office_activity")) if r.get("branch_office_activity") is not None else None,
                            parse_dt(r.get("process_date")), parse_dt(r.get("value_date")), r.get("debit_credit_type"), str(r.get("correlative_number")) if r.get("correlative_number") is not None else None,
                            str(r.get("source_account")) if r.get("source_account") is not None else None, r.get("code_description_standard"), r.get("operation_code_bank_standard"), raw_json,
                            extract_hash,
                            str(r.get("statement_number")) if r.get("statement_number") is not None else None, parse_dt(r.get("operation_date")), r.get("total_movements"),
                            r.get("opening_balance"), r.get("ending_balance"), r.get("operation_code_ib"), r.get("operation_code_bank"), r.get("code_description_ib"),
                            r.get("customer_cuit"), r.get("depositor_description"), r.get("code_description_bank"), parse_dt(r.get("movement_date")), parse_dt(r.get("real_date_activity")),
                            r.get("amount"), str(r.get("voucher_number")) if r.get("voucher_number") is not None else None, str(r.get("branch_office_activity")) if r.get("branch_office_activity") is not None else None,
                            parse_dt(r.get("process_date")), parse_dt(r.get("value_date")), r.get("debit_credit_type"), str(r.get("correlative_number")) if r.get("correlative_number") is not None else None,
                            str(r.get("source_account")) if r.get("source_account") is not None else None, r.get("code_description_standard"), r.get("operation_code_bank_standard"), raw_json,
                        )
                        rows_upserted += 1
                conn.commit()
            self.db.update_sync_control(process_name, "SUCCESS", parse_dt(begin_date), parse_dt(end_date), success=True)
            self.db.finish_sync_run(sync_run_id, "SUCCESS", rows_read, rows_upserted)
            return rows_read, rows_upserted
        except Exception as exc:
            self.db.update_sync_control(process_name, "ERROR", parse_dt(begin_date), parse_dt(end_date), str(exc), success=False)
            self.db.finish_sync_run(sync_run_id, "ERROR", rows_read, rows_upserted, 0, str(exc))
            raise

    def run(self) -> None:
        self._upsert_accounts()
        begin_date, end_date = self._window("interbanking_balances")
        self._load_balances(begin_date, end_date)
        begin_date, end_date = self._window("interbanking_movements")
        self._load_movements_for_accounts(begin_date, end_date)
        begin_date, end_date = self._window("interbanking_transfers")
        self._load_transfers_and_vouchers(begin_date, end_date)
        begin_date, end_date = self._window("interbanking_extracts")
        self._load_extracts_for_accounts(begin_date, end_date)


# =========================
# APP
# =========================

class UnifiedFinanceSyncApp:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.sql_connection_string)
        self.mp_sync = MercadoPagoSync(self.db, MercadoPagoClient(config.mp_access_token), config)
        self.ib_sync = InterbankingSync(self.db, config)

    def run_once(self, mp_initial_load: bool = False) -> None:
        logging.info("Iniciando sincronización integrada")
        self.mp_sync.run(initial_load=mp_initial_load)
        self.ib_sync.run()
        logging.info("Sincronización integrada finalizada")

    def run_forever(self, mp_initial_load: bool = False) -> None:
        first_cycle = True
        while True:
            try:
                self.run_once(mp_initial_load=mp_initial_load and first_cycle)
            except Exception:
                logging.exception("Falló un ciclo de sincronización")
            first_cycle = False
            time.sleep(self.config.poll_interval_seconds)


def main(argv: List[str]) -> int:
    config = Config.from_env()
    configure_logging(config.log_level)
    mode = argv[1] if len(argv) > 1 else "daemon"
    app = UnifiedFinanceSyncApp(config)
    if mode == "run-once":
        app.run_once(mp_initial_load=os.getenv("MP_INITIAL_LOAD", "false").lower() == "true")
    else:
        app.run_forever(mp_initial_load=os.getenv("MP_INITIAL_LOAD", "false").lower() == "true")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
