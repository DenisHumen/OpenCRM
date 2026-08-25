"""Счётчики подбора обязаны щёлкать на настоящих отказах.

**Зачем эта проверка, если метрика и так отдаётся.** Отдаваемая метрика,
которая никогда не растёт, — худший из возможных исходов: панель показывает
ровный ноль, ноль читается как «нас никто не трогает», и тревожиться перестают
именно тогда, когда пора. Отсутствующая панель хотя бы честна.

Поэтому проверки ниже идут через ЖИВОЙ вход, живую проверку PIN и живой запрос
бланка, а не зовут `otmetit` напрямую. Прямой вызов подтвердил бы, что Redis
умеет складывать, — о чём никто и не спрашивал. Вопрос в том, стоит ли вызов
на пути отказа, и ответить на него можно только пройдя этим путём.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core import bezopasnost, redis_client
from tests.conftest import app, login, register


@pytest.fixture(autouse=True)
def chistye_schetchiki():
    """Каждая проверка начинает с нуля — иначе они считают соседей."""
    bezopasnost.zabyt()
    yield
    bezopasnost.zabyt()


def _est_redis() -> bool:
    return redis_client.get_client() is not None


trebuet_redis = pytest.mark.skipif(
    not _est_redis(),
    reason="счётчики живут в Redis; без него мерить нечего",
)


@trebuet_redis
def test_nevernyy_parol_schitaetsya():
    """Один промах по паролю — одна единица в счётчике."""
    email = "schyot-promah@test.local"
    register(TestClient(app), "Promah", email)
    klient = TestClient(app)

    do = bezopasnost.snyat()["vhod_promah"]
    otvet = login(klient, email, "sovsem-ne-tot-parol")
    assert otvet.status_code == 401
    posle = bezopasnost.snyat()["vhod_promah"]

    assert posle == do + 1, (
        "промах по паролю не сосчитан. Метрика при этом отдаётся, то есть "
        "панель показывает ровный ноль и читается как «подбора нет»"
    )


@trebuet_redis
def test_udachnyy_vhod_nichego_ne_schitaet():
    """Обратная сторона: честный вход не имеет права шуметь на графике.

    Без этой проверки счётчик легко «починить» так, что он растёт на каждом
    обращении к входу, — и график подбора станет вторым именем для посещаемости.
    Выглядеть при этом он будет убедительно: числа большие, всплески по утрам.
    """
    email = "schyot-udacha@test.local"
    parol = "Sovershenno-Nadyozhnyy-1"
    register(TestClient(app), "Udacha", email, password=parol)
    klient = TestClient(app)

    do = bezopasnost.snyat()
    otvet = login(klient, email, parol)
    assert otvet.status_code in (200, 403), otvet.text
    posle = bezopasnost.snyat()

    assert posle == do, f"честный вход что-то насчитал: было {do}, стало {posle}"


@trebuet_redis
def test_zapret_ogranichitelya_schitaetsya_otdelno_ot_promahov():
    """Промах и запрет — разные числа, и в этом весь смысл деления.

    Промах говорит «кто-то ошибся», запрет — «защита сработала». Слитые в одно
    число, они отвечают на вопрос «плохо ли», но не на вопрос «держит ли», а
    ночью нужен именно второй.
    """
    email = "schyot-zapert@test.local"
    register(TestClient(app), "Zapert", email)
    klient = TestClient(app)

    for _ in range(5):
        assert login(klient, email, "ne-tot-parol").status_code == 401
    zapert = login(klient, email, "ne-tot-parol")
    assert zapert.status_code == 429

    schyot = bezopasnost.snyat()
    assert schyot["vhod_promah"] == 5, (
        f"промахов сосчитано {schyot['vhod_promah']}, а их было ровно пять"
    )
    assert schyot["vhod_zapert"] == 1, (
        f"запрет ограничителя сосчитан как {schyot['vhod_zapert']}, а он был один"
    )


@trebuet_redis
def test_nesushchestvuyushchaya_pochta_i_nevernyy_parol_v_odnom_vide():
    """Разделить их значило бы выдать графиком то, что прячет ответ.

    Сообщение об ошибке на входе одно на оба случая — нарочно, чтобы
    подбирающий не узнал, какие почты заведены. Счётчик, разделивший их, сдал
    бы это за нас: ряд «спросили несуществующего» и есть ответ на вопрос
    «а такая почта у вас есть?», только с задержкой в минуту.
    """
    klient = TestClient(app)
    otvet = login(klient, "takogo-cheloveka-net@test.local", "chto-ugodno")
    assert otvet.status_code == 401
    assert otvet.json()["error"]["code"] == "invalid_credentials"

    schyot = bezopasnost.snyat()
    assert schyot["vhod_promah"] == 1
    # Ни одного отдельного вида про «нет такой почты» существовать не должно.
    assert not any("net" in vid or "unknown" in vid for vid in schyot), (
        f"завёлся вид, разделяющий «нет почты» и «не тот пароль»: {sorted(schyot)}"
    )


@trebuet_redis
def test_neizvestnyy_vid_ne_zavodit_ryad_prizrak():
    """Опечатка в имени вида не имеет права завести ряд, который никто не ждёт."""
    do = bezopasnost.snyat()
    bezopasnost.otmetit("takogo-vida-net")
    assert bezopasnost.snyat() == do


def test_bez_redisa_schyot_ne_padaet(monkeypatch):
    """Лежащий Redis не имеет права уронить вход.

    Порядок важности прямой: не сосчитанная попытка — это дырка в графике,
    а упавший из-за счётчика вход — это человек, который не может работать.
    """
    monkeypatch.setattr(redis_client, "get_client", lambda: None)
    bezopasnost.otmetit("vhod_promah")  # не должно бросить
    assert bezopasnost.snyat() == {vid: 0 for vid in bezopasnost.VIDY}


def test_vse_vidy_opisany_po_russki():
    """Вид без описания — это `blank_zapert` и никакой подсказки.

    В МЕТКУ описание не уезжает нарочно (довод — в `_collect_bezopasnost`:
    метка входит в тождество ряда, и правка формулировки оборвала бы историю
    графика). Значит единственное место, где написано, что значит имя вида, —
    вот этот словарь, и пустое описание здесь оставляет читателя наедине с
    транслитерацией.
    """
    for vid, opisanie in bezopasnost.VIDY.items():
        assert opisanie and opisanie.strip(), f"вид {vid} без описания"
        assert any("а" <= bukva <= "я" for bukva in opisanie.lower()), (
            f"описание вида {vid} не по-русски: {opisanie!r}"
        )


def test_kazhdyy_vid_konchaetsya_promahom_ili_zapretom():
    """Панели отбирают виды по окончанию имени, а не перечислением.

    Запросы на дашборде — `vid=~".*_promah"` и `vid=~".*_zapert"`. Вид,
    названный иначе, не попадёт НИ В ОДИН из них: он будет исправно считаться и
    нигде не показываться. Такую пропажу невозможно заметить глазом — на
    дашборде она выглядит как отсутствие событий.
    """
    for vid in bezopasnost.VIDY:
        assert vid.endswith(("_promah", "_zapert")), (
            f"вид {vid!r} не оканчивается ни на _promah, ни на _zapert — "
            f"он не попадёт ни на одну панель дашборда безопасности"
        )
