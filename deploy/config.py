"""Настройки автообновления — из переменных окружения `OPENCRM_UPDATE_*`.

Обычный dataclass, а не Pydantic Settings как в `config/settings.py`: этот код
запускается на хосте вне контейнера и не имеет права тянуть зависимости
приложения (см. docstring пакета).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Корень чекаута: deploy/config.py → deploy/ → сам репозиторий.
PROJECT_DIR = Path(__file__).resolve().parent.parent

PREFIX = "OPENCRM_UPDATE_"


def _home(environ: dict[str, str]) -> Path:
    """Каталог состояния — тот же, что монтирует docker-compose (`OPENCRM_HOME`)."""
    raw = environ.get("OPENCRM_HOME")
    if raw:
        return Path(raw)
    return Path(environ.get("HOME", str(Path.home()))) / "opencrm"


def _iz_env_fayla(put: Path, kluch: str) -> str:
    """Значение переменной из `config/.env`. Пустая строка — не нашлось.

    Разбор свой и нарочно примитивный: демон обновления работает на хосте и не
    имеет права тянуть ни `python-dotenv`, ни настройки приложения (см. шапку
    пакета). Формат файла пишет сам установщик — `КЛЮЧ=значение`, по строке.
    """
    try:
        stroki = put.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    znachenie = ""
    for stroka in stroki:
        stroka = stroka.strip()
        if not stroka or stroka.startswith("#") or "=" not in stroka:
            continue
        imya, _, hvost = stroka.partition("=")
        if imya.strip() != kluch:
            continue
        # Последнее вхождение — рабочее: `env_set` в opencrm.sh дописывает
        # строку в конец, если ключа не было, и правит на месте, если был.
        znachenie = hvost.strip().strip('"').strip("'")
    return znachenie


def _adres_bazy(environ: dict[str, str], project_dir: Path) -> str:
    """Адрес базы, с которой РАБОТАЕТ приложение. Секрет наружу не отдаётся.

    Порядок: явная переменная обновления (ею пользуются проверки), затем
    `config/.env` — тот самый файл, который compose отдаёт контейнеру через
    `env_file`, — затем окружение самого демона.
    """
    yavno = environ.get(PREFIX + "DB_URL", "").strip()
    if yavno:
        return yavno
    iz_fayla = _iz_env_fayla(project_dir / "config" / ".env", "OPENCRM_DB_URL")
    return iz_fayla or environ.get("OPENCRM_DB_URL", "").strip()


def _imya_bazy(db_url: str) -> str:
    """Имя базы из URL — без пароля и без хоста. Пустая строка, если не MySQL.

    Имя уезжает в командную строку клиента `mysql` внутри контейнера базы,
    поэтому здесь же и отсеивается всё, что именем базы не бывает. Установщик
    пишет `opencrm`, но читаем мы файл, а не установщика: пустая строка на
    выходе означает внятный отказ отката, а не подстановку в чужую команду.
    """
    if not db_url.startswith("mysql"):
        return ""
    imya = urlsplit(db_url).path.lstrip("/").split("?")[0]
    return imya if re.fullmatch(r"[A-Za-z0-9_]+", imya) else ""


@dataclass(frozen=True)
class UpdateConfig:
    repo: str
    branch: str
    project_dir: Path
    state_dir: Path
    data_dir: Path
    db_name: str
    compose_file: Path
    health_url: str
    smoke_urls: tuple[str, ...]
    poll_seconds: int
    health_attempts: int
    health_delay: float
    build_timeout: int
    checks_timeout: int
    #: Потолок на снятие и заливку копии базы. Замерено: 2,7 млн строк — это
    #: 85 с дампа и файл на 1,2 ГБ, заливка обратно дольше. Пятиминутный потолок
    #: горячей копии SQLite здесь мал, а оборванный по таймауту дамп ничем не
    #: отличается от оборванного нехваткой места.
    snapshot_timeout: int
    run_checks: bool
    require_ci: bool
    allow_dirty: bool
    github_token: str
    telegram_token: str
    telegram_chat_id: str
    #: На чём работает база прямо сейчас. Не «лежит ли файл рядом»: после
    #: переезда на MySQL файл SQLite остаётся на месте НАРОЧНО (к нему
    #: возвращаются, если переезд не удался), и проверка по файлу отвечала бы
    #: «да» на установке, где эта база никому не нужна уже месяц.
    db_is_mysql: bool = False
    #: Имя базы в MySQL — им же зовут клиент при возврате. Пароля здесь нет и
    #: быть не должно: строка попадает в шаги обновления и в уведомления.
    mysql_db: str = ""
    #: Служба базы в compose. Клиент `mysql` живёт в её образе, а не в образе
    #: приложения, — поэтому дамп заливается заходом именно сюда.
    db_service: str = "db"

    @property
    def db_file(self) -> Path:
        return self.data_dir / self.db_name

    @property
    def history_file(self) -> Path:
        return self.state_dir / "history.jsonl"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> UpdateConfig:
        env = dict(os.environ if environ is None else environ)

        def get(name: str, default: str = "") -> str:
            return env.get(PREFIX + name, default).strip()

        def flag(name: str, default: bool) -> bool:
            raw = get(name)
            if not raw:
                return default
            return raw.lower() in {"1", "true", "yes", "on"}

        project_dir = Path(get("PROJECT_DIR") or PROJECT_DIR)
        home = _home(env)
        smoke = tuple(u.strip() for u in get("SMOKE_URLS", "http://127.0.0.1/").split(",") if u.strip())
        db_url = _adres_bazy(env, project_dir)

        return cls(
            repo=get("REPO", "DenisHumen/OpenCRM"),
            branch=get("BRANCH", "main"),
            project_dir=project_dir,
            state_dir=Path(get("STATE_DIR") or home / "updates"),
            data_dir=Path(get("DATA_DIR") or home / "data"),
            db_name=get("DB_NAME", "opencrm.db"),
            compose_file=Path(get("COMPOSE_FILE") or project_dir / "docker" / "docker-compose.yml"),
            health_url=get("HEALTH_URL", "http://127.0.0.1/healthz"),
            smoke_urls=smoke,
            poll_seconds=int(get("POLL_SECONDS", "300")),
            # Сборка образа с нуля на маленьком VPS занимает минуты, поэтому
            # ждать здоровья стоит долго: две минуты по умолчанию.
            health_attempts=int(get("HEALTH_ATTEMPTS", "30")),
            health_delay=float(get("HEALTH_DELAY", "4")),
            build_timeout=int(get("BUILD_TIMEOUT", "1800")),
            checks_timeout=int(get("CHECKS_TIMEOUT", "1800")),
            snapshot_timeout=int(get("SNAPSHOT_TIMEOUT", "3600")),
            run_checks=flag("RUN_CHECKS", True),
            # Деплоим только то, что уже позеленело в GitHub Actions. Выключать
            # осмысленно там, где Actions не настроены вовсе (форк, свой
            # gitea-зеркало): иначе гейт будет вечно ждать проверок, которых
            # никто не запустит.
            require_ci=flag("REQUIRE_CI", True),
            # Правки прямо на боевом сервере деплой затёр бы молча, поэтому по
            # умолчанию он на грязном дереве просто останавливается.
            allow_dirty=flag("ALLOW_DIRTY", False),
            github_token=get("GITHUB_TOKEN"),
            telegram_token=get("TELEGRAM_TOKEN"),
            telegram_chat_id=get("TELEGRAM_CHAT"),
            db_is_mysql=db_url.startswith("mysql"),
            mysql_db=_imya_bazy(db_url),
            db_service=get("DB_SERVICE", "db"),
        )
