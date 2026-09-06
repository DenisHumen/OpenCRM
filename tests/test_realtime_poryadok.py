"""Живые события уходят ПОСЛЕ фиксации, а не до неё.

Услышавший идёт за подробностями в базу тем же мгновением. Незафиксированной
записи там ещё нет — и экран получает «пришло новое сообщение», которого не
находит. Дальше он либо показывает пустоту, либо молчит до перезагрузки
страницы; и то и другое выглядит как потерянное сообщение клиента.

Беда снята с работающего кода, а не придумана: `docs/ustroystvo/12-zhivye-obnovleniya.md` называет
её отдельной строкой, и в `telegram_service` объявление стояло на двадцать
строк выше `db.commit()`. У отправки своей фиксации нет вовсе — транзакцию
закрывает запрос, уже после возврата из службы.
"""

from sqlalchemy import text

import core.realtime as realtime
from database.session import SessionLocal


class ShumnayaShina:
    """Подставная шина: запоминает, что и когда через неё прошло."""

    def __init__(self):
        self.uslyshano: list[dict] = []

    def publish(self, kanal, telo):
        import json

        self.uslyshano.append(json.loads(telo))
        return 1


def _podstavit_shinu(monkeypatch) -> ShumnayaShina:
    shina = ShumnayaShina()
    monkeypatch.setattr(realtime.redis_client, "get_client", lambda: shina)
    return shina


def test_do_fiksatsii_nikto_ne_slyshit(monkeypatch):
    """Пока транзакция открыта, наружу не уходит ничего.

    Это половина правила, которую легко потерять: объявление, ушедшее сразу,
    выглядит работающим на глаз — экран ведь обновляется. Ошибка видна только
    тому, кто в этот момент пошёл в базу.
    """
    shina = _podstavit_shinu(monkeypatch)
    db = SessionLocal()
    try:
        realtime.obyavit_posle_fiksatsii(db, "message", chat_id=1, message_id=7)
        assert shina.uslyshano == [], (
            "событие ушло до фиксации — услышавший не найдёт запись в базе: "
            + str(shina.uslyshano)
        )

        db.commit()
        assert [s["message_id"] for s in shina.uslyshano] == [7], (
            "после фиксации событие не ушло вовсе: " + str(shina.uslyshano)
        )
    finally:
        db.rollback()
        db.close()


def test_otkat_vybrasyvaet_ochered(monkeypatch):
    """Объявлять о том, чего не случилось, хуже, чем не объявлять вовсе.

    Откат — обычное дело: телеграм не принял ответ, нарушилась уникальность,
    оборвалась сеть. Уйди событие всё равно — экран показал бы сообщение,
    которого нет ни у кого.
    """
    shina = _podstavit_shinu(monkeypatch)
    db = SessionLocal()
    try:
        # Работа с базой ДО объявления — не украшение. Замерено: откат сессии,
        # которая ничего не спрашивала, не шлёт вообще никаких событий
        # (`after_rollback` не срабатывает, транзакции-то не было). Проверка без
        # запроса мерила бы форму, которой в боевом коде не бывает: объявление
        # там всегда следует за записью.
        db.execute(text("SELECT 1"))
        realtime.obyavit_posle_fiksatsii(db, "message", chat_id=1, message_id=8)
        db.rollback()
        assert shina.uslyshano == [], (
            "после отката событие всё равно ушло: " + str(shina.uslyshano)
        )

        # И очередь не осталась висеть: следующая удачная фиксация в той же
        # сессии не должна тащить за собой отменённое.
        db.execute(text("SELECT 1"))
        realtime.obyavit_posle_fiksatsii(db, "message", chat_id=1, message_id=9)
        db.commit()
        assert [s["message_id"] for s in shina.uslyshano] == [9], (
            "отменённое событие уехало вместе со следующим: " + str(shina.uslyshano)
        )
    finally:
        db.rollback()
        db.close()


def test_ochered_svoya_u_kazhdoy_sessii(monkeypatch):
    """Событие принадлежит своей транзакции, а не процессу.

    Общая очередь означала бы, что чужая фиксация рассылает твои события — и
    ровно в тот момент, когда твоя транзакция ещё открыта.
    """
    shina = _podstavit_shinu(monkeypatch)
    pervaya = SessionLocal()
    vtoraya = SessionLocal()
    try:
        realtime.obyavit_posle_fiksatsii(pervaya, "message", chat_id=1, message_id=1)
        realtime.obyavit_posle_fiksatsii(vtoraya, "message", chat_id=2, message_id=2)

        vtoraya.commit()
        assert [s["message_id"] for s in shina.uslyshano] == [2], (
            "чужая фиксация разослала не свои события: " + str(shina.uslyshano)
        )

        pervaya.commit()
        assert [s["message_id"] for s in shina.uslyshano] == [2, 1]
    finally:
        for db in (pervaya, vtoraya):
            db.rollback()
            db.close()


def test_sluzhba_perepiski_obyavlyaet_otlozhenno():
    """Приём и отправка обязаны пользоваться отложенным объявлением.

    Проверка по исходнику, а не по поведению, и это осознанно: `obyavit`
    остаётся законным вызовом там, где фиксация уже произошла (`_dokachat`).
    Отличить верный вызов от неверного можно только по тому, где он стоит
    относительно `db.commit()`, — а это и есть то, что проверяется здесь.
    """
    import inspect

    from core.services import telegram_service

    for imya in ("prinyat", "otpravit"):
        istochnik = inspect.getsource(getattr(telegram_service, imya))
        assert "obyavit_posle_fiksatsii" in istochnik, (
            f"`{imya}` объявляет в шину напрямую — событие уйдёт до фиксации"
        )
        assert "realtime.obyavit(" not in istochnik, (
            f"в `{imya}` остался прямой вызов шины"
        )
