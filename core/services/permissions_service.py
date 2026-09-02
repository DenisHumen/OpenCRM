"""Роли: пресеты, конструктор прав и проверка «можно ли».

Реестр прав — в `core/permissions.py`, здесь только состояние (кто чем владеет)
и правила, которые нельзя нарушить.

Три из них держат всю конструкцию.

**У root права все и всегда.** Он не описывается ролью и роли не получает.
Иначе можно было бы собрать конфигурацию, в которой раздать права обратно уже
некому, — а «некому» в системе на одном сервере означает переустановку.

**Отказ называет причину.** Как у блоков (`module_is_core` на выключении
несущего, `module_requires_unbuilt` на включении блока, чьё основание ещё не
написано): молчаливый 404 вместо «нет права» превращает настройку доступов в
гадание. Тот, кто настраивает роли, обязан понимать, почему у сотрудника не
открылся раздел.

**Кэша нет намеренно.** Состояние блоков кэшируется на две секунды, права — нет.
Разница в цене ошибки: блок, пропавший из меню на секунду позже, — неудобство;
право, снятое секунду назад и всё ещё работающее, — дыра. У человека может быть
открыта вкладка со старыми правами, и единственное, что защищает от неё, —
проверка на каждый запрос. Запрос при этом один и по индексу.
"""

from sqlalchemy.orm import Session

from core import exceptions as errors
from core import permissions
from core import uniqueness
from core.services import audit_service
from database.models import Role, User
from database.repositories import roles as roles_repo
from database.models.audit import SOURCE_MANUAL
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
    """Полный набор по разделу: видеть, создавать, изменять, удалять.

    **Спрашивает реестр, а не перечисляет вслепую.** У областей набор действий
    свой: бумага не удаляется, а отменяется, и `documents.delete` в матрице нет.
    Пока помощник выдавал все четыре, пресет обещал несуществующее право — и
    должность по нему не заводилась вовсе: `422 unknown_permission` на первом же
    нажатии. То есть ошибка в ОДНОЙ области ломала пресеты целиком.
    """
    return [
        f"{area}.{action}"
        for area in areas
        for action in permissions.BASE_ACTIONS
        if permissions.exists(area, action)
    ]


def _view(*areas: str) -> list[str]:
    return [f"{area}.{permissions.VIEW}" for area in areas]


PRESETS: dict[str, dict] = {
    # Что умел «менеджер» до появления ролей — ровно, клетка в клетку. Этот
    # набор не про удобство, а про совместимость: на него переезжают все
    # существующие сотрудники, и любое расхождение здесь означает, что после
    # обновления у человека пропала кнопка, которой он пользовался вчера.
    "manager": {
        "name": "Manager",
        "hint": "Runs clients and requests end to end. The same reach staff had before roles existed",
        # Ни `staff.view`, ни `settings.view` здесь нет, и это не забывчивость:
        # список сотрудников и настройки сайта были закрыты от менеджера и до
        # ролей (`require_root`). Выдать их «заодно» значило бы расширить
        # доступ под видом совместимости.
        "permissions": (
            _crud("clients", "deals", "boards", "tasks", "documents", "warehouse")
            + _view("companies", "reports", "mail", "telephony", "templates")
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
                # Переписка с клиентами — работа менеджера, а не владельца.
                # Прав на неё не было ни у одной должности: канал сделан
                # целиком, а открыть его мог только root. Владелец этого не
                # замечает по устройству — он root и видит всё.
                #
                # `view` и `create`, но НЕ `manage`: подключение бота, токен и
                # чат сводки — решение уровня «каким каналом работает фирма»,
                # и оно остаётся у того, кто настраивает сайт.
                "telegram.view",
                "telegram.create",
            ]
        ),
    },
    "accountant": {
        "name": "Accountant",
        "hint": "Money, paperwork and company details. Boards and mail are not needed here",
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
        "name": "Project manager",
        "hint": "Requests, stock and deadlines. Amounts and colleagues' pay stay out of reach",
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
        "name": "Director",
        "hint": "Sees and can do everything, roles and settings included. Except root's own rights",
        # Собирается из реестра, а не перечислением: должность «всё» обязана
        # получать и то, что появится в следующем блоке, иначе она перестанет
        # быть тем, чем названа, ровно в день выхода обновления.
        "permissions": list(permissions.all_codes()),
    },
    "viewer": {
        "name": "Viewer",
        "hint": "Looks only: creates nothing, changes nothing, sees no amounts",
        "permissions": _view(*[area.key for area in permissions.AREAS if not area.is_system]),
    },
}

DEFAULT_PRESET = "manager"


# --- чтение ---


#: Ключ памятки прав в `db.info`. Область жизни памятки — СЕССИЯ БАЗЫ, то есть
#: ровно один запрос: `web/api/deps._edinica_raboty` заводит сессию на запрос и
#: закрывает её до отправки ответа.
_PAMYATKA = "prava_roley"


def codes_of_role(db: Session, role_id: int) -> set[str]:
    """Права должности. Спрашивается у базы ОДИН раз за запрос.

    **Троение здесь не выдумка, а замер.** Обычная «денежная» ручка спрашивает
    права трижды: `require_perm` зовёт `has()` на входе, обработчик зовёт
    `deals_scope()` (внутри снова `has()`), а сериализатор — `sees_amounts()`
    (и там опять `has()`). Три одинаковых `SELECT` по `role_permissions` за один
    запрос, и так на каждой странице со суммами.

    Памятка живёт в `db.info`, а НЕ между запросами, и это разница
    принципиальная. Кэш прав между запросами означал бы, что снятое право ещё
    какое-то время действует, — дыра, про которую сказано в докстроке модуля, и
    сказано верно. Здесь же память живёт ровно столько, сколько живёт сессия:
    следующий запрос спросит базу заново и увидит снятое право сразу.

    По числам размен тоже не в пользу общего кэша: запрос по индексу стоит
    0,62 мс, круг до Redis — 0,55 мс. То есть покупались бы пять сотых
    миллисекунды ценой отложенного отзыва, а соединение при этом всё равно
    осталось бы занятым остальными запросами страницы.
    """
    pamyatka = db.info.setdefault(_PAMYATKA, {})
    if role_id in pamyatka:
        return pamyatka[role_id]

    rows = roles_repo.permissions_of(db, role_id)
    # Сверяем с реестром: строка от снесённого блока не должна ничего давать.
    kody = {
        permissions.code(row.area, row.action)
        for row in rows
        if permissions.exists(row.area, row.action)
    }
    pamyatka[role_id] = kody
    return kody


def zabyt_prava(db: Session, role_id: int | None = None) -> None:
    """Забыть памятку прав в этой сессии. Без номера — всю.

    Нужна там, где права МЕНЯЮТ и тут же читают: правка должности отвечает
    обновлённой карточкой, и без сброса ответ показал бы состояние до правки.
    За пределами такого случая не нужна вовсе — памятка и так не переживает
    запроса.
    """
    pamyatka = db.info.get(_PAMYATKA)
    if not pamyatka:
        return
    if role_id is None:
        pamyatka.clear()
    else:
        pamyatka.pop(role_id, None)


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
    return roles_repo.list_all(db)


def get_role(db: Session, role_id: int) -> Role:
    role = roles_repo.get(db, role_id)
    if role is None:
        raise errors.NotFoundError("Role not found", code="role_not_found")
    return role


def default_role(db: Session) -> Role | None:
    return roles_repo.default_role(db)


def count_users_of(db: Session, role_id: int) -> int:
    return roles_repo.users_count(db, role_id)


def kody_rolej(db: Session, role_ids: list[int]) -> dict[int, set[str]]:
    """Права сразу нескольких должностей, пачкой. И памятка заполняется заодно.

    Заодно — потому что список должностей почти всегда читают вместе с чем-то
    ещё, и второй проход по тем же ролям должен обойтись без базы.
    """
    pamyatka = db.info.setdefault(_PAMYATKA, {})
    itog: dict[int, set[str]] = {}
    sprosit = [role_id for role_id in role_ids if role_id not in pamyatka]
    if sprosit:
        for role_id, stroki in roles_repo.permissions_by_roles(db, sprosit).items():
            pamyatka[role_id] = {
                permissions.code(row.area, row.action)
                for row in stroki
                if permissions.exists(row.area, row.action)
            }
    for role_id in role_ids:
        itog[role_id] = pamyatka.get(role_id, set())
    return itog


def lyudey_v_rolyah(db: Session, role_ids: list[int]) -> dict[int, int]:
    """Сколько людей у каждой должности, пачкой."""
    return roles_repo.users_count_by_roles(db, role_ids)


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
    return roles_repo.managers_count(
        db,
        manage_area="roles",
        manage_action=permissions.MANAGE,
        exclude_user=exclude_user,
        exclude_role=exclude_role,
    )


def _refuse_if_nobody_left_to_manage_roles(
    db: Session, actor: User | None = None, **exclusions
) -> None:
    """Отказать, если после правки раздавать права станет некому.

    Сценарий, ради которого это существует: владелец завёл «гендиректора»,
    отдал ему управление доступами и потерял пароль root. Снять последнее такое
    право значит запереть систему — восстановить её после этого можно только с
    доступом к файлу базы.

    Стоит на ВСЕХ шести путях: снятие права с роли, перевод человека на другую
    роль, отключение и удаление сотрудника (`auth_service`). Четыре двери из
    шести были закрыты, а две — нет, и система оставалась без управляющего
    доступами ровно тем способом, от которого проверка и заведена.

    **Root — исключение, и это не послабление, а условие достижимости.** Без
    него состояние «только root, никаких управляющих» становится НЕДОСТИЖИМЫМ:
    назначили первого управляющего — и убрать всех уже нельзя ничем, кроме
    правки базы. А состояние это законное, с него начинается любая установка.

    Сценарий, ради которого проверка живёт, при этом закрыт полностью: «владелец
    завёл гендиректора и забыл пароль root». В нём root НИЧЕГО не делает — он
    недоступен, — а все остальные заперты. Мы опираемся не на существование
    root'а где-то в базе (на это опираться и правда нельзя), а на то, что он
    сейчас, вот этим запросом, ДЕЙСТВУЕТ: доступ есть, и доказан он входом.
    """
    if actor is not None and actor.role == ROLE_ROOT:
        return
    # Очередь на должности: счёт идёт до записи, и без неё двое снимают право
    # у разных должностей разом. Разбор — `roles_repo.zapert_roli`.
    roles_repo.zapert_roli(db)
    if _managers_of_roles(db, **exclusions) > 0:
        return
    raise errors.ForbiddenError(
        "This is the last role that can manage permissions — "
        "grant 'roles.manage' to someone else first",
        code="last_roles_manager",
    )


def ubedis_est_komu_razdavat(db: Session, actor: User, *, exclude_user: int) -> None:
    """Убедиться, что после снятия ЭТОГО человека раздавать права будет кому.

    Вход для `auth_service`: там отключают и удаляют сотрудников, и оба действия
    приводят к тому же состоянию, что и снятие права. Своя функция, а не вызов
    внутренней с подчёркиванием: имя объясняет, о чём спрашивают, — а
    `_refuse_if_nobody_left_to_manage_roles(db, exclude_user=...)` в чужом модуле
    читается как обращение к чужой кухне.

    **Тупика это не создаёт, вопреки прежнему опасению.** У управляющего есть
    `roles.manage`, то есть он вправе назначить преемника сам; выход из
    положения — сначала дать право второму человеку, потом увольнять первого.
    Необратимым назначение «гендиректора» стало бы только если бы права нельзя
    было выдать никому, а это не так.
    """
    _refuse_if_nobody_left_to_manage_roles(db, actor, exclude_user=exclude_user)


# --- изменение ---


def _clean_name(db: Session, name: str, *, role_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")
    name = name[:MAX_ROLE_NAME]
    clash = roles_repo.get_by_name(db, name)
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
    roles_repo.replace_permissions(db, role.id, pairs)
    # Памятку этой должности забываем ЗДЕСЬ, у единственной двери записи.
    # Ручка правки отвечает обновлённой карточкой, и памятка, набитая до записи,
    # показала бы состояние до неё. Сброс у двери, а не у вызывающих: вызывающих
    # двое сегодня и неизвестно сколько завтра, а дверь одна.
    zabyt_prava(db, role.id)


def create_role(
    db: Session,
    name: str,
    codes,
    *,
    preset: str = "",
    is_default: bool = False,
    actor: User | None = None,
) -> Role:
    """`actor` необязателен только ради установки: при посеве раздавать права
    некому и не от кого. Из запроса он приходит всегда — иначе сюда вернётся
    роль со всеми правами, заведённая тем, у кого их нет."""
    pairs = _clean_codes(codes)
    if actor is not None:
        _refuse_granting_what_you_lack(
            db, actor, {permissions.code(area, action) for area, action in pairs}
        )
    # Имя роли уникально, и между проверкой в `_clean_name` и вставкой есть то
    # же окно, что у почты и у номера бланка. Двое администраторов заводят
    # «Бухгалтера» одновременно — редкость, но отвечать на неё пятисоткой не
    # повод: сообщение «такая роль уже есть» человек понимает и без нас.
    role = uniqueness.insert_unique(
        db,
        Role(name=_clean_name(db, name), preset=preset[:32], is_default=False),
        taken=lambda row: roles_repo.name_exists(db, row.name),
        message="A role with this name already exists",
        code="role_name_taken",
    )
    _write_codes(db, role, pairs)
    if is_default:
        set_default(db, role.id)
    if actor is not None:
        # При посеве исполнителя нет и записи быть не должно: система создаёт
        # роли сама, и «Root завёл роль» в этот момент было бы выдумкой.
        audit_service.record(
            db,
            action=audit_service.ACTION_ROLE_CREATED,
            actor=actor,
            source=SOURCE_MANUAL,
            entity_type=audit_service.ENTITY_ROLE,
            entity_id=role.id,
            entity_label=role.name,
            after=audit_service.permissions_text(
                permissions.code(area, action) for area, action in pairs
            ),
        )
    return role


def create_from_preset(
    db: Session, preset: str, name: str | None = None, *, actor: User | None = None
) -> Role:
    if preset not in PRESETS:
        raise errors.ValidationError(f"Unknown preset: {preset}", code="unknown_preset")
    template = PRESETS[preset]
    return create_role(
        db, name or template["name"], template["permissions"], preset=preset, actor=actor
    )


def _refuse_granting_what_you_lack(db: Session, actor: User, codes: set[str]) -> None:
    """Раздать можно только то, что есть у самого.

    Одно правило на все двери сразу, и оно же — единственное, что отделяет
    «управляет правами» от «имеет все права».

    Первым закрывали частный случай: нельзя дописать прав своей же роли.
    Сотрудник с одним лишь `roles.manage` отправлял `PATCH /roles/{своя}` со
    всем реестром и получал журнал, настройки, сотрудников и суммы. Запрет
    работал, но обходился в два шага и два аккаунта: завести роль со всеми
    правами (это разрешалось!), отдать её коллеге, коллега отдаёт её тебе.
    Каждый шаг по отдельности выглядел законным.

    Поэтому проверка стоит не на «своей роли», а на самих правах: чего нет у
    тебя, того ты не выдашь — ни себе, ни коллеге, ни новой роли, ни через
    неделю чужими руками. Обход через второго человека закрывается тем, что
    роли-со-всеми-правами больше неоткуда взяться.

    **Чем за это платим.** Сотрудник с узкой ролью и `roles.manage` теперь
    раздаёт только то, что умеет сам. Офис-менеджер, заводивший людей на роль
    «Гендиректор», больше не может — это делает root или тот, у кого такие
    права есть. Размен сознательный: «раздаёт кто угодно что угодно» и есть та
    дыра, ради которой всё это писалось, а узкое место лечится выдачей нужных
    прав тому, кто раздаёт.

    Убавлять — можно всегда: снять лишнее не escalation, а уборка.

    Root сюда попадает наравне со всеми, и правило для него истинно само собой:
    у него весь реестр. Исключение «если не root», написанное руками, однажды
    окажется единственным местом, где про новую дверь забыли.
    """
    missing = sorted(codes - codes_of(db, actor))
    if not missing:
        return
    raise errors.ForbiddenError(
        "Cannot grant permissions you do not have: " + ", ".join(missing),
        code="cannot_grant_what_you_lack",
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
        # Сверяем только добавленное: убавлять роль можно и тем, у кого этих
        # прав нет, — иначе снять лишнее было бы некому.
        added = {permissions.code(area, action) for area, action in pairs}
        _refuse_granting_what_you_lack(db, actor, added - codes_of_role(db, role.id))
        loses_manage = _grants_manage(db, role.id) and (
            ("roles", permissions.MANAGE) not in pairs
        )
        if loses_manage:
            _refuse_if_nobody_left_to_manage_roles(db, actor, exclude_role=role.id)
        was = codes_of_role(db, role.id)
        _write_codes(db, role, pairs)
        if added != was:
            # Только настоящая правка. Экран присылает набор целиком при каждом
            # сохранении, и запись «изменил права» там, где не изменилось ничего,
            # утопила бы настоящие правки в шуме — ровно как у сумм заявки.
            audit_service.record(
                db,
                action=audit_service.ACTION_ROLE_PERMISSIONS_CHANGED,
                actor=actor,
                source=SOURCE_MANUAL,
                entity_type=audit_service.ENTITY_ROLE,
                entity_id=role.id,
                entity_label=role.name,
                before=audit_service.permissions_text(was),
                after=audit_service.permissions_text(added),
            )
    db.flush()
    return role


def set_default(db: Session, role_id: int) -> Role:
    """Роль по умолчанию ровно одна: её получает новый сотрудник.

    Оба шага — явные UPDATE, «своя» ставится первой; подробности и цена ошибки
    описаны у основной фирмы (`company_service.set_default`), правило здесь
    ровно то же. Раньше признак снимался перебором объектов, хотя комментарий
    обещал «одним проходом, как основная фирма»: текст говорил одно, код делал
    другое. Комментарий, разошедшийся с кодом, хуже отсутствующего — следующий
    читатель поверит ему и проверять не полезет.

    Роль по умолчанию потерять особенно неприятно: без неё зарегистрировавшийся
    сотрудник входит в CRM без единого раздела и без объяснения, почему.
    """
    role = get_role(db, role_id)
    roles_repo.make_default(db, role)
    return role


def delete_role(db: Session, role_id: int, actor: User) -> None:
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
    label, role_id_before_delete = role.name, role.id
    db.delete(role)
    db.flush()
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_ROLE,
        entity_id=role_id_before_delete,
        entity_label=label,
    )


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
    if role is not None:
        # Выдать чужими руками то, чего у тебя нет, — тот же обход, только в
        # два шага. Роль уже существует, значит её собрал кто-то с этими
        # правами; раздавать её вправе тоже они.
        _refuse_granting_what_you_lack(db, actor, codes_of_role(db, role.id))
    # Человек теряет право раздавать доступы, если оно у него было, а новая
    # должность его не даёт. Себя из счёта исключаем: после правки он уже не
    # в числе тех, кто может.
    if _grants_manage(db, user.role_id) and not (role and _grants_manage(db, role.id)):
        _refuse_if_nobody_left_to_manage_roles(db, actor, exclude_user=user.id)

    was = get_role(db, user.role_id).name if user.role_id else ""
    user.role_id = role.id if role else None
    db.flush()
    # Запись на СОТРУДНИКА, а не на роль: спрашивают «почему у Иванова появился
    # доступ к деньгам», а не «кому раздавали эту должность». Ответ на второй
    # вопрос собирается из тех же записей поиском по значению.
    audit_service.record(
        db,
        action=audit_service.ACTION_ROLE_ASSIGNED,
        actor=actor,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_USER,
        entity_id=user.id,
        entity_label=user.name,
        before=was or None,
        after=role.name if role else None,
    )
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
    if roles_repo.any_exists(db):
        return
    try:
        role = create_from_preset(db, DEFAULT_PRESET)
    except errors.ConflictError:
        # Соседний процесс успел посеять ту же роль между нашей проверкой и
        # вставкой: `uvicorn --workers 2` поднимает оба разом. Итог нужен один и
        # тот же, и он уже достигнут — падать в этот момент значит не поднять
        # приложение вовсе.
        return
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
