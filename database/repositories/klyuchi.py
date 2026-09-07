"""Запросы блока «Ключи»: хранилище вторых факторов.

**Кто что видит — решается ЗДЕСЬ, условием запроса, а не отбором в Python.**
Отбирать после выборки значит однажды посчитать «12 кодов» по всем и показать
это число тому, кому видно два, — а счётчик в шапке и есть первое, что читают.
Условие собрано один раз в `_vidno` и подставляется во все выборки.

Правило видимости: свой ключ, ключ, открытый поимённо, или ключ в категории,
куда пустили. Закрытая категория списков не слушает — в неё пускают только
того, кто её завёл (и root, который проходит мимо условия вовсе).
"""

from datetime import datetime

from sqlalchemy import Select, case, exists, func, or_, select
from sqlalchemy.orm import Session, undefer

from database.models import KeyCategory, KeyCategoryAccess, TwoFactorKey, TwoFactorKeyAccess
from database.models.task import VAZHNOSTI
from database.query import contains

#: Важность в число — словами она читается, числом сортируется.
_PO_VAZHNOSTI = case(
    {slovo: nomer for nomer, slovo in enumerate(VAZHNOSTI)},
    value=TwoFactorKey.vazhnost,
    else_=len(VAZHNOSTI),
)


def _vidno(user_id: int):
    """Условие «этот человек видит этот ключ»."""
    poimenno = exists().where(
        TwoFactorKeyAccess.key_id == TwoFactorKey.id,
        TwoFactorKeyAccess.user_id == user_id,
    )
    # Две таблицы в одном `EXISTS`: список доступа и сама категория. Закрытая
    # списков не слушает, и проверять это вторым запросом было бы окном, в
    # которое категорию успевают закрыть.
    cherez_kategoriyu = exists().where(
        KeyCategoryAccess.category_id == TwoFactorKey.category_id,
        KeyCategoryAccess.user_id == user_id,
        KeyCategory.id == KeyCategoryAccess.category_id,
        KeyCategory.zakrytaya.is_(False),
    )
    svoya_zakrytaya = exists().where(
        KeyCategory.id == TwoFactorKey.category_id,
        KeyCategory.zakrytaya.is_(True),
        KeyCategory.created_by == user_id,
    )
    return or_(
        TwoFactorKey.created_by == user_id,
        poimenno,
        cherez_kategoriyu,
        svoya_zakrytaya,
    )


def _osnova(user_id: int | None, *, v_korzine: bool) -> Select:
    zapros = select(TwoFactorKey)
    zapros = zapros.where(
        TwoFactorKey.deleted_at.is_not(None) if v_korzine else TwoFactorKey.deleted_at.is_(None)
    )
    # `None` — root: он видит всё, и лишнее условие только уводит план запроса.
    return zapros if user_id is None else zapros.where(_vidno(user_id))


def spisok(
    db: Session,
    user_id: int | None,
    *,
    category_id: int | None = None,
    tolko_svoi: bool = False,
    poisk: str = "",
    v_korzine: bool = False,
) -> list[TwoFactorKey]:
    """Ключи, которые видит этот человек. Свежий сверху внутри одной важности."""
    zapros = _osnova(user_id, v_korzine=v_korzine)
    if category_id is not None:
        zapros = zapros.where(TwoFactorKey.category_id == category_id)
    if tolko_svoi and user_id is not None:
        zapros = zapros.where(TwoFactorKey.created_by == user_id)
    if poisk:
        zapros = zapros.where(
            or_(contains(TwoFactorKey.title, poisk), contains(TwoFactorKey.account, poisk))
        )
    # Заметка отложена у модели: в списке она нужна, и без `undefer` каждая
    # карточка добирала бы её своим запросом.
    zapros = zapros.options(undefer(TwoFactorKey.note))
    zapros = zapros.order_by(_PO_VAZHNOSTI, TwoFactorKey.title.asc(), TwoFactorKey.id.desc())
    return list(db.scalars(zapros))


def skolko(db: Session, user_id: int | None, *, v_korzine: bool = False) -> int:
    zapros = _osnova(user_id, v_korzine=v_korzine).with_only_columns(func.count(TwoFactorKey.id))
    return int(db.scalar(zapros) or 0)


def po_kategoriyam(db: Session, user_id: int | None) -> dict[int | None, int]:
    """{id категории: сколько в ней видно}. `None` — ключи вне категорий."""
    zapros = (
        _osnova(user_id, v_korzine=False)
        .with_only_columns(TwoFactorKey.category_id, func.count(TwoFactorKey.id))
        .group_by(TwoFactorKey.category_id)
    )
    return {nomer: int(skolko_v_ney) for nomer, skolko_v_ney in db.execute(zapros).all()}


def svoih(db: Session, user_id: int) -> int:
    zapros = (
        _osnova(user_id, v_korzine=False)
        .where(TwoFactorKey.created_by == user_id)
        .with_only_columns(func.count(TwoFactorKey.id))
    )
    return int(db.scalar(zapros) or 0)


def skolko_vidyat(db: Session, key_ids) -> dict[int, int]:
    """{id ключа: сколько человек открыто поимённо}.

    Одним запросом на весь список, а не по ключу на карточку: двенадцать
    карточек — двенадцать запросов, и это видно уже на первом экране.
    """
    nomera = [int(x) for x in key_ids]
    if not nomera:
        return {}
    rows = db.execute(
        select(TwoFactorKeyAccess.key_id, func.count(TwoFactorKeyAccess.id))
        .where(TwoFactorKeyAccess.key_id.in_(nomera))
        .group_by(TwoFactorKeyAccess.key_id)
    ).all()
    return {int(nomer): int(skolko_ih) for nomer, skolko_ih in rows}


def get(db: Session, key_id: int) -> TwoFactorKey | None:
    return db.get(TwoFactorKey, key_id)


def zapert(db: Session, key_id: int) -> TwoFactorKey | None:
    """Ключ под замком до конца транзакции.

    Нужен там, где между «ключ ещё есть» и записью рядом с ним успевает пройти
    чужое удаление: доступ лёг бы на строку, которую уже унёс каскад.
    """
    return db.scalar(select(TwoFactorKey).where(TwoFactorKey.id == key_id).with_for_update())


def vidit_li(db: Session, user_id: int, key_id: int) -> bool:
    zapros = select(TwoFactorKey.id).where(TwoFactorKey.id == key_id, _vidno(user_id))
    return db.scalar(zapros) is not None


def sozdat(db: Session, **polya) -> TwoFactorKey:
    klyuch = TwoFactorKey(**polya)
    db.add(klyuch)
    db.flush()
    return klyuch


def udalit(db: Session, klyuch: TwoFactorKey, kogda: datetime) -> None:
    klyuch.deleted_at = kogda


def vernut(db: Session, klyuch: TwoFactorKey) -> None:
    klyuch.deleted_at = None


def steret(db: Session, klyuch: TwoFactorKey) -> None:
    """Совсем. Доступы уходят каскадом."""
    db.delete(klyuch)


# --- категории ---------------------------------------------------------------


def kategorii(db: Session) -> list[KeyCategory]:
    return list(db.scalars(select(KeyCategory).order_by(KeyCategory.name.asc(), KeyCategory.id.asc())))


def kategoriya(db: Session, category_id: int) -> KeyCategory | None:
    return db.get(KeyCategory, category_id)


def kategoriya_po_imeni(db: Session, name: str) -> KeyCategory | None:
    return db.scalar(
        select(KeyCategory).where(KeyCategory.name == name).order_by(KeyCategory.id.asc()).limit(1)
    )


def zapert_kategoriyu(db: Session, category_id: int) -> KeyCategory | None:
    return db.scalar(select(KeyCategory).where(KeyCategory.id == category_id).with_for_update())


def sozdat_kategoriyu(db: Session, **polya) -> KeyCategory:
    kat = KeyCategory(**polya)
    db.add(kat)
    db.flush()
    return kat


def udalit_kategoriyu(db: Session, kat: KeyCategory) -> None:
    """Ключи не удаляются вместе с ней — им обнуляется категория (SET NULL)."""
    db.delete(kat)


# --- доступ ------------------------------------------------------------------


def dostupy_klyucha(db: Session, key_id: int) -> list[TwoFactorKeyAccess]:
    return list(
        db.scalars(
            select(TwoFactorKeyAccess)
            .where(TwoFactorKeyAccess.key_id == key_id)
            .order_by(TwoFactorKeyAccess.user_id.asc(), TwoFactorKeyAccess.id.asc())
        )
    )


def dostupy_kategorii(db: Session, category_id: int) -> list[KeyCategoryAccess]:
    return list(
        db.scalars(
            select(KeyCategoryAccess)
            .where(KeyCategoryAccess.category_id == category_id)
            .order_by(KeyCategoryAccess.user_id.asc(), KeyCategoryAccess.id.asc())
        )
    )


def otkryt_klyuch(db: Session, key_id: int, user_id: int, granted_by: int | None) -> None:
    if db.scalar(
        select(TwoFactorKeyAccess.id).where(
            TwoFactorKeyAccess.key_id == key_id, TwoFactorKeyAccess.user_id == user_id
        )
    ):
        return
    db.add(TwoFactorKeyAccess(key_id=key_id, user_id=user_id, granted_by=granted_by))
    db.flush()


def zakryt_klyuch(db: Session, key_id: int, user_id: int) -> None:
    zapis = db.scalar(
        select(TwoFactorKeyAccess).where(
            TwoFactorKeyAccess.key_id == key_id, TwoFactorKeyAccess.user_id == user_id
        )
    )
    if zapis is not None:
        db.delete(zapis)


def otkryt_kategoriyu(db: Session, category_id: int, user_id: int, granted_by: int | None) -> None:
    if db.scalar(
        select(KeyCategoryAccess.id).where(
            KeyCategoryAccess.category_id == category_id, KeyCategoryAccess.user_id == user_id
        )
    ):
        return
    db.add(KeyCategoryAccess(category_id=category_id, user_id=user_id, granted_by=granted_by))
    db.flush()


def zakryt_kategoriyu(db: Session, category_id: int, user_id: int) -> None:
    zapis = db.scalar(
        select(KeyCategoryAccess).where(
            KeyCategoryAccess.category_id == category_id, KeyCategoryAccess.user_id == user_id
        )
    )
    if zapis is not None:
        db.delete(zapis)
