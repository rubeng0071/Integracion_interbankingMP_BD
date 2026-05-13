"""Tests para shared.secret_string.

El objetivo central: garantizar que el valor real NUNCA aparece en
representaciones de texto generadas por accidente (str/repr/format/json/pickle).
"""
from __future__ import annotations

import json
import pickle

import pytest

from shared.secret_string import SecretString, reveal


SECRET = "APP_USR_super_secret_token_xyz_123"


# ---------------------------------------------------------------------------
# Constructor y reveal
# ---------------------------------------------------------------------------


def test_init_acepta_str() -> None:
    s = SecretString(SECRET)
    assert s.reveal() == SECRET


def test_init_rechaza_no_str() -> None:
    with pytest.raises(TypeError, match="espera str"):
        SecretString(123)  # type: ignore[arg-type]


def test_init_acepta_string_vacio() -> None:
    s = SecretString("")
    assert s.reveal() == ""
    assert bool(s) is False


# ---------------------------------------------------------------------------
# Representaciones que NO deben filtrar
# ---------------------------------------------------------------------------


def test_str_devuelve_placeholder() -> None:
    s = SecretString(SECRET)
    assert str(s) == SecretString.PLACEHOLDER
    assert SECRET not in str(s)


def test_repr_devuelve_placeholder() -> None:
    s = SecretString(SECRET)
    assert SECRET not in repr(s)
    assert SecretString.PLACEHOLDER in repr(s)


def test_format_devuelve_placeholder() -> None:
    s = SecretString(SECRET)
    assert f"token={s}" == f"token={SecretString.PLACEHOLDER}"
    assert format(s, "") == SecretString.PLACEHOLDER
    assert format(s, ">30") == SecretString.PLACEHOLDER
    assert SECRET not in f"{s!r}"


def test_dict_repr_no_filtra() -> None:
    """`logger.info(f"config={obj}")` con un objeto que contiene secrets."""
    config_like = {"mp_token": SecretString(SECRET), "log_level": "INFO"}
    rendered = repr(config_like)
    assert SECRET not in rendered
    assert SecretString.PLACEHOLDER in rendered


def test_pickle_no_serializa_valor_real() -> None:
    """Pickle se usa en multiprocessing y caches; debe ser seguro."""
    s = SecretString(SECRET)
    blob = pickle.dumps(s)
    assert SECRET.encode("utf-8") not in blob
    restored = pickle.loads(blob)
    assert isinstance(restored, SecretString)
    # Al des-pickle queda el placeholder, no el valor original.
    assert restored.reveal() == SecretString.PLACEHOLDER


def test_json_dumps_default_str_no_filtra() -> None:
    """json.dumps con default=str cae a __str__, que devuelve placeholder."""
    payload = {"token": SecretString(SECRET)}
    serialized = json.dumps(payload, default=str)
    assert SECRET not in serialized
    assert SecretString.PLACEHOLDER in serialized


def test_json_dumps_sin_default_falla_explicitamente() -> None:
    """SecretString NO es serializable nativamente: forzamos a usar default=str.

    Si alguien hace json.dumps(secret) sin default, queremos que falle ruidosa-
    mente. Mucho mejor que un fallback silencioso que filtre el valor.
    """
    with pytest.raises(TypeError):
        json.dumps({"token": SecretString(SECRET)})


# ---------------------------------------------------------------------------
# Operadores
# ---------------------------------------------------------------------------


def test_bool_true_si_no_vacio() -> None:
    assert bool(SecretString("x")) is True
    assert bool(SecretString("")) is False


def test_len_devuelve_longitud_real() -> None:
    """len() expone longitud (no contenido) para validar tamaño mínimo."""
    s = SecretString(SECRET)
    assert len(s) == len(SECRET)


def test_eq_compara_valor_real() -> None:
    assert SecretString("abc") == SecretString("abc")
    assert SecretString("abc") != SecretString("abd")


def test_eq_no_compara_contra_str() -> None:
    """No queremos que `secret == "abc"` accidentalmente funcione."""
    s = SecretString("abc")
    # NotImplemented hace que Python pruebe el reverso; str no sabe comparar
    # con SecretString, así que la igualdad vale False.
    assert (s == "abc") is False


def test_hash_consistente_con_eq() -> None:
    s1 = SecretString("abc")
    s2 = SecretString("abc")
    assert hash(s1) == hash(s2)
    # Puede usarse como key de dict.
    d = {s1: "v"}
    assert d[s2] == "v"


# ---------------------------------------------------------------------------
# reveal() helper
# ---------------------------------------------------------------------------


def test_reveal_helper_con_secret() -> None:
    assert reveal(SecretString(SECRET)) == SECRET


def test_reveal_helper_con_str_plano() -> None:
    """Útil en sitios legacy donde el valor puede venir como str crudo."""
    assert reveal("plain_string") == "plain_string"
    assert reveal(None) is None
