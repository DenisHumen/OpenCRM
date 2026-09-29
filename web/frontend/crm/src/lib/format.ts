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

/** Та же сумма коротко: «900 тыс. ₽», «$1.2M». Для подписей осей, где полное
 *  число не помещается и не нужно — там читают порядок, а не копейки. */
export function formatMoneyShort(
  minor: number | null | undefined,
  currency: string,
  locale: Locale,
): string {
  if (minor === null || minor === undefined) return "—";
  try {
    return new Intl.NumberFormat(locale === "ru" ? "ru-RU" : "en-US", {
      style: "currency",
      currency,
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(minor / 100);
  } catch {
    return String(Math.round(minor / 100));
  }
}

/** Сумма из поля ввода в минорные единицы: «12,5» → 1250, «80» → 8000.
 *
 * Копейки считает браузер и только на самом краю — здесь: дальше число едет
 * целым и целым же лежит в базе. Округление до целой копейки не вольность:
 * дробных копеек не бывает, а «12.345» иначе доехало бы до сервера и получило
 * законный, но непонятный человеку отказ.
 *
 * Запятая как разделитель — обычный способ набора в русской раскладке. Мусор
 * вместо числа даёт ноль, а не `NaN`: `NaN` уехал бы в тело запроса словом
 * `null` и превратил бы опечатку в отказ без объяснения.
 */
export function toMinorUnits(typed: string): number {
  return toMinorOrNull(typed) ?? 0;
}

/** То же, но пустое или нечитаемое поле — `null`: «не указано», а не ноль.
 *
 * Пробел в тысячах — обычный набор («1 500», из буфера — с неразрывным): `Number`
 * на нём даёт `NaN`, и цена прихода ложилась нулём без единого слова (28.09.2026).
 */
export function toMinorOrNull(typed: string): number | null {
  let s = typed.replace(/[\s$€£₴₽]/g, "");
  const tochka = s.lastIndexOf(".");
  const zapyataya = s.lastIndexOf(",");
  if (tochka >= 0 && zapyataya >= 0) {
    // «1,234.56» и «1.234,56»: дробный знак — последний, второй делит тысячи.
    const [tysyachi, drob] = tochka > zapyataya ? [",", "."] : [".", ","];
    s = s.split(tysyachi).join("").replace(drob, ".");
  } else if (/^-?[1-9]\d{0,2}(,\d{3})+$/.test(s) || /^-?[1-9]\d{0,2}(\.\d{3}){2,}$/.test(s)) {
    // Перед ровно тремя цифрами знак делит тысячи: трёх знаков у денег не бывает,
    // а «1,500» в английском интерфейсе — полторы тысячи, не 1.50 (29.09.2026).
    s = s.replace(/[.,]/g, "");
  } else {
    s = s.replace(",", ".");
  }
  const m = /^(-?)(\d*)(?:\.(\d*))?$/.exec(s);
  if (!m || !(m[2] || m[3])) return null;
  // Целыми, а не `Number * 100`: 1.005 × 100 в двоичной дроби — 100.4999….
  const drob = (m[3] ?? "").padEnd(3, "0");
  const kopeyki = Number(m[2] || "0") * 100 + Number(drob.slice(0, 2)) + (Number(drob[2]) >= 5 ? 1 : 0);
  return m[1] && kopeyki ? -kopeyki : kopeyki;
}

/** День по МЕСТНОМУ календарю в виде «ГГГГ-ММ-ДД» — для `<input type="date">` и
 *  отбора периода. `toISOString().slice(0, 10)` переводит момент в UTC и в Киеве с
 *  полуночи до трёх отдаёт вчерашнее число: сегодняшнее выпадало из сводок. */
export function mestnyyDen(date: Date): string {
  const mesyats = String(date.getMonth() + 1).padStart(2, "0");
  const den = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${mesyats}-${den}`;
}

/** Количество из поля в тысячные: «1 000» → 1000000, «0,5» → 500. Целыми, без float.
 *  `null` — пусто, нечитаемо или больше трёх знаков: тихо округлять количество нельзя. */
export function toMilliOrNull(typed: string): number | null {
  const m = /^(\d*)(?:[.,](\d{0,3}))?$/.exec(typed.replace(/\s/g, ""));
  if (!m || !(m[1] || m[2])) return null;
  return Number(m[1] || "0") * 1000 + Number((m[2] ?? "").padEnd(3, "0"));
}

/** Отказ браузера до отправки: поле непустое, а числа в нём нет. Подписывается
 *  словами интерфейса в `podpisOshibki`, как отказ сервера. */
export class NechitaemoeChislo extends Error {
  klyuch: "amountUnreadable" | "quantityUnreadable";
  constructor(klyuch: "amountUnreadable" | "quantityUnreadable") {
    super(klyuch);
    this.klyuch = klyuch;
  }
}

/** Сумма для отправки: пусто — `null`, нечитаемое — отказ, а не тихий ноль.
 *  Ноль уезжал в платёж и в цену прихода без единого слова (разбор 29.09.2026). */
export function summaIliOtkaz(typed: string): number | null {
  const v = toMinorOrNull(typed);
  if (v === null && typed.trim()) throw new NechitaemoeChislo("amountUnreadable");
  return v;
}

/** Количество для отправки: пусто — `null`, нечитаемое — отказ. */
export function kolichestvoIliOtkaz(typed: string): number | null {
  const v = toMilliOrNull(typed);
  if (v === null && typed.trim()) throw new NechitaemoeChislo("quantityUnreadable");
  return v;
}

/** Ставка из базисных пунктов в проценты: 500 → «5%», 650 → «6,5%».
 *
 * Ставка хранится целой в сотых долях процента ровно затем, чтобы не проходить
 * через двоичную дробь: `float` на пути денег в проекте запрещён. Делим на
 * 10 000 только здесь, на самом краю, где число уже никуда не пойдёт дальше в
 * расчёты, — тем же приёмом, что копейки в `formatMoney`.
 *
 * Знак процента ставит `Intl`, а не мы: в русской типографике он отбивается
 * пробелом, в английской нет, и зашитый «%» был бы неверен ровно в одном из
 * двух языков.
 */
export function formatRate(bp: number | null | undefined, locale: Locale): string {
  if (bp === null || bp === undefined) return "—";
  try {
    return new Intl.NumberFormat(locale === "ru" ? "ru-RU" : "en-US", {
      style: "percent",
      maximumFractionDigits: 2,
    }).format(bp / 10000);
  } catch {
    return bp / 100 + "%";
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

/**
 * Размер ФАЙЛА. Отдельно от `formatBytes` — та про место на диске.
 *
 * Разница в одном знаке после запятой начиная с мегабайтов, и она не
 * косметическая: у файлов почти всё лежит в диапазоне единиц мегабайт, и «1 MB»
 * вместо «1.4 MB» прячет разницу втрое. У свободного места, наоборот, единицы —
 * гигабайты, и дробь там шум.
 */
export function fileSize(bytes: number): string {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  // Архив доски из тридцати работ переваливает за гигабайт, и «2048.0 MB»
  // читается хуже, чем «2.0 GB», хотя это одно и то же.
  return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB";
}

export function fileExt(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "?" : name.slice(dot + 1).toUpperCase().slice(0, 4);
}

/** Промежуток словами: «3 д», «5 ч», «12 мин». Для «сколько простояла в
 *  этапе» точнее не нужно, а секунды там читались бы как счётчик. */
export function formatSpan(ms: number, locale: Locale): string {
  const min = Math.max(0, Math.round(ms / 60_000));
  const ru = locale === "ru";
  if (min < 60) return ru ? `${min} мин` : `${min} min`;
  const hours = Math.round(min / 60);
  if (hours < 48) return ru ? `${hours} ч` : `${hours} h`;
  const days = Math.round(hours / 24);
  return ru ? `${days} д` : `${days} d`;
}

/** Как давно это было — короткой строкой: «5 мин назад», «вчера», «3 дн. назад».
 *
 * Отдельно от `formatSpan`: тот меряет ДЛИТЕЛЬНОСТЬ («звонок 3 мин»), а здесь
 * ДАВНОСТЬ, и по-русски это разные слова. В колонке присутствия полная дата со
 * временем занимала половину ширины и отвечала не на тот вопрос: спрашивают
 * «давно ли», а не «когда именно».
 */
export function davnost(iso: string | null | undefined, locale: Locale): string {
  const date = parseDate(iso);
  if (!date) return "—";
  const ru = locale === "ru";
  const min = Math.max(0, Math.round((Date.now() - date.getTime()) / 60_000));
  if (min < 1) return ru ? "только что" : "just now";
  if (min < 60) return ru ? `${min} мин назад` : `${min} min ago`;
  const hours = Math.round(min / 60);
  if (hours < 24) return ru ? `${hours} ч назад` : `${hours} h ago`;
  if (hours < 48) return ru ? "вчера" : "yesterday";
  const days = Math.round(hours / 24);
  if (days < 30) return ru ? `${days} дн. назад` : `${days} d ago`;
  return formatDate(iso, locale);
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
