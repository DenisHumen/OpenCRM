"""Отчёт об обновлении: два файла в телеграм вместо одной строки «не поднялось».

Проверяется здесь три вещи, и первая важнее двух остальных вместе взятых:

1. **Отчёт не имеет права уронить обновление.** Он приятная добавка; сообщение
   владельцу важнее её, а работающий сайт важнее их обоих.
2. Файлы получаются настоящими — PDF открывается и отдаёт кириллицу, `.docx`
   разбирается как ZIP с нужными частями.
3. Многочастная форма собрана так, как её ждёт телеграм: своими руками, потому
   что `urllib` этого не умеет, а зависимостей у `deploy/` нет и не будет.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from deploy import dokumenty, notify, otchyot


# --- двойники --------------------------------------------------------------


class Shag:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name, self.ok, self.detail = name, ok, detail


class Ishod:
    """Двойник `Outcome`: отчёту нужны только поля, и лишнего он не спрашивает."""

    def __init__(self, status="rolled-back", udacha=False, **pravki):
        self.status = status
        self.from_sha = "9" * 40
        self.to_sha = "d" * 40
        self.summary = "правка почты и прав"
        self.reason = "health-check не прошёл за 30 попыток — 502"
        self.seconds = 754.0
        self.steps = [
            Shag("preflight", True, "свободно 78339 МБ"),
            Shag("backup", True, "pre-update-dddddddddddd.sql (1289 МБ, MySQL)"),
            Shag("health", False, "health-check не прошёл за 30 попыток — 502"),
        ]
        self.ok = udacha
        for imya, znachenie in pravki.items():
            setattr(self, imya, znachenie)


class Nastroyki:
    repo = "DenisHumen/OpenCRM"
    branch = "main"


ZHURNAL = [
    "обновление 9999 → dddd правка почты и прав",
    "снят дамп базы: pre-update-dddddddddddd.sql (1289 МБ)",
    "health-check попытка 30/30: 502",
    "контейнер сказал: sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 5 reached",
]


# --- файлы получаются настоящими --------------------------------------------


def test_sobirayutsya_oba_fayla():
    fayly = otchyot.sdelat_fayly(Ishod(), Nastroyki(), ZHURNAL)
    imena = [imya for imya, _ in fayly]
    assert len(fayly) == 2, f"собралось не два файла: {imena}"
    assert any(i.endswith(".pdf") for i in imena), imena
    assert any(i.endswith(".docx") for i in imena), imena
    # Имя несёт номер коммита: в переписке эти файлы копятся десятками.
    assert all("dddddddddddd" in i for i in imena), imena


def test_pdf_eto_nastoyashchiy_pdf():
    """Подпись, перекрёстная таблица и метка конца — то, по чему его узнают."""
    (_, pdf), _ = _po_vidam(otchyot.sdelat_fayly(Ishod(), Nastroyki(), ZHURNAL))
    assert pdf.startswith(b"%PDF-1."), pdf[:16]
    assert pdf.rstrip().endswith(b"%%EOF"), pdf[-16:]
    assert b"startxref" in pdf
    # Шрифт вшит: без него кириллицы в PDF не бывает вовсе.
    assert b"/FontFile2" in pdf
    assert b"/Identity-H" in pdf


def test_smeshcheniya_v_perekrestnoy_tablice_vernye():
    """Каждое смещение обязано указывать на начало своего объекта.

    Просмотрщики разные: один прочитает файл и с битой таблицей, восстановив её
    перебором, другой откажется. Проверяем сами — иначе «у меня открывается»
    станет единственной проверкой.
    """
    (_, pdf), _ = _po_vidam(otchyot.sdelat_fayly(Ishod(), Nastroyki(), ZHURNAL))

    nachalo = int(re.search(rb"startxref\s+(\d+)", pdf).group(1))
    tablica = pdf[nachalo:]
    assert tablica.startswith(b"xref"), tablica[:32]

    stroki = re.findall(rb"(\d{10}) (\d{5}) ([nf])", tablica)
    zhivye = [int(s[0]) for s in stroki if s[2] == b"n"]
    assert len(zhivye) >= 5, f"объектов в таблице подозрительно мало: {len(zhivye)}"

    for nomer, smeshchenie in enumerate(zhivye, start=1):
        kusok = pdf[smeshchenie : smeshchenie + 24]
        assert kusok.startswith(f"{nomer} 0 obj".encode()), (
            f"смещение объекта {nomer} указывает не туда: {kusok!r}"
        )


def test_iz_pdf_izvlekaetsya_kirillica():
    """Из отчёта об аварии текст ошибки КОПИРУЮТ — значит он должен копироваться.

    Без карты `ToUnicode` файл рисуется верно, но копируется мусором: внутри
    лежат номера глифов. Замерено `pdftotext` — до карты он выдавал
    «γφϒϓχϐϊϒύϊ» вместо «Обновление».
    """
    (_, pdf), _ = _po_vidam(otchyot.sdelat_fayly(Ishod(), Nastroyki(), ZHURNAL))
    assert b"/ToUnicode" in pdf

    # Карта обязана называть настоящие знаки, а не быть пустой заготовкой.
    karta = re.search(rb"beginbfchar(.*?)endbfchar", pdf, re.S)
    assert karta is not None, "карты ToUnicode нет вовсе"
    pary = re.findall(rb"<([0-9A-F]{4})> <([0-9A-F]+)>", karta.group(1))
    assert len(pary) > 20, f"в карте всего {len(pary)} знаков"
    znaki = {bytes.fromhex(k.decode()).decode("utf-16-be") for _, k in pary}
    assert znaki & set("Обновление"), f"кириллицы в карте нет: {sorted(znaki)[:20]}"


def test_docx_razbiraetsya_kak_dokument_word():
    _, (_, docx) = _po_vidam(otchyot.sdelat_fayly(Ishod(), Nastroyki(), ZHURNAL))
    with zipfile.ZipFile(io.BytesIO(docx)) as arhiv:
        imena = set(arhiv.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= imena, imena
        telo = arhiv.read("word/document.xml").decode("utf-8")
    assert telo.startswith("<?xml"), telo[:40]
    assert "Обновление откатилось" in telo
    assert "health-check" in telo


def test_oba_fayla_rasskazyvayut_odno_i_to_zhe():
    """PDF и Word собираются одним кодом — значит и содержат одно и то же.

    Две копии раскладки разъехались бы на первой правке одной из них, и владелец
    получил бы два файла об одном событии с разным содержанием.
    """
    (_, pdf), (_, docx) = _po_vidam(otchyot.sdelat_fayly(Ishod(), Nastroyki(), ZHURNAL))
    with zipfile.ZipFile(io.BytesIO(docx)) as arhiv:
        telo = arhiv.read("word/document.xml").decode("utf-8")

    for kusok in ("preflight", "backup", "health", "DenisHumen/OpenCRM"):
        assert kusok in telo, f"в Word нет {kusok!r}"
        assert kusok.encode() in pdf or _v_pdf(pdf, kusok), f"в PDF нет {kusok!r}"


def _v_pdf(pdf: bytes, kusok: str) -> bool:
    """Есть ли строка в PDF. Текст лежит номерами глифов, поэтому через карту."""
    karta = re.search(rb"beginbfchar(.*?)endbfchar", pdf, re.S)
    if karta is None:
        return False
    znaki = {
        bytes.fromhex(k.decode()).decode("utf-16-be")
        for _, k in re.findall(rb"<([0-9A-F]{4})> <([0-9A-F]+)>", karta.group(1))
    }
    return set(kusok) <= znaki


def _po_vidam(fayly):
    pdf = next(f for f in fayly if f[0].endswith(".pdf"))
    docx = next(f for f in fayly if f[0].endswith(".docx"))
    return pdf, docx


# --- содержание отвечает на вопрос «что делать» ------------------------------


@pytest.mark.parametrize(
    "status,zhdyom",
    [
        ("broken", "doctor"),
        ("rolled-back", "прежней версии"),
        ("deployed", "Ничего делать не нужно"),
    ],
)
def test_sovet_svoy_na_kazhdyy_ishod(status, zhdyom):
    """«Посмотрите логи» одинаково бесполезно при откате и при сломанном сайте.

    Действия там противоположные: после отката сайт работает и спешить некуда,
    после `broken` — не работает и нужен человек сейчас.
    """
    ishod = otchyot.sobrat_ishod(Ishod(status=status), Nastroyki(), ZHURNAL)
    sovet = " ".join(ishod.chto_dalshe)
    assert zhdyom in sovet, f"для {status} совет: {sovet!r}"


def test_zhurnal_podrezaetsya_s_hvosta():
    """Беда всегда в хвосте: начало журнала одинаково у удачи и у неудачи."""
    dlinnyy = [f"строка {n}" for n in range(otchyot.STROK_ZHURNALA * 3)]
    ishod = otchyot.sobrat_ishod(Ishod(), Nastroyki(), dlinnyy)
    assert len(ishod.zhurnal) == otchyot.STROK_ZHURNALA
    assert ishod.zhurnal[-1] == dlinnyy[-1], "обрезали хвост вместо начала"


def test_ochen_dlinnaya_stroka_ne_uezzhaet_za_kray():
    """SQL целиком в одну строку — обычное дело в журнале."""
    ogromnaya = "SELECT " + "x" * 5000
    ishod = otchyot.sobrat_ishod(Ishod(), Nastroyki(), [ogromnaya])
    assert len(ishod.zhurnal[0]) <= otchyot.ZNAKOV_V_STROKE + 2


# --- многочастная форма ------------------------------------------------------


class Otvet:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_forma_sobrana_tak_kak_zhdyot_telegram():
    """Границы, поля и часть с файлом — руками, потому что `urllib` не умеет."""
    poymano = {}

    def otkryt(request, timeout=None):  # noqa: ARG001
        poymano["url"] = request.full_url
        poymano["telo"] = request.data
        poymano["tip"] = request.get_header("Content-type")
        return Otvet()

    bot = notify.Telegram("token-123", "chat-456", opener=otkryt)
    assert bot.send_document("otchyot.pdf", b"%PDF-1.4 proba", "Разбор неудачи") is True

    assert poymano["url"].endswith("/bottoken-123/sendDocument")
    granica = re.search(r"boundary=(\S+)", poymano["tip"]).group(1)
    telo = poymano["telo"]

    assert telo.startswith(f"--{granica}\r\n".encode())
    assert telo.rstrip().endswith(f"--{granica}--".encode())
    assert b'name="chat_id"' in telo and b"chat-456" in telo
    assert b'name="document"; filename="otchyot.pdf"' in telo
    assert b"%PDF-1.4 proba" in telo
    assert b'name="caption"' in telo


def test_slishkom_bolshoy_fayl_ne_otpravlyaetsya():
    """Телеграм режет на 50 МиБ. Упереться в это лучше у себя, чем у него."""
    bot = notify.Telegram("t", "c", opener=lambda *a, **k: Otvet())
    assert bot.send_document("ogromnyy.pdf", b"x" * (notify.PREDEL_FAYLA + 1)) is False
    assert bot.send_document("pustoy.pdf", b"") is False


def test_zaglushka_umeet_prinimat_fayl():
    """Канал не настроен — молчим, а не падаем на отсутствующем приёме."""
    assert notify.Silent().send_document("a.pdf", b"x") is False


# --- главное: отчёт не роняет обновление -------------------------------------


def test_slomannyy_pisatel_ne_ronyaet_otchyot(monkeypatch):
    """Не собрался PDF — уходит Word. Отчёт наполовину лучше, чем никакого."""

    class Bityy:
        def __init__(self, *a, **k):
            raise RuntimeError("шрифт не читается")

    monkeypatch.setattr(otchyot, "PDF", Bityy)
    fayly = otchyot.sdelat_fayly(Ishod(), Nastroyki(), ZHURNAL)
    imena = [imya for imya, _ in fayly]
    assert imena and all(i.endswith(".docx") for i in imena), imena


def test_propavshiy_shrift_ne_ronyaet_otchyot(monkeypatch):
    """Шрифта нет — PDF не собрать, но Word обязан уйти."""
    monkeypatch.setattr(dokumenty, "SHRIFT", dokumenty.SHRIFT.parent / "netu.ttf")
    monkeypatch.setattr(otchyot, "SHRIFT", dokumenty.SHRIFT.parent / "netu.ttf")
    fayly = otchyot.sdelat_fayly(Ishod(), Nastroyki(), ZHURNAL)
    assert [i for i, _ in fayly] == [
        i for i, _ in fayly if i.endswith(".docx")
    ], "PDF собрался без шрифта — значит проверка смотрит не туда"


def test_shrift_lezhit_ryadom_s_kodom():
    """Шрифт — часть поставки, а не находка в системе.

    На голой Ubuntu шрифтов нет вовсе (проверено на `python:3.12-slim`:
    каталога `/usr/share/fonts/truetype` не существует). «Нашли — хорошо, не
    нашли — отчёт без кириллицы» было бы худшим из решений: беда обнаружилась бы
    в день аварии.
    """
    assert dokumenty.SHRIFT.is_file(), f"нет {dokumenty.SHRIFT}"
    assert dokumenty.SHRIFT.read_bytes()[:4] == b"\x00\x01\x00\x00", "это не TrueType"
