import type { TranslationKey } from "./i18n";

type T = (key: TranslationKey, params?: Record<string, string | number>) => string;

/**
 * Названия этапов из наборов сервера (`pipeline_service.PRESETS`) хранятся
 * по-английски — `test_zasev_yazyk`: строка в базе одна, язык интерфейса у
 * каждого свой. Экран подписывает умолчание словом интерфейса; своё название
 * этапа человек видит как есть. Сторож: каждое имя из наборов сервера обязано
 * быть в этой карте (`test_screens.py`).
 */
export const ETAPY_UMOLCHANIYA: Record<string, TranslationKey> = {
  "New": "stageNew",
  "In progress": "stageInProgress",
  "Ready": "stageReady",
  "Completed": "stageCompleted",
  "Declined": "stageDeclined",
  "Request": "stageRequest",
  "Diagnostics": "stageDiagnostics",
  "Quote approval": "stageQuoteApproval",
  "Ready for pickup": "stageReadyForPickup",
  "Handed over": "stageHandedOver",
  "Enquiry": "stageEnquiry",
  "Booked": "stageBooked",
  "Confirmed": "stageConfirmed",
  "Arrived": "stageArrived",
  "Served": "stageServed",
  "No-show": "stageNoShow",
  "New order": "stageNewOrder",
  "Paid": "stagePaid",
  "Packed": "stagePacked",
  "Shipped": "stageShipped",
  "Received": "stageReceived",
  "Cancelled": "stageCancelled",
  "Lead": "stageLead",
  "Quote": "stageQuote",
  "Review": "stageReview",
  "Won": "stageWon",
  "Lost": "stageLost",
};

export function nazvanieEtapa(t: T, name: string | null | undefined): string {
  if (!name) return "";
  const key = ETAPY_UMOLCHANIYA[name];
  return key ? t(key) : name;
}
