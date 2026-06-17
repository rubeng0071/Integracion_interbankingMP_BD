"""CAL-02 + CAL-03 + SEC-03 — Helpers de base de datos y sanitización.

Resuelve tres problemas del código actual:

CAL-02 — `execute_upsert(cur, table, key_cols, all_cols, values)`:
    Hoy hay 6 bloques MERGE casi idénticos en unified_finance_sync_service.py
    (mp_payments, ib_accounts, ib_balances, ib_movements, ib_transfers,
    ib_vouchers, ib_extracts). Cada uno repite la lista de columnas dos veces
    (UPDATE + INSERT) y la lista de placeholders. El helper genera el MERGE
    parametrizado a partir de la lista de columnas.

CAL-03 — `to_str(value)`:
    Hoy hay 50+ ocurrencias de:
        str(r.get("voucher_number")) if r.get("voucher_number") is not None else None
    Reemplazado por: to_str(r.get("voucher_number"))

SEC-03 — `sanitize_for_storage(payload, source)`:
    Antes de persistir el raw_json, eliminar campos PII sensibles que no
    necesitamos retener: card.first_six_digits, card.last_four_digits,
    cardholder data, payer.email, payer.identification.number, etc.
    El campo en la columna sigue existiendo (legalmente lo necesitamos para
    conciliación), pero el dump completo del payload NO debe replicarlos.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# =====================================================================
# CAL-03 — to_str
# =====================================================================

def to_str(value: Any) -> Optional[str]:
    """Convierte value a str si no es None/'' ; devuelve None en caso contrario.

    Reemplaza el patrón verboso:
        str(r.get("x")) if r.get("x") is not None else None
    por:
        to_str(r.get("x"))
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    try:
        text = str(value).strip()
    except Exception as exc:
        logger.warning("to_str: no se pudo convertir %r (%s)", value, exc)
        return None
    return text if text else None


# =====================================================================
# CAL-02 — execute_upsert
# =====================================================================

def build_merge_sql(
    table: str,
    key_cols: Sequence[str],
    update_cols: Sequence[str],
    insert_cols: Sequence[str],
    extra_set: Optional[str] = None,
) -> str:
    """Construye una sentencia MERGE parametrizada para SQL Server.

    Args:
        table: Nombre completo de la tabla, ej: "finance.mp_payments".
        key_cols: Columnas de la condición ON (típicamente la PK natural).
        update_cols: Columnas a actualizar en el WHEN MATCHED.
        insert_cols: Columnas a insertar en el WHEN NOT MATCHED. Suelen ser
                     key_cols + update_cols (en este orden).
        extra_set: SQL adicional para el SET, ej: "updated_at=SYSUTCDATETIME()".

    Orden de parámetros esperado en execute_upsert:
        1. key_cols (para el USING)
        2. update_cols (para el SET del UPDATE)
        3. insert_cols (para los VALUES del INSERT)
    """
    using_select = ", ".join(f"? AS {c}" for c in key_cols)
    on_clause = " AND ".join(f"tgt.{c} = src.{c}" for c in key_cols)
    set_clause = ", ".join(f"{c}=?" for c in update_cols)
    if extra_set:
        set_clause += f", {extra_set}"
    insert_cols_sql = ", ".join(insert_cols)
    placeholders = ",".join(["?"] * len(insert_cols))

    return (
        f"MERGE {table} AS tgt\n"
        f"USING (SELECT {using_select}) AS src\n"
        f"ON {on_clause}\n"
        f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols_sql})\n"
        f"VALUES ({placeholders});"
    )


def execute_upsert_batch(
    cur: Any,
    table: str,
    key_cols: Sequence[str],
    update_cols: Sequence[str],
    rows: Sequence[Dict[str, Any]],
    extra_set: Optional[str] = "updated_at=SYSUTCDATETIME()",
) -> int:
    """UPSERT bulk vía staging table: 3 roundtrips para N filas.

    Pensado para tablas con volumen alto por ciclo (movements, extracts).
    Para una sola fila o lotes <10 conviene execute_upsert() individual:
    el overhead del CREATE/DROP de la temp table no se amortiza.

    Pasos:
        1. Crear #stg_<table> clonando solo las columnas relevantes
           (SELECT TOP 0 ... INTO ...). Copia los tipos pero ignora
           constraints/identity de la tabla destino.
        2. executemany() con fast_executemany=True para bulk insert al stg.
        3. MERGE stg → destino en una sola sentencia, luego DROP del stg.

    El stg vive en la sesión actual; con conexión persistente (refactor
    A) y una llamada que falle en (2), el DROP queda pendiente. Por eso
    usamos un nombre derivado de la tabla destino y lo recreamos en (1)
    con DROP-IF-EXISTS implícito vía DROP TABLE en el finally.

    Args:
        cur: pyodbc.Cursor activo (transacción manejada por el caller).
        table: Tabla destino calificada con schema (ej: "finance.ib_movements").
        key_cols: PK natural para el ON del MERGE.
        update_cols: Columnas a actualizar/insertar (sin contar key_cols).
        rows: Lista de dicts con TODAS las columnas key_cols + update_cols.
        extra_set: SQL extra para el SET (por default updated_at).

    Returns:
        Cantidad de filas procesadas (== len(rows)).

    Raises:
        KeyError: si algún row no tiene alguna columna esperada.
        pyodbc.Error: se propaga; el caller decide rollback.
    """
    if not rows:
        return 0

    # Deduplicar por key_cols. Si el batch contiene dos filas con la misma key
    # natural (por ejemplo dos movements IB con mismo hash porque sus campos
    # source_account/voucher_number/process_date/amount/... coinciden), el MERGE
    # bulk tira UNIQUE KEY violation: hace WHEN NOT MATCHED → INSERT para ambas.
    # Solución: nos quedamos con la última ocurrencia (asume que la más reciente
    # tiene datos más actualizados; en el peor caso, son idénticas).
    seen_keys: set = set()
    deduped: List[Dict[str, Any]] = []
    for row in reversed(list(rows)):
        try:
            key_tuple = tuple(row[c] for c in key_cols)
        except KeyError:
            # Si falta una key, dejamos pasar y que falle más abajo con mensaje claro.
            deduped.append(row)
            continue
        if key_tuple in seen_keys:
            continue
        seen_keys.add(key_tuple)
        deduped.append(row)
    deduped.reverse()
    if len(deduped) < len(rows):
        logger.info(
            "execute_upsert_batch(%s): dedup %d → %d (key_cols=%s)",
            table, len(rows), len(deduped), key_cols,
        )
    rows = deduped

    insert_cols = list(key_cols) + list(update_cols)
    col_list = ", ".join(insert_cols)
    placeholders = ", ".join(["?"] * len(insert_cols))

    # Nombre de stg derivado de la tabla destino para evitar choques entre
    # sub-procesos que compartan conexión (refactor A).
    table_short = table.split(".")[-1]
    stg = f"#stg_{table_short}"

    try:
        # Asegurar que no quede un stg residual de una corrida previa fallida.
        cur.execute(
            f"IF OBJECT_ID('tempdb..{stg}') IS NOT NULL DROP TABLE {stg};"
        )
        # 1. Clonar schema (solo las columnas que vamos a usar).
        cur.execute(f"SELECT TOP 0 {col_list} INTO {stg} FROM {table};")

        # 2. Bulk insert.
        try:
            params: List[Tuple[Any, ...]] = []
            for r in rows:
                params.append(tuple(_coerce_value(r[c]) for c in insert_cols))
        except KeyError as exc:
            raise KeyError(f"execute_upsert_batch({table}): falta {exc} en algun row") from exc

        # fast_executemany acelera 10-100x el bulk insert vía pyodbc, PERO
        # decide el buffer NVARCHAR a partir del primer valor que ve por columna.
        # Si después llega un valor más largo, el driver tira "right truncation".
        # Tomamos el max len de string visto en TODO el batch y le decimos a pyodbc
        # con setinputsizes que use NVARCHAR de ese tamaño (o MAX si > 4000).
        original_fast = getattr(cur, "fast_executemany", False)
        cur.fast_executemany = True
        try:
            try:
                import pyodbc as _pyodbc  # noqa: WPS433  import local controlado
                sql_wvarchar = _pyodbc.SQL_WVARCHAR
            except ImportError:
                sql_wvarchar = None

            # Detectar si alguna columna tiene strings > 250 chars. Si los hay,
            # fast_executemany trunca (usa el bind del primer valor del batch).
            # En ese caso desactivamos fast_executemany: pyodbc maneja cada row
            # individualmente respetando el schema SQL real (NVARCHAR(MAX) acepta
            # strings de cualquier tamaño, DECIMAL convierte string->Decimal, etc).
            # Penalización de performance: ~5x, manejable para volúmenes IB diarios.
            has_long_strings = False
            for row_params in params:
                for v in row_params:
                    if isinstance(v, str) and len(v) > 250:
                        has_long_strings = True
                        break
                if has_long_strings:
                    break
            if has_long_strings:
                cur.fast_executemany = False

            cur.executemany(
                f"INSERT INTO {stg} ({col_list}) VALUES ({placeholders})",
                params,
            )
        finally:
            cur.fast_executemany = original_fast

        # 3. MERGE stg → tabla destino.
        on_clause = " AND ".join(f"tgt.{c} = src.{c}" for c in key_cols)
        set_clause = ", ".join(f"{c}=src.{c}" for c in update_cols)
        if extra_set:
            set_clause += f", {extra_set}"
        insert_values_clause = ", ".join(f"src.{c}" for c in insert_cols)
        merge_sql = (
            f"MERGE {table} AS tgt "
            f"USING {stg} AS src "
            f"ON {on_clause} "
            f"WHEN MATCHED THEN UPDATE SET {set_clause} "
            f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({insert_values_clause});"
        )
        cur.execute(merge_sql)
    finally:
        # Limpiamos el stg aunque haya error; en una conexión persistente,
        # el siguiente upsert puede chocar contra un stg residual.
        try:
            cur.execute(f"IF OBJECT_ID('tempdb..{stg}') IS NOT NULL DROP TABLE {stg};")
        except Exception:  # pragma: no cover  defensivo
            pass

    return len(rows)


def _coerce_value(value: Any) -> Any:
    """Convierte tipos no soportados por pyodbc a algo que el driver acepte.

    Casos cubiertos:
        - dict / list: se serializan a JSON string. Pasa con `addenda`, `metadata`,
          subcuentas que IB devuelve anidadas y no se aplanaron antes.
        - bool: pyodbc soporta bool en SQL Server (BIT), no necesita conversión.
        - El resto (None, str, int, float, datetime, bytes, Decimal): tal cual.

    Sin esto, pyodbc tira HY105 "Invalid parameter type. param-type=dict" en cuanto
    un campo viene como objeto en lugar de scalar.
    """
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("_coerce_value: no se pudo serializar a JSON (%s); usando str()", exc)
            return str(value)
    return value


def execute_upsert(
    cur: Any,
    table: str,
    key_cols: Sequence[str],
    update_cols: Sequence[str],
    row: Dict[str, Any],
    extra_set: Optional[str] = "updated_at=SYSUTCDATETIME()",
) -> None:
    """Ejecuta un UPSERT (MERGE) idempotente sobre `table`.

    Args:
        cur: pyodbc.Cursor activo.
        table: Tabla destino (ej: "finance.mp_payments").
        key_cols: PK natural sobre la que matchear.
        update_cols: Columnas a actualizar/insertar (sin contar key_cols).
        row: Dict con los valores; debe contener TODAS las claves de
             key_cols + update_cols.
        extra_set: SQL extra para el SET (por defecto refresca updated_at).

    Raises:
        KeyError: si row no contiene alguna columna esperada.
        pyodbc.Error: errores de DB se propagan.
    """
    insert_cols = list(key_cols) + list(update_cols)
    sql = build_merge_sql(table, key_cols, update_cols, insert_cols, extra_set=extra_set)

    try:
        key_values    = [_coerce_value(row[c]) for c in key_cols]
        update_values = [_coerce_value(row[c]) for c in update_cols]
        insert_values = [_coerce_value(row[c]) for c in insert_cols]
    except KeyError as exc:
        raise KeyError(f"execute_upsert({table}): falta la columna {exc} en el row") from exc

    params = key_values + update_values + insert_values
    cur.execute(sql, *params)


# =====================================================================
# SEC-03 — sanitize_for_storage
# =====================================================================

# Campos a eliminar del raw_json antes de persistir.
# Las columnas dedicadas (payer_email, card_first_six_digits, etc.) siguen
# guardándose si la columna existe; lo que evitamos es DUPLICAR los datos PII
# dentro del dump JSON, donde quedarían fuera de cualquier control.
_MP_PII_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("payer", "email"),
    ("payer", "phone"),
    ("payer", "first_name"),
    ("payer", "last_name"),
    ("payer", "identification", "number"),
    ("payer", "address"),
    ("card", "first_six_digits"),
    ("card", "last_four_digits"),
    ("card", "cardholder", "name"),
    ("card", "cardholder", "identification", "number"),
    ("additional_info", "payer"),
    ("additional_info", "shipments", "receiver_address"),
    ("metadata",),  # MP a veces inyecta data del comercio acá
)

_IB_PII_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("account_cuit",),
    ("customer_cuit",),
    ("credit_account_customer_cuit",),
    ("debit_account_customer_cuit",),
    ("debit_account_taxpayer_cuit",),
    # Campos aplanados de addenda / billing_company / paying_customer que portan CUIT.
    ("addenda_seller_tax_id",),
    ("billing_company_billing_company_cuit",),
    ("paying_customer_account_cuit",),
    ("paying_customer_customer_cuit",),
)

_REDACTED = "***REDACTED***"


def _redact_path(payload: Dict[str, Any], path: Tuple[str, ...]) -> None:
    """Reemplaza payload[path[0]][path[1]]... por _REDACTED in-place si existe."""
    if not path:
        return
    cur = payload
    for key in path[:-1]:
        if not isinstance(cur, dict) or key not in cur:
            return
        cur = cur[key]
    if isinstance(cur, dict) and path[-1] in cur and cur[path[-1]] is not None:
        cur[path[-1]] = _REDACTED


def sanitize_for_storage(payload: Any, source: str = "mp") -> Any:
    """Devuelve una copia de payload con campos PII redactados.

    Args:
        payload: Dict (o lista de dicts) con el response crudo.
        source: "mp" para Mercado Pago, "ib" para Interbanking.

    Returns:
        Estructura nueva (copia profunda) con PII reemplazada por '***REDACTED***'.

    Notas:
        - No modifica el original.
        - Si payload no es dict/list, lo devuelve tal cual.
        - Las columnas dedicadas en SQL (payer_email, card_*, etc.) siguen
          almacenándose vía las claves del dict transformado en el caller;
          esta función solo limpia el blob raw_json.
    """
    if payload is None:
        return None
    if not isinstance(payload, (dict, list)):
        return payload

    paths = _MP_PII_PATHS if source == "mp" else _IB_PII_PATHS
    cloned = copy.deepcopy(payload)

    targets: Iterable[Dict[str, Any]]
    if isinstance(cloned, list):
        targets = (item for item in cloned if isinstance(item, dict))
    else:
        targets = (cloned,)

    for item in targets:
        for path in paths:
            try:
                _redact_path(item, path)
            except Exception as exc:
                # Nunca fallamos por sanitización; preferimos guardar el dato
                # y loguear que perder el insert completo.
                logger.warning("sanitize_for_storage: error redactando %s (%s)", path, exc)
    return cloned


def sanitize_to_json(payload: Any, source: str = "mp") -> str:
    """Atajo: sanitize_for_storage + json.dumps(ensure_ascii=False)."""
    sanitized = sanitize_for_storage(payload, source=source)
    try:
        return json.dumps(sanitized, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("sanitize_to_json: fallback a repr (%s)", exc)
        return json.dumps({"_sanitize_error": str(exc), "_repr": repr(sanitized)[:1000]})


# =====================================================================
# Validación de columnas (defensa adicional para execute_upsert)
# =====================================================================

def ensure_keys(row: Dict[str, Any], required: Sequence[str], context: str = "") -> None:
    """Lanza KeyError descriptivo si faltan columnas en row."""
    missing = [c for c in required if c not in row]
    if missing:
        prefix = f"{context}: " if context else ""
        raise KeyError(f"{prefix}faltan columnas en row: {missing}")
