"""Змейка со страницы обслуживания: приём счёта и таблица лидеров.

Маршруты публичные и без входа в систему — играют посетители, пока сайт
обновляется. Значит проверять надо не «а работает ли», а «что будет, если сюда
постучится не игрок»: счёт присылают запросом, и поверить на слово нельзя.
"""

from fastapi.testclient import TestClient

from core.services import arcade_service
from tests.conftest import API
from web.main import app

ARCADE = f"{API}/arcade"


def anon(base_client: TestClient) -> TestClient:
    """Клиент без входа в систему.

    Аргумент нужен не для запроса, а чтобы фикстура прогнала lifespan: схему
    создаёт именно он. Без этого файл проходил бы только вместе с остальными.
    """
    return TestClient(app)


def test_score_is_accepted_without_any_login(base_client):
    """Играет посетитель витрины, аккаунта у него нет и быть не должно."""
    response = anon(base_client).post(ARCADE + "/scores", json={"name": "Гость", "score": 12, "played_ms": 9000})
    assert response.status_code == 201, response.text
    assert response.json()["score"] == 12


def test_leaderboard_is_public_and_sorted(base_client):
    client = anon(base_client)
    client.post(ARCADE + "/scores", json={"name": "Тихий", "score": 3, "played_ms": 4000})
    client.post(ARCADE + "/scores", json={"name": "Громкий", "score": 40, "played_ms": 30000})

    items = client.get(ARCADE + "/leaderboard").json()["items"]
    assert items, "таблица пуста, хотя результаты отправляли"
    scores = [row["score"] for row in items]
    assert scores == sorted(scores, reverse=True), "лидеры идут не по убыванию"
    assert items[0]["score"] >= 40


def test_score_above_the_board_capacity_is_rejected(base_client):
    """Поле 20×20 и старт длиной 3 — больше 397 яблок на нём не помещается.

    Предел взят из самой игры, а не выдуман: он не может отсечь честного игрока.
    """
    over = arcade_service.MAX_SCORE + 1
    response = anon(base_client).post(
        ARCADE + "/scores", json={"name": "Читер", "score": over, "played_ms": 10_000_000}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "score_out_of_range"


def test_score_that_could_not_be_played_in_that_time_is_rejected(base_client):
    """Между двумя яблоками проходит хотя бы такт, а такт не быстрее 60 мс."""
    response = anon(base_client).post(
        ARCADE + "/scores", json={"name": "Быстрый", "score": 300, "played_ms": 500}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "score_too_fast"


def test_zero_and_negative_scores_are_rejected(base_client):
    for bad in (0, -5):
        response = anon(base_client).post(ARCADE + "/scores", json={"score": bad, "played_ms": 5000})
        assert response.status_code == 422, bad


def test_name_is_trimmed_and_never_empty(base_client):
    """Имя попадает в публичную таблицу: длину режем, пустое заменяем."""
    response = anon(base_client).post(
        ARCADE + "/scores",
        json={"name": "  " + "я" * 80 + "  ", "score": 2, "played_ms": 3000},
    )
    assert response.status_code == 201
    assert len(response.json()["name"]) <= arcade_service.MAX_NAME

    blank = anon(base_client).post(ARCADE + "/scores", json={"name": "   ", "score": 2, "played_ms": 3000})
    assert blank.json()["name"] == "Аноним"


def test_one_address_cannot_flood_the_table(base_client):
    """Отправка идёт без аккаунта — без ограничения таблицу забьют за минуту."""
    client = anon(base_client)
    codes = [
        client.post(ARCADE + "/scores", json={"score": 1, "played_ms": 2000}).status_code
        for _ in range(arcade_service.MAX_PER_HOUR + 6)
    ]
    assert 429 in codes, "частота отправки ничем не ограничена"


def test_the_page_and_the_server_agree_on_the_field():
    """Поле в игре и предел на сервере должны быть выведены из одних чисел.

    Разойдись они — сервер начал бы отклонять результаты, которые игра честно
    позволяет набрать, и таблица лидеров тихо перестала бы пополняться сверху.
    """
    from pathlib import Path

    page = (
        Path(__file__).resolve().parent.parent
        / "docker" / "nginx" / "maintenance" / "maintenance.html"
    ).read_text(encoding="utf-8")
    assert f"GRID = {arcade_service.GRID}" in page
    assert f"START_LENGTH = {arcade_service.START_LENGTH}" in page
