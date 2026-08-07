from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from config.settings import get_settings
from database import models  # noqa: F401 — регистрирует таблицы в metadata
from database import types  # noqa: F401 — правила переносимости типов (DATETIME(6) в MySQL)
from database.session import Base

config = context.config
# Настройку логирования из alembic.ini применяем осторожно и не всегда.
#
# `fileConfig` по умолчанию ГЛУШИТ все логгеры, созданные до него. Из отдельного
# процесса (`python -m alembic upgrade head` в entrypoint) это безобидно: кроме
# alembic там никого и нет. А вот вызов миграций изнутри приложения — так делают
# тесты, и так однажды сделает автоматическое обновление на старте — заодно
# выключал `opencrm.events` и всё остальное: ошибка подписчика переставала
# доходить до журнала, и «наблюдатель упал молча» становилось правдой.
#
# Поймано обратным прогоном набора: тесты про упавшего наблюдателя падали не
# сами по себе, а после того, как соседний тест прогнал миграции.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Адрес базы — из настроек приложения, но только если звавший не назвал свой.
#
# Раньше строка стояла безусловной и затирала любой переданный адрес. Снаружи
# это выглядело так: просишь alembic обновить одну базу, он молча обновляет ту,
# что указана в переменных окружения. На сервере с несколькими окружениями
# такая подмена стоит дорого и замечается не сразу.
#
# Сначала спрашиваем звавшего (`-x url=...`, `config.set_main_option` из кода),
# потом alembic.ini, и лишь затем настройки приложения — обычный порядок «явное
# важнее умолчания».
_asked = context.get_x_argument(as_dictionary=True).get("url")
if _asked:
    config.set_main_option("sqlalchemy.url", _asked)
elif not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # ALTER TABLE в SQLite
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
