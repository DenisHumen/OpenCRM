"""Цифры для утренней сводки — за сутки и «что требует внимания».

Сводка уходит в телеграм раз в день, и смысл её не в содержании, а в самом
факте прибытия: **молчание сводки — уже тревога**. Сломался бот, отозвали
токен, отвалился канал — узнать об этом иначе неоткуда, потому что тревоги в
таком случае тоже не придут, и тишина выглядит как «всё хорошо».

Отсюда требование к запросам здесь: они обязаны быть ДЕШЁВЫМИ и НЕЗАВИСИМЫМИ.
Дешёвыми — потому что сводка считается на живой базе рядом с работой людей, и
утренний отчёт не имеет права стать утренним торможением. Независимыми — потому
что упавший счёт одного раздела не должен унести всю сводку: считающий её
демон ловит отказ по разделу и пишет в сообщение «не сосчиталось», а не молчит
целиком.

**Почему запросы здесь, а не в демоне.** Правило проекта: запросы живут только
в `database/`, снаружи ни `select()`, ни `db.execute`. Каталог `deploy/`, где
живёт демон, под тем же сторожем (`tests/test_db_boundary.py`), и это не
формальность: счёт «сколько заявок закрыли» обязан совпадать с тем, что
показывает экран, а два разных запроса на один вопрос расходятся молча.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import Client, Deal, DealStageChange, PhoneCall, StockMove, Task
from database.repositories import pipeline as pipeline_repo

#: Вид этапа «выиграно». Здесь строкой, а не импортом из сервиса: репозиторий не
#: знает о слое сервисов, и знать не должен.
KIND_WON = "won"


def za_sutki(db: Session, ot: datetime, do: datetime) -> dict:
    """Что произошло за окно: клиенты, заявки, деньги, звонки.

    Окно передаётся снаружи целиком, а не считается здесь от «сейчас». Причина
    та же, по которой границы периода считает `core.utils.konets_dnya`: сводка
    уходит по местному времени владельца, а база живёт в UTC, и перевод — дело
    того, кто знает пояс. Репозиторий, считающий «сутки» сам, однажды посчитает
    их в другом поясе, чем заголовок сообщения.
    """
    kinds = pipeline_repo.kinds_by_key(db)
    vyigrannye = [key for key, kind in kinds.items() if kind == KIND_WON]

    novyh_klientov = db.scalar(
        select(func.count(Client.id)).where(
            Client.deleted_at.is_(None), Client.created_at >= ot, Client.created_at < do
        )
    )
    novyh_zayavok = db.scalar(
        select(func.count(Deal.id)).where(
            Deal.deleted_at.is_(None), Deal.created_at >= ot, Deal.created_at < do
        )
    )

    # Закрытые и выручка — одним запросом, а не двумя: считаются они по одному и
    # тому же набору строк, и разделять их значило бы дважды пройти тот же
    # индекс по `closed_at`.
    zakryto, vyruchka = 0, 0
    if vyigrannye:
        stroka = db.execute(
            select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount), 0)).where(
                Deal.deleted_at.is_(None),
                Deal.stage.in_(vyigrannye),
                Deal.closed_at >= ot,
                Deal.closed_at < do,
            )
        ).first()
        zakryto, vyruchka = int(stroka[0] or 0), int(stroka[1] or 0)

    return {
        "novyh_klientov": int(novyh_klientov or 0),
        "novyh_zayavok": int(novyh_zayavok or 0),
        "zakryto": zakryto,
        # Сумма выигранных заявок. Деньгами она не является — заявку можно
        # выиграть и не получить по ней ни копейки, — поэтому имя честное, а
        # «сколько пришло» кладёт рядом служба (`svodka_service`).
        "vyigrano_minor": vyruchka,
    }


def zayavki_s_sayta(db: Session, ot: datetime, do: datetime, istochnik: str) -> dict:
    """Сколько пришло с сайта и сколько из них никто не тронул.

    «Не тронул» — это отсутствие перехода этапа, СДЕЛАННОГО ЧЕЛОВЕКОМ, а не
    «стоит на первом». Разница существенная: заявку могли передвинуть и вернуть
    обратно, и такая рукам менеджера уже досталась. Журнал переходов отвечает на
    это точно, а положение на доске — нет.

    Отличаем по `from_stage`: заведение заявки тоже пишет строку в журнал (это
    правило проекта — «журнал заполняется всегда»), но у неё `from_stage`
    ПУСТОЙ, потому что уходить было неоткуда. Любая непустая — это уже чей-то
    осознанный перенос. Первая редакция этого не учла и считала нетронутых ноль
    всегда: строка о заведении есть у каждой заявки, и «нет ни одного перехода»
    не выполнялось никогда.
    """
    s_sayta = (
        select(Deal.id)
        .join(Client, Client.id == Deal.client_id)
        .where(
            Deal.deleted_at.is_(None),
            Deal.created_at >= ot,
            Deal.created_at < do,
            Client.source == istochnik,
        )
    )
    vsego = db.scalar(select(func.count()).select_from(s_sayta.subquery())) or 0

    netronutye = db.scalar(
        select(func.count()).select_from(
            s_sayta.where(
                ~select(DealStageChange.id)
                .where(
                    DealStageChange.deal_id == Deal.id,
                    DealStageChange.from_stage != "",
                )
                .exists()
            ).subquery()
        )
    ) or 0
    return {"vsego": int(vsego), "netronutye": int(netronutye)}


def zvonki(db: Session, ot: datetime, do: datetime) -> dict:
    """Звонки за окно и сколько из них пропущено.

    Пропущенный отличается от отвеченного нулевой длительности `outcome`-ом, а
    не длительностью: разговор в одну секунду — это разговор.
    """
    stroka = db.execute(
        select(
            func.count(PhoneCall.id),
            func.sum(func.if_(PhoneCall.outcome == "missed", 1, 0)),
        ).where(PhoneCall.started_at >= ot, PhoneCall.started_at < do)
    ).first()
    return {"vsego": int(stroka[0] or 0), "propushcheno": int(stroka[1] or 0)}


def prosrocheno_napominaniy(db: Session, seychas: datetime) -> int:
    """Сколько напоминаний просрочено — по всем сотрудникам сразу.

    Без ограничения по исполнителю, в отличие от счётчика на экране: сводка
    уходит владельцу, и вопрос у неё другой — не «что должен я», а «что
    провисает в деле».
    """
    return int(
        db.scalar(
            select(func.count(Task.id)).where(
                Task.done_at.is_(None),
                Task.due_at.is_not(None),
                Task.due_at < seychas,
            )
        )
        or 0
    )


def tovary_v_minuse(db: Session) -> int:
    """Сколько товаров ушло в отрицательный остаток.

    Остаток не хранится, он равен `SUM(quantity_milli)` — это правило проекта, и
    здесь оно же. Отрицательная сумма означает, что списали больше, чем было:
    либо приход не завели, либо списали дважды. Само по себе это не ошибка
    системы (склад не запрещает уйти в минус там, где товар физически есть, а
    бумага отстала), но провисеть незамеченным такое не должно.
    """
    ostatki = (
        select(StockMove.product_id.label("tovar"))
        .group_by(StockMove.product_id)
        .having(func.sum(StockMove.quantity_milli) < 0)
        .subquery()
    )
    return int(db.scalar(select(func.count()).select_from(ostatki)) or 0)
