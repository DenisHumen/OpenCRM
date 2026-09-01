/** Флаг страны из кода ISO 3166-1 alpha-2.
 *
 * Складывается из двух региональных символов шрифтом, а не берётся из таблицы
 * картинок: новая страна тогда не требует ни правки кода, ни файла в сборке.
 */
export function flagStrany(kod: string): string {
  const k = (kod || "").trim().toUpperCase();
  if (k.length !== 2) return "";
  return String.fromCodePoint(...[...k].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

/** Название страны на языке интерфейса.
 *
 * Спрашиваем у браузера (`Intl.DisplayNames`), а не держим свою таблицу: своя —
 * это двести названий на каждый язык, которые устареют молча. Если браузер не
 * умеет, отдаём сам код: `UA` понятнее пустоты.
 */
export function nazvanieStrany(kod: string, locale: string): string {
  const k = (kod || "").trim().toUpperCase();
  if (k.length !== 2) return "";
  try {
    return new Intl.DisplayNames([locale], { type: "region" }).of(k) ?? k;
  } catch {
    return k;
  }
}
