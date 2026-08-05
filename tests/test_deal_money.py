"""Деньги в сделке: сумма, предоплата, остаток.

Малый бизнес живёт деньгами, а не воронкой: без сумм канбан — доска задач.
Поэтому проверяем не «сохраняется ли число», а то, из-за чего потом не сходится
отчёт: что остаток считается и не хранится, что суммы целые, что итог по
колонке берёт все сделки, а не только показанные.
"""

from tests.conftest import API
from tests.test_deals import DEALS, make_client


def make_deal(manager_client, **extra):
    client = make_client(manager_client, extra.pop("client_name", "Платящий клиент"))
    body = {"title": "Работа за деньги", "client_id": client["id"]}
    body.update(extra)
    return manager_client.post(DEALS, json=body).json()


def test_remainder_is_calculated_not_stored(manager_client):
    """Третье поле рядом с суммой и предоплатой разошлось бы с ними при первой
    же правке. Остаток обязан считаться."""
    deal = make_deal(manager_client, amount=150000, prepaid=50000)
    assert deal["amount"] == 150000
    assert deal["prepaid"] == 50000
    assert deal["remainder"] == 100000
    assert deal["is_paid"] is False

    paid = manager_client.patch(f"{DEALS}/{deal['id']}", json={"prepaid": 150000}).json()
    assert paid["remainder"] == 0
    assert paid["is_paid"] is True


def test_amount_unset_is_not_the_same_as_zero(manager_client):
    """«Сумму ещё не назвали» и «работа бесплатная» — разные состояния.

    Подмени первое нулём — и в отчёте бесплатных работ станет столько же,
    сколько несогласованных.
    """
    unnamed = make_deal(manager_client)
    assert unnamed["amount"] is None
    assert unnamed["remainder"] is None
    assert unnamed["is_paid"] is False, "сделка без суммы не может считаться оплаченной"

    free = make_deal(manager_client, amount=0)
    assert free["amount"] == 0
    assert free["remainder"] == 0
    assert free["is_paid"] is True, "нулевая сумма закрыта нулевой оплатой"


def test_amount_can_be_taken_back_off(manager_client):
    """Ошиблись при вводе — должна быть возможность вернуть «не назначено»."""
    deal = make_deal(manager_client, amount=99000)
    cleared = manager_client.patch(f"{DEALS}/{deal['id']}", json={"amount": None}).json()
    assert cleared["amount"] is None
    assert cleared["remainder"] is None


def test_overpayment_is_allowed_and_visible(manager_client):
    """Клиент округлил вверх или доплатил за срочность — это жизнь, а не ошибка.

    Запрещать нельзя, но и прятать нельзя: остаток уходит в минус, и это видно.
    """
    deal = make_deal(manager_client, amount=100000, prepaid=120000)
    assert deal["remainder"] == -20000
    assert deal["is_paid"] is True


def test_negative_and_absurd_amounts_are_refused(manager_client):
    """Минус — бессмыслица, а лишний ноль превращает отчёт за месяц в кашу,
    и заметить это тем труднее, чем позже смотришь."""
    client = make_client(manager_client)
    for value, code in ((-1, "negative_money"), (10**13, "money_too_large")):
        bad = manager_client.post(
            DEALS, json={"title": "Кривая сумма", "client_id": client["id"], "amount": value}
        )
        assert bad.status_code == 422, value
        assert bad.json()["error"]["code"] == code


def test_column_total_counts_every_deal_not_only_the_shown_ones(manager_client):
    """Колонка канбана отдаётся с пределом.

    Сложи итог по загруженным карточкам — и он занизится ровно там, где сделок
    много, то есть там, где на него и смотрят. Ошибка тихая: число есть, оно
    правдоподобное, сверить можно только руками.
    """
    board = manager_client.get(f"{DEALS}/board").json()
    first = board["columns"][0]
    before = first["amount_total"]

    client = make_client(manager_client, "Клиент итога")
    for amount in (10000, 25000, 5000):
        manager_client.post(
            DEALS,
            json={
                "title": "В первую колонку",
                "client_id": client["id"],
                "stage": first["key"],
                "amount": amount,
            },
        )

    again = manager_client.get(f"{DEALS}/board").json()
    column = next(c for c in again["columns"] if c["key"] == first["key"])
    assert column["amount_total"] == before + 40000


def test_dashboard_shows_money_in_work_and_won_this_month(manager_client):
    """Владелец смотрит на деньги, а не на количество карточек."""
    board = manager_client.get(f"{DEALS}/board").json()
    open_stage = next(c for c in board["columns"] if c["kind"] == "open")
    won_stage = next(c for c in board["columns"] if c["kind"] == "won")

    before = manager_client.get(f"{API}/dashboard").json()
    client = make_client(manager_client, "Клиент сводки")

    manager_client.post(
        DEALS,
        json={"title": "В работе", "client_id": client["id"],
              "stage": open_stage["key"], "amount": 70000},
    )
    deal = manager_client.post(
        DEALS,
        json={"title": "Закрыта", "client_id": client["id"], "amount": 30000},
    ).json()
    manager_client.post(f"{DEALS}/{deal['id']}/move", json={"stage": won_stage["key"]})

    after = manager_client.get(f"{API}/dashboard").json()
    assert after["money_in_work"] == before["money_in_work"] + 70000
    assert after["money_won_this_month"] == before["money_won_this_month"] + 30000
    assert after["currency"], "валюта не отдана — сумму не на чем показать"


def test_money_is_stored_in_minor_units_as_whole_numbers(manager_client):
    """На дробных типах округление вылезает всегда: 0.1 + 0.2 != 0.3, и сумма
    колонки расходится с суммой карточек на копейку."""
    deal = make_deal(manager_client, amount=1, prepaid=0)
    assert isinstance(deal["amount"], int)
    assert deal["amount"] == 1, "минимальная единица не должна округляться"
