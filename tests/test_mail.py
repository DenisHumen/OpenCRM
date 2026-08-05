"""Почта: IMAP/SMTP.

Ни один тест не ходит в сеть: транспорт подменён подделкой (`FakeTransport`),
которая отдаёт заранее собранные письма. Проверяется не «работает ли imaplib», а
то, что легко сломать и трудно заметить: пароль ящика в ответах API, задвоение
ленты при повторной синхронизации, время письма и права на настройки.
"""

from datetime import datetime
from email.utils import make_msgid

import pytest
from fastapi.testclient import TestClient

from core.security import secretbox
from core.services import mail_service
from core.services.mail_transport import FetchedMessage, MailTransport, MailTransportError
from core.utils import now_utc
from database.repositories import mail as mail_repo
from database.session import SessionLocal
from tests.conftest import API, make_manager

MAIL = f"{API}/mail"
MAILBOX_PASSWORD = "s3cret-mailbox-pw"


# --- подделка транспорта ---

class FakeTransport(MailTransport):
    """Почтовый сервер, живущий в списке. Ни одного сокета."""

    inbox: list[FetchedMessage] = []
    sent: list = []
    fail_with: str | None = None

    def __init__(self, account) -> None:
        self.account = account

    def check(self) -> None:
        if FakeTransport.fail_with:
            raise MailTransportError(FakeTransport.fail_with)

    def fetch(self, since_uid=None, limit=50):
        if FakeTransport.fail_with:
            raise MailTransportError(FakeTransport.fail_with)
        return [m for m in FakeTransport.inbox if since_uid is None or (m.uid or 0) > since_uid]

    def send(self, message):
        if FakeTransport.fail_with:
            raise MailTransportError(FakeTransport.fail_with)
        sent = FetchedMessage(
            uid=None,
            # Как у настоящего транспорта: Message-ID генерируется на отправку и
            # глобально уникален. Счётчик здесь не годится — база одна на весь
            # прогон, и второй тест упёрся бы в уникальный индекс.
            message_id=make_msgid(domain="opencrm.test"),
            subject=message.subject,
            from_addr=self.account.address,
            to_addrs=list(message.to_addrs),
            sent_at=now_utc(),
            body_text=message.body_text,
        )
        FakeTransport.sent.append(sent)
        return sent


def incoming(
    uid: int,
    message_id: str,
    from_addr: str,
    sent_at: datetime,
    subject: str = "Про заказ",
    body: str = "Здравствуйте, уточните сроки.",
) -> FetchedMessage:
    return FetchedMessage(
        uid=uid,
        message_id=message_id,
        subject=subject,
        from_addr=from_addr,
        to_addrs=["office@studio.test"],
        sent_at=sent_at,
        body_text=body,
    )


@pytest.fixture(autouse=True)
def fake_transport():
    FakeTransport.inbox = []
    FakeTransport.sent = []
    FakeTransport.fail_with = None
    mail_service.set_transport_factory(FakeTransport)
    yield
    mail_service.reset_transport_factory()


@pytest.fixture()
def mail_on(root_client: TestClient):
    """Блок по умолчанию выключен — включаем его перед рабочими проверками."""
    response = root_client.post(f"{API}/modules/mail", json={"enabled": True})
    assert response.status_code == 200, response.text
    yield
    root_client.post(f"{API}/modules/mail", json={"enabled": True})


def make_account(root_client: TestClient, address: str = "office@studio.test") -> dict:
    response = root_client.post(
        f"{MAIL}/accounts",
        json={
            "title": "Общий ящик",
            "address": address,
            "imap_host": "imap.studio.test",
            "smtp_host": "smtp.studio.test",
            "password": MAILBOX_PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_client(root_client: TestClient, name: str, email: str) -> dict:
    response = root_client.post(f"{API}/clients", json={"name": name, "email": email})
    assert response.status_code == 201, response.text
    return response.json()


def feed(root_client: TestClient, client_id: int) -> list[dict]:
    return root_client.get(f"{API}/clients/{client_id}/notes?per_page=200").json()["items"]


# --- пароль ящика ---

def test_password_never_leaves_the_api(root_client, mail_on):
    """Пароль от ящика фирмы не должен появиться ни в одном ответе.

    Проверяется не поле, а весь текст ответа: утечка обычно случается не в
    задуманном поле, а в новом, которое кто-то добавил в сериализацию.
    """
    account = make_account(root_client, "leak-check@studio.test")

    responses = [
        root_client.get(f"{MAIL}/accounts"),
        root_client.patch(f"{MAIL}/accounts/{account['id']}", json={"title": "Переименовали"}),
    ]
    FakeTransport.fail_with = "SMTP: 535 authentication failed"
    responses.append(root_client.post(f"{MAIL}/accounts/{account['id']}/check"))
    FakeTransport.fail_with = None

    for response in responses:
        assert MAILBOX_PASSWORD not in response.text, response.text

    listed = root_client.get(f"{MAIL}/accounts").json()["items"]
    entry = next(item for item in listed if item["id"] == account["id"])
    assert "password" not in entry and "password_encrypted" not in entry
    assert entry["has_password"] is True, "признак «пароль задан» обязан быть"


def test_password_is_not_stored_as_plain_text(root_client, mail_on):
    """В базе лежит шифротекст, а не пароль."""
    account = make_account(root_client, "storage-check@studio.test")
    db = SessionLocal()
    try:
        from database.models import MailAccount

        row = db.get(MailAccount, account["id"])
        assert row.password_encrypted, "пароль не сохранён вовсе"
        assert MAILBOX_PASSWORD not in row.password_encrypted
        assert secretbox.decrypt(row.password_encrypted, mail_service.SECRET_PURPOSE) == MAILBOX_PASSWORD
    finally:
        db.close()


def test_password_is_not_written_to_the_log(root_client, mail_on, caplog):
    """Ошибка ящика уезжает в лог — пароля в ней быть не должно."""
    account = make_account(root_client, "log-check@studio.test")
    FakeTransport.fail_with = "IMAP: authentication failed"
    with caplog.at_level("DEBUG"):
        root_client.post(f"{MAIL}/accounts/{account['id']}/check")
    assert MAILBOX_PASSWORD not in caplog.text


def test_encrypted_password_does_not_survive_a_key_change(root_client, mail_on, monkeypatch):
    """Смена OPENCRM_SECRET_KEY делает шифротекст нечитаемым — так и задумано."""
    token = secretbox.encrypt(MAILBOX_PASSWORD, mail_service.SECRET_PURPOSE)
    assert secretbox.decrypt(token, mail_service.SECRET_PURPOSE) == MAILBOX_PASSWORD

    from config import settings as settings_module

    original = settings_module.get_settings()
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: original.model_copy(update={"secret_key": "another-secret-key"}),
    )
    monkeypatch.setattr("core.security.secretbox.get_settings", settings_module.get_settings)
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt(token, mail_service.SECRET_PURPOSE)


def test_tampered_secret_is_rejected_not_silently_decrypted():
    """Правка шифротекста в базе обязана ломать расшифровку, а не менять пароль."""
    token = secretbox.encrypt("hello", "test")
    broken = token[:-4] + ("aaaa" if token[-4:] != "aaaa" else "bbbb")
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt(broken, "test")


# --- синхронизация и лента ---

def test_incoming_from_a_known_address_lands_in_the_client_feed(root_client, mail_on):
    account = make_account(root_client, "feed@studio.test")
    client = make_client(root_client, "Брусника", "buyer@brusnika.test")

    FakeTransport.inbox = [
        incoming(11, "<in-1@remote.test>", client["email"], datetime(2026, 7, 30, 9, 15))
    ]
    synced = root_client.post(f"{MAIL}/accounts/{account['id']}/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["linked"] == 1

    entries = [n for n in feed(root_client, client["id"]) if n["kind"] == "email"]
    assert len(entries) == 1, "письмо не попало в ленту клиента"
    assert entries[0]["direction"] == "in"
    assert "Про заказ" in entries[0]["body"]


def test_second_sync_of_the_same_letter_does_not_double_the_feed(root_client, mail_on):
    """Идемпотентность по Message-ID: то же письмо — та же одна запись."""
    account = make_account(root_client, "idem@studio.test")
    client = make_client(root_client, "Повтор", "repeat@remote.test")

    FakeTransport.inbox = [
        incoming(21, "<idem-1@remote.test>", "repeat@remote.test", datetime(2026, 7, 29, 12, 0))
    ]
    first = root_client.post(f"{MAIL}/accounts/{account['id']}/sync").json()
    assert first["stored"] == 1

    # То же письмо приезжает снова под другим UID — так выглядит смена
    # UIDVALIDITY: сервер переиндексировал папку, номера поехали, Message-ID нет.
    # Именно на этом «фильтр по UID» перестаёт защищать, и работать обязан
    # Message-ID.
    FakeTransport.inbox = [
        incoming(22, "<idem-1@remote.test>", "repeat@remote.test", datetime(2026, 7, 29, 12, 0))
    ]
    second = root_client.post(f"{MAIL}/accounts/{account['id']}/sync").json()
    assert second["stored"] == 0 and second["skipped"] == 1

    entries = [n for n in feed(root_client, client["id"]) if n["kind"] == "email"]
    assert len(entries) == 1, "повторная синхронизация задвоила запись в ленте"

    messages = root_client.get(f"{MAIL}/messages?client_id={client['id']}").json()
    assert messages["total"] == 1, "письмо сохранилось дважды"


def test_letter_from_an_unknown_address_does_not_create_a_client(root_client, mail_on):
    """Рассылки и спам не должны заводить карточки клиентов."""
    account = make_account(root_client, "unknown@studio.test")
    before = root_client.get(f"{API}/clients?per_page=200").json()["total"]

    FakeTransport.inbox = [
        incoming(31, "<spam-1@ads.test>", "noreply@newsletter.test", datetime(2026, 7, 28, 8, 0))
    ]
    result = root_client.post(f"{MAIL}/accounts/{account['id']}/sync").json()
    assert result["stored"] == 1 and result["linked"] == 0

    after = root_client.get(f"{API}/clients?per_page=200").json()["total"]
    assert after == before, "письмо от неизвестного адреса завело клиента"

    stored = root_client.get(f"{MAIL}/messages?search=newsletter").json()["items"]
    assert stored and stored[0]["client_id"] is None


def test_feed_time_is_when_the_letter_was_sent_not_when_it_was_synced(root_client, mail_on):
    """Иначе почта, забранная одним заходом, встаёт в ленте одной кучей «сегодня»."""
    account = make_account(root_client, "when@studio.test")
    client = make_client(root_client, "Давний", "old@remote.test")

    sent_at = datetime(2026, 6, 1, 7, 45)
    FakeTransport.inbox = [incoming(41, "<old-1@remote.test>", "old@remote.test", sent_at)]
    root_client.post(f"{MAIL}/accounts/{account['id']}/sync")

    entry = next(n for n in feed(root_client, client["id"]) if n["kind"] == "email")
    assert entry["happened_at"].startswith("2026-06-01T07:45")
    assert not entry["happened_at"].startswith(now_utc().strftime("%Y-%m-%d"))


def test_letter_date_with_a_timezone_is_converted_to_utc():
    """Письмо из +09:00 обязано лечь в базу как UTC, иначе лента перемешается."""
    from core.services.mail_transport import header_date_to_utc

    assert header_date_to_utc("Tue, 5 Aug 2026 09:12:00 +0900") == datetime(2026, 8, 5, 0, 12)
    assert header_date_to_utc("Tue, 5 Aug 2026 09:12:00 +0000") == datetime(2026, 8, 5, 9, 12)


def test_raw_letter_is_parsed_without_network():
    """Разбор письма — чистая функция, её и проверяем прямо, без сервера."""
    from core.services.mail_transport import parse_raw_message

    raw = (
        b"From: Ivan <ivan@remote.test>\r\n"
        b"To: office@studio.test\r\n"
        b"Subject: =?utf-8?B?0J/RgNC40LLQtdGC?=\r\n"
        b"Date: Tue, 5 Aug 2026 09:12:00 +0300\r\n"
        b"Message-ID: <abc-1@remote.test>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"\xd0\xa2\xd0\xb5\xd0\xba\xd1\x81\xd1\x82\r\n"
    )
    parsed = parse_raw_message(raw, uid=7)
    assert parsed.from_addr == "ivan@remote.test"
    assert parsed.subject == "Привет"
    assert parsed.sent_at == datetime(2026, 8, 5, 6, 12)
    assert "Текст" in parsed.body_text


# --- отправка ---

def test_sent_letter_lands_in_the_feed_as_outgoing(root_client, mail_on):
    account = make_account(root_client, "out@studio.test")
    client = make_client(root_client, "Получатель", "to@remote.test")

    response = root_client.post(
        f"{MAIL}/send",
        json={
            "to": ["to@remote.test"],
            "subject": "Смета",
            "body": "Отправляю смету во вложении.",
            "account_id": account["id"],
            "client_id": client["id"],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["direction"] == "out"
    assert MAILBOX_PASSWORD not in response.text

    entries = [n for n in feed(root_client, client["id"]) if n["kind"] == "email"]
    assert len(entries) == 1 and entries[0]["direction"] == "out"
    assert entries[0]["author_id"] is not None, "у исходящего письма есть автор"


def test_sending_to_a_bad_address_is_refused(root_client, mail_on):
    account = make_account(root_client, "bad@studio.test")
    response = root_client.post(
        f"{MAIL}/send",
        json={"to": ["не-адрес"], "subject": "x", "body": "y", "account_id": account["id"]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_recipient"


# --- права и выключенный блок ---

def test_disabled_module_closes_the_api(root_client):
    """Выключенный блок отвечает 403 module_disabled, а не 404 и не тишиной."""
    off = root_client.post(f"{API}/modules/mail", json={"enabled": False})
    assert off.status_code == 200, off.text
    try:
        for method, path in (
            ("get", f"{MAIL}/accounts"),
            ("get", f"{MAIL}/messages"),
            ("post", f"{MAIL}/send"),
        ):
            response = getattr(root_client, method)(
                path, **({"json": {"to": ["a@b.test"], "body": "x"}} if method == "post" else {})
            )
            assert response.status_code == 403, (path, response.text)
            assert response.json()["error"]["code"] == "module_disabled", path
    finally:
        root_client.post(f"{API}/modules/mail", json={"enabled": True})


def test_manager_cannot_touch_mailboxes(root_client, mail_on):
    """Ящик — настройка фирмы: менеджер его не заводит, не правит и не удаляет."""
    account = make_account(root_client, "rights@studio.test")
    manager = make_manager(root_client, "mailmanager@test.local")

    forbidden = [
        manager.get(f"{MAIL}/accounts"),
        manager.post(f"{MAIL}/accounts", json={"address": "own@studio.test"}),
        manager.patch(f"{MAIL}/accounts/{account['id']}", json={"title": "Моё"}),
        manager.delete(f"{MAIL}/accounts/{account['id']}"),
        manager.post(f"{MAIL}/accounts/{account['id']}/check"),
    ]
    for response in forbidden:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "root_required"

    # Читать почту и синхронизировать её менеджеру при этом можно.
    assert manager.get(f"{MAIL}/messages").status_code == 200
    assert manager.post(f"{MAIL}/accounts/{account['id']}/sync").status_code == 200


def test_letter_sent_from_a_deal_lands_in_the_deal_feed(root_client, mail_on):
    """Письмо по заявке видно в ленте заявки, а не только у клиента.

    Ради этого лента и обобщалась: у клиента заказов бывает несколько, и «мы
    отправили смету» относится к одному из них.
    """
    account = make_account(root_client, "deal@studio.test")
    client = make_client(root_client, "Заказчик", "orders@remote.test")
    deal = root_client.post(
        f"{API}/deals", json={"title": "Ремонт витрины", "client_id": client["id"]}
    ).json()

    sent = root_client.post(
        f"{MAIL}/send",
        json={
            "to": ["orders@remote.test"],
            "subject": "Смета",
            "body": "Смета во вложении.",
            "account_id": account["id"],
            "client_id": client["id"],
            "deal_id": deal["id"],
        },
    )
    assert sent.status_code == 201, sent.text

    deal_feed = root_client.get(f"{API}/deals/{deal['id']}/feed").json()["items"]
    letters = [n for n in deal_feed if n["kind"] == "email"]
    assert len(letters) == 1, "письмо не попало в ленту заявки"
    assert letters[0]["direction"] == "out"
    assert letters[0]["deal_id"] == deal["id"]

    # и в ленте клиента оно тоже одно — а не по копии на каждый список
    client_letters = [n for n in feed(root_client, client["id"]) if n["kind"] == "email"]
    assert len(client_letters) == 1


def test_purged_client_does_not_take_the_correspondence_with_him(root_client, mail_on):
    """Клиента вычистили из корзины — письмо остаётся, но без привязки.

    CASCADE здесь стёр бы переписку фирмы вместе с карточкой, а это документы:
    по ним разбираются и после ухода клиента. Проверяем на уровне базы, потому
    что через API клиент удаляется мягко и до внешнего ключа дело не доходит.
    """
    account = make_account(root_client, "purge@studio.test")
    client = make_client(root_client, "Ушедший", "gone@remote.test")

    FakeTransport.inbox = [
        incoming(61, "<purge-1@remote.test>", "gone@remote.test", datetime(2026, 7, 26, 9, 0))
    ]
    root_client.post(f"{MAIL}/accounts/{account['id']}/sync")

    from database.models import Client, MailMessage

    db = SessionLocal()
    try:
        stored = mail_repo.find_by_message_id(db, "<purge-1@remote.test>")
        assert stored is not None and stored.client_id == client["id"]
        row_id = stored.id
        db.delete(db.get(Client, client["id"]))
        db.commit()
        db.expire_all()
        survivor = db.get(MailMessage, row_id)
        assert survivor is not None, "письмо исчезло вместе с клиентом"
        assert survivor.client_id is None
    finally:
        db.close()


def test_deleting_a_mailbox_keeps_the_client_feed(root_client, mail_on):
    """Зеркало писем уезжает с ящиком, история общения в ленте — остаётся."""
    account = make_account(root_client, "drop@studio.test")
    client = make_client(root_client, "Останется", "stays@remote.test")

    FakeTransport.inbox = [
        incoming(71, "<drop-1@remote.test>", "stays@remote.test", datetime(2026, 7, 25, 9, 0))
    ]
    root_client.post(f"{MAIL}/accounts/{account['id']}/sync")
    assert len([n for n in feed(root_client, client["id"]) if n["kind"] == "email"]) == 1

    assert root_client.delete(f"{MAIL}/accounts/{account['id']}").status_code == 200
    assert root_client.get(f"{MAIL}/messages?client_id={client['id']}").json()["total"] == 0
    assert len([n for n in feed(root_client, client["id"]) if n["kind"] == "email"]) == 1


def test_message_list_does_not_carry_bodies(root_client, mail_on):
    """Тела не должны ехать в список: письмо на сотни килобайт не редкость."""
    account = make_account(root_client, "bodies@studio.test")
    FakeTransport.inbox = [
        incoming(
            51,
            "<big-1@remote.test>",
            "someone@remote.test",
            datetime(2026, 7, 27, 10, 0),
            body="ц" * 5000,
        )
    ]
    root_client.post(f"{MAIL}/accounts/{account['id']}/sync")

    listed = root_client.get(f"{MAIL}/messages?search=someone").json()["items"]
    assert listed and "body_text" not in listed[0]

    full = root_client.get(f"{MAIL}/messages/{listed[0]['id']}").json()
    assert len(full["body_text"]) == 5000
