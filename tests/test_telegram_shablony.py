"""Шаблоны в мессенджере и заявка из разговора: две кнопки, которыми пользуются.

Файл заведён по двум жалобам владельца — «шаблоны как будто совсем не рабочие»
и «а кнопка „завести заявку“ вообще работает?». Обе про одно и то же место:
подпись под перепиской, где стоят выбор шаблона и кнопки шапки.

**Что здесь стережётся.** Путь шаблона в мессенджере состоит из четырёх звеньев,
и рвётся он молча в каждом:

1. справочник (`GET /templates`) — им заполняется выпадающий список, и экран
   ждёт от него поле `name`;
2. подстановка (`GET /templates/{id}/render`) — она отдаёт готовый текст, и она
   же обязана отвечать, когда диалог ни с кем не связан: непривязанный разговор
   это обычное состояние, а не поломка;
3. право и блок — закрытый справочник обязан отвечать понятным кодом, а не
   пятисоткой, иначе экрану нечего сказать человеку;
4. заявка из разговора (`POST /telegram/chats/{id}/deal`) — она обязана
   ЗАВЕСТИСЬ и обязана достаться тому клиенту, чей это разговор.

Пятое звено — сам экран, и оно проверяется чтением исходника, как в
`tests/test_screens.py`: пустой выбор без объяснения неотличим от сломанного, а
кнопка, которая всегда получает отказ, неотличима от неработающей.

Номера телеграм-чатов взяты от 540000: в `tests/test_telegram.py` занято до
520510, а `chat_id` уникален на всю таблицу, и общий номер означал бы, что
проверки молча портят друг друга (разбор — в
`test_nomera_chatov_u_proverok_ne_peresekayutsya`).
"""

import pathlib
import re

import pytest

from core.services import modules_service
from tests.conftest import API, make_manager

TG = f"{API}/telegram"
TEMPLATES = f"{API}/templates"
MODULES = f"{API}/modules"
ROLES = f"{API}/roles"
WEBHOOK = f"{API}/telegram/webhook"

SCREENS = pathlib.Path(__file__).resolve().parent.parent / "web" / "frontend" / "crm" / "src"

#: Тот же образец токена, что в `tests/test_telegram.py`: бот должен выглядеть
#: настроенным, иначе приём отвечает отказом ещё до разбора обновления.
OBRAZETS_TOKENA = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


# --- подготовка --------------------------------------------------------------


@pytest.fixture()
def bloki_vklyucheny(root_client):
    """Мессенджер и шаблоны включены на время проверок.

    Оба возвращаются на место в фикстуре, а не в конце теста: тело до конца
    может и не дойти, а выключенный блок посыпал бы посторонние файлы набора.
    Телеграм по умолчанию выключен, шаблоны включены — поэтому и гасим обратно
    по-разному.
    """
    modules_service.invalidate()
    for kluch in ("templates", "telegram"):
        otvet = root_client.post(f"{MODULES}/{kluch}", json={"enabled": True})
        assert otvet.status_code == 200, otvet.text
    yield
    modules_service.invalidate()
    root_client.post(f"{MODULES}/templates", json={"enabled": True})
    root_client.delete(f"{TG}/settings")
    root_client.post(f"{MODULES}/telegram", json={"enabled": False})


@pytest.fixture()
def bot_nastroen(root_client, bloki_vklyucheny):
    """Бот настроен — приём готов принимать. Возвращает секрет вебхука."""
    root_client.put(f"{TG}/settings", json={"token": OBRAZETS_TOKENA})
    from core.services import telegram_service
    from database.session import SessionLocal

    with SessionLocal() as db:
        return telegram_service.webhook_secret(db)


@pytest.fixture()
def shablon(root_client, bloki_vklyucheny):
    """Шаблон с подстановкой имени клиента. Убирается за собой: имя уникально."""
    otvet = root_client.post(
        TEMPLATES,
        json={"name": "Мессенджер ТЕСТ", "body": "Здравствуйте, {client_name}! Мы получили заявку."},
    )
    assert otvet.status_code == 201, otvet.text
    yield otvet.json()
    root_client.delete(f"{TEMPLATES}/{otvet.json()['id']}")


def _obnovlenie(chat_id: int, message_id: int, **pole) -> dict:
    """Обновление в том виде, в каком его шлёт телеграм."""
    return {
        "update_id": message_id * 10,
        "message": {
            "message_id": message_id,
            "date": 1786000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Пётр", "username": "petr"},
            **pole,
        },
    }


def _poslat(root_client, sekret: str, telo: dict):
    return root_client.post(
        WEBHOOK, json=telo, headers={"X-Telegram-Bot-Api-Secret-Token": sekret}
    )


def _dialog(root_client, chat_id: int) -> dict:
    nashli = [d for d in root_client.get(f"{TG}/chats").json()["items"] if d["chat_id"] == chat_id]
    assert len(nashli) == 1, f"диалогов с chat_id={chat_id} оказалось {len(nashli)}"
    return nashli[0]


def _razgovor(root_client, sekret: str, chat_id: int, *slova: str) -> dict:
    """Диалог, в котором клиент уже что-то сказал."""
    for nomer, slovo in enumerate(slova, start=1):
        otvet = _poslat(root_client, sekret, _obnovlenie(chat_id, nomer, text=slovo))
        assert otvet.status_code == 200, otvet.text
    return _dialog(root_client, chat_id)


# --- справочник шаблонов -----------------------------------------------------


def test_spravochnik_otdayot_shablony_po_tomu_zhe_adresu_chto_sprashivaet_ekran(
    root_client, shablon
):
    """Выпадающий список мессенджера заполняется ровно этим ответом.

    Экран зовёт `/templates?per_page=100` и берёт из каждой записи `id` и
    `name`. Ни того, ни другого проверить глазами нельзя: пустой выбор
    выглядит одинаково и когда справочник не ответил, и когда поле в ответе
    называется иначе. Поэтому и адрес, и оба поля закреплены здесь.

    `per_page` ручка не знает и знать не обязана — она отдаёт всё. Проверка
    стережёт обратное: появись у списка страницы, и мессенджер тихо показал бы
    первую, а остальные шаблоны исчезли бы из выбора.
    """
    otvet = root_client.get(f"{TEMPLATES}?per_page=100")
    assert otvet.status_code == 200, otvet.text

    nash = [s for s in otvet.json()["items"] if s["id"] == shablon["id"]]
    assert len(nash) == 1, f"шаблона нет в справочнике: {otvet.text}"
    assert nash[0]["name"] == "Мессенджер ТЕСТ", "выбор рисуется по полю name"


def test_podstanovka_otdayot_gotovyy_tekst_s_imenem_klienta(root_client, bot_nastroen, shablon):
    """Выбрали шаблон — в поле ввода ложится текст, а не имя шаблона.

    Это и есть работа кнопки: `render` подставляет клиента ПРИВЯЗАННОГО диалога.
    Отдай ручка тело шаблона как есть, менеджер отправил бы клиенту
    «Здравствуйте, {client_name}!» — и увидел бы это только клиент.
    """
    klient = root_client.post(f"{API}/clients", json={"name": "Тарас Шаблонов"}).json()
    dialog = _razgovor(root_client, bot_nastroen, 540100, "здравствуйте")
    assert (
        root_client.patch(f"{TG}/chats/{dialog['id']}", json={"client_id": klient["id"]}).status_code
        == 200
    )

    otvet = root_client.get(f"{TEMPLATES}/{shablon['id']}/render?client_id={klient['id']}")
    assert otvet.status_code == 200, otvet.text
    telo = otvet.json()
    assert telo["text"] == "Здравствуйте, Тарас Шаблонов! Мы получили заявку."
    assert telo["missing"] == [], f"заполненное поле попало в пропавшие: {telo}"


def test_podstanovka_bez_privyazannogo_klienta_ne_padaet_a_nazyvaet_probel(
    root_client, shablon
):
    """Диалог ни с кем не связан — подстановка всё равно отвечает.

    Непривязанный разговор — обычное состояние мессенджера: человек написал
    боту, кто он — ещё неизвестно. Откажи `render` без клиента, и выбор шаблона
    в таком диалоге просто ругался бы, а менеджер решил бы, что шаблоны
    сломаны.

    Второе здесь важнее первого: пустое место становится ВИДИМЫМ прочерком, а
    `missing` называет поле поимённо. Экран обязан на это смотреть — иначе в
    телеграм уедет «Здравствуйте, —!», и заметит это клиент.
    """
    otvet = root_client.get(f"{TEMPLATES}/{shablon['id']}/render")
    assert otvet.status_code == 200, otvet.text
    telo = otvet.json()
    assert telo["text"] == "Здравствуйте, —! Мы получили заявку."
    assert telo["missing"] == ["client_name"], f"пропавшее поле не названо: {telo}"


def test_shablony_chitaet_tot_kto_pishet_klientam_a_ne_tolko_vladelets(
    root_client, bot_nastroen, shablon
):
    """Менеджеру с правом на переписку шаблоны видны и подставляются.

    Ради этого `render` сделан `GET`'ом и закрыт правом `templates.view`, а не
    `templates.edit`: шаблоны читают все, кто пишет клиентам, а правит их тот,
    кому дано право. Съедь право на правку — и выбор шаблона в мессенджере
    отвечал бы отказом всем, кроме владельца, то есть был бы «нерабочим» ровно
    так, как о нём и сказали.
    """
    role = root_client.post(
        ROLES,
        json={
            "name": "Пишет клиентам ТЕСТ",
            "permissions": ["telegram.view", "telegram.create", "templates.view", "clients.view"],
        },
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]
    pishushchiy = make_manager(root_client, "tg-shablony@test.local")
    user_id = next(
        u["id"]
        for u in root_client.get(f"{API}/staff").json()["items"]
        if u["email"] == "tg-shablony@test.local"
    )
    assert (
        root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": role_id}).status_code == 200
    )

    try:
        spisok = pishushchiy.get(f"{TEMPLATES}?per_page=100")
        assert spisok.status_code == 200, spisok.text
        assert any(s["id"] == shablon["id"] for s in spisok.json()["items"])

        gotovo = pishushchiy.get(f"{TEMPLATES}/{shablon['id']}/render")
        assert gotovo.status_code == 200, gotovo.text
        assert "{client_name}" not in gotovo.json()["text"]
    finally:
        root_client.delete(f"{API}/staff/{user_id}")
        root_client.delete(f"{ROLES}/{role_id}")


def test_vyklyuchennyy_blok_shablonov_govorit_chto_on_vyklyuchen(root_client, shablon):
    """Блок шаблонов выключен — справочник отвечает кодом, а не пятисоткой.

    Разница не косметическая. `module_disabled` означает «раздела нет», и выбор
    шаблонов в мессенджере обязан исчезнуть целиком — как исчезает всё
    выключенное. Показать вместо этого «не удалось загрузить» значит соврать:
    ничего не ломалось, и повторять нечего.
    """
    assert (
        root_client.post(f"{MODULES}/templates", json={"enabled": False}).status_code == 200
    )
    modules_service.invalidate()
    try:
        spisok = root_client.get(f"{TEMPLATES}?per_page=100")
        assert spisok.status_code == 403, spisok.text
        assert spisok.json()["error"]["code"] == "module_disabled"

        podstanovka = root_client.get(f"{TEMPLATES}/{shablon['id']}/render")
        assert podstanovka.status_code == 403, podstanovka.text
        assert podstanovka.json()["error"]["code"] == "module_disabled"
    finally:
        root_client.post(f"{MODULES}/templates", json={"enabled": True})
        modules_service.invalidate()


# --- заявка из разговора -----------------------------------------------------


def test_zayavka_iz_dialoga_zavoditsya_i_dostayotsya_svoemu_klientu(root_client, bot_nastroen):
    """Нажали «завести заявку» — заявка есть, и она у этого клиента.

    Ради этого мессенджер и внутри CRM. Проверяется не ответ ручки, а САМА
    заявка: ручка могла бы вернуть 201 и не записать ничего, и снаружи это
    выглядело бы работающей кнопкой ровно до того дня, когда заявку станут
    искать.
    """
    klient = root_client.post(f"{API}/clients", json={"name": "Клиент из разговора"}).json()
    dialog = _razgovor(root_client, bot_nastroen, 540200, "почините ноутбук")
    assert (
        root_client.patch(f"{TG}/chats/{dialog['id']}", json={"client_id": klient["id"]}).status_code
        == 200
    )

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/deal", json={})
    assert otvet.status_code == 201, otvet.text
    zavedeno = otvet.json()
    assert zavedeno["client_id"] == klient["id"]

    kartochka = root_client.get(f"{API}/deals/{zavedeno['id']}")
    assert kartochka.status_code == 200, kartochka.text
    assert kartochka.json()["client_id"] == klient["id"], "заявка досталась не тому клиенту"


def test_zayavka_nazyvaetsya_poslednimi_slovami_klienta(root_client, bot_nastroen):
    """Название берётся из последнего входящего, а не из первого «здравствуйте».

    Девять раз из десяти заявка называется тем, с чего клиент начал разговор по
    делу. Возьми она первое сообщение подряд — все заявки конторы назывались бы
    «привет», и найти нужную по названию стало бы нельзя.
    """
    klient = root_client.post(f"{API}/clients", json={"name": "Клиент со словами"}).json()
    dialog = _razgovor(root_client, bot_nastroen, 540300, "здравствуйте", "нужен ремонт витрины")
    root_client.patch(f"{TG}/chats/{dialog['id']}", json={"client_id": klient["id"]})

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/deal", json={})
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["title"] == "нужен ремонт витрины", otvet.text


def test_zayavka_iz_neprivyazannogo_dialoga_otkazyvaet_svoim_kodom(root_client, bot_nastroen):
    """Диалог ни с кем не связан — отказ с кодом, а не заявка ничьему клиенту.

    Код нужен экрану: по нему кнопка объясняет, чего не хватает, и указывает на
    выбор клиента рядом. Без кода экрану остаётся только показать чужую фразу
    по-английски, а человеку — гадать, сломана кнопка или он что-то не сделал.
    """
    dialog = _razgovor(root_client, bot_nastroen, 540400, "а сколько стоит?")
    assert dialog["client_id"] is None, "диалог неожиданно оказался привязан"

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/deal", json={})
    assert otvet.status_code == 422, otvet.text
    assert otvet.json()["error"]["code"] == "telegram_chat_not_linked"


def test_napominanie_zavoditsya_i_bez_privyazki(root_client, bot_nastroen):
    """Соседняя кнопка шапки: «перезвонить этому человеку» осмысленно и до того,
    как выяснили, кто он.

    Проверка стоит рядом нарочно: обе кнопки шапки живут в одном месте экрана, и
    одинаковый запрет на них был бы соблазнительно «единообразным» — и
    неправильным.
    """
    dialog = _razgovor(root_client, bot_nastroen, 540500, "перезвоните мне")

    otvet = root_client.post(f"{TG}/chats/{dialog['id']}/task", json={})
    assert otvet.status_code == 201, otvet.text
    assert otvet.json()["title"], "напоминание завелось без названия"


def test_zayavku_iz_razgovora_zavodit_tot_komu_dano_pravo_na_zayavki(root_client, bot_nastroen):
    """Право спрашивается на ЗАЯВКИ, а не на переписку.

    Иначе право писать клиентам тихо давало бы право заводить работу. Кнопка на
    экране обязана стоять за тем же правом — не спрятанная, она отвечала бы
    отказом при каждом нажатии, то есть выглядела бы сломанной.
    """
    role = root_client.post(
        ROLES,
        json={
            "name": "Только переписка ТЕСТ",
            "permissions": ["telegram.view", "telegram.create", "clients.view"],
        },
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]
    tolko_perepiska = make_manager(root_client, "tg-bez-zayavok@test.local")
    user_id = next(
        u["id"]
        for u in root_client.get(f"{API}/staff").json()["items"]
        if u["email"] == "tg-bez-zayavok@test.local"
    )
    assert (
        root_client.post(f"{ROLES}/assign/{user_id}", json={"role_id": role_id}).status_code == 200
    )

    try:
        dialog = _razgovor(root_client, bot_nastroen, 540600, "заведите заявку")
        otkaz = tolko_perepiska.post(f"{TG}/chats/{dialog['id']}/deal", json={})
        assert otkaz.status_code == 403, otkaz.text
        assert otkaz.json()["error"]["code"] == "permission_denied"
    finally:
        root_client.delete(f"{API}/staff/{user_id}")
        root_client.delete(f"{ROLES}/{role_id}")


# --- сам экран ---------------------------------------------------------------
#
# Ниже — чтение исходника, как в `tests/test_screens.py`: собранного фронтенда
# в наборе нет, а правила простые и проверяются чтением. Каждое написано на
# беду, из-за которой этот файл и появился.


def _telegram_tsx() -> str:
    return (SCREENS / "screens" / "Telegram.tsx").read_text(encoding="utf-8")


def test_vybor_shablonov_ne_propadaet_ottogo_chto_shablonov_poka_net():
    """Шаблонов ноль — выбор всё равно на месте и говорит, что он пуст.

    Это и есть первая жалоба владельца. Выбор стоял за условием «список не
    пуст», поэтому на свежей системе, где шаблонов ещё не завели, никакого
    выбора в подписи не было ВОВСЕ. Со стороны это неотличимо от сломанной
    кнопки: человек ищет шаблоны там, где их обещали, и не находит даже места,
    где они должны быть.

    Пустой выбор без объяснения ничем не лучше: «шаблонов нет» и «справочник не
    приехал» обязаны выглядеть по-разному, иначе экран отвечает на вопрос,
    которого ему не задавали.
    """
    text = _telegram_tsx()
    assert "tg-templates" in text, "проверка смотрит не туда: выбора шаблонов на экране нет"

    assert "(shablony.items ?? []).length > 0 ||" not in text, (
        "выбор шаблонов спрятан за «список не пуст»: на системе, где шаблонов "
        "ещё не завели, в подписи не будет вовсе ничего"
    )
    assert "tgNoTemplates" in text, (
        "пустой выбор ничего не объясняет — «шаблонов нет» неотличимо от «не загрузилось»"
    )
    assert "shablony.failure" in text, "отказ справочника шаблонов экран замалчивает"


def test_vyklyuchennyy_blok_shablonov_ne_sprashivaetsya_vovse():
    """Блок выключен или права нет — справочник не спрашивается.

    `useReference(null)` для того и заведён: «спрашивать нечего» это не отказ, и
    строки «не удалось загрузить» быть не должно. Спроси экран всё равно, и
    выключенный блок шаблонов выглядел бы в мессенджере поломкой сервера —
    ровно та жалоба, с которой файл начался.
    """
    text = _telegram_tsx()
    okno = text[text.index("const shablony = useReference") : ][:400]
    assert "? null" in okno or ": null" in okno, (
        "справочник шаблонов спрашивается безусловно: при выключенном блоке "
        "экран покажет отказ вместо того, чтобы убрать выбор"
    )
    assert 'moduleOn(modules, "templates")' in text, "блок шаблонов экраном не проверяется"
    assert 'can(user, "templates.view")' in text, "право на шаблоны экраном не проверяется"


def test_podstanovka_govorit_o_polyah_kotorye_nechem_zapolnit():
    """Шаблон подставился с прочерками — экран об этом сказал.

    `render` возвращает `missing` поимённо, и молчать о нём нельзя: непривязанный
    диалог даёт «Здравствуйте, —!», и без предупреждения это уезжает клиенту.
    Отправку при этом не запрещаем — прочерк бывает и уместен, решает человек.
    """
    text = _telegram_tsx()
    assert "missing" in text, "ответ подстановки читается только полем text — пропуски замалчиваются"
    assert "tgTemplateGaps" in text, "о пропусках в шаблоне человеку не сказано"


def test_zavesti_zayavku_stoit_za_pravom_i_za_privyazkoy():
    """Кнопка не предлагает того, что сервер откажется делать.

    Две причины отказа, и обе известны экрану ЗАРАНЕЕ: нет права `deals.create`
    и диалог не привязан к карточке. Кнопка, которая на каждое нажатие отвечает
    чужой английской фразой, — это и есть «не работает» с точки зрения того, кто
    её нажимает.

    Напоминание при этом остаётся доступным всегда: оно и на сервере не требует
    привязки.
    """
    text = _telegram_tsx()
    assert 'can(user, "deals.create")' in text, "«завести заявку» показывается без права на заявки"
    assert 'can(user, "tasks.create")' in text, "«напомнить» показывается без права на задачи"
    assert "tgDealNeedsClient" in text, (
        "у непривязанного диалога кнопка молчит о том, чего ей не хватает"
    )

    # Засов на месте: два нажатия в одном тике завели бы две заявки.
    zavesti = text[text.index("const zavesti = async") :][:900]
    assert re.search(r"\w*[Gg]uard\.take\(\)", zavesti), "«завести заявку» осталась без засова"


def test_novye_stroki_ekrana_est_v_oboikh_yazykakh():
    """Подпись, которой нет в словаре, показывается ключом.

    Общий страж словаря (`test_screens.py`) следит за обратным — что в словаре
    нет лишнего. Здесь проверяется то, чего он не видит: строки, добавленной
    только в один язык, на другом языке не будет, и человек увидит
    `tgNoTemplates` посреди подписи.
    """
    slovar = (SCREENS / "lib" / "i18n.ts").read_text(encoding="utf-8")
    for kluch in ("tgNoTemplates", "tgTemplateGaps", "tgDealNeedsClient"):
        assert slovar.count(f"{kluch}:") == 2, (
            f"строка {kluch} объявлена не в обоих языках: найдено "
            f"{slovar.count(f'{kluch}:')} раз(а)"
        )
