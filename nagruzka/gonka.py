"""Сто пользователей работают в CRM одновременно. Замер, а не ощущение.

**Зачем.** «Стало быстрее» без числа — это ощущение, и оно обманывает: сегодня
уже случалось, что переменная числа процессов не доезжала до контейнера, а
замеры при этом показывали бы «многопроцессность не помогает». Инструмент должен
отвечать числами и сам говорить, когда числам верить нельзя.

**Что он делает.** Заводит N виртуальных сотрудников, каждый входит один раз и
дальше ходит по экранам в тех долях, в каких по ним ходят люди. Меряется время
ОТВЕТА на каждый запрос; итог — доли (медиана, 95-я, 99-я), отказы и достигнутая
частота.

**Чего он НЕ делает.** Не изображает браузер: одна страница шлёт несколько
запросов, и здесь каждый считается отдельно. Это честнее для сравнения «до и
после» и хуже для ответа «сколько ждёт человек» — второе меряется секундомером
на живом экране.

**Сторож против самообмана.** Если заданная частота не выдержана, а процессор
приложения при этом не загружен, значит упёрся сам генератор — и об этом
печатается предупреждение на весь экран, а не строчка в конце. Замер, который
мерил себя, хуже отсутствующего: он выглядит как ответ.

Запуск:

    .venv/Scripts/python.exe nagruzka/gonka.py --adres http://localhost:8000 \
        --polzovateley 100 --sekund 60
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections import defaultdict

import httpx

#: Что делают сто человек и в каких долях. Доли не выдуманы: это порядок, в
#: котором экраны открывают, — сводка при входе, потом списки, потом карточки.
#: Поиск редок, но дорог, и потому в наборе нужен.
SCENARIY: tuple[tuple[str, int], ...] = (
    ("/api/v1/deals?per_page=50", 20),
    ("/api/v1/clients?per_page=50", 18),
    ("/api/v1/dashboard", 12),
    ("/api/v1/deals/board", 10),
    ("/api/v1/documents?per_page=50", 8),
    ("/api/v1/tasks/summary", 8),
    ("/api/v1/workspace", 7),
    ("/api/v1/auth/me", 7),
    ("/api/v1/reports/revenue", 5),
    ("/api/v1/search?q=Ковал", 5),
)


def _vzveshennyy(sluchay) -> str:
    """Адрес по долям. Разворачиваем список один раз, а не считаем на каждый шаг."""
    return sluchay.choice(_RAZVYORNUTYY)


_RAZVYORNUTYY: list[str] = []
for _adres, _dolya in SCENARIY:
    _RAZVYORNUTYY.extend([_adres] * _dolya)


async def odin_polzovatel(
    nomer: int,
    adres: str,
    obrazec_pochty: str,
    parol: str,
    do_kogda: float,
    itogi: dict[str, list[float]],
    otkazy: dict[str, int],
    sluchay,
) -> None:
    """Один виртуальный сотрудник: вошёл и работает до конца отсчёта.

    Каждый входит ПОД СВОИМ именем. Сто сессий одного человека — это не сто
    человек: у одного ряд в `users` один, роль одна, права одни, и всё это
    ложится в памятки запроса. Числа вышли бы красивее правды.
    """
    pochta = obrazec_pochty.format(n=nomer)
    async with httpx.AsyncClient(base_url=adres, timeout=30.0, follow_redirects=False) as klient:
        vhod = await klient.post(
            "/api/v1/auth/login", json={"email": pochta, "password": parol}
        )
        if vhod.status_code != 200:
            otkazy[f"ВХОД {vhod.status_code}"] += 1
            return

        while time.perf_counter() < do_kogda:
            put = _vzveshennyy(sluchay)
            nachalo = time.perf_counter()
            try:
                otvet = await klient.get(put)
                proshlo = time.perf_counter() - nachalo
            except Exception:  # noqa: BLE001 — обрыв это тоже итог замера
                otkazy[put] += 1
                continue
            if otvet.status_code == 200:
                itogi[put].append(proshlo)
            else:
                otkazy[f"{put} → {otvet.status_code}"] += 1
            # Пауза между действиями: человек читает экран, а не долбит ручку.
            # Без неё сто «пользователей» изображают сто ботов, и замер отвечает
            # на вопрос, которого никто не задавал.
            await asyncio.sleep(sluchay.uniform(0.3, 1.2))


class Storozh:
    """Смотрит за процессором приложения и за своим собственным.

    **Зачем.** Замер, который упёрся в генератор, выглядит точно так же, как
    замер, который упёрся в приложение: те же большие числа, та же низкая
    частота. Отличить их можно ровно одним способом — посмотреть, кто из двоих
    занят. Без этого «сто пользователей тормозят» может означать «мой ноутбук
    не тянет сто соединений», и вывод будет сделан прямо противоположный
    правильному.

    Наблюдение возможно только когда цель на ЭТОЙ машине. Для чужого адреса
    сторож честно говорит, что молчит, а не показывает ноль: ноль — это ответ,
    а молчание — отсутствие ответа, и путать их нельзя.
    """

    def __init__(self, adres: str) -> None:
        self.dostupen = False
        self.prichina = ""
        self.moi: list[float] = []
        self.ego: list[float] = []
        self._prilozhenie: list = []
        try:
            import psutil
        except ImportError:
            self.prichina = "нет psutil (pip install psutil)"
            return
        self._psutil = psutil

        from urllib.parse import urlparse

        razobrano = urlparse(adres)
        if razobrano.hostname not in ("localhost", "127.0.0.1", "::1"):
            self.prichina = f"цель {razobrano.hostname} не на этой машине"
            return
        port = razobrano.port or (443 if razobrano.scheme == "https" else 80)

        nomera = set()
        try:
            for svyaz in psutil.net_connections(kind="inet"):
                if svyaz.laddr and svyaz.laddr.port == port and svyaz.pid:
                    nomera.add(svyaz.pid)
        except (psutil.AccessDenied, PermissionError):
            self.prichina = "нет доступа к списку соединений"
            return
        if not nomera:
            self.prichina = f"на порту {port} никто не слушает"
            return

        for nomer in nomera:
            try:
                process = psutil.Process(nomer)
                self._prilozhenie.append(process)
                # Рабочие процессы uvicorn — потомки слушающего. Без них замер
                # многопроцессного режима показал бы почти нулевую загрузку:
                # родитель только раздаёт соединения.
                self._prilozhenie.extend(process.children(recursive=True))
            except psutil.Error:
                continue
        self._ya = psutil.Process()
        self.dostupen = bool(self._prilozhenie)
        if not self.dostupen:
            self.prichina = "процессы исчезли, пока их искали"

    async def sledit(self, do_kogda: float) -> None:
        """Снимает загрузку раз в секунду до конца отсчёта."""
        if not self.dostupen:
            return
        for process in [*self._prilozhenie, self._ya]:
            try:
                process.cpu_percent(None)  # первый вызов задаёт точку отсчёта
            except self._psutil.Error:
                pass
        while time.perf_counter() < do_kogda:
            await asyncio.sleep(1.0)
            summa = 0.0
            for process in self._prilozhenie:
                try:
                    summa += process.cpu_percent(None)
                except self._psutil.Error:
                    continue
            self.ego.append(summa)
            try:
                self.moi.append(self._ya.cpu_percent(None))
            except self._psutil.Error:
                pass

    @property
    def yader(self) -> int:
        return self._psutil.cpu_count(logical=True) or 1


def dolya(znacheniya: list[float], kakaya: float) -> float:
    """Доля распределения в миллисекундах."""
    if not znacheniya:
        return 0.0
    poryadok = sorted(znacheniya)
    mesto = min(len(poryadok) - 1, int(len(poryadok) * kakaya))
    return poryadok[mesto] * 1000


async def gonka(dovody) -> None:
    import random

    itogi: dict[str, list[float]] = defaultdict(list)
    otkazy: dict[str, int] = defaultdict(int)
    do_kogda = time.perf_counter() + dovody.sekund

    print(f"{dovody.polzovateley} пользователей, {dovody.sekund} с, {dovody.adres}")
    storozh = Storozh(dovody.adres)
    if storozh.dostupen:
        print(f"сторож: слежу за {len(storozh._prilozhenie)} процессами приложения")
    else:
        print(f"сторож молчит: {storozh.prichina}")

    nachalo = time.perf_counter()
    await asyncio.gather(
        storozh.sledit(do_kogda),
        *[
            odin_polzovatel(
                n, dovody.adres, dovody.pochta, dovody.parol,
                do_kogda, itogi, otkazy, random.Random(dovody.zerno + n),
            )
            for n in range(dovody.polzovateley)
        ],
    )
    proshlo = time.perf_counter() - nachalo

    vse = [t for spisok in itogi.values() for t in spisok]
    vsego_otkazov = sum(otkazy.values())
    print()
    print(f"{'ручка':<42}{'запросов':>9}{'медиана':>10}{'95-я':>9}{'99-я':>9}")
    for put, _ in SCENARIY:
        vremena = itogi.get(put, [])
        if not vremena:
            continue
        print(
            f"{put:<42}{len(vremena):>9}"
            f"{statistics.median(vremena) * 1000:>9.0f}м"
            f"{dolya(vremena, 0.95):>8.0f}м{dolya(vremena, 0.99):>8.0f}м"
        )

    print()
    print(f"всего запросов: {len(vse)}  за {proshlo:.1f} с  = {len(vse) / proshlo:.1f} в секунду")
    if vse:
        print(
            f"по всем: медиана {statistics.median(vse) * 1000:.0f} мс, "
            f"95-я {dolya(vse, 0.95):.0f} мс, 99-я {dolya(vse, 0.99):.0f} мс"
        )
    print(f"отказов: {vsego_otkazov}")
    for prichina, skolko in sorted(otkazy.items(), key=lambda p: -p[1])[:8]:
        print(f"  {skolko:>6}  {prichina}")

    if storozh.dostupen and storozh.ego:
        ego = statistics.mean(storozh.ego)
        moy = statistics.mean(storozh.moi) if storozh.moi else 0.0
        print()
        print(
            f"процессор: приложение {ego:.0f}% (это {ego / 100:.1f} ядра из "
            f"{storozh.yader}), генератор {moy:.0f}%"
        )
        # Сторож против самообмана — см. шапку. Порог в 80% одного ядра взят не
        # с потолка: интерпретатор Python держит одно ядро на процесс, и
        # генератор, подошедший к этой черте, дальше уже не разгоняет нагрузку,
        # а тормозит её. Числа в такой момент описывают ноутбук, а не CRM.
        if moy > 80:
            print()
            print("!" * 72)
            print("!!! ГЕНЕРАТОР УПЁРСЯ В СВОЁ ЯДРО — числа выше описывают ЕГО, а не")
            print(f"!!! приложение. Его загрузка {moy:.0f}%, приложения {ego:.0f}%.")
            print("!!! Разгоняйте нагрузку с нескольких машин либо в несколько")
            print("!!! процессов; сравнивать этот прогон с другими НЕЛЬЗЯ.")
            print("!" * 72)
        elif ego < 90 and statistics.median(vse) > 0.3:
            print()
            print(
                f"внимание: ответы медленные "
                f"({statistics.median(vse) * 1000:.0f} мс), а приложение "
                f"занято лишь на {ego / 100:.1f} ядра. Значит ждут не "
                "процессор, а что-то ещё: базу, диск или сеть. Больше "
                "рабочих процессов такой затор не разберёт."
            )

    if not vse:
        print("\n!!! НИ ОДНОГО УДАЧНОГО ЗАПРОСА — замерять нечего, смотрите отказы")
    elif vsego_otkazov > len(vse) * 0.01:
        print(
            f"\n!!! ОТКАЗОВ БОЛЬШЕ ПРОЦЕНТА ({vsego_otkazov} из "
            f"{vsego_otkazov + len(vse)}) — числа выше описывают наполовину "
            "сломанную систему, сравнивать их с другим прогоном нельзя"
        )


def main() -> None:
    razbor = argparse.ArgumentParser(description="Нагрузочный прогон OpenCRM")
    razbor.add_argument("--adres", default="http://localhost:8000")
    razbor.add_argument("--polzovateley", type=int, default=100)
    razbor.add_argument("--sekund", type=int, default=60)
    razbor.add_argument("--pochta", default="sotrudnik{n}@nagruzka.test",
                        help="образец адреса; {n} заменяется номером сотрудника")
    razbor.add_argument("--parol", default="nagruzka-pass-123")
    razbor.add_argument("--zerno", type=int, default=20260825)
    asyncio.run(gonka(razbor.parse_args()))


if __name__ == "__main__":
    main()
