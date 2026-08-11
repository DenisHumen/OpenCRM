from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
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

    if url.startswith("mysql"):
        @event.listens_for(engine, "connect")
        def _prepare_mysql(dbapi_connection, _record):
            """Часовой пояс сессии — UTC. Это не настройка, а условие правильности.

            Весь проект хранит **наивный UTC**: `now_utc()` пишет время без
            смещения, и колонки объявлены `DateTime` без часового пояса. В
            SQLite `func.now()` (то есть `CURRENT_TIMESTAMP`) тоже даёт UTC, и
            всё сходится само.

            В MySQL `NOW()` возвращает **местное время сессии**. Оставь мы это
            как есть — часть времён легла бы UTC (те, что пишет Python), часть
            местным (те, что ставит `server_default=func.now()` и `onupdate`), и
            **различить их постфактум было бы нечем**: в колонке лежит голое
            число без пояса. Journал действий, время движения склада, отметка
            «когда клиент позвонил» — всё это разъехалось бы на величину
            смещения и молча.

            Одна строка на соединение закрывает разом все двадцать пять колонок
            с `func.now()` и все `onupdate` — переписывать каждую было бы и
            дороже, и ненадёжнее: забытая колонка не проявляется ничем.
            """
            cursor = dbapi_connection.cursor()
            cursor.execute("SET time_zone = '+00:00'")
            cursor.close()

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _prepare_sqlite(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            # Сколько ждать своей очереди на запись. У драйвера по умолчанию
            # пять секунд, и этого не хватало: писатель в SQLite ровно один, а
            # запрос успевает подержать блокировку дольше, чем кажется —
            # загрузка файла на доску и звонок на АТС случаются внутри запроса.
            # Дождавшийся своей очереди медленнее того, кто не ждал; не
            # дождавшийся отвечает пятисоткой, и человек теряет введённое.
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()
            _postavit_unicode_registr(dbapi_connection)
    return engine


def _postavit_unicode_registr(dbapi_connection) -> None:
    """Подменить lower/upper на понимающие Unicode — на голом соединении sqlite3.

    Встроенные в SQLite работают только с ASCII: `lower('Брусника')` возвращает
    строку без изменений, из-за чего поиск по русским именам становился
    регистрозависимым (`ilike` SQLAlchemy эмулирует через `lower()`).
    """
    dbapi_connection.create_function("lower", 1, _unicode_lower, deterministic=True)
    dbapi_connection.create_function("upper", 1, _unicode_upper, deterministic=True)


def unicode_registr_dlya(bind) -> None:
    """То же самое, но для ЧУЖОГО соединения — прежде всего для миграций.

    `database/migrations/env.py` строит свой движок через `engine_from_config`,
    а не через `_make_engine`, поэтому подписчика на `connect` там нет и
    подмены нет тоже: `lower()` в миграции знает только ASCII. Миграция,
    заполняющая нормализованную колонку одним `UPDATE` с `lower()`, оставила бы
    «БРУСНИКУ» заглавной на всей уже населённой базе — и карточка перестала бы
    находиться молча, ровно на том обновлении, ради которого всё делается.

    Регистрируются ТОЛЬКО функции. Цеплять `_prepare_sqlite` целиком к движку
    миграций нельзя: там же стоит `PRAGMA foreign_keys=ON`, а alembic в
    batch-режиме пересоздаёт таблицы — включённые внешние ключи ломают этот
    приём.

    На не-SQLite не делает ничего: в MySQL `LOWER()` знает Unicode сам.
    """
    if bind.dialect.name != "sqlite":
        return
    _postavit_unicode_registr(bind.connection.driver_connection)


def _unicode_lower(value):
    return value.lower() if isinstance(value, str) else value


def _unicode_upper(value):
    return value.upper() if isinstance(value, str) else value


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Session:
    """Новая сессия БД (для фоновых задач и скриптов). В веб-слое — через Depends."""
    return SessionLocal()


@contextmanager
def tochka_otkata(db: Session):
    """Точка отката, объявляющая намерение писать.

    Обычная `db.begin_nested()` плюс одна строка перед ней — и эта строка лечит
    `database is locked`, то есть пятисотку на пустом месте при двух
    одновременных запросах.

    **Как заводится беда.** Драйвер SQLite не открывает транзакцию перед
    `SELECT` — чтения идут вне транзакции, и на обычном пути «прочитали,
    записали» повышаться нечему. Но `SAVEPOINT` транзакцию открывает, причём
    отложенную: без блокировок. Дальше внутри неё случается чтение, транзакция
    получает снимок базы, а следующая за ним запись обязана повыситься до
    писателя. Если сосед за это время что-то записал, повышение невозможно —
    и SQLite отвечает «занято» **немедленно**, не выжидая `busy_timeout`: ждать
    бессмысленно, ни одна сторона не уступит. Поэтому беда и не лечилась
    таймаутом: таймаут тут ни при чём.

    **Почему замок ставится именно здесь.** Сначала казалось, что правильная
    форма — `BEGIN IMMEDIATE` на весь пишущий запрос, благо метод HTTP-запроса
    прямо говорит, пишет он или нет. Так делать нельзя: блокировка держалась бы
    и во время загрузки файла на доску, и во время звонка на АТС в
    `click_to_call` — то есть все записи в CRM ждали бы чужой медленной сети.
    Проверено и на тестах: два из них, изображающие гонку двумя сессиями в один
    поток, сразу встали намертво.

    Замок нужен не на запрос, а ровно перед записью — и размечать для этого
    ничего не пришлось: **сама точка отката и есть разметка**. Её ставят там и
    только там, где сейчас будет запись, которая может столкнуться с чужой.

    Проверено на настоящем uvicorn: четыре потока шлют события АТС с одним
    `call_id`. До починки — `sqlite3.OperationalError: database is locked`,
    после — все 48 запросов проходят.
    """
    _vzyat_zamok_zapisi(db)
    with db.begin_nested() as savepoint:
        yield savepoint


def vzyat_zamok_zapisi(db: Session) -> None:
    """То же намерение писать, но без точки отката (см. `tochka_otkata`)."""
    _vzyat_zamok_zapisi(db)


def _vzyat_zamok_zapisi(db: Session) -> None:
    if engine.dialect.name != "sqlite":
        # У MySQL блокировки строк и настоящее ожидание вместо немедленного
        # отказа — этого поведения там нет вовсе.
        return
    # Транзакция уже открыта — значит запись в ней либо уже была (мы и так
    # писатель), либо замок взят выше. Второй BEGIN был бы ошибкой.
    if db.connection().connection.driver_connection.in_transaction:
        return
    db.execute(text("BEGIN IMMEDIATE"))
