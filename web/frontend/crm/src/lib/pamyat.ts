/**
 * Память экрана в браузере: свёрнутые разделы меню и списков.
 *
 * `localStorage` бросает исключение, а не возвращает `null`, когда сайту запрещено
 * хранилище (приватное окно Safari, политика браузера). Прочитанное при отрисовке
 * без `try`, оно роняло приложение целиком — белым экраном (разбор 29.09.2026).
 */
export function prochitat(klyuch: string): string | null {
  try {
    return localStorage.getItem(klyuch);
  } catch {
    return null;
  }
}

export function zapisat(klyuch: string, znachenie: string): void {
  try {
    localStorage.setItem(klyuch, znachenie);
  } catch {
    // Не запомнили — раздел развернётся по умолчанию, работе это не мешает.
  }
}
