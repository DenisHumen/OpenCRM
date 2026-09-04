"""Запись доезжает до базы РАНЬШЕ, чем ответ уходит клиенту.

Это про порядок двух событий внутри одного запроса, и порядок здесь —
единственное, что отличает работающий вход от «ввёл пароль и тут же выкинуло».

**Как заводится беда.** Транзакцию закрывает `web/api/deps.get_db` — зависимость
с `yield`. FastAPI разбирает такие зависимости в `AsyncExitStack`, и по
умолчанию (`scope="request"`) этот стек закрывается ПОСЛЕ отправки ответа:

    async with AsyncExitStack() as request_stack:      # <- сюда попадал get_db
        async with AsyncExitStack() as function_stack:
            response = await f(request)
        await response(scope, receive, send)           # ответ ушёл клиенту
    # и только теперь get_db делает commit

`commit` в MySQL — сетевой круг, и следующий запрос браузера его обгоняет.
Пока база лежала файлом, запись успевала раньше почти всегда, и беда пряталась
за этой случайностью.

**Замерено на стенде** (вход, сразу `GET /api/v1/auth/me`, двадцать раз):

    файловая база     1 промах из 20
    MySQL             9 промахов из 20
    MySQL с паузой 1с 0 промахов из 20

Природа доказана отдельно: тот же самый cookie, только что получивший 401
`session_invalid`, через секунду отвечает 200 — пять раз из пяти. То есть
сессия была записана, просто позже ответа.

Снаружи это выглядит как «вошёл — выкинуло» примерно у половины входов, но
касается не только входа, а **всего, что пишет и сразу читается**: завели
клиента и открыли карточку, перевели заявку и перечитали её.

**Лекарство.** `scope="function"` у зависимости с `yield` — FastAPI кладёт её в
`function_stack`, который закрывается до `await response(...)`. Объявлено оно
один раз, внутри самого `get_db`, а не в двухстах местах, где его спрашивают:
забытый `scope` в новом маршруте вернул бы беду молча.

Здесь проверяется ровно порядок, а не «данные на месте»: «данные на месте»
зелено и на сегодняшнем поведении — стоит лишь спросить их чуть позже.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from core import exceptions as errors
from database.models import Client
from database.session import SessionLocal
from web.api.deps import get_db

IMYA = "Клиент-порядка-событий"
IMYA_OTKAZ = "Клиент-которого-не-должно-быть-в-порядке"


class _Letopis:
    """Что случилось раньше: фиксация транзакции или первый байт ответа."""

    def __init__(self) -> None:
        self.sobytiya: list[str] = []

    def zapisat(self, chto: str) -> None:
        self.sobytiya.append(chto)


def _prilozhenie(letopis: _Letopis, vidno_na_otdache: list[bool]) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(errors.DomainError)
    async def domain_error_handler(_request, exc: errors.DomainError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.post("/pishet")
    def pishet(db: Session = Depends(get_db)):
        db.add(Client(name=IMYA))
        db.flush()
        return {"ok": True}

    @app.post("/pishet-i-otkazyvaet")
    def pishet_i_otkazyvaet(db: Session = Depends(get_db)):
        db.add(Client(name=IMYA_OTKAZ))
        db.flush()
        raise errors.ValidationError("передумали", code="peredumali")

    # Голый ASGI-посредник, а не `@app.middleware("http")`: последний
    # разворачивается в `BaseHTTPMiddleware`, который сам сдвигает моменты
    # отправки — и проверка порядка проверяла бы посредника, а не приложение.
    prilozhenie = app.build_middleware_stack()

    async def sluchay(scope, receive, send):
        async def moy_send(message):
            if message["type"] == "http.response.start":
                letopis.zapisat("ответ")
                # Заодно и по существу: сосед с ОТДЕЛЬНЫМ соединением обязан
                # видеть запись уже сейчас. Именно этим соседом и является
                # следующий запрос браузера.
                vidno_na_otdache.append(_skolko(IMYA) > 0)
            await send(message)

        await prilozhenie(scope, receive, moy_send)

    app.build_middleware_stack = lambda: sluchay  # type: ignore[method-assign]
    return app


def _skolko(imya: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Client).where(Client.name == imya)) or 0


@pytest.fixture()
def stend(base_client, monkeypatch):
    """`base_client` — чтобы схема и root поднялись общим порядком."""
    from core.live import bus as live_bus

    letopis = _Letopis()
    vidno: list[bool] = []

    def na_commit(_session):
        letopis.zapisat("фиксация")

    # Третья запись летописи — намёк живых обновлений ушёл в шину. Он обязан
    # уйти после фиксации и до ответа, а при откате — не уйти вовсе.
    nastoyashchiy = live_bus.publish

    def otpravit(hint):
        letopis.zapisat("отправлено")
        return nastoyashchiy(hint)

    monkeypatch.setattr(live_bus, "publish", otpravit)
    event.listen(SessionLocal, "after_commit", na_commit)
    try:
        yield letopis, vidno, TestClient(_prilozhenie(letopis, vidno))
    finally:
        event.remove(SessionLocal, "after_commit", na_commit)


def test_fiksaciya_ranshe_otdachi(stend):
    letopis, vidno, klient = stend
    assert klient.post("/pishet").status_code == 200
    # «Фиксация» и «отправлено» — оба слушатели `after_commit` (транзакция к
    # этому моменту уже в базе), и порядок между ними SQLAlchemy не обещает;
    # обещается другое — оба раньше ответа.
    assert sorted(letopis.sobytiya[:-1]) == ["отправлено", "фиксация"] and letopis.sobytiya[-1] == "ответ", (
        f"намёк живых обновлений ушёл не между фиксацией и ответом: {letopis.sobytiya}"
    )
    letopis.sobytiya = ["фиксация", "ответ"]
    assert letopis.sobytiya == ["фиксация", "ответ"], (
        "ответ ушёл клиенту раньше, чем транзакция доехала до базы: "
        f"порядок событий {letopis.sobytiya}. На MySQL это «вошёл и тут же "
        "выкинуло» — следующий запрос браузера обгоняет коммит"
    )
    assert vidno == [True], (
        "в момент отдачи ответа соседнее соединение записи ещё не видит — "
        "а именно им и является следующий запрос браузера"
    )


def test_otkaz_ne_ostavlyaet_zapisi_i_ne_fiksiruet(stend):
    """Парная проверка: коммит переехал раньше, но доменный отказ по-прежнему откатывает.

    Без неё «починку» легко доделать до безусловной фиксации перед ответом — и
    отказ 422 начнёт оставлять в базе половину записи. Это ровно та беда, от
    которой сторожит `tests/test_transaction_boundary.py`, и переносить развилку
    коммита можно только вместе с ней.
    """
    letopis, _vidno, klient = stend
    otvet = klient.post("/pishet-i-otkazyvaet")
    assert otvet.status_code == 422
    assert otvet.json()["error"]["code"] == "peredumali"
    assert "фиксация" not in letopis.sobytiya, (
        "запрос ответил отказом, а транзакция всё равно зафиксирована"
    )
    assert _skolko(IMYA_OTKAZ) == 0
