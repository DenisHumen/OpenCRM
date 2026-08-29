"""Общий поиск командной палитры (Ctrl+K).

У поиска нет своего экрана и своих настроек, поэтому его легко считать мелочью.
На деле это единственное место, где одним запросом просматривается **сразу
несколько разделов**, — и потому единственное место, где обход блока или права
не виден никому: раздел закрыт, пункт из меню убран, а название записи из него
по-прежнему приезжает в выдачу.

Отсюда и состав проверок. Что заявки вообще находятся (без них палитра отвечала
на вопрос «где мой заказ» молчанием). Что строка поиска — это строка, а не
шаблон LIKE. Что выдача ДОЛИСТЫВАЕТСЯ до последней находки. И что каждая группа
пустеет по обеим причинам сразу: выключенный блок и отсутствующее право, —
причём на каждой странице, а не только на первой.

База у тестов общая и переживает файл, поэтому роли и сотрудники убираются за
собой, а свои записи ищутся по приметной приставке, а не по количеству строк.
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from core.services import modules_service
from database.repositories import deals as deals_repo
from tests.conftest import API, make_manager

SEARCH = f"{API}/search"
ROLES = f"{API}/roles"
STAFF = f"{API}/staff"

#: Форма пустой группы. `has_more` здесь наравне с `total`: ключи ответа не
#: зависят ни от прав, ни от набора блоков — иначе клиенту пришлось бы знать,
#: какие из них сегодня бывают.
EMPTY = {"items": [], "total": 0, "has_more": False}

#: Сколько строк в ОДНОЙ странице группы (`web/api/routes/search.py`).
#:
#: Именно в странице, а не всего: пределом выдачи это число было ровно один раз
#: и стоило беды — см. `test_palitra_dolistyvaetsya_do_kazhdoy_nakhodki`.
GROUP_LIMIT = 6

#: Сколько записей заводит проверка полноты выдачи.
#:
#: Две полные страницы и ещё одна запись сверх них. Меньше нельзя: на
#: `GROUP_LIMIT + 1` записи зелёным оказался бы и потолок в две страницы, а
#: беда — это именно потолок, где бы он ни стоял.
SKOLKO = GROUP_LIMIT * 2 + 1


# --- вспомогательное ---------------------------------------------------------


@pytest.fixture
def role_maker(root_client):
    """Роли с уборкой: тело теста может и не дойти до конца, а имя роли занято."""
    created: list[int] = []

    def make(name: str, codes: list[str]) -> dict:
        response = root_client.post(ROLES, json={"name": name, "permissions": codes})
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
        return response.json()

    yield make

    for role_id in created:
        root_client.delete(f"{ROLES}/{role_id}")


@pytest.fixture
def staff_maker(root_client):
    """Сотрудник с назначенной ролью и его залогиненный клиент."""
    created: list[int] = []

    def make(email: str, role_id: int | None) -> TestClient:
        client = make_manager(root_client, email)
        people = root_client.get(STAFF).json()["items"]
        user_id = next(u["id"] for u in people if u["email"] == email)
        created.append(user_id)
        assigned = root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": role_id})
        assert assigned.status_code == 200, assigned.text
        return client

    yield make

    for user_id in created:
        root_client.delete(f"{STAFF}/{user_id}")


def _client_id(client: TestClient, name: str) -> int:
    response = client.post(f"{API}/clients", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _deal(client: TestClient, **fields) -> dict:
    response = client.post(f"{API}/deals", json=fields)
    assert response.status_code == 201, response.text
    return response.json()


def _titles(found: dict, group: str = "deals") -> list[str]:
    return [item.get("title") or item.get("name") for item in found[group]["items"]]


def _dolistat(client: TestClient, group: str, q: str, predel: int = 20) -> list[dict]:
    """Пройти группу выдачи ДО КОНЦА — так же, как это делает «показать ещё».

    Ровно тот путь, которым ходит палитра: первая страница приходит в общем
    ответе, продолжение — отдельными запросами по номеру страницы. Потолок
    `predel` здесь не про размер выдачи, а про зацикливание: страница, которая
    всегда говорит «есть ещё», иначе повесила бы прогон молча.
    """
    sobrano: list[dict] = []
    stranitsa = 1
    while True:
        otvet = client.get(f"{SEARCH}/{group}", params={"q": q, "page": stranitsa})
        assert otvet.status_code == 200, otvet.text
        kusok = otvet.json()
        assert set(kusok) == {"items", "total", "has_more"}, (
            f"продолжение группы {group} отвечает не той формой, что общий поиск: {set(kusok)}"
        )
        sobrano += kusok["items"]
        if not kusok["has_more"]:
            return sobrano
        assert kusok["items"], (
            f"группа {group} обещает продолжение, а страница {stranitsa} пустая — "
            "«показать ещё» будет дописывать пустоту без конца"
        )
        stranitsa += 1
        assert stranitsa <= predel, f"группа {group} не кончается: страниц больше {predel}"


# --- выдача обязана быть полной ----------------------------------------------


def test_palitra_dolistyvaetsya_do_kazhdoy_nakhodki(root_client, manager_client):
    """Ради чего всё: из палитры видно ВСЕ находки, а не первые шесть.

    Беда снята с боевого сайта. Запрос «be», группа «доски», ровно шесть строк
    и подпись «↑↓ — выбор, ⏎ — открыть». Досок под «be» было больше шести, а
    седьмую нельзя было увидеть ничем: ни прокруткой в окне, ни «показать ещё»,
    ни отдельным запросом — продолжения в API не существовало вовсе. Шестёрка
    была последним словом системы, и человек не знал даже, что за ней что-то
    есть.

    Проверка листает до конца и сверяет НАБОР найденного с тем, что завели, —
    а не считает строки на первой странице. Так она краснеет на любом возврате
    предела, а не только на возврате шестёрки: уберут продолжение — покраснеет;
    поставят потолок на второй странице — покраснеет; начнут строки двоиться
    или проваливаться между страницами — покраснеет тоже, потому что множество
    не сойдётся.

    Все три группы разом: предел стоял в одном общем месте, и вернуться он
    может в любую из них поодиночке.
    """
    assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200

    # Приставки у групп разные: имя клиента входит в склейку заявки и доски, и
    # общая приставка сделала бы находки одной группы находками другой — тогда
    # проверка сверяла бы выдачу с чужим списком.
    zavedeno: dict[str, set[int]] = {"clients": set(), "deals": set(), "boards": set()}

    for number in range(SKOLKO):
        zavedeno["clients"].add(_client_id(manager_client, f"Длстнк Клиент {number}"))

    opora = _client_id(manager_client, "Длстн Опора")
    for number in range(SKOLKO):
        zavedeno["deals"].add(
            _deal(manager_client, title=f"Длстнз Работа {number}", client_id=opora)["id"]
        )
        doska = manager_client.post(
            f"{API}/boards", json={"title": f"Длстнд Доска {number}", "client_id": opora}
        )
        assert doska.status_code == 201, doska.text
        zavedeno["boards"].add(doska.json()["id"])

    zaprosy = {"clients": "Длстнк Клиент", "deals": "Длстнз Работа", "boards": "Длстнд Доска"}
    for group, zapros in zaprosy.items():
        # Первая страница приходит в общем ответе — с неё палитра и начинает.
        pervaya = manager_client.get(SEARCH, params={"q": zapros}).json()[group]
        assert len(pervaya["items"]) == GROUP_LIMIT
        assert pervaya["has_more"] is True, (
            f"{group}: заведено {SKOLKO} записей, показано {GROUP_LIMIT}, "
            "а продолжения не обещано — выдача обрывается молча"
        )

        vsyo = _dolistat(manager_client, group, zapros)
        nomera = [item["id"] for item in vsyo]
        assert len(nomera) == len(set(nomera)), (
            f"{group}: одна запись приехала на двух страницах — смещение считается не по тому "
            "размеру страницы"
        )
        assert set(nomera) == zavedeno[group], (
            f"{group}: долистали до {len(set(nomera))} находок из {SKOLKO} — "
            "выдача упирается в потолок вместо того, чтобы кончиться"
        )


def test_prodolzhenie_sobrano_tak_zhe_kak_pervaya_stranitsa(manager_client):
    """Первая страница общего поиска и первая страница продолжения — одно и то же.

    Два входа в одни данные разъезжаются молча и в мелочах: у заявки со второй
    страницы пропало бы имя клиента, у доски — обложка. Увидеть это можно было
    бы только глазами и только долистав, поэтому сверяем не глазами.
    """
    client_id = _client_id(manager_client, "Сврк Заказчик")
    _deal(manager_client, title="Сврк Работа", client_id=client_id)

    obshchiy = manager_client.get(SEARCH, params={"q": "Сврк"}).json()["deals"]
    otdelnaya = manager_client.get(f"{SEARCH}/deals", params={"q": "Сврк", "page": 1}).json()
    assert otdelnaya == obshchiy, (
        "продолжение собирает карточку не так, как общий поиск: вторая страница выдачи "
        "будет отличаться от первой"
    )


def test_u_kazhdoy_gruppy_vydachi_est_prodolzhenie(root_client, manager_client):
    """Новая группа в палитре обязана листаться с первого дня.

    Иначе беда возвращается по частям: группы добавляют по одной, и та, которой
    продолжения не завели, снова упирается в шесть строк — а прочие листаются,
    и со стороны всё выглядит работающим.
    """
    assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200

    obshchiy = manager_client.get(SEARCH).json()
    gruppy = [klyuch for klyuch in obshchiy if klyuch != "query"]
    assert len(gruppy) >= 3, f"групп в выдаче найдено {gruppy} — проверка смотрит не туда"

    for group in gruppy:
        otvet = manager_client.get(f"{SEARCH}/{group}", params={"page": 2})
        assert otvet.status_code == 200, (
            f"у группы «{group}» нет продолжения: {otvet.status_code} {otvet.text}"
        )
        assert set(otvet.json()) == {"items", "total", "has_more"}


def test_prodolzhenie_nesushchestvuyushchey_gruppy_otkazyvaet(manager_client):
    """Опечатка в имени группы — отказ, а не пустая выдача.

    Пустота читалась бы как «такое есть, но ничего не нашлось», и «показать
    ещё», пристроенное не к той группе, выглядело бы как честно кончившаяся
    выдача.
    """
    otvet = manager_client.get(f"{SEARCH}/skladskie-ostatki", params={"q": "что угодно"})
    assert otvet.status_code == 404, otvet.text


def test_nulevaya_stranitsa_prodolzheniya_ne_prokhodit(manager_client):
    """`page = 0` дал бы `OFFSET -6`, а это для MySQL синтаксическая ошибка.

    Отказ на границе, а не молчаливая подмена первой страницей: страница,
    показанная вместо запрошенной, выглядит как работающее продолжение и
    заканчивается тем, что «показать ещё» дописывает одно и то же по кругу.
    """
    for stranitsa in (0, -1):
        otvet = manager_client.get(f"{SEARCH}/deals", params={"page": stranitsa})
        assert otvet.status_code == 422, f"страница {stranitsa}: {otvet.status_code} {otvet.text}"


def test_palitra_umeet_poprosit_prodolzhenie(manager_client):
    """Ручка есть — а нажимает ли её палитра.

    Проверяется по исходникам, как и прочие правила экранов
    (`tests/test_screens.py`): собранного фронтенда в наборе нет. Правило же
    простое и читается глазами — и ровно оно было нарушено на боевом сайте:
    сервер честно присылал `has_more`, а палитра этот ключ не читала вовсе, и
    выдача обрывалась на шести строках при живом продолжении в API.

    Поэтому сверяются обе половины: что признак «есть ещё» вообще читается и
    что за следующей страницей палитра ходит по адресу продолжения.
    """
    import pathlib
    import re

    palitra = (
        pathlib.Path(__file__).resolve().parent.parent
        / "web" / "frontend" / "crm" / "src" / "components" / "CommandPalette.tsx"
    ).read_text(encoding="utf-8")

    # Комментарии снимаем, и это не придирка: докстрока рядом с починкой сама
    # называет и `has_more`, и адрес продолжения. Ищи мы по всему файлу — проверка
    # осталась бы зелёной над кодом, из которого вынули всё, кроме объяснения,
    # почему так сделано.
    kod = re.sub(r"/\*.*?\*/", "", palitra, flags=re.S)
    kod = "\n".join(re.sub(r"//.*$", "", stroka) for stroka in kod.splitlines())

    assert "has_more" in kod, (
        "палитра не читает `has_more`: сервер говорит «есть ещё», а показать это нечем"
    )
    assert "/search/${" in kod and "page=${" in kod, (
        "палитра не просит следующую страницу группы — «показать ещё» ведёт в никуда"
    )
    assert "showMore" in kod, "в палитре нет строки «показать ещё» — нажимать не на что"


# --- заявки в выдаче ---------------------------------------------------------


def test_zayavka_nakhoditsya_poiskom(manager_client):
    """Ради чего всё: палитра искала по клиентам и доскам, а заявок не знала.

    Заявка — стержень системы, и «где мой заказ» — первый вопрос, с которым
    жмут Ctrl+K. Ответом было молчание, а карточка при этом лежала в базе.
    """
    client_id = _client_id(manager_client, "Нхдм Заказчик")
    deal = _deal(
        manager_client,
        title="Нхдм Ремонт витрины",
        client_id=client_id,
        description="Нхдмопсн замена стекла",
    )

    by_title = manager_client.get(SEARCH, params={"q": "Нхдм Ремонт"}).json()
    assert [item["id"] for item in by_title["deals"]["items"]] == [deal["id"]]

    card = by_title["deals"]["items"][0]
    assert card["title"] == "Нхдм Ремонт витрины"
    # Имя клиента — подпись строки: две «Доставки» разных заказчиков иначе не
    # различить, и это тот же довод, по которому оно есть у доски.
    assert card["client_name"] == "Нхдм Заказчик"

    by_description = manager_client.get(SEARCH, params={"q": "Нхдмопсн"}).json()
    assert [item["id"] for item in by_description["deals"]["items"]] == [deal["id"]]

    # «Что там по Ромашке» — в жизни спрашивают именем клиента, а не названием
    # работы; репозиторий это умеет, и палитра обязана уметь тоже.
    by_client = manager_client.get(SEARCH, params={"q": "Нхдм Заказчик"}).json()
    assert deal["id"] in {item["id"] for item in by_client["deals"]["items"]}


def test_pustoy_zapros_pokazyvaet_i_nedavnie_zayavki(manager_client):
    """Палитра открывается не пустой — иначе первое нажатие Ctrl+K ничего не даёт."""
    client_id = _client_id(manager_client, "Ндвн Заказчик")
    deal = _deal(manager_client, title="Ндвн Свежая работа", client_id=client_id)

    recent = manager_client.get(SEARCH).json()
    assert recent["clients"]["items"], "недавние клиенты пропали из пустого запроса"
    assert deal["id"] in {item["id"] for item in recent["deals"]["items"]}


def test_forma_otveta_odna_pri_lyubom_naboru_grupp(root_client, manager_client):
    """Группа приходит пустой, а не исчезает.

    Клиенту API иначе пришлось бы знать, какие ключи сегодня бывают, — а
    зависит это от блоков и прав, то есть от чужих настроек.
    """
    assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200

    found = manager_client.get(SEARCH, params={"q": "щщщнеттакого"}).json()
    assert set(found) == {"query", "clients", "deals", "boards"}
    for group in ("clients", "deals", "boards"):
        assert found[group] == EMPTY, group


def test_total_ne_obeshchaet_bolshe_chem_pokazano(manager_client):
    """`total` — это длина показанного, а «есть ли ещё» отвечает `has_more`.

    Точное число найденного из палитры ушло намеренно: оно стоило второго
    полного прохода по таблице на каждую группу — ровно столько же, сколько
    сама выборка, — и никем не показывалось. Проверяем не только новый ключ, но
    и обещание: `total` никогда не больше, чем строк на экране, иначе подпись
    «найдено N» когда-нибудь припишут к числу, которого в списке нет.
    """
    client_id = _client_id(manager_client, "Ттлмн Заказчик")
    for number in range(GROUP_LIMIT + 1):
        _deal(manager_client, title=f"Ттлмн Работа {number}", client_id=client_id)

    many = manager_client.get(SEARCH, params={"q": "Ттлмн Работа"}).json()["deals"]
    assert len(many["items"]) == GROUP_LIMIT
    assert many["total"] == GROUP_LIMIT, "total обещает больше, чем показано"
    assert many["has_more"] is True, "семь совпадений, а «есть ещё» не сказано"

    # Ровно столько, сколько влезает: продолжения нет, и обещать его нельзя —
    # иначе подпись «и ещё» появлялась бы на полной, но последней странице.
    exact = manager_client.get(SEARCH, params={"q": "Ттлмн Работа 3"}).json()["deals"]
    assert len(exact["items"]) == 1
    assert exact["total"] == 1
    assert exact["has_more"] is False


def test_palitra_nakhodit_po_kusku_slova_vnutri(manager_client):
    """Поиск остаётся ПОДСТРОЧНЫМ: «ванов» находит «Иванова», «бук» — «Ноутбук».

    Сторож против тихого сползания на поиск по началу слова. Соблазн понятен:
    префикс берёт индекс, а подстрока с ведущим `%` не берёт его вовсе, и
    переписать условие «чтобы стало быстро» ничего не стоит. Цена
    же измерена на большой базе: «Иванов» подстрокой находит 14 132 клиента,
    префиксом — ноль, потому что фамилия стоит в имени второй.
    """
    client_id = _client_id(manager_client, "Кскслв Алексей Ивановский")
    deal = _deal(manager_client, title="Кскслв Ремонт ноутбука", client_id=client_id)

    vnutri_imeni = manager_client.get(SEARCH, params={"q": "вановский"}).json()
    assert client_id in {item["id"] for item in vnutri_imeni["clients"]["items"]}

    vnutri_nazvaniya = manager_client.get(SEARCH, params={"q": "утбук"}).json()
    assert deal["id"] in {item["id"] for item in vnutri_nazvaniya["deals"]["items"]}


def test_poisk_po_nomeru_otvechaet_pro_nomer(manager_client):
    """Цифры ищутся только в телефоне, а не по всей склейке карточки.

    Разница не теоретическая. В склейку входят имя, фирма, почта и метки, и у
    людей там встречаются длинные числа — номер заказа в имени, ЕДРПОУ в
    названии фирмы. Ищи мы цифры по всей склейке, карточка без телефона вовсе
    находилась бы по ЧУЖОМУ номеру: человек набирает телефон клиента, а видит
    посторонних. Поиск по номеру обязан отвечать про номер.

    Отдельным условием, а не общим: искомое приводится к цифрам, чтобы
    «+380 67 111 22 33» нашлось как набрано в карточке, — и это приведение
    осмысленно ровно для телефона.
    """
    s_nomerom_v_imeni = _client_id(manager_client, "Цфрраз Заказ 380671119999")
    s_telefonom = manager_client.post(
        f"{API}/clients", json={"name": "Цфрраз Пётр", "phone": "+380 67 111 99 99"}
    )
    assert s_telefonom.status_code == 201, s_telefonom.text

    nayden = manager_client.get(SEARCH, params={"q": "+380671119999"}).json()
    nomera = {item["id"] for item in nayden["clients"]["items"]}
    assert s_telefonom.json()["id"] in nomera, "карточка с этим телефоном не нашлась"
    assert s_nomerom_v_imeni not in nomera, (
        "по номеру телефона нашлась карточка, у которой телефона нет вовсе — "
        "цифры ищутся по всей склейке вместо phone_norm"
    )


def test_udalyonnyy_klient_ne_nakhoditsya_poiskom(manager_client):
    """Карточка из корзины не приходит ни в список, ни в палитру.

    Отдельным тестом, потому что условие `deleted_at IS NULL` стоит теперь в
    ОДНОМ месте на оба пути — и на экран раздела, и на Ctrl+K. Одна снятая
    строка возвращает удалённых сразу в двух местах, и до этого теста её
    снятие не роняло ни одной проверки из полутора тысяч.

    Для человека это выглядит так: карточку убрали из системы, а поиск её
    продолжает предлагать — и по ней заводят новую заявку.
    """
    client_id = _client_id(manager_client, "Удлнн Тимур Корзинный")
    assert manager_client.get(SEARCH, params={"q": "Удлнн"}).json()["clients"]["items"]

    udalenie = manager_client.delete(f"{API}/clients/{client_id}")
    assert udalenie.status_code in (200, 204), udalenie.text

    posle = manager_client.get(SEARCH, params={"q": "Удлнн"}).json()
    assert client_id not in {item["id"] for item in posle["clients"]["items"]}

    v_spiske = manager_client.get(f"{API}/clients", params={"search": "Удлнн"}).json()
    assert client_id not in {item["id"] for item in v_spiske["items"]}


def test_telefon_nakhoditsya_i_tak_kak_ego_zapisali(manager_client):
    """Телефон ищется и по цифрам, и по написанию — значит `phone` в склейке.

    Цифровая ветка ходит в `phone_norm` и прикрывает поиск по одним цифрам.
    Поле `phone` — то, что человек ВИДИТ в карточке: с пробелами, скобками,
    добавочным номером словом. Выпади оно из склейки, замолчал бы поиск по
    БУКВАМ внутри телефона, а все прочие проверки остались бы зелёными: цифры-то
    находятся через `phone_norm`.

    Поэтому здесь ищется «доб» — в запросе нет ни одной цифры, значит цифровая
    ветка не срабатывает и отвечает ровно склейка. Первая версия этого теста
    искала «доб. 415» и была бесполезной: цифра 415 попадает в `phone_norm`, и
    карточка находилась даже с выброшенным из склейки полем.
    """
    otvet = manager_client.post(
        f"{API}/clients", json={"name": "Дбвчн Ольга", "phone": "+380 44 200 10 20 доб. 415"}
    )
    assert otvet.status_code == 201, otvet.text
    client_id = otvet.json()["id"]

    po_bukvam = manager_client.get(SEARCH, params={"q": "доб"}).json()
    assert client_id in {item["id"] for item in po_bukvam["clients"]["items"]}, (
        "телефон не нашёлся по слову внутри него — поле phone выпало из склейки"
    )

    # А цифрами — по-прежнему в любом написании, это вторая половина обещания.
    for zapros in ("44 200 10 20", "380442001020"):
        nayden = manager_client.get(SEARCH, params={"q": zapros}).json()
        assert client_id in {item["id"] for item in nayden["clients"]["items"]}, (
            f"телефон не нашёлся по запросу «{zapros}»"
        )


def test_kartochka_nakhoditsya_srazu_posle_zavedeniya_i_pravki(manager_client):
    """Склейка обязана обновляться сама — и при заведении, и при каждой правке.

    Колонка `search_text` — копия того, что лежит рядом, и её беда известна
    заранее по `phone_norm`: заполняет одна точка, а пишут карточку несколько,
    и запись мимо неё оставляет колонку пустой МОЛЧА. У поиска цена такой
    пустоты выше: карточка просто перестаёт находиться, и узнать об этом можно
    только от человека, который её искал.

    Поэтому проверяются оба конца: старое имя больше не находит (склейка не
    осталась прежней), новое находит (склейка пересобрана).
    """
    client_id = _client_id(manager_client, "Прсчт Первоначальный")
    deal = _deal(manager_client, title="Прсчт Первая работа", client_id=client_id)

    zavedeno = manager_client.get(SEARCH, params={"q": "Прсчт Первоначальный"}).json()
    assert client_id in {item["id"] for item in zavedeno["clients"]["items"]}
    assert deal["id"] in {item["id"] for item in zavedeno["deals"]["items"]}

    assert manager_client.patch(
        f"{API}/clients/{client_id}", json={"name": "Прсчт Переименованный"}
    ).status_code == 200
    assert manager_client.patch(
        f"{API}/deals/{deal['id']}", json={"title": "Прсчт Вторая работа"}
    ).status_code == 200

    po_novomu = manager_client.get(SEARCH, params={"q": "Переименованный"}).json()
    assert client_id in {item["id"] for item in po_novomu["clients"]["items"]}
    po_novoy_rabote = manager_client.get(SEARCH, params={"q": "Вторая работа"}).json()
    assert deal["id"] in {item["id"] for item in po_novoy_rabote["deals"]["items"]}

    po_staromu = manager_client.get(SEARCH, params={"q": "Первоначальный"}).json()
    assert client_id not in {item["id"] for item in po_staromu["clients"]["items"]}, (
        "карточка находится по имени, которого у неё больше нет: склейка не пересобрана"
    )
    po_staroy_rabote = manager_client.get(SEARCH, params={"q": "Первая работа"}).json()
    assert deal["id"] not in {item["id"] for item in po_staroy_rabote["deals"]["items"]}


# --- строка поиска — это строка, а не шаблон ---------------------------------


def test_podchyorkivanie_v_zaprose_ne_zamenyaet_lyuboy_simvol(manager_client):
    """`_` в LIKE — «любой один символ», и в строку поиска он попадает как есть.

    Общий слой это уже чинит (`database/query.contains`), но проверялось это
    только на клиентах и только мимо HTTP. Поиск же ходит в три репозитория
    сразу, и достаточно одному из них собрать условие своими руками, чтобы
    беда вернулась ровно в том месте, где её никто не ищет.
    """
    client_id = _client_id(manager_client, "Пдчрк Заказчик")
    exact = _deal(manager_client, title="Пдчрк_один", client_id=client_id)
    _deal(manager_client, title="ПдчркХодин", client_id=client_id)

    found = manager_client.get(SEARCH, params={"q": "Пдчрк_один"}).json()
    assert [item["id"] for item in found["deals"]["items"]] == [exact["id"]]
    assert found["deals"]["total"] == 1


def test_protsent_v_zaprose_ishchet_protsent_a_ne_vsyo(manager_client):
    """Один знак `%` возвращал всю таблицу — то есть выгрузку всего по нажатию.

    В палитре это заметно ещё меньше, чем в списке: строк на экране всё равно
    шесть, а «найдено 1240» читается как «поиск работает».
    """
    client_id = _client_id(manager_client, "Прцнт Заказчик")
    discount = _deal(manager_client, title="Прцнт Скидка 100% для своих", client_id=client_id)
    _deal(manager_client, title="Прцнт Обычная работа", client_id=client_id)

    everything = manager_client.get(SEARCH).json()
    assert everything["deals"]["total"] >= 2, "заявок в базе мало — проверка ничего не поймает"

    pattern = manager_client.get(SEARCH, params={"q": "%"}).json()
    assert pattern["deals"]["total"] < everything["deals"]["total"], (
        "поиск по одному «%» вернул все заявки"
    )
    assert pattern["clients"]["total"] < everything["clients"]["total"], (
        "поиск по одному «%» вернул всех клиентов"
    )

    literal = manager_client.get(SEARCH, params={"q": "100%"}).json()
    assert [item["id"] for item in literal["deals"]["items"]] == [discount["id"]]


def test_nomer_stranitsy_prikhodit_tolko_v_prodolzhenie(manager_client, db):
    """Листает выдачу продолжение группы, а общий поиск — нет. И это не мелочь.

    `page = 0` даёт `OFFSET -50`, а это для MySQL синтаксическая ошибка, то
    есть пятисотка. Сторожить номер страницы надо ровно там, где он приходит, —
    и потому важно, чтобы мест было одно.

    Общий поиск отдаёт по первой странице каждой группы разом и номера не
    берёт: один `page` на три группы означал бы страницу вторую у досок, у
    которых нашлось двадцать, и её же у клиентов, у которых нашлось два, — то
    есть пустоту вместо уже показанного. Продолжение (`/search/{area}`) спрашивают
    про ОДНУ группу, там номер и живёт, там он и сторожится (`ge=1`).

    Размер страницы снаружи не приходит вовсе — ни туда, ни туда. Его задаёт
    сервер, и клиент про него не знает: узнай — и первое же расхождение дало бы
    пропущенные или задвоенные находки, причём молча.
    """
    from web.api.routes import search as search_route

    obshchiy = inspect.signature(search_route.global_search).parameters
    assert "page" not in obshchiy
    assert "per_page" not in obshchiy

    prodolzhenie = inspect.signature(search_route.search_group).parameters
    assert "page" in prodolzhenie, (
        "у продолжения группы нет номера страницы — листать выдачу нечем, "
        "и палитра снова упрётся в первые строки"
    )
    assert "per_page" not in prodolzhenie, "размер страницы задаёт сервер, а не запрос"

    # Лишний параметр в адресе общего поиска ничего не меняет: FastAPI его не читает.
    with_page = manager_client.get(SEARCH, params={"q": "Прцнт", "page": 0})
    assert with_page.status_code == 200, with_page.text

    # А репозиторий, в который ходит поиск, нулевую страницу считает первой —
    # общим слоем `page_of`, а не своей арифметикой.
    rows, total = deals_repo.search(db, q="Прцнт", page=0, per_page=1)
    assert total >= 1 and len(rows) == 1


# --- выдача уважает права ----------------------------------------------------


def test_bez_prava_gruppa_ostayotsya_pustoy(role_maker, staff_maker, manager_client):
    """Поиск иначе становится обходом доступов.

    Через него видно название и клиента заявки из раздела, который сотруднику
    закрыт, — то есть ровно то, что раздел и закрывает.
    """
    client_id = _client_id(manager_client, "Бзпрв Заказчик")
    _deal(manager_client, title="Бзпрв Секретная работа", client_id=client_id)

    role = role_maker("Только задачи", ["tasks.view"])
    blind = staff_maker("nodeals@test.local", role["id"])

    found = blind.get(SEARCH, params={"q": "Бзпрв"})
    assert found.status_code == 200, found.text
    assert found.json()["deals"] == EMPTY
    assert found.json()["clients"] == EMPTY


def test_bez_prava_na_chuzhie_nakhodyatsya_tolko_svoi(role_maker, staff_maker, manager_client):
    """«Вижу только свои заявки» обязано держаться и в поиске.

    Это самое удобное место обойти ограничение: список показывает три карточки,
    а Ctrl+K по тому же слову находил бы все тридцать — вместе с именами чужих
    клиентов. Сужение приходит из прав, а не из запроса, поэтому снять его
    нечем.
    """
    client_id = _client_id(manager_client, "Сржм Заказчик")
    foreign = _deal(manager_client, title="Сржм Чужая работа", client_id=client_id)

    role = role_maker("Только свои заявки", ["deals.view", "deals.create"])
    narrow = staff_maker("ownonly@test.local", role["id"])
    mine = _deal(narrow, title="Сржм Своя работа", client_id=client_id)

    found = narrow.get(SEARCH, params={"q": "Сржм"}).json()
    ids = {item["id"] for item in found["deals"]["items"]}
    assert mine["id"] in ids, "сотрудник не находит собственную заявку"
    assert foreign["id"] not in ids, "поиск показал чужую заявку в обход deals.view_others"
    assert found["deals"]["total"] == 1, "счётчик выдал число, которого не видно в списке"

    # У того, чьё право шире, поиск работает как работал.
    seen = manager_client.get(SEARCH, params={"q": "Сржм"}).json()
    assert {foreign["id"], mine["id"]} <= {item["id"] for item in seen["deals"]["items"]}


def test_prodolzhenie_zakryto_temi_zhe_pravami_chto_i_pervaya_stranitsa(
    role_maker, staff_maker, manager_client
):
    """Второй вход в те же данные обязан быть закрыт так же, как первый.

    Иначе продолжение и есть обход доступов, только менее заметный: закрытый
    раздел не отдаёт первую страницу — и охотно отдаёт вторую. Проверка на
    первой странице этого не увидит никогда.
    """
    client_id = _client_id(manager_client, "Прдлпрв Заказчик")
    _deal(manager_client, title="Прдлпрв Секретная работа", client_id=client_id)

    role = role_maker("Только задачи, продолжение", ["tasks.view"])
    blind = staff_maker("nodealspaging@test.local", role["id"])

    for group in ("clients", "deals"):
        for stranitsa in (1, 2):
            otvet = blind.get(f"{SEARCH}/{group}", params={"q": "Прдлпрв", "page": stranitsa})
            assert otvet.status_code == 200, otvet.text
            assert otvet.json() == EMPTY, f"{group}, страница {stranitsa}: выдача не пуста"


def test_prodolzhenie_ne_pokazyvaet_chuzhie_zayavki(role_maker, staff_maker, manager_client):
    """«Вижу только свои» держится на КАЖДОЙ странице, а не только на первой.

    Сужение приходит из прав и прикладывается к запросу, а не к показанному.
    Приложи его лишь к первой странице — и достаточно было бы нажать «показать
    ещё», чтобы увидеть чужие заявки вместе с именами чужих клиентов. Поэтому
    здесь заводится больше записей, чем влезает в страницу, и листается всё до
    конца: беда, которую ищем, живёт со второй страницы.
    """
    client_id = _client_id(manager_client, "Прдлсвои Заказчик")
    chuzhie = {
        _deal(manager_client, title=f"Прдлсвои Работа Ч{n}", client_id=client_id)["id"]
        for n in range(GROUP_LIMIT + 1)
    }

    role = role_maker("Только свои, продолжение", ["deals.view", "deals.create"])
    narrow = staff_maker("ownonlypaging@test.local", role["id"])
    svoi = {
        _deal(narrow, title=f"Прдлсвои Работа С{n}", client_id=client_id)["id"]
        for n in range(GROUP_LIMIT + 1)
    }

    vsyo = {item["id"] for item in _dolistat(narrow, "deals", "Прдлсвои Работа")}
    assert vsyo == svoi, "сотрудник видит не ровно свои заявки"
    assert not (vsyo & chuzhie), (
        "продолжение выдачи показало чужие заявки — сужение приложено только к первой странице"
    )


def test_summa_ne_priezzhaet_bez_prava_na_summy(role_maker, staff_maker, manager_client):
    """Иначе цену работы можно прочесть из выдачи, не открывая заявку.

    Ключи при этом на месте и приходят пустыми: форма ответа не зависит от
    того, кто спрашивает, — то же правило, что у карточки заявки.
    """
    client_id = _client_id(manager_client, "Смма Заказчик")
    _deal(manager_client, title="Смма Дорогая работа", client_id=client_id, amount=500000)

    role = role_maker("Заявки без денег", ["deals.view", "deals.view_others"])
    poor = staff_maker("noamounts@test.local", role["id"])

    card = poor.get(SEARCH, params={"q": "Смма Дорогая"}).json()["deals"]["items"][0]
    assert card["title"] == "Смма Дорогая работа"
    assert card["amount"] is None, "сумма видна тому, у кого нет deals.view_amounts"

    rich = manager_client.get(SEARCH, params={"q": "Смма Дорогая"}).json()["deals"]["items"][0]
    assert rich["amount"] == 500000, "поиск перестал показывать сумму тому, кому положено"


# --- выключенный блок исчезает целиком ---------------------------------------


def test_vyklyuchennyy_blok_ukhodit_iz_vydachi(monkeypatch, manager_client):
    """Выключенный блок обязан пропадать и из поиска, а не только из меню.

    Проверяется подменой состояния, а не выключателем: заявки — несущий блок,
    выключить его нельзя (`module_is_core`). Проверять при этом нужно всё
    равно — гейт в поиске общий для всех групп, и заявки ходят через него
    наравне с досками. Сломайся он, у досок это заметил бы `test_modules.py`,
    но сломаться он может и после того, как заявки станут единственной группой,
    которую он пропускает.
    """
    client_id = _client_id(manager_client, "Вклбл Заказчик")
    _deal(manager_client, title="Вклбл Работа", client_id=client_id)
    assert manager_client.get(SEARCH, params={"q": "Вклбл"}).json()["deals"]["items"]

    # Подмену ставим после того, как запись заведена: при выключенном блоке её
    # не завести — заявки закрыты тем же гейтом, что и всё остальное.
    real = modules_service.is_enabled
    monkeypatch.setattr(
        modules_service,
        "is_enabled",
        lambda db, key: False if key == "deals" else real(db, key),
    )

    found = manager_client.get(SEARCH, params={"q": "Вклбл"})
    assert found.status_code == 200, found.text
    assert found.json()["deals"] == EMPTY
    assert found.json()["clients"]["items"], "клиенты — несущий блок, они обязаны находиться"


def test_gruppa_zakryta_blokom_ranshe_chem_pravom(root_client, manager_client):
    """Порядок проверок тот же, что везде: сначала блок, потом право.

    Досками это видно без подмен: блок выключается по-настоящему, а право на
    них у менеджера есть. Выключенный блок обязан забрать группу и у него.
    """
    assert root_client.post(f"{API}/modules/boards", json={"enabled": True}).status_code == 200
    client_id = _client_id(manager_client, "Прдк Заказчик")
    created = manager_client.post(
        f"{API}/boards", json={"title": "Прдк Доска", "client_id": client_id}
    )
    assert created.status_code == 201, created.text
    assert manager_client.get(SEARCH, params={"q": "Прдк"}).json()["boards"]["items"]

    try:
        assert root_client.post(f"{API}/modules/boards", json={"enabled": False}).status_code == 200
        found = manager_client.get(SEARCH, params={"q": "Прдк"}).json()
        assert found["boards"] == EMPTY
        # Соседние группы при этом не задеты: блок выключается без вреда для
        # остальных, и поиск — не исключение.
        assert found["clients"]["items"]

        # Продолжение группы — тот же гейт и в том же порядке. Выключенный блок
        # обязан забирать всю выдачу, а не только её первую страницу: иначе
        # «показать ещё» осталось бы дверью в раздел, которого нет ни в меню, ни
        # в маршрутах.
        for stranitsa in (1, 2):
            dalshe = manager_client.get(
                f"{SEARCH}/boards", params={"q": "Прдк", "page": stranitsa}
            )
            assert dalshe.status_code == 200, dalshe.text
            assert dalshe.json() == EMPTY, f"страница {stranitsa} выключенного блока не пуста"
    finally:
        root_client.post(f"{API}/modules/boards", json={"enabled": True})
