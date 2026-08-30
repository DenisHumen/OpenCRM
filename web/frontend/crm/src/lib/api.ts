const API = "/api/v1";

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)opencrm_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function request<T>(method: string, path: string, body?: unknown, form?: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(API + path, {
    method,
    headers,
    credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : form,
  });
  if (response.status === 204) return undefined as T;
  let data: any = null;
  try {
    data = await response.json();
  } catch {
    /* пустое тело */
  }
  if (!response.ok) {
    const err = data?.error ?? {};
    throw new ApiError(response.status, err.code ?? "http_error", err.message ?? response.statusText);
  }
  return data as T;
}

export const api = {
  get: <T = any>(path: string) => request<T>("GET", path),
  post: <T = any>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T = any>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T = any>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T = any>(path: string) => request<T>("DELETE", path),
  upload: <T = any>(path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<T>("POST", path, undefined, form);
  },
  zagruzka: zagruzka,
};

/** Ход заливки: сколько байт ушло из скольких. */
export interface KhodZagruzki {
  ushlo: number;
  vsego: number;
}

export interface Zalivka<T> {
  /** Готовая запись — так же, как у обычной отправки. */
  gotovo: Promise<T>;
  /** Бросить заливку на полпути. */
  otmenit: () => void;
}

/**
 * Отправить файл, сообщая о ходе отправки.
 *
 * **Почему не `fetch`.** Он не рассказывает, сколько уже ушло: обещание молчит
 * до конца запроса. На стомегабайтном видео это выглядит как зависший экран —
 * человек не знает, идёт заливка или всё сломалось, и жмёт ещё раз.
 * `XMLHttpRequest` старше и многословнее, но событие `upload.progress` есть
 * только у него; замены в браузерах пока нет.
 *
 * Отмена — половина смысла показанного прогресса: увидев «12 МБ из 300»,
 * человек должен иметь возможность передумать, а не ждать конца впустую.
 */
function zagruzka<T = any>(
  path: string,
  file: File,
  khod?: (k: KhodZagruzki) => void,
  /** Спутники файла одной формой: подпись, «в ответ на» и что там ещё
   *  понадобится. Отдельным запросом их не отправить — сообщение с файлом
   *  создаётся один раз, и подпись, приехавшая следом, была бы ВТОРЫМ
   *  сообщением у клиента в телеграме. */
  polya?: Record<string, string>,
): Zalivka<T> {
  const xhr = new XMLHttpRequest();
  const gotovo = new Promise<T>((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    for (const [imya, znachenie] of Object.entries(polya ?? {})) {
      form.append(imya, znachenie);
    }
    xhr.open("POST", API + path, true);
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-CSRF-Token", csrfToken());

    xhr.upload.onprogress = (e) => {
      // `lengthComputable` — не формальность: без длины `total` равен нулю, и
      // доля обратилась бы в NaN, а полоса — в пустое место.
      if (e.lengthComputable) khod?.({ ushlo: e.loaded, vsego: e.total });
    };

    xhr.onload = () => {
      let data: any = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* пустое тело */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data as T);
        return;
      }
      const err = data?.error ?? {};
      reject(
        new ApiError(
          xhr.status,
          err.code ?? "http_error",
          // 413 приходит от nginx, а не от приложения: тело — его страница, не
          // наш JSON. Без своего текста человек читает «Request Entity Too
          // Large» и не понимает ни что случилось, ни что делать.
          err.message ??
            (xhr.status === 413 ? "File is too large to upload" : xhr.statusText),
        ),
      );
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", "Network error"));
    xhr.onabort = () => reject(new ApiError(0, "canceled", "Canceled"));
    xhr.send(form);
  });
  return { gotovo, otmenit: () => xhr.abort() };
}

export interface PhoneCall {
  id: number;
  external_id: string;
  direction: "in" | "out";
  from_number: string;
  to_number: string;
  /** Номер собеседника: у входящего — звонивший, у исходящего — вызываемый. */
  counterparty: string;
  started_at: string | null;
  /** null — длительность неизвестна; 0 — разговор длился ноль секунд. */
  duration_sec: number | null;
  /** null — звонок ещё идёт. */
  outcome: "answered" | "missed" | "busy" | "failed" | "canceled" | null;
  has_recording: boolean;
  client_id: number | null;
  deal_id: number | null;
  user_id: number | null;
  note_id: number | null;
  created_at: string | null;
}

/** Запись журнала действий. Только читается — писать её умеет лишь сервер. */
export interface AuditEvent {
  id: number;
  /** «объект.действие»: deal.stage_changed, client.deleted, module.switched. */
  action: string;
  /** null бывает только у вебхука АТС и синхронизации почты. */
  actor_id: number | null;
  /** Имя на момент действия: снимок, а не текущее имя из справочника. */
  actor_name: string;
  source: string;
  source_ref: string;
  entity_type: string;
  entity_id: number | null;
  entity_label: string;
  /** null — величины не было; пустая строка — величина была пустой. */
  value_before: string | null;
  value_after: string | null;
  created_at: string | null;
}

export interface User {
  id: number;
  email: string;
  name: string;
  /** Владелец системы или обычный сотрудник. Набор прав — в `permissions`. */
  role: "root" | "manager";
  /** Должность с набором прав. У root её нет: права у него все и всегда. */
  role_id: number | null;
  role_name: string | null;
  status: string;
  locale: "en" | "ru";
  must_change_password: boolean;
  avatar_url: string | null;
  last_seen_at: string | null;
  is_online: boolean;
  created_at: string | null;
  approved_at: string | null;
  /** Права вида «раздел.действие». Приходят только в ответах о себе
   *  (`/auth/me`, вход, правка профиля) — чужой набор прав это чужое дело. */
  permissions?: string[];
}

export interface Role {
  id: number;
  name: string;
  preset: string;
  is_default: boolean;
  permissions?: string[];
  users_count?: number;
  created_at: string | null;
  updated_at: string | null;
}

/** Строка матрицы доступов: раздел и осмысленные в нём действия. */
export interface PermissionArea {
  key: string;
  /** Блок, вместе с которым раздел закрывается. null — раздел вне блоков. */
  module: string | null;
  system: boolean;
  actions: string[];
}

export interface RolePreset {
  key: string;
  name: string;
  hint: string;
  permissions: string[];
}
