"""В репозитории не лежит файла с CRLF — иначе самообновление встаёт навсегда.

**Беда, ради которой это написано** (найдена репетицией обновления 03.09.2026).
`docs/README.md` уехал в коммит с `\\r\\n`, хотя `.gitattributes` объявляет
`*.md text`. На Linux git пишет в рабочую копию байты объекта как есть, а
считает её содержимое обратно уже нормализованным — и файл числится изменённым
ВСЕГДА, сколько его ни возвращай `git checkout`.

Дальше срабатывает `deploy/updater.py`: перед обновлением он отказывается
работать с грязным деревом, чтобы не затереть чужую правку. Первое обновление
проходит (грязь появляется после его же `checkout`), а каждое следующее падает
на preflight. Самообновление — главное обещание продукта — останавливается до
человека с консолью, и остановка эта необъяснима: `git status` показывает
правку, которой никто не делал.

Второй облик той же беды уже был: `.sh` с CRLF даёт `/bin/sh^M` и «bad
interpreter». Там поймал shellcheck, здесь ловить было некому.

**Откуда CRLF берётся.** Разработка идёт с Windows, где `core.autocrlf=true`.
Питон, открывший файл на запись без `newline=""`, переводит каждый `\\n` в
`\\r\\n` — и правка трёх строк делает CRLF-ным весь файл. Прочитать так же и
записать обратно — получить `\\r\\r\\n`, что и лежало в `docs/README.md`.
"""

import pathlib
import subprocess

import pytest

KOREN = pathlib.Path(__file__).resolve().parent.parent

#: Истории рядом нет в воротах деплоя: `.dockerignore` исключает `.git/`, и это
#: правильно — она весит больше кода. Проверка идёт в CI, см. тест ниже.
EST_ISTORIYA = (KOREN / ".git").exists()

bez_istorii = pytest.mark.skipif(
    not EST_ISTORIYA,
    reason="рядом нет .git — концы строк в объектах не прочитать (в образе её и нет)",
)

VOROTA = KOREN / ".github" / "workflows" / "tests.yml"

bez_vorot = pytest.mark.skipif(
    not VOROTA.exists(),
    reason="рядом нет .github/workflows — ворота CI не прочитать (в образе их и нет)",
)


def _opis() -> list[tuple[str, str, str]]:
    """`git ls-files --eol` разобранный: (конец строки в объекте, свойство, путь)."""
    itog = subprocess.run(
        ["git", "ls-files", "--eol"],
        cwd=KOREN,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    razobrano = []
    for stroka in itog.stdout.splitlines():
        if not stroka.strip():
            continue
        # Формат: `i/lf<таб>w/lf<таб>attr/text<таб>путь`. Путь отделён последним
        # табом, а в самом свойстве пробелы есть («text eol=lf»).
        kuski = stroka.split("\t")
        if len(kuski) < 2:
            continue
        put = kuski[-1].strip()
        polya = kuski[0].split()
        v_obekte = next((p[2:] for p in polya if p.startswith("i/")), "")
        svoystvo = next((p[5:] for p in polya if p.startswith("attr/")), "")
        razobrano.append((v_obekte, svoystvo, put))
    return razobrano


@bez_istorii
def test_perebor_vidit_fayly():
    """Сторож, читающий пустоту, зеленеет на любой беде."""
    opis = _opis()
    assert len(opis) > 300, (
        f"`git ls-files --eol` вернул {len(opis)} строк — разбор не сошёлся с "
        "форматом, и проверка ниже стерегла бы пустоту"
    )
    assert any(put == "docs/README.md" for _, _, put in opis), (
        "в описи нет docs/README.md — разбор берёт не тот столбец"
    )
    assert any("text" in svoystvo for _, svoystvo, _ in opis), (
        "ни у одного файла не прочиталось свойство text — разбор берёт не тот столбец"
    )


@bez_istorii
def test_v_obektakh_net_vozvrata_karetki():
    nayden = [
        (put, v_obekte, svoystvo)
        for v_obekte, svoystvo, put in _opis()
        if v_obekte in ("crlf", "mixed")
    ]
    assert not nayden, (
        "в объектах репозитория лежит возврат каретки — рабочая копия на Linux "
        "будет числиться изменённой всегда, и обновление встанет на preflight:\n  "
        + "\n  ".join(f"{put}: i/{eol}, attr/{attr}" for put, eol, attr in nayden)
        + "\nЛечится так: убрать `\\r` из файла и добавить его заново. Питон "
        'пишет файлы только с `newline=""`, иначе он сам их и портит.'
    )


@bez_vorot
def test_vorota_ci_gonyayut_nabor_s_istoriey():
    """Проверка выше в воротах деплоя пропускается — значит её обязан гонять CI.

    Пропуск без замены это не осторожность, а тишина: сторож, которого нигде не
    зовут, ничем не отличается от ненаписанного.
    """
    tekst = VOROTA.read_text(encoding="utf-8")
    assert "actions/checkout" in tekst, (
        "в воротах CI нет выкладки исходников — значит нет и .git, и проверка "
        "концов строк не гоняется нигде"
    )
    assert "fetch-depth: 0" in tekst, (
        "выкладка в CI без полной истории: `git ls-files` отработает, но соседние "
        "проверки по истории — нет; глубина задаётся один раз на всех"
    )
