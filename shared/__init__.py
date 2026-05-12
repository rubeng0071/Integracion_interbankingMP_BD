"""Paquete `shared`: código compartido entre componentes.

Cuando se complete el split (Bloque 2 del plan):
    - mp_webhook_function/ importará: shared.db_helpers, shared.azure_secrets, shared.config
    - ib_poller/ importará lo mismo + shared.secret_string

Mientras tanto, los archivos del proyecto monolítico actual también lo usan, así
no hay duplicación de lógica entre la versión actual y la versión Azure.
"""
from __future__ import annotations

__all__ = [
    "db_helpers",
    "secret_string",
    "azure_secrets",
    "config",
]
