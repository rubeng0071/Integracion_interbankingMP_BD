"""Tests para la fase 'dia' (movimientos del día corriente) del IB poller.

IB sólo expone los movimientos del día en curso en el feed 'dia'; recién
pasan a 'anteriores' al día hábil siguiente. El poller ahora lee también
'dia' y escribe esas filas PROVISIONALES marcadas con movement_source='dia'.

Estos tests verifican `_build_movement_row` en aislamiento:
    - el hash de las filas 'dia' está namespaceado (no colisiona con
      'anteriores' ni entre sí dentro del mismo lote),
    - se les estampa movement_date desde process_date (IB lo manda None),
    - quedan marcadas con movement_source correcto.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

# Importar ib_processor desde ib_poller/.
_IB_DIR = Path(__file__).resolve().parent.parent / "ib_poller"
if str(_IB_DIR) not in sys.path:
    sys.path.insert(0, str(_IB_DIR))

from ib_processor import IBProcessor  # noqa: E402


@pytest.fixture
def proc() -> IBProcessor:
    # _build_movement_row no toca self.db ni self.client; instanciamos sin
    # __init__ para no necesitar secretos ni conexión SQL en el test.
    return IBProcessor.__new__(IBProcessor)


def _acc() -> pd.Series:
    return pd.Series({"account_number": "000689940317", "bank_number": "007"})


def test_anteriores_marca_source_y_estampa_fechas(proc: IBProcessor) -> None:
    row = pd.Series({
        "source_account": "000689940317", "voucher_number": "90765",
        "process_date": "2026-06-16", "amount": 116506,
        "debit_credit_type": "C", "operation_code_ib": "917151",
        "branch_office_activity": "ZHBB0000", "correlative_number": "12",
        "movement_date": "2026-06-16",
    })
    r = proc._build_movement_row(row, _acc(), "anteriores")
    assert r["movement_source"] == "anteriores"
    assert len(r["movement_hash"]) == 64
    assert r["movement_date"] == datetime(2026, 6, 16)


def test_dia_estampa_movement_date_desde_process_date(proc: IBProcessor) -> None:
    # En el feed 'dia' IB no asigna movement_date y el process_date trae hora.
    row = pd.Series({
        "source_account": "000689940317", "voucher_number": "0",
        "process_date": "2026-06-17T13:53:45", "amount": 436570.18,
        "debit_credit_type": "C", "operation_code_ib": "144",
        "branch_office_activity": None, "correlative_number": None,
        "movement_date": None,
    })
    r = proc._build_movement_row(row, _acc(), "dia")
    assert r["movement_source"] == "dia"
    assert r["movement_date"] == datetime(2026, 6, 17, 13, 53, 45)


def test_dia_sin_process_date_cae_en_hoy(proc: IBProcessor) -> None:
    row = pd.Series({
        "source_account": "000689940317", "voucher_number": "0",
        "process_date": None, "amount": 100.0,
        "debit_credit_type": "D", "operation_code_ib": "M04",
        "branch_office_activity": None, "correlative_number": None,
        "movement_date": None,
    })
    r = proc._build_movement_row(row, _acc(), "dia")
    # Sin process_date, movement_date no puede quedar None (rompería el
    # filtrado por fecha); cae en "hoy".
    assert r["movement_date"] is not None
    assert isinstance(r["movement_date"], datetime)


def test_dia_hash_es_estable_entre_ciclos(proc: IBProcessor) -> None:
    # CRÍTICO para el consumidor (cobros): el MISMO movimiento 'dia' debe
    # producir el MISMO movement_hash en cada ciclo, así el MERGE actualiza
    # in-place y movement_id no cambia. El hash ya no depende de un índice de
    # lote (que mutaba entre ciclos).
    base = {
        "source_account": "000689940317", "voucher_number": "0",
        "process_date": "2026-06-17T13:53:45", "amount": 436570.18,
        "debit_credit_type": "C", "operation_code_ib": "144",
        "branch_office_activity": None, "correlative_number": None,
        "movement_date": None,
    }
    h1 = proc._build_movement_row(pd.Series(base), _acc(), "dia")["movement_hash"]
    h2 = proc._build_movement_row(pd.Series(base), _acc(), "dia")["movement_hash"]
    assert h1 == h2  # estable entre "ciclos"


def test_dia_hash_no_colisiona_con_anteriores(proc: IBProcessor) -> None:
    base = {
        "source_account": "000689940317", "voucher_number": "0",
        "process_date": "2026-06-17T13:53:45", "amount": 436570.18,
        "debit_credit_type": "C", "operation_code_ib": "144",
        "branch_office_activity": None, "correlative_number": None,
        "movement_date": None,
    }
    ant = proc._build_movement_row(pd.Series(base), _acc(), "anteriores")
    dia = proc._build_movement_row(pd.Series(base), _acc(), "dia")
    # Mismos campos pero distinto feed => hashes distintos (prefijo "dia").
    assert ant["movement_hash"] != dia["movement_hash"]


def test_dia_idem_distinto_seq_no_colapsan(proc: IBProcessor) -> None:
    # Dos movimientos del día IDÉNTICOS en todos los campos (IB no asigna
    # voucher/correlative) deben dar hashes DISTINTOS según su seq de
    # desempate -> no se pierden ni rompen la PK de la temp de limpieza.
    base = {
        "source_account": "2580096723", "voucher_number": "0",
        "process_date": "2026-06-18T10:00:00", "amount": 7922.56,
        "debit_credit_type": "C", "operation_code_ib": "144",
        "branch_office_activity": None, "correlative_number": None,
        "movement_date": None,
    }
    h0 = proc._build_movement_row(pd.Series(base), _acc(), "dia", 0)["movement_hash"]
    h1 = proc._build_movement_row(pd.Series(base), _acc(), "dia", 1)["movement_hash"]
    assert h0 != h1
    # pero el MISMO seq (mismo movimiento entre ciclos) -> mismo hash
    h0b = proc._build_movement_row(pd.Series(base), _acc(), "dia", 0)["movement_hash"]
    assert h0 == h0b


def test_dia_distintos_movimientos_distinto_hash(proc: IBProcessor) -> None:
    # Dos movimientos del día que difieren en importe deben tener hash distinto
    # (no colapsan), aunque ya no haya índice de lote.
    a = {
        "source_account": "000689940317", "voucher_number": "0",
        "process_date": "2026-06-17T13:53:45", "amount": 100.0,
        "debit_credit_type": "C", "operation_code_ib": "144",
        "branch_office_activity": None, "correlative_number": None,
        "movement_date": None,
    }
    b = dict(a, amount=200.0)
    ha = proc._build_movement_row(pd.Series(a), _acc(), "dia")["movement_hash"]
    hb = proc._build_movement_row(pd.Series(b), _acc(), "dia")["movement_hash"]
    assert ha != hb
