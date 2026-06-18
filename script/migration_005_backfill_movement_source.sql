/* ====================================================================
   Migración 005 — Backfill de movement_source en finance.ib_movements.

   Motivo:
     La migración 004 agregó movement_source ('anteriores' | 'dia') pero las
     filas previas al cambio quedaron en NULL. El consumidor (app de cobros)
     filtra los provisionales con `movement_source <> 'dia'`, y en SQL
     `NULL <> 'dia'` evalúa a NULL (no TRUE) → esas filas viejas quedarían
     EXCLUIDAS del filtro. Backfilleamos a 'anteriores' (todas las filas
     históricas son movimientos liquidados por definición) para que la bandera
     quede completa y el filtro sea seguro.

   Idempotencia:
     Solo toca filas con movement_source IS NULL. Re-correr no produce cambios.

   Costo:
     UPDATE masivo sobre ib_movements; en Azure SQL serverless puede tardar
     unos segundos según volumen. No bloquea lecturas largas (snapshot).
   ==================================================================== */

SET NOCOUNT ON;
GO

UPDATE finance.ib_movements
SET movement_source = 'anteriores'
WHERE movement_source IS NULL;
GO

/* Validación: no deben quedar NULLs; ver el reparto final. */
SELECT
    ISNULL(movement_source, '(NULL)') AS movement_source,
    COUNT(*) AS n
FROM finance.ib_movements
GROUP BY movement_source
ORDER BY movement_source;
GO
