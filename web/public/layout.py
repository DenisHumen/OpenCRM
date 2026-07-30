"""Модульная сетка витрины: одна композиция на любую доску.

Работы раскладываются не в колонки, а в «модуль» — заранее нарисованную
композицию с фиксированными местами. Плитки слегка **перекрывают** друг друга:
это и даёт живую, «собранную руками» страницу вместо ровных колонок. Соотношение
сторон работы при этом не сохраняется — форму задаёт место, картинка кадрируется
(``object-fit: cover``).

Композиция **одна на все размеры доски** (:data:`MASTER`, семь мест). Доска на N
работ занимает первые N мест, и места не пересчитываются: убрали работу —
оставшиеся прямоугольники сохранили и форму, и размер на экране, а композиция
просто стала у́же и встала по центру колонки. Работ больше семи — они разбиваются
на несколько модулей подряд (см. :func:`split_counts`).

Длинных работ как отдельного случая больше нет. Раньше у них была своя сетка из
колонок 1:3.4, и доска на пять лонгридов разваливалась на два модуля — вместо
композиции получалась лента. Теперь лонгрид занимает такое же место, как любая
другая работа, и показывает тот его фрагмент, который выбрал менеджер в
редакторе обрезки (``preview_focus``). Целиком работа открывается по клику.
"""

from dataclasses import dataclass

MAX_MODULE = 7

# Мастер-композиция снята с макетов (`tmp/image/template/Image_*.jpg`): семь
# мест в системе координат, где полная рамка — 1568×2064. Числа именно такие,
# какие намерены дизайном, — «округлять для красоты» их нельзя, иначе места
# перестанут совпадать с макетом.
MASTER_FRAME = (1568, 2064)

# Одна работа занимает не первое место (оно узкое и вертикальное — единственный
# кадр в нём выглядел бы обрезком), а второе, самое заметное. И ещё на 20%
# крупнее: одинокой работе незачем оставлять поля вокруг.
SINGLE_INDEX = 1
SINGLE_ZOOM = 1.2


@dataclass(frozen=True)
class Place:
    """Место под одну работу в координатах мастера (пиксели :data:`MASTER_FRAME`)."""

    x: float
    y: float
    w: float
    h: float
    z: int


@dataclass(frozen=True)
class Tile:
    """Место внутри конкретного модуля — проценты его рамки."""

    x: float
    y: float
    w: float
    h: float
    z: int


# Порядок мест = порядок работ на доске (его задаёт менеджер перетаскиванием).
MASTER: tuple[Place, ...] = (
    Place(0, 104, 452, 676, 1),
    Place(384, 0, 808, 604, 2),
    Place(980, 400, 588, 448, 4),
    Place(472, 616, 600, 808, 1),
    Place(0, 616, 628, 1032, 3),
    Place(980, 1044, 588, 604, 3),
    Place(472, 1436, 492, 628, 4),
)


# Сколько мест composition считать «первым рядом» — тем, что обязано попасть на
# первый экран целиком. Верхние три места мастера и есть та тройка, ради которой
# страница открывается; ниже уже начинается прокрутка.
FIRST_ROW = 3


def _module(size: int) -> tuple[float, float, float, tuple[Tile, ...]]:
    """Рамка и места композиции на `size` работ: `(доля ширины, ratio, ряд, места)`.

    Первая величина — какую долю полной ширины мастера занимает эта композиция.
    Именно она держит места одного размера при любом числе работ: семёрка
    занимает колонку целиком, а тройка — столько, сколько занимали бы те же три
    места внутри семёрки.

    Третья — высота первого ряда в долях **ширины самой композиции**. В таком
    виде её можно прямо поделить на свободную высоту экрана и получить предел
    ширины модуля (см. `--row` в `showcase.html`).
    """
    places = MASTER[:size] if size > 1 else (MASTER[SINGLE_INDEX],)

    left = min(p.x for p in places)
    top = min(p.y for p in places)
    width = max(p.x + p.w for p in places) - left
    height = max(p.y + p.h for p in places) - top

    tiles = tuple(
        Tile(
            x=round((p.x - left) / width * 100, 2),
            y=round((p.y - top) / height * 100, 2),
            w=round(p.w / width * 100, 2),
            h=round(p.h / height * 100, 2),
            z=p.z,
        )
        for p in places
    )
    span = width / MASTER_FRAME[0] * (SINGLE_ZOOM if size == 1 else 1)
    row = (max(p.y + p.h for p in places[:FIRST_ROW]) - top) / width
    return round(span, 4), round(width / height, 4), round(row, 4), tiles


_LAYOUTS = {size: _module(size) for size in range(1, MAX_MODULE + 1)}
MODULES: dict[int, tuple[float, tuple[Tile, ...]]] = {
    size: (ratio, tiles) for size, (_span, ratio, _row, tiles) in _LAYOUTS.items()
}
MODULE_SPANS: dict[int, float] = {
    size: span for size, (span, _ratio, _row, _tiles) in _LAYOUTS.items()
}
MODULE_ROWS: dict[int, float] = {
    size: row for size, (_span, _ratio, row, _tiles) in _LAYOUTS.items()
}


def split_counts(total: int) -> list[int]:
    """Разбивает работы на модули по 1–7 штук.

    Остаток в одну-две работы выглядел бы обрывком страницы, поэтому такой
    хвост добирается из предыдущего модуля: 8 → 4+4, 9 → 5+4, 15 → 7+4+4.
    """
    if total <= 0:
        return []
    if total <= MAX_MODULE:
        return [total]

    chunks: list[int] = []
    rest = total
    while rest > MAX_MODULE:
        chunks.append(MAX_MODULE)
        rest -= MAX_MODULE
    if rest == 0:
        return chunks
    if rest >= 3:
        chunks.append(rest)
        return chunks

    merged = chunks.pop() + rest
    chunks.append(merged - merged // 2)
    chunks.append(merged // 2)
    return chunks


def cropped_indexes(works: list[dict]) -> set[int]:
    """Номера работ, которые в своё место целиком не помещаются.

    Такой работе нужна подсказка «открыть целиком» и размытие на срезе — по
    самому кадру не видно, что под ним есть продолжение. Порог тот же, по
    которому производные ужимаются по ширине (`media_service.LONG_RATIO`).
    """
    from core.services.media_service import is_long_image

    return {
        index
        for index, work in enumerate(works)
        if work.get("kind") == "image" and is_long_image(work.get("width"), work.get("height"))
    }


def build_modules(count: int) -> list[dict]:
    """Готовая раскладка для шаблона: список модулей с плитками по порядку работ.

    Каждая вторая композиция отражается по горизонтали — на длинной доске
    одинаковые модули подряд читались бы как повтор шаблона.
    """
    modules: list[dict] = []
    cursor = 0
    for position, size in enumerate(split_counts(count)):
        ratio, tiles = MODULES[size]
        mirrored = position % 2 == 1
        modules.append(
            {
                "ratio": ratio,
                "span": MODULE_SPANS[size],
                "row": MODULE_ROWS[size],
                "tiles": [
                    {
                        "index": cursor + offset,
                        "x": round(100 - tile.x - tile.w, 2) if mirrored else tile.x,
                        "y": tile.y,
                        "w": tile.w,
                        "h": tile.h,
                        "z": tile.z,
                    }
                    for offset, tile in enumerate(tiles)
                ],
            }
        )
        cursor += size
    return modules


def place_ratios(count: int) -> list[float]:
    """Отношение сторон места каждой работы — по порядку работ на доске.

    Нужно редактору обрезки: рамка фрагмента должна быть той же формы, что и
    место работы на витрине, иначе менеджер выбирает не то, что увидит клиент.
    """
    ratios: list[float] = []
    for module in build_modules(count):
        for tile in module["tiles"]:
            ratios.append(round(tile["w"] / tile["h"] * module["ratio"], 4))
    return ratios
