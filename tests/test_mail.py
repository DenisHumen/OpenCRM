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
from database.models.mail import MESSAGE_ID_LENGTH
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
    """Блок по умолчанию выключен — включаем его перед рабочими проверками.

    Переключаем только когда он и правда выключен, а не на всякий случай.
    Переключение блока пишет строку в журнал действий **даже если значение не
    изменилось** (`modules_service._write_state`), а журнал — общий на весь
    прогон: полсотни лишних записей вытесняют со страницы чужие, и соседний
    тест начинает падать не от поломки, а от нашего соседства. Так уже было —
    у `test_audit.test_switching_a_module_records_both_states` про это написана
    отдельная докстрока.
    """
    def enabled() -> bool:
        listed = root_client.get(f"{API}/modules").json()["items"]
        return next(item["enabled"] for item in listed if item["key"] == "mail")

    def turn_on() -> None:
        if not enabled():
            response = root_client.post(f"{API}/modules/mail", json={"enabled": True})
            assert response.status_code == 200, response.text

    turn_on()
    yield
    turn_on()


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


def test_a_letter_delivered_to_two_mailboxes_is_stored_for_both(root_client, mail_on):
    """Письмо в общий ящик и в копии на личный — это два письма, полученных фирмой.

    Пока `message_id` был уникален глобально, второй ящик такое письмо не
    сохранял вовсе. Само по себе это ещё полбеды; беда в следствии: `last_uid`
    ящика — это `max(uid)` по его строкам, строк не появлялось, и каждая
    синхронизация тянула ящик с начала. Навсегда — до первого письма, пришедшего
    только на него.
    """
    first = make_account(root_client, "sales@studio.test")
    second = make_account(root_client, "support@studio.test")

    FakeTransport.inbox = [
        incoming(8101, "<two-boxes@remote.test>", "buyer@twobox.test", datetime(2026, 7, 24, 10, 0))
    ]
    to_first = root_client.post(f"{MAIL}/accounts/{first['id']}/sync").json()
    to_second = root_client.post(f"{MAIL}/accounts/{second['id']}/sync").json()
    assert to_first["stored"] == 1, to_first
    assert to_second["stored"] == 1, f"второй ящик потерял письмо целиком: {to_second}"

    db = SessionLocal()
    try:
        assert mail_repo.last_uid(db, second["id"]) == 8101, (
            "у второго ящика нет last_uid — он будет тянуть себя с начала каждый раз"
        )
    finally:
        db.close()

    # Идемпотентность внутри ящика при этом никуда не делась: то же письмо под
    # другим UID (так выглядит смена UIDVALIDITY) второй раз не сохраняется.
    FakeTransport.inbox = [
        incoming(8102, "<two-boxes@remote.test>", "buyer@twobox.test", datetime(2026, 7, 24, 10, 0))
    ]
    again = root_client.post(f"{MAIL}/accounts/{second['id']}/sync").json()
    assert again["stored"] == 0 and again["skipped"] == 1, again


def test_letters_whose_ids_differ_beyond_the_column_are_not_taken_for_one(root_client, mail_on):
    """Ключ сравнения нельзя обрезать: обрезанный склеивает разные письма.

    Раньше Message-ID резался до ширины колонки, и два письма, совпадающие в
    первых 320 знаках, становились одной строкой: второе тихо считалось
    дубликатом. Со стороны это неотличимо от честной идемпотентности — в ответе
    растёт `skipped`, — а у клиента в ленте не хватает письма.
    """
    account = make_account(root_client, "longid@studio.test")
    head = "<" + "a" * 400
    FakeTransport.inbox = [
        incoming(
            8201, f"{head}-first@mailer.test>", "sender@longid.test",
            datetime(2026, 7, 23, 9, 0), subject="Первое",
        ),
        incoming(
            8202, f"{head}-second@mailer.test>", "sender@longid.test",
            datetime(2026, 7, 23, 9, 5), subject="Второе",
        ),
    ]
    result = root_client.post(f"{MAIL}/accounts/{account['id']}/sync").json()
    assert result["stored"] == 2, f"письмо потеряно как мнимый дубль: {result}"

    listed = root_client.get(f"{MAIL}/messages?account_id={account['id']}").json()
    assert {m["subject"] for m in listed["items"]} == {"Первое", "Второе"}
    keys = {m["message_id"] for m in listed["items"]}
    assert len(keys) == 2, "у разных писем один ключ идемпотентности"
    assert all(len(key) <= MESSAGE_ID_LENGTH for key in keys), "ключ не влезает в колонку"

    # И повтор такое письмо по-прежнему ловит: ключ считается от значения, а не
    # выдаётся новым на каждую строку.
    FakeTransport.inbox = [
        incoming(
            8203, f"{head}-first@mailer.test>", "sender@longid.test",
            datetime(2026, 7, 23, 9, 0), subject="Первое",
        )
    ]
    again = root_client.post(f"{MAIL}/accounts/{account['id']}/sync").json()
    assert again["stored"] == 0 and again["skipped"] == 1, again


def test_a_failed_sync_leaves_its_error_on_the_mailbox(root_client, mail_on):
    """Ошибка синхронизации обязана дожить до интерфейса.

    `_remember_error` писал её в ту же транзакцию, которую следом откатывало
    исключение синхронизации. Root открывал настройки и видел чистый ящик,
    хотя почта не забиралась неделю: каждая попытка стирала собственный след.
    """
    account = make_account(root_client, "failing@studio.test")
    FakeTransport.fail_with = "IMAP: connection refused"
    failed = root_client.post(f"{MAIL}/accounts/{account['id']}/sync")
    assert failed.status_code == 422, failed.text
    assert failed.json()["error"]["code"] == "mail_sync_failed"
    FakeTransport.fail_with = None

    listed = root_client.get(f"{MAIL}/accounts").json()["items"]
    entry = next(item for item in listed if item["id"] == account["id"])
    assert entry["last_error"], "ошибка синхронизации не сохранилась — ящик выглядит здоровым"
    assert "connection refused" in entry["last_error"]
    assert entry["last_error_at"], "у ошибки нет времени — непонятно, свежая она или прошлогодняя"
    assert MAILBOX_PASSWORD not in entry["last_error"]

    # Удачная синхронизация ошибку снимает: иначе она висела бы вечно.
    healed = root_client.post(f"{MAIL}/accounts/{account['id']}/sync")
    assert healed.status_code == 200, healed.text
    listed = root_client.get(f"{MAIL}/accounts").json()["items"]
    entry = next(item for item in listed if item["id"] == account["id"])
    assert entry["last_error"] is None


def test_a_letter_dated_in_the_future_does_not_stick_to_the_top_of_the_feed(root_client, mail_on):
    """`Date:` пишет отправитель — единственное время в системе, которое выбираем не мы.

    Письмо с датой 9999-01-01 вставало наверху ленты клиента и оставалось там
    навсегда, закрывая собой всё, что действительно происходило.
    """
    account = make_account(root_client, "ahead@studio.test")
    client = make_client(root_client, "Из будущего", "ahead@remote.test")

    FakeTransport.inbox = [
        incoming(8601, "<ahead-1@remote.test>", "ahead@remote.test", datetime(9999, 1, 1))
    ]
    synced = root_client.post(f"{MAIL}/accounts/{account['id']}/sync")
    assert synced.status_code == 200 and synced.json()["stored"] == 1, synced.text

    stored = root_client.get(f"{MAIL}/messages?account_id={account['id']}").json()["items"][0]
    assert not stored["sent_at"].startswith("9999"), "письмо сохранило дату из будущего"
    assert stored["sent_at"].startswith(now_utc().strftime("%Y-%m-%d"))

    entry = next(n for n in feed(root_client, client["id"]) if n["kind"] == "email")
    assert not entry["happened_at"].startswith("9999"), "лента клиента навсегда заперта письмом"


def test_percent_in_the_search_is_a_character_not_a_wildcard(root_client, mail_on):
    """Проверка, а не починка: экранированием занят общий слой запросов.

    `search_messages` ищет через `database.query.contains`, а он обезвреживает
    `%` и `_` знаком `/`. Тест закрепляет это со стороны почты: замени кто-нибудь
    `contains` на голый `ilike`, и поиск по одному знаку `%` вернул бы всю
    переписку фирмы разом.
    """
    account = make_account(root_client, "wildcard@studio.test")
    FakeTransport.inbox = [
        incoming(
            8701, "<wild-1@remote.test>", "wild@remote.test",
            datetime(2026, 7, 22, 9, 0), subject="Скидка 50% на витрину",
        ),
        incoming(
            8702, "<wild-2@remote.test>", "wild@remote.test",
            datetime(2026, 7, 22, 9, 5), subject="Смета без знака",
        ),
        incoming(
            8703, "<wild-3@remote.test>", "wild@remote.test",
            datetime(2026, 7, 22, 9, 10), subject="акт_один",
        ),
        incoming(
            8704, "<wild-4@remote.test>", "wild@remote.test",
            datetime(2026, 7, 22, 9, 15), subject="актХодин",
        ),
    ]
    root_client.post(f"{MAIL}/accounts/{account['id']}/sync")

    def search(needle: str) -> list[str]:
        response = root_client.get(
            f"{MAIL}/messages", params={"account_id": account["id"], "search": needle}
        )
        assert response.status_code == 200, response.text
        return [m["subject"] for m in response.json()["items"]]

    assert search("%") == ["Скидка 50% на витрину"], "знак процента сработал как шаблон"
    assert search("_") == ["акт_один"], "подчёркивание сработало как «любой символ»"


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


def test_a_letter_cannot_be_written_onto_another_clients_deal(root_client, mail_on):
    """Клиент и заявка письма обязаны сходиться между собой.

    По отдельности существование каждого проверялось, а согласие — нет: письмо
    Алисе записывалось на заявку Боба. В ленте заявки Боба появлялась чужая
    переписка, и выглядела она совершенно обычно. Звонок эту проверку делает
    (`telephony_service.attach_deal`), письмо не делало.
    """
    account = make_account(root_client, "cross@studio.test")
    alice = make_client(root_client, "Алиса", "alice@cross.test")
    bob = make_client(root_client, "Боб", "bob@cross.test")
    bobs_deal = root_client.post(
        f"{API}/deals", json={"title": "Заказ Боба", "client_id": bob["id"]}
    ).json()

    response = root_client.post(
        f"{MAIL}/send",
        json={
            "to": ["alice@cross.test"],
            "subject": "Смета",
            "body": "Смета во вложении.",
            "account_id": account["id"],
            "client_id": alice["id"],
            "deal_id": bobs_deal["id"],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "deal_other_client"
    assert FakeTransport.sent == [], "письмо ушло в сеть до проверки"

    deal_feed = root_client.get(f"{API}/deals/{bobs_deal['id']}/feed").json()["items"]
    assert [n for n in deal_feed if n["kind"] == "email"] == [], "чужая переписка в ленте заявки"


def test_a_letter_to_thousands_of_recipients_is_refused(root_client, mail_on):
    """Три тысячи адресатов — это рассылка, а не переписка.

    Список получателей лежит в колонке TEXT, а TEXT в MySQL меряется байтами
    (65 535): такой список туда не помещается и в нестрогом режиме обрезается
    молча — письмо в базе оказалось бы адресовано не тем, кому ушло.
    """
    account = make_account(root_client, "bulk@studio.test")
    response = root_client.post(
        f"{MAIL}/send",
        json={
            "to": [f"user{number}@remote.test" for number in range(3000)],
            "subject": "Рассылка",
            "body": "Текст",
            "account_id": account["id"],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "too_many_recipients"
    assert FakeTransport.sent == [], "письмо ушло в сеть до проверки"


def test_overlong_mailbox_fields_are_refused(root_client, mail_on):
    """Название и хост длиннее своих колонок не должны доезжать до вставки.

    SQLite принимает такую строку молча, MySQL — куда проект переезжает —
    отвергает операцию. То есть беда сидела бы тихо ровно до дня переезда, а
    проявилась бы на настройке ящика, то есть на первом же шаге после него.
    """
    created = root_client.post(
        f"{MAIL}/accounts", json={"address": "toolong@studio.test", "title": "Я" * 5000}
    )
    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "title_too_long"

    account = make_account(root_client, "limits@studio.test")
    patched = root_client.patch(
        f"{MAIL}/accounts/{account['id']}", json={"imap_host": "h" * 5000}
    )
    assert patched.status_code == 422, patched.text
    assert patched.json()["error"]["code"] == "host_too_long"

    listed = root_client.get(f"{MAIL}/accounts").json()["items"]
    entry = next(item for item in listed if item["id"] == account["id"])
    assert entry["imap_host"] == "imap.studio.test", "отказ всё-таки испортил настройки ящика"

    long_address = "a" * 400 + "@studio.test"
    refused = root_client.post(f"{MAIL}/accounts", json={"address": long_address})
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "address_too_long"


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
        # Ящик — настройка системы, поэтому право на него `settings.manage`,
        # а не «нужен root»: должность «гендиректор» заводит ящики, не будучи
        # владельцем системы.
        assert response.json()["error"]["code"] == "permission_denied"
        assert "settings.manage" in response.json()["error"]["message"]

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
        stored = mail_repo.find_by_message_id(db, account["id"], "<purge-1@remote.test>")
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


def test_a_broken_message_does_not_take_the_whole_batch_with_it(root_client, mail_on):
    """Одно письмо, которое не разобрать, не останавливает почту фирмы.

    Цикл не изолировал письма: исключение на N-м откатывало уже сохранённые
    1…N−1, `last_uid` не двигался, и следующая синхронизация тянула ту же пачку
    и падала так же. Ящик вставал насовсем, а целые письма до битого исчезали.
    """
    account = make_account(root_client, "batch@studio.test")
    FakeTransport.inbox = [
        incoming(9001, "<good-one@example.com>", "client@example.com", now_utc()),
        # Дата отправки обязательна в базе: такое письмо разобрать не удастся.
        FetchedMessage(
            uid=9002,
            message_id="<broken-one@example.com>",
            subject="Битое",
            from_addr="client@example.com",
            to_addrs=["office@studio.test"],
            sent_at=None,
            body_text="текст",
        ),
        incoming(9003, "<good-two@example.com>", "client@example.com", now_utc()),
    ]

    synced = root_client.post(f"{API}/mail/accounts/{account['id']}/sync")
    assert synced.status_code == 200, synced.text
    result = synced.json()

    assert result["stored"] == 2, f"целые письма потерялись: {result}"
    assert result["broken"] == 1, f"битое письмо не посчитано: {result}"

    # Ящик остался рабочим: следующий заход не упирается в то же письмо.
    again = root_client.post(f"{API}/mail/accounts/{account['id']}/sync")
    assert again.status_code == 200, again.text
