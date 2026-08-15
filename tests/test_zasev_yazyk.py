"""Данные и надписи — на языке интерфейса, а не на языке разработчика.

Интерфейс продукта по умолчанию **английский**, а продукт — для любого малого
дела, не только русскоязычного. Значит человек, поставивший его и увидевший
английские экраны, обязан увидеть и английские данные: этапы воронки, склад,
роли, статьи расходов, названия заказов.

Так не было, и мест оказалось десять, а не четыре, как показалось сначала.
Разбор шёл в три захода, и каждый следующий находил то, чего не видел
предыдущий:

1. **Пресеты в коде** — воронка, склад, названия заказов, статьи финансов.
   Нашлось глазами при сборке витрины для README.
2. **Миграции** — и вот это главное. Свежая установка в докере получает данные
   ИМЕННО оттуда: точка входа гонит `alembic upgrade head` по пустой базе, все
   ревизии отрабатывают вместе с засевом, а сервисные `seed_defaults`
   запускаются после и молчат, потому что строки уже на месте. Перевод одних
   пресетов не изменил бы ничего — до кода дело не доходит. Нашлось не чтением,
   а вопросом «а кто, собственно, сеет на боевой машине».
3. **Страница обслуживания и словари локалей.** Страница была наполовину
   английской, наполовину русской, а в английской ветке словаря витрины лежала
   русская фраза (и наоборот — строки просто перепутали местами). Оба места
   выглядели заполненными, и отличить перепутанное от переведённого чтением
   нельзя.

**Почему именно английский, а не «по настройке».** Языка установки в продукте
нет: `showcase_locale` — это язык ПУБЛИЧНОЙ витрины для клиента, а язык CRM
выбирает каждый сотрудник себе. Данные же общие на всех, и выбрать для них язык
можно только один. Английский — тот, что человек видит на экранах по умолчанию;
русскоязычный владелец переключает интерфейс и переименовывает, к чему подсказка
пресета прямо и приглашает.

Проверки механические, потому что одиннадцатое такое место появится тем же
путём, что и первые десять: кто-то напишет строку на своём языке, и это никого
не остановит. Последняя из них — единственная, что смотрит не в исходники, а в
получившуюся базу: рассуждение по исходникам здесь один раз уже подвело.
"""

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Кириллица в строковом значении.
KIRILLITSA = re.compile(r"[А-Яа-яЁё]")

#: Где именно смотреть: файл → места, которые засевают.
#:
#: Список руками, и это осознанно: «засеваемое» по коду не отличить от прочих
#: строк, а ошибиться тут стоит дороже, чем перечислить. От протухания списка
#: стережёт `test_nazvannye_mesta_zaseva_sushchestvuyut`.
#:
#: Не файл целиком, и это важно. В тех же файлах есть русский текст, который
#: пишется в историю движений НА ХОДУ («отгрузка по заказу 42», «перевезено
#: сверх остатка»). Он тоже виден в английском интерфейсе и тоже спорен — но
#: это другая и куда большая работа, и втягивать её сюда молча значило бы
#: подменить вопрос, на который отвечает проверка.
#:
#: Имя — либо переменная уровня модуля, либо функция. Пустой набор означал бы
#: «смотреть весь файл», и такого здесь нет намеренно.
ZASEVAYUT: dict[str, set[str]] = {
    "core/services/pipeline_service.py": {"PRESETS"},
    "core/services/permissions_service.py": {"PRESETS"},
    "core/services/warehouse_service.py": {"seed_defaults"},
    "core/services/order_service.py": {"_title"},
    "core/services/act_service.py": {"DEFAULT_TITLE"},
    "core/services/arcade_service.py": {"_clean_name", "ANON_NAME"},
    "database/migrations/versions/d4e1a83c2f60_finance.py": {"SEED"},
    "database/migrations/versions/b2c8e4f1a396_warehouses.py": {"upgrade"},
    "database/migrations/versions/b7d94f2ae610_pipeline_stages.py": {"AGENCY", "UNIVERSAL"},
    "database/migrations/versions/c5e19a3d7b46_roles.py": {"_seed_manager_role"},
}


def _stroki_koda(put: pathlib.Path, imena: set[str]) -> list[tuple[int, str]]:
    """Строковые значения ТОЛЬКО в названных местах — без докстрок.

    Комментарии в этом проекте по-русски по правилу, и ловить их было бы
    ловлей собственного правила. Докстроки — тоже проза, и они отбрасываются:
    докстрока лежит первым узлом тела, и её видно по дереву.
    """
    derevo = ast.parse(put.read_text(encoding="utf-8"))

    interesnye = []
    for uzel in derevo.body:
        if isinstance(uzel, (ast.FunctionDef, ast.AsyncFunctionDef)) and uzel.name in imena:
            interesnye.append(uzel)
        elif isinstance(uzel, ast.Assign):
            for tsel in uzel.targets:
                if isinstance(tsel, ast.Name) and tsel.id in imena:
                    interesnye.append(uzel)
        elif isinstance(uzel, ast.AnnAssign):
            if isinstance(uzel.target, ast.Name) and uzel.target.id in imena:
                interesnye.append(uzel)

    dokstroki = set()
    for koren in interesnye:
        for uzel in ast.walk(koren):
            if isinstance(uzel, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                telo = uzel.body
                if telo and isinstance(telo[0], ast.Expr) and isinstance(telo[0].value, ast.Constant):
                    dokstroki.add(id(telo[0].value))

    nayd = []
    for koren in interesnye:
        for uzel in ast.walk(koren):
            if (
                isinstance(uzel, ast.Constant)
                and isinstance(uzel.value, str)
                and id(uzel) not in dokstroki
            ):
                nayd.append((uzel.lineno, uzel.value))
    return nayd


@pytest.mark.parametrize("rel", sorted(ZASEVAYUT))
def test_zasev_ne_na_russkom(rel: str):
    """Ни одной кириллической строки среди значений — только английские."""
    put = ROOT / rel
    assert put.exists(), f"{rel} — файла нет, список засева устарел"

    vinovnye = [
        f"{rel}:{nomer}  {znachenie!r}"
        for nomer, znachenie in _stroki_koda(put, ZASEVAYUT[rel])
        if KIRILLITSA.search(znachenie)
    ]
    assert vinovnye == [], (
        "в базу засевается русский текст, а интерфейс по умолчанию английский:\n  "
        + "\n  ".join(vinovnye)
        + "\nДанные общие на всех, и язык у них может быть только один."
    )


def test_perebor_zasevayushchikh_faylov_ne_pustoy():
    """Пустой список сделал бы проверку выше зелёной и бессмысленной."""
    est = [rel for rel in ZASEVAYUT if (ROOT / rel).exists()]
    assert len(est) == len(ZASEVAYUT), (
        "часть файлов засева пропала — список устарел: "
        + ", ".join(sorted(set(ZASEVAYUT) - set(est)))
    )


def test_storozh_vidit_kirillitsu():
    """Сам сторож обязан находить то, что ищет.

    Проверка на проверку: если разбор перестанет доставать строковые значения
    (сменится форма файла, уедет обход дерева), тест выше позеленеет на любом
    коде. Ровно этим сегодня уже обжигались — перебор, потерявший цель,
    выглядит как перебор, у которого всё в порядке.
    """
    obrazets = ROOT / "core" / "services" / "pipeline_service.py"
    znacheniya = [z for _, z in _stroki_koda(obrazets, {"PRESETS"})]
    assert znacheniya, "разбор не достал ни одной строки — сторож ослеп"
    assert any("Universal" in z for z in znacheniya), (
        "разбор не видит названий пресетов — проверка смотрит не туда"
    )


def test_nazvannye_mesta_zaseva_sushchestvuyut():
    """Названное место обязано существовать — иначе проверка смотрит в пустоту.

    Переименуют функцию или переменную, и проверка выше станет зелёной на любом
    коде: смотреть ей будет не на что. Тот же разбор, что у списков исключений
    в `test_db_boundary.py` и `test_route_guards.py`, только наоборот — там
    запись прикрывала пустоту, здесь пустота обесценивает саму проверку.
    """
    propavshie = []
    for rel, imena in sorted(ZASEVAYUT.items()):
        put = ROOT / rel
        if not put.exists():
            propavshie.append(f"{rel} — файла нет")
            continue
        derevo = ast.parse(put.read_text(encoding="utf-8"))
        est = set()
        for uzel in derevo.body:
            if isinstance(uzel, (ast.FunctionDef, ast.AsyncFunctionDef)):
                est.add(uzel.name)
            elif isinstance(uzel, ast.Assign):
                est |= {t.id for t in uzel.targets if isinstance(t, ast.Name)}
            elif isinstance(uzel, ast.AnnAssign) and isinstance(uzel.target, ast.Name):
                est.add(uzel.target.id)
        propavshie += [f"{rel}:{imya}" for imya in sorted(imena - est)]

    assert propavshie == [], (
        "проверка засева смотрит на то, чего нет:\n  " + "\n  ".join(propavshie)
    )


#: Страница обслуживания — тот же вопрос, но за пределами питона.
#:
#: Её видит КТО УГОДНО: nginx отдаёт её на каждый свой 502/503/504, в том числе
#: посетителю публичной витрины, который про CRM ничего не знает. Заголовок там
#: был английский, а подвал, змейка и таблица рекордов — русские, то есть
#: половина страницы говорила на языке разработчика.
#:
#: Языка у страницы нет и быть не может: она статическая, отдаётся без
#: приложения (в том числе когда приложение лежит) и про вошедшего человека не
#: знает ничего. Значит язык у неё один — тот же, что на экранах по умолчанию.
STRANITSA_OBSLUZHIVANIYA = "docker/nginx/maintenance/maintenance.html"

#: Кириллица, которая остаётся законно: буквы русской раскладки для WASD.
#: Это ВВОД, а не надпись — `e.key` отдаёт букву по текущей раскладке.
RAZRESHENO_V_RAZMETKE = ("turn(0, -1)", "turn(0, 1)", "turn(-1, 0)", "turn(1, 0)")


def _bez_kommentariev(tekst: str) -> str:
    """Разметка без комментариев — html, css и js."""
    tekst = re.sub(r"<!--.*?-->", "", tekst, flags=re.S)
    tekst = re.sub(r"/\*.*?\*/", "", tekst, flags=re.S)
    tekst = re.sub(r"(?m)^\s*//.*$", "", tekst)
    return re.sub(r"(?m)\s//[^\"'\n]*$", "", tekst)


def test_stranitsa_obsluzhivaniya_govorit_na_odnom_yazyke():
    """Ни одной русской надписи — только буквы раскладки в управлении игрой."""
    put = ROOT / STRANITSA_OBSLUZHIVANIYA
    assert put.exists(), f"{STRANITSA_OBSLUZHIVANIYA} — файла нет"

    vinovnye = [
        stroka.strip()
        for stroka in _bez_kommentariev(put.read_text(encoding="utf-8")).split("\n")
        if KIRILLITSA.search(stroka)
        and not any(znak in stroka for znak in RAZRESHENO_V_RAZMETKE)
    ]
    assert vinovnye == [], (
        "на странице обслуживания русские надписи, а интерфейс английский:\n  "
        + "\n  ".join(vinovnye)
        + "\nЭту страницу видит любой посетитель, и языка у неё только один."
    )


def test_razbor_razmetki_ne_vyedaet_lishnego():
    """Сам разбор обязан оставлять надписи — иначе проверка выше слепа.

    Выедание комментариев regex-ом хрупко: чуть изменится форма файла, и
    «ничего не нашлось» будет означать «нечего было искать». Поэтому опыт с
    заведомо известным ответом.
    """
    put = ROOT / STRANITSA_OBSLUZHIVANIYA
    ostalos = _bez_kommentariev(put.read_text(encoding="utf-8"))

    assert "We&rsquo;ll be right back" in ostalos, "разбор съел заголовок страницы"
    assert "Leaderboard" in ostalos, "разбор съел таблицу рекордов"
    assert 'k === "ц"' in ostalos, "разбор съел управление игрой"
    assert "nginx отдаёт эту страницу" not in ostalos, "разбор не выел комментарий"


# --- словари языков ---------------------------------------------------------


def _angliyskie_vetki(put: pathlib.Path) -> list[tuple[int, str]]:
    """Строки под ключом `"en"` в любом словаре файла.

    Правило без списка исключений, и потому не протухающее: где бы в коде ни
    стоял словарь с веткой `"en"`, эта ветка обязана быть английской. Больше
    правилу знать ничего не нужно.
    """
    derevo = ast.parse(put.read_text(encoding="utf-8"))
    nayd = []
    for uzel in ast.walk(derevo):
        if not isinstance(uzel, ast.Dict):
            continue
        for klyuch, znachenie in zip(uzel.keys, uzel.values):
            if not (isinstance(klyuch, ast.Constant) and klyuch.value == "en"):
                continue
            for vnutri in ast.walk(znachenie):
                if isinstance(vnutri, ast.Constant) and isinstance(vnutri.value, str):
                    nayd.append((vnutri.lineno, vnutri.value))
    return nayd


def _fayly_koda() -> list[pathlib.Path]:
    puti = []
    for vetka in ("core", "database", "web", "scripts", "deploy", "config"):
        puti += sorted((ROOT / vetka).rglob("*.py"))
    return [p for p in puti if "__pycache__" not in p.parts]


def test_angliyskaya_vetka_slovarey_bez_kirillitsy():
    """Ветка `"en"` любого словаря — по-английски.

    Печатные бланки, заголовки выгрузок и витрина уже переведены как надо: у
    каждого свой словарь с ветками `en`/`ru`/`uk`. Ломается такое не переписью,
    а дописью: добавляют ключ, переводят соседние ветки и забывают эту, потому
    что подсказки об этом нет ниоткуда. Здесь она есть.
    """
    vinovnye = []
    for put in _fayly_koda():
        rel = put.relative_to(ROOT).as_posix()
        vinovnye += [
            f"{rel}:{nomer}  {znachenie!r}"
            for nomer, znachenie in _angliyskie_vetki(put)
            if KIRILLITSA.search(znachenie)
        ]
    assert vinovnye == [], (
        "в английской ветке словаря лежит русский текст:\n  " + "\n  ".join(vinovnye)
    )


def test_perebor_slovarey_nakhodit_izvestnye():
    """Перебор обязан видеть словари, про которые известно, что они есть.

    Иначе «ничего не нашлось» будет означать «не туда смотрел» — ровно тот
    отказ, которым эта проверка и занимается.
    """
    obrazets = ROOT / "web" / "public" / "document_strings.py"
    znacheniya = [z for _, z in _angliyskie_vetki(obrazets)]
    assert znacheniya, "перебор не нашёл английскую ветку словаря бланков"
    assert any("Date" == z for z in znacheniya), "перебор нашёл не ту ветку"

    vsego = sum(len(_angliyskie_vetki(p)) for p in _fayly_koda())
    assert vsego > 50, f"английских строк в словарях всего {vsego} — перебор ослеп"


def test_v_imenakh_koda_net_kirillitsy():
    """Ни одной кириллической буквы в именах питона — во всём дереве.

    Питон разрешает кириллицу в идентификаторах, и это ловушка, а не свобода:
    `kudа` с русской «а» на вид неотличимо от `kuda`, компилируется, работает и
    молчит у pyflakes. Пока имя используется единообразно, всё цело; стоит
    кому-то дописать рядом латинскую версию — и получаются две разные
    переменные с одинаковым видом.

    Проверка заведена не из осторожности: за одну сессию так промахнулись
    ДВАЖДЫ, оба раза в этом же наборе, и оба раза ни один инструмент не сказал
    ни слова. Комментарии и строки правило не трогает — там кириллица по
    правилу проекта; речь только об именах.
    """
    vinovnye = []
    for put in _fayly_koda() + sorted((ROOT / "tests").rglob("*.py")):
        rel = put.relative_to(ROOT).as_posix()
        derevo = ast.parse(put.read_text(encoding="utf-8"))
        for uzel in ast.walk(derevo):
            imya = None
            if isinstance(uzel, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                imya = uzel.name
            elif isinstance(uzel, ast.Name):
                imya = uzel.id
            elif isinstance(uzel, ast.arg):
                imya = uzel.arg
            elif isinstance(uzel, ast.Attribute):
                imya = uzel.attr
            elif isinstance(uzel, ast.keyword) and uzel.arg:
                imya = uzel.arg
            if imya and KIRILLITSA.search(imya):
                vinovnye.append(f"{rel}:{getattr(uzel, 'lineno', 0)}  {imya!r}")

    assert vinovnye == [], (
        "в именах кода кириллические буквы — на вид неотличимы от латинских:\n  "
        + "\n  ".join(sorted(set(vinovnye)))
    )


def test_stranitsa_i_server_zovut_bezymyannogo_odinakovo():
    """Одно слово на двоих — у страницы обслуживания и у сервера.

    Страница статическая: её отдаёт nginx, когда приложения нет, и разделить с
    ней константу нечем. Значит совпадение держится не устройством, а этой
    проверкой. Расходились они молча: поле предлагало одно имя, сервер писал
    другое, и в таблице рекордов оказывались два разных безымянных игрока —
    заметить это можно было только сыграв дважды.
    """
    razmetka = (ROOT / STRANITSA_OBSLUZHIVANIYA).read_text(encoding="utf-8")
    sluzhba = (ROOT / "core" / "services" / "arcade_service.py").read_text(encoding="utf-8")

    imya = re.search(r'ANON_NAME\s*=\s*"([^"]+)"', sluzhba)
    assert imya, "в arcade_service пропала ANON_NAME — проверке не с чем сверять"
    slovo = imya.group(1)

    assert f'placeholder="{slovo}"' in razmetka, (
        f"поле на странице предлагает не {slovo!r} — сервер запишет другое имя"
    )
    assert f'row.name || "{slovo}"' in razmetka, (
        f"вывод строки на странице подставляет не {slovo!r}"
    )
    assert f'name: name || "{slovo}"' in razmetka, (
        f"отправка счёта со страницы подставляет не {slovo!r}"
    )


# --- опыт вместо рассуждения ------------------------------------------------


def test_svezhaya_ustanovka_poluchaet_angliyskie_dannye(chistaya_baza):
    """Пустая база + `alembic upgrade head` = английские данные. Проверено, не выведено.

    Это единственная проверка здесь, которая смотрит на РЕЗУЛЬТАТ, а не на
    исходники, и заведена она потому, что рассуждение по исходникам один раз уже
    подвело. Перевести пресеты в `core/services/` казалось достаточным — а
    достаточным это не было: свежая установка в докере получает данные из
    миграций. Точка входа гонит `alembic upgrade head` по пустой базе, все
    ревизии отрабатывают вместе с засевом, и сервисные `seed_defaults`
    запускаются уже после — видят строки на месте и молча выходят.

    То есть перевод в коде НЕ МЕНЯЛ НИЧЕГО на том единственном пути, ради
    которого затевался. Отличить одно от другого чтением нельзя: оба места
    выглядят как засев, оба выполняются при старте. Отличает только эта
    проверка — она строит базу ровно так, как это делает контейнер, и читает,
    что в ней оказалось.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", chistaya_baza)
    command.upgrade(config, "head")

    dvigatel = create_engine(chistaya_baza)
    russkie = []
    try:
        with dvigatel.connect() as soedinenie:
            for tablitsa, kolonka in (
                ("warehouses", "name"),
                ("pipeline_stages", "name"),
                ("roles", "name"),
                ("finance_categories", "name"),
            ):
                stroki = soedinenie.execute(
                    text(f"SELECT {kolonka} FROM {tablitsa}")  # noqa: S608 — имена свои
                ).scalars().all()
                assert stroki, f"{tablitsa} пуста — засев не отработал, проверять нечего"
                russkie += [
                    f"{tablitsa}.{kolonka} = {znachenie!r}"
                    for znachenie in stroki
                    if znachenie and KIRILLITSA.search(znachenie)
                ]
    finally:
        dvigatel.dispose()

    assert russkie == [], (
        "свежая установка получила русские данные при английском интерфейсе:\n  "
        + "\n  ".join(russkie)
    )
