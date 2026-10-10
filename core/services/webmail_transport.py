"""Транспорт зеркала для ящиков своего сервера (docs/bloki/32-pochtovyy-server.md §8).

Зеркало писем и лента клиента ходят через `mail_transport.ImapSmtpTransport`: тот
входит паролем ящика и сверяет сертификат с адресом соединения. У ящика своего
сервера пароля в базе нет (вход мастером), а адрес соединения на этой машине —
имя контейнера, не совпадающее с сертификатом. Здесь меняется только вход —
разбор писем, отправка и все её проверки остаются прежними.
"""

import imaplib
import smtplib

from core.services import webmail_imap
from core.services.mail_transport import TIMEOUT_SECONDS, ImapSmtpTransport, MailTransportError


class ServerTransport(ImapSmtpTransport):
    def __init__(self, n: webmail_imap.Nastroyki) -> None:
        super().__init__(
            address=n.address,
            login=n.login,
            password=n.password,
            imap_host=n.imap_host,
            imap_port=n.imap_port,
            imap_ssl=True,
            smtp_host=n.smtp_host,
            smtp_port=n.smtp_port,
            smtp_ssl=True,
        )
        self.tls_name = n.tls_name
        self.tls_pin = n.tls_pin

    def _imap(self) -> imaplib.IMAP4:
        try:
            connection = webmail_imap._Imap4Tls(
                self.imap_host,
                self.imap_port,
                tls_name=self.tls_name,
                tls_pin=self.tls_pin,
                timeout=TIMEOUT_SECONDS,
            )
            connection.login(self.login, self.password)
            return connection
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailTransportError(f"IMAP: {exc}") from exc

    def _smtp(self) -> smtplib.SMTP:
        try:
            server = webmail_imap._SmtpTls(
                self.smtp_host,
                self.smtp_port,
                tls_name=self.tls_name,
                tls_pin=self.tls_pin,
                timeout=TIMEOUT_SECONDS,
            )
            server.login(self.login, self.password)
            return server
        except (OSError, smtplib.SMTPException) as exc:
            raise MailTransportError(f"SMTP: {exc}") from exc
