"""Жизнь почтового сервера: черновик, SSH, проверка, установка, подключение, удаление.

Разбор — docs/bloki/32-pochtovyy-server.md. Работа (`MailJob`) идёт в потоке
процесса: на этой машине — через просьбу обновлятору (`mail_spool`), на другой —
заливкой `core/pochta/ustanovit.sh` по SSH. Протокол установщика (`##STEP`,
`##OK`, `##FAIL`…) разбирается здесь в шаги, сырой журнал копится целиком:
«что пошло не так» спрашивают и через неделю. На каждый код беды — инструкция
«как исправить» (`HINTS`), простыми шагами на двух языках.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from email.message import EmailMessage
from email.utils import make_msgid

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.security import secretbox
from core.services import mail_dns, mail_server_service, mail_spool, mail_ssh
from core.utils import is_valid_email, now_utc
from database.models import MailAccount, MailDomain, MailJob, MailServer, User
from database.models.mail_server import (
    DEFAULT_IMAGE,
    JOB_ADOPT,
    JOB_CHECK,
    JOB_FAILED,
    JOB_INSTALL,
    JOB_OK,
    JOB_REMOVE,
    JOB_ROLLED_BACK,
    JOB_RUNNING,
    MODE_LOCAL,
    MODE_SSH,
    SERVER_MODES,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INSTALLING,
    STATUS_READY,
)
from database.repositories import mail_jobs as jobs_repo
from database.repositories import mail_server as repo
from database.session import SessionLocal

logger = logging.getLogger("opencrm.mail")

HOSTNAME = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
SSH_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
PORTY = "25 465 587 993"
#: Отметка живости работы старше этого — процесс умер, работа прервана.
SERDTSE_USTARELO = 90
#: Журнал в базе — до 2 МБ; дальше пишутся только строки протокола.
ZHURNAL_MAX = 2 * 1024 * 1024
#: Обновлятор не забрал просьбу за это время — не ждём дальше.
ZHDAT_OBNOVLYATOR = 30 * 60
UDALENNAYA_PAPKA = ".opencrm-mail-setup"
PAPKA_POCHTY = "opencrm-mail"

# --- «как исправить» ---


def _h(title_ru: str, title_en: str, ru: list[str], en: list[str]) -> dict:
    return {"title": {"ru": title_ru, "en": title_en}, "steps": {"ru": ru, "en": en}}


HINTS: dict[str, dict] = {
    "os_unsupported": _h(
        "Система не подходит для автоматической установки",
        "This system is not supported for automatic install",
        ["Автоматически Docker ставится только на Debian и Ubuntu.",
         "Либо поставьте Docker и docker compose вручную (docs.docker.com/engine/install) и запустите проверку снова,",
         "либо возьмите сервер с Ubuntu 22.04/24.04 или Debian 12/13 — это самый простой путь."],
        ["Docker is installed automatically only on Debian and Ubuntu.",
         "Either install Docker and docker compose by hand (docs.docker.com/engine/install) and run the check again,",
         "or use a server with Ubuntu 22.04/24.04 or Debian 12/13 — the easiest way."],
    ),
    "no_privileges": _h(
        "Не хватает прав на сервере",
        "Not enough rights on the server",
        ["Подключитесь пользователем root или пользователем с sudo.",
         "Либо добавьте пользователя в группу docker: sudo usermod -aG docker ИМЯ, затем перезайдите.",
         "Вернитесь на шаг SSH и подключитесь заново."],
        ["Connect as root or as a user with sudo.",
         "Or add the user to the docker group: sudo usermod -aG docker NAME, then log in again.",
         "Go back to the SSH step and connect again."],
    ),
    "sudo_password_needed": _h(
        "Нужен пароль sudo",
        "The sudo password is needed",
        ["Пользователю нужен sudo, а sudo спрашивает пароль.",
         "Вернитесь на шаг SSH, введите пароль sudo и отметьте «запомнить пароль sudo».",
         "Или выполните на сервере: sudo usermod -aG docker ИМЯ — тогда пароль не понадобится."],
        ["The user needs sudo, and sudo asks for a password.",
         "Go back to the SSH step, enter the sudo password and tick “remember the sudo password”.",
         "Or run on the server: sudo usermod -aG docker NAME — then no password is needed."],
    ),
    "docker_missing": _h(
        "На сервере нет Docker",
        "Docker is not installed on the server",
        ["Отметьте «Установить Docker автоматически» и запустите установку — это займёт пару минут.",
         "Или поставьте вручную: curl -fsSL https://get.docker.com | sudo sh",
         "Если Docker стоит, но не отвечает: sudo systemctl start docker."],
        ["Tick “Install Docker automatically” and start the install — it takes a couple of minutes.",
         "Or install by hand: curl -fsSL https://get.docker.com | sudo sh",
         "If Docker is installed but silent: sudo systemctl start docker."],
    ),
    "docker_install_failed": _h(
        "Docker не установился",
        "Docker did not install",
        ["Посмотрите журнал ниже: обычно мешает занятый apt (идёт обновление системы) или нет интернета.",
         "Подождите 5 минут и запустите установку снова.",
         "Или поставьте Docker вручную: curl -fsSL https://get.docker.com | sudo sh"],
        ["See the log below: usually apt is busy (system update running) or there is no internet.",
         "Wait 5 minutes and start the install again.",
         "Or install Docker by hand: curl -fsSL https://get.docker.com | sudo sh"],
    ),
    "compose_missing": _h(
        "Нет плагина docker compose",
        "The docker compose plugin is missing",
        ["Выполните на сервере: sudo apt-get install -y docker-compose-plugin",
         "Проверьте: docker compose version — должна быть версия 2 или новее.",
         "Запустите проверку снова."],
        ["Run on the server: sudo apt-get install -y docker-compose-plugin",
         "Check: docker compose version — it must be version 2 or newer.",
         "Run the check again."],
    ),
    "port_busy": _h(
        "Почтовый порт уже занят",
        "A mail port is already in use",
        ["Почте нужны порты 25, 465, 587 и 993 — их занимает другая программа (см. журнал: кто именно).",
         "Посмотреть: sudo ss -ltnp | grep -E ':(25|465|587|993)\\b'",
         "Остановите эту программу (например, sudo systemctl disable --now postfix) и запустите установку снова.",
         "Если это уже установленный почтовый сервер — выберите «Подключить существующий»."],
        ["Mail needs ports 25, 465, 587 and 993 — another program holds one of them (the log says which).",
         "See: sudo ss -ltnp | grep -E ':(25|465|587|993)\\b'",
         "Stop that program (e.g. sudo systemctl disable --now postfix) and start the install again.",
         "If it is a mail server you already have — choose “Connect existing”."],
    ),
    "low_memory": _h(
        "Мало памяти",
        "Not enough memory",
        ["Почтовому серверу с защитой от спама нужен 1 ГБ памяти и файл подкачки (swap) 2 ГБ; меньше 1 ГБ — не хватит, возьмите тариф побольше.",
         "При входе root или с sudo установщик создаёт swap сам. Если не вышло — выполните на сервере: sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile",
         "Чтобы swap пережил перезагрузку: echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab",
         "Запустите проверку снова."],
        ["A mail server with spam protection needs 1 GB of memory and a 2 GB swap file; under 1 GB is not enough, take a bigger plan.",
         "Logged in as root or with sudo, the installer creates the swap itself. If it did not — run on the server: sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile",
         "To keep swap after a reboot: echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab",
         "Run the check again."],
    ),
    "low_disk": _h(
        "Мало места на диске",
        "Not enough disk space",
        ["Нужно хотя бы 2 ГБ свободного места, а письма с вложениями быстро его съедают.",
         "Почистите старое: docker system prune (осторожно: удалит остановленные контейнеры) или увеличьте диск.",
         "Запустите проверку снова."],
        ["At least 2 GB must be free, and mail with attachments eats it fast.",
         "Clean up: docker system prune (careful: removes stopped containers) or grow the disk.",
         "Run the check again."],
    ),
    "egress25_blocked": _h(
        "Хостинг закрыл исходящие порты почты",
        "Your hosting blocks outgoing mail ports",
        ["Принимать письма сервер сможет, а отправлять другим серверам — нет: Linode (Akamai), DigitalOcean, Vultr, Hetzner и многие облака на новых аккаунтах закрывают исходящие 25, 465 и 587 от спама.",
         "Linode: Help & Support → Open New Ticket, тема «SMTP restriction removal»; опишите почтовый сервер для своего домена и что A, MX, SPF, DKIM и PTR настроены. Обычно открывают за день.",
         "У других хостингов — так же, тикетом в поддержку: «откройте исходящий порт 25 для почтового сервера».",
         "Пока порт закрыт — задайте посредника (relay) в карточке сервера: SMTP вашего провайдера, Mailgun, SendGrid, Brevo и т. п. (обычно порт 587 — он бывает открыт, даже когда 25 закрыт). Установку можно продолжать."],
        ["The server can receive mail but cannot send to other servers: Linode (Akamai), DigitalOcean, Vultr, Hetzner and many clouds block outgoing 25, 465 and 587 on new accounts.",
         "Linode: Help & Support → Open New Ticket, subject “SMTP restriction removal”; describe the mail server for your domain and that A, MX, SPF, DKIM and PTR are set. Usually opened within a day.",
         "Other hosts work the same way: open a support ticket “please open outgoing port 25 for a mail server”.",
         "While the port is closed — set a relay in the server card: your provider's SMTP, Mailgun, SendGrid, Brevo, etc. (usually port 587, often open when 25 is not). You can go on with the install."],
    ),
    "port80_busy": _h(
        "Порт 80 занят — сертификат будет самоподписанным",
        "Port 80 is busy — the certificate will be self-signed",
        ["Let's Encrypt проверяет имя сервера через порт 80, а его занимает другая программа (обычно сайт: nginx или apache).",
         "Почта будет работать с самоподписанным сертификатом: CRM доверяет ему по отпечатку, а телефон и почтовые программы спросят, доверять ли.",
         "Чтобы сертификат выпустился сам, освободите порт 80 (например, sudo systemctl stop nginx) — служба certbot пробует раз в час."],
        ["Let's Encrypt checks the server name over port 80, and another program holds it (usually a website: nginx or apache).",
         "Mail works with a self-signed certificate: the CRM trusts it by fingerprint, phones and mail apps will ask whether to trust it.",
         "For a real certificate to come by itself, free port 80 (e.g. sudo systemctl stop nginx) — the certbot service tries every hour."],
    ),
    "tls_selfsigned": _h(
        "Пока самоподписанный сертификат",
        "A self-signed certificate for now",
        ["Let's Encrypt не выдал сертификат: чаще всего A-запись имени сервера ещё не указывает на его IP или ещё не разошлась (5–30 минут).",
         "Почта уже работает: CRM доверяет сертификату по отпечатку. Телефон и почтовые программы спросят, доверять ли ему.",
         "Проверьте A-запись (кнопка «Проверить DNS») и что порт 80 открыт в брандмауэре хостинга. Настоящий сертификат служба certbot выпустит сама в течение часа — переустанавливать ничего не нужно."],
        ["Let's Encrypt did not issue a certificate: most often the A record of the server name does not point to its IP yet or has not propagated (5–30 minutes).",
         "Mail already works: the CRM trusts the certificate by fingerprint. Phones and mail apps will ask whether to trust it.",
         "Check the A record (the “Check DNS” button) and that port 80 is open in the hosting firewall. The certbot service issues a real certificate by itself within an hour — nothing to reinstall."],
    ),
    "mta_running": _h(
        "На машине уже работает другой почтовый сервер",
        "Another mail server is running on the machine",
        ["Обычно это postfix или exim, поставленные вместе с системой.",
         "Если он не нужен: sudo systemctl disable --now postfix (или exim4).",
         "Запустите проверку снова."],
        ["Usually it is postfix or exim that came with the system.",
         "If you do not need it: sudo systemctl disable --now postfix (or exim4).",
         "Run the check again."],
    ),
    "dms_exists": _h(
        "Почтовый сервер здесь уже стоит",
        "A mail server is already installed here",
        ["Ставить второй поверх нельзя — письма первого потерялись бы.",
         "Нажмите «Подключить существующий»: CRM найдёт ящики и подтянет их.",
         "Если старый сервер не нужен — удалите его контейнер вручную и запустите установку снова."],
        ["Installing a second one on top is not possible — mail of the first would be lost.",
         "Press “Connect existing”: the CRM finds the mailboxes and imports them.",
         "If the old server is not needed — remove its container by hand and install again."],
    ),
    "dms_not_found": _h(
        "Почтовый сервер не найден",
        "No mail server found",
        ["На машине нет контейнера docker-mailserver.",
         "Если почта стоит без docker (postfix/dovecot) — подключить её нельзя, только поставить новую.",
         "Нажмите «Установить», чтобы поставить почтовый сервер."],
        ["There is no docker-mailserver container on the machine.",
         "Mail installed without docker (postfix/dovecot) cannot be connected — only a new one installed.",
         "Press “Install” to set up a mail server."],
    ),
    "image_pull_failed": _h(
        "Не скачался образ почтового сервера",
        "The mail server image did not download",
        ["Проверьте интернет на сервере: curl -I https://ghcr.io",
         "Проверьте место на диске: df -h",
         "Запустите установку снова — часто это разовый сбой сети."],
        ["Check internet on the server: curl -I https://ghcr.io",
         "Check disk space: df -h",
         "Start the install again — often it is a one-off network glitch."],
    ),
    "tls_failed": _h(
        "Не выпустился сертификат",
        "The certificate was not issued",
        ["Let's Encrypt проверяет, что имя сервера (A-запись) ведёт на этот сервер.",
         "Заведите A-запись для имени сервера на его IP и подождите 5–30 минут, пока она разойдётся.",
         "Порт 80 должен быть открыт снаружи (sudo ufw allow 80/tcp и в панели хостинга).",
         "Запустите установку снова."],
        ["Let's Encrypt checks that the server name (A record) points to this server.",
         "Add an A record for the server name to its IP and wait 5–30 minutes to propagate.",
         "Port 80 must be open from outside (sudo ufw allow 80/tcp and in the hosting panel).",
         "Start the install again."],
    ),
    "start_failed": _h(
        "Почтовый сервер не запустился",
        "The mail server did not start",
        ["Откройте полный журнал ниже — в конце написано, что именно не так.",
         "Частые причины: мало памяти, занятый порт, нет сети docker.",
         "Всё, что успела сделать установка, уже убрано. Исправьте причину и запустите снова."],
        ["Open the full log below — the end says what exactly went wrong.",
         "Common causes: low memory, a busy port, no docker network.",
         "Everything the install did is already undone. Fix the cause and start again."],
    ),
    "unhealthy": _h(
        "Почтовый сервер запустился, но не отвечает",
        "The mail server started but does not answer",
        ["В журнале ниже — последние строки самого сервера: обычно там видна причина.",
         "Частая причина — мало памяти: нужно от 1 ГБ.",
         "Установка откатилась. Исправьте причину и запустите снова."],
        ["The log below has the last lines of the server itself: usually the cause is there.",
         "A common cause is low memory: 1 GB or more is needed.",
         "The install was rolled back. Fix the cause and start again."],
    ),
    "dkim_failed": _h(
        "Не создалась подпись DKIM",
        "The DKIM signature was not created",
        ["Сервер работает, но без DKIM письма чаще попадают в спам.",
         "Откройте Настройки → Почтовый сервер, в карточке сервера найдите домен без DKIM и нажмите «Создать DKIM».",
         "Появившуюся TXT-запись mail._domainkey добавьте в DNS домена и нажмите «Проверить DNS»."],
        ["The server works, but without DKIM mail lands in spam more often.",
         "Open Settings → Mail server, find the domain without DKIM in the server card and press “Create DKIM”.",
         "Add the TXT record mail._domainkey that appears to the domain DNS and press “Check DNS”."],
    ),
    "firewall_failed": _h(
        "Не открылись порты в брандмауэре",
        "Ports were not opened in the firewall",
        ["Откройте порты вручную: sudo ufw allow 25,465,587,993/tcp",
         "Если у хостинга есть свой брандмауэр в панели — откройте эти порты и там."],
        ["Open the ports by hand: sudo ufw allow 25,465,587,993/tcp",
         "If your hosting has a firewall in its panel — open these ports there too."],
    ),
    "ssh_unreachable": _h(
        "Сервер не отвечает по SSH",
        "The server does not answer over SSH",
        ["Проверьте адрес и порт (обычно 22).",
         "Проверьте, что сервер включён и порт SSH открыт в брандмауэре и в панели хостинга.",
         "С вашего компьютера: ssh ПОЛЬЗОВАТЕЛЬ@АДРЕС -p ПОРТ — должно спросить пароль."],
        ["Check the address and port (usually 22).",
         "Check that the server is on and the SSH port is open in the firewall and hosting panel.",
         "From your computer: ssh USER@ADDRESS -p PORT — it should ask for a password."],
    ),
    "ssh_auth_failed": _h(
        "Сервер не пустил",
        "The server refused the login",
        ["Проверьте имя пользователя и пароль (или ключ).",
         "Если вход по паролю на сервере выключен — подключитесь ключом.",
         "Пароль с раскладкой: проверьте, не включён ли Caps Lock."],
        ["Check the user name and password (or key).",
         "If password login is disabled on the server — use a key.",
         "Check that Caps Lock is off."],
    ),
    "ssh_host_key_changed": _h(
        "Ключ сервера изменился",
        "The server key has changed",
        ["Так бывает после переустановки сервера — и так же выглядит подмена сервера.",
         "Если вы переустанавливали сервер — пройдите шаг SSH заново и подтвердите новый отпечаток.",
         "Если нет — не подключайтесь и проверьте, тот ли это сервер."],
        ["This happens after a server reinstall — and a server spoof looks the same.",
         "If you reinstalled the server — redo the SSH step and confirm the new fingerprint.",
         "If not — do not connect and check that it is the right server."],
    ),
    "ssh_host_key_unconfirmed": _h(
        "Сервер ещё не подтверждён",
        "The server is not confirmed yet",
        ["Пройдите шаг SSH: CRM покажет отпечаток ключа сервера, подтвердите его и войдите паролем или ключом."],
        ["Do the SSH step: the CRM shows the server key fingerprint, confirm it and log in with a password or key."],
    ),
    "ssh_key_bad": _h(
        "Ключ не читается",
        "The key is not readable",
        ["Вставьте закрытый ключ целиком, со строками BEGIN и END.",
         "Если ключ защищён паролем — введите его в поле «пароль ключа»."],
        ["Paste the whole private key, with the BEGIN and END lines.",
         "If the key has a passphrase — enter it in the “key passphrase” field."],
    ),
    "ssh_sudo_needed": _h(
        "Нет доступа к docker",
        "No access to docker",
        ["Пользователь не root и не в группе docker.",
         "Выполните на сервере: sudo usermod -aG docker ИМЯ — и подключитесь заново.",
         "Или введите пароль sudo на шаге SSH."],
        ["The user is neither root nor in the docker group.",
         "Run on the server: sudo usermod -aG docker NAME — and connect again.",
         "Or enter the sudo password on the SSH step."],
    ),
    "mail_agent_unreachable": _h(
        "CRM не достучалась до почтового сервера",
        "The CRM cannot reach the mail server",
        ["Зайдите на машину с почтой и проверьте, что контейнер запущен: docker ps | grep opencrm-mail",
         "Сервер на другой машине (по SSH): cd ~/opencrm-mail && docker compose -p opencrm-mail up -d (не root — с sudo впереди).",
         "Сервер на этой машине: в каталоге OpenCRM (обычно ~/opencrm) выполните cd mail && docker compose -p opencrm-mail up -d",
         "Для сервера по SSH проверьте вход: на экране почтового сервера нажмите «Проверить»."],
        ["Log in to the mail machine and check that the container runs: docker ps | grep opencrm-mail",
         "Server on another machine (over SSH): cd ~/opencrm-mail && docker compose -p opencrm-mail up -d (not root — prefix with sudo).",
         "Server on this machine: in the OpenCRM folder (usually ~/opencrm) run cd mail && docker compose -p opencrm-mail up -d",
         "For a server over SSH check the login: press “Check” on the mail server screen."],
    ),
    "mail_agent_failed": _h(
        "Почтовый сервер отказал",
        "The mail server refused",
        ["Подробность — в тексте ошибки. Журнал самого почтового сервера на его машине: docker logs --tail 100 opencrm-mail",
         "Повторите действие через минуту; если повторяется — пришлите текст ошибки и этот журнал в поддержку."],
        ["Details are in the error text. The log of the mail server itself, on its machine: docker logs --tail 100 opencrm-mail",
         "Repeat in a minute; if it repeats — send the error text and that log to support."],
    ),
    "mail_updater_offline": _h(
        "Служба обновления не отвечает",
        "The update service is not answering",
        ["Почтовый сервер на этой машине ставит служба обновления OpenCRM, а её сейчас нет.",
         "Зайдите на сервер по SSH и выполните в каталоге OpenCRM: ./opencrm.sh mail install",
         "Журнал установки появится и здесь, в этом окне."],
        ["The mail server on this machine is installed by the OpenCRM update service, which is not running.",
         "Log in to the server over SSH and run in the OpenCRM folder: ./opencrm.sh mail install",
         "The install log will also appear here, in this window."],
    ),
    "job_interrupted": _h(
        "Работа прервалась",
        "The job was interrupted",
        ["Приложение перезапустилось посреди работы (обновление или сбой).",
         "Запустите проверку: она покажет, в каком состоянии сервер, — и повторите действие."],
        ["The application restarted in the middle of the job (update or crash).",
         "Run the check: it shows the server state — then repeat the action."],
    ),
}


def hint_out(code: str | None) -> dict | None:
    h = HINTS.get(code or "")
    if h is None:
        return None
    return {
        "code": code,
        "title_ru": h["title"]["ru"],
        "title_en": h["title"]["en"],
        "steps_ru": list(h["steps"]["ru"]),
        "steps_en": list(h["steps"]["en"]),
    }


# --- протокол установщика ---


class Razbor:
    """Строки `##…` → шаги, данные и итог. Остальное — сырой журнал, его здесь не держим."""

    def __init__(self, steps: list[dict] | None = None):
        self.steps: list[dict] = steps if steps is not None else []
        self.data: dict[str, str] = {}
        self.existing: list[str] = []
        self.end: str | None = None
        self.error_code = ""
        self.error_detail = ""
        self.rollback = False

    def _shag(self, code: str) -> dict:
        for shag in reversed(self.steps):
            if shag["code"] == code:
                return shag
        shag = {"code": code, "status": "running", "detail": "", "hint": None}
        self.steps.append(shag)
        return shag

    def stroka(self, s: str) -> bool:
        if not s.startswith("##"):
            return False
        vid, _, ostatok = s[2:].strip().partition(" ")
        if vid == "STEP" and ostatok:
            self.steps.append({"code": ostatok.split()[0][:40], "status": "running", "detail": "", "hint": None})
        elif vid in ("OK", "WARN") and ostatok:
            code, _, detail = ostatok.partition(" ")
            shag = self._shag(code[:40])
            shag["status"] = "ok" if vid == "OK" else "warn"
            beda, _, ostalnoe = detail.partition(" ")
            if vid == "WARN" and beda in HINTS:
                shag["hint"] = beda
                detail = ostalnoe
            elif vid == "OK":
                # Шаг, прошедший после предупреждения (обновлятор забрал просьбу), беды больше не несёт.
                shag["hint"] = None
            shag["detail"] = detail.strip()[:500]
        elif vid == "FAIL" and ostatok:
            code, _, ostalnoe = ostatok.partition(" ")
            beda, _, detail = ostalnoe.partition(" ")
            shag = self._shag(code[:40])
            shag.update(status="fail", detail=detail.strip()[:500], hint=beda[:64] or None)
            if not self.error_code:
                self.error_code = beda[:64]
                self.error_detail = detail.strip()[:2000]
        elif vid == "DATA" and ostatok:
            klyuch, _, znachenie = ostatok.partition(" ")
            if klyuch == "existing_container":
                self.existing.append(znachenie.strip())
            self.data[klyuch[:64]] = znachenie.strip()
        elif vid == "ROLLBACK":
            self.rollback = True
        elif vid == "END":
            self.end = ostatok.strip() or "failed"
        for shag in self.steps:
            if shag["status"] == "running" and shag is not self.steps[-1]:
                shag["status"] = "ok"
        return True

    def status_raboty(self) -> str:
        if self.end == "ok":
            return JOB_OK
        if self.end == "rolled_back" or self.rollback:
            return JOB_ROLLED_BACK
        return JOB_FAILED


class Pisar:
    """Журнал работы в базу: не чаще раза в секунду, и отметка живости тем же ходом."""

    def __init__(self, job_id: int):
        self.job_id = job_id
        self.razbor = Razbor()
        self._bufer: list[str] = []
        self._zamok = threading.Lock()
        self._posledniy = 0.0
        self._razmer = 0

    def __call__(self, stroka: str) -> None:
        stroka = stroka.rstrip("\r\n")
        protokol = self.razbor.stroka(stroka)
        with self._zamok:
            if self._razmer < ZHURNAL_MAX or protokol:
                self._bufer.append(stroka + "\n")
                self._razmer += len(stroka) + 1
        if time.monotonic() - self._posledniy >= 1.0:
            self.sbrosit()

    def sbrosit(self) -> None:
        with self._zamok:
            kusok = "".join(self._bufer)
            self._bufer.clear()
            self._posledniy = time.monotonic()
            shagi = json.dumps(self.razbor.steps, ensure_ascii=False)
        try:
            with SessionLocal() as db:
                jobs_repo.dopisat_zhurnal(db, self.job_id, kusok, steps=shagi, kogda=now_utc())
                db.commit()
        except Exception:  # noqa: BLE001 — журнал не повод ронять установку
            logger.exception("mail job %s: log flush failed", self.job_id)


class _Serdtse:
    """Пока работа идёт, раз в 5 секунд — сброс журнала: тишина `docker pull` не выглядит смертью."""

    def __init__(self, pisar: Pisar):
        self.pisar = pisar
        self._stop = threading.Event()

    def __enter__(self):
        def tik():
            while not self._stop.wait(5):
                self.pisar.sbrosit()

        threading.Thread(target=tik, daemon=True, name=f"mail-job-{self.pisar.job_id}").start()
        return self

    def __exit__(self, *_a):
        self._stop.set()
        self.pisar.sbrosit()


def _v_fone(fn: Callable[[], None], imya: str) -> None:
    threading.Thread(target=fn, daemon=True, name=imya).start()


_zapusk: Callable[[Callable[[], None], str], None] = _v_fone


def set_runner(fn: Callable[[Callable[[], None], str], None]) -> None:
    """Шов для тестов: работа идёт сразу, в том же потоке."""
    global _zapusk
    _zapusk = fn


def reset_runner() -> None:
    global _zapusk
    _zapusk = _v_fone


# --- вывод ---


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def job_out(job: MailJob) -> dict:
    try:
        shagi = json.loads(job.steps or "[]")
    except ValueError:
        shagi = []
    for shag in shagi:
        shag["hint"] = hint_out(shag.get("hint")) if shag.get("hint") else None
    return {
        "id": job.id,
        "server_id": job.server_id,
        "kind": job.kind,
        "status": job.status,
        "steps": shagi,
        "error_code": job.error_code or "",
        "error_detail": job.error_detail or "",
        "hint": hint_out(job.error_code) if job.error_code else None,
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


def domain_out(d: MailDomain) -> dict:
    try:
        otchyot = json.loads(d.dns_report) if d.dns_report else None
    except ValueError:
        otchyot = None
    return {
        "id": d.id,
        "domain": d.domain,
        "dkim_selector": d.dkim_selector,
        "dkim_txt": d.dkim_txt,
        "dns_checked_at": _iso(d.dns_checked_at),
        "dns_report": otchyot,
    }


SKRYTO = {"ssh_key_encrypted", "sudo_password_encrypted", "master_password_encrypted",
          "agent_token_encrypted", "relay_password_encrypted"}


def server_out(db: Session, server: MailServer, chislo: int | None = None) -> dict:
    itog = {}
    for kolonka in MailServer.__table__.columns:
        if kolonka.name in SKRYTO or kolonka.name.endswith("_encrypted"):
            continue
        znachenie = getattr(server, kolonka.key)
        itog[kolonka.name] = _iso(znachenie) if hasattr(znachenie, "isoformat") else znachenie
    otpechatok = ""
    if server.ssh_host_key:
        try:
            otpechatok = mail_ssh.otpechatok(mail_ssh.razobrat_stroku_klyucha(server.ssh_host_key)[1])
        except mail_ssh.SshBeda:
            otpechatok = ""
    itog.update(
        ssh_fingerprint=otpechatok,
        has_ssh_key=bool(server.ssh_key_encrypted),
        has_sudo_password=bool(server.sudo_password_encrypted),
        has_relay_password=bool(server.relay_password_encrypted),
        domains=[domain_out(d) for d in repo.domeny_servera(db, server.id)],
        boxes_count=chislo if chislo is not None else jobs_repo.chislo_yashchikov(db, server.id),
    )
    return itog


# --- работы: начать, прервать, следить ---


def _prervat_zabytye(db: Session, server_id: int | None = None) -> None:
    """Работа без отметки живости дольше `SERDTSE_USTARELO` — процесс умер: честно «прервана»."""
    porog = now_utc() - timedelta(seconds=SERDTSE_USTARELO)
    for job in jobs_repo.idushchie(db, server_id):
        if (job.heartbeat_at or job.started_at) < porog:
            job.status = JOB_FAILED
            job.error_code = "job_interrupted"
            job.error_detail = "The application restarted while the job was running"
            job.finished_at = now_utc()
            server = repo.get_server(db, job.server_id)
            if server is not None and server.status == STATUS_INSTALLING:
                server.status = STATUS_FAILED
                server.last_error = "job_interrupted"


def spisok(db: Session) -> dict:
    """Серверы и жив ли обновлятор — без сети: экран открывается мгновенно."""
    _prervat_zabytye(db)
    chisla = jobs_repo.chislo_yashchikov_po_serveram(db)
    return {
        "servers": [server_out(db, s, chisla.get(s.id, 0)) for s in repo.servery(db)],
        "obnovlyator": mail_spool.obnovlyator(),
    }


def server(db: Session, server_id: int) -> MailServer:
    s = repo.get_server(db, server_id)
    if s is None:
        raise errors.NotFoundError("Mail server not found", code="mail_server_not_found")
    return s


def nachat(db: Session, server_id: int, kind: str, actor: User | None, params: dict | None = None) -> MailJob:
    """Новая работа над сервером. Две разом — нет: строка сервера запирается на время проверки."""
    s = jobs_repo.zapert_server(db, server_id)
    if s is None:
        raise errors.NotFoundError("Mail server not found", code="mail_server_not_found")
    _prervat_zabytye(db, server_id)
    if jobs_repo.idushchie(db, server_id):
        raise errors.ConflictError("Another job is running on this mail server", code="mail_job_running")
    job = repo.dobavit(db, MailJob(
        server_id=s.id, kind=kind, status=JOB_RUNNING, steps="[]", log="",
        actor_id=actor.id if actor else None, heartbeat_at=now_utc(),
    ))
    if kind == JOB_INSTALL:
        s.status = STATUS_INSTALLING
        s.last_error = None
    db.commit()
    job_id = job.id
    kto = actor.name if actor else ""
    _zapusk(lambda: _rabota(job_id, dict(params or {}), kto), f"mail-job-{job_id}")
    db.expire_all()
    return jobs_repo.get_job(db, job_id)


def _rabota(job_id: int, params: dict, kto: str) -> None:
    pisar = Pisar(job_id)
    with SessionLocal() as db:
        job = jobs_repo.get_job(db, job_id)
        s = repo.get_server(db, job.server_id) if job else None
        if job is None or s is None:
            return
        try:
            with _Serdtse(pisar):
                if job.kind == JOB_CHECK:
                    _proverka(db, s, pisar, params, kto, job_id)
                elif job.kind == JOB_INSTALL:
                    _ustanovka(db, s, pisar, params, kto, job_id)
                elif job.kind == JOB_ADOPT:
                    _podklyuchenie(db, s, pisar, kto, job_id)
                elif job.kind == JOB_REMOVE:
                    _udalenie(db, s, pisar, params, kto, job_id)
        except (mail_ssh.SshBeda, errors.DomainError) as beda:
            code = getattr(beda, "code", "") or "start_failed"
            pisar(f"##FAIL {code} {code} {getattr(beda, 'message', str(beda))}")
            pisar("##END failed")
            pisar.sbrosit()
        except Exception as beda:  # noqa: BLE001 — работа обязана закончиться записью, а не тишиной
            logger.exception("mail job %s crashed", job_id)
            pisar(f"##FAIL internal start_failed {type(beda).__name__}: {str(beda)[:300]}")
            pisar("##END failed")
            pisar.sbrosit()
        db.rollback()
        _zakonchit(db, job_id, pisar.razbor)


def _zakonchit(db: Session, job_id: int, razbor: Razbor) -> None:
    db.expire_all()
    job = jobs_repo.get_job(db, job_id)
    s = repo.get_server(db, job.server_id)
    job.status = razbor.status_raboty()
    job.error_code = razbor.error_code if job.status != JOB_OK else ""
    job.error_detail = razbor.error_detail if job.status != JOB_OK else ""
    job.finished_at = now_utc()
    job.heartbeat_at = now_utc()
    if job.status != JOB_OK and s is not None:
        if job.kind in (JOB_INSTALL, JOB_ADOPT) and s.status in (STATUS_INSTALLING, STATUS_DRAFT):
            s.status = STATUS_FAILED
        if job.kind != JOB_CHECK:
            s.last_error = f"{job.error_code}: {job.error_detail}"[:2000] if job.error_code else "failed"
    db.commit()


# --- как исполнить установщик ---


def _fayly_ustanovshchika() -> dict[str, bytes]:
    from core.services.mail_host import AGENT_PUT

    papka = AGENT_PUT.parent
    return {imya: (papka / imya).read_bytes().replace(b"\r\n", b"\n") for imya in ("ustanovit.sh", "compose.yml")}


def ssh_soedinenie(s: MailServer) -> mail_ssh.Soedinenie:
    if not s.ssh_key_encrypted or not s.ssh_host_key:
        raise mail_ssh.SshBeda("ssh_host_key_unconfirmed", "Connect over SSH first")
    try:
        klyuch = secretbox.decrypt(s.ssh_key_encrypted, mail_server_service.SSH_KEY_PURPOSE)
    except secretbox.SecretBoxError as beda:
        raise mail_ssh.SshBeda("ssh_key_bad", "SSH key cannot be decrypted with the current secret key") from beda
    return mail_ssh.klient().podklyuchit(
        s.ssh_host, s.ssh_port or 22, s.ssh_user, host_key_line=s.ssh_host_key, private_key=klyuch, timeout=15
    )


def _sudo_parol(s: MailServer) -> str:
    if not s.sudo_password_encrypted:
        return ""
    try:
        return secretbox.decrypt(s.sudo_password_encrypted, mail_server_service.SUDO_PURPOSE)
    except secretbox.SecretBoxError:
        return ""


def _ispolnit(s: MailServer, op: str, env: dict[str, str], pisar: Pisar, kto: str, job_id: int,
              spool_op: str | None = None) -> None:
    """Установщик с операцией `op`: по SSH — сразу, здесь — через обновлятор."""
    if s.mode == MODE_SSH:
        conn = ssh_soedinenie(s)
        try:
            kod, vyvod = conn.vypolnit('printf "%s" "$HOME"', timeout=20)
            home = vyvod.strip() or "/root"
            env.setdefault("MAIL_DIR", s.base_dir or f"{home}/{PAPKA_POCHTY}")
            env.setdefault("MAIL_LE_DIR", f"{env['MAIL_DIR']}/letsencrypt")
            env.setdefault("MAIL_SUDO_PASSWORD", _sudo_parol(s))
            env.setdefault("MAIL_NETWORK", "")
            env.setdefault("MAIL_AGENT_SRC", "")
            mail_ssh.zapustit_ustanovshchik(
                conn, f"{home}/{UDALENNAYA_PAPKA}", _fayly_ustanovshchika(), env, op, pisar
            )
        finally:
            conn.zakryt()
        return
    _cherez_obnovlyator(spool_op or op, env, pisar, kto, job_id)


def _cherez_obnovlyator(op: str, env: dict[str, str], pisar: Pisar, kto: str, job_id: int) -> None:
    """Просьба обновлятору и ожидание: журнал дочитывается из файла по ходу."""
    mail_spool.zaprosit(job_id, op, env, kto)
    pisar("##STEP updater")
    nachalo = time.monotonic()
    preduprezhden = False
    vzyata = False
    prochitano = 0
    while True:
        if not vzyata and mail_spool.vzyata(job_id):
            vzyata = True
            pisar("##OK updater the update service took the request")
        if not vzyata:
            if not preduprezhden and not mail_spool.obnovlyator()["zhiv"]:
                preduprezhden = True
                pisar("##WARN updater mail_updater_offline waiting: run ./opencrm.sh mail install on the server")
            if time.monotonic() - nachalo > ZHDAT_OBNOVLYATOR:
                mail_spool.otozvat(job_id)
                pisar("##FAIL updater mail_updater_offline nobody took the request in 30 minutes")
                pisar("##END failed")
                return
        # Итог — ДО куска: обновлятор пишет его, закрыв журнал, и тогда кусок
        # гарантированно последний. Наоборот терялись строки, дописанные между двумя чтениями.
        itog = mail_spool.itog(job_id)
        kusok, razmer = mail_spool.zhurnal(job_id, prochitano)
        if kusok:
            konets = kusok.rfind(b"\n")
            if konets >= 0:
                for stroka in kusok[: konets + 1].decode("utf-8", "replace").splitlines():
                    pisar(stroka)
                prochitano += konets + 1
        if itog is not None and prochitano >= razmer:
            if pisar.razbor.end is None:
                pisar(f"##END {'ok' if itog.get('ok') else 'failed'}")
            mail_spool.ubrat(job_id)
            return
        time.sleep(0.5)


def _bazovoe_env(s: MailServer, domen: str) -> dict[str, str]:
    return {
        "MAIL_HOSTNAME": s.hostname,
        "MAIL_DOMAIN": domen,
        "MAIL_IMAGE": s.image or DEFAULT_IMAGE,
        "MAIL_CONTAINER": s.container or "opencrm-mail",
        "MAIL_PORTS": PORTY,
    }


def _pervyy_domen(db: Session, s: MailServer) -> MailDomain:
    domeny = repo.domeny_servera(db, s.id)
    if not domeny:
        raise errors.ValidationError("The mail server has no domain", code="mail_domain_bad")
    # Первый заведённый — тот, что человек ввёл в мастере.
    return min(domeny, key=lambda d: d.id)


def _proverit_dns(db: Session, s: MailServer, pisar: Pisar | None = None) -> list[dict]:
    itog = []
    for d in repo.domeny_servera(db, s.id):
        zapisi = mail_dns.proverit(s.hostname, d.domain, s.public_ip, d.dkim_txt, selector=d.dkim_selector or "mail")
        d.dns_report = json.dumps(zapisi, ensure_ascii=False)
        d.dns_checked_at = now_utc()
        itog.append({"domain": d.domain, "records": zapisi})
        if pisar is not None:
            plokhie = [z["type"] for z in zapisi if z["status"] != mail_dns.OK and z["required"]]
            pisar("##STEP dns")
            if plokhie:
                pisar(f"##WARN dns {d.domain}: not ready yet: {', '.join(plokhie)}")
            else:
                pisar(f"##OK dns {d.domain}: all required records are in place")
    return itog


def _proverka(db, s, pisar, params, kto, job_id):
    d = _pervyy_domen(db, s)
    env = _bazovoe_env(s, d.domain)
    env["MAIL_TLS"] = params.get("tls") or ("webroot" if s.mode == MODE_LOCAL else "letsencrypt")
    env["MAIL_ALLOW_DOCKER_INSTALL"] = "1" if params.get("allow_docker_install") else "0"
    if params.get("sudo_password"):
        env["MAIL_SUDO_PASSWORD"] = params["sudo_password"]
    _ispolnit(s, "check", env, pisar, kto, job_id)
    s.checked_at = now_utc()
    if pisar.razbor.data.get("public_ip"):
        s.public_ip = pisar.razbor.data["public_ip"][:45]
    if s.mode == MODE_SSH and pisar.razbor.data.get("docker_cmd"):
        s.docker_cmd = pisar.razbor.data["docker_cmd"][:32]
    konets = pisar.razbor.end
    pisar.razbor.end = None
    _proverit_dns(db, s, pisar)
    pisar(f"##END {konets or 'failed'}")
    db.commit()


def _ustanovka(db, s, pisar, params, kto, job_id):
    d = _pervyy_domen(db, s)
    env = _bazovoe_env(s, d.domain)
    env.update({
        "MAIL_TLS": params.get("tls") or ("webroot" if s.mode == MODE_LOCAL else "letsencrypt"),
        "MAIL_ACME_EMAIL": params.get("acme_email") or "",
        "MAIL_FIRST": params["first_address"],
        "MAIL_FIRST_PASSWORD": params["first_password"],
        "MAIL_MASTER_USER": s.master_user,
        "MAIL_MASTER_PASSWORD": secretbox.decrypt(s.master_password_encrypted, mail_server_service.MASTER_PURPOSE),
        "MAIL_ALLOW_DOCKER_INSTALL": "1" if params.get("allow_docker_install") else "0",
    })
    if s.mode == MODE_LOCAL:
        env["MAIL_AGENT_TOKEN"] = secretbox.decrypt(s.agent_token_encrypted, mail_server_service.AGENT_PURPOSE)
    if params.get("sudo_password"):
        env["MAIL_SUDO_PASSWORD"] = params["sudo_password"]
    _ispolnit(s, "install", env, pisar, kto, job_id)
    if pisar.razbor.end != "ok":
        return
    data = pisar.razbor.data
    teper = now_utc()
    s.status = STATUS_READY
    s.installed_at = teper
    s.checked_at = teper
    s.last_error = None
    if data.get("public_ip"):
        s.public_ip = data["public_ip"][:45]
    # Let's Encrypt не выдал — установщик ставит самоподписанный и шлёт его отпечаток.
    s.tls_pin = (data.get("tls_pin") or "")[:64]
    if s.mode == MODE_SSH:
        s.docker_cmd = (data.get("docker_cmd") or "docker")[:32]
        s.base_dir = env.get("MAIL_DIR", "")[:255]
        s.imap_host = s.imap_host or s.ssh_host
    else:
        s.imap_host = s.container or "opencrm-mail"
    if data.get("dkim_txt"):
        d.dkim_txt = data["dkim_txt"]
        d.dkim_selector = (data.get("dkim_selector") or "mail")[:63]
    adres = params["first_address"]
    if repo.yashchik_po_adresu(db, adres) is None:
        repo.dobavit(db, mail_server_service._novyy_yashchik(s, adres, title=adres))
    db.commit()
    # Агент по SSH сменил группу пользователя (docker) — старые соединения её не видят.
    mail_ssh.vybrosit(server_id=s.id)
    try:
        _proverit_dns(db, s)
        db.commit()
    except Exception:  # noqa: BLE001 — DNS после установки — справка, а не условие успеха
        db.rollback()


def _agent(s: MailServer, op: str, args: dict | None = None, timeout: float = 120) -> dict:
    from core.services import mail_host

    try:
        return mail_host.host_dlya(s).vyzvat(op, args or {}, timeout=timeout)
    except mail_host.MailHostError as beda:
        raise errors.ValidationError(beda.message, code=beda.code) from beda


def _podklyuchenie(db, s, pisar, kto, job_id):
    """Найти почтовый сервер, выдать CRM мастер-вход, забрать ящики и псевдонимы."""
    master = secrets.token_urlsafe(24)
    if s.mode == MODE_SSH:
        pisar("# looking for docker-mailserver on the machine")
        d = repo.domeny_servera(db, s.id)
        env = _bazovoe_env(s, d[0].domain if d else "")
        env["MAIL_TLS"] = "selfsigned"
        _ispolnit(s, "check", env, pisar, kto, job_id)
        r = pisar.razbor
        r.end = None
        r.error_code = ""
        r.error_detail = ""
        for shag in r.steps:
            # Порт 25 занят найденным сервером — для подключения это и есть находка.
            if shag["code"] in ("ports", "mta", "existing") and shag["status"] == "fail":
                shag["status"] = "ok"
        if any(shag["status"] == "fail" for shag in r.steps if shag["code"] in ("privileges", "docker")):
            pisar("##FAIL find ssh_sudo_needed docker is not reachable for this user")
            pisar("##END failed")
            return
        vladelets = r.data.get("port_owner_25", "")
        kandidaty = r.existing
        if vladelets.startswith("container:"):
            kandidaty = [vladelets.split(":", 1)[1]] + [k for k in kandidaty if k != vladelets.split(":", 1)[1]]
        if s.container in kandidaty:
            kandidaty = [s.container]
        pisar("##STEP find")
        if not kandidaty:
            pisar("##FAIL find dms_not_found no docker-mailserver container on the machine")
            pisar("##END failed")
            return
        s.container = kandidaty[0][:64]
        s.docker_cmd = (r.data.get("docker_cmd") or s.docker_cmd or "docker")[:32]
        if r.data.get("public_ip"):
            s.public_ip = r.data["public_ip"][:45]
        pisar(f"##OK find container {s.container}")
    else:
        token = secrets.token_urlsafe(32)
        s.agent_token_encrypted = secretbox.encrypt(token, mail_server_service.AGENT_PURPOSE)
        s.container = "opencrm-mail"
        db.commit()
        _cherez_obnovlyator("adopt", {"MAIL_AGENT_TOKEN": token}, pisar, kto, job_id)
        if pisar.razbor.end != "ok":
            return
        pisar.razbor.end = None
    db.commit()

    pisar("##STEP agent")
    ping = _agent(s, "ping", timeout=60)
    pisar(f"##OK agent docker-mailserver {ping.get('dms')} on {ping.get('hostname')}")
    if ping.get("hostname") and HOSTNAME.match(str(ping["hostname"]).lower()):
        s.hostname = str(ping["hostname"]).lower()
    pisar("##STEP master")
    _agent(s, "master_ensure", {"user": s.master_user, "password": master})
    s.master_password_encrypted = secretbox.encrypt(master, mail_server_service.MASTER_PURPOSE)
    pisar(f"##OK master CRM login {s.master_user} is set")
    pisar("##STEP tls")
    zdorove = _agent(s, "health", timeout=60)
    cert = zdorove.get("cert") or {}
    s.tls_pin = (cert.get("sha256") or "")[:64] if cert.get("self_signed") else ""
    pisar(f"##OK tls certificate until {cert.get('not_after') or '?'}{' (self-signed, pinned)' if s.tls_pin else ''}")
    s.imap_host = s.ssh_host if s.mode == MODE_SSH else (s.container or "opencrm-mail")
    s.status = STATUS_READY
    s.installed_at = s.installed_at or now_utc()
    s.checked_at = now_utc()
    s.last_error = None
    db.commit()
    pisar("##STEP import")
    itog = mail_server_service.sverit(db, s)
    for d in repo.domeny_servera(db, s.id):
        try:
            dkim = _agent(s, "dkim", {"domain": d.domain})
            d.dkim_txt = dkim.get("txt") or d.dkim_txt
            d.dkim_selector = (dkim.get("selector") or "mail")[:63]
        except errors.DomainError as beda:
            pisar(f"##WARN dkim dkim_failed {d.domain}: {beda.message}")
    pisar(
        f"##OK import mailboxes: {len(itog.get('added', []))} new, {len(itog.get('linked', []))} linked;"
        f" aliases: {itog.get('aliases', {}).get('total', 0)}"
    )
    db.commit()
    pisar("##END ok")


def _udalenie(db, s, pisar, params, kto, job_id):
    env = {
        "MAIL_CONTAINER": s.container or "opencrm-mail",
        "MAIL_DELETE_DATA": "1" if params.get("delete_mail") else "0",
    }
    if s.base_dir:
        env["MAIL_DIR"] = s.base_dir
    _ispolnit(s, "remove", env, pisar, kto, job_id)
    if pisar.razbor.end != "ok":
        return
    s.status = STATUS_DRAFT
    s.installed_at = None
    s.last_error = None
    for box in repo.yashchiki_servera(db, s.id):
        box.is_active = False
    mail_ssh.vybrosit(server_id=s.id)
    db.commit()


# --- черновик сервера ---


def _hostname(znachenie: str | None) -> str:
    h = (znachenie or "").strip().lower().rstrip(".")
    if not HOSTNAME.match(h):
        raise errors.ValidationError("Server name must look like mail.example.com", code="mail_hostname_bad")
    return h


def _domen(znachenie: str | None) -> str:
    d = (znachenie or "").strip().lower().rstrip(".")
    if not mail_server_service.DOMAIN.match(d):
        raise errors.ValidationError("Domain must look like example.com", code="mail_domain_bad")
    return d


def _ssh_polya(s: MailServer, data: dict) -> None:
    if "ssh_host" in data:
        host = (data.get("ssh_host") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9.:\[\]-]{1,253}", host):
            raise errors.ValidationError("SSH address is not valid", code="mail_hostname_bad")
        if host != s.ssh_host:
            s.ssh_host_key = ""
        s.ssh_host = host
    if "ssh_port" in data and data.get("ssh_port") is not None:
        port = int(data["ssh_port"])
        if not 1 <= port <= 65535:
            raise errors.ValidationError("SSH port must be 1–65535", code="mail_hostname_bad")
        if port != s.ssh_port:
            s.ssh_host_key = ""
        s.ssh_port = port
    if "ssh_user" in data and data.get("ssh_user") is not None:
        user = (data.get("ssh_user") or "").strip()
        if user and not SSH_USER.match(user):
            raise errors.ValidationError("SSH user name is not valid", code="ssh_user_bad")
        s.ssh_user = user


def sozdat(db: Session, data: dict) -> MailServer:
    rezhim = data.get("mode")
    if rezhim not in SERVER_MODES:
        raise errors.ValidationError("Mode must be local or ssh", code="mail_server_mode_bad")
    hostname = _hostname(data.get("hostname"))
    domen = _domen(data.get("domain"))
    if repo.domen_po_imeni(db, domen) is not None:
        raise errors.ConflictError("This domain is already on a mail server", code="mail_domain_exists")
    if rezhim == MODE_LOCAL and any(x.mode == MODE_LOCAL for x in repo.servery(db)):
        raise errors.ConflictError("A mail server on this machine already exists", code="mail_server_busy")
    s = MailServer(
        title=(data.get("title") or hostname)[:120],
        mode=rezhim,
        status=STATUS_DRAFT,
        hostname=hostname,
        container="opencrm-mail",
        image=DEFAULT_IMAGE,
        master_user="opencrm",
        ssh_port=22,
    )
    if rezhim == MODE_SSH:
        if not (data.get("ssh_host") or "").strip():
            raise errors.ValidationError("SSH address is required", code="mail_hostname_bad")
        _ssh_polya(s, data)
        s.imap_host = s.ssh_host
    else:
        s.imap_host = s.container
    repo.dobavit(db, s)
    repo.dobavit(db, MailDomain(server_id=s.id, domain=domen))
    return s


def izmenit(db: Session, server_id: int, data: dict) -> MailServer:
    s = server(db, server_id)
    if "title" in data and data.get("title") is not None:
        s.title = str(data["title"]).strip()[:120]
    if s.status in (STATUS_DRAFT, STATUS_FAILED):
        if data.get("hostname"):
            s.hostname = _hostname(data["hostname"])
        if s.mode == MODE_SSH:
            _ssh_polya(s, data)
            s.imap_host = s.ssh_host
    elif any(k in data for k in ("hostname", "ssh_host", "ssh_port", "ssh_user")):
        raise errors.ConflictError("Remove the server first to change its address", code="mail_server_busy")
    db.flush()
    return s


def udalit(db: Session, server_id: int) -> dict:
    """Удалить сервер из CRM; по SSH — заодно снять с машины свой ключ входа."""
    s = server(db, server_id)
    _prervat_zabytye(db, server_id)
    if jobs_repo.idushchie(db, server_id):
        raise errors.ConflictError("A job is running on this mail server", code="mail_job_running")
    if s.status not in (STATUS_DRAFT, STATUS_FAILED):
        raise errors.ConflictError("Remove the mail server from the machine first", code="mail_server_busy")
    snyato, preduprezhdenie = 0, None
    if s.mode == MODE_SSH and s.ssh_key_encrypted and s.ssh_host_key:
        try:
            conn = ssh_soedinenie(s)
        except Exception as beda:  # noqa: BLE001 — машины может уже не быть: удаляем всё равно
            logger.warning("mail server %s: cannot log in to remove the key: %s", s.id, beda)
            preduprezhdenie = {
                "code": "ssh_old_keys_left",
                "message": "The OpenCRM key could not be removed from the machine",
                "command": mail_ssh.komanda_snyatiya(_metka_klyucha(s.id)),
            }
        else:
            try:
                snyato, preduprezhdenie = _snyat_starye_klyuchi(conn, s.id, None)
            finally:
                conn.zakryt()
    for box in repo.yashchiki_servera(db, s.id):
        box.is_active = False
        box.server_id = None
    mail_ssh.vybrosit(server_id=s.id)
    repo.udalit(db, s)
    return {"ok": True, "keys_removed": snyato, "warning": preduprezhdenie}


# --- SSH ---


def _metka_klyucha(server_id: int) -> str:
    return f"opencrm-mail-{server_id}"


def _snyat_starye_klyuchi(conn, server_id: int, ostavit: str | None) -> tuple[int, dict | None]:
    """Свои прежние ключи сервера — из authorized_keys. Не вышло — не провал, а
    предупреждение с готовой командой: человек снимет их сам."""
    metka = _metka_klyucha(server_id)
    try:
        return mail_ssh.snyat_klyuchi(conn, metka, ostavit), None
    except Exception as beda:  # noqa: BLE001 — уборка не повод отменять вход
        logger.warning("mail server %s: old keys were not removed: %s", server_id, beda)
        return 0, {
            "code": "ssh_old_keys_left",
            "message": "Old OpenCRM keys were not removed from ~/.ssh/authorized_keys",
            "command": mail_ssh.komanda_snyatiya(metka),
        }


def _ssh_otkaz(beda: mail_ssh.SshBeda) -> errors.DomainError:
    # Коды буквами: экран подписывает их своими словами, а сторож ищет `code="…"` в коде.
    if beda.code == "ssh_unreachable":
        return errors.PrehodyashchayaBedaError(beda.message, code="ssh_unreachable")
    if beda.code == "ssh_host_key_changed":
        return errors.ValidationError(beda.message, code="ssh_host_key_changed")
    if beda.code == "ssh_key_bad":
        return errors.ValidationError(beda.message, code="ssh_key_bad")
    return errors.ValidationError(beda.message, code=beda.code)


def _ssh_server(db: Session, server_id: int) -> MailServer:
    s = server(db, server_id)
    if s.mode != MODE_SSH:
        raise errors.ValidationError("This mail server is not reached over SSH", code="mail_server_mode_bad")
    if not s.ssh_host:
        raise errors.ValidationError("SSH address is required", code="mail_hostname_bad")
    return s


def ssh_proba(db: Session, server_id: int) -> dict:
    """Шаг 1 TOFU: ключ сервера и его отпечаток — человек сверяет и подтверждает."""
    s = _ssh_server(db, server_id)
    try:
        k = mail_ssh.klient().klyuch_hosta(s.ssh_host, s.ssh_port or 22)
    except mail_ssh.SshBeda as beda:
        raise _ssh_otkaz(beda) from beda
    izvesten = bool(s.ssh_host_key) and hmac.compare_digest(s.ssh_host_key.encode(), k.key_line.encode())
    return {"fingerprint": k.fingerprint, "key_type": k.key_type, "key_line": k.key_line, "known": izvesten}


def ssh_podklyuchit(db: Session, server_id: int, data: dict) -> dict:
    """Шаг 2: войти, положить свой ключ, проверить вход им. Пароль входа не сохраняется."""
    s = _ssh_server(db, server_id)
    stroka = (data.get("host_key_line") or "").strip()
    try:
        tip, telo = mail_ssh.razobrat_stroku_klyucha(stroka)
    except mail_ssh.SshBeda as beda:
        raise errors.ValidationError(beda.message, code="ssh_host_key_unconfirmed") from beda
    stroka = f"{tip} {base64.b64encode(telo).decode('ascii')}"
    user = (data.get("user") or s.ssh_user or "").strip()
    if not SSH_USER.match(user):
        raise errors.ValidationError("SSH user name is not valid", code="ssh_user_bad")
    parol = data.get("password")
    chuzhoy = (data.get("private_key") or "").strip() or None
    if not parol and not chuzhoy:
        raise errors.ValidationError("Enter a password or paste a key", code="ssh_auth_failed")
    sudo_parol = data.get("sudo_password") or None
    k = mail_ssh.klient()
    try:
        conn = k.podklyuchit(s.ssh_host, s.ssh_port or 22, user, host_key_line=stroka,
                             password=parol if not chuzhoy else None, private_key=chuzhoy,
                             passphrase=data.get("passphrase") or None)
        zakrytyy, otkrytyy = mail_ssh.sozdat_klyuch(f"opencrm-mail-{s.id}")
        try:
            mail_ssh.polozhit_klyuch(conn, otkrytyy)
        finally:
            conn.zakryt()
        conn = k.podklyuchit(s.ssh_host, s.ssh_port or 22, user, host_key_line=stroka, private_key=zakrytyy)
        try:
            razvedka = mail_ssh.razvedat(conn, sudo_parol)
            snyato, preduprezhdenie = _snyat_starye_klyuchi(conn, s.id, otkrytyy)
        finally:
            conn.zakryt()
    except mail_ssh.SshBeda as beda:
        raise _ssh_otkaz(beda) from beda
    s.ssh_host_key = stroka
    s.ssh_user = user
    s.ssh_key_encrypted = secretbox.encrypt(zakrytyy, mail_server_service.SSH_KEY_PURPOSE)
    s.ssh_public_key = otkrytyy
    if sudo_parol and data.get("keep_sudo") and razvedka["sudo"] == "password_ok":
        s.sudo_password_encrypted = secretbox.encrypt(sudo_parol, mail_server_service.SUDO_PURPOSE)
    elif not data.get("keep_sudo"):
        s.sudo_password_encrypted = None
    if razvedka["docker"] == "ok":
        s.docker_cmd = "docker"
    elif razvedka["sudo"] in ("root", "nopasswd"):
        s.docker_cmd = "sudo -n docker" if razvedka["sudo"] == "nopasswd" else "docker"
    mail_ssh.vybrosit(server_id=s.id)
    db.flush()
    return {
        "ok": True,
        "key_installed": True,
        "docker": razvedka["docker"],
        "sudo": razvedka["sudo"],
        "root": razvedka["root"],
        "fingerprint": mail_ssh.otpechatok(telo),
        "old_keys_removed": snyato,
        "warning": preduprezhdenie,
    }


# --- запуск работ с экрана ---


def _gotov_k_rabote(s: MailServer) -> None:
    if s.mode == MODE_SSH and (not s.ssh_key_encrypted or not s.ssh_host_key):
        raise errors.ValidationError("Connect over SSH first", code="ssh_host_key_unconfirmed")


def _odnorazovyy_sudo(data: dict) -> str:
    """Пароль sudo на одну работу, если человек не просил его запоминать."""
    parol = data.get("sudo_password") or ""
    if "\n" in parol or "\r" in parol:
        raise errors.ValidationError("Password must be one line", code="ssh_sudo_needed")
    return parol


def proverit(db: Session, server_id: int, actor: User, data: dict | None = None) -> MailJob:
    s = server(db, server_id)
    _gotov_k_rabote(s)
    data = data or {}
    return nachat(db, s.id, JOB_CHECK, actor, {
        "allow_docker_install": bool(data.get("allow_docker_install")),
        "sudo_password": _odnorazovyy_sudo(data),
        "tls": data.get("tls") if data.get("tls") in ("letsencrypt", "selfsigned", "webroot") else None,
    })


def ustanovit(db: Session, server_id: int, actor: User, data: dict) -> tuple[MailJob, str, str]:
    """Установка. Пароль первого ящика возвращается сразу и один раз — дальше его нигде нет."""
    s = server(db, server_id)
    _gotov_k_rabote(s)
    if s.status == STATUS_READY:
        raise errors.ConflictError("The mail server is already installed", code="mail_server_busy")
    d = _pervyy_domen(db, s)
    imya = (data.get("first_local") or "admin").strip().lower()
    if not mail_server_service.LOCAL_PART.match(imya):
        raise errors.ValidationError("Mailbox name may contain a-z, 0-9, dot, dash and underscore",
                                     code="mail_local_part_bad")
    adres = f"{imya}@{d.domain}"
    # Свой пароль, если его ввели; иначе — случайный (флаг по умолчанию и так «сгенерировать»).
    if data.get("first_password"):
        parol = mail_server_service.proverit_parol(data["first_password"], adres)
    else:
        parol = mail_server_service.sluchaynyy_parol()
    if any(c in parol for c in "\r\n"):
        raise errors.ValidationError("Password must be one line", code="mail_password_weak")
    acme = (data.get("acme_email") or "").strip()
    if acme and not is_valid_email(acme):
        raise errors.ValidationError("Email for Let's Encrypt is not valid", code="mail_acme_email_bad")
    tls = data.get("tls")
    if tls not in ("letsencrypt", "selfsigned", "webroot"):
        tls = None
    s.master_password_encrypted = secretbox.encrypt(secrets.token_urlsafe(24), mail_server_service.MASTER_PURPOSE)
    if s.mode == MODE_LOCAL:
        s.agent_token_encrypted = secretbox.encrypt(secrets.token_urlsafe(32), mail_server_service.AGENT_PURPOSE)
    db.flush()
    job = nachat(db, s.id, JOB_INSTALL, actor, {
        "first_address": adres,
        "first_password": parol,
        "acme_email": acme,
        "allow_docker_install": bool(data.get("allow_docker_install")),
        "tls": tls,
        "sudo_password": _odnorazovyy_sudo(data),
    })
    return job, adres, parol


def podklyuchit(db: Session, server_id: int, actor: User) -> MailJob:
    s = server(db, server_id)
    _gotov_k_rabote(s)
    return nachat(db, s.id, JOB_ADOPT, actor, {})


def snyat(db: Session, server_id: int, actor: User, data: dict) -> MailJob:
    s = server(db, server_id)
    _gotov_k_rabote(s)
    if (data.get("confirm") or "").strip().lower() != s.hostname:
        raise errors.ValidationError("Type the server name to confirm", code="mail_confirm_mismatch")
    return nachat(db, s.id, JOB_REMOVE, actor, {"delete_mail": bool(data.get("delete_mail"))})


def rabota(db: Session, job_id: int, log_from: int = 0) -> dict:
    job = jobs_repo.get_job(db, job_id)
    if job is None:
        raise errors.NotFoundError("Job not found", code="mail_server_not_found")
    _prervat_zabytye(db, job.server_id)
    # Кусок и следующее смещение — из ОДНОГО чтения: длина отдельным запросом отставала
    # от дописанного журнала, и экран получал те же строки второй раз.
    kusok, dalshe = jobs_repo.zhurnal(db, job_id, max(0, int(log_from or 0)), mail_spool.KUSOK_ZHURNALA)
    return {"job": job_out(job), "log_chunk": kusok, "log_size": dalshe}


def raboty(db: Session, server_id: int) -> list[dict]:
    server(db, server_id)
    return [job_out(j) for j in jobs_repo.raboty_servera(db, server_id)]


# --- готовый сервер: DNS, домены, сверка, здоровье, посредник, проверочное письмо ---


def dns(db: Session, server_id: int) -> dict:
    s = server(db, server_id)
    if not s.public_ip:
        s.public_ip = mail_dns.publichnyy_ip(timeout=3.0)[:45]
    return {"domains": _proverit_dns(db, s)}


def dobavit_domen(db: Session, server_id: int, data: dict) -> dict:
    s = mail_server_service.gotovyy_server(db, server_id)
    domen = _domen(data.get("domain"))
    if repo.domen_po_imeni(db, domen) is not None:
        raise errors.ConflictError("This domain is already on a mail server", code="mail_domain_exists")
    dkim = mail_server_service.vyzvat(s, "dkim", {"domain": domen}, timeout=120)
    d = repo.dobavit(db, MailDomain(
        server_id=s.id, domain=domen, dkim_selector=(dkim.get("selector") or "mail")[:63],
        dkim_txt=dkim.get("txt") or "",
    ))
    return domain_out(d)


def sozdat_dkim(db: Session, server_id: int, domain_id: int) -> dict:
    """DKIM для уже заведённого домена: установка могла не создать его (`dkim_failed`).
    Агент ключ не пересоздаёт, если он есть, — повторное нажатие безопасно."""
    s = mail_server_service.gotovyy_server(db, server_id)
    d = next((x for x in repo.domeny_servera(db, s.id) if x.id == domain_id), None)
    if d is None:
        raise errors.NotFoundError("Domain not found", code="mail_domain_bad")
    dkim = mail_server_service.vyzvat(s, "dkim", {"domain": d.domain}, timeout=120)
    if not dkim.get("txt"):
        raise errors.ValidationError("The mail server gave no DKIM key", code="mail_agent_failed")
    d.dkim_txt = dkim["txt"]
    d.dkim_selector = (dkim.get("selector") or "mail")[:63]
    db.flush()
    return domain_out(d)


def udalit_domen(db: Session, server_id: int, domain_id: int) -> None:
    s = server(db, server_id)
    d = next((x for x in repo.domeny_servera(db, s.id) if x.id == domain_id), None)
    if d is None:
        raise errors.NotFoundError("Domain not found", code="mail_domain_bad")
    if jobs_repo.yashchiki_domena(db, s.id, d.domain):
        raise errors.ConflictError("The domain still has mailboxes", code="mail_domain_has_boxes")
    repo.udalit(db, d)


def sinhronizirovat(db: Session, server_id: int, actor: User | None = None) -> dict:
    """Сверка ящиков и псевдонимов с сервером — та же, что делает наблюдатель раз в 10 минут."""
    s = mail_server_service.gotovyy_server(db, server_id)
    return mail_server_service.sverit(db, s, actor)


def primenit_sertifikat(s: MailServer, cert: dict | None) -> None:
    """Отпечаток — по тому, что стоит на сервере: самоподписанный сменился на
    Let's Encrypt (certbot выпустил сам) — CRM проверяет его уже по цепочке, а не
    по старому отпечатку, иначе IMAP отвалился бы на подмене."""
    cert = cert or {}
    otpechatok = str(cert.get("sha256") or "").strip().lower()[:64]
    if not otpechatok:
        return
    s.tls_pin = otpechatok if cert.get("self_signed") else ""


def zdorove(db: Session, server_id: int) -> dict:
    s = mail_server_service.gotovyy_server(db, server_id)
    otvet = mail_server_service.vyzvat(s, "health", timeout=60)
    primenit_sertifikat(s, otvet.get("cert"))
    db.flush()
    return otvet


def zadat_posrednika(db: Session, server_id: int, data: dict) -> dict:
    s = mail_server_service.gotovyy_server(db, server_id)
    host = (data.get("host") or "").strip().lower()
    if not HOSTNAME.match(host):
        raise errors.ValidationError("Relay host must look like smtp.example.com", code="mail_hostname_bad")
    try:
        port = int(data.get("port") or 587)
    except (TypeError, ValueError) as beda:
        raise errors.ValidationError("Relay port must be a number", code="mail_hostname_bad") from beda
    if not 1 <= port <= 65535:
        raise errors.ValidationError("Relay port must be 1–65535", code="mail_hostname_bad")
    user = (data.get("user") or "").strip()[:320]
    parol = data.get("password")
    if parol is None and s.relay_password_encrypted and user == s.relay_user:
        parol = secretbox.decrypt(s.relay_password_encrypted, mail_server_service.RELAY_PURPOSE)
    if user and not parol:
        raise errors.ValidationError("Relay password is required", code="mail_relay_password_required")
    for d in repo.domeny_servera(db, s.id):
        mail_server_service.vyzvat(s, "relay_set", {
            "domain": d.domain, "host": host, "port": port, "user": user, "password": parol or "",
        })
    s.relay_host, s.relay_port, s.relay_user = host, port, user
    s.relay_password_encrypted = secretbox.encrypt(parol, mail_server_service.RELAY_PURPOSE) if parol else None
    db.flush()
    return server_out(db, s)


def snyat_posrednika(db: Session, server_id: int) -> dict:
    s = mail_server_service.gotovyy_server(db, server_id)
    for d in repo.domeny_servera(db, s.id):
        mail_server_service.vyzvat(s, "relay_clear", {"domain": d.domain})
    s.relay_host, s.relay_user = "", ""
    s.relay_password_encrypted = None
    db.flush()
    return server_out(db, s)


def proverochnoe_pismo(db: Session, server_id: int, data: dict) -> dict:
    """Письмо от первого ящика сервера: видно, уходит ли почта наружу (порт 25, relay, DKIM)."""
    from core.services import webmail_imap, webmail_service

    s = mail_server_service.gotovyy_server(db, server_id)
    komu = (data.get("to") or "").strip()
    if not is_valid_email(komu):
        raise errors.ValidationError("Recipient address is not valid", code="mail_smtp_failed")
    yashchiki: list[MailAccount] = repo.yashchiki_servera(db, s.id)
    box = min((b for b in yashchiki if b.is_active), key=lambda b: b.id, default=None)
    if box is None:
        raise errors.ValidationError("The server has no mailbox to send from", code="mail_box_not_on_server")
    pismo = EmailMessage()
    pismo["From"] = box.address
    pismo["To"] = komu
    pismo["Subject"] = "OpenCRM mail server test"
    pismo["Message-ID"] = make_msgid(domain=box.address.split("@", 1)[1])
    pismo.set_content(
        f"This is a test message from the OpenCRM mail server {s.hostname}.\n"
        "If you see it in the inbox (not in spam), sending works.\n"
    )
    n = webmail_service.nastroyki(db, box)
    try:
        with webmail_imap.smtp(n) as klient:
            otkazy = klient.otpravit(box.address, [komu], pismo.as_bytes())
    except webmail_imap.WebmailError as beda:
        raise errors.ValidationError(beda.message, code="mail_smtp_failed") from beda
    return {"ok": not otkazy, "from": box.address, "to": komu, "refused": otkazy or {}}
