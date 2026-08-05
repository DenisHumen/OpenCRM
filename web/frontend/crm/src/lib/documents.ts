import type { TFunc, TranslationKey } from "./i18n";

/** Состояния бланка — те же, что и на сервере (database/models/document.py).
 *
 * Этапы сделки каждый настраивает под себя, а у бумаги цикл один и короткий:
 * приняли — делаем — готово — отдали. Делать его настраиваемым не за чем, зато
 * на печатной квитанции и на публичной странице состояния совпадают всегда.
 */
export const DOC_STATUSES = ["issued", "in_progress", "ready", "closed", "cancelled"] as const;

export type DocStatus = (typeof DOC_STATUSES)[number];

const LABELS: Record<DocStatus, TranslationKey> = {
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
  issued: ["in_progress", "ready", "cancelled"],
  in_progress: ["ready", "cancelled"],
  ready: ["closed", "in_progress"],
  closed: [],
  cancelled: [],
};

export function statusLabel(t: TFunc, status: string): string {
  return t(LABELS[status as DocStatus] ?? "docIssued");
}

export function statusVariant(status: string): "success" | "warning" | "accent" | undefined {
  if (status === "ready") return "success";
  if (status === "in_progress") return "accent";
  if (status === "cancelled") return "warning";
  return undefined;
}

export function nextStatuses(status: string): DocStatus[] {
  return NEXT[status as DocStatus] ?? [];
}

export function isFinished(status: string): boolean {
  return status === "closed" || status === "cancelled";
}
