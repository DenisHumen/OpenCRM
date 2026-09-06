"""Копия системы с экрана настроек: ключ, снятие, скачивание, восстановление.

Что здесь стережётся и почему (`docs/15-backup-encryption.md` §10):

- копия не снимается без подтверждённого ключа — иначе первая же копия
  оказалась бы незашифрованной «по недосмотру»;
- копия открывается ТЕМ ключом, что сейчас в системе, и замена ключа делает
  это видимым: «потерял ключ — потерял копию» обнаруживается кнопкой, а не в
  день аварии;
- восстановление отказывает ДО того, как тронуть базу: чужой ключ, не копия,
  обрывок без метки конца, ревизия, которой здесь нет;
- восстановление и вправду возвращает базу к снятому состоянию, снимок живой
  базы остаётся рядом, режим обслуживания снимается, след в журнале есть.
"""

import io
import shutil
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.services import backup_service
from scripts import snapshot_db, verify_backup
from tests.conftest import API

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="нужен openssl (он есть в образе app)"
)

BACKUPS = f"{API}/system/backups"


@pytest.fixture
def sayt(tmp_path, monkeypatch):
    """Свой каталог копий и своё хранилище: боевые `data/` и `storage/` не трогаем."""
    from config.settings import get_settings

    monkeypatch.setenv("OPENCRM_DATA_DIR", str(tmp_path / "data"))
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(get_settings(), "storage_dir", storage)
    return tmp_path


def _dozhdatsya(job_id: str, sekund: float = 120.0) -> dict:
    """Работа идёт в потоке — ждём отчёта, а не угадываем по времени."""
    krai = time.monotonic() + sekund
    while time.monotonic() < krai:
        job = backup_service.rabota(job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.2)
    raise AssertionError(f"работа {job_id} не кончилась за {sekund} с")


def _zavesti_klyuch(root_client, *, replace: bool = False) -> str:
    r = root_client.post(f"{BACKUPS}/key", json={"replace": replace})
    assert r.status_code == 200, r.text
    klyuch = r.json()["key"]
    r = root_client.post(f"{BACKUPS}/key/confirm", json={"fragment": klyuch[-backup_service.FRAGMENT:]})
    assert r.status_code == 200, r.text
    assert r.json()["exists"] is True
    return klyuch


def _snyat(root_client, kind: str) -> dict:
    r = root_client.post(f"{BACKUPS}/{kind}")
    assert r.status_code == 200, r.text
    job = _dozhdatsya(r.json()["id"])
    assert job["status"] == "done", job.get("error")
    return job


def _v_zhurnale(root_client, action: str) -> list[dict]:
    return [e for e in root_client.get(f"{API}/audit", params={"action": action}).json()["items"] if e["action"] == action]


# --- удаление ----------------------------------------------------------------


def test_kopiya_udalyaetsya_s_servera_ranshe_sroka(root_client, sayt):
    """Кнопка «Удалить» убирает файл и запись, а след остаётся в журнале.

    Без неё увезённая копия лежала бы на диске сутки: вся система одним файлом
    рядом с базой, и убрать её мог только таймер.
    """
    _zavesti_klyuch(root_client)
    job = _snyat(root_client, "db")
    fayl = backup_service.katalog() / f"{job['id']}.enc"
    assert fayl.is_file()

    r = root_client.delete(f"{BACKUPS}/jobs/{job['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == {"id": job["id"], "deleted": True}
    assert not fayl.exists()
    assert root_client.get(f"{BACKUPS}/jobs/{job['id']}").status_code == 404
    assert all(j["id"] != job["id"] for j in root_client.get(BACKUPS).json()["jobs"])
    sled = _v_zhurnale(root_client, "backup.deleted")
    assert sled and sled[0]["entity_label"] == "db"
    assert root_client.delete(f"{BACKUPS}/jobs/{job['id']}").status_code == 404


def test_idushchuyu_kopiyu_udalit_nelzya(root_client, sayt):
    """Файл ещё пишется: удаление из-под потока оставило бы огрызок без записи."""
    _zavesti_klyuch(root_client)
    job = backup_service._novaya("db", SimpleNamespace(name="root", email="root@example.test"))
    backup_service._zapisat(job)
    try:
        r = root_client.delete(f"{BACKUPS}/jobs/{job['id']}")
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "backup_busy"
    finally:
        backup_service._osvobodit()
        backup_service._put_raboty(job["id"]).unlink(missing_ok=True)


# --- ключ --------------------------------------------------------------------


def test_bez_prava_ekrana_net(manager_client, sayt):
    assert manager_client.get(BACKUPS).status_code == 403
    assert manager_client.post(f"{BACKUPS}/db").status_code == 403


def test_kopiya_ne_snimaetsya_bez_podtverzhdyonnogo_klyucha(root_client, sayt):
    """Ключ действует только после ввода его хвоста обратно.

    Черновик ключа, который никто не подтвердил, не должен шифровать копии:
    владелец его не сохранил, и такая копия не откроется никогда.
    """
    assert root_client.get(BACKUPS).json()["key"] == {
        "exists": False, "pending": False, "created_at": None, "fragment_length": backup_service.FRAGMENT,
    }
    r = root_client.post(f"{BACKUPS}/db")
    assert r.status_code == 409 and r.json()["error"]["code"] == "backup_key_missing"

    r = root_client.post(f"{BACKUPS}/key")
    assert r.status_code == 200
    klyuch = r.json()["key"]
    assert len(klyuch) == 64 and bytes.fromhex(klyuch)
    assert root_client.get(BACKUPS).json()["key"]["pending"] is True
    assert root_client.post(f"{BACKUPS}/db").status_code == 409, "черновик ключа не должен считаться ключом"

    r = root_client.post(f"{BACKUPS}/key/confirm", json={"fragment": "00000000"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "backup_key_fragment_mismatch"

    r = root_client.post(f"{BACKUPS}/key/confirm", json={"fragment": klyuch[-backup_service.FRAGMENT:].upper()})
    assert r.status_code == 200 and r.json()["exists"] is True
    assert verify_backup.prochitat_klyuch(sayt / "data" / "backups" / "sayt" / "klyuch").hex() == klyuch
    assert _v_zhurnale(root_client, "backup.key_created"), "заведение ключа не попало в журнал"

    r = root_client.post(f"{BACKUPS}/key")
    assert r.status_code == 409 and r.json()["error"]["code"] == "backup_key_exists"


# --- снятие и скачивание -----------------------------------------------------


def test_kopiya_bazy_snimaetsya_shifruetsya_i_skachivaetsya(root_client, sayt):
    klyuch = _zavesti_klyuch(root_client)
    job = _snyat(root_client, "db")
    assert job["filename"].endswith(".sql.enc") and job["tables"] > 0 and job["rows"] > 0
    assert job["check"]["ok"] is True, job["check"]
    from database.session import engine

    assert job["revision"] == snapshot_db.revizia(engine)

    katalog = sayt / "data" / "backups" / "sayt"
    assert not list(katalog.glob("*.sql")), "расшифрованный дамп не должен оставаться на диске"
    assert _v_zhurnale(root_client, "backup.taken")

    r = root_client.get(f"{BACKUPS}/jobs/{job['id']}/file")
    assert r.status_code == 200
    assert job["filename"] in r.headers["content-disposition"]
    skachano = sayt / "skachano.enc"
    skachano.write_bytes(r.content)
    assert verify_backup.zashifrovana(skachano)
    otchyot = verify_backup.verify(skachano, None, None, klyuch=bytes.fromhex(klyuch))
    assert otchyot["ok"], otchyot["problems"]
    assert (sayt / "data" / "backups" / "last-export").is_file(), "увоз копии не отмечен"
    assert backup_service.rabota(job["id"])["downloaded_at"]
    assert _v_zhurnale(root_client, "backup.downloaded")

    sostoyanie = root_client.get(BACKUPS).json()
    assert sostoyanie["last_check"]["ok"] is True
    assert sostoyanie["jobs"][0]["id"] == job["id"]


def test_snyatie_kopii_ne_portit_uroven_izolyatsii_pula(root_client, sayt):
    """Дамп идёт под REPEATABLE READ, и это не должно пережить возврат соединения в пул.

    Поймано полным прогоном 04.09.2026: после снятия копии из приложения дуэль
    «двое увозят последнее» пропускала обоих — соединение вернулось в пул с
    чужим уровнем изоляции, и пересчёт после замка читал старый снимок.
    """
    from sqlalchemy import text

    from database.session import engine

    _zavesti_klyuch(root_client)
    _snyat(root_client, "db")
    # Несколько заходов подряд: пул отдаёт соединения по очереди, и заражённое
    # обязано попасться. Уровень по умолчанию у движка — READ COMMITTED.
    for _ in range(8):
        with engine.connect() as c:
            uroven = c.scalar(text("SELECT @@transaction_isolation"))
            assert uroven == "READ-COMMITTED", uroven


def test_zamena_klyucha_vidna_proverkoy(root_client, sayt):
    """«Потерял ключ — потерял копию» обнаруживается кнопкой, а не в день аварии."""
    _zavesti_klyuch(root_client)
    job = _snyat(root_client, "db")
    r = root_client.post(f"{BACKUPS}/jobs/{job['id']}/check")
    assert r.status_code == 200 and r.json()["check"]["ok"] is True

    _zavesti_klyuch(root_client, replace=True)
    r = root_client.post(f"{BACKUPS}/jobs/{job['id']}/check")
    assert r.status_code == 200
    assert r.json()["check"]["ok"] is False, "копия старым ключом не должна открываться новым"
    assert root_client.get(BACKUPS).json()["last_check"]["ok"] is False


def test_vtoraya_rabota_ne_nachinaetsya_poka_idyot_pervaya(root_client, sayt):
    _zavesti_klyuch(root_client)
    assert backup_service._zanyat("deadbeefdeadbeef")
    try:
        r = root_client.post(f"{BACKUPS}/db")
        assert r.status_code == 409 and r.json()["error"]["code"] == "backup_busy"
    finally:
        backup_service._osvobodit()


def test_kopiya_faylov_eto_tot_zhe_arhiv_chto_u_backup_sh(root_client, sayt):
    from config.settings import get_settings

    klyuch = _zavesti_klyuch(root_client)
    (get_settings().storage_dir / "media").mkdir()
    (get_settings().storage_dir / "media" / "foto.webp").write_bytes(b"RIFF....WEBP")
    job = _snyat(root_client, "storage")
    assert job["files"] == 1 and job["filename"].endswith(".tar.gz.enc")

    r = root_client.get(f"{BACKUPS}/jobs/{job['id']}/file")
    enc = sayt / "storage.enc"
    enc.write_bytes(r.content)
    tgz = sayt / "storage.tar.gz"
    verify_backup.rasshifrovat(enc, tgz, klyuch=bytes.fromhex(klyuch))
    with tarfile.open(tgz) as tar:
        assert "./media/foto.webp" in tar.getnames()


def test_staraya_kopiya_ubiraetsya_s_diska(root_client, sayt):
    """Готовая копия — это вся система в файле на том же диске; сутки и не больше."""
    import os

    _zavesti_klyuch(root_client)
    job = _snyat(root_client, "db")
    put = sayt / "data" / "backups" / "sayt" / f"{job['id']}.enc"
    assert put.is_file()
    davno = time.time() - backup_service.HRANIT_SEKUND - 60
    os.utime(put, (davno, davno))
    root_client.get(BACKUPS)
    assert not put.exists()
    r = root_client.get(f"{BACKUPS}/jobs/{job['id']}/file")
    assert r.status_code == 404 and r.json()["error"]["code"] == "backup_gone"


# --- восстановление ----------------------------------------------------------


def _zalit(client, kind: str, put: Path):
    with put.open("rb") as f:
        return client.post(f"{BACKUPS}/restore", data={"kind": kind}, files={"file": (put.name, f, "application/octet-stream")})


def _zashifrovat(sayt: Path, klyuch: str, istochnik: Path, imya: str) -> Path:
    cel = sayt / imya
    verify_backup.zashifrovat(istochnik, cel, klyuch=bytes.fromhex(klyuch))
    return cel


def test_vosstanovlenie_tolko_pod_svoim_pravom(root_client, manager_client, sayt):
    _zavesti_klyuch(root_client)
    musor = sayt / "musor.enc"
    musor.write_bytes(b"not a copy")
    r = _zalit(manager_client, "db", musor)
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    assert "backups.manage" in r.json()["error"]["message"]


def test_vosstanovlenie_otkazyvaet_do_togo_kak_tronut_bazu(root_client, sayt):
    """Четыре отказа, и ни один не трогает базу и не включает обслуживание."""
    from database.session import engine

    klyuch = _zavesti_klyuch(root_client)
    chuzhoy = verify_backup.porodit_klyuch()

    musor = sayt / "musor.enc"
    musor.write_bytes(b"definitely not an encrypted copy")
    r = _zalit(root_client, "db", musor)
    assert r.status_code == 422 and r.json()["error"]["code"] == "backup_not_encrypted", r.text

    damp = sayt / "damp.sql"
    snapshot_db.snyat(engine, damp)
    chuzhim = sayt / "chuzhim.enc"
    verify_backup.zashifrovat(damp, chuzhim, klyuch=bytes.fromhex(chuzhoy))
    r = _zalit(root_client, "db", chuzhim)
    assert r.status_code == 422 and r.json()["error"]["code"] == "backup_bad_key", r.text

    obryvok = sayt / "obryvok.sql"
    obryvok.write_text(damp.read_text(encoding="utf-8").rsplit(snapshot_db.METKA, 1)[0], encoding="utf-8")
    r = _zalit(root_client, "db", _zashifrovat(sayt, klyuch, obryvok, "obryvok.enc"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "backup_truncated", r.text

    chuzhaya_reviziya = sayt / "reviziya.sql"
    text = damp.read_text(encoding="utf-8")
    assert "INSERT INTO `alembic_version`" in text
    chuzhaya_reviziya.write_text(
        text.replace("INSERT INTO `alembic_version` (`version_num`) VALUES\n('", "INSERT INTO `alembic_version` (`version_num`) VALUES\n('ffff", 1),
        encoding="utf-8",
    )
    r = _zalit(root_client, "db", _zashifrovat(sayt, klyuch, chuzhaya_reviziya, "reviziya.enc"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "backup_unknown_revision", r.text

    assert root_client.get(f"{API}/settings/maintenance").json()["enabled"] is False
    assert not (sayt / "data" / "backups" / "sayt" / "zanyato").exists(), "замок остался после отказа"
    assert not _v_zhurnale(root_client, "backup.restored")


def test_vosstanovlenie_vozvrashchaet_bazu_k_snyatomu(root_client, sayt):
    """Круг целиком: снял → изменил базу → залил копию → изменения пропали.

    Ровно тот случай, который кнопка и закрывает: «испортили данные». Снимок
    живой базы до заливки лежит рядом, обслуживание снято, в журнале след.
    """
    _zavesti_klyuch(root_client)
    kopiya = _snyat(root_client, "db")
    r = root_client.get(f"{BACKUPS}/jobs/{kopiya['id']}/file")
    fayl = sayt / kopiya["filename"]
    fayl.write_bytes(r.content)

    posle = root_client.post(f"{API}/clients", json={"name": "Появился после копии", "phone": "+79990000001"})
    assert posle.status_code == 201, posle.text
    client_id = posle.json()["id"]
    assert root_client.get(f"{API}/clients/{client_id}").status_code == 200

    r = _zalit(root_client, "db", fayl)
    assert r.status_code == 200, r.text
    assert root_client.get(f"{API}/settings/maintenance").json()["enabled"] is True, "на время заливки сайт закрыт"
    job = _dozhdatsya(r.json()["id"])
    assert job["status"] == "done", job
    assert job["tables"] == kopiya["tables"] and job["rows"] == kopiya["rows"]
    assert job["snapshot"].startswith("db-before-restore-")
    snimok = sayt / "data" / "backups" / "sayt" / job["snapshot"]
    assert snapshot_db.celaya(snimok), "снимок живой базы перед заливкой обязан быть целым"
    assert "Появился после копии" in snimok.read_text(encoding="utf-8")

    assert root_client.get(f"{API}/clients/{client_id}").status_code == 404, "клиент, заведённый после копии, обязан пропасть"
    assert root_client.get(f"{API}/settings/maintenance").json()["enabled"] is False
    assert root_client.get(f"{API}/system/schema").json()["ok"] is True
    zapisi = _v_zhurnale(root_client, "backup.restored")
    assert zapisi and f"revision {kopiya['revision']}" in (zapisi[0]["value_after"] or "")


def test_fayly_iz_kopii_lozhatsya_poverkh_nyneshnikh(root_client, sayt):
    from config.settings import get_settings

    klyuch = _zavesti_klyuch(root_client)
    storage = get_settings().storage_dir
    tgz = sayt / "storage.tar.gz"
    with tarfile.open(tgz, "w:gz") as tar:
        dannye = b"from the copy"
        info = tarfile.TarInfo("./media/iz_kopii.webp")
        info.size = len(dannye)
        tar.addfile(info, io.BytesIO(dannye))
        zloy = tarfile.TarInfo("../vne_hranilishcha.txt")
        zloy.size = 1
        tar.addfile(zloy, io.BytesIO(b"x"))
    (storage / "svoy.txt").write_text("stays", encoding="utf-8")

    r = _zalit(root_client, "storage", _zashifrovat(sayt, klyuch, tgz, "storage.enc"))
    assert r.status_code == 200, r.text
    job = _dozhdatsya(r.json()["id"])
    assert job["status"] == "failed", "путь наружу хранилища обязан остановить распаковку"
    assert not (sayt / "vne_hranilishcha.txt").exists()
    assert (storage / "svoy.txt").read_text(encoding="utf-8") == "stays"

    with tarfile.open(tgz, "w:gz") as tar:
        dannye = b"from the copy"
        info = tarfile.TarInfo("./media/iz_kopii.webp")
        info.size = len(dannye)
        tar.addfile(info, io.BytesIO(dannye))
    r = _zalit(root_client, "storage", _zashifrovat(sayt, klyuch, tgz, "storage2.enc"))
    assert r.status_code == 200, r.text
    job = _dozhdatsya(r.json()["id"])
    assert job["status"] == "done" and job["files"] == 1, job
    assert (storage / "media" / "iz_kopii.webp").read_bytes() == b"from the copy"
    assert (storage / "svoy.txt").exists()


def test_itog_raboty_oznachaet_svobodnyy_zamok(sayt):
    """«done»/«failed» видны снаружи только когда замок уже отпущен: иначе
    следующая работа, заведённая сразу после ожидания итога, ловит
    `backup_busy` (плавало в CI 06.09.2026)."""
    job = backup_service._novaya("db", SimpleNamespace(name="root", email="root@example.test"))
    zamok = backup_service.katalog() / backup_service.ZANYATO
    assert zamok.exists(), "замок не взят"
    try:
        backup_service._zavershit(job, "failed", "для проверки")
        assert not zamok.exists(), "итог записан, а замок ещё держится"
        assert backup_service.rabota(job["id"])["status"] == "failed"
    finally:
        backup_service._osvobodit()
        backup_service._put_raboty(job["id"]).unlink(missing_ok=True)
