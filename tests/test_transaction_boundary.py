"""Отказ не оставляет половины записи.

Правило простое и почти невидимое: **запрос, ответивший ошибкой, не должен
оставить в базе ничего**. Держится оно на одной развилке в `web/api/deps.get_db`
— `commit` на выходе, `rollback` на исключении, — и вся система молча полагается
на то, что доменная ошибка до этой развилки доходит.

Полагаться тут есть на что: FastAPI до версии 0.106 разбирал зависимости с
`yield` ПОСЛЕ того, как отработает обработчик исключения. То есть исключение до
`get_db` не доезжало, ветка `except` не срабатывала, и выполнялся обычный
`commit` — с половиной записи внутри. Сейчас в проекте 0.139, где это исправлено,
но обновления фреймворка случаются, а беда от такого отката поведения была бы
тихой: ответ 422 на экране и лишняя строка в базе.

Отсюда проверка не про какой-то один сервис, а про саму развилку: маленькое
приложение с настоящим `get_db` и настоящим обработчиком доменных ошибок.
Найдена эта беда была на разборе телефонии — отказ по неверному полю оставлял
в журнале обрубок звонка, — но место у неё общее, и чинить её по одному сервису
значит чинить бесконечно.
"""

import pathlib

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core import exceptions as errors
from database.models import Client
from database.session import SessionLocal
from web.api.deps import get_db

IMYA_OTKAZ = "Клиент-которого-не-должно-быть"
IMYA_UDACHA = "Клиент-который-должен-остаться"
IMYA_AVARIYA = "Клиент-написанный-перед-смертью-базы"


def _prilozhenie() -> FastAPI:
    """То же, что в web/main.py: обработчик доменных ошибок плюс `get_db`."""
    app = FastAPI()

    @app.exception_handler(errors.DomainError)
    async def domain_error_handler(_request, exc: errors.DomainError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.post("/pishet-i-otkazyvaet")
    def pishet_i_otkazyvaet(db: Session = Depends(get_db)):
        # Ровно тот порядок, из-за которого всё и случается: сначала пишем,
        # потом обнаруживаем, что запрос негодный. Порядок этот встречается в
        # каждом сервисе, где проверка опирается на уже сохранённое.
        db.add(Client(name=IMYA_OTKAZ))
        db.flush()
        raise errors.ValidationError("передумали", code="peredumali")

    @app.post("/pishet-i-otvechaet")
    def pishet_i_otvechaet(db: Session = Depends(get_db)):
        db.add(Client(name=IMYA_UDACHA))
        db.flush()
        return {"ok": True}

    @app.post("/pishet-i-teryaet-bazu")
    def pishet_i_teryaet_bazu(db: Session = Depends(get_db)):
        # Так это и выглядит на самом деле: строка записана, а следующий запрос
        # уже некому обслужить — база умерла между двумя обращениями.
        db.add(Client(name=IMYA_AVARIYA))
        db.flush()
        raise OperationalError("INSERT …", {}, Exception("база не отвечает"))

    return app


def _skolko(imya: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Client).where(Client.name == imya)) or 0


@pytest.fixture()
def klient(base_client):
    """`base_client` — чтобы схема и root уже были подняты общим порядком."""
    return TestClient(_prilozhenie())


@pytest.fixture()
def klient_bez_perekhvata(base_client):
    """Тот же клиент, но код ответа виден вместо всплывшего исключения.

    Наружу — в докер, в nginx, в обновлятор — уходит именно код, а не
    трассировка, и проверять надо его.
    """
    return TestClient(_prilozhenie(), raise_server_exceptions=False)


def test_otkaz_ne_ostavlyaet_poloviny_zapisi(klient):
    otvet = klient.post("/pishet-i-otkazyvaet")
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "peredumali"
    assert _skolko(IMYA_OTKAZ) == 0, (
        "запрос ответил отказом, но строка осталась в базе — значит исключение "
        "не дошло до get_db и вместо отката случился commit"
    )


def test_udachnyy_zapros_vsyo_zhe_sokhranyaet(klient):
    """Парная проверка: без неё откат можно «починить» до того, что не пишется ничего.

    Первая проверка одна прошла бы и на приложении, которое просто не сохраняет
    данные, — а это худшая из двух бед.
    """
    assert klient.post("/pishet-i-otvechaet").status_code == 200
    assert _skolko(IMYA_UDACHA) == 1


def test_upavshaya_baza_tozhe_ne_ostavlyaet_poloviny(klient_bez_perekhvata):
    """Отказ БАЗЫ посреди запроса не оставляет строки — как и доменная ошибка.

    Случай не выдуманный. Проверено на живом стеке: транзакция пишет строку,
    ждёт, mysqld убивают с хоста SIGKILL, следующая запись получает
    `Lost connection to MySQL server during query`. После возвращения базы в
    ней **ноль** строк от этой транзакции — ровно то, что закреплено здесь.

    **Проверка пиннит ИСХОД, а не устройство, и это выяснилось замером.**
    Напрашивалось объяснение «сузят `except Exception` до `DomainError` — и
    набор останется зелёным, а половина записи поедет в базу». Оно неверно:
    сужение поставили и прогнали — все три проверки зелёные. Держит исход
    вторая вещь, `db.close()` в `finally`: незакоммиченную транзакцию
    SQLAlchemy откатывает при возврате соединения в пул. То есть инвариант
    стоит на двух ногах сразу, и подпиливание одной его не роняет.

    Что проверка ловит вправду — тоже замерено: `commit`, переехавший в
    `finally` («сохраним, что успели»), красит и её, и соседку выше.
    """
    otvet = klient_bez_perekhvata.post("/pishet-i-teryaet-bazu")
    assert otvet.status_code == 500, "отказ базы обязан быть виден кодом ответа"
    assert _skolko(IMYA_AVARIYA) == 0, (
        "база отказала посреди запроса, а строка осталась — значит вместо "
        "отката случился commit"
    )


def test_imenovannye_zamki_snimayutsya_posle_fiksatsii():
    """Порядок в `finally`: сначала `commit`, потом снятие замков, потом `close`.

    Порядок здесь и есть вся защита, а по коду этого не видно — три строки
    выглядят взаимозаменяемыми. Они не взаимозаменяемы:

    - снять замок ДО `commit` — значит впустить соперника раньше, чем чужая
      запись станет видимой. Замок при этом есть, а толку от него нет. Так и
      было в первой редакции (снятие стояло в `finally` самого сервиса), и
      дуэль формы с сайта давала те же две карточки, что и без замка вовсе;
    - снять ПОСЛЕ `close` — некуда: сессия закрыта, запрос выполнить нечем;
    - не снять вовсе — соединение вернётся в пул запертым. `Session.close()`
      его не закрывает, а MySQL держит именованный замок за соединением, и
      следующая заявка от того же контакта прождёт впустую весь срок.

    Проверка текстовая, потому что проверять надо ПОРЯДОК СТРОК, а не поведение:
    поведение при неверном порядке отличается только под гонкой, то есть
    воспроизводится через раз.
    """
    istochnik = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "api" / "deps.py"
    ).read_text(encoding="utf-8")

    telo = istochnik[istochnik.index("def _edinica_raboty(") :]
    telo = telo[: telo.index("\ndef ")]

    gde_commit = telo.index("db.commit()")
    gde_snyatie = telo.index("snyat_zamki(db)")
    gde_close = telo.index("db.close()")

    assert gde_commit < gde_snyatie, (
        "замки снимаются до фиксации — соперник получит очередь раньше, чем "
        "чужая запись станет видимой, и замок перестанет что-либо значить"
    )
    assert gde_snyatie < gde_close, "замки снимаются после закрытия сессии — нечем"
