#!/usr/bin/env python3
"""Cliente para la API REST de Interbanking Argentina.

Autenticación: OAuth2 - client_credentials o password flow.
Base URL:      https://api-gw.interbanking.com.ar/api/prod/v1

Variables de entorno requeridas:
    IB_CLIENT_ID        Client ID obtenido en developers.interbanking.com.ar
    IB_CLIENT_SECRET    Client Secret de la aplicación registrada
    IB_SERVICE_URL      URL de redirección OAuth configurada en la aplicación (con https://)
    IB_CUSTOMER_ID      Código de abonado (Administración → Bancos y cuentas → Código Cliente Interno)

Variables opcionales (con defaults):
    IB_GRANT_TYPE       Flujo OAuth: "client_credentials" (default) o "password"
    IB_USERNAME         Requerido si IB_GRANT_TYPE=password (formato: -3|nro_clave|usuario)
    IB_PASSWORD         Requerido si IB_GRANT_TYPE=password
    IB_TOKEN_URL        Default: https://auth.interbanking.com.ar/cas/oidc/accessToken
    IB_API_BASE_URL     Default: https://api-gw.interbanking.com.ar/api/prod/v1
    IB_SCOPE            Default: info-financiera
    IB_PAGE_SIZE        Tamaño de página para paginación (default: 100)
    IB_TIMEOUT_SECONDS  Timeout HTTP en segundos (default: 60)

Notas de la API Interbanking:
    - El token se obtiene con POST a TOKEN_URL.
      SEC-02: las credenciales viajan en el BODY (application/x-www-form-urlencoded),
      conforme RFC 6749 §4.4.2, NO en query string. Esto evita que aparezcan en
      logs de proxies, access logs del API gateway o historiales de URL.
    - Headers de API: Authorization: Bearer {token} + client_id: {client_id}
    - El token expira en 7200 segundos; se renueva automáticamente con 60 s de margen.
    - Los movimientos no tienen un ID único en la API; se usa hash SHA-256 para deduplicar.
    - SEC-07: client_secret y access_token se manejan como SecretString para evitar
      filtraciones accidentales en logs / repr / dumps.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# SEC-07: ahora que el archivo vive dentro del paquete `shared/`, el import es
# relativo. No necesitamos fallback: si alguien importa este módulo, el paquete
# `shared` ya está en el path por definición.
from .secret_string import SecretString, reveal as _reveal_secret  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes de URLs
# ---------------------------------------------------------------------------

_DEFAULT_TOKEN_URL = "https://auth.interbanking.com.ar/cas/oidc/accessToken"
_DEFAULT_API_BASE  = "https://api-gw.interbanking.com.ar/api/prod/v1"
_DEFAULT_SCOPE     = "info-financiera"


# ---------------------------------------------------------------------------
# Lazy import de pandas
#
# pandas pesa ~250ms al importarse en cold start de Azure Functions Python.
# La mayor parte del cliente NO lo necesita: solo `_to_df()` (último paso
# de cada getter) y `export_to_excel()` (utility CLI). Mientras nadie llame
# a esos métodos, pandas no se carga en el proceso.
#
# `from __future__ import annotations` hace que los type hints como
# `pd.DataFrame` queden como strings y no fuercen el import. Por eso
# podemos seguir anotando `Tuple[pd.DataFrame, Any]` sin penalty.
#
# Para que el futuro lector entienda el patrón: usar `_pd()` adentro del
# código de runtime, NO importar pandas al top del módulo.
# ---------------------------------------------------------------------------

_pd_module = None  # cache de la primera importación


def _pd():
    """Devuelve el módulo pandas, importándolo en el primer uso."""
    global _pd_module
    if _pd_module is None:
        import pandas as _pd_mod  # noqa: WPS433  lazy intencional
        _pd_module = _pd_mod
    return _pd_module


# ---------------------------------------------------------------------------
# Helpers de normalización de DataFrames
# ---------------------------------------------------------------------------

def _to_df(records: List[Dict[str, Any]], columns: List[str]) -> "pd.DataFrame":  # noqa: F821
    """Crea un DataFrame con columnas fijas. Las columnas faltantes quedan en NaN."""
    pd = _pd()
    if not records:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(records)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df[columns]


def _rename(record: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    """Renombra claves de un dict según mapping {api_key: df_col}."""
    return {mapping.get(k, k): v for k, v in record.items()}


def _flatten_nested(record: Dict[str, Any], prefix: str, nested_key: str) -> Dict[str, Any]:
    """Aplana un sub-dict anidado agregando un prefijo a sus claves."""
    nested = record.pop(nested_key, {}) or {}
    for k, v in nested.items():
        record[f"{prefix}_{k}"] = v
    return record


# ---------------------------------------------------------------------------
# Columnas esperadas por cada endpoint (contrato con unified_finance_sync_service)
# ---------------------------------------------------------------------------

_ACCOUNTS_COLS = [
    "account_cbu", "bank_number", "account_cuit", "account_label",
    "currency", "bank_name", "account_number", "account_type",
]

_BALANCES_COLS = [
    "bank_number", "account_number", "account_type", "currency",
    "account_label", "account_name", "row_date", "message",
    "countable_balance", "initial_operating_balance", "current_operating_balance",
    "projected_balance_24hs", "projected_balance_48hs",
    "operation_date", "day_balance", "total_debits", "total_credits", "is_historical",
]

_MOVEMENTS_COLS = [
    "account_cbu", "depositor_code", "operation_code_ib", "operation_code_bank",
    "code_description_ib", "customer_cuit", "depositor_description",
    "code_description_bank", "amount", "voucher_number", "grouping_code_ib",
    "branch_office_activity", "process_date", "debit_credit_type",
    "movement_type", "source_account", "associated_voucher",
    "real_date_activity", "movement_date", "value_date", "correlative_number",
    # Campos "estándar" que IB devuelve en algunos periodos/movimientos.
    "grouping_code_standard", "code_description_standard", "operation_code_standard",
]

_TRANSFERS_COLS = [
    "transfer_id", "transaction_number", "request_date",
    "transfer_type_code", "transfer_type_description", "account_label",
    "amount", "currency", "reference_number", "lot_number", "payment_number",
    "status", "client", "statement_consolidated", "unified_send",
    "direct_import", "same_owner", "internal_client_id",
    "addenda", "transfer_comments",
    "credit_account_customer_cuit", "credit_account_account_cbu",
    "credit_account_account_number", "credit_account_currency",
    "credit_account_account_type", "credit_account_bank_number",
    "credit_account_bank_name", "credit_account_account_label",
    "debit_account_customer_cuit", "debit_account_account_cbu",
    "debit_account_account_number", "debit_account_currency",
    "debit_account_account_type", "debit_account_bank_number",
    "debit_account_bank_name", "debit_account_account_label",
    # Addenda (factura/recaudación) aplanada. `addenda` se conserva como blob JSON.
    "addenda_operation_numer", "addenda_payment_receipt", "addenda_amount",
    "addenda_seller_tax_id", "addenda_voucher_type", "addenda_seller_name",
    "addenda_community_code", "addenda_seller_code", "addenda_sale_point",
    "addenda_request_date", "addenda_issue_date", "addenda_seller_company_name",
    "addenda_voucher_number", "addenda_due_date",
]

_EXTRACTS_COLS = [
    "statement_number", "operation_date", "total_movements",
    "opening_balance", "ending_balance", "operation_code_ib",
    "operation_code_bank", "code_description_ib", "customer_cuit",
    "depositor_description", "code_description_bank", "movement_date",
    "real_date_activity", "amount", "voucher_number", "grouping_code_ib",
    "branch_office_activity",
    "process_date", "value_date", "debit_credit_type", "correlative_number",
    "source_account", "code_description_standard", "operation_code_bank_standard",
]

_VOUCHERS_COLS = [
    "transfer_id", "request_date", "transfer_type_description",
    "transfer_type_code", "network_number", "amount", "currency",
    "validation_code", "total_amount", "comments", "billing_company",
    "paying_customer", "debit_account_customer_cuit", "debit_account_account_cbu",
    "debit_account_voucher_number",
    "debit_account_taxpayer_cuit", "debit_account_bank_number",
    "debit_account_bank_name", "debit_account_account_label",
    "afip_concept_description", "afip_control_code", "afip_nro_formulario",
    "afip_tax_description", "afip_fee_number", "afip_pago_desc",
    "afip_provider_name", "afip_concept_code", "afip_tax_code",
    "afip_vep_number", "afip_fiscal_period", "afip_provider_code",
    "credit_account_customer_cuit", "credit_account_account_cbu",
    "credit_account_bank_number", "credit_account_bank_name",
    "credit_account_account_label",
    # billing_company aplanada (`billing_company` se conserva como blob JSON).
    "billing_company_billing_company_cuit", "billing_company_billing_company_name",
    "billing_company_billing_account_name", "billing_company_billing_seller",
    "billing_company_billing_account_id", "billing_company_due_date",
    # paying_customer aplanada (`paying_customer` se conserva como blob JSON).
    "paying_customer_voucher_number", "paying_customer_debit_bank",
    "paying_customer_company_name", "paying_customer_linkage_code",
    "paying_customer_account_cuit", "paying_customer_account_cbu",
    "paying_customer_account_label", "paying_customer_customer_cuit",
]


# ---------------------------------------------------------------------------
# Mapeos de campos API → columnas DataFrame
# La API Interbanking usa camelCase; los mapeamos a snake_case.
# Si la API devuelve campos distintos, ajustar aquí sin tocar el servicio.
# ---------------------------------------------------------------------------

_ACCOUNT_FIELD_MAP: Dict[str, str] = {
    "cbu":                  "account_cbu",
    "numeroBanco":          "bank_number",
    "bankNumber":           "bank_number",
    "cuit":                 "account_cuit",
    "alias":                "account_label",
    "descripcion":          "account_label",
    "label":                "account_label",
    "moneda":               "currency",
    "currency":             "currency",
    "nombreBanco":          "bank_name",
    "bankName":             "bank_name",
    "numeroCuenta":         "account_number",
    "accountNumber":        "account_number",
    "tipoCuenta":           "account_type",
    "accountType":          "account_type",
}

_BALANCE_FIELD_MAP: Dict[str, str] = {
    "numeroBanco":               "bank_number",
    "bankNumber":                "bank_number",
    "numeroCuenta":              "account_number",
    "accountNumber":             "account_number",
    "tipoCuenta":                "account_type",
    "accountType":               "account_type",
    "moneda":                    "currency",
    "currency":                  "currency",
    "alias":                     "account_label",
    "label":                     "account_label",
    "nombreCuenta":              "account_name",
    "accountName":               "account_name",
    "fecha":                     "row_date",
    "rowDate":                   "row_date",
    "date":                      "row_date",
    "mensaje":                   "message",
    "message":                   "message",
    "saldoContable":             "countable_balance",
    "countableBalance":          "countable_balance",
    "saldoOperativoInicial":     "initial_operating_balance",
    "initialOperatingBalance":   "initial_operating_balance",
    "saldoOperativoActual":      "current_operating_balance",
    "currentOperatingBalance":   "current_operating_balance",
    "saldoProyectado24":         "projected_balance_24hs",
    "projectedBalance24hs":      "projected_balance_24hs",
    "saldoProyectado48":         "projected_balance_48hs",
    "projectedBalance48hs":      "projected_balance_48hs",
    "fechaOperacion":            "operation_date",
    "operationDate":             "operation_date",
    "saldoDia":                  "day_balance",
    "dayBalance":                "day_balance",
    "totalDebitos":              "total_debits",
    "totalDebits":               "total_debits",
    "totalCreditos":             "total_credits",
    "totalCredits":              "total_credits",
    "esHistorico":               "is_historical",
    "isHistorical":              "is_historical",
    "historico":                 "is_historical",
}

_MOVEMENT_FIELD_MAP: Dict[str, str] = {
    "cbu":                       "account_cbu",
    "codigoDepositante":         "depositor_code",
    "depositorCode":             "depositor_code",
    "codigoOperacionIB":         "operation_code_ib",
    "operationCodeIb":           "operation_code_ib",
    "codigoOperacionBanco":      "operation_code_bank",
    "operationCodeBank":         "operation_code_bank",
    "descripcionIB":             "code_description_ib",
    "codeDescriptionIb":         "code_description_ib",
    "cuitCliente":               "customer_cuit",
    "customerCuit":              "customer_cuit",
    "descripcionDepositante":    "depositor_description",
    "depositorDescription":      "depositor_description",
    "descripcionBanco":          "code_description_bank",
    "codeDescriptionBank":       "code_description_bank",
    "importe":                   "amount",
    "amount":                    "amount",
    "nroComprobante":            "voucher_number",
    "voucherNumber":             "voucher_number",
    "codigoAgrupamiento":        "grouping_code_ib",
    "groupingCodeIb":            "grouping_code_ib",
    "sucursal":                  "branch_office_activity",
    "branchOfficeActivity":      "branch_office_activity",
    "fechaProceso":              "process_date",
    "processDate":               "process_date",
    "tipoMovimiento":            "debit_credit_type",
    "debitCreditType":           "debit_credit_type",
    "tipMovimiento":             "movement_type",
    "movementType":              "movement_type",
    "cuentaOrigen":              "source_account",
    "sourceAccount":             "source_account",
    "comprobanteAsociado":       "associated_voucher",
    "associatedVoucher":         "associated_voucher",
    "fechaReal":                 "real_date_activity",
    "realDateActivity":          "real_date_activity",
    "fechaMovimiento":           "movement_date",
    "movementDate":              "movement_date",
    "fechaValor":                "value_date",
    "valueDate":                 "value_date",
    "correlativo":               "correlative_number",
    "correlativeNumber":         "correlative_number",
}

_TRANSFER_FIELD_MAP: Dict[str, str] = {
    "id":                        "transfer_id",
    "transferId":                "transfer_id",
    "nroTransaccion":            "transaction_number",
    "transactionNumber":         "transaction_number",
    "fechaSolicitud":            "request_date",
    "requestDate":               "request_date",
    "codigoTipo":                "transfer_type_code",
    "transferTypeCode":          "transfer_type_code",
    "descripcionTipo":           "transfer_type_description",
    "transferTypeDescription":   "transfer_type_description",
    "alias":                     "account_label",
    "importe":                   "amount",
    "amount":                    "amount",
    "moneda":                    "currency",
    "currency":                  "currency",
    "nroReferencia":             "reference_number",
    "referenceNumber":           "reference_number",
    "nroLote":                   "lot_number",
    "lotNumber":                 "lot_number",
    "nroPago":                   "payment_number",
    "paymentNumber":             "payment_number",
    "estado":                    "status",
    "status":                    "status",
    "cliente":                   "client",
    "client":                    "client",
    "resumenConsolidado":        "statement_consolidated",
    "statementConsolidated":     "statement_consolidated",
    "envioUnificado":            "unified_send",
    "unifiedSend":               "unified_send",
    "importacionDirecta":        "direct_import",
    "directImport":              "direct_import",
    "mismoTitular":              "same_owner",
    "sameOwner":                 "same_owner",
    "idClienteInterno":          "internal_client_id",
    "internalClientId":          "internal_client_id",
    "addenda":                   "addenda",
    "comentarios":               "transfer_comments",
    "transferComments":          "transfer_comments",
}

_EXTRACT_FIELD_MAP: Dict[str, str] = {
    "nroExtracto":               "statement_number",
    "statementNumber":           "statement_number",
    "fechaOperacion":            "operation_date",
    "operationDate":             "operation_date",
    "totalMovimientos":          "total_movements",
    "totalMovements":            "total_movements",
    "saldoApertura":             "opening_balance",
    "openingBalance":            "opening_balance",
    "saldoCierre":               "ending_balance",
    "endingBalance":             "ending_balance",
    "codigoOperacionIB":         "operation_code_ib",
    "operationCodeIb":           "operation_code_ib",
    "codigoOperacionBanco":      "operation_code_bank",
    "operationCodeBank":         "operation_code_bank",
    "descripcionIB":             "code_description_ib",
    "codeDescriptionIb":         "code_description_ib",
    "cuitCliente":               "customer_cuit",
    "customerCuit":              "customer_cuit",
    "descripcionDepositante":    "depositor_description",
    "depositorDescription":      "depositor_description",
    "descripcionBanco":          "code_description_bank",
    "codeDescriptionBank":       "code_description_bank",
    "fechaMovimiento":           "movement_date",
    "movementDate":              "movement_date",
    "fechaReal":                 "real_date_activity",
    "realDateActivity":          "real_date_activity",
    "importe":                   "amount",
    "amount":                    "amount",
    "nroComprobante":            "voucher_number",
    "voucherNumber":             "voucher_number",
    "sucursal":                  "branch_office_activity",
    "branchOfficeActivity":      "branch_office_activity",
    "fechaProceso":              "process_date",
    "processDate":               "process_date",
    "fechaValor":                "value_date",
    "valueDate":                 "value_date",
    "tipoMovimiento":            "debit_credit_type",
    "debitCreditType":           "debit_credit_type",
    "correlativo":               "correlative_number",
    "correlativeNumber":         "correlative_number",
    "cuentaOrigen":              "source_account",
    "sourceAccount":             "source_account",
    "descripcionEstandar":       "code_description_standard",
    "codeDescriptionStandard":   "code_description_standard",
    "codigoBancoEstandar":       "operation_code_bank_standard",
    "operationCodeBankStandard": "operation_code_bank_standard",
}

_VOUCHER_FIELD_MAP: Dict[str, str] = {
    "id":                        "transfer_id",
    "transferId":                "transfer_id",
    "fechaSolicitud":            "request_date",
    "requestDate":               "request_date",
    "descripcionTipo":           "transfer_type_description",
    "transferTypeDescription":   "transfer_type_description",
    "codigoTipo":                "transfer_type_code",
    "transferTypeCode":          "transfer_type_code",
    "nroRed":                    "network_number",
    "networkNumber":             "network_number",
    "importe":                   "amount",
    "amount":                    "amount",
    "moneda":                    "currency",
    "currency":                  "currency",
    "codigoValidacion":          "validation_code",
    "validationCode":            "validation_code",
    "importeTotal":              "total_amount",
    "totalAmount":               "total_amount",
    "comentarios":               "comments",
    "comments":                  "comments",
    "empresaFacturadora":        "billing_company",
    "billingCompany":            "billing_company",
    "clientePagador":            "paying_customer",
    "payingCustomer":            "paying_customer",
}


# ---------------------------------------------------------------------------
# Helpers de aplanado para cuentas crédito/débito en transferencias
# ---------------------------------------------------------------------------

# Sub-claves camelCase/castellano → snake para cuentas anidadas.
# La API real ya devuelve snake_case (credit_account.account_cbu, etc.), por lo
# que para datos reales el flatten es directo; este mapa solo cubre variantes
# legacy. ANTES los helpers solo desempaquetaban camelCase ("creditAccount"),
# así que con la API snake_case las columnas credit_account_*/debit_account_*
# quedaban en NULL: este fix lo corrige soportando ambas convenciones.
_ACCOUNT_SUBKEY_MAP: Dict[str, str] = {
    "cuit":              "customer_cuit",
    "customerCuit":      "customer_cuit",
    "cuitContribuyente": "taxpayer_cuit",
    "cbu":               "account_cbu",
    "accountCbu":        "account_cbu",
    "numeroCuenta":      "account_number",
    "accountNumber":     "account_number",
    "moneda":            "currency",
    "tipoCuenta":        "account_type",
    "accountType":       "account_type",
    "numeroBanco":       "bank_number",
    "bankNumber":        "bank_number",
    "nombreBanco":       "bank_name",
    "bankName":          "bank_name",
    "alias":             "account_label",
    "label":             "account_label",
}


def _pop_first(record: Dict[str, Any], *keys: str) -> Any:
    """Devuelve y elimina el valor del primer key presente; None si ninguno está."""
    for k in keys:
        if k in record:
            return record.pop(k)
    return None


def _flatten_account(record: Dict[str, Any], prefix: str, *source_keys: str) -> None:
    """Aplana una cuenta anidada (credit/debit) a columnas {prefix}_{subclave}.

    Soporta el snake_case real de la API (credit_account.account_cbu) y variantes
    camelCase/castellano legacy via _ACCOUNT_SUBKEY_MAP.
    """
    nested = _pop_first(record, *source_keys)
    if isinstance(nested, dict):
        for k, v in nested.items():
            record[f"{prefix}_{_ACCOUNT_SUBKEY_MAP.get(k, k)}"] = v


def _flatten_blob(record: Dict[str, Any], prefix: str, *source_keys: str) -> None:
    """Aplana un dict anidado a columnas {prefix}_{subclave} y CONSERVA el dict
    original bajo `prefix` para persistirlo además como blob JSON.

    Para addenda / billing_company / paying_customer guardamos ambas
    representaciones: columnas individuales (consultables) + el blob completo.
    La API ya devuelve snake_case, así que el prefijo directo produce los nombres
    finales (addenda_amount, billing_company_due_date, etc.).
    """
    nested = _pop_first(record, *source_keys)
    if isinstance(nested, dict):
        for k, v in nested.items():
            record[f"{prefix}_{k}"] = v
        record[prefix] = nested  # re-inserción para el blob JSON
    elif nested is not None:
        record[prefix] = nested


def _flatten_transfer_accounts(record: Dict[str, Any]) -> Dict[str, Any]:
    """Aplana cuentas crédito/débito y la addenda en una transferencia."""
    _flatten_account(record, "credit_account", "credit_account", "creditAccount", "cuentaCreditora")
    _flatten_account(record, "debit_account", "debit_account", "debitAccount", "cuentaDebitora")
    _flatten_blob(record, "addenda", "addenda")
    return record


def _flatten_voucher_accounts(record: Dict[str, Any]) -> Dict[str, Any]:
    """Aplana cuentas, empresa facturadora, cliente pagador y datos AFIP."""
    _flatten_account(record, "credit_account", "credit_account", "creditAccount", "cuentaCreditora")
    _flatten_account(record, "debit_account", "debit_account", "debitAccount", "cuentaDebitora")
    _flatten_blob(record, "billing_company", "billing_company", "billingCompany", "empresaFacturadora")
    _flatten_blob(record, "paying_customer", "paying_customer", "payingCustomer", "clientePagador")

    afip = _pop_first(record, "afip", "datosAfip")
    if isinstance(afip, dict):
        afip_map = {
            "descripcionConcepto":  "afip_concept_description",
            "codigoControl":        "afip_control_code",
            "nroFormulario":        "afip_nro_formulario",
            "descripcionImpuesto":  "afip_tax_description",
            "nroCuota":             "afip_fee_number",
            "pagDesc":              "afip_pago_desc",
            "nombreProveedor":      "afip_provider_name",
            "codigoConcepto":       "afip_concept_code",
            "codigoImpuesto":       "afip_tax_code",
            "nroVep":               "afip_vep_number",
            "periodoFiscal":        "afip_fiscal_period",
            "codigoProveedor":      "afip_provider_code",
        }
        for k, v in afip.items():
            record[afip_map.get(k, f"afip_{k}")] = v
    return record


# ---------------------------------------------------------------------------
# Cliente principal
# ---------------------------------------------------------------------------

class InterbankingClient:
    """Cliente REST para la API de Interbanking Argentina.

    Todos los métodos devuelven (DataFrame, raw_response_dict) para mantener
    compatibilidad con el servicio unificado y acceso al response completo.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretString,
        service_url: str,
        customer_id: str,
        token_url: str = _DEFAULT_TOKEN_URL,
        api_base_url: str = _DEFAULT_API_BASE,
        grant_type: str = "client_credentials",
        username: Optional[SecretString] = None,
        password: Optional[SecretString] = None,
        scope: str = _DEFAULT_SCOPE,
        page_size: int = 100,
        timeout: int = 60,
    ) -> None:
        """Constructor explícito: todas las credenciales se inyectan.

        Para cargar desde variables de entorno (uso legacy o REPL), usar
        InterbankingClient.from_env(). Esto desacopla el cliente del
        global state de os.environ y lo hace testeable con valores
        controlados sin monkeypatch.
        """
        if not isinstance(client_secret, SecretString):
            raise TypeError("client_secret debe ser SecretString")

        self.client_id     = client_id
        # SEC-07: client_secret nunca debe aparecer como str plano en self.
        self.client_secret = client_secret
        self.service_url   = service_url
        self.customer_id   = customer_id
        self.token_url     = token_url
        self.api_base_url  = api_base_url.rstrip("/")
        self.grant_type    = grant_type
        # username puede contener identificación operativa, password obviamente sensible.
        self.username      = username
        self.password      = password
        self.scope         = scope
        self.page_size     = page_size
        self.timeout       = timeout

        # SEC-07: el access_token también va envuelto. Para usarlo: ._access_token.reveal()
        self._access_token: Optional[SecretString] = None
        self._token_expires_at: Optional[datetime] = None

        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    @classmethod
    def from_env(cls) -> "InterbankingClient":
        """Construye el cliente leyendo de variables de entorno.

        Útil para el código legacy (monolítico, CLI interactiva) que ya
        confía en os.environ. Para Azure Functions preferí construir el
        cliente con los valores resueltos del IbPollerConfig.

        Raises:
            KeyError: si falta alguna variable de entorno requerida.
        """
        _raw_username = os.getenv("IB_USERNAME")
        _raw_password = os.getenv("IB_PASSWORD")
        return cls(
            client_id=os.environ["IB_CLIENT_ID"],
            client_secret=SecretString(os.environ["IB_CLIENT_SECRET"]),
            service_url=os.environ["IB_SERVICE_URL"],
            customer_id=os.environ["IB_CUSTOMER_ID"],
            token_url=os.getenv("IB_TOKEN_URL", _DEFAULT_TOKEN_URL),
            api_base_url=os.getenv("IB_API_BASE_URL", _DEFAULT_API_BASE),
            grant_type=os.getenv("IB_GRANT_TYPE", "client_credentials"),
            username=SecretString(_raw_username) if _raw_username else None,
            password=SecretString(_raw_password) if _raw_password else None,
            scope=os.getenv("IB_SCOPE", _DEFAULT_SCOPE),
            page_size=int(os.getenv("IB_PAGE_SIZE", "100")),
            timeout=int(os.getenv("IB_TIMEOUT_SECONDS", "60")),
        )

    # ------------------------------------------------------------------
    # Autenticación
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Obtiene un access token cacheado. SEC-02: credenciales en body, no en URL.

        Returns:
            access_token como str plano (necesario para construir el header Authorization).
            El valor solo se materializa acá; en el resto del cliente se mantiene
            envuelto en SecretString.
        """
        if (
            self._access_token
            and self._token_expires_at
            and datetime.utcnow() < self._token_expires_at
        ):
            return self._access_token.reveal()

        # SEC-02: estos valores NUNCA deben ir en query string. Usamos `data=`
        # para que requests los serialice como application/x-www-form-urlencoded
        # en el body, conforme RFC 6749 §4.4.2.
        body: Dict[str, Any] = {
            "scope":         self.scope,
            "client_id":     self.client_id,
            "client_secret": self.client_secret.reveal(),
            "grant_type":    self.grant_type,
        }
        if self.grant_type == "password":
            if not self.username or not self.password:
                raise RuntimeError("IB_USERNAME e IB_PASSWORD son requeridos cuando IB_GRANT_TYPE=password")
            body["username"] = self.username.reveal()
            body["password"] = self.password.reveal()

        logger.debug("Obteniendo token Interbanking (grant_type=%s)", self.grant_type)
        try:
            response = self.session.post(
                self.token_url,
                data=body,  # SEC-02: body, NO params
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept":       "application/json",
                    "service":      self.service_url,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            # No re-loggeamos el body ni response.text porque puede contener
            # detalles del request con credenciales en algunos servidores.
            logger.error("Error HTTP obteniendo token Interbanking (status=%s)", response.status_code)
            raise

        data = response.json()
        token_value = data.get("access_token")
        if not token_value:
            raise RuntimeError("Respuesta de token Interbanking sin campo 'access_token'")

        self._access_token = SecretString(token_value)
        expires_in = int(data.get("expires_in", 7200))
        self._token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)
        logger.debug("Token obtenido, expira en %d s", expires_in)
        return token_value

    def _api_headers(self) -> Dict[str, str]:
        # _get_token() devuelve el str plano solo durante la construcción del header.
        # IB NO requiere customer-id en header: va como query param en cada request.
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "client_id":     self.client_id,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    # ------------------------------------------------------------------
    # HTTP helpers
    #
    # IMPORTANTE: la API de Interbanking exige `customer-id` como QUERY PARAM
    # (kebab-case) en cada request a /v1/*. Sin él, devuelve 400 "missing
    # required API parameters". Lo inyectamos automáticamente en _get para
    # que ningún caller pueda olvidárselo. Todos los demás params también
    # van en kebab-case: date-since, date-until, account-number, bank-number,
    # account-type, currency.
    # ------------------------------------------------------------------

    def _invalidate_token(self) -> None:
        """Fuerza un refresh del token en el próximo request.

        IB invalida el access_token de forma intermitente durante paginación
        profunda (se observaron 401 en /movements/anteriores?page=20 a mitad
        de un ciclo). Limpiamos el cache para que _api_headers() pida uno nuevo.
        """
        self._access_token = None
        self._token_expires_at = None

    # Reintentos ante 401 en un mismo GET (refrescando token). IB invalida el
    # token de forma intermitente en paginación profunda (cuentas con muchas
    # páginas), a veces en 401 consecutivos; un solo reintento no alcanzaba.
    _MAX_401_RETRIES = 3

    def _authed_get(self, url: str, params: Optional[Dict[str, Any]]) -> Any:
        """GET autenticado con retry ante 401 (refresca token y reintenta).

        El 401 de IB no está en el status_forcelist del Retry de urllib3 (que
        cubre 429/5xx), así que lo manejamos acá: ante 401, invalidamos el token
        cacheado, pedimos uno nuevo y reintentamos, hasta `_MAX_401_RETRIES`
        veces. Si sigue en 401, propaga. Devuelve el Response ya con
        raise_for_status aplicado.
        """
        attempt = 0
        while True:
            response = self.session.get(
                url, headers=self._api_headers(), params=params, timeout=self.timeout
            )
            if response.status_code == 401 and attempt < self._MAX_401_RETRIES:
                attempt += 1
                logger.warning(
                    "IB respondió 401 en %s (intento %d/%d); refrescando token y reintentando",
                    url, attempt, self._MAX_401_RETRIES,
                )
                self._invalidate_token()
                continue
            response.raise_for_status()
            return response

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.api_base_url}{path}"
        merged_params: Dict[str, Any] = {"customer-id": self.customer_id}
        if params:
            merged_params.update(params)
        logger.debug("GET %s params=%s", url, merged_params)
        return self._authed_get(url, merged_params).json()

    def _get_paginated(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Itera páginas y devuelve todos los registros concatenados.

        IB devuelve los resultados bajo claves específicas según el endpoint:
        - /accounts                            → {"accounts": [...], "general_data": {...}}
        - /accounts/balances                   → {"accounts": [...], "general_data": {...}}
        - /accounts/{id}/movements/{type}      → {"movements_detail": [...], "general_data": {...}}
        - /transfers/details                   → {"transfers": [...], "general_data": {...}}
        - /transfers/vouchers                  → {"transfers": [...], "general_data": {...}}
        - /accounts/{id}/statements            → {"statements": [...], "general_data": {...}}
        """
        base_params = dict(params or {})
        base_params.setdefault("limit", self.page_size)

        all_records: List[Dict[str, Any]] = []
        page = 0

        while True:
            base_params["page"] = page
            raw = self._get(path, base_params)

            records = self._extract_records(raw)
            if not records:
                break

            all_records.extend(records)

            if not self._has_next_page(raw, page, len(records)):
                break

            page += 1

        return all_records

    @staticmethod
    def _extract_records(raw: Any) -> List[Dict[str, Any]]:
        """Extrae la lista de registros de la respuesta, sea cual sea la estructura."""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in (
                # claves reales de IB API
                "accounts", "transfers", "statements", "movements_detail",
                # alternativas posibles
                "data", "results", "items", "content",
                # legacy / castellano (mantenidas por compat con versiones futuras)
                "movimientos", "cuentas", "saldos", "transferencias", "extractos", "comprobantes",
            ):
                if key in raw and isinstance(raw[key], list):
                    return raw[key]
        return []

    def _has_next_page(self, raw: Any, current_page: int, returned: int) -> bool:
        """Detecta si hay más páginas.

        IB usa `general_data.total_rows` como total absoluto. Si el limit por
        request es N y devolvió menos de N, asumimos fin del set.
        """
        if isinstance(raw, dict):
            general = raw.get("general_data") or {}
            if isinstance(general, dict):
                if "total_rows" in general:
                    try:
                        total = int(general["total_rows"])
                        fetched_so_far = (current_page + 1) * self.page_size
                        return fetched_so_far < total
                    except (TypeError, ValueError):
                        pass
            if "totalPages" in raw:
                return current_page + 1 < int(raw["totalPages"])
            if "last" in raw:
                return not raw["last"]
            if "totalElements" in raw:
                fetched_so_far = (current_page + 1) * self.page_size
                return fetched_so_far < int(raw["totalElements"])
        return returned >= self.page_size

    # ------------------------------------------------------------------
    # Métodos públicos
    # ------------------------------------------------------------------

    def get_cuentas(self) -> Tuple["pd.DataFrame", Any]:  # noqa: F821
        """GET /v1/accounts — Lista de cuentas bancarias.

        Devuelve {"general_data": {...}, "accounts": [{bank_number, account_cbu,
        account_cuit, account_label, currency, bank_name, account_number, account_type}]}.
        """
        raw = self._get("/accounts", {"limit": self.page_size, "page": 0})
        records = self._extract_records(raw)

        # La API ya devuelve snake_case; el rename es no-op para esos campos pero
        # cubre cualquier variante camelCase / castellano que la API pueda emitir.
        normalized = [_rename(dict(rec), _ACCOUNT_FIELD_MAP) for rec in records]

        df = _to_df(normalized, _ACCOUNTS_COLS)
        logger.info("get_cuentas: %d cuentas obtenidas", len(df))
        return df, raw

    def get_saldos(
        self,
        date_since: Optional[str] = None,
        date_until: Optional[str] = None,
        account_numbers: Optional[List[str]] = None,
    ) -> Tuple["pd.DataFrame", Any]:  # noqa: F821
        """GET /v1/accounts/balances — Saldos en un rango de fechas y/o cuentas específicas.

        Todos los parámetros son opcionales:
        - Sin fechas devuelve saldos actuales.
        - account_numbers filtra por lista de números de cuenta.

        IB anida `balances` y `historical_balances` dentro de cada account; los aplanamos
        para producir una fila por (cuenta, fecha) en el DataFrame final.
        """
        params: Dict[str, Any] = {}
        if date_since:
            params["date-since"] = date_since
        if date_until:
            params["date-until"] = date_until
        if account_numbers:
            params["account-number"] = list(account_numbers) if isinstance(account_numbers, list) else [account_numbers]

        records = self._get_paginated("/accounts/balances", params)

        # IB devuelve cada cuenta con balances anidados; aplanamos a filas independientes.
        flattened: List[Dict[str, Any]] = []
        for account in records:
            base = {
                "bank_number":     account.get("bank_number"),
                "account_number":  account.get("account_number"),
                "account_type":    account.get("account_type"),
                "currency":        account.get("currency"),
                "account_label":   account.get("account_label"),
                "account_name":    account.get("account_name"),
                "row_date":        account.get("row_date"),
                "message":         account.get("message"),
            }
            balances = account.get("balances") or {}
            if isinstance(balances, dict):
                base.update({
                    "countable_balance":         balances.get("countable_balance"),
                    "initial_operating_balance": balances.get("initial_operating_balance"),
                    "current_operating_balance": balances.get("current_operating_balance"),
                    "projected_balance_24hs":    balances.get("projected_balance_24hs"),
                    "projected_balance_48hs":    balances.get("projected_balance_48hs"),
                })
            flattened.append(base)
            for hist in (account.get("historical_balances") or []):
                if not isinstance(hist, dict):
                    continue
                row = base.copy()
                row.update({
                    "operation_date": hist.get("operation_date"),
                    "day_balance":    hist.get("day_balance"),
                    "total_debits":   hist.get("total_debits"),
                    "total_credits":  hist.get("total_credits"),
                    "is_historical":  True,
                })
                flattened.append(row)

        df = _to_df(flattened, _BALANCES_COLS)
        logger.info("get_saldos: %d filas [%s → %s]", len(df), date_since, date_until)
        return df, records

    def get_movimientos(
        self,
        account_number: str,
        bank_number: str,
        date_since: str,
        date_until: str,
        movement_type: str = "anteriores",
        version: str = "v2",
        account_type: str = "CC",
        currency: str = "ARS",
    ) -> Tuple["pd.DataFrame", Any]:  # noqa: F821
        """GET /{v1|v2}/accounts/{account_number}/movements/{movement_type} — Movimientos.

        movement_type: "anteriores" | "dia" | "diferidos" | "zughus" (solo v2)
        version:       "v1" | "v2"
        account_type:  "CC" (cta corriente) | "CA" (caja ahorro) | etc.

        Endpoint según versión IB:
            v1 -> /v1/accounts/{account_number}/movements/{movement_type}
            v2 -> /v2/accounts/{account_number}/movements/{movement_type}
        """
        # IB cambia el prefijo de versión por endpoint. Construimos la URL manualmente
        # quitando el /v1 del api_base_url y agregando la versión correcta.
        if self.api_base_url.endswith("/v1"):
            base_without_version = self.api_base_url[:-3]
        else:
            base_without_version = self.api_base_url
        path_for_log = f"/{version}/accounts/{account_number}/movements/{movement_type}"
        full_url = f"{base_without_version}{path_for_log}"

        params: Dict[str, Any] = {
            "customer-id":  self.customer_id,    # se mantiene igual: el query param es la fuente de verdad
            "account-type": account_type,
            "currency":     currency,
            "bank-number":  bank_number,
            "limit":        self.page_size,
        }
        # 'dia' es solo el día actual; las otras opciones aceptan ventana de fechas.
        if movement_type != "dia":
            if date_since:
                params["date-since"] = date_since
            if date_until:
                params["date-until"] = date_until

        # Iteramos paginación manual porque _get_paginated usa _get que asume path
        # relativo a self.api_base_url. Aquí pegamos contra full_url custom.
        all_records: List[Dict[str, Any]] = []
        page = 0
        while True:
            params["page"] = page
            logger.debug("GET %s params=%s", full_url, params)
            raw = self._authed_get(full_url, params).json()
            records = self._extract_records(raw)
            if not records:
                break
            all_records.extend(records)
            if not self._has_next_page(raw, page, len(records)):
                break
            page += 1

        normalized = [_rename(dict(r), _MOVEMENT_FIELD_MAP) for r in all_records]
        # La API NO incluye la cuenta de origen ni el tipo de movimiento dentro de
        # cada registro: los inyectamos desde los parámetros del request, igual que
        # el cliente de referencia (jsontoexcel) que hace mov_df['source_account'] =
        # account_number. Sin esto source_account queda NULL; y como el
        # movement_hash lo usa para deduplicar, movimientos de cuentas distintas
        # con el mismo comprobante/importe colisionarían y se pisarían.
        for rec in normalized:
            rec["source_account"] = account_number
            rec["movement_type"] = movement_type
        df = _to_df(normalized, _MOVEMENTS_COLS)
        logger.info(
            "get_movimientos cuenta=%s banco=%s tipo=%s v=%s: %d movimientos [%s → %s]",
            account_number, bank_number, movement_type, version, len(df), date_since, date_until,
        )
        return df, all_records

    def get_transferencias_detalle(
        self, date_since: str, date_until: str
    ) -> Tuple["pd.DataFrame", Any]:  # noqa: F821
        """GET /v1/transfers/details — Transferencias con detalle de cuentas crédito/débito.

        El endpoint correcto es /transfers/details (no /transfers — el /v1/transfers
        sin /details devuelve 404 en el gateway actual).
        """
        params = {
            "date-since": date_since,
            "date-until": date_until,
        }
        records = self._get_paginated("/transfers/details", params)

        normalized = [_rename(_flatten_transfer_accounts(dict(r)), _TRANSFER_FIELD_MAP) for r in records]
        df = _to_df(normalized, _TRANSFERS_COLS)
        logger.info("get_transferencias_detalle: %d transferencias [%s → %s]", len(df), date_since, date_until)
        return df, records

    def get_comprobantes(
        self, date_since: str, date_until: str
    ) -> Tuple["pd.DataFrame", Any]:  # noqa: F821
        """GET /v1/transfers/vouchers — Comprobantes de transferencias."""
        params = {
            "date-since": date_since,
            "date-until": date_until,
        }
        records = self._get_paginated("/transfers/vouchers", params)

        normalized = [_rename(_flatten_voucher_accounts(dict(r)), _VOUCHER_FIELD_MAP) for r in records]
        df = _to_df(normalized, _VOUCHERS_COLS)
        logger.info("get_comprobantes: %d comprobantes [%s → %s]", len(df), date_since, date_until)
        return df, records

    def get_extractos(
        self,
        account_number: str,
        bank_number: str,
        date_since: str,
        date_until: str,
        account_type: str = "CC",
        currency: str = "ARS",
    ) -> Tuple["pd.DataFrame", Any]:  # noqa: F821
        """GET /v1/accounts/{account_number}/statements — Extractos de una cuenta.

        IB devuelve {"statements":[{statement_info, "movement_detail":[{movements}]}]};
        aplanamos para producir una fila por movimiento, conservando los datos del extracto.
        """
        params: Dict[str, Any] = {
            "account-type": account_type,
            "bank-number":  bank_number,
            "currency":     currency,
            "date-since":   date_since,
            "date-until":   date_until,
        }
        records = self._get_paginated(f"/accounts/{account_number}/statements", params)

        # Aplanar: cada movement_detail dentro de statement genera una fila propia.
        flattened: List[Dict[str, Any]] = []
        for statement in records:
            if not isinstance(statement, dict):
                continue
            base = {
                "statement_number": statement.get("statement_number"),
                "operation_date":   statement.get("operation_date"),
                "total_movements":  statement.get("total_movements"),
                "opening_balance":  statement.get("opening_balance"),
                "ending_balance":   statement.get("ending_balance"),
            }
            movement_detail = statement.get("movement_detail") or statement.get("movements_detail") or []
            if movement_detail:
                for mov in movement_detail:
                    if not isinstance(mov, dict):
                        continue
                    row = base.copy()
                    row.update(mov)
                    flattened.append(row)
            else:
                flattened.append(base)

        normalized = [_rename(dict(r), _EXTRACT_FIELD_MAP) for r in flattened]
        # Igual que en movimientos: source_account no viene en el detalle del
        # extracto, se inyecta desde el parámetro account_number (lo mismo hace el
        # cliente de referencia). El extract_hash lo usa para deduplicar por cuenta.
        for rec in normalized:
            rec["source_account"] = account_number
        df = _to_df(normalized, _EXTRACTS_COLS)
        logger.info(
            "get_extractos cuenta=%s banco=%s: %d filas [%s → %s]",
            account_number, bank_number, len(df), date_since, date_until,
        )
        return df, records

    # ------------------------------------------------------------------
    # Utilidades extras
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        """Prueba la conectividad obteniendo el token y consultando cuentas.

        Retorna True si la conexión es exitosa, False en caso contrario.
        Usado por main_interactive.py al inicio del programa.
        """
        try:
            token = self._get_token()
            if not token:
                logger.error("test_connection: no se obtuvo token")
                print("❌ No se pudo obtener el token de autenticación")
                return False

            cuentas_df, _ = self.get_cuentas()
            print(f"✅ Conexión exitosa — {len(cuentas_df)} cuenta(s) encontrada(s)")
            logger.info("test_connection OK — %d cuentas", len(cuentas_df))
            return True

        except requests.exceptions.HTTPError as exc:
            logger.error("test_connection HTTP error: %s", exc)
            print(f"❌ Error HTTP al conectar: {exc}")
            return False
        except requests.exceptions.ConnectionError:
            logger.error("test_connection: no hay conexión a internet/API")
            print("❌ No se puede alcanzar la API de Interbanking. Verifica tu conexión.")
            return False
        except Exception as exc:
            logger.exception("test_connection: error inesperado")
            print(f"❌ Error inesperado: {exc}")
            return False

    def export_to_excel(
        self,
        date_since: str,
        date_until: str,
        limit: int = 500,
        use_pagination: bool = True,
        output_dir: Optional[str] = None,
    ) -> str:
        """Exporta cuentas, saldos, movimientos, transferencias y extractos a un Excel.

        Parámetros
        ----------
        date_since      Fecha inicio (YYYY-MM-DD)
        date_until      Fecha fin    (YYYY-MM-DD)
        limit           Máximo de registros por hoja (solo para información al usuario)
        use_pagination  Si True usa paginación completa; si False solo primera página
        output_dir      CAL-08: carpeta destino. Si None, usa la env var OUTPUT_DIR
                        y si tampoco existe, el directorio actual (compat).

        Retorna
        -------
        Ruta absoluta del archivo .xlsx generado.
        """
        try:
            import openpyxl  # noqa: F401 — validar que está disponible
        except ImportError as exc:
            raise RuntimeError("Instala openpyxl para exportar a Excel: pip install openpyxl") from exc

        # CAL-08: resolver output_dir; crear si no existe.
        target_dir = output_dir or os.getenv("OUTPUT_DIR") or "."
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"No se pudo crear el directorio de salida '{target_dir}': {exc}") from exc

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(
            target_dir,
            f"interbanking_export_{date_since}_{date_until}_{timestamp}.xlsx",
        )

        # Respaldar configuración de paginación y ajustar si no se usa
        original_page_size = self.page_size
        if not use_pagination:
            self.page_size = limit

        try:
            print("📊 Obteniendo cuentas...")
            cuentas_df, _ = self.get_cuentas()

            print("💰 Obteniendo saldos...")
            saldos_df, _ = self.get_saldos(date_since=date_since, date_until=date_until)

            print("🔄 Obteniendo transferencias...")
            transferencias_df, _ = self.get_transferencias_detalle(
                date_since=date_since, date_until=date_until
            )

            pd = _pd()  # export_to_excel necesita pandas; lo cargamos acá.

            print("📈 Obteniendo movimientos por cuenta...")
            todos_movimientos: List["pd.DataFrame"] = []  # noqa: F821
            for _, cuenta in cuentas_df.iterrows():
                try:
                    mov_df, _ = self.get_movimientos(
                        account_number=str(cuenta["account_number"]),
                        bank_number=cuenta["bank_number"],
                        date_since=date_since,
                        date_until=date_until,
                        movement_type="anteriores",
                        version="v2",
                    )
                    todos_movimientos.append(mov_df)
                except Exception as exc:
                    logger.warning("export_to_excel: error movimientos cuenta %s: %s", cuenta["account_number"], exc)

            movimientos_df = pd.concat(todos_movimientos, ignore_index=True) if todos_movimientos else pd.DataFrame(columns=_MOVEMENTS_COLS)

            print("📋 Obteniendo extractos por cuenta...")
            todos_extractos: List["pd.DataFrame"] = []  # noqa: F821
            for _, cuenta in cuentas_df.iterrows():
                try:
                    ext_df, _ = self.get_extractos(
                        account_number=str(cuenta["account_number"]),
                        bank_number=cuenta["bank_number"],
                        date_since=date_since,
                        date_until=date_until,
                    )
                    todos_extractos.append(ext_df)
                except Exception as exc:
                    logger.warning("export_to_excel: error extractos cuenta %s: %s", cuenta["account_number"], exc)

            extractos_df = pd.concat(todos_extractos, ignore_index=True) if todos_extractos else pd.DataFrame(columns=_EXTRACTS_COLS)

        finally:
            self.page_size = original_page_size

        print(f"💾 Escribiendo {filename}...")
        with pd.ExcelWriter(filename, engine="openpyxl") as writer:
            cuentas_df.to_excel(writer, sheet_name="Cuentas", index=False)
            saldos_df.to_excel(writer, sheet_name="Saldos", index=False)
            movimientos_df.head(limit).to_excel(writer, sheet_name="Movimientos", index=False)
            transferencias_df.head(limit).to_excel(writer, sheet_name="Transferencias", index=False)
            extractos_df.head(limit).to_excel(writer, sheet_name="Extractos", index=False)

        logger.info("export_to_excel: archivo generado %s", filename)
        return filename
