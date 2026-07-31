"""Настройки автообновления — из переменных окружения `OPENCRM_UPDATE_*`.

Обычный dataclass, а не Pydantic Settings как в `config/settings.py`: этот код
запускается на хосте вне контейнера и не имеет права тянуть зависимости
приложения (см. docstring пакета).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Корень чекаута: deploy/config.py → deploy/ → сам репозиторий.
PROJECT_DIR = Path(__file__).resolve().parent.parent

PREFIX = "OPENCRM_UPDATE_"


def _home(environ: dict[str, str]) -> Path:
    """Каталог состояния — тот же, что монтирует docker-compose (`OPENCRM_HOME`)."""
    raw = environ.get("OPENCRM_HOME")
    if raw:
        return Path(raw)
    return Path(environ.get("HOME", str(Path.home()))) / "opencrm"


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
    run_checks: bool
    allow_dirty: bool
    github_token: str
    telegram_token: str
    telegram_chat_id: str

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
            run_checks=flag("RUN_CHECKS", True),
            # Правки прямо на боевом сервере деплой затёр бы молча, поэтому по
            # умолчанию он на грязном дереве просто останавливается.
            allow_dirty=flag("ALLOW_DIRTY", False),
            github_token=get("GITHUB_TOKEN"),
            telegram_token=get("TELEGRAM_TOKEN"),
            telegram_chat_id=get("TELEGRAM_CHAT"),
        )
