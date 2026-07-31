"""Сообщение об исходе обновления в Telegram.

Текст отправляется без `parse_mode`: в сообщение попадает заголовок коммита, а
там сплошь `_`, `*` и backticks — с разметкой Telegram отбивал бы такие
сообщения ошибкой, и об упавшем деплое никто бы не узнал.

Не настроен токен — молчим. Уведомление приятно, но обновление не должно
зависеть от доступности мессенджера.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"


class Silent:
    """Заглушка: канал не настроен."""

    configured = False

    def send(self, text: str) -> bool:  # noqa: ARG002
        return False


class Telegram:
    configured = True

    def __init__(self, token: str, chat_id: str, opener=None, timeout: float = 10.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def send(self, text: str) -> bool:
        payload = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text[:4000],  # лимит Telegram — 4096 символов
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(f"{API}/bot{self.token}/sendMessage", data=payload)
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._open(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except (urllib.error.HTTPError, OSError):
            return False


def from_config(config, opener=None):
    if config.telegram_token and config.telegram_chat_id:
        return Telegram(config.telegram_token, config.telegram_chat_id, opener=opener)
    return Silent()
