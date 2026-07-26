import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import type { StorageStatus } from "../components/StorageCard";
import { api, ApiError, type User } from "./api";
import { makeT, type Locale, type TFunc } from "./i18n";

// проверка места дешёвая, но не бесплатная: обновляем раз в пару минут
const STORAGE_POLL_MS = 120_000;
// пинг присутствия: держит last_seen свежим, пока вкладка открыта
const HEARTBEAT_MS = 45_000;

interface Toast {
  id: number;
  text: string;
  error?: boolean;
}

interface AppContextValue {
  user: User | null;
  ready: boolean;
  locale: Locale;
  t: TFunc;
  settings: Record<string, string>;
  storage: StorageStatus | null;
  setUser: (user: User | null) => void;
  refreshSettings: () => Promise<void>;
  refreshStorage: () => Promise<void>;
  logout: () => Promise<void>;
  toast: (text: string, error?: boolean) => void;
  toastError: (e: unknown) => void;
  toasts: Toast[];
}

const AppContext = createContext<AppContextValue>(null as never);

export function useApp() {
  return useContext(AppContext);
}

let toastSeq = 1;

export function AppProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const locale: Locale = user?.locale === "ru" ? "ru" : "en";
  const t = useMemo(() => makeT(locale), [locale]);

  const refreshSettings = useCallback(async () => {
    // настройки сайта доступны только root; менеджерам не критично
    try {
      setSettings(await api.get("/settings"));
    } catch {
      /* нет прав — оставляем пусто */
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const me = await api.get<User>("/auth/me");
        setUser(me);
      } catch {
        setUser(null);
      } finally {
        setReady(true);
      }
    })();
  }, []);

  const refreshStorage = useCallback(async () => {
    try {
      setStorage(await api.get<StorageStatus>("/system/storage"));
    } catch {
      /* не мешаем работе, если статус недоступен */
    }
  }, []);

  useEffect(() => {
    if (user?.role === "root") void refreshSettings();
  }, [user?.role, refreshSettings]);

  useEffect(() => {
    if (!user || user.must_change_password) return;
    void refreshStorage();
    const timer = window.setInterval(() => void refreshStorage(), STORAGE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [user, refreshStorage]);

  // heartbeat присутствия: пока вкладка на переднем плане, отмечаемся «в сети»
  useEffect(() => {
    if (!user || user.must_change_password) return;
    const ping = () => {
      if (document.visibilityState === "visible") void api.get("/auth/heartbeat").catch(() => {});
    };
    ping();
    const timer = window.setInterval(ping, HEARTBEAT_MS);
    document.addEventListener("visibilitychange", ping);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", ping);
    };
  }, [user]);

  const toast = useCallback((text: string, error = false) => {
    const id = toastSeq++;
    setToasts((prev) => [...prev, { id, text, error }]);
    setTimeout(() => setToasts((prev) => prev.filter((item) => item.id !== id)), 4000);
  }, []);

  const toastError = useCallback(
    (e: unknown) => {
      toast(e instanceof ApiError ? e.message : t("error"), true);
    },
    [toast, t],
  );

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({
      user, ready, locale, t, settings, storage,
      setUser, refreshSettings, refreshStorage, logout, toast, toastError, toasts,
    }),
    [user, ready, locale, t, settings, storage, refreshSettings, refreshStorage, logout, toast, toastError, toasts],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
