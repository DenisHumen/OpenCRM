import type { Locale } from "./i18n";

/**
 * Как этот бизнес называет свою основную запись.
 *
 * Проверочный вопрос: «как это назовёт мастер по ремонту ноутбуков и как —
 * администратор салона?» Первый скажет «заказ», второй «запись», агентство —
 * «сделка», интернет-магазин — «заявка». Ответы разные, значит название обязано
 * быть настройкой, а не словом, зашитым в интерфейс.
 *
 * Пресет, а не свободная строка. Русский требует падежей: «Новый заказ»,
 * «Заказов пока нет», «Заказы». Свободный текст дал бы «Новый запись» и
 * «Записьов пока нет», и починить это без словаря форм невозможно — поэтому
 * формы перечислены явно, по одной на каждое место, где слово встречается.
 */
export type DealTerm = "deal" | "order" | "request" | "booking";

export type TermForm = "one" | "many" | "new" | "none" | "noneHint";

type Forms = Record<TermForm, string>;

const TERMS: Record<DealTerm, Record<Locale, Forms>> = {
  deal: {
    ru: {
      one: "Сделка",
      many: "Сделки",
      new: "Новая сделка",
      none: "Сделок пока нет",
      noneHint: "Сделка — это работа для клиента: от обращения до закрытия.",
    },
    en: {
      one: "Deal",
      many: "Deals",
      new: "New deal",
      none: "No deals yet",
      noneHint: "A deal is the work for a client: from the first contact to the close.",
    },
  },
  order: {
    ru: {
      one: "Заказ",
      many: "Заказы",
      new: "Новый заказ",
      none: "Заказов пока нет",
      noneHint: "Заказ — это работа для клиента: от приёма до выдачи.",
    },
    en: {
      one: "Order",
      many: "Orders",
      new: "New order",
      none: "No orders yet",
      noneHint: "An order is the work for a client: from intake to handover.",
    },
  },
  request: {
    ru: {
      one: "Заявка",
      many: "Заявки",
      new: "Новая заявка",
      none: "Заявок пока нет",
      noneHint: "Заявка — это обращение клиента: от первого контакта до выполнения.",
    },
    en: {
      one: "Request",
      many: "Requests",
      new: "New request",
      none: "No requests yet",
      noneHint: "A request is what the client asked for: from first contact to done.",
    },
  },
  booking: {
    ru: {
      one: "Запись",
      many: "Записи",
      new: "Новая запись",
      none: "Записей пока нет",
      noneHint: "Запись — это визит клиента: от согласования до приёма.",
    },
    en: {
      one: "Booking",
      many: "Bookings",
      new: "New booking",
      none: "No bookings yet",
      noneHint: "A booking is a client visit: from agreeing a time to the appointment.",
    },
  },
};

/** Список для выбора в настройках. Порядок — от самого частого. */
export const DEAL_TERMS: DealTerm[] = ["deal", "order", "request", "booking"];

/** Подпись блока заказов, когда заявки тоже зовутся «заказами». */
export function nazvanieZakazov(
  t: (klyuch: "orders" | "ordersStock") => string,
  zayavki: string,
): string {
  return zayavki.trim().toLowerCase() === t("orders").trim().toLowerCase() ? t("ordersStock") : t("orders");
}

export function term(name: string | undefined, locale: Locale, form: TermForm): string {
  // Неизвестное значение из базы не должно оставлять экран без подписи:
  // возвращаемся к набору по умолчанию, а не к пустой строке.
  const forms = TERMS[(name as DealTerm) in TERMS ? (name as DealTerm) : "deal"];
  return forms[locale][form];
}
