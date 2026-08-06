"""Правила, общие для всех экранов CRM.

Проверка идёт по исходникам, а не по браузеру: собранного фронтенда в тестах
нет, а держать ради пары правил отдельный прогон с headless-браузером — дороже
самих правил. Правила же простые и проверяются чтением.

Каждое из них написано на беду, которая случилась. Экраны пишутся в разное
время и разными руками, и расходятся они не в замысле, а в мелочах: один ждёт
паузу в наборе, другой шлёт запрос на каждую букву; один говорит об отказе,
другой молчит и крутит вертушку. Здесь эти мелочи и держатся.
"""

import pathlib
import re

SCREENS = pathlib.Path(__file__).resolve().parent.parent / "web" / "frontend" / "crm" / "src"

# Стражи, которым отказ не нужен, — с причиной у каждого.
EXEMPT = {
    # Пока приложение поднимается, отказывать не в чем: `/auth/me` без ответа
    # означает «не вошёл», и App.tsx показывает вход, а не ошибку.
    "App.tsx": "загрузка самого приложения: нет ответа — показываем вход",
}


def _screens_with_guard():
    """Файлы, где `ScreenLoading` стоит вместо данных, и сами строки-стражи."""
    found = {}
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        guards = [line.strip() for line in text.splitlines() if "return <ScreenLoading" in line]
        if guards:
            found[path.name] = guards
    return found


def test_the_check_itself_sees_the_screens():
    """Страховка от переименований: пустой список файлов проверил бы пустоту."""
    found = _screens_with_guard()
    assert len(found) >= 20, f"экранов со стражем найдено {len(found)} — проверка смотрит не туда"


def test_every_screen_can_say_it_did_not_load():
    """У стража есть и сообщение об отказе, и чем повторить загрузку."""
    silent = []
    for name, guards in _screens_with_guard().items():
        if name in EXEMPT:
            continue
        for guard in guards:
            if "error={" not in guard or "onRetry={" not in guard:
                silent.append(f"{name}: {guard}")

    assert not silent, "экраны молчат об отказе и остаются с вечной вертушкой:\n" + "\n".join(silent)


def test_a_failed_load_is_not_dressed_up_as_emptiness():
    """Отказ не подменяется пустым списком: «писем нет» ≠ «почта не ответила».

    Пустота — это ответ сервера, а отказ — его отсутствие. Пока они сходились в
    одном `[]`, экран почты на упавшем сервере бодро сообщал, что писем нет.
    """
    liars = []
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        # Тело `catch` без вложенных скобок: длинные разборы ошибок сюда не
        # попадают, а короткое «поймали и подсунули пустой список» — ровно то,
        # что ищем, — попадает целиком.
        for block in re.findall(r"catch\s*(?:\([^)]*\))?\s*\{([^{}]*)\}", text):
            if re.search(r"set\w+\(\[\]\)", block) and "fail(" not in block:
                liars.append(f"{path.name}: {' '.join(block.split())[:80]}")

    assert not liars, "отказ выдан за пустоту:\n" + "\n".join(liars)


def test_the_failure_screen_repeats_what_the_server_said():
    """Своё общее объяснение — только когда сервер промолчал.

    «Раздел выключен» и «нет связи» — разные беды: на вторую есть смысл нажать
    «ещё раз», на первую нет. Подменять ответ сервера общей фразой значит
    отнимать у человека это различие.
    """
    ui = (SCREENS / "components" / "ui.tsx").read_text(encoding="utf-8")
    assert "error instanceof ApiError ? error.message" in ui, (
        "экран отказа перестал показывать сообщение сервера"
    )


def test_every_search_box_waits_for_a_pause_in_typing():
    """Запрос уходит после паузы, а не на каждую букву.

    Половина экранов ждала паузу собственным таймером, половина не ждала вовсе:
    «Иванов» в поиске по журналу — шесть запросов, из них пять никому не нужны.
    Разница была не решением, а следом того, что экраны писались в разное время.

    Проверяем по признаку «экран отправляет набранное на сервер»: если в строке
    запроса есть поисковый параметр, значит есть и поле, и пауза обязана быть.
    """
    typed = re.compile(r'params\.set\("(search|number|query)"|[?&](search|query)=\$\{')
    hasty = []
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        if typed.search(text) and "useDebounced" not in text:
            hasty.append(path.name)

    assert not hasty, "экраны шлют запрос на каждую букву: " + ", ".join(hasty)


def test_the_pause_is_measured_in_one_place():
    """Своих таймеров у экранов больше нет.

    Три экрана держали одинаковые четыре строки с `setTimeout`, и у каждого
    была своя копия числа 250. Копия — это будущее расхождение: правят одну,
    забывают две.
    """
    own_timers = []
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        if "clearTimeout" in text and "setTimeout" in text and "Debounced" not in path.name:
            # Опрос обрабатывающихся работ — не про набор текста: там таймер
            # ждёт сервер, а не человека.
            if "poll" not in text.lower():
                own_timers.append(path.name)

    assert not own_timers, "экран отмеряет паузу сам, мимо общего крючка: " + ", ".join(own_timers)
