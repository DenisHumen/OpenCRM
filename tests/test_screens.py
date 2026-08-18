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
KOREN = pathlib.Path(__file__).resolve().parent.parent

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
    assert not re.search(r"\bt\(\s*`", dictionary + _all_screen_code()), (
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


def test_pole_skanera_lovit_enter_samo():
    """Сканер заканчивает ввод нажатием Enter — и это должно сработать.

    Проверено на живой странице: доверенный Enter долетает до поля, а событие
    `submit` у формы без кнопки отправки НЕ возникает. То есть на одном
    `onSubmit` поле молчало бы ровно там, ради чего блок и заводился: сканер
    пикает, а на экране ничего.

    Сторож стоит потому, что убрать `onKeyDown` при чистке кода легко —
    выглядит он как дубль обработчика формы, — а обнаружится пропажа только со
    сканером в руках.
    """
    source = (SCREENS / "components" / "ProductBarcodes.tsx").read_text(encoding="utf-8")
    scanner = source[source.index("export function BarcodeScanner"):]
    assert "onKeyDown" in scanner, "поле сканера полагается на отправку формы"
    assert 'e.key === "Enter"' in scanner


# --- ответ на прошлый запрос не ложится поверх нового -----------------------
#
# Экран с фильтром отправляет запрос на каждое изменение отбора, а ответы
# приходят в том порядке, в каком успел сервер. Набрали «Ив», через миг
# «Иванов» — и если первый ответ доехал вторым, на экране окажется выдача по
# «Ив» под словом «Иванов» в поле. Ошибка тихая: список правдоподобный, и
# заметить её можно, только зная, что искал.
#
# Лечится это одним приёмом на все экраны — флажком в замыкании эффекта,
# который снимает уборка при следующем запуске. Здесь проверяется, что приём
# стоит везде, где есть отбор: признаком отбора берём паузу в наборе
# (`useDebounced`) — она есть ровно там, где человек меняет условие быстрее,
# чем отвечает сервер.


def _cancels_stale_answers(text: str) -> bool:
    """Есть ли в файле флажок «этот ответ ещё нужен» и его снятие в уборке."""
    return bool(
        re.search(r"let (current|alive|fresh) = true", text)
        and re.search(r"\b(current|alive|fresh) = false", text)
    )


def test_screens_with_a_filter_cancel_stale_answers():
    """Каждый экран с паузой в наборе отбрасывает ответ на прошлый отбор."""
    filtered = {}
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        if "useDebounced" in text:
            filtered[path.name] = text
    assert len(filtered) >= 8, (
        f"экранов с отбором найдено {len(filtered)} — проверка смотрит не туда"
    )

    racing = sorted(name for name, text in filtered.items() if not _cancels_stale_answers(text))
    assert not racing, "ответ на прошлый отбор ляжет поверх нового: " + ", ".join(racing)


# --- двойное нажатие не заводит вторую запись -------------------------------
#
# Перебор, а не список подозрительных мест: точек `api.post` под шесть десятков,
# и раскладывали их руками. Каждая либо стоит за засовом (`useGuard`), либо
# названа ниже вместе с причиной, почему второе нажатие ей не страшно.
#
# Засов обязан быть именно ref'ом, а не состоянием React: `setBusy(true)` меняет
# значение только к следующему рендеру, а два обработчика, сработавшие в одном
# тике, читают `busy` каждый из своего замыкания — и оба видят `false`. Человек
# так жмёт редко, зато сканер, залипшая кнопка мыши и Enter на автоповторе —
# как раз так. Подробности — в `lib/guard.ts`.

# Ключ — файл и путь запроса; подстановки шаблона свёрнуты в `{}`.
POST_WITHOUT_LATCH: dict[tuple[str, str], str] = {
    # Перевод состояния уже существующей записи. Второе нажатие повторяет
    # первое: бланк, который уже «закрыт», закрывается в то же самое «закрыт».
    ("Telegram.tsx", "/telegram/chats/{}/presence"): (
        "отметка «я смотрю этот чат»: не запись, а срок годности ключа в Redis. "
        "Второе нажатие продлевает ту же отметку в то же самое значение"
    ),
    ("Telegram.tsx", "/telegram/chats/{}/read"): (
        "отметка «я это увидел»: граница прочитанного только растёт, и второй "
        "вызов с тем же числом не делает ничего. Записи не заводит вовсе"
    ),
    ("Telegram.tsx", "/telegram/chats/{}/messages/{}/fetch"): (
        "забрать видео у телеграма: идемпотентно по устройству — уже забранное "
        "второй раз не качается, проверено отдельной проверкой"
    ),
    ("DocumentCard.tsx", "/documents/{}/status"): "перевод состояния бланка",
    ("DealCard.tsx", "/deals/{}/move"): "перевод этапа: сервер отвечает stage_moved_meanwhile",
    ("Deals.tsx", "/deals/{}/move"): "перевод этапа перетаскиванием, тот же ответ сервера",
    ("OrderCard.tsx", "/orders/{}/ready"): "пометка «собран» — состояние, а не запись",
    ("CompanyCard.tsx", "/companies/{}/default"): "«основная» ровно одна: повтор ничего не меняет",
    ("SettingsRoles.tsx", "/roles/{}/default"): "должность по умолчанию ровно одна",
    ("SettingsModules.tsx", "/modules/{}"): "переключатель блока: включён либо выключен",
    ("Setup.tsx", "/modules/presets"): "набор блоков применяется целиком, повтор даёт тот же набор",
    ("app.tsx", "/settings/maintenance"): "режим обслуживания: включён либо выключен",
    ("Mail.tsx", "/mail/messages/{}/read"): "отметка «прочитано»",
    ("Mailboxes.tsx", "/mail/accounts/{}/check"): "проверка подключения ничего не пишет",
    ("Mailboxes.tsx", "/mail/accounts/{}/sync"): "забор почты: письма различаются по message-id",
    ("Settings.tsx", "/settings/site-logo/fetch"): "перечитывание логотипа с сайта",
    ("StorageCard.tsx", "/system/storage/purge"): "уборка мусора: повтор убирает уже убранное",
    ("ProductBarcodes.tsx", "/labels/products/{}/barcodes/{}/primary"): (
        "основной штрихкод ровно один"
    ),
    ("Staff.tsx", "path"): "одобрение, отказ, отключение и восстановление — состояния",
    ("Staff.tsx", "/staff/{}/role"): "владелец или сотрудник — состояние",
    ("Staff.tsx", "/roles/assign/{}"): "должность у человека одна",
    # Стоит за окном подтверждения, а окно закрывается тем же нажатием: второй
    # раз нажимать уже не по чему.
    ("OrderCard.tsx", "/orders/{}/{}"): "отмена и откат заказа — за окном подтверждения",
    ("DocumentCard.tsx", "/documents/acts/{}/cancel"): "за окном подтверждения",
    ("BoardEditor.tsx", "/shares/{}/regenerate"): "за окном подтверждения",
    ("TelephonySettings.tsx", "/telephony/settings/secret"): "за окном подтверждения",
    # Вход, регистрация и свой пароль. Записи не заводят: на занятую почту
    # сервер отвечает честным конфликтом, а пароль меняется в то же значение.
    ("Auth.tsx", "/auth/login"): "вход: записи не заводит",
    ("Auth.tsx", "/auth/register"): "регистрация: на занятую почту сервер отвечает конфликтом",
    ("Auth.tsx", "/auth/me/password"): "смена своего пароля в то же значение",
    ("Profile.tsx", "/auth/me/password"): "смена своего пароля в то же значение",
    ("app.tsx", "/auth/logout"): "выход",
}


def _first_argument(text: str, start: int) -> str:
    """Путь запроса из вызова: `start` — сразу за открывающей скобкой.

    Подстановки шаблона сворачиваем в `{}`: номер записи в ключе только мешал
    бы, а вложенные в подстановку кавычки (``${x ? "a" : "b"}``) простую
    регулярку ломают — на этом ключ у отмены заказа один раз уже съехал.
    """
    i = start
    while i < len(text) and text[i] in " \n\r\t":
        i += 1
    if i >= len(text):
        return "?"
    quote = text[i]
    if quote not in "\"'`":
        name = re.match(r"[A-Za-z_$][\w$]*", text[i:])
        return name.group(0) if name else "?"
    i += 1
    out: list[str] = []
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if quote == "`" and text.startswith("${", i):
            depth, i = 1, i + 2
            while i < len(text) and depth:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            out.append("{}")
            continue
        if char == quote:
            break
        out.append(char)
        i += 1
    return "".join(out)


def _handler_of(lines: list[str], at: int) -> str:
    """Текст обработчика, внутри которого стоит строка `at`.

    Ищем ближайшую выше строку, которая открывает функцию и стоит левее самого
    вызова. Соседний обработчик в счёт не идёт — иначе засов из соседней
    функции засчитывался бы этой: так `makeDefault` в конструкторе доступов
    один раз уже «прошёл» проверку за счёт стоящего выше `save`.
    """
    starts = re.compile(r"=>\s*\{|=\s*async\b|function\s+\w+")
    own = len(lines[at]) - len(lines[at].lstrip())
    for j in range(at - 1, -1, -1):
        line = lines[j]
        if line.strip() and len(line) - len(line.lstrip()) < own and starts.search(line):
            return "\n".join(lines[j:at + 1])
    return "\n".join(lines[:at + 1])


def _post_calls() -> list[tuple[str, str, int, str]]:
    """Все `api.post` интерфейса: файл, путь, строка, текст обработчика."""
    call = re.compile(r"api\.post(?:<[^<>]*>)?\(")
    found = []
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for match in call.finditer(text):
            at = text[:match.start()].count("\n")
            found.append(
                (path.name, _first_argument(text, match.end()), at + 1, _handler_of(lines, at))
            )
    return found


def test_the_sweep_sees_every_post():
    """Пустой перебор сделал бы проверку ниже зелёной и бессмысленной."""
    assert len(_post_calls()) > 50, "вызовы не собрались — проверка ничего не значит"


def test_every_created_record_stands_behind_a_latch():
    """Действие, заводящее запись, не срабатывает дважды от одного нажатия."""
    latch = re.compile(r"\w*[Gg]uard\.take\(\)")
    naked = []
    for name, route, line, handler in _post_calls():
        if latch.search(handler):
            continue
        if (name, route) in POST_WITHOUT_LATCH:
            continue
        naked.append(f'{name}:{line} — api.post("{route}")')

    assert not naked, (
        "запись заводится без засова, и двойное нажатие заведёт вторую:\n"
        + "\n".join(naked)
        + "\n\nЛибо возьми useGuard, либо внеси в POST_WITHOUT_LATCH с объяснением, "
        "почему повтор безвреден."
    )


def test_the_latch_is_a_ref_and_not_a_state():
    """Засов меняется в тот же миг, а не к следующему рендеру.

    Проверено нажатием: три вызова подряд в одном тике завели три заявки.
    Состояние остаётся рядом только ради `disabled` на кнопке.
    """
    text = (SCREENS / "lib" / "guard.ts").read_text(encoding="utf-8")
    assert "useRef(false)" in text, "засов держится состоянием — он опоздает на рендер"
    assert re.search(r"if \(held\.current\) return false", text)


# Флажки «это уже было», не имеющие отношения к засову, — с причиной у каждого.
OWN_FLAGS: dict[str, str] = {
    "app.tsx": "мастер первого запуска показывается один раз на загрузку страницы",
    "Documents.tsx": "фокус в поле сканера забирается однажды, а не при каждой перезагрузке списка",
}


def test_no_screen_keeps_its_own_copy_of_the_latch():
    """Своего `useRef(false)` под нужду засова у экранов нет.

    Копия расходится с оригиналом молча: палитра команд держала свой засов и
    свой комментарий к нему, и правка общего крючка её бы не касалась.
    """
    own = [
        path.name
        for path in sorted(SCREENS.rglob("*.tsx"))
        if "useRef(false)" in path.read_text(encoding="utf-8")
        and path.name not in OWN_FLAGS
    ]
    assert not own, "экран держит свой засов мимо lib/guard.ts: " + ", ".join(own)


# --- карточка не показывает чужие данные под своим адресом ------------------


def test_a_card_is_rebuilt_when_the_number_in_the_address_changes():
    """Переход с клиента А на клиента Б собирает карточку заново.

    Маршрут при таком переходе тот же, и React оставляет карточку смонтированной
    со всем состоянием: полсекунды до ответа сервера на экране висят имя, лента
    и файлы предыдущего клиента — данные А под адресом Б. Хуже с полями, которые
    держат содержимое сами (`defaultValue`): название заявки А оставалось в поле
    над заявкой Б и по потере фокуса уезжало на сервер как правка Б.

    Ключ по номеру рвёт эту связь одной обёрткой на все карточки, поэтому
    проверяем не каждый экран по отдельности, а сами маршруты.
    """
    app = (SCREENS / "App.tsx").read_text(encoding="utf-8")

    assert re.search(r"function ById\b", app), "обёртка ById исчезла"
    assert re.search(r"<Fragment key=\{id\}>", app), (
        "ById перестал собирать карточку заново: без ключа обёртка ничего не делает"
    )

    routes = re.findall(r'<Route\s+path="([^"]*:id[^"]*)"\s+element=\{(.*?)\}\s*/>', app, re.S)
    assert len(routes) >= 5, f"маршрутов с номером найдено {len(routes)} — проверка смотрит не туда"

    unkeyed = [path for path, element in routes if "<ById>" not in element]
    assert not unkeyed, (
        "карточка переживёт смену номера в адресе вместе с чужими данными: "
        + ", ".join(unkeyed)
    )


# --- доступность ------------------------------------------------------------


def test_a_switch_is_a_button_and_it_actually_switches():
    """Переключатель работает с клавиатуры — и сам, а не только через обёртку.

    Две беды подряд, обе про одно. Сначала переключатель был `div` с
    обработчиком: под Tab не попадает, на пробел не отвечает — и ни один блок
    системы нельзя было включить, не взяв мышь.

    Потом — уже настоящей кнопкой — он попадался с пустым `onToggle`: рядом
    стоял обработчик на всей строке, и выглядело это безобидно. Но `Toggle`
    останавливает всплытие (иначе одно нажатие срабатывало бы дважды), то есть
    нажатие ПО САМОМУ переключателю не делало ничего — а Tab приводит фокус
    именно на него. Так в настройках ящиков было три мёртвых переключателя,
    включая оба «использовать SSL».
    """
    ui = (SCREENS / "components" / "ui.tsx").read_text(encoding="utf-8")
    toggle = ui[ui.index("export function Toggle"):]
    assert "<button" in toggle, "переключатель перестал быть кнопкой"
    assert 'role="switch"' in toggle and "aria-checked={on}" in toggle
    assert "e.stopPropagation()" in toggle, (
        "без остановки всплытия строка-обёртка вернёт переключатель обратно"
    )

    dead = []
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"onToggle=\{([^{}]*)\}", text):
            body = " ".join(match.group(1).split())
            if re.fullmatch(r"\(\)\s*=>\s*(undefined|null|void 0)?", body):
                dead.append(f"{path.name}:{text[:match.start()].count(chr(10)) + 1}")
    assert not dead, (
        "переключатель не делает ничего, а фокус с клавиатуры приходит именно на него: "
        + ", ".join(dead)
    )


def test_pereklyuchatel_temy_rabotaet_s_klaviatury():
    """Тема выбирается кнопками, и по ним ходит Tab.

    Соседняя проверка (`Toggle`) написана на беду с `div`-переключателем: под
    Tab он не попадает, на пробел не отвечает, и настройку нельзя было тронуть
    без мыши. Ряд «светлая / тёмная / как в системе» — та же ловушка: три
    подсвеченных прямоугольника выглядят кнопками ровно до попытки дойти до них
    с клавиатуры.

    Состояние обязано читаться вслух: без `aria-pressed` читалка объявляет три
    одинаковые кнопки и ни слова о том, какая из них сейчас выбрана, — то есть
    ровно то, ради чего переключатель и открывают. У ряда есть имя: «группа» без
    подписи не говорит, к чему относятся «Светлая» и «Тёмная».
    """
    profile = (SCREENS / "screens" / "Profile.tsx").read_text(encoding="utf-8")
    picker = re.search(r'<div className="theme-pick".*?</div>', profile, re.S)
    assert picker, "ряд выбора темы исчез из профиля — проверка смотрит не туда"
    row = picker.group(0)

    assert 'role="group"' in row and "aria-label={t(" in row, (
        "у ряда выбора темы нет имени: читалка объявит три кнопки ни о чём"
    )
    assert "<button" in row, "тема выбирается не кнопкой — с клавиатуры до неё не дойти"
    assert "aria-pressed=" in row, (
        "выбранная тема не объявляется: вслух три кнопки звучат одинаково"
    )
    assert "onClick={() => setTheme(" in row, "кнопка темы ничего не переключает"

    # Все три положения на месте. Переключатель «светлая/тёмная» без «как в
    # системе» — это отказ следовать за системой, которая сама темнеет к вечеру.
    theme_lib = (SCREENS / "lib" / "theme.ts").read_text(encoding="utf-8")
    assert re.search(r'THEMES[^=]*=\s*\["light", "dark", "system"\]', theme_lib), (
        "положений темы стало не три"
    )


def test_tema_do_otrisovki_chitaet_tot_zhe_klyuch():
    """Скрипт, ставящий тему до первой отрисовки, и приложение сходятся в ключе.

    Скрипт лежит отдельным файлом и о `lib/theme.ts` ничего не знает — иначе он
    не мог бы выполниться раньше бандла. Разойдись они в ключе или в имени
    атрибута — приложение сохраняло бы выбор в одно место, а страница читала
    другое, и светлая тема возвращалась бы к тёмной на каждой перезагрузке.
    Молча: ошибки нет, просто выбор «не запоминается».

    Инлайновым скрипт сделать нельзя (`script-src 'self'` без `'unsafe-inline'`
    в `web/middleware.py`), а `type="module"` — это `defer`, то есть уже после
    первого кадра. Поэтому и обычный `<script src>`, и эта проверка.
    """
    boot = (SCREENS.parent / "public" / "assets" / "theme-boot.js").read_text(encoding="utf-8")
    theme_lib = (SCREENS / "lib" / "theme.ts").read_text(encoding="utf-8")
    index = (SCREENS.parent / "index.html").read_text(encoding="utf-8")

    key = re.search(r'THEME_KEY = "([^"]+)"', theme_lib)
    attr = re.search(r'THEME_ATTR = "([^"]+)"', theme_lib)
    assert key and attr, "в lib/theme.ts не нашлись ключ и атрибут — проверка смотрит не туда"
    assert f'"{key.group(1)}"' in boot, f"boot-скрипт читает не {key.group(1)}"
    assert f'"{attr.group(1)}"' in boot, f"boot-скрипт ставит не {attr.group(1)}"

    # Скрипт обязан стоять в <head> и БЕЗ type="module": module — это defer.
    tag = re.search(r'<script[^>]*theme-boot\.js[^>]*>', index)
    assert tag, "boot-скрипт не подключён — вернулась вспышка тёмного при загрузке"
    assert "type=" not in tag.group(0), (
        "boot-скрипт стал модулем, то есть defer: он выполнится уже после первого кадра"
    )
    head = index[: index.index("</head>")]
    assert "theme-boot.js" in head, "boot-скрипт уехал из <head> — отрисовка его не дождётся"


def test_a_modal_is_a_dialog_and_keeps_the_focus():
    """Окно объявлено окном и не выпускает Tab на страницу под собой.

    Пока ловушки не было, Tab уводил по ссылкам страницы ПОД затемнением — по
    тем самым, по которым мышью не попасть. А без `role="dialog"` читалка
    объявляла содержимое окна как продолжение страницы.
    """
    ui = (SCREENS / "components" / "ui.tsx").read_text(encoding="utf-8")
    modal = ui[ui.index("export function Modal"):]
    assert 'role="dialog"' in modal and 'aria-modal="true"' in modal
    assert "aria-label={title ?? label}" in modal, "у окна без заголовка не осталось имени"
    assert 'e.key !== "Tab"' in modal, "ловушка фокуса исчезла — Tab уйдёт под окно"
    assert 'e.key === "Escape"' in modal


def _open_tag_end(text: str, start: int) -> int:
    """Индекс `>`, закрывающего открывающий тег: `>` внутри `{...}` не в счёт.

    Без этого стрелка в `onClick={() => …}` обрывала бы разбор тега на первом же
    `>`, и половина кнопок молча выпадала бы из перебора — а зелёная проверка,
    которая ничего не смотрит, хуже её отсутствия.
    """
    depth, i = 0, start
    while i < len(text):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char in "\"'`":
            quote, i = char, i + 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif char == ">" and depth == 0:
            return i
        i += 1
    return -1


def _elements(tag: str):
    """Все `<tag …>…</tag>` интерфейса: файл, строка, атрибуты, содержимое."""
    edges = re.compile(r"<" + tag + r"\b|</" + tag + r">")
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<" + tag + r"\b", text):
            head = _open_tag_end(text, match.start())
            if head < 0 or text[head - 1] == "/":
                continue
            depth, i, close = 1, head + 1, None
            while i < len(text) and depth:
                nxt = edges.search(text, i)
                if not nxt:
                    break
                if nxt.group(0).startswith("</"):
                    depth -= 1
                    if depth == 0:
                        close = nxt.start()
                        break
                    i = nxt.end()
                else:
                    inner = _open_tag_end(text, nxt.start())
                    if inner > 0 and text[inner - 1] == "/":
                        i = nxt.start() + 1
                        continue
                    depth += 1
                    i = nxt.end()
            if close is None:
                continue
            line = text[:match.start()].count("\n") + 1
            yield path.name, line, text[match.start() + len(tag) + 1:head], text[head + 1:close]


def _named(attrs: str, body: str) -> bool:
    """Есть ли у кнопки доступное имя: подпись, `aria-label`, `title` или `alt`."""
    if "aria-label" in attrs or "aria-labelledby" in attrs or "title=" in attrs:
        return True
    # Ссылка вокруг картинки называется её `alt` — так устроен расчёт
    # доступного имени, и читалка объявляет именно его. Пустой `alt` не в счёт:
    # им помечают украшение, которое читать не надо.
    if re.search(r"<img\b[^>]*\balt=(?:\{[^}]+\}|\"[^\"]+\")", body):
        return True
    inner = re.sub(r"<Icon\b[^>]*/>", "", body)
    inner = re.sub(r"<[^>]*>", "", inner)
    return bool(re.search(r"\bt\(|[A-Za-zА-Яа-я]{2,}", inner))


def test_every_button_says_what_it_does():
    """У кнопки есть либо подпись, либо доступное имя.

    Кнопка из одного значка — пустая кнопка для всех, кто значка не видит:
    читалка объявляет «кнопка» и замолкает. Таких набралось десять — удаление
    файла, работы и штрихкода, закрытие окна, меню карточки клиента.
    """
    buttons = list(_elements("button")) + list(_elements("a"))
    assert len(buttons) >= 120, f"кнопок разобрано {len(buttons)} — проверка смотрит не туда"

    mute = [f"{name}:{line}" for name, line, attrs, body in buttons if not _named(attrs, body)]
    assert not mute, "кнопка без текста и без доступного имени: " + ", ".join(mute)


# --- мелочи, у каждой своя беда ---------------------------------------------


def _without_comments(text: str) -> str:
    """Тот же текст, но комментарии заменены пробелами — номера строк целы."""
    def blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return re.sub(r"/\*.*?\*/|//[^\n]*", blank, text, flags=re.S)


def test_copying_goes_through_the_one_place_that_has_a_fallback():
    """`navigator.clipboard` зовётся только из `lib/clipboard.ts`.

    Объект живёт лишь в защищённом контексте, а CRM у большинства открыта по
    HTTP на адресе вида 10.0.0.130: там его нет вовсе, и обращение падает
    синхронным TypeError — ещё до промиса, так что `.catch()` на нём бесполезен.
    Именно поэтому на экране телефонии всплывало «Скопировано» над пустым
    буфером: адрес вебхука вставляют в настройки АТС ровно один раз и не
    проверяя.
    """
    direct = []
    for path in sorted(SCREENS.rglob("*.ts*")):
        if path.name == "clipboard.ts":
            continue
        # Комментарии не в счёт: объяснение, почему так делать нельзя, стоит
        # ровно рядом с местом, где это когда-то делали.
        text = _without_comments(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"navigator\.clipboard", text):
            direct.append(f"{path.name}:{text[:match.start()].count(chr(10)) + 1}")
    assert not direct, (
        "буфер обмена мимо copyText — по HTTP это молчаливый отказ: " + ", ".join(direct)
    )


# --- цвет живёт только в токене ---------------------------------------------
#
# Перебор, а не список подозрительных мест: цветов в интерфейсе под сотню, и
# разложить их по темам руками нельзя — забытый останется тем же и на светлой.
# Беда у этого правила ровно одна и повторяемая: зашитый цвет светлую тему не
# переживает, а находится он не в коде, а глазами и не сразу. Так превью кнопки
# возврата на светлой теме стало чёрным с белой надписью, а под курсором — белым
# по белому; так плашка «Обложка» поверх работы получила чёрный текст на чёрном.
#
# Правило простое: в `styles.css` цвет стоит только в объявлении токена
# (`--что-то: цвет`), в коде экранов — только `var(--…)`. Всё остальное —
# именованное исключение с объяснением, почему это значение от темы не зависит.

COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")

# Цвет мимо токена — с причиной у каждого. Ключ — файл и имя, рядом с которым
# он стоит.
COLOR_OUTSIDE_A_TOKEN: dict[tuple[str, str], str] = {
    # Не цвета интерфейса, а значения: их выбирают владельцу в настройках, они
    # уезжают в базу и оттуда на витрину клиента. Тема CRM к ним отношения не
    # имеет — как и к «синему» в поле выбора цвета.
    ("Settings.tsx", "SWATCHES"): "палитра акцента витрины, а не цвета интерфейса",
}


def _color_owner(lines: list[str], at: int) -> str:
    """Чему принадлежит цвет: ближайшее объявление на строке или выше."""
    for j in range(at, -1, -1):
        named = re.search(r"(?:const|function|class)\s+(\w+)", lines[j])
        if named:
            return named.group(1)
    return "?"


def test_tsvet_zhivyot_tolko_v_tokene():
    """Ни одного цвета мимо `--токен` — иначе светлая тема неполна.

    Комментарии не в счёт: объяснение, почему токен именно такой, называет
    старое значение прямо рядом с ним.
    """
    styles = _without_comments((SCREENS / "styles.css").read_text(encoding="utf-8"))
    lines = styles.splitlines()
    assert sum(bool(COLOR.search(line)) for line in lines) > 40, (
        "цветов в styles.css не нашлось — проверка смотрит не туда"
    )

    hardcoded = []
    for number, line in enumerate(lines, 1):
        if COLOR.search(line) and not re.match(r"\s*--[\w-]+\s*:", line):
            hardcoded.append(f"styles.css:{number} — {line.strip()[:70]}")

    for path in sorted(SCREENS.rglob("*.ts*")):
        text = _without_comments(path.read_text(encoding="utf-8"))
        code = text.splitlines()
        for match in COLOR.finditer(text):
            at = text[: match.start()].count("\n")
            owner = _color_owner(code, at)
            if (path.name, owner) in COLOR_OUTSIDE_A_TOKEN:
                continue
            hardcoded.append(f"{path.name}:{at + 1} ({owner}) — {match.group(0)}")

    assert not hardcoded, (
        "цвет зашит мимо токена и не переживёт смену темы:\n"
        + "\n".join(hardcoded)
        + "\n\nЛибо заведи токен в обеих палитрах styles.css, либо внеси в "
        "COLOR_OUTSIDE_A_TOKEN с объяснением, почему цвет от темы не зависит."
    )


def test_obe_palitry_nazyvayut_odni_i_te_zhe_tokeny():
    """Тёмная и светлая объявляют один набор имён.

    Токен, забытый в светлой, не остаётся пустым — он наследует значение из
    блока по умолчанию, то есть ТЁМНОЕ. Получается тёмная плашка посреди светлой
    страницы, и виновата в ней не разметка, а пропущенная строка палитры.
    Обратная сторона так же тиха: токен, заведённый только в светлой, на тёмной
    теме просто не существует, и свойство молча откатывается к начальному.
    """
    styles = _without_comments((SCREENS / "styles.css").read_text(encoding="utf-8"))

    def names(selector: str) -> set[str]:
        block = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", styles, re.S)
        assert block, f"в styles.css не нашёлся блок {selector} — проверка смотрит не туда"
        return set(re.findall(r"^\s*(--[\w-]+)\s*:", block.group(1), re.M))

    dark = names(':root,\n:root[data-theme="dark"]')
    light = names(':root[data-theme="light"]')
    assert len(dark) > 30, f"токенов в тёмной палитре {len(dark)} — проверка смотрит не туда"

    assert not dark - light, f"в светлой палитре нет токенов: {sorted(dark - light)}"
    assert not light - dark, f"в тёмной палитре нет токенов: {sorted(light - dark)}"


# Справочники, чей отказ показывать негде и незачем, — с причиной у каждого.
# Ключ — файл и имя списка: «в файле где-то есть слово failure» проверкой не
# было бы вовсе, на карточке заявки таких списков семь.
REFERENCE_WITHOUT_WORD: dict[tuple[str, str], str] = {
    # Ящики нужны только выбору отправителя и доступны одному root. Не ответило
    # — форма всё равно работает: сервер возьмёт первый активный ящик сам.
    ("Mail.tsx", "accounts"): "отказ ни на что не влияет: отправку делает сервер",
    ("ClientCard.tsx", "mailAccounts"): "то же: сервер возьмёт первый активный ящик",
    ("DealCard.tsx", "mailAccounts"): "то же: сервер возьмёт первый активный ящик",
    # Названия этапов в деле клиента рядом с перепиской. Не ответил — на
    # месте названия остаётся КЛЮЧ этапа («new», «in_work»): не пусто, не
    # выдумано и не выглядит выбором. Сказать «справочник не приехал» тут
    # значило бы поставить жалобу в узкую колонку рядом с перепиской,
    # отняв место у того, ради чего экран открыт.
    ("Telegram.tsx", "stages"): "запасной вид — сам ключ этапа, он честен и не пуст",
}


def test_a_reference_that_did_not_load_says_so():
    """Экран со справочником умеет отличить «записей нет» от «не спросили».

    Крючок `useReference` держит эти два состояния врозь (`items === null` и
    отдельное `failure`), но толку от этого нет, пока экран смотрит только на
    `items`. Так и было: окно новой заявки на упавшем сервере советовало
    «сначала заведите клиента» там, где их двести, а список должностей
    рисовался из одного пункта «Без роли» — и выбор этого единственного пункта
    СНИМАЛ должность по-настоящему.
    """
    lists, silent = 0, []
    for path in sorted(SCREENS.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"const (\w+) = useReference", text):
            name = match.group(1)
            lists += 1
            if (path.name, name) in REFERENCE_WITHOUT_WORD:
                continue
            if f"{name}.failure" not in text:
                silent.append(f"{path.name}: {name}")

    assert lists >= 15, f"справочников найдено {lists} — проверка смотрит не туда"
    assert not silent, (
        "справочник не приехал, а экран об этом молчит:\n" + "\n".join(silent)
        + "\n\nЛибо покажи LoadFailed, либо внеси в REFERENCE_WITHOUT_WORD с объяснением."
    )


def test_spisok_isklyuchyonnykh_ekranov_ne_protukh():
    """Исключённый экран обязан существовать и обязан нуждаться в исключении.

    Пока файл назван в `EXEMPT`, его стражи не проверяются вовсе. Запись, у
    которой экран переименовали или где отказ уже показывается, прикрывает
    пустоту — и молчащий страж, добавленный в тот же файл завтра, пройдёт
    незамеченным.

    Тот же разбор, что у списков исключений в `tests/test_db_boundary.py` и
    `tests/test_route_guards.py`: список «сюда не смотреть» гниёт быстрее
    прочего, потому что пополняют его осознанно, а вычищать некому.
    """
    ekrany = _screens_with_guard()

    propavshie = sorted(set(EXEMPT) - set(ekrany))
    assert propavshie == [], (
        "исключения указывают на экраны, которых нет или у которых больше нет "
        "стражей:\n  " + "\n  ".join(propavshie)
    )

    lishnie = sorted(
        imya for imya in EXEMPT
        if all("error={" in g and "onRetry={" in g for g in ekrany[imya])
    )
    assert lishnie == [], (
        "эти экраны уже показывают отказ — исключение им не нужно:\n  "
        + "\n  ".join(lishnie)
    )


def test_lenta_messendzhera_popolnyaetsya_tolko_sliyaniem():
    """Ни одно `setMessages` не дописывает в список напрямую.

    Одно и то же сообщение приезжает тремя независимыми путями: ответом ручки
    при отправке, событием шины и дочитыванием после обрыва. Пути обгоняют друг
    друга, поэтому любой из них рано или поздно привозит то, что уже показано.

    Так и было на живом показе: отправленный файл показывался в переписке
    дважды. В телеграм он уходил ОДИН раз — то есть задваивался показ, а со
    стороны это неотличимо от повторной отправки клиенту.

    Чинить порядок присваиваний бессмысленно — он чинится ровно до следующего
    пути доставки. Лечит только идемпотентность: лента — множество по `id`.
    Проверка механическая, потому что дописать `[...bylo, stroka]` в четвёртом
    месте проще, чем вспомнить, почему так нельзя.
    """
    text = (SCREENS / "screens" / "Telegram.tsx").read_text(encoding="utf-8")

    vyzovy = re.findall(r"setMessages\((.{0,80})", text, re.S)
    assert len(vyzovy) >= 5, f"вызовов setMessages разобрано {len(vyzovy)} — смотрим не туда"

    mimo = [v.strip().splitlines()[0] for v in vyzovy if "slit(" not in v]
    assert not mimo, (
        "лента пополняется мимо слияния по `id`, и одно сообщение покажется "
        "дважды:\n  " + "\n  ".join(mimo)
    )


def test_potok_sobytiy_otkryvaetsya_v_odnom_meste():
    """`new EventSource` живёт ровно в одном модуле.

    Слушателей у потока двое: оболочка приложения — ради сигнала о письме
    клиента (менеджер сидит в заявках, а не в переписке), и экран мессенджера —
    ради ленты. Открой каждый своё соединение, и на одну вкладку придётся два
    живых генератора на сервере, два переподключения после обрыва и два
    обработчика одного события.

    Проверка механическая, потому что соблазн написать `new EventSource` прямо
    в экране велик: это на строку короче, а расплата видна только на сервере
    под нагрузкой.
    """
    gde = []
    for put in sorted(SCREENS.rglob("*.ts*")):
        if "node_modules" in put.parts:
            continue
        text = put.read_text(encoding="utf-8")
        if "new EventSource" in text:
            gde.append(put.relative_to(SCREENS).as_posix())

    assert gde == ["lib/tgpotok.ts"], (
        "поток событий открывается не в одном месте, а в: " + ", ".join(gde)
    )


def test_kartochka_klienta_znaet_vse_vidy_zapisey_lenty():
    """Подпись и значок есть у КАЖДОГО вида записи, какой умеет заводить сервер.

    В самой карточке об этом написано прямо: «заметить пропуск здесь придётся
    глазами — записи ленты приходят нетипизированными, и `tsc` про новый вид не
    скажет». Он и не сказал: мессенджер завёл вид `telegram`, а таблица подписей
    о нём не узнала, и в ленте клиента запись выходила БЕЗ ПОДПИСИ — видно, что
    что-то было, а что именно, не сказано.

    Проверка снимает это с глаз: список видов читается у сервера, таблицы — у
    экрана, и расхождение краснеет.
    """
    model = (KOREN / "database" / "models" / "client.py").read_text(encoding="utf-8")
    karta = (SCREENS / "screens" / "ClientCard.tsx").read_text(encoding="utf-8")

    vidy = set(re.findall(r'"(\w+)"', re.search(r"NOTE_KINDS = \(([^)]*)\)", model).group(1)))
    sistemnye = re.search(r"SYSTEM_NOTE_KINDS = \(([^)]*)\)", model).group(1)
    # Системные заданы через постоянные (`KIND_STAGE`), а не строками: берём их
    # значения из тех же строк объявления.
    for imya in re.findall(r"(KIND_\w+)", sistemnye):
        znachenie = re.search(rf'{imya} = "(\w+)"', model)
        if znachenie:
            vidy.add(znachenie.group(1))

    assert len(vidy) >= 7, f"видов записей разобрано {len(vidy)} — проверка смотрит не туда"

    for imya_tablicy in ("NOTE_LABELS", "NOTE_ICONS"):
        telo = re.search(rf"{imya_tablicy}[^=]*= \{{(.*?)\n\}};", karta, re.S).group(1)
        est = set(re.findall(r"(\w+):", telo))
        propali = sorted(vidy - est)
        assert not propali, (
            f"в {imya_tablicy} нет видов записей, которые заводит сервер: "
            + ", ".join(propali)
            + ". В ленте клиента такая запись выходит без подписи или без значка."
        )
