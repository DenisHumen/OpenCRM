"""Отчёт продаж для сводки: тепловая карта дней, матрица месяцев, города.

**Чем меряем — не наш выбор.** Ответ живёт в `finance_service.bazis_vyruchki`
и только там: касса, если блок финансов включён, иначе выигранные заявки.
Реши этот вопрос заново здесь — и на одной странице встанут рядом два разных
ответа на «сколько мы продали», ровно та беда, с которой всё началось
(разбор — `core/services/finance_service.py`, «чем меряем выручку»).

Отсюда и счёты: суммы и число событий берутся теми же вызовами, что у отчёта
о выручке (`reports.money_by_month`, `finance.postupleniya_po_mesyatsam`) —
оба принимают любые окна, а не только месяцы. Свои запросы здесь только на
то, чего у отчёта нет: первые события дня для подсказки и разрез по городам.

Рост считается **к тому же числу прошлого периода**, а не к периоду целиком:
седьмого сентября месяц прожит на пятую часть, и сравнение с полным августом
показывало бы падение на восемьдесят процентов каждое первое число.

Разбор виджета — `docs/dizayn/27-otchyot-prodazh.md`.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from core.services import finance_service
from core.utils import now_utc
from database.models.pipeline import KIND_WON
from database.repositories import finance as finance_repo
from database.repositories import otchyot_prodazh as otchyot_repo
from database.repositories import reports as reports_repo

#: Клеток тепловой карты: восемь рядов по восемь.
DNEY = 64
#: Месяцев в точечной матрице.
MESYATSEV = 3
#: Городов под показателями.
GORODOV = 3
#: Сколько событий дня попадает во всплывающее поле. Не все: в клетке бывает и
#: пятьдесят, а поле — подсказка, а не список.
V_DNE = 3


def _polnoch(den: date) -> datetime:
    return datetime(den.year, den.month, den.day)


def _rost_bp(stalo: int, bylo: int) -> int | None:
    """Рост в базисных пунктах. `None` — сравнивать не с чем.

    Целочисленно, без единого деления через `float`: деньги в этой системе не
    проходят через дробное ни на секунду. Округление к ближайшему делается
    прибавкой половины делителя, знак выносится — иначе `//` у спада округлял
    бы всегда вниз, и «−19.6%» превращалось бы в «−19.61%».
    """
    if bylo <= 0:
        return None
    raznitsa = (stalo - bylo) * 10_000
    znak = -1 if raznitsa < 0 else 1
    return znak * ((abs(raznitsa) + bylo // 2) // bylo)


def _mesyats_nazad(den: date, skolko: int) -> date:
    """Первое число месяца, отстоящего на `skolko` назад."""
    god, mesyats = den.year, den.month - skolko
    while mesyats <= 0:
        mesyats += 12
        god -= 1
    return date(god, mesyats, 1)


def _tot_zhe_den(den: date, god: int, mesyats: int) -> date:
    """То же число другого месяца. Тридцать первого мая в апреле нет — берём
    последний день, а не уезжаем в май."""
    return date(god, mesyats, min(den.day, monthrange(god, mesyats)[1]))


def _po_oknam(db: Session, kassa: bool, okna: list[tuple[datetime, datetime]]) -> list[tuple[int, int]]:
    """[(сколько событий, сумма), …] по окнам — нынешним счётом выручки.

    Окна обязаны идти по возрастанию и не пересекаться: `CASE` относит строку к
    ПЕРВОМУ подошедшему окну, и месяц, вложенный в год, просто не посчитался бы.
    """
    if not okna:
        return []
    if kassa:
        dengi = finance_service.postupleniya_po_mesyatsam(db, okna) or {}
        return [
            (int(dengi.get(i, {}).get("count", 0)), int(dengi.get(i, {}).get("total", 0)))
            for i in range(len(okna))
        ]
    dengi = reports_repo.money_by_month(db, okna)
    return [
        (
            int(dengi.get((i, KIND_WON), {}).get("count", 0)),
            int(dengi.get((i, KIND_WON), {}).get("total", 0)),
        )
        for i in range(len(okna))
    ]


def _sutki(ot: date, do: date) -> list[tuple[datetime, datetime]]:
    """Суточные окна [ot; do] включительно."""
    okna = []
    den = ot
    while den <= do:
        okna.append((_polnoch(den), _polnoch(den + timedelta(days=1))))
        den += timedelta(days=1)
    return okna


def _itog(db: Session, kassa: bool, bylo_ot: date, bylo_do: date, stalo_ot: date, stalo_do: date) -> dict:
    """Показатель «сейчас против того же куска прошлого периода».

    Оба окна — одним запросом: они не пересекаются и идут по возрастанию.
    """
    (_, bylo), (_, stalo) = _po_oknam(
        db,
        kassa,
        [
            (_polnoch(bylo_ot), _polnoch(bylo_do + timedelta(days=1))),
            (_polnoch(stalo_ot), _polnoch(stalo_do + timedelta(days=1))),
        ],
    )
    return {"summa_minor": stalo, "rost_bp": _rost_bp(stalo, bylo), "bylo_minor": bylo}


def otchyot(db: Session, seychas: datetime | None = None) -> dict:
    """Всё, что рисуют оба вида отчёта, одним ответом."""
    seychas = seychas or now_utc()
    segodnya = seychas.date()
    kassa = finance_service.bazis_vyruchki(db) == finance_service.BAZIS_KASSA

    # Дни считаются один раз на обе карточки: 64 клетки — подмножество трёх
    # месяцев, кроме первых чисел, когда три календарных месяца короче 64 дней.
    nachalo_mesyatsev = _mesyats_nazad(segodnya, MESYATSEV - 1)
    nachalo_dney = min(nachalo_mesyatsev, segodnya - timedelta(days=DNEY - 1))
    okna = _sutki(nachalo_dney, segodnya)
    po_dnyam = {
        (nachalo_dney + timedelta(days=i)).isoformat(): znachenie
        for i, znachenie in enumerate(_po_oknam(db, kassa, okna))
    }

    pervye = (
        finance_repo.pervye_postupleniya_dney(db, okna[0][0], okna[-1][1], V_DNE)
        if kassa
        else otchyot_repo.pervye_vyigrannye_dney(db, okna[0][0], okna[-1][1], V_DNE)
    )

    kletki = []
    for shag in range(DNEY):
        den = (segodnya - timedelta(days=shag)).isoformat()
        skolko, summa = po_dnyam.get(den, (0, 0))
        kletki.append(
            {
                "den": den,
                "znachenie": skolko,
                "summa_minor": summa,
                "sobytiya": [
                    {"nomer": nomer, "nazvanie": nazvanie, "summa_minor": summa_sobytiya}
                    for nomer, nazvanie, summa_sobytiya in pervye.get(den, ())
                ],
            }
        )

    # Матрица: три месяца, старший первым — как на образце.
    mesyatsy = []
    for nazad in range(MESYATSEV - 1, -1, -1):
        nachalo = _mesyats_nazad(segodnya, nazad)
        dney_v_mesyatse = monthrange(nachalo.year, nachalo.month)[1]
        dni = [
            po_dnyam.get(date(nachalo.year, nachalo.month, chislo).isoformat(), (0, 0))[1]
            for chislo in range(1, dney_v_mesyatse + 1)
        ]
        mesyatsy.append({"mesyats": nachalo.isoformat(), "summa_minor": sum(dni), "dni": dni})

    proshlyy_mesyats = _mesyats_nazad(segodnya, 1)
    za_mesyats = _itog(
        db, kassa,
        proshlyy_mesyats, _tot_zhe_den(segodnya, proshlyy_mesyats.year, proshlyy_mesyats.month),
        date(segodnya.year, segodnya.month, 1), segodnya,
    )
    proshlyy_god = segodnya.year - 1
    za_god = _itog(
        db, kassa,
        date(proshlyy_god, 1, 1), _tot_zhe_den(segodnya, proshlyy_god, segodnya.month),
        date(segodnya.year, 1, 1), segodnya,
    )

    nachalo_goda = _polnoch(date(segodnya.year, 1, 1))
    zavtra = _polnoch(segodnya + timedelta(days=1))
    goroda = (
        finance_repo.goroda_postupleniy(db, nachalo_goda, zavtra, GORODOV)
        if kassa
        else otchyot_repo.goroda_vyigrannyh(db, nachalo_goda, zavtra, GORODOV)
    )

    return {
        # Чем меряется — экран называет это словами: молчаливая подмена одного
        # счёта другим и есть та беда, ради которой заведён `bazis_vyruchki`.
        "money_basis": finance_service.BAZIS_KASSA if kassa else finance_service.BAZIS_ZAYAVKI,
        "kletki": kletki,
        "mesyatsy": mesyatsy,
        "za_mesyats": za_mesyats,
        "za_god": za_god,
        "goroda": [{"gorod": gorod, "summa_minor": summa} for gorod, summa in goroda],
    }
