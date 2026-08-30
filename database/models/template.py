from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

#: Где шаблон применим. `any` — не «неизвестно», а «годится везде»: без него
#: один и тот же текст пришлось бы заводить дважды ради двух мест.
CHANNEL_EMAIL = "email"
CHANNEL_NOTE = "note"
CHANNEL_ANY = "any"

CHANNELS = (CHANNEL_ANY, CHANNEL_EMAIL, CHANNEL_NOTE)

MAX_CHANNEL = 16
MAX_TEMPLATE_NAME = 200

#: Потолок тела. Шаблон — типовой ответ, а не документ, двадцати тысяч знаков
#: хватает с многократным запасом. В MySQL `TEXT` меряется БАЙТАМИ (65 535),
#: кириллица по два — предел движка ~32 тысячи знаков, свой заведомо ниже,
#: чтобы отказ приходил словами от нас, а не молчаливым обрезанием от базы.
MAX_TEMPLATE_BODY = 20_000


class MessageTemplate(Base):
    """Типовой текст с подстановками: ответ на заявку, ссылка на доску, напоминание об оплате.

    Темы письма нет намеренно: шаблон один и тот же нужен и письму, и ленте, а у
    записи в ленте темы нет. Своей истории у шаблона нет и не будет: применённый
    становится обычным письмом или записью ленты и живёт своей жизнью.

    Связи «эта запись сделана шаблоном №5» тоже нет — она означала бы, что правка
    шаблона меняет смысл уже отправленного.
    """

    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Уникально: шаблон выбирают из списка ГЛАЗАМИ, два «Напоминания об оплате»
    # означают выбор наугад; заодно это единственная защита от двойного нажатия
    # — засов на кнопке (`lib/guard.ts`) живёт во вкладке. `ExactString` не
    # нужен: не токен, регистр не значащий, «Оплата» и «оплата» — одно название.
    name: Mapped[str] = mapped_column(String(MAX_TEMPLATE_NAME), unique=True)
    # Ключ из CHANNELS. Не внешний ключ и не enum: три значения заданы кодом, и
    # справочник из трёх строк только добавил бы способ разойтись с ним. Индекса
    # нет: три значения на десяток строк планировщик не возьмёт.
    channel: Mapped[str] = mapped_column(String(MAX_CHANNEL), default=CHANNEL_ANY)
    # Тело с подстановками вида `{client_name}`; набор полей закрыт и объявлен в
    # `core/services/template_service.py`. Подстановка вставляется одним проходом
    # и не перечитывается: иначе имя клиента, похожее на неё, тянуло бы чужое.
    body: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
