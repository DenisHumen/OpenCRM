"""Запросы по шаблонам сообщений.

Список короткий по природе: шаблонов у бизнеса десяток, а не десять тысяч, —
поэтому ни пагинации, ни поиска здесь нет. Появятся — появится и параметр, но
заводить их «на вырост» значит писать код, который никто не проверял.

Отбор по применимости живёт здесь, а не в сервисе, ровно по правилу границы:
условие `channel IN (:канал, 'any')` — это запрос, и править его будут в одном
месте.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import MessageTemplate
from database.models.template import CHANNEL_ANY


def get(db: Session, template_id: int) -> MessageTemplate | None:
    return db.get(MessageTemplate, template_id)


def list_all(db: Session, channel: str | None = None) -> list[MessageTemplate]:
    """Шаблоны по алфавиту. `channel` — где их собираются применить.

    Универсальный шаблон (`any`) попадает в выдачу по любому каналу: он для того
    и заведён, чтобы один и тот же текст не приходилось держать в двух
    экземплярах — письменном и ленточном.

    Сортировка по названию, а не по дате: шаблон ищут глазами по имени, и список,
    переставляющийся после каждой правки, искать мешает. `id` вторым ключом —
    чтобы порядок двух одноимённых шаблонов не плавал между запросами.
    """
    query = select(MessageTemplate)
    if channel is not None and channel != CHANNEL_ANY:
        query = query.where(MessageTemplate.channel.in_((channel, CHANNEL_ANY)))
    return list(db.scalars(query.order_by(MessageTemplate.name.asc(), MessageTemplate.id.asc())))


def name_exists(db: Session, name: str, *, exclude_id: int | None = None) -> bool:
    """Есть ли уже шаблон с таким названием.

    Шаблон выбирают из списка по имени, и два «Напоминания об оплате» в нём
    означают выбор наугад: увидеть разницу можно, только открыв оба.
    """
    query = select(MessageTemplate.id).where(MessageTemplate.name == name)
    if exclude_id is not None:
        query = query.where(MessageTemplate.id != exclude_id)
    return db.scalar(query.limit(1)) is not None
