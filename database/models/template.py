from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

#: Где шаблон применим.
#:
#: `any` — не «неизвестно», а «годится везде»: «Добрый день, {client_name}!»
#: одинаково хорош и в письме, и в записи ленты. Отдельное значение нужно, чтобы
#: не заводить один и тот же текст дважды ради двух мест.
CHANNEL_EMAIL = "email"
CHANNEL_NOTE = "note"
CHANNEL_ANY = "any"

CHANNELS = (CHANNEL_ANY, CHANNEL_EMAIL, CHANNEL_NOTE)

MAX_CHANNEL = 16
MAX_TEMPLATE_NAME = 200

#: Потолок тела. Шаблон — это типовой ответ, а не документ: двадцати тысяч
#: знаков хватает с многократным запасом, а в MySQL `TEXT` меряется БАЙТАМИ
#: (65 535), и кириллица занимает по два — то есть предел движка ~32 тысячи
#: знаков. Свой потолок стоит заведомо ниже, чтобы отказ приходил словами от
#: нас, а не молчаливым обрезанием от базы.
MAX_TEMPLATE_BODY = 20_000


class MessageTemplate(Base):
    """Типовой текст с подстановками: ответ на заявку, ссылка на доску, напоминание об оплате.

    Три поля и ни одним больше: название (по нему шаблон выбирают), тело (то, что
    подставится) и признак применимости. Тема письма сюда намеренно не заведена —
    шаблон один и тот же нужен и письму, и ленте, а у записи в ленте темы нет.

    Своей истории у шаблона нет и не будет: применённый шаблон превращается в
    обычное письмо или обычную запись ленты, и дальше живёт своей жизнью. Связи
    «эта запись сделана шаблоном №5» тоже нет — она означала бы, что правка
    шаблона задним числом меняет смысл уже отправленного.
    """

    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Название уникально, и это не косметика. Шаблон выбирают из выпадающего
    # списка ГЛАЗАМИ, по имени: два «Напоминания об оплате» в нём означают выбор
    # наугад, а увидеть разницу можно, только открыв оба. Заодно уникальность —
    # единственная настоящая защита от двойного нажатия: засов на кнопке
    # (`lib/guard.ts`) живёт во вкладке, а вкладок бывает две.
    #
    # `ExactString` здесь не нужен, как и у названия роли: это не токен, регистр
    # в нём не значащий, и «Оплата» с «оплата» — одно и то же название.
    name: Mapped[str] = mapped_column(String(MAX_TEMPLATE_NAME), unique=True)
    # Ключ из CHANNELS. Не внешний ключ и не enum: значений три, они заданы
    # кодом, и справочник из трёх строк в базе только добавил бы способ
    # разойтись с кодом.
    #
    # Индекса нет намеренно: значений три на десяток строк — такой индекс
    # планировщик не возьмёт, а взяв, прочитает больше, чем сама таблица.
    channel: Mapped[str] = mapped_column(String(MAX_CHANNEL), default=CHANNEL_ANY)
    # Тело с подстановками вида `{client_name}`. Обычный текст: набор полей
    # закрыт и объявлен в `core/services/template_service.py`, а подстановка
    # вставляется одним проходом и не перечитывается — иначе имя клиента,
    # похожее на подстановку, вытаскивало бы чужое значение.
    body: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
