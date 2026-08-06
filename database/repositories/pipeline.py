"""Запросы по этапам воронки.

Этап — это ключ, которым помечена заявка, и запись в справочнике. Связь между
ними держится не внешним ключом, а строкой: у каждого бизнеса набор этапов свой,
переименовывается на ходу и переживает переезд заявок. Отсюда `keys_in_use` и
`stages_with_keys` — вопросы, которые в этом блоке задают чаще всего: «есть ли
кто-нибудь в этом этапе» и «покажи вот эти по ключам».
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import PipelineStage


def get_by_key(db: Session, key: str) -> PipelineStage | None:
    return db.scalar(select(PipelineStage).where(PipelineStage.key == key))


def list_stages(db: Session, include_archived: bool = False) -> list[PipelineStage]:
    """Порядок — заданный руками, при равенстве по id: доска не должна плясать."""
    stmt = select(PipelineStage)
    if not include_archived:
        stmt = stmt.where(PipelineStage.is_archived.is_(False))
    return list(db.scalars(stmt.order_by(PipelineStage.sort_order.asc(), PipelineStage.id.asc())))


def any_exists(db: Session) -> bool:
    return db.scalar(select(PipelineStage.id).limit(1)) is not None
