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

    def run(self, argv, cwd: Path | None = None, timeout: float | None = None) -> Result:
        argv = tuple(str(part) for part in argv)
        self._log("$ " + " ".join(argv))
        try:
            done = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
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
        return Result(argv, done.returncode, done.stdout or "", done.stderr or "")


@dataclass(frozen=True)
class Response:
    status: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpProbe:
    def __init__(self, timeout: float = 10.0, opener=None) -> None:
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def get(self, url: str) -> Response:
        request = urllib.request.Request(url)
        request.add_header("User-Agent", "opencrm-autoupdate-healthcheck")
        try:
            with self._open(request, timeout=self.timeout) as response:
                return Response(response.status, response.read(4096).decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            return Response(error.code, "")
        except OSError as error:
            # Сервер ещё поднимается — это ожидаемое состояние, не исключение.
            return Response(0, str(error))
