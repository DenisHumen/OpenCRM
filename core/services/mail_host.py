"""Разговор с агентом внутри контейнера почтового сервера (docs/bloki/32-pochtovyy-server.md).

Один вызов — одна операция агента (`core/pochta/agent.py`): `ping`, `accounts`,
`account_add`… Как он доходит, решает режим сервера: на этой машине — HTTP по
сети compose, на другой — `docker exec` через SSH. Вызывающему всё равно: он
получает `data` ответа или `MailHostError` с кодом.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import httpx

from core.security import secretbox
from database.models import MailServer
from database.models.mail_server import MODE_LOCAL, MODE_SSH

AGENT_PORT = 8790
AGENT_PUT = Path(__file__).resolve().parent.parent / "pochta" / "agent.py"
#: Чем звать docker на машине — только из этого списка: строка уходит в команду SSH.
DOCKER_KOMANDY = ("docker", "sudo -n docker")
KONTEYNER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
OPERATSIYA = re.compile(r"^[a-z_]{2,32}$")
#: `python3 -c` по SSH: первая строка stdin — base64(JSON) аргументов, остальное —
#: текст агента. Аргументы в argv нельзя: там пароли, а argv видно в `ps` машины.
ZAGRUZCHIK = (
    "import sys;"
    "a=sys.stdin.buffer.readline();"
    "g={\"__name__\":\"opencrm_agent\"};"
    "exec(compile(sys.stdin.buffer.read(),\"agent.py\",\"exec\"),g);"
    "sys.exit(g[\"po_stdin\"](sys.argv[1],a))"
)

# Назначения secretbox — те же, что в mail_server_service (там их реестр для MESTA);
# продублированы, чтобы транспорт не тянул сервис ящиков.
SSH_KEY_PURPOSE = "mail-server-ssh-key"
AGENT_PURPOSE = "mail-server-agent"


class MailHostError(Exception):
    """Агент недоступен или отказал. `code` — для экрана и для подсказки «как исправить»."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class MailHost(Protocol):
    def vyzvat(self, op: str, args: dict | None = None, *, timeout: float = 60) -> dict:
        """Выполнить операцию агента и вернуть её `data`; отказ — `MailHostError`."""
        ...


@lru_cache(maxsize=1)
def tekst_agenta() -> bytes:
    return AGENT_PUT.read_bytes()


def _otvet(stroka: str) -> dict:
    """Ответ агента — последняя строка JSON вывода; до неё может быть мусор docker/sudo."""
    for kandidat in reversed([s for s in (stroka or "").splitlines() if s.strip()]):
        kandidat = kandidat.strip()
        if kandidat.startswith("{"):
            try:
                return json.loads(kandidat)
            except ValueError:
                continue
    raise MailHostError("mail_agent_failed", f"Agent gave no answer: {(stroka or '').strip()[-300:]}")


def _data(otvet: dict) -> dict:
    if otvet.get("ok"):
        return otvet.get("data") or {}
    raise MailHostError(str(otvet.get("code") or "mail_agent_failed"), str(otvet.get("message") or ""))


def _proverit_op(op: str) -> None:
    if not OPERATSIYA.match(op or ""):
        raise MailHostError("mail_agent_failed", "bad operation name")


class AgentPoHttp:
    """Сервер на этой машине: агент слушает 8790 в сети compose, наружу не опубликован."""

    def __init__(self, server: MailServer):
        self.host = server.container or "opencrm-mail"
        self._token_enc = server.agent_token_encrypted

    def vyzvat(self, op, args=None, *, timeout=60):
        _proverit_op(op)
        if not self._token_enc:
            raise MailHostError("mail_agent_unreachable", "Agent token is not set: install or connect the server")
        try:
            token = secretbox.decrypt(self._token_enc, AGENT_PURPOSE)
        except secretbox.SecretBoxError as beda:
            raise MailHostError("mail_agent_unreachable", "Agent token cannot be decrypted") from beda
        try:
            r = httpx.post(
                f"http://{self.host}:{AGENT_PORT}/v1/{op}",
                json=args or {},
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        except httpx.HTTPError as beda:
            raise MailHostError("mail_agent_unreachable", f"Mail server agent does not answer: {beda}") from beda
        if r.status_code == 401:
            raise MailHostError("mail_agent_unreachable", "Mail server agent refused the token")
        try:
            return _data(r.json())
        except ValueError as beda:
            raise MailHostError("mail_agent_failed", f"Agent answered HTTP {r.status_code}") from beda


class AgentPoSsh:
    """Сервер на другой машине: аргументы и агент едут на stdin `docker exec -i … python3 -c`."""

    def __init__(self, server: MailServer):
        self.server_id = server.id
        self.host = server.ssh_host
        self.port = server.ssh_port or 22
        self.user = server.ssh_user
        self.host_key = server.ssh_host_key
        self.key_enc = server.ssh_key_encrypted
        self.docker = server.docker_cmd or "docker"
        self.container = server.container or "opencrm-mail"

    def _soedinenie(self):
        from core.services import mail_ssh

        if not self.key_enc:
            raise MailHostError("mail_agent_unreachable", "SSH key is not installed yet")
        try:
            klyuch = secretbox.decrypt(self.key_enc, SSH_KEY_PURPOSE)
        except secretbox.SecretBoxError as beda:
            raise MailHostError("mail_agent_unreachable", "SSH key cannot be decrypted") from beda

        def sozdat():
            return mail_ssh.klient().podklyuchit(
                self.host, self.port, self.user, host_key_line=self.host_key, private_key=klyuch, timeout=10
            )

        try:
            return mail_ssh.iz_pula((self.server_id, self.host, self.port, self.user, self.host_key), sozdat)
        except mail_ssh.SshBeda as beda:
            code = beda.code if beda.code == "ssh_host_key_changed" else "mail_agent_unreachable"
            raise MailHostError(code, beda.message) from beda

    def vyzvat(self, op, args=None, *, timeout=60):
        from core.services import mail_ssh

        _proverit_op(op)
        if self.docker not in DOCKER_KOMANDY or not KONTEYNER.match(self.container):
            raise MailHostError("mail_agent_failed", "Bad docker command or container name")
        b64 = base64.b64encode(json.dumps(args or {}).encode("utf-8"))
        komanda = f"{self.docker} exec -i {self.container} python3 -c {shlex.quote(ZAGRUZCHIK)} {op}"
        conn = self._soedinenie()
        try:
            kod, vyvod = conn.vypolnit(komanda, stdin=b64 + b"\n" + tekst_agenta(), timeout=timeout)
        except mail_ssh.SshBeda as beda:
            mail_ssh.vybrosit(server_id=self.server_id)
            raise MailHostError("mail_agent_unreachable", beda.message) from beda
        except Exception as beda:  # noqa: BLE001 — оборванный канал: соединение больше не годится
            mail_ssh.vybrosit(server_id=self.server_id)
            raise MailHostError("mail_agent_unreachable", f"SSH channel failed: {beda}") from beda
        if kod != 0 and "{" not in vyvod:
            tekst = vyvod.strip()[-300:]
            if "No such container" in tekst or "is not running" in tekst:
                raise MailHostError("mail_agent_unreachable", f"Mail server container is not running: {tekst}")
            if "permission denied" in tekst.lower():
                raise MailHostError("ssh_sudo_needed", f"No access to docker: {tekst}")
            raise MailHostError("mail_agent_failed", tekst or f"exit code {kod}")
        return _data(_otvet(vyvod))


def _po_umolchaniyu(server: MailServer) -> MailHost:
    if server.mode == MODE_LOCAL:
        return AgentPoHttp(server)
    if server.mode == MODE_SSH:
        return AgentPoSsh(server)
    raise MailHostError("mail_server_mode_bad", f"Unknown server mode {server.mode!r}")


_factory: Callable[[MailServer], MailHost] = _po_umolchaniyu


def host_dlya(server: MailServer) -> MailHost:
    return _factory(server)


def set_host_factory(factory: Callable[[MailServer], MailHost]) -> None:
    """Шов для тестов: сеть в наборе запрещена, агент подменяется целиком."""
    global _factory
    _factory = factory


def reset_host_factory() -> None:
    global _factory
    _factory = _po_umolchaniyu
