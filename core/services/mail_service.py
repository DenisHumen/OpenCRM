"""Почта: ящики фирмы, синхронизация, отправка и попадание писем в общую ленту.

Главное правило модуля: переписка с клиентом не заводит собственную историю.
Письмо появилось — в ленте клиента (`client_notes`, kind='email') появляется
запись, ровно такая же по природе, как звонок или встреча. Менеджер открывает
карточку и видит один разговор, а не два списка рядом.
"""

import logging

from sqlalchemy.orm import Session

from core import exceptions as errors
from core import references
from core.services import audit_service
from core.security import secretbox
from core.services.mail_transport import (
    FetchedMessage,
    ImapSmtpTransport,
    MailTransport,
    MailTransportError,
    OutgoingMessage,
)
from core.utils import is_valid_email, now_utc
from database.models import ClientNote, MailAccount, MailMessage, User
from database.models.mail import DIRECTION_IN, DIRECTION_OUT, MESSAGE_ID_LENGTH
from database.repositories import mail as mail_repo

logger = logging.getLogger("opencrm.mail")

SECRET_PURPOSE = "mail-account-password"
# Сколько текста письма уезжает в ленту. Лента — это обзор разговора: в ней
# листают глазами, а не читают договоры. Полный текст лежит в mail_messages и
# открывается в карточке письма.
FEED_BODY_LIMIT = 2000


# --- ящики ---

def get_account(db: Session, account_id: int) -> MailAccount:
    account = mail_repo.get_account(db, account_id)
    if account is None:
        raise errors.NotFoundError("Mail account not found", code="mail_account_not_found")
    return account


def create_account(db: Session, data: dict) -> MailAccount:
    address = (data.get("address") or "").strip()
    if not is_valid_email(address):
        raise errors.ValidationError("A valid mailbox address is required", code="bad_address")
    account = MailAccount(
        title=(data.get("title") or "").strip() or address,
        address=address,
        imap_host=(data.get("imap_host") or "").strip(),
        imap_port=int(data.get("imap_port") or 993),
        imap_ssl=bool(data.get("imap_ssl", True)),
        smtp_host=(data.get("smtp_host") or "").strip(),
        smtp_port=int(data.get("smtp_port") or 465),
        smtp_ssl=bool(data.get("smtp_ssl", True)),
        login=(data.get("login") or "").strip() or address,
        is_active=bool(data.get("is_active", True)),
    )
    _set_password(account, data.get("password"))
    db.add(account)
    db.flush()
    return account


def update_account(db: Session, account_id: int, data: dict) -> MailAccount:
    account = get_account(db, account_id)
    for text_field in ("title", "imap_host", "smtp_host", "login"):
        if data.get(text_field) is not None:
            setattr(account, text_field, data[text_field].strip())
    if data.get("address") is not None:
        address = data["address"].strip()
        if not is_valid_email(address):
            raise errors.ValidationError("A valid mailbox address is required", code="bad_address")
        account.address = address
    for int_field in ("imap_port", "smtp_port"):
        if data.get(int_field) is not None:
            setattr(account, int_field, int(data[int_field]))
    for flag in ("imap_ssl", "smtp_ssl", "is_active"):
        if data.get(flag) is not None:
            setattr(account, flag, bool(data[flag]))
    # Пароль трогаем только если его прислали: форма редактирования не знает
    # текущего значения и присылает пустое поле — это «не менять», а не «стереть».
    if data.get("password"):
        _set_password(account, data["password"])
    account.updated_at = now_utc()
    return account


def delete_account(db: Session, account_id: int, actor: User) -> None:
    """Убрать ящик. В журнал — как удаление чего угодно другого.

    Ящик — это чужой сервер, логин и пароль; его исчезновение означает, что
    почта перестала забираться и уходить. Молчание журнала здесь превращает
    «письма не доходят» в загадку на неделю.
    """
    account = get_account(db, account_id)
    label, account_id_before = account.address or account.title, account.id
    db.delete(account)
    db.flush()
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_MAILBOX,
        entity_id=account_id_before,
        entity_label=label,
    )


def _set_password(account: MailAccount, password: str | None) -> None:
    value = (password or "").strip()
    account.password_encrypted = secretbox.encrypt(value, SECRET_PURPOSE) if value else None


def account_password(account: MailAccount) -> str:
    """Расшифрованный пароль. Дальше сервиса не уходит — только в транспорт."""
    if not account.password_encrypted:
        raise errors.ValidationError(
            "Mailbox password is not set", code="mail_password_missing"
        )
    try:
        return secretbox.decrypt(account.password_encrypted, SECRET_PURPOSE)
    except secretbox.SecretBoxError as exc:
        # Так выглядит смена OPENCRM_SECRET_KEY: база на месте, ключа к ней нет.
        # Текст ошибки без подробностей — в него не должно попасть ничего от пароля.
        raise errors.ValidationError(
            "Mailbox password cannot be decrypted with the current OPENCRM_SECRET_KEY",
            code="mail_password_undecryptable",
        ) from exc


# --- транспорт ---

def _default_factory(account: MailAccount) -> MailTransport:
    return ImapSmtpTransport(
        address=account.address,
        login=account.login or account.address,
        password=account_password(account),
        imap_host=account.imap_host,
        imap_port=account.imap_port,
        imap_ssl=account.imap_ssl,
        smtp_host=account.smtp_host,
        smtp_port=account.smtp_port,
        smtp_ssl=account.smtp_ssl,
    )


# Шов для тестов и для будущего второго транспорта (тот же Gmail API): всё, что
# ходит в сеть, создаётся здесь и только здесь.
_transport_factory = _default_factory


def set_transport_factory(factory) -> None:
    global _transport_factory
    _transport_factory = factory


def reset_transport_factory() -> None:
    global _transport_factory
    _transport_factory = _default_factory


def make_transport(account: MailAccount) -> MailTransport:
    return _transport_factory(account)


# --- проверка соединения ---

def check_account(db: Session, account_id: int) -> dict:
    account = get_account(db, account_id)
    try:
        make_transport(account).check()
    except MailTransportError as exc:
        _remember_error(account, str(exc))
        db.flush()
        return {"ok": False, "error": account.last_error}
    account.last_error = None
    account.last_error_at = None
    db.flush()
    return {"ok": True, "error": None}


# --- синхронизация ---

def sync_account(db: Session, account_id: int) -> dict:
    """Забирает новые письма, привязывает к клиентам и пишет их в ленту.

    Повторный запуск на тех же письмах ничего не меняет: ключ идемпотентности —
    Message-ID, и до создания записи ленты дело просто не доходит.
    """
    account = get_account(db, account_id)
    if not account.is_active:
        raise errors.ValidationError("Mailbox is switched off", code="mail_account_inactive")

    transport = make_transport(account)
    try:
        fetched = transport.fetch(since_uid=mail_repo.last_uid(db, account.id))
    except MailTransportError as exc:
        _remember_error(account, str(exc))
        db.flush()
        raise errors.ValidationError(account.last_error or "Sync failed", code="mail_sync_failed") from exc

    stored, linked, skipped, broken = 0, 0, 0, 0
    for item in fetched:
        # Каждое письмо — под своей точкой отката. Без неё одно битое письмо
        # роняло весь заход: исключение откатывало уже сохранённые письма из той
        # же пачки, `last_uid` не двигался, и следующая синхронизация тянула ту
        # же пачку и падала так же. Ящик вставал насовсем, а письма до него —
        # уже сохранённые и целые — исчезали.
        #
        # Проверено: пачка из двух писем, второе без даты отправки → в базе не
        # оставалось ни одного, ответ 500.
        try:
            with db.begin_nested():
                message = store_incoming(db, account, item)
        except Exception:
            # Письмо, которое мы не смогли разобрать, не должно останавливать
            # почту фирмы. Считаем его и идём дальше — счётчик виден в ответе, и
            # по нему видно, что разбираться есть с чем.
            broken += 1
            logger.exception("ящик %s: письмо не удалось сохранить", account.address)
            continue
        if message is None:
            skipped += 1
            continue
        stored += 1
        if message.client_id is not None:
            linked += 1

    account.last_sync_at = now_utc()
    account.last_error = None
    account.last_error_at = None
    db.flush()
    return {
        "fetched": len(fetched),
        "stored": stored,
        "linked": linked,
        "skipped": skipped,
        # Битые письма считаем отдельно от пропущенных: «уже было» и «не смогли
        # разобрать» — разные вещи, и вторая требует, чтобы на неё посмотрели.
        "broken": broken,
    }


def store_incoming(db: Session, account: MailAccount, item: FetchedMessage) -> MailMessage | None:
    """Сохраняет входящее письмо. `None` — письмо уже было, ничего не изменилось."""
    message_id = (item.message_id or "").strip()[:MESSAGE_ID_LENGTH]
    if not message_id:
        return None
    if mail_repo.find_by_message_id(db, message_id) is not None:
        return None

    # Сопоставление с клиентом идёт по адресу отправителя — и только сопоставление.
    # Клиента из письма НЕ создаём: в ящик фирмы валятся рассылки, спам, счета от
    # хостера и ответы роботов вида noreply@. Автосоздание превратило бы список
    # клиентов в свалку адресов, из которой настоящих пришлось бы выбирать руками,
    # а «клиент» — это решение человека, а не факт получения письма.
    client = mail_repo.find_client_by_email(db, item.from_addr)

    # К заявке входящее письмо само не привязывается. У клиента их бывает
    # несколько сразу, и «взять последнюю открытую» — это угадывание: письмо про
    # старый заказ ляжет в ленту нового и собьёт картину. Клиент — факт (адрес
    # совпал), заявка — решение, и его принимает человек.
    message = MailMessage(
        account_id=account.id,
        uid=item.uid,
        message_id=message_id,
        direction=DIRECTION_IN,
        subject=item.subject[:500],
        body_text=item.body_text,
        body_html=item.body_html,
        from_addr=item.from_addr[:320],
        to_addrs=", ".join(item.to_addrs),
        sent_at=item.sent_at,
        has_attachments=item.has_attachments,
        client_id=client.id if client else None,
        is_read=False,
    )
    db.add(message)
    db.flush()
    _add_feed_entry(db, message, author_id=None)
    return message


# --- отправка ---

def send_message(
    db: Session,
    author: User,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    account_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
) -> MailMessage:
    recipients = [addr.strip() for addr in to_addrs if addr and addr.strip()]
    if not recipients:
        raise errors.ValidationError("At least one recipient is required", code="no_recipients")
    for addr in recipients:
        if not is_valid_email(addr):
            raise errors.ValidationError(f"Bad recipient address: {addr}", code="bad_recipient")
    if not body_text.strip():
        raise errors.ValidationError("Message body is required", code="body_required")

    # Ссылки проверяем ДО отправки. Письмо уходит в SMTP раньше вставки, и
    # несуществующая заявка роняла запрос уже ПОСЛЕ доставки: адресат письмо
    # получил, в базе не осталось ничего, человек видит 500 и жмёт «отправить»
    # снова — клиенту приходит второе письмо.
    client_id = references.client(db, client_id)
    deal_id = references.deal(db, deal_id)

    account = get_account(db, account_id) if account_id else mail_repo.first_active_account(db)
    if account is None:
        raise errors.ValidationError("No mailbox is configured", code="no_mail_account")
    if not account.is_active:
        raise errors.ValidationError("Mailbox is switched off", code="mail_account_inactive")

    try:
        sent = make_transport(account).send(
            OutgoingMessage(to_addrs=recipients, subject=subject.strip(), body_text=body_text)
        )
    except MailTransportError as exc:
        _remember_error(account, str(exc))
        db.flush()
        raise errors.ValidationError(account.last_error or "Send failed", code="mail_send_failed") from exc

    # Адресат мог не значиться клиентом — тогда письмо остаётся непривязанным,
    # ровно как входящее от незнакомого адреса.
    if client_id is None:
        matched = mail_repo.find_client_by_email(db, recipients[0])
        client_id = matched.id if matched else None

    message = MailMessage(
        account_id=account.id,
        uid=None,
        message_id=sent.message_id[:MESSAGE_ID_LENGTH],
        direction=DIRECTION_OUT,
        subject=sent.subject[:500],
        body_text=sent.body_text,
        body_html="",
        from_addr=account.address[:320],
        to_addrs=", ".join(recipients),
        sent_at=sent.sent_at,
        has_attachments=False,
        client_id=client_id,
        deal_id=deal_id,
        is_read=True,  # своё письмо читать не надо, его только что написали
    )
    db.add(message)
    db.flush()
    _add_feed_entry(db, message, author_id=author.id)
    return message


def mark_read(db: Session, message_id: int, is_read: bool = True) -> MailMessage:
    message = mail_repo.get_message(db, message_id)
    if message is None:
        raise errors.NotFoundError("Message not found", code="mail_message_not_found")
    message.is_read = is_read
    db.flush()
    return message


# --- лента ---

def _add_feed_entry(db: Session, message: MailMessage, author_id: int | None) -> ClientNote | None:
    """Запись в общую ленту клиента.

    Письмо без клиента записи не порождает: лента принадлежит карточке, а
    непривязанное письмо ничьё. Оно лежит в списке писем и ждёт, пока адрес
    появится у какого-нибудь клиента.

    `happened_at` — момент отправки письма, а не синхронизации. Иначе вся почта,
    забранная одним заходом, встала бы в ленте единым столбиком «сегодня», и
    порядок разговора потерялся бы.
    """
    if message.client_id is None:
        return None
    note = ClientNote(
        client_id=message.client_id,
        # Заявка необязательна: переписка бывает и до неё, и вообще без неё.
        # Указана — письмо видно и в ленте заявки, а не только клиента.
        deal_id=message.deal_id,
        # Входящее письмо автора не имеет: его никто из сотрудников не писал.
        # NULL здесь честнее, чем «автор — тот, кто нажал синхронизацию». Это
        # один из двух законных случаев пустого автора во всей системе
        # (``SOURCE_MAIL_SYNC`` в ``database/models/audit.py``); у исходящего
        # письма автор есть всегда — его писал живой человек.
        author_id=author_id,
        kind="email",
        direction=message.direction,
        body=_feed_body(message),
        happened_at=message.sent_at,
    )
    db.add(note)
    db.flush()
    return note


def _feed_body(message: MailMessage) -> str:
    subject = message.subject.strip() or "(без темы)"
    text = (message.body_text or "").strip()
    if len(text) > FEED_BODY_LIMIT:
        text = text[:FEED_BODY_LIMIT].rstrip() + "…"
    return f"{subject}\n\n{text}".strip()


def _remember_error(account: MailAccount, text: str) -> None:
    """Последняя ошибка ящика — чтобы root видел её в интерфейсе, а не в логе.

    В текст ошибки пароль не попадает: сюда приходит сообщение транспорта
    («SMTP: 535 authentication failed»), а сам пароль не логируется нигде —
    ни здесь, ни в `mail_transport`.
    """
    account.last_error = text[:500]
    account.last_error_at = now_utc()
    logger.warning("mail account %s: %s", account.address, account.last_error)
