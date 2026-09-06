"""Каждый блок системы описан в руководстве.

**Зачем сторож.** Руководство читают вместо того, чтобы спрашивать. Блок,
которого в нём нет, для читателя не существует — а узнать о нём он может только
случайно, наткнувшись на пункт меню.

Так уже было, и не раз: **весь блок накладных отсутствовал** и в таблице «что
умеет» `01-obzor.md`, и в плане работ, и в справочнике API — двенадцать ручек
целиком. Каждый раз это находилось сплошной сверкой с кодом, а не чтением.

**Источник полноты — реестр блоков, а не документы.** `core/modules.py` — это
единственное место, где блок объявляется; таблицы в `docs/` от него отстают,
что и проверено 02.09.2026: две из них говорили «шестнадцать» при семнадцати.

Проверка сверяет ИМЕНА, а не содержание: судить, хорошо ли написана статья, она
не умеет. Её работа — не дать блоку остаться неописанным вовсе.
"""

import pathlib
import re

from core import modules

KOREN = pathlib.Path(__file__).resolve().parent.parent
RUKOVODSTVO = KOREN / "web" / "frontend" / "crm" / "src" / "lib" / "rukovodstvo.ts"

#: Блоки, которым статьи не полагается, — с доводом.
#:
#: Повод законный один: блок не даёт пользователю НИЧЕГО, что он делал бы сам.
#: Список закрытый; «пока не написали» доводом не является.
BEZ_STATYI: dict[str, str] = {}


def _tekst() -> str:
    return RUKOVODSTVO.read_text(encoding="utf-8")


def opisannye_bloki() -> set[str]:
    """Блоки, названные признаком видимости у раздела или статьи.

    Признак — не украшение: по нему статья прячется у того, кто блок выключил
    (`Docs.tsx`, тот же `allowed`, что у меню). Значит «блок описан» и «блок
    назван признаком» — одно и то же, и второе проверяемо.
    """
    return set(re.findall(r'module:\s*"(\w+)"', _tekst()))


def test_perebor_ne_pustoy():
    """Сторож, ничего не нашедший, зеленеет на любой беде."""
    assert len(modules.MODULES) > 10, "реестр блоков не собрался"
    assert len(opisannye_bloki()) > 5, (
        "в руководстве не нашлось признаков блока — сменился способ их писать, "
        "и проверка ниже объявила бы неописанным всё подряд"
    )


def test_kazhdyy_blok_opisan_v_rukovodstve():
    nuzhno = {m.key for m in modules.MODULES} - set(BEZ_STATYI)
    est = opisannye_bloki()
    net = sorted(nuzhno - est)
    assert not net, (
        "блок есть в системе, а в руководстве про него ни слова: "
        + ", ".join(net)
        + ". Для читателя это значит, что блока не существует. Либо статья, "
        "либо строка в `BEZ_STATYI` с доводом"
    )

    chuzhie = sorted(est - {m.key for m in modules.MODULES})
    assert not chuzhie, (
        "руководство прячет статью за несуществующим блоком: "
        + ", ".join(chuzhie)
        + ". Такая статья не покажется никому и никогда"
    )


def test_prava_v_priznakakh_sushchestvuyut():
    """Статья, спрятанная за несуществующим правом, не покажется никому."""
    from core import permissions

    nayd = set(re.findall(r'perm:\s*"([a-z_]+\.[a-z_]+)"', _tekst()))
    plohie = sorted(
        kod for kod in nayd if not permissions.exists(*kod.split(".", 1))
    )
    assert not plohie, (
        "в руководстве названо несуществующее право: "
        + ", ".join(plohie)
        + ". Статья за ним не покажется ни одному читателю"
    )


def test_u_kazhdoy_statyi_est_oba_yazyka():
    """Недописанный английский увидят раньше русского: он язык по умолчанию."""
    tekst = _tekst()
    # Пары `ru:`/`en:` идут рядом по всему файлу; считаем их и сверяем.
    ru = len(re.findall(r'\bru:\s*"', tekst))
    en = len(re.findall(r'\ben:\s*"', tekst))
    assert ru == en, (
        f"двуязычие разъехалось: русских строк {ru}, английских {en}. "
        "Английский — язык продукта по умолчанию, и пустоту в нём увидят первой"
    )


def test_znachki_razdelov_sushchestvuyut():
    """Несуществующий значок не рисуется НИЧЕМ и не роняет ничего.

    Раздел остаётся с пустым местом слева от подписи, и узнают об этом от
    человека, который открыл руководство. Так и вышло: три новых раздела просили
    `file`, `board` и `chart`, а в наборе значков их нет — увидено глазами на
    живом экране, ни одна проверка не покраснела.

    Соседний сторож (`test_screens.py::test_vsyakiy_znachok_sushchestvuet`) сюда
    не дотягивался: он читает экраны, а руководство — данные.
    """
    znachki = set(
        re.findall(r'znachok:\s*"(\w+)"', _tekst())
    )
    assert znachki, "перебор значков пуст — сменился способ их писать"

    ikonki = (KOREN / "web" / "frontend" / "crm" / "src" / "components" / "Icon.tsx").read_text(
        encoding="utf-8"
    )
    izvestnye = set(re.findall(r"^  (\w+):", ikonki, re.M))
    net = sorted(znachki - izvestnye)
    assert not net, (
        "у раздела руководства значок, которого нет в наборе: "
        + ", ".join(net)
        + ". Он не нарисуется ничем, и место слева от подписи останется пустым"
    )


def _marshruty() -> set[str]:
    """Полные адреса экранов из `App.tsx`, вложенные — собранными.

    Читать `path="…"` по одной строке НЕЛЬЗЯ, и это проверено: настройки
    объявлены вложенно (`/settings` и внутри `brand`), поэтому наивный разбор
    объявил битыми три живые ссылки сразу. Сторож, врущий в первый же день,
    отучает смотреть на себя быстрее, чем приносит пользу.

    Вложенность в JSX выражена отступом, по нему и собираем.
    """
    tekst = (KOREN / "web" / "frontend" / "crm" / "src" / "App.tsx").read_text(
        encoding="utf-8"
    )
    polnye: set[str] = set()
    stek: list[tuple[int, str]] = []
    for stroka in tekst.splitlines():
        if "<Route" not in stroka:
            continue
        otstup = len(stroka) - len(stroka.lstrip())
        while stek and stek[-1][0] >= otstup:
            stek.pop()
        roditel = stek[-1][1] if stek else ""
        nayd = re.search(r'path="([^"]+)"', stroka)
        if nayd:
            put = nayd.group(1)
            polnyy = put if put.startswith("/") else f"{roditel.rstrip('/')}/{put}"
            polnye.add(polnyy)
        else:
            polnyy = roditel
        # Открывает ли строка вложенные — видно по ХВОСТУ, а не по наличию
        # «/>» где-нибудь в ней: `element={<SettingsLayout />}` содержит его в
        # середине, и проверка «нет в строке» теряла весь раздел настроек.
        if not stroka.rstrip().endswith("/>"):
            stek.append((otstup, polnyy))
    return polnye


def test_ssylki_na_ekrany_vedut_kuda_to():
    """«Открыть накладные» обязано открывать накладные, а не пустоту.

    Ссылка на несуществующий адрес не роняет ничего: роутер уводит на главную,
    и человек, пришедший в справку за помощью, получает от неё круг. Найти это
    можно только руками, перещёлкав тридцать статей.

    Проверяются только куски `ekran`: у `ruchka` в статье про API путь тоже
    есть, но он адрес СЕРВЕРА, и в роутере ему делать нечего.
    """
    puti = set(re.findall(r'vid:\s*"ekran",\s*put:\s*"([^"]+)"', _tekst()))
    assert puti, "перебор ссылок на экраны пуст — сменился способ их писать"

    izvestnye = _marshruty()
    assert "/settings/brand" in izvestnye, (
        "разбор маршрутов не собирает вложенные адреса — проверка ниже объявит "
        "битыми живые ссылки"
    )
    net = sorted(p for p in puti if p not in izvestnye)
    assert not net, (
        "справка зовёт на адрес, которого в системе нет: "
        + ", ".join(net)
        + ". Читатель нажмёт и вернётся на главную"
    )


def _prava_marshrutov() -> dict[str, str]:
    """Адрес экрана -> право, которым он закрыт в `App.tsx`.

    Право стоит на ОБЁРТКЕ (`<Route element={<PermRoute perm="x" />}>`), а не
    на самом маршруте, и действует на всё вложенное. Собираем по отступу, как
    и адреса выше.
    """
    tekst = (KOREN / "web" / "frontend" / "crm" / "src" / "App.tsx").read_text(
        encoding="utf-8"
    )
    prava: dict[str, str] = {}
    stek: list[tuple[int, str, str]] = []  # отступ, адрес, право
    for stroka in tekst.splitlines():
        if "<Route" not in stroka:
            continue
        otstup = len(stroka) - len(stroka.lstrip())
        while stek and stek[0 - 1][0] >= otstup:
            stek.pop()
        roditel = stek[0 - 1][1] if stek else ""
        pravo = stek[0 - 1][2] if stek else ""
        nayd = re.search(r'perm="([^"]+)"', stroka)
        if nayd:
            pravo = nayd.group(1)
        put = re.search(r'path="([^"]+)"', stroka)
        if put:
            znachenie = put.group(1)
            polnyy = znachenie if znachenie.startswith("/") else (
                roditel.rstrip('/') + '/' + znachenie
            )
            if pravo:
                prava[polnyy] = pravo
        else:
            polnyy = roditel
        if not stroka.rstrip().endswith("/>"):
            stek.append((otstup, polnyy, pravo))
    return prava


def _statyi_so_ssylkami() -> list[tuple[str, str, str]]:
    """(статья, объявленное право, адрес экрана) — по всем кнопкам `ekran`."""
    itog = []
    statya = ""
    pravo = ""
    for stroka in _tekst().splitlines():
        nayd = re.match(r'^\s*id: "([a-z0-9_-]+)",\s*$', stroka)
        if nayd:
            statya = nayd.group(1)
            pravo = ""
        nayd = re.match(r'^\s*perm: "([^"]+)",\s*$', stroka)
        if nayd:
            pravo = nayd.group(1)
        nayd = re.search(r'vid:\s*"ekran",\s*put:\s*"([^"]+)"', stroka)
        if nayd:
            itog.append((statya, pravo, nayd.group(1)))
    return itog


def test_razbor_prav_nahodit_izvestnoe():
    """Сторож на сам разбор: пустая карта прав зеленела бы на любой беде."""
    prava = _prava_marshrutov()
    assert len(prava) > 10, f"маршрутов под правом нашлось {len(prava)} — разбор сломан"
    assert prava.get("/tasks") == "tasks.view", prava.get("/tasks")
    assert prava.get("/settings/labels") == "settings.manage", (
        "вложенный адрес настроек не собрался — право взято не то"
    )


def test_statya_nazyvaet_pravo_svoego_ekrana():
    """Кнопка «Открыть» у читателя без права уводит на сводку молча.

    Статьи закрыты БЛОКОМ, а экраны — ещё и правом. Руководство при этом
    описывает раздел, которого у человека нет, и кнопка в нём не открывает
    ничего: `PermRoute` отскакивает на главную без объяснения. Ради этого в
    `Docs.tsx` и заведён отбор по видимости — «иначе руководство описывает
    чужую систему».
    """
    prava = _prava_marshrutov()
    vinovnye = []
    for statya, obyavleno, put in _statyi_so_ssylkami():
        nuzhno = prava.get(put)
        if nuzhno is None or nuzhno == obyavleno:
            continue
        vinovnye.append(f"{statya} -> {put}: нужно {nuzhno}, объявлено {obyavleno or '—'}")
    assert not vinovnye, (
        "статья ведёт кнопкой на экран, закрытый правом, и права не объявляет:\n  "
        + "\n  ".join(vinovnye)
        + "\nПоставьте `perm` рядом с `id` статьи"
    )
