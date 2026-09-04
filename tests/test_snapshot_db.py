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

from scripts import snapshot_db  # noqa: E402
from scripts.snapshot_db import (  # noqa: E402
    METKA,
    OTKAZY_DOSTUPA,
    _pochemu_ne_pustil,
    celaya,
    kod_oshibki,
    pochemu_ne_celaya,
    snyat,
)


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

    def execution_options(self, **kwargs):
        # Уровень изоляции ставится через SQLAlchemy, а не голым SET SESSION:
        # тот переживал возврат соединения в пул (см. `snapshot_db.snyat`).
        # Записываем в след, чтобы проверка видела и его.
        if "isolation_level" in kwargs:
            self.sled.append(f"[isolation_level={kwargs['isolation_level']}]")
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
    assert "[isolation_level=REPEATABLE READ]" in dvizhok.sled, (
        "уровень изоляции обязан ставиться через execution_options: голый "
        "SET SESSION переживал возврат соединения в пул"
    )
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


# --------------------------------------------------------------------------
# `ping`: молчание против отказа
# --------------------------------------------------------------------------
#
# Ради этого различения `ping` и живёт. Точка входа ждёт базу три минуты, и
# ожидание оправдано ровно одним случаем: база поднимается дольше приложения.
# Неверный пароль в этот случай не входит — сервер работает и отказал по
# существу, — а выглядел он до этого точно так же, и человек три минуты ждал
# ошибку, видимую с первой попытки.


class _Ohibka(Exception):
    """Ошибка драйвера: номер первым аргументом, текст вторым — как у pymysql."""


class _ObolochkaSQLAlchemy(Exception):
    """Обёртка SQLAlchemy: настоящую ошибку она кладёт в `orig`."""

    def __init__(self, orig):
        super().__init__("(pymysql.err.OperationalError) ...")
        self.orig = orig


def test_nomer_oshibki_dostayotsya_iz_obolochki():
    """Номер лежит под обёрткой, и доставать его надо оттуда.

    Снаружи сюда прилетает то ошибка драйвера, то обёртка SQLAlchemy — смотря
    где рвануло. Если бы разбор смотрел только на верхний слой, отказ доступа
    через `engine.connect()` не опознавался бы никогда: у обёртки в `args`
    стоит текст, а не номер.
    """
    pryamaya = _Ohibka(1045, "Access denied for user 'opencrm'@'172.18.0.4'")
    assert kod_oshibki(pryamaya) == 1045
    assert kod_oshibki(_ObolochkaSQLAlchemy(pryamaya)) == 1045
    assert kod_oshibki(Exception("совсем без номера")) is None


def test_molchanie_porta_ne_schitaetsya_otkazom():
    """У «сервера нет» номер тоже есть — и ждать его как раз НАДО.

    Это и есть причина, по которой список отказов белый, а не чёрный. 2003
    («Can't connect to MySQL server») приходит ровно в том случае, ради
    которого ожидание заведено: контейнер базы ещё не поднялся. Попади он в
    список — обычный перезапуск машины снова стал бы аварией, ровно той, из-за
    которой ожидание и появилось.
    """
    assert 2003 not in OTKAZY_DOSTUPA, (
        "«не дозвонились» попало в отказы доступа: старт быстрее базы теперь "
        "убивает контейнер вместо того, чтобы подождать"
    )
    assert 2013 not in OTKAZY_DOSTUPA, "обрыв соединения — тоже повод подождать"
    assert kod_oshibki(_Ohibka(2003, "Can't connect to MySQL server on 'db'")) == 2003


def test_otkazy_dostupa_nazvany_polnostyu():
    """Все четыре отказа значат одно: сервер жив и не пустил.

    Список маленький, и каждый его номер стоит там по делу. Пропажа любого
    возвращает трёхминутное ожидание там, где ответ известен сразу; появление
    лишнего — наоборот, убивает контейнер там, где надо было подождать.
    """
    assert OTKAZY_DOSTUPA == {1044, 1045, 1049, 1698}


def test_prichina_otkaza_ne_neset_parol():
    """Строку отказа мы печатаем в журнал старта — пароля в ней быть не может.

    Драйвер в таких ошибках пишет `using password: YES`, самого пароля не
    приводит. Проверка стережёт не драйвер, а нас: разбор берёт ПОСЛЕДНИЙ
    аргумент (текст сервера), а не всё исключение целиком — у обёртки в текст
    попадает строка подключения, а в ней пароль стоит открытым.
    """
    pryamaya = _Ohibka(1045, "Access denied for user 'opencrm'@'x' (using password: YES)")
    vyshlo = _pochemu_ne_pustil(_ObolochkaSQLAlchemy(pryamaya))
    assert "using password: YES" in vyshlo
    assert "1045" not in vyshlo, "номер в тексте лишний — он ничего не объясняет"


def _pozvat_ping(monkeypatch, beda: BaseException | None):
    """Позвать `main()` подкомандой `ping` с движком, который ведёт себя так.

    Проверяется САМ выход, а не помощники: решение «умереть или подождать»
    точка входа принимает по коду, и коды выше проверены поимённо, а тот, кто
    их отдаёт, — не был. Между разбором номера и кодом выхода стоит ветвление,
    и оно ошибается ничуть не реже.
    """

    class _Soedinenie:
        def __enter__(self):
            if beda is not None:
                raise beda
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            return None

    class _Dvizhok:
        def connect(self):
            return _Soedinenie()

    monkeypatch.setattr(snapshot_db, "_engine", lambda: _Dvizhok())
    monkeypatch.setattr(sys, "argv", ["snapshot_db", "ping"])
    return snapshot_db.main()


def test_ping_otdayot_nol_zhivoy_baze(monkeypatch):
    assert _pozvat_ping(monkeypatch, None) == 0


def test_ping_otdayot_edinicu_molchashchemu_portu(monkeypatch):
    """«Не дозвонились» — это ждать. Ровно ради этого случая ожидание и есть."""
    beda = _ObolochkaSQLAlchemy(_Ohibka(2003, "Can't connect to MySQL server on 'db'"))
    assert _pozvat_ping(monkeypatch, beda) == snapshot_db.NE_OTVECHAET == 1


def test_ping_otdayot_troyku_otkazu_dostupa(monkeypatch, capsys):
    """«Ответил и не пустил» — это сдаваться сразу, и говорить об этом вслух."""
    beda = _ObolochkaSQLAlchemy(
        _Ohibka(1045, "Access denied for user 'opencrm'@'172.18.0.4' (using password: YES)")
    )
    assert _pozvat_ping(monkeypatch, beda) == snapshot_db.NE_PUSTIL == 3

    skazano = capsys.readouterr()
    assert "Access denied" in skazano.err, (
        "причина отказа не попала в журнал: человек увидит, что что-то не так, "
        f"но не увидит ЧТО. Вышло: {skazano.err!r}"
    )
    assert skazano.out == "", "разговоры идут в stderr — stdout здесь читает скрипт"


def test_kod_otkaza_ne_sovpadaet_s_argparse():
    """Двойка занята: ею выходит `argparse`, когда его позвали неверно.

    Совпади они — опечатка в имени подкоманды заставила бы точку входа объявить
    неверный пароль базы и убить контейнер вместо того, чтобы подождать. Сигнал,
    по которому принимается такое решение, не имеет права совпадать с «ты меня
    неправильно позвал».
    """
    assert snapshot_db.NE_PUSTIL != 2
    assert snapshot_db.NE_OTVECHAET != snapshot_db.NE_PUSTIL
    assert 0 not in (snapshot_db.NE_OTVECHAET, snapshot_db.NE_PUSTIL)


def test_otkaz_nazyvaet_kakaya_imenno_beda(dvizhok, tmp_path):
    """Три разные беды не должны выходить наружу одним предложением.

    «Файла нет», «файл пуст» и «дамп оборван» лечатся по-разному, а сообщение
    было на все три одно: «снята не до конца — метки конца в ней нет». Боевое
    обновление встало ровно на нём, и разобрать по сообщению оказалось нечего:
    файл к тому времени уже убран, а какая из трёх бед случилась — неизвестно.

    Отдельно проверяется, что в отказе видно ПОСЛЕДНЮЮ строку файла: именно на
    ней дамп и оборвался, и без неё разбор начинается с догадок.
    """
    net = tmp_path / "нет.sql"
    assert "нет вовсе" in pochemu_ne_celaya(net)

    pusto = tmp_path / "пусто.sql"
    pusto.write_bytes(b"")
    assert "пуст" in pochemu_ne_celaya(pusto)

    oborvan = tmp_path / "оборван.sql"
    snyat(dvizhok, oborvan)
    celoe = oborvan.read_text(encoding="utf-8")
    oborvan.write_text(celoe[: len(celoe) // 2] + "INSERT INTO хвост VALUES (1", encoding="utf-8")
    prichina = pochemu_ne_celaya(oborvan)
    assert "нет метки конца" in prichina
    assert "INSERT INTO хвост" in prichina, (
        "в отказе не видно, на чём дамп оборвался: " + prichina
    )

    # И годная копия по-прежнему годна — иначе разбор стал бы отказывать всем.
    godnaya = tmp_path / "годная.sql"
    snyat(dvizhok, godnaya)
    assert pochemu_ne_celaya(godnaya) == ""


def test_pod_itogovym_imenem_ne_byvaet_ogryzka(dvizhok, tmp_path, monkeypatch):
    """Дамп пишется рядом и переименовывается — под своим именем он всегда цел.

    Имя снимка перед обновлением складывается из коммита, то есть у повторного
    захода на ту же версию оно ТО ЖЕ САМОЕ. Два захода внахлёст — и открытие
    на запись во втором обрезает файл, пока первый ещё пишет: на диске остаётся
    мешанина без метки конца. Разово это и случилось на боевом сервере, а
    ручной прогон следом снимал копию безупречно — потому что был один.

    Здесь падение подкладывается посреди дампа: под итоговым именем не должно
    остаться НИЧЕГО, и рядом тоже — огрызок ничем не отличается от годной
    копии, кроме отсутствующего хвоста, и однажды его попробуют залить.
    """
    import scripts.snapshot_db as dumper

    put = tmp_path / "damp.sql"
    put.write_text("прежняя годная копия", encoding="utf-8")

    nastoyashchaya = dumper._odna_tablica

    def padaet(*args, **kwargs):
        nastoyashchaya(*args, **kwargs)
        raise RuntimeError("контейнер убили посреди дампа")

    monkeypatch.setattr(dumper, "_odna_tablica", padaet)
    with pytest.raises(RuntimeError):
        snyat(dvizhok, put)

    assert put.read_text(encoding="utf-8") == "прежняя годная копия", (
        "оборванный дамп затёр то, что лежало под этим именем"
    )
    ostatki = [x.name for x in tmp_path.iterdir() if "chernovik" in x.name]
    assert not ostatki, f"огрызок остался лежать рядом: {ostatki}"
