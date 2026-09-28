"""Повторы напоминаний: правило RRULE в поясе напоминания (docs/bloki/29 §4)."""

from datetime import datetime

import pytest

from core import exceptions as errors
from core.services import povtor_service as p

KIEV = "Europe/Kyiv"
#: 20 марта 2026, 09:00 по Киеву (зима, UTC+2).
NACHALO = datetime(2026, 3, 20, 7, 0)


def test_kazhdyy_den_v_devyat_ostayotsya_v_devyat_posle_perevoda_chasov():
    """29 марта Киев переходит на UTC+3. Сдвиг сроком в сутки дал бы 10:00 по местному."""
    sleduyushchiy = p.sleduyushchiy("FREQ=DAILY", NACHALO, KIEV, datetime(2026, 3, 30, 5, 0))
    assert sleduyushchiy == datetime(2026, 3, 30, 6, 0)
    assert p.mestnoe(sleduyushchiy, KIEV).hour == 9


def test_kazhdye_28_dney():
    assert p.sleduyushchiy("FREQ=DAILY;INTERVAL=28", NACHALO, KIEV, NACHALO) == datetime(2026, 4, 17, 6, 0)


def test_poslednyaya_pyatnitsa_mesyatsa():
    razy = p.v_okne("FREQ=MONTHLY;BYDAY=-1FR", NACHALO, KIEV, datetime(2026, 3, 1), datetime(2026, 7, 1))
    assert [p.mestnoe(r, KIEV).date().isoformat() for r in razy] == [
        "2026-03-27", "2026-04-24", "2026-05-29", "2026-06-26",
    ]


def test_count_i_until_obryvayut_povtory():
    razy = p.v_okne("FREQ=WEEKLY;COUNT=3", NACHALO, KIEV, datetime(2026, 1, 1), datetime(2027, 1, 1))
    assert len(razy) == 3
    # UNTIL датой — включительно, до конца того дня по местному.
    assert p.sleduyushchiy("FREQ=DAILY;UNTIL=20260322", NACHALO, KIEV, datetime(2026, 3, 21, 7, 0)) == datetime(
        2026, 3, 22, 7, 0
    )
    assert p.sleduyushchiy("FREQ=DAILY;UNTIL=20260322", NACHALO, KIEV, datetime(2026, 3, 22, 7, 0)) is None


def test_posledniy_den_mesyatsa_i_31_chislo():
    """31-е по стандарту пропускает короткие месяцы; «последний день» — это -1."""
    nachalo = datetime(2026, 1, 31, 7, 0)
    tridtsat_pervoe = p.v_okne("FREQ=MONTHLY;BYMONTHDAY=31", nachalo, KIEV, datetime(2026, 1, 1), datetime(2026, 6, 1))
    assert [p.mestnoe(r, KIEV).month for r in tridtsat_pervoe] == [1, 3, 5]
    posledniy = p.v_okne("FREQ=MONTHLY;BYMONTHDAY=-1", nachalo, KIEV, datetime(2026, 1, 1), datetime(2026, 6, 1))
    assert [p.mestnoe(r, KIEV).day for r in posledniy] == [31, 28, 31, 30, 31]


def test_posle_vypolneniya_schitaet_ot_dnya_vypolneniya():
    """«Каждые 28 дней после выполнения»: закрыли 25-го — следующий 22 апреля, в тот же час."""
    sleduyushchiy = p.posle_vypolneniya("FREQ=DAILY;INTERVAL=28", NACHALO, KIEV, datetime(2026, 3, 25, 12, 0), 1)
    assert sleduyushchiy == datetime(2026, 4, 22, 6, 0)
    assert p.posle_vypolneniya("FREQ=DAILY;COUNT=2", NACHALO, KIEV, NACHALO, 2) is None


def test_pravilo_privoditsya_k_odnomu_vidu():
    assert p.razobrat("freq=daily;interval=1") == "FREQ=DAILY"
    assert p.razobrat("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR") == "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    assert p.razobrat("") is None


@pytest.mark.parametrize(
    "pravilo",
    [
        "FREQ=HOURLY",
        "FREQ=DAILY;BYHOUR=9",
        "FREQ=WEEKLY;BYDAY=2TU",
        "FREQ=DAILY;COUNT=2;UNTIL=20270101",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=MONTHLY;BYMONTHDAY=0",
        "INTERVAL=2",
    ],
)
def test_neponyatoe_pravilo_otvergaetsya(pravilo):
    """Молча съеденная часть правила — это напоминание, которое звонит не тогда."""
    with pytest.raises(errors.ValidationError) as otkaz:
        p.razobrat(pravilo)
    assert otkaz.value.code == "povtor_ne_ponyat"


def test_neizvestnyy_poyas_otvergaetsya():
    with pytest.raises(errors.ValidationError):
        p.mestnoe(NACHALO, "Europe/Atlantida")
