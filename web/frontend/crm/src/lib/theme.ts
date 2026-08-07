/**
 * Тема интерфейса CRM: светлая, тёмная, «как в системе».
 *
 * **Выбор живёт в браузере, а не в аккаунте** — в отличие от языка
 * (`users.locale`), и это не экономия на миграции.
 *
 * 1. Тема нужна ДО первой отрисовки. Ответ `/auth/me` приходит через сеть,
 *    то есть уже после того, как страница показалась: серверное значение
 *    физически не может успеть к первому кадру, и вспышка тёмного перед
 *    светлым была бы при каждом открытии. Пришлось бы всё равно держать
 *    копию в браузере — то есть два источника правды на одну настройку и
 *    вечный вопрос, какой из них главнее.
 * 2. Экраны входа и регистрации пользователя ещё не знают. Тема из аккаунта
 *    оставила бы их тёмными навсегда, а после входа страница дёргалась бы в
 *    светлое.
 * 3. Язык — свойство человека, он один и тот же везде. Тема — свойство места:
 *    та же учётная запись на планшете в цеху при дневном свете и на ноутбуке
 *    в тёмной комнате хочет разного. Само положение «как в системе» — это и
 *    есть настройка устройства, а не человека.
 *
 * Отсюда `localStorage`. Мигание снято тем, что выбор применяется до
 * отрисовки — `web/frontend/crm/public/assets/theme-boot.js`, обычный
 * (не модульный) скрипт в `<head>`, который браузер обязан выполнить прежде,
 * чем что-либо покажет. Инлайновым его сделать нельзя: у CRM
 * `script-src 'self'` без `'unsafe-inline'` (`web/middleware.py`), а
 * `<script type="module">` — это `defer`, то есть уже после первого кадра.
 *
 * Ключ и атрибут ОБЯЗАНЫ совпадать с тем, что читает boot-скрипт: он живёт
 * отдельным файлом и об этом модуле ничего не знает. Сторожит
 * `tests/test_screens.py::test_tema_do_otrisovki_chitaet_tot_zhe_klyuch`.
 */

export type Theme = "light" | "dark" | "system";

/** Порядок здесь — порядок кнопок в профиле. */
export const THEMES: readonly Theme[] = ["light", "dark", "system"];

/** Ключ в localStorage. Тот же читает public/assets/theme-boot.js. */
export const THEME_KEY = "crm:theme";

/** Атрибут на `<html>`. Значение всегда разрешённое: light либо dark. */
export const THEME_ATTR = "data-theme";

const SYSTEM_DARK = "(prefers-color-scheme: dark)";

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark" || value === "system";
}

/**
 * Что выбрано. Ничего не выбрано либо мусор в хранилище — «как в системе».
 *
 * `localStorage` бросает исключение, а не возвращает `null`, когда сайту
 * запрещено хранилище (режим инкогнито в Safari, «блокировать данные сайтов»
 * в настройках). Падать из-за темы нельзя — это уронило бы всю CRM.
 */
export function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return isTheme(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function storeTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* хранилище закрыто: тема продержится до перезагрузки страницы */
  }
}

/** Что показывать прямо сейчас: «как в системе» разворачивается в light|dark. */
export function resolveTheme(theme: Theme): "light" | "dark" {
  if (theme !== "system") return theme;
  return window.matchMedia?.(SYSTEM_DARK).matches ? "dark" : "light";
}

/**
 * Поставить тему на документ.
 *
 * В атрибут пишем РАЗРЕШЁННОЕ значение, а не «system»: тогда в CSS нет
 * медиазапроса вовсе, и обе палитры лежат по одному разу. Копия палитры (одна
 * под запрос, вторая под атрибут) разошлась бы при первой же правке цвета.
 */
export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute(THEME_ATTR, resolveTheme(theme));
}

/**
 * Следить за переключением темы в системе, пока выбрано «как в системе».
 *
 * Без этого вкладка, открытая днём, остаётся светлой после того, как система
 * вечером сама ушла в тёмное. Возвращает отписку.
 */
export function watchSystemTheme(onChange: () => void): () => void {
  const query = window.matchMedia?.(SYSTEM_DARK);
  if (!query) return () => {};
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}
