import type { TFunc, TranslationKey } from "./i18n";

/**
 * Справочник источников — тот же список ключей, что и на сервере
 * (`CLIENT_SOURCES` в database/models/client.py).
 *
 * Ключ стабилен, а показываемое слово зависит от языка сотрудника: «сарафан» и
 * «word of mouth» — один и тот же источник, и отчёт обязан складывать их в одну
 * строку независимо от того, кто заводил клиента.
 */
export const CLIENT_SOURCES = [
  "referral",
  "search",
  "social",
  "ads",
  // Ставится сам, приёмом заявок с сайта (`core/services/lead_service.py`), но
  // выбрать его руками тоже надо: клиент, позвонивший после того, как посмотрел
  // сайт, пришёл оттуда же — а форму при этом не заполнял.
  "site",
  "repeat",
  "other",
] as const;

const LABELS: Record<string, TranslationKey> = {
  referral: "srcReferral",
  site: "srcSite",
  search: "srcSearch",
  social: "srcSocial",
  ads: "srcAds",
  repeat: "srcRepeat",
  other: "srcOther",
};

/**
 * Как показать источник человеку.
 *
 * Пусто — «не указан», и это не то же самое, что «другое»: первое означает, что
 * не спросили, второе — что спросили и получили ответ.
 *
 * Своё значение показываем как есть: пока справочника-таблицы нет, ключ такого
 * источника и есть его название, и подставлять вместо него ключ из словаря
 * было бы просто нечего.
 */
export function sourceLabel(key: string | null | undefined, t: TFunc): string {
  if (!key) return t("srcNone");
  const preset = LABELS[key];
  return preset ? t(preset) : key;
}
