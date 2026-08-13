"""Внешний мир обновления: запуск команд и HTTP-проверки.

Вынесено отдельным тонким слоем ровно затем, чтобы `updater` можно было
прогнать в тестах целиком — вместе с откатом — не собирая образ и не поднимая
сервер. Всё, что в `updater` выходит наружу, проходит через эти два класса.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return (self.out + "\n" + self.err).strip()

    def tail(self, lines: int = 12) -> str:
        """Хвост вывода — в отчёт: целиком лог сборки в Telegram не влезет."""
        return "\n".join(self.text.splitlines()[-lines:])


class Shell:
    def __init__(self, log=None) -> None:
        self._log = log or (lambda _message: None)

    def run(
        self,
        argv,
        cwd: Path | None = None,
        timeout: float | None = None,
        stdin: Path | None = None,
    ) -> Result:
        """`stdin` — файл на вход команде.

        Нужен ровно для одного: вернуть базу MySQL из дампа. Клиент `mysql`
        живёт в образе базы, а не приложения, и штатный способ залить дамп —
        отдать его этому клиенту на вход (`compose exec -T db … < файл`), ровно
        как это описано в шапке `scripts/snapshot_db.py`. Читать гигабайтный
        дамп в память ради `input=` нельзя, поэтому именно файл.
        """
        argv = tuple(str(part) for part in argv)
        self._log("$ " + " ".join(argv) + (f" < {stdin}" if stdin else ""))
        istochnik = None
        try:
            if stdin is not None:
                istochnik = open(stdin, "rb")  # noqa: SIM115 — закрываем в finally
            done = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                stdin=istochnik,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return Result(argv, 124, "", f"команда не уложилась в {timeout} c")
        except OSError as error:
            return Result(argv, 127, "", str(error))
        finally:
            if istochnik is not None:
                istochnik.close()
        return Result(argv, done.returncode, done.stdout or "", done.stderr or "")


@dataclass(frozen=True)
class Response:
    status: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def alive(self) -> bool:
        """Сервер ответил осмысленно — включая перенаправление.

        Для smoke-теста редирект — такой же признак жизни, как страница: его
        выдаёт настроенный и работающий nginx.
        """
        return 200 <= self.status < 400


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Опенер, который не идёт по перенаправлению, а возвращает его как ответ."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


class HttpProbe:
    def __init__(self, timeout: float = 10.0, opener=None) -> None:
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        # Отдельный опенер для проверок, которым нельзя идти по редиректу.
        # Подменённый снаружи (в тестах) используется как есть — иначе тест
        # проверял бы не тот код, который работает на сервере.
        self._open_here = opener or urllib.request.build_opener(_NoRedirect).open

    def get(self, url: str, follow: bool = True) -> Response:
        """`follow=False` — не ходить по перенаправлению.

        Нужно для проверок с самой машины. Сайт на HTTPS отвечает на
        http://127.0.0.1/ перенаправлением на https://127.0.0.1/, а сертификат
        выписан на домен и к IP-адресу не подходит — пойти по такому редиректу
        значит гарантированно упереться в ошибку проверки сертификата. Именно
        это и роняло smoke-тест деплоя после переезда на HTTPS.
        """
        request = urllib.request.Request(url)
        request.add_header("User-Agent", "opencrm-autoupdate-healthcheck")
        opener = self._open if follow else self._open_here
        try:
            with opener(request, timeout=self.timeout) as response:
                return Response(response.status, response.read(4096).decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            return Response(error.code, "")
        except OSError as error:
            # Сервер ещё поднимается — это ожидаемое состояние, не исключение.
            return Response(0, str(error))
