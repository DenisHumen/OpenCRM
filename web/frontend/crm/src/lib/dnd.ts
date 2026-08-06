import type { DragEvent } from "react";

/** Класс подсветки цели. Живёт здесь же, где и обработчики, иначе правка стиля
 *  в `styles.css` разъедется с местом, которое этот класс вешает. */
const OVER = "drag-over";

/**
 * Обработчики цели перетаскивания: подсветка и приём.
 *
 * Три места держали одни и те же девять строк — зона файлов у доски, зона
 * файлов у клиента и перестановка работ. Отличался в них только последний шаг:
 * что делать с брошенным. Всё остальное — `preventDefault`, без которого
 * браузер откроет файл вместо приёма, и снятие подсветки, о котором как раз и
 * забывают: не снял на `dragLeave` — рамка остаётся гореть на пустом месте.
 *
 * Канбан заявок сюда не переведён нарочно: там подсветка колонки — это
 * состояние, общее с самим перетаскиванием (`overStage`), и подмешивать ей
 * второй способ показать то же самое значило бы держать два источника правды
 * об одном.
 */
export function dropTarget<T extends HTMLElement>(onFiles: (event: DragEvent<T>) => void) {
  return {
    onDragOver: (event: DragEvent<T>) => {
      event.preventDefault();
      event.currentTarget.classList.add(OVER);
    },
    onDragLeave: (event: DragEvent<T>) => event.currentTarget.classList.remove(OVER),
    onDrop: (event: DragEvent<T>) => {
      event.preventDefault();
      event.currentTarget.classList.remove(OVER);
      onFiles(event);
    },
  };
}
