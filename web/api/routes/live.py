"""`GET /api/v1/live` — поток намёков для вкладки сотрудника.

Устройство — `docs/ustroystvo/12-zhivye-obnovleniya.md` §3, §6, §7. Коротко: SSE без правки nginx
(`X-Accel-Buffering: no`, пульс чаще `proxy_read_timeout`), номер записи
потока в `id:` — он же `Last-Event-ID` при переподключении, три исхода на
переподключении названы явно (догнали / `resync` / `mode: off`), права и
живость сессии спрашиваются на КАЖДОЕ сообщение из свежей сессии базы.

Обработчик `async`, и в цикле нет ничего блокирующего: разговор с базой уходит
в пул потоков, иначе один синхронный вызов затормозил бы все соединения
процесса разом.
"""

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from core.live import access, bus
from core.live.message import Hint
from core.services import auth_service, settings_service
from database.models import User
from database.session import SessionLocal
from web.api.deps import SESSION_COOKIE, get_db, require_staff
from web.api.routes.telegram import MAX_ZHIZN_POTOKA

router = APIRouter(tags=["live"])

#: Пульс комментарием: nginx держит `proxy_read_timeout 120s`, пульс вдвое чаще.
PULS_SEKUND = 20
#: Как часто заглядывать в очередь: «мгновенно» для глаза, четыре раза в секунду для машины.
SHAG_SEKUND = 0.25
NASTROYKA = "realtime_enabled"


def vklyucheno(db: Session) -> bool:
    return settings_service.get_all(db).get(NASTROYKA, "1") == "1"


def _dostavit(token: str, hint: Hint) -> tuple[bool, bool]:
    """(сессия жива, намёк полагается) — из свежей сессии базы, без кэша прав."""
    with SessionLocal() as db:
        user = auth_service.get_user_by_session(db, token)
        if user is None or user.must_change_password:
            return False, False
        return True, access.delivers(db, user, hint)


def _zhiva(token: str) -> bool:
    with SessionLocal() as db:
        user = auth_service.get_user_by_session(db, token)
        return user is not None and not user.must_change_password


def _sobytie(vid: str, telo: dict, nomer: str | None = None) -> str:
    stroki = []
    if nomer:
        stroki.append(f"id: {nomer}")
    stroki.append(f"event: {vid}")
    stroki.append("data: " + json.dumps(telo, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(stroki) + "\n\n"


@router.get("/live")
async def zhivoy_potok(
    request: Request,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    token = request.cookies.get(SESSION_COOKIE, "")
    rabotaet = vklyucheno(db)
    since = request.headers.get("last-event-id") or None

    async def sobytiya():
        # Выключено настройкой или шины нет — говорим об этом первым же
        # сообщением и закрываем: молчание клиент прочёл бы как «всё свежее».
        if not rabotaet:
            yield _sobytie("mode", {"mode": "off", "reason": "disabled"})
            return
        if not await asyncio.to_thread(bus.zhiva):
            yield _sobytie("mode", {"mode": "off", "reason": "bus_unavailable"})
            return
        podpiska = await asyncio.to_thread(bus.podpisatsya)
        bus._schitat("connections", 1)
        try:
            yield "retry: 5000\n\n"
            # Догон по номеру — тем же отбором, что и живая рассылка, правами на
            # момент переподключения. Номера нет или он не в хвосте — `resync`.
            hvost = await asyncio.to_thread(bus.dognat, since) if since else None
            if hvost is None:
                yield _sobytie("resync", {"reason": "first" if not since else "gap"})
            else:
                for nomer, hint in hvost:
                    zhiv, polozhen = await asyncio.to_thread(_dostavit, token, hint)
                    if not zhiv:
                        return
                    if polozhen:
                        yield _sobytie("change", json.loads(hint.to_json()), nomer)
            nachalo = time.monotonic()
            posledniy_puls = nachalo
            while True:
                if await request.is_disconnected():
                    return
                if time.monotonic() - nachalo > MAX_ZHIZN_POTOKA:
                    return
                while True:
                    paketik = podpiska.get()
                    if paketik is None:
                        break
                    nomer, hint = paketik
                    zhiv, polozhen = await asyncio.to_thread(_dostavit, token, hint)
                    if not zhiv:
                        # Вышли из системы или удалили сотрудника: поток рвётся,
                        # а не живёт на памяти о том, кто когда-то вошёл.
                        return
                    if polozhen:
                        yield _sobytie("change", json.loads(hint.to_json()), nomer)
                if time.monotonic() - posledniy_puls > PULS_SEKUND:
                    posledniy_puls = time.monotonic()
                    if not await asyncio.to_thread(_zhiva, token):
                        return
                    # Redis лёг ПОСЛЕ подключения: соединение живо, а намёков не
                    # будет ни одного — молчание прочли бы как «всё свежее».
                    if not await asyncio.to_thread(bus.zhiva):
                        yield _sobytie("mode", {"mode": "off", "reason": "bus_unavailable"})
                        return
                    yield ": ping\n\n"
                await asyncio.sleep(SHAG_SEKUND)
        finally:
            bus._schitat("connections", -1)
            podpiska.close()

    return StreamingResponse(
        sobytiya(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
