"""Роли: пресеты, конструктор прав и проверка «можно ли».

Реестр прав — в `core/permissions.py`, здесь только состояние (кто чем владеет)
и правила, которые нельзя нарушить.

Три из них держат всю конструкцию.

**У root права все и всегда.** Он не описывается ролью и роли не получает.
Иначе можно было бы собрать конфигурацию, в которой раздать права обратно уже
некому, — а «некому» в системе на одном сервере означает переустановку.

**Отказ называет причину.** Как у блоков (`module_is_core`, `module_requires`):
молчаливый 404 вместо «нет права» превращает настройку доступов в гадание. Тот,
кто настраивает роли, обязан понимать, почему у сотрудника не открылся раздел.

**Кэша нет намеренно.** Состояние блоков кэшируется на две секунды, права — нет.
Разница в цене ошибки: блок, пропавший из меню на секунду позже, — неудобство;
право, снятое секунду назад и всё ещё работающее, — дыра. У человека может быть
открыта вкладка со старыми правами, и единственное, что защищает от неё, —
проверка на каждый запрос. Запрос при этом один и по индексу.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import exceptions as errors
from core import permissions
from database.models import Role, RolePermission, User
from database.models.role import MAX_ROLE_NAME
from database.models.user import ROLE_ROOT

# Готовые наборы прав под должность. Как пресеты воронки: малый бизнес не станет
# собирать матрицу из шестидесяти клеток вместо того, чтобы работать, а пустой
# конструктор на первом экране — причина закрыть вкладку. Поэтому даём готовое
# начало, а переделать можно всё.
#
# Права перечислены строками «область.действие» — так же, как они ездят в API.
# Несуществующее право в пресете роняет тест реестра (`test_roles.py`), а не
# тихо пропадает при создании роли.


def _crud(*areas: str) -> list[str]:
    """Полный набор по разделу: видеть, создавать, изменять, удалять."""
    return [f"{area}.{action}" for area in areas for action in permissions.BASE_ACTIONS]


def _view(*areas: str) -> list[str]:
    return [f"{area}.{permissions.VIEW}" for area in areas]


PRESETS: dict[str, dict] = {
    # Что умел «менеджер» до появления ролей — ровно, клетка в клетку. Этот
    # набор не про удобство, а про совместимость: на него переезжают все
    # существующие сотрудники, и любое расхождение здесь означает, что после
    # обновления у человека пропала кнопка, которой он пользовался вчера.
    "manager": {
        "name": "Менеджер",
        "hint": "Ведёт клиентов и заявки целиком. То же, что умели сотрудники до появления ролей",
        # Ни `staff.view`, ни `settings.view` здесь нет, и это не забывчивость:
        # список сотрудников и настройки сайта были закрыты от менеджера и до
        # ролей (`require_root`). Выдать их «заодно» значило бы расширить
        # доступ под видом совместимости.
        "permissions": (
            _crud("clients", "deals", "boards", "tasks", "documents", "warehouse")
            + _view("companies", "reports", "mail", "telephony")
            + [
                "deals.move_stage",
                "deals.view_others",
                "deals.view_amounts",
                "documents.issue",
                "warehouse.restore",
                "warehouse.view_amounts",
                "reports.view_amounts",
                "mail.create",
                "telephony.create",
                "telephony.edit",
            ]
        ),
    },
    "accountant": {
        "name": "Бухгалтер",
        "hint": "Деньги, бланки и реквизиты. Доски и переписка ему не нужны",
        "permissions": (
            _view("clients", "deals", "settings")
            + _crud("documents", "companies")
            + [
                "documents.issue",
                "deals.view_others",
                "deals.view_amounts",
                "reports.view",
                "reports.view_amounts",
                "warehouse.view",
                "warehouse.view_amounts",
            ]
        ),
    },
    "project_manager": {
        "name": "Проджект-менеджер",
        "hint": "Заявки, склад и сроки. Суммы и зарплаты коллег — мимо",
        "permissions": (
            _crud("deals", "tasks", "boards")
            + _view("clients", "companies", "documents", "settings")
            + [
                "clients.create",
                "clients.edit",
                "deals.move_stage",
                "deals.view_others",
                "documents.create",
                "warehouse.view",
                "warehouse.create",
                "warehouse.edit",
                "reports.view",
                "staff.view",
            ]
        ),
    },
    "director": {
        "name": "Гендиректор",
        "hint": "Видит и может всё, включая роли и настройки. Кроме прав самого root",
        # Собирается из реестра, а не перечислением: должность «всё» обязана
        # получать и то, что появится в следующем блоке, иначе она перестанет
        # быть тем, чем названа, ровно в день выхода обновления.
        "permissions": list(permissions.all_codes()),
    },
    "viewer": {
        "name": "Наблюдатель",
        "hint": "Только смотрит: ничего не создаёт, не меняет и не видит сумм",
        "permissions": _view(*[area.key for area in permissions.AREAS if not area.is_system]),
    },
}

DEFAULT_PRESET = "manager"


# --- чтение ---


def codes_of_role(db: Session, role_id: int) -> set[str]:
    rows = db.scalars(select(RolePermission).where(RolePermission.role_id == role_id))
    # Сверяем с реестром: строка от снесённого блока не должна ничего давать.
    return {
        permissions.code(row.area, row.action)
        for row in rows
        if permissions.exists(row.area, row.action)
    }


def codes_of(db: Session, user: User) -> set[str]:
    """Все права сотрудника. У root — весь реестр, что бы ни лежало в базе."""
    if user.role == ROLE_ROOT:
        return set(permissions.all_codes())
    if not user.role_id:
        return set()
    return codes_of_role(db, user.role_id)


def has(db: Session, user: User, area: str, action: str) -> bool:
    if user.role == ROLE_ROOT:
        # Право у root есть всегда — но только настоящее. Опечатка в имени не
        # должна проходить проверку лишь потому, что спросил root: иначе
        # мёртвая проверка обнаружилась бы у первого же менеджера.
        return permissions.exists(area, action)
    if not user.role_id:
        return False
    return permissions.code(area, action) in codes_of_role(db, user.role_id)


def deals_scope(db: Session, user: User) -> int | None:
    """Чьи заявки видит сотрудник. None — все.

    Это фильтр в запросах, а не признак в интерфейсе: спрятать чужие карточки
    на экране значит оставить их доступными по адресу, в поиске и в выгрузке.
    Возвращаем id, а не булево, чтобы вызывающий не решал заново, чьи именно.

    Сужается **только доступ к заявкам**. «Видит только своих клиентов» в первую
    версию не входит осознанно: клиент — несущий блок, на нём висят бланки,
    письма, звонки, доски и задачи, и фильтровать пришлось бы каждое соединение
    в семи блоках сразу. Наполовину сделанный фильтр здесь хуже отсутствующего:
    менеджер не увидит карточку клиента, но увидит его имя в своей заявке, его
    письмо в почте и его номер в журнале звонков — то есть запрет будет выглядеть
    работающим, не будучи им. Место оставлено: у заявок это ровно один параметр
    в репозитории, и клиенты примут такой же, когда за ним придут с остальными
    шестью блоками.
    """
    if has(db, user, "deals", permissions.VIEW_OTHERS):
        return None
    return user.id


def sees_amounts(db: Session, user: User, area: str = "deals") -> bool:
    """Показывать ли суммы. Отдельно от права на раздел — это и есть просьба
    «менеджер ведёт заявку, но не видит её маржу»."""
    return has(db, user, area, permissions.VIEW_AMOUNTS)


def list_roles(db: Session) -> list[Role]:
    return list(db.scalars(select(Role).order_by(Role.name.asc())))


def get_role(db: Session, role_id: int) -> Role:
    role = db.get(Role, role_id)
    if role is None:
        raise errors.NotFoundError("Role not found", code="role_not_found")
    return role


def default_role(db: Session) -> Role | None:
    return db.scalar(select(Role).where(Role.is_default.is_(True)))


def count_users_of(db: Session, role_id: int) -> int:
    from sqlalchemy import func

    return (
        db.scalar(select(func.count()).select_from(User).where(User.role_id == role_id))
        or 0
    )


# --- инвариант: раздавать права всегда есть кому ---


def _managers_of_roles(
    db: Session, *, exclude_user: int | None = None, exclude_role: int | None = None
) -> int:
    """Сколько живых сотрудников (кроме root) смогут менять роли ПОСЛЕ правки.

    Считается на «как будет», а не «как есть»: проверка стоит до записи, и без
    исключений она увидела бы того самого человека, которого правка как раз и
    лишает права, — то есть всегда разрешала бы себя.

    - `exclude_role` — роль, у которой сейчас отбирают `roles.manage`;
    - `exclude_user` — сотрудник, которого сейчас переводят на другую роль.

    Root в счёт не идёт нарочно. У него право есть всегда, и учитывать его
    значило бы разрешить снять последнего «гендиректора»: формально доступ
    остался, а на деле управление правами уехало к тому, кто заходит в систему
    раз в год и чей пароль, случается, уже потерян.

    Считаем по активным: у отключённого сотрудника право есть, а войти он не
    может — управлять правами ему нечем.
    """
    from database.models.user import STATUS_ACTIVE

    granting = select(RolePermission.role_id).where(
        RolePermission.area == "roles", RolePermission.action == permissions.MANAGE
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


def _refuse_if_nobody_left_to_manage_roles(db: Session, **exclusions) -> None:
    """Отказать, если после правки раздавать права станет некому.

    Сценарий, ради которого это существует: владелец завёл «гендиректора»,
    отдал ему управление доступами и потерял пароль root. Снять последнее такое
    право значит запереть систему — восстановить её после этого можно только с
    доступом к файлу базы.

    Стоит на четырёх путях из шести: отключение и удаление сотрудника
    (`auth_service.disable`, `auth_service.delete_user`) приводят к тому же
    состоянию и этой проверки не делают. Это известное расхождение, а не
    недосмотр, — почему оно не закрывается здесь, написано в `disable`.
    """
    if _managers_of_roles(db, **exclusions) > 0:
        return
    raise errors.ForbiddenError(
        "This is the last role that can manage permissions — "
        "grant 'roles.manage' to someone else first",
        code="last_roles_manager",
    )


# --- изменение ---


def _clean_name(db: Session, name: str, *, role_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")
    name = name[:MAX_ROLE_NAME]
    clash = db.scalar(select(Role).where(Role.name == name))
    if clash is not None and clash.id != role_id:
        raise errors.ConflictError("A role with this name already exists", code="role_name_taken")
    return name


def _clean_codes(values) -> list[tuple[str, str]]:
    """Разобрать присланные права. Несуществующее — отказ с указанием, какое.

    Молча отбрасывать неизвестное нельзя: человек нажал «сохранить», увидел
    успех и остался в уверенности, что право выдано.
    """
    pairs: list[tuple[str, str]] = []
    for value in values or []:
        parsed = permissions.parse(str(value))
        if parsed is None:
            raise errors.ValidationError(
                f"Unknown permission: {value}", code="unknown_permission"
            )
        if parsed not in pairs:
            pairs.append(parsed)
    return pairs


def _write_codes(db: Session, role: Role, pairs: list[tuple[str, str]]) -> None:
    for row in db.scalars(select(RolePermission).where(RolePermission.role_id == role.id)):
        db.delete(row)
    db.flush()
    for area, action in pairs:
        db.add(RolePermission(role_id=role.id, area=area, action=action))
    db.flush()


def create_role(
    db: Session, name: str, codes, *, preset: str = "", is_default: bool = False
) -> Role:
    role = Role(name=_clean_name(db, name), preset=preset[:32], is_default=False)
    db.add(role)
    db.flush()
    _write_codes(db, role, _clean_codes(codes))
    if is_default:
        set_default(db, role.id)
    return role


def create_from_preset(db: Session, preset: str, name: str | None = None) -> Role:
    if preset not in PRESETS:
        raise errors.ValidationError(f"Unknown preset: {preset}", code="unknown_preset")
    template = PRESETS[preset]
    return create_role(
        db, name or template["name"], template["permissions"], preset=preset
    )


def _refuse_self_promotion(db: Session, actor: User, role: Role, pairs: list[tuple[str, str]]) -> None:
    """Своей должности новых прав не выписывают.

    Это вторая половина запрета из `assign`, без которой первая ничего не
    стоила. Там сказано: нельзя назначить себе другую роль, потому что иначе
    «управляет правами» становится «имеет все права». Ровно то же самое
    получалось одним запросом с другой стороны — не менять роль, а дописать
    права в ту, которая уже своя. Сотрудник с одним лишь `roles.manage`
    отправлял `PATCH /roles/{своя}` со всем реестром и получал журнал,
    настройки, сотрудников и суммы; проверялось это на стенде и работало.

    Убавлять — можно. Снять с себя лишнее не escalation, а обычная уборка, и
    запрещать её значило бы требовать второго человека ради того, чтобы отдать
    доступ. Переименовать роль — тем более можно: `codes=None` сюда не доходит.

    Root сюда не попадает: у него прав весь реестр и добавить к ним нечего.
    Проверка всё равно общая, а не «если не root»: исключение, написанное
    руками, однажды окажется единственным местом, где новую роль забыли учесть.
    """
    if actor.role_id != role.id:
        return
    have = codes_of(db, actor)
    added = sorted(permissions.code(area, action) for area, action in pairs)
    new = [code for code in added if code not in have]
    if not new:
        return
    raise errors.ForbiddenError(
        "Cannot grant yourself permissions you do not have: " + ", ".join(new),
        code="cannot_grant_to_own_role",
    )


def update_role(db: Session, role_id: int, name: str | None, codes, *, actor: User) -> Role:
    """`actor` обязателен и без значения по умолчанию нарочно: проверка на
    самоповышение опирается только на него, а необязательный аргумент однажды
    забудут передать — и запрет исчезнет молча, оставив подпись на месте."""
    role = get_role(db, role_id)
    if name is not None:
        role.name = _clean_name(db, name, role_id=role.id)
    if codes is not None:
        pairs = _clean_codes(codes)
        _refuse_self_promotion(db, actor, role, pairs)
        loses_manage = _grants_manage(db, role.id) and (
            ("roles", permissions.MANAGE) not in pairs
        )
        if loses_manage:
            _refuse_if_nobody_left_to_manage_roles(db, exclude_role=role.id)
        _write_codes(db, role, pairs)
    db.flush()
    return role


def set_default(db: Session, role_id: int) -> Role:
    """Роль по умолчанию ровно одна: её получает новый сотрудник.

    Переставляется одним проходом, как основная фирма: без этого система тихо
    осталась бы без роли по умолчанию, и зарегистрировавшийся сотрудник входил
    бы в пустую CRM без единого раздела.
    """
    role = get_role(db, role_id)
    for other in db.scalars(select(Role).where(Role.is_default.is_(True))):
        other.is_default = False
    role.is_default = True
    db.flush()
    return role


def delete_role(db: Session, role_id: int) -> None:
    role = get_role(db, role_id)
    if role.is_default:
        raise errors.ValidationError(
            "The default role cannot be deleted — make another one default first",
            code="role_is_default",
        )
    busy = count_users_of(db, role.id)
    if busy:
        # SET NULL оставил бы людей без прав молча: сотрудник приходит утром и
        # обнаруживает пустую CRM, а причина — вчерашняя уборка в справочнике.
        raise errors.ConflictError(
            f"The role is assigned to {busy} employee(s) — reassign them first",
            code="role_in_use",
        )
    db.delete(role)
    db.flush()


def assign(db: Session, actor: User, user_id: int, role_id: int | None) -> User:
    """Назначить сотруднику роль.

    Себе — нельзя. Не из вежливости: право менять роли есть у того, кто это
    делает, и без запрета он мог бы выдать себе любое другое право или снять с
    себя ограничение, ради которого роль и заводилась. Запрет на себя — то
    единственное, что отделяет «управляет правами» от «имеет все права».
    """
    from database.repositories import users as users_repo

    user = users_repo.get_by_id(db, user_id)
    if user is None:
        raise errors.NotFoundError("User not found", code="user_not_found")
    if user.role == ROLE_ROOT:
        raise errors.ForbiddenError(
            "Root does not get a role — root has every permission",
            code="cannot_assign_role_to_root",
        )
    if user.id == actor.id:
        raise errors.ForbiddenError(
            "Cannot change your own role", code="cannot_change_own_role"
        )

    role = get_role(db, role_id) if role_id is not None else None
    # Человек теряет право раздавать доступы, если оно у него было, а новая
    # должность его не даёт. Себя из счёта исключаем: после правки он уже не
    # в числе тех, кто может.
    if _grants_manage(db, user.role_id) and not (role and _grants_manage(db, role.id)):
        _refuse_if_nobody_left_to_manage_roles(db, exclude_user=user.id)

    user.role_id = role.id if role else None
    db.flush()
    return user


def _grants_manage(db: Session, role_id: int | None) -> bool:
    if not role_id:
        return False
    return permissions.code("roles", permissions.MANAGE) in codes_of_role(db, role_id)


# --- установка ---


def seed_defaults(db: Session) -> None:
    """Кладёт роль по умолчанию, если ролей нет вовсе. Зовётся при старте.

    Ровно как `pipeline_service.seed_defaults`: система без единой роли не может
    принять ни одного сотрудника, а требовать собрать матрицу до первого найма —
    тот самый пустой конструктор, которого здесь не должно быть.
    """
    if db.scalar(select(Role).limit(1)) is not None:
        return
    role = create_from_preset(db, DEFAULT_PRESET)
    set_default(db, role.id)


def matrix() -> list[dict]:
    """Матрица для конструктора: строки — области, столбцы — действия.

    Собирается из реестра на каждый запрос, а не хранится: появился блок —
    появилась строка, и правки в интерфейсе для этого не нужно.
    """
    return [
        {
            "key": area.key,
            "module": area.module,
            "system": area.is_system,
            "actions": list(area.actions),
        }
        for area in permissions.AREAS
    ]
