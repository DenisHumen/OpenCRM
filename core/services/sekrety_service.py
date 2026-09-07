"""Где в базе лежат шифротексты и как переложить их с чужого ключа на свой.

**Беда, ради которой это написано.** Копия базы, снятая на одной машине и
залитая на другую, привозит токены `secretbox`, зашифрованные ЧУЖИМ
`OPENCRM_SECRET_KEY`. На новой машине `opencrm.sh` заводит ключ сам, база
встаёт целиком, схема сходится, `/healthz` отвечает — а всё зашифрованное в ней
превращается в мусор МОЛЧА, без единой ошибки. Узнают об этом на первом входе в
почтовый ящик, то есть уже после того, как старую машину погасили.

Поэтому копия базы везёт в себе ключ, которым снята (`backup_service`), а
восстановление сразу после заливки перекладывает токены под нынешний ключ.

**Реестр `MESTA` — не удобство, а условие.** Место, забытое здесь, переживёт
восстановление нечитаемым. Сторож `tests/test_sekrety.py` требует, чтобы каждая
колонка `*_encrypted` в моделях была названа тут.
"""
from __future__ import annotations

import hmac

from sqlalchemy.orm import Session

from config.settings import get_settings
from core.security import secretbox
from core.services import klyuchi_service, mail_service
from database.models import MailAccount, TwoFactorKey
from database.repositories import sekrety as sekrety_repo

#: (модель, колонка, назначение) — все шифротексты системы.
MESTA = (
    (MailAccount, MailAccount.password_encrypted, mail_service.SECRET_PURPOSE),
    (TwoFactorKey, TwoFactorKey.secret_encrypted, klyuchi_service.SECRET_PURPOSE),
    (TwoFactorKey, TwoFactorKey.backup_codes_encrypted, klyuchi_service.BACKUP_PURPOSE),
)


def perelozhit(db: Session, iz_klyucha: str) -> dict:
    """Переложить все шифротексты с ключа `iz_klyucha` под нынешний.

    Одной транзакцией: половина строк под старым ключом, половина под новым —
    состояние, из которого без второй копии выхода нет. Транзакцию фиксирует
    вызывающий.
    """
    itog = {"perelozheno": 0, "ne_otkrylis": 0, "tot_zhe_klyuch": False, "bez_klyucha": False}
    if not iz_klyucha:
        # Копия снята до того, как копии стали возить ключ. Переложить нечем, и
        # объявлять это успехом нельзя: на чужой машине она уже потеряна.
        itog["bez_klyucha"] = True
        return itog
    if hmac.compare_digest(iz_klyucha.encode("utf-8"), get_settings().secret_key.encode("utf-8")):
        # Восстановление на своей же машине: токены и так под нынешним ключом,
        # и переписывать их — трогать данные без причины.
        itog["tot_zhe_klyuch"] = True
        return itog
    for model, kolonka, naznachenie in MESTA:
        novye: dict[int, str] = {}
        for nomer, token in sekrety_repo.shifrotexty(db, model, kolonka):
            try:
                otkrytoe = secretbox.decrypt(token, naznachenie, klyuch=iz_klyucha)
            except secretbox.SecretBoxError:
                # Ключом из копии не открылся: либо токен испорчен, либо он уже
                # под нынешним ключом. Ни то, ни другое трогать нельзя, но и
                # молчать о нём — значит объявить перекладку удавшейся.
                itog["ne_otkrylis"] += 1
                continue
            novye[nomer] = secretbox.encrypt(otkrytoe, naznachenie)
        itog["perelozheno"] += sekrety_repo.perepisat(db, model, kolonka, novye)
    return itog
