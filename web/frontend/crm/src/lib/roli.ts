import type { TranslationKey } from "./i18n";

type T = (key: TranslationKey, params?: Record<string, string | number>) => string;

/** Имена ролей-пресетов сервера (`permissions_service.PRESETS`) — по-английски
 *  в базе, словом интерфейса на экране; переименованная роль — как есть. */
export const ROLI_UMOLCHANIYA: Record<string, TranslationKey> = {
  "Manager": "roleManager",
  "Accountant": "roleAccountant",
  "Project manager": "roleProjectManager",
  "Director": "roleDirector",
  "Viewer": "roleViewer",
};

/** Подсказки пресетов — по ключу набора, их не переименовывают. */
export const PODSKAZKI_ROLEY: Record<string, TranslationKey> = {
  "manager": "roleManagerHint",
  "accountant": "roleAccountantHint",
  "project_manager": "roleProjectManagerHint",
  "director": "roleDirectorHint",
  "viewer": "roleViewerHint",
};

export function nazvanieRoli(t: T, name: string | null | undefined): string {
  if (!name) return "";
  const key = ROLI_UMOLCHANIYA[name];
  return key ? t(key) : name;
}

export function podskazkaRoli(t: T, preset: { key: string; hint: string }): string {
  const key = PODSKAZKI_ROLEY[preset.key];
  return key ? t(key) : preset.hint;
}
