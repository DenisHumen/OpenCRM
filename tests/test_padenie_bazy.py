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


def _put_dlya_sh(katalog: pathlib.Path) -> str:
    """Путь к каталогу так, как его понимает `sh`.

    Нужен ровно из-за Windows. `sh` здесь стоит из Git for Windows и разбирает
    в `PATH` только POSIX-пути: запись вида `C:/Temp/...` он не находит вовсе.
    Подстава при этом молча не подхватывается — `python` берётся настоящий,
    отвечает 1 («нет такой команды» или обычная беда), и проверка зеленеет, ни
    разу не тронув то, ради чего написана. Поймано на подставе, которая должна
    была вернуть 2, а вернула 1.

    На Linux (и в CI) `cygpath` отсутствует, путь и так POSIX — отдаём как есть.
    """
    if shutil.which("cygpath") is None:
        return str(katalog)
    gotovo = subprocess.run(
        ["cygpath", "-u", str(katalog)], capture_output=True, text=True, timeout=30
    )
    return gotovo.stdout.strip() or str(katalog)


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
            "set -e\n"
            f'PATH="{_put_dlya_sh(katalog)}:$PATH"\n'
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


def _vyrezat_razbor_itoga() -> str:
    """Кусок, который разбирает ИТОГ ожидания. Функция без него ничего не решает."""
    text = TOCHKA_VHODA.read_text(encoding="utf-8")
    try:
        nachalo = text.index("\nDB_ITOG=0\nzhdat_bazu ||")
        konets = text.index('if [ "$DB_ITOG" -ne 0 ]', nachalo)
        konets = text.index("\nfi\n", konets) + len("\nfi\n")
    except ValueError:  # pragma: no cover
        pytest.fail(
            "в docker/entrypoint.sh не нашёлся разбор DB_ITOG — проверку надо "
            "чинить вместе с переименованием"
        )
    return text[nachalo:konets]


@pytest.mark.skipif(shutil.which(_SH) is None, reason="sh рядом нет")
def test_otkaz_dostupa_ne_uhodit_v_ozhidanie():
    """Неверный пароль базы — не гонка старта, и ждать его три минуты нечего.

    Ожидание заведено под ОДИН случай: база поднимается дольше приложения. У
    неверного пароля (или чужого имени базы) всё наоборот — сервер работает,
    запрос разобрал и отказал по существу. За три минуты это само не поправится
    ни разу.

    А выглядело оно ровно как молчание: `ping` на любую беду отвечал единицей,
    цикл честно крутил девяносто попыток и на выходе говорил «база не ответила
    за 180 с» — про базу, которая ответила с первой попытки. Человек три минуты
    ждал ошибку, видимую сразу, и шёл читать `logs db`, где ничего плохого нет.

    Здесь `ping` подделан отказом доступа. Ожидание обязано кончиться сразу и
    кодом 3, а не сроком: со старым разбором проверка уходила бы в полный срок.
    """
    katalog = pathlib.Path(tempfile.mkdtemp(prefix="opencrm-otkaz-"))
    try:
        podstava = katalog / "python"
        podstava.write_text(
            "#!/bin/sh\n"
            'echo "сервер базы ответил и не пустил: Access denied for user" >&2\n'
            "exit 3\n",
            encoding="utf-8",
        )
        podstava.chmod(0o755)

        stsenariy = (
            "set -e\n"
            f'PATH="{_put_dlya_sh(katalog)}:$PATH"\n'
            f"OPENCRM_DB_WAIT_TRIES={SROK_POPYTOK}\n"
            + _vyrezat_zhdat_bazu()
            + "\n_k=0; zhdat_bazu || _k=$?; echo KOD=$_k\n"
        )
        nachalo = datetime.now(timezone.utc)
        vyhod = subprocess.run(
            [_SH, "-c", stsenariy], capture_output=True, text=True, timeout=120
        )
        proshlo = (datetime.now(timezone.utc) - nachalo).total_seconds()

        assert "KOD=3" in vyhod.stdout, (
            "ожидание не отличило отказ доступа от молчания: "
            f"{vyhod.stdout!r} / {vyhod.stderr!r}"
        )
        assert proshlo < SROK_POPYTOK * 2, (
            f"ожидание длилось {proshlo:.1f} с при сроке {SROK_POPYTOK * 2} с — "
            "значит отказ доступа снова крутится в цикле вместо мгновенного отказа"
        )
        assert "Access denied" in vyhod.stderr, (
            "причина отказа не дошла до журнала: человек увидит, что что-то не "
            f"так, но не увидит ЧТО. Вышло: {vyhod.stderr!r}"
        )
    finally:
        shutil.rmtree(katalog, ignore_errors=True)


@pytest.mark.skipif(shutil.which(_SH) is None, reason="sh рядом нет")
def test_otkaz_dostupa_nazyvaet_chto_chinit():
    """Сообщение обязано назвать ключи `docker/.env`, а не слать в `logs db`.

    Отличить «база лежит» от «база не пускает» человеку неоткуда: контейнер
    базы в обоих случаях жив, и в его журнале ничего не происходит. Поэтому
    строку, которую он увидит, пишем мы, и в ней должны стоять имена настроек,
    которые надо сверить, — иначе различение внутри скрипта останется знанием
    скрипта, а не человека.
    """
    katalog = pathlib.Path(tempfile.mkdtemp(prefix="opencrm-itog-"))
    try:
        podstava = katalog / "python"
        podstava.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        podstava.chmod(0o755)

        stsenariy = (
            "set -e\n"
            f'PATH="{_put_dlya_sh(katalog)}:$PATH"\n'
            f"OPENCRM_DB_WAIT_TRIES={SROK_POPYTOK}\n"
            'write_state() { echo "STATE $*"; }\n'
            + _vyrezat_zhdat_bazu()
            + _vyrezat_razbor_itoga()
            + "\necho DOSHLI_DALSHE\n"
        )
        vyhod = subprocess.run(
            [_SH, "-c", stsenariy], capture_output=True, text=True, timeout=120
        )

        assert "DOSHLI_DALSHE" not in vyhod.stdout, (
            "точка входа пошла дальше с базой, которая не пускает: следом идут "
            "копия и миграции, а им работать нечем"
        )
        assert vyhod.returncode == 1, (
            f"выход не единицей ({vyhod.returncode}) — докер не поймёт, "
            "что старт не удался"
        )
        vsyo = vyhod.stdout + vyhod.stderr
        # Имена ровно те, что стоят в `docker/.env`. Приблизительные
        # (`DB_USER` вместо `OPENCRM_DB_USER`) отправили бы человека искать
        # ключ, которого в файле нет, — а он и так уже в неприятной ситуации.
        for klyuch in ("OPENCRM_DB_USER", "OPENCRM_DB_PASSWORD", "OPENCRM_DB_NAME"):
            assert klyuch in vsyo, (
                f"в сообщении об отказе не назван {klyuch}. Человеку сказали, что "
                f"не пустили, но не сказали, где смотреть: {vsyo!r}"
            )
        assert "STATE failed migrate" in vyhod.stdout, (
            "ход обновления не отмечен неудачей — страница так и будет обещать "
            f"миграции, которых не будет: {vyhod.stdout!r}"
        )
    finally:
        shutil.rmtree(katalog, ignore_errors=True)
