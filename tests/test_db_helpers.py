"""Tests para shared.db_helpers (CAL-02, CAL-03, SEC-03)."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from shared.db_helpers import (
    build_merge_sql,
    ensure_keys,
    execute_upsert,
    execute_upsert_batch,
    sanitize_for_storage,
    sanitize_to_json,
    to_str,
)


# =====================================================================
# to_str  (CAL-03)
# =====================================================================


class TestToStr:
    def test_none(self) -> None:
        assert to_str(None) is None

    def test_empty_string(self) -> None:
        assert to_str("") is None

    def test_whitespace_only(self) -> None:
        assert to_str("   ") is None

    def test_plain_string(self) -> None:
        assert to_str("abc") == "abc"

    def test_strips_surrounding_whitespace(self) -> None:
        assert to_str("  abc  ") == "abc"

    def test_int(self) -> None:
        assert to_str(123) == "123"

    def test_float(self) -> None:
        assert to_str(123.45) == "123.45"

    def test_zero_int(self) -> None:
        """0 NO debe ser tratado como None: es un valor válido."""
        assert to_str(0) == "0"

    def test_bool(self) -> None:
        assert to_str(True) == "True"
        assert to_str(False) == "False"


# =====================================================================
# build_merge_sql  (CAL-02)
# =====================================================================


class TestBuildMergeSql:
    def test_clausulas_basicas_presentes(self) -> None:
        sql = build_merge_sql(
            "finance.t",
            key_cols=("id",),
            update_cols=("name",),
            insert_cols=("id", "name"),
        )
        # Comprobaciones liberales (no chequeamos whitespace exacto).
        assert "MERGE finance.t AS tgt" in sql
        assert "USING (SELECT" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "WHEN NOT MATCHED THEN INSERT" in sql

    def test_multiples_keys_genera_AND(self) -> None:
        sql = build_merge_sql(
            "finance.t",
            key_cols=("a", "b", "c"),
            update_cols=("x",),
            insert_cols=("a", "b", "c", "x"),
        )
        assert "tgt.a = src.a" in sql
        assert "tgt.b = src.b" in sql
        assert "tgt.c = src.c" in sql
        # Las tres condiciones unidas con AND.
        assert sql.count("AND") >= 2

    def test_placeholders_count_matches_insert_cols(self) -> None:
        sql = build_merge_sql(
            "finance.t",
            key_cols=("id",),
            update_cols=("a", "b"),
            insert_cols=("id", "a", "b"),
        )
        # 3 placeholders en VALUES.
        values_block = sql.split("VALUES")[-1]
        assert values_block.count("?") == 3

    def test_extra_set_se_agrega_al_update(self) -> None:
        sql = build_merge_sql(
            "finance.t",
            key_cols=("id",),
            update_cols=("name",),
            insert_cols=("id", "name"),
            extra_set="updated_at=SYSUTCDATETIME()",
        )
        assert "updated_at=SYSUTCDATETIME()" in sql

    def test_sin_extra_set_no_aparece(self) -> None:
        sql = build_merge_sql(
            "finance.t",
            key_cols=("id",),
            update_cols=("name",),
            insert_cols=("id", "name"),
            extra_set=None,
        )
        assert "updated_at" not in sql


# =====================================================================
# execute_upsert  (CAL-02)
# =====================================================================


class TestExecuteUpsert:
    def test_ejecuta_con_params_en_orden_correcto(self) -> None:
        """Orden esperado: keys + update_values + insert_values."""
        cur = MagicMock()
        row = {"id": 1, "name": "foo", "extra": "bar"}
        execute_upsert(
            cur,
            table="finance.t",
            key_cols=("id",),
            update_cols=("name", "extra"),
            row=row,
            extra_set=None,
        )
        cur.execute.assert_called_once()
        args = cur.execute.call_args.args
        sql = args[0]
        params = list(args[1:])
        assert "MERGE finance.t" in sql
        # keys + update_values + insert_values (insert = keys + update).
        assert params == [1, "foo", "bar", 1, "foo", "bar"]

    def test_keyerror_si_falta_columna(self) -> None:
        cur = MagicMock()
        with pytest.raises(KeyError, match="finance.t"):
            execute_upsert(
                cur,
                table="finance.t",
                key_cols=("id",),
                update_cols=("name", "missing"),
                row={"id": 1, "name": "foo"},
                extra_set=None,
            )

    def test_default_extra_set_actualiza_updated_at(self) -> None:
        cur = MagicMock()
        execute_upsert(
            cur,
            table="finance.t",
            key_cols=("id",),
            update_cols=("name",),
            row={"id": 1, "name": "foo"},
        )
        sql = cur.execute.call_args.args[0]
        assert "updated_at=SYSUTCDATETIME()" in sql


# =====================================================================
# execute_upsert_batch  (CAL-02)
# =====================================================================


class TestExecuteUpsertBatch:
    def test_filas_vacias_no_ejecuta_nada(self) -> None:
        cur = MagicMock()
        n = execute_upsert_batch(
            cur,
            table="finance.t",
            key_cols=("id",),
            update_cols=("name",),
            rows=[],
        )
        assert n == 0
        cur.execute.assert_not_called()

    def test_secuencia_de_sentencias(self) -> None:
        """3 SQLs en orden: pre-drop, SELECT TOP 0 INTO, MERGE, post-drop."""
        cur = MagicMock()
        rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        n = execute_upsert_batch(
            cur,
            table="finance.t",
            key_cols=("id",),
            update_cols=("name",),
            rows=rows,
        )
        assert n == 2

        # Capturamos todos los SQLs (execute + executemany).
        execute_calls = [c.args[0] for c in cur.execute.call_args_list]
        # Pre-drop defensivo, clonado de schema, MERGE y post-drop deben aparecer.
        assert any("DROP TABLE" in s for s in execute_calls[:1]), "falta pre-drop defensivo"
        assert any("SELECT TOP 0" in s for s in execute_calls), "falta clonado de schema"
        assert any("MERGE finance.t" in s for s in execute_calls), "falta MERGE"
        assert any("DROP TABLE" in s for s in execute_calls[-1:]), "falta post-drop"

        # Bulk insert via executemany.
        cur.executemany.assert_called_once()
        insert_sql = cur.executemany.call_args.args[0]
        params = cur.executemany.call_args.args[1]
        assert "INSERT INTO #stg_t" in insert_sql
        # Orden de columnas: keys + update_cols.
        assert params == [(1, "a"), (2, "b")]

    def test_fast_executemany_se_activa_y_se_restaura(self) -> None:
        cur = MagicMock()
        cur.fast_executemany = False  # estado previo
        execute_upsert_batch(
            cur,
            table="finance.t",
            key_cols=("id",),
            update_cols=("name",),
            rows=[{"id": 1, "name": "a"}],
        )
        # Tras la operación, el flag vuelve a su valor original.
        assert cur.fast_executemany is False

    def test_keyerror_cita_la_tabla(self) -> None:
        cur = MagicMock()
        with pytest.raises(KeyError, match="finance.t"):
            execute_upsert_batch(
                cur,
                table="finance.t",
                key_cols=("id",),
                update_cols=("name",),
                rows=[{"id": 1}],  # falta 'name'
            )

    def test_merge_incluye_extra_set_default(self) -> None:
        cur = MagicMock()
        execute_upsert_batch(
            cur,
            table="finance.t",
            key_cols=("id",),
            update_cols=("name",),
            rows=[{"id": 1, "name": "a"}],
        )
        merge_sql = next(
            c.args[0] for c in cur.execute.call_args_list if "MERGE" in c.args[0]
        )
        assert "updated_at=SYSUTCDATETIME()" in merge_sql

    def test_stg_name_por_tabla_short(self) -> None:
        """Tablas distintas deben usar stg distintos (para no chocarse)."""
        cur = MagicMock()
        execute_upsert_batch(
            cur,
            table="finance.ib_movements",
            key_cols=("movement_hash",),
            update_cols=("amount",),
            rows=[{"movement_hash": "h", "amount": 1}],
        )
        execute_upsert_batch(
            cur,
            table="finance.ib_extracts",
            key_cols=("extract_hash",),
            update_cols=("amount",),
            rows=[{"extract_hash": "h", "amount": 2}],
        )
        all_sql = [c.args[0] for c in cur.execute.call_args_list]
        assert any("#stg_ib_movements" in s for s in all_sql)
        assert any("#stg_ib_extracts" in s for s in all_sql)


# =====================================================================
# sanitize_for_storage  (SEC-03)
# =====================================================================


class TestSanitizeMP:
    def test_none(self) -> None:
        assert sanitize_for_storage(None, source="mp") is None

    def test_primitive_passthrough(self) -> None:
        assert sanitize_for_storage("hola", source="mp") == "hola"
        assert sanitize_for_storage(42, source="mp") == 42

    def test_redacta_payer_email(self) -> None:
        out = sanitize_for_storage(
            {"payer": {"email": "user@example.com", "id": "X"}}, source="mp"
        )
        assert out["payer"]["email"] == "***REDACTED***"
        assert out["payer"]["id"] == "X"

    def test_redacta_card_digits(self) -> None:
        out = sanitize_for_storage(
            {
                "card": {
                    "first_six_digits": "411111",
                    "last_four_digits": "1234",
                    "cardholder": {"name": "John Doe"},
                }
            },
            source="mp",
        )
        assert out["card"]["first_six_digits"] == "***REDACTED***"
        assert out["card"]["last_four_digits"] == "***REDACTED***"
        assert out["card"]["cardholder"]["name"] == "***REDACTED***"

    def test_redacta_metadata_completa(self) -> None:
        """metadata es un escape hatch que comercios usan para meter PII libre."""
        out = sanitize_for_storage(
            {"metadata": {"dni": "12345678", "phone": "+541112345678"}},
            source="mp",
        )
        assert out["metadata"] == "***REDACTED***"

    def test_no_muta_input(self) -> None:
        original = {"payer": {"email": "user@example.com"}}
        sanitize_for_storage(original, source="mp")
        assert original["payer"]["email"] == "user@example.com"

    def test_lista_de_dicts(self) -> None:
        out = sanitize_for_storage(
            [{"payer": {"email": "a@b.com"}}, {"payer": {"email": "c@d.com"}}],
            source="mp",
        )
        assert isinstance(out, list)
        assert all(item["payer"]["email"] == "***REDACTED***" for item in out)

    def test_no_falla_si_path_no_existe(self) -> None:
        """El payload puede no tener todas las claves; no debe explotar."""
        out = sanitize_for_storage({"id": 1, "status": "approved"}, source="mp")
        assert out == {"id": 1, "status": "approved"}

    def test_source_ib_no_redacta_mp_paths(self) -> None:
        """Si declaro source='ib', no toco campos MP (no cross-contamination)."""
        out = sanitize_for_storage(
            {"payer": {"email": "user@example.com"}, "account_cuit": "20-12345678-9"},
            source="ib",
        )
        # email MP queda intacto, cuit IB se redacta.
        assert out["payer"]["email"] == "user@example.com"
        assert out["account_cuit"] == "***REDACTED***"


class TestSanitizeIB:
    def test_redacta_cuits(self) -> None:
        out = sanitize_for_storage(
            {
                "account_cuit": "20-12345678-9",
                "customer_cuit": "30-87654321-0",
                "credit_account_customer_cuit": "27-11111111-1",
                "debit_account_customer_cuit": "23-22222222-2",
                "debit_account_taxpayer_cuit": "24-33333333-3",
                "amount": 1000,
            },
            source="ib",
        )
        for k in (
            "account_cuit",
            "customer_cuit",
            "credit_account_customer_cuit",
            "debit_account_customer_cuit",
            "debit_account_taxpayer_cuit",
        ):
            assert out[k] == "***REDACTED***"
        assert out["amount"] == 1000  # no-PII intacto


# =====================================================================
# sanitize_to_json  (SEC-03)
# =====================================================================


class TestSanitizeToJson:
    def test_genera_json_valido(self) -> None:
        out = sanitize_to_json({"payer": {"email": "a@b.com"}, "id": 1}, source="mp")
        decoded = json.loads(out)
        assert decoded["payer"]["email"] == "***REDACTED***"
        assert decoded["id"] == 1

    def test_serializa_datetime_con_default_str(self) -> None:
        """raw_json suele incluir datetimes — no debe explotar."""
        out = sanitize_to_json({"created_at": datetime(2026, 1, 1, 12, 0, 0)}, source="mp")
        decoded = json.loads(out)
        assert "2026-01-01" in decoded["created_at"]

    def test_ensure_ascii_false(self) -> None:
        """Acentos no deben quedar como \\uXXXX (UTF-8 directo)."""
        out = sanitize_to_json({"description": "café"}, source="mp")
        assert "café" in out


# =====================================================================
# ensure_keys
# =====================================================================


class TestEnsureKeys:
    def test_no_levanta_si_estan_todas(self) -> None:
        ensure_keys({"a": 1, "b": 2}, required=("a", "b"))

    def test_levanta_con_lista_de_faltantes(self) -> None:
        with pytest.raises(KeyError, match=r"\['c', 'd'\]"):
            ensure_keys({"a": 1, "b": 2}, required=("a", "c", "d"))

    def test_context_aparece_en_mensaje(self) -> None:
        with pytest.raises(KeyError, match="finance.t:"):
            ensure_keys({}, required=("a",), context="finance.t")
