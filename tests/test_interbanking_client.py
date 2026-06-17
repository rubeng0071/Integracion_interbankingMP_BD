"""Tests para shared.interbanking_client.

Cubrimos lo que el refactor agrega: constructor con inyección explícita,
factory from_env() para retrocompatibilidad y normalizaciones que ya estaban
(strip de trailing slash en api_base_url).

NO testeamos los endpoints REST: requieren creds reales o un mock de toda la
API IB. El valor está en validar el contrato de construcción, no en
re-validar requests.
"""
from __future__ import annotations

import pytest

from shared.interbanking_client import (
    InterbankingClient,
    _EXTRACTS_COLS,
    _MOVEMENTS_COLS,
    _TRANSFERS_COLS,
    _TRANSFER_FIELD_MAP,
    _VOUCHERS_COLS,
    _VOUCHER_FIELD_MAP,
    _flatten_transfer_accounts,
    _flatten_voucher_accounts,
    _rename,
    _to_df,
)
from shared.secret_string import SecretString


# ---------------------------------------------------------------------------
# Constructor explícito
# ---------------------------------------------------------------------------


def _build(**overrides) -> InterbankingClient:
    """Helper: construye un cliente con defaults sensatos para tests."""
    base = dict(
        client_id="cid",
        client_secret=SecretString("csec"),
        service_url="https://callback.example.com",
        customer_id="12345678",
    )
    base.update(overrides)
    return InterbankingClient(**base)


class TestConstructor:
    def test_args_explicitos_se_aplican(self) -> None:
        c = _build(
            grant_type="password",
            username=SecretString("u"),
            password=SecretString("p"),
            scope="custom-scope",
            page_size=250,
            timeout=120,
        )
        assert c.client_id == "cid"
        assert c.client_secret.reveal() == "csec"
        assert c.service_url == "https://callback.example.com"
        assert c.customer_id == "12345678"
        assert c.grant_type == "password"
        assert c.username is not None and c.username.reveal() == "u"
        assert c.password is not None and c.password.reveal() == "p"
        assert c.scope == "custom-scope"
        assert c.page_size == 250
        assert c.timeout == 120

    def test_defaults(self) -> None:
        c = _build()
        assert c.grant_type == "client_credentials"
        assert c.username is None
        assert c.password is None
        assert c.scope == "info-financiera"
        assert c.page_size == 100
        assert c.timeout == 60
        assert c.token_url.startswith("https://auth.interbanking.com.ar")
        assert c.api_base_url == "https://api-gw.interbanking.com.ar/api/prod/v1"

    def test_api_base_url_strip_trailing_slash(self) -> None:
        c = _build(api_base_url="https://api.example.com/v1/")
        assert c.api_base_url == "https://api.example.com/v1"

    def test_client_secret_debe_ser_secret_string(self) -> None:
        """str crudo debe rechazarse: SEC-07 exige envoltura explícita."""
        with pytest.raises(TypeError, match="SecretString"):
            InterbankingClient(
                client_id="cid",
                client_secret="csec",  # type: ignore[arg-type]
                service_url="https://x",
                customer_id="1",
            )


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


class TestLazyPandasImport:
    """Verifica que pandas no se cargue al importar el cliente.

    El test se hace en un subproceso para no contaminar sys.modules: si pytest
    o un test previo ya importaron pandas (por ejemplo TestPandasInDataFrame),
    el assert in-process daría falso positivo.
    """

    def test_modulo_no_carga_pandas_al_importarse(self) -> None:
        import subprocess
        import sys

        script = (
            "import sys; "
            "assert 'pandas' not in sys.modules; "
            "from shared.interbanking_client import InterbankingClient; "
            "assert 'pandas' not in sys.modules, 'pandas se cargo al importar el cliente'; "
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "ok" in result.stdout


class TestFromEnv:
    def test_construye_desde_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IB_CLIENT_ID", "envcid")
        monkeypatch.setenv("IB_CLIENT_SECRET", "envcsec")
        monkeypatch.setenv("IB_SERVICE_URL", "https://env.example.com")
        monkeypatch.setenv("IB_CUSTOMER_ID", "99")
        c = InterbankingClient.from_env()
        assert c.client_id == "envcid"
        assert c.client_secret.reveal() == "envcsec"
        assert c.service_url == "https://env.example.com"
        assert c.customer_id == "99"

    def test_falla_si_falta_obligatoria(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env() debe propagar KeyError; mantener fail-fast del comportamiento legacy."""
        monkeypatch.setenv("IB_CLIENT_ID", "x")
        # Sin IB_CLIENT_SECRET, IB_SERVICE_URL, IB_CUSTOMER_ID.
        with pytest.raises(KeyError):
            InterbankingClient.from_env()

    def test_username_password_opcionales(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IB_CLIENT_ID", "x")
        monkeypatch.setenv("IB_CLIENT_SECRET", "y")
        monkeypatch.setenv("IB_SERVICE_URL", "https://x")
        monkeypatch.setenv("IB_CUSTOMER_ID", "1")
        c = InterbankingClient.from_env()
        assert c.username is None
        assert c.password is None

    def test_username_password_se_envuelven_en_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IB_CLIENT_ID", "x")
        monkeypatch.setenv("IB_CLIENT_SECRET", "y")
        monkeypatch.setenv("IB_SERVICE_URL", "https://x")
        monkeypatch.setenv("IB_CUSTOMER_ID", "1")
        monkeypatch.setenv("IB_USERNAME", "-3|123|u")
        monkeypatch.setenv("IB_PASSWORD", "passw0rd")
        c = InterbankingClient.from_env()
        assert isinstance(c.username, SecretString)
        assert isinstance(c.password, SecretString)
        assert c.username.reveal() == "-3|123|u"
        assert c.password.reveal() == "passw0rd"


# ---------------------------------------------------------------------------
# Aplanado de dicts anidados (cuentas crédito/débito, addenda, billing, paying)
#
# La API real devuelve snake_case (credit_account.account_cbu). Antes los
# helpers solo desempaquetaban camelCase y dejaban estas columnas en NULL.
# ---------------------------------------------------------------------------


class TestFlattenTransfers:
    def test_aplana_cuentas_snake_case(self) -> None:
        rec = {
            "transfer_id": 1,
            "credit_account": {"account_cbu": "C1", "bank_number": 7, "customer_cuit": "30aa"},
            "debit_account": {"account_cbu": "D1", "bank_name": "Galicia", "account_type": "CC"},
        }
        out = _rename(_flatten_transfer_accounts(dict(rec)), _TRANSFER_FIELD_MAP)
        assert out["credit_account_account_cbu"] == "C1"
        assert out["credit_account_bank_number"] == 7
        assert out["credit_account_customer_cuit"] == "30aa"
        assert out["debit_account_account_cbu"] == "D1"
        assert out["debit_account_bank_name"] == "Galicia"
        assert out["debit_account_account_type"] == "CC"
        # El dict anidado original no debe sobrevivir como columna cruda.
        assert "credit_account" not in out
        assert "debit_account" not in out

    def test_aplana_cuentas_camel_case_legacy(self) -> None:
        rec = {"creditAccount": {"cbu": "C1", "numeroBanco": 7, "cuit": "30aa"}}
        out = _flatten_transfer_accounts(dict(rec))
        assert out["credit_account_account_cbu"] == "C1"
        assert out["credit_account_bank_number"] == 7
        assert out["credit_account_customer_cuit"] == "30aa"

    def test_aplana_addenda_y_conserva_blob(self) -> None:
        addenda = {
            "operation_numer": "5478664", "amount": 0.0, "seller_tax_id": "30643233343",
            "request_date": "2026-04-10T00:00:00", "due_date": "2026-04-10T23:59:59",
            "payment_receipt": 229067480, "voucher_number": None,
        }
        out = _flatten_transfer_accounts({"transfer_id": 1, "addenda": dict(addenda)})
        assert out["addenda_operation_numer"] == "5478664"
        assert out["addenda_seller_tax_id"] == "30643233343"
        assert out["addenda_amount"] == 0.0
        assert out["addenda_payment_receipt"] == 229067480
        # El blob original se conserva para persistirlo como JSON.
        assert isinstance(out["addenda"], dict)
        assert out["addenda"]["operation_numer"] == "5478664"

    def test_columnas_addenda_sobreviven_to_df(self) -> None:
        out = _rename(
            _flatten_transfer_accounts({"transfer_id": 1, "addenda": {"amount": 5, "operation_numer": "X"}}),
            _TRANSFER_FIELD_MAP,
        )
        row = _to_df([out], _TRANSFERS_COLS).iloc[0].to_dict()
        for col in ("addenda_operation_numer", "addenda_amount", "addenda_request_date",
                    "addenda_due_date", "credit_account_account_cbu"):
            assert col in row


class TestFlattenVouchers:
    def test_aplana_billing_paying_afip_y_cuentas(self) -> None:
        rec = {
            "transfer_id": 9,
            "debit_account": {"account_cbu": "D", "voucher_number": "V1", "taxpayer_cuit": "30zz"},
            "credit_account": {"account_cbu": "C", "bank_name": "Banco"},
            "billing_company": {"billing_company_cuit": "30643233343", "due_date": "2026-04-10T23:59:59"},
            "paying_customer": {"company_name": "TRONADOR", "account_cbu": "0070", "customer_cuit": "305"},
            "afip": {"provider_name": "ARBA", "fee_number": "1"},
        }
        out = _rename(_flatten_voucher_accounts(dict(rec)), _VOUCHER_FIELD_MAP)
        assert out["debit_account_voucher_number"] == "V1"
        assert out["debit_account_taxpayer_cuit"] == "30zz"
        assert out["credit_account_bank_name"] == "Banco"
        assert out["billing_company_billing_company_cuit"] == "30643233343"
        assert out["billing_company_due_date"] == "2026-04-10T23:59:59"
        assert out["paying_customer_company_name"] == "TRONADOR"
        assert out["paying_customer_account_cbu"] == "0070"
        assert out["afip_provider_name"] == "ARBA"
        # Blobs conservados.
        assert isinstance(out["billing_company"], dict)
        assert isinstance(out["paying_customer"], dict)

    def test_columnas_sobreviven_to_df(self) -> None:
        rec = {
            "transfer_id": 9,
            "debit_account": {"voucher_number": "V1"},
            "billing_company": {"billing_company_cuit": "30"},
            "paying_customer": {"customer_cuit": "30"},
        }
        out = _rename(_flatten_voucher_accounts(dict(rec)), _VOUCHER_FIELD_MAP)
        row = _to_df([out], _VOUCHERS_COLS).iloc[0].to_dict()
        for col in ("debit_account_voucher_number", "billing_company_billing_company_cuit",
                    "billing_company_due_date", "paying_customer_customer_cuit",
                    "paying_customer_account_cbu"):
            assert col in row


class TestColumnContracts:
    """Las columnas que exporta el cliente de referencia (jsontoexcel) deben
    estar todas representadas en las listas _*_COLS del cliente compartido."""

    def test_movements_incluye_campos_standard(self) -> None:
        for c in ("grouping_code_standard", "code_description_standard",
                  "operation_code_standard", "source_account", "movement_type"):
            assert c in _MOVEMENTS_COLS

    def test_extracts_incluye_grouping_code_ib(self) -> None:
        assert "grouping_code_ib" in _EXTRACTS_COLS

    def test_transfers_cubre_header_excel(self) -> None:
        excel = {
            "transfer_id", "transaction_number", "request_date", "transfer_type_code",
            "transfer_type_description", "account_label", "amount", "currency",
            "reference_number", "lot_number", "payment_number", "status", "client",
            "statement_consolidated", "unified_send", "direct_import", "same_owner",
            "internal_client_id", "transfer_comments",
            "credit_account_currency", "credit_account_bank_number", "credit_account_bank_name",
            "credit_account_account_cbu", "credit_account_account_type",
            "credit_account_account_number", "credit_account_account_label",
            "credit_account_customer_cuit", "debit_account_currency", "debit_account_bank_number",
            "debit_account_bank_name", "debit_account_account_cbu", "debit_account_account_type",
            "debit_account_account_number", "debit_account_account_label",
            "debit_account_customer_cuit", "addenda_operation_numer", "addenda_payment_receipt",
            "addenda_amount", "addenda_seller_tax_id", "addenda_voucher_type", "addenda_seller_name",
            "addenda_community_code", "addenda_seller_code", "addenda_sale_point",
            "addenda_request_date", "addenda_issue_date", "addenda_seller_company_name",
            "addenda_voucher_number", "addenda_due_date",
        }
        assert excel.issubset(set(_TRANSFERS_COLS))

    def test_vouchers_cubre_header_excel(self) -> None:
        excel = {
            "request_date", "transfer_type_description", "transfer_type_code", "transfer_id",
            "network_number", "amount", "currency", "validation_code", "total_amount", "comments",
            "debit_account_voucher_number", "debit_account_customer_cuit", "debit_account_bank_number",
            "debit_account_bank_name", "debit_account_taxpayer_cuit", "debit_account_account_label",
            "debit_account_account_cbu", "afip_provider_name", "afip_fee_number", "afip_nro_formulario",
            "afip_provider_code", "afip_tax_description", "afip_concept_description", "afip_tax_code",
            "afip_control_code", "afip_fiscal_period", "afip_vep_number", "afip_concept_code",
            "afip_pago_desc", "billing_company_billing_company_cuit",
            "billing_company_billing_company_name", "billing_company_billing_account_name",
            "billing_company_billing_seller", "billing_company_billing_account_id",
            "billing_company_due_date", "paying_customer_voucher_number", "paying_customer_debit_bank",
            "paying_customer_company_name", "paying_customer_linkage_code",
            "paying_customer_account_cuit", "paying_customer_account_cbu",
            "paying_customer_account_label", "paying_customer_customer_cuit",
            "credit_account_bank_number", "credit_account_bank_name", "credit_account_account_cbu",
            "credit_account_account_label", "credit_account_customer_cuit",
        }
        assert excel.issubset(set(_VOUCHERS_COLS))


class TestMovementInjection:
    """source_account / movement_type no vienen en el registro de la API; el
    cliente los inyecta desde los parámetros del request (igual que jsontoexcel)."""

    def _patch(self, monkeypatch: pytest.MonkeyPatch, client: InterbankingClient, payload: dict) -> None:
        monkeypatch.setattr(client, "_get_token", lambda: "tok")

        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return payload

        monkeypatch.setattr(client.session, "get", lambda *a, **k: _Resp())

    def test_get_movimientos_inyecta_source_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _build()
        self._patch(monkeypatch, c, {
            "movements_detail": [{"account_cbu": "X", "amount": 10, "voucher_number": "5"}],
            "general_data": {"total_rows": 1},
        })
        df, _ = c.get_movimientos(
            account_number="123", bank_number="7",
            date_since="2026-01-01", date_until="2026-01-31",
        )
        row = df.iloc[0].to_dict()
        assert row["source_account"] == "123"
        assert row["movement_type"] == "anteriores"
        assert row["account_cbu"] == "X"

    def test_get_extractos_inyecta_source_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _build()
        self._patch(monkeypatch, c, {
            "statements": [{"statement_number": "1",
                            "movement_detail": [{"amount": 5, "grouping_code_ib": "8 1 1"}]}],
            "general_data": {"total_rows": 1},
        })
        df, _ = c.get_extractos(
            account_number="999", bank_number="7",
            date_since="2026-01-01", date_until="2026-01-31",
        )
        row = df.iloc[0].to_dict()
        assert row["source_account"] == "999"
        assert row["grouping_code_ib"] == "8 1 1"
