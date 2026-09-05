import type { TFunc, TranslationKey } from "./i18n";

/** Состояния бланка — те же, что и на сервере (database/models/document.py).
 *
 * Этапы сделки каждый настраивает под себя, а у бумаги цикл один и короткий:
 * приняли — делаем — готово — отдали. Делать его настраиваемым не за чем, зато
 * на печатной квитанции и на публичной странице состояния совпадают всегда.
 */
export const DOC_STATUSES = ["draft", "issued", "in_progress", "ready", "closed", "cancelled"] as const;

export type DocStatus = (typeof DOC_STATUSES)[number];

/** Виды бумаги — те же шесть, что и на сервере (`DOCUMENT_KINDS`).
 *
 * Порядок не алфавитный и не серверный, а по ходу работы: приняли вещь
 * (квитанция) → заказали или продали → сделали (акт) → отгрузили и приняли
 * (накладные). Список «Бланки» показывает их вперемешку, и до появления
 * категорий различить их на экране было нечем вовсе.
 */
export const DOC_KINDS = [
  "intake",
  "sales_order",
  "purchase_order",
  "act",
  "waybill_out",
  "waybill_in",
] as const;

export type DocKind = (typeof DOC_KINDS)[number];

//: Подписи СВОИ, а не взятые у экранов заказов и накладных.
//
// Там чипы стоят под заголовком «Заказы» и «Накладные», и «Покупателя» рядом с
// «Поставщику» читается верно. Здесь те же слова становятся заголовком
// категории в ряду с «Квитанция приёмки» и «Акт работ» — и «Покупателя» уже не
// отвечает на вопрос, что это за бумага. Увидено на живом экране.
const KIND_LABELS: Record<DocKind, TranslationKey> = {
  intake: "kindIntake",
  sales_order: "kindSalesOrder",
  purchase_order: "kindPurchaseOrder",
  act: "kindAct",
  waybill_out: "kindWaybillOut",
  waybill_in: "kindWaybillIn",
};

export function kindLabel(t: TFunc, kind: string): string {
  return t(KIND_LABELS[kind as DocKind] ?? "kindIntake");
}

/** Порядки списка бумаг — те же ключи, что у сервера (`documents_repo.PORYADKI`). */
export const DOC_SORTS = ["new", "old", "number", "status"] as const;

const SORT_LABELS: Record<(typeof DOC_SORTS)[number], TranslationKey> = {
  new: "sortNew",
  old: "sortOld",
  number: "sortNumber",
  status: "sortStatus",
};

export function sortLabel(t: TFunc, sort: string): string {
  return t(SORT_LABELS[sort as (typeof DOC_SORTS)[number]] ?? "sortNew");
}

const LABELS: Record<DocStatus, TranslationKey> = {
  draft: "wbDraft",
  issued: "docIssued",
  in_progress: "docInProgress",
  ready: "docReady",
  closed: "docClosed",
  cancelled: "docCancelled",
};

/** Куда можно перейти из текущего состояния.
 *
 * Сервер отказывает только в одном — оживить завершённый бланк, — но кнопка
 * «Принято» у бланка, который уже приняли, всё равно бессмысленна. Здесь
 * перечислено то, что осмысленно нажать, включая возврат «Готово → В работе»:
 * вещь забрали проверить, нашли ещё поломку, вернули в работу.
 */
const NEXT: Record<DocStatus, DocStatus[]> = {
  draft: [],
  issued: ["in_progress", "ready", "cancelled"],
  in_progress: ["ready", "cancelled"],
  ready: ["closed", "in_progress"],
  closed: [],
  cancelled: [],
};

/** Состояния накладной — своими словами: «проведена» и «принята получателем»
 *  значат не то же, что «выдан» и «выдано» у квитанции. */
export const WAYBILL_STATUS_LABEL = {
  draft: "wbDraft",
  issued: "wbPosted",
  closed: "wbConfirmed",
  cancelled: "wbCancelled",
} as const;

const ORDER_KINDS = ["sales_order", "purchase_order"];
const WAYBILL_KINDS = ["waybill_out", "waybill_in"];

/** Подпись состояния по виду бумаги. Один статус в трёх разделах подписывался
 *  тремя словами в зависимости от экрана; теперь слово идёт за видом бумаги,
 *  а не за экраном: черновик накладной в «Бланках» больше не «Выдан». */
export function statusLabel(t: TFunc, status: string, kind?: string): string {
  if (kind && WAYBILL_KINDS.includes(kind)) {
    return t(WAYBILL_STATUS_LABEL[status as keyof typeof WAYBILL_STATUS_LABEL] ?? "wbDraft");
  }
  return t(LABELS[status as DocStatus] ?? "docIssued");
}

/** Куда ведёт бумага: заказ и накладная — на свои карточки, где их проводят;
 *  ссылка на карточку бланка открывала бы заказ там, где закрыть его нельзя. */
export function paperLink(doc: { id: number; kind: string }): string {
  if (ORDER_KINDS.includes(doc.kind)) return `/orders/${doc.id}`;
  if (WAYBILL_KINDS.includes(doc.kind)) return `/waybills/${doc.id}`;
  return `/documents/${doc.id}`;
}

/** Цвет состояния — по смыслу, одинаково у бланка, заказа и накладной:
 *  выдан/принят — в работе (accent), готов — ждёт (warning), закрыт — сделано
 *  (success), отменён — беда (danger). Черновик и прочее — нейтрально. */
export function statusVariant(status: string): "success" | "warning" | "accent" | "danger" | undefined {
  if (status === "issued" || status === "in_progress") return "accent";
  if (status === "ready") return "warning";
  if (status === "closed") return "success";
  if (status === "cancelled") return "danger";
  return undefined;
}

export function nextStatuses(status: string): DocStatus[] {
  return NEXT[status as DocStatus] ?? [];
}

export function isFinished(status: string): boolean {
  return status === "closed" || status === "cancelled";
}
