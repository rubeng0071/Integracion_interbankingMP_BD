/* ====================================================================
   Migración 002 — Ensanchar columnas de texto a NVARCHAR(MAX).

   Motivo:
     Bancos (Interbanking) y Mercado Pago devuelven descripciones,
     comments, addendas y labels de longitud variable. El schema
     inicial dejó muchas de estas columnas en NVARCHAR(255) o (1000),
     lo que produjo en producción (2026-05-22):

       pyodbc.ProgrammingError: ('String data, right truncation:
         length 1512 buffer 510', 'HY000')

     en upserts a finance.ib_movements y finance.ib_extracts. Hay
     varios truncamientos análogos esperables en otras tablas (ib_transfers
     addenda, ib_vouchers comments, mp_payments description, etc.).

   Estrategia:
     ALTER COLUMN a NVARCHAR(MAX) para todas las columnas de texto libre
     (descripciones, comentarios, labels, mensajes, títulos, URLs). Las
     columnas que son códigos / identificadores cortos (CBU, CUIT,
     account_number, voucher_number, etc.) quedan en su tipo original
     porque son tamaño acotado por contrato y suelen estar indexadas.

   Indexes a respetar (NO ensanchar estas columnas, NVARCHAR(MAX) no
   es indexable como key column):
     - finance.mp_payments.external_reference   (IX_mp_payments_external_reference)
     - finance.mp_payments.store_id, pos_id     (IX_mp_payments_store_pos)
     - finance.ib_transfers.transaction_number  (UQ_ib_transfers_transaction_number)
     - finance.ib_movements.source_account      (IX_ib_movements_source_account_process_date)
     - finance.ib_balances.account_number       (IX_ib_balances_account_row_date)
     - finance.ib_extracts.source_account       (IX_ib_extracts_source_account_operation_date)

   Idempotencia:
     Cada ALTER se ejecuta solo si la columna no es ya NVARCHAR(MAX)
     (max_length = -1 en sys.columns). Re-correr el script no produce
     cambios sobre una base ya migrada.

   Costo:
     ALTER COLUMN ... NVARCHAR(MAX) NULL es metadata-only en SQL Server
     cuando la columna no tiene constraint NOT NULL ni default; el motor
     no reescribe los rows. Tablas grandes terminan en segundos.
   ==================================================================== */

SET NOCOUNT ON;
GO

-- Required by Azure SQL when ALTERing tables that participate in filtered
-- indexes / computed-column indexes / etc. Sin estos sets, falla con Msg 1934.
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET NUMERIC_ROUNDABORT OFF;
GO

-- Helper local: macro replicada en línea (T-SQL no tiene macros reales).
-- El patrón es:
--   IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('finance.X')
--              AND name='col' AND max_length <> -1)
--       ALTER TABLE finance.X ALTER COLUMN col NVARCHAR(MAX) NULL;

/* ----------- finance.ib_accounts ----------- */
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_accounts') AND name = N'account_label' AND max_length <> -1)
    ALTER TABLE finance.ib_accounts ALTER COLUMN account_label NVARCHAR(MAX) NULL;
GO

/* ----------- finance.ib_balances ----------- */
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_balances') AND name = N'account_label' AND max_length <> -1)
    ALTER TABLE finance.ib_balances ALTER COLUMN account_label NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_balances') AND name = N'account_name' AND max_length <> -1)
    ALTER TABLE finance.ib_balances ALTER COLUMN account_name NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_balances') AND name = N'message' AND max_length <> -1)
    ALTER TABLE finance.ib_balances ALTER COLUMN message NVARCHAR(MAX) NULL;
GO

/* ----------- finance.ib_movements ----------- */
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND name = N'depositor_code' AND max_length <> -1)
    ALTER TABLE finance.ib_movements ALTER COLUMN depositor_code NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND name = N'code_description_ib' AND max_length <> -1)
    ALTER TABLE finance.ib_movements ALTER COLUMN code_description_ib NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND name = N'depositor_description' AND max_length <> -1)
    ALTER TABLE finance.ib_movements ALTER COLUMN depositor_description NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND name = N'code_description_bank' AND max_length <> -1)
    ALTER TABLE finance.ib_movements ALTER COLUMN code_description_bank NVARCHAR(MAX) NULL;
GO

/* ----------- finance.ib_transfers ----------- */
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'transfer_type_description' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN transfer_type_description NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'account_label' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN account_label NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'client' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN client NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'addenda' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN addenda NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'transfer_comments' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN transfer_comments NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'credit_account_bank_name' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN credit_account_bank_name NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'credit_account_account_label' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN credit_account_account_label NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'debit_account_bank_name' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN debit_account_bank_name NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND name = N'debit_account_account_label' AND max_length <> -1)
    ALTER TABLE finance.ib_transfers ALTER COLUMN debit_account_account_label NVARCHAR(MAX) NULL;
GO

/* ----------- finance.ib_vouchers ----------- */
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'transfer_type_description' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN transfer_type_description NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'comments' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN comments NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'billing_company' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN billing_company NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'paying_customer' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN paying_customer NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'debit_account_bank_name' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN debit_account_bank_name NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'debit_account_account_label' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN debit_account_account_label NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'afip_concept_description' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN afip_concept_description NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'afip_tax_description' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN afip_tax_description NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'afip_pago_desc' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN afip_pago_desc NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'afip_provider_name' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN afip_provider_name NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'credit_account_bank_name' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN credit_account_bank_name NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_vouchers') AND name = N'credit_account_account_label' AND max_length <> -1)
    ALTER TABLE finance.ib_vouchers ALTER COLUMN credit_account_account_label NVARCHAR(MAX) NULL;
GO

/* ----------- finance.ib_extracts ----------- */
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_extracts') AND name = N'code_description_ib' AND max_length <> -1)
    ALTER TABLE finance.ib_extracts ALTER COLUMN code_description_ib NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_extracts') AND name = N'depositor_description' AND max_length <> -1)
    ALTER TABLE finance.ib_extracts ALTER COLUMN depositor_description NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_extracts') AND name = N'code_description_bank' AND max_length <> -1)
    ALTER TABLE finance.ib_extracts ALTER COLUMN code_description_bank NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_extracts') AND name = N'code_description_standard' AND max_length <> -1)
    ALTER TABLE finance.ib_extracts ALTER COLUMN code_description_standard NVARCHAR(MAX) NULL;
GO

/* ----------- finance.mp_payments -----------
   NOTA: external_reference y store_id NO se ensanchan: tienen índices
   y NVARCHAR(MAX) no es indexable como key column. */
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.mp_payments') AND name = N'description' AND max_length <> -1)
    ALTER TABLE finance.mp_payments ALTER COLUMN description NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.mp_payments') AND name = N'notification_url' AND max_length <> -1)
    ALTER TABLE finance.mp_payments ALTER COLUMN notification_url NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.mp_payments') AND name = N'statement_descriptor' AND max_length <> -1)
    ALTER TABLE finance.mp_payments ALTER COLUMN statement_descriptor NVARCHAR(MAX) NULL;
GO

/* ----------- finance.mp_payment_items ----------- */
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.mp_payment_items') AND name = N'title' AND max_length <> -1)
    ALTER TABLE finance.mp_payment_items ALTER COLUMN title NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.mp_payment_items') AND name = N'description' AND max_length <> -1)
    ALTER TABLE finance.mp_payment_items ALTER COLUMN description NVARCHAR(MAX) NULL;
GO
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.mp_payment_items') AND name = N'picture_url' AND max_length <> -1)
    ALTER TABLE finance.mp_payment_items ALTER COLUMN picture_url NVARCHAR(MAX) NULL;
GO

/* ====================================================================
   Validación post-migración: lista las columnas afectadas y su estado.
   max_length = -1  →  NVARCHAR(MAX)
   ==================================================================== */
SELECT
    OBJECT_SCHEMA_NAME(c.object_id) AS schema_name,
    OBJECT_NAME(c.object_id)        AS table_name,
    c.name                          AS column_name,
    t.name                          AS data_type,
    CASE WHEN c.max_length = -1 THEN 'MAX' ELSE CAST(c.max_length / 2 AS VARCHAR(10)) END AS char_length,
    c.is_nullable
FROM sys.columns c
JOIN sys.types t ON c.user_type_id = t.user_type_id
WHERE OBJECT_SCHEMA_NAME(c.object_id) = N'finance'
  AND t.name = N'nvarchar'
  AND c.name IN (
      -- ib_accounts
      'account_label',
      -- ib_balances
      'account_name', 'message',
      -- ib_movements / ib_extracts
      'depositor_code', 'code_description_ib', 'depositor_description',
      'code_description_bank', 'code_description_standard',
      -- ib_transfers / ib_vouchers
      'transfer_type_description', 'client', 'addenda', 'transfer_comments',
      'credit_account_bank_name', 'credit_account_account_label',
      'debit_account_bank_name', 'debit_account_account_label',
      'comments', 'billing_company', 'paying_customer',
      'afip_concept_description', 'afip_tax_description',
      'afip_pago_desc', 'afip_provider_name',
      -- mp_payments / mp_payment_items
      'description', 'notification_url', 'statement_descriptor',
      'title', 'picture_url'
  )
ORDER BY table_name, column_name;
