"""Procesamiento de un Mercado Pago payment → SQL Server.

Responsabilidades:
    1. Transformar el payload crudo MP en 3 rows normalizadas (parent + charges + items).
    2. UPSERT idempotente vía shared.db_helpers.execute_upsert (CAL-02).
    3. Sanitizar PII del raw_json antes de persistir (SEC-03, heredado de shared).
    4. Verificación de idempotencia previa (AZ-03): si el payment_id ya existe
       con el mismo date_last_updated, skip.

Separado de la lógica HTTP (function_app.py) para poder testearlo unitariamente.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pyodbc
from dateutil import parser as dtparser

from shared.db_helpers import execute_upsert, sanitize_to_json, to_str

logger = logging.getLogger(__name__)


# =====================================================================
# Helpers locales (sin duplicar con sync_service: son primitivas puras).
# =====================================================================

def safe_get(d: Dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return dtparser.parse(str(value)).replace(tzinfo=None)
    except (ValueError, TypeError) as exc:
        logger.warning("parse_dt: valor no parseable %r (%s)", value, exc)
        return None


# =====================================================================
# Transform
# =====================================================================

def transform_payment(payment: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Convierte un payment MP en (parent_row, charges_rows, items_rows).

    Raises:
        KeyError: si el payment no tiene 'id'. Otros campos faltantes resultan
                  en valor None (persistimos lo que haya).
    """
    payment_id = payment["id"]
    parent: Dict[str, Any] = {
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
        # SEC-03: raw_json con PII redactada. Columnas dedicadas siguen guardando los valores.
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


# =====================================================================
# Persistencia — CAL-02 aplicado vía execute_upsert
# =====================================================================

# Columnas de actualización para mp_payments (sin la PK `payment_id`).
_MP_PAYMENTS_UPDATE_COLS: Tuple[str, ...] = (
    "collector_id", "payer_id", "external_reference", "order_id", "order_type",
    "status", "status_detail", "operation_type",
    "payment_method_id", "payment_type_id", "payment_method_type",
    "issuer_id", "currency_id", "installments",
    "transaction_amount", "transaction_amount_refunded",
    "shipping_amount", "shipping_cost", "taxes_amount", "coupon_amount",
    "net_received_amount", "total_paid_amount", "installment_amount", "overpaid_amount",
    "description", "authorization_code", "money_release_status",
    "binary_mode", "captured", "live_mode",
    "store_id", "pos_id", "notification_url", "statement_descriptor", "processing_mode",
    "point_type", "point_unit", "point_sub_unit", "point_branch", "point_source", "point_state_id",
    "payer_email", "payer_identification_type", "payer_identification_number",
    "card_first_six_digits", "card_last_four_digits",
    "cardholder_name", "cardholder_ident_type", "cardholder_ident_number",
    "date_created", "date_approved", "date_last_updated", "money_release_date",
    "raw_json",
)


@dataclass
class UpsertResult:
    payment_id: Any
    skipped_idempotent: bool
    charges_upserted: int
    items_upserted: int


def _is_already_current(cur: pyodbc.Cursor, payment_id: Any, date_last_updated: Optional[datetime]) -> bool:
    """AZ-03 — idempotencia: skip solo si ya tenemos EXACTAMENTE este date_last_updated.

    Antes usaba `>=`, que skipea cuando lo almacenado es más nuevo que lo entrante.
    Eso es peligroso ante delivery fuera de orden: si llega un webhook con un
    cambio legítimo en otros campos pero su date_last_updated quedó ligeramente
    por debajo del que ya guardamos, perdemos la actualización.

    `==` es más conservador: cualquier diferencia dispara el MERGE. El MERGE en
    SQL es idempotente y barato si no hay cambios reales; la red de seguridad
    contra duplicados queda en la DB.
    """
    if date_last_updated is None:
        return False
    try:
        row = cur.execute(
            "SELECT date_last_updated FROM finance.mp_payments WHERE payment_id = ?",
            payment_id,
        ).fetchone()
    except pyodbc.Error as exc:
        logger.warning("_is_already_current: query falló (%s); procedemos con upsert", exc)
        return False
    if not row or row[0] is None:
        return False
    existing = row[0]
    if isinstance(existing, datetime):
        return existing.replace(tzinfo=None) == date_last_updated
    return False


def upsert_payment(conn: pyodbc.Connection, payment: Dict[str, Any]) -> UpsertResult:
    """Persiste un payment completo (parent + charges + items) transaccionalmente.

    Args:
        conn: conexión pyodbc ABIERTA y con autocommit=False.
        payment: dict tal como vino de GET /v1/payments/{id}.

    Returns:
        UpsertResult con contadores y flag de idempotencia.

    Raises:
        pyodbc.Error: se propagan para que el caller decida retry/log.
    """
    parent, charges, items = transform_payment(payment)
    payment_id = parent["payment_id"]

    cur = conn.cursor()

    if _is_already_current(cur, payment_id, parent["date_last_updated"]):
        logger.info("mp_payment %s: skip (idempotente, date_last_updated sin cambios)", payment_id)
        return UpsertResult(payment_id=payment_id, skipped_idempotent=True, charges_upserted=0, items_upserted=0)

    # Parent upsert (CAL-02).
    execute_upsert(
        cur,
        table="finance.mp_payments",
        key_cols=("payment_id",),
        update_cols=_MP_PAYMENTS_UPDATE_COLS,
        row=parent,
    )

    # Children: reemplazo completo por simplicidad (MP no envía deltas de charges/items).
    cur.execute("DELETE FROM finance.mp_payment_charges WHERE payment_id = ?", payment_id)
    cur.execute("DELETE FROM finance.mp_payment_items   WHERE payment_id = ?", payment_id)

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
            ch["payment_id"], to_str(ch["charge_id"]), ch["charge_type"], ch["charge_name"],
            ch["account_from"], ch["account_to"],
            ch["amount_original"], ch["amount_refunded"], ch["base_amount"], ch["rate"],
            ch["reserve_id"], ch["client_id"],
            ch["tax_id"], ch["tax_status"], ch["mov_detail"], ch["mov_financial_entity"], ch["mov_type"],
            ch["metadata_user_id"], ch["metadata_source"],
            ch["charge_date_created"], ch["charge_last_updated"],
        )

    for item in items:
        cur.execute(
            """
            INSERT INTO finance.mp_payment_items (
                payment_id, item_id, category_id, title, description, quantity, unit_price, picture_url
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            item["payment_id"], to_str(item["item_id"]), to_str(item["category_id"]),
            item["title"], item["description"], item["quantity"], item["unit_price"], item["picture_url"],
        )

    return UpsertResult(
        payment_id=payment_id,
        skipped_idempotent=False,
        charges_upserted=len(charges),
        items_upserted=len(items),
    )
