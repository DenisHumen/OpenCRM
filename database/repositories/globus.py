"""Запросы блока «Глобус»: клиенты с адресом, их бумаги, гости витрин.

Все агрегаты — одним запросом на список, а не по запросу на точку: планета
рисуется целиком, и сорок клиентов означали бы сорок заходов в базу.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from database.models import Board, Client, Deal, Document, ShareLink, ShareView, User
from database.models.document import OPEN_ORDER_STATUSES, ORDER_KINDS
from database.models.pipeline import CLOSED_KINDS, PipelineStage


def klienty_s_mestom(db: Session, predel: int) -> list[Client]:
    """Живые клиенты, у которых есть хоть какое-то место: страна или точка.

    Порядок — от свежих: при упоре в потолок на планету попадут те, с кем
    работают сейчас, а не те, кого завели первыми.
    """
    stmt = (
        select(Client)
        .where(
            Client.deleted_at.is_(None),
            (Client.country != "") | (Client.lat_e7.is_not(None)),
        )
        .order_by(Client.updated_at.desc(), Client.id.desc())
        .limit(predel)
    )
    return list(db.scalars(stmt).all())


def bez_mesta(db: Session) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Client)
            .where(Client.deleted_at.is_(None), Client.country == "", Client.lat_e7.is_(None))
        )
        or 0
    )


def otkrytye_zayavki(db: Session, client_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Клиент → сколько открытых заявок и на какую сумму (минорные единицы)."""
    if not client_ids:
        return {}
    zakrytye = select(PipelineStage.key).where(PipelineStage.kind.in_(CLOSED_KINDS))
    stmt = (
        select(Deal.client_id, func.count(), func.coalesce(func.sum(Deal.amount), 0))
        .where(
            Deal.client_id.in_(client_ids),
            Deal.deleted_at.is_(None),
            Deal.stage.not_in(zakrytye),
        )
        .group_by(Deal.client_id)
    )
    return {row[0]: (int(row[1]), int(row[2])) for row in db.execute(stmt).all()}


def otkrytye_zakazy(db: Session, client_ids: list[int], seychas: datetime) -> dict[int, tuple[int, int]]:
    """Клиент → сколько незакрытых заказов и сколько из них просрочено."""
    if not client_ids:
        return {}
    stmt = (
        select(
            Document.client_id,
            func.count(),
            func.sum(case((Document.due_at < seychas, 1), else_=0)),
        )
        .where(
            Document.client_id.in_(client_ids),
            Document.kind.in_(ORDER_KINDS),
            Document.status.in_(OPEN_ORDER_STATUSES),
        )
        .group_by(Document.client_id)
    )
    return {row[0]: (int(row[1]), int(row[2] or 0)) for row in db.execute(stmt).all()}


def imena_menedzherov(db: Session, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    stmt = select(User.id, User.name).where(User.id.in_(ids))
    return {row[0]: row[1] for row in db.execute(stmt).all()}


def gosti(db: Session, predel: int) -> list[tuple[ShareView, int, str, int | None]]:
    """Просмотры витрин, где браузер назвал часовой пояс.

    Отдаёт саму запись, номер доски, её название и клиента доски — из них
    рисуется точка гостя и линия к клиенту, чью доску смотрели.
    """
    stmt = (
        select(ShareView, Board.id, Board.title, Board.client_id)
        .join(ShareLink, ShareLink.id == ShareView.share_link_id)
        .join(Board, Board.id == ShareLink.board_id)
        .where(ShareView.tz != "", Board.deleted_at.is_(None))
        .order_by(ShareView.viewed_at.desc(), ShareView.id.desc())
        .limit(predel)
    )
    return [(row[0], row[1], row[2], row[3]) for row in db.execute(stmt).all()]


def poslednij_prosmotr(db: Session, share_link_id: int, ip_hash: str) -> ShareView | None:
    """Последний просмотр этого гостя по этой ссылке — ему и дописываем пояс."""
    stmt = (
        select(ShareView)
        .where(ShareView.share_link_id == share_link_id, ShareView.ip_hash == ip_hash)
        .order_by(ShareView.viewed_at.desc(), ShareView.id.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def dosok_s_geo(db: Session) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Board)
            .where(Board.deleted_at.is_(None), Board.geo_enabled.is_(True))
        )
        or 0
    )
