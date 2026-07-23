from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    url = settings.db_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # каталог для файла БД должен существовать
        db_path = url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _prepare_sqlite(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
            # Встроенные lower/upper в SQLite работают только с ASCII: lower('Брусника')
            # возвращает строку без изменений, из-за чего поиск по русским именам
            # становится регистрозависимым (ilike SQLAlchemy эмулирует через lower()).
            # Подменяем их Python-реализациями, знающими Unicode.
            dbapi_connection.create_function("lower", 1, _unicode_lower, deterministic=True)
            dbapi_connection.create_function("upper", 1, _unicode_upper, deterministic=True)
    return engine


def _unicode_lower(value):
    return value.lower() if isinstance(value, str) else value


def _unicode_upper(value):
    return value.upper() if isinstance(value, str) else value


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Session:
    """Новая сессия БД (для фоновых задач и скриптов). В веб-слое — через Depends."""
    return SessionLocal()
