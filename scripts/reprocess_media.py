"""Перегенерация превью у уже загруженных работ.

Нужна после изменения правил обработки: файлы `card/large/thumb.webp` создаются
один раз при загрузке, поэтому новая логика сама по себе на старые работы не
распространяется. Свежий повод — длинные изображения: раньше они ужимались по
длинной стороне (у лонгрида 1:10 от `card` оставалось ~80px ширины), теперь
ограничивается ширина. Оригиналы на диске сохраняются, так что превью можно
пересобрать без перезагрузки файлов.

Примеры:
    # посмотреть, кого затронет (ничего не меняет)
    python scripts/reprocess_media.py --dry-run

    # пересобрать только длинные изображения — обычно нужно именно это
    python scripts/reprocess_media.py --long-only

    # пересобрать все изображения на всех досках
    python scripts/reprocess_media.py --all

Запускается на сервере: нужен доступ к файлу БД и каталогу storage.
"""

import argparse
import sys
from pathlib import Path

# запуск вида `python scripts/reprocess_media.py` кладёт в sys.path каталог scripts/,
# поэтому корень проекта добавляем явно
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from core.services import media_service  # noqa: E402
from database.models import Work  # noqa: E402
from database.session import SessionLocal  # noqa: E402


def _original_path(work: Work) -> Path | None:
    ext = work.mime.split("/")[-1].replace("jpeg", "jpg")
    path = media_service.work_dir(work.work_uid) / f"original.{ext}"
    return path if path.is_file() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--long-only", action="store_true", help="только длинные изображения (по умолчанию)")
    scope.add_argument("--all", action="store_true", help="все изображения")
    parser.add_argument("--board", type=int, help="ограничить одной доской (id)")
    parser.add_argument("--dry-run", action="store_true", help="показать список и выйти")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        query = select(Work).where(Work.kind == "image", Work.status == "ready")
        if args.board:
            query = query.where(Work.board_id == args.board)
        # SVG отдаётся как есть — превью у него нет
        works = [w for w in db.scalars(query) if w.mime != "image/svg+xml"]
        if not args.all:
            works = [w for w in works if media_service.is_long_image(w.width, w.height)]

        if not works:
            print("Подходящих работ не нашлось.")
            return 0

        for work in works:
            original = _original_path(work)
            label = f"#{work.id} доска {work.board_id} {work.width}×{work.height}"
            if original is None:
                print(f"  ПРОПУСК {label}: оригинал не найден, перезагрузите файл")
                continue
            if args.dry_run:
                print(f"  {label}")
                continue
            meta = media_service.process_image(work.work_uid, original)
            for field in ("width", "height", "blurhash"):
                if meta.get(field) is not None:
                    setattr(work, field, meta[field])
            print(f"  готово {label}")
        if args.dry_run:
            print(f"\n--dry-run: затронуло бы работ — {len(works)}")
        else:
            db.commit()
            print(f"\nПересобрано работ: {len(works)}")
            # имена файлов постоянные, а отдаются они с `immutable` на год: у того,
            # кто уже открывал доску, останется старая версия до жёсткой перезагрузки
            print(
                "Файлы отдаются с длинным кэшем под теми же именами — в браузере,\n"
                "где доска уже открывалась, обновите страницу с Ctrl+Shift+R."
            )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
