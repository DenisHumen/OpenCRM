"""Сколько у проекта звёзд на GitHub. Единственное место, откуда мы ходим наружу.

Правило проекта — панель и приложение на чужие серверы не ходят. Здесь сделано
исключение, названное владельцем, и обставлено так, чтобы оно осталось
исключением: ходит СЕРВЕР, а не браузер, раз в сутки, с коротким сроком
ожидания, и отказ никогда не доходит до экрана — показывается прошлое число.
"""

import json
from datetime import datetime, timedelta, timezone

from database.repositories import settings as settings_repo

#: Чей репозиторий считаем.
REPO = "DenisHumen/OpenCRM"

#: Ключи кэша. В `SETTING_DEFAULTS` их нет намеренно: `settings_service.get_all`
#: отдаёт наружу только объявленные ключи, а `update` чужие отвергает — значит
#: служебная запись не протечёт ни в ответ, ни в правку из интерфейса.
KLYUCH_ZVYOZD = "github_stars"
KLYUCH_KOGDA = "github_stars_at"

#: Раз в сутки. Число меняется медленно, а каждый лишний поход наружу — это
#: рубеж, за которым сторонний сбой становится нашим.
SROK = timedelta(hours=24)

#: Ждём коротко: ответ нужен для украшения, и задерживать из-за него ответ
#: приложения нельзя.
OZHIDANIE_SEK = 3


def _teper() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _prochitat(db) -> tuple[int | None, datetime | None]:
    stroka = settings_repo.get_row(db, KLYUCH_ZVYOZD)
    kogda_stroka = settings_repo.get_row(db, KLYUCH_KOGDA)
    zvyozd = None
    if stroka is not None and stroka.value.isdigit():
        zvyozd = int(stroka.value)
    kogda = None
    if kogda_stroka is not None:
        try:
            kogda = datetime.fromisoformat(kogda_stroka.value)
        except ValueError:
            kogda = None
    return zvyozd, kogda


def _sprosit(opener=None) -> int | None:
    """Спросить у GitHub. Возвращает `None` на любой беде — она не наша."""
    import urllib.request

    zapros = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "OpenCRM"},
    )
    otkryt = opener or urllib.request.urlopen
    try:
        with otkryt(zapros, timeout=OZHIDANIE_SEK) as otvet:
            dannye = json.loads(otvet.read().decode("utf-8"))
        znachenie = dannye.get("stargazers_count")
        return int(znachenie) if isinstance(znachenie, int) else None
    except Exception:
        # Звёзды — украшение, и ради них не падает ни одна страница: сеть,
        # таймаут, смена ответа GitHub дают `None`, а экран показывает кнопку
        # без числа. Ноль был бы утверждением, которого мы не делали.
        return None


def zvyozdy(db, opener=None) -> int | None:
    """Число звёзд: из кэша, а раз в сутки — заново.

    Срок истекает лениво, самим запросом: фоновая задача означала бы, что
    верность числа зависит от того, отработал ли таймер, а таймер здесь дороже
    самого числа.
    """
    bylo, kogda = _prochitat(db)
    svezho = kogda is not None and _teper() - kogda < SROK
    if svezho:
        return bylo

    stalo = _sprosit(opener)
    if stalo is None:
        # Не дозвонились — отдаём прошлое. Отметку времени НЕ трогаем, иначе
        # неудача продлила бы срок годности и следующая попытка ушла бы на сутки.
        return bylo

    settings_repo.write(db, KLYUCH_ZVYOZD, str(stalo))
    settings_repo.write(db, KLYUCH_KOGDA, _teper().isoformat())
    return stalo
