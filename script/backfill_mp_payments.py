#!/usr/bin/env python3
"""Backfill one-time de pagos Mercado Pago a finance.mp_payments.

Trae TODOS los pagos creados en [begin, end] usando
`MercadoPagoClient.iter_all_payments` (que esquiva el cap de offset 10_000 de MP
partiendo el rango por fecha) y los persiste con `upsert_payment` DIRECTO desde el
resultado del search, sin re-hidratar via GET /payments/{id}.

Por qué un script aparte y no el poller: el poller corre en una Azure Function con
functionTimeout de 10 min; ~95k pagos no entran. Esto corre local, sin límite de
tiempo, y escribe directo a SQL. Es idempotente (upsert_payment skipea por
date_last_updated), así que se puede re-correr sin duplicar.

Credenciales por env var (NUNCA se imprimen):
    MP_CLIENT_ID, MP_CLIENT_SECRET  -> OAuth client_credentials de la app MP.
    MP_SQL_CONN                     -> connection string de Azure SQL (pyodbc).

Uso (inyectando desde Key Vault, sin que el secreto quede en el comando):
    MP_CLIENT_ID=$(az keyvault secret show --vault-name kv-rapanui-finance-prod --name MP-CLIENT-ID --query value -o tsv) \
    MP_CLIENT_SECRET=$(az keyvault secret show --vault-name kv-rapanui-finance-prod --name MP-CLIENT-SECRET --query value -o tsv) \
    MP_SQL_CONN=$(az keyvault secret show --vault-name kv-rapanui-finance-prod --name SQL-CONNECTION-STRING --query value -o tsv) \
    python script/backfill_mp_payments.py --begin 2026-05-01 [--end 2026-06-01] [--inspect]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# El script vive en script/; agregamos repo root (para `shared`) y la carpeta de la
# Function MP (para `mp_client` / `mp_processor`) al path.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "mp_webhook_function"))

import pyodbc  # noqa: E402
from dateutil import parser as dtparser  # noqa: E402

# Windows local: el bundle de certifi suele no tener el root CA corporativo, y el
# POST /oauth/token de MP falla con SSLError. truststore usa el almacén de
# certificados del SO (mismo patrón que el cliente de referencia jsontoexcel). En
# Azure/Linux no hace falta; si no está instalado, seguimos sin romper.
try:
    import truststore  # noqa: E402

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover
    pass

from shared.secret_string import SecretString  # noqa: E402
from mp_client import MercadoPagoClient  # noqa: E402
from mp_processor import upsert_payment  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_mp")


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Falta la env var {name} (inyectala desde Key Vault, ver docstring)")
    return val


def _parse_day(s: str) -> datetime:
    d = dtparser.parse(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _inspect(client: MercadoPagoClient, begin: datetime, end: datetime) -> None:
    """Trae 1 resultado y reporta total + presencia de estructuras anidadas.

    Sirve para confirmar que el search devuelve el objeto completo que necesita
    upsert_payment (transaction_details / point_of_interaction / charges_details /
    additional_info.items). Si charges/items vienen recortados, los rows quedan con
    el parent completo y children vacíos (aceptable; el poller/webhook los completa
    cuando el pago se actualiza).
    """
    resp = client.search_payments(
        begin_date=begin, end_date=end, limit=1, offset=0,
        range_field="date_created", sort="date_created", criteria="asc",
    )
    total = (resp.get("paging") or {}).get("total")
    results = resp.get("results") or []
    logger.info("paging.total reportado para la ventana: %s", total)
    if not results:
        logger.info("sin resultados en la ventana")
        return
    p = results[0]
    logger.info("ejemplo: id=%s date_created=%s status=%s",
                p.get("id"), p.get("date_created"), p.get("status"))
    for key in ("transaction_details", "point_of_interaction", "charges_details", "additional_info"):
        val = p.get(key)
        logger.info("  '%s' presente: %s", key, val not in (None, {}, []))
    addl = p.get("additional_info")
    items = addl.get("items") if isinstance(addl, dict) else None
    logger.info("  additional_info.items: %s", "si" if items else "no/vacio")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill de pagos MP a finance.mp_payments")
    ap.add_argument("--begin", default="2026-05-01", help="fecha inicio UTC (default 2026-05-01)")
    ap.add_argument("--end", default=None, help="fecha fin UTC (default: ahora)")
    ap.add_argument("--batch", type=int, default=200, help="commit cada N pagos (default 200)")
    ap.add_argument("--page-delay-ms", type=int, default=150, help="delay entre páginas del search")
    ap.add_argument("--inspect", action="store_true", help="solo inspeccionar 1 resultado y salir")
    args = ap.parse_args()

    begin = _parse_day(args.begin)
    end = _parse_day(args.end) if args.end else datetime.now(timezone.utc)

    client = MercadoPagoClient(
        client_id=SecretString(_env("MP_CLIENT_ID")),
        client_secret=SecretString(_env("MP_CLIENT_SECRET")),
    )

    logger.info("backfill MP: range=date_created ventana [%s -> %s]", begin.isoformat(), end.isoformat())
    _inspect(client, begin, end)
    if args.inspect:
        return

    conn = pyodbc.connect(_env("MP_SQL_CONN"), autocommit=False)
    processed = skipped = charges = items = errors = 0
    pending = 0
    try:
        for payment in client.iter_all_payments(
            begin=begin, end=end, range_field="date_created",
            page_delay_seconds=args.page_delay_ms / 1000.0,
        ):
            try:
                res = upsert_payment(conn, payment)
                processed += 1
                pending += 1
                if res.skipped_idempotent:
                    skipped += 1
                charges += res.charges_upserted
                items += res.items_upserted
            except pyodbc.Error:
                conn.rollback()
                pending = 0
                errors += 1
                logger.exception("upsert falló para payment id=%s; continúo", payment.get("id"))
                continue
            if pending >= args.batch:
                conn.commit()
                pending = 0
                logger.info("progreso: procesados=%d (skip=%d) charges=%d items=%d errores=%d",
                            processed, skipped, charges, items, errors)
        conn.commit()
    finally:
        conn.close()

    logger.info("BACKFILL COMPLETO: procesados=%d skip_idempotente=%d charges=%d items=%d errores=%d",
                processed, skipped, charges, items, errors)


if __name__ == "__main__":
    main()
