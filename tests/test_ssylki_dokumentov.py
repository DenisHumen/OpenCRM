"""Ссылки на документы ведут в существующие файлы.

**Зачем сторож.** 06.09.2026 `docs/` разложили по каталогам и переименовали
файлы: 24 документа, 258 упоминаний по репозиторию — в самих документах, в
комментариях кода, в тестах, в скриптах и в справке интерфейса. Ссылка,
уехавшая от файла, не падает ничем: страница открывается, комментарий читается,
а путь в нём ведёт в пустоту, и узнаёт об этом тот, кто пошёл читать разбор.

Три проверки: относительные ссылки внутри md-файлов; упоминания `docs/…md` в
любом отслеживаемом файле; указатель `docs/README.md` знает каждый документ.
Четвёртая — заголовок документа начинается с его номера: по номерам задан
порядок чтения, и файл без номера в заголовке выпадает из него.
"""
import pathlib
import re
import subprocess

KOREN = pathlib.Path(__file__).resolve().parent.parent
DOCS = KOREN / "docs"
SSYLKA = re.compile(r"\]\(([^)\s]+)")
#: Упоминание пути документа в тексте любого файла: каталог, имя, `.md`.
UPOMINANIE = re.compile(r"docs/[\w./-]+?\.md")
NOMER = re.compile(r"^(\d\d)-")

#: Выгруженные миграции не правят (`test_migratsii_ne_pravyat`), поэтому в их
#: комментариях остались прежние имена документов. Соответствие прежних имён
#: новым — таблица «Прежние имена» в `docs/README.md`.
ZAMOROZHENO = ("database/migrations/versions/",)


def _dokumenty() -> list[pathlib.Path]:
    return sorted(p for p in DOCS.rglob("*.md") if NOMER.match(p.name))


def _md_fayly() -> list[pathlib.Path]:
    korennye = [KOREN / imya for imya in ("README.md", "README.ru.md", "CLAUDE.md")]
    return sorted(DOCS.rglob("*.md")) + [p for p in korennye if p.exists()]


def _otslezhivaemye() -> list[pathlib.Path]:
    vyvod = subprocess.run(
        ["git", "ls-files", "-z"], cwd=KOREN, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [KOREN / f for f in vyvod.split("\0") if f]


def test_perebor_dokumentov_ne_pustoy():
    assert len(_dokumenty()) >= 20, "документы не нашлись — сторож смотрит не туда"


def test_otnositelnye_ssylki_vedut_v_sushchestvuyushchie_fayly():
    bityye = []
    for fayl in _md_fayly():
        for m in SSYLKA.finditer(fayl.read_text(encoding="utf-8")):
            target = m.group(1)
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            put = target.partition("#")[0]
            if not put:
                continue
            if not (fayl.parent / put).exists():
                bityye.append(f"{fayl.relative_to(KOREN)} → {target}")
    assert bityye == [], "ссылки в пустоту:\n  " + "\n  ".join(bityye)


def test_upominaniya_dokumentov_v_kode_vedut_v_sushchestvuyushchie_fayly():
    bityye = []
    for fayl in _otslezhivaemye():
        otnositelnyy = fayl.relative_to(KOREN).as_posix()
        if not fayl.is_file() or fayl.name == pathlib.Path(__file__).name or otnositelnyy.startswith(ZAMOROZHENO):
            continue
        try:
            text = fayl.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        for put in set(UPOMINANIE.findall(text)):
            if not (KOREN / put).exists():
                bityye.append(f"{fayl.relative_to(KOREN)}: {put}")
    assert bityye == [], "упоминания документов, которых нет:\n  " + "\n  ".join(sorted(bityye))


def test_ukazatel_znaet_kazhdyy_dokument():
    ukazatel = (DOCS / "README.md").read_text(encoding="utf-8")
    nazvannye = {
        (DOCS / m.group(1)).resolve() for m in SSYLKA.finditer(ukazatel) if m.group(1).endswith(".md")
    }
    propushchennye = [p.relative_to(DOCS).as_posix() for p in _dokumenty() if p.resolve() not in nazvannye]
    assert propushchennye == [], f"документы не названы в docs/README.md: {propushchennye}"


def test_zagolovok_dokumenta_nachinaetsya_s_nomera():
    ne_te = []
    for p in _dokumenty():
        nomer = NOMER.match(p.name).group(1)
        zagolovok = next((s for s in p.read_text(encoding="utf-8").splitlines() if s.startswith("# ")), "")
        if not zagolovok.startswith(f"# {nomer} — "):
            ne_te.append(f"{p.relative_to(DOCS).as_posix()}: {zagolovok[:60]!r}")
    assert ne_te == [], "заголовок без номера документа:\n  " + "\n  ".join(ne_te)
