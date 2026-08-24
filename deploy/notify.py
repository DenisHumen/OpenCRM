"""Сообщение об исходе обновления в Telegram.

--------------------------------------------------------------------------
Почему HTML, а не MarkdownV2, и почему разметка вообще вернулась
--------------------------------------------------------------------------

Разметка тут когда-то была выключена целиком, и повод был настоящий: в текст
попадает заголовок коммита, а там сплошь `_`, `*` и backticks. В MarkdownV2
экранировать надо ВОСЕМНАДЦАТЬ символов (``_*[]()~`>#+-=|{}.!``), любой
пропущенный — и Telegram отбивает сообщение целиком с `can't parse entities`.
То есть об упавшем деплое не узнаёт никто именно потому, что сообщение было
подробным. Лечение тогда выбрали грубое — снять разметку совсем.

В HTML экранировать надо ТРИ символа: `&`, `<`, `>`. Ни один из них не
встречается ни в заголовках коммитов, ни в путях, ни в командах — а если
встретится, `_ekranirovat` его переведёт. Поэтому причина, по которой разметку
снимали, в HTML не действует.

**И всё равно есть запасной путь.** Telegram отбивает сообщение по разбору
разметки кодом 400. Получив его, отправляем то же самое ПЛОСКИМ текстом, без
`parse_mode`. Свойство, ради которого это написано, простое: ошибка в
оформлении не имеет права заглушить сообщение об аварии. Красивое сообщение —
удобство, дошедшее — необходимость.

Не настроен токен — молчим. Уведомление приятно, но обновление не должно
зависеть от доступности мессенджера.
"""

from __future__ import annotations

import html
import json
import re
import uuid
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"

#: Предел Telegram — 4096 символов, и считает он их ПОСЛЕ разбора разметки.
#: Берём с запасом: у обрезанного по живому тегу сообщения разметка не
#: разбирается, и оно отбивается целиком.
PREDEL = 3800

#: Теги, которые снимаем при откате на плоский текст.
_TEG = re.compile(r"<[^>]+>")


def ekranirovat(text: str) -> str:
    """Текст, пригодный для вставки в HTML-разметку Telegram.

    Через `html.escape` с `quote=False`: кавычки внутри текста сообщения
    экранировать не нужно (мы не строим атрибуты из пользовательских строк), а
    `&quot;` в заголовке коммита читался бы как мусор.
    """
    return html.escape(text or "", quote=False)


def bez_razmetki(text: str) -> str:
    """То же сообщение, но плоским текстом — для запасного пути."""
    return html.unescape(_TEG.sub("", text))


class Silent:
    """Заглушка: канал не настроен."""

    configured = False

    def send(self, text: str, *, tiho: bool = False) -> bool:  # noqa: ARG002
        return False

    def send_document(  # noqa: ARG002
        self, imya: str, soderzhimoe: bytes, podpis: str = "", *, tiho: bool = False
    ) -> bool:
        return False


#: Предел телеграма на файл от бота — 50 МиБ. Наши отчёты весят меньше мегабайта
#: (из них семьсот килобайт — вшитый шрифт), но проверка стоит здесь, а не «мы же
#: знаем»: журнал однажды вырастет, и упереться в отказ телеграма лучше заранее и
#: с внятной записью, чем молчаливой неудачей отправки.
PREDEL_FAYLA = 50 * 1024 * 1024


class Telegram:
    configured = True

    def __init__(self, token: str, chat_id: str, opener=None, timeout: float = 10.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def send(self, text: str, *, tiho: bool = False) -> bool:
        """Отправить сообщение. `tiho` — без звука на телефоне.

        Беззвучно уходит то, что читают утром: удачное обновление ночью — не
        повод будить. Всё, что требует человека, звучит.
        """
        text = text[:PREDEL]
        if self._otpravit(text, html_razmetka=True, tiho=tiho):
            return True
        # Разметка не разобралась (или Telegram передумал) — то же самое, но
        # плоским текстом. Молчание здесь было бы худшим из исходов.
        return self._otpravit(bez_razmetki(text), html_razmetka=False, tiho=tiho)

    def _otpravit(self, text: str, *, html_razmetka: bool, tiho: bool) -> bool:
        polya = {
            "chat_id": self.chat_id,
            "text": text,
            # `link_preview_options` вместо устаревшего
            # `disable_web_page_preview`: ссылка на коммит развернулась бы
            # карточкой репозитория на пол-экрана и утопила бы сам текст.
            "link_preview_options": json.dumps({"is_disabled": True}),
        }
        if html_razmetka:
            polya["parse_mode"] = "HTML"
        if tiho:
            polya["disable_notification"] = "true"
        payload = urllib.parse.urlencode(polya).encode("utf-8")
        request = urllib.request.Request(f"{API}/bot{self.token}/sendMessage", data=payload)
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._open(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except (urllib.error.HTTPError, OSError):
            return False

    def send_document(
        self, imya: str, soderzhimoe: bytes, podpis: str = "", *, tiho: bool = False
    ) -> bool:
        """Приложить файл к переписке. `True` — телеграм принял.

        Форма собирается руками, потому что в `urllib` многочастной отправки
        нет, а тянуть ради неё `requests` в пакет, который работает на хосте
        системным питоном без venv, нельзя (разбор — в шапке
        `deploy/dokumenty.py`). Кода здесь на двадцать строк, и он не меняется.

        Отдельным таймаутом: файл на сотни килобайт уходит дольше строки текста,
        и десяти секунд, которых хватает сообщению, здесь мало. Не уложились —
        возвращаем `False`, а не роняем обновление: файл к отчёту приятен, но
        сообщение владельцу важнее, а работающий сайт важнее их обоих.
        """
        if not soderzhimoe or len(soderzhimoe) > PREDEL_FAYLA:
            return False

        granica = "----OpenCRMOtchyot" + uuid.uuid4().hex
        chasti: list[bytes] = []

        def pole(imya_polya: str, znachenie: str) -> None:
            chasti.append(
                f"--{granica}\r\n"
                f'Content-Disposition: form-data; name="{imya_polya}"\r\n\r\n'
                f"{znachenie}\r\n".encode("utf-8")
            )

        pole("chat_id", self.chat_id)
        if podpis:
            # Подпись телеграм режет на 1024 знаках — режем сами, иначе он
            # отвергнет весь запрос, и файл не уйдёт вовсе.
            pole("caption", podpis[:1024])
            pole("parse_mode", "HTML")
        if tiho:
            pole("disable_notification", "true")

        chasti.append(
            f"--{granica}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{imya}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        )
        chasti.append(soderzhimoe)
        chasti.append(f"\r\n--{granica}--\r\n".encode("utf-8"))

        telo = b"".join(chasti)
        request = urllib.request.Request(f"{API}/bot{self.token}/sendDocument", data=telo)
        request.add_header("Content-Type", f"multipart/form-data; boundary={granica}")
        request.add_header("Content-Length", str(len(telo)))
        try:
            with self._open(request, timeout=max(self.timeout, 60.0)) as response:
                return 200 <= response.status < 300
        except (urllib.error.HTTPError, OSError):
            return False


def from_config(config, opener=None):
    if config.telegram_token and config.telegram_chat_id:
        return Telegram(config.telegram_token, config.telegram_chat_id, opener=opener)
    return Silent()
