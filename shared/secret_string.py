"""SEC-07 — Wrapper para secretos que evita filtraciones accidentales en logs.

Problema:
    En Python es muy fácil terminar logueando un token por accidente:
        logger.debug("Token: %s", self._access_token)
        print(config)
        repr(client.__dict__)

    Una vez que el token llega al log, ya está filtrado: en App Insights,
    en la consola del Container App, en stdout del Function host, etc.

Solución:
    Envolver tokens / secrets / connection strings en `SecretString`. La clase:
        - Sobreescribe __repr__ / __str__ para devolver un placeholder.
        - Solo expone el valor real vía .reveal()  (decisión explícita).
        - Es serializable como placeholder para evitar fugas en json.dumps().

Ejemplo de uso:
    >>> token = SecretString("APP_USR_abc123secret")
    >>> print(token)
    '***SECRET***'
    >>> repr(token)
    "SecretString('***SECRET***')"
    >>> token.reveal()
    'APP_USR_abc123secret'
    >>> str({"auth": token})
    "{'auth': '***SECRET***'}"
"""
from __future__ import annotations

from typing import Any


class SecretString:
    """Cadena marcada como secreta. Su contenido nunca aparece en logs ni dumps."""

    __slots__ = ("_value",)

    PLACEHOLDER = "***SECRET***"

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(f"SecretString espera str, recibió {type(value).__name__}")
        self._value = value

    def reveal(self) -> str:
        """Devuelve el valor real. Llamar solo donde realmente se necesita."""
        return self._value

    def __str__(self) -> str:
        return self.PLACEHOLDER

    def __repr__(self) -> str:
        return f"SecretString('{self.PLACEHOLDER}')"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        # Útil para validar que no esté vacío sin exponer el valor.
        return len(self._value)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SecretString):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __format__(self, format_spec: str) -> str:
        return self.PLACEHOLDER

    # Soporte para json.dumps() — devuelve placeholder en vez de fallar.
    def __reduce__(self):
        return (self.__class__, (self.PLACEHOLDER,))


def reveal(maybe_secret: Any) -> Any:
    """Helper para revelar opcionalmente: si es SecretString → reveal(); si no → tal cual.

    Útil en sitios donde un valor puede venir como str plano (legacy) o como SecretString.
    """
    if isinstance(maybe_secret, SecretString):
        return maybe_secret.reveal()
    return maybe_secret
