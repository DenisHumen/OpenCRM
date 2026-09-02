"""Контроль места на диске, блокировка загрузок и освобождение места."""

import shutil

import pytest
from fastapi.testclient import TestClient

from config.settings import get_settings
from core.services import media_service, storage_service
from tests.conftest import API, Zaprosy, png_bytes
from web.main import app


@pytest.fixture
def fake_disk(monkeypatch):
    """Подменяет показания диска, чтобы проверить пороги без реального заполнения."""

    def set_usage(total_gb: float, free_gb: float):
        total = int(total_gb * 1024**3)
        free = int(free_gb * 1024**3)
        monkeypatch.setattr(
            shutil, "disk_usage", lambda _path: shutil._ntuple_diskusage(total, total - free, free)
        )

    return set_usage


def test_disk_usage_works_without_root(monkeypatch):
    # никаких внешних утилит и прав: только statvfs через shutil
    usage = storage_service.disk_usage()
    assert usage["total_bytes"] > 0
    assert 0 <= usage["percent_used"] <= 100
    assert usage["free_bytes"] >= 0


def test_levels_by_thresholds(fake_disk):
    settings = get_settings()
    assert storage_service.level_for(500 * 1024**3, 10.0) == storage_service.LEVEL_OK
    assert (
        storage_service.level_for(500 * 1024**3, settings.disk_warning_percent + 1)
        == storage_service.LEVEL_WARNING
    )
    assert (
        storage_service.level_for(500 * 1024**3, settings.disk_critical_percent + 1)
        == storage_service.LEVEL_CRITICAL
    )
    # мало свободного в абсолюте — критично даже при небольшом проценте
    assert storage_service.level_for(1024, 5.0) == storage_service.LEVEL_CRITICAL


def test_storage_endpoint_reports_status(manager_client):
    data = manager_client.get(f"{API}/system/storage").json()
    assert data["level"] in ("ok", "warning", "critical")
    assert data["total_bytes"] > 0
    assert "reclaimable" in data
    assert data["uploads_blocked"] is False


def test_storage_endpoint_requires_auth(base_client):
    assert base_client.get(f"{API}/system/storage").status_code == 401


def test_warning_and_critical_levels_visible_in_api(manager_client, fake_disk):
    fake_disk(total_gb=100, free_gb=15)  # занято 85% → предупреждение
    data = manager_client.get(f"{API}/system/storage").json()
    assert data["level"] == "warning"
    assert data["uploads_blocked"] is False

    fake_disk(total_gb=100, free_gb=0.2)  # занято 99.8% и меньше запаса → критично
    data = manager_client.get(f"{API}/system/storage").json()
    assert data["level"] == "critical"
    assert data["uploads_blocked"] is True


def test_upload_blocked_when_disk_is_full(manager_client, fake_disk):
    board = manager_client.post(f"{API}/boards", json={"title": "Диск полон"}).json()
    fake_disk(total_gb=100, free_gb=0.1)

    response = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "disk_full"

    client = manager_client.post(f"{API}/clients", json={"name": "Диск"}).json()
    file_response = manager_client.post(
        f"{API}/clients/{client['id']}/files",
        files={"file": ("brief.pdf", b"%PDF-1.4 data", "application/pdf")},
    )
    assert file_response.status_code == 422
    assert file_response.json()["error"]["code"] == "disk_full"


def test_deleting_share_keeps_board_files(manager_client):
    """Ссылка и работы — разные сущности: удаление ссылки не трогает файлы."""
    board = manager_client.post(f"{API}/boards", json={"title": "Ссылка и файлы"}).json()
    work = manager_client.post(
        f"{API}/boards/{board['id']}/works",
        files={"file": ("w.png", png_bytes(), "image/png")},
    ).json()
    manager_client.patch(f"{API}/boards/{board['id']}", json={"is_published": True})
    first = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()
    second = manager_client.post(f"{API}/boards/{board['id']}/shares", json={}).json()

    work_dir = media_service.work_dir(
        manager_client.get(f"{API}/boards/{board['id']}/works/{work['id']}").json()["media"]["card"].split("/")[2]
    )
    assert work_dir.exists()

    assert manager_client.delete(f"{API}/shares/{first['id']}").status_code == 200
    assert TestClient(app).get(f"/b/{first['token']}").status_code == 404
    # вторая ссылка и файлы живы
    assert TestClient(app).get(f"/b/{second['token']}").status_code == 200
    assert work_dir.exists()
    assert manager_client.get(f"{API}/boards/{board['id']}").json()["works"]


def test_purge_frees_disk_space(root_client, manager_client):
    # Корзина копит мягко удалённых клиентов (их можно восстановить); purge их вычищает.
    # Доски удаляются сразу вместе с файлами — см. tests/test_files.py.
    client = manager_client.post(f"{API}/clients", json={"name": "На удаление"}).json()
    upload = manager_client.post(
        f"{API}/clients/{client['id']}/files",
        files={"file": ("brief.pdf", b"%PDF-1.4 " + b"x" * 5000, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text

    # мягкое удаление клиента файлы пока не трогает — данные ещё можно вернуть
    manager_client.delete(f"{API}/clients/{client['id']}")

    before = root_client.get(f"{API}/system/storage").json()["reclaimable"]
    assert before["clients"] >= 1 and before["bytes"] > 0

    result = root_client.post(f"{API}/system/storage/purge").json()
    assert result["clients"] >= 1
    assert result["freed_bytes"] > 0

    # Проверяем судьбу своего клиента, а не общий счётчик до нуля. Нулём тест
    # требовал, чтобы корзина после уборки была пуста, — а это неправда даже в
    # рабочей системе: клиента с выигранной заявкой уборка не трогает нарочно.
    # Стоило соседнему тесту оставить такого, и этот падал на чужих данных;
    # в полном наборе спасал только случайный порядок.
    after = root_client.get(f"{API}/system/storage").json()["reclaimable"]
    assert after["clients"] < before["clients"], "корзина не убавилась"
    assert after["bytes"] < before["bytes"], "место не освободилось"
    assert root_client.get(f"{API}/clients/{client['id']}").status_code == 404


def test_purge_is_root_only(manager_client):
    assert manager_client.post(f"{API}/system/storage/purge").status_code == 403


def test_format_bytes():
    assert storage_service.format_bytes(512) == "512 B"
    assert storage_service.format_bytes(2 * 1024**3).startswith("2.0 GB")


def test_cleanup_asks_the_database_a_fixed_number_of_questions(root_client, manager_client):
    """Уборка не растёт запросами вместе с корзиной.

    Раньше на каждого клиента уходило по три запроса — «есть ли выручка»,
    «сколько заявок», «какие файлы», — и по одному на каждую доску. На базе,
    ради которой уборку и запускают, это тысячи запросов внутри одной
    транзакции: минуты ожидания там, где работы на секунду.

    Считаем не время, а сами запросы: время на стенде зависит от диска и
    соседей, а число обращений — только от кода. Проверка сравнивает два
    прогона, у которых разница лишь в количестве мусора: если запросов стало
    больше, значит какой-то снова ушёл в цикл.
    """

    def purge_with_counting(clients_count: int) -> int:
        for i in range(clients_count):
            created = manager_client.post(
                f"{API}/clients", json={"name": f"Мусор {clients_count}-{i}"}
            ).json()
            manager_client.delete(f"{API}/clients/{created['id']}")

        # Холостой заход: первый запрос после чужого сброса кэша платит за
        # чтение настроек и состава блоков, а тест не про них.
        root_client.get(f"{API}/system/storage")

        # Общий счётчик из `conftest`, а не свой слушатель. Свой не замораживал
        # кэши со сроком годности (блоки системы и режим обслуживания живут
        # две секунды), и обновление одного из них попадало в замер по часам,
        # а не по делу: в обратном порядке файлов проверка краснела «было 12,
        # стало 13» на неизменном коде.
        #
        # Две копии одного инструмента расходятся ровно так: беду чинят в
        # одной, а вторая продолжает мигать.
        with Zaprosy() as z:
            assert root_client.post(f"{API}/system/storage/purge").status_code == 200
        return len(z.spisok)

    # Сначала сливаем чужое, и только потом меряем.
    #
    # **Без этого два замера несравнимы.** База у набора ОБЩАЯ: к моменту, когда
    # проверка доходит до дела, в корзине лежат сотни карточек от соседних
    # файлов. Первый замер вычищал ВЕСЬ этот хлам, второй — только свои
    # двенадцать, и сравнивались не «мало мусора против многого», а «чужая
    # свалка против нашей горстки». Проходило это или нет, зависело от того,
    # сколько мусора оставили соседи, то есть от порядка файлов.
    #
    # Поймано полным прогоном: в одиночку и файлом проверка зелёная, а в наборе
    # краснела. Холостая уборка ставит оба замера в одинаковые условия.
    assert root_client.post(f"{API}/system/storage/purge").status_code == 200

    few = purge_with_counting(2)
    many = purge_with_counting(12)

    # Допуск в два запроса, а не «ни одного лишнего». Стережём мы РОСТ НА
    # СТРОКУ: он стоил бы по три запроса на клиента, то есть тридцать на этих
    # десятерых, — и допуск его ловит с тридцатикратным запасом. А ровное
    # равенство ловило заодно и то, чем код не управляет: границу пачки внутри
    # уборки и обновление кэша, попавшее в замер по часам. Проверка краснела
    # на неизменном коде примерно в каждом четвёртом прогоне ворот — и красное,
    # которому не верят, хуже отсутствующей проверки.
    assert many <= few + 2, (
        f"уборка десяти лишних клиентов стоила лишних запросов: было {few}, стало {many}"
    )
