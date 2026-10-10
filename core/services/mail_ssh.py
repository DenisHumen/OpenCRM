"""SSH к машине с почтовым сервером (docs/bloki/32-pochtovyy-server.md, «Вариант 2: SSH»).

Ключ хоста — по TOFU: первый вход показывает отпечаток, человек его подтверждает,
строка ключа ложится в `MailServer.ssh_host_key`; дальше другой ключ — отказ
`ssh_host_key_changed`, а не «запомнить новый»: подмена сервера выглядит именно так.

Пароль входа нужен один раз: им кладётся СВОЙ ключ CRM (ed25519) в
`authorized_keys`, после чего пароль забывается. Свой, а не ключ человека: его
можно отозвать отдельно, не трогая доступ самого человека.

Сеть в тестах запрещена — шов `set_ssh_factory()`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
import shlex
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class SshBeda(Exception):
    """Отказ SSH с кодом для экрана: ssh_unreachable, ssh_auth_failed, ssh_host_key_changed…"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


@dataclass(frozen=True)
class KlyuchHosta:
    fingerprint: str  # SHA256:… — как печатает ssh-keygen -lf
    key_type: str
    key_line: str  # «ssh-ed25519 AAAA…» — то, что хранится и сверяется


class Soedinenie(Protocol):
    def vypolnit(
        self,
        komanda: str,
        *,
        stdin: bytes | str | None = None,
        timeout: float = 60,
        na_stroku: Callable[[str], None] | None = None,
    ) -> tuple[int, str]:
        """(код возврата, stdout+stderr). `na_stroku` — каждая строка по мере прихода."""
        ...

    def zalit(self, put: str, dannye: bytes, rezhim: int = 0o600) -> None: ...

    def zhivo(self) -> bool: ...

    def zakryt(self) -> None: ...


class SshKlient(Protocol):
    def klyuch_hosta(self, host: str, port: int, *, timeout: float = 8) -> KlyuchHosta: ...

    def podklyuchit(
        self,
        host: str,
        port: int,
        user: str,
        *,
        host_key_line: str,
        password: str | None = None,
        private_key: str | None = None,
        passphrase: str | None = None,
        timeout: float = 10,
    ) -> Soedinenie: ...


# --- ключи ---


def otpechatok(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def razobrat_stroku_klyucha(stroka: str) -> tuple[str, bytes]:
    """«тип base64 [комментарий]» → (тип, тело). Кривую строку не храним вовсе."""
    chasti = (stroka or "").strip().split()
    if len(chasti) < 2:
        raise SshBeda("ssh_key_bad", "Host key line is malformed")
    try:
        telo = base64.b64decode(chasti[1], validate=True)
    except ValueError as beda:
        raise SshBeda("ssh_key_bad", "Host key line is malformed") from beda
    return chasti[0], telo


def sozdat_klyuch(kommentariy: str = "opencrm-mail") -> tuple[str, str]:
    """Новый ed25519: (закрытый в формате OpenSSH, строка для authorized_keys)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    klyuch = Ed25519PrivateKey.generate()
    zakrytyy = klyuch.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode("ascii")
    otkrytyy = klyuch.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode("ascii")
    return zakrytyy, f"{otkrytyy} {kommentariy}".strip()


def _zagruzit_klyuch(tekst: str, passphrase: str | None):
    import paramiko

    oshibka: Exception | None = None
    for klass in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return klass.from_private_key(io.StringIO(tekst), password=passphrase or None)
        except paramiko.PasswordRequiredException as beda:
            raise SshBeda("ssh_key_bad", "The private key needs a passphrase") from beda
        except (paramiko.SSHException, ValueError) as beda:
            oshibka = beda
    raise SshBeda("ssh_key_bad", f"Private key is not readable: {oshibka}")


def otkrytyy_iz_zakrytogo(tekst: str, passphrase: str | None = None) -> str:
    klyuch = _zagruzit_klyuch(tekst, passphrase)
    return f"{klyuch.get_name()} {klyuch.get_base64()}"


# --- paramiko ---


class _ParamikoSoedinenie:
    def __init__(self, transport):
        self._t = transport

    def vypolnit(self, komanda, *, stdin=None, timeout=60, na_stroku=None):
        kanal = self._t.open_session(timeout=15)
        try:
            kanal.set_combine_stderr(True)
            kanal.settimeout(1.0)
            kanal.exec_command(komanda)
            if stdin is not None:
                dannye = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
                kanal.sendall(dannye)
            kanal.shutdown_write()
            konets = time.monotonic() + timeout
            bufer = b""
            vsyo: list[bytes] = []
            while True:
                if time.monotonic() > konets:
                    raise SshBeda("ssh_unreachable", f"Remote command timed out after {int(timeout)} s")
                try:
                    kusok = kanal.recv(65536)
                except socket.timeout:
                    if kanal.exit_status_ready() and not kanal.recv_ready():
                        break
                    continue
                if not kusok:
                    break
                vsyo.append(kusok)
                if na_stroku is not None:
                    bufer += kusok
                    while b"\n" in bufer:
                        stroka, bufer = bufer.split(b"\n", 1)
                        na_stroku(stroka.decode("utf-8", "replace").rstrip("\r"))
            if na_stroku is not None and bufer:
                na_stroku(bufer.decode("utf-8", "replace").rstrip("\r"))
            kod = kanal.recv_exit_status()
            return kod, b"".join(vsyo).decode("utf-8", "replace")
        finally:
            kanal.close()

    def zalit(self, put, dannye, rezhim=0o600):
        sftp = self._t.open_sftp_client()
        try:
            with sftp.open(put, "wb") as f:
                f.write(dannye)
            sftp.chmod(put, rezhim)
        finally:
            sftp.close()

    def zhivo(self):
        return self._t.is_active()

    def zakryt(self):
        try:
            self._t.close()
        except Exception:  # noqa: BLE001 — закрываем уже мёртвое
            pass


class ParamikoKlient:
    def _transport(self, host, port, timeout):
        import paramiko

        try:
            sok = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as beda:
            raise SshBeda("ssh_unreachable", f"Cannot reach {host}:{port}: {beda}") from beda
        t = paramiko.Transport(sok)
        t.banner_timeout = timeout
        try:
            t.start_client(timeout=timeout)
        except (paramiko.SSHException, OSError, EOFError) as beda:
            t.close()
            raise SshBeda("ssh_unreachable", f"SSH handshake with {host}:{port} failed: {beda}") from beda
        return t

    def klyuch_hosta(self, host, port, *, timeout=8):
        t = self._transport(host, port, timeout)
        try:
            k = t.get_remote_server_key()
            return KlyuchHosta(
                fingerprint=otpechatok(k.asbytes()),
                key_type=k.get_name(),
                key_line=f"{k.get_name()} {k.get_base64()}",
            )
        finally:
            t.close()

    def podklyuchit(self, host, port, user, *, host_key_line, password=None, private_key=None,
                    passphrase=None, timeout=10):
        import paramiko

        if not host_key_line:
            raise SshBeda("ssh_host_key_unconfirmed", "Host key is not confirmed yet")
        ozhidaem_tip, ozhidaem_telo = razobrat_stroku_klyucha(host_key_line)
        t = self._transport(host, port, timeout)
        try:
            k = t.get_remote_server_key()
            if k.get_name() != ozhidaem_tip or not hmac.compare_digest(k.asbytes(), ozhidaem_telo):
                raise SshBeda(
                    "ssh_host_key_changed",
                    f"Host key of {host} changed: now {otpechatok(k.asbytes())}",
                )
            try:
                if private_key:
                    t.auth_publickey(user, _zagruzit_klyuch(private_key, passphrase))
                elif password is not None:
                    t.auth_password(user, password)
                else:
                    raise SshBeda("ssh_auth_failed", "Neither a password nor a key was given")
            except paramiko.BadAuthenticationType as beda:
                raise SshBeda("ssh_auth_failed", f"Server refuses this way of login: {beda.allowed_types}") from beda
            except paramiko.AuthenticationException as beda:
                raise SshBeda("ssh_auth_failed", "Login refused: wrong user, password or key") from beda
            t.set_keepalive(30)
            return _ParamikoSoedinenie(t)
        except SshBeda:
            t.close()
            raise
        except (paramiko.SSHException, OSError, EOFError) as beda:
            t.close()
            raise SshBeda("ssh_unreachable", f"SSH session with {host} broke: {beda}") from beda


_factory: Callable[[], SshKlient] = ParamikoKlient


def klient() -> SshKlient:
    return _factory()


def set_ssh_factory(factory: Callable[[], SshKlient]) -> None:
    """Шов для тестов: сеть в наборе запрещена, SSH подменяется целиком."""
    global _factory
    _factory = factory
    zakryt_pul()


def reset_ssh_factory() -> None:
    global _factory
    _factory = ParamikoKlient
    zakryt_pul()


# --- то, что делается на сервере ---


def polozhit_klyuch(conn: Soedinenie, stroka: str) -> None:
    """Свой ключ в authorized_keys: идемпотентно, права 700/600. Строка — через stdin."""
    komanda = (
        "umask 077; K=$(cat); mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys"
        " && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
        ' && (grep -qxF "$K" ~/.ssh/authorized_keys || printf "%s\\n" "$K" >> ~/.ssh/authorized_keys)'
    )
    kod, vyvod = conn.vypolnit(komanda, stdin=stroka.strip() + "\n", timeout=20)
    if kod != 0:
        raise SshBeda("ssh_auth_failed", f"Cannot write ~/.ssh/authorized_keys: {vyvod.strip()[-200:]}")


METKA_KLYUCHA = re.compile(r"^opencrm-mail-\d{1,12}$")


def komanda_snyatiya(metka: str) -> str:
    """Готовая команда для человека, если CRM не смогла снять свои ключи сама."""
    return f"sed -i '/ {metka}$/d' ~/.ssh/authorized_keys"


def snyat_klyuchi(conn: Soedinenie, metka: str, ostavit: str | None = None) -> int:
    """Убрать из authorized_keys свои ключи с меткой `metka`, кроме `ostavit`.

    Старый ключ CRM жил бы там вечно: утёкшая копия базы с ключом secretbox
    пускала бы на машину и после смены ключа или удаления сервера.
    Сличаем по телу ключа, а не строкой: лишний пробел в файле не должен снять
    только что положенный ключ — это потеря входа.
    """
    if not METKA_KLYUCHA.match(metka):
        raise ValueError(metka)
    telo = ""
    if ostavit:
        _tip, syroe = razobrat_stroku_klyucha(ostavit)
        telo = base64.b64encode(syroe).decode("ascii")
    m = shlex.quote(metka)
    komanda = (
        'umask 077; T=$(cat); f="$HOME/.ssh/authorized_keys";'
        ' [ -f "$f" ] || { echo removed=0; exit 0; };'
        f' n=$(awk -v c={m} -v t="$T" \'$NF == c && $2 != t {{ n++ }} END {{ print n+0 }}\' "$f");'
        ' [ "$n" -gt 0 ] || { echo removed=0; exit 0; };'
        f' awk -v c={m} -v t="$T" \'!($NF == c && $2 != t)\' "$f" > "$f.opencrm-tmp"'
        ' && chmod 600 "$f.opencrm-tmp"'
        ' && if [ -L "$f" ]; then cat "$f.opencrm-tmp" > "$f" && rm -f "$f.opencrm-tmp"; else mv "$f.opencrm-tmp" "$f"; fi'
        ' && echo "removed=$n"'
    )
    kod, vyvod = conn.vypolnit(komanda, stdin=telo + "\n", timeout=20)
    najdeno = re.search(r"removed=(\d+)", vyvod or "")
    if kod != 0 or not najdeno:
        raise SshBeda("ssh_unreachable", f"Cannot clean ~/.ssh/authorized_keys: {(vyvod or '').strip()[-200:]}")
    return int(najdeno.group(1))


def razvedat(conn: Soedinenie, sudo_password: str | None = None) -> dict:
    """Кто мы на машине: root ли, есть ли docker и пускает ли он, есть ли sudo."""
    komanda = (
        'echo "uid=$(id -u)"; echo "home=$HOME";'
        " if command -v docker >/dev/null 2>&1; then"
        "  if docker info >/dev/null 2>&1; then echo docker=ok; else echo docker=noaccess; fi;"
        " else echo docker=missing; fi;"
        " if command -v sudo >/dev/null 2>&1; then"
        "  if sudo -n true >/dev/null 2>&1; then echo sudo=nopasswd; else echo sudo=password; fi;"
        " else echo sudo=none; fi"
    )
    kod, vyvod = conn.vypolnit(komanda, timeout=30)
    polya = {}
    for stroka in vyvod.splitlines():
        k, _, v = stroka.partition("=")
        polya[k.strip()] = v.strip()
    root = polya.get("uid") == "0"
    sudo = "root" if root else polya.get("sudo", "none")
    if sudo == "password" and sudo_password:
        kod, _ = conn.vypolnit("sudo -S -p '' true", stdin=sudo_password + "\n", timeout=20)
        sudo = "password_ok" if kod == 0 else "password_bad"
    return {"root": root, "home": polya.get("home", ""), "docker": polya.get("docker", "missing"), "sudo": sudo}


def zapustit_ustanovshchik(
    conn: Soedinenie,
    papka: str,
    fayly: dict[str, bytes],
    env: dict[str, str],
    op: str,
    na_stroku: Callable[[str], None],
    *,
    timeout: float = 3600,
) -> int:
    """Залить установщик с шаблонами в `papka`, положить ustanovka.env (0600) и запустить.

    Файл параметров установщик удаляет сразу после чтения: в нём пароли. Остатки
    прошлого оборванного запуска (SIGKILL ловушкой не поймать) убираем до заливки.
    """
    if op not in ("check", "install", "remove"):
        raise ValueError(op)
    p = shlex.quote(papka)
    kod, vyvod = conn.vypolnit(
        f"umask 077; mkdir -p {p} && chmod 700 {p} && rm -f {p}/ustanovka.env {p}/.parol.tmp", timeout=20
    )
    if kod != 0:
        raise SshBeda("ssh_unreachable", f"Cannot create {papka}: {vyvod.strip()[-200:]}")
    for imya, dannye in fayly.items():
        conn.zalit(f"{papka}/{imya}", dannye, 0o600)
    stroki = []
    for k, v in env.items():
        znachenie = str(v if v is not None else "")
        if "\n" in znachenie or "\r" in znachenie:
            raise ValueError(f"newline in {k}")
        stroki.append(f"{k}={znachenie}")
    conn.zalit(f"{papka}/ustanovka.env", ("\n".join(stroki) + "\n").encode("utf-8"), 0o600)
    komanda = f"cd {shlex.quote(papka)} && sh ./ustanovit.sh {op}"
    kod, _ = conn.vypolnit(komanda, timeout=timeout, na_stroku=na_stroku)
    return kod


# --- пул для наблюдателя: одно соединение на сервер ---

_pul: dict[tuple, Soedinenie] = {}
_pul_zamok = threading.Lock()


def iz_pula(klyuch: tuple, sozdat: Callable[[], Soedinenie]) -> Soedinenie:
    """Живое соединение из пула или новое. `klyuch` — (server_id, host, port, user, ключ хоста)."""
    with _pul_zamok:
        conn = _pul.get(klyuch)
        if conn is not None and conn.zhivo():
            return conn
        if conn is not None:
            conn.zakryt()
            _pul.pop(klyuch, None)
    novoe = sozdat()
    with _pul_zamok:
        staroe = _pul.get(klyuch)
        if staroe is not None and staroe.zhivo():
            novoe.zakryt()
            return staroe
        _pul[klyuch] = novoe
    return novoe


def vybrosit(klyuch: tuple | None = None, *, server_id: int | None = None) -> None:
    """Закрыть соединения сервера: после смены ключа или добавления в группу docker."""
    with _pul_zamok:
        for k in list(_pul):
            # не-секрет: ключ пула — номер сервера и адрес, по нему ищут, а не впускают.
            if k == klyuch or (server_id is not None and k and k[0] == server_id):
                _pul.pop(k).zakryt()


def zakryt_pul() -> None:
    with _pul_zamok:
        for conn in _pul.values():
            conn.zakryt()
        _pul.clear()
