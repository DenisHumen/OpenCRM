"""Почтовый сервер: черновик, SSH, проверка, установка с откатом, подключение, удаление
(docs/bloki/32-pochtovyy-server.md).

Сети нет: SSH подменён `FakeKlient` (машина в словаре: authorized_keys, заливки,
установщик с заготовленным протоколом, агент в контейнере), DNS — словарём. Работы
идут сразу, в потоке запроса (`mail_install.set_runner`). Настоящий установщик и
агент проверены вживую на стенде — здесь то, что легко сломать незаметно: разбор
протокола, откат в статусах, пароль первого ящика один раз, ключи и пароли мимо
ответов, близнецы имён файлов просьбы обновлятору.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from core.pochta import agent
from core.services import mail_dns, mail_host, mail_install, mail_spool, mail_ssh
from database.models import MailAccount, MailJob, MailServer
from database.models.mail_server import JOB_RUNNING, MODE_SSH, STATUS_READY
from database.session import SessionLocal
from deploy import pochta as deploy_pochta
from tests.conftest import API
from tests.test_mail import mail_on  # noqa: F401 — фикстура
from tests.test_roles import role_maker, staff_maker  # noqa: F401 — фикстуры

SERVER = f"{API}/mail/server"
KOREN = Path(__file__).resolve().parent.parent
DOMEN = "alfa-a.test"
HOST = f"mail.{DOMEN}"

# Протокол, снятый с живой установки на стенде (09.10.2026), без сырого журнала.
UDACHA = """##STEP os
##OK os Ubuntu 24.04.4 LTS
##STEP privileges
##OK privileges root as root
##STEP docker
##OK docker Docker 29.7.0 via docker
##DATA docker_cmd docker
##STEP ports
##OK ports free: 25 465 587 993
##STEP egress25
##WARN egress25 egress25_blocked outbound port 25 is closed: mail to other servers needs a relay
##STEP existing
##DATA existing_container dmsprobe
##OK existing other mail containers do not serve port 25
##STEP public_ip
##DATA public_ip 203.0.113.7
##OK public_ip 203.0.113.7
pulling something
##STEP tls
##DATA tls_pin 65621d602ed52aaeb03e9ee026a373c00f0ed17bf66388761fd5e2237d38dc0c
##OK tls self-signed
##STEP dkim
##DATA dkim_selector mail
##DATA dkim_txt v=DKIM1; k=rsa; p=MIIBIjAN
##OK dkim selector mail
##STEP selfcheck
##OK selfcheck ports answer: 25 465 587 993
##END ok""".splitlines()

OTKAT = """##STEP os
##OK os Ubuntu 24.04.4 LTS
##STEP start
Error response from daemon: address already in use
##FAIL start port_busy a mail port became busy while starting
##STEP rollback
##ROLLBACK
##OK rollback everything done by this run is undone
##END rolled_back""".splitlines()


# --- подделки ---


class FakeConn:
    def __init__(self, mashina):
        self.m = mashina

    def vypolnit(self, komanda, *, stdin=None, timeout=60, na_stroku=None):
        m = self.m
        m.komandy.append(komanda)
        tekst = stdin.decode() if isinstance(stdin, bytes) else (stdin or "")
        if "authorized_keys" in komanda and "awk" in komanda:
            if m.chistka_lomaetsya:
                return 1, "awk: permission denied"
            return _po_nastoyashchemu(m, komanda, tekst)
        if "authorized_keys" in komanda:
            m.klyuchi.append(tekst.strip())
            return 0, ""
        if komanda.startswith('echo "uid='):
            return 0, f"uid={m.uid}\nhome=/home/u\ndocker={m.docker}\nsudo={m.sudo}\n"
        if komanda.startswith("sudo -S"):
            return (0, "") if tekst == "sudo-pass\n" else (1, "Sorry, try again.")
        if komanda.startswith('printf "%s" "$HOME"'):
            return 0, "/home/u"
        if "ustanovit.sh" in komanda:
            op = komanda.split()[-1]
            m.zapuski.append((op, dict(m.env)))
            for stroka in m.protokol[op]:
                na_stroku(stroka)
            return (0 if m.protokol[op][-1] == "##END ok" else 1), ""
        if " exec -i " in komanda:
            op = komanda.split()[-1]
            b64, _, agent_tekst = tekst.partition("\n")
            assert agent_tekst.startswith('"""Агент OpenCRM'), "агент обязан ехать текстом на stdin"
            assert " python3 -c " in komanda and b64 not in komanda, "аргументы — не в argv"
            m.agent_vyzovy.append((komanda.split(" exec -i ")[0], op))
            return 0, json.dumps(m.agent(op, json.loads(base64.b64decode(b64))))
        return 0, ""

    def zalit(self, put, dannye, rezhim=0o600):
        self.m.fayly[put] = dannye
        if put.endswith("ustanovka.env"):
            self.m.env = dict(s.split("=", 1) for s in dannye.decode().splitlines() if s)

    def zhivo(self):
        return True

    def zakryt(self):
        pass


class FakeKlient:
    """Машина по SSH: пускает по паролю `right` или по ключу из authorized_keys."""

    def __init__(self):
        _, otkrytyy = mail_ssh.sozdat_klyuch("host")
        self.host_line = " ".join(otkrytyy.split()[:2])
        self.klyuchi: list[str] = []
        self.komandy: list[str] = []
        self.fayly: dict[str, bytes] = {}
        self.env: dict[str, str] = {}
        self.zapuski: list[tuple[str, dict]] = []
        self.agent_vyzovy: list[tuple[str, str]] = []
        self.uid, self.docker, self.sudo = 1000, "noaccess", "password"
        self.protokol = {"check": UDACHA, "install": UDACHA, "remove": ["##STEP stop", "##OK stop done", "##END ok"]}
        self.yashchiki = {"boss@" + DOMEN: None}
        self.mastera: dict[str, str] = {}
        self.chistka_lomaetsya = False
        self.dkim_txt = "v=DKIM1; k=rsa; p=ADOPTED"

    def __call__(self):
        return self

    def klyuch_hosta(self, host, port, *, timeout=8):
        tip, telo = mail_ssh.razobrat_stroku_klyucha(self.host_line)
        return mail_ssh.KlyuchHosta(mail_ssh.otpechatok(telo), tip, self.host_line)

    def podklyuchit(self, host, port, user, *, host_key_line, password=None, private_key=None,
                    passphrase=None, timeout=10):
        if host_key_line != self.host_line:
            raise mail_ssh.SshBeda("ssh_host_key_changed", "changed")
        if private_key:
            if mail_ssh.otkrytyy_iz_zakrytogo(private_key) not in [" ".join(k.split()[:2]) for k in self.klyuchi]:
                raise mail_ssh.SshBeda("ssh_auth_failed", "key refused")
        elif password != "right":
            raise mail_ssh.SshBeda("ssh_auth_failed", "password refused")
        return FakeConn(self)

    def agent(self, op, args):
        if op == "ping":
            return {"ok": True, "data": {"agent": 1, "dms": "v16.0.1", "hostname": HOST}}
        if op == "master_ensure":
            self.mastera[args["user"]] = args["password"]
            return {"ok": True, "data": {}}
        if op == "health":
            return {"ok": True, "data": {"services": {"postfix": True}, "cert": {"self_signed": True, "sha256": "ab" * 32}, "queue": 0}}
        if op == "accounts":
            return {"ok": True, "data": {
                "accounts": [{"address": a, "quota_mb": q, "used_kb": 0} for a, q in self.yashchiki.items()],
                "aliases": [{"source": "info@" + DOMEN, "destination": "boss@" + DOMEN}],
                "domains": [DOMEN], "masters": list(self.mastera),
            }}
        if op == "dkim":
            return {"ok": True, "data": {"selector": "mail", "txt": self.dkim_txt}}
        return {"ok": False, "code": "mail_agent_failed", "message": f"unknown {op}"}


def _po_nastoyashchemu(m, komanda, tekst):
    """Уборку authorized_keys гоняем настоящим sh: ошибка в awk и кавычках здесь —
    это потерянный вход на машину, подделка бы её не заметила."""
    if shutil.which("sh") is None:
        pytest.skip("нужен sh")
    import tempfile

    with tempfile.TemporaryDirectory() as dom:
        (Path(dom) / ".ssh").mkdir()
        fayl = Path(dom) / ".ssh" / "authorized_keys"
        fayl.write_bytes(("".join(k + "\n" for k in m.klyuchi)).encode())
        r = subprocess.run(["sh", "-c", komanda], input=tekst.encode(), capture_output=True,
                           env={**os.environ, "HOME": dom}, timeout=30)
        m.klyuchi = [s for s in fayl.read_text().splitlines() if s.strip()]
    return r.returncode, (r.stdout + r.stderr).decode()


@pytest.fixture
def mashina(mail_on, monkeypatch, tmp_path):  # noqa: F811
    m = FakeKlient()
    mail_ssh.set_ssh_factory(m)
    mail_install.set_runner(lambda fn, _imya: fn())
    mail_dns.set_dns_resolver(lambda imya, tip: {
        (HOST, "A"): ["203.0.113.7"],
        (DOMEN, "MX"): [f"10 {HOST}"],
        (DOMEN, "TXT"): ["v=spf1 mx ~all"],
    }.get((imya, tip), []))
    monkeypatch.setenv("OPENCRM_DATA_DIR", str(tmp_path / "data"))
    try:
        yield m
    finally:
        mail_ssh.reset_ssh_factory()
        mail_install.reset_runner()
        mail_dns.reset_dns_resolver()
        mail_host.reset_host_factory()
        with SessionLocal() as db:
            db.execute(delete(MailAccount).where(MailAccount.address.like(f"%@{DOMEN}")))
            db.execute(delete(MailServer).where(MailServer.hostname == HOST))
            db.commit()


def _chernovik(client, **pola) -> dict:
    otvet = client.post(SERVER, json={"mode": "ssh", "hostname": HOST, "domain": DOMEN,
                                      "ssh_host": "203.0.113.7", "ssh_user": "u", **pola})
    assert otvet.status_code == 201, otvet.text
    return otvet.json()


def _po_ssh(client, m) -> dict:
    s = _chernovik(client)
    proba = client.post(f"{SERVER}/{s['id']}/ssh/probe").json()
    assert proba["fingerprint"].startswith("SHA256:") and proba["known"] is False
    otvet = client.post(f"{SERVER}/{s['id']}/ssh/connect", json={
        "host_key_line": proba["key_line"], "user": "u", "password": "right",
        "sudo_password": "sudo-pass", "keep_sudo": True,
    })
    assert otvet.status_code == 200, otvet.text
    return s


def _bez_sekretov(obekt):
    tekst = json.dumps(obekt)
    assert "_encrypted" not in tekst
    assert "PRIVATE KEY" not in tekst


# --- протокол и подсказки ---


def test_razbor_protokola_shagi_dannye_itog():
    r = mail_install.Razbor()
    for stroka in UDACHA:
        r.stroka(stroka)
    assert r.end == "ok" and r.status_raboty() == "ok"
    shagi = {s["code"]: s for s in r.steps}
    assert shagi["egress25"]["status"] == "warn" and shagi["egress25"]["hint"] == "egress25_blocked"
    assert shagi["egress25"]["detail"].startswith("outbound port 25")
    assert all(s["status"] in ("ok", "warn") for s in r.steps)
    assert r.data["public_ip"] == "203.0.113.7" and r.data["dkim_txt"] == "v=DKIM1; k=rsa; p=MIIBIjAN"
    assert r.existing == ["dmsprobe"]
    assert r.stroka("pulling something") is False


def test_razbor_otkata():
    r = mail_install.Razbor()
    for stroka in OTKAT:
        r.stroka(stroka)
    assert r.status_raboty() == "rolled_back" and r.rollback
    assert r.error_code == "port_busy" and "busy" in r.error_detail
    assert next(s for s in r.steps if s["code"] == "start")["status"] == "fail"


def _kody_ustanovshchika() -> set[str]:
    tekst = (KOREN / "core" / "pochta" / "ustanovit.sh").read_text(encoding="utf-8")
    kody = set(re.findall(r"(?:p_fail|provalit) \S+ ([a-z0-9_]+) ", tekst))
    # Предупреждение несёт код беды первым словом, если он есть: слово с подчёркиванием.
    kody |= set(re.findall(r'p_warn \S+ "([a-z0-9]+_[a-z0-9_]+) ', tekst))
    return kody


def test_na_kazhduyu_bedu_est_instruktsiya_na_dvuh_yazykah():
    kontrakt = set("""os_unsupported no_privileges sudo_password_needed docker_missing docker_install_failed
        compose_missing port_busy low_memory low_disk egress25_blocked mta_running dms_exists
        image_pull_failed tls_failed start_failed unhealthy dkim_failed firewall_failed
        ssh_unreachable ssh_auth_failed ssh_host_key_changed ssh_host_key_unconfirmed ssh_key_bad
        ssh_sudo_needed mail_agent_unreachable mail_agent_failed mail_updater_offline""".split())
    kody = kontrakt | _kody_ustanovshchika()
    assert "port_busy" in _kody_ustanovshchika() and "egress25_blocked" in _kody_ustanovshchika()
    for kod in kody:
        h = mail_install.HINTS.get(kod)
        assert h, f"нет инструкции для {kod}"
        assert h["title"]["ru"] and h["title"]["en"]
        assert h["steps"]["ru"] and len(h["steps"]["ru"]) == len(h["steps"]["en"]), kod


# --- близнецы и обновлятор ---


def test_imena_faylov_sovpadayut_s_obnovlyatorom():
    """Разойдись имя — просьба лежала бы там, куда обновлятор не смотрит, и молча."""
    for imya in ("PAPKA", "ZAPROS", "ZHURNAL", "ITOG", "OBNOVLYATOR", "OPERATSII"):
        assert getattr(mail_spool, imya) == getattr(deploy_pochta, imya), imya


def test_proekt_pochty_nikogda_ne_osnovnoy():
    shablon = (KOREN / "core" / "pochta" / "compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^name: opencrm-mail$", shablon, re.M)
    assert deploy_pochta.PROEKT == "opencrm-mail"
    assert "PROJECT=opencrm-mail" in (KOREN / "core" / "pochta" / "ustanovit.sh").read_text(encoding="utf-8")


def test_khost_sam_reshaet_puti_i_set(tmp_path):
    env = deploy_pochta.okruzhenie(
        {"MAIL_DIR": "/etc", "MAIL_HOSTNAME": "mail.x.test", "MAIL_DOMAIN": "x.test", "MAIL_SUDO_PASSWORD": "x",
         "MAIL_TLS": "letsencrypt", "bad key": "1", "MAIL_IMAGE": "evil/root:latest", "MAIL_PORTS": "22 80",
         "MAIL_ALLOW_DOCKER_INSTALL": "1"},
        tmp_path / "home", tmp_path / "repo",
    )
    assert env["MAIL_DIR"] == str(tmp_path / "home" / "mail")
    assert env["MAIL_NETWORK"].endswith("_default") and env["MAIL_CONTAINER"] == "opencrm-mail"
    assert env["MAIL_SUDO_PASSWORD"] == "" and env["MAIL_TLS"] == "webroot"
    assert env["MAIL_HOSTNAME"] == "mail.x.test" and "bad key" not in env
    # Образ и порты — хоста: `data/` пишет приложение, и взломанное подсунуло бы свой образ под root.
    assert env["MAIL_IMAGE"] == deploy_pochta.OBRAZ and env["MAIL_PORTS"] == "25 465 587 993"
    assert env["MAIL_ALLOW_DOCKER_INSTALL"] == "0"
    for plokho in ({"MAIL_HOSTNAME": "mail.x.test\nMAIL_IMAGE=evil"}, {"MAIL_HOSTNAME": "-oProxy.x.test"},
                   {"MAIL_HOSTNAME": "mail.x.test", "MAIL_FIRST_PASSWORD": "a\nb"},
                   {"MAIL_HOSTNAME": "mail.x.test", "MAIL_ACME_EMAIL": "--agree x@y.test"}):
        with pytest.raises(deploy_pochta.PlokhayaProsba):
            deploy_pochta.okruzhenie(plokho, tmp_path / "home", tmp_path / "repo")


def test_obraz_khosta_bliznets_modeli():
    from database.models.mail_server import DEFAULT_IMAGE

    assert deploy_pochta.OBRAZ == DEFAULT_IMAGE
    assert deploy_pochta.PORTY == mail_install.PORTY


def _podstavnoy_ustanovshchik(repo: Path) -> None:
    papka = repo / "core" / "pochta"
    papka.mkdir(parents=True, exist_ok=True)
    (papka / "ustanovit.sh").write_text(
        "#!/bin/sh\n"
        'echo "##STEP env"\n'
        'grep -q "^MAIL_DIR=" ustanovka.env && echo "##OK env has-dir"\n'
        'echo "op=$1"\n'
        'rm -f ustanovka.env\n'
        'echo "##END ok"\n',
        encoding="utf-8", newline="\n",
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="нужен sh")
def test_obnovlyator_ispolnyaet_prosbu_i_pishet_zhurnal(tmp_path):
    from tests.test_autoupdate import make_updater

    updater = make_updater(tmp_path)
    _podstavnoy_ustanovshchik(updater.config.project_dir)
    papka = deploy_pochta.papka(updater.config.data_dir)
    papka.mkdir(parents=True, exist_ok=True)
    (papka / deploy_pochta.ZAPROS).write_text(
        json.dumps({"job_id": 77, "op": "check", "env": {"MAIL_HOSTNAME": "mail.x.test"}, "kto": "Анна"}),
        encoding="utf-8",
    )

    updater._podozhdat(15)

    assert not (papka / deploy_pochta.ZAPROS).exists(), "просьба исполняется один раз"
    zhurnal = (papka / deploy_pochta.ZHURNAL.format(job=77)).read_text(encoding="utf-8")
    assert "##OK env has-dir" in zhurnal and "op=check" in zhurnal and "##END ok" in zhurnal
    itog = json.loads((papka / deploy_pochta.ITOG.format(job=77)).read_text(encoding="utf-8"))
    assert itog["ok"] is True and itog["status"] == "ok"
    rabota = updater.config.state_dir / deploy_pochta.RABOTA
    assert rabota.is_dir(), "рабочий каталог установщика — у обновлятора, а не в общем data/"
    assert not (papka / deploy_pochta.RABOTA).exists()
    assert not (rabota / "ustanovka.env").exists(), "пароли не остаются на диске"
    assert json.loads((papka / deploy_pochta.OBNOVLYATOR).read_text(encoding="utf-8"))["at"].endswith("Z")


def test_obnovlyator_ne_ispolnyaet_plokhuyu_prosbu(tmp_path):
    """Имя сервера с переводом строки дописало бы в ustanovka.env свой MAIL_IMAGE."""
    from tests.test_autoupdate import make_updater

    updater = make_updater(tmp_path)
    _podstavnoy_ustanovshchik(updater.config.project_dir)
    papka = deploy_pochta.papka(updater.config.data_dir)
    papka.mkdir(parents=True, exist_ok=True)
    (papka / deploy_pochta.ZAPROS).write_text(json.dumps(
        {"job_id": 78, "op": "install", "env": {"MAIL_HOSTNAME": "mail.x.test\nMAIL_IMAGE=evil/x"}}), encoding="utf-8")
    updater._podozhdat(15)
    zhurnal = (papka / deploy_pochta.ZHURNAL.format(job=78)).read_text(encoding="utf-8")
    assert "##FAIL request start_failed" in zhurnal and "op=install" not in zhurnal
    assert json.loads((papka / deploy_pochta.ITOG.format(job=78)).read_text(encoding="utf-8"))["ok"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="ссылки и O_NOFOLLOW — на хосте Linux")
def test_obnovlyator_ne_idyot_po_ssylkam_iz_data(tmp_path):
    """Журнал, подменённый приложением на ссылку, не превращается в запись root куда угодно."""
    data = tmp_path / "data"
    papka = deploy_pochta.papka(data)
    papka.mkdir(parents=True)
    zhertva = tmp_path / "zhertva"
    zhertva.write_text("целое\n", encoding="utf-8")
    (papka / deploy_pochta.ZHURNAL.format(job=79)).symlink_to(zhertva)
    with pytest.raises(OSError):
        deploy_pochta.vypolnit({"job_id": 79, "op": "check", "env": {"MAIL_HOSTNAME": "mail.x.test"}},
                               data, tmp_path, tmp_path / "repo", state_dir=tmp_path / "updates")
    assert zhertva.read_text(encoding="utf-8") == "целое\n"


def test_spool_prosba_odna_i_zakryta(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCRM_DATA_DIR", str(tmp_path))
    mail_spool.zaprosit(5, "install", {"MAIL_FIRST_PASSWORD": "секрет"}, "Анна")
    assert mail_spool.zapros()["job_id"] == 5
    mail_spool.zaprosit(5, "install", {}, "Анна")  # своя — перезаписывается
    from core import exceptions as errors

    with pytest.raises(errors.ConflictError):
        mail_spool.zaprosit(6, "check", {}, "Борис")
    assert mail_spool.obnovlyator()["zhiv"] is False
    assert mail_spool.vzyata(5) is False
    assert mail_spool.otozvat(5) is True and mail_spool.zapros() is None
    if sys.platform != "win32":
        mail_spool.zaprosit(7, "check", {}, "x")
        assert (mail_spool.papka() / mail_spool.ZAPROS).stat().st_mode & 0o777 == 0o600


# --- агент ---


def test_agent_dkim_odnoy_strokoy():
    fayl = 'mail._domainkey IN TXT ( "v=DKIM1; k=rsa; "\n\t"p=MIIBAQ" "ABCD" ) ;'
    assert agent._dkim_txt(fayl) == "v=DKIM1; k=rsa; p=MIIBAQABCD"
    assert agent._dkim_txt("v=DKIM1; k=rsa; p=XYZ\n") == "v=DKIM1; k=rsa; p=XYZ"


def test_agent_tablitsa_po_imenam_stolbtsov():
    """doveadm сам выбирает порядок столбцов — читаем по шапке, а не по месту."""
    vyvod = "Username\tmailbox\tmessages\tuidnext\tuidvalidity\tunseen\nu2@x.test\tINBOX\t4\t5\t17\t3\n"
    assert agent._tab(vyvod) == [{"username": "u2@x.test", "mailbox": "INBOX", "messages": "4",
                                  "uidnext": "5", "uidvalidity": "17", "unseen": "3"}]


def test_agent_paroli_ne_v_argv(monkeypatch, tmp_path):
    """argv виден в `ps` всем на машине: пароль ящика, мастера и посредника — только stdin,
    дважды (DMS спрашивает пароль и подтверждение)."""
    vyzovy = []

    class Otvet:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **kw):
        vyzovy.append((list(argv), kw.get("input")))
        return Otvet()

    monkeypatch.setattr(agent.subprocess, "run", run)
    monkeypatch.setattr(agent, "CONFIG", str(tmp_path))
    monkeypatch.setattr(agent, "_dozhdatsya", lambda *_a, **_k: True)
    parol = "Sekret-parol-123"
    assert agent.vypolnit("account_add", {"address": "a@x.test", "password": parol})["ok"]
    assert agent.vypolnit("master_ensure", {"user": "opencrm", "password": parol})["ok"]
    assert agent.vypolnit("relay_set", {"domain": "x.test", "host": "smtp.r.test", "port": 587,
                                        "user": "u", "password": parol})["ok"]
    (tmp_path / "postfix-accounts.cf").write_text("a@x.test|{SHA512-CRYPT}x\n", encoding="utf-8")
    assert agent.vypolnit("account_password", {"address": "a@x.test", "password": parol})["ok"]
    agent._vhod_rabotaet("a@x.test", parol)
    assert all(parol not in " ".join(argv) for argv, _ in vyzovy), vyzovy
    s_parolem = [(argv, vvod) for argv, vvod in vyzovy if vvod and parol in vvod]
    assert {argv[1] for argv, _ in s_parolem if argv[0] == "setup"} == {"email", "dovecot-master", "relay"}
    assert all(vvod == f"{parol}\n{parol}\n" for argv, vvod in s_parolem if argv[0] == "setup")
    otkaz = agent.vypolnit("relay_set", {"domain": "x.test", "host": "smtp.r.test", "user": "u", "password": ""})
    assert otkaz["code"] == "mail_relay_password_required"
    assert agent.vypolnit("account_quota", {"address": "a@x.test", "quota_mb": -5})["code"] == "mail_quota_bad"


def test_zagruzchik_agenta_beryot_argumenty_iz_stdin():
    """Тот же загрузчик, что уходит по SSH, — настоящим python: аргументы первой строкой."""
    stdin = base64.b64encode(json.dumps({"address": "-bad@x.test", "password": "Long-pass-123"}).encode())
    stdin += b"\n" + mail_host.tekst_agenta()
    r = subprocess.run([sys.executable, "-c", mail_host.ZAGRUZCHIK, "account_add"], input=stdin,
                       capture_output=True, timeout=60)
    assert json.loads(r.stdout.decode().strip().splitlines()[-1])["code"] == "mail_local_part_bad", r.stderr
    r = subprocess.run([sys.executable, "-c", mail_host.ZAGRUZCHIK, "nope"], input=b"\n" + mail_host.tekst_agenta(),
                       capture_output=True, timeout=60)
    assert json.loads(r.stdout.decode().strip())["message"] == "unknown operation"


def test_agent_otkazy_v_json():
    assert agent.vypolnit("nope", {}) == {"ok": False, "code": "mail_agent_failed", "message": "unknown operation"}
    otvet = agent.vypolnit("account_add", {"address": "-bad@x.test", "password": "Long-pass-123"})
    assert otvet["ok"] is False and otvet["code"] == "mail_local_part_bad"
    otvet = agent.vypolnit("account_add", {"address": "a@x.test", "password": "a|b\nc"})
    assert otvet["code"] == "mail_password_weak"
    assert agent._v_mb("200M") == 200 and agent._v_mb("1G") == 1024 and agent._v_mb("") is None


# --- SSH ---


def test_svoy_klyuch_chitaetsya_i_imeet_otpechatok():
    zakrytyy, otkrytyy = mail_ssh.sozdat_klyuch("opencrm-mail-1")
    assert zakrytyy.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert otkrytyy.startswith("ssh-ed25519 ") and otkrytyy.endswith(" opencrm-mail-1")
    assert mail_ssh.otkrytyy_iz_zakrytogo(zakrytyy) == " ".join(otkrytyy.split()[:2])
    tip, telo = mail_ssh.razobrat_stroku_klyucha(otkrytyy)
    assert tip == "ssh-ed25519" and re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", mail_ssh.otpechatok(telo))
    with pytest.raises(mail_ssh.SshBeda):
        mail_ssh.razobrat_stroku_klyucha("ssh-ed25519 !!!")


def test_ssh_tofu_svoy_klyuch_i_parol_ne_khranitsya(root_client, mashina):
    s = _po_ssh(root_client, mashina)
    assert len(mashina.klyuchi) == 1 and mashina.klyuchi[0].endswith(f"opencrm-mail-{s['id']}")
    with SessionLocal() as db:
        server = db.get(MailServer, s["id"])
        assert server.ssh_key_encrypted and server.ssh_host_key == mashina.host_line
        assert server.sudo_password_encrypted, "просили запомнить sudo — запомнен"
        assert "right" not in json.dumps({c.name: str(getattr(server, c.key)) for c in MailServer.__table__.columns})
    spisok = root_client.get(SERVER).json()
    nash = next(x for x in spisok["servers"] if x["id"] == s["id"])
    assert nash["has_ssh_key"] and nash["has_sudo_password"] and nash["ssh_fingerprint"].startswith("SHA256:")
    _bez_sekretov(spisok)
    assert root_client.post(f"{SERVER}/{s['id']}/ssh/probe").json()["known"] is True

    # Подмена сервера: другой ключ хоста — отказ, а не «запомнить новый».
    _, chuzhoy = mail_ssh.sozdat_klyuch("evil")
    mashina.host_line = " ".join(chuzhoy.split()[:2])
    with SessionLocal() as db:
        server = db.get(MailServer, s["id"])
        with pytest.raises(mail_ssh.SshBeda) as beda:
            mail_install.ssh_soedinenie(server)
    assert beda.value.code == "ssh_host_key_changed"


def test_ssh_svoi_starye_klyuchi_snimayutsya(root_client, mashina):
    """Свой прежний ключ CRM — прочь при новом подключении и при удалении сервера;
    ключ человека и ключ другого сервера CRM не трогаются."""
    chelovek = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGNoZWxvdmVrLWtsanVjaC0xMjM0NTY3ODkwMTIzNDU2 anna@laptop"
    mashina.klyuchi.append(chelovek)
    s = _po_ssh(root_client, mashina)
    _, drugoy = mail_ssh.sozdat_klyuch("opencrm-mail-999999")
    mashina.klyuchi.append(drugoy)
    proba = root_client.post(f"{SERVER}/{s['id']}/ssh/probe").json()
    otvet = root_client.post(f"{SERVER}/{s['id']}/ssh/connect", json={
        "host_key_line": proba["key_line"], "user": "u", "password": "right"}).json()
    nashi = [k for k in mashina.klyuchi if k.endswith(f" opencrm-mail-{s['id']}")]
    assert len(nashi) == 1 and otvet["old_keys_removed"] == 1 and otvet["warning"] is None
    assert chelovek in mashina.klyuchi and drugoy in mashina.klyuchi
    with SessionLocal() as db:
        assert db.get(MailServer, s["id"]).ssh_public_key == nashi[0], "остался именно новый ключ"
    # Новый ключ впускает — значит уборка не сняла его.
    assert root_client.post(f"{SERVER}/{s['id']}/check", json={}).status_code == 202

    udaleno = root_client.delete(f"{SERVER}/{s['id']}")
    assert udaleno.status_code == 200 and udaleno.json()["keys_removed"] == 1
    assert not [k for k in mashina.klyuchi if k.endswith(f" opencrm-mail-{s['id']}")]
    assert chelovek in mashina.klyuchi and drugoy in mashina.klyuchi


def test_ssh_klyuch_ne_snyalsya_preduprezhdenie_s_komandoy(root_client, mashina):
    s = _po_ssh(root_client, mashina)
    mashina.chistka_lomaetsya = True
    otvet = root_client.delete(f"{SERVER}/{s['id']}")
    assert otvet.status_code == 200, otvet.text
    preduprezhdenie = otvet.json()["warning"]
    assert preduprezhdenie["code"] == "ssh_old_keys_left"
    assert preduprezhdenie["command"] == f"sed -i '/ opencrm-mail-{s['id']}$/d' ~/.ssh/authorized_keys"


def test_kody_otkazov_po_smyslu(root_client, mashina):
    s = _chernovik(root_client)
    proba = root_client.post(f"{SERVER}/{s['id']}/ssh/probe").json()
    otvet = root_client.post(f"{SERVER}/{s['id']}/ssh/connect", json={
        "host_key_line": proba["key_line"], "user": "Bad User", "password": "right"})
    assert otvet.json()["error"]["code"] == "ssh_user_bad"
    assert root_client.patch(f"{SERVER}/{s['id']}", json={"ssh_user": "-x"}).json()["error"]["code"] == "ssh_user_bad"
    root_client.post(f"{SERVER}/{s['id']}/ssh/connect", json={
        "host_key_line": proba["key_line"], "user": "u", "password": "right"})
    otvet = root_client.post(f"{SERVER}/{s['id']}/install", json={"acme_email": "not-an-email"})
    assert otvet.json()["error"]["code"] == "mail_acme_email_bad"
    assert root_client.post(f"{SERVER}/{s['id']}/install", json={"tls": "selfsigned"}).status_code == 202
    otvet = root_client.put(f"{SERVER}/{s['id']}/relay", json={"host": "smtp.relay.test", "user": "login"})
    assert otvet.json()["error"]["code"] == "mail_relay_password_required"


def test_dkim_dlya_zavedyonnogo_domena(root_client, mashina):
    """Установка не создала DKIM (`dkim_failed` — предупреждение, не провал) — кнопка его создаёт."""
    s = _po_ssh(root_client, mashina)
    mashina.protokol["install"] = [s for s in UDACHA if "dkim" not in s]
    assert root_client.post(f"{SERVER}/{s['id']}/install", json={"tls": "selfsigned"}).status_code == 202
    domen = next(x for x in root_client.get(SERVER).json()["servers"] if x["id"] == s["id"])["domains"][0]
    assert not domen["dkim_txt"]
    mashina.dkim_txt = "v=DKIM1; k=rsa; p=CREATED"
    otvet = root_client.post(f"{SERVER}/{s['id']}/domains/{domen['id']}/dkim")
    assert otvet.status_code == 200, otvet.text
    assert otvet.json()["dkim_txt"] == "v=DKIM1; k=rsa; p=CREATED" and otvet.json()["dkim_selector"] == "mail"
    domen = next(x for x in root_client.get(SERVER).json()["servers"] if x["id"] == s["id"])["domains"][0]
    assert domen["dkim_txt"] == "v=DKIM1; k=rsa; p=CREATED"
    assert root_client.post(f"{SERVER}/{s['id']}/domains/999999/dkim").status_code == 404


def test_zhurnal_raboty_bez_povtorov(root_client, mashina, monkeypatch):
    """Pisar дописал журнал между чтением длины и куска — строки приходили дважды."""
    from database.repositories import mail_jobs as jobs_repo

    s = _chernovik(root_client)
    with SessionLocal() as db:
        job = MailJob(server_id=s["id"], kind="check", status="ok", steps="[]", log="первая строка\n")
        db.add(job)
        db.commit()
        job_id = job.id
    nastoyashchiy = jobs_repo.zhurnal
    dopisano = []

    def s_dopiskoy(db, *a, **k):
        if not dopisano:
            dopisano.append(1)
            with SessionLocal() as drugaya:
                jobs_repo.dopisat_zhurnal(drugaya, job_id, "вторая ✓\n", steps="[]", kogda=None)
                drugaya.commit()
        return nastoyashchiy(db, *a, **k)

    monkeypatch.setattr(jobs_repo, "zhurnal", s_dopiskoy)
    pervyy = root_client.get(f"{SERVER}/jobs/{job_id}").json()
    vtoroy = root_client.get(f"{SERVER}/jobs/{job_id}", params={"log_from": pervyy["log_size"]}).json()
    assert pervyy["log_chunk"] + vtoroy["log_chunk"] == "первая строка\nвторая ✓\n"


def test_kusok_zhurnala_ne_rvyot_simvoly(mashina, root_client):
    from database.repositories import mail_jobs as jobs_repo

    s = _chernovik(root_client)
    tekst = "шаг ✓ 🙂 готово\n" * 3
    with SessionLocal() as db:
        job = MailJob(server_id=s["id"], kind="check", status="ok", steps="[]", log=tekst)
        db.add(job)
        db.commit()
        sobrano, otkuda = "", 0
        for _ in range(200):
            kusok, otkuda = jobs_repo.zhurnal(db, job.id, otkuda, 5)
            sobrano += kusok
            if otkuda >= len(tekst.encode()):
                break
        assert sobrano == tekst
        assert jobs_repo.zhurnal(db, job.id, 1, 100)[0].startswith("аг"), "смещение посреди символа — со следующего"


def test_obnovlyator_poslednie_stroki_ne_teryayutsya(monkeypatch):
    """Итог появился между чтением журнала и проверкой итога — хвост журнала терялся."""
    sostoyanie = {"zhurnal": b"##STEP start\n", "itog": None}

    def zhurnal(_job, s_bayta=0):
        kusok = sostoyanie["zhurnal"][s_bayta:]
        if sostoyanie["itog"] is None:
            sostoyanie["zhurnal"] += b"##DATA dkim_txt v=DKIM1; p=TAIL\n##END ok\n"
            sostoyanie["itog"] = {"ok": True, "status": "ok"}
        return kusok, s_bayta + len(kusok)

    monkeypatch.setattr(mail_spool, "zaprosit", lambda *a, **k: None)
    monkeypatch.setattr(mail_spool, "vzyata", lambda _j: True)
    monkeypatch.setattr(mail_spool, "zhurnal", zhurnal)
    monkeypatch.setattr(mail_spool, "itog", lambda _j: sostoyanie["itog"])
    monkeypatch.setattr(mail_spool, "ubrat", lambda _j: None)
    monkeypatch.setattr(mail_install.time, "sleep", lambda _s: None)

    class Pisar:
        def __init__(self):
            self.razbor = mail_install.Razbor()

        def __call__(self, stroka):
            self.razbor.stroka(stroka)

    pisar = Pisar()
    mail_install._cherez_obnovlyator("install", {}, pisar, "kto", 1)
    assert pisar.razbor.data.get("dkim_txt") == "v=DKIM1; p=TAIL" and pisar.razbor.end == "ok"


@pytest.mark.skipif(shutil.which("sh") is None, reason="нужен sh")
def test_ustanovshchik_stiraet_paroli_srazu(tmp_path):
    """Убитый установщик (SIGKILL, обрыв) не оставляет ustanovka.env: его стирают сразу
    после чтения, а не в ловушке EXIT, до которой при SIGKILL дело не доходит."""
    shutil.copy(KOREN / "core" / "pochta" / "ustanovit.sh", tmp_path / "ustanovit.sh")
    env = tmp_path / "ustanovka.env"
    env.write_text("MAIL_HOSTNAME=mail.x.test\nMAIL_FIRST_PASSWORD=Sekret-123\nMAIL_DIR=/tmp/f1-net\n",
                   encoding="utf-8", newline="\n")
    proc = subprocess.Popen(["sh", "./ustanovit.sh", "check"], cwd=tmp_path, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        pervaya = proc.stdout.readline().decode()
        assert pervaya.startswith("##STEP os"), pervaya
        proc.kill()
    finally:
        proc.wait(30)
    assert not env.exists()


def test_ssh_nevernyy_parol(root_client, mashina):
    s = _chernovik(root_client)
    proba = root_client.post(f"{SERVER}/{s['id']}/ssh/probe").json()
    otvet = root_client.post(f"{SERVER}/{s['id']}/ssh/connect", json={
        "host_key_line": proba["key_line"], "user": "u", "password": "wrong"})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "ssh_auth_failed"
    otvet = root_client.post(f"{SERVER}/{s['id']}/install", json={})
    assert otvet.status_code == 422 and otvet.json()["error"]["code"] == "ssh_host_key_unconfirmed"


# --- работы ---


def test_proverka_ustanovka_parol_odin_raz_i_udalenie(root_client, mashina):
    s = _po_ssh(root_client, mashina)
    proverka = root_client.post(f"{SERVER}/{s['id']}/check", json={})
    assert proverka.status_code == 202, proverka.text
    job = root_client.get(f"{SERVER}/jobs/{proverka.json()['id']}").json()
    assert job["job"]["status"] == "ok" and job["log_size"] > 0
    assert "pulling something" in job["log_chunk"]
    assert any(st["code"] == "dns" for st in job["job"]["steps"])
    egress = next(st for st in job["job"]["steps"] if st["code"] == "egress25")
    assert egress["hint"]["title_en"] and egress["hint"]["steps_ru"]
    assert mashina.zapuski[-1][0] == "check"
    assert mashina.zapuski[-1][1]["MAIL_SUDO_PASSWORD"] == "sudo-pass"
    assert set(mashina.fayly) >= {"/home/u/.opencrm-mail-setup/ustanovit.sh", "/home/u/.opencrm-mail-setup/compose.yml"}
    kusok = root_client.get(f"{SERVER}/jobs/{job['job']['id']}", params={"log_from": job["log_size"]}).json()
    assert kusok["log_chunk"] == ""

    otvet = root_client.post(f"{SERVER}/{s['id']}/install", json={"tls": "selfsigned"})
    assert otvet.status_code == 202, otvet.text
    telo = otvet.json()
    assert telo["first_address"] == f"admin@{DOMEN}" and len(telo["first_password"]) == 16
    env = mashina.zapuski[-1][1]
    assert env["MAIL_FIRST"] == f"admin@{DOMEN}" and env["MAIL_FIRST_PASSWORD"] == telo["first_password"]
    assert env["MAIL_MASTER_USER"] == "opencrm" and env["MAIL_MASTER_PASSWORD"]
    assert env["MAIL_TLS"] == "selfsigned" and env["MAIL_DIR"] == "/home/u/opencrm-mail"

    spisok = root_client.get(SERVER).json()
    nash = next(x for x in spisok["servers"] if x["id"] == s["id"])
    assert nash["status"] == STATUS_READY and nash["tls_pin"].startswith("65621d60")
    assert nash["boxes_count"] == 1 and nash["imap_host"] == "203.0.113.7"
    assert nash["domains"][0]["dkim_txt"] == "v=DKIM1; k=rsa; p=MIIBIjAN"
    _bez_sekretov(spisok)
    raboty = root_client.get(f"{SERVER}/{s['id']}/jobs").json()
    assert [r["kind"] for r in raboty[:2]] == ["install", "check"]
    assert telo["first_password"] not in json.dumps(raboty)
    assert telo["first_password"] not in root_client.get(f"{SERVER}/jobs/{telo['job']['id']}").text

    dns = root_client.post(f"{SERVER}/{s['id']}/dns").json()
    zapisi = {z["type"]: z for z in dns["domains"][0]["records"]}
    assert zapisi["A"]["status"] == "ok" and zapisi["MX"]["status"] == "ok" and zapisi["SPF"]["status"] == "ok"
    assert zapisi["DKIM"]["status"] == "missing" and zapisi["DKIM"]["hint"]["ru"]
    assert zapisi["PTR"]["status"] == "warn" and zapisi["PTR"]["required"] is False

    assert root_client.post(f"{SERVER}/{s['id']}/install", json={}).json()["error"]["code"] == "mail_server_busy"
    assert root_client.delete(f"{SERVER}/{s['id']}").json()["error"]["code"] == "mail_server_busy"
    ne_to = root_client.post(f"{SERVER}/{s['id']}/remove", json={"confirm": "mail.other.test"})
    assert ne_to.status_code == 422 and ne_to.json()["error"]["code"] == "mail_confirm_mismatch"
    snyat = root_client.post(f"{SERVER}/{s['id']}/remove", json={"confirm": HOST, "delete_mail": True})
    assert snyat.status_code == 202 and snyat.json()["status"] == "ok"
    assert mashina.zapuski[-1][1]["MAIL_DELETE_DATA"] == "1"
    with SessionLocal() as db:
        box = db.scalar(select(MailAccount).where(MailAccount.address == f"admin@{DOMEN}"))
        assert box.is_active is False, "ящики снятого сервера гаснут, но остаются"
    assert root_client.delete(f"{SERVER}/{s['id']}").status_code == 200


def test_provalennaya_ustanovka_otkatyvaetsya_s_instruktsiey(root_client, mashina):
    s = _po_ssh(root_client, mashina)
    mashina.protokol["install"] = OTKAT
    otvet = root_client.post(f"{SERVER}/{s['id']}/install", json={"first_local": "info", "first_password": "Strong-pass-123"})
    assert otvet.status_code == 202
    assert otvet.json()["first_password"] == "Strong-pass-123"
    job = root_client.get(f"{SERVER}/jobs/{otvet.json()['job']['id']}").json()["job"]
    assert job["status"] == "rolled_back" and job["error_code"] == "port_busy"
    assert job["hint"]["code"] == "port_busy" and job["hint"]["steps_en"]
    nash = next(x for x in root_client.get(SERVER).json()["servers"] if x["id"] == s["id"])
    assert nash["status"] == "failed" and nash["last_error"].startswith("port_busy")
    assert nash["boxes_count"] == 0
    assert root_client.delete(f"{SERVER}/{s['id']}").status_code == 200


def test_podklyuchenie_sushchestvuyushchego(root_client, mashina):
    s = _po_ssh(root_client, mashina)
    mashina.protokol["check"] = [
        "##STEP docker", "##OK docker via docker", "##DATA docker_cmd sudo -n docker",
        "##STEP ports", "##DATA port_owner_25 container:mailserver",
        "##FAIL ports port_busy busy: 25(container:mailserver)",
        "##STEP existing", "##DATA existing_container mailserver", "##WARN existing dms_exists found",
        "##END failed",
    ]
    otvet = root_client.post(f"{SERVER}/{s['id']}/adopt")
    assert otvet.status_code == 202, otvet.text
    job = root_client.get(f"{SERVER}/jobs/{otvet.json()['id']}").json()["job"]
    assert job["status"] == "ok", job
    assert mashina.agent_vyzovy[0] == ("sudo -n docker", "ping")
    assert "opencrm" in mashina.mastera
    nash = next(x for x in root_client.get(SERVER).json()["servers"] if x["id"] == s["id"])
    assert nash["status"] == STATUS_READY and nash["container"] == "mailserver"
    assert nash["tls_pin"] == "ab" * 32 and nash["boxes_count"] == 1
    assert nash["domains"][0]["dkim_txt"] == "v=DKIM1; k=rsa; p=ADOPTED"
    with SessionLocal() as db:
        server = db.get(MailServer, s["id"])
        from core.security import secretbox
        from core.services import mail_server_service

        assert secretbox.decrypt(server.master_password_encrypted, mail_server_service.MASTER_PURPOSE) == mashina.mastera["opencrm"]


def test_prervannaya_rabota_chestno_padaet(root_client, mashina):
    s = _chernovik(root_client)
    from datetime import timedelta

    from core.utils import now_utc

    with SessionLocal() as db:
        db.add(MailJob(server_id=s["id"], kind="install", status=JOB_RUNNING, steps="[]", log="",
                       heartbeat_at=now_utc() - timedelta(minutes=10)))
        db.get(MailServer, s["id"]).status = "installing"
        db.commit()
    nash = next(x for x in root_client.get(SERVER).json()["servers"] if x["id"] == s["id"])
    assert nash["status"] == "failed"
    raboty = root_client.get(f"{SERVER}/{s['id']}/jobs").json()
    assert raboty[0]["status"] == "failed" and raboty[0]["error_code"] == "job_interrupted"
    assert raboty[0]["hint"]["title_ru"]


def test_vtoraya_rabota_ne_nachinaetsya(root_client, mashina):
    s = _po_ssh(root_client, mashina)
    from core.utils import now_utc

    with SessionLocal() as db:
        db.add(MailJob(server_id=s["id"], kind="check", status=JOB_RUNNING, steps="[]", log="", heartbeat_at=now_utc()))
        db.commit()
    otvet = root_client.post(f"{SERVER}/{s['id']}/check", json={})
    assert otvet.status_code == 409 and otvet.json()["error"]["code"] == "mail_job_running"


def test_chernovik_proveryaet_vvod(root_client, mashina):
    plokho = root_client.post(SERVER, json={"mode": "ftp", "hostname": HOST, "domain": DOMEN})
    assert plokho.json()["error"]["code"] == "mail_server_mode_bad"
    plokho = root_client.post(SERVER, json={"mode": "ssh", "hostname": "mail", "domain": DOMEN, "ssh_host": "h"})
    assert plokho.json()["error"]["code"] == "mail_hostname_bad"
    plokho = root_client.post(SERVER, json={"mode": "ssh", "hostname": HOST, "domain": "x", "ssh_host": "h"})
    assert plokho.json()["error"]["code"] == "mail_domain_bad"
    _chernovik(root_client)
    dubl = root_client.post(SERVER, json={"mode": "ssh", "hostname": HOST, "domain": DOMEN, "ssh_host": "h"})
    assert dubl.status_code == 409 and dubl.json()["error"]["code"] == "mail_domain_exists"


def test_mestnyy_server_cherez_obnovlyator(root_client, mashina, tmp_path):
    """Вариант 1: просьба файлом, обновлятор исполняет, экран дочитывает журнал."""
    otlozheno = []
    mail_install.set_runner(lambda fn, _imya: otlozheno.append(fn))
    s = root_client.post(SERVER, json={"mode": "local", "hostname": HOST, "domain": DOMEN})
    assert s.status_code == 201, s.text
    s = s.json()
    assert s["imap_host"] == "opencrm-mail"
    otvet = root_client.post(f"{SERVER}/{s['id']}/install", json={"tls": "selfsigned"})
    assert otvet.status_code == 202
    prosba = mail_spool.zapros()
    assert prosba is None, "просьба кладётся из потока работы, не из запроса"
    import threading

    potok = threading.Thread(target=otlozheno[0])
    potok.start()
    for _ in range(100):
        prosba = mail_spool.zapros()
        if prosba:
            break
        potok.join(0.05)
    assert prosba["op"] == "install" and prosba["env"]["MAIL_AGENT_TOKEN"]
    # Обновлятор: убрал просьбу, написал журнал и итог.
    papka = mail_spool.papka()
    (papka / mail_spool.ZAPROS).unlink()
    (papka / mail_spool.ZHURNAL.format(job=prosba["job_id"])).write_text("\n".join(UDACHA) + "\n", encoding="utf-8")
    (papka / mail_spool.ITOG.format(job=prosba["job_id"])).write_text('{"ok": true, "status": "ok"}', encoding="utf-8")
    potok.join(20)
    assert not potok.is_alive()
    nash = next(x for x in root_client.get(SERVER).json()["servers"] if x["id"] == s["id"])
    assert nash["status"] == STATUS_READY and nash["imap_host"] == "opencrm-mail"
    assert not (papka / mail_spool.ZHURNAL.format(job=prosba["job_id"])).exists(), "файлы переписаны в базу и убраны"
    job = root_client.get(f"{SERVER}/jobs/{prosba['job_id']}").json()
    assert job["job"]["status"] == "ok" and "##END ok" in job["log_chunk"]
    with SessionLocal() as db:
        server = db.get(MailServer, s["id"])
        assert server.agent_token_encrypted and server.mode != MODE_SSH


def test_agent_po_ssh_razbiraet_otkazy(mashina):
    server = MailServer(id=999999, mode=MODE_SSH, ssh_host="h", ssh_port=22, ssh_user="u",
                        ssh_host_key=mashina.host_line, docker_cmd="docker; rm -rf /", container="opencrm-mail")
    from core.security import secretbox
    from core.services import mail_server_service

    zakrytyy, otkrytyy = mail_ssh.sozdat_klyuch()
    mashina.klyuchi.append(otkrytyy)
    server.ssh_key_encrypted = secretbox.encrypt(zakrytyy, mail_server_service.SSH_KEY_PURPOSE)
    with pytest.raises(mail_host.MailHostError) as beda:
        mail_host.AgentPoSsh(server).vyzvat("ping")
    assert beda.value.code == "mail_agent_failed", "чужая строка в docker_cmd не уходит в команду"
    server.docker_cmd = "docker"
    assert mail_host.AgentPoSsh(server).vyzvat("ping")["dms"] == "v16.0.1"
    with pytest.raises(mail_host.MailHostError) as beda:
        mail_host.AgentPoSsh(server).vyzvat("nope")
    assert beda.value.code == "mail_agent_failed"
    assert mail_host.SSH_KEY_PURPOSE == mail_server_service.SSH_KEY_PURPOSE
    assert mail_host.AGENT_PURPOSE == mail_server_service.AGENT_PURPOSE


def test_agent_po_http_s_tokenom(monkeypatch):
    from core.security import secretbox
    from core.services import mail_server_service

    poslano = {}

    class Otvet:
        status_code = 200

        def json(self):
            return {"ok": False, "code": "mail_box_exists", "message": "exists"}

    def post(url, json, headers, timeout):  # noqa: A002 — подпись httpx
        poslano.update(url=url, headers=headers, telo=json)
        return Otvet()

    monkeypatch.setattr(mail_host.httpx, "post", post)
    server = MailServer(mode="local", container="opencrm-mail",
                        agent_token_encrypted=secretbox.encrypt("tok-123", mail_server_service.AGENT_PURPOSE))
    with pytest.raises(mail_host.MailHostError) as beda:
        mail_host.host_dlya(server).vyzvat("account_add", {"address": "a@b.test"})
    assert beda.value.code == "mail_box_exists"
    assert poslano["url"] == "http://opencrm-mail:8790/v1/account_add"
    assert poslano["headers"]["Authorization"] == "Bearer tok-123"


def test_upravlyat_serverom_mozhet_tolko_upravlyayushchiy(root_client, mail_on, role_maker, staff_maker):  # noqa: F811
    rol = role_maker("Почта — только читать", ["mail.view", "mail.create"])
    anna = staff_maker("pochta-server-anna@test.local", rol["id"])
    assert anna.get(SERVER).status_code == 403
    assert anna.post(SERVER, json={"mode": "ssh", "hostname": HOST, "domain": DOMEN}).status_code == 403
