"""Отчёт продаж на сводке: чем меряет, что считает и чего не показывает.

Виджет — картинка, и проверять его на «нарисовалось» бессмысленно. Проверяется
то, из-за чего цифрам под ним перестанут верить: что счёт тот же, каким живут
соседние плитки денег (`bazis_vyruchki`), что рост считается к тому же числу
прошлого периода, а не к периоду целиком, и что без права на суммы виджета нет.

Разбор — `docs/dizayn/27-otchyot-prodazh.md`.
"""

from datetime import date, datetime, timezone

import pytest

from core.services import otchyot_prodazh_service as otchyot
from tests.conftest import API, make_manager
from tests.test_dashboard_deals import win
from tests.test_deals import make_client

OTCHYOT = f"{API}/dashboard/sales-report"


@pytest.fixture(autouse=True)
def finansy_vyklyucheny(root_client):
    """Базис выручки решает блок финансов, а он общий на всю базу набора.

    Без явного выключения соседний файл, включивший финансы, менял бы здесь
    смысл каждой цифры — и краснело бы это только в обратном порядке ворот.
    """
    root_client.post(f"{API}/modules/finance", json={"enabled": False})
    yield
    root_client.post(f"{API}/modules/finance", json={"enabled": False})


def test_otchyot_meryaet_tem_zhe_chem_plitki_deneg(root_client):
    """Один экран — один ответ на «сколько мы продали».

    Базис выручки живёт в одном месте на всю систему; реши виджет этот вопрос
    заново — и рядом со сводкой встало бы второе число под тем же словом.
    """
    otvet = root_client.get(OTCHYOT)
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["money_basis"] == "deals", "финансы выключены — считаем заявками"

    root_client.post(f"{API}/modules/finance", json={"enabled": True})
    try:
        assert root_client.get(OTCHYOT).json()["money_basis"] == "cash"
    finally:
        root_client.post(f"{API}/modules/finance", json={"enabled": False})


def test_vyigrannaya_zayavka_poyavlyaetsya_v_segodnyashney_kletke(root_client):
    """Клетка дня — не украшение: закрыли сделку, число выросло."""
    bylo = root_client.get(OTCHYOT).json()["kletki"][0]
    klient = make_client(root_client, "Отчёт продаж клетка")
    win(root_client, klient["id"], amount=150000)

    stalo = root_client.get(OTCHYOT).json()["kletki"][0]
    assert stalo["den"] == datetime.now(timezone.utc).date().isoformat(), "первой идёт не сегодняшняя клетка"
    assert stalo["znachenie"] == bylo["znachenie"] + 1
    assert stalo["summa_minor"] == bylo["summa_minor"] + 150000


def test_v_podskazku_dnya_edet_ne_ves_den(root_client):
    """В клетке бывает и пятьдесят событий, а поле — подсказка, а не список.

    Счётчик при этом считает ВСЁ: по разнице с числом строк виджет печатает
    «ещё N», и совпади они — эта строка исчезла бы навсегда.
    """
    klient = make_client(root_client, "Отчёт продаж подсказка")
    for _ in range(otchyot.V_DNE + 2):
        win(root_client, klient["id"], amount=10000)

    segodnya = root_client.get(OTCHYOT).json()["kletki"][0]
    assert len(segodnya["sobytiya"]) == otchyot.V_DNE, "в подсказку уехал весь день"
    assert segodnya["znachenie"] > otchyot.V_DNE, "счётчик посчитал только показанное"
    assert all(s["nomer"].startswith("#") for s in segodnya["sobytiya"])


def test_kletok_rovno_vosem_na_vosem_i_svezhaya_pervaya(root_client):
    """Восемь рядов по восемь: сетка образца, и дыр в ней быть не должно."""
    dannye = root_client.get(OTCHYOT).json()
    assert len(dannye["kletki"]) == otchyot.DNEY
    dni = [k["den"] for k in dannye["kletki"]]
    assert dni == sorted(dni, reverse=True), "клетки идут не от свежей к старой"
    assert len(set(dni)) == otchyot.DNEY, "день повторился дважды"


def test_v_mesyatse_stolko_dney_skolko_v_kalendare(root_client):
    """Столбец матрицы — день месяца, и февраль короче мая.

    Пришли сервер ровно тридцать чисел на каждый месяц — и точки поехали бы
    относительно дат, а последний день февраля рисовался бы первым мартовским.
    """
    from calendar import monthrange

    mesyatsy = root_client.get(OTCHYOT).json()["mesyatsy"]
    assert len(mesyatsy) == otchyot.MESYATSEV
    for m in mesyatsy:
        nachalo = date.fromisoformat(m["mesyats"])
        assert nachalo.day == 1, "месяц назван не первым числом"
        assert len(m["dni"]) == monthrange(nachalo.year, nachalo.month)[1]
        assert m["summa_minor"] == sum(m["dni"]), "подпись месяца разошлась с его днями"
    assert [m["mesyats"] for m in mesyatsy] == sorted(m["mesyats"] for m in mesyatsy), (
        "месяцы идут не по порядку — на образце старший сверху"
    )


def test_rost_schitaetsya_k_tomu_zhe_chislu_proshlogo_perioda():
    """Иначе первого числа каждый месяц падал бы на девяносто процентов.

    Сравнение с ПОЛНЫМ прошлым месяцем — самая частая ошибка таких виджетов:
    седьмого числа месяц прожит на пятую часть, и «−80%» появлялось бы каждый
    месяц само собой.
    """
    segodnya = date(2026, 3, 7)
    assert otchyot._tot_zhe_den(segodnya, 2026, 2) == date(2026, 2, 7)
    # Тридцать первого мая в апреле нет — берём последний день, а не уезжаем в май.
    assert otchyot._tot_zhe_den(date(2026, 5, 31), 2026, 4) == date(2026, 4, 30)
    # 29 февраля в невисокосном прошлом году не существует.
    assert otchyot._tot_zhe_den(date(2024, 2, 29), 2023, 2) == date(2023, 2, 28)


def test_bez_proshlogo_perioda_rost_ne_vydumyvaetsya():
    """Пустой прошлый период — это «не с чем сравнить», а не «рост на 100%»."""
    assert otchyot._rost_bp(500, 0) is None
    assert otchyot._rost_bp(120, 100) == 2000
    assert otchyot._rost_bp(80, 100) == -2000, "спад обязан приезжать со знаком"


def test_gorod_bez_imeni_ne_stanovitsya_gorodom(root_client):
    """Пустая строка в списке городов — это пропуск в данных, а не город.

    Оставь её — и первой строкой она встанет чаще всех: карточек без города в
    любой базе больше, чем с любым одним городом.
    """
    bezymyannyy = make_client(root_client, "Отчёт продаж без города")
    win(root_client, bezymyannyy["id"], amount=9_000_000)
    imenitiy = root_client.post(
        f"{API}/clients", json={"name": "Отчёт продаж с городом", "city": "Львов"}
    ).json()
    win(root_client, imenitiy["id"], amount=5_000_000)

    goroda = root_client.get(OTCHYOT).json()["goroda"]
    assert goroda, "города не сосчитались вовсе"
    assert all(g["gorod"] for g in goroda), "в список уехал город без имени"
    summy = [g["summa_minor"] for g in goroda]
    assert summy == sorted(summy, reverse=True), "города идут не по убыванию суммы"


def test_otchyot_zakryt_pravom_na_summy(root_client):
    """Под тепловой картой стоят деньги за месяц и за год: кому суммы не
    показывают, тому и виджета нет."""
    rol = root_client.post(
        f"{API}/roles", json={"name": "Отчёт без сумм", "permissions": ["deals.view"]}
    )
    assert rol.status_code == 201, rol.text
    pochta = "otchyot.bez.summ@test.local"
    smotritel = make_manager(root_client, pochta)
    lyudi = root_client.get(f"{API}/staff").json()["items"]
    user_id = next(u["id"] for u in lyudi if u["email"] == pochta)
    assert root_client.post(
        f"{API}/roles/assign/{user_id}", json={"role_id": rol.json()["id"]}
    ).status_code == 200

    try:
        assert smotritel.get(OTCHYOT).status_code == 403
        # И в раскладку сводки он его не положит: реестр называет то же право.
        otkaz = smotritel.put(
            f"{API}/dashboard/layout",
            json={"widgets": [{"kind": "sales_grid", "w": 2, "params": {}}]},
        )
        assert otkaz.status_code in (403, 422), otkaz.text
    finally:
        root_client.delete(f"{API}/roles/{rol.json()['id']}")


def test_vidzhet_stavitsya_v_raskladku(root_client):
    """Оба вида — самостоятельные виджеты, и сводка их принимает."""
    raskladka = root_client.put(
        f"{API}/dashboard/layout",
        json={
            "widgets": [
                {"kind": "sales_grid", "x": 0, "y": 0, "params": {}},
                {"kind": "sales_matrix", "x": 6, "y": 0, "params": {}},
            ]
        },
    )
    assert raskladka.status_code == 200, raskladka.text
    vidy = [w["kind"] for w in raskladka.json()["layout"]["widgets"]]
    assert vidy == ["sales_grid", "sales_matrix"]
    root_client.delete(f"{API}/dashboard/layout")


def test_okna_sutok_ne_peresekayutsya(root_client):
    """`CASE` относит строку к ПЕРВОМУ подошедшему окну.

    Пересекись они — день посчитался бы один раз вместо двух, и разошлись бы
    ровно те две карточки, которые обязаны показывать одно и то же.
    """
    okna = otchyot._sutki(date(2026, 1, 30), date(2026, 2, 2))
    assert len(okna) == 4
    for (_, konets), (nachalo, _) in zip(okna, okna[1:]):
        assert konets == nachalo, "между сутками появилась щель или нахлёст"
