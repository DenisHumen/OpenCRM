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

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from core.services import report_service
from core.utils import now_utc
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

# Сентябрь 2019 — своё пустое окно: в марте у соседних тестов свои сделки, а
# здесь важны абсолютные числа, а не приросты.
#
# Год ПРОШЛЫЙ, и это не мелочь. Окно стояло в 2026-м, и 01.09.2026 оно
# перестало быть пустым: сделки, которые соседние тесты заводят «сейчас»,
# попали внутрь, и `won_count` стал двойкой вместо нуля. Пустое окно в будущем
# пусто только до своей даты — а она приходит молча и валит чужие тесты.
def sep(day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2019, 9, day, hour, minute)


def test_revenue_stays_inside_the_requested_days(manager_client):
    """Отчёт за 1–10 сентября не имеет права считать сделку, закрытую 28-го.

    Выручка разбита по месяцам, и месяцы легко нарезать целиком — от первого
    числа до первого числа. Тогда период «с 1 по 10» молча превращается во «весь
    сентябрь»: воронка и источники считают запрошенные дни, выручка — месяц, а
    подпись над ними одна. Два несводимых числа на одном экране отменяют доверие
    ко всему отчёту, и заметить подмену можно только пересчитав руками.
    """
    person = new_client(manager_client, "Вне запрошенных дней", moment=sep(1))
    narrow = {"from": "2019-09-01", "to": "2019-09-10", "tz_offset": 0}
    make_deal(manager_client, person["id"], "won", sep(28), amount=500_000)

    inside = report(manager_client, "revenue", **narrow)
    assert inside["won_count"] == 0, "сделка вне периода попала в выручку"
    assert inside["won_amount"] is None
    assert inside["months"][0]["won_count"] == 0, "месяц посчитан целиком, а не по периоду"

    # и та же сделка обязана найтись, когда её день в период входит
    wide = report(manager_client, "revenue", **{**narrow, "to": "2019-09-30"})
    assert wide["won_count"] == 1
    assert wide["won_amount"] == 500_000


def test_revenue_agrees_with_sources_on_a_partial_month(manager_client):
    """Три отчёта на одном экране обязаны считать один и тот же период."""
    person = new_client(
        manager_client, "Сверка выручки с источниками", source="сверка-периода", moment=sep(1)
    )
    make_deal(manager_client, person["id"], "won", sep(25), amount=310_000)

    window = {"from": "2019-09-01", "to": "2019-09-05", "tz_offset": 0}
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

    assert row[header.index("Won value")] == "", "сумма выигранных без цены выгрузилась нулём"
    assert row[header.index("Lost value")] == "", "сумма потерь без цены выгрузилась нулём"
    # «Выручка» из заголовка ушла намеренно: выручка — это полученные деньги, а
    # сумма выигранных заявок деньгами не является. Пока обе величины назывались
    # одним словом, выгрузка спорила с кассой.
    assert "Revenue" not in header, "в выгрузке снова две величины под одним словом"
    assert len(row) == len(header), "строка и заголовок разошлись столбцами"


def test_hidden_amounts_do_not_show_through_the_order_of_rows(root_client):
    """Порядок строк — тоже сведения о суммах.

    Числа зануляются, а ранжирование по ним оставалось: сузив период до одного
    дня с одной сделкой на источник, человек читает порядок конкретных сумм, а
    повторяя с разными периодами — оценивает каждую. Запрет выглядел
    работающим, не будучи им.
    """
    from tests.conftest import make_manager
    from tests.test_roles import ROLES, _user_id

    made = root_client.post(
        ROLES,
        json={
            "name": "Смотрит отчёты без денег",
            "permissions": ["clients.view", "deals.view", "deals.view_others", "reports.view"],
        },
    )
    assert made.status_code == 201, made.text
    role = made.json()
    watcher = make_manager(root_client, "nomoney-order@test.local")
    user_id = _user_id(root_client, "nomoney-order@test.local")
    assert root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": role["id"]}).status_code == 200

    client = root_client.post(f"{API}/clients", json={"name": "Источники", "source": "ads"}).json()
    won = next(
        c for c in root_client.get(f"{API}/deals/board").json()["columns"] if c["kind"] == "won"
    )["key"]
    # Свои источники, а не общие «ads»/«search»: база одна на весь прогон, и
    # деньги, добавленные сюда, попадали бы в чужие проверки итогов.
    for source, amount in (
        ("probe-order-low", 50_00),
        ("probe-order-high", 5000_00),
        ("probe-order-mid", 300_00),
    ):
        buyer = root_client.post(
            f"{API}/clients", json={"name": f"Клиент {source}", "source": source}
        ).json()
        deal = root_client.post(
            f"{API}/deals", json={"title": source, "client_id": buyer["id"], "amount": amount}
        ).json()
        root_client.post(f"{API}/deals/{deal['id']}/move", json={"stage": won})

    seen = watcher.get(f"{API}/reports/sources", params={"tz_offset": 0}).json()
    rows = [row for row in seen["items"] if str(row["source"] or "").startswith("probe-order")]

    assert all(row["revenue"] is None for row in rows), "суммы не спрятаны"
    # Порядок по деньгам был бы search → social → ads. Проверяем, что его нет.
    by_money = ["probe-order-high", "probe-order-mid", "probe-order-low"]
    assert [row["source"] for row in rows] != by_money, (
        "строки по-прежнему выстроены по спрятанным суммам"
    )


def test_the_sources_total_says_nothing_instead_of_zero(root_client):
    """Цену не назвали ни у одной строки — прочерк, а не ноль.

    Иначе на одном экране блок «Выручка» пишет «—», а таблица источников
    «0 ₽», и владелец получает два разных ответа на один вопрос.
    """
    client = root_client.post(
        f"{API}/clients", json={"name": "Без цены", "source": "referral"}
    ).json()
    deal = root_client.post(
        f"{API}/deals", json={"title": "Без суммы", "client_id": client["id"]}
    ).json()
    won = next(
        c for c in root_client.get(f"{API}/deals/board").json()["columns"] if c["kind"] == "won"
    )["key"]
    root_client.post(f"{API}/deals/{deal['id']}/move", json={"stage": won})

    revenue = root_client.get(f"{API}/reports/revenue", params={"tz_offset": 0}).json()
    sources = root_client.get(f"{API}/reports/sources", params={"tz_offset": 0}).json()

    row = next(r for r in sources["items"] if r["source"] == "referral")
    assert row["revenue"] is None, "строка источника показала ноль вместо прочерка"
    if revenue["won_amount"] is None:
        assert sources["revenue_total"] is None, (
            "выручка показывает прочерк, а итог источников — ноль"
        )


# --- одно слово — одна величина ---

# Декабрь 2019 — своё пустое окно: здесь сверяются абсолютные числа трёх
# отчётов между собой, и чужие сделки в периоде сделали бы сверку бессмысленной.
# Год прошлый по той же причине, что и у `sep`, — окно в будущем однажды
# наступает. Это стояло в 2026-м и выстрелило бы 01.12.2026.
def dec(day: int, hour: int = 12) -> datetime:
    return datetime(2019, 12, day, hour)


DECEMBER = {"from": "2019-12-01", "to": "2019-12-31", "tz_offset": 0}


def test_won_is_one_number_across_the_whole_screen(manager_client):
    """«Выиграно» в трёх карточках отчёта — одна величина, а не две разных.

    Расхождение достижимо и достигается одним движением мышью: заявку довели до
    выигранного этапа и вернули в работу. Дата закрытия при возврате обнуляется
    (`deal_service.move`), а запись журнала «вошла в выигранный этап» остаётся
    навсегда. Пока воронка считала по журналу, а выручка и источники — по дате
    закрытия, экран показывал «Выиграно 2» сверху и «Выиграно 1» ниже, про одни
    и те же заявки.

    Правильна дата закрытия: заявку вернули в работу, и выигранной она уже не
    является — это видно и в списке, и на канбане. Журнальное число остаётся, но
    отвечает на другой вопрос («сколько прошло через этап») и стоит в столбцах
    воронки, а не под словом «Выиграно».
    """
    person = new_client(
        manager_client, "Возврат из выигранного", source="возврат-декабрь", moment=dec(1)
    )
    make_deal(manager_client, person["id"], "won", dec(10), amount=200_000)

    returned = manager_client.post(
        DEALS, json={"title": "Вернули в работу", "client_id": person["id"]}
    ).json()
    won_key, open_key = stage_key(manager_client, "won"), stage_key(manager_client, "open")
    assert manager_client.post(
        f"{DEALS}/{returned['id']}/move", json={"stage": won_key}
    ).status_code == 200
    back = manager_client.post(f"{DEALS}/{returned['id']}/move", json={"stage": open_key})
    assert back.status_code == 200, back.text
    assert back.json()["closed_at"] is None, "возврат в работу не снял дату закрытия"
    # Обе записи журнала — в отчётное окно; дата закрытия у вернувшейся пуста.
    backdate(returned["id"], None, journal=dec(12))

    funnel = report(manager_client, "funnel", **DECEMBER)
    revenue = report(manager_client, "revenue", **DECEMBER)
    sources = report(manager_client, "sources", **DECEMBER)
    row = next(r for r in sources["items"] if r["source"] == "возврат-декабрь")

    assert funnel["won"] == revenue["won_count"], (
        "«Выиграно» воронки не сошлось с «Выиграно» выручки"
    )
    assert row["won_count"] == revenue["won_count"], (
        "«Выиграно» в таблице источников не сошлось с остальным экраном"
    )
    assert revenue["won_count"] == 1, "вернувшаяся в работу заявка осталась выигранной"

    # А столбец воронки по-прежнему помнит обе: через выигранный этап прошли
    # двое, и это другой вопрос — под другой подписью («вошло заявок»).
    passed = next(s["entered"] for s in funnel["stages"] if s["key"] == won_key)
    assert passed == 2, "воронка забыла заявку, которая через этап всё же прошла"


def test_conversions_are_computed_from_the_same_won(manager_client):
    """Проценты на экране считаются от одного и того же «Выиграно».

    Числитель у конверсии воронки — то же число, что стоит в плитке «Выиграно»
    рядом и в выручке ниже. Пока он брался из журнала, а соседние — из даты
    закрытия, три процента на экране считались от двух разных числителей, и
    пересчитать их вручную было нельзя ни по одному набору чисел.

    Знаменатели у воронки и у выручки при этом разные намеренно: «доля от всего,
    что пришло» и «доля выигранных среди закрытых» — законные и разные вопросы.
    Это и есть та пара, которой нужны РАЗНЫЕ подписи на экране.
    """
    funnel = report(manager_client, "funnel", **DECEMBER)
    revenue = report(manager_client, "revenue", **DECEMBER)
    sources = report(manager_client, "sources", **DECEMBER)
    row = next(r for r in sources["items"] if r["source"] == "возврат-декабрь")

    assert funnel["entered"] > 0, "пустое окно — проверять нечего"
    assert funnel["conversion"] == round(revenue["won_count"] * 100 / funnel["entered"], 1), (
        "конверсия воронки посчитана не от того «Выиграно», что стоит рядом с ней"
    )
    # Конверсия источника и конверсия выручки — одна величина в разном охвате: в
    # этом окне источник один, значит и число обязано быть одно.
    assert row["conversion"] == revenue["conversion"], (
        "«Конверсия» источника и «Конверсия» выручки посчитаны по-разному"
    )


# --- охват строки отчёта ---

def test_clients_column_narrows_with_the_rest_of_its_row(root_client):
    """Строка отчёта складывается из чисел одного охвата.

    Право «только свои заявки» сужало выигранные и потерянные в строке, а
    колонку «Клиентов» — нет. Строка получалась из делимого по всей фирме и
    делителя по своим: менеджер видел «клиентов 40, выиграно 2» и делал вывод о
    своей работе из чужих цифр.
    """
    from tests.conftest import make_manager
    from tests.test_roles import ROLES, _user_id

    made = root_client.post(
        ROLES,
        json={
            "name": "Отчёты только по своим заявкам",
            # Ни `deals.view_others`, ни прав на суммы: проверяем именно охват
            # строки, а не то, видно ли в ней деньги.
            "permissions": [
                "clients.view", "clients.create",
                "deals.view", "deals.create", "deals.move_stage",
                "reports.view",
            ],
        },
    )
    assert made.status_code == 201, made.text
    watcher = make_manager(root_client, "own-scope@test.local")
    user_id = _user_id(root_client, "own-scope@test.local")
    assert root_client.post(
        f"{ROLES}/assign/{user_id}", json={"role_id": made.json()["id"]}
    ).status_code == 200

    won = next(
        c for c in root_client.get(f"{API}/deals/board").json()["columns"] if c["kind"] == "won"
    )["key"]
    source = "охват-строки"

    def sell(client, name: str) -> None:
        made = client.post(f"{API}/clients", json={"name": name, "source": source})
        assert made.status_code == 201, made.text
        deal = client.post(
            f"{API}/deals", json={"title": name, "client_id": made.json()["id"]}
        )
        assert deal.status_code == 201, deal.text
        moved = client.post(f"{API}/deals/{deal.json()['id']}/move", json={"stage": won})
        assert moved.status_code == 200, moved.text

    sell(watcher, "Свой клиент")
    sell(root_client, "Чужой клиент")

    seen = watcher.get(f"{API}/reports/sources", params={"tz_offset": 0}).json()
    row = next(r for r in seen["items"] if r["source"] == source)

    assert row["won_count"] == 1, "заявки в строке уже сужались — тест ни о чём"
    assert row["clients"] == 1, (
        "колонка «Клиентов» посчитана по всей фирме, а соседние — по своим"
    )
    assert seen["clients_total"] == sum(r["clients"] for r in seen["items"])


# --- список источников ---

def test_a_deleted_client_leaves_the_sources_report(manager_client):
    """Клиент в корзине не должен рисовать в отчёте строку про себя.

    Список источников собирался отдельным проходом по всей таблице клиентов, и
    `deleted_at` в нём не смотрели. Источник, оставшийся только у удалённых,
    висел в отчёте пустой строкой — про людей, которых убрали.
    """
    person = new_client(manager_client, "Ушёл в корзину", source="источник-в-корзине", moment=at(8))
    assert "источник-в-корзине" in [
        row["source"] for row in report(manager_client, "sources")["items"]
    ], "источник живого клиента в отчёт не попал — проверять нечего"

    assert manager_client.delete(f"{API}/clients/{person['id']}").status_code == 200
    keys = [row["source"] for row in report(manager_client, "sources")["items"]]
    assert "источник-в-корзине" not in keys, "удалённый клиент остался строкой отчёта"


def test_a_source_untouched_in_the_period_is_not_a_row(manager_client):
    """Отчёт за март не рассказывает про источник, которого в марте не было.

    Своё значение источника попадало в отчёт из прохода по всем клиентам за всю
    историю — то есть навсегда и в любой период. Кроме нулевых строк в каждом
    отчёте это давало полное чтение таблицы клиентов на каждый показ: незаметно
    на тестовой базе и неработающе на третий год.
    """
    new_client(
        manager_client, "Позапрошлогодний", source="позапрошлый-год", moment=datetime(2024, 5, 5)
    )
    keys = [row["source"] for row in report(manager_client, "sources")["items"]]
    assert "позапрошлый-год" not in keys, "источник вне периода стал строкой отчёта"

    # А в своём периоде он, разумеется, есть.
    old = report(manager_client, "sources", **{"from": "2024-05-01", "to": "2024-05-31"})
    assert "позапрошлый-год" in [row["source"] for row in old["items"]]


# --- умолчание периода ---

def test_default_period_is_today_in_the_callers_timezone():
    """«Сегодня» без явных дат — местное, а не UTC.

    Интерфейс всегда присылает даты, поэтому расхождения не видно. Интеграции не
    присылают: для UTC+14 отчёт «за сегодня» после десяти утра по UTC отдавал
    вчерашний день, а первого числа — весь прошлый месяц. Проверяем оба края
    диапазона зон: хотя бы в одном из них местная дата всегда расходится с UTC.
    """
    for tz_offset in (-14 * 60, 12 * 60):  # UTC+14 и UTC-12
        shift = timedelta(minutes=tz_offset)
        before = now_utc()
        start, end, start_day, end_day = report_service.parse_period(None, None, tz_offset)
        after = now_utc()

        assert end_day in {(before - shift).date(), (after - shift).date()}, (
            f"умолчание «по» посчитано в UTC мимо tz_offset={tz_offset}"
        )
        assert start_day == end_day.replace(day=1), "умолчание «с» — первое число того же месяца"
        # И главное следствие: период по умолчанию обязан содержать текущий
        # момент. Иначе «за сегодня» отвечает про день, который ещё не начался
        # или уже кончился.
        assert start <= after < end, f"период по умолчанию не включает сейчас, tz_offset={tz_offset}"


def test_a_bad_timezone_offset_is_refused_before_it_is_used():
    """Смещение проверяется до того, как подействует на умолчание."""
    try:
        report_service.parse_period(None, None, 20 * 60)
    except Exception as failure:  # noqa: BLE001 — проверяем именно код ошибки
        assert getattr(failure, "code", None) == "bad_tz_offset"
    else:
        raise AssertionError("смещение за пределами суток принято")


# --- округление денег ---

def test_average_deal_does_not_round_a_half_to_the_even(manager_client):
    """Средний чек округляется как деньги, а не как `round()` в Python.

    `round()` округляет половину к ЧЁТНОМУ: 100,5 даёт 100, а 101,5 — 102. В
    отчёте о деньгах это ошибка в половину минорной единицы, гуляющая то в одну
    сторону, то в другую, и человеку, пересчитавшему чек на калькуляторе,
    объяснить её нечем.

    Ноябрь 2026 — своё пустое окно: здесь важно точное число, а не прирост.
    """
    window = {"from": "2026-11-01", "to": "2026-11-30", "tz_offset": 0}
    november = datetime(2026, 11, 12, 12, 0)
    person = new_client(manager_client, "Половина минорной единицы", moment=november)
    make_deal(manager_client, person["id"], "won", november, amount=100)
    make_deal(manager_client, person["id"], "won", november, amount=101)

    data = report(manager_client, "revenue", **window)
    assert data["won_priced"] == 2 and data["won_amount"] == 201, "окно не пустое — тест ни о чём"
    assert data["avg_check"] == 101, "100,5 округлилось к чётному вниз"
    assert data["months"][0]["avg_check"] == 101, "средний чек месяца округлён иначе, чем итог"


def test_money_is_divided_without_float():
    """Половина уходит ОТ нуля, и деление идёт целыми."""
    assert report_service.divide_money(201, 2) == 101  # round() дал бы 100
    assert report_service.divide_money(203, 2) == 102  # сюда round() попадает случайно
    assert report_service.divide_money(200, 2) == 100
    assert report_service.divide_money(-201, 2) == -101, "знак поменял правило округления"
    assert report_service.divide_money(1, 3) == 0
    assert report_service.divide_money(2, 3) == 1


# --- выручка считается по кассе -------------------------------------------------
#
# Решение владельца от 19.08.2026: банк один, источник правды о деньгах —
# финансовые операции. До него счётов было два и они не сходились: заказ на
# 12 000 оплачен, касса полна, а отчёт показывал пустой месяц — потому что
# считал выигранные заявки, а заказ к заявке привязан не был.


@pytest.fixture()
def kassa(root_client):
    """Блок «Финансы» на время проверки. По умолчанию он выключен."""
    vklyuchen = root_client.post(f"{MODULES}/finance", json={"enabled": True})
    assert vklyuchen.status_code == 200, vklyuchen.text
    yield
    root_client.post(f"{MODULES}/finance", json={"enabled": False})


def _dohodnaya(root_client) -> dict:
    sozdana = root_client.post(
        f"{API}/finance/categories",
        json={"name": f"Касса отчёта {at(1).isoformat()}", "direction": "income"},
    )
    assert sozdana.status_code == 201, sozdana.text
    return sozdana.json()


def _oplata(root_client, category_id: int, amount: int, when=None) -> None:
    otvet = root_client.post(
        f"{API}/finance/operations",
        json={
            "category_id": category_id,
            "amount": amount,
            "happened_at": (when or at(10)).isoformat(),
        },
    )
    assert otvet.status_code == 201, otvet.text


def test_dengi_bez_zayavki_vidny_v_otchyote(root_client, kassa):
    """Дословный сценарий карточки: оплата, не привязанная ни к какой заявке.

    Заказ у стойки законно живёт без заявки, и по прежнему счёту он был не виден
    вовсе: отчёт складывал суммы выигранных ЗАЯВОК. Владелец открывал главную,
    видел пустой месяц при полной кассе — и переставал верить системе.
    """
    do = report(root_client, "revenue")
    statya = _dohodnaya(root_client)
    _oplata(root_client, statya["id"], 12_000_00)

    itog = report(root_client, "revenue")
    assert itog["basis"] == "cash", "отчёт считает не по кассе при включённом блоке"
    # Приростом, а не абсолютом: база и отчётное окно у проверок общие, и
    # соседняя оплата сдвинула бы итог — приём тот же, что у проверок воронки.
    assert itog["received_amount"] - (do["received_amount"] or 0) == 12_000_00
    assert itog["received_count"] - (do["received_count"] or 0) == 1
    assert sum(m["received_amount"] for m in itog["months"]) == itog["received_amount"]


def test_bez_kassy_otchyot_govorit_chem_meryaet(root_client):
    """Блок выключен — считаем по заявкам и НАЗЫВАЕМ это, а не молчим.

    Пустое, а не ноль: «кассы в системе нет» и «денег не приходило» — разные
    ответы, и ноль на месте первого читался бы как второй.
    """
    itog = report(root_client, "revenue")
    assert itog["basis"] == "deals"
    assert itog["received_amount"] is None
    assert itog["received_count"] is None
    assert all(m["received_amount"] is None for m in itog["months"])


def test_vozvrat_umenshayet_postupleniya(root_client, kassa):
    """Возврат клиенту — доходная операция с минусом, и касса от неё убывает.

    Суммы заявок отрицательными не бывают, а касса — бывает: это разные
    величины, и складывать их в одну колонку было бы враньём в обе стороны.
    """
    do = report(root_client, "revenue")
    statya = _dohodnaya(root_client)
    _oplata(root_client, statya["id"], 5_000_00)
    _oplata(root_client, statya["id"], -2_000_00)

    itog = report(root_client, "revenue")
    assert itog["received_amount"] - do["received_amount"] == 3_000_00


def test_nachislennoe_pravilom_ne_schitaetsya_postupleniem(root_client, kassa):
    """Начисление в доходную статью деньгами в кассе не является.

    Возврат переплаченного налога — законная доходная статья, но денег от него
    не приходило. Сторож на `rule_id IS NULL`: потеряв это условие, отчёт
    разошёлся бы с врезкой «получено по заказу» на той же неделе.
    """
    dohod = _dohodnaya(root_client)
    do = report(root_client, "revenue")
    _oplata(root_client, dohod["id"], 1_000_00)

    vozvrat = root_client.post(
        f"{API}/finance/categories",
        json={"name": f"Возврат налога {at(2).isoformat()}", "direction": "income"},
    ).json()
    pravilo = root_client.post(
        f"{API}/finance/rules",
        json={
            "name": "Возврат переплаты",
            "base": "income_percent",
            "category_id": vozvrat["id"],
            "rate_bp": 1000,
        },
    )
    assert pravilo.status_code == 201, pravilo.text
    try:
        # Начисление рождается от прихода: платим ещё раз и смотрим, что в кассу
        # попал только сам платёж, а не начисленные с него десять процентов.
        _oplata(root_client, dohod["id"], 1_000_00)

        itog = report(root_client, "revenue")
        assert itog["received_amount"] - do["received_amount"] == 2_000_00, (
            "в кассу попало начисленное правилом — потеряно условие «не начисление»"
        )
    finally:
        # Убираем за собой. База у прогона ОБЩАЯ, и оставленная ставка живёт до
        # конца набора: сосед `test_finance_rules.py` считает всякую ставку
        # с непривычным именем засеянной миграцией и краснеет.
        # Поймано прогоном в обратном порядке файлов — тем самым, что
        # CI гоняет вторым заходом ровно ради таких сцепок.
        root_client.delete(f"{API}/finance/rules/{pravilo.json()['id']}")


def test_okna_otchyotov_lezhat_v_proshlom():
    """Ни одно окно этого файла не смеет лежать в будущем.

    Окна стояли в 2026-м, и 01.09.2026 сентябрьское перестало быть пустым:
    сделки, которые соседние тесты заводят «сейчас», попали внутрь, и
    `won_count` стал двойкой вместо нуля. Набор зеленел месяцами и упал в день,
    когда наступила дата, — ни правка кода, ни ревью такое не ловят.

    Проверка стоит на самих помощниках, а не на строках периодов: строку и
    помощник правят по отдельности, и разъехаться они обязаны заметно.
    """
    segodnya = datetime.now()
    for imya, moment in (("at", at(1)), ("sep", sep(1)), ("dec", dec(1))):
        assert moment < segodnya, f"окно {imya}() лежит в будущем — оно однажды наступит"


def test_dolgi_klientov_schitayut_ostatok_k_oplate(root_client):
    """Долги (план И-03): заказ на 1000, получено 300 — в отчёте остаток 700;
    доплата 700 — из отчёта уходит. Выгрузка — как у соседей."""
    bylo = {m["key"]: m["enabled"] for m in root_client.get(f"{MODULES}").json()["items"]}
    for key in ("documents", "warehouse", "orders", "finance"):
        root_client.post(f"{MODULES}/{key}", json={"enabled": True})
    try:
        statya = root_client.post(
            f"{API}/finance/categories", json={"name": "Долги: продажи", "direction": "income"}
        ).json()
        klient = root_client.post(f"{API}/clients", json={"name": "Должник отчёта"}).json()
        tovar = root_client.post(
            f"{API}/warehouse/products", json={"name": "Товар долга", "price": 500, "cost": 100}
        ).json()
        order = root_client.post(f"{API}/orders", json={"kind": "sales_order", "client_id": klient["id"]}).json()
        root_client.post(f"{API}/orders/{order['id']}/lines", json={"product_id": tovar["id"], "quantity": "2"})
        root_client.post(
            f"{API}/finance/payments",
            json={"category_id": statya["id"], "amount": 300, "document_id": order["id"], "client_id": klient["id"]},
        )
        otchyot = root_client.get(f"{REPORTS}/debts").json()
        [stroka] = [r for r in otchyot["items"] if r["document_id"] == order["id"]]
        assert stroka["total"] == 1000 and stroka["received"] == 300 and stroka["due"] == 700
        assert stroka["client_name"] == "Должник отчёта"
        assert otchyot["total_due"] >= 700

        csv = root_client.get(f"{REPORTS}/debts.csv", params=WINDOW)
        assert csv.status_code == 200 and csv.content.startswith(b"\xef\xbb\xbf")
        assert order["number"] in csv.content.decode("utf-8-sig")

        root_client.post(
            f"{API}/finance/payments",
            json={"category_id": statya["id"], "amount": 700, "document_id": order["id"], "client_id": klient["id"]},
        )
        assert not [r for r in root_client.get(f"{REPORTS}/debts").json()["items"] if r["document_id"] == order["id"]]
    finally:
        for key in ("finance", "orders", "warehouse", "documents"):
            root_client.post(f"{MODULES}/{key}", json={"enabled": bylo.get(key, False)})
