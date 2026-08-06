"""Сходится ли схема живой базы с тем, что описано моделями.

**Зачем это существует.** Схему в проекте строят двое: `alembic upgrade head` (так
делает docker/entrypoint.sh на сервере) и `Base.metadata.create_all` (так
поднимается база при запуске без контейнера). Расходятся они молча, и цена
расхождения не одинакова:

- в модели появилась **таблица**, а миграцию забыли — `create_all` создаст её на
  разработке, всё будет работать, а на сервере таблицы не будет, и раздел
  ответит 500. Обратный случай не лучше: таблица, созданная `create_all`, потом
  ловит от миграции «table already exists», и обновление встаёт колом;
- в модели появилась **колонка** — `create_all` не добавляет колонок вовсе.
  Расхождение не заметит никто, пока к колонке не обратятся.

Оба случая уже случались в этом проекте, второй — на живой базе разработки.

**Чего этот модуль НЕ делает.** Он не чинит и не мигрирует: он отвечает на
вопрос «сходится ли», а решение принимает вызывающий. На старте приложения
несхождение — повод не подниматься (см. `web/main.py`): обновление увидит
провалившийся health-check и откатится, а это ровно то поведение, которое нужно.

Сравниваем осторожно. Ловим то, от чего запросы падают, — **нехватку**: нет
таблицы, нет колонки. Лишнее (таблица или колонка, оставшаяся от отката) не
мешает ни одному запросу и в отказ не превращается: превратить его в отказ
значит запретить откат на предыдущую версию, а откат — главный способ починки.

Типы колонок не сверяем намеренно. SQLite хранит объявленный тип строкой и
трактует его свободно, MySQL приводит к своим; сравнение строк типов дало бы
поток ложных тревог («VARCHAR(200)» против «VARCHAR» против «TEXT») на ровном
месте. Несовпадение типа ловит `tests/test_schema_conventions.py` — там сравнение
идёт с настоящим автогенератором alembic и на пустой базе, где оно осмысленно.
"""

from dataclasses import dataclass, field

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from database.session import Base


@dataclass(frozen=True)
class SchemaReport:
    """Что нашли. Пустые списки — схема на месте."""

    #: Таблиц, описанных моделями, нет в базе. Любой запрос к ним падает.
    missing_tables: tuple[str, ...] = ()
    #: Колонок нет: «таблица.колонка». Падает всякий запрос, который их называет.
    missing_columns: tuple[str, ...] = ()
    #: Лишнее в базе — обычно след отката. Не мешает, но сказать о нём стоит.
    extra_tables: tuple[str, ...] = ()
    extra_columns: tuple[str, ...] = ()
    #: Версия миграций, на которой стоит база. None — таблицы `alembic_version`
    #: нет вовсе, то есть схему поднимал `create_all` и не отметился.
    revision: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Схема годна к работе.

        Именно «годна», а не «идеальна»: лишнее не мешает, нехватка — мешает.
        """
        return not self.missing_tables and not self.missing_columns

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "revision": self.revision,
            "missing_tables": list(self.missing_tables),
            "missing_columns": list(self.missing_columns),
            "extra_tables": list(self.extra_tables),
            "extra_columns": list(self.extra_columns),
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        """Одной строкой — для журнала и для сообщения об отказе."""
        if self.ok:
            extra = len(self.extra_tables) + len(self.extra_columns)
            tail = f", лишнего: {extra}" if extra else ""
            return f"схема сходится (миграция {self.revision or 'не отмечена'}{tail})"
        parts = []
        if self.missing_tables:
            parts.append("нет таблиц: " + ", ".join(self.missing_tables))
        if self.missing_columns:
            parts.append("нет колонок: " + ", ".join(self.missing_columns))
        return "; ".join(parts)


#: Таблицы, которых в моделях нет и быть не должно.
#:
#: `alembic_version` ведёт сам alembic. Не назови её здесь — она попадёт в
#: «лишние» на каждой проверке и приучит не читать этот список.
KNOWN_FOREIGN = frozenset({"alembic_version"})


def check(engine: Engine) -> SchemaReport:
    """Сверить живую схему с моделями. Ничего не меняет."""
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    model_tables = set(Base.metadata.tables)

    missing_tables = sorted(model_tables - live_tables)
    extra_tables = sorted(live_tables - model_tables - KNOWN_FOREIGN)

    missing_columns: list[str] = []
    extra_columns: list[str] = []
    for name in sorted(model_tables & live_tables):
        described = {column["name"] for column in inspector.get_columns(name)}
        expected = {column.name for column in Base.metadata.tables[name].columns}
        missing_columns += [f"{name}.{c}" for c in sorted(expected - described)]
        extra_columns += [f"{name}.{c}" for c in sorted(described - expected)]

    return SchemaReport(
        missing_tables=tuple(missing_tables),
        missing_columns=tuple(missing_columns),
        extra_tables=tuple(extra_tables),
        extra_columns=tuple(extra_columns),
        revision=current_revision(engine),
    )


def current_revision(engine: Engine) -> str | None:
    """На какой миграции стоит база. None — не отмечена вовсе."""
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    from sqlalchemy import text

    with engine.connect() as connection:
        row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def is_empty(engine: Engine) -> bool:
    """Пустая ли база — то есть её ещё никто не строил.

    Отличать пустую от населённой обязательно: пустую можно поднять `create_all`
    и отметить как «на последней миграции», а населённую — только провести
    миграциями. `create_all` на населённой базе не добавит колонок и потому
    оставит расхождение, которое обнаружится первым же запросом к новому полю.

    Пустой считаем базу вообще без таблиц. Одна `alembic_version` без прочих —
    тоже пустая: так выглядит база, где миграции сорвались на первой же.
    """
    tables = set(inspect(engine).get_table_names())
    return not (tables - KNOWN_FOREIGN)
