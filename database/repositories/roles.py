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


def permissions_by_roles(db: Session, role_ids: list[int]) -> dict[int, list[RolePermission]]:
    """Права сразу нескольких должностей. Один запрос на весь список.

    **Список должностей спрашивал по запросу на строку**, и наклон был ровно
    плюс два на роль: права и число людей. Десять должностей — двадцать три
    запроса вместо пяти; замерено сторожем формы в `tests/test_speed.py`.

    Образец, как надо, стоит рядом — `documents_repo.lines_by_documents`.
    """
    if not role_ids:
        return {}
    itog: dict[int, list[RolePermission]] = {role_id: [] for role_id in role_ids}
    stroki = db.scalars(select(RolePermission).where(RolePermission.role_id.in_(role_ids)))
    for stroka in stroki:
        itog.setdefault(stroka.role_id, []).append(stroka)
    return itog


def users_count_by_roles(db: Session, role_ids: list[int]) -> dict[int, int]:
    """Сколько людей у каждой должности. Один запрос на весь список.

    Ноль для должности без людей приходит из заготовки, а не из базы: `GROUP BY`
    пустых групп не возвращает, и без заготовки такая должность выпала бы из
    ответа вовсе.
    """
    if not role_ids:
        return {}
    itog = {role_id: 0 for role_id in role_ids}
    stroki = db.execute(
        select(User.role_id, func.count())
        .where(User.role_id.in_(role_ids))
        .group_by(User.role_id)
    )
    for role_id, skolko in stroki:
        itog[role_id] = int(skolko)
    return itog


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


def zapert_roli(db: Session) -> None:
    """Занять все должности до конца транзакции.

    **Спор идёт не о строке, а о том, сколько управляющих останется.** Отказ
    «это последняя должность, которая может раздавать права» считается на «как
    будет», то есть запросом до записи. Между счётом и записью окно, и в него
    попадают двое: каждый снимает право у СВОЕЙ должности, каждый видит соседа и
    проходит, а управлять правами становится некому.

    Замерено дуэлью: пять раз из пяти оба ответа 200 и ноль управляющих. Root в
    счёт не идёт нарочно, и заводился этот отказ ровно на случай, когда пароль
    root потерян, — то есть гонка приводит систему в состояние, ради недопущения
    которого проверка и написана.

    Замок на ВСЕ должности, а не на правимую: считаются они все, и второй
    дуэлянт обязан пересчитать после первого. Тот же приём и тот же разбор, что
    у последнего владельца (`repositories/users.py`) и у последнего склада.

    Должностей в системе единицы, и берётся замок только на путях, где право
    отбирают, — цена ничтожна.
    """
    db.execute(select(Role.id).with_for_update()).all()


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
