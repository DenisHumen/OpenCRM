"""Кто на что подписан — одним списком.

Подписки собраны здесь, а не разложены по сервисам, ровно по той причине, из-за
которой затевался весь механизм: через три блока на вопрос «что от чего
срабатывает» должно отвечать одно место, а не обход десяти файлов с grep'ом.
Объявляющего это ни к чему не обязывает — `deal_service` по-прежнему не знает,
кто его слушает, и не изменится, когда подписчиков станет пять.

Сами обработчики тонкие: работу делает сервис того блока, которому запись
принадлежит. Здесь — только «когда» и «в каком качестве».

--------------------------------------------------------------------------
Запись в ленту или сборка ленты из нескольких источников при чтении
--------------------------------------------------------------------------

Выбрано первое: подписчик пишет строку в `client_notes`, а `document_events` и
`stock_moves` остаются нетронутыми — на них держатся свои экраны и отчёты.

Довод против записи известен: копия способна разойтись с оригиналом. Здесь он
не работает, и это не удача, а свойство обоих источников. Движение склада не
редактируется и не удаляется — ошибку исправляют обратным движением
(`core/services/warehouse_service.py`); запись `document_events` тоже только
дописывается. Расходиться копии не с чем: у оригинала нет второй редакции.

Довод за запись весомее, и он про модули. Лента обязана пережить выключение
склада — запись о списании принадлежит ленте, а не складу, ровно как письмо и
звонок. Сборка при чтении означала бы, что лента лезет в `stock_moves` каждый
раз, когда её открывают, — то есть читает хранилище выключенного блока. Это
прямо противоречит тому, ради чего заведён реестр блоков: выключенный блок
исчезает целиком. Записанная строка от выключения не зависит вовсе.

Дальше — то, что видно глазами. Лента сортируется по «когда случилось» и
листается страницами; при сборке из трёх источников и сортировка, и страницы
уезжают в Python, и каждый новый источник переписывает их заново. При записи
это один `ORDER BY` и один `LIMIT`, уже написанные.
"""

from datetime import timedelta

from core import events
from core.events import DEFAULT_ORDER
from core.services import (
    deal_lines_service,
    client_service,
    deal_service,
    finance_service,
    pipeline_service,
    task_service,
    warehouse_service,
)
from core.services import act_service
from core.services.act_service import ACT_COMPLETED
from core.services.deal_lines_service import DEAL_LINES_CHANGED
from core.services.lead_service import LEAD_RECEIVED
from core.services.deal_service import DEAL_DELETED, DEAL_STAGE_CHANGED
from core.services import notification_service, order_service, waybill_service
from core.services import audit_service
from core.services.order_service import (
    ORDER_CANCELLED,
    ORDER_CLOSED,
    ORDER_LINES_CHANGED,
)
from core.services.return_service import RETURN_POSTED
from core.services.document_service import (
    DOCUMENT_CLOSED,
    DOCUMENT_ISSUED,
    payload_of,
)
from core.services.warehouse_service import STOCK_WRITTEN_OFF, format_quantity
from core.services.waybill_service import WAYBILL_POSTED
from core.utils import now_utc
from database.models.client import KIND_DOCUMENT, KIND_STAGE, KIND_STOCK
from database.models.document import KIND_SALES_ORDER, STATUS_CANCELLED
from database.models.pipeline import CLOSED_KINDS, KIND_LOST, KIND_WON


# --- уведомления сотрудникам (docs/21 §4) ------------------------------------
#
# Наблюдатели, не участники: подсказка не стоит отгрузки. Автор действия
# уведомления не получает — он видит ответ экрана.


def _uvedomit(event: events.Event, area: str, kind: str, params: dict, link: str, manager_id=None) -> None:
    notification_service.notify(
        event.db,
        notification_service.adresaty(event.db, area, manager_id=manager_id, krome=event.actor),
        kind,
        params,
        link,
    )


@events.observer(ORDER_CLOSED, module="orders")
def uvedomit_o_zakrytom_zakaze(event: events.Event) -> None:
    order = event["order"]
    _uvedomit(event, "orders", "order_closed", {"number": order.number, "kind": order.kind}, f"/orders/{order.id}")


@events.observer(RETURN_POSTED, module="orders")
def uvedomit_o_vozvrate(event: events.Event) -> None:
    vozvrat = event["vozvrat"]
    _uvedomit(
        event, "orders", "return_posted",
        {"number": vozvrat.number, "order": event["order"].number}, f"/returns/{vozvrat.id}",
    )


@events.observer(ORDER_CANCELLED, module="orders")
def uvedomit_ob_otmene_zakaza(event: events.Event) -> None:
    order = event["order"]
    _uvedomit(event, "orders", "order_cancelled", {"number": order.number}, f"/orders/{order.id}")


@events.observer(WAYBILL_POSTED, module="waybills")
def uvedomit_o_provedyonnoy_nakladnoy(event: events.Event) -> None:
    waybill = event["waybill"]
    _uvedomit(event, "waybills", "waybill_posted", {"number": waybill.number, "kind": waybill.kind}, f"/waybills/{waybill.id}")


@events.observer(ACT_COMPLETED, module="documents")
def uvedomit_o_provedyonnom_akte(event: events.Event) -> None:
    act = event["act"]
    _uvedomit(event, "documents", "act_completed", {"number": act.number}, f"/documents/{act.id}")


@events.observer(DEAL_STAGE_CHANGED, module="deals")
def uvedomit_o_smene_etapa(event: events.Event) -> None:
    deal = event["deal"]

    # Названиями этапов, а не ключами: «new → packed» читается хуже, чем
    # «Новый заказ → Собран». Ключ остаётся на случай, если этап уже убрали.
    def imya(klyuch: str) -> str:
        try:
            return pipeline_service.get_stage(event.db, klyuch).name
        except Exception:  # noqa: BLE001 — уведомление не стоит смены этапа
            return klyuch

    _uvedomit(
        event, "deals", "deal_stage",
        {"title": deal.title, "from_stage": imya(event["from_stage"]), "to_stage": imya(event["to_stage"])},
        f"/deals/{deal.id}", manager_id=deal.manager_id,
    )


@events.observer(LEAD_RECEIVED, module="deals")
def uvedomit_o_zayavke_s_sayta(event: events.Event) -> None:
    deal = event["deal"]
    _uvedomit(event, "deals", "lead_received", {"title": deal.title, "client": event["client"].name}, f"/deals/{deal.id}", manager_id=deal.manager_id)


@events.participant(DEAL_LINES_CHANGED, module="documents")
def akt_povtoryaet_zayavku(event: events.Event) -> None:
    """Акт работ по заявке заводится и переписывается сам (docs/21)."""
    act_service.zerkalo_po_zayavke(event.db, event["deal"], event.actor)


@events.participant(DEAL_STAGE_CHANGED, module="documents", order=DEFAULT_ORDER + 10)
def avto_akt_ukhodit_s_zakrytiem(event: events.Event) -> None:
    """Заявка закрыта на доске мимо акта — заведённый сам акт отменяется.

    После списания (`spisat_tovar_pri_vyigryshe`, порядок ниже): списала
    заявка, и второй путь к складу через акт обязан закрыться.
    """
    stage = pipeline_service.get_stage(event.db, event["to_stage"])
    if stage.kind not in CLOSED_KINDS or event.actor is None:
        return
    act_service.zakryt_avto_akt(
        event.db, event["deal"].id, event.actor, "deal closed on the board"
    )


@events.participant(DEAL_DELETED, module="documents")
def avto_akt_ukhodit_s_zayavkoy(event: events.Event) -> None:
    act_service.ubrat_avto_akt(event.db, event["deal"].id)


@events.participant(ORDER_LINES_CHANGED, module="waybills")
def nakladnaya_povtoryaet_zakaz(event: events.Event) -> None:
    """Черновик накладной по заказу заводится и переписывается сам.

    `participant`: не удалось повторить — правка заказа не прошла. Иначе заказ и
    накладная разошлись бы на одну позицию, и заметил бы это клиент.
    """
    waybill_service.zerkalo_po_zakazu(event.db, event["order"], event.actor)


@events.participant(ORDER_CANCELLED, module="waybills")
def chernovik_ukhodit_s_zakazom(event: events.Event) -> None:
    waybill_service.ubrat_avto_chernovik(event.db, event["order"])


@events.participant(DEAL_STAGE_CHANGED, module="warehouse")
def spisat_tovar_pri_vyigryshe(event: events.Event) -> None:
    """Выигранная заявка списывает со склада то, что обещала и что ещё не ушло.

    Подписчиком, а не вызовом из `move_stage`, по двум причинам сразу. Первая —
    путей закрытия несколько: кнопка на карточке, перетаскивание на доске,
    проведение акта с `next_stage`. Вызов пришлось бы вписать в каждый, и
    следующий путь его не получил бы. Вторая — модульность: у студии без склада
    подписчика просто нет, и `is_enabled` не нужен ни здесь, ни в сервисе.

    `participant`, а не `observer`: не списалось — заявка не закрылась. Иначе
    этап уехал бы в «выиграно», товар остался бы на остатке, и расхождение
    заметили бы при инвентаризации, через месяц.

    ПРОИГРАННАЯ заявка не списывает ничего: товар по ней никуда не уехал. Бронь
    при этом исчезает сама — заявка закрыта (`reserve_service`).
    """
    stage = pipeline_service.get_stage(event.db, event["to_stage"])
    if stage.kind != KIND_WON:
        return
    deal_lines_service.spisat_pri_zakrytii(event.db, event["deal"], event.actor)


@events.participant(DEAL_STAGE_CHANGED, module="orders", order=DEFAULT_ORDER + 20)
def zakazy_ukhodyat_s_proigryshem(event: events.Event) -> None:
    """Проигранная заявка отменяет свои открытые заказы.

    Отгружать по ней нечего, а открытый заказ держал бы бронь и висел в списке
    «принято» без единого пути закрыть его честно. Выигранная — не трогает:
    заказ там и есть путь выдачи, и закрывается своим ходом. Безликий источник
    (`actor is None`) пропускается, как у авто-акта: отмена пишется от имени
    человека.
    """
    stage = pipeline_service.get_stage(event.db, event["to_stage"])
    if stage.kind != KIND_LOST or event.actor is None:
        return
    for zakaz in order_service.otkrytye_po_zayavke(event.db, event["deal"].id):
        order_service.cancel(event.db, zakaz.id, event.actor, note="deal lost on the board")


@events.observer(ORDER_CLOSED, module="clients")
def order_closed_into_feed(event: events.Event) -> None:
    """Отгрузка и приёмка попадают в ленту клиента одной строкой.

    Отмена заказа в ленту шла (через `set_status`), а отгрузка — нет: лента
    отвечала «заказ отменили» и молчала о том, что товар выдали.
    """
    order = event["order"]
    if order.client_id is None:
        return
    what = "shipped" if order.kind == KIND_SALES_ORDER else "received"
    client_service.add_system_note(
        event.db,
        order.client_id,
        event.actor,
        KIND_DOCUMENT,
        f"Order {order.number} {what} ({event.reason})",
        deal_id=order.deal_id,
        source=event.source,
    )


@events.observer(RETURN_POSTED, module="clients")
def return_into_feed(event: events.Event) -> None:
    vozvrat = event["vozvrat"]
    if vozvrat.client_id is None:
        return
    client_service.add_system_note(
        event.db,
        vozvrat.client_id,
        event.actor,
        KIND_DOCUMENT,
        f"Return {vozvrat.number} for order {event['order'].number}: "
        f"{len(event['lines'])} line(s), refund {audit_service.money_text(vozvrat.refund_minor or 0)} "
        f"({event.reason})",
        deal_id=vozvrat.deal_id,
        source=event.source,
    )


@events.observer(WAYBILL_POSTED, module="clients")
def waybill_into_feed(event: events.Event) -> None:
    """Накладная без заказа — одной строкой в ленту; по заказу строку даёт сам
    заказ (он закрывается той же накладной и называет её номер)."""
    waybill = event["waybill"]
    if waybill.client_id is None or waybill.basis_id is not None:
        return
    client_service.add_system_note(
        event.db,
        waybill.client_id,
        event.actor,
        KIND_DOCUMENT,
        f"Waybill {waybill.number} posted: {len(event['lines'])} line(s) ({event.reason})",
        deal_id=waybill.deal_id,
        source=event.source,
    )


@events.observer(DEAL_STAGE_CHANGED, module="clients")
def stage_change_into_feed(event: events.Event) -> None:
    """Смена этапа попадает в ленту заявки.

    Почему именно это первым подписчиком. Журнал этапов
    (`deal_stage_changes`) существует и до событий, но живёт отдельной панелью
    на одном экране: менеджер, читающий ленту клиента, видит звонки и письма и
    не видит, что заявка при этом доехала до «Выдано». Лента заведена как
    единственный поток всего, что происходило, — смена этапа туда и просится,
    и это связь между блоками, а не выдумка ради демонстрации механизма.

    Наблюдатель, а не участник: потерянная строка в ленте — потеря, но отменять
    из-за неё состоявшийся перевод по воронке было бы хуже. Заявка на доске уже
    в другой колонке, и возвращать её назад из-за записи в журнале — значит
    спорить с тем, что человек только что сделал руками.

    Блок `clients` сегодня несущий и выключенным не бывает; указан он не для
    красоты, а потому что лента принадлежит именно ему — и если она когда-нибудь
    переедет в необязательный блок, эта строка уже верна.

    Исполнитель и источник берутся из события и уезжают в запись как есть.
    Подставить здесь «систему» — значит сделать половину истории заявки ничьей;
    подставить «руку» вместо настоящего источника — значит стереть разницу между
    «перетащил на доске» и «переехало, потому что провели акт».
    """
    deal = event["deal"]
    stages = {s.key: s for s in pipeline_service.list_stages(event.db, include_archived=True)}
    was = stages[event["from_stage"]].name if event["from_stage"] in stages else ""
    now = stages[event["to_stage"]].name if event["to_stage"] in stages else event["to_stage"]

    # По-английски, как и тело записи о звонке: язык интерфейса у каждого
    # сотрудника свой, а тело заметки хранится строкой и переводу задним числом
    # не подлежит.
    body = f"Stage: {was} → {now}" if was else f"Stage: {now}"
    client_service.add_system_note(
        event.db,
        deal.client_id,
        event.actor,
        KIND_STAGE,
        f"{body} ({event.reason})",
        deal_id=deal.id,
        source=event.source,
    )


@events.observer(DOCUMENT_ISSUED, module="clients")
def document_issued_into_feed(event: events.Event) -> None:
    """Выпуск бланка попадает в ленту.

    До этого выданная квитанция жила только в `document_events` и в списке
    бланков сделки. Менеджер, читающий ленту, видел звонки и письма и не видел,
    что вчера человеку выдали бумагу, — а это ровно то событие, о котором
    клиент завтра позвонит.

    Наблюдатель: потерять строку в ленте плохо, отменить из-за неё уже
    напечатанный и отданный бланк — хуже. Бумага у клиента на руках, и спорить
    с этим записью в журнале бессмысленно.
    """
    _document_entry(event, "issued")


@events.observer(DOCUMENT_CLOSED, module="clients")
def document_closed_into_feed(event: events.Event) -> None:
    """Закрытие и аннулирование бланка попадают в ленту.

    Пара к выпуску: «выдали квитанцию» без «закрыли квитанцию» оставляет ленту с
    открытым концом, и на вопрос «вещь-то отдали?» отвечать снова нечем.
    """
    _document_entry(event, "cancelled" if event["to_status"] == STATUS_CANCELLED else "closed")


def _document_entry(event: events.Event, what: str) -> None:
    """Общее тело обеих записей о бланке.

    Клиента берём у бланка, а не у заявки: бланк выдают и без заявки — человек
    стоит у стойки, и заводить работу до квитанции неудобно. А вот без клиента
    записи не будет: запись в ленте всегда о ком-то, и «бланк на имя,
    набранное в поле» приткнуть некуда.
    """
    document = event["document"]
    if document.client_id is None:
        return

    # Номер и предмет — то, чем бланк называют вслух: «где там ноутбук по
    # двести двадцать третьему». Снимок берём из самого бланка, а не из карточки
    # товара: на бумаге у клиента напечатано именно это.
    item = (payload_of(document).get("fields") or {}).get("item") or ""
    subject = f": {item}" if item and what == "issued" else ""
    client_service.add_system_note(
        event.db,
        document.client_id,
        event.actor,
        KIND_DOCUMENT,
        f"Document {document.number} {what}{subject} ({event.reason})",
        deal_id=document.deal_id,
        # Источник протаскивается вниз без изменений — как обещает докстрока
        # `Event`. Без него запись бралась за «сделал человек», а у безликого
        # источника (вебхук АТС, забор почты) исполнителя нет: проверка
        # `assert_actor` отказывала, наблюдатель падал под точкой отката, и
        # **запись в ленте исчезала молча** при состоявшейся операции.
        source=event.source,
    )


@events.observer(STOCK_WRITTEN_OFF, module="clients")
def write_off_into_feed(event: events.Event) -> None:
    """Списание со склада под заявку попадает в ленту.

    Врезка себестоимости в карточке заявки показывает итог «сколько всего
    ушло», но не отвечает на вопрос «когда»: списали деталь до разговора с
    клиентом или после того, как он отказался. В ленте это стоит на своём месте
    во времени, между звонком и письмом.

    Наблюдатель, и здесь это особенно важно: товар уже физически ушёл с полки.
    Отменить списание из-за не записавшейся строки в ленте значит вернуть на
    остаток то, чего на складе нет.

    `module="clients"`, а не `"warehouse"`: подписчик пишет в ленту, а лента
    принадлежит клиентам. Выключенный склад перестанет поднимать событие сам —
    его роутер закрыт целиком, — и уже написанные строки это никак не тронет.

    **Денег в строке нет, и это не забывчивость.** Себестоимость списания
    просилась сюда («списали деталей на три тысячи» — то, ради чего ленту и
    открывают), и какое-то время она здесь была. Стоила она права
    `warehouse.view_amounts`: тело записи ленты — обычная строка, и ни
    `GET /clients/{id}/notes`, ни `GET /deals/{id}/feed` не умеют вычёркивать из
    неё числа. Получалось, что `GET /warehouse/moves` честно отдавал
    `cost: null` тому, кому суммы не положены, а лента показывала ту же сумму
    словами — то есть автоматика проносила деньги мимо права, которым они
    закрыты. Право, обходимое соседним экраном, не право.

    Вернуть сумму в ленту можно, но не строкой: нужна отдельная колонка у
    `client_notes` и вычёркивание при выдаче, как у сделок и товаров, — то есть
    миграция. Пока её нет, деньги живут там, где на них есть охранник: в
    движениях склада и во врезке себестоимости в карточке заявки.
    """
    move, product, deal = event["move"], event["product"], event["deal"]

    # Количество без знака: «списали 3 шт». Минус в строке ничего не добавляет —
    # направление уже сказано словом.
    amount = f"{format_quantity(abs(move.quantity_milli))} {product.unit}"

    client_service.add_system_note(
        event.db,
        deal.client_id,
        event.actor,
        KIND_STOCK,
        f"Stock: {product.name} — {amount} ({event.reason})",
        deal_id=deal.id,
        source=event.source,   # см. объяснение у бланков выше
    )


# --- акт выполненных работ ----------------------------------------------------
#
# Здесь и видно, ради чего затевался весь механизм. Акт закрывает работу **одним
# действием**, а действие это трогает три блока сразу: списывает материалы
# (склад), фиксирует сделанное (бланки) и переводит заявку дальше (воронка).
# Прямые вызовы между ними означали бы, что раздел бланков не работает без
# склада, а склад знает про воронку, — то есть «выключить блок» перестало бы
# быть безопасным действием ровно в том месте, где блоков сходится больше всего.
#
# Оба подписчика ниже — **участники**, и это главное решение всей задачи.
# Наблюдатель проглатывает своё падение (`core/events._run_observer`), а значит
# акт мог бы закрыться, оставив материалы на остатке или заявку в прежнем
# этапе, — молча, и разбирать это потом пришлось бы по журналу вручную. Участник
# не проглатывает: не списалось — акт не провёлся, не перевелось — не провёлся
# тоже, и не осталось ни движений склада, ни закрытой бумаги.
#
# Порядок: сначала склад, потом воронка. Оба откатятся вместе в любом случае, но
# отказ «не хватает на складе» обязан прийти раньше, чем заявку кто-то тронул, —
# иначе причина отказа в журнале окажется не первой, и разбирающийся начнёт с
# конца цепочки.


@events.participant(ACT_COMPLETED, module="warehouse", order=10)
def write_off_act_materials(event: events.Event) -> None:
    """Проведённый акт снимает со склада то, что израсходовал.

    Работу делает склад: что считать материалом, откуда снимать, хватает ли
    остатка и во сколько это обошлось — его вопросы и его ответы. Здесь только
    «когда»: акт проведён.

    Склад выключен — подписчика не зовут, и акт проводится без списания. Это не
    половина операции: пока блока нет, списания не существует как действия, и
    мастерской без учёта деталей акт нужен ровно так же.

    Исполнитель у акта есть всегда: бумагу подписывают, а не синхронизируют, —
    безликих источников (вебхук АТС, забор почты) у этого события не бывает.
    """
    act = event["act"]
    # Что под ту же заявку уже отгрузил заказ, акт второй раз не списывает.
    uzhe_ushlo = None
    if act.deal_id:
        uzhe_ushlo = deal_lines_service.ushlo_pod_zayavku(
            event.db, act.deal_id, [row.product_id for row in event["lines"]]
        )
    warehouse_service.write_off_materials(
        event.db,
        act,
        event["lines"],
        event.actor,
        warehouse_id=event["warehouse_id"],
        confirm_negative=event["confirm_negative"],
        uzhe_ushlo=uzhe_ushlo,
        # Комментарий движения — то, чем его назовут вслух: «for certificate
        # 2026-000123». По нему же в журнале склада находят, откуда взялся минус.
        #
        # По-английски, как и всё, что система пишет в базу сама: интерфейс по
        # умолчанию английский, а запись эта общая на всех, кто откроет журнал.
        comment=f"for certificate {act.number}",
        source=event.source,
        source_ref=event.source_ref,
    )


@events.participant(ACT_COMPLETED, module="deals", order=20)
def move_deal_after_act(event: events.Event) -> None:
    """Проведённый акт переводит заявку на следующий этап.

    Через `move_stage`, а не присваиванием: это единственная точка смены этапа, и
    мимо неё не заполнятся ни журнал этапов, ни журнал действий, ни лента. Отчёт
    «сколько заявка простояла в этапе» стал бы дырявым ровно там, где её двинул
    акт, — то есть на самом частом переходе.

    Причина уезжает та, с которой пришло событие: в ленте «переехало, потому что
    провели акт» и «перетащил на доске» обязаны выглядеть по-разному.

    Заявку успели передвинуть — `move_stage` ответит конфликтом, и откатится всё,
    включая уже записанные движения склада. Так и задумано: списание без смены
    этапа выглядит как выполненная работа, которую никто не принял, и всплывает
    не раньше, чем кто-то заметит минус на складе.

    Блок `deals` несущий и выключенным не бывает; указан он не для красоты, а
    потому что переводимая запись принадлежит именно ему.
    """
    if event["to_stage"] is None:
        # Заявка уже закрыта на доске: акт записал работу, двигать нечего.
        return
    deal_service.move_stage(
        event.db,
        event["deal_id"],
        event["to_stage"],
        event.actor,
        reason=event.reason,
        source=event.source,
        source_ref=event.source_ref,
    )


@events.observer(ACT_COMPLETED, module="clients")
def act_into_feed(event: events.Event) -> None:
    """Проведённый акт попадает в ленту заявки — **одной строкой**.

    Одной, а не по строке на каждую снятую деталь: акт на пять позиций это одно
    решение человека, и лента должна показывать его так же. Обещание записано в
    докстроке `STOCK_WRITTEN_OFF`, исполняется молчаливыми движениями
    (`write_off_materials`), а строка про акт появляется здесь.

    Наблюдатель: материалы уже физически ушли с полки, а заявка уже в другой
    колонке. Отменять состоявшееся из-за не записавшейся строки в переписке —
    хуже, чем потерять строку.

    Денег в строке нет по той же причине, что и у списания: тело записи ленты —
    обычная строка, и вычёркивать из неё суммы тому, у кого нет права на них,
    нечем (разбор — у `write_off_into_feed`).
    """
    act = event["act"]
    if act.client_id is None:
        # Акт прохожему без карточки законен — как и квитанция; записи в ленте
        # тогда просто не о ком.
        return

    client_service.add_system_note(
        event.db,
        act.client_id,
        event.actor,
        KIND_DOCUMENT,
        f"Act {act.number} carried out: {len(event['lines'])} line(s)",
        deal_id=event["deal_id"],
        source=event.source,
    )


# --- деньги закрытого заказа --------------------------------------------------
#
# Оба подписчика — **участники**, и это решение того же рода, что у акта.
# Наблюдатель проглотил бы своё падение, и заказ закрылся бы без единой копейки
# — молча; а закрытый заказ, не списавший упаковку и доставку, всплывает не
# раньше, чем кто-то сведёт кассу за месяц. Именно это заказчик и просил сделать
# автоматическим.
#
# При этом отгрузка от участника не падает — не потому, что ошибку глотают, а
# потому что её нечем вызвать: у подписчика не остаётся ни одного состояния, в
# котором он падает по причине, не названной заранее.
#
# - правил нет, все выключены или все закрыты → не делает ничего, и это не
#   ошибка: у бизнеса без правил стандартных расходов не существует;
# - статья правила закрыта — единственный настоящий отказ, и он ПРЕДОТВРАЩЁН:
#   `finance_service.close_category` отвечает `category_in_use_by_rule`, пока на
#   статью смотрит живое правило. Отказ перенесён на момент правки справочника,
#   где человек его понимает;
# - сумма больше `MAX_AMOUNT_MINOR` → доменный отказ 422 с кодом, а не 500. Это
#   правда о заказе, а не сбой.
#
# `order=30` — после склада (10) и воронки (20) у соседнего события: порядок
# один на всю систему, и деньги в нём идут последними. Отказ «не хватает на
# складе» обязан прийти раньше, чем кто-то тронул деньги.


@events.participant(ORDER_CLOSED, module="finance", order=30)
def money_of_closed_order(event: events.Event) -> None:
    """Закрытый заказ списывает стандартные расходы: упаковка, доставка.

    Работу делает блок денег: что считать стандартным расходом, сколько он стоит
    и в какую статью ложится — его вопросы и его ответы. Здесь только «когда»:
    заказ проведён.

    Блок финансов выключен — подписчика не зовут, и заказ отгружается без единой
    денежной операции. Это не половина операции, а правда о системе без
    финансов: расходов не существует как явления, пока блока нет.

    Исполнитель, источник и «чем именно» уезжают вниз БЕЗ ИЗМЕНЕНИЙ — иначе
    автоматика будет выглядеть в журнале как «Иванов завёл операцию руками», а
    разбираться будут именно в разнице между этими двумя.
    """
    finance_service.accrue_order_costs(
        event.db,
        event["order"],
        event["lines"],
        event.actor,
        source=event.source,
        source_ref=event.source_ref,
    )


@events.participant(RETURN_POSTED, module="finance", order=30)
def money_of_return(event: events.Event) -> None:
    """Проведённый возврат отдаёт деньги клиенту минусом по доходной статье.

    Участник, а не наблюдатель: возврат без записанных денег при включённых
    финансах — половина операции, и отказ денег обязан откатить проведение.
    """
    finance_service.refund_for_return(
        event.db,
        event["vozvrat"],
        event["order"],
        event["category_id"],
        event.actor,
        source=event.source,
        source_ref=event.source_ref,
    )


@events.observer(LEAD_RECEIVED, module="tasks")
def lead_into_task(event: events.Event) -> None:
    """Заявка с сайта ставит напоминание ответственному.

    Наблюдатель, а не участник. Заявка уже заведена и уже видна на доске;
    отменить её из-за того, что не записалось напоминание, значит потерять
    обращение клиента ради строки в списке дел. Ошибка при этом не пропадает —
    она уходит в журнал (`events._run_observer`).

    Блок напоминаний выключен — подписчика не зовут, и заявка приходит без
    задачи. Это не половина работы: пока блока нет, напоминаний в системе не
    существует как явления, и место заявки на доске — тот же самый сигнал.

    Свой список дел рядом с чужим не заводим по той же причине, по которой его
    не завела телефония (`telephony_service.create_callback_task`): два места,
    где ищут одно и то же, — верный способ пропустить и там, и там.
    """
    client, deal = event["client"], event["deal"]
    task_service.create(
        event.db,
        {
            "title": f"New website request: {client.name}"[: task_service.MAX_TITLE],
            # Час — как у напоминания перезвонить по пропущенному звонку. Срок
            # правится в самой задаче, и он не про то, «когда можно», а про то,
            # чтобы обращение не пролежало до завтра.
            "due_at": now_utc() + timedelta(hours=1),
            "assignee_id": event.actor.id if event.actor else None,
            "client_id": client.id,
            "deal_id": deal.id,
        },
        event.actor,
    )
