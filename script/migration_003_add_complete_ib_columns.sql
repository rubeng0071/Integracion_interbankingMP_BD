/* ====================================================================
   Migración 003 — Columnas faltantes para completar el dataset IB.

   Motivo:
     Comparando contra el cliente de referencia (doc/jsontoexcel, que
     exporta el dataset completo a Excel) detectamos que el pipeline
     descartaba campos que la API SÍ devuelve, porque el cliente
     compartido los filtraba con listas de columnas fijas y porque los
     dicts anidados (addenda, billing_company, paying_customer) se
     guardaban solo como blob JSON sin desglosar.

   Columnas agregadas:
     finance.ib_movements  : grouping_code_standard, code_description_standard,
                             operation_code_standard
     finance.ib_extracts   : grouping_code_ib
     finance.ib_transfers  : addenda_* (14 columnas desglosadas de la addenda)
     finance.ib_vouchers   : debit_account_voucher_number,
                             billing_company_* (6), paying_customer_* (8)

   Nota: las columnas blob existentes (ib_transfers.addenda,
     ib_vouchers.billing_company, ib_vouchers.paying_customer) se
     conservan y se siguen poblando con el JSON completo. Las nuevas
     columnas son el desglose consultable de ese mismo objeto.

   Tipos de fecha:
     addenda_request_date / addenda_issue_date / addenda_due_date /
     billing_company_due_date van como DATETIME2 (el poller las parsea con
     dateutil; si el formato no es parseable, quedan NULL, nunca rompen el
     upsert). addenda_amount va DECIMAL(18,2) como el resto de importes.

   Idempotencia:
     Cada ALTER ADD se ejecuta solo si la columna no existe todavía
     (sys.columns). Re-correr el script no produce cambios.

   Costo:
     ALTER TABLE ADD <col> NULL es metadata-only en SQL Server; no
     reescribe los rows existentes. Termina en milisegundos.
   ==================================================================== */

SET NOCOUNT ON;
GO
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

/* ----------- finance.ib_movements ----------- */
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND name = N'grouping_code_standard')
    ALTER TABLE finance.ib_movements ADD grouping_code_standard NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND name = N'code_description_standard')
    ALTER TABLE finance.ib_movements ADD code_description_standard NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND name = N'operation_code_standard')
    ALTER TABLE finance.ib_movements ADD operation_code_standard NVARCHAR(50) NULL;
GO

/* ----------- finance.ib_extracts ----------- */
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_extracts') AND name = N'grouping_code_ib')
    ALTER TABLE finance.ib_extracts ADD grouping_code_ib NVARCHAR(50) NULL;
GO

/* ----------- finance.ib_transfers (addenda desglosada) ----------- */
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_operation_numer')
    ALTER TABLE finance.ib_transfers ADD addenda_operation_numer NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_payment_receipt')
    ALTER TABLE finance.ib_transfers ADD addenda_payment_receipt NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_amount')
    ALTER TABLE finance.ib_transfers ADD addenda_amount DECIMAL(18,2) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_seller_tax_id')
    ALTER TABLE finance.ib_transfers ADD addenda_seller_tax_id NVARCHAR(20) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_voucher_type')
    ALTER TABLE finance.ib_transfers ADD addenda_voucher_type NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_seller_name')
    ALTER TABLE finance.ib_transfers ADD addenda_seller_name NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_community_code')
    ALTER TABLE finance.ib_transfers ADD addenda_community_code NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_seller_code')
    ALTER TABLE finance.ib_transfers ADD addenda_seller_code NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_sale_point')
    ALTER TABLE finance.ib_transfers ADD addenda_sale_point NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_request_date')
    ALTER TABLE finance.ib_transfers ADD addenda_request_date DATETIME2 NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_issue_date')
    ALTER TABLE finance.ib_transfers ADD addenda_issue_date DATETIME2 NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_seller_company_name')
    ALTER TABLE finance.ib_transfers ADD addenda_seller_company_name NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_voucher_number')
    ALTER TABLE finance.ib_transfers ADD addenda_voucher_number NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda_due_date')
    ALTER TABLE finance.ib_transfers ADD addenda_due_date DATETIME2 NULL;
GO

/* ----------- finance.ib_vouchers ----------- */
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'debit_account_voucher_number')
    ALTER TABLE finance.ib_vouchers ADD debit_account_voucher_number NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'billing_company_billing_company_cuit')
    ALTER TABLE finance.ib_vouchers ADD billing_company_billing_company_cuit NVARCHAR(20) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'billing_company_billing_company_name')
    ALTER TABLE finance.ib_vouchers ADD billing_company_billing_company_name NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'billing_company_billing_account_name')
    ALTER TABLE finance.ib_vouchers ADD billing_company_billing_account_name NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'billing_company_billing_seller')
    ALTER TABLE finance.ib_vouchers ADD billing_company_billing_seller NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'billing_company_billing_account_id')
    ALTER TABLE finance.ib_vouchers ADD billing_company_billing_account_id NVARCHAR(100) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'billing_company_due_date')
    ALTER TABLE finance.ib_vouchers ADD billing_company_due_date DATETIME2 NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer_voucher_number')
    ALTER TABLE finance.ib_vouchers ADD paying_customer_voucher_number NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer_debit_bank')
    ALTER TABLE finance.ib_vouchers ADD paying_customer_debit_bank NVARCHAR(100) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer_company_name')
    ALTER TABLE finance.ib_vouchers ADD paying_customer_company_name NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer_linkage_code')
    ALTER TABLE finance.ib_vouchers ADD paying_customer_linkage_code NVARCHAR(50) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer_account_cuit')
    ALTER TABLE finance.ib_vouchers ADD paying_customer_account_cuit NVARCHAR(20) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer_account_cbu')
    ALTER TABLE finance.ib_vouchers ADD paying_customer_account_cbu NVARCHAR(32) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer_account_label')
    ALTER TABLE finance.ib_vouchers ADD paying_customer_account_label NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer_customer_cuit')
    ALTER TABLE finance.ib_vouchers ADD paying_customer_customer_cuit NVARCHAR(20) NULL;
GO

/* ====================================================================
   Validación post-migración: lista las columnas nuevas y su tipo.
   ==================================================================== */
SELECT
    OBJECT_NAME(c.object_id)        AS table_name,
    c.name                          AS column_name,
    t.name                          AS data_type,
    CASE WHEN c.max_length = -1 THEN 'MAX' ELSE CAST(c.max_length / 2 AS VARCHAR(10)) END AS char_length
FROM sys.columns c
JOIN sys.types t ON c.user_type_id = t.user_type_id
WHERE c.object_id IN (
        OBJECT_ID(N'finance.ib_movements'),
        OBJECT_ID(N'finance.ib_extracts'),
        OBJECT_ID(N'finance.ib_transfers'),
        OBJECT_ID(N'finance.ib_vouchers'))
  AND c.name IN (
      'grouping_code_standard', 'code_description_standard', 'operation_code_standard',
      'grouping_code_ib',
      'addenda_operation_numer', 'addenda_payment_receipt', 'addenda_amount',
      'addenda_seller_tax_id', 'addenda_voucher_type', 'addenda_seller_name',
      'addenda_community_code', 'addenda_seller_code', 'addenda_sale_point',
      'addenda_request_date', 'addenda_issue_date', 'addenda_seller_company_name',
      'addenda_voucher_number', 'addenda_due_date',
      'debit_account_voucher_number',
      'billing_company_billing_company_cuit', 'billing_company_billing_company_name',
      'billing_company_billing_account_name', 'billing_company_billing_seller',
      'billing_company_billing_account_id', 'billing_company_due_date',
      'paying_customer_voucher_number', 'paying_customer_debit_bank',
      'paying_customer_company_name', 'paying_customer_linkage_code',
      'paying_customer_account_cuit', 'paying_customer_account_cbu',
      'paying_customer_account_label', 'paying_customer_customer_cuit'
  )
ORDER BY table_name, column_name;
