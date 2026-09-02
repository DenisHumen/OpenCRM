"""Каждый блок системы описан в руководстве.

**Зачем сторож.** Руководство читают вместо того, чтобы спрашивать. Блок,
которого в нём нет, для читателя не существует — а узнать о нём он может только
случайно, наткнувшись на пункт меню.

Так уже было, и не раз: **весь блок накладных отсутствовал** и в таблице «что
умеет» `01-overview.md`, и в плане работ, и в справочнике API — двенадцать ручек
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
