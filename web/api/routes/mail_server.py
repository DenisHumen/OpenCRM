"""Свой почтовый сервер: черновик, SSH, проверка, установка, подключение, удаление.

Устройство — `core/services/mail_install.py`, разбор — docs/bloki/32-pochtovyy-server.md.
Весь роутер — блок `mail` и право `mail.manage`. Учётные данные и установку токен
не трогает (`token_service.ZAKRYTO`): это делает человек на экране.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.services import mail_install
from database.models import User
from web.api.deps import get_db, require_module, require_perm

router = APIRouter(
    prefix="/mail/server",
    tags=["mail-server"],
    dependencies=[Depends(require_module("mail")), Depends(require_perm("mail", "manage"))],
)

upravlyayushchiy = require_perm("mail", "manage")


class ServerIn(BaseModel):
    mode: str
    title: str | None = Field(default=None, max_length=120)
    hostname: str = Field(max_length=255)
    domain: str = Field(max_length=253)
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_port: int | None = None
    ssh_user: str | None = Field(default=None, max_length=64)


class ServerPatch(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    hostname: str | None = Field(default=None, max_length=255)
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_port: int | None = None
    ssh_user: str | None = Field(default=None, max_length=64)


class SshConnectIn(BaseModel):
    host_key_line: str = Field(max_length=16384)
    user: str = Field(max_length=64)
    password: str | None = Field(default=None, max_length=1024)
    private_key: str | None = Field(default=None, max_length=32768)
    passphrase: str | None = Field(default=None, max_length=1024)
    keep_sudo: bool = False
    sudo_password: str | None = Field(default=None, max_length=1024)


class CheckIn(BaseModel):
    allow_docker_install: bool = False
    tls: str | None = None
    sudo_password: str | None = Field(default=None, max_length=1024)


class InstallIn(BaseModel):
    first_local: str | None = Field(default=None, max_length=64)
    first_password: str | None = Field(default=None, max_length=128)
    generate_password: bool = True
    acme_email: str | None = Field(default=None, max_length=320)
    allow_docker_install: bool = False
    # Самоподписанный — для стенда без DNS; по умолчанию Let's Encrypt.
    tls: str | None = None
    sudo_password: str | None = Field(default=None, max_length=1024)


class RemoveIn(BaseModel):
    delete_mail: bool = False
    confirm: str = Field(max_length=255)


class DomainIn(BaseModel):
    domain: str = Field(max_length=253)


class RelayIn(BaseModel):
    host: str = Field(max_length=255)
    port: int = 587
    user: str = Field(default="", max_length=320)
    password: str | None = Field(default=None, max_length=1024)


class TestIn(BaseModel):
    to: str = Field(max_length=320)


@router.get("")
def servery(db: Session = Depends(get_db)):
    """Серверы и жив ли обновлятор — без сети: открывается мгновенно и при пустой настройке."""
    return mail_install.spisok(db)


@router.post("", status_code=201)
def sozdat(body: ServerIn, db: Session = Depends(get_db)):
    server = mail_install.sozdat(db, body.model_dump())
    return mail_install.server_out(db, server)


@router.patch("/{server_id}")
def izmenit(server_id: int, body: ServerPatch, db: Session = Depends(get_db)):
    server = mail_install.izmenit(db, server_id, body.model_dump(exclude_unset=True))
    return mail_install.server_out(db, server)


@router.delete("/{server_id}")
def udalit(server_id: int, db: Session = Depends(get_db)):
    """По SSH заодно снимает свой ключ с машины; не вышло — `warning` с готовой командой."""
    return mail_install.udalit(db, server_id)


@router.post("/{server_id}/ssh/probe")
def ssh_proba(server_id: int, db: Session = Depends(get_db)):
    """Шаг 1: отпечаток ключа сервера — человек сверяет его и подтверждает."""
    return mail_install.ssh_proba(db, server_id)


@router.post("/{server_id}/ssh/connect")
def ssh_podklyuchit(server_id: int, body: SshConnectIn, db: Session = Depends(get_db)):
    """Шаг 2: вход паролем или ключом, свой ключ CRM — в authorized_keys; пароль не хранится."""
    return mail_install.ssh_podklyuchit(db, server_id, body.model_dump())


@router.post("/{server_id}/check", status_code=202)
def proverit(
    server_id: int,
    body: CheckIn | None = None,
    actor: User = Depends(upravlyayushchiy),
    db: Session = Depends(get_db),
):
    job = mail_install.proverit(db, server_id, actor, body.model_dump() if body else {})
    return mail_install.job_out(job)


@router.post("/{server_id}/install", status_code=202)
def ustanovit(
    server_id: int,
    body: InstallIn,
    actor: User = Depends(upravlyayushchiy),
    db: Session = Depends(get_db),
):
    """Пароль первого ящика — в этом ответе и больше нигде."""
    job, adres, parol = mail_install.ustanovit(db, server_id, actor, body.model_dump())
    return {"job": mail_install.job_out(job), "first_address": adres, "first_password": parol}


@router.post("/{server_id}/adopt", status_code=202)
def podklyuchit(server_id: int, actor: User = Depends(upravlyayushchiy), db: Session = Depends(get_db)):
    """Найти уже стоящий сервер, выдать CRM мастер-вход, подтянуть ящики."""
    return mail_install.job_out(mail_install.podklyuchit(db, server_id, actor))


@router.post("/{server_id}/remove", status_code=202)
def snyat(server_id: int, body: RemoveIn, actor: User = Depends(upravlyayushchiy), db: Session = Depends(get_db)):
    return mail_install.job_out(mail_install.snyat(db, server_id, actor, body.model_dump()))


@router.get("/jobs/{job_id}")
def rabota(job_id: int, log_from: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    """Работа и кусок журнала с байта `log_from` — экран дочитывает, пока идёт установка."""
    return mail_install.rabota(db, job_id, log_from)


@router.get("/{server_id}/jobs")
def raboty(server_id: int, db: Session = Depends(get_db)):
    return mail_install.raboty(db, server_id)


@router.post("/{server_id}/dns")
def dns(server_id: int, db: Session = Depends(get_db)):
    return mail_install.dns(db, server_id)


@router.post("/{server_id}/domains", status_code=201)
def dobavit_domen(server_id: int, body: DomainIn, db: Session = Depends(get_db)):
    return mail_install.dobavit_domen(db, server_id, body.model_dump())


@router.post("/{server_id}/domains/{domain_id}/dkim")
def sozdat_dkim(server_id: int, domain_id: int, db: Session = Depends(get_db)):
    """DKIM домену, которому установка его не создала (`dkim_failed`)."""
    return mail_install.sozdat_dkim(db, server_id, domain_id)


@router.delete("/{server_id}/domains/{domain_id}", status_code=204)
def udalit_domen(server_id: int, domain_id: int, db: Session = Depends(get_db)):
    mail_install.udalit_domen(db, server_id, domain_id)


@router.post("/{server_id}/sync")
def sverit(server_id: int, actor: User = Depends(upravlyayushchiy), db: Session = Depends(get_db)):
    return mail_install.sinhronizirovat(db, server_id, actor)


@router.get("/{server_id}/health")
def zdorove(server_id: int, db: Session = Depends(get_db)):
    return mail_install.zdorove(db, server_id)


@router.put("/{server_id}/relay")
def posrednik(server_id: int, body: RelayIn, db: Session = Depends(get_db)):
    return mail_install.zadat_posrednika(db, server_id, body.model_dump())


@router.delete("/{server_id}/relay")
def bez_posrednika(server_id: int, db: Session = Depends(get_db)):
    return mail_install.snyat_posrednika(db, server_id)


@router.post("/{server_id}/test")
def proverochnoe(server_id: int, body: TestIn, db: Session = Depends(get_db)):
    return mail_install.proverochnoe_pismo(db, server_id, body.model_dump())
