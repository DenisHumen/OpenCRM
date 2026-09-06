"""Годна ли резервная копия к восстановлению. Ничего не восстанавливает.

**Зачем это существует.** Копии, которые никто не проверял, — это не копии, а
надежда. Обычная история: скрипт год пишет файлы, диск заполняется, дамп
начинает обрываться на полпути, а узнают об этом в день, когда база
понадобилась. Проверка стоит секунду и превращает «надеюсь, есть копия» в
«копия открывается, в ней столько-то клиентов».

Копия одного вида — дамп `db-ГГГГ-ММ-ДД.sql`. Файл от прежней установки может
ещё лежать в том же каталоге, и он называется негодным: читать чужой формат
так, будто он свой, — способ узнать правду в самый неподходящий день.

Проверяем ровно то, из-за чего копия оказывается негодной:

1. **Копия дописана до конца.** Признак у дампа один и плохо заметный: это
   обычный текст, и оборванный ничем не отличается от целого, кроме
   отсутствующего хвоста `-- Dump completed`. Залитый до места обрыва, он
   оставит половину таблиц.
2. **Схема отмечена миграцией.** База без `alembic_version` не поднимется
   новым кодом: приложение не знает, чем её доводить (см.
   `database/schema_check.py`).
3. **Данные на месте.** Пустая копия — самый коварный случай: файл есть, размер
   правдоподобный, а внутри одни пустые таблицы.
4. **Архив storage читается** — `tar -tzf` по списку, без распаковки.
5. **Ключ шифрования сохранён.** Без `OPENCRM_SECRET_KEY` пароли почтовых
   ящиков в восстановленной базе не расшифровать НИКОГДА: ключ не выводится из
   данных, и потеря его необратима.
6. **Зашифрованная копия вправду открывается данным ключом** — и внутри
   оказывается годный дамп, а не мусор нужного размера.

**Про шестой пункт отдельно.** «Потерял ключ — потерял копию» — свойство
шифрования, а не недоработка, но голого предупреждения мало: беда обнаруживается
в день аварии, когда поздно. Поэтому проверка открывает копию по-настоящему:
сверяет метку подлинности и разбирает расшифрованный дамп. Копия, которую ни разу
не разворачивали, — это надежда, а не копия.

Формат зашифрованной копии, её шифрование и расшифровка живут ЗДЕСЬ же (раздел
«зашифрованная копия»), рядом с единственным, кто их читает: разъехавшись,
писатель и читатель дадут не отказ, а файл, который не открывается уже ничем.
Разбор устройства и доводы — `docs/ekspluatatsiya/15-kopii-s-shifrovaniem.md`.

Запускается сразу после снятия копии (`scripts/backup.sh`) и отдельно — рукой
или из `./opencrm.sh doctor`.

Код возврата: 0 — копия годна, 1 — нет. Отчёт печатается всегда и кладётся
рядом с копиями в `last-check.json`, чтобы «когда проверяли в последний раз»
имело ответ.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

#: Таблицы, пустота которых означает, что копия бесполезна.
#:
#: Ровно две: без пользователей в систему не войти вообще, без настроек она не
#: та, что была. Проверять «клиенты не пусты» нельзя — свежая установка
#: законно пуста, и такая проверка ругалась бы на верную копию.
MUST_HAVE_ROWS = ("users", "site_settings")

#: Что осмысленно показать человеку: «в копии 48 клиентов» отвечает на вопрос
#: «та ли это копия» лучше, чем размер файла.
#:
#: Таблицы из `MUST_HAVE_ROWS` обязаны быть здесь же: проверка «не пусто»
#: смотрит именно в эти счётчики, и таблица, которую забыли посчитать,
#: объявлялась бы пустой всегда. Так и вышло с `site_settings` на первом же
#: прогоне — поэтому список собирается, а не набирается руками во второй раз.
COUNT_TABLES = tuple(dict.fromkeys(
    MUST_HAVE_ROWS + (
        "clients", "deals", "documents", "products", "stock_moves",
        "warehouses", "boards", "document_lines",
    )
))

#: Хвост, которым mysqldump заканчивает работу. Его отсутствие — единственный
#: признак оборванного дампа: текстовый файл, обрезанный на полуслове, читается
#: и выглядит совершенно обычно.
#:
#: Строка появляется, пока дамп снимается с комментариями (по умолчанию);
#: `--skip-comments` её убирает, поэтому в scripts/backup.sh этого флага нет и
#: быть не должно.
KHVOST_DUMPA = "-- Dump completed"
#: Тот же хвост у копии, снятой самим приложением (`scripts/snapshot_db.METKA`):
#: предмиграционный снимок и копия с экрана настроек. Литерал, а не ввоз: этот
#: файл зовут и с хоста, где ни SQLAlchemy, ни настроек приложения нет.
KHVOST_SNIMKA = "-- opencrm snapshot complete"

#: `CREATE TABLE \`users\` (` — имя таблицы в дампе всегда в обратных кавычках.
_SOZDANIE_TABLICY = re.compile(r"^CREATE TABLE `([^`]+)`", re.IGNORECASE)
#: `INSERT INTO \`users\` VALUES (...),(...);` — а с `--complete-insert` между
#: именем таблицы и словом VALUES стоит ещё и список столбцов.
_VSTAVKA = re.compile(r"^INSERT INTO `([^`]+)`.*?\bVALUES\b", re.IGNORECASE)


class _SchyotchikVstavki:
    """Считает строки в одном операторе `INSERT ... VALUES (..),(..);`.

    Считаем скобки, а не запятые: значения — это текст заметок и адресов, и
    `),(` внутри такого текста встречается ровно тогда, когда меньше всего
    ждёшь. Внутри строкового литерала скобки не считаются вовсе, экранирование
    `\\'` учитывается — mysqldump экранирует кавычки именно так.

    Кормить можно по кусочку, и это не про удобство. **Оператор INSERT не
    обязан помещаться в одну строку файла.** mysqldump от Oracle пишет его
    одной строкой, а mariadb-dump (именно он ставится на Debian и Ubuntu под
    именем `mysqldump`) переносит значения на следующие строки. Поймано живьём:
    построчный разбор насчитал ноль строк во всех таблицах и объявил заведомо
    годную копию негодной — а ложная тревога про копии почти так же вредна, как
    молчание, потому что после неё проверке перестают верить.
    """

    def __init__(self) -> None:
        self.stroki = 0
        self.zakonchen = False
        self._glubina = 0
        self._v_stroke = False
        self._ekran = False

    def dobavit(self, kusok: str) -> None:
        for znak in kusok:
            if self._ekran:
                self._ekran = False
            elif self._v_stroke:
                if znak == "\\":
                    self._ekran = True
                elif znak == "'":
                    self._v_stroke = False
            elif znak == "'":
                self._v_stroke = True
            elif znak == "(":
                self._glubina += 1
            elif znak == ")":
                self._glubina -= 1
                if self._glubina == 0:
                    self.stroki += 1
            elif znak == ";" and self._glubina == 0:
                # Точка с запятой вне строки и вне кортежа — конец оператора.
                self.zakonchen = True
                return


def _prochitat_dump(path: Path) -> tuple[set[str], dict[str, int], str | None, str]:
    """Разбирает дамп из файла. Разбор — в `_razobrat_dump`."""
    with path.open("r", encoding="utf-8", errors="replace") as fayl:
        return _razobrat_dump(fayl)


def _razobrat_dump(stroki_faila) -> tuple[set[str], dict[str, int], str | None, str]:
    """Разбирает дамп: какие таблицы есть, сколько в них строк, ревизия, хвост.

    Читаем построчно, а не целиком: дамп рабочей базы — это сотни мегабайт, и
    проверка копии не имеет права требовать под себя столько же памяти. По той же
    причине источник здесь — любые строки, а не файл: расшифрованная копия идёт
    сюда трубой прямо из openssl и на диск не ложится вовсе.
    """
    tablicy: set[str] = set()
    stroki: dict[str, int] = {}
    revizia: str | None = None
    poslednyaya = ""

    # Оператор INSERT, который сейчас разбираем: его имя, счётчик и — только для
    # alembic_version — накопленный текст. Копить текст всех вставок подряд
    # значило бы прочитать дамп в память целиком, ради чего всё это и не делается.
    imya: str | None = None
    schyotchik: _SchyotchikVstavki | None = None
    otmetka_revizii: list[str] = []

    for stroka in stroki_faila:
        stroka = stroka.rstrip("\n")
        if stroka.strip():
            poslednyaya = stroka

        if imya is None:
            sozdanie = _SOZDANIE_TABLICY.match(stroka)
            if sozdanie:
                tablicy.add(sozdanie.group(1))
                continue
            vstavka = _VSTAVKA.match(stroka)
            if not vstavka:
                continue
            imya = vstavka.group(1)
            # Вставка бывает и в таблицу, чьего CREATE TABLE в дампе нет
            # (частичный дамп); тогда таблица всё равно считается имеющейся.
            tablicy.add(imya)
            schyotchik = _SchyotchikVstavki()
            kusok = stroka[vstavka.end():]
        else:
            kusok = stroka

        assert schyotchik is not None
        schyotchik.dobavit(kusok)
        if imya == "alembic_version":
            otmetka_revizii.append(kusok)

        if schyotchik.zakonchen:
            stroki[imya] = stroki.get(imya, 0) + schyotchik.stroki
            if imya == "alembic_version" and revizia is None:
                nayden = re.search(r"\('([^']+)'\)", "".join(otmetka_revizii))
                if nayden:
                    revizia = nayden.group(1)
            imya = None
            schyotchik = None

    # Оператор, оборвавшийся на середине файла, до сюда не досчитан — и это
    # верно: строки, которых в файле нет, восстановлению не помогут.
    if imya is not None and schyotchik is not None:
        stroki[imya] = stroki.get(imya, 0) + schyotchik.stroki

    return tablicy, stroki, revizia, poslednyaya


def _proverit_dump(razbor, report: dict, fail) -> None:
    """Дамп базы: дочитан ли до конца, отмечен ли миграцией, есть ли данные."""
    tablicy, stroki, revizia, poslednyaya = razbor

    if KHVOST_DUMPA not in poslednyaya and not poslednyaya.startswith(KHVOST_SNIMKA):
        # Оборванный дамп — обычный текстовый файл: он открывается, читается и
        # выглядит целым. Единственное, чем он себя выдаёт, — отсутствие хвоста,
        # который mysqldump дописывает последним действием.
        fail("дамп не дописан до конца — снятие копии оборвалось")

    if "alembic_version" not in tablicy:
        fail("копия не отмечена миграцией — новый код не будет знать, чем её доводить")
    elif revizia is None:
        fail("таблица alembic_version в дампе есть, а ревизии в ней нет")
    else:
        report["revision"] = revizia

    for table in COUNT_TABLES:
        if table in tablicy:
            report["counts"][table] = stroki.get(table, 0)

    for table in MUST_HAVE_ROWS:
        if table not in tablicy:
            fail(f"в копии нет таблицы {table}")
        elif not report["counts"].get(table):
            # Дамп со схемой, но без данных — ровно то, что получается при
            # `--no-data` или при дампе не той базы. Восстанавливают такую копию
            # и обнаруживают, что войти в систему некому.
            fail(f"таблица {table} пуста — копия бесполезна")


# --- зашифрованная копия ------------------------------------------------------
#
# Шифр — AES-256-CTR потоком через `openssl` (он уже есть в образе), подлинность
# — HMAC-SHA256 по шифротексту, ключи разведены HKDF. Почему именно так, почему
# не GCM и почему не своими руками — docs/ekspluatatsiya/15-kopii-s-shifrovaniem.md §3, 3-бис.

#: Опознание в начале файла. Нужно дважды: чтобы не расшифровывать чужое и чтобы
#: через год `head -c 20` отвечал, что это вообще за файл.
MAGIYA = b"OPENCRM-BACKUP\x00"
VERSIYA = 1

SPOSOB_PAROL = 1
SPOSOB_KLYUCH = 2
NAZVANIE_SPOSOBA = {SPOSOB_PAROL: "пароль", SPOSOB_KLYUCH: "файл ключа"}

DLINA_SOLI = 16
DLINA_SCHYOTCHIKA = 16
DLINA_METKI = 32
#: опознание, версия, способ, три параметра scrypt по 4 байта, соль, счётчик
DLINA_SHAPKI = len(MAGIYA) + 2 + 12 + DLINA_SOLI + DLINA_SCHYOTCHIKA

#: Параметры scrypt. Едут В ФАЙЛ и при расшифровке берутся оттуда, а не отсюда:
#: копия, снятая сегодня, обязана открыться после того, как параметры ужесточат.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 15, 8, 1

#: Итерации PBKDF2, которыми openssl доводит наш пароль до ключа AES. Пришпилены
#: числом, а не оставлены умолчанию: умолчание принадлежит openssl из чужого
#: базового образа, и его смена сделала бы вчерашнюю копию неоткрываемой.
ITERACII_OPENSSL = 10000

SHIFR = "-aes-256-ctr"
#: По сколько читаем файлы. Дамп не помещается в память — в этом вся затея.
KUSOK = 1 << 20


class NeTaKopiya(Exception):
    """Файл не того вида: не наше опознание, чужая версия или не файл ключа."""


class NeTotKlyuch(Exception):
    """Метка подлинности не сошлась: неверный ключ либо испорченный файл."""


class Shapka(NamedTuple):
    """Открытый заголовок копии. Секрета в нём нет — только чем её открывать."""

    sposob: int
    n: int
    r: int
    p: int
    sol: bytes
    schyotchik: bytes


def porodit_klyuch() -> str:
    """Новый ключ копии — 32 байта случайности шестнадцатеричной строкой.

    KDF над ним не нужен и был бы обманом: корень уже полноэнтропийный, а scrypt
    поверх делает вид, что усилил то, чего усилить нельзя.
    """
    return os.urandom(32).hex()


def zapisat_klyuch(put: Path) -> str:
    """Завести файл ключа. Права 600 сразу: это и есть вся копия."""
    klyuch = porodit_klyuch()
    put.write_text(
        "# Ключ резервной копии OpenCRM.\n"
        "# Потеряете этот файл — копия не откроется НИКОГДА и НИЧЕМ:\n"
        "# подбирать здесь нечего, и восстановить его неоткуда.\n"
        f"{klyuch}\n",
        encoding="utf-8",
    )
    os.chmod(put, 0o600)
    return klyuch


def prochitat_klyuch(put: Path) -> bytes:
    """Ключ из файла: первая строка, которая не пуста и не комментарий."""
    for stroka in put.read_text(encoding="utf-8").splitlines():
        stroka = stroka.strip()
        if not stroka or stroka.startswith("#"):
            continue
        try:
            klyuch = bytes.fromhex(stroka)
        except ValueError:
            raise NeTaKopiya(f"{put.name}: это не файл ключа копии") from None
        if len(klyuch) != 32:
            raise NeTaKopiya(f"{put.name}: в ключе {len(klyuch)} байт вместо 32")
        return klyuch
    raise NeTaKopiya(f"{put.name}: файла ключа хватило только на комментарии")


def zashifrovana(put: Path) -> bool:
    """Зашифрована ли копия. Спрашиваем файл, а не его расширение."""
    try:
        with put.open("rb") as fayl:
            return fayl.read(len(MAGIYA)) == MAGIYA
    except OSError:
        return False


def _sobrat_shapku(shapka: Shapka) -> bytes:
    return (
        MAGIYA
        + bytes((VERSIYA, shapka.sposob))
        + struct.pack(">III", shapka.n, shapka.r, shapka.p)
        + shapka.sol
        + shapka.schyotchik
    )


def prochitat_shapku(put: Path) -> Shapka:
    """Заголовок копии. Ключа не требует: по нему и видно, ЧЕМ её открывать."""
    with put.open("rb") as fayl:
        syraya = fayl.read(DLINA_SHAPKI)
    if len(syraya) < DLINA_SHAPKI or not syraya.startswith(MAGIYA):
        raise NeTaKopiya(f"{put.name}: это не зашифрованная копия OpenCRM")
    versiya, sposob = syraya[len(MAGIYA)], syraya[len(MAGIYA) + 1]
    if versiya != VERSIYA:
        raise NeTaKopiya(
            f"{put.name}: копия формата версии {versiya}, а этот код знает {VERSIYA}"
        )
    if sposob not in NAZVANIE_SPOSOBA:
        raise NeTaKopiya(f"{put.name}: неизвестный способ шифрования {sposob}")
    n, r, p = struct.unpack_from(">III", syraya, len(MAGIYA) + 2)
    nachalo_soli = len(MAGIYA) + 2 + 12
    return Shapka(
        sposob=sposob,
        n=n,
        r=r,
        p=p,
        sol=syraya[nachalo_soli : nachalo_soli + DLINA_SOLI],
        schyotchik=syraya[nachalo_soli + DLINA_SOLI : DLINA_SHAPKI],
    )


def _koren(shapka: Shapka, parol: str | None, klyuch: bytes | None) -> bytes:
    """Корень, из которого выводится всё остальное."""
    if shapka.sposob == SPOSOB_PAROL:
        if parol is None:
            raise NeTotKlyuch("копия зашифрована паролем, а пароля не дали")
        # Предел памяти считаем по параметрам ИЗ ФАЙЛА: умолчание hashlib (32 МиБ)
        # отказало бы копии, снятой с более дорогими параметрами, — то есть ровно
        # той, ради которой параметры и записаны в файл.
        predel = 128 * shapka.r * (shapka.n + shapka.p) + (1 << 20)
        return hashlib.scrypt(
            parol.encode("utf-8"), salt=shapka.sol,
            n=shapka.n, r=shapka.r, p=shapka.p, maxmem=predel, dklen=32,
        )
    if klyuch is None:
        raise NeTotKlyuch("копия зашифрована файлом ключа, а ключа не дали")
    return klyuch


def _hkdf(ikm: bytes, sol: bytes, info: bytes, dlina: int) -> bytes:
    """HKDF-SHA256 (RFC 5869). Двойник живёт в `core/security/secretbox.py`:
    этот модуль намеренно не тянет за собой ни настроек, ни базы."""
    prk = hmac.new(sol, ikm, hashlib.sha256).digest()
    out, blok, nomer = b"", b"", 1
    while len(out) < dlina:
        blok = hmac.new(prk, blok + info + bytes([nomer]), hashlib.sha256).digest()
        out += blok
        nomer += 1
    return out[:dlina]


def _podklyuchi(shapka: Shapka, parol: str | None, klyuch: bytes | None) -> tuple[bytes, str]:
    """(ключ метки, пароль для openssl). Один ключ на две задачи — старая
    ошибка, ослабляющая обе; поэтому их двое и оба из одного корня.

    В openssl секрет уходит ТЕКСТОМ: сырой ключ он принимает только через `-K`,
    то есть через argv, где его видит `ps` любого пользователя машины.
    """
    material = _hkdf(
        _koren(shapka, parol, klyuch),
        shapka.sol,
        b"opencrm/backup/v1" + shapka.schyotchik,
        64,
    )
    return material[:32], material[32:].hex()


def _komanda(rezhim: str, put_parolya: Path) -> list[str]:
    """Командная строка openssl. Секрета в ней нет и быть не может."""
    return [
        "openssl", "enc", rezhim, SHIFR,
        "-pbkdf2", "-iter", str(ITERACII_OPENSSL), "-md", "sha256",
        # Соль своя, лежит в шапке и уже вмешана в пароль через HKDF: пусть
        # openssl не сочиняет вторую и не пишет поверх нашего формата свой
        # заголовок `Salted__`.
        "-nosalt",
        "-pass", f"file:{put_parolya}",
    ]


@contextmanager
def _parol_faylom(parol_openssl: str):
    """Пароль для openssl — во временном файле с правами 600.

    Тем же приёмом `scripts/backup.sh` отдаёт пароль mysqldump: аргументы
    процесса видит `ps`, и они же оседают в `docker inspect`.
    """
    fd, imya = tempfile.mkstemp(prefix="opencrm-backup-")  # mkstemp даёт 0600
    try:
        with os.fdopen(fd, "wb") as fayl:
            fayl.write(parol_openssl.encode("ascii") + b"\n")
        yield Path(imya)
    finally:
        Path(imya).unlink(missing_ok=True)


def _metka_faila(put: Path, klyuch_metki: bytes, dlina: int) -> bytes:
    """HMAC-SHA256 по первым `dlina` байтам файла — шапке и шифротексту."""
    metka = hmac.new(klyuch_metki, digestmod=hashlib.sha256)
    ostalos = dlina
    with put.open("rb") as fayl:
        while ostalos > 0:
            kusok = fayl.read(min(KUSOK, ostalos))
            if not kusok:
                break
            metka.update(kusok)
            ostalos -= len(kusok)
    return metka.digest()


@contextmanager
def _chernovik(cel: Path):
    """Пишем рядом и переименовываем в конце — приём из `scripts/snapshot_db.py`.

    Под итоговым именем файл появляется только целым: копия без метки в хвосте
    ничем не отличается от годной, пока её не попробуют открыть.
    """
    fd, imya = tempfile.mkstemp(dir=str(cel.parent), prefix=cel.name + ".", suffix=".chernovik")
    os.close(fd)
    chernovik = Path(imya)
    try:
        yield chernovik
        os.replace(chernovik, cel)
    except BaseException:
        chernovik.unlink(missing_ok=True)
        raise


def zashifrovat(
    istochnik: Path, cel: Path, *, parol: str | None = None, klyuch: bytes | None = None
) -> dict:
    """Зашифровать готовую копию. Исходник не трогается вовсе."""
    if (parol is None) == (klyuch is None):
        raise ValueError("нужен ровно один способ: пароль или файл ключа")
    sposob = SPOSOB_PAROL if parol is not None else SPOSOB_KLYUCH
    shapka = Shapka(
        sposob=sposob,
        n=SCRYPT_N if sposob == SPOSOB_PAROL else 0,
        r=SCRYPT_R if sposob == SPOSOB_PAROL else 0,
        p=SCRYPT_P if sposob == SPOSOB_PAROL else 0,
        sol=os.urandom(DLINA_SOLI),
        schyotchik=os.urandom(DLINA_SCHYOTCHIKA),
    )
    klyuch_metki, parol_openssl = _podklyuchi(shapka, parol, klyuch)

    with _chernovik(cel) as chernovik:
        with _parol_faylom(parol_openssl) as put_parolya, chernovik.open("wb") as vyhod:
            vyhod.write(_sobrat_shapku(shapka))
            vyhod.flush()
            gotovo = subprocess.run(
                _komanda("-e", put_parolya) + ["-in", str(istochnik)],
                stdout=vyhod, stderr=subprocess.PIPE,
            )
        if gotovo.returncode != 0:
            raise RuntimeError(
                "openssl не зашифровал копию: "
                + gotovo.stderr.decode("utf-8", "replace").strip()[:200]
            )
        # Метка считается по тому, что вправду легло на диск, а не по тому, что
        # мы собирались записать.
        metka = _metka_faila(chernovik, klyuch_metki, chernovik.stat().st_size)
        with chernovik.open("ab") as vyhod:
            vyhod.write(metka)

    os.chmod(cel, 0o600)
    return {"sposob": NAZVANIE_SPOSOBA[sposob], "size": cel.stat().st_size}


def _sverit(istochnik: Path, parol: str | None, klyuch: bytes | None) -> tuple[str, int]:
    """Сверить метку и вернуть (пароль для openssl, длину шифротекста).

    **Метка сверяется ДО первого расшифрованного байта, и это главное место
    всего формата.** `openssl enc -d` с неверным паролем не сообщает об ошибке:
    он молча отдаёт мусор нужного размера и выходит с нулём. Залитый поверх
    живой базы, такой мусор кончает и базу, и копию разом.
    """
    shapka = prochitat_shapku(istochnik)
    razmer = istochnik.stat().st_size
    dlina_shifra = razmer - DLINA_SHAPKI - DLINA_METKI
    if dlina_shifra < 0:
        raise NeTaKopiya(
            f"{istochnik.name}: в файле {razmer} Б — меньше, чем весит его "
            "собственный заголовок с меткой; копия оборвана"
        )
    klyuch_metki, parol_openssl = _podklyuchi(shapka, parol, klyuch)
    with istochnik.open("rb") as fayl:
        fayl.seek(razmer - DLINA_METKI)
        zapisannaya = fayl.read(DLINA_METKI)
    if not hmac.compare_digest(
        zapisannaya, _metka_faila(istochnik, klyuch_metki, razmer - DLINA_METKI)
    ):
        raise NeTotKlyuch(
            f"{istochnik.name}: метка подлинности не сошлась — неверный "
            f"{NAZVANIE_SPOSOBA[shapka.sposob]} либо файл испорчен. "
            "Копия НЕ расшифрована, и трогать ею ничего нельзя"
        )
    return parol_openssl, dlina_shifra


def _kormit(kuda, istochnik: Path, nachalo: int, skolko: int) -> None:
    """Отдаёт openssl ровно шифротекст — без шапки и без метки в хвосте."""
    try:
        with istochnik.open("rb") as fayl:
            fayl.seek(nachalo)
            ostalos = skolko
            while ostalos > 0:
                kusok = fayl.read(min(KUSOK, ostalos))
                if not kusok:
                    break
                kuda.write(kusok)
                ostalos -= len(kusok)
    except (BrokenPipeError, OSError):
        pass  # openssl закрылся раньше — о причине скажет его код возврата
    finally:
        try:
            kuda.close()
        except OSError:
            pass


def sverit_metku(istochnik: Path, *, parol: str | None = None, klyuch: bytes | None = None) -> int:
    """Подходит ли ключ к копии и цела ли она. Возвращает длину шифротекста.

    Ничего не расшифровывает и на диск не пишет: метка считается по
    шифротексту, и её сходство — единственное доказательство, что ключ тот
    самый (§4 docs/15). Нужна там, где расшифровывать незачем, — при проверке
    только что снятого архива файлов на гигабайты.
    """
    return _sverit(istochnik, parol, klyuch)[1]


def rasshifrovat(
    istochnik: Path, cel: Path, *, parol: str | None = None, klyuch: bytes | None = None
) -> None:
    """Расшифровать копию в файл. Метка сверяется до того, как что-то писать."""
    parol_openssl, dlina = _sverit(istochnik, parol, klyuch)
    with _chernovik(cel) as chernovik:
        with _parol_faylom(parol_openssl) as put_parolya, chernovik.open("wb") as vyhod:
            process = subprocess.Popen(
                _komanda("-d", put_parolya),
                stdin=subprocess.PIPE, stdout=vyhod, stderr=subprocess.PIPE,
            )
            # Кормим прямо отсюда: читает трубу openssl, а не мы, — вставать
            # некому.
            _kormit(process.stdin, istochnik, DLINA_SHAPKI, dlina)
            oshibka = process.stderr.read()
            process.stderr.close()
            if process.wait() != 0:
                raise RuntimeError(
                    "openssl не расшифровал копию: "
                    + oshibka.decode("utf-8", "replace").strip()[:200]
                )
    os.chmod(cel, 0o600)


@contextmanager
def otkryt_kopiyu(istochnik: Path, *, parol: str | None = None, klyuch: bytes | None = None):
    """Строки расшифрованного дампа, не кладя его на диск.

    На диск не кладём намеренно: расшифрованный дамп — это вся система в одном
    файле, и лишний его экземпляр во временном каталоге живёт ровно до первого
    сбоя уборки.
    """
    parol_openssl, dlina = _sverit(istochnik, parol, klyuch)
    with _parol_faylom(parol_openssl) as put_parolya:
        process = subprocess.Popen(
            _komanda("-d", put_parolya),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # Здесь труба с обеих сторон, и кормить из этого же потока нельзя: на
        # первом же дампе, который не помещается в буфер трубы, обе стороны
        # встанут насмерть.
        podacha = threading.Thread(
            target=_kormit,
            args=(process.stdin, istochnik, DLINA_SHAPKI, dlina),
            daemon=True,
        )
        podacha.start()
        try:
            yield io.TextIOWrapper(process.stdout, encoding="utf-8", errors="replace")
        finally:
            process.stdout.close()
            process.stderr.close()
            process.wait()
            podacha.join(timeout=30)


def _proverit_zashifrovannuyu(
    db_path: Path, report: dict, fail, parol: str | None, klyuch: bytes | None
) -> None:
    """Открывается ли копия этим ключом и годен ли дамп внутри."""
    report["encrypted"] = True
    try:
        report["sposob"] = NAZVANIE_SPOSOBA[prochitat_shapku(db_path).sposob]
        with otkryt_kopiyu(db_path, parol=parol, klyuch=klyuch) as stroki:
            razbor = _razobrat_dump(stroki)
    except NeTaKopiya as beda:
        report["engine"] = "unknown"
        fail(str(beda))
        return
    except NeTotKlyuch as beda:
        report["engine"] = "mysql"
        fail(str(beda))
        return
    report["engine"] = "mysql"
    # Метка сошлась — значит ключ тот самый, а не «похожий»: она выведена из того
    # же корня, и подобрать её, не зная ключа, нечем.
    report["klyuch"] = "подошёл"
    _proverit_dump(razbor, report, fail)


def verify(
    db_path: Path,
    storage_path: Path | None,
    secret_path: Path | None,
    *,
    parol: str | None = None,
    klyuch: bytes | None = None,
) -> dict:
    report: dict = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": str(db_path),
        "ok": True,
        "problems": [],
        "counts": {},
    }

    def fail(message: str) -> None:
        report["ok"] = False
        report["problems"].append(message)

    if not db_path.is_file():
        fail(f"файла базы нет: {db_path}")
        return report
    report["size"] = db_path.stat().st_size

    # Зашифрованную копию узнаём по опознанию в первых байтах, а не по имени:
    # имя ей даёт человек, который её скачал, и `.enc` он потерять волен.
    #
    # Незашифрованная — по расширению, которое ставит scripts/backup.sh. Чужой
    # формат называем негодным, а не читаем «как получится»: файл от прежней
    # установки лежит в том же каталоге, и выбрать его можно по ошибке.
    if zashifrovana(db_path):
        _proverit_zashifrovannuyu(db_path, report, fail, parol, klyuch)
    elif db_path.suffix.lower() != ".sql":
        report["engine"] = "unknown"
        fail(f"{db_path.name}: это не дамп базы — копии называются db-ГГГГ-ММ-ДД.sql")
        return report
    else:
        report["engine"] = "mysql"
        _proverit_dump(_prochitat_dump(db_path), report, fail)

    if storage_path is not None:
        report["storage"] = str(storage_path)
        if not storage_path.is_file():
            fail(f"архива storage нет: {storage_path}")
        else:
            # Читаем оглавление, не распаковывая: оборванный архив падает уже
            # на нём, а место под распаковку может и не найтись.
            listed = subprocess.run(
                ["tar", "-tzf", str(storage_path)], capture_output=True, text=True
            )
            if listed.returncode != 0:
                fail(f"архив storage не читается: {listed.stderr.strip()[:200]}")
            else:
                report["storage_entries"] = len(listed.stdout.splitlines())

    # Ключ шифрования. Его потеря необратима: пароли почтовых ящиков в
    # восстановленной базе не расшифровать никогда — ключ не выводится из
    # данных (см. `core/security/secretbox.py`).
    if secret_path is not None:
        report["secret"] = str(secret_path)
        if not secret_path.is_file():
            fail(
                "в копии нет ключа шифрования — восстановить получится, "
                "но пароли ящиков будут потеряны навсегда"
            )
        elif "OPENCRM_SECRET_KEY=" not in secret_path.read_text(encoding="utf-8"):
            fail("файл ключа есть, но самого ключа в нём нет")

    return report


ISPOLZOVANIE = "\n".join((
    "использование:",
    "  verify_backup.py <dump.sql|dump.sql.enc> [storage.tar.gz] [secret.env] [ключ]",
    "  verify_backup.py --zashifrovat ИСТОЧНИК ЦЕЛЬ ключ",
    "  verify_backup.py --rasshifrovat ИСТОЧНИК ЦЕЛЬ ключ",
    "  verify_backup.py --sozdat-klyuch ФАЙЛ",
    "",
    "ключ — одно из:",
    "  --parol-stdin        пароль первой строкой стандартного ввода",
    "  --klyuch-fayl ФАЙЛ   файл ключа (--sozdat-klyuch заводит такой)",
    "",
    "Ни пароля, ни ключа в аргументах: их видит `ps` любого пользователя машины",
    "и запоминает `docker inspect`.",
))

#: Флаги без значения. Отдельным списком, чтобы разбор не съел следующий
#: аргумент и не объявил путь к копии значением флага.
_ODINOCHNYE = ("--parol-stdin", "--zashifrovat", "--rasshifrovat")
_SO_ZNACHENIEM = ("--klyuch-fayl", "--sozdat-klyuch")


def _razobrat_argv(argv: list[str]) -> tuple[list[str], dict]:
    flagi: dict = {}
    puti: list[str] = []
    ostalos = list(argv)
    while ostalos:
        arg = ostalos.pop(0)
        if arg in _ODINOCHNYE:
            flagi[arg] = True
        elif arg in _SO_ZNACHENIEM:
            if not ostalos:
                raise ValueError(f"у {arg} не хватает значения")
            flagi[arg] = ostalos.pop(0)
        elif arg.startswith("--"):
            raise ValueError(f"неизвестный ключ {arg}")
        else:
            puti.append(arg)
    return puti, flagi


def _klyuch_ili_parol(flagi: dict) -> dict:
    """Как открывать копию. Пароль читается со стандартного ввода."""
    sposoby = {}
    if flagi.get("--parol-stdin"):
        parol = sys.stdin.readline().rstrip("\r\n")
        if not parol:
            raise ValueError("пароль пуст — открывать копию нечем")
        sposoby["parol"] = parol
    if flagi.get("--klyuch-fayl"):
        sposoby["klyuch"] = prochitat_klyuch(Path(flagi["--klyuch-fayl"]))
    if len(sposoby) > 1:
        raise ValueError("пароль и файл ключа разом — выберите одно")
    return sposoby


def main(argv: list[str]) -> int:
    if not argv:
        print(ISPOLZOVANIE)
        return 2
    try:
        puti, flagi = _razobrat_argv(argv)
        if "--sozdat-klyuch" in flagi:
            put = Path(flagi["--sozdat-klyuch"])
            zapisat_klyuch(put)
            print(f"ключ записан в {put}. Потеряете его — копия не откроется ничем")
            return 0
        sposoby = _klyuch_ili_parol(flagi)
        for flag, deystvie in (("--zashifrovat", zashifrovat), ("--rasshifrovat", rasshifrovat)):
            if not flagi.get(flag):
                continue
            if len(puti) != 2:
                raise ValueError(f"{flag} ждёт два пути: источник и цель")
            if not sposoby:
                raise ValueError(f"{flag} без пароля и без файла ключа")
            deystvie(Path(puti[0]), Path(puti[1]), **sposoby)
            print(f"{puti[0]} → {puti[1]}")
            return 0
    except (ValueError, NeTaKopiya, NeTotKlyuch) as beda:
        print(f"{beda}\n\n{ISPOLZOVANIE}")
        return 2

    if not puti:
        print(ISPOLZOVANIE)
        return 2
    db_path = Path(puti[0])
    storage = Path(puti[1]) if len(puti) > 1 else None
    secret = Path(puti[2]) if len(puti) > 2 else None

    report = verify(db_path, storage, secret, **sposoby)

    # Отчёт кладём рядом с копиями: вопрос «когда проверяли в последний раз»
    # задают тогда, когда копия уже понадобилась, и ответ должен лежать на
    # диске, а не в чьей-то памяти.
    try:
        (db_path.parent.parent / "last-check.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        # Некуда записать — не повод объявить годную копию негодной.
        pass

    counts = ", ".join(f"{k}: {v}" for k, v in report["counts"].items() if v)
    if report["ok"]:
        chasti = [report.get("engine", "?")]
        if report.get("encrypted"):
            # То, ради чего проверка зашифрованной копии вообще существует:
            # «ключ подошёл» — единственный способ узнать о потере ключа не в
            # день аварии.
            chasti.append(f"зашифрована ({report.get('sposob')}), ключ подошёл")
        chasti.append(counts or "пусто")
        print("копия годна · " + " · ".join(chasti))
        return 0
    print("КОПИЯ НЕГОДНА:")
    for problem in report["problems"]:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
