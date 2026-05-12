# interbanking-mp-shared

Código compartido entre `mp_webhook_function/` e `ib_poller/`.

## Módulos

| Módulo | Qué hace | IDs del plan |
|---|---|---|
| `secret_string` | `SecretString` con `__repr__`/`__str__` redactados para evitar filtraciones | SEC-07 |
| `azure_secrets` | `AzureSecretsClient` con Key Vault + fallback a env vars | SEC-04 |
| `config` | `AppConfig.from_env()` con validación fail-fast y reporte agregado | CAL-11 |
| `db_helpers` | `to_str()`, `execute_upsert()`, `sanitize_to_json()` | CAL-02, CAL-03, SEC-03 |

## Construir el wheel

Desde la raíz del repo (donde vive `pyproject.toml`):

```powershell
python -m pip install --upgrade build
python -m build --wheel
```

Salida: `./dist/interbanking_mp_shared-0.1.0-py3-none-any.whl`

O más fácil, desde Windows:

```powershell
.\build_shared_wheel.ps1
```

Esto además copia el wheel a `mp_webhook_function/` y a `ib_poller/` para que el
`func pack` de Azure Functions lo incluya en el deploy.

## Uso

```python
from shared.secret_string import SecretString
from shared.azure_secrets import default_secrets_client
from shared.config import AppConfig
from shared.db_helpers import to_str, execute_upsert, sanitize_to_json

config = AppConfig.from_env()
sql_conn_str = config.sql_connection_string.reveal()  # solo donde se necesita
```
