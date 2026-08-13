"""Копия MySQL перед миграциями — сам дампер, а не обвязка вокруг него.

ПОЧЕМУ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. У `scripts/snapshot_db.py` не было НИ ОДНОГО
собственного теста: `tests/test_pre_migrate_snapshot.py` проверяет поведение
точки входа по следу подставных `python`/`sqlite3`, а `tests/test_db_boundary.py`
— только то, что файл назван в списке исключений. Проверено ломанием: обе
поломки ниже оставляли ВЕСЬ набор зелёным, и обе бьют ровно по тем двум
свойствам, которые шапка скрипта объявляет главными.

  1. `celaya()` → всегда истина: оборванный дамп сходит за годную копию. А
     негодность у него не видна ничем, кроме отсутствующего хвоста, — это
     обычный текстовый файл.
  2. Снят `START TRANSACTION WITH CONSISTENT SNAPSHOT`: копия перестаёт быть
     согласованной. Половина таблиц — до чужой транзакции, половина — после;
     заявка есть, а её строк уже нет. Отличить такую копию от годной нельзя
     ничем, а возвращаются именно к ней.

Живого MySQL в наборе нет и не будет (то же ограничение, что у
`test_mysql_portability.py`), поэтому движок здесь подставной: он записывает
всё, что у него спросили. Проверяется не текст запросов ради текста, а два
свойства — согласованность снимка и полнота файла.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.snapshot_db import METKA, celaya, snyat  # noqa: E402


class _Otvet:
    """Ответ подставного соединения: и список, и одна строка, и поток."""

    def __init__(self, stroki, kolonki=()):
        self._stroki = list(stroki)
        self._kolonki = list(kolonki)

    def all(self):
        return list(self._stroki)

    def one(self):
        return self._stroki[0]

    def keys(self):
        return self._kolonki

    def fetchmany(self, skolko):
        kusok = self._stroki[:skolko]
        self._stroki = self._stroki[skolko:]
        return kusok


class _Soedinenie:
    """Соединение, которое запоминает каждый запрос и отвечает по словарю."""

    def __init__(self, sled, tablicy):
        self.sled = sled
        self.tablicy = tablicy

    def execute(self, vyrazhenie):
        zapros = str(vyrazhenie)
        self.sled.append(zapros)
        if zapros.startswith("SHOW FULL TABLES"):
            # Второй столбец — вид: представления дамп не воспроизводит.
            return _Otvet(
                [(imya, "BASE TABLE") for imya in self.tablicy] + [("otchyot_vid", "VIEW")]
            )
        if zapros.startswith("SHOW CREATE TABLE"):
            imya = zapros.split("`")[1]
            return _Otvet([(imya, f"CREATE TABLE `{imya}` (id INT)")])
        if zapros.startswith("SELECT * FROM"):
            imya = zapros.split("`")[1]
            return _Otvet(self.tablicy[imya], kolonki=("id", "name"))
        return _Otvet([])

    def execution_options(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_beda):
        return False


class _Dvizhok:
    def __init__(self, tablicy):
        self.sled: list[str] = []
        self.tablicy = tablicy
        self.url = SimpleNamespace(database="opencrm")
        self.zakryto = []

    def raw_connection(self):
        return SimpleNamespace(
            driver_connection=SimpleNamespace(escape=_ekranirovat),
            close=lambda: self.zakryto.append(True),
        )

    def connect(self):
        return _Soedinenie(self.sled, self.tablicy)


def _ekranirovat(znachenie):
    """То, что в жизни делает pymysql: значение как литерал SQL."""
    if isinstance(znachenie, (int, float)):
        return str(znachenie)
    return "'" + str(znachenie).replace("'", "''") + "'"


@pytest.fixture()
def dvizhok():
    return _Dvizhok(
        {
            "clients": [(1, "Иван"), (2, "Пётр 🌿")],
            "products": [(1, "Товар")],
        }
    )


# --- согласованность снимка ---------------------------------------------------


def test_snimok_soglasovannyy(dvizhok, tmp_path):
    """Без одной транзакции на весь дамп копия врёт, и это не видно ничем.

    `START TRANSACTION WITH CONSISTENT SNAPSHOT` даёт InnoDB одну точку во
    времени на весь обход таблиц и НЕ блокирует пишущих — ровно то же делает
    `mysqldump --single-transaction`. Снятая без него копия собирается из
    разных моментов: клиент есть, а его заявок уже нет.
    """
    tablic, strok = snyat(dvizhok, tmp_path / "damp.sql")

    assert (tablic, strok) == (2, 3)
    assert "START TRANSACTION WITH CONSISTENT SNAPSHOT" in dvizhok.sled, (
        "дамп собирается из разных моментов времени — копия несогласованная"
    )
    assert "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ" in dvizhok.sled
    # Порядок важен: транзакция обязана открыться ДО первого чтения данных.
    nachalo = dvizhok.sled.index("START TRANSACTION WITH CONSISTENT SNAPSHOT")
    pervoe_chtenie = next(
        n for n, z in enumerate(dvizhok.sled) if z.startswith(("SHOW FULL TABLES", "SELECT * FROM"))
    )
    assert nachalo < pervoe_chtenie, "снимок открыт уже после того, как начали читать"


def test_predstavleniya_v_damp_ne_edut(dvizhok, tmp_path):
    """Своих представлений в проекте нет, а чужое в нашей схеме — не наше дело."""
    snyat(dvizhok, tmp_path / "damp.sql")
    tekst = (tmp_path / "damp.sql").read_text(encoding="utf-8")
    assert "otchyot_vid" not in tekst


def test_soedinenie_zakryvaetsya(dvizhok, tmp_path):
    """Дамп снимается на каждом старте контейнера — утечка накопится молча."""
    snyat(dvizhok, tmp_path / "damp.sql")
    assert dvizhok.zakryto, "сырое соединение осталось открытым"


# --- годность копии видна только по хвосту ------------------------------------


def test_damp_konchaetsya_metkoy(dvizhok, tmp_path):
    put = tmp_path / "damp.sql"
    snyat(dvizhok, put)
    assert celaya(put), "свежеснятая копия объявлена негодной"
    assert put.read_text(encoding="utf-8").rstrip().endswith("таблиц 2, строк 3")


def test_oborvannyy_damp_ne_schitaetsya_godnym(dvizhok, tmp_path):
    """Кончилось место, убили контейнер — файл на диске выглядит как обычный.

    Это и есть вся разница между «копия есть» и «копия годна»: у оборванного
    дампа нет ни признака порчи, ни неверного размера — только нет хвоста.
    """
    put = tmp_path / "damp.sql"
    snyat(dvizhok, put)
    celoe = put.read_text(encoding="utf-8")
    put.write_text(celoe[: len(celoe) // 2], encoding="utf-8")

    assert not celaya(put), "оборванный дамп сошёл за годную копию"


def test_pustoy_i_propavshiy_fayl_ne_godny(tmp_path):
    assert not celaya(tmp_path / "net-takogo.sql")
    pustoy = tmp_path / "pustoy.sql"
    pustoy.write_text("", encoding="utf-8")
    assert not celaya(pustoy)


def test_metka_ishchetsya_v_hvoste_a_ne_vo_vsyom_fayle(tmp_path):
    """Дамп — гигабайты; читать его целиком ради одной строки незачем.

    Обратная сторона: метка, случайно попавшая в СЕРЕДИНУ файла (в значении
    поля, например), не должна выдавать оборванный дамп за целый.
    """
    put = tmp_path / "damp.sql"
    put.write_text(
        f"INSERT INTO notes VALUES ('{METKA}');\n" + "x" * 20_000, encoding="utf-8"
    )
    assert not celaya(put), "метка в середине файла выдана за конец копии"


def test_znacheniya_ekraniruet_drayver(dvizhok, tmp_path):
    """Экранирование делегировано pymysql — той же функции, что и в запросах.

    Своё экранирование здесь было бы единственным местом в проекте, где
    кавычка в имени клиента решает судьбу восстановления.
    """
    dvizhok.tablicy["clients"] = [(1, "О'Коннор")]
    put = tmp_path / "damp.sql"
    snyat(dvizhok, put)
    assert "'О''Коннор'" in put.read_text(encoding="utf-8")
