"""Файлы: дерево всего, что лежит на диске, свои папки и своя загрузка.

Раздел раньше жил в системных настройках под `settings.manage` и показывал
только работы досок плоским списком. Стал блоком со своими правами: смотреть
дерево, приносить файлы, убирать их и выпускать наружу по ссылке. ЧТО видно в
дереве, решают права на разделы, из которых файлы пришли, — разбор в
`core/services/fayly_service.py`; про ссылки — в `fayly_ssylki_service.py`.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import (
    fayly_service,
    fayly_ssylki_service,
    permissions_service,
    storage_service,
)
from database.models import User
from web.api.deps import get_db, require_perm

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/tree")
def derevo(
    actor: User = Depends(require_perm("files", "view")),
    db: Session = Depends(get_db),
):
    """Дерево со счётчиками и состояние диска: полоса занятого места внизу
    панели берётся отсюда же, а не вторым запросом."""
    return {"tree": fayly_service.derevo(db, actor), "storage": storage_service.status(db)}


@router.get("")
def spisok(
    node: str = Query(default=fayly_service.VSE, max_length=64),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    actor: User = Depends(require_perm("files", "view")),
    db: Session = Depends(get_db),
):
    otvet = fayly_service.soderzhimoe(db, actor, node, page, per_page)
    # Состояние ссылок — тем же ответом и одним запросом на страницу: значок в
    # строке нужен у каждой, а по запросу на строку это обращение к базе на
    # каждую. Без права выпускать — не спрашиваем вовсе.
    if permissions_service.has(db, actor, "files", "share"):
        otvet["links"] = fayly_ssylki_service.po_faylam(db, [f["id"] for f in otvet["items"]])
    return otvet


class PapkaIn(BaseModel):
    name: str = Field(max_length=120)
    parent_id: int | None = None


@router.post("/folders", status_code=201)
def zavesti_papku(
    payload: PapkaIn,
    actor: User = Depends(require_perm("files", "create")),
    db: Session = Depends(get_db),
):
    papka = fayly_service.sozdat_papku(db, actor, payload.parent_id, payload.name)
    return {"id": papka.id, "name": papka.name, "parent_id": papka.parent_id}


@router.delete("/folders/{folder_id}")
def snyat_papku(
    folder_id: int,
    actor: User = Depends(require_perm("files", "delete")),
    db: Session = Depends(get_db),
):
    fayly_service.udalit_papku(db, actor, folder_id)
    return {"message": "Folder deleted", "storage": storage_service.status(db)}


@router.post("", status_code=201)
async def zalit(
    file: UploadFile,
    folder_id: int | None = Query(default=None),
    actor: User = Depends(require_perm("files", "create")),
    db: Session = Depends(get_db),
):
    """Принять файл в свою папку.

    Обычной формой, а не кусками: кусочная загрузка — это другой способ класть
    байты на диск, и второй такой в системе означал бы две приёмки с разными
    проверками. Понадобится она — понадобится всем, кто грузит видео, включая
    доски; это отдельная работа, а не свойство этого экрана.
    """
    content = await file.read()
    fayl = fayly_service.prinyat(db, actor, folder_id, file.filename or "file", content)
    return {
        "id": f"stored:{fayl.id}",
        "name": fayl.original_name,
        "size_bytes": fayl.size_bytes,
        "folder_id": fayl.folder_id,
    }


@router.get("/{file_id}/download")
def skachat(
    file_id: int,
    _: User = Depends(require_perm("files", "view")),
    db: Session = Depends(get_db),
):
    """Отдать свой файл сотруднику. Чужие файлы отсюда не отдаются: у них есть
    своя ручка у своей карточки, и вторая дорога к тем же байтам разошлась бы
    с первой на первой же правке прав."""
    fayl = fayly_service.poluchit(db, file_id)
    return FileResponse(
        fayly_service.fayl_na_diske(fayl),
        media_type=fayl.mime,
        filename=fayl.original_name,
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/{file_id}")
def udalit(
    file_id: int,
    actor: User = Depends(require_perm("files", "delete")),
    db: Session = Depends(get_db),
):
    fayly_service.udalit(db, actor, file_id)
    return {"message": "File deleted", "storage": storage_service.status(db)}


class SsylkaIn(BaseModel):
    """Настройки ссылки.

    Поле, которого в теле НЕТ, означает «не трогай»; присланное `null` — «сними»
    (код) или «без срока». Различать их обязательно: пока и то и другое было
    одним `None`, смена срока у ссылки под кодом отвечала «нужен код», и
    поправить срок было нельзя вовсе.
    """

    rezhim: str = Field(default="view", max_length=16)
    krug: str = Field(default="link", max_length=16)
    pin: str | None = None
    expires_at: datetime | None = None

    def chto_prislali(self) -> dict:
        """Только те поля, которые вправду были в теле запроса."""
        return {imya: getattr(self, imya) for imya in self.model_fields_set}


@router.get("/{node}/link")
def uznat_ssylku(
    node: str,
    _: User = Depends(require_perm("files", "share")),
    db: Session = Depends(get_db),
):
    """Что сейчас у файла со ссылкой. Пусто — не делились."""
    ssylka = fayly_ssylki_service.ssylka_fayla(db, node)
    return {"link": None if ssylka is None else fayly_ssylki_service.kartochka(db, ssylka)}


@router.post("/{node}/link", status_code=201)
def vypustit_ssylku(
    node: str,
    payload: SsylkaIn,
    actor: User = Depends(require_perm("files", "share")),
    db: Session = Depends(get_db),
):
    ssylka = fayly_ssylki_service.vypustit(
        db,
        actor,
        node,
        rezhim=payload.rezhim,
        krug=payload.krug,
        pin=payload.pin,
        expires_at=payload.expires_at,
    )
    return {"link": fayly_ssylki_service.kartochka(db, ssylka)}


@router.patch("/links/{link_id}")
def nastroit_ssylku(
    link_id: int,
    payload: SsylkaIn,
    actor: User = Depends(require_perm("files", "share")),
    db: Session = Depends(get_db),
):
    ssylka = fayly_ssylki_service.nastroit(db, actor, link_id, **payload.chto_prislali())
    return {"link": fayly_ssylki_service.kartochka(db, ssylka)}


@router.delete("/links/{link_id}")
def otozvat_ssylku(
    link_id: int,
    actor: User = Depends(require_perm("files", "share")),
    db: Session = Depends(get_db),
):
    """Отозвать насовсем: строка уходит вместе с журналом открытий. Отзыв — не
    «выключить»: оставленная неактивной строка выглядит как «просто выключена»,
    и её включают обратно по ошибке."""
    fayly_ssylki_service.otozvat(db, actor, link_id)
    return {"message": "Link revoked"}
