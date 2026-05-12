/* ====================================================================
   Capa de seguridad + performance para el schema `finance`.

   Este script es ADITIVO: corre DESPUÉS de unified_finance_schema.sql.
   No borra ni recrea tablas; solo agrega indexes, compresión y guías de
   Always Encrypted. Es idempotente (cada bloque verifica existencia).

   Bloques:
     OPS-05: Indexes de soporte para queries frecuentes.
     OPS-06: Page compression sobre tablas con raw_json (NVARCHAR(MAX) suele
             comprimirse 3x-6x con DATA_COMPRESSION = PAGE en SQL Server).
     SEC-08: Guías para activar Always Encrypted sobre columnas PII críticas.
             (No se aplica automáticamente; requiere Column Master Key en
             Azure Key Vault y configuración manual en el portal Azure SQL.)

   Compatibilidad:
     - Azure SQL Database: TODO funciona, incluido Always Encrypted.
     - SQL Server 2016+ on-prem: indexes y compresión funcionan; Always Encrypted
       requiere edición Enterprise/Developer/Standard 2016 SP1+.
   ==================================================================== */

USE [TODO_REEMPLAZAR_NOMBRE_BASE];
GO

/* ====================================================================
   OPS-05 — Indexes para queries frecuentes
   ==================================================================== */

-- ib_movements: filtros típicos por cuenta (account_cbu) y por fecha de proceso.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_ib_movements_account_cbu' AND object_id = OBJECT_ID(N'finance.ib_movements'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_ib_movements_account_cbu
        ON finance.ib_movements(account_cbu)
        INCLUDE (process_date, amount, debit_credit_type, voucher_number);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_ib_movements_process_date' AND object_id = OBJECT_ID(N'finance.ib_movements'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_ib_movements_process_date
        ON finance.ib_movements(process_date DESC)
        INCLUDE (account_cbu, amount, operation_code_ib);
END
GO

-- mp_payments: filtro por status + fecha es la query de conciliación más usada.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_mp_payments_status_date_created' AND object_id = OBJECT_ID(N'finance.mp_payments'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_mp_payments_status_date_created
        ON finance.mp_payments(status, date_created DESC)
        INCLUDE (transaction_amount, payer_id, external_reference);
END
GO

-- mp_payments: lookup por payer_id (reportes de comportamiento de pagador).
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_mp_payments_payer_id' AND object_id = OBJECT_ID(N'finance.mp_payments'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_mp_payments_payer_id
        ON finance.mp_payments(payer_id)
        INCLUDE (status, transaction_amount, date_created);
END
GO

-- mp_payments: external_reference es la clave de conciliación con el ERP.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_mp_payments_external_reference' AND object_id = OBJECT_ID(N'finance.mp_payments'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_mp_payments_external_reference
        ON finance.mp_payments(external_reference)
        WHERE external_reference IS NOT NULL;
END
GO

-- sync_runs: dashboard "última ejecución por proceso".
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_sync_runs_process_started' AND object_id = OBJECT_ID(N'finance.sync_runs'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_sync_runs_process_started
        ON finance.sync_runs(process_name, started_at DESC)
        INCLUDE (status, rows_upserted, error_message);
END
GO

/* ====================================================================
   OPS-06 — Page compression para tablas con raw_json
   ==================================================================== */

-- raw_json es NVARCHAR(MAX) con payloads JSON repetitivos: ratio típico 4-6x.
-- ALTER TABLE REBUILD requiere bloqueo de tabla; correr en ventana de mantenimiento.

IF EXISTS (SELECT 1 FROM sys.partitions WHERE object_id = OBJECT_ID(N'finance.mp_payments') AND data_compression_desc <> 'PAGE')
BEGIN
    ALTER TABLE finance.mp_payments REBUILD WITH (DATA_COMPRESSION = PAGE);
END
GO

IF EXISTS (SELECT 1 FROM sys.partitions WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND data_compression_desc <> 'PAGE')
BEGIN
    ALTER TABLE finance.ib_movements REBUILD WITH (DATA_COMPRESSION = PAGE);
END
GO

IF EXISTS (SELECT 1 FROM sys.partitions WHERE object_id = OBJECT_ID(N'finance.ib_balances') AND data_compression_desc <> 'PAGE')
BEGIN
    ALTER TABLE finance.ib_balances REBUILD WITH (DATA_COMPRESSION = PAGE);
END
GO

IF EXISTS (SELECT 1 FROM sys.partitions WHERE object_id = OBJECT_ID(N'finance.ib_transfers') AND data_compression_desc <> 'PAGE')
BEGIN
    ALTER TABLE finance.ib_transfers REBUILD WITH (DATA_COMPRESSION = PAGE);
END
GO

IF EXISTS (SELECT 1 FROM sys.partitions WHERE object_id = OBJECT_ID(N'finance.ib_extracts') AND data_compression_desc <> 'PAGE')
BEGIN
    ALTER TABLE finance.ib_extracts REBUILD WITH (DATA_COMPRESSION = PAGE);
END
GO

/* ====================================================================
   SEC-08 — Always Encrypted sobre columnas PII críticas
   ====================================================================
   IMPORTANTE: estos comandos están comentados a propósito.

   Activar Always Encrypted requiere:
     1. Crear un Column Master Key (CMK) en Azure Key Vault.
     2. Crear un Column Encryption Key (CEK) cifrado con el CMK.
     3. La aplicación necesita acceso al CMK (Managed Identity → Key Vault role:
        "Key Vault Crypto User" sobre la key específica).
     4. Connection string debe incluir: "Column Encryption Setting=Enabled".
     5. ALTER COLUMN para activar requiere que la tabla esté vacía O usar el
        Always Encrypted Wizard del Portal/SSMS para hacer el cifrado en lote.

   Columnas candidatas (priorizadas por sensibilidad):

     Alta sensibilidad (cifrar SÍ o SÍ en producción):
       - finance.mp_payments.card_first_six_digits      (PCI scope reducido)
       - finance.mp_payments.card_last_four_digits      (PCI scope reducido)
       - finance.mp_payments.cardholder_name            (PII tarjetahabiente)
       - finance.mp_payments.cardholder_ident_number    (PII identificación)

     Media sensibilidad (evaluar Dynamic Data Masking primero):
       - finance.mp_payments.payer_email
       - finance.mp_payments.payer_identification_number
       - finance.ib_accounts.account_cbu                (operativo, alto volumen de joins)
       - finance.ib_accounts.account_cuit

   Tipo de cifrado:
     - DETERMINISTIC: permite igualdad y joins (úsalo para CBU/CUIT que se joinean).
     - RANDOMIZED: más seguro pero NO permite búsquedas (úsalo para email/cardholder).

   --- Ejemplo de comandos a correr UNA VEZ que los keys estén configurados ---

   /*
   ALTER TABLE finance.mp_payments
       ALTER COLUMN cardholder_name NVARCHAR(255) COLLATE Latin1_General_BIN2
           ENCRYPTED WITH (
               COLUMN_ENCRYPTION_KEY = CEK_Auto1,
               ENCRYPTION_TYPE = RANDOMIZED,
               ALGORITHM = 'AEAD_AES_256_CBC_HMAC_SHA_256'
           ) NULL
       WITH (ONLINE = OFF);
   GO
   */
   ====================================================================*/

/* ====================================================================
   Validación: muestra el estado de compresión y los indexes creados.
   ==================================================================== */
SELECT
    OBJECT_SCHEMA_NAME(p.object_id) AS schema_name,
    OBJECT_NAME(p.object_id)        AS table_name,
    p.partition_number,
    p.rows,
    p.data_compression_desc
FROM sys.partitions p
WHERE OBJECT_SCHEMA_NAME(p.object_id) = N'finance'
  AND p.index_id IN (0, 1)
ORDER BY table_name;

SELECT
    OBJECT_SCHEMA_NAME(i.object_id) AS schema_name,
    OBJECT_NAME(i.object_id)        AS table_name,
    i.name                          AS index_name,
    i.type_desc,
    i.has_filter,
    i.filter_definition
FROM sys.indexes i
WHERE OBJECT_SCHEMA_NAME(i.object_id) = N'finance'
  AND i.name IS NOT NULL
ORDER BY table_name, index_name;
GO
