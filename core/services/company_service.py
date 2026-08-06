"""Свои юрлица: от чьего имени работаем и что печатается в реквизитах."""

# `update` переименован: у сервиса своя функция с этим именем, и без псевдонима
# она молча закрывает собой конструктор запроса.
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import modules_service
from core.utils import now_utc
from database.models import Company, Deal

MAX_NAME = 200
MAX_LEGAL_NAME = 300
MAX_ADDRESS = 500
MAX_NOTE = 2000

# Поля, которые правятся снаружи, и их пределы. Списком, а не набором ifов:
# полей полтора десятка, и каждое новое иначе требовало бы трёх правок в разных
# местах — в create, в update и в проверке длины.
TEXT_FIELDS: dict[str, int] = {
    "legal_name": MAX_LEGAL_NAME,
    "tax_number": 64,
    "reg_number": 64,
    "vat_number": 64,
    "legal_address": MAX_ADDRESS,
    "actual_address": MAX_ADDRESS,
    "phone": 64,
    "email": 255,
    "website": 500,
    "bank_name": 200,
    "bank_account": 64,
    "bank_code": 32,
    "signatory_name": 200,
    "signatory_basis": 200,
    "signature_path": 255,
    "stamp_path": 255,
    "note": MAX_NOTE,
}

# Что уходит в снимок документа. Порядок и состав зафиксированы здесь, а не в
# шаблоне печати: снимок обязан быть одинаковым у всех бланков, иначе через год
# половина документов окажется без банка, потому что в шаблон его добавили
# позже.
SNAPSHOT_FIELDS = (
    "legal_name",
    "tax_number",
    "reg_number",
    "vat_number",
    "legal_address",
    "actual_address",
    "phone",
    "email",
    "website",
    "bank_name",
    "bank_account",
    "bank_code",
    "signatory_name",
    "signatory_basis",
)


def get_company(db: Session, company_id: int, include_deleted: bool = False) -> Company:
    company = db.get(Company, company_id)
    if company is None or (company.deleted_at is not None and not include_deleted):
        raise errors.NotFoundError("Company not found", code="company_not_found")
    return company


def list_companies(db: Session) -> list[Company]:
    """Основная — первой: её выбирают чаще всех остальных вместе взятых."""
    return list(
        db.scalars(
            select(Company)
            .where(Company.deleted_at.is_(None))
            .order_by(Company.is_default.desc(), Company.name.asc(), Company.id.asc())
        )
    )


def default_company(db: Session) -> Company | None:
    """Фирма, от имени которой работаем, когда её не выбрали руками.

    Сортировка по id не украшение: если две записи каким-то образом окажутся
    помеченными основными, ответ обязан остаться одним и тем же при каждом
    вызове. Молча плавающая фирма в реквизитах хуже, чем неверная.
    """
    return db.scalars(
        select(Company)
        .where(Company.deleted_at.is_(None), Company.is_default.is_(True))
        .order_by(Company.id.asc())
    ).first()


def _apply(company: Company, data: dict) -> None:
    if "name" in data and data["name"] is not None:
        name = data["name"].strip()
        if not name:
            raise errors.ValidationError("Name is required", code="name_required")
        company.name = name[:MAX_NAME]
    for field, limit in TEXT_FIELDS.items():
        if field in data and data[field] is not None:
            setattr(company, field, str(data[field]).strip()[:limit])


def create(db: Session, data: dict) -> Company:
    name = (data.get("name") or "").strip()
    if not name:
        raise errors.ValidationError("Name is required", code="name_required")

    company = Company(name=name[:MAX_NAME])
    _apply(company, data)
    db.add(company)
    db.flush()

    # Первая заведённая фирма становится основной сама. Иначе система с одной
    # фирмой осталась бы без основной, и бланки продолжили бы печататься без
    # реквизитов — ровно та беда, ради которой заводили справочник.
    if data.get("is_default") or default_company(db) is None:
        set_default(db, company.id)
    return company


def update(db: Session, company_id: int, data: dict) -> Company:
    company = get_company(db, company_id)
    _apply(company, data)
    db.flush()
    # Снять «по умолчанию» правкой нельзя — только назначив другую. Иначе
    # получилась бы система без основной фирмы, и снимать флаг пришлось бы
    # чинить отдельной кнопкой.
    if data.get("is_default"):
        set_default(db, company.id)
    return company


def set_default(db: Session, company_id: int) -> Company:
    """Назначить фирму основной, сняв признак со всех остальных.

    Единственность держится здесь, а не частичным уникальным индексом
    (``UNIQUE ... WHERE is_default``), по двум причинам. Первая — в проекте
    такие индексы запрещены: система рассчитана на переезд SQLite → MySQL, а в
    MySQL частичных индексов нет вовсе, и схема, которую нельзя перенести,
    сломается ровно в момент переезда. Вторая — обычный UNIQUE на колонку не
    заменяет его ничем: он запретил бы двум фирмам одновременно НЕ быть
    основными, то есть сделал бы невозможным нормальный случай.

    **Оба шага — явные UPDATE, и «своя» ставится первой.** Через присваивание
    полю ORM второй шаг иногда не давал запроса вовсе: если в момент чтения
    фирма уже была основной, присваивание `True` ничего не меняло, и SQLAlchemy
    правильно не писал ничего. Стоило соседнему запросу в это время снять
    признак — и основной не оставалось НИ ОДНОЙ. Поймано живым прогоном: пять
    раундов по три одновременные попытки, на пятом основных стало ноль.

    Ноль хуже двух. Две основные читатель разрешает сам (берёт первую), а без
    основной бланк печатается без шапки — и виновата будет бумага, а не гонка,
    которая случилась неделю назад.
    """
    company = get_company(db, company_id)
    db.execute(sql_update(Company).where(Company.id == company.id).values(is_default=True))
    db.execute(
        sql_update(Company)
        .where(Company.id != company.id, Company.is_default.is_(True))
        .values(is_default=False)
    )
    # Объект в памяти помнит прежнее значение: писали запросом, мимо него.
    db.refresh(company)
    return company


def delete(db: Session, company_id: int) -> None:
    """Мягкое удаление: на фирму ссылаются заявки и уже выданные бумаги.

    Ссылку в заявках не чистим. Заявка помнит, от чьего имени её вели, и после
    удаления фирмы этот ответ остаётся верным — а `company_id` в ней всё равно
    ведёт на живую строку, просто скрытую из справочника.
    """
    company = get_company(db, company_id)
    company.deleted_at = now_utc()
    company.is_default = False
    db.flush()

    # Основную удалили — назначаем следующую. Оставить систему без основной
    # значит вернуть бланки к печати без реквизитов, причём незаметно: ошибки
    # нет, просто в шапке пусто.
    if default_company(db) is None:
        replacement = db.scalars(
            select(Company)
            .where(Company.deleted_at.is_(None))
            .order_by(Company.id.asc())
        ).first()
        if replacement is not None:
            set_default(db, replacement.id)


def requisites(company: Company | None) -> dict:
    """Реквизиты для снимка документа.

    Плоский словарь простых значений, а не ссылка на строку: снимок переживает
    и правку фирмы, и её удаление. Название кладём в `name` — под тем же
    ключом, что раньше лежало название из настроек сайта, чтобы старые бланки
    печатались прежним шаблоном.
    """
    if company is None:
        return {}
    data = {"id": company.id, "name": company.legal_name or company.name}
    data.update({field: getattr(company, field) for field in SNAPSHOT_FIELDS})
    return data


def for_document(db: Session, company_id: int | None, deal: Deal | None) -> Company | None:
    """Чья печать и чей счёт окажутся на этом бланке.

    Порядок: явно выбранная → фирма заявки → основная. Спрашивать фирму на
    каждой квитанции незачем — у большинства она одна, — но и печатать бумагу
    без реквизитов, когда справочник заполнен, нельзя.

    Удалённую фирму берём как есть (`include_deleted`): заявку вели от неё, и
    бланк по этой заявке обязан выйти с теми же реквизитами, а не подменяться
    чужими задним числом.

    Выключенный блок фирм означает, что реквизиты не печатаются вовсе. Это то
    же правило, что и для остальных блоков: выключено — значит не видно нигде,
    включая бумагу, которая уходит из системы на руки. Уже выданные бланки при
    этом не меняются: у них свой снимок.
    """
    if not modules_service.is_enabled(db, "companies"):
        return None
    if company_id:
        return get_company(db, int(company_id), include_deleted=True)
    if deal is not None and deal.company_id:
        return db.get(Company, deal.company_id)
    return default_company(db)
