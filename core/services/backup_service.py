"""Копия системы с экрана настроек: ключ, снятие, скачивание, восстановление.

Устройство и решения владельца — `docs/15-backup-encryption.md` §10. Коротко:

- копия всегда зашифрована ключом, который система завела сама и хранит в
  `data/backups/sayt/klyuch`; ключ показывается один раз и подтверждается
  вводом его хвоста обратно;
- снятие и восстановление идут в потоке, а не в запросе: дамп боевой базы —
  минуты, и ответ, который живёт дольше `proxy_read_timeout`, nginx обрывает;
- состояние работы лежит файлом рядом с копией, чтобы его видел любой рабочий
  процесс, а не только тот, что работу начал;
- одна работа за раз: замок — файл, созданный с `O_EXCL`.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from config.settings import BASE_DIR, get_settings
from core import exceptions as errors
from core.services import audit_service, maintenance_mode, modules_service
from database import schema_check
from database.models import User
from database.models.audit import SOURCE_MANUAL
from database.repositories import backups as backups_repo
from database.repositories import users as users_repo
from database.session import SessionLocal, engine
from scripts import snapshot_db, verify_backup

KLYUCH = "klyuch"
KLYUCH_CHERNOVIK = "klyuch.chernovik"
#: Сколько знаков ключа владелец вводит обратно в подтверждение «я его сохранил».
FRAGMENT = 8

ZANYATO = "zanyato"
#: Работа, не отчитавшаяся за два часа, считается брошенной: процесс убили
#: посреди дампа, и замок никто не снял.
USTAREL_SEKUND = 2 * 3600
#: Готовая копия лежит сутки — на случай, если скачивание сорвалось. Дольше
#: нельзя: это вся система в одном файле на том же диске.
HRANIT_SEKUND = 24 * 3600
#: Снимок живой базы перед восстановлением — неделя, как у ежедневных копий.
HRANIT_SNIMOK_SEKUND = 7 * 24 * 3600
POKAZYVAT_RABOT = 10

VIDY = ("db", "storage")

_REVIZIYA = re.compile(r"^INSERT INTO `alembic_version` \(`version_num`\) VALUES\s*\('([0-9a-f]+)'\)", re.M)
_SNYATO = re.compile(r"^-- база \S+, снято (\S+ \S+)$", re.M)
_ITOG = re.compile(rf"^{re.escape(snapshot_db.METKA)}: таблиц (\d+), строк (\d+)", re.M)


def _seychas() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _shtamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def katalog() -> Path:
    """`data/backups/sayt`: тот же том, что у ежедневных копий (`scripts/backup.sh`)."""
    koren = os.environ.get("OPENCRM_DATA_DIR")
    put = (Path(koren) if koren else BASE_DIR / "data") / "backups" / "sayt"
    put.mkdir(parents=True, exist_ok=True)
    return put


# --- ключ --------------------------------------------------------------------


def klyuch_sostoyanie() -> dict:
    k = katalog()
    gotov = k / KLYUCH
    return {
        "exists": gotov.is_file(),
        "pending": (k / KLYUCH_CHERNOVIK).is_file(),
        "created_at": (
            datetime.fromtimestamp(gotov.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            if gotov.is_file()
            else None
        ),
        "fragment_length": FRAGMENT,
    }


def zavesti_klyuch(*, replace: bool = False) -> dict:
    """Породить ключ. Возвращается ОДИН раз и нигде больше не показывается."""
    k = katalog()
    if (k / KLYUCH).is_file() and not replace:
        raise errors.ConflictError("Backup key already exists", code="backup_key_exists")
    klyuch = verify_backup.zapisat_klyuch(k / KLYUCH_CHERNOVIK)
    return {"key": klyuch, "fragment_length": FRAGMENT}


def podtverdit_klyuch(db: Session, actor: User, fragment: str) -> dict:
    """Хвост ключа сошёлся — черновик становится ключом. Старый, если был, забыт."""
    k = katalog()
    chernovik = k / KLYUCH_CHERNOVIK
    if not chernovik.is_file():
        raise errors.NotFoundError("No pending backup key", code="backup_key_not_pending")
    klyuch = verify_backup.prochitat_klyuch(chernovik).hex()
    if not hmac.compare_digest(klyuch[-FRAGMENT:], (fragment or "").strip().lower()):
        raise errors.ValidationError("Key fragment does not match", code="backup_key_fragment_mismatch")
    zamena = (k / KLYUCH).is_file()
    os.replace(chernovik, k / KLYUCH)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_BACKUP_KEY_CREATED,
        entity_type=audit_service.ENTITY_BACKUP,
        entity_label="key",
        after="replaced" if zamena else "created",
    )
    return klyuch_sostoyanie()


def _klyuch() -> bytes:
    put = katalog() / KLYUCH
    if not put.is_file():
        raise errors.ConflictError("Create the backup key first", code="backup_key_missing")
    return verify_backup.prochitat_klyuch(put)


# --- работы ------------------------------------------------------------------


def _put_raboty(job_id: str) -> Path:
    return katalog() / f"{job_id}.json"


def _zapisat(job: dict) -> None:
    put = _put_raboty(job["id"])
    chernovik = put.with_suffix(f".{uuid4().hex[:6]}.tmp")
    chernovik.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    # Windows не даёт подменить файл, который в эту миллисекунду читает опрос
    # экрана; на Linux замена атомарна и с первого раза.
    for popytka in range(10):
        try:
            os.replace(chernovik, put)
            return
        except PermissionError:
            if popytka == 9:
                raise
            time.sleep(0.05)


def rabota(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{16}", job_id or ""):
        raise errors.NotFoundError("Backup job not found", code="backup_job_not_found")
    put = _put_raboty(job_id)
    if not put.is_file():
        raise errors.NotFoundError("Backup job not found", code="backup_job_not_found")
    return json.loads(put.read_text(encoding="utf-8"))


def _zanyat(job_id: str) -> bool:
    zamok = katalog() / ZANYATO
    for _ in range(2):
        try:
            fd = os.open(zamok, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - zamok.stat().st_mtime < USTAREL_SEKUND:
                    return False
            except FileNotFoundError:
                continue
            zamok.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(job_id)
        return True
    return False


def _osvobodit(job_id: str | None = None) -> None:
    """Отпустить замок. С номером работы — только свой: `finally` потока
    добегает уже после итога, и без сверки стирал бы замок следующей работы,
    взятый в это окно (CI 06.09.2026). Без номера — безусловно, для уборки."""
    zamok = katalog() / ZANYATO
    if job_id is not None:
        try:
            if zamok.read_text(encoding="utf-8").strip() != job_id:
                return
        except FileNotFoundError:
            return
    zamok.unlink(missing_ok=True)


def _ubrat_staroe() -> None:
    """Готовые копии — сутки, снимки перед восстановлением — неделя.

    Только по возрасту, без «огрызки — сразу»: черновик дампа, который поток
    пишет в эту секунду, ничем не отличается от брошенного, а экран опрашивает
    состояние каждые три секунды.
    """
    seychas = time.time()
    for put in katalog().iterdir():
        if put.name in (KLYUCH, KLYUCH_CHERNOVIK, ZANYATO, "last-check.json"):
            continue
        try:
            vozrast = seychas - put.stat().st_mtime
        except FileNotFoundError:
            continue
        predel = HRANIT_SNIMOK_SEKUND if put.name.startswith("db-before-restore-") else HRANIT_SEKUND
        if vozrast > predel:
            put.unlink(missing_ok=True)


def sostoyanie() -> dict:
    _ubrat_staroe()
    raboty = []
    for put in katalog().glob("*.json"):
        if put.name == "last-check.json":
            continue
        try:
            raboty.append(json.loads(put.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    raboty.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    proverka = katalog() / "last-check.json"
    return {
        "key": klyuch_sostoyanie(),
        "jobs": raboty[:POKAZYVAT_RABOT],
        "busy": (katalog() / ZANYATO).is_file(),
        "last_check": (
            json.loads(proverka.read_text(encoding="utf-8")) if proverka.is_file() else None
        ),
    }


def _novaya(kind: str, actor: User) -> dict:
    job = {
        "id": uuid4().hex[:16],
        "kind": kind,
        "status": "running",
        "started_at": _seychas(),
        "finished_at": None,
        "actor": actor.name or actor.email,
        "error": None,
    }
    if not _zanyat(job["id"]):
        raise errors.ConflictError("Another backup job is running", code="backup_busy")
    return job


def _zavershit(job: dict, status: str, error: str | None = None) -> None:
    """Итог работы. Замок отпускается ДО записи итога: тот, кто дождался
    «done»/«failed» и тут же завёл следующую работу, получал `backup_busy` —
    замок ещё держал `finally` потока (CI, 06.09.2026)."""
    _osvobodit(job["id"])
    job["status"] = status
    job["error"] = error
    job["finished_at"] = _seychas()
    _zapisat(job)


def _v_zhurnal(actor_id: int, action: str, label: str, after: str) -> None:
    with SessionLocal() as db:
        audit_service.record(
            db,
            actor=users_repo.get_by_id(db, actor_id),
            source=SOURCE_MANUAL,
            action=action,
            entity_type=audit_service.ENTITY_BACKUP,
            entity_label=label,
            after=after,
        )
        db.commit()


# --- снятие ------------------------------------------------------------------


def snyat(actor: User, kind: str) -> dict:
    """Начать снятие копии. Ответ — работа, за которой следят по `rabota()`."""
    if kind not in VIDY:
        raise errors.ValidationError("Unknown backup kind", code="backup_bad_kind")
    _klyuch()
    job = _novaya(kind, actor)
    _zapisat(job)
    threading.Thread(target=_snyatie, args=(job, actor.id), daemon=True, name=f"backup-{job['id']}").start()
    return job


def _arhiv_storage(cel: Path) -> int:
    """Тот же архив, что снимает `scripts/backup.sh`: `tar -C storage .`."""
    storage = get_settings().storage_dir
    storage.mkdir(parents=True, exist_ok=True)
    with tarfile.open(cel, "w:gz") as tar:
        tar.add(storage, arcname=".")
    return sum(1 for p in storage.rglob("*") if p.is_file())


def _zapisat_proverku(proverka: dict) -> None:
    put = katalog() / "last-check.json"
    chernovik = put.with_suffix(".tmp")
    chernovik.write_text(json.dumps(proverka, ensure_ascii=False), encoding="utf-8")
    os.replace(chernovik, put)


def _proverit_fayl(cel: Path, kind: str, klyuch: bytes) -> dict:
    """Копия открывается ЭТИМ ключом и годна внутри. Иначе она не копия."""
    if kind == "db":
        otchyot = verify_backup.verify(cel, None, None, klyuch=klyuch)
        problems = list(otchyot["problems"])
        ok = bool(otchyot["ok"])
    else:
        verify_backup.sverit_metku(cel, klyuch=klyuch)
        problems, ok = [], True
    proverka = {"ok": ok, "problems": problems, "kind": kind, "checked_at": _seychas()}
    _zapisat_proverku(proverka)
    if not ok:
        raise RuntimeError("the copy failed verification: " + "; ".join(problems))
    return proverka


def _snyatie(job: dict, actor_id: int) -> None:
    k = katalog()
    syroy = k / f"{job['id']}.{'sql' if job['kind'] == 'db' else 'tar.gz'}"
    try:
        klyuch = _klyuch()
        if job["kind"] == "db":
            job["tables"], job["rows"] = snapshot_db.snyat(engine, syroy)
            job["revision"] = snapshot_db.revizia(engine)
            job["filename"] = f"opencrm-db-{_shtamp()}.sql.enc"
        else:
            job["files"] = _arhiv_storage(syroy)
            job["filename"] = f"opencrm-storage-{_shtamp()}.tar.gz.enc"
        cel = k / f"{job['id']}.enc"
        verify_backup.zashifrovat(syroy, cel, klyuch=klyuch)
        syroy.unlink(missing_ok=True)
        job["check"] = _proverit_fayl(cel, job["kind"], klyuch)
        job["size"] = cel.stat().st_size
        _zavershit(job, "done")
        _v_zhurnal(
            actor_id,
            audit_service.ACTION_BACKUP_TAKEN,
            job["kind"],
            f"{job['filename']}, {job['size']} bytes",
        )
    except Exception as beda:  # noqa: BLE001 — любая беда должна лечь в отчёт, а не в лог потока
        _zavershit(job, "failed", str(beda)[:500])
    finally:
        syroy.unlink(missing_ok=True)
        _osvobodit(job["id"])


# --- скачивание и проверка ---------------------------------------------------


def fayl_dlya_skachivaniya(db: Session, actor: User, job_id: str) -> tuple[Path, str]:
    job = rabota(job_id)
    if job.get("status") != "done" or job["kind"] not in VIDY:
        raise errors.NotFoundError("The copy is not ready", code="backup_not_ready")
    put = katalog() / f"{job_id}.enc"
    if not put.is_file():
        raise errors.NotFoundError("The copy has already been removed", code="backup_gone")
    job["downloaded_at"] = _seychas()
    _zapisat(job)
    # Отметка увоза — та же, что у ежедневных копий: пока копию не забирали,
    # `scripts/backup.sh` предупреждает об этом.
    (katalog().parent / "last-export").write_text(job["downloaded_at"] + "\n", encoding="utf-8")
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_BACKUP_DOWNLOADED,
        entity_type=audit_service.ENTITY_BACKUP,
        entity_label=job["kind"],
        after=job["filename"],
    )
    return put, job["filename"]


def udalit(db: Session, actor: User, job_id: str) -> dict:
    """Убрать копию с сервера раньше срока. Идущую — нельзя: файл ещё пишется."""
    job = rabota(job_id)
    if job.get("status") == "running":
        raise errors.ConflictError("The copy is still being taken", code="backup_busy")
    (katalog() / f"{job_id}.enc").unlink(missing_ok=True)
    _put_raboty(job_id).unlink(missing_ok=True)
    audit_service.record(
        db,
        actor=actor,
        source=SOURCE_MANUAL,
        action=audit_service.ACTION_BACKUP_DELETED,
        entity_type=audit_service.ENTITY_BACKUP,
        entity_label=job["kind"],
        after=job.get("filename") or job_id,
    )
    return {"id": job_id, "deleted": True}


def proverit(job_id: str) -> dict:
    """Ещё раз открыть готовую копию нынешним ключом. Ловит потерянный ключ."""
    job = rabota(job_id)
    put = katalog() / f"{job_id}.enc"
    if job.get("status") != "done" or not put.is_file():
        raise errors.NotFoundError("The copy is not ready", code="backup_not_ready")
    try:
        job["check"] = _proverit_fayl(put, job["kind"], _klyuch())
    except (verify_backup.NeTotKlyuch, verify_backup.NeTaKopiya, RuntimeError) as beda:
        job["check"] = {"ok": False, "problems": [str(beda)], "kind": job["kind"], "checked_at": _seychas()}
        _zapisat_proverku(job["check"])
    _zapisat(job)
    return job


# --- восстановление ----------------------------------------------------------


def _reviziya_dampa(damp: Path) -> str:
    text = damp.read_text(encoding="utf-8", errors="replace")
    naydeno = _REVIZIYA.search(text)
    return naydeno.group(1) if naydeno else "none"


def _snyato(damp: Path) -> str | None:
    with damp.open(encoding="utf-8", errors="replace") as f:
        shapka = f.read(4096)
    naydeno = _SNYATO.search(shapka)
    return naydeno.group(1) if naydeno else None


def _itog_dampa(damp: Path) -> tuple[int, int]:
    with damp.open("rb") as f:
        f.seek(max(0, damp.stat().st_size - 512))
        hvost = f.read().decode("utf-8", "replace")
    naydeno = _ITOG.search(hvost)
    return (int(naydeno.group(1)), int(naydeno.group(2))) if naydeno else (0, 0)


def _alembic(url: str):
    from alembic.config import Config

    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _proverit_reviziyu(reviziya: str) -> None:
    """Копия от кода, которого здесь нет, не заливается: догнать её нечем."""
    from alembic.script import ScriptDirectory

    if reviziya == "none":
        raise errors.ValidationError(
            "The copy carries no migration revision", code="backup_unknown_revision"
        )
    script = ScriptDirectory.from_config(_alembic(engine.url.render_as_string(hide_password=False)))
    try:
        script.get_revision(reviziya)
    except Exception:  # noqa: BLE001 — alembic отвечает своим классом на «нет такой»
        raise errors.ValidationError(
            f"The copy was taken by an unknown code revision {reviziya}",
            code="backup_unknown_revision",
        ) from None


def vosstanovit(db: Session, actor: User, kind: str, zagruzka: Path) -> dict:
    """Проверить копию и начать восстановление. Порядок — docs/15 §6."""
    if kind not in VIDY:
        raise errors.ValidationError("Unknown backup kind", code="backup_bad_kind")
    klyuch = _klyuch()
    job = _novaya(f"restore-{kind}", actor)
    k = katalog()
    syroy = k / f"{job['id']}.{'sql' if kind == 'db' else 'tar.gz'}"
    try:
        if not verify_backup.zashifrovana(zagruzka):
            raise errors.ValidationError(
                "The file is not an encrypted OpenCRM copy", code="backup_not_encrypted"
            )
        try:
            verify_backup.rasshifrovat(zagruzka, syroy, klyuch=klyuch)
        except verify_backup.NeTotKlyuch:
            raise errors.ValidationError(
                "The current backup key does not open this copy", code="backup_bad_key"
            ) from None
        except verify_backup.NeTaKopiya as beda:
            raise errors.ValidationError(str(beda), code="backup_not_encrypted") from None
        if kind == "db":
            if not snapshot_db.celaya(syroy):
                raise errors.ValidationError(
                    "The copy is truncated: no end marker", code="backup_truncated"
                )
            job["revision"] = _reviziya_dampa(syroy)
            _proverit_reviziyu(job["revision"])
            job["copy_taken_at"] = _snyato(syroy)
            job["tables"], job["rows"] = _itog_dampa(syroy)
            maintenance_mode.set_mode(db, True, "Restoring a database copy", actor.name or actor.email)
            # Фиксация ДО заливки: `DROP TABLE` ждёт метаданных у всякой открытой
            # транзакции, включая нашу же, — и повис бы на самом себе.
            db.commit()
        else:
            if not tarfile.is_tarfile(syroy):
                raise errors.ValidationError("The copy is not a storage archive", code="backup_not_archive")
        _zapisat(job)
    except Exception:
        syroy.unlink(missing_ok=True)
        _osvobodit(job["id"])
        raise
    threading.Thread(
        target=_vosstanovlenie, args=(job, actor.id, syroy), daemon=True, name=f"restore-{job['id']}"
    ).start()
    return job


def _dognat_migratsii(url: str) -> None:
    from alembic import command

    command.upgrade(_alembic(url), "head")


def _raspakovat(arhiv: Path) -> int:
    """Файлы кладутся ПОВЕРХ нынешних, ничего не стирая: копия только добавляет."""
    storage = get_settings().storage_dir
    storage.mkdir(parents=True, exist_ok=True)
    with tarfile.open(arhiv, "r:gz") as tar:
        chleny = [m for m in tar.getmembers() if m.isfile()]
        # Весь архив проверяется ДО первого файла: путь наружу хранилища,
        # найденный на середине, оставил бы половину копии разложенной.
        for chlen in chleny:
            tarfile.data_filter(chlen, str(storage))
        tar.extractall(storage, members=chleny, filter="data")
    return len(chleny)


def _vosstanovlenie(job: dict, actor_id: int, syroy: Path) -> None:
    baza = job["kind"] == "restore-db"
    try:
        if baza:
            snimok = katalog() / f"db-before-restore-{_shtamp()}.sql"
            snapshot_db.snyat(engine, snimok)
            job["snapshot"] = snimok.name
            url = engine.url.render_as_string(hide_password=False)
            with schema_check.zamok_shemy(engine):
                backups_repo.zalit_damp(url, syroy)
                _dognat_migratsii(url)
            otchyot = schema_check.check(engine)
            if not otchyot.ok:
                raise RuntimeError("schema mismatch after restore: " + otchyot.summary())
            maintenance_mode.invalidate()
            modules_service.invalidate()
            with SessionLocal() as db:
                maintenance_mode.set_mode(db, False, "", "")
                db.commit()
            itog = f"revision {job['revision']}, tables {job['tables']}, rows {job['rows']}"
        else:
            job["files"] = _raspakovat(syroy)
            itog = f"files {job['files']}"
        _zavershit(job, "done")
        _v_zhurnal(actor_id, audit_service.ACTION_BACKUP_RESTORED, job["kind"][len("restore-"):], itog)
    except Exception as beda:  # noqa: BLE001 — беда обязана лечь в отчёт вместе с именем снимка
        podskazka = (
            f" Maintenance mode is left on; the pre-restore snapshot is {job.get('snapshot')}."
            if baza
            else ""
        )
        _zavershit(job, "failed", (str(beda)[:400] + podskazka))
    finally:
        syroy.unlink(missing_ok=True)
        _osvobodit(job["id"])
