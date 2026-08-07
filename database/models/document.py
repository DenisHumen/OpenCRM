from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

# Виды бланков.
#
# **Заказ — это вид бланка, а не отдельная сущность.** Соблазн завести таблицу
# `orders` со своими номерами, статусами и печатью велик и означает написать
# второй раз всё, что здесь уже выстрадано: сквозной номер в году вместе с
# починенной гонкой его выдачи (двое у стойки), смену статуса условием «пока он
# тот, что прочитали», снимок для печати, три языка, QR со штрихкодом, поиск
# сканом, публичную страницу и связь с заявкой. Из этого списка гонку номеров
# ловили живым прогоном, где 13 из 20 одновременных выдач отвечали 500.
KIND_INTAKE = "intake"
#: Заказ покупателя: клиент просит товар, мы откладываем и отгружаем.
KIND_SALES_ORDER = "sales_order"
#: Заказ поставщику: мы просим товар, потом принимаем на склад.
KIND_PURCHASE_ORDER = "purchase_order"
#: Акт выполненных работ: работа закончена, бумага её закрывает.
#:
#: Виду бланка, а не своей сущности, по той же причине, что и заказу (разбор
#: выше). Но у акта есть и собственный довод: он ровно та бумага, которую
#: клиенту показывают в паре с квитанцией — «приняли по этой, отдаём по этой».
#: Разные номера у двух половин одной работы означали бы, что связать их можно
#: только памятью приёмщика.
#:
#: Отличие акта от заказа — не в содержании, а в том, что он делает при
#: проведении. Заказ двигает склад и на этом заканчивается; акт закрывает
#: работу целиком: списывает израсходованное, фиксирует сделанное и переводит
#: заявку дальше по воронке. Три действия объявлены **одним**, потому что
#: раздельно они расходятся: списание прошло, этап не сменился — и восстановить,
#: что имелось в виду, потом уже неоткуда.
KIND_ACT = "act"
DOCUMENT_KINDS = (KIND_INTAKE, KIND_SALES_ORDER, KIND_PURCHASE_ORDER, KIND_ACT)

#: Виды заказа: покупателю и поставщику.
ORDER_KINDS = (KIND_SALES_ORDER, KIND_PURCHASE_ORDER)

#: Виды, у которых есть перечень позиций (`document_lines`).
#:
#: Акт стоит здесь наравне с заказами и пользуется той же таблицей строк —
#: почему именно так, разобрано в докстроке `DocumentLine`.
LINE_KINDS = ORDER_KINDS + (KIND_ACT,)

# Состояния документа. Отдельно от этапа сделки намеренно: сделка живёт в
# воронке, которую каждый бизнес настраивает под себя, а документ — бумага с
# коротким и общим для всех жизненным циклом. Смешай их — и скан квитанции
# начнёт двигать сделку по чужой воронке.
STATUS_ISSUED = "issued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_READY = "ready"
STATUS_CLOSED = "closed"
STATUS_CANCELLED = "cancelled"
DOCUMENT_STATUSES = (
    STATUS_ISSUED,
    STATUS_IN_PROGRESS,
    STATUS_READY,
    STATUS_CLOSED,
    STATUS_CANCELLED,
)

DOCUMENT_LOCALES = ("ru", "en", "uk")

#: Какие состояния осмысленны у какого вида.
#:
#: Новых состояний заказу не понадобилось, и это не экономия, а признак того,
#: что бланк и заказ — действительно одно и то же с разным содержанием:
#:
#: - `issued` у заказа покупателя означает «принят, товар отложен» (то есть
#:   зарезервирован), у заказа поставщику — «отправлен поставщику»;
#: - `ready` — «собран» и «приехало, ждёт приёмки»;
#: - `closed` — «отгружен» и «принят на склад». Именно на переходе в него
#:   случается движение склада, и только тогда;
#: - `cancelled` — резерв снялся, склад не тронут.
#:
#: `in_progress` заказу не нужен: между «принят» и «собран» у него нет
#: состояния, которое кто-то захотел бы увидеть отдельно.
#:
#: У акта состояний три, и это не урезание набора, а его существо. `issued` —
#: «набирается»: работа идёт, позиции дописывают по ходу. `closed` — «проведён»:
#: тот единственный переход, на котором случается всё сразу. `cancelled` —
#: передумали до проведения, ничего не случилось вовсе.
#:
#: `in_progress` и `ready` у акта отсутствуют намеренно. Акт не проходит путь —
#: путь проходит РАБОТА, и её состояние живёт в этапе заявки. Заведи мы у акта
#: «готов», и у одной работы стало бы два состояния сразу: этап заявки и статус
#: бумаги. Расходиться они начали бы на первой же неудачной попытке провести.
KIND_STATUSES: dict[str, tuple[str, ...]] = {
    KIND_INTAKE: DOCUMENT_STATUSES,
    KIND_SALES_ORDER: (STATUS_ISSUED, STATUS_READY, STATUS_CLOSED, STATUS_CANCELLED),
    KIND_PURCHASE_ORDER: (STATUS_ISSUED, STATUS_READY, STATUS_CLOSED, STATUS_CANCELLED),
    KIND_ACT: (STATUS_ISSUED, STATUS_CLOSED, STATUS_CANCELLED),
}

#: Статусы, в которых заказ ещё держит обещание: покупателю — отложенный товар,
#: нам — ожидаемую поставку. Резерв считается ровно по ним.
OPEN_ORDER_STATUSES = (STATUS_ISSUED, STATUS_READY)


class Document(Base):
    """Бланк, выданный на руки: половина клиенту, половина остаётся в мастерской.

    Хранится не картинкой, а данными: снимком того, что было напечатано, плюс
    номером и состоянием. Так бланк можно перепечатать на другом языке, найти
    сканом и провести по состояниям, ничего не теряя.
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

    # Куда акт переводит заявку при проведении. Пусто у всех остальных видов —
    # квитанция и заказ воронку не двигают.
    #
    # **Хранится, а не передаётся одним параметром запроса.** Довод не про
    # удобство, а про то, что потом можно проверить. Проведение акта — одно
    # действие из трёх частей, и главный страх здесь: «списалось, а этап не
    # сменился». Записанное намерение делает такое расхождение ОБНАРУЖИМЫМ:
    # акт закрыт → заявка обязана стоять в `next_stage`, и несовпадение видно
    # запросом. Параметр, живший только в теле HTTP-запроса, не оставил бы
    # следа вовсе — сверять было бы не с чем.
    #
    # Второй довод бытовой: акт набирают по ходу работы, а решение «подпишем —
    # значит выдано» принимают тогда же. Пусть оно стоит на бумаге, а не
    # вспоминается в момент нажатия кнопки.
    #
    # Ширина — как у `pipeline_stages.key`: одна величина, одна ширина. Внешнего
    # ключа нет и здесь (этап архивируют, а акт обязан остаться читаемым) —
    # ровно как у `deals.stage`.
    next_stage: Mapped[str] = mapped_column(String(32), default="", server_default="")

    # Снимок данных на момент выдачи, JSON. Именно снимок, а не ссылки на
    # клиента и сделку: у человека на руках бумага, и она обязана совпадать с
    # тем, что в базе, даже если клиента потом переименовали, телефон исправили,
    # а сделку удалили. Иначе спор «что вы мне выдали» решать нечем.
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


class DocumentEvent(Base):
    """Что с бланком делали: выдали, приняли в работу, отдали.

    Нужен не для красоты: спор о сроках («когда вы сказали, что готово»)
    разрешается только записью с временем, а не текущим состоянием.
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class DocumentLine(Base):
    """Строка перечня: что, сколько и по какой цене.

    **Таблица общая для заказа и акта выполненных работ.** Две параллельные
    системы строк — это два места, где считаются деньги, и они разъедутся:
    сначала в округлении, потом в скидках, потом в налоге. Кто из двух задач
    пришёл первым, тот таблицу и завёл; вторая пользуется этой. Заказ пришёл
    первым, акт пользуется — вместе со счётом суммы (`document_service.total_minor`).

    **Работа и материал различаются не колонкой, а карточкой товара.** В акте
    строка с услугой (`products.is_service`) и строка без товара вовсе —
    выполненная работа, строка с обычным товаром — израсходованный материал, и
    только он списывается со склада. Заводить здесь признак «списывать ли»
    значило бы держать второй ответ на вопрос, у которого уже есть первый, — и
    однажды получить строку, которая услуга и списывается одновременно.

    **Название хранится снимком.** Товар переименуют, а в заказе клиента должно
    остаться то, что он заказывал, — ровно та же причина, по которой у бланка
    снимок для печати. `product_id` при этом остаётся: по нему считаются резерв
    и списание, и терять связь со справочником нельзя.

    **Цена фиксируется при добавлении позиции.** Прайс поменяли — старые заказы
    не трогаются, иначе сумма, названная клиенту вчера, сегодня станет другой.
    """

    __tablename__ = "document_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    # Пусто — разовая позиция без карточки товара («доставка», «упаковка»).
    # Такие бывают в каждом втором заказе, и заводить ради них справочник
    # значит замусорить его одноразовыми строками.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name_snapshot: Mapped[str] = mapped_column(String(200))
    #: Количество в тысячных, как везде на складе.
    quantity_milli: Mapped[int] = mapped_column(Integer)
    #: Цена за единицу в минорных единицах, на момент добавления позиции.
    price_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Себестоимость за единицу — снимок на момент проведения, как у движения.
    cost_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Сколько уже собрано сканером. Отдельно от количества: расхождение
    #: «заказано пять, собрано четыре» видно построчно ДО отгрузки, а не после
    #: пересчёта коробки у выдачи.
    picked_milli: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
