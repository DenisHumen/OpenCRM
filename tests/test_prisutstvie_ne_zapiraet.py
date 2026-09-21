"""«Последний раз в сети» не запирает строку сотрудника на весь запрос.

БЕДА, СЛУЧИВШАЯСЯ НА БОЕВОМ СЕРВЕРЕ 21.09.2026. Отметка присутствия ставилась
через ORM, а грязная строка уходит в базу на первом же `autoflush` — то есть в
НАЧАЛЕ запроса, — и запирала строку сотрудника до конца транзакции. Браузер шлёт
запросы пачкой, и соседний запрос ТОГО ЖЕ человека упирался в эту строку, ждал
`innodb_lock_wait_timeout` (полсотни секунд) и получал
`(1205, 'Lock wait timeout exceeded')` и пятисотую. В журнале так падали
`/live` и состояние хранилища.

Чтением такое не находится: код верен, запрос верен, а замок держится не там,
где на него смотрят. Поэтому здесь дуэль — второе соединение со своим, коротким
потолком ожидания.
"""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select, text

from core.services import auth_service
from core.utils import now_utc
from database.models import User
from database.session import SessionLocal

from web.api.deps import SESSION_COOKIE


@pytest.fixture
def sosed():
    """Второе соединение к той же базе — «соседний запрос».

    READ COMMITTED осознанно: под REPEATABLE READ соединение видело бы снимок
    на момент своего начала, и проверка врала бы о том, что успело записаться.
    Потолок ожидания — секунда вместо полусотни: беда воспроизводится за ту же
    секунду, а красный набор не должен стоить минуты простоя.
    """
    import os

    dvizhok = create_engine(os.environ["OPENCRM_DB_URL"], isolation_level="READ COMMITTED")
    try:
        with dvizhok.connect() as soedinenie:
            soedinenie.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            yield soedinenie
    finally:
        dvizhok.dispose()


def _otmetka_ustarela(nomer: int) -> None:
    """Состарить отметку: иначе присутствие не трогают вовсе.

    Без этого обе проверки зеленели бы ВХОЛОСТУЮ — `get_user_by_session`
    отмечает присутствие не чаще раза в минуту (`PRESENCE_TOUCH_SECONDS`), а
    клиент набора только что ходил по ручкам.
    """
    svoya = SessionLocal()
    try:
        svoya.execute(
            text("UPDATE users SET last_seen_at = :kogda WHERE id = :nomer"),
            {"kogda": now_utc() - timedelta(hours=1), "nomer": nomer},
        )
        svoya.commit()
    finally:
        svoya.close()


def _token(client) -> str:
    znachenie = client.cookies.get(SESSION_COOKIE)
    assert znachenie, "у клиента нет сессии — тест не о том"
    return znachenie


def test_zapros_ne_derzhit_stroku_sotrudnika(root_client, sosed):
    """Пока запрос работает, строка сотрудника свободна.

    Здесь воспроизведён именно момент беды: сессия запроса уже опознала
    сотрудника (и захотела отметить присутствие), после чего сделала обычный
    запрос — тот самый `autoflush`, на котором отметка и уходила в базу.
    """
    db = SessionLocal()
    try:
        polzovatel = auth_service.get_user_by_session(db, _token(root_client))
        assert polzovatel is not None
        _otmetka_ustarela(polzovatel.id)
        db.expire(polzovatel)
        polzovatel = auth_service.get_user_by_session(db, _token(root_client))
        assert auth_service.PRISUTSTVIE in db.info, "присутствие не отмечали — проверка вхолостую"
        # Обычный запрос внутри обработчика: именно он вызывал `autoflush`.
        db.execute(select(User.id).where(User.id == polzovatel.id)).all()

        # Сосед пишет в ту же строку. Замка быть не должно — иначе он ждёт
        # секунду и получает 1205, как получал живой сайт полсотни секунд.
        sosed.execute(
            text("UPDATE users SET last_seen_at = :kogda WHERE id = :nomer"),
            {"kogda": now_utc(), "nomer": polzovatel.id},
        )
        sosed.commit()
    finally:
        db.rollback()
        db.close()


def test_otmetka_vsyo_zhe_dopisyvaetsya(root_client):
    """Отложили — не значит потеряли: перед фиксацией отметка уходит в базу."""
    db = SessionLocal()
    try:
        polzovatel = auth_service.get_user_by_session(db, _token(root_client))
        nomer = polzovatel.id
        _otmetka_ustarela(nomer)
        db.expire(polzovatel)
        auth_service.get_user_by_session(db, _token(root_client))
        # Просьба записана в сессии запроса, а не в самой строке.
        assert auth_service.PRISUTSTVIE in db.info

        auth_service.zapisat_prisutstvie(db)
        db.commit()
        assert auth_service.PRISUTSTVIE not in db.info, "просьба должна исполняться один раз"
    finally:
        db.close()

    proverka = SessionLocal()
    try:
        bylo = proverka.execute(select(User.last_seen_at).where(User.id == nomer)).scalar_one()
        assert bylo is not None
        assert (now_utc() - bylo).total_seconds() < 120, "отметка не доехала до базы"
    finally:
        proverka.close()
