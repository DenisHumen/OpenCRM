"""Связь блоков: событие вместо прямого вызова.

Блоки связаны полями — `deal_id` есть у бланков, движений, писем, звонков и
задач, — но не связаны поведением. Связывать их прямыми вызовами можно, пока
блоков три; их десять, и каждый следующий требовал бы правки старых. Старые
начали бы знать о новых, и «выключить блок» перестало бы быть безопасным
действием.

Здесь блок объявляет, что у него произошло, а кто это слушает — не его дело.
Отсюда главное обещание модульности: **выключенный блок просто не подписан**.
Не проверка `is_enabled` в десяти местах, которую забудут в одиннадцатом, а
отсутствие вызова; проверка одна и стоит в диспетчере.

Синхронно и в той же транзакции, без очередей и брокера. Асинхронность стоит
отдельного сервиса в развёртывании и отбирает атомарность — ровно то, ради чего
всё затевалось: списание со склада обязано отменяться вместе с актом, а не
«когда-нибудь потом». CRM ставится одной командой на VPS, и брокер в этот
размер не входит.

Кто на что подписан — в `core/subscriptions.py`, одним списком.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from core import modules
from core.services import modules_service
from database.models import User
from database.models.audit import SOURCES, SOURCE_MANUAL
from database.session import vzyat_zamok_zapisi

logger = logging.getLogger("opencrm.events")

#: Место в очереди по умолчанию. Числа с запасом в обе стороны: подписчику,
#: которому нужно раньше или позже соседа, не приходится переставлять чужие.
DEFAULT_ORDER = 100


@dataclass(frozen=True)
class Event:
    """Что произошло, у кого и почему.

    `actor`, `source` и `source_ref` — часть контракта события, а не забота
    каждого подписчика по отдельности. Подписчик, создавая запись, ставит того
    же живого человека, а не «систему»: иначе половина истории заявки
    оказывается ничьей, и на вопрос «кто это списал» отвечать нечем.

    Все трое протаскиваются вниз без изменений. `source` называет то, чем
    цепочка началась, а не то звено, на котором находимся: движение по складу,
    порождённое проведением акта, обязано остаться «проведением акта» и в
    журнале, и в ленте, — иначе «Иванов списал руками» и «списалось, потому что
    Иванов провёл акт» станут одной записью.
    """

    name: str
    db: Session
    #: Живой человек, чьё действие всё это запустило. Пусто бывает только у
    #: источников из `FACELESS_SOURCES` — вебхука АТС и синхронизации почты.
    actor: User | None
    #: Короткая фраза о том, что именно он сделал. Уезжает в порождённые
    #: записи, а тело записи хранится строкой и переводу задним числом не
    #: подлежит — поэтому по-английски, как и остальной текст продукта.
    reason: str
    #: Чем вызвано (`database/models/audit.py`): рука, вебхук АТС, почта.
    source: str = SOURCE_MANUAL
    #: Чем именно: номер акта, идентификатор события АТС.
    source_ref: str = ""
    #: Подробности события. Состав объявляет тот, кто событие поднимает, и
    #: описывает его рядом с вызовом `emit` — подписчику больше спросить негде.
    data: Mapping[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


@dataclass(frozen=True)
class Subscriber:
    event: str
    handler: Callable[[Event], None]
    #: Блок, которому подписчик принадлежит. Выключен блок — подписчика не
    #: зовут. None означает «общий код, выключать нечего».
    module: str | None
    order: int
    #: Участник работает в той же транзакции и может отменить операцию;
    #: наблюдатель не может отменить ничего.
    is_participant: bool
    #: Полное имя обработчика: попадает в журнал при падении и участвует в
    #: сортировке, поэтому обязано быть стабильным между запусками.
    name: str


_subscribers: list[Subscriber] = []
_order_cache: dict[str, tuple[Subscriber, ...]] = {}
_wired = False


def _sort_key(sub: Subscriber) -> tuple:
    """Порядок подписчиков — заданный, а не тот, в котором сложились импорты.

    Сначала все участники, потом наблюдатели: наблюдатель записывает то, что
    состоялось, а состоялось оно только после того, как ни один участник не
    возразил. Запусти наблюдателей первыми — и в ленте появлялись бы записи об
    операции, которую следующий участник отменил.

    Дальше — явный `order`, а при равенстве имя блока и полное имя функции.
    Оставь хвост сортировки за порядком импортов — и поведение начнёт плавать
    между запусками, а воспроизвести падение станет нечем.
    """
    return (not sub.is_participant, sub.order, sub.module or "", sub.name)


def subscribe(
    event: str,
    handler: Callable[[Event], None],
    *,
    is_participant: bool,
    module: str | None = None,
    order: int = DEFAULT_ORDER,
) -> Subscriber:
    # Ключ блока сверяем с реестром здесь, в момент подписки, а не в `emit`.
    # `is_enabled` про незнакомый ключ отвечает «выключен», и опечатка
    # (`warehous`, `client`) от честно выключенного блока ничем не отличается:
    # подписчик просто никогда не зовётся, событие проходит мимо, и никакой
    # ошибки при этом нет. Списание по акту тихо перестало бы происходить, а
    # искать причину пришлось бы по остаткам склада.
    #
    # Здесь же отказ приходит один раз и громко: подписки загружаются при первом
    # событии (`_load_subscriptions`), то есть приложение падает на старте, а не
    # ведёт себя странно месяцами.
    if module is not None and modules.get(module) is None:
        raise ValueError(
            f"Unknown module {module!r} in subscription to {event!r} "
            f"(реестр блоков — core/modules.py)"
        )

    sub = Subscriber(
        event=event,
        handler=handler,
        module=module,
        order=order,
        is_participant=is_participant,
        name=f"{handler.__module__}.{getattr(handler, '__qualname__', handler)}",
    )
    _subscribers.append(sub)
    _order_cache.clear()
    return sub


def unsubscribe(sub: Subscriber) -> None:
    """Снять подписку. Нужно тестам; в рабочем коде подписка живёт всё время."""
    if sub in _subscribers:
        _subscribers.remove(sub)
        _order_cache.clear()


def participant(event: str, *, module: str | None = None, order: int = DEFAULT_ORDER):
    """Подписчик, который может отменить операцию.

    Работает в той же транзакции: упал — не состоялось ничего, включая то, ради
    чего всё начиналось. Так подписывают списание со склада при проведении акта:
    не списалось — акт не провёлся.
    """
    def decorate(handler: Callable[[Event], None]) -> Callable[[Event], None]:
        subscribe(event, handler, is_participant=True, module=module, order=order)
        return handler

    return decorate


def observer(event: str, *, module: str | None = None, order: int = DEFAULT_ORDER):
    """Подписчик, который не может отменить ничего.

    Упал — основная операция всё равно состоялась, а ошибка ушла в журнал. Так
    подписывают запись в ленте: потерять строку в ленте плохо, но отменять
    из-за неё уже состоявшееся списание хуже.
    """
    def decorate(handler: Callable[[Event], None]) -> Callable[[Event], None]:
        subscribe(event, handler, is_participant=False, module=module, order=order)
        return handler

    return decorate


def _load_subscriptions() -> None:
    """Подтянуть список подписок при первом обращении.

    Импортом на уровне модуля это не сделать: `core.subscriptions` знает про
    сервисы, а сервисы — про события. Полагаться же на то, что кто-то другой
    успел импортировать подписки раньше, нельзя: скрипт обслуживания, дёрнувший
    сервис напрямую, молча остался бы без них, и одно и то же действие
    отрабатывало бы по-разному в приложении и в консоли.
    """
    global _wired
    if _wired:
        return
    # Флаг ставим до импорта — от повторного захода, если подписки при загрузке
    # сами дойдут до события; и снимаем при ошибке — иначе первое падение
    # импорта осталось бы единственным, а дальше система работала бы вовсе без
    # подписчиков и молча.
    _wired = True
    try:
        from core import subscriptions  # noqa: F401
    except Exception:
        _wired = False
        raise


def subscribers_of(event: str) -> tuple[Subscriber, ...]:
    """Кто слушает это событие, в том порядке, в котором будет вызван."""
    _load_subscriptions()
    if event not in _order_cache:
        _order_cache[event] = tuple(
            sorted((s for s in _subscribers if s.event == event), key=_sort_key)
        )
    return _order_cache[event]


def emit(
    name: str,
    *,
    db: Session,
    actor: User | None,
    reason: str,
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
    **data: Any,
) -> None:
    """Объявить событие и дать подписчикам отработать.

    Подписчиков может не быть вовсе — это норма, а не повод для тревоги: блок,
    выключенный у этого бизнеса, снял свою подписку вместе с собой.

    `source` по умолчанию «рука»: событие поднимает сервис, а до сервиса
    добираются через API, то есть через человека. Автоматика, вызывающая сервис
    сама, обязана сказать об этом явно — и говорит, потому что иначе исполнитель
    у неё пуст, а `audit_service.record` пустого исполнителя у руки не примет.
    """
    # Источник проверяем на входе, а не там, куда он доедет. Незнакомое значение
    # доходило до `assert_actor` внутри наблюдателя, там превращалось в ошибку,
    # наблюдатель падал под точкой отката — и запись в ленте исчезала молча, а
    # основная операция состоялась. Отказ на входе называет виноватого сразу.
    if source not in SOURCES:
        raise ValueError(f"Unknown event source: {source!r} (см. database/models/audit.py)")

    event = Event(
        name=name,
        db=db,
        actor=actor,
        reason=reason,
        source=source,
        source_ref=source_ref,
        data=data,
    )
    for sub in subscribers_of(name):
        if sub.module is not None and not modules_service.is_enabled(db, sub.module):
            continue
        if sub.is_participant:
            # Ошибку не ловим намеренно: она обязана дойти до вызывающего и
            # отменить всю операцию целиком.
            _run_participant(sub, event)
        else:
            _run_observer(sub, event)


#: Метка на исключении: «это отказ участника, глотать нельзя». Ставится на самом
#: объекте исключения, а не на глобальном флаге, потому что должна пережить
#: подъём по стеку через чужие `except` и не зависеть от того, сколько событий
#: поднято одновременно и в скольких потоках.
_REFUSAL = "opencrm_participant_refusal"


def _run_participant(sub: Subscriber, event: Event) -> None:
    """Участник работает без точки отката и его отказ помечается как отказ.

    Метка нужна не здесь, а этажом выше — в `_run_observer`. Разбор там.
    """
    try:
        sub.handler(event)
    except Exception as refusal:
        setattr(refusal, _REFUSAL, True)
        raise


def _run_observer(sub: Subscriber, event: Event) -> None:
    """Наблюдатель работает под точкой отката.

    Без неё «упавший наблюдатель ничего не отменяет» — обещание на словах.
    Наблюдатель, упавший на середине, оставил бы половину своих записей; а
    ошибка от самой базы (нарушенное ограничение, например) испортила бы
    сессию, и основная операция не смогла бы завершиться — то есть наблюдатель
    отменил бы то, что отменять не имеет права. Точка отката снимает оба
    случая: откатывается ровно то, что успел сделать он.

    **Проглатывается падение самого наблюдателя, а не чей угодно отказ.** Пусть
    наблюдатель поднял внутри себя второе событие — так делают, когда запись в
    ленте сама по себе является поводом для чего-то ещё. У второго события свои
    участники, и участник имеет право отменить операцию: не списалось — акт не
    провёлся. Поймай мы этот отказ здесь — обещание «участник может отменить»
    держалось бы только на первом уровне, а на втором операция состоялась бы
    вопреки прямому «нет», и в журнале не осталось бы даже строки о том, что
    кто-то возражал.

    Свои записи наблюдателя при этом всё равно откатываются: он их не доделал.
    Дальше отказ едет вверх и отменяет всё целиком — как отменил бы участник
    первого уровня.
    """
    # Замок записи — до точки отката: SAVEPOINT сам открывает транзакцию,
    # и без этого она осталась бы читающей (разбор в database/session.py).
    vzyat_zamok_zapisi(event.db)
    savepoint = event.db.begin_nested()
    try:
        sub.handler(event)
        savepoint.commit()
    except Exception as failure:
        if savepoint.is_active:
            savepoint.rollback()
        if getattr(failure, _REFUSAL, False):
            raise
        # exception, а не warning: наблюдатель молчаливо потерянный — это то,
        # чего никто не заметит до разговора с клиентом.
        logger.exception("subscriber %s failed on %s", sub.name, event.name)
