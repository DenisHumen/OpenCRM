"""Свой почтовый сервер: серверы, ящики, псевдонимы, доступ (docs/bloki/32-pochtovyy-server.md).

Ящик на своём сервере — это `MailAccount` с `server_id`: заводится агентом на
сервере, а в базе живёт той же строкой, что и внешний. Пароля ящика CRM не
хранит: сама она ходит мастер-входом, а пароль, заданный при создании или
смене, показывается человеку один раз — для телефона и почтовой программы.

Порядок у каждой правки один: проверки по базе → агент на сервере → запись в
базу. Агент отказал — в базе ничего не изменилось; база не записалась — ящик на
сервере найдёт сверка (`sverit`), он не потеряется.
"""

import logging
import re
import secrets
import string

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.services import audit_service, mail_access, mail_host, mail_service
from core.utils import is_valid_email, now_utc
from database.models import MailAccount, MailAlias, MailDomain, MailServer, User
from database.models.audit import SOURCE_MAIL_SYNC, SOURCE_MANUAL
from database.models.mail import ADDRESS_LENGTH, TITLE_LENGTH
from database.models.mail_server import STATUS_READY
from database.models.user import STATUS_ACTIVE
from database.repositories import mail as mail_repo
from database.repositories import mail_boxes as boxes_repo
from database.repositories import mail_server as repo
from database.repositories import users as users_repo

#: Назначения шифротекстов secretbox — у каждого секрета своё, чтобы шифротекст
#: одного места нельзя было подсунуть в другое.
SSH_KEY_PURPOSE = "mail-server-ssh-key"
SUDO_PURPOSE = "mail-server-sudo"
MASTER_PURPOSE = "mail-server-master"
AGENT_PURPOSE = "mail-server-agent"
RELAY_PURPOSE = "mail-server-relay"

logger = logging.getLogger("opencrm.mail")

# Имя ящика: без `+` — Postfix режет по нему подадреса, и ящик `a+b` не получил бы ничего.
LOCAL_PART = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
DOMAIN = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

# Без похожих знаков: пароль диктуют по телефону и перепечатывают с экрана.
PAROL_ZNAKI = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
PAROL_DLINA = 16
PAROL_MIN = 10
PAROL_MAX = 128
QUOTA_MAX_MB = 1_000_000

#: Словарь случайных имён: короткие английские слова без двусмысленностей.
SLOVA = (
    "amber", "apple", "arrow", "aspen", "beacon", "birch", "blue", "breeze", "brook", "cedar",
    "cloud", "comet", "coral", "cosmo", "crane", "delta", "drift", "eagle", "ember", "falcon",
    "fern", "field", "flint", "frost", "glade", "grove", "harbor", "hazel", "heron", "iris",
    "jade", "juniper", "kite", "lake", "lark", "lemon", "lotus", "maple", "meadow", "mint",
    "nova", "oak", "ocean", "olive", "orbit", "pearl", "pine", "pixel", "plum", "quartz",
    "rain", "raven", "reef", "river", "robin", "sage", "silver", "sky", "sparrow", "spruce",
    "stone", "storm", "sun", "swift", "tide", "topaz", "valley", "violet", "willow", "wind",
)

#: Отказ агента, после которого удалять строку из базы всё равно верно: ящика на сервере уже нет.
NET_NA_SERVERE = frozenset({"mail_box_not_on_server", "not_found"})


# --- случайные имена и пароли ---


def sluchaynyy_parol() -> str:
    """16 знаков из `secrets`: обязательно строчная, прописная и цифра."""
    while True:
        parol = "".join(secrets.choice(PAROL_ZNAKI) for _ in range(PAROL_DLINA))
        if (
            any(c.islower() for c in parol)
            and any(c.isupper() for c in parol)
            and any(c.isdigit() for c in parol)
        ):
            return parol


def sluchaynoe_imya() -> str:
    """`слово.слово42` — запоминается и диктуется; 10 знаков [a-z0-9] — запасной вид."""
    if secrets.randbelow(5) == 0:
        return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    cifry = str(secrets.randbelow(990) + 10)
    return f"{secrets.choice(SLOVA)}.{secrets.choice(SLOVA)}{cifry}"


def _chistyy_domen(domain: str | None) -> str:
    domen = (domain or "").strip().lower().rstrip(".")
    if not DOMAIN.match(domen):
        raise errors.ValidationError("Domain name is not valid", code="mail_domain_bad")
    return domen


def predlozhit(db: Session, domain: str | None = None) -> dict:
    """Случайное свободное имя и пароль. Без сети: занятость — по базе."""
    domen = _chistyy_domen(domain) if domain else ""
    for _ in range(5):
        imena = list(dict.fromkeys(sluchaynoe_imya() for _ in range(8)))
        if not domen:
            return {"local_part": imena[0], "address": None, "password": sluchaynyy_parol()}
        zanyato = repo.zanyatye_adresa(db, [f"{i}@{domen}" for i in imena])
        svobodnoe = next((i for i in imena if f"{i}@{domen}" not in zanyato), None)
        if svobodnoe:
            return {
                "local_part": svobodnoe,
                "address": f"{svobodnoe}@{domen}",
                "password": sluchaynyy_parol(),
            }
    raise errors.ConflictError("Could not find a free random name", code="mail_box_exists")


def proverit_parol(parol: str, address: str) -> str:
    if len(parol) < PAROL_MIN or len(parol) > PAROL_MAX:
        raise errors.ValidationError(
            f"Password must be {PAROL_MIN} to {PAROL_MAX} characters long", code="mail_password_weak"
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in parol):
        raise errors.ValidationError(
            "Password must not contain line breaks or control characters", code="mail_password_weak"
        )
    nizhniy = parol.lower()
    if nizhniy in (address.lower(), address.split("@", 1)[0].lower()):
        raise errors.ValidationError("Password must not repeat the address", code="mail_password_weak")
    return parol


def _parol(data: dict, address: str, *, pole: str = "password", sgenerirovat: str = "generate_password") -> str:
    """Свой пароль, если прислали, иначе сгенерированный: пустой ящик без пароля не нужен никому."""
    svoy = data.get(pole)
    if svoy:
        return proverit_parol(svoy, address)
    if data.get(sgenerirovat) is False:
        raise errors.ValidationError("Password is required", code="mail_password_weak")
    return sluchaynyy_parol()


def _kvota(znachenie) -> int | None:
    if znachenie is None:
        return None
    try:
        kvota = int(znachenie)
    except (TypeError, ValueError):
        kvota = 0
    if not 1 <= kvota <= QUOTA_MAX_MB:
        raise errors.ValidationError(
            f"Quota must be between 1 and {QUOTA_MAX_MB} MB", code="mail_quota_bad"
        )
    return kvota


# --- сервер и агент ---


def gotovyy_server(db: Session, server_id: int | None) -> MailServer:
    server = repo.get_server(db, server_id) if server_id else None
    if server is None:
        raise errors.NotFoundError("Mail server not found", code="mail_server_not_found")
    if server.status != STATUS_READY:
        raise errors.ConflictError("Mail server is not ready yet", code="mail_server_not_ready")
    return server


def vyzvat(server: MailServer, op: str, args: dict | None = None, *, timeout: float = 60) -> dict:
    """Операция агента; его отказ — доменной ошибкой с тем же кодом.

    Недоступный агент — 503: дело не в запросе, а в том, что сервер сейчас не ответил.
    """
    try:
        return mail_host.host_dlya(server).vyzvat(op, args or {}, timeout=timeout) or {}
    except mail_host.MailHostError as exc:
        if exc.code == "mail_agent_unreachable":
            raise errors.PrehodyashchayaBedaError(exc.message, code="mail_agent_unreachable") from exc
        if not exc.code:
            raise errors.ValidationError(exc.message, code="mail_agent_failed") from exc
        raise errors.ValidationError(exc.message, code=exc.code) from exc


def _domen_servera(db: Session, server: MailServer, domain: str | None) -> str:
    domen = _chistyy_domen(domain)
    zapis = repo.domen_po_imeni(db, domen)
    if zapis is None or zapis.server_id != server.id:
        raise errors.ValidationError("This domain is not on the mail server", code="mail_domain_bad")
    return domen


def _imya(local_part: str | None) -> str:
    imya = (local_part or "").strip().lower()
    if not LOCAL_PART.match(imya) or ".." in imya:
        raise errors.ValidationError(
            "Mailbox name may contain a-z, 0-9, dot, dash and underscore", code="mail_local_part_bad"
        )
    return imya


def _adres_svoboden(db: Session, server: MailServer, address: str) -> None:
    if len(address) > ADDRESS_LENGTH:
        raise errors.ValidationError("Mailbox address is too long", code="mail_local_part_bad")
    if repo.yashchik_po_adresu(db, address) is not None:
        raise errors.ConflictError("A mailbox with this address already exists", code="mail_box_exists")
    if repo.psevdonim_s_adresa(db, server.id, address) is not None:
        raise errors.ConflictError("This address is already an alias", code="mail_box_exists")


# --- видимость управляющему ---


def _oblast(db: Session) -> set[int] | None:
    return mail_access.oblast_tokena(db)


def _bez_ogranicheniya(db: Session) -> None:
    """Заводить ящики и псевдонимы может только токен на все ящики: узкий токен — про свои."""
    if _oblast(db) is not None:
        raise errors.ForbiddenError(
            "This token is limited to selected mailboxes", code="mail_box_forbidden"
        )


def yashchik(db: Session, account_id: int) -> MailAccount:
    box = mail_repo.get_account(db, account_id)
    oblast = _oblast(db)
    if box is None or (oblast is not None and account_id not in oblast):
        raise errors.NotFoundError("Mailbox not found", code="mail_account_not_found")
    return box


def _na_servere(db: Session, box: MailAccount) -> MailServer:
    if box.server_id is None:
        raise errors.ValidationError(
            "This mailbox is not on our mail server", code="mail_box_not_on_server"
        )
    return gotovyy_server(db, box.server_id)


def _dostup_out(stroki) -> list[dict]:
    return [{"user_id": u.id, "name": u.name, "can_send": d.can_send} for d, u in stroki]


def box_out(box: MailAccount, dostupy: list | None = None, servery: dict | None = None) -> dict:
    server = (servery or {}).get(box.server_id)
    return {
        "id": box.id,
        "address": box.address,
        "title": box.title,
        "kind": "server" if box.server_id else "external",
        "server_id": box.server_id,
        "server_hostname": server.hostname if server else None,
        "is_shared": box.is_shared,
        "is_active": box.is_active,
        "quota_mb": box.quota_mb,
        "unseen": box.unseen_count,
        "status_checked_at": box.status_checked_at.isoformat() if box.status_checked_at else None,
        "last_sync_at": box.last_sync_at.isoformat() if box.last_sync_at else None,
        "last_error": box.last_error,
        "access": _dostup_out(dostupy or []),
    }


def yashchik_out(db: Session, box: MailAccount) -> dict:
    servery = {box.server_id: repo.get_server(db, box.server_id)} if box.server_id else {}
    return box_out(box, boxes_repo.dostupy(db, box.id), servery)


def spisok(db: Session) -> list[dict]:
    boxes = repo.vse_yashchiki(db, _oblast(db))
    dostupy = repo.dostupy_yashchikov(db, [b.id for b in boxes])
    servery = {s.id: s for s in repo.servery(db)}
    return [box_out(b, dostupy.get(b.id), servery) for b in boxes]


# --- ящики ---


def _zapisat(db: Session, actor: User | None, action: str, box: MailAccount, *, before=None, after=None,
             source: str = SOURCE_MANUAL) -> None:
    audit_service.record(
        db,
        action=action,
        actor=actor,
        source=source,
        entity_type=audit_service.ENTITY_MAILBOX,
        entity_id=box.id,
        entity_label=box.address,
        before=before,
        after=after,
    )


def _opisanie(box: MailAccount) -> str:
    """Свойства ящика строкой журнала. По-английски: журнал читают в интерфейсе любого языка."""
    kvota = f"{box.quota_mb} MB" if box.quota_mb else "unlimited"
    return f"quota {kvota}, {'shared' if box.is_shared else 'private'}"


def _proverit_lyudey(db: Session, stroki: list[dict]) -> list[tuple[int, bool]]:
    nuzhno: dict[int, bool] = {}
    for stroka in stroki or []:
        nuzhno[int(stroka["user_id"])] = bool(stroka.get("can_send", True))
    if nuzhno:
        est = {u.id for u in users_repo.get_many(db, nuzhno)}
        propali = sorted(set(nuzhno) - est)
        if propali:
            raise errors.NotFoundError(f"User not found: {propali[0]}", code="user_not_found")
    return sorted(nuzhno.items())


def sozdat(db: Session, actor: User, data: dict) -> tuple[MailAccount, str]:
    """Новый ящик на сервере. Пароль — в ответе один раз и больше нигде."""
    _bez_ogranicheniya(db)
    server = gotovyy_server(db, data.get("server_id"))
    domen = _domen_servera(db, server, data.get("domain"))
    if data.get("random") or not data.get("local_part"):
        if not data.get("random"):
            raise errors.ValidationError("Mailbox name is required", code="mail_local_part_bad")
        imya = predlozhit(db, domen)["local_part"]
    else:
        imya = _imya(data.get("local_part"))
    address = f"{imya}@{domen}"
    _adres_svoboden(db, server, address)
    parol = _parol(data, address)
    kvota = _kvota(data.get("quota_mb"))
    title = (data.get("title") or "").strip()
    if len(title) > TITLE_LENGTH:
        raise errors.ValidationError(
            f"Title is too long (max {TITLE_LENGTH} characters)", code="title_too_long"
        )
    lyudi = _proverit_lyudey(db, data.get("users") or [])

    vyzvat(server, "account_add", {"address": address, "password": parol, "quota_mb": kvota})

    box = repo.dobavit(db, _novyy_yashchik(server, address, title=title, kvota=kvota,
                                           obshchiy=bool(data.get("is_shared", False))))
    if lyudi:
        boxes_repo.zamenit_dostupy(db, box.id, lyudi, actor.id)
    _zapisat(db, actor, audit_service.ACTION_MAILBOX_CREATED, box, after=_opisanie(box))
    return box, parol


def _novyy_yashchik(server: MailServer, address: str, *, title: str = "", kvota: int | None = None,
                    obshchiy: bool = False) -> MailAccount:
    # Хост и порты — куда ходит сама CRM (в сети compose это имя контейнера);
    # сертификат при этом сверяется с `server.hostname`.
    host = server.imap_host or server.hostname
    return MailAccount(
        title=(title or address)[:TITLE_LENGTH],
        address=address,
        login=address,
        imap_host=host,
        imap_port=server.imap_port,
        imap_ssl=True,
        smtp_host=host,
        smtp_port=server.smtp_port,
        smtp_ssl=True,
        password_encrypted=None,
        is_active=True,
        server_id=server.id,
        is_shared=obshchiy,
        quota_mb=kvota,
    )


def izmenit(db: Session, actor: User, account_id: int, data: dict) -> MailAccount:
    """Название, квота, общий ли, включён ли. Квота меняется и на сервере."""
    box = yashchik(db, account_id)
    bylo = _opisanie(box) + f", {'on' if box.is_active else 'off'}"
    if "quota_mb" in data:
        kvota = _kvota(data["quota_mb"])
        if kvota != box.quota_mb:
            server = _na_servere(db, box)
            vyzvat(server, "account_quota", {"address": box.address, "quota_mb": kvota})
            box.quota_mb = kvota
    if data.get("title") is not None:
        title = data["title"].strip()
        if len(title) > TITLE_LENGTH:
            raise errors.ValidationError(
                f"Title is too long (max {TITLE_LENGTH} characters)", code="title_too_long"
            )
        box.title = title or box.address[:TITLE_LENGTH]
    for flag in ("is_shared", "is_active"):
        if data.get(flag) is not None:
            setattr(box, flag, bool(data[flag]))
    box.updated_at = now_utc()
    db.flush()
    stalo = _opisanie(box) + f", {'on' if box.is_active else 'off'}"
    if stalo != bylo:
        _zapisat(db, actor, audit_service.ACTION_MAILBOX_UPDATED, box, before=bylo, after=stalo)
    return box


def smenit_parol(db: Session, actor: User, account_id: int, data: dict) -> str:
    box = yashchik(db, account_id)
    server = _na_servere(db, box)
    parol = _parol(data, box.address, sgenerirovat="generate")
    vyzvat(server, "account_password", {"address": box.address, "password": parol})
    # В журнал — сам факт смены: пароль туда не попадает ни в каком виде.
    _zapisat(db, actor, audit_service.ACTION_MAILBOX_PASSWORD_CHANGED, box)
    return parol


def pereimenovat(db: Session, actor: User, account_id: int, data: dict) -> tuple[MailAccount, str]:
    """Новый адрес: агент заводит ящик, переносит каталог писем и убирает старый.

    Строка в базе та же — зеркало писем, доступы и токены остаются при ящике.
    Каталог переезжает целиком, с UID и UIDVALIDITY, поэтому счёт наблюдателя и
    номера в зеркале остаются верными.
    """
    box = yashchik(db, account_id)
    server = _na_servere(db, box)
    stary = box.address
    domen = _domen_servera(db, server, data.get("domain") or stary.split("@", 1)[1])
    novyy = f"{_imya(data.get('local_part'))}@{domen}"
    if novyy.lower() == stary.lower():
        raise errors.ValidationError("The new address is the same as the old one", code="mail_local_part_bad")
    _adres_svoboden(db, server, novyy)
    parol = _parol(data, novyy)
    ostavit = bool(data.get("keep_alias"))

    vyzvat(server, "account_rename", {"old": stary, "new": novyy, "password": parol, "keep_alias": ostavit},
           timeout=600)
    # Псевдонимы, что вели в старый адрес, агент перевёл на новый сам; база — следом.
    for alias in repo.psevdonimy_na(db, server.id, stary):
        alias.destination = novyy
    if ostavit and repo.psevdonim(db, server.id, stary, novyy) is None:
        repo.dobavit(db, MailAlias(server_id=server.id, source=stary, destination=novyy))

    if box.title == stary[:TITLE_LENGTH]:
        box.title = novyy[:TITLE_LENGTH]
    box.address = novyy
    box.login = novyy
    box.updated_at = now_utc()
    db.flush()
    _zapisat(db, actor, audit_service.ACTION_MAILBOX_RENAMED, box, before=stary,
             after=novyy + (", old address kept as alias" if ostavit else ""))
    return box, parol


def udalit(db: Session, actor: User, account_id: int, *, keep_data: bool = False) -> None:
    """Убрать ящик. Свой — и на сервере (почту можно оставить на диске), внешний — только из CRM."""
    box = yashchik(db, account_id)
    if box.server_id is None:
        mail_service.delete_account(db, box.id, actor)
        return
    server = _na_servere(db, box)
    adres = box.address
    try:
        vyzvat(server, "account_delete", {"address": adres, "keep_data": bool(keep_data)})
    except errors.ValidationError as exc:
        if exc.code not in NET_NA_SERVERE:
            raise
    # Псевдонимы, ведущие в ящик, `setup email del` снимает на сервере сам.
    for alias in repo.psevdonimy_na(db, server.id, adres):
        repo.udalit(db, alias)
    box_id = box.id
    repo.udalit(db, box)
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_MAILBOX,
        entity_id=box_id,
        entity_label=adres + (" (mail kept on disk)" if keep_data else ""),
    )


# --- доступ ---


def dostup(db: Session, account_id: int) -> list[dict]:
    box = yashchik(db, account_id)
    return _dostup_out(boxes_repo.dostupy(db, box.id))


def _dostup_tekst(stroki) -> str:
    return ", ".join(f"{u.name}{'' if d.can_send else ' (read only)'}" for d, u in stroki) or "nobody"


def zadat_dostup(db: Session, actor: User, account_id: int, stroki: list[dict]) -> list[dict]:
    """Список доступа целиком: кого нет в списке — тот ящика больше не видит."""
    box = yashchik(db, account_id)
    lyudi = _proverit_lyudey(db, stroki)
    bylo = _dostup_tekst(boxes_repo.dostupy(db, box.id))
    boxes_repo.zamenit_dostupy(db, box.id, lyudi, actor.id)
    db.expire_all()
    novye = boxes_repo.dostupy(db, box.id)
    stalo = _dostup_tekst(novye)
    if stalo != bylo:
        _zapisat(db, actor, audit_service.ACTION_MAILBOX_ACCESS_CHANGED, box, before=bylo, after=stalo)
    return _dostup_out(novye)


# --- настройки для телефона ---


def nastroyki_klienta(db: Session, account_id: int) -> dict:
    """IMAP/SMTP для телефона и почтовой программы. Пароля здесь нет — он у человека."""
    box = yashchik(db, account_id)
    if box.server_id is not None:
        server = repo.get_server(db, box.server_id)
        # Телефону — публичное имя сервера, а не имя контейнера, куда ходит CRM.
        host = (server.hostname if server else "") or box.imap_host
        return {
            "address": box.address,
            "login": box.address,
            "imap": {"host": host, "port": 993, "security": "ssl"},
            "smtp": {"host": host, "port": 465, "security": "ssl"},
            "submission": {"host": host, "port": 587, "security": "starttls"},
            "password_hint": "set_or_reset",
        }
    return {
        "address": box.address,
        "login": box.login or box.address,
        "imap": {"host": box.imap_host, "port": box.imap_port, "security": "ssl" if box.imap_ssl else "plain"},
        "smtp": {"host": box.smtp_host, "port": box.smtp_port, "security": "ssl" if box.smtp_ssl else "starttls"},
        "submission": None,
        "password_hint": "external",
    }


# --- псевдонимы ---


def alias_out(alias: MailAlias) -> dict:
    return {
        "id": alias.id,
        "server_id": alias.server_id,
        "source": alias.source,
        "destination": alias.destination,
        "created_at": alias.created_at.isoformat() if alias.created_at else None,
    }


def psevdonimy(db: Session, server_id: int | None = None) -> list[dict]:
    spisok_ = repo.psevdonimy(db, server_id)
    oblast = _oblast(db)
    if oblast is not None:
        # Узкому токену — только псевдонимы, ведущие в его ящики.
        adresa = {b.address.lower() for b in repo.vse_yashchiki(db, oblast)}
        spisok_ = [a for a in spisok_ if a.destination.lower() in adresa]
    return [alias_out(a) for a in spisok_]


def _adres(znachenie: str | None) -> str:
    adres = (znachenie or "").strip().lower()
    if len(adres) > ADDRESS_LENGTH or not is_valid_email(adres):
        raise errors.ValidationError("Alias address is not valid", code="mail_alias_bad")
    return adres


def dobavit_psevdonim(db: Session, actor: User, data: dict) -> MailAlias:
    _bez_ogranicheniya(db)
    server = gotovyy_server(db, data.get("server_id"))
    source = _adres(data.get("source"))
    destination = _adres(data.get("destination"))
    if source == destination:
        raise errors.ValidationError("Alias points to itself", code="mail_alias_bad")
    _domen_servera(db, server, source.split("@", 1)[1])
    if repo.yashchik_po_adresu(db, source) is not None:
        raise errors.ValidationError("This address is a mailbox, not an alias", code="mail_alias_bad")
    if repo.psevdonim(db, server.id, source, destination) is not None:
        raise errors.ConflictError("This alias already exists", code="mail_alias_exists")
    vyzvat(server, "alias_add", {"source": source, "destination": destination})
    alias = repo.dobavit(db, MailAlias(server_id=server.id, source=source, destination=destination))
    audit_service.record(
        db,
        action=audit_service.ACTION_MAIL_ALIAS_CREATED,
        actor=actor,
        source=SOURCE_MANUAL,
        entity_type=audit_service.ENTITY_MAIL_ALIAS,
        entity_id=alias.id,
        entity_label=f"{source} -> {destination}",
    )
    return alias


def udalit_psevdonim(db: Session, actor: User, alias_id: int) -> None:
    _bez_ogranicheniya(db)
    alias = repo.get_alias(db, alias_id)
    if alias is None:
        raise errors.NotFoundError("Alias not found", code="mail_alias_not_found")
    server = gotovyy_server(db, alias.server_id)
    try:
        vyzvat(server, "alias_delete", {"source": alias.source, "destination": alias.destination})
    except errors.ValidationError as exc:
        if exc.code not in NET_NA_SERVERE:
            raise
    metka = f"{alias.source} -> {alias.destination}"
    alias_id = alias.id
    repo.udalit(db, alias)
    audit_service.record_deletion(
        db, actor=actor, entity_type=audit_service.ENTITY_MAIL_ALIAS, entity_id=alias_id, entity_label=metka
    )


# --- сверка с сервером ---


def sverit(db: Session, server: MailServer, actor: User | None = None) -> dict:
    """Ящики, псевдонимы и домены сервера → база. Сервер — источник правды.

    Ящик, которого в базе нет, заводится (личным: кому его видеть, решит
    управляющий); ящик базы, которого нет на сервере, не стирается — его
    показывают в `missing`, решать человеку: там может быть переписка клиентов.
    """
    data = vyzvat(server, "accounts", timeout=120)
    istochnik = SOURCE_MANUAL if actor is not None else SOURCE_MAIL_SYNC
    na_servere = {}
    for a in data.get("accounts") or []:
        adres = str(a.get("address") or "").strip().lower()
        if adres:
            na_servere[adres] = a

    for domen in data.get("domains") or []:
        domen = str(domen).strip().lower()
        if domen and DOMAIN.match(domen) and repo.domen_po_imeni(db, domen) is None:
            repo.dobavit(db, MailDomain(server_id=server.id, domain=domen))

    added, linked = [], []
    for adres, a in sorted(na_servere.items()):
        kvota = a.get("quota_mb")
        kvota = int(kvota) if isinstance(kvota, (int, float)) and kvota > 0 else None
        box = repo.yashchik_po_adresu(db, adres)
        if box is None:
            box = repo.dobavit(db, _novyy_yashchik(server, adres, kvota=kvota))
            _zapisat(db, actor, audit_service.ACTION_MAILBOX_CREATED, box,
                     after="imported from the mail server", source=istochnik)
            added.append(adres)
        elif box.server_id is None:
            # Тот же адрес был заведён внешним ящиком: теперь он наш, ходим к нему мастером.
            svezhiy = _novyy_yashchik(server, adres, kvota=kvota)
            for pole in ("imap_host", "imap_port", "smtp_host", "smtp_port", "server_id", "quota_mb"):
                setattr(box, pole, getattr(svezhiy, pole))
            linked.append(adres)
        elif box.server_id == server.id and box.quota_mb != kvota:
            box.quota_mb = kvota
    missing = sorted(b.address for b in repo.yashchiki_servera(db, server.id)
                     if b.address.lower() not in na_servere)

    nuzhnye = set()
    for a in data.get("aliases") or []:
        s, d = str(a.get("source") or "").strip().lower(), str(a.get("destination") or "").strip().lower()
        if s and d:
            nuzhnye.add((s, d))
    est = {(a.source.lower(), a.destination.lower()): a for a in repo.psevdonimy(db, server.id)}
    dobavleno = 0
    for s, d in sorted(nuzhnye - set(est)):
        repo.dobavit(db, MailAlias(server_id=server.id, source=s, destination=d))
        dobavleno += 1
    ubrano = 0
    for para in sorted(set(est) - nuzhnye):
        repo.udalit(db, est[para])
        ubrano += 1
    db.flush()
    return {
        "added": added,
        "linked": linked,
        "missing": missing,
        "aliases": {"added": dobavleno, "removed": ubrano, "total": len(nuzhnye)},
    }


def aktivnye_lyudi(db: Session) -> list[User]:
    """Кому вообще слать о новой почте: только работающие сотрудники."""
    return users_repo.list_staff(db, STATUS_ACTIVE)
