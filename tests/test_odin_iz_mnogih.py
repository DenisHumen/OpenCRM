"""Инварианты «ровно один» под одновременными запросами.

**Зачем этот файл появился.** Правила проекта говорят: инварианты «ровно один»
держатся ЗАПРОСАМИ, а не частичными индексами, — их в MySQL не существует.
Мест таких несколько: последний владелец, основная фирма, основной склад, роль
по умолчанию.

Прогон набора против настоящей MySQL показал, что у владельцев эта защита не
работала вовсе: условие стояло внутри `UPDATE`, но подзапрос был обёрнут в
производную таблицу, а её MySQL материализует обычным чтением, без замков. Двое
владельцев снимали root друг с друга разом, оба видели «двое», проходили оба —
и владельцев оставалось НОЛЬ. Пока набор гонялся на файловой базе, беды не было
по случайности устройства: писатель там был один.

Раз одно место держалось на однопоточности движка, надо проверить и остальные.
Проверяются они одинаково — настоящей гонкой двух потоков через барьер, — и
утверждение у всех одно: **после гонки инвариант цел**. Не «прошёл ровно один»:
гонку никто не обязан выигрывать, и требовать этого значит завести мигающий
тест.

Гоняются они на той же базе, что и боевой сервер, — иначе гонка проверяла бы не
то устройство, на котором работает продукт.
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


# --- склад: двое увозят последнее ---------------------------------------------


def test_dvoe_uvozyat_posledneye_razom(root_client):
    """Увезти больше, чем есть, можно только с подтверждением — и оно записывается.

    Правило склада: остаток не хранится, он равен `SUM(quantity_milli)` и
    считается запросом. У переезда отсюда два шага — спросить остаток и записать
    движение, — и между ними есть окно.

    Двое увозят последнее разом: оба спрашивают до чужой записи, оба видят
    «хватает», оба пишут. Остаток уходит в минус, а комментария «перевезено
    сверх остатка» нет ни у одного — то есть через месяц на вопрос «почему на
    складе минус» ответить будет нечем. Это хуже самого минуса: обычному
    движению минус разрешён нарочно (деталь поставили сегодня, накладную
    занесут в пятницу), и отличает законный минус от незаконного как раз запись
    о подтверждении.

    Проверяется поэтому не «остаток неотрицателен», а **инвариант целиком**:
    сколько увезли сверх остатка, столько и подтверждено.
    """
    from tests.test_warehouse import WH, move, new_product

    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    otkuda = root_client.post(f"{API}/warehouses", json={"name": "Дуэль откуда"}).json()
    kuda = root_client.post(f"{API}/warehouses", json={"name": "Дуэль куда"}).json()
    tovar = new_product(root_client, name="Дуэль плёнка")

    try:
        # Ровно две единицы на источнике — и двое увозят по две.
        move(root_client, tovar["id"], "in", 2, warehouse_id=otkuda["id"])

        codes = duel(
            lambda _: root_client.post(
                f"{WH}/transfers",
                json={
                    "product_id": tovar["id"],
                    "from_warehouse_id": otkuda["id"],
                    "to_warehouse_id": kuda["id"],
                    "quantity": 2,
                },
            ).status_code,
            "первый",
            "второй",
        )
        assert set(codes) == {"first", "second"}, f"об ударе не отчитались: {codes}"

        perevozki = root_client.get(f"{WH}/transfers").json()["items"]
        svoi = [p for p in perevozki if p["from_warehouse_id"] == otkuda["id"]]
        sverkh = [p for p in svoi if "сверх остатка" in (p.get("comment") or "")]

        # Прошли оба — значит второй увозил сверх остатка и обязан был это
        # записать. Прошёл один — записывать нечего.
        udachnyh = sum(1 for k in codes.values() if k == 201)
        assert len(svoi) == udachnyh, f"переездов {len(svoi)} при {udachnyh} удачных: {codes}"
        assert len(sverkh) == max(0, udachnyh - 1), (
            f"увезли сверх остатка молча: удачных {udachnyh}, "
            f"с записью о подтверждении {len(sverkh)}, ответы {codes}, "
            f"комментарии {[p.get('comment') for p in svoi]}"
        )
    finally:
        # Склад с остатком закрыть НЕЛЬЗЯ, и это верное правило: товар лежит
        # физически, а в системе его не будет нигде. Значит уборка обязана
        # сначала обнулить остаток — иначе дуэль оставляет за собой два склада,
        # и соседние проверки, считающие склады поимённо, краснеют не на своей
        # беде. Ровно так и вышло.
        from database.repositories import warehouse as sklad_repo
        from database.session import SessionLocal

        for w in (otkuda, kuda):
            with SessionLocal() as db:
                ostatok = sklad_repo.stock_of(db, tovar["id"], w["id"])
            if ostatok:
                move(
                    root_client, tovar["id"],
                    "out" if ostatok > 0 else "in",
                    abs(ostatok) / 1000,
                    warehouse_id=w["id"],
                )
        root_client.delete(f"{WH}/products/{tovar['id']}")
        for w in (otkuda, kuda):
            otvet = root_client.delete(f"{API}/warehouses/{w['id']}")
            # Уборка, не удавшаяся молча, отравляет соседей: требуем ответа.
            assert otvet.status_code in (200, 204), (
                f"склад {w['name']} не закрылся: {otvet.status_code} {otvet.text}"
            )
