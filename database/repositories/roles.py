"""Запросы по должностям и выданным правам.

Правило, которое эти запросы обслуживают, одно: **раздавать права всегда есть
кому**. Отсюда `managers_count` — счёт «как будет после правки», а не «как
есть»: проверка стоит до записи, и без исключений она увидела бы того самого
человека, которого правка как раз и лишает права.

Второе правило — **роль по умолчанию ровно одна**. `make_default` пишет двумя
явными UPDATE, и «своя» ставится ПЕРВОЙ. Порядок не косметический: присваивание
полю ORM ничего не пишет, если роль уже была основной, а первый шаг соседнего
запроса тем временем признак снимает — и основных не остаётся вовсе. Ноль хуже
двух: две основные читатель разрешает сам (берёт первую), а без основной
зарегистрировавшийся сотрудник входит в CRM без единого раздела и без
объяснения, почему.
"""

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from database.models import Role, RolePermission, User
from database.models.user import ROLE_ROOT, STATUS_ACTIVE


def get(db: Session, role_id: int) -> Role | None:
    return db.get(Role, role_id)


def get_by_name(db: Session, name: str) -> Role | None:
    return db.scalar(select(Role).where(Role.name == name))


def name_exists(db: Session, name: str) -> bool:
    return db.scalar(select(Role.id).where(Role.name == name)) is not None


def list_all(db: Session) -> list[Role]:
    return list(db.scalars(select(Role).order_by(Role.name.asc())))


def any_exists(db: Session) -> bool:
    return db.scalar(select(Role.id).limit(1)) is not None


def default_role(db: Session) -> Role | None:
    return db.scalar(select(Role).where(Role.is_default.is_(True)))


def users_count(db: Session, role_id: int) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role_id == role_id)) or 0


def permissions_of(db: Session, role_id: int) -> list[RolePermission]:
    return list(db.scalars(select(RolePermission).where(RolePermission.role_id == role_id)))


def replace_permissions(db: Session, role_id: int, pairs: list[tuple[str, str]]) -> None:
    """Переписать набор прав роли целиком.

    Именно целиком, а не «добавить недостающие и убрать лишние»: набор приходит
    из конструктора как готовая картина, и разбирать её на разницу значило бы
    решать за человека, что он снял намеренно, а что не отметил случайно.
    """
    for row in permissions_of(db, role_id):
        db.delete(row)
    db.flush()
    for area, action in pairs:
        db.add(RolePermission(role_id=role_id, area=area, action=action))
    db.flush()


def managers_count(
    db: Session, *, manage_area: str, manage_action: str,
    exclude_user: int | None = None, exclude_role: int | None = None,
) -> int:
    """Сколько живых сотрудников (кроме root) смогут раздавать права после правки.

    Root в счёт не идёт нарочно — почему, объяснено у вызывающего: формально его
    право остаётся, а на деле управление уезжает к тому, кто заходит раз в год.
    Считаем по активным: у отключённого право есть, а войти он не может.
    """
    granting = select(RolePermission.role_id).where(
        RolePermission.area == manage_area, RolePermission.action == manage_action
    )
    if exclude_role is not None:
        granting = granting.where(RolePermission.role_id != exclude_role)

    stmt = select(func.count(User.id)).where(
        User.role != ROLE_ROOT,
        User.status == STATUS_ACTIVE,
        User.role_id.in_(granting),
    )
    if exclude_user is not None:
        stmt = stmt.where(User.id != exclude_user)
    return db.scalar(stmt) or 0


def make_default(db: Session, role: Role) -> None:
    """Сделать роль основной, сняв признак у остальных. Про порядок — в шапке."""
    db.execute(update(Role).where(Role.id == role.id).values(is_default=True))
    db.execute(
        update(Role).where(Role.id != role.id, Role.is_default.is_(True)).values(is_default=False)
    )
    db.refresh(role)
