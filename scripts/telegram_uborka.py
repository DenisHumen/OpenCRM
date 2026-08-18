"""Убрать переписку старше выбранного владельцем срока. Точка входа расписания.

Запускается так:

    docker compose exec -T app python -m scripts.telegram_uborka

и по расписанию — `./opencrm.sh tg-uborka` из `opencrm-telegram-uborka.timer`.

**Ничего не делает, пока владелец не назвал срок.** Умолчание — хранить вечно,
и это выбранное поведение: переписка с клиентом бывает доказательством. Прогон
при невыбранном сроке молча сообщает «уборка выключена» и выходит с нулём —
расписание не должно краснеть у того, кто уборку не включал.

**Прогон ограничен по времени и идёт пачками** (`core/services/telegram_uborka`):
`telegram_messages` — самая большая таблица системы, и удаление миллиона строк
одной транзакцией кладёт базу вместе с приёмом сообщений от клиентов. Не
успевшее уйти достанется следующему прогону: пачки берутся с самого старого
края, и граница за каждый прогон продвигается вперёд.

**Каждый прогон отчитывается числами** — и здесь, в журнале службы, и в
настройке, которую показывает экран канала. Уборка, о которой негде прочитать,
и есть то молчаливое удаление, которого допускать нельзя.

Сухой прогон (`--dry-run`) считает и не трогает ничего: им отвечают на вопрос
«сколько уйдёт» перед тем, как включать уборку впервые.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.services import storage_service, telegram_uborka  # noqa: E402
from database.session import SessionLocal  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    razbor = argparse.ArgumentParser(description="уборка старой переписки телеграма")
    razbor.add_argument(
        "--seconds",
        type=int,
        default=telegram_uborka.PREDEL_SEKUND,
        help="сколько секунд отпущено прогону",
    )
    razbor.add_argument(
        "--batch",
        type=int,
        default=telegram_uborka.PACHKA,
        help="сколько строк удалять одной транзакцией",
    )
    razbor.add_argument(
        "--dry-run",
        action="store_true",
        help="только посчитать, не удалять ничего",
    )
    dovody = razbor.parse_args()

    with SessionLocal() as db:
        itog = telegram_uborka.ubrat(
            db,
            pachka=max(1, dovody.batch),
            predel_sekund=max(0, dovody.seconds),
            na_sukho=dovody.dry_run,
        )

    if itog["status"] == "off":
        # Не выбрано — не отказ. Хранить вечно это законное решение, и по
        # умолчанию именно оно.
        print("уборка выключена: срок хранения не выбран, переписка хранится вечно")
        return 0

    granitsa = itog["cutoff"].isoformat(sep=" ", timespec="seconds")

    if itog["status"] == "dry":
        print(
            f"[сухой прогон] под срок {itog['months']} мес. (старше {granitsa}) "
            f"попадает {itog['messages']} сообщений — не удалено ничего"
        )
        return 0

    hvost = "" if itog["done"] else " (прогон уперся в предел времени, продолжит следующий)"
    print(
        f"убрано {itog['messages']} сообщений и {itog['files']} файлов, "
        f"освобождено {storage_service.format_bytes(itog['bytes'])}; "
        f"срок {itog['months']} мес., граница {granitsa}{hvost}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
