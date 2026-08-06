"""Сборка карточек для списков.

Между `schemas` и роутом есть зазор. `schemas` превращает модель в словарь и
базы не знает вовсе — это его достоинство, и тащить туда запросы нельзя. Роут
знает базу, но роутов много, а карточка одна, и три роута собирали её слово в
слово, отличаясь одной строкой.

Здесь живёт то, что не влезает в `schemas` (нужен запрос) и не должно
повторяться в роутах. Правило для этого модуля одно: **спрашивать пачкой**.
Списки на то и списки, что записей в них много.
"""

from sqlalchemy.orm import Session

from database.models import Board, Work
from database.repositories import boards as boards_repo
from database.repositories import clients as clients_repo
from database.repositories import stats as stats_repo
from web.api import schemas


def _pick_cover(board: Board, ready: list[Work]) -> Work | None:
    """То же правило обложки, что и в `board_service.cover_work`, но по уже
    прочитанным работам: назначенная, иначе первая готовая."""
    if board.cover_work_id:
        chosen = next((w for w in ready if w.id == board.cover_work_id), None)
        if chosen is not None:
            return chosen
    return ready[0] if ready else None


def board_cards(db: Session, boards: list[Board], with_client: bool = False) -> list[dict]:
    """Доски для списка: карточка со счётчиками, обложкой и метками ссылок.

    Одна сборка на три места — список досок, дашборд и палитра Ctrl+K. Раньше
    каждое собирало карточку само, слово в слово, и различались они только тем,
    что палитра не спрашивала имя клиента. Три копии одного кода — это не про
    красоту: у карточки досок восемь полей, и добавить девятое в двух местах из
    трёх ничего не стоит.

    Считается пачкой. Поштучно на каждую доску уходило пять запросов — работы,
    обложка, просмотры, ссылки, клиент, — то есть двести пятьдесят запросов на
    страницу из пятидесяти досок. Здесь их пять на любой размер списка.

    `with_client` — не про экономию, а про смысл: списку досок и палитре клиент
    нужен подписью (две «Витрины» разных заказчиков иначе не различить), а на
    дашборде доски свои и подпись там лишняя. Проверено ошибкой: сначала клиента
    убрали и у палитры — тест назвал это сразу.
    """
    if not boards:
        return []

    ids = [board.id for board in boards]
    works_count = boards_repo.works_count_by_board(db, ids)
    ready_works = boards_repo.works_by_board(db, ids, only_ready=True)
    views = stats_repo.views_by_board(db, ids)
    flags = stats_repo.share_flags_by_board(db, ids)

    names: dict[int, str] = {}
    if with_client:
        client_ids = [board.client_id for board in boards if board.client_id]
        names = clients_repo.names_by_ids(db, client_ids)

    cards = []
    for board in boards:
        data = schemas.board_out(board, works_count=works_count.get(board.id, 0))
        cover = _pick_cover(board, ready_works.get(board.id, []))
        data["cover"] = schemas.work_out(cover)["media"] if cover else None
        data["views_count"] = views.get(board.id, 0)
        data.update(
            flags.get(board.id, {"has_links": False, "has_active_link": False, "has_pin": False})
        )
        if with_client:
            data["client_name"] = names.get(board.client_id) if board.client_id else None
        cards.append(data)
    return cards
