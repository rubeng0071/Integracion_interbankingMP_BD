/* ====================================================================
   Migración 004 — Columna movement_source en finance.ib_movements.

   Motivo:
     El poller sólo consultaba el feed 'anteriores' de Interbanking
     (movimientos ya liquidados). IB expone los movimientos del día en
     curso ÚNICAMENTE en el feed 'dia'; recién pasan a 'anteriores' al
     día hábil siguiente. Resultado: el día corriente nunca se
     sincronizaba (lag de ~1 día hábil), verificado contra el extracto
     de Banco Galicia.

     Se agregó al poller una segunda fase que lee el feed 'dia' y
     escribe esas filas PROVISIONALES marcadas con movement_source='dia'.
     Cada ciclo el poller las borra y reescribe enteras (DELETE WHERE
     movement_source='dia' + reinsert). Las filas liquidadas quedan con
     movement_source='anteriores'.

   Columnas agregadas:
     finance.ib_movements : movement_source NVARCHAR(12) NULL

   Índice:
     IX_ib_movements_source_dia — índice filtrado sobre las filas 'dia'
     para que el DELETE/recambio por ciclo sea barato.

   Idempotencia:
     El ALTER se ejecuta sólo si la columna no existe; el CREATE INDEX
     sólo si no existe. Re-correr el script no produce cambios.

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
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'finance.ib_movements') AND name = N'movement_source')
    ALTER TABLE finance.ib_movements ADD movement_source NVARCHAR(12) NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_ib_movements_source_dia'
      AND object_id = OBJECT_ID(N'finance.ib_movements')
)
    CREATE INDEX IX_ib_movements_source_dia
        ON finance.ib_movements (movement_source)
        WHERE movement_source = 'dia';
GO

/* ====================================================================
   Validación post-migración.
   ==================================================================== */
SELECT
    OBJECT_NAME(c.object_id)        AS table_name,
    c.name                          AS column_name,
    t.name                          AS data_type,
    CASE WHEN c.max_length = -1 THEN 'MAX' ELSE CAST(c.max_length / 2 AS VARCHAR(10)) END AS char_length
FROM sys.columns c
JOIN sys.types t ON c.user_type_id = t.user_type_id
WHERE c.object_id = OBJECT_ID(N'finance.ib_movements')
  AND c.name = 'movement_source'
ORDER BY table_name, column_name;
