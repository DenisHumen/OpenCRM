"""Инварианты «ровно один» под одновременными запросами.

**Зачем этот файл появился.** Правила проекта говорят: инварианты «ровно один»
держатся ЗАПРОСАМИ, а не частичными индексами, — их в MySQL не существует.
Мест таких несколько: последний владелец, основная фирма, основной склад, роль
по умолчанию.

Прогон набора против настоящей MySQL показал, что у владельцев эта защита не
работала вовсе: условие стояло внутри `UPDATE`, но подзапрос был обёрнут в
производную таблицу, а её MySQL материализует обычным чтением, без замков. Двое
владельцев снимали root друг с друга разом, оба видели «двое», проходили оба —
и владельцев оставалось НОЛЬ. На SQLite беды не было по случайности устройства:
писатель там один.

Раз одно место держалось на однопоточности движка, надо проверить и остальные.
Проверяются они одинаково — настоящей гонкой двух потоков через барьер, — и
утверждение у всех одно: **после гонки инвариант цел**. Не «прошёл ровно один»:
гонку никто не обязан выигрывать, и требовать этого значит завести мигающий
тест.

Проверки осмысленны и на файловой базе (там они стерегут, что защиту не сняли),
но настоящую цену имеют на MySQL — ровно там, где работает боевой сервер.
"""

import threading

from tests.conftest import API


def duel(strike, first_arg, second_arg):
    """Два удара разом. Возвращает {имя: исход}.

    Исключение записывается как исход, а не теряется: непойманное в потоке
    убивает его молча, и разбор красного прогона начинается с вопроса «а
    сколько ударов вообще было».
    """
    codes: dict[str, object] = {}
    at_once = threading.Barrier(2)

    def go(name, arg):
        at_once.wait()
        try:
            codes[name] = strike(arg)
        except Exception as beda:  # noqa: BLE001 — исход удара, а не наша ошибка
            codes[name] = f"исключение: {beda!r}"

    threads = [
        threading.Thread(target=go, args=("first", first_arg)),
        threading.Thread(target=go, args=("second", second_arg)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return codes


def _osnovnoy_id(model_name: str):
    """Кто сейчас основной. Нужен, чтобы вернуть всё как было."""
    from sqlalchemy import select

    from database.session import Base, SessionLocal

    table = Base.metadata.tables[model_name]
    with SessionLocal() as db:
        zapros = select(table.c.id).where(table.c.is_default.is_(True))
        if "deleted_at" in table.c:
            zapros = zapros.where(table.c.deleted_at.is_(None))
        return db.scalar(zapros)


def _skolko_osnovnyh(model_name: str) -> int:
    """Сколько строк помечено основными — считаем мимо кэшей приложения."""
    from sqlalchemy import func, select

    from database.session import Base, SessionLocal

    table = Base.metadata.tables[model_name]
    with SessionLocal() as db:
        zapros = select(func.count()).select_from(table).where(table.c.is_default.is_(True))
        if "deleted_at" in table.c:
            zapros = zapros.where(table.c.deleted_at.is_(None))
        return db.scalar(zapros) or 0


# --- основная фирма -----------------------------------------------------------


def test_dve_firmy_naznachayut_sebya_osnovnymi_razom(root_client):
    """После гонки основная фирма обязана остаться ровно одна.

    Опасное переплетение: A ставит себя, B ставит себя, A снимает у B, B
    снимает у A — и основных не остаётся вовсе. Тогда документы печатаются без
    реквизитов, а человек не понимает, почему.
    """
    root_client.post(f"{API}/modules/companies", json={"enabled": True})
    bylo = _osnovnoy_id("companies")
    first = root_client.post(f"{API}/companies", json={"name": "Дуэль А"})
    second = root_client.post(f"{API}/companies", json={"name": "Дуэль Б"})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    try:
        codes = duel(
            lambda company_id: root_client.post(
                f"{API}/companies/{company_id}/default"
            ).status_code,
            first.json()["id"],
            second.json()["id"],
        )
        assert set(codes) == {"first", "second"}, f"об ударе не отчитались: {codes}"
        stalo = _skolko_osnovnyh("companies")
        assert stalo == 1, f"основных фирм стало {stalo}, ответы: {codes}"
    finally:
        # Убираем за собой: отката между тестами в наборе нет, и оставленное
        # умолчание ломает соседей — они ждут своё.
        if bylo is not None:
            root_client.post(f"{API}/companies/{bylo}/default")
        for otvet in (first, second):
            root_client.delete(f"{API}/companies/{otvet.json()['id']}")


# --- основной склад -----------------------------------------------------------


def test_dva_sklada_naznachayut_sebya_osnovnymi_razom(root_client):
    """То же со складами: без основного приход упирается в «склад не выбран»."""
    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    bylo = _osnovnoy_id("warehouses")
    first = root_client.post(f"{API}/warehouses", json={"name": "Дуэль склад А"})
    second = root_client.post(f"{API}/warehouses", json={"name": "Дуэль склад Б"})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    try:
        codes = duel(
            lambda warehouse_id: root_client.patch(
                f"{API}/warehouses/{warehouse_id}", json={"is_default": True}
            ).status_code,
            first.json()["id"],
            second.json()["id"],
        )
        assert set(codes) == {"first", "second"}, f"об ударе не отчитались: {codes}"
        stalo = _skolko_osnovnyh("warehouses")
        assert stalo == 1, f"основных складов стало {stalo}, ответы: {codes}"
    finally:
        if bylo is not None:
            root_client.patch(f"{API}/warehouses/{bylo}", json={"is_default": True})
        for otvet in (first, second):
            root_client.delete(f"{API}/warehouses/{otvet.json()['id']}")


# --- роль по умолчанию --------------------------------------------------------


def test_dve_roli_naznachayut_sebya_osnovnymi_razom(root_client):
    """Без роли по умолчанию одобренный сотрудник входит в CRM без разделов."""
    bylo = _osnovnoy_id("roles")
    first = root_client.post(f"{API}/roles", json={"name": "Дуэль роль А"})
    second = root_client.post(f"{API}/roles", json={"name": "Дуэль роль Б"})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    try:
        codes = duel(
            lambda role_id: root_client.post(f"{API}/roles/{role_id}/default").status_code,
            first.json()["id"],
            second.json()["id"],
        )
        assert set(codes) == {"first", "second"}, f"об ударе не отчитались: {codes}"
        stalo = _skolko_osnovnyh("roles")
        assert stalo == 1, f"ролей по умолчанию стало {stalo}, ответы: {codes}"
    finally:
        if bylo is not None:
            root_client.post(f"{API}/roles/{bylo}/default")
        for otvet in (first, second):
            root_client.delete(f"{API}/roles/{otvet.json()['id']}")
