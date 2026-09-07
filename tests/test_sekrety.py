"""Зашифрованное переживает восстановление копии на ДРУГОЙ машине.

Беда тихая, и в этом вся её сила: копия базы восстанавливается целиком, схема
сходится, `/healthz` отвечает — а пароли ящиков и секреты двухфакторок в ней
превращаются в мусор, потому что на новой машине `opencrm.sh` завёл свой
`OPENCRM_SECRET_KEY`. Ни одной ошибки при этом не происходит: узнают об этом на
первом входе, когда старую машину уже погасили.

Лечится тем, что копия базы везёт ключ, которым снята, а восстановление сразу
после заливки перекладывает токены под нынешний ключ. Проверяется здесь.

Разбор — `docs/ekspluatatsiya/15-kopii-s-shifrovaniem.md` §11.
"""

from pathlib import Path

import pytest

from core.security import secretbox
from core.services import backup_service, mail_service, sekrety_service
from database.models import MailAccount
from database.session import Base
from scripts import snapshot_db

CHUZHOY_KLYUCH = "klyuch-so-staroy-mashiny-0001"


def test_reestr_znaet_kazhduyu_zashifrovannuyu_kolonku():
    """Колонка, забытая в `MESTA`, переживёт восстановление нечитаемой.

    И узнают об этом не здесь, а через полгода, когда понадобится войти. Отсюда
    сторож по схеме, а не по памяти: новая колонка `*_encrypted` обязана быть
    названа в реестре в том же коммите, что и сама колонка.
    """
    v_reestre = {(model.__tablename__, kolonka.key) for model, kolonka, _ in sekrety_service.MESTA}
    v_sheme = {
        (tablica.name, kolonka.name)
        for tablica in Base.metadata.tables.values()
        for kolonka in tablica.columns
        if kolonka.name.endswith("_encrypted")
    }
    assert v_sheme, "в схеме не нашлось ни одной колонки *_encrypted — сторож перестал стеречь"
    assert v_sheme <= v_reestre, (
        "эти шифротексты не переживут восстановления на другой машине: "
        f"{sorted(v_sheme - v_reestre)}"
    )


def test_kopiya_bazy_vezyot_klyuch_shifrovaniya(tmp_path: Path):
    """Ключ едет строкой-комментарием в самом дампе — и не портит копию.

    Комментарий выбран не от лени: MySQL его при заливке не заметит. А стоит он
    ПЕРЕД меткой конца, потому что годность копии проверяется по последней
    строке: строка за меткой читается как «дамп оборвался», и копия с ключом
    объявлялась бы негодной (поймано `test_backup_sayt.py`).
    """
    damp = tmp_path / "db.sql"
    damp.write_text(
        "CREATE TABLE `users` (`id` int);\n"
        f"-- ёлочки и кириллица, чтобы хвост резался не по букве\n"
        f"{snapshot_db.METKA}: таблиц 1, строк 0\n",
        encoding="utf-8",
    )
    backup_service._dopisat_klyuchi(damp)

    assert snapshot_db.celaya(damp), snapshot_db.pochemu_ne_celaya(damp)
    stroki = damp.read_text(encoding="utf-8").splitlines()
    assert stroki[-1].startswith(snapshot_db.METKA), "метка конца перестала быть последней"
    klyuchi = backup_service.klyuchi_iz_dampa(damp)
    assert klyuchi["OPENCRM_SECRET_KEY"], "копия уехала без ключа шифрования"
    assert "OPENCRM_IP_HASH_SALT" in klyuchi
    assert stroki.count(stroki[-2]) == 1 and stroki[-2].startswith(backup_service.KLYUCHI_V_KOPII)


def test_damp_bez_stroki_klyuchey_nichego_ne_vydumyvaet(tmp_path: Path):
    """Копия, снятая до этой правки, обязана честно сказать «ключа нет».

    Пустой ответ здесь превращается в отметку «bez_klyucha» на экране; выдумай
    разбор что-нибудь своё — восстановление молча решило бы, что перекладывать
    нечего.
    """
    damp = tmp_path / "staraya.sql"
    damp.write_text(f"{snapshot_db.METKA}: таблиц 0, строк 0\n", encoding="utf-8")
    assert backup_service.klyuchi_iz_dampa(damp) == {}


def test_isporchennaya_stroka_klyuchey_ne_roniaet_razbor(tmp_path: Path):
    """Битая строка — это «ключа нет», а не отказ восстановления.

    Копия и без ключа стоит того, чтобы её залить: данные в ней целы.
    """
    damp = tmp_path / "bitaya.sql"
    damp.write_text(
        f"{snapshot_db.METKA}: таблиц 0, строк 0\n{backup_service.KLYUCHI_V_KOPII}не-base64!!\n",
        encoding="utf-8",
    )
    assert backup_service.klyuchi_iz_dampa(damp) == {}


def _yashchik(db, adres: str, token: str) -> MailAccount:
    yashchik = MailAccount(title="Сундук секретов", address=adres, password_encrypted=token)
    db.add(yashchik)
    db.flush()
    return yashchik


def test_token_s_chuzhogo_klyucha_perekladyvaetsya_pod_nyneshniy(db, chuzhoy_shifr):
    """Ради этого всё и написано: копия с другой машины открывается.

    Токен зашифрован ключом, которого у этой машины нет. После перекладки он
    обязан читаться НЫНЕШНИМ ключом — то есть `mail_service` откроет пароль
    ящика, не спрашивая человека заново.
    """
    parol = "parol-yashchika-8891"
    chuzhoy = chuzhoy_shifr(parol)
    yashchik = _yashchik(db, "chuzhoy.klyuch@test.local", chuzhoy)

    itog = sekrety_service.perelozhit(db, CHUZHOY_KLYUCH)
    db.flush()
    db.refresh(yashchik)

    assert itog["perelozheno"] >= 1, itog
    assert not itog["tot_zhe_klyuch"] and not itog["bez_klyucha"]
    assert yashchik.password_encrypted != chuzhoy, "токен остался под чужим ключом"
    assert secretbox.decrypt(yashchik.password_encrypted, mail_service.SECRET_PURPOSE) == parol


def test_tot_zhe_klyuch_nichego_ne_trogaet(db):
    """Восстановление на своей же машине — самый частый случай (откат).

    Токены там и так под нынешним ключом, и переписывать их значило бы трогать
    данные без причины: каждая перезапись — ещё один шанс оборваться на
    середине.
    """
    token = secretbox.encrypt("parol-svoy", mail_service.SECRET_PURPOSE)
    yashchik = _yashchik(db, "svoy.klyuch@test.local", token)

    from config.settings import get_settings

    itog = sekrety_service.perelozhit(db, get_settings().secret_key)
    db.flush()
    db.refresh(yashchik)

    assert itog["tot_zhe_klyuch"] is True
    assert itog["perelozheno"] == 0
    assert yashchik.password_encrypted == token, "токен переписали без нужды"


def test_ne_otkryvshiysya_token_schitayetsya_a_ne_zatirayetsya(db):
    """Токен, не открывшийся ключом из копии, — испорчен или уже наш.

    Ни то, ни другое трогать нельзя: перезапись мусором сделала бы потерю
    окончательной. Но и молчать нельзя — иначе перекладка объявит себя
    удавшейся над данными, которых не открыла.
    """
    musor = secretbox.encrypt("parol-tretiy", mail_service.SECRET_PURPOSE)
    yashchik = _yashchik(db, "ne.otkroetsya@test.local", musor)

    itog = sekrety_service.perelozhit(db, CHUZHOY_KLYUCH)
    db.flush()
    db.refresh(yashchik)

    assert itog["ne_otkrylis"] >= 1, itog
    assert yashchik.password_encrypted == musor, "нечитаемый токен затёрли"


def test_krug_tselikom_kopiya_otsyuda_otkryvaetsya_na_drugoy_mashine(db, tmp_path, monkeypatch):
    """Обещание модуля целиком: снял здесь — открылось там.

    Другая машина изображается подменой `get_settings` в двух модулях, которые
    только и знают о ключе. Иначе для этой проверки нужна вторая установка, а
    без неё все части (ключ в дампе, разбор, перекладка) стерегутся порознь и
    ни одна не отвечает за целое.
    """
    parol = "parol-kotoryy-dolzhen-vyzhit"
    yashchik = _yashchik(db, "krug.tselikom@test.local", secretbox.encrypt(parol, mail_service.SECRET_PURPOSE))

    damp = tmp_path / "db.sql"
    damp.write_text(f"{snapshot_db.METKA}: таблиц 0, строк 0\n", encoding="utf-8")
    backup_service._dopisat_klyuchi(damp)
    klyuch_kopii = backup_service.klyuchi_iz_dampa(damp)["OPENCRM_SECRET_KEY"]

    class NovayaMashina:
        secret_key = "klyuch-zavedyonnyy-ustanovkoy-na-novoy-mashine"

    monkeypatch.setattr(secretbox, "get_settings", lambda: NovayaMashina)
    monkeypatch.setattr(sekrety_service, "get_settings", lambda: NovayaMashina)

    itog = sekrety_service.perelozhit(db, klyuch_kopii)
    db.flush()
    db.refresh(yashchik)

    assert itog["perelozheno"] >= 1 and itog["ne_otkrylis"] == 0, itog
    assert (
        secretbox.decrypt(yashchik.password_encrypted, mail_service.SECRET_PURPOSE) == parol
    ), "на новой машине пароль ящика не открылся — ради этого всё и написано"


def test_kopiya_bez_klyucha_ne_schitaetsya_uspehom(db):
    """Копия, снятая до этой правки, на другой машине уже потеряна.

    Сказать об этом обязаны словами: «переложено 0» человек прочитает как
    «нечего было перекладывать».
    """
    itog = sekrety_service.perelozhit(db, "")
    assert itog["bez_klyucha"] is True
    assert itog["perelozheno"] == 0


@pytest.fixture
def chuzhoy_shifr(monkeypatch):
    """Токен, снятый на машине с другим `OPENCRM_SECRET_KEY`.

    Настоящим `secretbox.encrypt` с подменённым ключом настроек, а не своей
    сборкой токена: свою пришлось бы править вслед за форматом, и она молча
    проверяла бы вчерашний.
    """

    class Chuzhie:
        secret_key = CHUZHOY_KLYUCH

    def sdelat(otkrytoe: str) -> str:
        monkeypatch.setattr(secretbox, "get_settings", lambda: Chuzhie)
        try:
            return secretbox.encrypt(otkrytoe, mail_service.SECRET_PURPOSE)
        finally:
            monkeypatch.undo()

    return sdelat
