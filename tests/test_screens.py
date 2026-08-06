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


def test_no_translation_stays_without_a_screen_to_show_it():
    """Каждый ключ перевода кто-то показывает.

    Словарь на полторы тысячи строк переживает экраны: раздел переписали, ключ
    остался, и следующий человек переводит на второй язык слова, которых нигде
    нет. Двадцать таких нашлось этой проверкой при первом же запуске.

    Ищем по кавычкам, а не по вызову `t(...)`: половина ключей приходит из
    таблиц соответствия (`NOTE_LABELS`, `ACTION`, `KIND_LABEL`), где они лежат
    значениями. Шаблонных ключей вида t(`unit${x}`) в коде нет — если появятся,
    проверку придётся учить их видеть, и лучше узнать об этом здесь, чем
    удалить живой перевод.
    """
    dictionary = (SCREENS / "lib" / "i18n.ts").read_text(encoding="utf-8")
    assert not re.search(r"t\(\s*`", dictionary + _all_screen_code()), (
        "появился ключ, собранный шаблоном: проверка мёртвых переводов его не видит"
    )

    keys = re.findall(r"^  ([A-Za-z][A-Za-z0-9_]*):", dictionary, flags=re.M)
    code = _all_screen_code()
    quoted = r"""['"`]"""
    orphans = sorted(
        {key for key in keys if not re.search(quoted + re.escape(key) + quoted, code)}
    )

    assert not orphans, "переводы без экрана: " + ", ".join(orphans)


def _all_screen_code() -> str:
    """Весь код интерфейса, кроме самого словаря."""
    parts = [p.read_text(encoding="utf-8") for p in SCREENS.rglob("*.tsx")]
    parts += [p.read_text(encoding="utf-8") for p in SCREENS.rglob("*.ts") if p.name != "i18n.ts"]
    return chr(10).join(parts)


def test_a_screen_does_not_offer_what_the_server_will_refuse():
    """Кнопка действия стоит за тем же правом, что спросит сервер.

    Правило записано в `lib/permissions.ts`: «интерфейс прячет то, что всё равно
    получит отказ». Держалось оно не везде: экран сотрудников закрыт правом
    `staff.view`, а кнопки одобрения, сброса пароля, отключения и удаления
    спрашивают `staff.manage` — смотрящий получал полный набор, включая «Удалить
    навсегда», и каждая кнопка отвечала отказом.

    Фирмы проверяли `role === "root"` вместо права: менеджер с `companies.edit`
    видел пункт меню, открывал карточку и находил все поля запертыми с подписью
    «правит владелец». Право выдано и работает — интерфейс его не признавал.
    """
    rules = {
        "Staff.tsx": ("staff.manage", "deletePermanently"),
        "Companies.tsx": ("companies.create", "newCompany"),
        "CompanyCard.tsx": ("companies.edit", "companyReadOnly"),
    }
    for name, (permission, marker) in rules.items():
        text = (SCREENS / "screens" / name).read_text(encoding="utf-8")
        assert marker in text, f"{name}: проверка смотрит не туда, {marker} не найден"
        assert f'can(user, "{permission}")' in text, (
            f"{name}: действие показывается без проверки права {permission}"
        )
        assert 'user?.role === "root"' not in text, (
            f"{name}: право подменено проверкой «это root» — роль с этим правом останется ни с чем"
        )


def test_the_journal_reads_currency_from_where_everyone_has_it():
    """Валюта журнала берётся из рабочего пространства, а не из настроек сайта.

    Настройки сайта подгружаются только для root (`lib/app.tsx`), и сотрудник с
    правом на журнал видел суммы в долларах там, где фирма работает в гривне.
    """
    text = (SCREENS / "screens" / "Audit.tsx").read_text(encoding="utf-8")
    assert "workspace.currency" in text
    assert "settings.currency" not in text


def test_the_profile_calls_the_job_by_its_name():
    """В профиле стоит настоящая должность, а не слово «Менеджер».

    В левой колонке тот же человек подписан своей должностью — с комментарием
    «ролей теперь столько, сколько их завели». В профиль правка не дошла:
    бухгалтер видел в меню «Бухгалтер», а у себя в профиле «Менеджер».
    """
    text = (SCREENS / "screens" / "Profile.tsx").read_text(encoding="utf-8")
    assert "role_name" in text, "профиль по-прежнему называет должность словом из словаря"


# --- новый блок не должен потеряться в интерфейсе ---------------------------
#
# Реестр блоков (`core/modules.py`) и реестр прав (`core/permissions.py`) —
# источники правды на сервере. Фронтенд хранит рядом с ними свои карты: подпись,
# описание, значок, строку матрицы доступов. Карты эти ничем не связаны с
# реестром, и забытая строка **не ломает ничего видимого** — она подставляет
# запасное значение:
#
#     SettingsModules:  {t(LABEL[key] ?? "modules")}      → раздел «Модули»
#                       {t(ABOUT[key] ?? "modulesSub")}   → «Какие части системы…»
#                       <Icon name={ICON[key] ?? "docs"}> → общий значок
#     SettingsRoles:    {t(AREA_LABEL[key] ?? "roles")}   → строка «Роли»
#
# Так и вышло: строка журнала действий в конструкторе доступов называлась
# «Роли», рядом со строкой «Роли и доступы». Владелец, раздающий доступ к
# журналу, жал галочку не в той строке. Заметить это глазами нельзя — надо
# знать, что искать.
#
# Приём тот же, что в `test_feed.py`: читаем `.tsx` и сверяем со списком с
# сервера. Хрупко к переформатированию — и это осознанный размен, уже принятый в
# этом файле.

MODULE_MAPS = (
    ("SettingsModules.tsx", "LABEL", "подпись блока"),
    ("SettingsModules.tsx", "ABOUT", "описание блока"),
    ("SettingsModules.tsx", "ICON", "значок блока"),
)


def _map_keys(filename: str, name: str) -> set[str]:
    text = (SCREENS / "screens" / filename).read_text(encoding="utf-8")
    block = re.search(rf"const {name}[^=]*=\s*\{{(.*?)\n\}};", text, re.S)
    assert block, f"в {filename} не нашлась карта {name} — проверка смотрит не туда"
    return set(re.findall(r"^\s*(\w+):", block.group(1), re.M))


def test_kazhdyy_blok_nazvan_v_nastroykakh():
    """Забытый блок показывается как «Модули» с общим значком и работает."""
    from core import modules

    for filename, name, what in MODULE_MAPS:
        missing = sorted(set(modules.KEYS) - _map_keys(filename, name))
        assert not missing, f"{what}: в {filename}.{name} нет блоков {missing}"


def test_kazhdaya_oblast_prav_nazvana_v_matritse():
    """Забытая область показывается строкой «Роли» — рядом с настоящими «Ролями»."""
    from core import permissions

    have = _map_keys("SettingsRoles.tsx", "AREA_LABEL")
    missing = sorted({area.key for area in permissions.AREAS} - have)
    assert not missing, f"в SettingsRoles.AREA_LABEL нет областей {missing}"


def test_znachok_bloka_odin_i_tot_zhe_v_menyu_i_v_nastroykakh():
    """Две карты значков разъехались молча: заметить их некому, они в разных файлах.

    Поймано перебором: у склада в меню стоял «warehouse», а в настройках
    «database»; у телефонии — «callIn» против «call».
    """
    sidebar = (SCREENS / "components" / "Sidebar.tsx").read_text(encoding="utf-8")
    settings_icons = {}
    text = (SCREENS / "screens" / "SettingsModules.tsx").read_text(encoding="utf-8")
    block = re.search(r"const ICON[^=]*=\s*\{(.*?)\n\};", text, re.S)
    for key, icon in re.findall(r"^\s*(\w+):\s*\"([^\"]+)\"", block.group(1), re.M):
        settings_icons[key] = icon

    # В меню значок и ключ блока стоят рядом внутри одного объекта пункта.
    #
    # Сверяем только СОБСТВЕННЫЙ пункт блока — тот, где право начинается с его
    # же ключа. Соседний пункт бывает закрыт чужим выключателем: «Файлы» стоят
    # на блоке досок, но правом `settings.manage`, и значок у них свой по делу.
    guilty = []
    for match in re.finditer(r"\{([^{}]*module:\s*\"(\w+)\"[^{}]*)\}", sidebar):
        body, key = match.group(1), match.group(2)
        perm = re.search(r"perm:\s*\"([^\"]+)\"", body)
        icon = re.search(r"icon:\s*\"([^\"]+)\"", body)
        if not icon or key not in settings_icons:
            continue
        if not perm or not perm.group(1).startswith(f"{key}."):
            continue
        if icon.group(1) != settings_icons[key]:
            guilty.append(f"{key}: меню «{icon.group(1)}», настройки «{settings_icons[key]}»")
    assert not guilty, "значки блоков разошлись:\n" + "\n".join(guilty)


def test_vsyakiy_znachok_sushchestvuet():
    """`Icon` типизирован как `keyof typeof PATHS | string`, то есть опечатка
    даёт пустой `<svg>` — ни ошибки, ни предупреждения сборки."""
    icons = (SCREENS / "components" / "Icon.tsx").read_text(encoding="utf-8")
    block = re.search(r"const PATHS[^=]*=\s*\{(.*?)\n\};", icons, re.S)
    known = set(re.findall(r"^\s*(\w+):", block.group(1), re.M))

    used = set()
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        used.update(re.findall(r"<Icon\s+name=\"([^\"]+)\"", text))
        used.update(re.findall(r"icon:\s*\"([^\"]+)\"", text))
    unknown = sorted(used - known)
    assert not unknown, f"значков нет в Icon.PATHS: {unknown}"
