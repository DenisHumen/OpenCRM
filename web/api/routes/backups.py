"""Копия системы с экрана настроек. Устройство — `core/services/backup_service.py`."""

import shutil
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services import backup_service
from database.models import User
from web.api.deps import get_db, require_perm

router = APIRouter(prefix="/system/backups", tags=["backups"])

manage = Depends(require_perm("settings", "manage"))


class KeyIn(BaseModel):
    replace: bool = False


class KeyConfirmIn(BaseModel):
    fragment: str


@router.get("", dependencies=[manage])
def backups_status():
    return backup_service.sostoyanie()


@router.post("/key", dependencies=[manage])
def create_key(payload: KeyIn | None = None):
    """Породить ключ копий. Показывается один раз, пока не подтверждён — не действует."""
    return backup_service.zavesti_klyuch(replace=bool(payload and payload.replace))


@router.post("/key/confirm")
def confirm_key(
    payload: KeyConfirmIn,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    return backup_service.podtverdit_klyuch(db, actor, payload.fragment)


@router.post("/db")
def take_db_backup(actor: User = Depends(require_perm("settings", "manage"))):
    return backup_service.snyat(actor, "db")


@router.post("/storage")
def take_storage_backup(actor: User = Depends(require_perm("settings", "manage"))):
    return backup_service.snyat(actor, "storage")


@router.get("/jobs/{job_id}", dependencies=[manage])
def backup_job(job_id: str):
    return backup_service.rabota(job_id)


@router.post("/jobs/{job_id}/check", dependencies=[manage])
def check_backup(job_id: str):
    return backup_service.proverit(job_id)


@router.delete("/jobs/{job_id}")
def delete_backup(
    job_id: str,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    return backup_service.udalit(db, actor, job_id)


@router.get("/jobs/{job_id}/file")
def download_backup(
    job_id: str,
    actor: User = Depends(require_perm("settings", "manage")),
    db: Session = Depends(get_db),
):
    """Готовая копия файлом. Под той же сессией и правом, что и снятие:
    одноразовая ссылка без входа была бы ещё одним способом унести систему."""
    put, imya = backup_service.fayl_dlya_skachivaniya(db, actor, job_id)
    return FileResponse(put, filename=imya, media_type="application/octet-stream")


@router.post("/restore")
def restore_backup(
    file: UploadFile,
    kind: str = Form(...),
    actor: User = Depends(require_perm("backups", "manage")),
    db: Session = Depends(get_db),
):
    """Заменить базу (или дополнить файлы) из зашифрованной копии.

    Файл сначала целиком ложится на диск: расшифровка сверяет метку по всему
    шифротексту, и делать это на потоке из запроса значило бы держать его
    открытым столько, сколько идёт сверка.
    """
    zagruzka = backup_service.katalog() / f"upload-{uuid4().hex[:8]}.enc"
    try:
        with zagruzka.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        return backup_service.vosstanovit(db, actor, kind, zagruzka)
    finally:
        zagruzka.unlink(missing_ok=True)
