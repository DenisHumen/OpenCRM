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
import { cachedModules, moduleOn, rememberModules } from "./modules";

// проверка места дешёвая, но не бесплатная: обновляем раз в пару минут
const STORAGE_POLL_MS = 120_000;
// пинг присутствия: держит last_seen свежим, пока вкладка открыта
const HEARTBEAT_MS = 45_000;

interface Toast {
  id: number;
  text: string;
  error?: boolean;
}

export interface MaintenanceState {
  enabled: boolean;
  note: string;
  since: string;
  by: string;
}

export interface ModuleInfo {
  key: string;
  enabled: boolean;
  core: boolean;
  ready: boolean;
  requires: string[];
  required_by: string[];
  /** Кого выключение уведёт за собой — считает сервер, обходя дерево связей. */
  off_takes: string[];
  /** Кого включение поднимет следом — по той же причине считает сервер. */
  on_needs: string[];
  updated_at: string | null;
  updated_by_name: string | null;
}

export interface Workspace {
  brand_name: string;
  currency: string;
  /** Как этот бизнес называет свои записи: deal | order | request | booking. */
  deal_term: string;
}

interface AppContextValue {
  user: User | null;
  ready: boolean;
  locale: Locale;
  t: TFunc;
  settings: Record<string, string>;
  storage: StorageStatus | null;
  maintenance: MaintenanceState | null;
  /** Ключ блока → включён ли. null, пока не загружено. */
  modules: Record<string, boolean> | null;
  refreshModules: () => Promise<void>;
  /** Валюта и названия — нужны всем, а полные настройки читает только root. */
  workspace: Workspace;
  refreshWorkspace: () => Promise<void>;
  /** Просроченные напоминания. Живут в общем состоянии, а не в сайдбаре:
   *  закрыл задачу — число обязано измениться сразу, иначе счётчику
   *  перестают верить, и он становится хуже, чем его отсутствие. */
  overdueTasks: number;
  refreshTasks: () => Promise<void>;
  setUser: (user: User | null) => void;
  setMaintenance: (enabled: boolean, note: string) => Promise<void>;
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
  const [maintenance, setMaintenanceState] = useState<MaintenanceState | null>(null);
  // Начинаем с карты блоков прошлой загрузки, а не с пустой: иначе блоки,
  // выключенные по умолчанию, успевают мелькнуть в меню до ответа сервера
  // (обоснование — в lib/modules.ts).
  const [modules, setModules] = useState<Record<string, boolean> | null>(cachedModules);
  const [workspace, setWorkspace] = useState<Workspace>({
    brand_name: "",
    currency: "USD",
    deal_term: "deal",
  });
  const [overdueTasks, setOverdueTasks] = useState(0);
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
    // Режим обслуживания — тоже только для root. Нужен ему постоянно, а не
    // только на странице настроек: забытый включённым режим держит сайт
    // закрытым молча, поэтому о нём напоминает полоса в шапке.
    try {
      setMaintenanceState(await api.get<MaintenanceState>("/settings/maintenance"));
    } catch {
      setMaintenanceState(null);
    }
  }, []);

  const setMaintenance = useCallback(async (enabled: boolean, note: string) => {
    setMaintenanceState(await api.post<MaintenanceState>("/settings/maintenance", { enabled, note }));
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

  // Набор блоков общий для всей системы, а не личный: его читают все сотрудники,
  // иначе интерфейс не знает, что показывать в меню. Переключает только root.
  const refreshModules = useCallback(async () => {
    try {
      const data = await api.get<{ items: ModuleInfo[] }>("/modules");
      const map = Object.fromEntries(data.items.map((m) => [m.key, m.enabled]));
      setModules(map);
      // Запоминаем только настоящий ответ: заглушка ниже не должна остаться в
      // кэше как «у этого бизнеса включено всё».
      rememberModules(map);
    } catch {
      // Не смогли узнать состав — показываем всё. Отсутствующий ключ читается
      // как «включён» (moduleOn), поэтому пустая карта не прячет разделы, а
      // оставляет решение серверу: выключенный блок всё равно ответит отказом.
      // Обратный порядок оставил бы человека перед CRM без единого пункта меню.
      setModules({});
    }
  }, []);

  const refreshWorkspace = useCallback(async () => {
    try {
      setWorkspace(await api.get<Workspace>("/workspace"));
    } catch {
      // Не ответило — остаёмся на значениях по умолчанию: экран без подписей
      // хуже, чем экран с подписями по умолчанию.
    }
  }, []);

  // Счётчик просроченных напоминаний. Выключенный блок не спрашиваем вовсе:
  // отказ мы всё равно проглотим, но стучаться в закрытый раздел на каждой
  // загрузке — то же самое «выключено, а оно живёт», только в журнале сервера.
  // Проверка не заменяет `catch`: карта блоков могла ещё не приехать.
  const tasksOn = moduleOn(modules, "tasks");

  const refreshTasks = useCallback(async () => {
    if (!tasksOn) {
      setOverdueTasks(0);
      return;
    }
    try {
      const data = await api.get<{ overdue: number }>("/tasks/summary");
      setOverdueTasks(data.overdue ?? 0);
    } catch {
      // Блок напоминаний выключен или недоступен — счётчика просто нет.
      setOverdueTasks(0);
    }
  }, [tasksOn]);

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
    void refreshModules();
    void refreshWorkspace();
    void refreshTasks();
  }, [user, refreshModules, refreshWorkspace, refreshTasks]);

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
      user, ready, locale, t, settings, storage, maintenance, modules, refreshModules,
      workspace, refreshWorkspace, overdueTasks, refreshTasks,
      setUser, setMaintenance, refreshSettings, refreshStorage, logout, toast, toastError, toasts,
    }),
    [user, ready, locale, t, settings, storage, maintenance, modules, refreshModules,
     workspace, refreshWorkspace, overdueTasks, refreshTasks,
     setMaintenance, refreshSettings, refreshStorage, logout, toast, toastError, toasts],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
