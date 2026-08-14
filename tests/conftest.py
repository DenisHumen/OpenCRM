"""Интеграционные тесты гоняются против настоящего приложения:
временная SQLite-БД и временный storage на каждый прогон."""

import io
import os
import tempfile
from pathlib import Path

import pytest

# Окружение — до импорта приложения (настройки кэшируются)
_TMP = Path(tempfile.mkdtemp(prefix="opencrm-test-"))
os.environ.update(
    {
        "OPENCRM_ENV": "test",
        "OPENCRM_SECRET_KEY": "test-secret-key",
        # Адрес базы для набора. Берётся из окружения, если задан: так один и
        # тот же набор гоняется и против настоящей MySQL, и (пока) против файла.
        "OPENCRM_DB_URL": os.environ.get(
            "OPENCRM_TEST_DB_URL", f"sqlite:///{(_TMP / 'test.db').as_posix()}"
        ),
        "OPENCRM_STORAGE_DIR": str(_TMP / "storage"),
        # Каталог данных — там копии и служебные файлы. Своим именем, а не
        # выведенным из пути к файлу базы: база живёт в сервере, а не в файле.
        "OPENCRM_DATA_DIR": str(_TMP / "data"),
        "OPENCRM_BASE_URL": "http://testserver",
        "OPENCRM_ROOT_EMAIL": "root@test.local",
        "OPENCRM_ROOT_PASSWORD": "root-initial-pw",
        "OPENCRM_IP_HASH_SALT": "test-salt",
        # Тесты — не развёртывание. Флаг зашит в образ (`OPENCRM_DEPLOYED=1`),
        # а этап `tests` наследует его от этапа `app`: без явного снятия весь
        # набор падал бы в контейнере на «конфиг не доехал» — и падал бы
        # именно там, где его гоняет автообновление перед деплоем.
        "OPENCRM_DEPLOYED": "0",
    }
)

from core.security import passwords  # noqa: E402

passwords.BCRYPT_ROUNDS = 4  # быстрые хэши в тестах


def _build_schema_with_migrations() -> None:
    """Схему тестовой базы поднимают МИГРАЦИИ, а не `create_all`.

    Схему в проекте умеют создавать двое: `alembic upgrade head` (так делает
    docker/entrypoint.sh на сервере) и `Base.metadata.create_all` в lifespan.
    Пока тесты шли вторым путём, они проверяли схему из моделей — а на сервер
    уезжала схема из миграций, и разойтись они могли молча.

    Так и вышло: `deals.stage` был VARCHAR(20) в миграции против String(32) в
    модели, весь набор тестов этого не видел, а на MySQL ключ этапа длиннее
    20 символов обрезался бы, и заявка переставала попадать в свою колонку.

    Теперь каждый прогон тестов — заодно и прогон миграций: сломанная миграция
    роняет набор здесь, а не на развёртывании. `create_all` в lifespan после
    этого просто не находит, что создавать.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    command.upgrade(config, "head")


_build_schema_with_migrations()

from fastapi.testclient import TestClient  # noqa: E402

from web.main import app  # noqa: E402

ROOT_EMAIL = "root@test.local"
ROOT_PASSWORD = "root-secure-password-1"  # после обязательной смены

API = "/api/v1"


def login(client: TestClient, email: str, password: str):
    response = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    if response.status_code == 200:
        client.headers["X-CSRF-Token"] = client.cookies.get("opencrm_csrf", "")
    return response


def register(client: TestClient, name: str, email: str, password: str = "manager-pass-123"):
    return client.post(
        f"{API}/auth/register", json={"name": name, "email": email, "password": password}
    )


def make_manager(root_client: TestClient, email: str, password: str = "manager-pass-123") -> TestClient:
    """Регистрирует менеджера, одобряет root'ом и возвращает залогиненный клиент."""
    anon = TestClient(app)
    response = register(anon, email.split("@")[0], email, password)
    assert response.status_code == 201, response.text
    user_id = response.json()["user"]["id"]
    approve = root_client.post(f"{API}/staff/{user_id}/approve")
    assert approve.status_code == 200, approve.text
    manager = TestClient(app)
    assert login(manager, email, password).status_code == 200
    return manager


def png_bytes(color=(217, 119, 87), size=(640, 480)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def db():
    """Сессия БД для проверок уровнем ниже HTTP — репозитории и общий слой запросов.

    Всё, что в ней сделано, откатывается: тесты в наборе гоняются в обоих
    порядках, и записи, оставленные одним, не должны попадаться на глаза
    другому. Отсюда же требование к самим проверкам — не считать строки во всей
    таблице, а искать свои по приметному имени.
    """
    from database.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="session")
def base_client():
    """Первый клиент: прогоняет lifespan (создание схемы, bootstrap root)."""
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def root_client(base_client) -> TestClient:
    """Root, прошедший обязательную смену пароля."""
    client = TestClient(app)
    response = login(client, ROOT_EMAIL, "root-initial-pw")
    assert response.status_code == 200, response.text
    assert response.json()["must_change_password"] is True

    # до смены пароля рабочие эндпоинты закрыты
    blocked = client.get(f"{API}/clients")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "password_change_required"

    changed = client.post(
        f"{API}/auth/me/password",
        json={"old_password": "root-initial-pw", "new_password": ROOT_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    assert client.get(f"{API}/clients").status_code == 200
    return client


@pytest.fixture(scope="session")
def manager_client(root_client) -> TestClient:
    return make_manager(root_client, "manager@test.local")
