# Infraestructura Azure

Scripts PowerShell idempotentes para crear (y borrar) todos los recursos Azure
que requiere el servicio: Resource Group, Log Analytics, Application Insights,
Key Vault, Storage, SQL Server + DB y las dos Function Apps.

> **Idempotente** significa que podés correr cualquier script N veces sin
> romper nada. Cada uno chequea si el recurso existe antes de intentar
> crearlo, y reaplica configuración (App Settings, RBAC) sin replicar.

## Orden de ejecución (primera vez)

```powershell
.\00-prereqs.ps1                # Valida az login, versión, providers.
.\10-create-foundation.ps1      # RG + Log Analytics + AppInsights + KV + Storage.
.\20-create-sql.ps1             # SQL Server + DB + firewall + admin AAD.
.\30-create-function-mp.ps1     # Function App MP webhook + MI + App Settings.
.\40-create-function-ib.ps1     # Function App IB poller + MI + App Settings.
.\50-load-secrets.ps1           # Cargá .env → Key Vault y RBAC a las MIs.
.\70-create-queue.ps1           # Crea la queue mp-payment-ids del refactor J.
.\60-deploy-code.ps1            # Build del wheel y `func azure functionapp publish`.
```

Tiempo estimado primera vez: 15-25 min. Reaplicaciones: <2 min porque las
operaciones ya hechas se saltan.

## Configuración

Editá `_config.ps1` antes de la primera corrida:

- `$Subscription` — nombre o ID de la suscripción.
- `$Env` — `prod` por default; cambialo a `staging` o `dev` para crear un
  set paralelo de recursos.
- `$Location` — región Azure (default `eastus`).
- Nombres de recursos: hay defaults consistentes con la sección 9.1 de
  `docs/PROYECTO.md`. Si dos environments necesitan nombres distintos,
  los defaults se prefijan con `$Env`.

Los scripts leen `_config.ps1` con `. .\_config.ps1` (dot-source).

## Después del setup

```powershell
# Aplicar el esquema SQL (manualmente con sqlcmd o SSMS):
sqlcmd -S <sql-server>.database.windows.net -d finance -U admin -P '***' `
       -i ..\script\unified_finance_schema.sql

sqlcmd -S <sql-server>.database.windows.net -d finance -U admin -P '***' `
       -i ..\script\create_db_user.sql

sqlcmd -S <sql-server>.database.windows.net -d finance -U admin -P '***' `
       -i ..\script\unified_finance_schema_security_v2.sql
```

Después, recorrer la sección 8 de `docs/PROYECTO.md` para validar el
deploy end-to-end (HMAC con `ngrok`, smoke test del Timer, etc.).

## Borrado total

```powershell
.\99-teardown.ps1               # Te pide confirmación; borra el RG entero.
```

## Limitaciones conocidas

1. **SQL auth en lugar de Managed Identity sobre SQL**: la connection string
   incluye user + password (vive en Key Vault, no en el filesystem).
   Migrar a MI requiere agregar `Authentication=ActiveDirectoryMsi` al conn
   string y dar grant en la DB. Queda como mejora futura.
2. **Storage compartido**: las dos Functions usan el mismo storage account
   (`AzureWebJobsStorage`). Si en algún momento querés aislarlas por
   blast radius, separá el storage en `40-create-function-ib.ps1`.
3. **Sin reglas de alerta**: las alertas de Application Insights se
   configuran en el Bloque 5 (no incluido en este conjunto de scripts).
