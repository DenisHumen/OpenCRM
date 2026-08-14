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


def kinds_by_key(db: Session) -> dict[str, str]:
    """{ключ этапа: его тип} — весь справочник, включая архивные.

    Нужен там, где раньше стояло соединение `deals JOIN pipeline_stages ON
    key = stage`. Соединение ради двух десятков строк справочника обходится
    дорого, и не размером, а планом: увидев любой индекс с `stage`,
    планировщик начинает вести отчёт ОТ справочника — «пять этапов наружу, по
    восемьдесят тысяч заявок на каждый» — вместо узкого окна по `closed_at`.
    Замерено на 400 000 заявок: счёт закрытых за месяц 195 мс соединением
    против 6.0 мс списком ключей, деньги за месяц — 220 против 9.8 мс.

    Оценка строк у такого плана — 68 при настоящих 78 000: у индекса по
    `stage` пять разных значений, и статистика по-прежнему хранит СРЕДНЕЕ на
    значение. Это та же ловушка, из-за которой в f9b41c7e2d08 отказались от
    пары «живые + свежие», — и на MySQL она никуда не делась.

    Архивные включены намеренно: соединение их тоже не отсеивало, а закрытые
    сделки стоят как раз в архивных этапах чаще всего. Отсеять их здесь значило
    бы тихо занизить выручку за прошлые месяцы.
    """
    return {
        key: kind
        for key, kind in db.execute(select(PipelineStage.key, PipelineStage.kind)).all()
    }


def keys_of_kinds(db: Session, kinds) -> list[str]:
    """Ключи этапов перечисленных типов — тем же справочником и тем же правилом."""
    wanted = set(kinds)
    return [key for key, kind in kinds_by_key(db).items() if kind in wanted]
