"""Включить или выключить режим обслуживания из командной строки.

Тот же самый режим, что root переключает в настройках сайта
(`core/services/maintenance_mode.py`), — не заглушка nginx и не вторая её
разновидность. Ручка нужна там, где человека за экраном нет: переезд на MySQL
закрывает сайт на время переноса и открывает после переключения, и делает это
скрипт, а не инструкция «зайдите в настройки и нажмите».

Почему не через API. Ходить в собственный HTTP-интерфейс изнутри установки
пришлось бы с сессией root — то есть с паролем в командной строке хоста и
разбором ответа в оболочке. Здесь то же действие делается тем же сервисом
напрямую, одним заходом в контейнер приложения.

    python -m scripts.maintenance on  --note "Переезд на MySQL"
    python -m scripts.maintenance off
    python -m scripts.maintenance status

Выход: 0 — сделано, 1 — не смогли. Повторное включение включённого режима —
это успех, а не ошибка: скрипт зовут в том числе после осечки, когда состояние
неизвестно.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.services import maintenance_mode  # noqa: E402
from database import models  # noqa: E402,F401 — регистрирует модели
from database.session import SessionLocal  # noqa: E402

#: Чьим именем помечается закрытие. В колонку `maintenance_by` кладётся снимок
#: имени, и «система» здесь честнее любого человека: экран покажет, что сайт
#: закрыт не сотрудником, и искать его не нужно.
KTO = "система"


def main() -> int:
    parser = argparse.ArgumentParser(description="Режим обслуживания")
    parser.add_argument("deystvie", choices=["on", "off", "status"])
    parser.add_argument("--note", default="", help="что показать посетителю")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.deystvie == "status":
            sostoyanie = maintenance_mode.state(db)
        else:
            sostoyanie = maintenance_mode.set_mode(
                db, args.deystvie == "on", args.note, KTO
            )
            db.commit()
    finally:
        db.close()

    print("обслуживание: " + ("включено" if sostoyanie["enabled"] else "выключено"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
