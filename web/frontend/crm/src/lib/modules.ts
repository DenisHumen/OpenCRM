/** Включён ли блок системы.
 *
 * Отсутствие ключа читается как «включён», и это намеренно. Карта блоков может
 * быть ещё не загружена или не загрузиться вовсе; спрятать в этот момент все
 * разделы значит показать человеку пустую CRM и оставить его гадать, что
 * сломалось. Показать лишний пункт безопаснее: сервер закрывает выключенный
 * блок сам (require_module), так что дальше меню дело не пойдёт.
 */
export function moduleOn(modules: Record<string, boolean> | null, key: string): boolean {
  return modules?.[key] !== false;
}

/** Элемент интерфейса, принадлежащий блоку системы. Без `module` — общий. */
export interface Gated {
  module?: string;
}

/**
 * Что из списка показывать: пункты выключенных блоков уходят.
 *
 * Правило «выключенный блок не показываем» одно, а мест применения много: меню,
 * выпадающий список настроек, вкладки карточки, группы командной строки. Пока
 * каждое место расставляло условия по разметке, правило существовало в стольких
 * же экземплярах — и новый раздел забывали закрыть ровно в одном из них.
 * Собранный список закрывает и второе правило заодно: пустой раздел (длина 0)
 * виден вызывающему как обычная проверка, а не как отдельная забота.
 */
export function shown<T extends Gated>(
  modules: Record<string, boolean> | null,
  items: T[],
): T[] {
  return items.filter((item) => item.module === undefined || moduleOn(modules, item.module));
}

/** Последняя известная карта блоков. */
const CACHE_KEY = "opencrm:modules";

/**
 * Карта блоков с прошлой загрузки.
 *
 * Зачем: `moduleOn` читает неизвестный ключ как «включён», поэтому до ответа
 * сервера в меню на мгновение появляются блоки, выключенные по умолчанию
 * (склад, почта, телефония) — и тут же исчезают. Прятать на это время всё
 * подряд нельзя (см. `moduleOn`), а скелет вместо меню лечил бы мигание одних
 * пунктов миганием всех.
 *
 * Поэтому первое предположение делается не «всё включено», а «как было в
 * прошлый раз» — состав блоков меняют раз в жизни системы. Реестр при этом не
 * дублируется: в кэше лежит ответ сервера, а не список блоков, записанный
 * вторым экземпляром во фронтенде. Кэша нет или он испорчен — возвращаемся к
 * прежнему поведению, то есть показываем всё.
 */
export function cachedModules(): Record<string, boolean> | null {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const entries = Object.entries(parsed as Record<string, unknown>).filter(
      ([, value]) => typeof value === "boolean",
    );
    return Object.fromEntries(entries) as Record<string, boolean>;
  } catch {
    return null;
  }
}

/** Запомнить карту блоков. Зовётся только для ответа сервера, не для заглушки. */
export function rememberModules(modules: Record<string, boolean>): void {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(modules));
  } catch {
    // Приватный режим или переполненное хранилище — мигание вернётся, но
    // работать это не мешает.
  }
}
