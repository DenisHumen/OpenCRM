from contextlib import contextmanager

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import get_settings


class Base(DeclarativeBase):
    pass


#: Сколько соединений к базе безопасно занять ВСЕМ приложением сразу.
#:
#: У mysql:8.0 из коробки `max_connections=151`. Запас оставлен на разовые заходы
#: (дамп, `alembic`, сверка схемы, метрики): упирается в потолок не приложение, а
#: тот, кто пришёл чинить, — «Too many connections» в момент разбора аварии.
BYUDZHET_SOEDINENIY = 100

#: Наименьшее число одновременных обработчиков, ниже которого опускаться нельзя.
#:
#: Восемь — это «страница с картинками грузится, и в это время можно ещё
#: работать». Меньше — и одна пачка плиток занимает приложение целиком.
MINIMUM_ODNOVREMENNYH = 8


def razmer_pula(workers: int) -> tuple[int, int]:
    """`pool_size` и `max_overflow` на ОДИН рабочий процесс.

    Считаем от бюджета и числа воркеров, а не берём числом: числом оно уже стояло —
    и вразрез с одновременностью (что стояло и насколько мимо — docs/03-database.md).
    Делим пополам: за постоянно открытое соединение база платит памятью.
    """
    workers = max(1, int(workers or 1))
    na_vorkera = max(MINIMUM_ODNOVREMENNYH, BYUDZHET_SOEDINENIY // workers)
    postoyanno = na_vorkera // 2
    return postoyanno, na_vorkera - postoyanno


def predel_odnovremennyh(workers: int) -> int:
    """Сколько запросов приложению позволено выполнять разом в одном процессе.

    Ровно столько, сколько у процесса соединений к базе. Больше — лишние
    обработчики ждут `pool_timeout` и отдают пятисотую вместо медленного ответа;
    меньше — соединения простаивают.
    """
    postoyanno, sverh = razmer_pula(workers)
    return postoyanno + sverh


def _make_engine():
    settings = get_settings()
    url = settings.db_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("mysql"):
        # Соединений не меньше, чем одновременных обработчиков: сорок ручек в потоках
        # anyio на пул в десять дали 24.08.2026 `QueuePool limit` и пятисотые. Держит
        # `web.main`, сверяет `tests/test_pool_sizing.py`, разбор — docs/03-database.md.
        kwargs["pool_size"], kwargs["max_overflow"] = razmer_pula(settings.workers)
        # READ COMMITTED — условие правильности, а не настройка. Приём «вставили, получили
        # отказ, перечитали» (core/uniqueness.py) обязан увидеть чужую фиксацию: под
        # REPEATABLE READ не видит и бьёт пятисоткой. Поймано делом — docs/03-database.md.
        kwargs["isolation_level"] = "READ COMMITTED"
    engine = create_engine(url, **kwargs)

    if url.startswith("mysql"):
        @event.listens_for(engine, "connect")
        def _prepare_mysql(dbapi_connection, _record):
            """Часовой пояс сессии — UTC. Это не настройка, а условие правильности.

            Проект хранит наивный UTC, а `NOW()` отдаёт местное время сессии: время от Python
            и от `func.now()` разъехалось бы молча — в колонке голое число без пояса. Строка
            на соединение закрывает все колонки; по одной не чинят: забытая не проявится ничем.
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
    не потеряв остальную транзакцию: `core/uniqueness.py`, приём почты, АТС.
    """
    with db.begin_nested() as savepoint:
        yield savepoint


#: Ключ, под которым сессия помнит взятые ею именованные замки.
ZAMKI = "imenovannye_zamki"


def zapomnit_zamok(db: Session, imya: str) -> None:
    """Записать взятый замок, чтобы граница запроса его сняла.

    Именованный замок MySQL держится за СОЕДИНЕНИЕМ: ни `COMMIT`, ни `ROLLBACK`, ни
    `Session.close()` — та возвращает соединение в пул, а не закрывает — его не снимают.
    Забытый уезжает в пул вместе с соединением и заставляет следующего ждать впустую.
    """
    db.info.setdefault(ZAMKI, []).append(imya)


def snyat_zamki(db: Session) -> None:
    """Снять всё, что сессия успела занять. Зовётся на границе запроса.

    ПОСЛЕ фиксации, а не до: снятый до `COMMIT` замок пускает соперника на пустоту —
    дуэлью проверено, форма с сайта заводила две карточки. Отказ проглочен: в `finally`
    своё исключение скрыло бы причину, а забытый замок MySQL снимет при обрыве соединения.
    """
    imena = db.info.pop(ZAMKI, None)
    if not imena:
        return
    for imya in imena:
        try:
            db.execute(select(func.release_lock(imya)))
        except Exception:  # noqa: BLE001 — снятие замка не должно прятать причину отказа
            pass
