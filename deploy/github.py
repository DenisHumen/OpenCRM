"""Какой коммит сейчас в отслеживаемой ветке.

Опрос, а не webhook: чтобы задеплоить, снаружи ничего дёргать не нужно, поэтому
нет ни публичного эндпоинта, ни секрета подписи, ни риска, что деплой запустит
любой, кто узнал URL. Заодно работает за NAT — сайт в локальной сети (см.
docs/08-deployment.md) обновляется так же, как боевой.

Запрос дешёвый вдвойне: `Accept: …github.sha` возвращает 40 байт вместо
JSON-простыни коммита, а `If-None-Match` с прошлым ETag превращает «ничего не
изменилось» в пустой 304 — при опросе раз в пять минут это 12 запросов в час
против лимита GitHub в 60 для анонимного клиента.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "https://api.github.com"
USER_AGENT = "opencrm-autoupdate"


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class Head:
    """Голова ветки. `changed=False` — GitHub ответил 304, `sha` неизвестен."""

    sha: str
    etag: str
    changed: bool


class GitHub:
    def __init__(self, repo: str, token: str = "", opener=None, timeout: float = 15.0) -> None:
        self.repo = repo
        self.token = token
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def head(self, branch: str, etag: str = "") -> Head:
        request = self._request(
            f"{API}/repos/{self.repo}/commits/{branch}",
            accept="application/vnd.github.sha",
        )
        # Заголовки HTTP — latin-1, и порченый ETag из состояния уронил бы опрос
        # UnicodeEncodeError'ом навсегда: демон не смог бы даже узнать, что в
        # ветке. Нечитаемый ETag — просто отсутствующий ETag.
        if etag and etag.isascii():
            request.add_header("If-None-Match", etag)
        try:
            with self._open(request, timeout=self.timeout) as response:
                sha = response.read().decode("utf-8", "replace").strip()
                fresh = response.headers.get("ETag", "")
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return Head(sha="", etag=etag, changed=False)
            raise GitHubError(f"GitHub ответил {error.code} на голову ветки {branch}") from error
        except OSError as error:
            raise GitHubError(f"GitHub недоступен: {error}") from error

        if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
            raise GitHubError(f"вместо SHA пришло {sha[:80]!r}")
        return Head(sha=sha, etag=fresh, changed=True)

    def summary(self, sha: str) -> str:
        """Первая строка сообщения коммита — для человека в уведомлении.

        Косметика: если GitHub не ответил, обновление всё равно должно поехать.
        """
        try:
            request = self._request(f"{API}/repos/{self.repo}/commits/{sha}")
            with self._open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            message = str(payload.get("commit", {}).get("message", ""))
            return message.strip().splitlines()[0] if message.strip() else ""
        except (OSError, ValueError, KeyError, IndexError):
            return ""

    def _request(self, url: str, accept: str = "application/vnd.github+json"):
        request = urllib.request.Request(url)
        request.add_header("Accept", accept)
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        return request
