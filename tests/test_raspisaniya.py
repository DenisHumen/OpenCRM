"""Расписания systemd: час, который написан, — это час, в который сработает.

Юниты `deploy/systemd/*.timer` — единственное место в проекте, где время
задаётся не кодом, а конфигом чужой программы, и проверить его нечем: systemd
разбирает эти файлы **молча и снисходительно**. Незнакомый ключ он не считает
ошибкой — пишет в журнал «Unknown key name» и идёт дальше. Юнит при этом
поднимается, `systemctl status` зелёный, `list-timers` показывает время, и
единственный признак беды — строка в журнале, которую никто не читает, потому
что читать её незачем: ничего не сломалось.

Ровно так здесь и вышло. В двух юнитах стояло

    OnCalendar=*-*-* 09:00:00
    Timezone=Europe/Kyiv

Ключа `Timezone=` в секции `[Timer]` у systemd НЕТ (`man systemd.timer`,
раздел OPTIONS: есть `OnTimezoneChange=`, и это совсем другое — признак
«сработать при смене пояса»). Строка игнорировалась, расписание шло по времени
СЕРВЕРА, и на UTC-VPS утренняя сводка уходила в 11:00–12:00 по Киеву вместо
девяти, а ночная уборка переписки — в 06:30–07:30 вместо 04:30. Правильная
запись — пояс внутри календарного выражения:

    OnCalendar=*-*-* 09:00:00 Europe/Kyiv

Понимает такое systemd с версии 235; целевая среда проекта — Ubuntu 24.04 с
systemd 255 (`docs/ekspluatatsiya/08-razvyortyvanie.md`).

Проверки ниже перебирают ВСЕ файлы каталога, а не два названных: беда была не в
конкретном юните, а в том, что расписание можно написать неверно и не узнать об
этом. Следующий таймер напишут так же.
"""

import pathlib
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

YUNITY = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "systemd"

#: Все таймеры каталога. Список берётся с диска, а не выписывается здесь:
#: выписанный однажды разойдётся с каталогом, и новый юнит окажется
#: непроверенным ровно тем же способом, каким оказались эти два.
TAYMERY = sorted(YUNITY.glob("*.timer")) if YUNITY.is_dir() else []

pytestmark = pytest.mark.skipif(
    not TAYMERY, reason="каталог deploy/systemd не рядом с набором"
)

#: Время суток в календарном выражении: `09:00`, `04:30:00`.
CHAS = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")

#: `20m`, `90s`, `1h`, голое число — секунды. Хватает для того, что здесь пишут.
SROK = re.compile(r"^(\d+)\s*(us|ms|s|m|min|h|d|w)?$")


def _stroki(put: pathlib.Path) -> list[tuple[str, str, str, int]]:
    """Присваивания юнита: (секция, ключ, значение, номер строки).

    Свой разбор, а не `configparser`: последний спотыкается о повторяющиеся
    ключи, которые в юнитах законны (`OnCalendar=` можно перечислять), и теряет
    номера строк — а без номера сообщение о поломке заставляет искать глазами.
    """
    zapisi: list[tuple[str, str, str, int]] = []
    sektsiya = ""
    for nomer, syraya in enumerate(put.read_text(encoding="utf-8").splitlines(), 1):
        stroka = syraya.strip()
        if not stroka or stroka.startswith(("#", ";")):
            continue
        if stroka.startswith("[") and stroka.endswith("]"):
            sektsiya = stroka[1:-1]
            continue
        if "=" not in stroka:
            continue
        klyuch, _, znachenie = stroka.partition("=")
        zapisi.append((sektsiya, klyuch.strip(), znachenie.strip(), nomer))
    return zapisi


def _poyas(vyrazhenie: str) -> str | None:
    """Пояс, названный в конце календарного выражения. Нет — `None`.

    Признак пояса — не вид строки, а то, что база поясов её знает: `Europe/Kyiv`
    отличается от `Europe/Kyev` одной буквой, и второй systemd не примет, а
    любая проверка «похоже на Регион/Город» примет обе.
    """
    chasti = vyrazhenie.split()
    if not chasti:
        return None
    khvost = chasti[-1]
    if khvost.upper() == "UTC":
        return "UTC"
    try:
        ZoneInfo(khvost)
    except Exception:  # noqa: BLE001 — неизвестный ключ, битый ключ, нет базы
        return None
    return khvost


def _sekundy(srok: str) -> timedelta | None:
    """`20m` → 20 минут. Непонятное — `None`."""
    sovpalo = SROK.match(srok.strip())
    if not sovpalo:
        return None
    chislo = int(sovpalo.group(1))
    mera = sovpalo.group(2) or "s"
    return {
        "us": timedelta(microseconds=chislo),
        "ms": timedelta(milliseconds=chislo),
        "s": timedelta(seconds=chislo),
        "m": timedelta(minutes=chislo),
        "min": timedelta(minutes=chislo),
        "h": timedelta(hours=chislo),
        "d": timedelta(days=chislo),
        "w": timedelta(weeks=chislo),
    }[mera]


def _vremya_supok(vyrazhenie: str) -> timedelta | None:
    """Время суток из календарного выражения."""
    for chast in vyrazhenie.split():
        if CHAS.match(chast):
            doli = [int(x) for x in chast.split(":")]
            while len(doli) < 3:
                doli.append(0)
            return timedelta(hours=doli[0], minutes=doli[1], seconds=doli[2])
    return None


def _odin(imya: str) -> pathlib.Path:
    put = YUNITY / imya
    if not put.exists():
        pytest.skip(f"{imya} нет рядом")
    return put


# --- собственно сторожа -------------------------------------------------------


@pytest.mark.parametrize("yunit", TAYMERY, ids=lambda p: p.name)
def test_v_taymere_net_klyucha_timezone(yunit: pathlib.Path):
    """`Timezone=` отдельным ключом запрещён: systemd такого ключа не знает.

    Проверка кажется мелочной ровно до первого раза. Ключ выглядит настолько
    естественно, что его пишут не задумываясь, а systemd на него не ругается —
    он просто идёт по времени сервера. Поймать это можно либо здесь, либо через
    полгода по вопросу «почему сводка приходит к обеду».
    """
    lishnie = [
        (klyuch, nomer)
        for _sektsiya, klyuch, _znachenie, nomer in _stroki(yunit)
        if klyuch.lower() == "timezone"
    ]
    assert not lishnie, (
        f"{yunit.name}: ключа `Timezone=` у systemd нет — он пишет «Unknown key name» "
        f"и идёт по времени сервера. Пояс ставится ВНУТРЬ выражения: "
        f"`OnCalendar=*-*-* 09:00:00 Europe/Kyiv`. Найдено в строках: "
        f"{[n for _k, n in lishnie]}"
    )


@pytest.mark.parametrize("yunit", TAYMERY, ids=lambda p: p.name)
def test_kazhdoe_raspisanie_nazyvaet_poyas(yunit: pathlib.Path):
    """У каждого `OnCalendar=` пояс назван, и база поясов его знает.

    Без пояса расписание идёт по времени сервера, а времени сервера здесь
    никто не выбирает: VPS ставят в UTC по умолчанию, и час, написанный в
    юните, начинает значить не то, что написано. Требование строгое —
    безымянных выражений в этом каталоге быть не должно вовсе: все расписания
    проекта привязаны к часу человека, а не к оси времени.
    """
    raspisaniya = [
        (znachenie, nomer)
        for _sektsiya, klyuch, znachenie, nomer in _stroki(yunit)
        if klyuch.lower() == "oncalendar" and znachenie
    ]
    assert raspisaniya, f"{yunit.name}: таймер без единого `OnCalendar=`"

    bezymyannye = [(v, n) for v, n in raspisaniya if _poyas(v) is None]
    assert not bezymyannye, (
        f"{yunit.name}: расписание без пояса (или с поясом, которого нет в базе IANA) — "
        f"оно пойдёт по времени сервера, а на VPS это UTC. Строки: "
        f"{[(v, n) for v, n in bezymyannye]}"
    )


@pytest.mark.parametrize("yunit", TAYMERY, ids=lambda p: p.name)
def test_yunit_bez_vozvrata_karetki(yunit: pathlib.Path):
    """В юните нет `\\r`: systemd его не срезает.

    Правка с Windows кладёт CRLF в рабочую копию, репозиторий при этом остаётся
    чистым (`.gitattributes` держит `*.timer text eol=lf`), а на сервере
    `Europe/Kyiv\\r` перестаёт быть поясом. Отказ при этом всё тот же тихий:
    строка не разобралась — расписание пошло по времени сервера.
    """
    syroe = yunit.read_bytes()
    assert b"\r" not in syroe, (
        f"{yunit.name}: в файле есть возврат каретки — systemd прочтёт хвост строки "
        "вместе с ним, и пояс перестанет быть поясом"
    )


def test_uborka_perepiski_idet_posle_nochnoy_kopii():
    """Уборка старой переписки стоит ПОСЛЕ копии, и запас на разброс есть.

    Это не про аккуратность расписания, а про безвозвратность. Уборка удаляет
    переписку насовсем; поставленная раньше копии, она удалила бы то, чего
    потом не окажется ни в одной копии за эти сутки. Поставленная после — то,
    что уже лежит во вчерашней, и ошибка в сроке хранения остаётся исправимой.

    Проверка сравнивает не часы, а часы ВМЕСТЕ С ПОЯСОМ, и это здесь главное.
    Пока пояс был написан несуществующим ключом, оба юнита шли по времени
    сервера и порядок держался случайно. Почини пояс в одном и забудь в другом
    — на UTC-сервере копия осталась бы на 03:30 UTC, а уборка уехала бы на
    01:30 UTC, то есть ВПЕРЁД копии, и довод перевернулся бы наизнанку. Именно
    этот способ всё сломать проверка и ловит.
    """
    kopiya = _odin("opencrm-backup.timer")
    uborka = _odin("opencrm-telegram-uborka.timer")

    def raspisanie(put: pathlib.Path) -> tuple[timedelta, str, timedelta]:
        chas = poyas = None
        razbros = timedelta(0)
        for _sektsiya, klyuch, znachenie, _nomer in _stroki(put):
            if klyuch.lower() == "oncalendar":
                chas, poyas = _vremya_supok(znachenie), _poyas(znachenie)
            elif klyuch.lower() == "randomizeddelaysec":
                razbros = _sekundy(znachenie) or timedelta(0)
        assert chas is not None, f"{put.name}: в `OnCalendar=` не разобрать время суток"
        assert poyas is not None, f"{put.name}: в `OnCalendar=` не назван пояс"
        return chas, poyas, razbros

    chas_kopii, poyas_kopii, razbros_kopii = raspisanie(kopiya)
    chas_uborki, poyas_uborki, _razbros_uborki = raspisanie(uborka)

    # Один пояс на оба: часы в разных поясах сравнивать бессмысленно, а
    # разъехаться они могут ровно так же тихо, как разъехался пояс сам.
    assert poyas_kopii == poyas_uborki, (
        f"копия идёт по {poyas_kopii}, а уборка по {poyas_uborki} — их порядок "
        "перестал быть определённым"
    )

    assert chas_uborki > chas_kopii, (
        f"уборка переписки ({chas_uborki}) стоит РАНЬШЕ ночной копии ({chas_kopii}): "
        "она удалит то, чего не окажется ни в одной копии за эти сутки"
    )

    # Копия уводится с пика соседей по железу разбросом, и уборка обязана
    # пережидать не назначенный час, а самый поздний возможный старт копии —
    # плюс время на сам дамп.
    zapas = (chas_uborki - chas_kopii) - razbros_kopii
    assert zapas >= timedelta(minutes=30), (
        f"между самым поздним стартом копии и уборкой остаётся {zapas}: разброс копии "
        f"{razbros_kopii} съедает разрыв, и уборка может начаться по ещё идущему дампу"
    )
