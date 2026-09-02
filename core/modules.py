"""Реестр блоков системы.

Только описание: какие блоки бывают и что от чего зависит. Код — источник
правды о том, какие блоки есть, база — о том, какие нужны этому бизнесу: иначе
строка от снесённого блока продолжала бы «включать» то, чего в коде уже нет.

Выключенный блок обязан исчезать целиком, из меню, API и отчётов, не задевая
соседей; прикручивать это к десяти готовым блокам поздно.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Module:
    key: str
    #: Несущий блок: на нём держится всё остальное, выключать нечему.
    core: bool = False
    #: Написан и работает. False — блок в планах: показываем с пометкой «в
    #: разработке», но не включаем, потому что переключатель к ненаписанному —
    #: обещание, а не функция.
    ready: bool = True
    #: Включён ли у нового пользователя, если блок необязательный.
    default: bool = True
    #: Блоки, без которых этот не имеет смысла.
    requires: tuple[str, ...] = field(default_factory=tuple)


# Порядок важен: в таком виде блоки показываются в настройках, от несущих к
# необязательным.
MODULES: tuple[Module, ...] = (
    # --- ядро ---
    # Без клиента не к чему привязать ни заявку, ни документ, ни письмо.
    Module(key="clients", core=True),
    # Заявка — стержень: на ней сходятся этапы, комментарии, документы.
    # Выключить её значит рассыпать систему на несвязанные списки.
    Module(key="deals", core=True, requires=("clients",)),
    # --- необязательные, уже работают ---
    # Ни от чего не зависят намеренно: счёт выставляют раньше, чем заводят
    # работу, — реквизиты нужны и тому, у кого нет ни одной заявки.
    Module(key="companies"),
    Module(key="documents", requires=("clients",)),
    # Ни от чего не зависят: «отвезти документы в банк» — тоже задача.
    Module(key="tasks"),
    # Включены по умолчанию, в отличие от почты и телефонии: тем нужна
    # настроенная снаружи железка, а шаблону — только клавиатура. Стоят на
    # клиентах: подстановки называют клиента и его заявку.
    Module(key="templates", requires=("clients",)),
    Module(key="boards", default=True),
    # Выключен, в отличие от остальных готовых: магазину и мастерской нужен,
    # дизайн-студии нет. Списание идёт под заявку — без заявок остаётся голый
    # остаток, не отвечающий на вопрос «во сколько нам обошлась эта работа».
    Module(key="warehouse", default=False, requires=("deals",)),
    # Выключены: без термопринтера это кнопка «печать», ведущая в никуда.
    # Выключение прячет печать и сканер, но НЕ трогает коды: они свойство
    # товара, а не наклейки, — включат обратно, и всё на месте.
    Module(key="labels", default=False, requires=("warehouse",)),
    # Заказ — вид бланка (номер, статусы, печать, поиск сканом берутся оттуда),
    # отсюда жёсткая связь. Со складом мягкая, проверкой `is_enabled` в
    # `order_service.close` и `revert`: заказ на услуги складу не нужен.
    Module(key="orders", default=False, requires=("documents",)),
    # Свой переключатель, хотя накладная — вид бланка, как и заказ: мастерская
    # выдаёт квитанции и не отгружает, магазин наоборот. Со складом связь
    # мягкая, как у заказов: проверка `is_enabled` в `waybill_service`.
    Module(key="waybills", default=False, requires=("documents",)),
    # Отчёты считаются по заявкам и их этапам — без заявок считать нечего.
    Module(key="reports", requires=("deals",)),
    # Выключена: ящик фирмы есть не у всех, а блок без ящика — пустой раздел в
    # меню. Стоит на клиентах: письмо привязывается к клиенту по адресу.
    Module(key="mail", default=False, requires=("clients",)),
    # Выключена: без настроенной АТС это пустой журнал звонков в меню. Зависит
    # только от клиентов: заявка у звонка бывает не всегда («спросил про цены»).
    Module(key="telephony", default=False, requires=("clients",)),
    # Третий канал рядом с почтой и телефонией, и доводы те же: сообщение
    # обязано находить, чьё оно, а заявка у переписки бывает не всегда; без
    # настроенного бота раздел пуст.
    Module(key="telegram", default=False, requires=("clients",)),
    # Без `requires` намеренно: «Аренда за август» не относится ни к заявке, ни
    # к клиенту. Зависимость запретила бы вести расходы тому, кто не ведёт
    # заявок, — ровно тому, ради кого блок и включают отдельно.
    Module(key="finance", default=False),
    # Ни от чего не зависит: он про машину, а не про заявки. Выключен: на VPS с
    # двумя гигабайтами полный набор наблюдателей забирает заметную часть
    # памяти, а сайт живёт с неё же.
    Module(key="monitoring", default=False),
)

BY_KEY: dict[str, Module] = {module.key: module for module in MODULES}

KEYS: tuple[str, ...] = tuple(module.key for module in MODULES)


def get(key: str) -> Module | None:
    return BY_KEY.get(key)


def dependents_of(key: str) -> tuple[str, ...]:
    """Кто перестанет иметь смысл, если выключить этот блок. Только прямые."""
    return tuple(m.key for m in MODULES if key in m.requires)


def dependents_tree(key: str) -> tuple[str, ...]:
    """Все, кто уйдёт следом за этим блоком, — включая зависимых от зависимых.

    Цепочки длиннее звена уже есть: наклейки на складе, склад на заявках.
    Порядок — от дальних к ближним: гасить надо снизу вверх, иначе на середине
    окажется блок, чьё основание уже погасили.
    """
    order: list[str] = []
    seen = {key}

    def walk(current: str) -> None:
        for dependent in dependents_of(current):
            if dependent in seen:
                continue
            seen.add(dependent)
            walk(dependent)
            order.append(dependent)

    walk(key)
    return tuple(order)


def requirements_tree(key: str) -> tuple[str, ...]:
    """Всё, что должно быть включено, чтобы этот блок имел смысл.

    Порядок — от дальних к ближним: основание раньше того, что на нём стоит.
    """
    order: list[str] = []
    seen = {key}

    def walk(current: str) -> None:
        module = BY_KEY.get(current)
        if module is None:
            return
        for required in module.requires:
            if required in seen:
                continue
            seen.add(required)
            walk(required)
            order.append(required)

    walk(key)
    return tuple(order)


# --- наборы блоков под тип дела ----------------------------------------------
#
# Набор — точка отсчёта, а не режим работы: тип дела нигде не запоминается,
# иначе через полгода в коде тридцать развилок `if тип == ...`. Состав лежит
# здесь, а не во фронтенде: список ключей в `.tsx` разошёлся бы с реестром, и
# молча — ровно так однажды разошлись значки блоков между меню и настройками.


@dataclass(frozen=True)
class Preset:
    """Чем занимается бизнес — и что ему для этого включить."""

    key: str
    #: Блоки сверх несущих. Порядок не важен: включает их `modules_service`, и
    #: он же поднимает то, на чём они стоят.
    modules: tuple[str, ...]
    #: Пресет воронки из `pipeline_service.PRESETS`.
    pipeline: str
    #: Как называть заявку: `deal | order | request | booking` (см.
    #: `database/models/settings.py`). Подписи по всей системе меняются вслед.
    deal_term: str


#: Порядок — как показывать карточки при первом входе: от самого частого.
PRESETS: tuple[Preset, ...] = (
    Preset(
        key="services",
        modules=("documents", "warehouse", "tasks", "telephony", "telegram", "finance"),
        pipeline="services",
        deal_term="request",
    ),
    Preset(
        key="retail",
        modules=("warehouse", "labels", "orders", "documents", "finance"),
        pipeline="shop",
        deal_term="order",
    ),
    # Накладные — в оптовом и производственном, но НЕ в розничном: там товар
    # уходит через кассу и бумагой служит чек, а в опте партию без
    # сопроводительного документа получатель не примет.
    Preset(
        key="wholesale",
        modules=(
            "warehouse", "labels", "orders", "documents", "waybills",
            "companies", "mail", "finance",
        ),
        pipeline="shop",
        deal_term="order",
    ),
    Preset(
        key="production",
        modules=(
            "warehouse", "labels", "orders", "documents", "waybills",
            "companies", "finance",
        ),
        pipeline="universal",
        deal_term="order",
    ),
    Preset(
        key="agency",
        modules=("boards", "tasks", "mail", "finance"),
        pipeline="agency",
        deal_term="deal",
    ),
)

PRESETS_BY_KEY: dict[str, Preset] = {preset.key: preset for preset in PRESETS}


def preset(key: str) -> Preset | None:
    return PRESETS_BY_KEY.get(key)
