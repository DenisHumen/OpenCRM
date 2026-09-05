"""Ключи доступа для чужих программ: сайт магазина, витрина, маркетплейс.

Ключ — не сотрудник и не роль (`docs/16-api-sayta.md` §8). В базе только
отпечаток: сам ключ показывается один раз при выдаче, и унесённая копия базы
не открывает чужую витрину. Области — строкой на область в дочерней таблице,
как `role_permissions`: по ним спрашивают («какие ключи пишут заказы»), а не
читают целиком.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base
from database.types import ExactString

#: Что ключ умеет. Свой словарь, а не роли сотрудников: роли описывают человека
#: у экрана, и натянутые на интеграцию они раздали бы ей `orders.view_amounts`.
SCOPE_CATALOG_READ = "catalog.read"
SCOPE_STOCK_READ = "stock.read"
SCOPE_ORDERS_WRITE = "orders.write"
SCOPE_ORDERS_READ = "orders.read"
SCOPE_CUSTOMERS_WRITE = "customers.write"
SCOPE_LEADS_WRITE = "leads.write"
SCOPES = (
    SCOPE_CATALOG_READ,
    SCOPE_STOCK_READ,
    SCOPE_ORDERS_WRITE,
    SCOPE_ORDERS_READ,
    SCOPE_CUSTOMERS_WRITE,
    SCOPE_LEADS_WRITE,
)
#: Блок, без которого область не работает. `None` — несущий блок, всегда есть.
#: Порядок проверок: блок включён → есть область, как у `require_perm`.
SCOPE_MODULE = {
    SCOPE_CATALOG_READ: "warehouse",
    SCOPE_STOCK_READ: "warehouse",
    SCOPE_ORDERS_WRITE: "orders",
    SCOPE_ORDERS_READ: "orders",
    SCOPE_CUSTOMERS_WRITE: None,
    SCOPE_LEADS_WRITE: None,
}

#: Точность наличия — свойство ключа, а не кода (§4): `exact` отдаёт число,
#: `bucket` — many/few/none, `boolean` — many/none.
STOCK_EXACT = "exact"
STOCK_BUCKET = "bucket"
STOCK_BOOLEAN = "boolean"
STOCK_MODES = (STOCK_EXACT, STOCK_BUCKET, STOCK_BOOLEAN)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    #: Видимая часть ключа: сличить строку в конфиге сайта со строкой в CRM,
    #: не имея возможности ею воспользоваться.
    prefix: Mapped[str] = mapped_column(String(12))
    #: Побайтное сравнение: регистронезависимое дало бы совпадение с чужим
    #: отпечатком, отличающимся только регистром (как `user_sessions.token_hash`).
    token_hash: Mapped[str] = mapped_column(ExactString(64), unique=True, index=True)
    #: Какой склад обслуживает; пусто у ключа без `stock.read`. RESTRICT — ключ
    #: ссылается на место, и удалить место вместе со ссылкой значит переписать
    #: прошлое; штатный путь — закрытие склада, и ключ тогда отвечает 409.
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    stock_mode: Mapped[str] = mapped_column(String(8), default=STOCK_BUCKET, server_default=STOCK_BUCKET)
    #: Порог `few` в тысячных; наружу не сообщается — сообщённый порог
    #: превращает `few` обратно в число.
    few_threshold_milli: Mapped[int] = mapped_column(Integer, default=5000, server_default="5000")
    rate_per_min: Mapped[int] = mapped_column(Integer, default=120, server_default="120")
    max_reserve_minutes: Mapped[int] = mapped_column(Integer, default=1440, server_default="1440")
    #: Сколько сайту верить ответу о наличии (§15 п. 1: поле ключа, а не константа).
    ttl_sec: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Отзыв — отметка, а не удаление: строка хранит ответ на «кто ходил ключом».
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Как есть, не хэшем: это адрес сервера владельца, и спрашивают «откуда
    #: ходят нашим ключом», а на это хэш не отвечает.
    last_used_ip: Mapped[str] = mapped_column(String(45), default="", server_default="")


class ApiKeyScope(Base):
    __tablename__ = "api_key_scopes"

    id: Mapped[int] = mapped_column(primary_key=True)
    api_key_id: Mapped[int] = mapped_column(
        ForeignKey("api_keys.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(32))

    __table_args__ = (UniqueConstraint("api_key_id", "scope", name="uq_api_key_scope"),)


class ApiKeyHit(Base):
    """Обращения по ключу, сложенные по часам и областям: строка на (ключ, час,
    область). Поштучно каждое обращение хранить незачем — графики строятся по
    дням и часам, а таблица росла бы на сто тысяч строк в сутки у одного сайта.
    Устройство и сводка — `core/services/api_stats_service.py`."""

    __tablename__ = "api_key_hits"
    __table_args__ = (UniqueConstraint("api_key_id", "bucket_at", "category", name="uq_api_key_hit"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    bucket_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    category: Mapped[str] = mapped_column(String(32))
    count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rejected: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
