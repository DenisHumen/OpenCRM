"""Реестр прав: какие бывают области и какие действия в них осмысленны.

Как `core/modules.py`, это только описание: какие права есть, знает код, кому
выданы — база (`roles`, `role_permissions`), а сводит всё вместе
`core/services/permissions_service.py`. Иначе строка от снесённого блока
продолжала бы «давать право» на то, чего в коде уже нет.

Матрица строится по реестру блоков, а не руками: ручной список разошёлся бы
молча — право на несуществующий раздел никто не проверит, а раздел без права
открыт всем.
"""

from dataclasses import dataclass

from core import modules

# --- действия ---

#: Базовые действия. Есть у любой области, если не сказано иное.
VIEW = "view"
CREATE = "create"
EDIT = "edit"
DELETE = "delete"

BASE_ACTIONS: tuple[str, ...] = (VIEW, CREATE, EDIT, DELETE)

#: Особые действия: через «видит / не видит раздел» такая просьба не
#: описывается. Выпустить бланк — не то же, что завести его: квитанцию печатает
#: приёмщик, а закрывает работу мастер.
ISSUE = "issue"
#: Двигать заявку по воронке. Вести заявку и решать, что она «выполнена», —
#: разные полномочия: на этапе `won` считается выручка.
MOVE_STAGE = "move_stage"
#: Видеть чужие заявки. Без права человек видит только те, где он ответственный;
#: это фильтр в запросах, а не признак в интерфейсе.
VIEW_OTHERS = "view_others"
#: Видеть суммы. Самая частая просьба и главная причина, по которой прав на
#: раздел недостаточно: менеджер ведёт заявку, но не видит её маржу.
VIEW_AMOUNTS = "view_amounts"
#: Вернуть мягко удалённое. Отдельно от `delete`: удалять — рутина, поднимать
#: удалённое — разбор происшествия.
RESTORE = "restore"
#: Распоряжаться системной областью (сотрудники, роли, настройки). Дробить их
#: на четыре базовых действия нечего: «создать половину настройки» не бывает.
MANAGE = "manage"

#: Все действия, какие бывают. Порядок — как показывать столбцы матрицы.
ACTIONS: tuple[str, ...] = (
    VIEW,
    CREATE,
    EDIT,
    DELETE,
    RESTORE,
    ISSUE,
    MOVE_STAGE,
    VIEW_OTHERS,
    VIEW_AMOUNTS,
    MANAGE,
)


@dataclass(frozen=True)
class Area:
    """Строка матрицы: раздел, в котором раздаются права."""

    key: str
    #: Блок, вместе с которым область закрывается. None — область вне блоков
    #: (сотрудники, роли, настройки): выключить их нельзя, проверять нечего.
    module: str | None
    actions: tuple[str, ...]

    @property
    def is_system(self) -> bool:
        return self.module is None


# Чем область отличается от базового набора. Блок, не названный здесь, получает
# ровно `BASE_ACTIONS` — это и есть «появился блок, появилась строка». Особые
# действия и сужения по ключу блока не угадать: их объявляют здесь поимённо.
AREA_ACTIONS: dict[str, tuple[str, ...]] = {
    "clients": BASE_ACTIONS + (RESTORE,),
    "deals": BASE_ACTIONS + (MOVE_STAGE, VIEW_OTHERS, VIEW_AMOUNTS),
    "documents": BASE_ACTIONS + (ISSUE,),
    # `issue` у заказа — провести: отгрузить или принять на склад. Отделено от
    # набора позиций: сборщик набирает, отгружает старший. `view_amounts` — как
    # у склада: менеджер ведёт заказ, но цену закупки видеть не обязан.
    "orders": BASE_ACTIONS + (ISSUE, VIEW_AMOUNTS),
    # Накладные. Набор и доводы те же, что у заказов. `delete` в базовом наборе
    # есть, но проведённую накладную не удалить: проверка стоит в службе — право
    # говорит, КОМУ можно, а не КОГДА.
    "waybills": BASE_ACTIONS + (ISSUE, VIEW_AMOUNTS),
    # `manage` — распоряжаться складами как местами: завести, переименовать,
    # закрыть. Приход, расход и перемещение остаются на `create`: товар двигают
    # каждый день, а склады заводят раз в год.
    "warehouse": BASE_ACTIONS + (RESTORE, VIEW_AMOUNTS, MANAGE),
    # Наклейки: смотреть и печатать — одно действие (печать и есть просмотр
    # страницы), а привязка кода к товару отдельно: чужой код значит, что сканер
    # молча подставляет не тот товар, и видно это только на инвентаризации.
    "labels": (VIEW, CREATE, DELETE),
    # Отчёты нельзя «создать» или «удалить» — они считаются по заявкам.
    # Осмысленное деление одно: видеть картину и видеть в ней деньги.
    "reports": (VIEW, VIEW_AMOUNTS),
    # `edit` и `delete` не объявлены: операцию не правят, ошибку исправляют
    # обратной (разбор — в модели `FinanceOperation`). `manage` — справочник
    # статей и планы: их заводят раз в год. `view_amounts` нет: финансы без
    # сумм — пустая таблица, `view` и есть оно.
    "finance": (VIEW, CREATE, MANAGE),
    # `edit` и `delete` не объявлены: отправленное сообщение клиент уже прочитал,
    # правка задним числом была бы способом подделать переписку. `view_others`
    # нет: у диалога нет ответственного, фильтровать нечего. `manage` — токен,
    # чат сводки, подключение: пишут каждый день, бота подключают раз и навсегда.
    "telegram": (VIEW, CREATE, MANAGE),
}

# Области вне реестра блоков: они про управление системой, выключить их нельзя —
# отсюда `module=None`. `roles` отделена от `staff`: слитые в одно право они
# означали бы, что всякий, кто заводит людей, может выдать себе что угодно.
SYSTEM_AREAS: tuple[Area, ...] = (
    Area(key="staff", module=None, actions=(VIEW, MANAGE)),
    Area(key="roles", module=None, actions=(VIEW, MANAGE)),
    Area(key="settings", module=None, actions=(VIEW, MANAGE)),
    # Журнал только дописывается, и запрет на правку стоит в самой модели, а не
    # в правах: `edit` или `delete` обещали бы то, чего система не позволит
    # никому, включая root: право, которое ничего не даёт, хуже его отсутствия.
    Area(key="audit", module=None, actions=(VIEW,)),
)


def _build() -> tuple[Area, ...]:
    """Матрица: сначала блоки в порядке реестра, потом системные области."""
    from_modules = tuple(
        Area(
            key=module.key,
            module=module.key,
            actions=AREA_ACTIONS.get(module.key, BASE_ACTIONS),
        )
        for module in modules.MODULES
    )
    return from_modules + SYSTEM_AREAS


AREAS: tuple[Area, ...] = _build()

BY_KEY: dict[str, Area] = {area.key: area for area in AREAS}


def get(key: str) -> Area | None:
    return BY_KEY.get(key)


def exists(area: str, action: str) -> bool:
    found = BY_KEY.get(area)
    return found is not None and action in found.actions


def module_of(area: str) -> str | None:
    """Какой блок закрывает область. None — область вне блоков."""
    found = BY_KEY.get(area)
    return found.module if found else None


def code(area: str, action: str) -> str:
    """Строковый вид права — то, чем оно ездит в API и лежит во фронтенде."""
    return f"{area}.{action}"


def all_codes() -> tuple[str, ...]:
    """Все права, какие есть. У root — ровно этот набор, всегда."""
    return tuple(code(area.key, action) for area in AREAS for action in area.actions)


def parse(value: str) -> tuple[str, str] | None:
    """Разобрать «область.действие». None — если такого права не существует.

    Разбор с правого конца, а не с левого: правая часть всегда действие, даже
    если в ключе однажды окажется точка.
    """
    if "." not in value:
        return None
    area, _, action = value.rpartition(".")
    return (area, action) if exists(area, action) else None
