"""Почтовый сервер на этой машине: просьба приложения → `core/pochta/ustanovit.sh`.

Разбор — docs/bloki/32-pochtovyy-server.md, «Вариант 1». Приложение docker не
видит, поэтому кладёт просьбу файлом в общий `data/obsluzhivanie/`, а здесь,
на хосте, её исполняют: обновлятор в паузе между опросами (под замком
обслуживания) или человек командой `./opencrm.sh mail install`, если
обновлятора нет. Журнал пишется построчно — экран мастера читает его, пока идёт
установка. Имена файлов продублированы в `core/services/mail_spool.py`: этот
каталог в образ приложения не едет. Только stdlib: код работает вне контейнера.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PAPKA = "obsluzhivanie"
ZAPROS = "pochta-zapros.json"
ZHURNAL = "pochta-zhurnal-{job}.log"
ITOG = "pochta-itog-{job}.json"
OBNOVLYATOR = "pochta-obnovlyator.json"
OPERATSII = ("check", "install", "remove", "adopt")
#: Проект compose почты — отдельный и всегда этот: `down` под чужим `-p` сносит живой стек.
PROEKT = "opencrm-mail"
KONTEYNER = "opencrm-mail"
#: Установка с выпуском сертификата и скачиванием образа укладывается в минуты.
POTOLOK_SEKUND = 3600
#: Образ и порты решает хост, а не просьба: `data/` пишет контейнер приложения, и
#: взломанное приложение иначе запустило бы свой образ с NET_ADMIN от root хоста.
#: Близнец — `database/models/mail_server.py:DEFAULT_IMAGE` (сверяет тест).
OBRAZ = "ghcr.io/docker-mailserver/docker-mailserver:16.0.1"
PORTY = "25 465 587 993"
_IMYA = r"(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
#: Что приложение вправе задать и каким оно обязано быть; остальное решает хост.
PRINIMAEM = {
    "MAIL_HOSTNAME": re.compile(rf"^{_IMYA}$"),
    "MAIL_DOMAIN": re.compile(rf"^{_IMYA}$"),
    "MAIL_TLS": re.compile(r"^(webroot|selfsigned|letsencrypt)$"),
    "MAIL_ACME_EMAIL": re.compile(r"^([A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,253})?$"),
    "MAIL_FIRST": re.compile(r"^[a-z0-9._+-]{1,64}@[a-z0-9.-]{1,253}$"),
    "MAIL_FIRST_PASSWORD": re.compile(r"^[^\r\n]{1,200}$"),
    "MAIL_MASTER_USER": re.compile(r"^[a-z0-9._-]{1,64}$"),
    "MAIL_MASTER_PASSWORD": re.compile(r"^[^\r\n]{1,200}$"),
    "MAIL_AGENT_TOKEN": re.compile(r"^[A-Za-z0-9_-]{20,200}$"),
    "MAIL_DELETE_DATA": re.compile(r"^[01]$"),
}
RABOTA = "pochta-rabota"


def _teper() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def papka(data_dir: Path) -> Path:
    return Path(data_dir) / PAPKA


def _svoya_papka(put: Path) -> Path:
    """Каталог в `data/` — не ссылка: её подложило бы приложение, и root хоста писал
    бы наши файлы туда, куда оно укажет."""
    put.mkdir(parents=True, exist_ok=True)
    if put.is_symlink() or not put.is_dir():
        raise OSError(f"{put} is not a plain directory")
    return put


def _novyy_fayl(put: Path, rezhim: int = 0o600):
    """Новый файл без перехода по ссылке: O_EXCL | O_NOFOLLOW."""
    flagi = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(put, flagi, rezhim), "w", encoding="utf-8")


def _zapisat(put: Path, dannye: dict) -> None:
    """Целиком и подменой: экран не должен прочесть файл, записанный наполовину."""
    _svoya_papka(put.parent)
    vremennyy = put.with_name(f"{put.name}.{secrets.token_hex(4)}.tmp")
    # Читает приложение (пользователь opencrm в контейнере) — 0644, секретов тут нет.
    with _novyy_fayl(vremennyy, 0o644) as f:
        f.write(json.dumps(dannye, ensure_ascii=False))
    os.replace(vremennyy, put)


def otmetit_zhivoy(data_dir: Path) -> None:
    """Обновлятор жив: экран мастера не отправит человека к консоли зря."""
    try:
        _zapisat(papka(data_dir) / OBNOVLYATOR, {"at": _teper()})
    except OSError:
        pass


def dom(data_dir: Path) -> Path:
    """Каталог установки (`OPENCRM_HOME`): почта ляжет в его `mail/`, рядом с `data/`."""
    yavno = os.environ.get("OPENCRM_HOME", "").strip()
    return Path(yavno) if yavno else Path(data_dir).parent


def proekt_compose() -> str:
    return os.environ.get("COMPOSE_PROJECT_NAME", "").strip() or "opencrm"


class PlokhayaProsba(ValueError):
    """Просьба с негодным значением: исполнять нельзя, в журнал — какое поле."""


def okruzhenie(prosba_env: dict, home: Path, project_dir: Path) -> dict[str, str]:
    """Параметры установщика: от приложения — только `PRINIMAEM` и по их образцам,
    от хоста — пути, сеть, образ и порты."""
    env = {}
    for k, v in (prosba_env or {}).items():
        obrazets = PRINIMAEM.get(str(k))
        if obrazets is None:
            continue
        znachenie = str(v if v is not None else "")
        if not obrazets.match(znachenie):
            raise PlokhayaProsba(k)
        env[k] = znachenie
    env.update({
        "MAIL_IMAGE": OBRAZ,
        "MAIL_PORTS": PORTY,
        "MAIL_DIR": str(Path(home) / "mail"),
        "MAIL_NETWORK": f"{proekt_compose()}_default",
        "MAIL_AGENT_SRC": str(Path(project_dir) / "core" / "pochta" / "agent.py"),
        "MAIL_WEBROOT": str(Path(home) / "acme"),
        "MAIL_LE_DIR": str(Path(home) / "letsencrypt"),
        "MAIL_CONTAINER": KONTEYNER,
        "MAIL_ALLOW_DOCKER_INSTALL": "0",
        "MAIL_SUDO_PASSWORD": "",
    })
    # Сертификат на этой машине выпускает тот же nginx OpenCRM (webroot); самоподписанный —
    # только по явной просьбе (стенд без DNS).
    if env.get("MAIL_TLS") != "selfsigned":
        env["MAIL_TLS"] = "webroot"
    return env


def _prinyat_token(env: dict[str, str], zhurnal) -> int:
    """adopt: новый ключ агенту уже стоящего сервера. Агент читает файл на каждый запрос."""
    token = env.get("MAIL_AGENT_TOKEN", "")
    kuda = Path(env["MAIL_DIR"]) / "config" / "opencrm"
    zhurnal("##STEP agent")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", token):
        zhurnal("##FAIL agent start_failed agent token is missing")
        zhurnal("##END failed")
        return 1
    if not (Path(env["MAIL_DIR"]) / "compose.yml").exists():
        zhurnal(f"##FAIL agent start_failed {env['MAIL_DIR']}/compose.yml not found: the server was not installed here")
        zhurnal("##END failed")
        return 1
    try:
        kuda.mkdir(parents=True, exist_ok=True)
        put = kuda / "agent.token"
        vremennyy = kuda / ("agent.token." + secrets.token_hex(4))
        fd = os.open(vremennyy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.replace(vremennyy, put)
    except OSError as beda:
        zhurnal(f"##FAIL agent start_failed cannot write the agent token: {beda}")
        zhurnal("##END failed")
        return 1
    zhurnal("##OK agent new agent token is in place")
    zhurnal("##END ok")
    return 0


def _rabochiy_katalog(state_dir: Path) -> Path:
    """Где лежит ustanovka.env: в каталоге обновлятора, куда приложение не пишет
    (`updates/` у него только на чтение), — а не в общем `data/`."""
    rabota = Path(state_dir) / RABOTA
    rabota.mkdir(parents=True, exist_ok=True, mode=0o700)
    if rabota.is_symlink() or not rabota.is_dir():
        raise OSError(f"{rabota} is not a plain directory")
    os.chmod(rabota, 0o700)
    for ostatok in ("ustanovka.env", ".parol.tmp"):
        (rabota / ostatok).unlink(missing_ok=True)
    return rabota


def vypolnit(prosba: dict, data_dir: Path, home: Path, project_dir: Path, log=None,
             state_dir: Path | None = None) -> dict | None:
    """Исполнить одну просьбу: журнал построчно в файл, итог — отдельным файлом."""
    log = log or (lambda _s: None)
    try:
        job = int(prosba.get("job_id"))
    except (TypeError, ValueError):
        log("почта: просьба без номера работы — выброшена")
        return None
    op = str(prosba.get("op") or "")
    kuda = papka(data_dir)
    put_zhurnala = kuda / ZHURNAL.format(job=job)
    if op not in OPERATSII:
        _zapisat(kuda / ITOG.format(job=job), {"ok": False, "status": "failed", "exit_code": 2, "at": _teper()})
        return None
    plokho = ""
    env: dict[str, str] = {}
    try:
        env = okruzhenie(prosba.get("env") or {}, home, project_dir)
        if op in ("check", "install") and not env.get("MAIL_HOSTNAME"):
            raise PlokhayaProsba("MAIL_HOSTNAME")
    except PlokhayaProsba as beda:
        plokho = str(beda)
    rabota = _rabochiy_katalog(state_dir if state_dir is not None else Path(home) / "updates")
    status = "failed"
    kod = 1
    zamok = threading.Lock()
    _svoya_papka(kuda)
    flagi = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(put_zhurnala, flagi, 0o644), "a", encoding="utf-8") as zh:
        poslednyaya = [0.0]

        def zhurnal(stroka: str) -> None:
            nonlocal status
            with zamok:
                zh.write(stroka.rstrip("\n") + "\n")
                zh.flush()
            if stroka.startswith("##END "):
                status = stroka.split(" ", 1)[1].strip()
            # Длинная установка держит обновлятор: отметка живости — по ходу, а не раз в круг.
            if time.monotonic() - poslednyaya[0] > 10:
                poslednyaya[0] = time.monotonic()
                otmetit_zhivoy(data_dir)

        zhurnal(f"# {_teper()} {op} by {str(prosba.get('kto') or '')[:80]}")
        if plokho:
            zhurnal("##STEP request")
            zhurnal(f"##FAIL request start_failed the request has a bad {plokho}: the installer is not run")
            zhurnal("##END failed")
        elif op == "adopt":
            kod = _prinyat_token(env, zhurnal)
        else:
            env_fayl = rabota / "ustanovka.env"
            with _novyy_fayl(env_fayl) as f:
                for k, v in env.items():
                    f.write(f"{k}={v}\n")
            skript = Path(project_dir) / "core" / "pochta" / "ustanovit.sh"
            try:
                proc = subprocess.Popen(
                    ["sh", str(skript), op],
                    cwd=str(rabota),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                taymer = threading.Timer(POTOLOK_SEKUND, proc.kill)
                taymer.start()
                try:
                    for stroka in proc.stdout:
                        zhurnal(stroka)
                    kod = proc.wait()
                finally:
                    taymer.cancel()
            except OSError as beda:
                zhurnal(f"##FAIL {op} start_failed cannot run the installer: {beda}")
                zhurnal("##END failed")
                kod = 1
            finally:
                env_fayl.unlink(missing_ok=True)
    itog = {"ok": kod == 0 and status == "ok", "status": status, "exit_code": kod, "at": _teper()}
    _zapisat(kuda / ITOG.format(job=job), itog)
    log(f"почта: {op} (работа {job}) — {status}")
    return itog


def obrabotat(data_dir: Path, home: Path, project_dir: Path, log=None,
              state_dir: Path | None = None) -> dict | None:
    """Забрать просьбу, если лежит. Удаляется ДО работы: упади установка — по кругу она не пойдёт."""
    put = papka(data_dir) / ZAPROS
    if not put.exists():
        return None
    try:
        prosba = json.loads(put.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prosba = {}
    put.unlink(missing_ok=True)
    if not isinstance(prosba, dict):
        return None
    return vypolnit(prosba, data_dir, home, project_dir, log=log, state_dir=state_dir)


# --- `./opencrm.sh mail …` — когда обновлятора нет ---


def _zamok(state_dir: Path):
    """Тот же замок обслуживания, что у обновлятора: установка посреди обновления — нет."""
    try:
        import fcntl
    except ImportError:
        return None
    state_dir.mkdir(parents=True, exist_ok=True)
    fayl = open(state_dir / ".zamok-obsluzhivaniya", "a")
    try:
        fcntl.flock(fayl, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fayl.close()
        raise
    return fayl


def main(argv: list[str]) -> int:
    komanda = argv[1] if len(argv) > 1 else "status"
    data_dir = Path(
        os.environ.get("OPENCRM_UPDATE_DATA_DIR")
        or Path(os.environ.get("OPENCRM_HOME") or Path.home() / "opencrm") / "data"
    )
    home = dom(data_dir)
    state_dir = Path(os.environ.get("OPENCRM_UPDATE_STATE_DIR") or Path(home) / "updates")
    project_dir = Path(__file__).resolve().parent.parent
    if komanda == "status":
        lezhit = papka(data_dir) / ZAPROS
        print(f"request: {'waiting' if lezhit.exists() else 'none'}")
        rez = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{KONTEYNER}$", "--format", "{{.Names}} {{.Status}}"],
            capture_output=True, text=True,
        )
        print(f"container: {rez.stdout.strip() or 'not installed'}")
        return 0
    if komanda not in ("install", "remove"):
        print("usage: pochta.py install|remove|status")
        return 2
    try:
        zamok = _zamok(state_dir)
    except BlockingIOError:
        print("another maintenance is running (update or backup) — try again in a minute")
        return 1
    try:
        put = papka(data_dir) / ZAPROS
        prosba = None
        if put.exists():
            try:
                prosba = json.loads(put.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prosba = None
        if isinstance(prosba, dict) and (prosba.get("op") == komanda or komanda == "install"):
            put.unlink(missing_ok=True)
            itog = vypolnit(prosba, data_dir, home, project_dir, log=print, state_dir=state_dir)
            vyvod = (papka(data_dir) / ZHURNAL.format(job=prosba.get("job_id"))).read_text(encoding="utf-8")
            print(vyvod)
            return 0 if itog and itog.get("ok") else 1
        if komanda == "install":
            print("no request from OpenCRM: start the setup in Settings → Mail server, then run this again")
            return 1
        # Удалить без просьбы: только остановить, почта остаётся на диске.
        prosba = {"job_id": 0, "op": "remove", "env": {"MAIL_DELETE_DATA": "0"}, "kto": "console"}
        itog = vypolnit(prosba, data_dir, home, project_dir, log=print, state_dir=state_dir)
        print((papka(data_dir) / ZHURNAL.format(job=0)).read_text(encoding="utf-8"))
        return 0 if itog and itog.get("ok") else 1
    finally:
        if zamok is not None:
            zamok.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
