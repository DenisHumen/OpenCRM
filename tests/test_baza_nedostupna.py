"""Обрыв связи с базой: 503 «переспросите», а не пятисотая с трейсом.

Перезапуск MySQL под живым приложением даёт несколько секунд отказов, и это не
ошибка кода — пул сам заменяет мёртвые соединения. Но 19.09.2026 такой
перезапуск на боевом сервере выглядел аварией: голые пятисотые у людей и трейс
строк на сто семьдесят на каждый отказ. Здесь сторожим обе стороны — обрыв
становится 503, а настоящая ошибка запроса остаётся громкой.
"""

import pymysql
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from core.services import maintenance_mode
from tests.conftest import API
from web.main import app
from web.middleware import POVTOR_CHEREZ, baza_nedostupna


def _obryv(kod=2013, soobshchenie="Lost connection to MySQL server during query"):
    """Ровно та ошибка, что пришла с боевого сервера."""
    return OperationalError("SELECT 1", {}, pymysql.err.OperationalError(kod, soobshchenie))


@pytest.fixture
def baza_lezhit(monkeypatch):
    """Режим обслуживания читает базу на КАЖДОМ запросе — там обрыв и случается
    первым. Подменяем именно его: так проверяется настоящий путь запроса."""

    def upala(_db):
        raise _obryv()

    monkeypatch.setattr(maintenance_mode, "state", upala)


@pytest.mark.parametrize("kod", [2003, 2006, 2013, 2055])
def test_opoznayotsya_nedostupnost(kod):
    assert baza_nedostupna(_obryv(kod)) is True


@pytest.mark.parametrize(
    "kod",
    [
        1213,  # взаимная блокировка — беда самого запроса
        1205,  # ожидание замка вышло
        1062,  # дубль ключа
    ],
)
def test_oshibka_zaprosa_ne_vydayotsya_za_nedostupnost(kod):
    assert baza_nedostupna(_obryv(kod, "что-то своё")) is False


def test_opoznaetsya_po_metke_sqlalchemy():
    """Обрыв, опознанный самим SQLAlchemy, — недоступность при любом коде."""
    oshibka = OperationalError("SELECT 1", {}, Exception("гадость"), connection_invalidated=True)
    assert baza_nedostupna(oshibka) is True


def test_chuzhoe_isklyuchenie_ne_trogaetsya():
    assert baza_nedostupna(ValueError("не база")) is False


def test_api_pri_obryve_otvechaet_503_s_povtorom(baza_lezhit):
    otvet = TestClient(app).get(f"{API}/clients")
    assert otvet.status_code == 503
    assert otvet.json()["error"]["code"] == "db_unavailable"
    assert otvet.headers["retry-after"] == str(POVTOR_CHEREZ)
    assert otvet.headers["cache-control"] == "no-store"


def test_stranitsa_pri_obryve_obnovlyaetsya_sama(baza_lezhit):
    """Человеку не нужно знать про перезапуск — ему нужно, чтобы через пять
    секунд открылось то, что он открывал."""
    otvet = TestClient(app).get("/", headers={"Accept-Language": "ru-RU,ru;q=0.9"})
    assert otvet.status_code == 503
    assert f'http-equiv="refresh" content="{POVTOR_CHEREZ}"' in otvet.text
    assert "перезапускается" in otvet.text
    # Заголовки безопасности достаются и этому ответу: посредник стоит под ними.
    assert "content-security-policy" in otvet.headers


def test_healthz_pri_obryve_ne_pyatisotaya(baza_lezhit):
    """Здоровье идёт мимо режима обслуживания, но читает базу само — обрыв там
    тоже 503, а не трейс."""
    otvet = TestClient(app).get("/healthz")
    assert otvet.status_code == 503
    assert otvet.json()["error"]["code"] == "db_unavailable"


def test_nastoyashchaya_oshibka_ostayotsya_gromkoy(monkeypatch):
    """Взаимная блокировка — не «переспросите», а ошибка, которую надо видеть.
    Спрячь её посредник — трейс пропал бы как раз тогда, когда он нужен."""

    def upala(_db):
        raise _obryv(1213, "Deadlock found when trying to get lock")

    monkeypatch.setattr(maintenance_mode, "state", upala)
    with pytest.raises(OperationalError):
        TestClient(app).get(f"{API}/clients")
