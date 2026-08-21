"""Транспорт почты: забрать письма и отправить письмо.

Слой существует ради одной вещи — чтобы всё остальное (сопоставление с клиентом,
запись в ленту, идемпотентность) можно было проверить без сети. Логика сидит в
`mail_service`, а сюда вынесены только разговоры с сервером; в тестах на это место
подставляется подделка (`tests/test_mail.py`), и ни один тест никуда не ходит.

Настоящая реализация — на `imaplib`/`smtplib` из стандартной библиотеки:
поднимать зависимость ради двух хорошо описанных протоколов незачем.
"""

import email
import email.policy
import email.utils
import imaplib
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime

from core.utils import now_utc

# Сколько писем забираем за один заход. Первая синхронизация большого ящика иначе
# затянула бы HTTP-запрос на минуты и упёрлась бы в таймаут прокси.
FETCH_BATCH = 50
# Сеть молчит — не держим соединение вечно: синхронизацию запускают из интерфейса,
# и человек ждёт ответа.
TIMEOUT_SECONDS = 20


class MailTransportError(Exception):
    """Сервер недоступен, отказал в аутентификации или ответил не по протоколу."""


@dataclass(frozen=True)
class FetchedMessage:
    """Письмо, как его увидел транспорт. Времена уже приведены к naive UTC."""

    uid: int | None
    message_id: str
    subject: str
    from_addr: str
    to_addrs: list[str]
    sent_at: datetime
    body_text: str = ""
    body_html: str = ""
    has_attachments: bool = False
    #: Цепочка входящего письма — чтобы наш ответ продолжил её, а не начал свою.
    in_reply_to: str = ""
    references: str = ""
    #: Кому сервер отказал при отправке. Пусто — досталось всем.
    #:
    #: Полем, а не исключением, и это исправление исправления. Первая редакция
    #: бросала отказ — но письмо к этому моменту УЖЕ доставлено остальным, и
    #: записи в CRM не оставалось: человек видел ошибку, жал «отправить» снова,
    #: и получившие письмо получали его дважды. Отправка состоялась частично,
    #: и честный ответ на это — «состоялась, но не всем», а не «не состоялась».
    refused: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutgoingMessage:
    to_addrs: list[str] = field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    #: `Message-ID` письма, на которое отвечаем, и вся цепочка до него.
    #:
    #: Без них ответ приходит к человеку ОТДЕЛЬНЫМ письмом, а не под своим
    #: вопросом: почтовые клиенты собирают переписку именно по этим двум
    #: заголовкам. Проверено живьём на двух серверах — ответ вставал в ящике
    #: сам по себе, и связь с исходным письмом восстанавливал только человек.
    in_reply_to: str = ""
    references: str = ""


class MailTransport(ABC):
    """Что почтовому модулю нужно от сервера — и ничего сверх того."""

    @abstractmethod
    def check(self) -> None:
        """Проверить, что с этими настройками вход проходит. Молчит — значит всё."""

    @abstractmethod
    def fetch(self, since_uid: int | None = None, limit: int = FETCH_BATCH) -> list[FetchedMessage]:
        """Письма новее `since_uid` (None — с начала ящика)."""

    @abstractmethod
    def send(self, message: OutgoingMessage) -> FetchedMessage:
        """Отправить письмо и вернуть его же — с Message-ID, который ушёл в сеть."""


class ImapSmtpTransport(MailTransport):
    """Настоящий транспорт: IMAP на приём, SMTP на отправку."""

    def __init__(
        self,
        *,
        address: str,
        login: str,
        password: str,
        imap_host: str,
        imap_port: int,
        imap_ssl: bool,
        smtp_host: str,
        smtp_port: int,
        smtp_ssl: bool,
    ) -> None:
        self.address = address
        self.login = login
        self.password = password
        self.imap_host, self.imap_port, self.imap_ssl = imap_host, imap_port, imap_ssl
        self.smtp_host, self.smtp_port, self.smtp_ssl = smtp_host, smtp_port, smtp_ssl

    # --- IMAP ---

    def _imap(self) -> imaplib.IMAP4:
        try:
            if self.imap_ssl:
                # Контекст по умолчанию проверяет и цепочку, и имя хоста. Отдельной
                # «галочки не проверять сертификат» в настройках нет намеренно:
                # такая галочка однажды включается «на разок» и остаётся навсегда.
                connection: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    self.imap_host,
                    self.imap_port,
                    ssl_context=ssl.create_default_context(),
                    timeout=TIMEOUT_SECONDS,
                )
            else:
                connection = imaplib.IMAP4(self.imap_host, self.imap_port, timeout=TIMEOUT_SECONDS)
                connection.starttls(ssl_context=ssl.create_default_context())
            connection.login(self.login or self.address, self.password)
            return connection
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailTransportError(f"IMAP: {exc}") from exc

    def check(self) -> None:
        connection = self._imap()
        try:
            # Статус ответа проверяем САМИ. `imaplib` на «NO» исключения не
            # бросает — он возвращает ('NO', …) и остаётся в прежнем состоянии.
            # Пока статус не смотрели, проверка ящика отвечала «всё хорошо» и
            # на том сервере, где INBOX открыть нечем: человек видел зелёную
            # галочку, а письма не приходили никогда.
            status, otvet = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise MailTransportError(f"IMAP SELECT INBOX: {_kratko(otvet)}")
        except imaplib.IMAP4.error as exc:
            raise MailTransportError(f"IMAP: {exc}") from exc
        finally:
            _close_quietly(connection)
        self._smtp().quit()

    def fetch(self, since_uid: int | None = None, limit: int = FETCH_BATCH) -> list[FetchedMessage]:
        connection = self._imap()
        try:
            # readonly: синхронизация не должна ставить письмам «прочитано» в чужом
            # почтовом клиенте — человек может читать тот же ящик из телефона.
            status, otvet = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise MailTransportError(f"IMAP SELECT INBOX: {_kratko(otvet)}")
            criterion = f"{(since_uid or 0) + 1}:*"
            status, data = connection.uid("SEARCH", None, "UID", criterion)
            if status != "OK":
                raise MailTransportError(f"IMAP SEARCH: {status}")
            uids = [int(chunk) for chunk in (data[0] or b"").split()]
            # SEARCH с «N:*» на пустом хвосте возвращает последнее письмо ящика —
            # так устроен диапазон в IMAP. Отсекаем сами, иначе каждая синхронизация
            # приносила бы одно и то же письмо.
            uids = [uid for uid in uids if since_uid is None or uid > since_uid]
            messages = []
            for uid in sorted(uids)[:limit]:
                status, payload = connection.uid("FETCH", str(uid), "(RFC822)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                messages.append(parse_raw_message(payload[0][1], uid=uid))
            return messages
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailTransportError(f"IMAP: {exc}") from exc
        finally:
            _close_quietly(connection)

    # --- SMTP ---

    def _smtp(self) -> smtplib.SMTP:
        try:
            if self.smtp_ssl:
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.smtp_host,
                    self.smtp_port,
                    timeout=TIMEOUT_SECONDS,
                    context=ssl.create_default_context(),
                )
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=TIMEOUT_SECONDS)
                server.starttls(context=ssl.create_default_context())
            server.login(self.login or self.address, self.password)
            return server
        except (OSError, smtplib.SMTPException) as exc:
            raise MailTransportError(f"SMTP: {exc}") from exc

    def send(self, message: OutgoingMessage) -> FetchedMessage:
        letter = EmailMessage()
        letter["From"] = self.address
        letter["To"] = ", ".join(_odna_stroka(a) for a in message.to_addrs)
        letter["Subject"] = _odna_stroka(message.subject)
        # Message-ID генерируем сами и запоминаем: по нему письмо не задвоится,
        # когда та же переписка вернётся из ящика при следующей синхронизации.
        message_id = make_msgid(domain=self.address.split("@")[-1] or None)
        letter["Message-ID"] = message_id
        letter["Date"] = email.utils.formatdate(localtime=True)
        # Заголовки цепочки — только когда есть на что отвечать. Пустой
        # `In-Reply-To` хуже отсутствующего: часть клиентов считает его ссылкой
        # в никуда и прячет письмо из переписки вовсе.
        if message.in_reply_to:
            letter["In-Reply-To"] = _odna_stroka(message.in_reply_to)
        if message.references:
            letter["References"] = _odna_stroka(message.references)
        letter.set_content(message.body_text)

        server = self._smtp()
        try:
            otkazali = server.send_message(letter)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailTransportError(f"SMTP: {exc}") from exc
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 — соединение уже закрыто, письмо ушло
                pass

        # Отказ по ЧАСТИ адресатов — не успех, но и не провал.
        #
        # `send_message` бросает исключение, только когда отвергнуты ВСЕ; при
        # частичном отказе он молча возвращает словарь тех, кому не досталось.
        # Пока этот словарь выбрасывался, в CRM появлялась запись «отправлено»,
        # а половина адресатов письма не получала — и узнать об этом было
        # неоткуда. Замерено на двух живых серверах: письмо дошло одному из
        # двух, транспорт отчитался успехом.
        #
        # Отдаём список, а не бросаем: к этому месту письмо уже у остальных, и
        # отказ означал бы, что записи в CRM нет — человек нажмёт «отправить»
        # снова, и получившие получат его дважды. Отозвать письмо нельзя ничем.
        return FetchedMessage(
            uid=None,
            message_id=message_id,
            subject=message.subject,
            from_addr=self.address,
            to_addrs=list(message.to_addrs),
            sent_at=now_utc(),
            body_text=message.body_text,
            in_reply_to=message.in_reply_to,
            references=message.references,
            # Ключи словаря `smtplib` — адреса строкой, но приводим на всякий
            # случай: сервер отвечает байтами, и один экзотический сервер уже
            # ловился на этом в другом месте проекта.
            refused=tuple(sorted(_stroka(a) for a in otkazali)),
        )


def _stroka(znachenie) -> str:
    """Байты или строка — всегда строка."""
    if isinstance(znachenie, bytes):
        return znachenie.decode("utf-8", "replace")
    return str(znachenie)


def _odna_stroka(znachenie: str) -> str:
    """Значение заголовка без переводов строк.

    **Это защита от подделки письма, а не опрятность.** Заголовок с `\n` внутри
    дописывает в письмо ЧУЖИЕ заголовки: тема вида `Счёт\nBcc: vor@example.com`
    отправила бы копию постороннему. Библиотека такое значение отвергает, но
    отвергает `ValueError`, а он летит мимо `MailTransportError` — наружу это
    выходило пятисотой ошибкой вместо внятного отказа.
    """
    return " ".join(_stroka(znachenie).split())


def _kratko(otvet) -> str:
    """Ответ сервера строкой. `imaplib` отдаёт список байтов, и как есть он в
    сообщении об ошибке выглядит как мусор из скобок."""
    if isinstance(otvet, (list, tuple)):
        otvet = b" ".join(x for x in otvet if isinstance(x, bytes))
    if isinstance(otvet, bytes):
        otvet = otvet.decode("utf-8", "replace")
    return str(otvet)[:200]


def _close_quietly(connection: imaplib.IMAP4) -> None:
    try:
        connection.logout()
    except Exception:  # noqa: BLE001 — письма уже забраны, разрыв связи не новость
        pass


# --- разбор письма (без сети, поэтому проверяется тестами напрямую) ---

def parse_raw_message(raw: bytes, uid: int | None = None) -> FetchedMessage:
    letter = email.message_from_bytes(raw, policy=email.policy.default)

    text, html, has_attachments = "", "", False
    for part in letter.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment":
            has_attachments = True
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and not text:
            text = _part_text(part)
        elif content_type == "text/html" and not html:
            html = _part_text(part)

    from_addrs = [addr for _, addr in getaddresses(letter.get_all("From", []))]
    to_addrs = [addr for _, addr in getaddresses(letter.get_all("To", []) + letter.get_all("Cc", []))]

    return FetchedMessage(
        uid=uid,
        # Письмо без Message-ID встречается у самодельных рассылок. Придумываем
        # свой, иначе на идемпотентности такое письмо задваивалось бы каждый заход.
        message_id=(letter.get("Message-ID") or make_msgid(domain="opencrm.local")).strip(),
        subject=_decode(letter.get("Subject")),
        from_addr=from_addrs[0] if from_addrs else "",
        to_addrs=to_addrs,
        sent_at=header_date_to_utc(letter.get("Date")),
        body_text=text,
        body_html=html,
        has_attachments=has_attachments,
        # Цепочка входящего — чтобы наш ответ продолжил её, а не начал свою.
        # `References` у длинной переписки складывается из всех предыдущих
        # писем, и наш ответ обязан унести её целиком: клиент собирает ветку
        # по ней, а не по одному `In-Reply-To`.
        in_reply_to=(letter.get("In-Reply-To") or "").strip(),
        references=" ".join((letter.get("References") or "").split()),
    )


def header_date_to_utc(value: str | None) -> datetime:
    """Дата из заголовка письма → naive UTC.

    В `Date:` стоит время отправителя вместе с его смещением: «Tue, 5 Aug 2026
    09:12:00 +0900». В базе времена naive UTC, и положить туда 09:12 как есть —
    значит сдвинуть письмо на девять часов вперёд относительно заметок и звонков.
    Лента после этого перемешивается, причём молча.

    Заголовка нет или он не разбирается (бывает у спама и у кривых рассылок) —
    берём момент разбора: письмо всё равно должно встать в ленту, а не пропасть.
    """
    if not value:
        return now_utc()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return now_utc()
    if parsed is None:
        return now_utc()
    if parsed.tzinfo is None:
        # Отправитель не указал зону. По RFC это «местное время неизвестно где»;
        # считаем UTC — другого разумного предположения нет.
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 — битая кодировка в теме не повод терять письмо
        return value


def _part_text(part) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")
