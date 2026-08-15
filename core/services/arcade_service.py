"""Приём результатов змейки со страницы обслуживания.

Страница публичная и работает, когда приложения нет, поэтому счёт приходит
позже — из localStorage игрока, как только сайт вернулся. Проверить «честно ли
сыграно» задним числом нельзя; можно проверить, что заявленное вообще возможно.

Три границы, и все три следуют из самой игры, а не выдуманы:

* поле 20×20 = 400 клеток, змейка стартует длиной 3 — больше 397 яблок на нём
  физически не помещается;
* между двумя яблоками проходит хотя бы один такт, а такт не быстрее 60 мс —
  значит на каждое очко нужно хотя бы столько времени (берём 50 мс с запасом,
  чтобы не отказать честному игроку из-за расхождения таймеров);
* с одного адреса за час приходит не больше двух десятков партий.

Этого хватает против «отправлю счёт 999999 запросом», а против человека, который
терпеливо играет ботом на своём поле, не поможет никакая проверка на сервере —
и ради таблицы лидеров в игре на минуту она и не нужна.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from core import exceptions as errors
from core.security import tokens
from core.utils import now_utc
from database.repositories import arcade as arcade_repo

GRID = 20
START_LENGTH = 3
MAX_SCORE = GRID * GRID - START_LENGTH
MIN_MS_PER_POINT = 50
MAX_PLAYED_MS = 6 * 60 * 60 * 1000
MAX_NAME = 24
MAX_PER_HOUR = 20

#: Как подписан игрок, не назвавшийся никак.
#:
#: Ровно то же слово стоит в placeholder-е поля и подставляется при выводе
#: строки на самой странице (`docker/nginx/maintenance/maintenance.html`).
#: Пока здесь было «Аноним», страница обещала одно имя, а сервер записывал
#: другое, и в таблице рекордов соседствовали два безымянных игрока.
#: Разделить константу с той страницей нечем — она статическая и отдаётся
#: без приложения, — поэтому совпадение стережёт `tests/test_zasev_yazyk.py`.
ANON_NAME = "Anonymous"


def _clean_name(name: str) -> str:
    # Управляющие символы и переводы строк вырезаем: имя попадает в публичную
    # таблицу, а вокруг неё не должно быть сюрпризов при выводе.
    cleaned = "".join(ch for ch in (name or "") if ch.isprintable()).strip()
    return cleaned[:MAX_NAME] or ANON_NAME


def submit(db: Session, name: str, score: int, played_ms: int, ip: str) -> dict:
    if not isinstance(score, int) or not isinstance(played_ms, int):
        raise errors.ValidationError("Score must be a whole number", code="bad_score")
    if score < 1 or score > MAX_SCORE:
        raise errors.ValidationError(
            f"Score must be between 1 and {MAX_SCORE}", code="score_out_of_range"
        )
    if played_ms < 0 or played_ms > MAX_PLAYED_MS:
        raise errors.ValidationError("Implausible game duration", code="bad_duration")
    if played_ms < score * MIN_MS_PER_POINT:
        raise errors.ValidationError(
            "Score is not reachable in that time", code="score_too_fast"
        )

    ip_hash = tokens.hash_ip(ip)
    if arcade_repo.count_since(db, ip_hash, now_utc() - timedelta(hours=1)) >= MAX_PER_HOUR:
        raise errors.RateLimitedError("Too many results, try later", code="arcade_rate_limited")

    row = arcade_repo.add(db, _clean_name(name), score, played_ms, ip_hash)
    return {"id": row.id, "score": row.score, "name": row.name}


def leaderboard(db: Session, limit: int = 10) -> list[dict]:
    return [
        {"name": row.name, "score": row.score, "at": row.created_at.isoformat()}
        for row in arcade_repo.top(db, limit)
    ]
