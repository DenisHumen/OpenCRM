"""Шифрование резервной копии: круг «зашифровал → расшифровал → байт в байт».

Копия уезжает с сервера и лежит где угодно годами — в «Загрузках», в мессенджере
«чтобы не потерять», на чужой флешке. Дамп при этом не «данные клиентов», а вся
система в одном файле: адреса, переписка, деньги, хэши паролей и — намеренно —
`OPENCRM_SECRET_KEY`, которым расшифровывается всё остальное. Поэтому наружу она
уезжает только зашифрованной, и проверять тут надо две вещи, а не одну.

**Первая — что круг замыкается.** Шифрование, из которого не выходит исходный
файл байт в байт, обнаруживается в день аварии, и тогда копии нет вовсе.

**Вторая, и она важнее, — что неверный ключ ОТКАЗЫВАЕТ.** Проверено живьём:
`openssl enc -d -aes-256-ctr` с неверным паролем не сообщает об ошибке. Он молча
отдаёт мусор нужного размера и выходит с нулём. Владелец ошибается паролем на
букву, система «расшифровывает» копию и заливает мусор поверх живой базы — живой
базы больше нет, копии тоже. Ради этого случая в формате есть метка подлинности,
и сверяется она ДО расшифровки.

Разбор устройства и доводы — `docs/15-backup-encryption.md`.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_backup
# Снятие копии и правдоподобный дамп берём у соседнего набора: способ звать
# `scripts/backup.sh` и вид дампа не должны разъехаться в двух местах.
from tests.test_backup import _snyat_kopiyu, good_dump

# Гоняем настоящий openssl — тот же, что стоит в образе приложения. Подставлять
# сюда чистый Python значило бы проверить не то, чем копия шифруется на деле.
pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="нужен openssl (он есть в образе app)"
)

nuzhen_sh = pytest.mark.skipif(
    os.name != "posix" or shutil.which("sh") is None, reason="нужен POSIX sh"
)

PAROL = "правильный пароль копии"
NE_TOT_PAROL = "правильный пароль копиИ"  # отличается одной буквой — как опечатка


def _klyuch(tmp_path: Path) -> bytes:
    return bytes.fromhex(verify_backup.zapisat_klyuch(tmp_path / "kluch.txt"))


# --- круг: зашифровал → расшифровал → байт в байт -----------------------------


def test_krug_parolem(tmp_path):
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    bylo = ishodnik.read_bytes()
    shifrovannaya = tmp_path / "db.sql.enc"

    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)
    vernuli = tmp_path / "vernuli.sql"
    verify_backup.rasshifrovat(shifrovannaya, vernuli, parol=PAROL)

    assert vernuli.read_bytes() == bylo
    assert ishodnik.read_bytes() == bylo, "шифрование испортило исходник"


def test_krug_faylom_klyucha(tmp_path):
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    klyuch = _klyuch(tmp_path)

    verify_backup.zashifrovat(ishodnik, shifrovannaya, klyuch=klyuch)
    vernuli = tmp_path / "vernuli.sql"
    verify_backup.rasshifrovat(shifrovannaya, vernuli, klyuch=klyuch)

    assert vernuli.read_bytes() == ishodnik.read_bytes()


def test_krug_na_bolshom_fayle(tmp_path):
    """Дамп не помещается в память — в этом вся затея с потоком.

    Файл нарочно длиннее одного куска чтения: обрыв на границе куска даёт копию,
    которая расшифровывается «почти вся», и заметить это на трёх строках нельзя.
    """
    ishodnik = tmp_path / "db.sql"
    ishodnik.write_bytes(os.urandom(verify_backup.KUSOK * 3 + 12345))
    shifrovannaya = tmp_path / "db.sql.enc"

    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)
    vernuli = tmp_path / "vernuli.sql"
    verify_backup.rasshifrovat(shifrovannaya, vernuli, parol=PAROL)

    assert vernuli.read_bytes() == ishodnik.read_bytes()


def test_shifrotekst_ne_soderzhit_dampa(tmp_path):
    """Положительная половина: файл вправду зашифрован, а не переложен.

    Без неё «шифрование», которое просто копирует файл, прошло бы весь круг
    зелёным — и уехало бы наружу открытым текстом.
    """
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    telo = shifrovannaya.read_bytes()
    assert b"CREATE TABLE" not in telo
    assert b"u0@example.com" not in telo
    assert telo.startswith(verify_backup.MAGIYA), "копия не опознаётся по заголовку"


# --- неверный ключ обязан быть ОТКАЗОМ ----------------------------------------


def test_nevernyy_parol_otkazyvaet_a_ne_otdayot_musor(tmp_path):
    """Главная проверка всего формата.

    `openssl` про неверный пароль молчит и выходит с нулём — значит отказать
    обязаны мы, и обязаны ДО того, как на диске появится хоть байт «расшифровки».
    Иначе этим мусором зальют живую базу.
    """
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    vernuli = tmp_path / "vernuli.sql"
    with pytest.raises(verify_backup.NeTotKlyuch) as beda:
        verify_backup.rasshifrovat(shifrovannaya, vernuli, parol=NE_TOT_PAROL)

    # Отказ обязан быть понятным: человек должен узнать, что перепутал пароль, а
    # не пойти искать копию поновее.
    assert "пароль" in str(beda.value)
    assert not vernuli.exists(), "мусор всё-таки лёг на диск"


def test_nevernyy_klyuch_otkazyvaet(tmp_path):
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, klyuch=_klyuch(tmp_path))

    chuzhoy = bytes.fromhex(verify_backup.porodit_klyuch())
    with pytest.raises(verify_backup.NeTotKlyuch) as beda:
        verify_backup.rasshifrovat(shifrovannaya, tmp_path / "vernuli.sql", klyuch=chuzhoy)
    assert "файл ключа" in str(beda.value)


def test_isporchennyy_shifrotekst_zamechen(tmp_path):
    """Недокачанный или побитый файл — вторая половина той же беды.

    Мусор из-за одной перевёрнутой в дороге буквы ничем не лучше мусора из-за
    неверного пароля: залив его, живую базу теряют так же.
    """
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    telo = bytearray(shifrovannaya.read_bytes())
    seredina = len(telo) // 2
    telo[seredina] ^= 0x01
    shifrovannaya.write_bytes(telo)

    with pytest.raises(verify_backup.NeTotKlyuch):
        verify_backup.rasshifrovat(shifrovannaya, tmp_path / "vernuli.sql", parol=PAROL)


def test_podmenyonnye_parametry_vyvoda_klyucha_zamecheny(tmp_path):
    """Параметры scrypt лежат в файле открытым текстом — и это нормально.

    Ненормально было бы, если бы их можно было ослабить по дороге: подмени `p` с
    единицы на двойку — и копия «открылась бы» другим ключом. Проверяем, что
    подделанный заголовок кончается отказом, а не расшифровкой.
    """
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    telo = bytearray(shifrovannaya.read_bytes())
    # Последний байт параметра `p` — он идёт третьим из трёх четырёхбайтных.
    telo[len(verify_backup.MAGIYA) + 2 + 11] = 2
    shifrovannaya.write_bytes(telo)

    with pytest.raises(verify_backup.NeTotKlyuch):
        verify_backup.rasshifrovat(shifrovannaya, tmp_path / "vernuli.sql", parol=PAROL)


def test_chuzhoy_fayl_ne_prinimayut_za_kopiyu(tmp_path):
    """Опознание в начале файла — чтобы не расшифровывать чужое.

    В каталоге копий лежат файлы от прежних установок, и выбрать не тот легко.
    «Расшифровать» такой файл значит получить мусор и не узнать об этом.
    """
    chuzhoy = tmp_path / "chuzhoy.enc"
    chuzhoy.write_bytes(b"age-encryption.org/v1\n" + os.urandom(200))

    with pytest.raises(verify_backup.NeTaKopiya):
        verify_backup.rasshifrovat(chuzhoy, tmp_path / "vernuli.sql", parol=PAROL)
    assert not verify_backup.zashifrovana(chuzhoy)

    # И отдельно — файл, у которого верно ВСЁ, кроме опознания. Так выглядела бы
    # копия чужой системы, совпавшая с нами длиной заголовка: без опознания её
    # разбор доехал бы до самой расшифровки и кончился бы «неверным паролем» —
    # то есть человека послали бы искать пароль к чужому файлу.
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    podmena = tmp_path / "podmena.enc"
    verify_backup.zashifrovat(ishodnik, podmena, parol=PAROL)
    telo = bytearray(podmena.read_bytes())
    telo[:7] = b"OPENXXX"
    podmena.write_bytes(telo)

    with pytest.raises(verify_backup.NeTaKopiya):
        verify_backup.rasshifrovat(podmena, tmp_path / "vernuli2.sql", parol=PAROL)


def test_kopiya_chuzhoy_versii_otkazyvaet_nazvav_versiyu(tmp_path):
    """Формат будет меняться. Копия из будущего обязана сказать это словами,
    а не быть расшифрованной по правилам, которых она не знает."""
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    telo = bytearray(shifrovannaya.read_bytes())
    telo[len(verify_backup.MAGIYA)] = verify_backup.VERSIYA + 1
    shifrovannaya.write_bytes(telo)

    with pytest.raises(verify_backup.NeTaKopiya) as beda:
        verify_backup.rasshifrovat(shifrovannaya, tmp_path / "vernuli.sql", parol=PAROL)
    assert str(verify_backup.VERSIYA + 1) in str(beda.value)


# --- заголовок: по нему видно, чем копию открывать ----------------------------


def test_sposob_viden_bez_vsyakogo_klyucha(tmp_path):
    """Через год человек не вспомнит, паролем он её закрыл или файлом ключа."""
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    parolem = tmp_path / "parolem.enc"
    klyuchom = tmp_path / "klyuchom.enc"
    verify_backup.zashifrovat(ishodnik, parolem, parol=PAROL)
    verify_backup.zashifrovat(ishodnik, klyuchom, klyuch=_klyuch(tmp_path))

    assert verify_backup.prochitat_shapku(parolem).sposob == verify_backup.SPOSOB_PAROL
    assert verify_backup.prochitat_shapku(klyuchom).sposob == verify_backup.SPOSOB_KLYUCH


def test_parametry_scrypt_zhivut_v_fayle_a_ne_v_kode(tmp_path, monkeypatch):
    """Копия, снятая сегодня, обязана открыться после того, как параметры
    ужесточат. Иначе первое же удорожание перебора обнулит весь архив копий."""
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    shapka = verify_backup.prochitat_shapku(shifrovannaya)
    assert (shapka.n, shapka.r, shapka.p) == (
        verify_backup.SCRYPT_N, verify_backup.SCRYPT_R, verify_backup.SCRYPT_P
    )

    monkeypatch.setattr(verify_backup, "SCRYPT_N", verify_backup.SCRYPT_N * 2)
    vernuli = tmp_path / "vernuli.sql"
    verify_backup.rasshifrovat(shifrovannaya, vernuli, parol=PAROL)
    assert vernuli.read_bytes() == ishodnik.read_bytes()


def test_v_sposobe_s_klyuchom_scrypt_ne_pritvoryaetsya(tmp_path):
    """Файл ключа — 32 байта из `os.urandom`, корень уже полноэнтропийный.

    Записанные над ним параметры scrypt означали бы, что мы усилили то, чего
    усилить нельзя, и заодно платили бы за это памятью на каждой расшифровке.
    """
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    klyuchom = tmp_path / "klyuchom.enc"
    verify_backup.zashifrovat(ishodnik, klyuchom, klyuch=_klyuch(tmp_path))

    shapka = verify_backup.prochitat_shapku(klyuchom)
    assert (shapka.n, shapka.r, shapka.p) == (0, 0, 0)


# --- секрет мимо argv ---------------------------------------------------------


def test_ni_parol_ni_klyuch_ne_popadayut_v_argv(tmp_path, monkeypatch):
    """Аргументы процесса видит `ps` любого пользователя машины.

    Они же оседают в `docker inspect`, то есть переживают сам процесс. У openssl
    сырой ключ принимает только `-K`, и это ровно тот путь, которым ходить
    нельзя; единственный законный — файл, как у пароля mysqldump в backup.sh.

    Проверяем НАСТОЯЩИЙ запуск, а не текст модуля: подсматриваем командную
    строку и содержимое файла пароля в тот момент, когда openssl уже зовут.
    """
    komandy: list[list[str]] = []
    sekrety: list[str] = []
    rezhimy: list[int] = []

    def podsmotret(argv) -> None:
        komandy.append([str(a) for a in argv])
        for arg in komandy[-1]:
            if arg.startswith("file:"):
                put = Path(arg[len("file:"):])
                sekrety.append(put.read_text(encoding="ascii").strip())
                if os.name == "posix":
                    rezhimy.append(stat.S_IMODE(put.stat().st_mode))

    nastoyashchiy_run = subprocess.run
    nastoyashchiy_popen = subprocess.Popen

    def run(argv, *ostalnye, **imenovannye):
        podsmotret(argv)
        return nastoyashchiy_run(argv, *ostalnye, **imenovannye)

    def popen(argv, *ostalnye, **imenovannye):
        podsmotret(argv)
        return nastoyashchiy_popen(argv, *ostalnye, **imenovannye)

    monkeypatch.setattr(verify_backup.subprocess, "run", run)
    monkeypatch.setattr(verify_backup.subprocess, "Popen", popen)

    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    klyuch = _klyuch(tmp_path)
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)
    verify_backup.rasshifrovat(shifrovannaya, tmp_path / "v1.sql", parol=PAROL)
    klyuchom = tmp_path / "klyuchom.enc"
    verify_backup.zashifrovat(ishodnik, klyuchom, klyuch=klyuch)
    verify_backup.rasshifrovat(klyuchom, tmp_path / "v2.sql", klyuch=klyuch)

    # `subprocess.run` зовёт `Popen` изнутри, поэтому записей больше четырёх.
    # Важно другое: openssl вправду звали, и в обе стороны.
    assert len(komandy) >= 4, f"openssl звали {len(komandy)} раз — проверять нечего"
    assert {"-e", "-d"} <= {k[2] for k in komandy}, komandy

    for komanda in komandy:
        assert "-K" not in komanda, "сырой ключ ушёл в argv"
        assert "-k" not in komanda, "пароль ушёл в argv"
        for arg in komanda:
            assert PAROL not in arg, f"пароль владельца в argv: {komanda}"
            assert klyuch.hex() not in arg, f"файл ключа в argv: {komanda}"
            assert not arg.startswith("pass:"), f"секрет в argv: {komanda}"
            # Производный пароль — 64 знака шестнадцатеричной строкой. Он такой
            # же секрет: знающий его расшифрует копию без пароля владельца.
            assert not re.fullmatch(r"[0-9a-fA-F]{64}", arg), f"ключ в argv: {komanda}"
        for sekret in sekrety:
            assert sekret not in komanda, f"секрет из файла оказался и в argv: {komanda}"

    assert sekrety, "секрет ушёл в openssl не файлом — значит чем-то ещё"
    if os.name == "posix":
        assert rezhimy and all(r == 0o600 for r in rezhimy), (
            f"файл пароля открыт посторонним: {[oct(r) for r in rezhimy]}"
        )


# --- проверка годности зашифрованной копии ------------------------------------


def test_verify_otkryvaet_zashifrovannuyu_kopiyu(tmp_path):
    """Страховка к «потерял ключ — потерял копию».

    Голого предупреждения мало: потеря ключа обнаруживается в день аварии, когда
    поздно. Проверка обязана СКАЗАТЬ, что копия этим ключом открывается.
    """
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    otchyot = verify_backup.verify(shifrovannaya, None, None, parol=PAROL)
    assert otchyot["ok"], otchyot["problems"]
    assert otchyot["encrypted"] is True
    assert otchyot["klyuch"] == "подошёл"
    assert otchyot["sposob"] == "пароль"
    # И то же, что у обычной копии: проверка вправду заглянула ВНУТРЬ.
    assert otchyot["revision"] == "c3d9f2a71b58"
    assert otchyot["counts"]["users"] == 3


def test_verify_vidit_oborvannyy_damp_vnutri_shifra(tmp_path):
    """Ключ подошёл — а копия всё равно негодна, и это разные вещи.

    Проверка, довольствующаяся сошедшейся меткой, объявила бы годным
    зашифрованный огрызок. Метка стережёт ключ, а не полноту дампа.
    """
    ishodnik = good_dump(tmp_path / "db.sql", users=400)
    celikom = ishodnik.read_bytes()
    ishodnik.write_bytes(celikom[: len(celikom) // 2])
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    otchyot = verify_backup.verify(shifrovannaya, None, None, parol=PAROL)
    assert otchyot["klyuch"] == "подошёл", "беда не в ключе"
    assert not otchyot["ok"]
    assert any("не дописан до конца" in p for p in otchyot["problems"]), otchyot["problems"]


def test_verify_ne_tem_klyuchom_govorit_pro_klyuch(tmp_path):
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    otchyot = verify_backup.verify(shifrovannaya, None, None, parol=NE_TOT_PAROL)
    assert not otchyot["ok"]
    assert "klyuch" not in otchyot, "непроверенный ключ объявлен подошедшим"
    assert any("пароль" in p for p in otchyot["problems"]), otchyot["problems"]


def test_verify_bez_klyucha_govorit_chem_otkryvat(tmp_path):
    """Копия есть, ключа не дали. Сказать «негодна» тут — соврать: она годна,
    просто нечем открыть, и человеку надо объяснить именно это."""
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, klyuch=_klyuch(tmp_path))

    otchyot = verify_backup.verify(shifrovannaya, None, None)
    assert not otchyot["ok"]
    assert otchyot["sposob"] == "файл ключа"
    assert any("не дали" in p for p in otchyot["problems"]), otchyot["problems"]


def test_obychnaya_kopiya_proveryaetsya_kak_prezhde(tmp_path):
    """Шифрование добавлено СБОКУ и ничего не ломает.

    Ежедневная копия остаётся незашифрованной и проверяется тем же вызовом без
    единого нового довода.
    """
    otchyot = verify_backup.verify(good_dump(tmp_path / "db.sql", users=3), None, None)
    assert otchyot["ok"], otchyot["problems"]
    assert otchyot["engine"] == "mysql"
    assert "encrypted" not in otchyot


# --- то же самое через настоящую точку входа ----------------------------------


def _zapustit(*argv, vvod: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.verify_backup", *argv],
        input=vvod, capture_output=True, text=True, encoding="utf-8", timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_cli_krug_parolem_so_standartnogo_vvoda(tmp_path):
    """Тем же вызовом это делает человек и будет делать кнопка на сайте."""
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    vernuli = tmp_path / "vernuli.sql"

    zapusk = _zapustit("--zashifrovat", str(ishodnik), str(shifrovannaya),
                       "--parol-stdin", vvod=f"{PAROL}\n")
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr

    zapusk = _zapustit("--rasshifrovat", str(shifrovannaya), str(vernuli),
                       "--parol-stdin", vvod=f"{PAROL}\n")
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr
    assert vernuli.read_bytes() == ishodnik.read_bytes()


def test_cli_proveryaet_kopiyu_faylom_klyucha(tmp_path):
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    klyuch_fayl = tmp_path / "kluch.txt"

    assert _zapustit("--sozdat-klyuch", str(klyuch_fayl)).returncode == 0
    assert _zapustit("--zashifrovat", str(ishodnik), str(shifrovannaya),
                     "--klyuch-fayl", str(klyuch_fayl)).returncode == 0

    zapusk = _zapustit(str(shifrovannaya), "--klyuch-fayl", str(klyuch_fayl))
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr
    assert "ключ подошёл" in zapusk.stdout, zapusk.stdout


def test_cli_ne_tem_klyuchom_daet_nenulevoy_kod(tmp_path):
    """Отказ обязан быть кодом возврата, а не строчкой в выводе: зовут это
    из скриптов, а скрипты читают код."""
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    verify_backup.zashifrovat(ishodnik, shifrovannaya, parol=PAROL)

    zapusk = _zapustit(str(shifrovannaya), "--parol-stdin", vvod=f"{NE_TOT_PAROL}\n")
    assert zapusk.returncode == 1, zapusk.stdout
    assert "НЕГОДНА" in zapusk.stdout, zapusk.stdout


@pytest.mark.skipif(os.name != "posix", reason="права файла — понятие POSIX")
def test_zashifrovannaya_kopiya_i_klyuch_zakryty_ot_postoronnih(tmp_path):
    """Зашифрованный дамп — всё ещё вся система в одном файле, а файл ключа —
    и вовсе вся копия. Оба закрываются так же, как обычная копия."""
    ishodnik = good_dump(tmp_path / "db.sql", users=3)
    shifrovannaya = tmp_path / "db.sql.enc"
    klyuch_fayl = tmp_path / "kluch.txt"
    verify_backup.zapisat_klyuch(klyuch_fayl)
    verify_backup.zashifrovat(ishodnik, shifrovannaya,
                              klyuch=verify_backup.prochitat_klyuch(klyuch_fayl))

    for fayl in (shifrovannaya, klyuch_fayl):
        rezhim = stat.S_IMODE(fayl.stat().st_mode)
        assert rezhim == 0o600, f"{fayl.name}: права {rezhim:o}, а не 600"


# --- «копию давно не забирали» ------------------------------------------------
#
# Копии лежат на том же диске, что и база. Пока увоз ручной, единственная защита
# — сказать вслух; молчание здесь означает «всё хорошо», чего мы как раз не знаем.
#
# Гоняем НАСТОЯЩИЙ scripts/backup.sh: беда эта в поведении, а не в тексте.


def _predupredil(zapusk) -> bool:
    return "не забирали" in zapusk.stderr


@nuzhen_sh
def test_bez_otmetki_uvoza_predupreditel_govorit(tmp_path):
    zapusk, backups = _snyat_kopiyu(tmp_path)

    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr
    assert "backup done" in zapusk.stdout, "предупреждение сорвало снятие копии"
    assert _predupredil(zapusk), zapusk.stderr
    assert "НИ РАЗУ" in zapusk.stderr, zapusk.stderr
    # Человеку сказано не только «плохо», но и что именно сделать.
    assert str(backups / "last-export") in zapusk.stderr, zapusk.stderr


@nuzhen_sh
def test_svezhaya_otmetka_uvoza_zastavlyaet_molchat(tmp_path):
    """Половина сторожа, без которой «ругаться всегда» тоже было бы зелёным.

    Предупреждение, которое горит постоянно, перестают читать за неделю — вместе
    с теми, что рядом и по делу.
    """
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "last-export").write_text("забрали сегодня", encoding="utf-8")

    zapusk, _ = _snyat_kopiyu(tmp_path)
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr
    assert not _predupredil(zapusk), zapusk.stderr


@nuzhen_sh
def test_staraya_otmetka_uvoza_snova_predupreditel(tmp_path):
    """Забрали однажды — и с тех пор полгода не забирали: это ровно тот случай,
    ради которого отметка вообще заведена."""
    backups = tmp_path / "backups"
    backups.mkdir()
    otmetka = backups / "last-export"
    otmetka.write_text("забрали давным-давно", encoding="utf-8")
    davno = os.stat(otmetka).st_mtime - 30 * 86400
    os.utime(otmetka, (davno, davno))

    zapusk, _ = _snyat_kopiyu(tmp_path)
    assert zapusk.returncode == 0, zapusk.stdout + zapusk.stderr
    assert _predupredil(zapusk), zapusk.stderr
    assert "больше 7 дней" in zapusk.stderr, zapusk.stderr
