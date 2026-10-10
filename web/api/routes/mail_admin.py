"""Ящики своего почтового сервера: завести, сменить пароль и адрес, квота, доступ, псевдонимы.

Устройство — `core/services/mail_server_service.py`, разбор — docs/bloki/32-pochtovyy-server.md.
Весь роутер — блок `mail` и `mail.manage`: раздавать ящики и пароли от них —
решение управляющего почтой, а не каждого, кто читает письма.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import mail_server_service as boxes
from database.models import User
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/mail/admin",
    tags=["mail"],
    dependencies=[Depends(require_module("mail")), Depends(require_perm("mail", "manage"))],
)

manage = require_perm("mail", "manage")


class AccessIn(BaseModel):
    user_id: int
    can_send: bool = True


class BoxIn(BaseModel):
    server_id: int
    domain: str
    local_part: str | None = None
    random: bool = False
    password: str | None = None
    generate_password: bool = True
    quota_mb: int | None = None
    title: str | None = None
    is_shared: bool = False
    users: list[AccessIn] = Field(default_factory=list)


class BoxPatchIn(BaseModel):
    title: str | None = None
    # Явный null — снять квоту; поля нет — не трогать.
    quota_mb: int | None = None
    is_shared: bool | None = None
    is_active: bool | None = None


class PasswordIn(BaseModel):
    password: str | None = None
    generate: bool = True


class RenameIn(BaseModel):
    local_part: str
    domain: str | None = None
    password: str | None = None
    generate_password: bool = True
    keep_alias: bool = False


class AliasIn(BaseModel):
    server_id: int
    source: str
    destination: str


@router.get("/boxes")
def list_boxes(db: Session = Depends(get_db)):
    """Все ящики — свои и внешние — со списками доступа. Без сети: только база."""
    return {"items": boxes.spisok(db)}


@router.post("/boxes", status_code=201)
def create_box(payload: BoxIn, actor: User = Depends(manage), db: Session = Depends(get_db)):
    """Пароль — в этом ответе и больше нигде: CRM его не хранит."""
    box, parol = boxes.sozdat(db, actor, payload.model_dump())
    return {"box": boxes.yashchik_out(db, box), "password": parol}


@router.patch("/boxes/{account_id}")
def update_box(
    account_id: int, payload: BoxPatchIn, actor: User = Depends(manage), db: Session = Depends(get_db)
):
    box = boxes.izmenit(db, actor, account_id, payload.model_dump(exclude_unset=True))
    return boxes.yashchik_out(db, box)


@router.post("/boxes/{account_id}/password")
def change_password(
    account_id: int, payload: PasswordIn, actor: User = Depends(manage), db: Session = Depends(get_db)
):
    return {"password": boxes.smenit_parol(db, actor, account_id, payload.model_dump())}


@router.post("/boxes/{account_id}/rename")
def rename_box(
    account_id: int, payload: RenameIn, actor: User = Depends(manage), db: Session = Depends(get_db)
):
    box, parol = boxes.pereimenovat(db, actor, account_id, payload.model_dump())
    return {"box": boxes.yashchik_out(db, box), "password": parol}


@router.delete("/boxes/{account_id}")
def delete_box(
    account_id: int,
    keep_data: bool = Query(default=False),
    actor: User = Depends(manage),
    db: Session = Depends(get_db),
):
    boxes.udalit(db, actor, account_id, keep_data=keep_data)
    return {"message": "Mailbox deleted"}


@router.get("/boxes/{account_id}/access")
def get_access(account_id: int, db: Session = Depends(get_db)):
    return boxes.dostup(db, account_id)


@router.put("/boxes/{account_id}/access")
def put_access(
    account_id: int, payload: list[AccessIn], actor: User = Depends(manage), db: Session = Depends(get_db)
):
    """Список целиком: кого нет в теле — тот ящик больше не видит."""
    return boxes.zadat_dostup(db, actor, account_id, [p.model_dump() for p in payload])


@router.get("/boxes/{account_id}/client-settings")
def client_settings(account_id: int, db: Session = Depends(get_db)):
    """IMAP/SMTP для телефона и почтовой программы."""
    return boxes.nastroyki_klienta(db, account_id)


@router.get("/suggest")
def suggest(domain: str | None = Query(default=None, max_length=253), db: Session = Depends(get_db)):
    """Случайное свободное имя и пароль. Без сети — занятость сверяется с базой."""
    return boxes.predlozhit(db, domain)


@router.get("/aliases")
def list_aliases(server_id: int | None = None, db: Session = Depends(get_db)):
    return {"items": boxes.psevdonimy(db, server_id)}


@router.post("/aliases", status_code=201)
def create_alias(payload: AliasIn, actor: User = Depends(manage), db: Session = Depends(get_db)):
    return boxes.alias_out(boxes.dobavit_psevdonim(db, actor, payload.model_dump()))


@router.delete("/aliases/{alias_id}")
def delete_alias(alias_id: int, actor: User = Depends(manage), db: Session = Depends(get_db)):
    boxes.udalit_psevdonim(db, actor, alias_id)
    return {"message": "Alias deleted"}
