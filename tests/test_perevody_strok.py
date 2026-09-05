"""Ни одного текстового файла со смешанными переводами строк в репозитории.

Беда 06.09.2026: в `web/public/routes.py` одна строка приехала в репозиторий с
CRLF среди LF. На Windows это невидимо (autocrlf делает всю копию CRLF, а
лишний CR прячется как `\\r\\r\\n`), а на Linux `git checkout` кладёт блоб как
есть, `*.py text` нормализует рабочую копию при сравнении — и дерево «грязное»
сразу после чекаута. Обновлятор на боевом сервере видит «несохранённые
правки» и отказывается обновляться, а `git checkout -- .` не лечит: чекаут
снова пишет тот же блоб.

Два способа смотреть, и оба про блоб, а не про рабочую копию:

- есть `.git` — спрашиваем сам git (`ls-files --eol`): колонка `i/` говорит,
  что лежит в индексе. Рабочая копия на Windows тут не годится: после
  autocrlf она целиком CRLF, а правки скриптами оставляют в ней LF-строки,
  которые при `git add` нормализуются и до блоба не доезжают;
- `.git` нет (образ ворот) — байты файлов: чекаут на Linux пишет блоб как
  есть, и смешанный файл там значит смешанный блоб.
"""

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Текстовые расширения — те же, что объявлены `text` в `.gitattributes`.
TEKSTOVYE = {
    ".py", ".md", ".toml", ".ini", ".html", ".css", ".ts", ".tsx", ".json",
    ".sh", ".service", ".timer", ".yml", ".yaml", ".conf", ".template", ".inc",
}
#: Не исходники: окружения, сборки, данные, чужой код.
MIMO = {
    ".git", ".venv", "venv", "node_modules", "dist", "__pycache__", ".pytest_cache",
    ".ruff_cache", "data", "storage", "tmp", "shablony",
}

LONE_CR = re.compile(rb"\r(?!\n)")


def _po_baytam() -> list[str]:
    vinovnye = []
    for put in sorted(ROOT.rglob("*")):
        if any(chast in MIMO for chast in put.relative_to(ROOT).parts):
            continue
        if not put.is_file() or put.suffix not in TEKSTOVYE:
            continue
        b = put.read_bytes()
        crlf = b.count(b"\r\n")
        lf = b.count(b"\n") - crlf
        lone = len(LONE_CR.findall(b))
        if lone or (crlf and lf):
            vinovnye.append(f"{put.relative_to(ROOT).as_posix()}: CRLF {crlf}, LF {lf}, одиночных CR {lone}")
    return vinovnye


def _po_indeksu() -> list[str] | None:
    """Что лежит в индексе git; `None` — git или `.git` недоступны."""
    if not (ROOT / ".git").exists():
        return None
    try:
        otvet = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--eol"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if otvet.returncode != 0:
        return None
    vinovnye = []
    for stroka in otvet.stdout.splitlines():
        # `i/lf w/crlf attr/text  путь` — три колонки и путь через табуляцию.
        chasti = stroka.split("\t", 1)
        if len(chasti) != 2:
            continue
        kolonki, put = chasti[0].split(), chasti[1]
        indeks = next((k for k in kolonki if k.startswith("i/")), "")
        attr = next((k for k in kolonki if k.startswith("attr/")), "")
        if "text" not in attr or "-text" in attr:
            continue
        if indeks in ("i/mixed", "i/crlf"):
            vinovnye.append(f"{put}: в индексе {indeks[2:]}")
    return vinovnye


def test_perebor_vidit_fayly():
    """Сторож, ничего не нашедший, зеленеет на любой беде."""
    schyot = sum(
        1 for put in ROOT.rglob("*")
        if put.is_file() and put.suffix in TEKSTOVYE
        and not any(chast in MIMO for chast in put.relative_to(ROOT).parts)
    )
    assert schyot > 200


def test_ni_odnogo_fayla_so_smeshannymi_perevodami_strok():
    vinovnye = _po_indeksu()
    if vinovnye is None:
        vinovnye = _po_baytam()
    assert vinovnye == [], (
        "смешанные переводы строк — на Linux такой файл «грязный» сразу после чекаута, "
        "и обновлятор откажется обновляться:\n  " + "\n  ".join(vinovnye)
        + "\nЛечится перезаписью файла с одними LF и `git add` (autocrlf нормализует)."
    )


def test_baytovyy_perebor_lovit_smeshannoe(tmp_path, monkeypatch):
    """Ветка без `.git` (образ ворот) обязана ловить то же самое."""
    koren = tmp_path / "proekt"
    (koren / "core").mkdir(parents=True)
    (koren / "core" / "chistyy.py").write_bytes(b"a = 1\nb = 2\n")
    (koren / "core" / "smeshannyy.py").write_bytes(b"a = 1\r\nb = 2\n")
    (koren / "core" / "odinochnyy.py").write_bytes(b"a = 1\rb = 2\n")
    monkeypatch.setattr("tests.test_perevody_strok.ROOT", koren)
    naydeno = _po_baytam()
    assert [v.split(":")[0] for v in naydeno] == ["core/odinochnyy.py", "core/smeshannyy.py"]
