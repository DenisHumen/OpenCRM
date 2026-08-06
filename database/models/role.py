from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

#: Длина названия роли. Больше в списке всё равно не читается, а роль — это
#: должность («Бухгалтер»), а не описание обязанностей.
MAX_ROLE_NAME = 60


class Role(Base):
    """Должность с набором прав. Создаёт её пользователь, а не разработчик.

    Ролей в системе изначально было две, и обе жили в коде. Бухгалтеру нужны
    финансы и не нужны доски, проджекту — заявки и склад, но не зарплаты
    коллег: дописывать в код по строке на каждую должность — не выход.

    Root ролью не описывается и роли не получает: у него все права всегда
    (`core/services/permissions_service.has`). Иначе можно было бы собрать
    конфигурацию, в которой раздать права обратно уже некому.
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_ROLE_NAME), unique=True)
    #: Из какого пресета роль выросла. Только для показа («создана из набора
    #: „Бухгалтер“»): права после создания живут своей жизнью, и сверяться с
    #: пресетом никто не будет — набор в нём может измениться с обновлением.
    preset: Mapped[str] = mapped_column(String(32), default="")
    #: Роль, которую получает новый сотрудник при регистрации. Ровно одна;
    #: следит за этим сервис, а не база — частичный уникальный индекс
    #: (`UNIQUE ... WHERE is_default`) запрещён принципами docs/03-database.md
    #: ради переезда на MySQL, а обычный `UNIQUE` запретил бы двум ролям
    #: одновременно *не* быть основной.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class RolePermission(Base):
    """Одно право одной роли: «область × действие».

    Строкой на право, а не списком в одной колонке: право — это то, по чему
    спрашивают («кто может менять роли»), а не то, что читают целиком. Список в
    тексте пришлось бы разбирать в приложении на каждый такой вопрос, и
    инвариант «последний управляющий правами» проверялся бы перебором всех
    ролей.

    Какие пары бывают, знает код (`core/permissions.py`). Строка с областью,
    которой больше нет в реестре, ничего не даёт: проверка идёт от реестра.
    """

    __tablename__ = "role_permissions"
    __table_args__ = (
        # Одно и то же право дважды — не ошибка данных, а мусор, который
        # начинает жить своей жизнью: снимаешь право, а оно остаётся.
        UniqueConstraint("role_id", "area", "action", name="uq_role_permission"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), index=True
    )
    area: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(24))
