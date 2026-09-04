/**
 * Живые обновления: одно соединение на вкладку, намёки — подписчикам по темам.
 *
 * Устройство — docs/12-realtime.md §2, §6, §11. Коротко:
 *
 * - соединение открывается после входа и смены временного пароля, закрывается
 *   при выходе; переподключение после «похорон» браузером — своё, с паузой;
 * - намёк — «перечитай», а не данные: подписчик зовёт тот же `load()`, что и
 *   всегда, права проверяет обработчик `GET`;
 * - склейка: намёки одной темы копятся 250 мс и дают один перезапрос; вкладка
 *   в фоне копит и разбирает одним перезапросом при возвращении;
 * - состояние связи видно всегда (полоса в App.tsx), «потеряно» ≠ «выключено».
 *
 * Не в `AppProvider`: там весь контекст собран одним `useMemo`, и поток в том же
 * значении перерисовывал бы всё дерево на каждый намёк. Раздача — подпиской.
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { api } from "./api";
import { useApp } from "./app";
import { SEARCH_DELAY } from "./debounce";

export interface Namyok {
  topic: string;
  action: "created" | "updated" | "deleted";
  id: number | null;
  scope_key: number | null;
  actor_id: number | null;
  module: string | null;
}

/** Что получает подписчик: намёки темы, склеенные, либо «перечитай всё». */
export interface Sobytie {
  resync: boolean;
  hints: Namyok[];
}

export type Sostoyanie = "off" | "connecting" | "on" | "lost";

type Slushatel = (sobytie: Sobytie) => void;

/** Пауза перед повторной попыткой после закрытого насмерть соединения:
 *  контейнер подменяется секунды, и очередь мгновенных повторов легла бы на
 *  приложение в самый неудачный момент. */
const POVTOR_MS = 5000;
/** Склейка намёков одной темы — то же число, что у поиска. */
const SKLEYKA_MS = SEARCH_DELAY;
/** Запасной перезапрос экрана, пока живости нет: редкий, только видимой вкладке. */
export const ZAPASNOY_MS = 120_000;

const slushateli = new Map<string, Set<Slushatel>>();
const sostoyanie_slushateli = new Set<(s: Sostoyanie) => void>();
let istochnik: EventSource | null = null;
let sostoyanie: Sostoyanie = "off";
let nakoplennoe = new Map<string, Namyok[]>();
let taymer: number | null = null;
let otlozhennyy_resync = false;
let povtor: number | null = null;

function smenit(novoe: Sostoyanie): void {
  if (sostoyanie === novoe) return;
  sostoyanie = novoe;
  for (const kto of [...sostoyanie_slushateli]) kto(novoe);
}

function vidno(): boolean {
  return document.visibilityState === "visible";
}

/** Раздать накопленное. Зовётся по таймеру склейки и при возвращении во вкладку. */
function razobrat(): void {
  taymer = null;
  if (!vidno()) return;
  const resync = otlozhennyy_resync;
  otlozhennyy_resync = false;
  const pachka = nakoplennoe;
  nakoplennoe = new Map();
  const temy = resync ? new Set([...slushateli.keys()]) : new Set(pachka.keys());
  for (const tema of temy) {
    const komu = slushateli.get(tema);
    if (!komu) continue;
    const sobytie: Sobytie = { resync, hints: pachka.get(tema) ?? [] };
    for (const kto of [...komu]) {
      try {
        kto(sobytie);
      } catch {
        /* упавший подписчик не лишает намёков остальных */
      }
    }
  }
}

function zaplanirovat(): void {
  if (taymer !== null || !vidno()) return;
  taymer = window.setTimeout(razobrat, SKLEYKA_MS);
}

function prinyat(namyok: Namyok): void {
  const spisok = nakoplennoe.get(namyok.topic) ?? [];
  spisok.push(namyok);
  nakoplennoe.set(namyok.topic, spisok);
  zaplanirovat();
}

function zakryt(): void {
  if (povtor !== null) {
    window.clearTimeout(povtor);
    povtor = null;
  }
  if (istochnik !== null) {
    istochnik.close();
    istochnik = null;
  }
}

function otkryt(): void {
  if (istochnik !== null) return;
  smenit("connecting");
  const es = new EventSource("/api/v1/live");
  istochnik = es;
  es.onopen = () => smenit("on");
  es.addEventListener("change", (e) => {
    try {
      prinyat(JSON.parse((e as MessageEvent).data) as Namyok);
    } catch {
      /* чужой формат — молчим */
    }
  });
  es.addEventListener("resync", () => {
    // Первое подключение и «догнать нечем»: перечитать всё, что открыто.
    otlozhennyy_resync = true;
    zaplanirovat();
  });
  es.addEventListener("mode", (e) => {
    let prichina = "";
    try {
      prichina = JSON.parse((e as MessageEvent).data).reason ?? "";
    } catch {
      /* пусто */
    }
    zakryt();
    // Выключено настройкой — это выбор, а не авария: полосы нет. Шины нет —
    // потеряно: полоса есть, пробуем позже и при возвращении во вкладку.
    if (prichina === "disabled") {
      smenit("off");
    } else {
      smenit("lost");
      povtor = window.setTimeout(vosstanovit, POVTOR_MS * 6);
    }
  });
  es.onerror = () => {
    // Сеть моргнула — браузер переподключится сам (`retry:` шлёт сервер). Ответ
    // не тем кодом или типом закрывает соединение НАСМЕРТЬ: тогда пробуем сами,
    // а протухшая сессия уводит на вход, а не в бесконечный цикл.
    if (es.readyState === EventSource.CLOSED) {
      istochnik = null;
      smenit("lost");
      povtor = window.setTimeout(() => {
        povtor = null;
        api.get("/auth/me").then(vosstanovit).catch((oshibka) => {
          if (oshibka && typeof oshibka === "object" && (oshibka as { status?: number }).status === 401) {
            window.location.assign("/login");
            return;
          }
          povtor = window.setTimeout(vosstanovit, POVTOR_MS);
        });
      }, POVTOR_MS);
    } else {
      smenit("connecting");
    }
  };
}

function vosstanovit(): void {
  if (istochnik !== null) return;
  otkryt();
}

export function podpisatsya(tema: string, slushatel: Slushatel): () => void {
  let komu = slushateli.get(tema);
  if (!komu) {
    komu = new Set();
    slushateli.set(tema, komu);
  }
  komu.add(slushatel);
  return () => {
    komu!.delete(slushatel);
    if (komu!.size === 0) slushateli.delete(tema);
  };
}

const LiveContext = createContext<Sostoyanie>("off");

/** Держит соединение, пока сотрудник вошёл и живость включена. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const { user, workspace } = useApp();
  const [tekushchee, setTekushchee] = useState<Sostoyanie>(sostoyanie);
  const vklyucheno = !!user && !user.must_change_password && workspace.realtime_enabled;

  useEffect(() => {
    sostoyanie_slushateli.add(setTekushchee);
    return () => {
      sostoyanie_slushateli.delete(setTekushchee);
    };
  }, []);

  useEffect(() => {
    if (!vklyucheno) {
      zakryt();
      smenit("off");
      nakoplennoe = new Map();
      return;
    }
    otkryt();
    // Вернулись во вкладку: разобрать накопленное и поднять соединение, если
    // его похоронил сон машины.
    const vernulis = () => {
      if (!vidno()) return;
      // Вернулись — пробуем сразу, не дожидаясь отложенной попытки: человек
      // вернулся именно затем, чтобы посмотреть свежее.
      if (istochnik === null) {
        if (povtor !== null) {
          window.clearTimeout(povtor);
          povtor = null;
        }
        vosstanovit();
      }
      if (nakoplennoe.size > 0 || otlozhennyy_resync) zaplanirovat();
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      document.removeEventListener("visibilitychange", vernulis);
      zakryt();
      smenit("off");
    };
  }, [vklyucheno]);

  return <LiveContext.Provider value={tekushchee}>{children}</LiveContext.Provider>;
}

export function useLive(): Sostoyanie {
  return useContext(LiveContext);
}

/**
 * Подписать экран на тему. `onChange` — тот же `load()`, что и всегда.
 *
 * Пока живости нет (выключена или потеряна), экран перечитывается редко и
 * только видимой вкладке — тем же `load()`, а не вторым механизмом. Возврат
 * во вкладку перечитывает сразу.
 */
export function useLiveTopic(tema: string | string[], onChange: (sobytie: Sobytie) => void): void {
  const sostoyanie = useLive();
  const poslednee = useRef(onChange);
  poslednee.current = onChange;
  const temy = Array.isArray(tema) ? tema.join(",") : tema;

  useEffect(() => {
    const snyat = temy.split(",").filter(Boolean).map((t) => podpisatsya(t, (s) => poslednee.current(s)));
    return () => snyat.forEach((f) => f());
  }, [temy]);

  useEffect(() => {
    if (sostoyanie === "on" || sostoyanie === "connecting") return;
    const vidno = () => document.visibilityState === "visible";
    const timer = window.setInterval(() => {
      if (vidno()) poslednee.current({ resync: true, hints: [] });
    }, ZAPASNOY_MS);
    const vernulis = () => {
      if (vidno()) poslednee.current({ resync: true, hints: [] });
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", vernulis);
    };
  }, [sostoyanie]);
}

/**
 * Признак начатой правки: курсор в поле формы или содержимое отличается от
 * загруженного. Хук наблюдает, полями не управляет (docs/12-realtime.md §8).
 *
 * `versiya` — что-то, меняющееся при каждом перечитывании карточки (например,
 * `updated_at` записи): с новой версией исходные значения берутся заново.
 */
export function useNachatayaPravka(koren: React.RefObject<HTMLElement | null>, versiya: unknown): boolean {
  const [nachata, setNachata] = useState(false);
  const iskhodnye = useRef<Map<Element, string>>(new Map());

  const proverit = useCallback(() => {
    const el = koren.current;
    if (!el) return;
    const polya = el.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
      "input, textarea, select",
    );
    let otlichaetsya = false;
    for (const pole of polya) {
      if (!iskhodnye.current.has(pole)) iskhodnye.current.set(pole, pole.value);
      if (iskhodnye.current.get(pole) !== pole.value) otlichaetsya = true;
    }
    const aktivnyy = document.activeElement;
    const kursor = aktivnyy !== null && el.contains(aktivnyy) && ["INPUT", "TEXTAREA", "SELECT"].includes(aktivnyy.tagName);
    setNachata(otlichaetsya || kursor);
  }, [koren]);

  useEffect(() => {
    iskhodnye.current = new Map();
    const el = koren.current;
    if (!el) return;
    proverit();
    const sobytiya = ["input", "change", "focusin", "focusout"] as const;
    sobytiya.forEach((s) => el.addEventListener(s, proverit));
    return () => sobytiya.forEach((s) => el.removeEventListener(s, proverit));
  }, [koren, proverit, versiya]);

  return nachata;
}
