from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base
from database.types import LongText, text_default


class KeyCategory(Base):
    """Категория ключей: «Рабочие сервисы», «Бухгалтерия», «Денис — личные».

    Категория — это ещё и способ раздать доступ разом: пустить человека в
    категорию проще, чем в каждый ключ по отдельности. Закрытая категория
    видна всем по имени и числу ключей, но не по содержимому: спрятать её
    целиком значило бы, что второй такой же заведут рядом.
    """

    __tablename__ = "key_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    # Закрытая: внутрь пускают только создателя и root, доступом по списку её
    # не открыть. Для личных ключей сотрудника.
    zakrytaya: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class TwoFactorKey(Base):
    """Ключ двухфакторной авторизации: секрет сервиса, из которого растут коды.

    Секрет зашифрован `core/security/secretbox` и наружу не выходит нигде,
    кроме окна переноса на телефон (только root, с записью в журнал). Коды
    считаются на сервере: уйди секрет в браузер — он осел бы в кэше вкладки,
    в снимке экрана и в отчёте об ошибке.
    """

    __tablename__ = "two_factor_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Пусто — ключ лежит вне категорий, «Мои» и «Все ключи» его показывают.
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("key_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Как ключ зовут у нас: «GitHub — организация». Пять учёток одного сервиса
    # без своих имён — пять одинаковых строк в списке.
    title: Mapped[str] = mapped_column(String(120))
    # Как сервис назвал себя в строке otpauth. Отсюда берётся фирменный знак —
    # но сам знак не хранится: он производное от имени и набора значков, а
    # производное в этой системе не хранят (`core/services/znaki_service.py`).
    issuer: Mapped[str] = mapped_column(String(80), default="", server_default="")
    # Учётка в сервисе: почта, телефон, логин бота.
    account: Mapped[str] = mapped_column(String(120), default="", server_default="")

    secret_encrypted: Mapped[str] = mapped_column(LongText)
    # Запасные коды сервиса списком, тем же шифром. NULL — их не заводили;
    # пустой список означал бы «завели и все потратили», а это другое.
    backup_codes_encrypted: Mapped[str | None] = mapped_column(LongText, nullable=True)

    digits: Mapped[int] = mapped_column(Integer, default=6, server_default="6")
    period: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    algorithm: Mapped[str] = mapped_column(String(8), default="SHA1", server_default="SHA1")

    # Те же четыре слова, что у напоминаний (`task.VAZHNOSTI`): срочный ключ
    # получает ту же рябь по краю карточки, и заводить вторую шкалу важности
    # значило бы объяснять человеку разницу, которой нет.
    vazhnost: Mapped[str] = mapped_column(String(8), default="normal", server_default="normal")
    note: Mapped[str] = mapped_column(
        LongText, default="", server_default=text_default(), deferred=True
    )

    # Напоминание «сменить ключ»: обычная задача из блока напоминаний, а не
    # свой срок рядом. Свой срок пришлось бы показывать, отсчитывать и гасить
    # вторым способом — и он разошёлся бы с напоминаниями на первой правке.
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    # Корзина: удалённый ключ ещё можно поднять. Секрет при этом остаётся
    # зашифрованным на месте — «удалить» и «стереть» здесь разные слова.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class TwoFactorKeyAccess(Base):
    """Кому открыт один ключ. Создатель и root в списке не значатся — им видно
    всегда, и строка о них была бы данными, которые можно снять."""

    __tablename__ = "two_factor_key_access"
    __table_args__ = (UniqueConstraint("key_id", "user_id", name="uq_key_access"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key_id: Mapped[int] = mapped_column(
        ForeignKey("two_factor_keys.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    granted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KeyCategoryAccess(Base):
    """Кому открыта категория целиком — вместе со всем, что в ней есть и будет."""

    __tablename__ = "key_category_access"
    __table_args__ = (UniqueConstraint("category_id", "user_id", name="uq_key_category_access"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("key_categories.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    granted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
