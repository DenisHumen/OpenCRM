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
    # Проверка резервной копии открывает ЧУЖОЙ файл — снятый месяц назад, с
    # прежней схемой, возможно битый. Репозитории здесь не годятся вдвойне: они
    # ходят в базу приложения через общую сессию, а этому коду нужен другой
    # файл и голый sqlite3 без моделей, иначе копия со старой схемой уронила бы
    # проверку вместо того, чтобы быть проверенной.
    "scripts/verify_backup.py",
    # Копия базы перед миграциями снимается с ЖИВОЙ схемы, а не с моделей, и в
    # том её смысл: код в этот момент уже новый, а база ещё старая. `SHOW
    # TABLES`, `SHOW CREATE TABLE` и `SELECT *` по имени таблицы — разговор с
    # движком о том, что в нём лежит; репозиторий на такой вопрос ответить не
    # может по определению, он знает только сегодняшние модели.
    "scripts/snapshot_db.py",
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


#: Поля, из которых собирается поисковая склейка `search_text`.
#:
#: Пересчитывают её мэппер-события на моделях (`database/models/client.py`,
#: `database/models/deal.py`), и мимо них проходит ровно одно — массовый
#: `update()` по таблице: он уезжает в базу одним запросом, объектов не трогает
#: и ни одного события не зовёт.
SKLEYKA = {
    "Client": {"name", "company", "phone", "phone_norm", "email", "tags"},
    "Deal": {"title", "description"},
}

#: Массовые правки, про которые известно, что текста они не трогают.
#:
#: Названы поимённо, потому что состав полей у них приходит словарём из сервиса
#: и по тексту его не прочитать. Появится второй такой — набор упадёт, и его
#: придётся либо объяснить здесь, либо переписать через объект.
SPLAT_RAZRESHYON = {
    # Смена этапа условием «пока он тот, что прочитали». Словарь собирает
    # `core/services/deal_service.py`: `stage`, `closed_at`, `lost_reason`,
    # `sort_order` — ни названия, ни описания там нет и быть не может.
    "database/repositories/deals.py:take_stage",
}


def _update_sites(path: pathlib.Path):
    """Все цепочки `update(Модель)....values(...)` в файле, с именем функции."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    vladelets: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                vladelets.setdefault(id(inner), node.name)

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "values"):
            continue
        tsel = node.func.value
        while isinstance(tsel, ast.Call) and not (
            isinstance(tsel.func, ast.Name) and tsel.func.id == "update"
        ):
            tsel = tsel.func.value if isinstance(tsel.func, ast.Attribute) else None
            if tsel is None:
                break
        if not (isinstance(tsel, ast.Call) and isinstance(tsel.func, ast.Name)):
            continue
        if not (tsel.args and isinstance(tsel.args[0], ast.Name)):
            continue
        yield node, tsel.args[0].id, vladelets.get(id(node), "<модуль>")


def test_massovyy_update_ne_obkhodit_pereschyot():
    """Массовая правка полей, входящих в склейку, оставила бы её неправдой.

    Склейку `search_text` пересчитывают мэппер-события на моделях, и мимо них
    проходит ровно одно: `session.execute(update(Модель).values(...))` уезжает в
    базу одним запросом, объектов не трогает и ни одного события не зовёт.

    Цена тихая и оттого дорогая: ошибки нет, запись прошла, а карточка
    перестала находиться поиском — и узнать об этом можно только от человека,
    который её искал.

    Словарь через `**` тоже считается нарушением: что в нём лежит, по тексту не
    видно, а «наверное, ничего такого» — это ровно тот способ рассуждать,
    которым беды и заводятся. Такие места называются поимённо в
    `SPLAT_RAZRESHYON`.
    """
    repo_dir = ROOT / "database" / "repositories"
    guilty = []
    for path in sorted(repo_dir.glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for node, model, funktsiya in _update_sites(path):
            zapreshcheno = SKLEYKA.get(model)
            if not zapreshcheno:
                continue
            tronuto = {kw.arg for kw in node.keywords if kw.arg} & zapreshcheno
            if tronuto:
                guilty.append(f"{rel}:{node.lineno}  update({model}) правит {sorted(tronuto)}")
            splat = any(kw.arg is None for kw in node.keywords)
            if splat and f"{rel}:{funktsiya}" not in SPLAT_RAZRESHYON:
                guilty.append(
                    f"{rel}:{node.lineno}  update({model}).values(**…) — что там, "
                    f"неизвестно; назовите место в SPLAT_RAZRESHYON"
                )
    assert guilty == [], (
        "Массовый update правит поля поисковой склейки мимо пересчёта — "
        "карточка перестанет находиться молча:\n" + "\n".join(guilty)
    )


def test_storozh_massovogo_update_deystvitelno_smotrit():
    """Сторож выше обязан что-то находить, иначе он сторожит пустоту.

    Проверка на самого себя: разбор AST легко сломать так, что цепочка
    `update(...).values(...)` перестанет опознаваться вовсе, — и тест станет
    вечнозелёным, ничего не проверяя.
    """
    deals = ROOT / "database" / "repositories" / "deals.py"
    naydeno = {(model, funktsiya) for _node, model, funktsiya in _update_sites(deals)}
    assert ("Deal", "take_stage") in naydeno, (
        f"сторож не видит единственный массовый update в проекте: {naydeno}"
    )


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
