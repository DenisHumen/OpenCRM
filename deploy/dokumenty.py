"""Отчёт об обновлении двумя файлами: PDF и Word. Только стандартная библиотека.

**Почему написано руками, а не взято готовым.** Обновлятор запускается НА ХОСТЕ
системным `python3` (`*/5 * * * * /usr/bin/python3 scripts/autoupdate.py check`),
без venv и без единой зависимости проекта — весь пакет `deploy/` сегодня обходится
`urllib`, `json` и `subprocess`. Поставить туда `python-docx` и `fpdf2` значило бы
завести на боевой машине вторую установку пакетов, которую надо чинить отдельно от
контейнера и которая молча отвалится при первом же обновлении системы.

**И есть довод сильнее удобства.** Отчёт о НЕУДАЧНОМ обновлении обязан
получиться именно тогда, когда всё сломалось. Собери мы его внутри контейнера
приложения — он не собрался бы ровно в том случае, ради которого затевался:
контейнер не поднялся, отчитываться нечем, владелец получает «сломано» без единой
подробности. Отчёт делает тот, кто пережил аварию, — хост.

Тот же довод, по которому проект пишет дамп базы сам (`scripts/snapshot_db.py`):
клиента `mysqldump` в образе нет и не будет.

--------------------------------------------------------------------------
Про кириллицу в PDF
--------------------------------------------------------------------------
Четырнадцать стандартных шрифтов PDF кириллицы не содержат вовсе — ни одного
знака. Поэтому шрифт **вшивается в файл**: `deploy/shrifty/DejaVuSans.ttf`,
лицензия Bitstream Vera, распространение разрешено прямо.

Вшивается целиком, без выкусывания нужных знаков. Выкусывание сэкономило бы
семьсот килобайт на файл, но потребовало бы пересборки таблиц `glyf` и `loca` —
это ровно тот код, ошибка в котором даёт файл, который откроется у нас и не
откроется у владельца. Семьсот килобайт в телеграме не стоят такого риска.

В Word шрифт не вшивается: `.docx` — это XML в кодировке UTF-8, а начертание
подбирает сам Word из системных. Оттого и разная сложность у двух писателей.
"""

from __future__ import annotations

import io
import struct
import zipfile
from datetime import datetime
from pathlib import Path

#: Шрифт для PDF. Лежит рядом с кодом, а не ищется в системе: на голой Ubuntu
#: шрифтов нет вовсе (проверено на `python:3.12-slim` — каталога
#: `/usr/share/fonts/truetype` не существует), и «нашли — хорошо, не нашли —
#: отчёт без кириллицы» было бы худшим из решений.
SHRIFT = Path(__file__).parent / "shrifty" / "DejaVuSans.ttf"

# --- цвета отчёта ------------------------------------------------------------
#
# Держим их в одном месте и одинаковыми для обоих писателей: два набора цветов
# разъедутся при первой же правке одного из них, и два файла об одном событии
# станут выглядеть как два разных документа.
CVET_UDACHA = (0x1B, 0x7F, 0x3B)   # зелёный: шаг прошёл
CVET_BEDA = (0xC0, 0x27, 0x2B)     # красный: шаг не прошёл
CVET_TEKST = (0x1A, 0x1A, 0x1A)
CVET_TIHIY = (0x6B, 0x6B, 0x6B)    # подписи, время, служебное
CVET_LINIYA = (0xD8, 0xD8, 0xD8)
CVET_FON_SHAPKI = (0xF2, 0xF4, 0xF7)


# =============================================================================
# Разбор TTF: ровно то, что нужно PDF, и ни строкой больше
# =============================================================================


class Shrift:
    """Начертание, разобранное настолько, насколько это нужно PDF.

    Нужно четыре вещи: сколько единиц в кегле (`head.unitsPerEm`), ширина
    каждого знака (`hmtx`), соответствие «символ → номер глифа» (`cmap`) и рамка
    с высотами для описателя шрифта. Всё остальное в файле шрифта PDF не
    спрашивает — он вшивает файл целиком и разбирает его сам.
    """

    def __init__(self, put: Path):
        self.bayty = put.read_bytes()
        self._tablicy = self._prochitat_oglavlenie()

        # head: единицы кегля и рамка.
        head = self._tablica("head")
        self.edinic = struct.unpack(">H", head[18:20])[0]
        self.ramka = struct.unpack(">hhhh", head[36:44])

        # maxp: сколько всего глифов.
        self.glifov = struct.unpack(">H", self._tablica("maxp")[4:6])[0]

        # hhea: сколько глифов имеют собственную ширину, и высоты.
        hhea = self._tablica("hhea")
        self.vverh, self.vniz = struct.unpack(">hh", hhea[4:8])
        s_shirinoy = struct.unpack(">H", hhea[34:36])[0]

        self.shiriny = self._prochitat_shiriny(s_shirinoy)
        self.karta = self._prochitat_kartu()

    # --- разбор оглавления файла ---------------------------------------------

    def _prochitat_oglavlenie(self) -> dict[str, tuple[int, int]]:
        tablic = struct.unpack(">H", self.bayty[4:6])[0]
        itog = {}
        for nomer in range(tablic):
            nachalo = 12 + nomer * 16
            imya = self.bayty[nachalo : nachalo + 4].decode("latin-1").strip()
            smeshchenie, dlina = struct.unpack(">II", self.bayty[nachalo + 8 : nachalo + 16])
            itog[imya] = (smeshchenie, dlina)
        return itog

    def _tablica(self, imya: str) -> bytes:
        if imya not in self._tablicy:
            raise ValueError(f"в шрифте нет таблицы {imya!r} — файл не годится для PDF")
        smeshchenie, dlina = self._tablicy[imya]
        return self.bayty[smeshchenie : smeshchenie + dlina]

    # --- ширины ---------------------------------------------------------------

    def _prochitat_shiriny(self, s_shirinoy: int) -> list[int]:
        """Ширина каждого глифа в единицах кегля.

        В `hmtx` собственную ширину имеют только первые `numberOfHMetrics`
        глифов; у всех остальных она равна ширине последнего из них. Так
        устроен формат: у моноширинных хвост из тысяч одинаковых значений не
        хранится вовсе.
        """
        hmtx = self._tablica("hmtx")
        shiriny = []
        for nomer in range(min(s_shirinoy, self.glifov)):
            shiriny.append(struct.unpack(">H", hmtx[nomer * 4 : nomer * 4 + 2])[0])
        poslednyaya = shiriny[-1] if shiriny else self.edinic // 2
        while len(shiriny) < self.glifov:
            shiriny.append(poslednyaya)
        return shiriny

    # --- соответствие «символ → глиф» -----------------------------------------

    def _prochitat_kartu(self) -> dict[int, int]:
        """Карта Unicode → номер глифа. Берём подтаблицу формата 4.

        Формат 4 — тот, в котором лежит основная многоязычная плоскость, и
        кириллица в том числе. Формат 12 (за пределами плоскости) нам не нужен:
        в отчёте об обновлении нет ни эмодзи, ни редких письменностей.
        """
        cmap = self._tablica("cmap")
        podtablic = struct.unpack(">H", cmap[2:4])[0]

        nuzhnaya = None
        for nomer in range(podtablic):
            nachalo = 4 + nomer * 8
            platforma, kodirovka = struct.unpack(">HH", cmap[nachalo : nachalo + 4])
            smeshchenie = struct.unpack(">I", cmap[nachalo + 4 : nachalo + 8])[0]
            format_ = struct.unpack(">H", cmap[smeshchenie : smeshchenie + 2])[0]
            if format_ != 4:
                continue
            # (3,1) — Windows Unicode BMP, самая надёжная; (0,*) — Unicode.
            if (platforma, kodirovka) == (3, 1) or platforma == 0:
                nuzhnaya = smeshchenie
                if (platforma, kodirovka) == (3, 1):
                    break
        if nuzhnaya is None:
            raise ValueError("в шрифте нет карты символов формата 4")

        return self._razobrat_format4(cmap, nuzhnaya)

    @staticmethod
    def _razobrat_format4(cmap: bytes, nachalo: int) -> dict[int, int]:
        polovina = struct.unpack(">H", cmap[nachalo + 6 : nachalo + 8])[0]
        otrezkov = polovina // 2

        def slova(smeshchenie: int) -> list[int]:
            kusok = cmap[nachalo + smeshchenie : nachalo + smeshchenie + otrezkov * 2]
            return list(struct.unpack(f">{otrezkov}H", kusok))

        konec = slova(14)
        start = slova(16 + otrezkov * 2)
        delta = slova(16 + otrezkov * 4)
        smeshcheniya_nachalo = nachalo + 16 + otrezkov * 6
        smeshcheniya = list(
            struct.unpack(f">{otrezkov}H", cmap[smeshcheniya_nachalo : smeshcheniya_nachalo + otrezkov * 2])
        )

        karta: dict[int, int] = {}
        for nomer in range(otrezkov):
            for simvol in range(start[nomer], min(konec[nomer], 0xFFFF) + 1):
                if smeshcheniya[nomer] == 0:
                    glif = (simvol + delta[nomer]) & 0xFFFF
                else:
                    mesto = (
                        smeshcheniya_nachalo
                        + nomer * 2
                        + smeshcheniya[nomer]
                        + (simvol - start[nomer]) * 2
                    )
                    if mesto + 2 > len(cmap):
                        continue
                    glif = struct.unpack(">H", cmap[mesto : mesto + 2])[0]
                    if glif:
                        glif = (glif + delta[nomer]) & 0xFFFF
                if glif:
                    karta[simvol] = glif
        return karta

    # --- то, чем пользуется писатель PDF --------------------------------------

    def glify(self, tekst: str) -> list[int]:
        """Номера глифов для строки. Незнакомый знак заменяем вопросом.

        Заменяем, а не пропускаем: пропущенный знак делает строку короче и
        незаметно меняет смысл, а вопросительный виден глазом и просит починки.
        """
        zapas = self.karta.get(ord("?"), 0)
        return [self.karta.get(ord(z), zapas) for z in tekst]

    def shirina(self, tekst: str, kegl: float) -> float:
        """Ширина строки в точках при данном кегле."""
        vsego = sum(self.shiriny[g] if g < len(self.shiriny) else 0 for g in self.glify(tekst))
        return vsego * kegl / self.edinic


# =============================================================================
# Писатель PDF
# =============================================================================

#: A4 в точках. Целыми: дробная страница не бывает.
SHIRINA_STRANICY = 595
VYSOTA_STRANICY = 842
POLE = 48


def _pdf_stroka(tekst: str) -> bytes:
    """Строка PDF в скобках, с экранированием того, что ломает разбор."""
    ekran = tekst.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return ekran.encode("latin-1", "replace")


class PDF:
    """Складывает страницы и выдаёт готовый файл.

    Устроено просто нарочно: объекты копятся списком, перекрёстная таблица
    считается в конце по накопленным смещениям. Ни сжатия, ни потоков объектов —
    отчёт об обновлении весит десятки килобайт, и экономия на нём не окупает
    ни строчки лишнего кода.
    """

    def __init__(self, shrift: Shrift):
        self.shrift = shrift
        self.stranicy: list[bytes] = []
        self._tekushchaya = io.BytesIO()
        self._y = VYSOTA_STRANICY - POLE
        # Какие глифы пошли в дело и каким знакам они отвечают. Нужно для карты
        # `ToUnicode`, без которой из отчёта нельзя скопировать ни строчки:
        # текст в файле лежит номерами глифов, и без обратной карты просмотрщик
        # выдаёт при копировании мусор. Для документа, из которого будут
        # выдёргивать текст ошибки в поиск, это не мелочь.
        #
        # Копим только использованное: полная карта на шесть тысяч глифов
        # весила бы больше самого отчёта и ничего бы не добавила.
        self._ispolzovano: dict[int, str] = {}

    # --- складывание содержимого ---------------------------------------------

    def _shestnadcat(self, stroka: str) -> str:
        """Строка номерами глифов — и заодно отметка, что эти глифы в ходу."""
        glify = self.shrift.glify(stroka)
        for glif, znak in zip(glify, stroka):
            self._ispolzovano[glif] = znak
        return "".join(f"{g:04X}" for g in glify)

    def _mesto(self, nuzhno: float) -> None:
        """Хватит ли места до низа; не хватит — начинаем новую страницу."""
        if self._y - nuzhno < POLE:
            self.novaya_stranica()

    def novaya_stranica(self) -> None:
        self.stranicy.append(self._tekushchaya.getvalue())
        self._tekushchaya = io.BytesIO()
        self._y = VYSOTA_STRANICY - POLE

    def tekst(
        self,
        stroka: str,
        kegl: float = 10,
        cvet: tuple[int, int, int] = CVET_TEKST,
        otstup: float = 0,
        snizu: float = 4,
    ) -> None:
        self._mesto(kegl + snizu)
        self._y -= kegl
        shestnadcat = self._shestnadcat(stroka)
        r, g, b = (z / 255 for z in cvet)
        self._tekushchaya.write(
            f"BT /F1 {kegl} Tf {r:.3f} {g:.3f} {b:.3f} rg "
            f"{POLE + otstup} {self._y} Td <{shestnadcat}> Tj ET\n".encode("latin-1")
        )
        self._y -= snizu

    def zagolovok(self, stroka: str) -> None:
        self.otstup(6)
        self.tekst(stroka, kegl=15, snizu=8)

    def otstup(self, skolko: float) -> None:
        self._mesto(skolko)
        self._y -= skolko

    def liniya(self) -> None:
        self._mesto(8)
        self._y -= 6
        r, g, b = (z / 255 for z in CVET_LINIYA)
        self._tekushchaya.write(
            f"{r:.3f} {g:.3f} {b:.3f} RG 0.7 w {POLE} {self._y} m "
            f"{SHIRINA_STRANICY - POLE} {self._y} l S\n".encode("latin-1")
        )
        self._y -= 6

    def plashka(self, stroka: str, cvet: tuple[int, int, int]) -> None:
        """Крупная цветная плашка — итог одним взглядом."""
        vysota = 34
        self._mesto(vysota + 10)
        self._y -= vysota
        r, g, b = (z / 255 for z in cvet)
        self._tekushchaya.write(
            f"{r:.3f} {g:.3f} {b:.3f} rg {POLE} {self._y} "
            f"{SHIRINA_STRANICY - 2 * POLE} {vysota} re f\n".encode("latin-1")
        )
        shestnadcat = self._shestnadcat(stroka)
        self._tekushchaya.write(
            f"BT /F1 15 Tf 1 1 1 rg {POLE + 14} {self._y + 11} Td "
            f"<{shestnadcat}> Tj ET\n".encode("latin-1")
        )
        self._y -= 10

    def stroka_shaga(self, imya: str, proshel: bool, podrobnost: str) -> None:
        """Один шаг обновления: галочка или крест, имя, подробность."""
        znak = "OK" if proshel else "X"
        cvet = CVET_UDACHA if proshel else CVET_BEDA
        self._mesto(14)
        self._y -= 11
        r, g, b = (z / 255 for z in cvet)
        znak_glify = self._shestnadcat(znak)
        imya_glify = self._shestnadcat(imya)
        self._tekushchaya.write(
            f"BT /F1 9 Tf {r:.3f} {g:.3f} {b:.3f} rg {POLE} {self._y} Td "
            f"<{znak_glify}> Tj ET\n".encode("latin-1")
        )
        tr, tg, tb = (z / 255 for z in CVET_TEKST)
        self._tekushchaya.write(
            f"BT /F1 10 Tf {tr:.3f} {tg:.3f} {tb:.3f} rg {POLE + 26} {self._y} Td "
            f"<{imya_glify}> Tj ET\n".encode("latin-1")
        )
        self._y -= 3
        if podrobnost:
            for kusok in _razbit(podrobnost, self.shrift, 8.5, SHIRINA_STRANICY - 2 * POLE - 26):
                self.tekst(kusok, kegl=8.5, cvet=CVET_TIHIY, otstup=26, snizu=2)

    def para(self, imya: str, znachenie: str) -> None:
        """Строка «имя — значение» в шапке: подпись серым, значение чёрным.

        Двумя цветами, а не одним: в шапке восемь строк подряд, и глаз ищет в
        них значение, а не подпись. Одинаковый цвет заставляет читать всё.
        """
        self._mesto(14)
        self._y -= 11
        r, g, b = (z / 255 for z in CVET_TIHIY)
        podpis = self._shestnadcat(imya)
        self._tekushchaya.write(
            f"BT /F1 9 Tf {r:.3f} {g:.3f} {b:.3f} rg {POLE} {self._y} Td "
            f"<{podpis}> Tj ET\n".encode("latin-1")
        )
        tr, tg, tb = (z / 255 for z in CVET_TEKST)
        znach = self._shestnadcat(znachenie)
        self._tekushchaya.write(
            f"BT /F1 10 Tf {tr:.3f} {tg:.3f} {tb:.3f} rg {POLE + 86} {self._y} Td "
            f"<{znach}> Tj ET\n".encode("latin-1")
        )
        self._y -= 3

    def kod(self, stroki: list[str]) -> None:
        """Вырезка из журнала: моноширинно по смыслу, на светлой подложке.

        Подложка нужна не для красоты. Журнал в отчёте — чужой текст среди
        нашего, и без границы взгляд не понимает, где кончается разбор и
        начинается то, что сказала машина. Тот же приём, что в любом разборе
        аварии: цитата отбита от рассуждения.
        """
        gotovye: list[str] = []
        for stroka in stroki:
            gotovye.extend(_razbit(stroka, self.shrift, 7.5, SHIRINA_STRANICY - 2 * POLE - 24))

        # Рисуем пачками по странице: подложка не умеет переезжать через разрыв,
        # и одна на весь журнал оборвалась бы на первой же смене страницы.
        nomer = 0
        while nomer < len(gotovye):
            vlezet = max(1, int((self._y - POLE - 12) / 9.5))
            pachka = gotovye[nomer : nomer + vlezet]
            if not pachka:
                self.novaya_stranica()
                continue
            vysota = len(pachka) * 9.5 + 10
            self._mesto(vysota)
            verh = self._y
            r, g, b = (z / 255 for z in CVET_FON_SHAPKI)
            self._tekushchaya.write(
                f"{r:.3f} {g:.3f} {b:.3f} rg {POLE} {verh - vysota} "
                f"{SHIRINA_STRANICY - 2 * POLE} {vysota} re f\n".encode("latin-1")
            )
            self._y -= 5
            for kusok in pachka:
                self.tekst(kusok, kegl=7.5, cvet=CVET_TIHIY, otstup=10, snizu=2)
            self._y -= 5
            nomer += len(pachka)
            if nomer < len(gotovye):
                self.novaya_stranica()

    # --- сборка файла ---------------------------------------------------------

    def sobrat(self) -> bytes:
        if self._tekushchaya.getvalue():
            self.stranicy.append(self._tekushchaya.getvalue())
        if not self.stranicy:
            self.stranicy.append(b"")

        obekty: list[bytes] = []

        def dobavit(telo: bytes) -> int:
            obekty.append(telo)
            return len(obekty)

        # Порядок объектов важен только для ссылок, поэтому считаем номера заранее.
        nomer_katalog = 1
        nomer_stranic = 2
        pervaya_stranica = 3
        nomer_shrift0 = pervaya_stranica + len(self.stranicy) * 2
        nomer_shriftcid = nomer_shrift0 + 1
        nomer_opisatel = nomer_shrift0 + 2
        nomer_fayl = nomer_shrift0 + 3
        nomer_tounicode = nomer_shrift0 + 4

        deti = " ".join(f"{pervaya_stranica + i * 2} 0 R" for i in range(len(self.stranicy)))
        dobavit(f"<< /Type /Catalog /Pages {nomer_stranic} 0 R >>".encode("latin-1"))
        dobavit(
            f"<< /Type /Pages /Kids [{deti}] /Count {len(self.stranicy)} >>".encode("latin-1")
        )
        for nomer, soderzhimoe in enumerate(self.stranicy):
            svoy = pervaya_stranica + nomer * 2
            dobavit(
                (
                    f"<< /Type /Page /Parent {nomer_stranic} 0 R "
                    f"/MediaBox [0 0 {SHIRINA_STRANICY} {VYSOTA_STRANICY}] "
                    f"/Resources << /Font << /F1 {nomer_shrift0} 0 R >> >> "
                    f"/Contents {svoy + 1} 0 R >>"
                ).encode("latin-1")
            )
            dobavit(
                b"<< /Length " + str(len(soderzhimoe)).encode("latin-1") + b" >>\nstream\n"
                + soderzhimoe + b"\nendstream"
            )

        dobavit(
            (
                f"<< /Type /Font /Subtype /Type0 /BaseFont /DejaVuSans "
                f"/Encoding /Identity-H /DescendantFonts [{nomer_shriftcid} 0 R] "
                f"/ToUnicode {nomer_tounicode} 0 R >>"
            ).encode("latin-1")
        )

        shiriny = " ".join(
            str(round(sh * 1000 / self.shrift.edinic)) for sh in self.shrift.shiriny
        )
        dobavit(
            (
                f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /DejaVuSans "
                f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                f"/FontDescriptor {nomer_opisatel} 0 R /DW 1000 "
                f"/W [0 [{shiriny}]] /CIDToGIDMap /Identity >>"
            ).encode("latin-1")
        )

        x0, y0, x1, y1 = self.shrift.ramka
        k = 1000 / self.shrift.edinic
        dobavit(
            (
                f"<< /Type /FontDescriptor /FontName /DejaVuSans /Flags 32 "
                f"/FontBBox [{round(x0 * k)} {round(y0 * k)} {round(x1 * k)} {round(y1 * k)}] "
                f"/ItalicAngle 0 /Ascent {round(self.shrift.vverh * k)} "
                f"/Descent {round(self.shrift.vniz * k)} /CapHeight {round(0.7 * 1000)} "
                f"/StemV 80 /FontFile2 {nomer_fayl} 0 R >>"
            ).encode("latin-1")
        )
        dobavit(
            b"<< /Length " + str(len(self.shrift.bayty)).encode("latin-1")
            + b" /Length1 " + str(len(self.shrift.bayty)).encode("latin-1")
            + b" >>\nstream\n" + self.shrift.bayty + b"\nendstream"
        )

        dobavit(_karta_tounicode(self._ispolzovano))

        # Сборка с перекрёстной таблицей.
        vyvod = io.BytesIO()
        vyvod.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        smeshcheniya = [0]
        for nomer, telo in enumerate(obekty, start=1):
            smeshcheniya.append(vyvod.tell())
            vyvod.write(f"{nomer} 0 obj\n".encode("latin-1"))
            vyvod.write(telo)
            vyvod.write(b"\nendobj\n")

        nachalo_tablicy = vyvod.tell()
        vyvod.write(f"xref\n0 {len(obekty) + 1}\n".encode("latin-1"))
        vyvod.write(b"0000000000 65535 f \n")
        for smeshchenie in smeshcheniya[1:]:
            vyvod.write(f"{smeshchenie:010d} 00000 n \n".encode("latin-1"))
        vyvod.write(
            f"trailer\n<< /Size {len(obekty) + 1} /Root {nomer_katalog} 0 R >>\n"
            f"startxref\n{nachalo_tablicy}\n%%EOF\n".encode("latin-1")
        )
        return vyvod.getvalue()


def _karta_tounicode(ispolzovano: dict[int, str]) -> bytes:
    """Обратная карта «номер глифа → знак» для копирования текста из PDF.

    Без неё файл рисуется верно, но копируется мусором: внутри лежат номера
    глифов, а не буквы, и просмотрщику неоткуда узнать соответствие. Проверено
    `pdftotext` — до этой карты он выдавал «γφϒϓχϐϊϒύϊ» вместо «Обновление».
    """
    stroki = []
    for glif, znak in sorted(ispolzovano.items()):
        # UTF-16BE, как требует формат: знаки за пределами основной плоскости
        # займут две единицы, и это нормально.
        edinicy = znak.encode("utf-16-be").hex().upper()
        stroki.append(f"<{glif:04X}> <{edinicy}>")

    # `bfchar` идёт пачками не длиннее ста — так велит спецификация.
    kuski = []
    for nachalo in range(0, len(stroki), 100):
        pachka = stroki[nachalo : nachalo + 100]
        kuski.append(f"{len(pachka)} beginbfchar\n" + "\n".join(pachka) + "\nendbfchar")

    telo = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        + "\n".join(kuski)
        + "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    ).encode("latin-1", "replace")

    return b"<< /Length " + str(len(telo)).encode("latin-1") + b" >>\nstream\n" + telo + b"\nendstream"


def _razbit(stroka: str, shrift: Shrift, kegl: float, shirina: float) -> list[str]:
    """Разбить длинную строку по ширине. Рвём по словам, а длинное слово — силой.

    Силой — потому что в журнале сплошь пути и SQL без пробелов: не порви мы их,
    строка уехала бы за край страницы и пропала бы совсем.
    """
    if not stroka:
        return [""]
    kuski: list[str] = []
    tekushchaya = ""
    for slovo in stroka.split(" "):
        proba = f"{tekushchaya} {slovo}".strip()
        if shrift.shirina(proba, kegl) <= shirina:
            tekushchaya = proba
            continue
        if tekushchaya:
            kuski.append(tekushchaya)
            tekushchaya = ""
        while shrift.shirina(slovo, kegl) > shirina:
            skolko = 1
            while skolko < len(slovo) and shrift.shirina(slovo[:skolko + 1], kegl) <= shirina:
                skolko += 1
            kuski.append(slovo[:skolko])
            slovo = slovo[skolko:]
        tekushchaya = slovo
    if tekushchaya:
        kuski.append(tekushchaya)
    return kuski


# =============================================================================
# Писатель DOCX
# =============================================================================
#
# `.docx` — это ZIP с несколькими файлами XML. Минимальный годный набор: тип
# содержимого, две связи и сам документ. Word открывает такой файл без единого
# нарекания, а начертание подбирает сам — поэтому шрифт сюда не вшивается.


def _xml_ekran(tekst: str) -> str:
    return (
        tekst.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


class Word:
    """Складывает абзацы и выдаёт готовый `.docx`."""

    def __init__(self):
        self.kuski: list[str] = []

    def _abzac(self, soderzhimoe: str, stil: str = "") -> None:
        self.kuski.append(f"<w:p>{stil}{soderzhimoe}</w:p>")

    def tekst(
        self,
        stroka: str,
        kegl: int = 10,
        cvet: tuple[int, int, int] = CVET_TEKST,
        zhirno: bool = False,
        odnoshirinnyy: bool = False,
        otstup: int = 0,
    ) -> None:
        svoystva = [f'<w:sz w:val="{kegl * 2}"/>']
        svoystva.append(f'<w:color w:val="{cvet[0]:02X}{cvet[1]:02X}{cvet[2]:02X}"/>')
        if zhirno:
            svoystva.append("<w:b/>")
        if odnoshirinnyy:
            svoystva.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
        stil = ""
        if otstup:
            stil = f'<w:pPr><w:ind w:left="{otstup}"/></w:pPr>'
        self._abzac(
            f'<w:r><w:rPr>{"".join(svoystva)}</w:rPr>'
            f'<w:t xml:space="preserve">{_xml_ekran(stroka)}</w:t></w:r>',
            stil,
        )

    def zagolovok(self, stroka: str) -> None:
        self.tekst("", kegl=6)
        self.tekst(stroka, kegl=15, zhirno=True)

    def plashka(self, stroka: str, cvet: tuple[int, int, int]) -> None:
        """Абзац с заливкой — итог одним взглядом, как плашка в PDF."""
        fon = f"{cvet[0]:02X}{cvet[1]:02X}{cvet[2]:02X}"
        stil = (
            f'<w:pPr><w:shd w:val="clear" w:fill="{fon}"/>'
            f'<w:spacing w:before="120" w:after="120"/></w:pPr>'
        )
        self._abzac(
            f'<w:r><w:rPr><w:sz w:val="30"/><w:b/><w:color w:val="FFFFFF"/></w:rPr>'
            f'<w:t xml:space="preserve">  {_xml_ekran(stroka)}</w:t></w:r>',
            stil,
        )

    def stroka_shaga(self, imya: str, proshel: bool, podrobnost: str) -> None:
        znak = "OK" if proshel else "X"
        cvet = CVET_UDACHA if proshel else CVET_BEDA
        self._abzac(
            f'<w:r><w:rPr><w:sz w:val="18"/><w:b/>'
            f'<w:color w:val="{cvet[0]:02X}{cvet[1]:02X}{cvet[2]:02X}"/></w:rPr>'
            f'<w:t xml:space="preserve">{znak}  </w:t></w:r>'
            f'<w:r><w:rPr><w:sz w:val="20"/></w:rPr>'
            f'<w:t xml:space="preserve">{_xml_ekran(imya)}</w:t></w:r>'
        )
        if podrobnost:
            self.tekst(podrobnost, kegl=8, cvet=CVET_TIHIY, otstup=420)

    def para(self, imya: str, znachenie: str) -> None:
        """Строка «имя — значение»: подпись серым, значение чёрным."""
        self._abzac(
            f'<w:r><w:rPr><w:sz w:val="18"/>'
            f'<w:color w:val="{CVET_TIHIY[0]:02X}{CVET_TIHIY[1]:02X}{CVET_TIHIY[2]:02X}"/></w:rPr>'
            f'<w:t xml:space="preserve">{_xml_ekran(imya)}   </w:t></w:r>'
            f'<w:r><w:rPr><w:sz w:val="20"/></w:rPr>'
            f'<w:t xml:space="preserve">{_xml_ekran(znachenie)}</w:t></w:r>'
        )

    def kod(self, stroki: list[str]) -> None:
        """Вырезка из журнала: моноширинно, на светлой подложке — как в PDF."""
        fon = f"{CVET_FON_SHAPKI[0]:02X}{CVET_FON_SHAPKI[1]:02X}{CVET_FON_SHAPKI[2]:02X}"
        for stroka in stroki:
            stil = (
                f'<w:pPr><w:shd w:val="clear" w:fill="{fon}"/>'
                f'<w:ind w:left="200"/><w:spacing w:after="0"/></w:pPr>'
            )
            self._abzac(
                f'<w:r><w:rPr><w:sz w:val="15"/>'
                f'<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>'
                f'<w:color w:val="{CVET_TIHIY[0]:02X}{CVET_TIHIY[1]:02X}{CVET_TIHIY[2]:02X}"/>'
                f'</w:rPr><w:t xml:space="preserve">{_xml_ekran(stroka)}</w:t></w:r>',
                stil,
            )

    def sobrat(self) -> bytes:
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{"".join(self.kuski)}'
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
            "</w:sectPr></w:body></w:document>"
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        )

        vyvod = io.BytesIO()
        # Время фиксированное: два запуска на одних данных обязаны давать
        # одинаковый файл, иначе сравнить их в проверке нечем.
        with zipfile.ZipFile(vyvod, "w", zipfile.ZIP_DEFLATED) as arhiv:
            for imya, telo in (
                ("[Content_Types].xml", content_types),
                ("_rels/.rels", rels),
                ("word/document.xml", document),
            ):
                zapis = zipfile.ZipInfo(imya, date_time=(2026, 1, 1, 0, 0, 0))
                zapis.compress_type = zipfile.ZIP_DEFLATED
                arhiv.writestr(zapis, telo.encode("utf-8"))
        return vyvod.getvalue()


def seychas() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")
