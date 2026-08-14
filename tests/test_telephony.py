"""Телефония: вебхук, идемпотентность, номера, лента, click-to-call.

Настоящей АТС в тестах нет и быть не должно — станцию заменяет встроенная
подделка (``core/services/telephony_providers.FakeProvider``), а события
вебхука отправляются так же, как их отправила бы станция: подписанным телом.
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core import exceptions as errors
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


def test_simultaneous_events_with_one_call_id_do_not_fail(root_client, telephony, monkeypatch):
    """Два события одного звонка, пришедшие РАЗОМ, — это по-прежнему один звонок.

    Идемпотентность держалась проверкой «нет ли уже такого» и уникальным
    индексом, а между ними есть окно. События одного звонка станция шлёт
    подряд, при разрыве связи — повторяет пачкой, и в это окно она попадает
    регулярно: оба запроса читают «нет такого», оба вставляют, второй получает
    нарушение уникальности. Данные оставались целы, но станция видела 500,
    считала событие недоставленным и слала его снова.

    Гонку здесь воспроизводим не потоками (их исход зависит от того, кто успел
    первым, и тест был бы плавающим), а тем самым
    промахом чтения: заставляем сервис один раз не увидеть уже существующую
    строку — ровно то, что видит запрос, прочитавший базу до чужого коммита.
    """
    payload = {
        "call_id": "race-1",
        "direction": "in",
        "from": "+380671230001",
        "to": "0442000000",
        "started_at": "2026-08-05T12:00:00+00:00",
    }
    first = send_event(telephony, payload)
    assert first.status_code == 200, first.text

    from core.services import telephony_service as service

    real = service.telephony_repo.get_by_external_id
    seen = {"lookups": 0}

    def blind_once(db, external_id):
        """Первый поиск промахивается — как у проигравшего гонку запроса."""
        seen["lookups"] += 1
        return None if seen["lookups"] == 1 else real(db, external_id)

    monkeypatch.setattr(service.telephony_repo, "get_by_external_id", blind_once)

    second = send_event(telephony, {**payload, "outcome": "answered", "duration": 30})
    assert second.status_code == 200, second.text
    assert seen["lookups"] >= 2, "запасной поиск после нарушения уникальности не сработал"
    assert second.json()["call"]["id"] == first.json()["call"]["id"], "звонок задвоился"
    # проигравший вставку не теряет своё событие: поля из него доехали
    assert second.json()["call"]["duration_sec"] == 30

    calls = root_client.get(f"{CALLS}?per_page=200").json()
    assert len([c for c in calls["items"] if c["external_id"] == "race-1"]) == 1


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


def test_the_station_names_who_talked_but_does_not_sign_the_entry(
    root_client, telephony, client_record
):
    """Оператор от станции виден в журнале звонков и не подписывает ленту.

    Это было единственное место во всей системе, где **внешний источник выбирал
    живого автора**: `operator_email` в теле вебхука превращался в `author_id`
    записи ленты. Держатель секрета присылал почту root'а и получал в карточке
    клиента строку за его подписью.

    Кто говорил — станция знает и называет, это законно и полезно. Но
    «поговорил» и «сделал запись в CRM» — разные утверждения, и второе именем
    человека не подписывается.
    """
    root = root_client.get(f"{API}/auth/me").json()
    sent = send_event(telephony, {
        "call_id": "operator-signature-probe",
        "direction": "in",
        "from": "+380671234567",
        "to": "0442000000",
        "started_at": "2026-08-06T10:00:00+00:00",
        "status": "answered",
        "duration": 30,
        "operator_email": root["email"],
    })
    assert sent.status_code == 200, sent.text

    # В журнале звонков оператор на месте: станция про него знает.
    call = next(
        c for c in root_client.get(CALLS).json()["items"]
        if c["external_id"] == "operator-signature-probe"
    )
    assert call["user_id"] == root["id"], "оператор от станции потерялся"

    # А в ленте у записи автора нет — её сделала станция, а не человек.
    notes = root_client.get(f"{API}/clients/{client_record['id']}/notes").json()["items"]
    entry = next(n for n in notes if n["kind"] == "call")
    assert entry["author_id"] is None, "станция подписала запись живым человеком"
    assert entry["author_name"] is None


def test_a_broken_webhook_never_answers_with_a_server_error(root_client, telephony):
    """Кривое поле от станции — отказ с объяснением, а не 500.

    Станция на 500 считает событие недоставленным и шлёт его снова, раскручивая
    петлю повторов. Хуже того, часть этих ответов не требовала ни секрета, ни
    правильного тела: большая метка времени в заголовке или один не-ASCII байт
    в подписи роняли запрос у любого из интернета.
    """
    from fastapi.testclient import TestClient as Client

    quiet = Client(app, raise_server_exceptions=False)

    # 1. Заголовки: подпись и метка времени. Секрет знать не нужно.
    body = json.dumps({"call_id": "probe"}).encode()
    for headers, name in (
        ({"X-OpenCRM-Timestamp": "1000000000000", "X-OpenCRM-Signature": "0" * 64}, "метка из будущего"),
        # Значение байтами: так его пришлёт настоящий клиент, а Starlette
        # декодирует заголовки как latin-1 — отсюда и не-ASCII в подписи.
        (
            {
                "X-OpenCRM-Timestamp": str(int(time.time())).encode(),
                "X-OpenCRM-Signature": bytes([0xFF]) * 64,
            },
            "подпись не ASCII",
        ),
    ):
        answer = quiet.post(
            WEBHOOK, content=body, headers={**headers, "Content-Type": "application/json"}
        )
        assert answer.status_code < 500, f"{name}: {answer.status_code}"

    # 2. Тело с подписью настоящей станции: каждое поле по отдельности.
    for payload, name in (
        ({"call_id": "p1", "direction": "in", "started_at": float("inf")}, "время: бесконечность"),
        ({"call_id": "p2", "direction": "in", "started_at": 1e20}, "время: гигант"),
        ({"call_id": "p3", "direction": "in", "started_at": "9999-12-31T23:59:59"}, "время у края календаря"),
        ({"call_id": "p4", "direction": "in", "duration": 10**19}, "длительность: гигант"),
        ({"call_id": "p" * 5000, "direction": "in"}, "ключ длиной 5000"),
    ):
        raw = json.dumps(payload).encode()
        answer = quiet.post(WEBHOOK, content=raw, headers=sign(telephony, raw))
        assert answer.status_code < 500, f"{name}: {answer.status_code} {answer.text[:120]}"

    # 3. Тело — не JSON вовсе (оборвалась передача).
    raw = b"not json at all"
    answer = quiet.post(WEBHOOK, content=raw, headers=sign(telephony, raw))
    assert answer.status_code < 500, f"мусор вместо JSON: {answer.status_code}"


def test_the_call_key_fits_the_column(root_client, telephony, client_record):
    """Длинный ключ обрезается под колонку, а не ломает вставку.

    Ключ длиннее колонки либо роняет вставку, либо обрезается базой молча — и
    тогда два разных звонка с общим началом ключа склеиваются в один.
    """
    from core.services.telephony_service import MAX_EXTERNAL_ID

    long_id = "z" * 5000
    sent = send_event(telephony, {
        "call_id": long_id, "direction": "in", "from": "+380671234567",
        "to": "0442000000", "started_at": "2026-08-06T10:00:00+00:00", "status": "answered",
    })
    assert sent.status_code == 200, sent.text

    stored = root_client.get(CALLS).json()["items"]
    saved = next(c for c in stored if c["external_id"].startswith("zzz"))
    assert len(saved["external_id"]) == MAX_EXTERNAL_ID, len(saved["external_id"])


def test_a_period_filter_with_a_zone_finds_the_call(root_client, telephony):
    """Границы периода звонков считаются в UTC, а не как пришло.

    «13:00+03:00» — это 10:00 UTC. Без приведения фильтр сравнивал его с naive
    UTC как 13:00 и терял звонки, попадающие в период.
    """
    sent = send_event(telephony, {
        "call_id": "period-probe", "direction": "in", "from": "+380671234567",
        "to": "0442000000", "started_at": "2026-08-05T10:00:00+00:00", "status": "answered",
    })
    assert sent.status_code == 200, sent.text

    naive = root_client.get(CALLS, params={"since": "2026-08-05T09:00:00"}).json()["items"]
    aware = root_client.get(CALLS, params={"since": "2026-08-05T12:00:00+03:00"}).json()["items"]
    assert [c["external_id"] for c in naive if c["external_id"] == "period-probe"], "звонка нет и без зоны"
    assert [c["external_id"] for c in aware if c["external_id"] == "period-probe"], (
        "смещение зоны в границе периода потеряло звонок"
    )


def test_a_long_number_filter_is_refused_not_crashed(root_client, telephony):
    """Пятьдесят тысяч знаков в фильтре номера — отказ, а не ошибка сервера."""
    from web.api.deps import MAX_SEARCH

    fine = root_client.get(CALLS, params={"number": "7" * MAX_SEARCH})
    assert fine.status_code == 200, fine.text

    too_long = root_client.get(CALLS, params={"number": "7" * (MAX_SEARCH + 1)})
    assert too_long.status_code == 422, too_long.text


# --- кнопка звонка не забирает чужую строку ---

class RepeatingStation:
    """Станция, назвавшая уже занятый идентификатор звонка.

    Так ведёт себя и демо-провайдер после перезапуска процесса (счётчик
    начинается заново, а база — нет), и настоящая АТС после своего перезапуска
    или смены нумерации.
    """

    def __init__(self, external_id: str) -> None:
        self.external_id = external_id

    def originate(self, from_ext: str, to_number: str):
        return telephony_providers.OriginateResult(external_id=self.external_id)


def test_click_to_call_does_not_take_over_a_foreign_call(
    root_client, telephony, client_record, monkeypatch
):
    """Занятый ключ от станции — отказ, а не запись поверх чужого разговора.

    Кнопка «позвонить» заводила строку так же, как вебхук: искала звонок с тем
    же ``external_id`` и, найдя, дописывала в него свои поля. У входящих
    событий это законно — они описывают один разговор. Здесь разговор родился
    секунду назад, и строка с таким же ключом — заведомо другая: чужие номера
    и оператор переписывались, а клиент, заявка и запись в ленте оставались от
    прежнего звонка. Исходящий звонок оказывался в карточке чужого клиента, и
    следующее событие переписывало его же запись в чужой ленте.
    """
    taken = "reused-by-pbx"
    old = send_event(telephony, {
        "call_id": taken, "direction": "in", "from": "+380671234567", "to": "0442000000",
        "started_at": "2026-08-05T09:45:00+00:00", "outcome": "answered", "duration": 30,
    })
    assert old.status_code == 200, old.text
    before = old.json()["call"]
    assert before["client_id"] == client_record["id"]

    monkeypatch.setattr(
        telephony_providers, "build", lambda *args, **kwargs: RepeatingStation(taken)
    )
    response = root_client.post(
        f"{API}/telephony/click-to-call",
        json={"number": "+380991112233", "from_ext": "101"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "pbx_call_id_taken"

    after = root_client.get(f"{CALLS}/{before['id']}").json()
    assert after["direction"] == "in", "чужой звонок стал исходящим"
    assert after["from_number"] == before["from_number"], "чужой номер переписан"
    assert after["to_number"] == before["to_number"], "чужой номер переписан"
    assert after["user_id"] is None, "чужому звонку приписан оператор"
    assert after["client_id"] == client_record["id"]
    # и второй строки на тот же ключ тоже не появилось
    stored = root_client.get(f"{CALLS}?per_page=200").json()["items"]
    assert len([c for c in stored if c["external_id"] == taken]) == 1


def test_the_demo_station_does_not_reissue_call_ids():
    """Перезапуск процесса не возвращает станцию к уже выданным ключам.

    Счётчик подделки живёт в памяти, а журнал звонков — в базе, которая
    перезапуск переживает. Пока ключ был просто «fake-N», второй запуск
    демо-стенда выдавал идентификаторы, уже лежащие в журнале.
    """
    before_restart = telephony_providers.FakeProvider()
    after_restart = telephony_providers.FakeProvider()
    first = {before_restart.originate("101", "+380671234567").external_id for _ in range(3)}
    second = {after_restart.originate("101", "+380671234567").external_id for _ in range(3)}
    assert len(first) == 3, "ключи повторяются внутри одного запуска"
    assert not (first & second), "после перезапуска станция выдаёт те же ключи"


# --- адрес станции не должен уводить сервер куда попало ---

@pytest.mark.parametrize(
    "bad",
    [
        "file:///etc/passwd",
        "gopher://127.0.0.1:11211/_stats",
        "javascript:alert(1)",
        "//169.254.169.254/latest/meta-data/",
        "https://pbx.example/" + "a" * 600,
    ],
)
def test_the_pbx_address_must_be_an_http_url(root_client, telephony, bad):
    """Адрес станции — единственная настройка, по которой сервер ходит сам.

    Непроверенным он делал из CRM ходока по чужим адресам: право на настройки
    телефонии превращалось в «попроси сервер сходить куда угодно и покажи, что
    ответили».
    """
    response = root_client.patch(
        f"{API}/telephony/settings", json={"values": {"telephony_api_url": bad}}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "bad_telephony_url"
    assert root_client.get(f"{API}/telephony/settings").json()["api_url"] != bad


def test_a_stored_bad_pbx_address_never_becomes_a_request():
    """Значение могло лечь в базу до проверки — наружу оно всё равно не уйдёт."""
    with pytest.raises(errors.ValidationError):
        telephony_providers.build("http", "file:///etc/passwd", "")


def test_a_pbx_inside_the_local_network_stays_allowed(root_client, telephony):
    """Asterisk обычно стоит в той же локальной сети — это законный адрес."""
    try:
        saved = root_client.patch(
            f"{API}/telephony/settings",
            json={"values": {"telephony_api_url": "http://10.0.0.5:8088/originate"}},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["api_url"] == "http://10.0.0.5:8088/originate"
    finally:
        root_client.patch(
            f"{API}/telephony/settings", json={"values": {"telephony_api_url": ""}}
        )


# --- фильтры журнала ---

@pytest.mark.parametrize(
    "params", [{"direction": "incoming"}, {"direction": "IN"}, {"outcome": "miss"}]
)
def test_an_unknown_filter_value_is_refused_not_silently_empty(root_client, telephony, params):
    """Опечатка в фильтре — отказ, а не пустая выдача.

    Пустой список неотличим от «звонков нет»: человек ищет пропавшие звонки в
    данных, которых по такому фильтру и не было.
    """
    response = root_client.get(CALLS, params=params)
    assert response.status_code == 422, f"{params}: {response.text}"


def test_known_filter_values_still_work(root_client, telephony):
    assert root_client.get(CALLS, params={"direction": "in"}).status_code == 200
    assert root_client.get(CALLS, params={"outcome": "missed"}).status_code == 200


# --- время события ---

def test_a_call_from_the_year_9999_does_not_stick_on_top_of_the_feed(
    root_client, telephony, client_record
):
    """Время события приходит от станции, и потолок у него обязан быть.

    `9999-01-01` вставал первой строкой в карточке клиента и оставался там
    навсегда: времени у звонка не правят, а удалять факт разговора ради
    починки сортировки нечестно.
    """
    sent = send_event(telephony, {
        "call_id": "far-future", "direction": "in", "from": "+380671234567",
        "to": "0442000000", "started_at": "9999-01-01T00:00:00+00:00",
        "outcome": "answered", "duration": 5,
    })
    assert sent.status_code == 422, sent.text
    assert sent.json()["error"]["code"] == "started_at_in_future"

    stored = root_client.get(f"{CALLS}?per_page=200").json()["items"]
    assert all(c["external_id"] != "far-future" for c in stored)
    notes = root_client.get(
        f"{API}/clients/{client_record['id']}/notes?per_page=200"
    ).json()["items"]
    assert all(not n["happened_at"].startswith("9999") for n in notes)


def test_a_station_clock_a_minute_ahead_does_not_lose_the_call(telephony):
    """Часы станции нам не подчиняются: небольшой уход вперёд — не повод отказывать."""
    ahead = (datetime.now(timezone.utc) + timedelta(minutes=1)).replace(microsecond=0)
    sent = send_event(telephony, {
        "call_id": "clock-skew", "direction": "in", "from": "+380671234567",
        "to": "0442000000", "started_at": ahead.isoformat(), "outcome": "answered",
    })
    assert sent.status_code == 200, sent.text


# --- типы полей вебхука ---

def test_a_nested_object_instead_of_a_number_is_refused(root_client, telephony):
    """`str(...)` над телом вебхука был согласием на что угодно.

    `{"from": {"nested": "html"}}` ложился в номер репрезентацией словаря и
    ехал дальше как настоящий номер: в карточку клиента, в текст записи ленты
    («Incoming call from {'nested': 'html'}») и в заголовок задачи
    «перезвонить». Ни одна проверка ниже такое не ловит — это уже строка.
    """
    sent = send_event(telephony, {
        "call_id": "nested-from", "direction": "in", "from": {"nested": "html"},
        "to": "0442000000", "started_at": "2026-08-05T09:50:00+00:00",
        "outcome": "answered", "duration": 5,
    })
    assert sent.status_code == 422, sent.text
    assert sent.json()["error"]["code"] == "bad_field_type"


@pytest.mark.parametrize(
    "call_id, broken",
    [
        ("half-written-number", {"from": {"nested": "html"}}),
        ("half-written-duration", {"from": "+380671234567", "duration": 10**19}),
    ],
)
def test_a_refused_event_does_not_leave_half_a_call(root_client, telephony, call_id, broken):
    """Отказ на полпути не оставляет в журнале полузаполненный звонок.

    Ошибку ловит `@app.exception_handler(DomainError)`, а он срабатывает
    раньше, чем `get_db` увидит исключение: сессия закрывается обычным
    `commit`, и всё записанное до отказа остаётся в базе. Событие с
    длительностью 10^19 так оставляло звонок без длительности и без итога — а
    повторить его станция уже не могла: ключ занят, и в журнале навсегда
    висел обрубок разговора.
    """
    sent = send_event(telephony, {
        "call_id": call_id, "direction": "in", "to": "0442000000",
        "started_at": "2026-08-05T09:52:00+00:00", "outcome": "answered", **broken,
    })
    assert sent.status_code == 422, sent.text

    stored = root_client.get(f"{CALLS}?per_page=200").json()["items"]
    assert all(c["external_id"] != call_id for c in stored), "в журнале осталась половина звонка"


def test_numbers_from_the_station_are_still_accepted(root_client, telephony, client_record):
    """Станция вправе прислать идентификатор и номер числом — это не мусор."""
    sent = send_event(telephony, {
        "call_id": 770077, "direction": "in", "from": 380671234567,
        "to": "0442000000", "started_at": "2026-08-05T09:55:00+00:00",
        "outcome": "answered", "duration": 5,
    })
    assert sent.status_code == 200, sent.text
    call = sent.json()["call"]
    assert call["external_id"] == "770077"
    assert call["from_number"] == "380671234567"
    assert call["client_id"] == client_record["id"]
