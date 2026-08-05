import type { Locale } from "./i18n";

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]!.toUpperCase())
    .join("") || "?";
}

/** Серверные даты — naive UTC ISO; приводим к Date с явным Z. */
export function parseDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  return new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
}

export function formatDate(iso: string | null | undefined, locale: Locale): string {
  const date = parseDate(iso);
  if (!date) return "—";
  return date.toLocaleDateString(locale === "ru" ? "ru-RU" : "en-US", {
    day: "numeric",
    month: "short",
    year: date.getFullYear() === new Date().getFullYear() ? undefined : "numeric",
  });
}

export function formatDateTime(iso: string | null | undefined, locale: Locale): string {
  const date = parseDate(iso);
  if (!date) return "—";
  const lang = locale === "ru" ? "ru-RU" : "en-US";
  const time = date.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDay) return (locale === "ru" ? "сегодня, " : "today, ") + time;
  if (date.toDateString() === yesterday.toDateString())
    return (locale === "ru" ? "вчера, " : "yesterday, ") + time;
  return formatDate(iso, locale) + ", " + time;
}

export function relativeDay(iso: string | null | undefined, locale: Locale): string {
  const date = parseDate(iso);
  if (!date) return "—";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return locale === "ru" ? "Сегодня" : "Today";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString())
    return locale === "ru" ? "Вчера" : "Yesterday";
  return formatDate(iso, locale);
}

/** Сумма из минимальных единиц (копеек, центов) в читаемый вид.
 *
 * В базе деньги лежат целыми: на дробных типах округление вылезает всегда, и
 * сумма колонки расходится с суммой карточек. Делим на 100 только здесь, на
 * самом краю, где число уже никуда не пойдёт дальше в расчёты.
 *
 * Копейки показываем, только когда они есть: «1 500 ₽» читается быстрее, чем
 * «1 500,00 ₽», а в канбане это десятки чисел подряд.
 */
export function formatMoney(
  minor: number | null | undefined,
  currency: string,
  locale: Locale,
): string {
  if (minor === null || minor === undefined) return "—";
  const whole = minor % 100 === 0;
  try {
    return new Intl.NumberFormat(locale === "ru" ? "ru-RU" : "en-US", {
      style: "currency",
      currency,
      minimumFractionDigits: whole ? 0 : 2,
      maximumFractionDigits: 2,
    }).format(minor / 100);
  } catch {
    // Неизвестный код валюты уронил бы Intl, а вместе с ним и весь экран.
    // Показать сумму без обозначения лучше, чем не показать ничего.
    return (minor / 100).toFixed(whole ? 0 : 2);
  }
}

/** Количество из тысячных долей единицы в читаемый вид: 1500 → «1.5», 2000 → «2».
 *
 * Делим целочисленно, а не через `/ 1000`: количество и хранится целым ровно
 * затем, чтобы не проходить через двоичную дробь. Хвостовые нули убираем —
 * «0.5 кг» читается быстрее, чем «0.500 кг», а в списке это десятки строк. */
export function formatQuantity(milli: number | null | undefined): string {
  if (milli === null || milli === undefined) return "—";
  const sign = milli < 0 ? "-" : "";
  const abs = Math.abs(milli);
  const frac = abs % 1000;
  const whole = (abs - frac) / 1000;
  if (frac === 0) return sign + whole;
  return sign + whole + "." + String(frac).padStart(3, "0").replace(/0+$/, "");
}

/** Компактный размер: 4.2 GB, 512 MB, 18 KB. */
export function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (Math.abs(value) >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  const digits = unit >= 3 ? 1 : 0;
  return `${value.toFixed(digits)} ${units[unit]}`;
}

export function fileSize(bytes: number): string {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

export function fileExt(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "?" : name.slice(dot + 1).toUpperCase().slice(0, 4);
}

export function formatDuration(sec: number | null | undefined): string | null {
  if (!sec) return null;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * Длительность звонка: null и 0 — разные вещи.
 *
 * null — станция длительность не прислала или звонок ещё идёт; 0 — сняли трубку
 * и тут же положили. formatDuration схлопывает их в одно (оба «ничего»), а в
 * журнале звонков это два разных события, и путать их нельзя.
 */
export function formatCallDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
