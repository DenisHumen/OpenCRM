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
