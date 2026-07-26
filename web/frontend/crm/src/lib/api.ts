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
