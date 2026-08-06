"""Граница базы: запросы живут только в `database/`.

**Правило одно: за пределами `database/` никто не называет таблицу или колонку
в запросе.** Ни `select(...)`, ни `db.execute`, ни `db.scalar`, ни
`db.get(Модель, id)`. Сервис просит репозиторий и получает объекты.

Почему граница проведена именно здесь, а не по слову «сессия». `db.add(объект)`,
`db.delete(объект)`, `db.flush()` сервису оставлены: они работают с записью,
которая у него уже на руках, своего SQL не сочиняют и от движка не зависят.
Запрос — сочиняет. Именно запрос переписывают при переезде на MySQL, именно в
запросе живут смещение страницы, шаблоны в LIKE, регистр и часовой пояс. Пока
запросы разбросаны по двадцати сервисам, каждая такая починка — это двадцать
мест и надежда, что ни одно не забыли.

Проверка механическая нарочно. Договорённость, которую стережёт только
внимательность, живёт до первого спешащего дня: следующий блок допишут прямо в
сервисе, потому что так на две минуты быстрее, — и граница, ради которой всё
затевалось, перестанет существовать молча.

Список исключений ниже — не «пока не дошли руки», а места, где обойти границу
правильно. Каждое названо и объяснено. Добавлять в него можно, но с
объяснением: строка без причины здесь и есть та самая тихая эрозия.
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Сессия умеет и то, что порождает запрос от имени вызывающего.
QUERY_METHODS = {"execute", "scalar", "scalars", "query", "get"}

#: Имена, которые собирают запрос.
QUERY_BUILDERS = {"select", "update", "delete", "insert", "text", "exists", "union", "union_all"}

#: Где границу обходить можно — и почему.
ALLOWED = {
    # Точка входа приложения проверяет, что схема на месте, ДО того как
    # появится хоть один репозиторий: `inspect(engine)` и `SELECT 1` — это
    # разговор с движком, а не с предметной областью.
    "web/main.py",
    # Скрипты обслуживания запускаются руками, поштучно и мимо приложения:
    # перенос на MySQL по определению работает с чужой схемой, а сброс root'а
    # чинит базу, в которую иначе не войти.
    "scripts/migrate_to_mysql.py",
    "scripts/reset_root.py",
    "scripts/reprocess_media.py",
    "scripts/purge_deleted.py",
    # Общая вставка строки с уникальным полем: она не знает, какую таблицу
    # вставляет, — объект приходит готовым от вызывающего.
    "core/uniqueness.py",
}


def _sources():
    for area in ("core", "web", "config", "scripts", "deploy"):
        for path in (ROOT / area).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED:
                continue
            yield rel, path


def _offences(rel: str, path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    builders = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            for alias in node.names:
                if alias.name in QUERY_BUILDERS:
                    builders.add(alias.asname or alias.name)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in builders:
            found.append(f"{rel}:{node.lineno}  {func.id}(...) — запрос собран вне database/")
        if (
            isinstance(func, ast.Attribute)
            and func.attr in QUERY_METHODS
            and isinstance(func.value, ast.Name)
            and func.value.id in {"db", "session"}
        ):
            found.append(f"{rel}:{node.lineno}  {func.value.id}.{func.attr}(...) — запрос вне database/")
    return found


def test_zaprosy_zhivut_tolko_v_database():
    guilty = [line for rel, path in _sources() for line in _offences(rel, path)]
    assert guilty == [], (
        "Запрос собран за пределами database/. Перенесите его в репозиторий "
        "(зачем — в докстроке этого файла):\n" + "\n".join(guilty)
    )


def test_spisok_isklyucheniy_ne_protuh():
    """Исключение, указывающее в пустоту, — приглашение обойти границу молча."""
    missing = [rel for rel in ALLOWED if not (ROOT / rel).exists()]
    assert missing == [], f"в списке исключений файлы, которых больше нет: {missing}"


@pytest.mark.parametrize("area", ["core", "web"])
def test_modeli_ne_importiruyutsya_radi_zaprosa(area):
    """Модель в сервисе — это тип и поля, а не таблица.

    Проверка мягкая нарочно: импорт модели законен (её создают, у неё читают
    поля, ею аннотируют), и запрещать его значило бы запрещать работу с
    записями. Ловим только то, ради чего граница и проведена, — сборку запроса;
    этим занят тест выше. Здесь же просто закрепляем, что `database.query`
    — общий слой запросов — снаружи не зовут: его дело обслуживать репозитории.
    """
    guilty = []
    for path in (ROOT / area).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "database.query":
                guilty.append(f"{rel}:{node.lineno}")
    assert guilty == [], "database.query — для репозиториев, не для сервисов:\n" + "\n".join(guilty)
