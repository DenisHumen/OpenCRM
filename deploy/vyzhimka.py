"""Выбрать из вывода команды то, ради чего его вообще читают.

**Отчего это понадобилось.** Отчёт о неудаче показывал хвост вывода —
последние двенадцать строк. Довод звучал разумно: беда в хвосте, начало
одинаково у всех заходов. На `docker build` это работало. На `docker compose
up` — не работает НИКОГДА, и вот почему.

`Result.text` склеивает `out` и `err` в таком порядке. Компоуз пишет ход
контейнеров в поток ошибок, а pytest внутри контейнера — в обычный вывод.
Значит после склейки ВЕСЬ вывод pytest оказывается выше ВСЕГО хода
контейнеров, и хвост — это гарантированно строки вида «Container … Stopped».
Не «иногда не повезло с порядком», а по устройству, всегда.

Живой случай (25.08.2026): обновление откатилось на шаге `tests`, владельцу
пришло сообщение, в котором про тесты не было ни слова — только девять строк
об остановке контейнеров и два сообщения mysqld о завершении работы. Что
именно упало, узнать было неоткуда.

**Что делает этот модуль.** Выбирает строки ПО СОДЕРЖАНИЮ, а не по месту:

1. выбрасывает заведомый шум — ход контейнеров, шаги сборки, журнал mysqld;
2. ищет признаки беды: сводку pytest, строки `FAILED`/`ERROR`, трассировки,
   отказ сборки. Нашлось — показывает их вместе с окрестностями;
3. не нашлось — отдаёт хвост очищенного вывода;
4. и в любом случае ГОВОРИТ, каким из трёх способов выбрано.

Пункт четвёртый — не украшение. Хвост, выданный молча, выглядит точно так же,
как найденная причина, и читатель принимает одно за другое. Вырезка, о способе
которой не сказано, — это снова тот же обман, только на новом витке.

Стандартная библиотека и ничего больше: модуль зовётся с ХОСТА, где нет ни
venv проекта, ни его зависимостей (тот же довод, что у `deploy/dokumenty.py`).
"""

from __future__ import annotations

import re

#: Приставка компоуза перед строкой контейнера: `tests-1     | …`.
#:
#: Без её снятия не работает ничего: pytest внутри контейнера печатает
#: `FAILED tests/…`, а до нас доезжает `tests-1     | FAILED tests/…`, и любой
#: поиск по началу строки промахивается.
PRISTAVKA = re.compile(r"^(?P<sluzhba>[a-z0-9_.-]+-\d+)\s*\|\s?")

#: Строки, которые не несут ничего, кроме хода дела.
#:
#: Список закрытый и намеренно узкий: выбросить лишнее здесь дороже, чем
#: оставить. Пропущенный шум — это несколько скучных строк в отчёте; выброшенная
#: по ошибке строка беды — это отчёт, который снова ничего не объясняет.
SHUM = (
    # Ход контейнеров и сетей: `Container …  Started`, `Network …  Created`.
    re.compile(r"^\s*(Container|Network|Volume|Image|Service|Compose)\s+\S*\s*"
               r"(Creating|Created|Starting|Started|Stopping|Stopped|Waiting|Healthy|"
               r"Removing|Removed|Recreate|Recreated|Running|Built|Building|Pulling|Pulled)\s*$"),
    re.compile(r"^\s*Aborting on container exit\.\.\.\s*$"),
    re.compile(r"^\s*Compose\s+\S*\s*$"),
    # Шаги buildkit: `#12 [ 4/10] RUN …`, `#12 CACHED`, `#12 DONE 0.3s`.
    re.compile(r"^#\d+\s+(DONE|CACHED|sha256:|extracting|resolve|naming|exporting|"
               r"transferring|load |\[)"),
    re.compile(r"^\s*=>\s"),
    # Журнал mysqld и redis: они рассказывают о себе, а не о наборе тестов.
    re.compile(r"\[(System|Note|Warning)\]\s*\[MY-\d+\]"),
    re.compile(r"^\s*\d+:[A-Z]\s+\d{1,2}\s+\w+\s+\d{4}\s"),
    # Пустая строка сама по себе шумом не считается, но и признаком не является.
)

#: Признаки беды, от самого говорящего к самому общему.
#:
#: Порядок важен: первое совпадение задаёт, что показывать, и сводка pytest
#: полезнее одинокой строки `Traceback`, потому что перечисляет ВСЕ падения, а
#: не то, до которого случайно долистали.
PRIZNAKI = (
    ("сводка pytest", re.compile(r"^=+\s*(short test summary info|FAILURES|ERRORS)\s*=+")),
    ("итог pytest", re.compile(r"^=+.*\b\d+\s+(failed|error|errors)\b.*=+")),
    ("падение теста", re.compile(r"^(FAILED|ERROR)\s+\S+")),
    ("разбор утверждения", re.compile(r"^E\s{3}\S")),
    ("трассировка", re.compile(r"^Traceback \(most recent call last\):")),
    ("отказ сборки", re.compile(r"^(ERROR: failed to solve|failed to solve:|"
                                r"ERROR: Service .* failed to build)")),
    ("исключение", re.compile(r"^\w[\w.]*(Error|Exception|Timeout)(:|\b)")),
    ("ошибка", re.compile(r"\b(?:ERROR|FATAL|CRITICAL)\b")),
)

#: Что означает код выхода контейнера, если он не ноль.
#:
#: **Зачем переводить.** Компоуз печатает `tests-1 exited with code 137` и
#: больше ничего. Сто тридцать семь — это 128+9, то есть SIGKILL, то есть почти
#: всегда нехватка памяти: ядро выбрало жертву и унесло её без объяснений.
#: Человек, читающий отчёт, видит число и не знает, чинить ему тест или
#: добавлять памяти, — а это прямо противоположные действия.
#:
#: Живой повод: 25.08.2026 шлюз тестов упал на сервере с 7937 МБ, где рядом с
#: боевым стеком поднимаются ещё одна MySQL (tmpfs на гигабайт), Redis и сам
#: набор. Гипотеза про нехватку памяти проверяется по этому числу за секунду —
#: если оно вообще доедет до читателя.
KODY_VYHODA = {
    137: "убит сигналом KILL — почти всегда нехватка памяти "
         "(проверьте `dmesg -T | grep -i \"killed process\"`)",
    139: "упал по обращению к чужой памяти (SIGSEGV)",
    143: "остановлен сигналом TERM — кто-то попросил его закончить",
    125: "не смог запуститься сам докер",
    126: "команда внутри образа найдена, но не запускается",
    127: "команды внутри образа нет вовсе",
}

#: `tests-1 exited with code 137`. Ловится отдельно от признаков: это не место в
#: выводе, а факт обо всём прогоне, и он идёт в вырезку ПЕРВОЙ строкой, чем бы
#: ни кончился поиск признаков.
KOD_VYHODA = re.compile(
    r"^\[?K?(?P<sluzhba>[a-z0-9_.-]+-\d+) exited with code (?P<kod>\d+)"
)


def kody_vyhoda(stroki: list[str]) -> list[str]:
    """Ненулевые коды выхода контейнеров, переведённые на человеческий."""
    nayden = []
    for stroka in stroki:
        sovpalo = KOD_VYHODA.search(stroka.strip())
        if not sovpalo:
            continue
        kod = int(sovpalo.group("kod"))
        if kod == 0:
            continue
        poyasnenie = KODY_VYHODA.get(kod)
        itog = f"{sovpalo.group('sluzhba')}: код выхода {kod}"
        if poyasnenie:
            itog += f" — {poyasnenie}"
        nayden.append(itog)
    return nayden

#: Сколько строк вокруг найденного признака показывать.
#:
#: Ноль сверху, потому что признак — это начало беды (`FAILED …`,
#: `Traceback …`), а объяснение идёт ПОСЛЕ него. Три снизу — чтобы у
#: одинокой строки исключения был виден её текст.
DO = 1
POSLE = 3


def _bez_pristavki(stroka: str) -> str:
    return PRISTAVKA.sub("", stroka)


def _shum(golaya: str) -> bool:
    return bool(golaya.strip()) and any(obrazec.search(golaya) for obrazec in SHUM)


def ochistit(stroki: list[str]) -> list[str]:
    """Убрать ход дела, оставить сказанное."""
    ostalos = []
    for stroka in stroki:
        golaya = _bez_pristavki(stroka.rstrip())
        if _shum(golaya):
            continue
        ostalos.append(stroka.rstrip())
    return ostalos


def _nashli_priznak(stroki: list[str]) -> tuple[str, list[int]]:
    """Имя признака и номера строк, где он встретился."""
    for imya, obrazec in PRIZNAKI:
        mesta = [n for n, s in enumerate(stroki) if obrazec.search(_bez_pristavki(s.strip()))]
        if mesta:
            return imya, mesta
    return "", []


def _okna(mesta: list[int], vsego: int) -> list[int]:
    """Номера строк с окрестностями, слитые в один упорядоченный набор."""
    nuzhno: set[int] = set()
    for mesto in mesta:
        for n in range(max(0, mesto - DO), min(vsego, mesto + POSLE + 1)):
            nuzhno.add(n)
    return sorted(nuzhno)


def vyzhat(text: str, predel: int = 12) -> tuple[list[str], str]:
    """Вырезка и ЧЕСТНОЕ имя способа, которым она получена.

    Возвращается пара, а не строка, именно чтобы способ нельзя было потерять по
    дороге: вызывающий обязан что-то с ним сделать, потому что он его получил.
    """
    vse = [s for s in (text or "").splitlines()]
    if not any(s.strip() for s in vse):
        return [], "вывода не было вовсе"

    # Коды выхода снимаются с СЫРОГО вывода: строка `tests-1 exited with code
    # 137` попадает под чистку как ход дела, и при коде ноль правильно
    # попадает. Ненулевой код — уже не ход дела, а итог.
    kody = kody_vyhoda(vse)

    chisto = ochistit(vse)
    if not chisto:
        hvost = [s.rstrip() for s in vse[-predel:]]
        if kody:
            return kody, "по коду выхода контейнера"
        return hvost, "только ход дела, беды не видно"

    imya, mesta = _nashli_priznak(chisto)
    # Код выхода занимает место в вырезке, и это правильный размен: одна строка
    # «убит нехваткой памяти» объясняет больше, чем двенадцать строк любого
    # вывода, который после такого убийства успел напечататься.
    zapas = predel - len(kody)
    if mesta and zapas > 0:
        nomera = _okna(mesta, len(chisto))
        # Обрезаем С НАЧАЛА: первое падение важнее последнего. Если тестов
        # упало сорок, читателю нужен первый — остальные обычно им же и
        # вызваны, а хвост списка не объясняет ничего.
        vidno = [chisto[n] for n in nomera[:zapas]]
        skryto = len(nomera) - len(vidno)
        sposob = f"по признаку «{imya}»"
        if skryto:
            sposob += f", показано {len(vidno)} строк из {len(nomera)}"
        if kody:
            sposob += " и по коду выхода"
        return kody + vidno, sposob

    if kody:
        return kody + chisto[-max(0, zapas):], "по коду выхода контейнера"
    return chisto[-predel:], "признаков беды не нашлось, это просто хвост вывода"


def vyzhat_strokoy(text: str, predel: int = 12) -> str:
    """То же одной строкой — для короткой пометки у шага.

    Способ приписывается в конце и в скобках: у шага место есть только на
    несколько строк, а знать, читаешь ты причину или наугад взятый хвост,
    нужно и там.
    """
    stroki, sposob = vyzhat(text, predel)
    if not stroki:
        return f"({sposob})"
    return "\n".join(stroki) + f"\n({sposob})"
