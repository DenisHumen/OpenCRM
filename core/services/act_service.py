"""Акт выполненных работ: работа закрывается одним действием.

Акт — **вид бланка** (разбор — в докстроке `database/models/document.py`),
поэтому номер, состояния, история переходов, снимок для печати, поиск сканом и
связь с заявкой берутся готовыми. Здесь живёт только то, чего у квитанции нет:
перечень сделанного, израсходованное и проведение.

Три решения, на которых всё держится.

**Проведение — одно действие, а не три подряд.** Списать материалы, зафиксировать
работы и перевести заявку дальше по воронке — это одно решение человека
(«работа закончена»), и разбивать его на три нажатия значит гарантированно
получить состояние, в котором сделаны только два. До акта так и было: склад
списывали на экране склада, этап двигали на доске, и связывала их только память
мастера.

**Либо всё, либо ничего.** Всё перечисленное едет в ОДНОЙ транзакции, открытой
на запрос (`web/api/deps.get_db`), и подписчики работают в ней же, синхронно
(`core/events`). Участник, отказавшийся или упавший, роняет операцию целиком:
исключение из `emit` не ловится, доходит до роута, `get_db` откатывает — и не
остаётся ни движений склада, ни закрытого акта, ни переведённой заявки. Разбор
каждого «а если посередине» — в докстроке `complete`.

**Кто что делает — решают события, а не прямые вызовы.** Акт трогает три блока
сразу, и прямой вызов склада отсюда означал бы, что раздел бланков не работает
без склада. Поэтому акт только объявляет `ACT_COMPLETED`, а списывает склад и
двигает воронку — подписчики, каждый под своим блоком (`core/subscriptions.py`).
Выключенный склад просто не подписан: акт при этом проводится и работы
фиксирует, а списывать ему нечем — и это правда о системе без склада, а не дыра.
"""

import json

from sqlalchemy.orm import Session

# Под псевдонимом по той же причине, что и в `document_service`: ниже есть своя
# `events()` — история акта для его экрана.
from core import events as event_bus
from core import exceptions as errors
from core.services import audit_service, document_service, notification_service, pipeline_service, settings_service
from core.services import warehouse_service
from database.models import Document, DocumentEvent, DocumentLine, User
from database.models.audit import SOURCE_MANUAL
from database.models.document import (
    KIND_ACT,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_ISSUED,
)
from database.repositories import deal_lines as lines_repo
from database.repositories import deals as deals_repo
from database.repositories import documents as documents_repo

MAX_LINE_NAME = 200

#: Акт проведён: работы приняты, дальше идут те, кого это касается.
#:
#: Подробности: `act`, `lines`, `deal`, `to_stage`, `warehouse_id`,
#: `confirm_negative`.
#:
#: Событие поднимается ПОСЛЕ того, как акт занял состояние «проведён» условной
#: сменой статуса, и ДО того, как операция признана состоявшейся. Порядок важен
#: обеими половинами: до смены статуса подписчиков позвал бы и проигравший гонку
#: («двое нажали разом»), то есть склад списался бы дважды; после завершения
#: операции отказ участника уже ничего не отменил бы.
#:
#: Подписчики здесь — **участники**, а не наблюдатели, и это главное про акт.
#: Наблюдатель проглатывает своё падение: акт закрылся бы, а материалы остались
#: бы на остатке или заявка — в прежнем этапе. Молча. Разбирать такое потом
#: пришлось бы по журналу вручную, а до разбора система показывала бы неправду.
ACT_COMPLETED = "act.completed"

#: Заголовок акта, когда своего не вписали. По-английски, как все умолчания.
#:
#: По ней же печать и узнаёт, что названия не выбирали, и ставит на лист
#: заголовок по языку бумаги (`web/api/routes/documents._act_title`). Поправишь
#: её одну — у выданных актов повторная печать разойдётся с листом у клиента.
DEFAULT_TITLE = "Certificate of completed works"


# --- акт ----------------------------------------------------------------------


def create(db: Session, data: dict, author: User) -> Document:
    """Завести акт. Позиции добавляются отдельно — сразу их обычно нет.

    **Заявка обязательна, и это единственное, чем акт строже квитанции.** Акт
    закрывает работу и переводит заявку дальше; акт без заявки не может сделать
    ни того, ни другого — останется бумага с перечнем, то есть заказ, у которого
    для этого есть свой вид. Отказ ясный, а не «этап не найден» при проведении:
    выяснять это в момент, когда работа уже сделана, поздно.
    """
    deal_id = data.get("deal_id")
    if not deal_id:
        raise errors.ValidationError(
            "An act needs a deal — it is what closes the work", code="deal_required"
        )

    # Этап проверяем ДО создания. Откат транзакции отменил бы бумагу и так, но
    # до отката успели бы сходить за номером и поднять событие о выпуске: отказ
    # на опечатке в ключе не должен тратить номер из сквозной нумерации года.
    next_stage = _clean_stage(db, data.get("next_stage"))

    fields = dict(data)
    fields["kind"] = KIND_ACT
    # Квитанция требует описания вещи; у акта его роль играет заголовок бумаги,
    # а что именно сделано — перечислено строками.
    fields["item"] = (data.get("title") or "").strip() or DEFAULT_TITLE

    act = document_service.create(db, fields, author)
    act.next_stage = next_stage
    db.flush()
    return act


def get(db: Session, act_id: int) -> Document:
    """Акт по номеру записи. Квитанция и заказ сюда не проходят: проводить их
    нечем, а перечень работ у них означает другое."""
    act = document_service.get(db, act_id)
    if act.kind != KIND_ACT:
        raise errors.ValidationError("This document is not an act", code="not_an_act")
    return act


def lines(db: Session, act_id: int) -> list[DocumentLine]:
    return documents_repo.lines_of(db, act_id)


def events(db: Session, act_id: int) -> list[DocumentEvent]:
    return documents_repo.events(db, act_id)


def add_line(db: Session, act_id: int, data: dict, author: User) -> DocumentLine:
    """Добавить строку: выполненную работу или израсходованный материал.

    Различает их не эта функция, а карточка товара: услуга и строка без товара —
    работа, обычный товар — материал, и только он потом спишется со склада (см.
    `warehouse_service.write_off_materials`). Спрашивать об этом человека значит
    предлагать ему ошибиться там, где ответ уже известен системе.

    Название и цена фиксируются снимком — здесь и сейчас, как у заказа: товар
    переименуют, прайс поменяют, а в подписанном акте обязано остаться то, что
    клиенту назвали.
    """
    act = get(db, act_id)
    _assert_open(act)

    quantity = warehouse_service.parse_quantity(data.get("quantity"))
    if quantity is None or quantity <= 0:
        raise errors.ValidationError(
            "Quantity must be greater than zero", code="bad_line_quantity"
        )

    product = None
    if data.get("product_id") is not None:
        product = warehouse_service.get_product(db, data["product_id"])

    name = (data.get("name") or (product.name if product else "")).strip()
    if not name:
        # Разовая строка без карточки товара законна и в акте она частая:
        # «выезд мастера», «диагностика». Но названа быть обязана — безымянная
        # строка не читается ни на экране, ни на подписываемой бумаге.
        raise errors.ValidationError("Line needs a name", code="line_name_required")

    price = data.get("price")
    if price is None and product is not None:
        price = product.price_minor

    line = DocumentLine(
        document_id=act.id,
        product_id=product.id if product else None,
        name_snapshot=name[:MAX_LINE_NAME],
        quantity_milli=quantity,
        price_minor=price,
        # Себестоимость снимаем при проведении, а не сейчас: до него деталь ещё
        # может приехать по другой закупке, и снимок «на момент набора» соврал
        # бы о том, во сколько работа обошлась на самом деле.
        cost_minor=None,
        sort_order=documents_repo.next_sort_order(db, act.id),
    )
    documents_repo.add_line(db, line)
    return line


def remove_line(db: Session, act_id: int, line_id: int) -> None:
    act = get(db, act_id)
    _assert_open(act)
    line = documents_repo.get_line(db, act.id, line_id)
    if line is None:
        raise errors.NotFoundError("Line not found", code="line_not_found")
    documents_repo.drop_line(db, line)


def cost_minor(rows: list[DocumentLine]) -> int | None:
    """Во сколько обошёлся акт: себестоимость строк, в минорных единицах.

    **Считается, а не хранится** — как остаток склада и как сумма заказа. Третье
    число рядом с ценой и себестоимостью строки разошлось бы с ними при первой
    же правке, и узнать, какое из двух верно, было бы неоткуда.

    None — себестоимость неизвестна ни по одной строке: акт ещё не проведён,
    либо склад выключен и снимать её было неоткуда. Это не то же самое, что
    ноль («работа не стоила нам ничего»), и на экране выглядеть должно иначе.

    Делим на тысячу один раз в конце и целыми — тем же приёмом, что и сумма.
    """
    known = [row for row in rows if row.cost_minor is not None]
    if not known:
        return None
    scaled = sum(row.quantity_milli * row.cost_minor for row in known)
    return (scaled + 500) // 1000


# --- проведение ---------------------------------------------------------------


def complete(
    db: Session,
    act_id: int,
    author: User,
    warehouse_id: int | None = None,
    stage: str | None = None,
    confirm_negative: bool = False,
    source: str = SOURCE_MANUAL,
) -> Document:
    """Провести акт: списать израсходованное, зафиксировать работы, двинуть заявку.

    Все три — **одной транзакцией**, и вот что происходит при отказе на каждом
    шаге. Разбор длинный намеренно: половина проведения — это расхождение,
    которое потом никто не восстановит, и выбор поведения здесь важнее кода.

    *Гонка «двое нажали разом».* Отсекается условной сменой статуса
    (`take_status`), а не проверкой «уже проведён»: проверка гоняется — оба
    прочитали `issued`, оба пошли списывать, — а условный UPDATE пропускает
    ровно одного. Смена статуса стоит ПЕРЕД событием: проигравший уходит с
    отказом, не тронув ни склада, ни воронки. Тот же приём, что у отгрузки
    заказа и у смены этапа.

    *Не хватает на складе.* Отказ до единой записи: списывать нечего физически.
    В отказе названы позиции поимённо, и есть явное подтверждение — «всё равно
    провести». Проверку делает склад, а не акт: это его вопрос и его ответ.

    *Списание упало на середине* (товар успели удалить, движение не записалось).
    Подписчик склада — **участник**: его исключение не ловится, доходит до
    роута, транзакция откатывается целиком. Акт остаётся открытым, заявка — на
    прежнем этапе, движений нет ни одного. Человек видит причину и повторяет.
    Альтернатива — «списалось наполовину, акт закрыт» — это остаток, разошедшийся
    с полкой, и найти, где именно, можно только пересчётом всего склада.

    *Заявку успели передвинуть.* `move_stage` отвечает конфликтом, и откатывается
    ВСЁ, включая уже записанные движения склада. Именно это соотношение и есть
    ответ на «что важнее»: списание без смены этапа выглядит как выполненная
    работа, которую никто не принял, и всплывает не раньше, чем кто-то заметит
    минус на складе. Пусть лучше человек нажмёт второй раз.

    *Упала запись в ленте.* Не отменяет ничего: наблюдатель работает под точкой
    отката, ошибка уходит в журнал. Материалы уже физически ушли с полки, и
    отменять состоявшееся из-за строки в переписке нельзя.

    *Склад выключен у этого бизнеса.* Подписчика склада не зовут вовсе — акт
    фиксирует работы и двигает заявку. Это не половина операции: списания не
    существует как действия, пока блока нет. Заявке без склада (мастерская без
    учёта деталей, студия) акт нужен ровно так же.
    """
    act = get(db, act_id)
    _assert_open(act)

    rows = documents_repo.lines_of(db, act.id)
    if not rows:
        # Провести пустой акт значит закрыть работу, не сказав, в чём она
        # состояла, — и подписать бумагу, на которой ничего не напечатано.
        raise errors.ValidationError("This act has no lines", code="act_is_empty")

    # Заявка есть по построению (`create` без неё не пускает), но за годы у
    # бланка её могли и снять: `documents.deal_id` — SET NULL при удалении
    # заявки. Проводить такой акт нечем — двигать нечего.
    if act.deal_id is None:
        raise errors.ValidationError(
            "The deal of this act is gone — nothing to move", code="deal_required"
        )

    # Куда переводим. Названное руками важнее записанного при заведении: решение
    # принимают в момент подписи, и оно же остаётся на акте — бумага обязана
    # помнить, что именно сделала.
    zayavka = deals_repo.get(db, act.deal_id)
    if zayavka is not None and zayavka.closed_at is not None and not _clean_stage(db, stage):
        # Заявку уже закрыли на доске, а акт заводили руками: он фиксирует
        # работу и материалы, этап не трогает — двигать некуда, и «нет
        # следующего этапа» оставлял бы бумагу открытой навсегда.
        target = None
    else:
        target = _clean_stage(db, stage) or act.next_stage or _next_stage_of(db, act.deal_id)
    # Этап заявки спрашиваем ДО события: после него заявка уже переведена, и
    # «было» в журнале совпало бы со «стало» — то есть запись перестала бы
    # отвечать на единственный вопрос, ради которого её читают.
    was_stage = stage_of(db, act.deal_id)

    previous = act.status
    if not documents_repo.take_status(db, act, expected=previous, status=STATUS_CLOSED):
        raise errors.ConflictError(
            "The act has already been carried out by someone else",
            code="document_status_changed",
        )
    if target is not None:
        act.next_stage = target
    documents_repo.add_event(
        db,
        DocumentEvent(
            document_id=act.id,
            from_status=previous,
            to_status=STATUS_CLOSED,
            author_id=author.id,
        ),
    )

    event_bus.emit(
        ACT_COMPLETED,
        db=db,
        actor=author,
        # Причина уезжает в порождённые записи как есть и объясняет их: строка
        # в ленте про списание должна читаться «потому что провели акт», а не
        # «Иванов списал руками». Разница между этими двумя и есть то, ради чего
        # источник и причина протаскиваются вниз.
        reason=f"act {act.number} carried out",
        source=source,
        # Чем именно вызвано — номером бумаги, а не номером записи: по журналу
        # ищут «двести двадцать третий», а не `document_id=417`.
        source_ref=act.number,
        act=act,
        lines=rows,
        deal_id=act.deal_id,
        to_stage=target,
        warehouse_id=warehouse_id,
        confirm_negative=confirm_negative,
    )

    audit_service.record(
        db,
        action=audit_service.ACTION_ACT_COMPLETED,
        actor=author,
        source=source,
        source_ref=act.number,
        entity_type=audit_service.ENTITY_DOCUMENT,
        entity_id=act.id,
        entity_label=act.number,
        # «Было» и «стало» у акта — не статус бумаги, а этап работы: спрашивают
        # в журнале именно об этом, а статус виден в истории самого акта.
        before=was_stage,
        after=target or was_stage,
    )
    return act


# --- акт по заявке сам --------------------------------------------------------


def nezakrytyy_po_zayavke(db: Session, deal_id: int) -> Document | None:
    """Непроведённый акт заявки, если есть. Один: заводит его зеркало."""
    return documents_repo.nezakrytaya(db, deal_id, KIND_ACT, (STATUS_ISSUED,))


def avto(act: Document) -> bool:
    """Заведён ли акт сам, по заявке, а не руками."""
    return bool(document_service.payload_of(act).get("auto"))


def zerkalo_po_zayavke(db: Session, deal, author: User | None) -> None:
    """Акт работ повторяет строки заявки, пока не проведён.

    Владелец просил (05.09.2026), чтобы бумага появлялась сама: работа набрана
    строками заявки, и акт по ней — это те же строки, а не второй набор руками.
    Выключается настройкой `auto_act`; при выключенном блоке бланков сюда не
    приходят вовсе. Заявка без строк — заведённый сами акт убираем, ручной
    оставляем человеку.
    """
    if settings_service.get_all(db).get("auto_act", "1") != "1":
        return
    if deal.closed_at is not None or deal.deleted_at is not None:
        return
    stroki = lines_repo.list_for_deal(db, deal.id)
    act = nezakrytyy_po_zayavke(db, deal.id)
    if not stroki:
        if act is not None and avto(act):
            documents_repo.drop(db, act)
        return
    if act is None:
        if author is None:
            return
        # У заявки уже был акт — проведённый или отменённый руками: заводить
        # новый после каждой правки строк значит спорить с человеком.
        if documents_repo.est_nezakrytaya(db, deal.id, KIND_ACT, (STATUS_CLOSED, STATUS_CANCELLED)):
            return
        act = create(
            db,
            {"deal_id": deal.id, "client_id": deal.client_id, "company_id": deal.company_id},
            author,
        )
        snimok = document_service.payload_of(act)
        snimok["auto"] = True
        act.payload = json.dumps(snimok, ensure_ascii=False)
        db.flush()
        notification_service.notify(
            db,
            notification_service.adresaty(db, "documents"),
            "auto_act",
            {"number": act.number, "deal": deal.title},
            f"/documents/{act.id}",
        )
    # Строки переписываются целиком — как у накладной по заказу: два перечня
    # согласуются по сути, а не по номерам строк.
    for row in documents_repo.lines_of(db, act.id):
        documents_repo.drop_line(db, row)
    for stroka in stroki:
        documents_repo.add_line(
            db,
            DocumentLine(
                document_id=act.id,
                product_id=stroka.product_id,
                name_snapshot=stroka.name_snapshot,
                quantity_milli=stroka.quantity_milli,
                price_minor=stroka.price_minor,
                cost_minor=None,
                sort_order=stroka.sort_order,
            ),
        )


def zakryt_avto_akt(db: Session, deal_id: int, author: User, note: str) -> None:
    """Заявка закрылась мимо акта — акт, заведённый сам, отменяется с записью.

    Списание уже сделала заявка (docs/19 §Р4); провести акт после неё значило
    бы открыть второй путь к складу. Отмена, а не удаление: заявка жила, и
    номер акта остаётся в её истории.
    """
    act = nezakrytyy_po_zayavke(db, deal_id)
    if act is not None and avto(act):
        document_service.set_status(db, act.id, STATUS_CANCELLED, author, note)


def ubrat_avto_akt(db: Session, deal_id: int) -> None:
    """Заявка убрана — акт, заведённый по ней сам, уходит вместе с ней."""
    act = nezakrytyy_po_zayavke(db, deal_id)
    if act is not None and avto(act):
        documents_repo.drop(db, act)


def cancel(db: Session, act_id: int, author: User, note: str = "") -> Document:
    """Отменить непроведённый акт. Склада и воронки не касается — их и не трогали.

    Проведённый акт этим путём не отменяется, и отдельной отмены проведения у
    акта нет намеренно. У заказа она есть, потому что отменять там нечего, кроме
    склада: обратные движения — и всё. У акта откат означал бы вернуть детали на
    полку И отыграть этап заявки назад, а второе — не техническая операция:
    заявка с тех пор могла уехать дальше, закрыться и попасть в отчёт за месяц.
    Возвращать её оттуда автоматически значит переписывать историю работы. Всё,
    что для этого нужно, человек делает руками и осознанно: возврат на склад —
    приходом с комментарием, этап — на доске.
    """
    act = get(db, act_id)
    _assert_open(act)
    return document_service.set_status(db, act.id, STATUS_CANCELLED, author, note)


# --- мелочи -------------------------------------------------------------------


def _assert_open(act: Document) -> None:
    if act.status != STATUS_ISSUED:
        raise errors.ValidationError("This act is already finished", code="act_finished")


def _clean_stage(db: Session, key: str | None) -> str:
    """Этап из запроса, с проверкой существования. Пусто — пусто.

    Проверяем сразу, а не при проведении: несуществующий ключ, записанный на
    акт, всплыл бы ровно в тот момент, когда работа уже сделана и бумага ждёт
    подписи.
    """
    value = (key or "").strip()
    if not value:
        return ""
    pipeline_service.get_stage(db, value)  # бросит unknown_stage, если этапа нет
    return value


def stage_of(db: Session, deal_id: int) -> str:
    """Где заявка стоит сейчас. Нет заявки — 404 с внятной причиной.

    Через репозиторий, а не через `deal_service`: акту нужно одно поле, а не
    поведение блока заявок, и лишний слой здесь только запутал бы, кто кого
    зовёт при проведении.
    """
    deal = deals_repo.get(db, deal_id)
    if deal is None:
        raise errors.NotFoundError("Deal not found", code="deal_not_found")
    return deal.stage


def _next_stage_of(db: Session, deal_id: int) -> str:
    """Следующий этап воронки за тем, где заявка стоит сейчас.

    Умолчание, а не правило: этап у акта обычно выбирают явно. Но проведение без
    выбора обязано делать что-то осмысленное, иначе «одно действие» на деле
    требует двух — сначала выбрать, потом нажать.

    Берём соседа по порядку, а не «первый выигранный»: воронки у всех разные, и
    у ремонта после работ идёт «готово к выдаче», а вовсе не «выдано» — деньги
    ещё не получены. Решать за бизнес, какой этап у него означает конец, мы не
    можем; сказать «следующий» — можем.

    Заявка в последнем этапе — отказ с внятной причиной: двигать её некуда, и
    молча закрыть акт, ничего не сделав, было бы хуже.
    """
    current = stage_of(db, deal_id)
    keys = [stage.key for stage in pipeline_service.list_stages(db)]
    if current in keys:
        following = keys[keys.index(current) + 1:]
        if following:
            return following[0]
    raise errors.ValidationError(
        "There is no next stage — name the one to move to", code="no_next_stage"
    )
