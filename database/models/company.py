from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base


class Company(Base):
    """Своё юрлицо: от чьего имени работаем.

    Аналог «Организаций» в 1С. У малого бизнеса юрлиц обычно больше одного —
    ФОП для розницы и ООО для договоров, — и счёт выставляется от одного, а акт
    подписывается другим. До сих пор реквизиты хранить было негде, поэтому
    бланки печатались без них: с названием из настроек сайта и без единого
    номера, по которому бумага что-то значит.

    Название фирмы и название сайта (`brand_name`) — разные вещи и намеренно
    живут порознь. «Мастерская на Садовой» — то, что видит клиент; «ФОП Иванов
    И. И.» — то, что стоит в счёте. Слить их значило бы либо печатать вывеску в
    реквизитах, либо показывать клиенту налоговый номер вместо вывески.

    Пустые строки, а не NULL, у текстовых полей — так во всех таблицах системы
    (см. `clients`). Для строки «не указано» и «пусто» — одно и то же
    состояние, и второй способ его записать порождал бы только проверки на обе
    формы. Для чисел правило обратное, но чисел здесь нет.
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Короткое имя для списков и выпадающих меню: «ФОП Иванов».
    name: Mapped[str] = mapped_column(String(200), index=True)
    # Полное юридическое название — то, что печатается в бланке. Отдельно от
    # краткого: «Общество с ограниченной ответственностью „Ромашка"» не влезает
    # в строку выбора, а в шапке документа обязано стоять целиком.
    legal_name: Mapped[str] = mapped_column(String(300), default="")

    # Какую фирму подставлять, когда её не выбрали руками. Ровно одна на
    # систему — за этим следит company_service.set_default, а не индекс в базе;
    # почему именно так, объяснено там.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Номера. Держим строками, а не числами: ЄДРПОУ и ИНН бывают с ведущими
    # нулями, а VAT-номер в Европе начинается с букв страны. Целое число молча
    # съело бы ноль, и реквизит перестал бы совпадать с бумагой.
    tax_number: Mapped[str] = mapped_column(String(64), default="")
    reg_number: Mapped[str] = mapped_column(String(64), default="")
    vat_number: Mapped[str] = mapped_column(String(64), default="")

    # Юридический и фактический адреса — разные поля, потому что расходятся
    # чаще, чем совпадают: зарегистрирован по прописке, работает в мастерской.
    legal_address: Mapped[str] = mapped_column(String(500), default="")
    actual_address: Mapped[str] = mapped_column(String(500), default="")

    phone: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    website: Mapped[str] = mapped_column(String(500), default="")

    bank_name: Mapped[str] = mapped_column(String(200), default="")
    # Счёт или IBAN одним полем: это одна и та же строка в платёжке, и разводить
    # её на два поля значило бы заставлять выбирать, в какое писать.
    bank_account: Mapped[str] = mapped_column(String(64), default="")
    # МФО, БИК, SWIFT — у каждой страны своё имя для банковского кода.
    bank_code: Mapped[str] = mapped_column(String(32), default="")

    # Кто подписывает и на основании чего: «Иванов И. И.», «Устава».
    signatory_name: Mapped[str] = mapped_column(String(200), default="")
    signatory_basis: Mapped[str] = mapped_column(String(200), default="")

    # Пути к изображениям, а не сами картинки: файлы лежат в хранилище рядом с
    # остальными медиа, а в базе — ссылка, как у аватара пользователя.
    signature_path: Mapped[str] = mapped_column(String(255), default="")
    stamp_path: Mapped[str] = mapped_column(String(255), default="")

    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    # Мягкое удаление: на фирму ссылаются заявки, а её реквизиты уже разосланы
    # на бумаге. Стереть строку значит оставить заявки без ответа на вопрос
    # «от кого это было».
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
