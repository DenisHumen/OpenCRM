"""Интеграционные тесты гоняются против настоящего приложения: настоящая MySQL
и временный storage на каждый прогон.

**Почему настоящая, а не файл.** База у продукта одна — MySQL, и набор обязан
гоняться на ней же. Пока он шёл на файле, зелёный прогон ничего не обещал:
одинаковость двух движков — предположение, и оно уже подводило. На файле
проходила проверка двойного нажатия «Запросить доступ», а на MySQL она давала
500. На файле держалась защита «последний владелец», а на MySQL двое владельцев
снимали root друг с друга разом и запирали систему насмерть. Обе беды были на
боевом сервере всё это время, и не видел их только набор.

Адрес берётся из `OPENCRM_TEST_DB_URL`. Поднять базу под набор:

    docker compose -f docker/docker-compose.tests.yml up --build \
        --abort-on-container-exit --exit-code-from tests

Она эфемерная: данные в tmpfs, после прогона не остаётся ничего."""

import io
import os
import tempfile
from pathlib import Path

import pytest

_PODSKAZKA = "\n".join((
    "Набор гоняется против настоящей MySQL — другой базы у продукта нет.",
    "Задайте OPENCRM_TEST_DB_URL или поднимите базу вместе с набором:",
    "    docker compose -f docker/docker-compose.tests.yml up --build \\",
    "        --abort-on-container-exit --exit-code-from tests",
))


def _adres_bazy() -> str:
    url = os.environ.get("OPENCRM_TEST_DB_URL", "").strip()
    if not url:
        raise RuntimeError(f"OPENCRM_TEST_DB_URL не задан.\n{_PODSKAZKA}")
    if not url.startswith("mysql"):
        # Отдельная проверка, потому что ошибка эта тихая: набор на чужом
        # движке бывает ЗЕЛЁНЫМ и ничего при этом не обещает.
        raise RuntimeError(f"OPENCRM_TEST_DB_URL={url!r} — не MySQL.\n{_PODSKAZKA}")
    return url


# Окружение — до импорта приложения (настройки кэшируются)
_TMP = Path(tempfile.mkdtemp(prefix="opencrm-test-"))
os.environ.update(
    {
        "OPENCRM_ENV": "test",
        "OPENCRM_SECRET_KEY": "test-secret-key",
        # Адрес базы для набора — только снаружи. Умолчания тут нет намеренно:
        # любое сочинённое значение означало бы «прогон пошёл не туда, куда
        # думал человек», а такой прогон хуже несостоявшегося.
        "OPENCRM_DB_URL": _adres_bazy(),
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


def _ochistit_bazu() -> None:
    """Прогон начинается с ПУСТОЙ базы. Иначе второй прогон невозможен.

    Пока набор гонялся на файле, это свойство было даровым: каждый прогон
    получал свой временный файл. С общей MySQL оно исчезло — и исчезло молча,
    потому что один прогон подряд по-прежнему зелёный.

    Вылезло на CI, где набор гоняется дважды: прямым порядком файлов и обратным
    (обратный ловит тесты, которые роняют не себя, а соседа). Первый прогон
    меняет root'у пароль — смена при первом входе обязательна и проверяется, —
    второй стартует на той же базе, и `root-initial-pw` больше не подходит.
    826 ошибок «Invalid email or password», и ни одна не про то, что сломано.

    Чистится содержимое, а не база целиком: `DROP DATABASE` требует прав уровня
    сервера, а они есть не у всякого, кому дали базу под тесты.

    **Отказ по имени базы — не перестраховка.** Этот код стирает всё, что
    найдёт. Единственное, что стоит между ним и чужими данными, — переменная
    окружения, а опечатка в ней стоила бы боевой базы. Поэтому имя обязано
    содержать `test`, и никакое соглашение вместо проверки тут не годится.
    """
    from sqlalchemy import create_engine, text

    url = os.environ["OPENCRM_DB_URL"]
    imya = url.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in imya.lower():
        raise RuntimeError(
            f"база {imya!r} не похожа на тестовую, а набор стирает её содержимое.\n"
            "Имя обязано содержать «test» — это единственное, что отделяет прогон "
            "от чужих данных.\n" + _PODSKAZKA
        )

    engine = create_engine(url)
    try:
        with engine.begin() as soedinenie:
            tablitsy = [
                row[0]
                for row in soedinenie.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
                ))
            ]
            if not tablitsy:
                return
            # Внешние ключи выключаются на время сноса: порядок удаления иначе
            # пришлось бы вычислять по графу связей, а он меняется с каждой
            # миграцией. Признак возвращается на месте, соединение уходит в пул
            # как было.
            soedinenie.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for tablitsa in tablitsy:
                soedinenie.execute(text(f"DROP TABLE IF EXISTS `{tablitsa}`"))
            soedinenie.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    finally:
        engine.dispose()


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


def _ochistit_redis() -> None:
    """Убрать ВСЁ, что осталось в Redis от прошлого прогона.

    База пересоздаётся перед каждым прогоном (`_ochistit_bazu`), а Redis — нет,
    и это несимметрично опасно: **идентификаторы строк начинаются заново, а
    ключи в Redis остаются от прошлых**. Прогон натыкается на чужую границу
    «прочитано» для диалога с тем же номером и на чужой счётчик неудачных
    попыток входа.

    Поймано на CI: он гоняет набор ДВАЖДЫ — прямым порядком файлов и обратным.
    Первый прогон оставлял ключи, второй краснел на них: «непрочитанных 0» там,
    где их два, и «429» там, где ждали «401». В воротах деплоя этого не видно
    вовсе — там прогон один.

    Чистим по общей приставке приложения, а не всю базу Redis: на машине
    разработчика это может быть тот же сервер, что у боевого стенда, и
    `FLUSHDB` унёс бы чужое.
    """
    from core import redis_client

    client = redis_client.get_client()
    if client is None:
        return
    try:
        for klyuch in client.scan_iter(match=f"{redis_client.PREFIX}*", count=500):
            client.delete(klyuch)
    except Exception as beda:  # noqa: BLE001 — Redis не обязан быть живым
        print(f"[тесты] не удалось прибрать Redis: {beda!r}", flush=True)


_ochistit_bazu()
_ochistit_redis()
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


@pytest.fixture
def chistaya_baza(request):
    """Пустая база на том же сервере — для проверок, которым нужна СВОЯ схема.

    Таких проверок хватает: сверка схемы с моделями, откат миграции и накат её
    заново, поведение при недостающей таблице. Все они портят схему нарочно, и
    делать это в базе набора нельзя — соседние проверки идут следом.

    Раньше каждая из них строила себе базу файлом рядом (`sqlite:///tmp/...`).
    Это было удобно и почти бесполезно: сверялась схема, собранная ДРУГИМ
    движком, а расхождение с боевым как раз и есть то, что эти проверки ищут.
    `deals.stage` был VARCHAR(20) в миграции против String(32) в модели, и
    файловая база этого не видела вовсе.

    Теперь база настоящая — отдельная схема на том же сервере, со своим именем
    по имени проверки. Убирается она в любом исходе: остаться на сервере после
    красного прогона она не должна, иначе следующий начнётся на чужих остатках.
    """
    from sqlalchemy import create_engine, text

    osnovnoy = os.environ["OPENCRM_DB_URL"]
    koren, _, hvost = osnovnoy.rpartition("/")
    imya_bazy, _, parametry = hvost.partition("?")
    # Имя по проверке — чтобы в разборе аварии было видно, чья база осталась,
    # если убрать её всё же не вышло. MySQL держит 64 знака.
    ochischennoe = "".join(z if z.isalnum() else "_" for z in request.node.name)
    svoyo = f"t_{ochischennoe}"[:64]
    sluzhebnyy = create_engine(f"{koren}/{imya_bazy}?{parametry}" if parametry else osnovnoy)
    try:
        with sluzhebnyy.connect() as soedinenie:
            soedinenie.execute(text(f"DROP DATABASE IF EXISTS {svoyo}"))
            soedinenie.execute(text(
                f"CREATE DATABASE {svoyo} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            ))
            soedinenie.commit()
        yield f"{koren}/{svoyo}?{parametry}" if parametry else f"{koren}/{svoyo}"
    finally:
        with sluzhebnyy.connect() as soedinenie:
            soedinenie.execute(text(f"DROP DATABASE IF EXISTS {svoyo}"))
            soedinenie.commit()
        sluzhebnyy.dispose()
