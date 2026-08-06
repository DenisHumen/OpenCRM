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
};

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
  role: "root" | "manager";
  status: string;
  locale: "en" | "ru";
  must_change_password: boolean;
  avatar_url: string | null;
  last_seen_at: string | null;
  is_online: boolean;
  created_at: string | null;
  approved_at: string | null;
}
