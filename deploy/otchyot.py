"""Отчёт об обновлении: что произошло, по шагам, с вырезками из журнала.

**Зачем.** До этого о неудаче сообщалась одна строка: «обновление откатилось,
health-check не прошёл». Владелец видел, что плохо, и не видел НИЧЕГО о том,
почему. Чтобы разобраться, требовался доступ к серверу, `docker compose logs` и
умение их читать, — то есть разбор откладывался до того, кто это умеет.

Отчёт отвечает на четыре вопроса, и в таком порядке: **что случилось, на каком
шаге, что сказала машина, что делать дальше.**

Два файла, а не один: PDF открывается на телефоне сразу и выглядит одинаково
везде, Word — правится и пересылается дальше. Складываются они из одного и того
же описания, поэтому разойтись по содержанию не могут (`_nalozhit`).

**Отчёт никогда не мешает обновлению.** Всё, что здесь есть, зовётся из
`Updater._notify` под `try/except`: сообщение владельцу важнее приложенных
файлов, а обновление важнее их обоих. Не собрался отчёт — уходит обычное
сообщение, как раньше.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from deploy.dokumenty import (
    CVET_BEDA,
    CVET_TIHIY,
    CVET_UDACHA,
    PDF,
    SHRIFT,
    Shrift,
    Word,
)

#: Сколько строк журнала прикладываем. Двести — это примерно две страницы
#: мелким кеглем: достаточно, чтобы увидеть трассировку целиком, и мало, чтобы
#: отчёт не превратился в свалку, которую никто не откроет.
STROK_ZHURNALA = 200

#: Сколько знаков оставляем от одной строки. Длиннее в журнале бывают только
#: SQL-запросы целиком, и от них нужен не хвост, а начало.
ZNAKOV_V_STROKE = 300


@dataclass
class Ishod:
    """Что рассказывать. Собрано отдельно от того, чем рисовать."""

    zagolovok: str
    udacha: bool
    stroki_shapki: list[tuple[str, str]]
    shagi: list[tuple[str, bool, str]]
    prichina: str
    zhurnal: list[str]
    chto_dalshe: list[str]


def sobrat_ishod(outcome, config, zhurnal: list[str] | None = None) -> Ishod:
    """Разложить итог обновления по полочкам отчёта."""
    udacha = bool(getattr(outcome, "ok", False))

    zagolovki = {
        "deployed": "Обновление прошло",
        "up-to-date": "Обновлять нечего",
        "disabled": "Автообновление выключено",
        "waiting": "Обновление ждёт",
        "aborted": "Обновление не начиналось",
        "rolled-back": "Обновление откатилось",
        "broken": "Обновление сломало сайт",
    }
    zagolovok = zagolovki.get(outcome.status, outcome.status)

    shapka: list[tuple[str, str]] = []
    if outcome.summary:
        shapka.append(("Коммит", outcome.summary))
    if outcome.to_sha:
        bylo = (outcome.from_sha or "")[:12] or "—"
        shapka.append(("Версия", f"{bylo} → {outcome.to_sha[:12]}"))
    shapka.append(("Откуда", f"{config.repo}@{config.branch}"))
    shapka.append(("Заняло", _dlitelnost(outcome.seconds)))
    shapka.append(("Когда", datetime.now().strftime("%d.%m.%Y %H:%M:%S")))

    shagi = [(s.name, s.ok, (s.detail or "").strip()) for s in (outcome.steps or [])]

    return Ishod(
        zagolovok=zagolovok,
        udacha=udacha,
        stroki_shapki=shapka,
        shagi=shagi,
        prichina=(outcome.reason or "").strip(),
        zhurnal=_podrezat(zhurnal or []),
        chto_dalshe=_chto_dalshe(outcome),
    )


def _dlitelnost(sekund: float) -> str:
    sekund = int(sekund or 0)
    if sekund < 60:
        return f"{sekund} с"
    return f"{sekund // 60} мин {sekund % 60:02d} с"


def _podrezat(stroki: list[str]) -> list[str]:
    """Последние строки журнала, каждая — не длиннее разумного.

    Последние, а не первые: беда всегда в хвосте, а начало журнала одинаково у
    удачного и у неудачного захода.
    """
    hvost = [s.rstrip() for s in stroki if s and s.strip()][-STROK_ZHURNALA:]
    return [
        s if len(s) <= ZNAKOV_V_STROKE else s[:ZNAKOV_V_STROKE] + " …"
        for s in hvost
    ]


def _chto_dalshe(outcome) -> list[str]:
    """Что делать человеку. Отчёт без этого — это жалоба, а не отчёт.

    Советы разные по исходам нарочно: «посмотрите логи» одинаково бесполезно и
    при откате, и при сломанном сайте, а действия там прямо противоположные.
    """
    if outcome.status == "broken":
        return [
            "Сайт не поднялся ДАЖЕ ПОСЛЕ ОТКАТА — это требует человека сейчас.",
            "./opencrm.sh doctor — что именно не сходится",
            "./opencrm.sh logs app — что сказал контейнер приложения",
            "./opencrm.sh restore — вернуть базу из копии, если разошлась схема",
        ]
    if outcome.status == "rolled-back":
        return [
            "Сайт работает на прежней версии — спешить некуда, разбираться можно спокойно.",
            "Коммит запомнен как неудачный и сам повторяться не будет.",
            "Причина — ниже, в шагах и в журнале. Следующий коммит поедет как обычно.",
        ]
    if outcome.status == "aborted":
        return [
            "До живого сайта дело не дошло — ничего не менялось.",
            "Обычная причина — красные проверки GitHub на этом коммите.",
        ]
    if outcome.status == "deployed":
        return [
            "Ничего делать не нужно. Файл приложен, чтобы было видно, что и как прошло.",
        ]
    return []


# =============================================================================
# Наложение на бумагу
# =============================================================================


def _nalozhit(list_, ishod: Ishod) -> None:
    """Разложить исход по странице. Один порядок на оба писателя.

    Одно тело на PDF и на Word намеренно: у писателей одинаковые имена приёмов
    (`plashka`, `zagolovok`, `tekst`, `stroka_shaga`, `kod`), и пока их
    заполняет один код, два файла об одном событии не могут разойтись. Две
    копии этой раскладки разъехались бы на первой же правке одной из них.
    """
    cvet = CVET_UDACHA if ishod.udacha else CVET_BEDA
    list_.plashka(ishod.zagolovok, cvet)

    for imya, znachenie in ishod.stroki_shapki:
        list_.para(imya, znachenie)

    if ishod.prichina:
        list_.zagolovok("Причина")
        list_.tekst(ishod.prichina, kegl=11, cvet=cvet)

    if ishod.chto_dalshe:
        list_.zagolovok("Что делать")
        for stroka in ishod.chto_dalshe:
            list_.tekst(stroka, kegl=10)

    if ishod.shagi:
        list_.zagolovok("Шаги обновления")
        for imya, proshel, podrobnost in ishod.shagi:
            list_.stroka_shaga(imya, proshel, podrobnost)

    if ishod.zhurnal:
        list_.zagolovok("Журнал обновления")
        list_.tekst(
            f"последние {len(ishod.zhurnal)} строк",
            kegl=8,
            cvet=CVET_TIHIY,
        )
        list_.kod(ishod.zhurnal)


def sdelat_fayly(outcome, config, zhurnal: list[str] | None = None) -> list[tuple[str, bytes]]:
    """Два файла отчёта: `(имя, содержимое)`. Пустой список — не собралось.

    Имя несёт дату и номер коммита: в переписке эти файлы копятся, и «отчёт.pdf»
    двадцатый по счёту не находится никогда.
    """
    ishod = sobrat_ishod(outcome, config, zhurnal)
    metka = datetime.now().strftime("%Y%m%d-%H%M")
    kusok = (outcome.to_sha or "")[:12] or "bez-kommita"
    osnova = f"obnovlenie-{metka}-{kusok}"

    fayly: list[tuple[str, bytes]] = []

    # PDF и Word собираются по отдельности: не собрался один — второй всё равно
    # уйдёт. Отчёт наполовину лучше, чем никакого.
    try:
        pdf = PDF(Shrift(SHRIFT))
        _nalozhit(pdf, ishod)
        fayly.append((f"{osnova}.pdf", pdf.sobrat()))
    except Exception:  # noqa: BLE001 — отчёт не имеет права ронять уведомление
        pass

    try:
        word = Word()
        _nalozhit(word, ishod)
        fayly.append((f"{osnova}.docx", word.sobrat()))
    except Exception:  # noqa: BLE001
        pass

    return fayly
