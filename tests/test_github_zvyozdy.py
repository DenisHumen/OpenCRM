"""Звёзды GitHub: кэш в базе, поход наружу раз в сутки.

Правило проекта — наружу не ходим. Здесь исключение, названное владельцем, и
проверки стерегут именно то, что делает его исключением, а не дырой: ходит
сервер, раз в сутки, и чужой сбой не доезжает до экрана.
"""

import json
from datetime import timedelta

from core.services import github_service, settings_service
from database.repositories import settings as settings_repo


class _Otvet:
    """Подделка ответа urllib: тот же протокол менеджера контекста."""

    def __init__(self, telo: dict):
        self._telo = json.dumps(telo).encode("utf-8")

    def read(self):
        return self._telo

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _opener(telo: dict, schyot: list):
    def otkryt(zapros, timeout=None):
        schyot.append(getattr(zapros, "full_url", zapros))
        return _Otvet(telo)

    return otkryt


def _polozhit(db, zvyozd: str, kogda):
    settings_repo.write(db, github_service.KLYUCH_ZVYOZD, zvyozd)
    settings_repo.write(db, github_service.KLYUCH_KOGDA, kogda.isoformat())
    db.commit()


def test_svezhee_chislo_beryotsya_iz_bazy_bez_pokhoda_naruzhu(db):
    """Пока число свежее, наружу не ходим вовсе.

    Это и есть смысл кэша: обращение к чужому серверу на каждый показ панели —
    это чужая доступность, ставшая нашей.
    """
    _polozhit(db, "42", github_service._teper())
    schyot: list = []

    assert github_service.zvyozdy(db, opener=_opener({"stargazers_count": 999}, schyot)) == 42
    assert schyot == [], f"сходили наружу при свежем кэше: {schyot}"


def test_protukhshee_chislo_obnovlyaetsya(db):
    """Сутки прошли — спрашиваем заново и запоминаем."""
    _polozhit(db, "42", github_service._teper() - timedelta(hours=25))
    schyot: list = []

    assert github_service.zvyozdy(db, opener=_opener({"stargazers_count": 57}, schyot)) == 57
    assert len(schyot) == 1, "не сходили за обновлением"
    assert "api.github.com" in schyot[0]

    db.commit()
    assert settings_repo.get_row(db, github_service.KLYUCH_ZVYOZD).value == "57"


def test_otkaz_naruzhu_ne_doezzhaet_do_ekrana(db):
    """Не дозвонились — отдаём прошлое число и НЕ продлеваем срок годности.

    Второе не мелочь. Продли мы отметку времени при неудаче — следующая попытка
    ушла бы на сутки вперёд, и одна моргнувшая сеть заморозила бы число до
    завтра.
    """
    staraya = github_service._teper() - timedelta(hours=25)
    _polozhit(db, "42", staraya)

    def padaet(zapros, timeout=None):
        raise OSError("сети нет")

    assert github_service.zvyozdy(db, opener=padaet) == 42
    db.commit()
    assert settings_repo.get_row(db, github_service.KLYUCH_KOGDA).value == staraya.isoformat(), (
        "неудача продлила срок годности — следующая попытка ушла бы на сутки"
    )


def test_bez_kesha_i_bez_seti_otvechaem_nichem(db):
    """Ничего не знаем — говорим `None`, а не ноль.

    Ноль — это утверждение «звёзд нет», которого мы не делали. Экран на `None`
    показывает кнопку без числа.
    """
    settings_repo.write(db, github_service.KLYUCH_ZVYOZD, "")
    settings_repo.write(db, github_service.KLYUCH_KOGDA, "")
    db.commit()

    def padaet(zapros, timeout=None):
        raise OSError("сети нет")

    assert github_service.zvyozdy(db, opener=padaet) is None


def test_sluzhebnye_klyuchi_ne_protekayut_v_nastroyki(db):
    """Кэш лежит в таблице настроек, но настройкой не становится.

    `get_all` отдаёт только объявленные ключи, `update` чужие отвергает. Иначе
    служебное число появилось бы на экране настроек сайта и уехало бы в ответ
    вместе с брендом.
    """
    _polozhit(db, "42", github_service._teper())

    znacheniya = settings_service.get_all(db)
    assert github_service.KLYUCH_ZVYOZD not in znacheniya
    assert github_service.KLYUCH_KOGDA not in znacheniya
