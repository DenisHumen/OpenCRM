"""Почта: ящики фирмы, синхронизация, отправка и попадание писем в общую ленту.

Главное правило модуля: переписка с клиентом не заводит собственную историю.
Письмо появилось — в ленте клиента (`client_notes`, kind='email') появляется
запись, ровно такая же по природе, как звонок или встреча. Менеджер открывает
карточку и видит один разговор, а не два списка рядом.
"""

import logging
from datetime import datetime, timedelta

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
from database.models.mail import (
    ADDRESS_LENGTH,
    DIRECTION_IN,
    DIRECTION_OUT,
    HOST_LENGTH,
    MESSAGE_ID_DIGEST_PREFIX,
    MESSAGE_ID_LENGTH,
    TITLE_LENGTH,
    message_id_key,
)
from database.repositories import deals as deals_repo
from database.repositories import mail as mail_repo
from database.session import tochka_otkata

logger = logging.getLogger("opencrm.mail")

SECRET_PURPOSE = "mail-account-password"
# Сколько текста письма уезжает в ленту. Лента — это обзор разговора: в ней
# листают глазами, а не читают договоры. Полный текст лежит в mail_messages и
# открывается в карточке письма.
FEED_BODY_LIMIT = 2000

#: Сколько адресатов допускается в одном письме.
#:
#: Потолок нужен по двум причинам, и вторая тяжелее. Первая: переписка — это
#: разговор, а письмо на три тысячи адресов — рассылка, которую почтовый сервер
#: всё равно отвергнет или посчитает спамом. Вторая: список получателей лежит в
#: колонке TEXT, а TEXT в MySQL — 65 535 **байт**; три тысячи адресов туда не
#: помещаются, и в нестрогом режиме список обрежется молча — письмо в базе
#: окажется адресовано не тем, кому ушло.
MAX_RECIPIENTS = 50

#: Насколько письмо вправе опережать наши часы.
#:
#: `sent_at` приезжает из заголовка `Date:`, а его пишет отправитель — то есть
#: это единственное время в системе, которое выбирает не она. Письмо с датой
#: `9999-01-01` встаёт наверху ленты клиента и остаётся там навсегда, закрывая
#: собой всё, что действительно происходило.
#:
#: Час — это расхождение часов, а не ошибка: у отправителя они бывают сбиты на
#: минуты. Всё, что дальше, — уже не «спешат часы», и такому письму мы ставим
#: время синхронизации, ровно как письму без разбираемой даты
#: (`mail_transport.header_date_to_utc`).
FUTURE_TOLERANCE = timedelta(hours=1)


# --- ящики ---

def get_account(db: Session, account_id: int) -> MailAccount:
    account = mail_repo.get_account(db, account_id)
    if account is None:
        raise errors.NotFoundError("Mail account not found", code="mail_account_not_found")
    return account


#: Поле ящика → его потолок, имя в ответе и код отказа.
#:
#: Потолки взяты из ширины колонок (`database/models/mail.py`), а не выбраны
#: заново: длинное название и длинный хост доезжали до вставки как есть, а
#: строка длиннее колонки роняет операцию отказом базы — вместо внятного
#: «слишком длинно» человек получал бы пятисотку на настройке ящика.
_ACCOUNT_FIELDS = (
    ("title", TITLE_LENGTH, "Title", "title_too_long"),
    ("imap_host", HOST_LENGTH, "IMAP host", "host_too_long"),
    ("smtp_host", HOST_LENGTH, "SMTP host", "host_too_long"),
    ("login", ADDRESS_LENGTH, "Login", "login_too_long"),
)


def create_account(db: Session, data: dict) -> MailAccount:
    address = _clean_address(data.get("address"))
    fields = {
        name: _clean_field(data.get(name), limit, label=label, code=code)
        for name, limit, label, code in _ACCOUNT_FIELDS
    }
    account = MailAccount(
        # Название по умолчанию — адрес, но колонка названия уже колонки адреса,
        # и длинный адрес обязан обрезаться: это подпись для списка, а не данные.
        title=fields["title"] or address[:TITLE_LENGTH],
        address=address,
        imap_host=fields["imap_host"],
        imap_port=_clean_port(data.get("imap_port"), 993),
        imap_ssl=bool(data.get("imap_ssl", True)),
        smtp_host=fields["smtp_host"],
        smtp_port=_clean_port(data.get("smtp_port"), 465),
        smtp_ssl=bool(data.get("smtp_ssl", True)),
        login=fields["login"] or address,
        is_active=bool(data.get("is_active", True)),
    )
    _set_password(account, data.get("password"))
    db.add(account)
    db.flush()
    return account


def update_account(db: Session, account_id: int, data: dict) -> MailAccount:
    account = get_account(db, account_id)
    for name, limit, label, code in _ACCOUNT_FIELDS:
        if data.get(name) is not None:
            setattr(account, name, _clean_field(data[name], limit, label=label, code=code))
    if data.get("address") is not None:
        account.address = _clean_address(data["address"])
    for int_field, default in (("imap_port", 993), ("smtp_port", 465)):
        if data.get(int_field) is not None:
            setattr(account, int_field, _clean_port(data[int_field], default))
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


def _clean_field(value: str | None, limit: int, *, label: str, code: str) -> str:
    """Строка настроек ящика: без краёв и не длиннее своей колонки.

    Отказ, а не обрезание: обрезанный хост — это другой хост, и ящик после
    такого «сохранения» просто перестал бы забирать почту, не сказав почему.
    """
    text = (value or "").strip()
    if len(text) > limit:
        raise errors.ValidationError(f"{label} is too long (max {limit} characters)", code=code)
    return text


def _clean_address(value: str | None) -> str:
    """Адрес ящика. Длину проверяем ДО вида: `is_valid_email` её не смотрит,
    и адрес на пять тысяч знаков спокойно проходил как «валидный»."""
    address = (value or "").strip()
    if len(address) > ADDRESS_LENGTH:
        raise errors.ValidationError(
            f"Mailbox address is too long (max {ADDRESS_LENGTH} characters)",
            code="address_too_long",
        )
    if not is_valid_email(address):
        raise errors.ValidationError("A valid mailbox address is required", code="bad_address")
    return address


def _clean_port(value, default: int) -> int:
    """Номер порта. Тот же разговор, что и с длиной строки, только про число:
    в колонке INT, и значение вне диапазона портов уронило бы вставку на MySQL,
    а до неё — сам вызов `socket`."""
    if value is None or value == "":
        return default
    port = int(value)
    if not 1 <= port <= 65535:
        raise errors.ValidationError("Port must be between 1 and 65535", code="bad_port")
    return port


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
        _remember_error(db, account, str(exc))
        return {"ok": False, "error": account.last_error}
    account.last_error = None
    account.last_error_at = None
    db.flush()
    return {"ok": True, "error": None}


# --- синхронизация ---

def sync_account(db: Session, account_id: int) -> dict:
    """Забирает новые письма, привязывает к клиентам и пишет их в ленту.

    Повторный запуск на тех же письмах ничего не меняет: ключ идемпотентности —
    пара (ящик, Message-ID), и до создания записи ленты дело просто не доходит.
    Именно пара, а не один Message-ID: то же письмо в соседнем ящике фирмы —
    это его письмо, со своим UID и своим местом в его выборке.
    """
    account = get_account(db, account_id)
    if not account.is_active:
        raise errors.ValidationError("Mailbox is switched off", code="mail_account_inactive")

    transport = make_transport(account)
    try:
        fetched = transport.fetch(since_uid=mail_repo.last_uid(db, account.id))
    except MailTransportError as exc:
        _remember_error(db, account, str(exc))
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
            with tochka_otkata(db):
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
    """Сохраняет входящее письмо. `None` — письмо уже было, ничего не изменилось.

    «Уже было» считается по паре (ящик, Message-ID). То же письмо, доставленное
    на другой ящик фирмы, — это другая строка: у каждого ящика своё зеркало и
    свой `last_uid`.
    """
    message_id = message_id_key(item.message_id or "")
    if not message_id:
        return None
    if mail_repo.find_by_message_id(db, account.id, message_id) is not None:
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
        from_addr=item.from_addr[:ADDRESS_LENGTH],
        to_addrs=", ".join(item.to_addrs),
        sent_at=_sent_at_not_ahead_of_us(item.sent_at),
        has_attachments=item.has_attachments,
        # Цепочку входящего сохраняем ПРИ СИНХРОНИЗАЦИИ, а не собираем потом:
        # взять её позже неоткуда — заголовки живут в самом письме, а тело мы
        # храним без них. Не сохрани мы её здесь, ответ на третье письмо
        # переписки унёс бы ссылку только на второе, и у собеседника ветка
        # распалась бы надвое.
        in_reply_to=item.in_reply_to[:MESSAGE_ID_LENGTH],
        # Обрезаем ХВОСТОМ, а не началом: клиенты собирают ветку по последним
        # ссылкам. Без обрезки письмо из очень длинной ветки (тикет-система,
        # рассылочный лист) даёт строку длиннее 65 535 байт, MySQL отвечает
        # 1406 «Data too long», письмо откатывается и не попадает в CRM
        # НИКОГДА — `last_uid` уезжает вперёд по соседям, и второго захода за
        # ним не будет.
        references=_hvost_vetki(item.references),
        client_id=client.id if client else None,
        is_read=False,
    )
    db.add(message)
    db.flush()
    _add_feed_entry(db, message, author_id=None)
    return message


def _sent_at_not_ahead_of_us(sent_at: datetime) -> datetime:
    """Время письма, не забегающее вперёд наших часов.

    Проверка стоит здесь, а не в разборе заголовка, нарочно: разбор — дело
    транспорта, а транспортов будет больше одного (тот же Gmail API отдаёт своё
    время своим полем). Через `store_incoming` проходит любое входящее письмо,
    каким бы путём оно ни пришло, и это единственное место, где потолок нельзя
    обойти, добавив второй транспорт.

    Письмо без времени отправки (`None`) до базы не доходит и здесь: сравнение
    бросит `TypeError`, синхронизация посчитает письмо битым и пойдёт дальше —
    ровно то, что нужно, потому что пустое `sent_at` колонка не примет.
    """
    now = now_utc()
    if sent_at > now + FUTURE_TOLERANCE:
        logger.warning(
            "письмо с датой отправки из будущего (%s) — ставим время синхронизации", sent_at
        )
        return now
    return sent_at


# --- отправка ---

#: Сколько ссылок цепочки уносим в ответ.
#:
#: У переписки на сотню писем `References` разрастается до килобайтов, а
#: почтовые клиенты собирают ветку по ПОСЛЕДНИМ ссылкам. Двадцать — с запасом
#: на любую живую переписку и без риска, что заголовок перестанет влезать.
MAX_REFERENCES = 20


def _hvost_vetki(references: str) -> str:
    """Последние `MAX_REFERENCES` ссылок ветки. Столько же уносит и ответ."""
    return " ".join((references or "").split()[-MAX_REFERENCES:])


def _pismo_dlya_otveta(db: Session, message_id: int) -> MailMessage:
    """Письмо, на которое отвечают. Нет его — внятный отказ, а не тихий ответ
    в никуда: без исходного письма ответ не встанет в переписку, и человек
    узнает об этом только от собеседника."""
    pismo = mail_repo.get_message(db, message_id)
    if pismo is None:
        raise errors.NotFoundError("Message not found", code="mail_message_not_found")
    return pismo


def _tsepochka(pismo: MailMessage | None) -> tuple[str, str]:
    """`In-Reply-To` и `References` для ответа на это письмо.

    По RFC 5322 ответ ссылается на само письмо (`In-Reply-To`) и несёт всю
    ветку до него (`References` исходного плюс его собственный `Message-ID`).
    Одного `In-Reply-To` мало: у переписки длиннее двух писем клиенты собирают
    ветку именно по `References`, и без неё она распадается на куски.
    """
    if pismo is None:
        return "", ""
    svoy = (pismo.message_id or "").strip()
    if not svoy:
        return "", ""
    # В колонке лежит КЛЮЧ ИДЕМПОТЕНТНОСТИ, а не всегда сам заголовок: у письма
    # с Message-ID длиннее 320 знаков (рассылка, тикет-система) туда кладётся
    # отпечаток `opencrm-sha256:…`. Поставь мы его в `In-Reply-To` — собеседник
    # получил бы ссылку на письмо, которого у него нет, то есть ответ снова
    # встал бы отдельно, только теперь молча. Заодно наш внутренний ключ уехал
    # бы в заголовки чужого сервера.
    #
    # Настоящего заголовка у нас в этом случае нет нигде, и выдумать его нельзя.
    # Честный ответ — не ставить заголовков вовсе: письмо придёт отдельным, как
    # и раньше, но без вранья в цепочке.
    if svoy.startswith(MESSAGE_ID_DIGEST_PREFIX):
        return "", ""
    if not svoy.startswith("<"):
        svoy = f"<{svoy}>"
    bylo = (pismo.references or "").split()
    vetka = [*bylo, svoy][-MAX_REFERENCES:]
    return svoy, " ".join(vetka)


def send_message(
    db: Session,
    author: User,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    account_id: int | None = None,
    client_id: int | None = None,
    deal_id: int | None = None,
    reply_to_id: int | None = None,
) -> MailMessage:
    recipients = [addr.strip() for addr in to_addrs if addr and addr.strip()]
    if not recipients:
        raise errors.ValidationError("At least one recipient is required", code="no_recipients")
    if len(recipients) > MAX_RECIPIENTS:
        raise errors.ValidationError(
            f"Too many recipients (max {MAX_RECIPIENTS})", code="too_many_recipients"
        )
    for addr in recipients:
        if len(addr) > ADDRESS_LENGTH:
            raise errors.ValidationError(
                f"Recipient address is too long (max {ADDRESS_LENGTH} characters)",
                code="recipient_too_long",
            )
        if not is_valid_email(addr):
            raise errors.ValidationError(f"Bad recipient address: {addr}", code="bad_recipient")
    if not body_text.strip():
        raise errors.ValidationError("Message body is required", code="body_required")

    # Ссылки проверяем ДО отправки. Письмо уходит в SMTP раньше вставки, и
    # несуществующая заявка роняла запрос уже ПОСЛЕ доставки: адресат письмо
    # получил, в базе не осталось ничего, человек видит 500 и жмёт «отправить»
    # снова — клиенту приходит второе письмо.
    # Письмо, на которое отвечаем, читаем ПЕРВЫМ делом: от него зависят и
    # привязки, и ящик, и цепочка. Наследование ПОСЛЕ проверки ссылок было
    # ошибкой — унаследованный клиент не проходил `_agree_client_and_deal`, и
    # ответ мог унести заявку чужого клиента.
    otvechaem = _pismo_dlya_otveta(db, reply_to_id) if reply_to_id else None
    if otvechaem is not None:
        client_id = client_id or otvechaem.client_id
        deal_id = deal_id or otvechaem.deal_id

    client_id = references.client(db, client_id)
    deal_id = references.deal(db, deal_id)
    client_id = _agree_client_and_deal(db, client_id, deal_id)

    # Отвечаем ИЗ ТОГО ЯЩИКА, куда письмо пришло, если ящик не назвали явно.
    # Ответ с другого адреса приходит собеседнику от незнакомца и в переписку
    # не встаёт — а интерфейс мог и не подставить ящик: ручка открыта не только
    # ему.
    if account_id is None and otvechaem is not None:
        account_id = otvechaem.account_id
    account = get_account(db, account_id) if account_id else mail_repo.first_active_account(db)
    if account is None:
        raise errors.ValidationError("No mailbox is configured", code="no_mail_account")
    if not account.is_active:
        raise errors.ValidationError("Mailbox is switched off", code="mail_account_inactive")

    in_reply_to, vetka = _tsepochka(otvechaem)

    try:
        sent = make_transport(account).send(
            OutgoingMessage(
                to_addrs=recipients,
                subject=subject.strip(),
                body_text=body_text,
                in_reply_to=in_reply_to,
                references=vetka,
            )
        )
    except MailTransportError as exc:
        _remember_error(db, account, str(exc))
        raise errors.ValidationError(account.last_error or "Send failed", code="mail_send_failed") from exc

    # Адресат мог не значиться клиентом — тогда письмо остаётся непривязанным,
    # ровно как входящее от незнакомого адреса.
    if client_id is None:
        matched = mail_repo.find_client_by_email(db, recipients[0])
        client_id = matched.id if matched else None

    message = MailMessage(
        account_id=account.id,
        uid=None,
        message_id=message_id_key(sent.message_id),
        direction=DIRECTION_OUT,
        subject=sent.subject[:500],
        body_text=sent.body_text,
        body_html="",
        from_addr=account.address[:ADDRESS_LENGTH],
        to_addrs=", ".join(recipients),
        sent_at=sent.sent_at,
        has_attachments=False,
        in_reply_to=in_reply_to[:MESSAGE_ID_LENGTH],
        references=vetka,
        client_id=client_id,
        deal_id=deal_id,
        is_read=True,  # своё письмо читать не надо, его только что написали
    )
    db.add(message)
    db.flush()
    _add_feed_entry(db, message, author_id=author.id)

    # Часть адресатов отвергнута — письмо ушло остальным, и запись о нём уже
    # есть. Молчать нельзя: человек будет ждать ответа от того, кто письма не
    # получал. Пишем беду в ящик — там её видно на экране почтовых ящиков — и
    # отдаём наверх, чтобы отправляющий увидел её сразу.
    if sent.refused:
        _remember_error(
            db, account, "не доставлено: " + ", ".join(sent.refused)
        )
        message.refused = list(sent.refused)
    return message


def _agree_client_and_deal(db: Session, client_id: int | None, deal_id: int | None) -> int | None:
    """Клиент и заявка письма обязаны сходиться друг с другом.

    Существование каждого по отдельности уже проверено (`core/references.py`), а
    вот их согласие — нет: письмо Алисе записывалось на заявку Боба. В ленте
    заявки после этого лежит переписка, которой в ней не было, и выглядит она
    совершенно обычно — заметить подмену не по чему.

    Проверка и отказ — те же, что у звонка (`telephony_service.attach_deal`,
    код `deal_other_client`). Одинаковая беда обязана отвечать одинаково, иначе
    интерфейсу приходится разбирать два кода про одно и то же.

    Заявка указана, клиент нет — берём клиента у заявки. Это не угадывание:
    заявку выбрал человек, а клиент у неё ровно один. Заодно это отменяет
    сопоставление по адресу получателя, которое иначе могло привязать письмо к
    клиенту, к этой заявке отношения не имеющему.
    """
    if deal_id is None:
        return client_id
    deal = deals_repo.get(db, deal_id)
    if deal is None:
        # Сюда не попасть: `references.deal` выше уже ответил бы 404. Но
        # полагаться на порядок соседних строк — способ однажды получить
        # AttributeError вместо внятного отказа.
        raise errors.NotFoundError("Deal not found", code="deal_not_found")
    if client_id is not None and deal.client_id != client_id:
        raise errors.ValidationError("Deal belongs to another client", code="deal_other_client")
    return client_id if client_id is not None else deal.client_id


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


def _remember_error(db: Session, account: MailAccount, text: str) -> None:
    """Последняя ошибка ящика — чтобы root видел её в интерфейсе, а не в логе.

    Запись сразу фиксируется (`mail_repo.save_last_error`), и это не
    перестраховка, а условие того, чтобы обещание первой строки выполнялось.
    Синхронизация и отправка после этой записи бросают `ValidationError`, а
    исключение откатывает транзакцию запроса вместе с ней. Пока фиксации не
    было, беда выглядела так: почта не забирается неделю, а в настройках у
    ящика чисто — потому что каждая попытка сама же и стирала свой след.
    Проверялось это, судя по всему, на `check_account`, где исключения нет и
    запись доживала до конца запроса.

    В текст ошибки пароль не попадает: сюда приходит сообщение транспорта
    («SMTP: 535 authentication failed»), а сам пароль не логируется нигде —
    ни здесь, ни в `mail_transport`.
    """
    text = text[:500]
    logger.warning("mail account %s: %s", account.address, text)
    try:
        mail_repo.save_last_error(db, account, text, now_utc())
    except Exception:  # noqa: BLE001
        # Запись ошибки не должна подменить собой саму ошибку. Вызывающий сейчас
        # скажет человеку «почта не забралась, вот почему»; если вместо этого
        # улетит отказ базы, до причины никто не доберётся.
        logger.exception("ящик %s: последнюю ошибку не удалось сохранить", account.address)
