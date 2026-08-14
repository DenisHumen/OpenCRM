"""Запросы уборки: что можно вычистить из корзины и во что это обойдётся.

Отдельно от репозиториев досок и клиентов нарочно. Здесь другой взгляд на те же
таблицы — не «покажи карточку», а «найди всё, что подлежит окончательному
удалению, и посчитай, что уйдёт вместе с ним». Смешивать эти два взгляда в одном
файле значит однажды подставить отбор для корзины в обычный список.

**Все вопросы задаются наперёд, по разу на весь список.** Уборка запускается
редко, но именно на той базе, где мусора накопилось много: запрос в цикле там
означает тысячи обращений и минуты ожидания на операции, которая держит
транзакцию открытой.

**Отбор идёт подзапросом с тем же условием, а не списком id.** Список id растёт
вместе с мусором и упирается в предел размера запроса (`max_allowed_packet`)
ровно тогда, когда уборка нужнее всего.
"""

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from database.models import Board, Client, ClientFile, Deal, PipelineStage, Work
from database.models.pipeline import KIND_WON


def doomed_boards(cutoff) -> Select:
    """Подзапрос: id досок, помеченных удалёнными раньше карантина."""
    return select(Board.id).where(Board.deleted_at.is_not(None), Board.deleted_at <= cutoff)


def doomed_clients(cutoff) -> Select:
    """Подзапрос: id клиентов, помеченных удалёнными раньше карантина."""
    return select(Client.id).where(Client.deleted_at.is_not(None), Client.deleted_at <= cutoff)


def boards_to_purge(db: Session, cutoff) -> list[Board]:
    return list(db.scalars(select(Board).where(Board.id.in_(doomed_boards(cutoff)))))


def works_of_doomed_boards(db: Session, cutoff) -> dict[int, list[Work]]:
    grouped: dict[int, list[Work]] = {}
    for work in db.scalars(select(Work).where(Work.board_id.in_(doomed_boards(cutoff)))):
        grouped.setdefault(work.board_id, []).append(work)
    return grouped


def clients_to_purge(db: Session, cutoff) -> list[Client]:
    return list(db.scalars(select(Client).where(Client.id.in_(doomed_clients(cutoff)))))


def clients_with_revenue(db: Session, cutoff) -> set[int]:
    """Клиенты из корзины, у которых есть выигранные заявки.

    Таких не трогаем совсем: закрытая сделка — это деньги, которые фирма
    действительно получила, и то, что карточку клиента потом убрали, их не
    отменяет. Отчёт за прошлый год не должен меняться от уборки диска.
    """
    return set(
        db.scalars(
            select(Deal.client_id)
            .join(PipelineStage, PipelineStage.key == Deal.stage)
            .where(
                Deal.client_id.in_(doomed_clients(cutoff)),
                Deal.deleted_at.is_(None),
                PipelineStage.kind == KIND_WON,
            )
        )
    )


def deals_count_by_client(db: Session, cutoff) -> dict[int, int]:
    """Сколько заявок уйдёт вместе с каждым клиентом.

    Считаем и НАЗЫВАЕМ. Молчать об этом хуже, чем удалить: человек видит
    «удалено 3 клиента» и не знает про сорок заявок, ушедших каскадом.
    """
    rows = db.execute(
        select(Deal.client_id, func.count())
        .where(Deal.client_id.in_(doomed_clients(cutoff)))
        .group_by(Deal.client_id)
    ).all()
    return {client_id: count for client_id, count in rows}


def files_of_doomed_clients(db: Session, cutoff) -> dict[int, list[ClientFile]]:
    grouped: dict[int, list[ClientFile]] = {}
    for file in db.scalars(
        select(ClientFile).where(ClientFile.client_id.in_(doomed_clients(cutoff)))
    ):
        grouped.setdefault(file.client_id, []).append(file)
    return grouped
