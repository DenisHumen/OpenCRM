"""Класс в разметке обязан существовать в стилях.

**Это сторож на беду, которую не видит ничто другое.** Несуществующее имя
класса не ломает ни типы, ни сборку, ни один тест: браузер молча не находит
правило и раскладывает всё подряд. Экран при этом «работает» — по нему можно
ходить, — но выглядит развалившимся, и узнают об этом от человека, который его
открыл.

Так и вышло: экран настроек бота был размечен классами `screen`,
`settings-screen`, `field`, `hint`, `row`. Ни одного из них в стилях нет. Поля,
подписи и пояснения слиплись в одну строку, кнопки уехали влево. Ни типы, ни
сборка, ни полсотни соседних проверок этого не заметили.

Проверка разбирает только СТРОКОВЫЕ `className="..."`. Имена, собранные
выражением, здесь не проверить: они складываются на ходу, и статически про них
известно не больше, чем браузеру.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
ISTOCHNIK = ROOT / "web" / "frontend" / "crm" / "src"
STILI = ISTOCHNIK / "styles.css"

#: Классы, у которых стилей нет намеренно, и почему.
#:
#: Законный повод один: класс — не оформление, а ярлык. Оформление у такого
#: узла задано встроенным стилем или он служит только зацепкой для разметки.
BEZ_STILEY: dict[str, str] = {
    "theme-pick": (
        "ярлык группы переключателей темы в профиле: всё оформление задано "
        "встроенным `style`, класс нужен как имя для чтения разметки"
    ),
}


def _izvestnye_klassy() -> set[str]:
    """Все имена классов, встречающиеся в таблице стилей."""
    return set(re.findall(r"\.([a-z][a-z0-9-]{1,40})", STILI.read_text(encoding="utf-8")))


def _klassy_razmetki() -> dict[str, set[str]]:
    """Имя класса → файлы, где оно встречается строкой."""
    nayd: dict[str, set[str]] = {}
    for put in sorted(ISTOCHNIK.rglob("*.tsx")):
        tekst = put.read_text(encoding="utf-8")
        for kusok in re.findall(r'className="([^"{}]+)"', tekst):
            for imya in kusok.split():
                nayd.setdefault(imya, set()).add(put.name)
    return nayd


def test_kazhdyy_klass_razmetki_est_v_stilyakh():
    """Ни одного имени класса без правила в стилях."""
    izvestnye = _izvestnye_klassy()
    assert len(izvestnye) > 100, (
        f"в стилях найдено {len(izvestnye)} классов — проверка смотрит не туда"
    )

    razmetka = _klassy_razmetki()
    assert len(razmetka) > 50, (
        f"в разметке найдено {len(razmetka)} классов — проверка смотрит не туда"
    )

    # Подчёркивание — отдельный разговор, и говорить его надо ПЕРВЫМ.
    #
    # Образец, по которому собраны известные классы, подчёркивания не знает:
    # `.zaliv-polosa__hod` он читает как `zaliv-polosa` и обрывается. То есть
    # класс с `__`, СТИЛЬ ДЛЯ КОТОРОГО ЕСТЬ, объявлялся бы бездомным, и человек
    # шёл бы искать несуществующую пропажу — так и вышло однажды.
    #
    # Расширять образец не стали: в проекте нет ни одного класса с `__`, имена
    # везде через дефис, и заводить второе соглашение ради одного случая
    # незачем. Но молчать об этом нельзя — отказ должен называть настоящую
    # причину, а не выдуманную.
    s_podchyorkivaniem = sorted(
        f"{imya} — {', '.join(sorted(fayly))}"
        for imya, fayly in razmetka.items()
        if "_" in imya and imya not in BEZ_STILEY
    )
    assert s_podchyorkivaniem == [], (
        "класс с подчёркиванием в имени:\n  "
        + "\n  ".join(s_podchyorkivaniem)
        + "\n\nВ проекте имена классов пишутся через дефис — двойного "
        "подчёркивания нет ни у одного из сотен существующих. Дело не во вкусе: "
        "проверка ниже собирает известные классы образцом без `_`, и класс с "
        "подчёркиванием она объявит бездомным, даже когда правило для него "
        "написано. Переименуйте через дефис."
    )

    bezdomnye = [
        f"{imya} — {', '.join(sorted(fayly))}"
        for imya, fayly in sorted(razmetka.items())
        if imya not in izvestnye and imya not in BEZ_STILEY
    ]
    assert bezdomnye == [], (
        "класс есть в разметке, а правила для него нет:\n  "
        + "\n  ".join(bezdomnye)
        + "\n\nБраузер молча не найдёт правило и разложит узлы как придётся: "
        "экран будет работать и выглядеть развалившимся.\n"
        "Либо возьмите существующий класс, либо заведите правило, либо внесите "
        "в BEZ_STILEY с объяснением, почему оформления не нужно."
    )


#: Свойства, без которых `<select>` остаётся стоковым — тем, что рисует браузер.
#:
#: `appearance` снимает системный вид целиком (без него не убрать ни системную
#: стрелку, ни системный шрифт), `background-image` ставит взамен свою стрелку,
#: остальное — «как соседнее поле»: рамка, скругление, цвет текста, шрифт.
ODEZHDA_SPISKA = (
    "appearance",
    "background-image",
    "border",
    "border-radius",
    "color",
    "font-family",
)

_KOMMENTARIY = re.compile(r"/\*.*?\*/", re.S)
_PRAVILO = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
#: Простой селектор: без пробелов, `>`, `+`, `~` — то есть накрывающий узел сам,
#: а не через предка. Правило вида `.tg-link select` одевает список только в
#: одном месте экрана и общим приёмом не считается.
_SOSTAVNOY = re.compile(r"[\s>+~]")


def _svoystva(telo: str) -> dict[str, str]:
    """Свойства правила. Имя без вендорной приставки: `-webkit-appearance`
    делает ровно то же, что `appearance`, и считаться должен за него."""
    nayd: dict[str, str] = {}
    for kusok in telo.split(";"):
        if ":" not in kusok:
            continue
        imya, znachenie = kusok.split(":", 1)
        nayd[re.sub(r"^-\w+-", "", imya.strip().lower())] = znachenie.strip()
    return nayd


def _pravila_stiley() -> list[tuple[list[str], dict[str, str]]]:
    """(селекторы, свойства) для каждого правила таблицы стилей."""
    css = _KOMMENTARIY.sub("", STILI.read_text(encoding="utf-8"))
    return [
        ([s.strip() for s in selektor.split(",") if s.strip()], _svoystva(telo))
        for selektor, telo in _PRAVILO.findall(css)
    ]


def _telo_tega(tekst: str, nachalo: int) -> str:
    """Открывающий тег целиком, от `<select` до его `>`.

    Считать до первого `>` нельзя: в JSX он встречается внутри выражений
    (`onChange={(e) => …}`). Поэтому `>` признаётся концом тега только вне
    фигурных скобок.
    """
    glubina = 0
    for i in range(nachalo, len(tekst)):
        znak = tekst[i]
        if znak == "{":
            glubina += 1
        elif znak == "}":
            glubina -= 1
        elif znak == ">" and glubina == 0:
            return tekst[nachalo:i]
    return tekst[nachalo:]


def _spiski_razmetki() -> list[tuple[str, set[str]]]:
    """Каждый `<select>` разметки: где он и какими классами одет."""
    nayd: list[tuple[str, set[str]]] = []
    for put in sorted(ISTOCHNIK.rglob("*.tsx")):
        tekst = put.read_text(encoding="utf-8")
        for spichka in re.finditer(r"<select\b", tekst):
            teg = _telo_tega(tekst, spichka.start())
            klassy: set[str] = set()
            for kusok in re.findall(r'className="([^"{}]+)"', teg):
                klassy.update(kusok.split())
            gde = f"{put.name}:{tekst[: spichka.start()].count(chr(10)) + 1}"
            nayd.append((gde, klassy))
    return nayd


def test_u_kazhdogo_vypadayushchego_spiska_est_odezhda():
    """Ни одного `<select>` в стоковом виде браузера.

    Беда та же, что у бездомного класса, только тише: несуществующего правила
    здесь нет вовсе — браузер просто рисует свой элемент. Экран работает, но
    посреди полей CRM стоит чужая деталь: системный шрифт, системная стрелка, а
    в тёмной теме ещё и светлый прямоугольник. Заметно это только глазами, и
    только если открыть именно этот экран.

    Держит проверка общий приём: правило на сам `select` рядом с `.input`
    одевает все списки разом, и новый список на новом экране одет с рождения.
    Уберите это правило — и покраснеет здесь, а не у человека, открывшего экран.
    Правило через предка (`.tg-link select`) за одежду не считается: оно
    действует в одном месте, а списки заводят везде.
    """
    pravila = _pravila_stiley()
    assert len(pravila) > 100, (
        f"в стилях разобрано {len(pravila)} правил — проверка смотрит не туда"
    )

    spiski = _spiski_razmetki()
    assert len(spiski) > 20, (
        f"в разметке найдено {len(spiski)} выпадающих списков — проверка смотрит не туда"
    )

    def nakryvaet(selektor: str, klassy: set[str]) -> bool:
        # Состояния (`:hover`, `:disabled`) — надстройка над базовым видом, и
        # искать одежду надо в нём, иначе «одет» окажется список, у которого
        # оформлено только наведение.
        if _SOSTAVNOY.search(selektor) or ":" in selektor:
            return False
        # Условие в скобках (`select[class]`) сужает, кому правило достанется,
        # но не меняет, чем оно одевает.
        chasti = re.sub(r"\[[^\]]*\]", "", selektor).split(".")
        if chasti[0] not in ("", "select"):
            return False
        return set(chasti[1:]) <= klassy

    razdetye = []
    for gde, klassy in spiski:
        odezhda: dict[str, str] = {}
        for selektory, svoystva in pravila:
            if any(nakryvaet(s, klassy) for s in selektory):
                odezhda.update(svoystva)
        ne_khvataet = [imya for imya in ODEZHDA_SPISKA if imya not in odezhda]
        if ne_khvataet:
            razdetye.append(f"{gde} ({' '.join(sorted(klassy)) or 'без класса'}) — нет: {', '.join(ne_khvataet)}")

    assert razdetye == [], (
        "выпадающий список остался в стоковом виде браузера:\n  "
        + "\n  ".join(razdetye)
        + "\n\nОдежда для всех списков одна — правило `select` рядом с `.input` "
        "в styles.css. Классу остаются только размеры."
    )


def test_v_spiske_bez_stiley_net_lishnikh():
    """Названный класс обязан встречаться в разметке.

    Иначе запись переживёт свой узел и станет памяткой о прошлом, а настоящее
    бездомное имя пройдёт мимо проверки под её прикрытием.
    """
    razmetka = _klassy_razmetki()
    propavshie = sorted(set(BEZ_STILEY) - set(razmetka))
    assert propavshie == [], (
        "в списке классов без стилей записи, которых нет в разметке: "
        + ", ".join(propavshie)
    )
