"""Отчёты: воронка, выручка, источники.

Отчётом либо пользуются, либо ему не верят, и середины нет. Поэтому проверяем не
«отвечает ли ручка», а то, из-за чего отчёту перестают верить:

* считается по ВИДУ этапа, а не по названию — переименование не должно двигать
  ни одного числа;
* «за март» включает 31 марта целиком, и граница берётся по местной полуночи, а
  не по UTC;
* сделка без названной суммы не занижает средний чек;
* удалённая сделка исчезает из отчёта;
* пустой период отвечает прочерком, а не делением на ноль;
* выключенный блок закрывает API, а не только прячет пункт меню.

Все тесты работают в марте 2026 — заведомо пустом окне. База у прогона одна, и
соседние файлы закрывают свои сделки «сейчас»: попади отчёт на сегодняшний день,
числа менялись бы от порядка запуска файлов.
"""

from datetime import datetime

from sqlalchemy import select

from database.models import Client, Deal, DealStageChange
from database.session import SessionLocal
from tests.conftest import API
from tests.test_deals import DEALS

REPORTS = f"{API}/reports"
PIPE = f"{API}/pipeline"
MODULES = f"{API}/modules"

# Отчётное окно этих тестов. tz_offset=0 — считаем в UTC везде, кроме теста,
# который проверяет саму зону.
WINDOW = {"from": "2026-03-01", "to": "2026-03-31", "tz_offset": 0}


def at(day: int, hour: int = 12, minute: int = 0) -> datetime:
    """Момент внутри отчётного окна (naive UTC, как в базе)."""
    return datetime(2026, 3, day, hour, minute)


def stage_key(client, kind: str) -> str:
    """Ключ первого этапа нужного вида — как его берёт сам отчёт.

    Зашивать «done» в тест нельзя: набор этапов настраивается, а проверяем мы
    как раз независимость от названий и ключей.
    """
    stages = client.get(f"{PIPE}/stages").json()["items"]
    return next(s["key"] for s in stages if s["kind"] == kind)


def backdate(deal_id: int, moment: datetime | None, journal: datetime | None = None) -> None:
    """Переносит сделку в отчётное окно прямо в базе.

    Через API это недостижимо: `closed_at` и журнал этапов проставляются по
    часам сервера, а отчёт за выбранный период без прошлого проверять нечем.
    """
    with SessionLocal() as db:
        deal = db.get(Deal, deal_id)
        deal.closed_at = moment
        if journal is not None:
            for change in db.scalars(
                select(DealStageChange).where(DealStageChange.deal_id == deal_id)
            ):
                change.changed_at = journal
        db.commit()


def backdate_client(client_id: int, moment: datetime) -> None:
    with SessionLocal() as db:
        db.get(Client, client_id).created_at = moment
        db.commit()


def make_deal(client, client_id: int, kind: str, moment: datetime, amount=None) -> dict:
    """Заводит сделку, доводит её до этапа нужного вида и ставит в окно."""
    body = {"title": "Отчётная", "client_id": client_id}
    if amount is not None:
        body["amount"] = amount
    deal = client.post(DEALS, json=body).json()
    client.post(f"{DEALS}/{deal['id']}/move", json={"stage": stage_key(client, kind)})
    backdate(deal["id"], moment, journal=moment)
    return deal


def report(client, name: str, **overrides) -> dict:
    params = {**WINDOW, **overrides}
    response = client.get(f"{REPORTS}/{name}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def new_client(client, name: str, source: str | None = None, moment: datetime | None = None) -> dict:
    body = {"name": name}
    if source is not None:
        body["source"] = source
    created = client.post(f"{API}/clients", json=body).json()
    backdate_client(created["id"], moment or at(2))
    return created


# --- вид этапа против названия ---

def test_report_does_not_notice_a_stage_being_renamed(manager_client, root_client):
    """Главный инвариант: считаем по виду этапа, а не по его названию.

    У ремонта выигранный этап называется «Выдано», у магазина «Получен», у
    агентства «Закрыта». Отчёт, который смотрит на слово, работает ровно у
    одного бизнеса — и ломается молча, стоит переименовать колонку.
    """
    person = new_client(manager_client, "Переименование этапа")
    make_deal(manager_client, person["id"], "won", at(10), amount=250_000)

    before_revenue = report(manager_client, "revenue")
    before_funnel = report(manager_client, "funnel")

    won = stage_key(manager_client, "won")
    original = next(
        s["name"] for s in manager_client.get(f"{PIPE}/stages").json()["items"] if s["key"] == won
    )
    renamed = root_client.patch(f"{PIPE}/stages/{won}", json={"name": "Оплачено и выдано"})
    assert renamed.status_code == 200, renamed.text
    try:
        after_revenue = report(manager_client, "revenue")
        after_funnel = report(manager_client, "funnel")

        assert after_revenue["won_amount"] == before_revenue["won_amount"]
        assert after_revenue["avg_check"] == before_revenue["avg_check"]
        assert after_funnel["won"] == before_funnel["won"]
        assert after_funnel["conversion"] == before_funnel["conversion"]
        # Название в отчёте, разумеется, новое — оно и должно следовать за
        # воронкой. Меняются подписи, а не числа.
        names = [s["name"] for s in after_funnel["stages"]]
        assert "Оплачено и выдано" in names
    finally:
        root_client.patch(f"{PIPE}/stages/{won}", json={"name": original})


# --- границы периода ---

def test_last_day_of_the_period_counts_in_full(manager_client):
    """«За март» обязано включать 31 марта целиком, до последней секунды.

    Классическая потеря: границу берут как 31 марта 00:00, и весь последний день
    выпадает. Заметить это по общей сумме почти невозможно — она просто чуть
    меньше, чем в бухгалтерии.
    """
    person = new_client(manager_client, "Последний день")
    make_deal(manager_client, person["id"], "won", at(31, 23, 59), amount=700_000)
    before = report(manager_client, "revenue")["won_amount"]

    # а сделка, закрытая в первую минуту апреля, в мартовский отчёт не попадает
    make_deal(manager_client, person["id"], "won", datetime(2026, 4, 1, 0, 1), amount=900_000)
    after = report(manager_client, "revenue")["won_amount"]

    assert before >= 700_000, "сделка последнего дня не попала в отчёт"
    assert after == before, "в мартовский отчёт затесался апрель"


def test_period_boundary_follows_the_local_midnight(manager_client):
    """Граница периода — местная полночь, а не UTC.

    В базе время naive UTC, а «март» человек понимает по своему календарю. Для
    Киева (UTC+3) 1 апреля 00:30 по местному — это 31 марта 21:30 UTC, и такая
    сделка в мартовский отчёт попасть НЕ должна, хотя по UTC она ещё мартовская.
    """
    # Отдельный месяц: в марте у соседних тестов свои сделки, а здесь считается
    # прирост ровно от одной.
    february = {"from": "2026-02-01", "to": "2026-02-28"}
    # getTimezoneOffset() в Киеве отдаёт -180: столько минут прибавить к
    # местному времени, чтобы получить UTC.
    KYIV = -180

    person = new_client(manager_client, "Часовой пояс", moment=datetime(2026, 2, 10))
    before_utc = report(manager_client, "revenue", **february, tz_offset=0)["won_amount"]
    before_kyiv = report(manager_client, "revenue", **february, tz_offset=KYIV)["won_amount"]

    # 28 февраля 21:30 UTC — это уже 1 марта 00:30 в Киеве
    make_deal(manager_client, person["id"], "won", datetime(2026, 2, 28, 21, 30), amount=430_000)

    after_utc = report(manager_client, "revenue", **february, tz_offset=0)["won_amount"]
    after_kyiv = report(manager_client, "revenue", **february, tz_offset=KYIV)["won_amount"]

    # `or 0` — потому что период без единой названной цены отвечает прочерком
    # (None), а не нулём: см. test_revenue_without_a_named_price_is_a_dash_not_a_zero.
    assert (after_utc or 0) - (before_utc or 0) == 430_000, (
        "сделка последнего дня выпала из отчёта по UTC"
    )
    assert after_kyiv == before_kyiv, "граница периода посчитана по UTC, а не по местной полуночи"


def test_month_buckets_cover_the_whole_period(manager_client):
    months = [row["month"] for row in report(manager_client, "revenue", **{"to": "2026-05-15"})["months"]]
    assert months == ["2026-03", "2026-04", "2026-05"], "месяцы периода нарезаны неверно"


def test_backwards_period_is_rejected(manager_client):
    bad = manager_client.get(f"{REPORTS}/revenue", params={"from": "2026-03-31", "to": "2026-03-01"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "bad_period"


# --- деньги ---

def test_deals_without_a_price_do_not_drag_the_average_down(manager_client):
    """«Сумму ещё не назвали» — не то же самое, что «работа бесплатная».

    Возьми в знаменатель все выигранные сделки — и каждая без цены будет тихо
    занижать средний чек. Число останется правдоподобным.
    """
    person = new_client(manager_client, "Средний чек отчёта")
    make_deal(manager_client, person["id"], "won", at(5), amount=100_000)
    make_deal(manager_client, person["id"], "won", at(6), amount=300_000)
    priced = report(manager_client, "revenue")

    make_deal(manager_client, person["id"], "won", at(7))  # цену не назвали
    unpriced = report(manager_client, "revenue")

    assert unpriced["won_count"] == priced["won_count"] + 1, "сделка без цены пропала совсем"
    assert unpriced["won_priced"] == priced["won_priced"], "сделка без цены попала в знаменатель"
    assert unpriced["avg_check"] == priced["avg_check"]
    assert unpriced["avg_check"] == round(unpriced["won_amount"] / unpriced["won_priced"])


def test_deleted_deal_leaves_the_report(manager_client):
    """Удалённая сделка не должна продолжать приносить выручку.

    И уходить обязана отовсюду сразу: остаться в воронке, но пропасть из
    выручки — значит показать два несводимых числа на одном экране.
    """
    person = new_client(manager_client, "Удаление из отчёта")
    deal = make_deal(manager_client, person["id"], "won", at(12), amount=555_000)
    money_before = report(manager_client, "revenue")
    funnel_before = report(manager_client, "funnel")

    assert manager_client.delete(f"{DEALS}/{deal['id']}").status_code == 200
    money_after = report(manager_client, "revenue")
    funnel_after = report(manager_client, "funnel")

    assert money_before["won_amount"] - money_after["won_amount"] == 555_000
    assert money_before["won_count"] - money_after["won_count"] == 1
    assert funnel_before["won"] - funnel_after["won"] == 1
    assert funnel_before["entered"] - funnel_after["entered"] == 1


def test_losses_are_counted_separately_from_revenue(manager_client):
    person = new_client(manager_client, "Потери")
    before = report(manager_client, "revenue")
    make_deal(manager_client, person["id"], "lost", at(14), amount=120_000)
    after = report(manager_client, "revenue")

    # `or 0` — прочерк вместо нуля, пока ни у одной потери не названа цена.
    assert (after["lost_amount"] or 0) - (before["lost_amount"] or 0) == 120_000
    assert after["won_amount"] == before["won_amount"], "потеря попала в выручку"


# --- воронка ---

def test_funnel_counts_entries_not_the_current_column(manager_client):
    """Воронка отвечает на «сколько прошло через шаг», а не «где всё лежит».

    Сделка, ушедшая дальше, обязана остаться в числе прошедших через этап;
    сделка, перепрыгнувшая этап, не должна в нём появиться.
    """
    person = new_client(manager_client, "Воронка")
    stages = manager_client.get(f"{PIPE}/stages").json()["items"]
    open_stages = [s["key"] for s in stages if s["kind"] == "open"]
    assert len(open_stages) >= 2, "в воронке меньше двух открытых этапов — тест не о чем"
    first, second = open_stages[0], open_stages[1]

    before = {s["key"]: s["entered"] for s in report(manager_client, "funnel")["stages"]}

    skipper = manager_client.post(
        DEALS, json={"title": "Перепрыгнувшая", "client_id": person["id"]}
    ).json()
    manager_client.post(f"{DEALS}/{skipper['id']}/move", json={"stage": stage_key(manager_client, "won")})
    backdate(skipper["id"], at(20), journal=at(20))

    after = {s["key"]: s["entered"] for s in report(manager_client, "funnel")["stages"]}
    assert after[first] == before[first] + 1, "вход в первый этап не засчитан"
    assert after[second] == before[second], "этап, который перепрыгнули, засчитал сделку"


def test_funnel_conversion_is_a_share_of_the_entries(manager_client):
    """Знаменатель — вход в первый этап, то есть «пришла заявка».

    Потолка в 100% здесь намеренно нет: в июле закрывают и то, что пришло в
    июне, и ограничение сверху заменило бы правду красивым числом.
    """
    person = new_client(manager_client, "Конверсия")
    make_deal(manager_client, person["id"], "won", at(21), amount=10_000)
    make_deal(manager_client, person["id"], "lost", at(22))

    data = report(manager_client, "funnel")
    assert data["entered"] >= 2
    assert data["conversion"] == round(data["won"] * 100 / data["entered"], 1)
    assert data["loss_rate"] == round(data["lost"] * 100 / data["entered"], 1)


def test_empty_period_does_not_divide_by_zero(manager_client):
    """Пустой период отвечает прочерком, а не нулём и не пятисоткой.

    Ноль прочитали бы как «ни одна заявка не дошла», а верный ответ —
    «заявок не было».
    """
    empty = {"from": "2019-01-01", "to": "2019-01-31", "tz_offset": 0}
    funnel = manager_client.get(f"{REPORTS}/funnel", params=empty)
    revenue = manager_client.get(f"{REPORTS}/revenue", params=empty)
    sources = manager_client.get(f"{REPORTS}/sources", params=empty)

    assert funnel.status_code == 200, funnel.text
    assert revenue.status_code == 200, revenue.text
    assert sources.status_code == 200, sources.text

    assert funnel.json()["entered"] == 0
    assert funnel.json()["conversion"] is None
    assert all(step["from_previous"] is None for step in funnel.json()["stages"])
    assert revenue.json()["avg_check"] is None, "средний чек без сделок с ценой — не ноль"
    # Выручка на пустом периоде — тоже прочерк, а не ноль: ноль означал бы
    # «работали даром», а сделок не было вовсе. То же правило, что у среднего
    # чека и у строк таблицы источников; см.
    # test_revenue_without_a_named_price_is_a_dash_not_a_zero.
    assert revenue.json()["won_amount"] is None
    assert all(row["conversion"] is None for row in sources.json()["items"])


def test_empty_stage_stays_visible_in_the_funnel(manager_client):
    """Провал в середине воронки видно только тогда, когда пустой этап нарисован."""
    stages = manager_client.get(f"{PIPE}/stages").json()["items"]
    reported = report(manager_client, "funnel")["stages"]
    assert [s["key"] for s in reported] == [s["key"] for s in stages]


def test_closing_stages_are_not_links_in_the_chain(manager_client):
    """«Из выигранных в потерянные» не переходят.

    Этапы `won` и `lost` стоят в воронке рядом, и пошаговая конверсия между
    ними — деление двух чисел, которое ничего не означает. Итог по ним считается
    отдельно, от общего знаменателя.
    """
    person = new_client(manager_client, "Цепочка этапов")
    make_deal(manager_client, person["id"], "won", at(23), amount=1_000)
    make_deal(manager_client, person["id"], "lost", at(24))

    steps = report(manager_client, "funnel")["stages"]
    closing = [s for s in steps if s["kind"] in ("won", "lost")]
    assert closing, "в воронке нет закрывающих этапов — проверять нечего"
    assert all(s["from_previous"] is None for s in closing)
    # первый открытый этап тоже без доли: сравнивать не с чем
    assert next(s for s in steps if s["kind"] == "open")["from_previous"] is None


# --- источники ---

def test_sources_split_clients_and_money(manager_client):
    """Сколько клиентов и сколько выручки пришло с каждого источника."""
    before = {row["source"]: row for row in report(manager_client, "sources")["items"]}

    from_ads = new_client(manager_client, "С рекламы", source="ads", moment=at(3))
    make_deal(manager_client, from_ads["id"], "won", at(9), amount=400_000)
    from_word = new_client(manager_client, "По совету", source="referral", moment=at(4))
    make_deal(manager_client, from_word["id"], "lost", at(11))

    after = {row["source"]: row for row in report(manager_client, "sources")["items"]}

    # `or 0` — потому что источник без единой названной цены отвечает
    # прочерком (None), а не нулём: см.
    # test_a_source_without_a_named_price_shows_a_dash_not_zero.
    assert after["ads"]["clients"] == before["ads"]["clients"] + 1
    assert (after["ads"]["revenue"] or 0) - (before["ads"]["revenue"] or 0) == 400_000
    assert after["referral"]["clients"] == before["referral"]["clients"] + 1
    assert after["referral"]["revenue"] == before["referral"]["revenue"]
    assert after["referral"]["lost_count"] == before["referral"]["lost_count"] + 1


def test_source_not_asked_is_not_the_same_as_other(manager_client):
    """«Не спросили, откуда» и «клиент ответил „другое“» — разные вещи.

    Слить их в одну строку значит выдать пробел в работе менеджеров за факт о
    клиентах.
    """
    before = {row["source"]: row for row in report(manager_client, "sources")["items"]}
    new_client(manager_client, "Не спрашивали", moment=at(5))
    new_client(manager_client, "Ответил «другое»", source="other", moment=at(5))
    after = {row["source"]: row for row in report(manager_client, "sources")["items"]}

    assert None in after, "строки «источник не указан» в отчёте нет"
    assert after[None]["clients"] == before[None]["clients"] + 1
    assert after["other"]["clients"] == before["other"]["clients"] + 1


def test_preset_sources_are_shown_even_when_empty(manager_client):
    """«С рекламы ноль» — это ответ, ради которого отчёт и открывают."""
    empty = manager_client.get(
        f"{REPORTS}/sources", params={"from": "2019-01-01", "to": "2019-01-31"}
    ).json()
    keys = [row["source"] for row in empty["items"]]
    for preset in ("referral", "search", "social", "ads", "repeat", "other"):
        assert preset in keys, f"источник {preset} пропал из отчёта"


def test_custom_source_survives_and_is_reported(manager_client):
    """Своё значение источника — законное: справочник не покрывает всех."""
    person = new_client(manager_client, "Своё значение", source="Радиореклама", moment=at(6))
    assert manager_client.get(f"{API}/clients/{person['id']}").json()["source"] == "Радиореклама"

    keys = [row["source"] for row in report(manager_client, "sources")["items"]]
    assert "Радиореклама" in keys


def test_source_can_be_cleared(manager_client):
    """Источник снимается: «выяснили, что спрашивать было не у кого» — тоже итог."""
    person = new_client(manager_client, "Снять источник", source="search", moment=at(7))
    cleared = manager_client.patch(f"{API}/clients/{person['id']}", json={"source": ""}).json()
    assert cleared["source"] is None, "пустая строка обязана стать NULL, а не вторым «не знаю»"


# --- выгрузка ---

def test_csv_opens_in_excel_without_repair(manager_client):
    """Две вещи, из-за которых выгрузку обычно чинят руками.

    Без BOM Excel читает файл в системной ANSI и показывает кракозябры вместо
    кириллицы; без точки с запятой строка в русской локали сваливается в один
    столбец.
    """
    for name in ("funnel", "revenue", "sources"):
        response = manager_client.get(f"{REPORTS}/{name}.csv", params=WINDOW)
        assert response.status_code == 200, response.text
        assert response.content.startswith(b"\xef\xbb\xbf"), f"{name}: выгрузка без BOM"
        header = response.content.decode("utf-8-sig").splitlines()[0]
        assert ";" in header, f"{name}: разделитель не точка с запятой"
        assert "\r\n" in response.text, f"{name}: перевод строки не CRLF"
        assert f"{name}-2026-03-01_2026-03-31.csv" in response.headers["content-disposition"]


def test_csv_headers_follow_the_reader_language(manager_client):
    """Выгрузку открывает человек, а не программа: «won_amount» ему ни о чём
    не говорит."""
    manager_client.patch(f"{API}/auth/me", json={"locale": "ru"})
    try:
        text = manager_client.get(f"{REPORTS}/funnel.csv", params=WINDOW).content.decode("utf-8-sig")
        assert text.splitlines()[0].startswith("Этап;")
    finally:
        manager_client.patch(f"{API}/auth/me", json={"locale": "en"})


def test_csv_leaves_an_unnamed_amount_empty(manager_client):
    """Пустая ячейка и ноль в выгрузке значат разное — как и в базе."""
    empty = {"from": "2019-01-01", "to": "2019-01-31"}
    text = manager_client.get(f"{REPORTS}/revenue.csv", params=empty).content.decode("utf-8-sig")
    row = text.splitlines()[1].split(";")
    assert row[-1] == "", "средний чек без сделок с ценой выгрузился нулём"


# --- модуль и доступ ---

def test_reports_close_with_their_module(manager_client, root_client):
    """Спрятать пункт меню мало: адрес остаётся в закладках и в старых письмах."""
    assert root_client.post(f"{MODULES}/reports", json={"enabled": False}).status_code == 200
    try:
        for path in ("/reports/funnel", "/reports/revenue", "/reports/sources", "/reports/funnel.csv"):
            blocked = manager_client.get(f"{API}{path}", params=WINDOW)
            assert blocked.status_code == 403, path
            assert blocked.json()["error"]["code"] == "module_disabled", path
        # а соседние разделы продолжают работать
        assert manager_client.get(f"{API}/clients").status_code == 200
        assert manager_client.get(f"{API}/deals").status_code == 200
        assert manager_client.get(f"{API}/dashboard").status_code == 200
    finally:
        root_client.post(f"{MODULES}/reports", json={"enabled": True})


def test_reports_require_login(base_client):
    assert base_client.get(f"{REPORTS}/funnel").status_code == 401
    assert base_client.get(f"{REPORTS}/revenue.csv").status_code == 401


def test_a_source_without_a_named_price_shows_a_dash_not_zero(manager_client):
    """«Выиграно 1, выручка 0» читается как «с рекламы не заработали».

    Верный ответ — «сумму не назвали», и он обязан отличаться от настоящего
    нуля так же, как отличается средний чек. Иначе источник, по которому просто
    не заполнили цену, выглядит убыточным, и на него перестают тратить деньги.
    """
    # Свои источники, а не пресетные: база у прогона одна, и в «ads» соседние
    # тесты уже кладут свои сделки.
    unpriced = new_client(manager_client, "Пришёл без цены", source="щит-у-дороги")
    make_deal(manager_client, unpriced["id"], "won", at(10))  # цену не назвали

    free = new_client(manager_client, "Пришёл за бесплатным", source="буклет-в-подъезде")
    make_deal(manager_client, free["id"], "won", at(11), amount=0)  # работали даром

    rows = {row["source"]: row for row in report(manager_client, "sources")["items"]}

    assert rows["щит-у-дороги"]["won_count"] == 1
    assert rows["щит-у-дороги"]["revenue"] is None, "цену не назвали — это не ноль"

    assert rows["буклет-в-подъезде"]["won_count"] == 1
    assert rows["буклет-в-подъезде"]["revenue"] == 0, "названный ноль обязан остаться нулём"


def test_unnamed_price_does_not_inflate_the_total_across_sources(manager_client):
    """Прочерк не должен ломать итог: сумма по источникам остаётся числом."""
    data = report(manager_client, "sources")
    named = sum(row["revenue"] for row in data["items"] if row["revenue"] is not None)
    assert data["revenue_total"] == named


# --- границы периода против помесячной разбивки ---

# Сентябрь 2026 — своё пустое окно: в марте у соседних тестов свои сделки, а
# здесь важны абсолютные числа, а не приросты.
def sep(day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute)


def test_revenue_stays_inside_the_requested_days(manager_client):
    """Отчёт за 1–10 сентября не имеет права считать сделку, закрытую 28-го.

    Выручка разбита по месяцам, и месяцы легко нарезать целиком — от первого
    числа до первого числа. Тогда период «с 1 по 10» молча превращается во «весь
    сентябрь»: воронка и источники считают запрошенные дни, выручка — месяц, а
    подпись над ними одна. Два несводимых числа на одном экране отменяют доверие
    ко всему отчёту, и заметить подмену можно только пересчитав руками.
    """
    person = new_client(manager_client, "Вне запрошенных дней", moment=sep(1))
    narrow = {"from": "2026-09-01", "to": "2026-09-10", "tz_offset": 0}
    make_deal(manager_client, person["id"], "won", sep(28), amount=500_000)

    inside = report(manager_client, "revenue", **narrow)
    assert inside["won_count"] == 0, "сделка вне периода попала в выручку"
    assert inside["won_amount"] is None
    assert inside["months"][0]["won_count"] == 0, "месяц посчитан целиком, а не по периоду"

    # и та же сделка обязана найтись, когда её день в период входит
    wide = report(manager_client, "revenue", **{**narrow, "to": "2026-09-30"})
    assert wide["won_count"] == 1
    assert wide["won_amount"] == 500_000


def test_revenue_agrees_with_sources_on_a_partial_month(manager_client):
    """Три отчёта на одном экране обязаны считать один и тот же период."""
    person = new_client(
        manager_client, "Сверка выручки с источниками", source="сверка-периода", moment=sep(1)
    )
    make_deal(manager_client, person["id"], "won", sep(25), amount=310_000)

    window = {"from": "2026-09-01", "to": "2026-09-05", "tz_offset": 0}
    revenue = report(manager_client, "revenue", **window)
    sources = report(manager_client, "sources", **window)
    row = next(r for r in sources["items"] if r["source"] == "сверка-периода")

    assert row["won_count"] == 0, "источники не увидели сделку вне периода"
    assert revenue["won_count"] == 0, "а выручка увидела — периоды разъехались"


def test_partial_month_boundary_still_follows_the_local_midnight(manager_client):
    """Подрезка периода не должна съесть поправку на часовой пояс.

    Для Киева 11 октября 00:30 по местному — это 10 октября 21:30 UTC. Период,
    оканчивающийся 10 октября, такую сделку включать НЕ должен, а закрытую в
    23:30 по местному (20:30 UTC) — обязан.
    """
    KYIV = -180
    window = {"from": "2026-10-01", "to": "2026-10-10", "tz_offset": KYIV}
    person = new_client(manager_client, "Полночь на границе подрезки", moment=datetime(2026, 10, 1))

    make_deal(manager_client, person["id"], "won", datetime(2026, 10, 10, 21, 30), amount=170_000)
    after = report(manager_client, "revenue", **window)
    assert after["won_count"] == 0, "граница подрезки посчитана по UTC, а не по местной полуночи"

    # 10 октября 20:30 UTC — это ещё 23:30 по Киеву, последний вечер периода
    earlier = new_client(manager_client, "Последний вечер периода", moment=datetime(2026, 10, 1))
    make_deal(manager_client, earlier["id"], "won", datetime(2026, 10, 10, 20, 30), amount=170_000)
    later = report(manager_client, "revenue", **window)
    assert later["won_count"] == 1, "вечер последнего дня периода потерян"
    assert later["won_amount"] == 170_000


# --- ноль против прочерка в выручке ---

def test_revenue_without_a_named_price_is_a_dash_not_a_zero(manager_client):
    """«Выиграно 1, выручка 0» читается как «сработали даром».

    Верный ответ — «сумму не назвали», и показывается он прочерком: ровно так
    же, как в таблице источников на том же экране и как у среднего чека. Пока
    здесь стоял ноль, две строки одного отчёта отвечали на один вопрос
    по-разному, причём про одну и ту же сделку.
    """
    window = {"from": "2026-06-01", "to": "2026-06-30", "tz_offset": 0}
    june = datetime(2026, 6, 15, 12, 0)

    empty = report(manager_client, "revenue", **window)
    assert empty["won_amount"] is None, "пустой период выдал ноль вместо прочерка"

    person = new_client(manager_client, "Июньский без цены", moment=june)
    make_deal(manager_client, person["id"], "won", june)
    unpriced = report(manager_client, "revenue", **window)

    assert unpriced["won_count"] == 1, "сделка без цены пропала совсем"
    assert unpriced["won_priced"] == 0
    assert unpriced["won_amount"] is None, "сделка без цены дала выручку 0"
    assert unpriced["months"][0]["won_amount"] is None

    # названный ноль обязан остаться нулём — это другое состояние
    free = new_client(manager_client, "Июньский за бесплатно", moment=june)
    make_deal(manager_client, free["id"], "won", june, amount=0)
    priced = report(manager_client, "revenue", **window)
    assert priced["won_priced"] == 1
    assert priced["won_amount"] == 0, "названный ноль превратился в прочерк"


def test_revenue_csv_leaves_an_unnamed_total_empty(manager_client):
    """Выгрузка обязана повторять экран: прочерк — пустая ячейка, не «0,00»."""
    window = {"from": "2019-01-01", "to": "2019-01-31"}
    text = manager_client.get(f"{REPORTS}/revenue.csv", params=window).content.decode("utf-8-sig")
    lines = text.splitlines()
    header, row = lines[0].split(";"), lines[1].split(";")

    assert row[header.index("Revenue")] == "", "выручка без названной цены выгрузилась нулём"
    assert row[header.index("Lost value")] == "", "сумма потерь без цены выгрузилась нулём"
