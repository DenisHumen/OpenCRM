"""Разделы настроек: у каждого есть место в меню, и блок уносит их вместе с собой.

Настройки разложены по категориям (заказ владельца 02.09.2026): плоский список
из четырнадцати пунктов не отвечал на вопрос «где искать». Разложить их —
работа разовая, а вот **потерять раздел при следующей правке — постоянная**:
маршрут заводится в `App.tsx`, пункт меню в `Sidebar.tsx`, и оба места правят
порознь.

Две беды, обе молчаливые:

- **раздел без пункта меню.** Экран есть, адрес работает, а дойти до него можно
  только по прямой ссылке. Так уже было с настройками наклейки: ключи в базе
  лежали с самого начала, экрана не было вовсе, и размер рулона задать было
  нечем;
- **пункт спрятан, а адрес жив.** Выключили доски — из меню ушло, а
  `/settings/showcase` открывался по закладке, и настроить можно было оформление
  того, чего в системе нет. Это не защита, а её видимость: сторож требует
  `ModuleRoute` на маршруте, а не только признак блока у пункта.

Проверка читает исходники обоих файлов. Разбор строковый: имена собираются
выражением только у подписей, а адреса и признаки блоков пишутся буквами.
"""

import pathlib
import re

KOREN = pathlib.Path(__file__).resolve().parent.parent
ISTOCHNIK = KOREN / "web" / "frontend" / "crm" / "src"
MARSHRUTY = ISTOCHNIK / "App.tsx"
MENYU = ISTOCHNIK / "components" / "Sidebar.tsx"

#: Начало общего раздела настроек: у детей этого маршрута путь записан без
#: приставки (`brand`, а не `/settings/brand`).
OBSHCHIY = '<Route path="/settings" element={<SettingsLayout />}>'

#: Адреса настроек, которым пункт меню не полагается, — с доводом.
BEZ_PUNKTA: dict[str, str] = {
    "/settings": "перенаправление на первый раздел, своего экрана нет",
}


def _blok_obshchego(tekst: str) -> str:
    """Кусок разметки от `<Route path="/settings" …>` до его закрытия.

    Границу берём по ОТСТУПУ, а не счётом угловых скобок. Счёт ломается о саму
    разметку: в `<Route path="/settings" element={<SettingsLayout />}>` первая
    же закрывающая скобка принадлежит вложенному `<SettingsLayout />`, и тег
    выглядит самозакрывающимся. Отступ в этом файле выдержан, а закрытие своего
    уровня — ровно то, что нужно.
    """
    stroki = tekst.splitlines()
    nachalo = next(i for i, s in enumerate(stroki) if OBSHCHIY in s)
    otstup = len(stroki[nachalo]) - len(stroki[nachalo].lstrip())
    for j in range(nachalo + 1, len(stroki)):
        s = stroki[j]
        if s.strip() == "</Route>" and len(s) - len(s.lstrip()) == otstup:
            return chr(10).join(stroki[nachalo : j + 1])
    raise AssertionError("не нашёл закрытие общего раздела настроек")


def adresa_marshrutov() -> set[str]:
    """Все адреса настроек, до которых можно дойти по ссылке."""
    tekst = MARSHRUTY.read_text(encoding="utf-8")
    svoi = set(re.findall(r'<Route path="(/settings/[a-z-]+)"', tekst))
    obshchiy = _blok_obshchego(tekst)
    deti = {
        f"/settings/{put}"
        for put in re.findall(r'<Route path="([a-z-]+)" element=', obshchiy)
    }
    return svoi | deti


def adresa_menyu() -> list[str]:
    """Адреса настроек из меню — списком, чтобы увидеть повтор."""
    tekst = MENYU.read_text(encoding="utf-8")
    nachalo = tekst.index("const settingsItems: NavEntry[] = [")
    konets = tekst.index("\n  ];", nachalo)
    return re.findall(r'to: "(/settings/[a-z-]+)"', tekst[nachalo:konets])


def test_perebor_nakhodit_razdely():
    """Сторож, ничего не нашедший, зеленеет на любой беде."""
    assert len(adresa_marshrutov()) > 8, "маршруты настроек не разобрались"
    assert len(adresa_menyu()) > 8, "пункты меню не разобрались"


def test_kazhdyy_razdel_nastroek_stoit_v_menyu_rovno_odin_raz():
    marshruty = adresa_marshrutov() - set(BEZ_PUNKTA)
    menyu = adresa_menyu()

    poteryany = sorted(marshruty - set(menyu))
    assert not poteryany, (
        "у раздела настроек нет пункта меню: "
        + ", ".join(poteryany)
        + ". Экран есть, адрес работает, а дойти до него можно только по прямой "
        "ссылке — либо завести пункт, либо назвать адрес в `BEZ_PUNKTA` с доводом"
    )

    lishnie = sorted(set(menyu) - marshruty)
    assert not lishnie, (
        "пункт меню ведёт в никуда: " + ", ".join(lishnie) + " — маршрута нет"
    )

    dvazhdy = sorted({put for put in menyu if menyu.count(put) > 1})
    assert not dvazhdy, (
        "раздел стоит в двух категориях сразу: "
        + ", ".join(dvazhdy)
        + ". Категория обязана быть одна, иначе один экран ищут в двух местах"
    )


#: Раздел настроек → блок, вместе с которым он обязан исчезать.
#:
#: Только те, у кого блок есть. Бренд, контакты и обслуживание не привязаны ни к
#: какому блоку и привязаны быть не могут: фирма называется как-то и без досок.
POD_BLOKOM: dict[str, str] = {
    "/settings/showcase": "boards",
    "/settings/return-button": "boards",
    "/settings/labels": "labels",
    "/settings/warehouses": "warehouse",
    "/settings/finance": "finance",
    "/settings/mailboxes": "mail",
    "/settings/telephony": "telephony",
    "/settings/telegram": "telegram",
}


def _blok_marshruta(tekst: str, adres: str) -> str | None:
    """Какой блок закрывает маршрут: имя из ближайшего охватывающего `ModuleRoute`.

    Ищем от адреса ВВЕРХ по тексту: обёртка пишется перед вложенным маршрутом, а
    ближайшая и есть та, что его закрывает.
    """
    hvost = adres.rsplit("/", 1)[-1]
    mesto = tekst.find(f'<Route path="{adres}"')
    if mesto == -1:
        mesto = tekst.find(f'<Route path="{hvost}" element=')
    if mesto == -1:
        return None
    obyortki = list(re.finditer(r'<ModuleRoute module="(\w+)" />', tekst[:mesto]))
    if not obyortki:
        return None
    # Обёртка считается охватывающей, если между ней и маршрутом её не закрыли.
    posle = tekst[obyortki[-1].end() : mesto]
    return None if "</Route>" in posle else obyortki[-1].group(1)


def test_vyklyuchennyy_blok_unosit_svoi_nastroyki():
    marshruty = MARSHRUTY.read_text(encoding="utf-8")
    menyu = MENYU.read_text(encoding="utf-8")
    bedy = []
    for adres, blok in POD_BLOKOM.items():
        nashli = _blok_marshruta(marshruty, adres)
        if nashli != blok:
            bedy.append(
                f"{adres}: маршрут закрыт блоком {nashli!r}, а ожидался {blok!r}"
            )
        # У пункта меню признак блока стоит рядом с адресом.
        okno = menyu[max(0, menyu.find(f'to: "{adres}"') - 220) : menyu.find(f'to: "{adres}"')]
        if f'module: "{blok}"' not in okno:
            bedy.append(f"{adres}: у пункта меню нет признака блока {blok!r}")
    assert not bedy, (
        "выключенный блок обязан уносить свои настройки целиком:\n  "
        + "\n  ".join(bedy)
        + "\nСпрятанный пункт при живом адресе — это не защита, а её видимость"
    )


#: Охранники маршрута: они стоят МЕЖДУ каркасом и экраном.
OHRANNIKI = ("ModuleRoute", "PermRoute")


def test_okhrannik_marshruta_ne_syedaet_kontekst_karkasa():
    """Сторож между каркасом и экраном обязан пропускать контекст насквозь.

    `useOutletContext` берёт значение у БЛИЖАЙШЕГО `Outlet`. Поставили охранника
    внутрь каркаса — ближайшим становится его собственный `Outlet`, и экран
    получает `undefined` вместо общего состояния раздела.

    Беда не выдуманная и чтением не находится: разделы настроек, закрытые блоком
    `boards`, падали на `Cannot destructure property 'values'` — белый экран
    вместо страницы, без единой красной проверки. Нашлось живой пробой.
    """
    tekst = MARSHRUTY.read_text(encoding="utf-8")
    bedy = []
    for imya in OHRANNIKI:
        nachalo = tekst.find(f"function {imya}(")
        assert nachalo != -1, f"охранника {imya} нет вовсе — перебор смотрит в пустоту"
        telo = tekst[nachalo : tekst.index(chr(10) + "}", nachalo)]
        if "<Outlet context=" not in telo:
            bedy.append(
                f"{imya} отдаёт голый <Outlet />: контекст каркаса, внутри которого "
                f"он стоит, до экрана не доедет"
            )
    assert not bedy, (chr(10) + "  ").join(bedy)
