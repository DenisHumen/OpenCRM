"""Что продукт говорит и делает, когда база не отвечает.

Проверки в этом файле выросли из живого опыта: настоящий стек в докере, база
ломалась по очереди всеми способами, какими она ломается на сервере, — штатный
перезапуск, внезапная смерть, пропажа из сети, молчание с живым сокетом,
отсутствие в момент старта, кончившееся под ней место. Числа и разбор — в
`docs/08-deployment.md`, раздел «А что делает система, когда не отвечает БАЗА».

Здесь остаётся то, что можно стеречь без докера. Стеречь приходится потому, что
все три беды ниже **тихие**: они не роняют ни одного теста, не пишут ни строчки
в лог и снаружи выглядят как исправная работа. Общее у них одно — человек,
глядя на сайт, понимает происходящее НЕВЕРНО: ему говорят про обновление, когда
лежит база; показывают время начала от позапрошлого запуска; заставляют ждать
семнадцать минут там, где обещано три.
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

KOREN = pathlib.Path(__file__).resolve().parent.parent
STRANITSA = KOREN / "docker" / "nginx" / "maintenance" / "maintenance.html"
TOCHKA_VHODA = KOREN / "docker" / "entrypoint.sh"

#: Слова, которыми страница ОБЪЯВЛЯЕТ, что идут работы.
#:
#: Список короткий нарочно: сторожить надо не формулировку, а утверждение.
#: Переписать текст можно как угодно, нельзя одного — обещать посетителю
#: работы там, где о работах ничего не известно.
OBYAVLENIYA_O_RABOTAH = (
    "being updated",
    "is restarting",
    "Идёт обновление",
    "Идёт перезапуск",
)

pytestmark = pytest.mark.skipif(
    not STRANITSA.exists() or not TOCHKA_VHODA.exists(),
    reason="обвязка докера рядом не лежит (набор гоняют и вне репозитория)",
)


def _bez_kommentariev(html: str) -> str:
    """Разметка без комментариев: посетителю их не видно, сторожу они мешают.

    В комментариях этой страницы разобрано, ПОЧЕМУ слов про обновление в
    разметке быть не должно, — и сами эти слова там, разумеется, названы.
    Без вычёркивания сторож ловил бы собственное объяснение.
    """
    return re.sub(r"<!--.*?-->", " ", html, flags=re.S)


def _razmetka(html: str) -> str:
    """То, что посетитель видит СРАЗУ: от <body> до первого скрипта.

    Граница здесь по существу. Всё до неё показывается любому, кому nginx не
    смог отдать приложение, — в том числе тому, у кого JavaScript выключен, и
    тому, кто закроет вкладку раньше первого опроса. Всё после неё выполняется
    уже с ответом на руках: есть свежая запись о ходе работ или нет.
    """
    telo = html.split("<body", 1)[-1]
    return _bez_kommentariev(telo.split("<script", 1)[0])


def _skript(html: str) -> str:
    return _bez_kommentariev(html.split("<script", 1)[-1])


def test_stranitsa_obsluzhivaniya_ne_obyavlyaet_rabot_naugad():
    """«Скоро вернёмся, сайт обновляется» — при аварии это враньё, и дорогое.

    nginx отдаёт эту страницу на КАЖДЫЙ свой 502/503/504, то есть всякий раз,
    когда до приложения не достучаться. Обновление — лишь одна из причин;
    остальные — авария.

    Поймано живьём на стенде: пакеты до базы выброшены молча (правило
    фаервола), запрос к `/healthz` провисел 127 секунд, nginx упёрся в
    `proxy_read_timeout 120s` и показал ровно эту страницу со словами «сайт
    обновляется». Никакого обновления не было — лежала база.

    Цена не в красоте формулировки. Владелец, увидевший «идёт обновление»,
    ЖДЁТ: он уверен, что процесс идёт сам и кончится. А кончиться нечем —
    нездоровый контейнер docker не трогает, база сама не встаёт. Объявленные
    работы выглядят ровно так же, как авария, которая без человека не пройдёт.
    """
    html = STRANITSA.read_text(encoding="utf-8")
    naydeno = [s for s in OBYAVLENIYA_O_RABOTAH if s in _razmetka(html)]
    assert naydeno == [], (
        "страница обслуживания объявляет работы, ничего о них не зная: "
        + ", ".join(repr(s) for s in naydeno)
        + ".\nЭту же страницу видит посетитель, когда лежит база, а не когда "
        "идёт обновление. Слова про работы ставит скрипт — и только по свежей "
        "записи в /branding/update-state.json."
    )


def test_pro_raboty_stranitsa_vsyo_zhe_umeet_skazat():
    """Парная проверка: соседнюю нельзя «починить» тем, что страница онемеет.

    Сказать про идущее обновление — полезно и правда: посетитель понимает, что
    ждать осталось недолго и делать ничего не надо. Запрещено ровно обратное —
    говорить это НАУГАД. Поэтому слова обязаны существовать и обязаны стоять за
    проверкой свежести записи (`usableState`).
    """
    html = STRANITSA.read_text(encoding="utf-8")
    skript = _skript(html)

    umeet = [s for s in OBYAVLENIYA_O_RABOTAH if s in skript]
    assert umeet, (
        "страница разучилась говорить про идущие работы вовсе — это не "
        "починка, а потеря: при настоящем обновлении посетитель имеет право "
        "знать, что ждать недолго"
    )
    assert "usableState" in skript, (
        "слова про работы больше не проходят через проверку свежести записи"
    )


def test_neitralnyy_tekst_stranitsy_dostayotsya_i_bez_skripta():
    """Посетитель без JavaScript обязан увидеть честный текст, а не пустоту.

    Страница живёт вне приложения и открывается одним файлом; скрипт в ней —
    удобство (ход работ, змейка, возврат на сайт), а не условие. Значит
    нейтральные слова стоят в самой разметке, а места, куда скрипт их заменит,
    названы по имени — иначе замена молча не состоится, и страница навсегда
    останется молчаливой даже при настоящем обновлении.
    """
    html = STRANITSA.read_text(encoding="utf-8")
    razmetka = _razmetka(html)
    for imya in ('id="lead"', 'id="foot"'):
        assert imya in razmetka, f"в разметке нет {imya} — скрипту нечего заменять"
    assert "temporarily unavailable" in razmetka, (
        "в разметке не осталось нейтрального объяснения: посетитель без "
        "скрипта не узнает даже того, что сайт временно недоступен"
    )


# --------------------------------------------------------------------------
# Отсчёт времени на той же странице
# --------------------------------------------------------------------------
#
# Пока база не поднялась, точка входа ждёт её до трёх минут, и всё это время
# посетитель видит страницу обслуживания со списком шагов и строкой «Started
# HH:MM:SS». Строка эта берётся из `started_at` в update-state.json, а пишет
# его `write_state` в docker/entrypoint.sh.

_SH = "sh"


def _vyrezat_write_state() -> str:
    """Вырезать из точки входа `json_escape` и `write_state` — и только их.

    Позвать сценарий целиком нельзя: он тут же полезет ждать базу и снимать
    копию. Проверять же надо настоящий код, а не его пересказ, — иначе сторож
    сторожит собственную копию и расходится с оригиналом в первый же день.
    """
    text = TOCHKA_VHODA.read_text(encoding="utf-8")
    try:
        nachalo = text.index("json_escape() {")
        konets = text.index("\n}\n", text.index("write_state() {")) + len("\n}\n")
    except ValueError:  # pragma: no cover — сработает, если функции переименуют
        pytest.fail(
            "в docker/entrypoint.sh не нашлись json_escape/write_state — "
            "проверку надо чинить вместе с переименованием"
        )
    return text[nachalo:konets]


def _pozvat_write_state(bylo: dict | None, faza="running", shag="migrate") -> dict:
    """Положить прежнее состояние (или ничего), позвать write_state, вернуть новое."""
    katalog = pathlib.Path(tempfile.mkdtemp(prefix="opencrm-hod-"))
    try:
        fayl = katalog / "update-state.json"
        if bylo is not None:
            fayl.write_text(json.dumps(bylo, ensure_ascii=False), encoding="utf-8")
        stsenariy = (
            f'STATE_DIR="{katalog}"\n'
            f'STATE_FILE="{fayl}"\n'
            + _vyrezat_write_state()
            + f'\nwrite_state {faza} {shag} ""\n'
        )
        vyhod = subprocess.run(
            [_SH, "-c", stsenariy], capture_output=True, text=True, timeout=30
        )
        assert vyhod.returncode == 0, f"write_state отказал: {vyhod.stderr}"
        return json.loads(fayl.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(katalog, ignore_errors=True)


@pytest.mark.skipif(shutil.which(_SH) is None, reason="sh рядом нет")
def test_obychnyy_perezapusk_nachinaet_otschyot_zanovo():
    """Перезапуск контейнера — своё событие, а не продолжение прошлого.

    Последнее, что пишет точка входа перед стартом uvicorn, — `running start`.
    Отметки «закончилось хорошо» после этого не ставит НИКТО: у обновлятора для
    этого есть `_progress_finish` (phase `done`), а у обычного старта нет
    ничего. Значит запись «идёт» лежит на диске всё время, пока сайт работает,
    и наследовать по ней начало отсчёта означало наследовать его от первого
    запуска контейнера — навсегда.

    Замерено на стенде: контейнер поднят в 08:56:32, перезапущен в 09:28:13, а
    в файле по-прежнему `started_at: 08:56:32Z` — тридцать две минуты разницы.
    Дальше хуже: старше двух часов запись считается следом прошлого раза
    (`STATE_MAX_AGE_MS` в maintenance.html), и список шагов прячется ВОВСЕ. То
    есть контейнер, проживший полдня, при перезапуске переставал показывать
    ход — ровно тогда, когда показать его и правда есть что.
    """
    davno = datetime.now(timezone.utc) - timedelta(hours=3)
    stalo = _pozvat_write_state({
        "scope": "restart",
        "phase": "running",
        "step": "start",
        "started_at": davno.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": "",
    })

    assert stalo["scope"] == "restart"
    nachalo = datetime.strptime(stalo["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    vozrast = (datetime.now(timezone.utc) - nachalo).total_seconds()
    assert vozrast < 120, (
        f"отсчёт унаследован от прошлого запуска: записи {vozrast/60:.0f} минут. "
        "Страница обслуживания покажет чужое время, а старше двух часов спрячет "
        "список шагов совсем"
    )


@pytest.mark.skipif(shutil.which(_SH) is None, reason="sh рядом нет")
def test_u_obnovleniya_otschyot_po_prezhnemu_naslednyy():
    """Парная проверка: у обновления владелец отсчёта настоящий, и он на хосте.

    `deploy/updater.py` заводит начало ещё до сборки образа, а контейнер
    вступает посреди процесса и начала не знает. Начни он считать заново —
    посетитель увидел бы, что обновление идёт восемь секунд, тогда как оно идёт
    четвёртую минуту. Соседнюю проверку нельзя «починить» тем, что наследование
    выкинут вовсе.
    """
    nachato = "2026-08-15T08:56:32Z"
    stalo = _pozvat_write_state({
        "scope": "update",
        "phase": "running",
        "step": "build",
        "started_at": nachato,
        "error": "",
    })
    assert stalo["scope"] == "update", "контейнер потерял, что идёт обновление"
    assert stalo["started_at"] == nachato, (
        "контейнер начал отсчёт обновления заново — посетитель увидит, что "
        "обновление только началось, хотя оно идёт давно"
    )


# --------------------------------------------------------------------------
# Сколько точка входа ждёт базу
# --------------------------------------------------------------------------

#: Сколько тянется поддельная попытка и какой ей дают срок.
#:
#: Числа выбраны так, чтобы разница была не в проценты, а в разы: попытка
#: дороже паузы между попытками — ровно как на живой машине, где ответ
#: резолвера стоит девять секунд, а `sleep` две.
POPYTKA_SEK = 4
SROK_POPYTOK = 3          # то есть срок 3 × 2 = 6 секунд


def _vyrezat_zhdat_bazu() -> str:
    text = TOCHKA_VHODA.read_text(encoding="utf-8")
    try:
        nachalo = text.index("DB_WAIT_TRIES=")
        konets = text.index("\n}\n", text.index("zhdat_bazu() {")) + len("\n}\n")
    except ValueError:  # pragma: no cover
        pytest.fail(
            "в docker/entrypoint.sh не нашлись DB_WAIT_TRIES/zhdat_bazu — "
            "проверку надо чинить вместе с переименованием"
        )
    return text[nachalo:konets]


@pytest.mark.skipif(shutil.which(_SH) is None, reason="sh рядом нет")
def test_ozhidanie_bazy_ogranicheno_vremenem_a_ne_chislom_popytok():
    """«90 попыток по 2 с = 3 минуты» — верно, только пока попытка бесплатна.

    Она бесплатна ровно в одном случае: контейнер базы ЕСТЬ и отвергает
    подключение сразу. А когда его нет — машина перезагрузилась, база не
    поднялась, `docker stop` — имя `db` не разрешается вовсе, и попытка стоит
    потолок резолвера. Замерено на стенде с остановленной базой: 11282, 8558,
    8502 мс. С паузами это 90 × 11,4 ≈ 1030 с — семнадцать минут вместо трёх.

    Считать надо было время. Расплата за счёт попыток не только в терпении:
    посетитель все семнадцать минут видит шаг «миграции», который не начинался,
    а правило `ContainerRestartLoop` («больше трёх перезапусков за 15 минут»)
    при цикле в семнадцать минут не срабатывает НИКОГДА — тревога, написанная
    ровно про этот случай, не может зажечься.

    Здесь попытка подделана дорогой, а срок дан короткий: со счётом по попыткам
    ожидание вышло бы втрое дольше срока и проверка покраснела бы.
    """
    katalog = pathlib.Path(tempfile.mkdtemp(prefix="opencrm-zhdu-"))
    try:
        podstava = katalog / "python"
        podstava.write_text(f"#!/bin/sh\nsleep {POPYTKA_SEK}\nexit 1\n", encoding="utf-8")
        podstava.chmod(0o755)

        stsenariy = (
            f'PATH="{katalog}:$PATH"\n'
            f'OPENCRM_DB_WAIT_TRIES={SROK_POPYTOK}\n'
            + _vyrezat_zhdat_bazu()
            + "\nzhdat_bazu && echo OTVETILA || echo SDALAS\n"
        )
        nachalo = datetime.now(timezone.utc)
        vyhod = subprocess.run(
            [_SH, "-c", stsenariy], capture_output=True, text=True, timeout=120
        )
        proshlo = (datetime.now(timezone.utc) - nachalo).total_seconds()

        assert "SDALAS" in vyhod.stdout, f"ожидание не сдалось вовсе: {vyhod.stdout!r}"
        srok = SROK_POPYTOK * 2
        potolok = srok + POPYTKA_SEK + 2          # срок + одна незаконченная попытка + запас
        po_popytkam = SROK_POPYTOK * (POPYTKA_SEK + 2)
        assert proshlo < potolok < po_popytkam, (
            f"ожидание длилось {proshlo:.1f} с при сроке {srok} с. Похоже, считаются "
            f"попытки, а не время: по попыткам вышло бы около {po_popytkam} с, и на "
            "живой машине с неразрешимым именем базы это семнадцать минут вместо трёх"
        )
    finally:
        shutil.rmtree(katalog, ignore_errors=True)
