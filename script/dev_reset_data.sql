/* ====================================================================
   dev_reset_data.sql  —  WIPE de datos para entorno de DESARROLLO.

   Uso intencional:
     Estamos en fase de desarrollo del portal consumidor. Después de
     un deploy con bugs (ver migration_002 + fix de _coerce_value), los
     datos existentes están parcialmente corruptos o no aplicaron por
     truncamiento. Es más limpio borrar todo y dejar que el poller
     vuelva a llenar las tablas con el código bueno.

   ⚠️  NO CORRER EN PRODUCCIÓN REAL una vez que haya datos vivos.
       Este script borra TODAS las filas de las tablas de datos +
       resetea sync_control para forzar re-ingest desde el inicio.

   Estructura:
     1. DELETE en orden child → parent (respeta FKs sin DROP).
     2. RESEED de IDENTITY a 0 para que arranquen en 1 los nuevos rows.
     3. RESET de sync_control: last_successful_sync = NULL para que el
        próximo ciclo del IB poller use el lookback default.
        Para MP, el poller usa lookback_hours desde "ahora", no lee
        sync_control, así que el reset no le afecta.

   Idempotencia:
     Re-correr el script no rompe nada. DELETE sobre tabla vacía es
     no-op. RESEED es no-op si el seed actual ya es 0.
   ==================================================================== */

SET NOCOUNT ON;
GO

-- Required by Azure SQL: DELETE sobre tablas con índices filtrados (p.ej.
-- UQ_ib_transfers_transaction_number WHERE transaction_number IS NOT NULL)
-- exige estos sets, o falla con Msg 1934.
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET NUMERIC_ROUNDABORT OFF;
GO

PRINT '== dev_reset_data.sql — arrancando wipe ==';
GO

/* ----- Children primero (FK constraints) ----- */
DELETE FROM finance.mp_payment_charges;
PRINT 'mp_payment_charges: vacío';
GO

DELETE FROM finance.mp_payment_items;
PRINT 'mp_payment_items: vacío';
GO

DELETE FROM finance.ib_vouchers;
PRINT 'ib_vouchers: vacío';
GO

/* ----- Parents ----- */
DELETE FROM finance.mp_payments;
PRINT 'mp_payments: vacío';
GO

DELETE FROM finance.ib_transfers;
PRINT 'ib_transfers: vacío';
GO

DELETE FROM finance.ib_extracts;
PRINT 'ib_extracts: vacío';
GO

DELETE FROM finance.ib_movements;
PRINT 'ib_movements: vacío';
GO

DELETE FROM finance.ib_balances;
PRINT 'ib_balances: vacío';
GO

DELETE FROM finance.ib_accounts;
PRINT 'ib_accounts: vacío';
GO

/* ----- Tablas de bookkeeping ----- */
DELETE FROM finance.sync_runs;
PRINT 'sync_runs: vacío';
GO

UPDATE finance.sync_control
SET last_successful_sync  = NULL,
    last_attempt_sync     = NULL,
    last_begin_date_used  = NULL,
    last_end_date_used    = NULL,
    last_status           = NULL,
    last_error            = NULL,
    updated_at            = SYSUTCDATETIME();
PRINT 'sync_control: reseteado (last_successful_sync = NULL)';
GO

/* ----- RESEED de IDENTITY columns ----- */
DBCC CHECKIDENT('finance.mp_payment_charges', RESEED, 0) WITH NO_INFOMSGS;
DBCC CHECKIDENT('finance.mp_payment_items',   RESEED, 0) WITH NO_INFOMSGS;
DBCC CHECKIDENT('finance.ib_vouchers',        RESEED, 0) WITH NO_INFOMSGS;
DBCC CHECKIDENT('finance.ib_extracts',        RESEED, 0) WITH NO_INFOMSGS;
DBCC CHECKIDENT('finance.ib_movements',       RESEED, 0) WITH NO_INFOMSGS;
DBCC CHECKIDENT('finance.ib_balances',        RESEED, 0) WITH NO_INFOMSGS;
DBCC CHECKIDENT('finance.sync_runs',          RESEED, 0) WITH NO_INFOMSGS;
PRINT 'IDENTITY columns: reseed a 0';
GO

/* ====================================================================
   Validación final: confirma que todas las tablas quedaron en 0.
   ==================================================================== */
SELECT
    OBJECT_SCHEMA_NAME(t.object_id) AS schema_name,
    t.name                          AS table_name,
    p.rows
FROM sys.tables t
JOIN sys.partitions p
    ON p.object_id = t.object_id
   AND p.index_id IN (0, 1)
WHERE OBJECT_SCHEMA_NAME(t.object_id) = N'finance'
  AND t.name NOT IN (N'sync_control')   -- sync_control conserva las filas (reseteadas, no borradas)
ORDER BY t.name;

SELECT process_name, last_successful_sync, last_status
FROM finance.sync_control
ORDER BY process_name;
GO

PRINT '== dev_reset_data.sql — completado ==';
GO
