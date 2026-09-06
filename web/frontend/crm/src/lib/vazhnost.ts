/**
 * Важность напоминания — от срочного к низкому.
 *
 * Перечень повторяет `database/models/task.py`: сервер отвергает чужое слово,
 * и экран не должен предлагать того, что будет отвергнуто.
 */
export const VAZHNOSTI = ["urgent", "high", "normal", "low"] as const;

export type Vazhnost = (typeof VAZHNOSTI)[number];

export const VAZHNOST_PO_UMOLCHANIYU: Vazhnost = "normal";

/** Ключи подписей. Держатся здесь, чтобы список и карточка звали их одинаково. */
export const VAZHNOST_LABEL = {
  urgent: "vazhnostUrgent",
  high: "vazhnostHigh",
  normal: "vazhnostNormal",
  low: "vazhnostLow",
} as const;

/** Чужое слово из старого ответа не должно рисовать пустой чип. */
export function vazhnost(slovo: unknown): Vazhnost {
  return VAZHNOSTI.includes(slovo as Vazhnost) ? (slovo as Vazhnost) : VAZHNOST_PO_UMOLCHANIYU;
}

/** Максимальная важность — та, вокруг которой идёт волна по краю блока. */
export function srochno(slovo: unknown): boolean {
  return vazhnost(slovo) === "urgent";
}
