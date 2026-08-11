from typing import Callable

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core import permissions
from core.services import modules_service, permissions_service
from database.models import User
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import deals as deals_repo
from web.api import cards, schemas
from web.api.deps import MAX_SEARCH, get_db, require_staff

router = APIRouter(prefix="/search", tags=["search"])

# палитра показывает короткий список: длинные выдачи листают на своих экранах
GROUP_LIMIT = 6

EMPTY = {"items": [], "total": 0, "has_more": False}


def _clients_group(db: Session, query: str | None) -> dict:
    found, has_more = clients_repo.search_top(db, q=query, per_page=GROUP_LIMIT)
    return {
        "items": [schemas.client_out(c) for c in found],
        "total": len(found),
        "has_more": has_more,
    }


def _deals_group(db: Session, user: User, query: str | None) -> dict:
    """Заявки — стержень системы, и в палитре их ищут чаще всего остального.

    Сужение приходит из прав, а не из запроса: у кого нет `deals.view_others`,
    тот и здесь находит только свои. Это не повтор проверки со списка заявок, а
    её обязательная вторая половина — поиск иначе становится самым удобным
    способом обойти ограничение: список показывает три карточки, а Ctrl+K по
    тому же слову — все тридцать, вместе с именами чужих клиентов.

    Суммы прячет то же право (`deals.view_amounts`), что и в карточке. Иначе
    цену работы можно было бы прочесть прямо из выдачи, не открывая заявку, —
    то есть запрет закрывал бы экран, а не деньги.
    """
    found, has_more = deals_repo.search_top(
        db,
        q=query,
        per_page=GROUP_LIMIT,
        only_manager_id=permissions_service.deals_scope(db, user),
    )
    # Имя клиента — одним запросом на всю группу, как в списке заявок. Палитра
    # подписывает им строку: две «Доставки» разных заказчиков иначе не различить.
    names = clients_repo.names_by_ids(db, [deal.client_id for deal in found])
    amounts = permissions_service.sees_amounts(db, user)
    return {
        "items": [
            # Ответственного не спрашиваем: палитра его не показывает, а ради
            # одного поля это ещё один запрос на каждую выдачу.
            schemas.deal_out(deal, names.get(deal.client_id), amounts=amounts)
            for deal in found
        ],
        "total": len(found),
        "has_more": has_more,
    }


def _boards_group(db: Session, query: str | None) -> dict:
    boards, has_more = boards_repo.search_top(db, q=query, per_page=GROUP_LIMIT)
    # Клиент нужен и здесь: палитра подписывает им доску, и без подписи две
    # «Витрины» разных заказчиков в списке не различить.
    return {
        "items": cards.board_cards(db, boards, with_client=True),
        "total": len(boards),
        "has_more": has_more,
    }


def _group(db: Session, user: User, area: str, build: Callable[[], dict]) -> dict:
    """Группа выдачи — или пустота, если её этому сотруднику видеть не положено.

    Порядок проверок тот же, что в `require_perm`: сначала блок, потом право.

    Блок берётся из реестра прав, а не выписывается рядом с каждой группой.
    Список, набранный руками, разошёлся бы с реестром на первом же новом
    разделе — и разошёлся бы молча: лишняя группа в поиске ничем себя не
    выдаёт, она просто продолжает находить то, чего в системе больше нет.
    У несущих блоков (клиенты, заявки) проверка истинна всегда, и это не повод
    делать их исключением: исключение и есть то место, про которое забудут.
    """
    module = permissions.module_of(area)
    if module is not None and not modules_service.is_enabled(db, module):
        return EMPTY
    if not permissions_service.has(db, user, area, permissions.VIEW):
        return EMPTY
    return build()


@router.get("")
def global_search(
    q: str = Query(default="", max_length=MAX_SEARCH),
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Общий поиск для командной палитры (Ctrl+K).

    Пустой запрос — недавние записи, чтобы палитра открывалась не пустой.

    Группа приходит пустой, а не отсутствует: форма ответа одна при любом наборе
    блоков и прав, и клиенту не приходится знать, какие ключи сегодня бывают.

    Пустеет она по двум причинам, и обе обязательны. Выключенный блок — иначе
    доски продолжали бы находиться поиском и уводить в раздел, которого нет ни в
    меню, ни в маршрутах. Отсутствие права — по той же причине и с добавкой:
    поиск иначе стал бы обходом доступов, через который видно имена и названия
    записей из закрытого раздела. Обе проверки собраны в `_group`.

    Страницы здесь нет намеренно: палитра показывает первые несколько строк, а
    листают выдачу на своих экранах. Поэтому и `page` в запрос не приходит —
    номер страницы, взятый снаружи, пришлось бы сторожить от нуля и минуса
    (см. `database/query.py`), а несуществующий параметр сторожить не надо.

    **Точного числа найденного здесь больше нет и не будет.** `total` — это
    длина показанного (не больше `GROUP_LIMIT`), а «есть ли ещё» отвечает
    `has_more`. Точный счёт означал второй полный проход по таблице на каждую
    группу и стоил ровно столько же, сколько сама выборка: на 400 000 заявок
    3128 мс против 3165 мс. Палитра показывает шесть строк и никуда не листает,
    то есть тратились эти секунды на число, которое клиент выбрасывал не глядя.
    Точный `total` живёт на экранах разделов — там из него считается номер
    последней страницы.
    """
    query = q.strip() or None
    return {
        "query": q,
        # Порядок групп — как в левой колонке: клиенты, заявки, доски.
        "clients": _group(db, user, "clients", lambda: _clients_group(db, query)),
        "deals": _group(db, user, "deals", lambda: _deals_group(db, user, query)),
        "boards": _group(db, user, "boards", lambda: _boards_group(db, query)),
    }
