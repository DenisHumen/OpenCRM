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

import sqlite3

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import exceptions as errors
from database.models import Client
from database.session import SessionLocal, engine, tochka_otkata
from web.api.deps import get_db

IMYA_OTKAZ = "Клиент-которого-не-должно-быть"
IMYA_UDACHA = "Клиент-который-должен-остаться"


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

    return app


def _skolko(imya: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Client).where(Client.name == imya)) or 0


@pytest.fixture()
def klient(base_client):
    """`base_client` — чтобы схема и root уже были подняты общим порядком."""
    return TestClient(_prilozhenie())


def test_otkaz_ne_ostavlyaet_poloviny_zapisi(klient):
    otvet = klient.post("/pishet-i-otkazyvaet")
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "peredumali"
    assert _skolko(IMYA_OTKAZ) == 0, (
        "запрос ответил отказом, но строка осталась в базе — значит исключение "
        "не дошло до get_db и вместо отката случился commit"
    )


def _vtoroe_soedinenie() -> sqlite3.Connection:
    """Отдельное соединение с той же базой — «сосед», работающий одновременно.

    Таймаут короткий нарочно: нам нужен быстрый ответ «занято», а не ожидание.
    """
    return sqlite3.connect(engine.url.database, timeout=0.2)


def test_tochka_otkata_beryot_zamok_zapisi_zaranee(base_client):
    """Точка отката объявляет намерение писать — и берёт блокировку сразу.

    Апгрейд-дедлок: `SAVEPOINT` открывает транзакцию, внутри неё случается
    чтение, транзакция получает снимок базы, а следующая за ним запись обязана
    повыситься до писателя. Если сосед за это время что-то записал, повышение
    невозможно, и SQLite отвечает «занято» НЕМЕДЛЕННО, не выжидая
    `busy_timeout`: ждать бессмысленно, ни одна сторона не уступит. Наружу это
    выходит пятисоткой, и таймаутом не лечится — таймаут тут ни при чём.

    Воспроизведено на настоящем uvicorn: четыре потока с событиями АТС под
    одним `call_id` роняли `sqlite3.OperationalError: database is locked`.
    После починки те же 4×12 запросов проходят все.

    Здесь проверяется само лекарство: пока идёт запись под точкой отката,
    соседу есть чего ждать, а не на что наткнуться.
    """
    db = SessionLocal()
    try:
        with tochka_otkata(db):
            sosed = _vtoroe_soedinenie()
            try:
                with pytest.raises(sqlite3.OperationalError):
                    sosed.execute("BEGIN IMMEDIATE")
            finally:
                sosed.close()
    finally:
        db.rollback()
        db.close()


def test_chtenie_nikogo_ne_zapiraet(base_client):
    """Парная проверка: читающие запросы обязаны идти параллельно.

    Без неё «починку» легко доделать до блокировки на весь запрос — и тогда
    все записи в CRM начнут ждать загрузки файла на доску или ответа от АТС,
    потому что и то, и другое случается внутри запроса до записи. Это медленнее,
    чем было до починки, и незаметнее, чем пятисотка.
    """
    db = SessionLocal()
    try:
        db.execute(select(func.count()).select_from(Client)).scalar()
        sosed = _vtoroe_soedinenie()
        try:
            sosed.execute("BEGIN IMMEDIATE")
            sosed.rollback()
        finally:
            sosed.close()
    finally:
        db.rollback()
        db.close()


def test_udachnyy_zapros_vsyo_zhe_sokhranyaet(klient):
    """Парная проверка: без неё откат можно «починить» до того, что не пишется ничего.

    Первая проверка одна прошла бы и на приложении, которое просто не сохраняет
    данные, — а это худшая из двух бед.
    """
    assert klient.post("/pishet-i-otvechaet").status_code == 200
    assert _skolko(IMYA_UDACHA) == 1
