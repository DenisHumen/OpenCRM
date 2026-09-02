from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    inspect,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, object_session
from sqlalchemy.orm import util as orm_util
from sqlalchemy.sql import func

from core import exceptions as errors
from database.session import Base

# Виды бланков.
#
# **Заказ — это вид бланка, а не отдельная сущность.** Своя таблица `orders`
# означала бы заново написать номер, печать, три языка, QR и поиск сканом — и
# заново словить гонку выдачи номера: живой прогон дал 13 ответов 500 из 20.
KIND_INTAKE = "intake"
#: Заказ покупателя: клиент просит товар, мы откладываем и отгружаем.
KIND_SALES_ORDER = "sales_order"
#: Заказ поставщику: мы просим товар, потом принимаем на склад.
KIND_PURCHASE_ORDER = "purchase_order"
#: Акт выполненных работ: работа закончена, бумага её закрывает.
#:
#: Вид бланка по доводу выше, плюс свой: акт и квитанция — две половины одной
#: работы, и разные номера связывали бы их только памятью приёмщика. Проведение
#: объявлено ОДНИМ действием: списание, факт и сдвиг заявки порознь расходятся.
KIND_ACT = "act"
#: Расходная накладная: товар физически уезжает со склада.
#:
#: Вид бланка по доводу выше. Заказ обещает, накладная отдаёт, но перечень строк
#: у них один: две таблицы — два места счёта денег, расходятся постепенно
#: (округление, скидка, налог). Резерв против списания даёт накладной черновик.
KIND_WAYBILL_OUT = "waybill_out"
#: Приходная накладная: товар принят на склад.
KIND_WAYBILL_IN = "waybill_in"
DOCUMENT_KINDS = (
    KIND_INTAKE,
    KIND_SALES_ORDER,
    KIND_PURCHASE_ORDER,
    KIND_ACT,
    KIND_WAYBILL_OUT,
    KIND_WAYBILL_IN,
)

#: Виды заказа: покупателю и поставщику.
ORDER_KINDS = (KIND_SALES_ORDER, KIND_PURCHASE_ORDER)

#: Виды накладной: расходная и приходная.
WAYBILL_KINDS = (KIND_WAYBILL_OUT, KIND_WAYBILL_IN)

#: Виды, которые двигают склад САМИ, своим путём проведения.
#:
#: Список нужен проверке `document_service.tolko_blank`: общая ручка статуса
#: склада не касается, и пропущенная сюда бумага закроется, а товар останется на
#: полке. Заказы попали сюда после случая: ручка закрывала их мимо склада.
SKLADSKIE_KINDS = ORDER_KINDS + WAYBILL_KINDS

#: Виды, у которых есть перечень позиций (`document_lines`).
#:
#: Акт делит таблицу строк с заказами — разбор в докстроке `DocumentLine`.
LINE_KINDS = ORDER_KINDS + (KIND_ACT,) + WAYBILL_KINDS

# Состояния документа. Отдельно от этапа сделки намеренно: сделка живёт в
# настраиваемой воронке, документ — бумага с коротким общим циклом. Смешай их —
# и скан квитанции начнёт двигать сделку по чужой воронке.
STATUS_ISSUED = "issued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_READY = "ready"
STATUS_CLOSED = "closed"
STATUS_CANCELLED = "cancelled"
#: Черновик: бумага набирается и ещё ничего не сделала.
#:
#: **Есть только у накладной**: квитанцию печатают за один заход, а накладную
#: собирают сканером по позиции, и «заведи и сразу проведи» значит либо провести
#: неполную, либо держать перечень в голове. Править её можно только здесь.
STATUS_DRAFT = "draft"
DOCUMENT_STATUSES = (
    STATUS_DRAFT,
    STATUS_ISSUED,
    STATUS_IN_PROGRESS,
    STATUS_READY,
    STATUS_CLOSED,
    STATUS_CANCELLED,
)

#: Статусы квитанции. Разведены с `DOCUMENT_STATUSES` («все, какие бывают»)
#: появлением черновика: квитанцию печатают за один заход, черновиком она не
#: бывает.
INTAKE_STATUSES = (
    STATUS_ISSUED,
    STATUS_IN_PROGRESS,
    STATUS_READY,
    STATUS_CLOSED,
    STATUS_CANCELLED,
)

DOCUMENT_LOCALES = ("ru", "en", "uk")

#: Какие состояния осмысленны у какого вида.
#:
#: `closed` двигает склад только у заказа, у накладной он делит «отгружено» и
#: «принято» ради спора о приёмке. `in_progress`/`ready` у акта и накладной
#: разошлись бы с этапом заявки, заказу между «принят» и «собран» нечего показать.
KIND_STATUSES: dict[str, tuple[str, ...]] = {
    KIND_INTAKE: INTAKE_STATUSES,
    KIND_SALES_ORDER: (STATUS_ISSUED, STATUS_READY, STATUS_CLOSED, STATUS_CANCELLED),
    KIND_PURCHASE_ORDER: (STATUS_ISSUED, STATUS_READY, STATUS_CLOSED, STATUS_CANCELLED),
    KIND_ACT: (STATUS_ISSUED, STATUS_CLOSED, STATUS_CANCELLED),
    KIND_WAYBILL_OUT: (STATUS_DRAFT, STATUS_ISSUED, STATUS_CLOSED, STATUS_CANCELLED),
    KIND_WAYBILL_IN: (STATUS_DRAFT, STATUS_ISSUED, STATUS_CLOSED, STATUS_CANCELLED),
}


def statuses_for(kind: str) -> tuple[str, ...]:
    """Какие статусы осмысленны у этого вида.

    До черновика словарь не читал никто, проверка сверялась со всеми статусами
    разом — и квитанцию можно было отправить в `draft`. Описание, которое ничего
    не ограничивает, тем и опасно: выглядит как правило.
    """
    return KIND_STATUSES.get(kind, DOCUMENT_STATUSES)

#: Статусы, в которых заказ ещё держит обещание: покупателю — отложенный товар,
#: нам — ожидаемую поставку. Резерв считается ровно по ним.
OPEN_ORDER_STATUSES = (STATUS_ISSUED, STATUS_READY)


class Document(Base):
    """Бланк, выданный на руки: половина клиенту, половина остаётся в мастерской.

    Хранится не картинкой, а данными: так бланк перепечатывается на другом
    языке, находится сканом и проводится по состояниям, ничего не теряя.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Номер человеку и штрихкоду: «2026-000123». Он же попадает в Code128 и в
    # адрес внутри QR — второго идентификатора заводить незачем.
    number: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default=KIND_INTAKE)
    locale: Mapped[str] = mapped_column(String(2), default="ru")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ISSUED, index=True)

    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deal_id: Mapped[int | None] = mapped_column(
        ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Куда акт переводит заявку при проведении. Пусто у остальных видов.
    #
    # Хранится, а не параметром: записанное намерение делает расхождение
    # «списалось, а этап не сменился» обнаружимым. Ключа нет — этап архивируют,
    # акт читаем. Ширина как у `pipeline_stages.key` (сторож — STAGE_KEY_COLUMNS).
    next_stage: Mapped[str] = mapped_column(String(32), default="", server_default="")

    # На основании чего выписана бумага; пусто — сама по себе.
    #
    # Одна колонка, а не `basis_id` плюс `corrects_id`: две разрешали бы
    # «исправляет одну, а основана на другой», чего не бывает. SET NULL —
    # удаление основания не уносит проведённую накладную с уехавшим товаром.
    basis_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Склад, с которого отпущено или на который принято.
    #
    # Хранится, а не выводится из `stock_moves`: на черновике и при выключенном
    # блоке движений нет вовсе, а намерение делает «списалось не с того склада»
    # обнаружимым. Не производное: склад — решение человека ДО движений.
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Снимок данных на момент выдачи, JSON. Именно снимок, а не ссылки: у
    # человека на руках бумага, и спор «что вы мне выдали» решать нечем, если
    # клиента переименовали, телефон исправили, а сделку удалили.
    payload: Mapped[str] = mapped_column(Text, default="{}")

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Списки бумаг отбирают по виду и СЧИТАЮТ категории на каждый заход, а
    # счёт на пятидесятой строке не останавливается: он читал таблицу целиком.
    # Замер и отвергнутые формы — в миграции `a3f81c62d947`.
    __table_args__ = (
        Index("ix_documents_kind_status_created", "kind", "status", "created_at"),
    )


class DocumentEvent(Base):
    """Что с бланком делали: выдали, приняли в работу, отдали.

    Спор о сроках («когда вы сказали, что готово») разрешается записью с
    временем, а не текущим состоянием.
    """

    __tablename__ = "document_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str] = mapped_column(String(16), default="")
    to_status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(200), default="")
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Имя автора снимком, на момент перехода.
    #:
    #: `author_id` объявлен SET NULL правильно, но вместе с ссылкой пропадает и
    #: имя: после увольнения кладовщика история задним числом отвечала бы
    #: «неизвестно кто» по всем прошлым записям. Как `audit_events.actor_name`.
    author_name: Mapped[str] = mapped_column(String(120), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class DocumentLine(Base):
    """Строка перечня: что, сколько и по какой цене.

    Таблица общая для заказа и акта: два места счёта денег разъедутся. Название и
    цена — снимком, иначе вчерашний заказ поедет за справочником; «списывать ли»
    решает карточка товара (`products.is_service`), а не колонка здесь.
    """

    __tablename__ = "document_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    # Пусто — разовая позиция без карточки товара («доставка», «упаковка»).
    # Заводить ради них справочник значит замусорить его одноразовыми строками.
    # Снимок имени ссылку не отменяет: по ней считаются резерв и списание.
    #
    # RESTRICT, а не SET NULL: обнулённая ссылка молча превращает товарную
    # строку ПРОВЕДЁННОЙ бумаги в разовую позицию, и мимо сторожа неизменяемости
    # — он стоит событиями ORM, а `ON DELETE` исполняет сама база.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    name_snapshot: Mapped[str] = mapped_column(String(200))
    #: Количество в тысячных, как везде на складе.
    quantity_milli: Mapped[int] = mapped_column(Integer)
    #: Цена за единицу в минорных единицах, на момент добавления позиции.
    price_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Себестоимость за единицу — снимок на момент проведения, как у движения.
    cost_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Сколько уже собрано сканером. Отдельно от количества: «заказано пять,
    #: собрано четыре» видно построчно ДО отгрузки, а не после пересчёта коробки.
    picked_milli: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


# --- накладная после проведения не меняется -----------------------------------
#
# Событиями мэппера, а не проверкой в службе (образец — `models/audit.py`):
# службу обходят не злым умыслом, а `db.delete(line)` из соседнего модуля. Прямой
# SQL мимо ORM не закрыт: триггер закрыл бы, но он невидим для `schema_check`.

_NEIZMENYAEMA = "A posted waybill cannot be changed"
_KOD = "waybill_immutable"


def _bumaga_stroki(connection, target) -> tuple[str | None, str | None]:
    """Вид и статус бумаги, которой принадлежит строка.

    Сначала из карты объектов сессии: сторож стоит на ВСЕХ строках, и запрос на
    каждую превратил бы сохранение заказа в полсотни лишних (`tests/test_speed`).
    """
    sessiya = object_session(target)
    if sessiya is not None:
        bumaga = sessiya.identity_map.get(
            orm_util.identity_key(Document, target.document_id)
        )
        if bumaga is not None:
            return bumaga.kind, bumaga.status
    stroka = connection.execute(
        select(Document.kind, Document.status).where(Document.id == target.document_id)
    ).first()
    return (stroka[0], stroka[1]) if stroka else (None, None)


def _zakryta(connection, target) -> bool:
    """Строка принадлежит накладной, которую уже нельзя трогать.

    Не «проведённой», а «не черновику»: отменённый черновик менять незачем, а
    лишний законный случай в стороже — дырка, про которую забудут.
    """
    kind, status = _bumaga_stroki(connection, target)
    return kind in WAYBILL_KINDS and status != STATUS_DRAFT


def _izmenyonnye(target) -> set[str]:
    """Какие поля объекта поменялись до отправки в базу.

    `history` намеренно не поднимает незагруженные поля: сторож не должен
    дочитывать объект ради того, чтобы разрешить или запретить.
    """
    sostoyanie = inspect(target)
    return {
        svoystvo.key
        for svoystvo in sostoyanie.attrs
        if svoystvo.history.has_changes()
    }


@event.listens_for(DocumentLine, "before_insert", propagate=True)
def _v_provedyonnuyu_ne_dopisyvayut(mapper, connection, target) -> None:
    if _zakryta(connection, target):
        raise errors.ForbiddenError(_NEIZMENYAEMA, code=_KOD)


@event.listens_for(DocumentLine, "before_delete", propagate=True)
def _iz_provedyonnoy_ne_udalyayut(mapper, connection, target) -> None:
    if _zakryta(connection, target):
        raise errors.ForbiddenError(_NEIZMENYAEMA, code=_KOD)


@event.listens_for(DocumentLine, "before_update", propagate=True)
def _stroki_provedyonnoy_ne_pravyat(mapper, connection, target) -> None:
    """Правка строки проведённой — отказ. Кроме одного поля и одного перехода.

    `cost_minor` проставляется уже ПОСЛЕ захвата статуса (иначе двойное нажатие
    проведёт дважды), и запрет в лоб отказал бы в самом проведении. Разрешён
    только переход `NULL → число`: переписывать снятое и есть то, что защищаем.
    """
    if not _zakryta(connection, target):
        return

    izmeneno = _izmenyonnye(target)
    if not izmeneno:
        # Объект попал в грязные, но ничего не поменялось. Без этой ветки
        # сторож ниже полез бы за прежним значением и запретил бы пустоту.
        return
    if izmeneno - {"cost_minor"}:
        raise errors.ForbiddenError(_NEIZMENYAEMA, code=_KOD)

    istoriya = inspect(target).attrs.cost_minor.history
    if istoriya.deleted:
        bylo = istoriya.deleted[0]
    else:
        # Поля не было в памяти. Редкий путь: строки при проведении читаются
        # целиком, но «редкий» и «невозможный» — разное.
        bylo = connection.execute(
            select(DocumentLine.cost_minor).where(DocumentLine.id == target.id)
        ).scalar()
    if bylo is not None:
        raise errors.ForbiddenError(_NEIZMENYAEMA, code=_KOD)


@event.listens_for(Document, "before_update", propagate=True)
def _shapka_provedyonnoy_ne_pravitsya(mapper, connection, target) -> None:
    """Шапку проведённой накладной не правят вовсе.

    Статуса это не запрещает: законные переходы идут `documents_repo.take_status`
    — условным `UPDATE` мимо мэппера, ради гонки «двое нажали провести разом».
    `updated_at` исключён: он меняется сам, а не потому, что его меняли.
    """
    if target.kind not in WAYBILL_KINDS or target.status == STATUS_DRAFT:
        return
    if _izmenyonnye(target) - {"updated_at"}:
        raise errors.ForbiddenError(_NEIZMENYAEMA, code=_KOD)


@event.listens_for(Session, "do_orm_execute")
def _nikakih_massovyh_pravok_strok(state) -> None:
    """Массовая правка строк закрыта целиком, а не только у накладных.

    Три сторожа выше зовёт flush по объектам, а `update(DocumentLine)` уезжает в
    базу мимо мэппера и мимо всех трёх. Вид бумаги не разбирается: у массового
    запроса объекта нет, а разбирать `WHERE` значило бы гадать.

    Почему сплошной — docs/03-database.md, «Массовые правки `document_lines`».
    """
    if not (state.is_update or state.is_delete):
        return
    tablica = getattr(state.statement, "table", None)
    if getattr(tablica, "name", None) == DocumentLine.__tablename__:
        raise errors.ForbiddenError(
            "document_lines cannot be changed in bulk", code=_KOD
        )
