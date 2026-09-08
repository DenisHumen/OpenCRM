"""Блок «Ключи»: хранилище вторых факторов. Роут тонкий, всё в службе.

Секрет отдаёт ровно одна ручка — `POST /keys/{id}/secret`, только root и с
записью в журнал (`core/services/klyuchi_service.sekret`). Остальные отдают уже
цифры кода: разбор — `docs/bloki/27-klyuchi.md`.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import klyuchi_service, znaki_service
from database.models import User
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/keys",
    tags=["keys"],
    dependencies=[Depends(require_module("keys"))],
)


class RazborIn(BaseModel):
    secret: str


class KlyuchIn(BaseModel):
    secret: str
    title: str = ""
    issuer: str | None = None
    account: str | None = None
    category: str = ""
    note: str = ""
    vazhnost: str = ""
    backup_codes: list[str] | str | None = None


class PravkaIn(BaseModel):
    title: str | None = None
    issuer: str | None = None
    account: str | None = None
    category: str | None = None
    note: str | None = None
    vazhnost: str | None = None
    backup_codes: list[str] | str | None = None
    task_id: int | None = None


class DostupIn(BaseModel):
    user_id: int
    otkryt: bool


class KategoriyaIn(BaseModel):
    name: str
    zakrytaya: bool = False


class NapominanieIn(BaseModel):
    cherez_dney: int = 180


@router.get("")
def spisok(
    category_id: int | None = Query(default=None),
    mine: bool = Query(default=False),
    q: str = Query(default=""),
    trash: bool = Query(default=False),
    alarm: bool = Query(default=False),
    user: User = Depends(require_perm("keys", "view")),
    db: Session = Depends(get_db),
):
    """Ключи, категории и счётчики одним ответом — как рисует экран.

    `alarm=1` — только те, что просят обновления. Отбор делает сервер: число в
    шапке считается по всему разделу, и вычитание из показанной полки давало бы
    под ненулевым числом пустой список.
    """
    return klyuchi_service.spisok(
        db,
        user,
        category_id=category_id,
        tolko_svoi=mine,
        poisk=q,
        v_korzine=trash,
        tolko_prosyat=alarm,
    )


@router.post("/parse")
def razobrat(
    payload: RazborIn,
    _: User = Depends(require_perm("keys", "create")),
    db: Session = Depends(get_db),
):
    """Что система поняла из строки сервиса.

    POST, а не GET: строка `otpauth://` содержит сам секрет, а адрес запроса
    оседает в журнале доступа веб-сервера и уезжает оттуда в сборщик логов.
    """
    return klyuchi_service.razobrat(payload.secret)


@router.post("", status_code=201)
def sozdat(
    payload: KlyuchIn,
    user: User = Depends(require_perm("keys", "create")),
    db: Session = Depends(get_db),
):
    klyuch = klyuchi_service.sozdat(db, user, payload.model_dump(exclude_unset=True))
    db.commit()
    return klyuchi_service.kratko(db, user, klyuch)


@router.patch("/{key_id}")
def pravit(
    key_id: int,
    payload: PravkaIn,
    user: User = Depends(require_perm("keys", "edit")),
    db: Session = Depends(get_db),
):
    klyuch = klyuchi_service.pravit(db, user, key_id, payload.model_dump(exclude_unset=True))
    db.commit()
    return klyuchi_service.kratko(db, user, klyuch)


@router.delete("/{key_id}")
def udalit(
    key_id: int,
    user: User = Depends(require_perm("keys", "delete")),
    db: Session = Depends(get_db),
):
    klyuchi_service.udalit(db, user, key_id)
    db.commit()
    return {"ok": True}


@router.post("/{key_id}/restore")
def vernut(
    key_id: int,
    user: User = Depends(require_perm("keys", "restore")),
    db: Session = Depends(get_db),
):
    klyuch = klyuchi_service.vernut(db, user, key_id)
    db.commit()
    return klyuchi_service.kratko(db, user, klyuch)


@router.delete("/{key_id}/forever")
def steret(
    key_id: int,
    user: User = Depends(require_perm("keys", "delete")),
    db: Session = Depends(get_db),
):
    klyuchi_service.steret(db, user, key_id)
    db.commit()
    return {"ok": True}


@router.post("/{key_id}/code")
def kod(
    key_id: int,
    user: User = Depends(require_perm("keys", "view")),
    db: Session = Depends(get_db),
):
    """Нынешний код и сколько ему жить. Показ всегда идёт в журнал.

    Выключателя записи снаружи нет намеренно: прежний `?silent=1` позволял
    снимать чужие коды месяцами, не оставив ни строки в журнале, ради которого
    журнал и заведён. Повторы схлопывает окно (`klyuchi_service.POKAZ_OKNO_SEKUND`).
    """
    otvet = klyuchi_service.kod(db, user, key_id)
    db.commit()
    return otvet


@router.post("/{key_id}/secret")
def sekret(
    key_id: int,
    user: User = Depends(require_perm("keys", "view")),
    db: Session = Depends(get_db),
):
    """Сам ключ и строка `otpauth://` для QR. Только root, всегда в журнал."""
    otvet = klyuchi_service.sekret(db, user, key_id)
    db.commit()
    return otvet


@router.get("/{key_id}/backup-codes")
def zapasnye(
    key_id: int,
    user: User = Depends(require_perm("keys", "view")),
    db: Session = Depends(get_db),
):
    return klyuchi_service.zapasnye(db, user, key_id)


@router.post("/{key_id}/backup-codes/{nomer}/spend")
def potratit(
    key_id: int,
    nomer: int,
    user: User = Depends(require_perm("keys", "edit")),
    db: Session = Depends(get_db),
):
    otvet = klyuchi_service.potratit_zapasnoy(db, user, key_id, nomer)
    db.commit()
    return otvet


@router.get("/{key_id}/access")
def kto_vidit(
    key_id: int,
    user: User = Depends(require_perm("keys", "manage")),
    db: Session = Depends(get_db),
):
    return klyuchi_service.kto_vidit(db, user, key_id)


@router.post("/{key_id}/access")
def otkryt(
    key_id: int,
    payload: DostupIn,
    user: User = Depends(require_perm("keys", "manage")),
    db: Session = Depends(get_db),
):
    otvet = klyuchi_service.otkryt(db, user, key_id, payload.user_id, otkryt_li=payload.otkryt)
    db.commit()
    return otvet


@router.post("/{key_id}/reminder")
def napominanie(
    key_id: int,
    payload: NapominanieIn,
    user: User = Depends(require_perm("keys", "edit")),
    db: Session = Depends(get_db),
):
    otvet = klyuchi_service.zavesti_napominanie(db, user, key_id, payload.cherez_dney)
    db.commit()
    return otvet


@router.post("/categories", status_code=201)
def sozdat_kategoriyu(
    payload: KategoriyaIn,
    user: User = Depends(require_perm("keys", "manage")),
    db: Session = Depends(get_db),
):
    otvet = klyuchi_service.sozdat_kategoriyu(db, user, payload.model_dump())
    db.commit()
    return otvet


@router.get("/categories/{category_id}/access")
def kto_vidit_kategoriyu(
    category_id: int,
    user: User = Depends(require_perm("keys", "manage")),
    db: Session = Depends(get_db),
):
    """Кого пустили в категорию. Распоряжается ею тот, кто её завёл, и root."""
    return klyuchi_service.kto_vidit_kategoriyu(db, user, category_id)


@router.post("/categories/{category_id}/access")
def otkryt_kategoriyu(
    category_id: int,
    payload: DostupIn,
    user: User = Depends(require_perm("keys", "manage")),
    db: Session = Depends(get_db),
):
    otvet = klyuchi_service.otkryt_kategoriyu(
        db, user, category_id, payload.user_id, otkryt_li=payload.otkryt
    )
    db.commit()
    return otvet


@router.delete("/categories/{category_id}")
def udalit_kategoriyu(
    category_id: int,
    user: User = Depends(require_perm("keys", "manage")),
    db: Session = Depends(get_db),
):
    klyuchi_service.udalit_kategoriyu(db, user, category_id)
    db.commit()
    return {"ok": True}


@router.get("/znak/{slug}.svg")
def znak(slug: str, _: User = Depends(require_perm("keys", "view"))):
    """Фирменный значок сервиса одним файлом.

    По одному, а не всем набором: их три с половиной тысячи, и отдать их разом
    значило бы четыре мегабайта на каждое открытие экрана. Кэш браузера потом
    не спрашивает второй раз.
    """
    kartinka = znaki_service.svg(slug)
    if kartinka is None:
        return Response(status_code=404)
    return Response(
        content=kartinka,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=604800"},
    )
