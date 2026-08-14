from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    url = settings.db_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("mysql"):
        # Потолок соединений на ПРОЦЕСС, а не на систему, — и считать надо
        # именно так. Пул заводится в каждом рабочем процессе отдельно, а
        # переезд на MySQL как раз и разрешает их несколько: при умолчаниях
        # SQLAlchemy (5 + 10 сверх) восемь воркеров дают до 120 соединений
        # против `max_connections=151` у mysql:8.0 из коробки. Оставшийся
        # десяток съедают разовые заходы — снятие дампа, `alembic`, сверка
        # переезда, — и упирается в потолок не приложение, а тот, кто пришёл
        # чинить: «Too many connections» ровно в момент разбора аварии.
        #
        # Пять и пять: восемь воркеров укладываются в 80, запас остаётся всем.
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 5
        # READ COMMITTED вместо REPEATABLE READ, и это не тонкая настройка, а
        # условие правильности.
        #
        # Приём «вставили, получили отказ, перечитали — правда ли занято»
        # (core/uniqueness.py) держит на себе четыре места: регистрация,
        # название должности, артикул склада, номер бланка. Перечитывание
        # обязано увидеть соседа, который только что зафиксировал свою строку.
        #
        # При REPEATABLE READ оно его НЕ ВИДИТ: снимок транзакции сделан до
        # чужой фиксации, и повторное чтение отдаёт прежнюю картину. Значит
        # `taken` отвечает «свободно», отказ базы уходит наверх как чужая
        # поломка, и человек получает 500 там, где система отработала верно.
        #
        # Поймано делом: пока набор тестов гонялся на файловой базе, проверка
        # двойного нажатия «Запросить доступ» проходила, а на MySQL падала с
        # `Duplicate entry ... for key users.ix_users_email`. То есть на боевом
        # сервере это уже работало именно так, и не видел этого только набор.
        kwargs["isolation_level"] = "READ COMMITTED"
    engine = create_engine(url, **kwargs)

    if url.startswith("mysql"):
        @event.listens_for(engine, "connect")
        def _prepare_mysql(dbapi_connection, _record):
            """Часовой пояс сессии — UTC. Это не настройка, а условие правильности.

            Весь проект хранит **наивный UTC**: `now_utc()` пишет время без
            смещения, и колонки объявлены `DateTime` без часового пояса.

            А `NOW()` в MySQL возвращает **местное время сессии**. Оставь мы это
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

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Session:
    """Новая сессия БД (для фоновых задач и скриптов). В веб-слое — через Depends."""
    return SessionLocal()


@contextmanager
def tochka_otkata(db: Session):
    """Точка отката: вложенная транзакция вокруг записи, которая может не выйти.

    Ставится там и только там, где сейчас будет запись, чей отказ надо поймать,
    не потеряв остальную транзакцию: приём «вставили, получили отказ, перечитали»
    (`core/uniqueness.py`), приём почты, события АТС.
    """
    with db.begin_nested() as savepoint:
        yield savepoint
