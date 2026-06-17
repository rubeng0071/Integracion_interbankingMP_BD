/* ====================================================================
   SEC-06 — Usuario de aplicación con permisos MÍNIMOS sobre `finance`.

   Crea un login + user dedicado para el servicio. Solo puede leer/escribir
   sobre el schema `finance`; NO puede crear/borrar tablas, no tiene db_owner,
   no puede tocar otros schemas, no puede leer master/system tables.

   Ejecutar como:
     - Azure SQL: en la base `master` para CREATE LOGIN, luego en la base de
       negocio para CREATE USER + GRANT.
     - SQL Server on-prem: idem (master para login, base de negocio para user).

   Antes de correr en producción:
     1. Cambiar el password (mínimo 16 chars, mayúsculas + minúsculas + dígitos + símbolos).
     2. Si se usa Managed Identity en Azure, reemplazar la sección de LOGIN/USER por:
            CREATE USER [<nombre-managed-identity>] FROM EXTERNAL PROVIDER;
        y eliminar todo lo de PASSWORD.

   Para revocar (rollback):
     USE finance_db; DROP USER finance_svc;
     USE master;     DROP LOGIN finance_svc;
   ==================================================================== */

/* --------- 1) Crear LOGIN (correr en master en Azure SQL) --------- */
USE master;
GO

IF NOT EXISTS (SELECT 1 FROM sys.sql_logins WHERE name = N'finance_svc')
BEGIN
    -- TODO: reemplazar el password antes de ejecutar.
    CREATE LOGIN finance_svc
        WITH PASSWORD = N'CambiarEsto-Min16-Chars#2026',
             CHECK_POLICY = ON,
             CHECK_EXPIRATION = OFF;
END
GO

/* --------- 2) Crear USER en la base de negocio --------- */
-- TODO: cambiar el USE por el nombre real de la base.
-- USE finance_db;
-- GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'finance_svc')
BEGIN
    CREATE USER finance_svc FOR LOGIN finance_svc;
END
GO

/* --------- 3) Permisos mínimos sobre el schema `finance` --------- */
-- Lectura/escritura sobre todas las tablas del schema (incluye las nuevas).
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::finance TO finance_svc;

-- Necesario para usar MERGE y procedimientos del schema.
GRANT EXECUTE ON SCHEMA::finance TO finance_svc;

-- Necesario para SET IDENTITY_INSERT, alterar trigger state, etc.
-- (Si NO se usa, se puede comentar para reducir aún más permisos.)
-- GRANT ALTER ON SCHEMA::finance TO finance_svc;

/* --------- 4) Denegar explícitamente lo peligroso --------- */
-- NOTA: DENY CONTROL es meta-permiso (incluye SELECT/INSERT/UPDATE/DELETE).
-- Si lo agregás aquí, el GRANT SELECT/INSERT/UPDATE/DELETE del paso 3
-- queda anulado y `finance_svc` no puede leer ni escribir nada.
-- Para bloquear solo cambios estructurales, dejá únicamente ALTER y REFERENCES.
DENY ALTER, REFERENCES ON SCHEMA::finance TO finance_svc;

-- Bloquear acceso a metadata sensible (Azure SQL ya lo restringe, pero por las dudas).
DENY VIEW SERVER STATE TO finance_svc;
DENY VIEW DATABASE STATE TO finance_svc;
GO

/* --------- 5) Validación: lista los permisos efectivos --------- */
SELECT
    pr.name        AS principal_name,
    pe.permission_name,
    pe.state_desc,
    pe.class_desc,
    OBJECT_NAME(pe.major_id) AS object_name,
    SCHEMA_NAME(pe.major_id) AS schema_name
FROM sys.database_permissions pe
JOIN sys.database_principals pr ON pr.principal_id = pe.grantee_principal_id
WHERE pr.name = N'finance_svc'
ORDER BY pe.permission_name;
GO
