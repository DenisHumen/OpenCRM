"""Живое меню: рисуется, слушается стрелок и отдаёт терминал обратно.

**Главное здесь — не красота, а возврат терминала.** Живое меню намеренно
включает сырой режим: в нём `read` не ждёт перевода строки, а Ctrl+C не доходит
как сигнал. Оставленный сырой режим со стороны неотличим от зависшего скрипта, и
выход остаётся один — переподключиться по ssh. На боевом сервере это потеря
управления, и потому проверки ниже начинаются именно с неё.

**Проверки идут в НАСТОЯЩЕМ псевдотерминале**, а не чтением исходника. Без pty
живое меню не включается вовсе (`tui_dostupen` требует `-t 1`), и проверка,
читающая текст скрипта, зеленела бы, ни разу его не запустив, — та же беда, что
у shell-проверок на Windows.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

KOREN = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = KOREN / "opencrm.sh"

#: Живое меню работает только там, где есть pty, sh и stty. На Windows их нет,
#: и «пропущено» здесь честнее зелёного.
nuzhen_pty = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "openpty"),
    reason="живому меню нужен настоящий псевдотерминал",
)


def istochnik() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _stend(tmp_path: pathlib.Path) -> pathlib.Path:
    """Установленный сайт, какого нет: подставные docker, curl и python3.

    Меню не должно зависеть от того, поднят ли рядом настоящий стек, — иначе
    проверка мерила бы докер, а не меню.
    """
    dom = tmp_path / "stend"
    (dom / "config").mkdir(parents=True)
    (dom / "docker").mkdir(parents=True)
    (dom / "bin").mkdir(parents=True)
    shutil.copy(SCRIPT, dom / "opencrm.sh")
    (dom / "config" / ".env").write_text(
        "OPENCRM_BASE_URL=https://proba.example.com\n", encoding="utf-8"
    )
    (dom / "docker" / ".env").write_text("OPENCRM_LANG=ru\n", encoding="utf-8")

    (dom / "bin" / "docker").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"ps --services --filter status=running"*) printf "app\\ndb\\n" ;;\n'
        '  *"ps --services"*) printf "app\\ndb\\nnginx\\n" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (dom / "bin" / "curl").write_text(
        '#!/bin/sh\nprintf \'{"status":"ok"}\'\n', encoding="utf-8"
    )
    (dom / "bin" / "python3").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *status*)\n"
        '    printf "развёрнуто:     abc1234\\n"\n'
        '    printf "обновление:     есть\\n"\n'
        '    printf "автообновление: включено\\n" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    for imya in ("docker", "curl", "python3"):
        (dom / "bin" / imya).chmod(0o755)
    return dom


def _pokazat(dom: pathlib.Path, klavishi: str = "", sekund: float = 7.0) -> str:
    """Запускает меню в pty, шлёт клавиши и возвращает всё, что нарисовалось.

    Семь секунд, а не «сколько-нибудь». Цикл меню ждёт клавишу с секундной
    гранулярностью (`stty time 10` — то самое, что делает шапку живой), и к
    этой секунде добавляются первая отрисовка и фоновый сбор сводки. При
    четырёх секундах проверка цифрового выбора падала через раз — не от
    поломки, а от нехватки времени. Мигающая проверка хуже отсутствующей:
    её быстро приучаются перезапускать.
    """
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover — это уже другой процесс
        os.environ.update({
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "OPENCRM_LANG": "ru",
            "PATH": f"{dom / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(dom),
        })
        os.execvp("sh", ["sh", str(dom / "opencrm.sh")])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    kody = {
        "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
        "enter": b"\r", "q": b"q",
    }
    vyvod = b""
    nachalo = time.time()
    poslano = False
    while time.time() - nachalo < sekund:
        gotov, _, _ = select.select([fd], [], [], 0.2)
        if gotov:
            try:
                kusok = os.read(fd, 65536)
            except OSError:
                break
            if not kusok:
                break
            vyvod += kusok
        # Ждём ГОТОВНОСТИ меню, а не часов.
        #
        # По таймеру клавиша уходила раньше, чем скрипт успевал войти в сырой
        # режим, а переключение режима сбрасывает то, что уже лежит в буфере
        # ввода, — нажатие просто исчезало. Падало это через раз и выглядело как
        # поломка цифрового выбора, хотя выбор был исправен: тот же «4» в
        # ручном прогоне открывал раздел безотказно.
        #
        # Признак готовности — подсказка внизу экрана: она печатается последней
        # строкой первой отрисовки, то есть после `tui_vklyuchit`.
        if not poslano and klavishi and "↑↓".encode() in vyvod:
            for k in klavishi.split(","):
                os.write(fd, kody.get(k, k.encode()))
                time.sleep(0.4)
            poslano = True
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass
    return vyvod.decode("utf-8", "replace")


# --- то, ради чего всё ------------------------------------------------------


@nuzhen_pty
def test_terminal_vozvrashchaetsya_posle_vyhoda(tmp_path):
    """Сырой режим, оставленный после меню, — это потеря управления сервером.

    Проверяется не текст скрипта, а состояние ПОСЛЕ его работы: спрашиваем у
    того же терминала, вернулись ли эхо и построчный ввод. Чтение исходника
    сказало бы лишь, что нужные слова написаны.
    """
    import pty
    import termios
    import time

    dom = _stend(tmp_path)
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover
        os.environ.update({
            "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "OPENCRM_LANG": "ru",
            "PATH": f"{dom / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(dom),
        })
        os.execvp("sh", ["sh", str(dom / "opencrm.sh")])

    do = termios.tcgetattr(fd)
    time.sleep(2.0)
    os.write(fd, b"q")          # выход из живого меню
    time.sleep(1.5)
    posle = termios.tcgetattr(fd)
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass

    # lflag несёт ECHO и ICANON — ровно то, что снимает сырой режим.
    assert posle[3] == do[3], (
        "терминал остался в сыром режиме после выхода из меню: "
        f"lflag было {do[3]:#x}, стало {posle[3]:#x}"
    )


@nuzhen_pty
def test_menyu_risuetsya_i_pokazyvaet_sostoyanie(tmp_path):
    """Шапка отвечает на вопрос «что сейчас», не дожидаясь нажатий.

    Ради этого живое меню и заведено: прежнее печатало список и молчало о сайте,
    пока человек не выберет пункт.
    """
    ekran = _pokazat(_stend(tmp_path))
    assert "OpenCRM" in ekran
    for slovo in ("работает", "версия", "контейнеры", "Состояние", "Управление"):
        assert slovo in ekran, f"в живом меню нет «{slovo}»:\n{ekran[-1500:]}"
    # Лежачий контейнер обязан быть назван: счётчик «2/3» без имени не отвечает
    # на вопрос, ради которого на него смотрят.
    assert "nginx" in ekran, "не названа служба, которая лежит"


@nuzhen_pty
def test_strelki_hodyat_po_spisku_i_vhodyat_v_razdel(tmp_path):
    """Вправо открывает раздел, влево возвращает — иначе это не меню, а список."""
    dom = _stend(tmp_path)
    vnutri = _pokazat(dom, "down,down,right")
    assert "Обновить сейчас" in vnutri, (
        f"стрелка вправо не открыла раздел:\n{vnutri[-1200:]}"
    )
    nazad = _pokazat(dom, "down,down,right,left")
    hvost = nazad[-1500:]
    assert "Копии" in hvost and "Доступ и сеть" in hvost, (
        f"стрелка влево не вернула в главный список:\n{hvost}"
    )


@nuzhen_pty
def test_cifra_otkryvaet_razdel_srazu(tmp_path):
    """Привычка жать номер осталась от прежнего меню, и ломать её незачем."""
    ekran = _pokazat(_stend(tmp_path), "4")
    assert "Снять копию" in ekran, f"цифра не открыла раздел:\n{ekran[-1200:]}"


# --- запасной путь ----------------------------------------------------------


def test_bez_terminala_rabotaet_prezhnee_menyu(tmp_path):
    """Пульт обязан работать в самом бедном окружении, какое бывает.

    Пайп, cron, аварийная консоль хостера, вывод в файл — живому меню там либо
    нельзя, либо незачем. Прежнее номерное остаётся на месте без единого
    отличия, и проверка держит именно это.
    """
    if sys.platform == "win32":
        pytest.skip("нужен sh")
    dom = _stend(tmp_path)
    gotovo = subprocess.run(
        ["sh", str(dom / "opencrm.sh")],
        input="0\n", capture_output=True, text=True, timeout=60,
        env={
            **os.environ,
            "OPENCRM_INPUT": "stdin",
            "OPENCRM_LANG": "ru",
            "PATH": f"{dom / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(dom),
        },
    )
    vyvod = gotovo.stdout + gotovo.stderr
    assert "1) Статус и здоровье" in vyvod, f"номерное меню не показалось:\n{vyvod[:900]}"
    assert "17)" in vyvod, "из номерного меню пропали пункты"
    # И ни слова ругани про терминал: /dev/tty без управляющего терминала
    # открыть нельзя, и сообщение об этом пугает ровно там, где всё в порядке.
    assert "/dev/tty" not in vyvod, f"в выводе осталась ругань про терминал:\n{vyvod[:400]}"


def test_zhivoe_menyu_otklyuchaetsya_peremennoy():
    """Отдушина обязана быть названа: чужая автоматизация не должна гадать."""
    text = istochnik()
    assert "OPENCRM_TUI" in text, "нет способа выключить живое меню"
    dostupen = text[text.index("tui_dostupen() {"):]
    dostupen = dostupen[: dostupen.index("\n}")]
    for uslovie in ("OPENCRM_TUI", "ASSUME_YES", "OPENCRM_INPUT", "-t 1", "TERM"):
        assert uslovie in dostupen, f"живое меню не проверяет {uslovie}"


def test_syroy_rezhim_snimaetsya_lovushkoy():
    """`trap` — единственное, что работает при обрыве.

    Возврат терминала, написанный только в конце цикла, не выполнится ни при
    Ctrl+C, ни при `set -e`, ни при закрытом ssh — то есть ровно тогда, когда он
    и нужен.
    """
    text = istochnik()
    telo = text[text.index("tui_menu() {"):]
    telo = telo[: telo.index("\n}")]
    assert "trap" in telo, "возврат терминала не повешен на trap"
    assert "INT TERM" in telo, "trap не ловит Ctrl+C и остановку"
    assert "EXIT" in telo, "trap не ловит обычный выход"
    assert telo.index("trap") < telo.index("tui_vklyuchit"), (
        "ловушка ставится ПОСЛЕ входа в сырой режим — окно, в котором обрыв "
        "оставит терминал сырым"
    )


def test_pomoshchniki_ne_zatirayut_peremennye_tsikla():
    """В POSIX sh нет локальных переменных, и это стоило самой дорогой ошибки.

    `tui_shapka` держала счётчик контейнеров в `_vsego` — том же имени, каким
    цикл меню считает пункты. Отрисовка шапки затирала счётчик пунктов ЧУЖИМ
    числом, и дальше цифровой выбор сверялся с числом контейнеров: «4» работала,
    пока их было четыре, и переставала на трёх. Стрелки при этом выглядели
    исправными — они заворачивались по тому же чужому числу.

    Заметить это чтением невозможно: обе функции по отдельности верны. Поэтому
    проверка не ищет конкретное имя, а держит ГРАНИЦУ: помощники пользуются
    своим префиксом и не могут дотянуться до состояния цикла.
    """
    text = istochnik()
    blok = text[text.index("# Живое меню (TUI)") : text.index(chr(10) + "menu() {")]
    granica = blok.index("tui_menu() {")
    pomoshchniki = blok[:granica]

    # Имена, которыми цикл меню держит своё состояние.
    tsikla = {"_razdel", "_vybor", "_stek", "_vsego", "_k", "_n", "_tek", "_deystvie"}
    vzyaty = {m.group(1) for m in re.finditer(r"(?<![\w])(_[a-z][a-z0-9_]*)", pomoshchniki)}
    stolknoveniya = sorted(vzyaty & tsikla)
    assert not stolknoveniya, (
        "помощники живого меню трогают переменные цикла: " + ", ".join(stolknoveniya)
        + ". В POSIX sh это одна и та же переменная, и состояние меню будет "
        "затираться молча."
    )

    # И обратная сторона: префикс обязан быть, иначе граница держится случайно.
    bez_prefiksa = sorted(
        i for i in vzyaty if not i.startswith("_tui_")
    )
    assert not bez_prefiksa, (
        "в помощниках завелись переменные без префикса `_tui_`: "
        + ", ".join(bez_prefiksa)
        + ". Сегодня они ни с чем не совпали, завтра совпадут."
    )


def test_rezka_strok_ne_bayty():
    """Байтовая резка оставляет половину буквы, и видно это только глазами.

    Замерено на Debian и Alpine: `cut -c 1-20` и `awk substr` режут БАЙТЫ —
    «последнее \\xd0» вместо «последнее обновление». Ширину при этом `wc -m`
    считает верно, знаками.
    """
    text = istochnik()
    tui = text[text.index("# Живое меню (TUI)"):]
    stroki = [
        s for s in tui.splitlines()
        if not s.strip().startswith("#") and ("cut -c" in s or "awk substr" in s)
    ]
    assert not stroki, (
        "в живом меню снова режут байтами:\n  " + "\n  ".join(s.strip() for s in stroki)
    )
    assert "tui_srez()" in tui, "исчез общий резак по знакам"


def test_cvet_snimaetsya_perednim_esc():
    """`\\033` внутри `sed` — четыре обычных знака, а не escape.

    Пока цвет не снимался, ширина строки считалась вместе с управляющими
    последовательностями, и правая рамка уезжала на их длину. Ни `sh -n`, ни
    остальной набор такого не видят.
    """
    text = istochnik()
    assert "TUI_ESC=$(printf '\\033')" in text, "ESC больше не берётся отдельной переменной"
    tui = text[text.index("# Живое меню (TUI)"):]
    plohie = [
        s for s in tui.splitlines()
        if not s.strip().startswith("#") and re.search(r"sed\s+'s/\\033", s)
    ]
    assert not plohie, "в sed снова записан \\033 напрямую:\n" + "\n".join(plohie)
