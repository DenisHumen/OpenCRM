"""Отчёты: воронка, выручка, источники — и выгрузка каждого в CSV.

Роуты тонкие: период разбирает и считает `core/services/report_service.py`.
Здесь только приём параметров и отдача файла.

Период приходит датами (`from`, `to`) и смещением зоны браузера, а не готовыми
метками времени. Так ссылка на выгрузку остаётся обычным адресом, который можно
положить в `href` и переслать, а границы дня всё равно считаются по местному
календарю — см. `parse_period`.
"""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from core.services import permissions_service, report_service, settings_service
from database.models import User
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(require_module("reports"))],
)


class Period:
    """Общие параметры периода для всех отчётов сразу.

    Отдельным классом-зависимостью, а не пятью одинаковыми аргументами в каждом
    роуте: разъехавшиеся значения по умолчанию у трёх отчётов на одном экране —
    это три разных периода под одной подписью.
    """

    def __init__(
        self,
        date_from: str | None = Query(default=None, alias="from"),
        date_to: str | None = Query(default=None, alias="to"),
        # Минуты из Date#getTimezoneOffset(): сколько прибавить к местному
        # времени, чтобы получить UTC. Для Киева летом это -180.
        tz_offset: int = Query(default=0, ge=-840, le=840),
    ):
        self.start, self.end, self.start_day, self.end_day = report_service.parse_period(
            date_from, date_to, tz_offset
        )
        self.tz_offset = tz_offset

    def envelope(self, db: Session) -> dict:
        """Период и валюта в каждом ответе.

        Отчёт без подписи периода — просто набор чисел: сохранённый экран или
        распечатку потом невозможно соотнести ни с чем.
        """
        return {
            "from": self.start_day.isoformat(),
            "to": self.end_day.isoformat(),
            "currency": settings_service.get_all(db).get("currency", "USD"),
        }


def _csv_response(content: bytes, name: str, period: Period) -> Response:
    # Имя файла с периодом: в папке «Загрузки» через месяц лежит пять выгрузок,
    # и «reports.csv (3)» не отвечает, какая из них за июль.
    filename = f"{name}-{period.start_day.isoformat()}_{period.end_day.isoformat()}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Отчёт зависит от данных, которые меняются каждый день; кэш браузера
            # отдал бы вчерашний файл по той же ссылке.
            "Cache-Control": "no-store",
        },
    )


@router.get("/funnel")
def funnel_report(
    period: Period = Depends(),
    _: User = Depends(require_perm("reports", "view")),
    db: Session = Depends(get_db),
):
    return {**period.envelope(db), **report_service.funnel(db, period.start, period.end)}


@router.get("/funnel.csv")
def funnel_export(
    period: Period = Depends(),
    user: User = Depends(require_perm("reports", "view")),
    db: Session = Depends(get_db),
):
    data = report_service.funnel(db, period.start, period.end)
    return _csv_response(report_service.funnel_csv(data, user.locale), "funnel", period)


# Отчёт по выручке — это деньги целиком, а не отчёт, в котором среди прочего
# есть суммы. Прятать в нём числа значило бы отдавать пустую таблицу; честнее
# закрыть его целиком тем же правом, что и суммы.
@router.get("/revenue")
def revenue_report(
    period: Period = Depends(),
    _: User = Depends(require_perm("reports", "view_amounts")),
    db: Session = Depends(get_db),
):
    data = report_service.revenue(db, period.start_day, period.end_day, period.tz_offset)
    return {**period.envelope(db), **data}


@router.get("/revenue.csv")
def revenue_export(
    period: Period = Depends(),
    user: User = Depends(require_perm("reports", "view_amounts")),
    db: Session = Depends(get_db),
):
    data = report_service.revenue(db, period.start_day, period.end_day, period.tz_offset)
    return _csv_response(report_service.revenue_csv(data, user.locale), "revenue", period)


def _without_money(data: dict) -> dict:
    """Источники без выручки: сколько заявок пришло — видно, почём — нет.

    Отчёт по источникам отвечает на два вопроса сразу: откуда приходят и сколько
    приносят. Первый нужен и тому, у кого нет права на суммы, поэтому здесь
    именно сужение, а не отказ, — в отличие от выручки, где без сумм не осталось
    бы ничего.

    Строки лежат под ключом `items` — тем самым, который отдаёт
    `report_service.sources`. Сужение по ключу `rows`, которого в ответе нет,
    зануляло только итог: на экране «выручка —», а в каждой строке настоящие
    деньги, и в выгрузке тоже. Пустует итог, а не данные — самый незаметный вид
    отказа из возможных, потому что выглядит он как работающий.
    """
    return {
        **data,
        "items": [{**row, "revenue": None} for row in data.get("items", [])],
        "revenue_total": None,
    }


@router.get("/sources")
def sources_report(
    period: Period = Depends(),
    user: User = Depends(require_perm("reports", "view")),
    db: Session = Depends(get_db),
):
    data = report_service.sources(db, period.start, period.end)
    if not permissions_service.sees_amounts(db, user, "reports"):
        data = _without_money(data)
    return {**period.envelope(db), **data}


@router.get("/sources.csv")
def sources_export(
    period: Period = Depends(),
    user: User = Depends(require_perm("reports", "view")),
    db: Session = Depends(get_db),
):
    data = report_service.sources(db, period.start, period.end)
    if not permissions_service.sees_amounts(db, user, "reports"):
        # Выгрузка обязана совпадать с экраном: иначе право обходится кнопкой
        # «скачать», и это самый вероятный обход из всех.
        data = _without_money(data)
    return _csv_response(report_service.sources_csv(data, user.locale), "sources", period)
