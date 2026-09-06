import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";

import type { StorageStatus } from "../components/StorageCard";
import { api, type User } from "./api";
import { podpisOshibki } from "./oshibki";
import { makeT, type Locale, type TFunc } from "./i18n";
import { cachedModules, moduleOn, rememberModules } from "./modules";
import { can } from "./permissions";
import { applyTheme, readTheme, storeTheme, watchSystemTheme, type Theme } from "./theme";

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
  /** Открывать ли поток живых обновлений. Выключено настройкой — не авария. */
  realtime_enabled: boolean;
}

interface AppContextValue {
  user: User | null;
  ready: boolean;
  locale: Locale;
  /** Светлая, тёмная или «как в системе». Живёт в браузере — почему, в lib/theme.ts. */
  theme: Theme;
  setTheme: (theme: Theme) => void;
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
  toastDismiss: (id: number) => void;
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
  // Настраивали ли в этой системе состав блоков. null — ещё не знаем; про
  // выбор признака и цену ошибки — у эффекта с мастером первого запуска.
  const [modulesUntouched, setModulesUntouched] = useState<boolean | null>(null);
  const [workspace, setWorkspace] = useState<Workspace>({
    brand_name: "",
    currency: "USD",
    deal_term: "deal",
    // Пока не ответил сервер — не открываем: иначе соединение поднялось бы и
    // тут же закрылось у тех, кто живость выключил.
    realtime_enabled: false,
  });
  const [overdueTasks, setOverdueTasks] = useState(0);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const locale: Locale = user?.locale === "ru" ? "ru" : "en";
  const t = useMemo(() => makeT(locale), [locale]);

  // Тема. Начальное значение читаем из того же хранилища, что и скрипт,
  // поставивший её до отрисовки, — здесь только держим состояние для профиля.
  const [theme, setThemeState] = useState<Theme>(readTheme);

  const setTheme = useCallback((next: Theme) => {
    storeTheme(next);
    setThemeState(next);
  }, []);

  useEffect(() => {
    applyTheme(theme);
    // «Как в системе» — не разовое решение: система переключается по расписанию,
    // и открытая с утра вкладка обязана потемнеть вечером вместе с ней.
    if (theme !== "system") return;
    return watchSystemTheme(() => applyTheme(theme));
  }, [theme]);

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
      // Ни у одного блока нет отметки о переключении — состав системы не
      // трогали ни разу.
      setModulesUntouched(data.items.every((m) => !m.updated_at));
      // Запоминаем только настоящий ответ: заглушка ниже не должна остаться в
      // кэше как «у этого бизнеса включено всё».
      rememberModules(map);
    } catch {
      // Не смогли узнать состав — показываем всё. Отсутствующий ключ читается
      // как «включён» (moduleOn), поэтому пустая карта не прячет разделы, а
      // оставляет решение серверу: выключенный блок всё равно ответит отказом.
      // Обратный порядок оставил бы человека перед CRM без единого пункта меню.
      setModules({});
      // А вот про «настраивали или нет» ответа не было вовсе, и `null` здесь
      // остаётся `null`: гадать нельзя ни в одну сторону — мастер на не
      // отвечающем сервере всё равно не соберётся.
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

  /**
   * Мастер первого запуска открывается сам.
   *
   * Иначе он не мастер первого запуска, а ссылка в настройках: до неё доходит
   * тот, кто и так знает, чего хочет, — то есть ровно не тот, ради кого экран
   * писался. Человек, только что поставивший систему, видел дашборд с нулями и
   * оставался на наборе блоков по умолчанию навсегда.
   *
   * **Признак свежести — «состав блоков не трогали ни разу»** (`updated_at`
   * пуст у всех блоков сразу). Он не врёт в ту сторону, которая дорого стоит:
   * и набор из мастера, и переключатель руками пишут строку в `module_states`,
   * поэтому настроенная система второй раз не спросит. Считать свежесть по
   * пустоте справочников нельзя — клиентов заводят и не настроив ничего, а
   * заведённый клиент не означает, что вопрос «чем вы занимаетесь» задавали.
   *
   * **Сторона размена выбрана осознанно.** Показать мастер второй раз — потеря
   * десяти секунд: он ничего не выключает и уходит по «Пропустить». Не показать
   * тому, кто его не проходил, — оставить систему ненастроенной, и починить это
   * будет некому. Поэтому спрашиваем, пока не появится доказательство обратного.
   *
   * Один раз на загрузку страницы: ушедшему по «Пропустить» мастер не должен
   * возвращаться на каждый переход по меню. Следующий вход спросит снова — это
   * и есть та самая раздражающая, но безопасная половина размена.
   */
  const navigate = useNavigate();
  const setupOffered = useRef(false);

  useEffect(() => {
    if (setupOffered.current) return;
    if (!user || user.must_change_password) return;
    // Мастер закрыт правом `settings.manage` — тем же, что и маршрут `/setup`.
    // Уводить туда менеджера значило бы показать ему отказ вместо экрана, на
    // который он не просился.
    if (!can(user, "settings.manage")) return;
    if (modulesUntouched !== true) return;
    setupOffered.current = true;
    navigate("/setup", { replace: true });
  }, [user, modulesUntouched, navigate]);

  // Только видимая вкладка. Свёрнутая спрашивала место тоже — пять запросов
  // раз в две минуты с КАЖДОЙ вкладки любого сотрудника, независимо от экрана.
  // Десять забытых вкладок на фирму — полторы тысячи запросов в час ни за чем.
  useEffect(() => {
    if (!user || user.must_change_password) return;
    const vidno = () => document.visibilityState === "visible";
    void refreshStorage();
    const timer = window.setInterval(() => {
      if (vidno()) void refreshStorage();
    }, STORAGE_POLL_MS);
    const vernulis = () => {
      if (vidno()) void refreshStorage();
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", vernulis);
    };
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
    // Одинаковые подряд не копятся: три нажатия по кнопке с отказом давали
    // три одинаковые плашки столбиком. Ошибка висит вдвое дольше — её читают,
    // а «сохранено» и так понятно; в очереди не больше четырёх.
    setToasts((prev) => {
      if (prev.some((item) => item.text === text && item.error === error)) return prev;
      return [...prev, { id, text, error }].slice(-4);
    });
    setTimeout(() => setToasts((prev) => prev.filter((item) => item.id !== id)), error ? 8000 : 4000);
  }, []);

  const toastError = useCallback(
    (e: unknown) => {
      toast(podpisOshibki(e, t), true);
    },
    [toast, t],
  );

  const toastDismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({
      user, ready, locale, theme, setTheme, t, settings, storage, maintenance, modules, refreshModules,
      workspace, refreshWorkspace, overdueTasks, refreshTasks,
      setUser, setMaintenance, refreshSettings, refreshStorage, logout, toast, toastError, toastDismiss, toasts,
    }),
    [user, ready, locale, theme, setTheme, t, settings, storage, maintenance, modules, refreshModules,
     workspace, refreshWorkspace, overdueTasks, refreshTasks,
     setMaintenance, refreshSettings, refreshStorage, logout, toast, toastError, toastDismiss, toasts],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
