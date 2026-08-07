"""Провайдеры АТС: как именно CRM просит станцию набрать номер.

АТС у всех разная (Asterisk, Binotel, Zadarma, «облако» оператора), и меняют её
чаще, чем CRM. Поэтому наружу торчит один метод ``originate`` и одна ошибка на
все случаи, а различия живут в классе провайдера.

Подделка (``FakeProvider``) нужна не только тестам: она же — режим «показать,
как это работает» на демо-стенде, где настоящей станции нет. В сеть из тестов
не ходим никогда: ``HttpProvider`` в них просто не выбирается.
"""

import secrets
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from core import exceptions as errors
from core.utils import normalize_external_url

# Ждать станцию долго нет смысла: команду набора она подтверждает сразу, а
# менеджер стоит перед экраном и ждёт ответа кнопки.
ORIGINATE_TIMEOUT_SECONDS = 8.0


@dataclass
class OriginateResult:
    """Что вернула станция на команду набора."""

    #: идентификатор звонка у АТС; по нему придут события вебхука
    external_id: str


class TelephonyProvider(Protocol):
    def originate(self, from_ext: str, to_number: str) -> OriginateResult:
        """Просит станцию соединить внутренний номер ``from_ext`` с ``to_number``."""


@dataclass
class FakeProvider:
    """Локальная подделка: ничего не набирает, но ведёт себя как станция.

    Список ``calls`` — то, что «ушло» в АТС: на нём тесты проверяют, что кнопка
    click-to-call дошла до провайдера, не выходя в сеть.
    """

    calls: list[tuple[str, str]] = field(default_factory=list)
    counter: int = 0
    #: Метка запуска. Счётчик живёт в памяти процесса и после перезапуска
    #: начинается заново, а база — нет: без метки демо-стенд на второй запуск
    #: выдавал ``fake-1`` для звонка, который в журнале уже лежит. Ключ звонка
    #: обязан быть новым, иначе новый разговор придёт на место старого.
    run_id: str = field(default_factory=lambda: secrets.token_hex(4))

    def originate(self, from_ext: str, to_number: str) -> OriginateResult:
        self.counter += 1
        self.calls.append((from_ext, to_number))
        return OriginateResult(external_id=f"fake-{self.run_id}-{self.counter}")


@dataclass
class HttpProvider:
    """Обобщённая станция с HTTP-командой набора.

    Универсальный вариант «отправить POST по адресу с токеном»: под него
    подходит большинство облачных АТС. Токен уходит заголовком, а не в строке
    запроса — строка запроса оседает в логах прокси целиком.
    """

    url: str
    token: str = ""

    def originate(self, from_ext: str, to_number: str) -> OriginateResult:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = httpx.post(
                self.url,
                json={"from": from_ext, "to": to_number},
                headers=headers,
                timeout=ORIGINATE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json() if response.content else {}
        except Exception as exc:  # сеть, таймаут, ответ не 2xx, битый JSON — для менеджера это одно
            raise errors.DomainError(
                "PBX did not accept the call request", code="pbx_unavailable"
            ) from exc
        external_id = str(
            data.get("call_id") or data.get("id") or data.get("uuid") or ""
        ).strip()
        if not external_id:
            # Без идентификатора события вебхука не с чем склеить: звонок
            # состоится, но в журнале останется сиротой. Честнее сказать сразу.
            raise errors.DomainError(
                "PBX response has no call id", code="pbx_no_call_id"
            )
        return OriginateResult(external_id=external_id)


#: Ключи для настройки ``telephony_provider``. Пусто — станция не подключена.
PROVIDER_KEYS = ("fake", "http")


def safe_pbx_url(url: str) -> str:
    """Адрес станции в пригодном для запроса виде; пусто — если он не задан.

    Проверяется и на записи настройки (`telephony_service.update_settings`), и
    здесь. Второй раз — не перестраховка: в базе уже может лежать значение,
    записанное до этой проверки, а в исходящий запрос оно уходит именно
    отсюда.
    """
    try:
        return normalize_external_url(url)
    except ValueError as exc:
        raise errors.ValidationError(str(exc), code="bad_telephony_url") from exc


def build(provider: str, url: str, token: str) -> TelephonyProvider:
    if provider == "fake":
        return _FAKE
    if provider == "http":
        # Адрес станции — единственное поле настроек, которое превращается в
        # исходящий запрос сервера. Непроверенным он делал из CRM ходока по
        # чужим адресам: `file:///etc/passwd`, `gopher://…` и строка на
        # мегабайт доезжали до httpx как есть. Проверка та же, что у остальных
        # внешних ссылок в системе (`core/utils.normalize_external_url`):
        # только http/https, длина и никаких управляющих символов.
        #
        # Адрес во внутренней сети при этом законен и остаётся разрешённым:
        # Asterisk почти всегда стоит рядом, в той же локальной сети, и
        # запрет частных адресов сломал бы обычную установку. Кто может
        # задать этот адрес, решает право `settings.manage` (см. safe_pbx_url).
        url = safe_pbx_url(url)
        if not url:
            raise errors.ValidationError(
                "PBX address is not set", code="telephony_not_configured"
            )
        return HttpProvider(url=url, token=token)
    raise errors.ValidationError(
        "Telephony provider is not configured", code="telephony_not_configured"
    )


# Один экземпляр на процесс: тесты смотрят в его ``calls``.
_FAKE = FakeProvider()


def fake() -> FakeProvider:
    return _FAKE
