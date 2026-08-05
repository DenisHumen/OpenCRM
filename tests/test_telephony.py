"""Телефония: вебхук, идемпотентность, номера, лента, click-to-call.

Настоящей АТС в тестах нет и быть не должно — станцию заменяет встроенная
подделка (``core/services/telephony_providers.FakeProvider``), а события
вебхука отправляются так же, как их отправила бы станция: подписанным телом.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from core.services import telephony_providers
from core.utils import normalize_phone
from tests.conftest import API
from web.main import app

WEBHOOK = f"{API}/telephony/webhook"
CALLS = f"{API}/telephony/calls"


def sign(secret: str, body: bytes, timestamp: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-OpenCRM-Timestamp": timestamp,
        "X-OpenCRM-Signature": signature,
        "Content-Type": "application/json",
    }


def send_event(secret: str, payload: dict, **kwargs):
    """Событие от «АТС»: отдельный клиент без cookie — как настоящий сервер."""
    body = json.dumps(payload).encode()
    return TestClient(app).post(WEBHOOK, content=body, headers=sign(secret, body, **kwargs))


@pytest.fixture(scope="module")
def telephony(root_client) -> str:
    """Включает блок, задаёт код страны и секрет; возвращает секрет."""
    switched = root_client.post(f"{API}/modules/telephony", json={"enabled": True})
    assert switched.status_code == 200, switched.text
    assert any(m["key"] == "telephony" and m["enabled"] for m in switched.json()["items"])

    settings = root_client.patch(
        f"{API}/settings", json={"values": {"default_country_code": "380"}}
    )
    assert settings.status_code == 200, settings.text

    created = root_client.post(f"{API}/telephony/settings/secret")
    assert created.status_code == 201, created.text
    yield created.json()["secret"]

    # Состояние блоков глобальное, а база у тестов общая: возвращаем как было,
    # иначе соседние файлы поедут на чужих настройках.
    root_client.post(f"{API}/modules/telephony", json={"enabled": False})
    root_client.patch(f"{API}/settings", json={"values": {"default_country_code": ""}})


@pytest.fixture(scope="module")
def client_record(root_client, telephony) -> dict:
    """Клиент с номером, записанным «по-человечески», а не канонически."""
    response = root_client.post(
        f"{API}/clients", json={"name": "Телефонный Клиент", "phone": "+38 (067) 123-45-67"}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- нормализация номеров ---

def test_normalize_phone_formats():
    """Один номер, записанный пятью способами, сводится к одному виду."""
    canonical = normalize_phone("+380671234567", "380")
    for variant in (
        "380671234567",
        "0671234567",
        "+38 (067) 123-45-67",
        "067 123 45 67",
        "00380671234567",
    ):
        assert normalize_phone(variant, "380") == canonical, variant


def test_normalize_phone_keeps_foreign_numbers():
    """Международный номер с «+» не трогаем: код страны у него уже свой."""
    assert normalize_phone("+1 202 555 0143", "380") == "12025550143"


def test_normalize_phone_keeps_extensions():
    """Внутренний номер станции не превращается в абонентский."""
    assert normalize_phone("101", "380") == "101"


def test_client_phone_variants_resolve_to_one_client(root_client, telephony, client_record):
    """Звонки с разных написаний одного номера попадают в одну карточку."""
    found = set()
    for index, variant in enumerate(
        ["+380671234567", "380671234567", "0671234567", "+38 (067) 123-45-67"]
    ):
        response = send_event(
            telephony,
            {
                "call_id": f"variant-{index}",
                "direction": "in",
                "from": variant,
                "to": "0442000000",
                "started_at": "2026-08-05T09:00:00+00:00",
                "outcome": "answered",
                "duration": 30,
            },
        )
        assert response.status_code == 200, response.text
        found.add(response.json()["call"]["client_id"])
    assert found == {client_record["id"]}


# --- безопасность вебхука ---

def test_webhook_rejects_unsigned(telephony):
    response = TestClient(app).post(WEBHOOK, json={"call_id": "unsigned", "direction": "in"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "bad_signature"


def test_webhook_rejects_foreign_signature(telephony):
    response = send_event("not-the-real-secret", {"call_id": "forged", "direction": "in"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "bad_signature"


def test_webhook_rejects_tampered_body(telephony):
    """Подпись привязана к телу: подменённый номер её ломает."""
    body = json.dumps({"call_id": "tamper", "direction": "in", "from": "+380671234567"}).encode()
    headers = sign(telephony, body)
    tampered = json.dumps({"call_id": "tamper", "direction": "in", "from": "+380999999999"}).encode()
    response = TestClient(app).post(WEBHOOK, content=tampered, headers=headers)
    assert response.status_code == 401


def test_webhook_rejects_stale_timestamp(telephony):
    """Перехваченный запрос нельзя переиграть через час."""
    old = str(int(time.time()) - 3600)
    response = send_event(telephony, {"call_id": "stale", "direction": "in"}, timestamp=old)
    assert response.status_code == 401


# --- идемпотентность ---

def test_repeated_event_does_not_duplicate(root_client, telephony, client_record):
    """Повтор того же события не создаёт ни второго звонка, ни второй записи в ленте."""
    payload = {
        "call_id": "repeat-1",
        "direction": "in",
        "from": "+380671234567",
        "to": "0442000000",
        "started_at": "2026-08-05T10:15:00+00:00",
        "outcome": "answered",
        "duration": 75,
    }
    first = send_event(telephony, payload)
    assert first.status_code == 200, first.text
    second = send_event(telephony, payload)
    assert second.status_code == 200, second.text
    assert first.json()["call"]["id"] == second.json()["call"]["id"]

    calls = root_client.get(f"{CALLS}?number=380671234567&per_page=200").json()
    assert len([c for c in calls["items"] if c["external_id"] == "repeat-1"]) == 1

    notes = root_client.get(f"{API}/clients/{client_record['id']}/notes?per_page=200").json()
    bodies = [n["body"] for n in notes["items"] if n["kind"] == "call"]
    assert bodies.count("Incoming call from +380671234567 — 1:15") == 1


def test_call_lifecycle_events_update_one_record(root_client, telephony):
    """Начался → ответили → завершился: одна строка, а не три."""
    for event in (
        {"call_id": "life-1", "direction": "in", "from": "+380500000001", "to": "0442000000",
         "started_at": "2026-08-05T11:00:00+00:00"},
        {"call_id": "life-1", "outcome": None},
        {"call_id": "life-1", "outcome": "answered", "duration": 42},
    ):
        response = send_event(telephony, event)
        assert response.status_code == 200, response.text

    call = response.json()["call"]
    assert call["outcome"] == "answered"
    assert call["duration_sec"] == 42
    assert root_client.get(f"{CALLS}?number=380500000001").json()["total"] == 1


# --- единая лента ---

def test_feed_note_uses_call_start_time(root_client, telephony, client_record):
    """Время записи в ленте — начало разговора, а не приход события."""
    response = send_event(
        telephony,
        {
            "call_id": "feed-time",
            "direction": "in",
            "from": "0671234567",
            "to": "0442000000",
            "started_at": "2026-08-04T08:30:00+00:00",
            "outcome": "answered",
            "duration": 10,
        },
    )
    assert response.status_code == 200, response.text
    note_id = response.json()["call"]["note_id"]
    assert note_id is not None
    notes = root_client.get(f"{API}/clients/{client_record['id']}/notes?per_page=200").json()
    note = next(n for n in notes["items"] if n["id"] == note_id)
    assert note["happened_at"].startswith("2026-08-04T08:30:00")
    assert note["kind"] == "call"
    assert note["direction"] == "in"


def test_call_lands_in_the_deal_feed(root_client, telephony, client_record):
    """Звонок, привязанный к заявке, виден в её ленте — ради этого всё и делалось."""
    deal = root_client.post(
        f"{API}/deals", json={"title": "Заказ по звонку", "client_id": client_record["id"]}
    ).json()
    call = send_event(
        telephony,
        {"call_id": "deal-call", "direction": "in", "from": "+380671234567", "to": "0442000000",
         "started_at": "2026-08-05T16:00:00+00:00", "outcome": "answered", "duration": 65},
    ).json()["call"]
    assert call["deal_id"] is None  # станция про заявки ничего не знает

    attached = root_client.patch(f"{CALLS}/{call['id']}", json={"deal_id": deal["id"]})
    assert attached.status_code == 200, attached.text
    assert attached.json()["deal_id"] == deal["id"]

    feed = root_client.get(f"{API}/deals/{deal['id']}/feed").json()["items"]
    assert [n["id"] for n in feed] == [call["note_id"]], "звонка нет в ленте заявки"
    assert feed[0]["kind"] == "call" and feed[0]["direction"] == "in"


def test_call_cannot_be_attached_to_another_clients_deal(root_client, telephony, client_record):
    stranger = root_client.post(f"{API}/clients", json={"name": "Посторонний"}).json()
    other_deal = root_client.post(
        f"{API}/deals", json={"title": "Чужой заказ", "client_id": stranger["id"]}
    ).json()
    call = send_event(
        telephony,
        {"call_id": "wrong-deal", "direction": "in", "from": "+380671234567", "to": "044",
         "started_at": "2026-08-05T16:30:00+00:00", "outcome": "answered", "duration": 5},
    ).json()["call"]

    response = root_client.patch(f"{CALLS}/{call['id']}", json={"deal_id": other_deal["id"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "deal_other_client"


def test_unfinished_call_has_no_feed_note(telephony, client_record):
    """Пока звонок идёт, в ленте ему делать нечего."""
    response = send_event(
        telephony,
        {"call_id": "ringing", "direction": "in", "from": "+380671234567", "to": "0442000000"},
    )
    assert response.status_code == 200, response.text
    call = response.json()["call"]
    assert call["note_id"] is None
    assert call["outcome"] is None
    assert call["duration_sec"] is None


# --- клиент не создаётся сам ---

def test_unknown_number_does_not_create_client(root_client, telephony):
    before = root_client.get(f"{API}/clients?per_page=200").json()["total"]
    response = send_event(
        telephony,
        {
            "call_id": "stranger-call",
            "direction": "in",
            "from": "+380999888777",
            "to": "0442000000",
            "started_at": "2026-08-05T12:00:00+00:00",
            "outcome": "missed",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["call"]["client_id"] is None
    assert root_client.get(f"{API}/clients?per_page=200").json()["total"] == before


# --- пропущенный против отвеченного с нулём ---

def test_missed_differs_from_answered_zero_length(root_client, telephony, client_record):
    missed = send_event(
        telephony,
        {"call_id": "missed-1", "direction": "in", "from": "+380671234567", "to": "0442000000",
         "started_at": "2026-08-05T13:00:00+00:00", "outcome": "missed"},
    ).json()["call"]
    instant = send_event(
        telephony,
        {"call_id": "zero-1", "direction": "in", "from": "+380671234567", "to": "0442000000",
         "started_at": "2026-08-05T13:05:00+00:00", "outcome": "answered", "duration": 0},
    ).json()["call"]

    assert missed["outcome"] == "missed"
    assert missed["duration_sec"] is None  # длительности у пропущенного нет вовсе
    assert instant["outcome"] == "answered"
    assert instant["duration_sec"] == 0  # а здесь она есть и равна нулю

    notes = root_client.get(f"{API}/clients/{client_record['id']}/notes?per_page=200").json()
    bodies = {n["id"]: n["body"] for n in notes["items"]}
    assert bodies[missed["note_id"]].startswith("Missed call")
    assert not bodies[instant["note_id"]].startswith("Missed call")

    only_missed = root_client.get(f"{CALLS}?outcome=missed").json()
    assert all(c["outcome"] == "missed" for c in only_missed["items"])


def test_missed_call_turns_into_a_reminder(root_client, telephony, client_record):
    """Пропущенный звонок не теряется: из него заводится напоминание перезвонить."""
    call = send_event(
        telephony,
        {"call_id": "missed-task", "direction": "in", "from": "+380671234567", "to": "044",
         "started_at": "2026-08-05T14:00:00+00:00", "outcome": "missed"},
    ).json()["call"]

    created = root_client.post(f"{CALLS}/{call['id']}/callback-task")
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["client_id"] == client_record["id"]
    assert task["due_at"] is not None
    assert "+380671234567" in task["title"]
    assert task["id"] in [t["id"] for t in root_client.get(f"{API}/tasks").json()["items"]]


def test_reminder_is_only_for_missed_calls(root_client, telephony, client_record):
    answered = send_event(
        telephony,
        {"call_id": "answered-task", "direction": "in", "from": "+380671234567", "to": "044",
         "started_at": "2026-08-05T14:10:00+00:00", "outcome": "answered", "duration": 12},
    ).json()["call"]
    response = root_client.post(f"{CALLS}/{answered['id']}/callback-task")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "call_not_missed"


def test_reminder_is_unavailable_when_tasks_are_off(root_client, telephony, client_record):
    """Блок напоминаний выключен — возможность не ломается, а отвечает понятно."""
    call = send_event(
        telephony,
        {"call_id": "missed-no-tasks", "direction": "in", "from": "+380671234567", "to": "044",
         "started_at": "2026-08-05T14:20:00+00:00", "outcome": "missed"},
    ).json()["call"]

    assert root_client.post(f"{API}/modules/tasks", json={"enabled": False}).status_code == 200
    try:
        response = root_client.post(f"{CALLS}/{call['id']}/callback-task")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "module_disabled"
    finally:
        root_client.post(f"{API}/modules/tasks", json={"enabled": True})


# --- звонок из CRM ---

def test_click_to_call_goes_through_provider(root_client, telephony, client_record):
    configured = root_client.patch(
        f"{API}/telephony/settings",
        json={"values": {"telephony_provider": "fake", "telephony_default_ext": "101"}},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["provider"] == "fake"

    before = len(telephony_providers.fake().calls)
    response = root_client.post(
        f"{API}/telephony/click-to-call", json={"number": "+38 (067) 123-45-67"}
    )
    assert response.status_code == 201, response.text
    call = response.json()
    assert call["direction"] == "out"
    assert call["client_id"] == client_record["id"]  # исходящий тоже находит карточку
    assert telephony_providers.fake().calls[before:] == [("101", "+38 (067) 123-45-67")]


def test_settings_never_return_secrets(root_client, telephony):
    """Секреты не отдаются наружу — только факт, что они заданы."""
    body = root_client.get(f"{API}/telephony/settings").json()
    assert body["has_webhook_secret"] is True
    assert telephony not in json.dumps(body)
    # и в общие настройки сайта они тоже не попадают
    assert "telephony_webhook_secret" not in root_client.get(f"{API}/settings").json()


def test_manager_cannot_read_connection_settings(manager_client, telephony):
    assert manager_client.get(f"{API}/telephony/settings").status_code == 403


# --- выключенный блок ---

def test_disabled_module_closes_api(root_client, manager_client, telephony):
    """Выключили блок — API закрыто, вебхук принимает событие, но не пишет."""
    assert root_client.post(f"{API}/modules/telephony", json={"enabled": False}).status_code == 200
    try:
        assert manager_client.get(CALLS).status_code == 403
        assert root_client.get(CALLS).status_code == 403
        assert (
            root_client.post(
                f"{API}/telephony/click-to-call", json={"number": "+380671234567"}
            ).status_code
            == 403
        )

        # Станция не знает про выключенный блок и продолжает слать события.
        # Отвечаем 200 «принято, ничего не делаем» — иначе она уйдёт в ретраи.
        ignored = send_event(
            telephony,
            {"call_id": "while-off", "direction": "in", "from": "+380671234567", "to": "044",
             "started_at": "2026-08-05T15:00:00+00:00", "outcome": "answered"},
        )
        assert ignored.status_code == 200
        assert ignored.json() == {"status": "ignored", "reason": "module_disabled"}

        # но подпись проверяется и при выключенном блоке
        assert TestClient(app).post(WEBHOOK, json={"call_id": "x"}).status_code == 401
    finally:
        root_client.post(f"{API}/modules/telephony", json={"enabled": True})

    # событие, пришедшее при выключенном блоке, в базу не попало
    stored = root_client.get(f"{CALLS}?per_page=200").json()["items"]
    assert all(c["external_id"] != "while-off" for c in stored)
