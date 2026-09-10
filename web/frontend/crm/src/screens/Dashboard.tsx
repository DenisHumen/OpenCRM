import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { BoardCard } from "../components/BoardCard";
import { Icon } from "../components/Icon";
import { IstochnikiKlientov } from "../components/IstochnikiKlientov";
import { NewBoardButton } from "../components/NewBoardButton";
import { PotokDeneg } from "../components/PotokDeneg";
import { StorageCard } from "../components/StorageCard";
import { OtchyotProdazh } from "../components/OtchyotProdazh";
import { TablitsaKlientov } from "../components/TablitsaKlientov";
import { VidzhetKlyucha, type KlyuchSayta } from "../components/VidzhetKlyucha";
import { Chip, EmptyState, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { orderStatusLabel, statusVariant } from "../lib/documents";
import { nazvanieEtapa } from "../lib/etapy";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { formatDateTime, formatMoney, formatQuantity, parseDate } from "../lib/format";
import type { TranslationKey } from "../lib/i18n";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import { obtech, podzhat, svobodnoeMesto, vpredelah, vysota } from "../lib/setka";

/** Виджет раскладки: вид, место и размер в сетке, параметры (ключ сайта). */
interface Vidzhet {
  id: string;
  kind: string;
  x: number;
  y: number;
  w: number;
  h: number;
  params: { key_id?: number };
}

/** Реестр с сервера (`vidzhety_service.REESTR`): размеры, блок и право. Экран
 *  свою копию не держит — две карты разошлись бы молча. */
interface OpisVidzheta {
  w: number;
  h: number;
  min_w: number;
  min_h: number;
  odin: boolean;
  module: string | null;
  perm: string | null;
}

interface ZapisVidzheta {
  kind: string;
  x: number;
  y: number;
  w: number;
  h: number;
  params: { key_id?: number };
}

interface Raskladka {
  layout: { version: number; widgets: ZapisVidzheta[] } | null;
  kinds: Record<string, OpisVidzheta>;
  grid: { cols: number; rows: number };
}

/** Высота строки сетки и зазор между блоками, пиксели. Строка мелкая нарочно:
 *  ею меряется высота, и крупный шаг не дал бы поставить плитку чуть ниже. */
const SHAG = 28;
const ZAZOR = 12;

/** Ниже этой ширины сетки нет вовсе: двенадцать колонок в телефоне — это
 *  двенадцать полосок по тридцать пикселей. Блоки идут столбцом в том порядке,
 *  в котором лежат в сетке. */
const UZKO = 900;

/** Порядок умолчания. Сверху — состав присланного макета (плитки, поток денег,
 *  источники, клиенты), ниже — всё остальное: редизайн задаёт первый экран, а
 *  не отнимает блоки у тех, кто раскладку не трогал. */
const PORYADOK_UMOLCHANIYA = [
  "money_in_work", "clients", "avg_check", "lost_share",
  "money_received", "money_won", "money_due", "calls",
  "money_flow", "client_sources",
  "recent_clients",
  "funnel", "my_tasks", "orders_week", "low_stock", "showcase_views", "storage", "recent_boards",
];

const ZAGOLOVKI: Record<string, TranslationKey> = {
  money_in_work: "moneyInWork",
  money_received: "moneyReceivedThisMonth",
  money_won: "moneyWonThisMonth",
  money_due: "dashMoneyDue",
  avg_check: "avgCheck",
  clients: "metricClients",
  calls: "dashCallsToday",
  lost_share: "dashLostShare",
  funnel: "funnel",
  my_tasks: "myTasksToday",
  orders_week: "dashOrdersWeek",
  low_stock: "dashStock",
  showcase_views: "showcaseViews",
  storage: "storage",
  recent_boards: "recentBoards",
  recent_clients: "recentClients",
  money_flow: "dashMoneyFlow",
  client_sources: "dashClientSources",
  sales_grid: "salesReport",
  sales_matrix: "salesReport",
  api_key: "dashApiKey",
};

/** Значок в шапке блока. Один на вид — шапку рисует рама, а не каждый блок
 *  сам: без общей шапки блок не за что взять рукой. */
const ZNACHKI: Record<string, string> = {
  money_in_work: "deals",
  money_received: "receipt",
  money_won: "analytics",
  money_due: "clock",
  avg_check: "star",
  clients: "clients",
  calls: "call",
  lost_share: "analytics",
  funnel: "deals",
  my_tasks: "clock",
  orders_week: "inbox",
  low_stock: "warehouse",
  showcase_views: "eye",
  recent_boards: "boards",
  recent_clients: "clients",
  money_flow: "analytics",
  client_sources: "link",
};

/** Ход из шапки блока в раздел, откуда его числа. */
const HODY: Record<string, string> = {
  money_in_work: "/deals",
  clients: "/clients",
  calls: "/calls",
  lost_share: "/deals",
  funnel: "/deals",
  my_tasks: "/tasks",
  orders_week: "/orders",
  low_stock: "/warehouse?low=1",
  showcase_views: "/boards",
  recent_boards: "/boards",
  recent_clients: "/clients",
  money_flow: "/reports",
  client_sources: "/reports",
};

/** Виды, которые рисуют карточку сами. Раме остаётся место и ручки: своя
 *  шапка у такого блока уже есть, и вторая над ней была бы вдвое. */
const SVOYA_KARTA = new Set(["storage", "sales_grid", "sales_matrix", "api_key"]);

function metka(v: { kind: string; params: { key_id?: number } }): string {
  return v.params.key_id ? `${v.kind}:${v.params.key_id}` : v.kind;
}

/** Разложить виды по порядку слева направо с переносом. Тем же способом
 *  сервер переводит записи первой версии — иначе умолчание экрана и перевод
 *  старой раскладки давали бы разные сводки одному человеку. */
function razlozhit(vidy: { kind: string; params: { key_id?: number } }[], kinds: Record<string, OpisVidzheta>, kolonok: number): Vidzhet[] {
  const itog: Vidzhet[] = [];
  let x = 0;
  let y = 0;
  let ryad = 0;
  for (const v of vidy) {
    const opis = kinds[v.kind];
    if (!opis) continue;
    if (x + opis.w > kolonok) {
      x = 0;
      y += ryad;
      ryad = 0;
    }
    itog.push({ id: metka(v), kind: v.kind, x, y, w: opis.w, h: opis.h, params: v.params });
    x += opis.w;
    ryad = Math.max(ryad, opis.h);
  }
  return itog;
}

/** Ширина экрана решает, есть ли сетка. Слушаем `matchMedia`, а не `resize`:
 *  тот дёргается на каждый пиксель, этот — на переход через границу. */
function useShirokiy(): boolean {
  const [shirokiy, setShirokiy] = useState(
    () => typeof window === "undefined" || window.matchMedia(`(min-width: ${UZKO}px)`).matches,
  );
  useEffect(() => {
    const zapros = window.matchMedia(`(min-width: ${UZKO}px)`);
    const slushatel = () => setShirokiy(zapros.matches);
    zapros.addEventListener("change", slushatel);
    return () => zapros.removeEventListener("change", slushatel);
  }, []);
  return shirokiy;
}

/** Сводка. Ширина своя (`page-summary`, 1320px), а не списочная 1800px: на
 *  обычном мониторе плитки и ленты растягивались во всю ширину и читались
 *  хуже, чем две колонки (владелец, 06.09.2026). С того же дня сводка собрана
 *  из виджетов: блоки добавляются, убираются, перетягиваются и меняют размер,
 *  раскладка хранится у сотрудника (`/dashboard/layout`); блок выключенного
 *  раздела или без права не появляется, даже если записан в раскладке.
 *
 *  С 10.09.2026 раскладка — сетка из двенадцати колонок: блок берут за шапку и
 *  тянут за правый нижний угол, остальные расступаются и падают вверх. Разбор
 *  и доводы — в `docs/dizayn/05-dizayn-crm.md`. */
export function Dashboard() {
  const { user, t, locale, storage, modules, refreshStorage, toastError, toast } = useApp();
  // Раздел «последние клиенты» — только тому, кому карточки открыты. Сервер
  // теперь отдаёт пустой список без права, и без этой проверки на экране
  // осталась бы надпись «клиентов пока нет» и ссылка в раздел, куда не пускают.
  const seesClients = can(user, "clients.view");
  const [data, setData] = useState<any>(null);
  const [raskladka, setRaskladka] = useState<Raskladka | null>(null);
  const [nastroyka, setNastroyka] = useState(false);
  const [dobavlenie, setDobavlenie] = useState(false);
  /** Что держит рука: черновик раскладки, пока идёт перетаскивание. */
  const [chernovik, setChernovik] = useState<Vidzhet[] | null>(null);
  const [derzhim, setDerzhim] = useState<string | null>(null);
  const polotno = useRef<HTMLDivElement | null>(null);
  const shirokiy = useShirokiy();
  // Ключи сайта нужны виджету наблюдения и окну добавления; без права на
  // настройки справочник не спрашивается вовсе (`null`), и виджета ключа нет.
  const klyuchi = useReference<KlyuchSayta>(can(user, "settings.manage") ? "/settings/api-keys" : null);

  const { failure, fail, clear } = useFailure();

  const [obnovleno, setObnovleno] = useState<Date | null>(null);
  //: Номер последней правки раскладки. Ответы приходят вразнобой, и без него
  //: последним словом становился бы последний ОТВЕТ, а не последняя правка.
  const pravok = useRef(0);

  /** Одна дорога за данными, два способа обойтись с отказом.
   *
   * `tikho` — фоновый перезапрос: отказ проглатывается, и на экране остаются
   * прежние числа. Иначе мигнувшая сеть стирала бы работающую сводку и
   * подставляла экран отказа человеку, который в неё даже не смотрел.
   *
   * Второй ручки за теми же данными здесь нет намеренно: два способа получать
   * одно и то же расходятся на первой же правке.
   */
  const load = useCallback(
    (tikho = false) => {
      if (!tikho) clear();
      api
        .get("/dashboard")
        .then((svezhee) => {
          setData(svezhee);
          setObnovleno(new Date());
        })
        .catch((beda) => {
          if (!tikho) fail(beda);
        });
    },
    [fail, clear],
  );

  useEffect(() => load(), [load]);

  useEffect(() => {
    api
      .get<Raskladka>("/dashboard/layout")
      .then(setRaskladka)
      .catch(fail);
  }, [fail]);

  // Сводка живая по намёкам: из тем, из которых она считается, тем же
  // обработчиком — значит и права те же. Перезапрос идёт после склейки, и
  // двадцать правок подряд дают одно чтение самой дорогой ручки.
  useLiveTopic(
    ["deals", "clients", "tasks", "finance", "documents", "orders", "boards", "warehouse", "telephony"],
    () => load(true),
  );
  // Запасного перезапроса ЗДЕСЬ нет намеренно: его ведёт `useLiveTopic` — тем
  // же периодом, тем же условием видимости и тем же молчанием при живом потоке.
  //
  // Свой такой же здесь стоял, и это была не подстраховка, а удвоение: при
  // выключенной живости самая дорогая ручка дёргалась по два раза подряд каждые
  // две минуты и ещё по два при каждом возврате на вкладку. Опыт в проекте уже
  // есть: команда, безобидная в руках человека, из цикла отрисовки дала 240
  // запросов в час и уронила боевое обновление.

  if (!data || !raskladka) return <ScreenLoading error={failure} onRetry={load} />;

  const hour = new Date().getHours();
  const greeting = hour < 12 ? t("goodMorning") : hour < 18 ? t("goodAfternoon") : t("goodEvening");
  const growth =
    data.views_prev_7d > 0
      ? Math.round(((data.views_7d - data.views_prev_7d) / data.views_prev_7d) * 100)
      : null;
  const maxDay = Math.max(1, ...data.views_by_day.map((d: any) => d.count));
  // Ширина полосы этапа — доля от самого населённого, а не от общего числа:
  // при пяти этапах доли от целого дают пять одинаково коротких полос.
  const maxStage = Math.max(1, ...data.deals_by_stage.map((s: any) => s.count));
  const openDeals = data.deals_by_stage
    .filter((s: any) => s.kind === "open")
    .reduce((sum: number, s: any) => sum + s.count, 0);
  // Доля отказов — от ЗАКРЫТЫХ заявок: сделки в работе ничем ещё не кончились,
  // и в знаменателе им не место. Ни одной закрытой — прочерк, а не ноль.
  const wonCount = data.deals_by_stage
    .filter((s: any) => s.kind === "won")
    .reduce((sum: number, s: any) => sum + s.count, 0);
  const lostCount = data.deals_by_stage
    .filter((s: any) => s.kind === "lost")
    .reduce((sum: number, s: any) => sum + s.count, 0);
  const zakryto = wonCount + lostCount;
  const dolyaOtkazov = zakryto > 0 ? (lostCount / zakryto) * 100 : null;
  const dayLabels = data.views_by_day.map((d: any) =>
    new Date(d.date + "T00:00:00").toLocaleDateString(locale === "ru" ? "ru-RU" : "en-US", { weekday: "short" }),
  );
  const sum = (value: number | null) => formatMoney(value, data.currency, locale);
  const ordersOn = data.orders_week !== null && data.orders_week !== undefined;
  const boardsOn = moduleOn(modules, "boards");
  const overdue = data.tasks_counters?.overdue ?? 0;
  const kinds = raskladka.kinds;
  const kolonok = raskladka.grid.cols;

  /** Право и блок виджета — по реестру сервера. Отдельно от данных: то, что
   *  нельзя показывать, нельзя и хранить в раскладке (сервер откажет). */
  const pozvoleno = (kind: string): boolean => {
    const opis = kinds[kind];
    if (!opis) return false;
    if (opis.module && !moduleOn(modules, opis.module)) return false;
    if (opis.perm && !can(user, opis.perm)) return false;
    if (kind === "recent_clients" && !seesClients) return false;
    return true;
  };
  /** Есть ли виджету что показать: без данных он молчит, но в раскладке остаётся. */
  const estChto = (v: Vidzhet): boolean => {
    switch (v.kind) {
      case "money_received":
        return data.money_basis === "cash";
      case "money_due":
        return data.money_due !== null;
      case "calls":
        return Boolean(data.calls_24h);
      case "orders_week":
        return ordersOn;
      case "storage":
        return Boolean(storage);
      default:
        return true;
    }
  };
  const umolchanie: Vidzhet[] = razlozhit(
    PORYADOK_UMOLCHANIYA.filter(pozvoleno).map((kind) => ({ kind, params: {} })),
    kinds,
    kolonok,
  );
  const zapisano: Vidzhet[] | null = raskladka.layout
    ? raskladka.layout.widgets.map((v) => ({ ...v, id: metka(v) }))
    : null;
  const polnyy: Vidzhet[] = zapisano ?? umolchanie;
  const vidimye = polnyy.filter((v) => pozvoleno(v.kind) && estChto(v));
  const skrytye = polnyy.filter((v) => !vidimye.includes(v));
  // Показываем поджатым: спрятанный блок оставил бы дырку ровно своего
  // размера, и человек читал бы её как сломанную вёрстку.
  const razlozhennye: Vidzhet[] =
    chernovik ??
    podzhat(vidimye.map((v) => ({ i: v.id, x: v.x, y: v.y, w: v.w, h: v.h }))).map((m) => {
      const v = vidimye.find((x) => x.id === m.i)!;
      return { ...v, x: m.x, y: m.y };
    });
  const strok = vysota(razlozhennye.map((v) => ({ i: v.id, x: v.x, y: v.y, w: v.w, h: v.h })));

  const zagolovok = (v: Vidzhet): string => {
    if (v.kind === "api_key") {
      const k = (klyuchi.items ?? []).find((x) => x.id === v.params.key_id);
      return `${t("dashApiKey")} ${k ? k.name : `#${v.params.key_id}`}`;
    }
    if (v.kind === "money_won" && data.money_basis === "cash") return t("moneyWonValue");
    return t(ZAGOLOVKI[v.kind] ?? "funnel");
  };

  /** Сохранить раскладку. Виджеты без права или с выключенным блоком не
   *  записываются: сервер отказал бы всей раскладке, и человек не смог бы
   *  сохранить ничего, пока сосед не включит блок обратно.
   *
   *  Спрятанные по данным блоки дописываются ПОД видимыми: они в раскладке
   *  остаются (завтра касса включится, и блок вернётся туда, где стоял), но
   *  наложиться на поджатые видимые им нельзя — сервер отказал бы всей записи. */
  const sohranit = (novye: Vidzhet[]) => {
    const chistye = novye.filter((v) => pozvoleno(v.kind));
    let niz = vysota(chistye.map((v) => ({ i: v.id, x: v.x, y: v.y, w: v.w, h: v.h })));
    const hvost = skrytye
      .filter((v) => pozvoleno(v.kind))
      .map((v) => {
        const mesto = { ...v, x: 0, y: niz };
        niz += v.h;
        return mesto;
      });
    const vsyo = [...chistye, ...hvost].map(({ kind, x, y, w, h, params }) => ({ kind, x, y, w, h, params }));
    setRaskladka({ ...raskladka, layout: { version: 2, widgets: vsyo } });
    // Последним словом остаётся последняя ПРАВКА, а не последний ответ. Два
    // крестика подряд шлют два PUT; приди ответ первого позже — он вернул бы на
    // экран убранный виджет, которого на сервере уже нет, и убирать его
    // пришлось бы второй раз.
    const moya = (pravok.current += 1);
    api
      .put<Raskladka>("/dashboard/layout", { widgets: vsyo })
      .then((otvet) => {
        if (moya === pravok.current) setRaskladka(otvet);
      })
      .catch((beda) => {
        toastError(beda);
        api
          .get<Raskladka>("/dashboard/layout")
          .then((otvet) => {
            if (moya === pravok.current) setRaskladka(otvet);
          })
          .catch(() => undefined);
      });
  };

  const ubrat = (id: string) => sohranit(razlozhennye.filter((v) => v.id !== id));

  const dobavit = (kind: string, key_id?: number) => {
    const opis = kinds[kind];
    const mesta = razlozhennye.map((v) => ({ i: v.id, x: v.x, y: v.y, w: v.w, h: v.h }));
    const { x, y } = svobodnoeMesto(mesta);
    const v: Vidzhet = {
      id: metka({ kind, params: key_id ? { key_id } : {} }),
      kind,
      x,
      y,
      w: opis.w,
      h: opis.h,
      params: key_id ? { key_id } : {},
    };
    setDobavlenie(false);
    sohranit([...razlozhennye, v]);
  };

  const sbrosit = () => {
    api
      .del("/dashboard/layout")
      .then(() => {
        setRaskladka({ ...raskladka, layout: null });
        toast(t("dashLayoutReset"));
      })
      .catch(toastError);
  };

  /** Взять блок рукой: за шапку — двигать, за угол — менять размер.
   *
   *  Указатель захватывается элементом (`setPointerCapture`), иначе быстрый
   *  жест «уронил» бы блок на первой же кнопке под курсором. Пока рука держит,
   *  раскладка живёт в черновике: писать её на сервер на каждый пиксель —
   *  тот же цикл отрисовки, который однажды дал 240 запросов в час.
   */
  const vzyat = (v: Vidzhet, rezhim: "move" | "size") => (sobytie: React.PointerEvent) => {
    if (!shirokiy || sobytie.button !== 0) return;
    sobytie.preventDefault();
    const nachalo = { x: sobytie.clientX, y: sobytie.clientY };
    const bylo = { x: v.x, y: v.y, w: v.w, h: v.h };
    const shirinaPolotna = polotno.current?.clientWidth ?? 1;
    const kolonka = (shirinaPolotna - ZAZOR * (kolonok - 1)) / kolonok;
    const opis = kinds[v.kind];
    // Захват указателя — удобство, а не условие: без него быстрый жест
    // «роняет» блок на первой кнопке под курсором, но с отказом захвата
    // (мышь уже отпущена, указателя такого нет) тащить всё равно можно.
    try {
      (sobytie.currentTarget as HTMLElement).setPointerCapture(sobytie.pointerId);
    } catch {
      /* указателя уже нет — тянем по событиям окна */
    }
    setDerzhim(v.id);

    let itog: Vidzhet[] = razlozhennye;
    const dvizhenie = (e: PointerEvent) => {
      const dx = Math.round((e.clientX - nachalo.x) / (kolonka + ZAZOR));
      const dy = Math.round((e.clientY - nachalo.y) / (SHAG + ZAZOR));
      const svoyo = vpredelah(
        rezhim === "move"
          ? { i: v.id, x: bylo.x + dx, y: bylo.y + dy, w: bylo.w, h: bylo.h }
          : { i: v.id, x: bylo.x, y: bylo.y, w: bylo.w + dx, h: bylo.h + dy },
        { kolonok, strok: raskladka.grid.rows, minW: opis.min_w, minH: opis.min_h },
      );
      const mesta = razlozhennye.map((x) => ({ i: x.id, x: x.x, y: x.y, w: x.w, h: x.h }));
      itog = obtech(mesta, svoyo).map((m) => {
        const staryy = razlozhennye.find((x) => x.id === m.i)!;
        return { ...staryy, x: m.x, y: m.y, w: m.w, h: m.h };
      });
      setChernovik(itog);
    };
    const otpustili = () => {
      window.removeEventListener("pointermove", dvizhenie);
      window.removeEventListener("pointerup", otpustili);
      window.removeEventListener("pointercancel", otpustili);
      setDerzhim(null);
      setChernovik(null);
      const izmenilos = itog.some((n) => {
        const s = razlozhennye.find((x) => x.id === n.id);
        return !s || s.x !== n.x || s.y !== n.y || s.w !== n.w || s.h !== n.h;
      });
      if (izmenilos) sohranit(itog);
    };
    window.addEventListener("pointermove", dvizhenie);
    window.addEventListener("pointerup", otpustili);
    window.addEventListener("pointercancel", otpustili);
  };

  // Что можно добавить: виды «по одному», которых ещё нет, и ключи сайта,
  // на которые виджета ещё нет. Без единого живого ключа — ни одного пункта
  // про ключ: виджет без ключа заводить нельзя (владелец, 06.09.2026).
  //
  // **Список берётся из реестра сервера, а не из порядка умолчания.** Пока он
  // строился по `PORYADOK_UMOLCHANIYA`, новый виджет было НЕЧЕМ добавить: в
  // реестре он есть, подпись есть, рисуется — а в окне «Добавить блок» его
  // нет. Так пропал «Отчёт продаж» (найдено владельцем на боевом 07.09.2026).
  // Дописывать его в порядок умолчания было бы хуже: тот задаёт сводку по
  // умолчанию ВСЕМ, а виджет ставят себе те, кому он нужен.
  const est = new Set(polnyy.map((v) => v.id));
  const poryadok = (kind: string) => {
    const mesto = PORYADOK_UMOLCHANIYA.indexOf(kind);
    return mesto < 0 ? PORYADOK_UMOLCHANIYA.length : mesto;
  };
  const kandidaty = Object.keys(kinds)
    .filter((kind) => kinds[kind].odin && pozvoleno(kind) && !est.has(kind))
    .sort((a, b) => poryadok(a) - poryadok(b) || a.localeCompare(b));
  const klyuchiBezVidzheta = (klyuchi.items ?? []).filter((k) => k.state === "active" && !est.has(`api_key:${k.id}`));

  /** Тело блока. Карточку, шапку и ручки рисует рама — здесь только то, ради
   *  чего блок стоит на сводке. */
  const soderzhimoe = (v: Vidzhet) => {
    switch (v.kind) {
      case "money_in_work":
        return (
          <>
            <div className="vd-chislo vd-dengi">{sum(data.money_in_work)}</div>
            <div className="vd-pod">{t("dealsOpenNow", { n: openDeals })}</div>
          </>
        );
      case "money_received":
        // Считаем по кассе (решение «банк один»): заказ, оплаченный мимо
        // заявки, виден только так. «Закрыто за месяц» рядом — другой вопрос.
        return (
          <>
            <div className="vd-chislo vd-dengi">{sum(data.money_received_this_month)}</div>
            <div className="vd-pod">{t("moneyReceivedHint")}</div>
          </>
        );
      case "money_won":
        return (
          <>
            <div className="vd-chislo vd-dengi">{sum(data.money_won_this_month)}</div>
            <div className="vd-pod">
              {data.money_basis === "cash"
                ? t("moneyWonValueHint", { n: data.won_count_this_month })
                : t("dealsWonThisMonth", { n: data.won_count_this_month })}
            </div>
          </>
        );
      case "money_due":
        // К получению: цена открытых заявок минус предоплата — долг, о котором стоит напоминать.
        return (
          <>
            <div className="vd-chislo vd-dengi">{sum(data.money_due)}</div>
            <div className="vd-pod">{t("dashMoneyDueHint")}</div>
          </>
        );
      case "avg_check":
        // Без единой сделки с ценой средний чек — прочерк, а не ноль: ноль прочитают как «работаем даром».
        return (
          <>
            <div className="vd-chislo vd-dengi">{data.avg_check === null ? "—" : sum(data.avg_check)}</div>
            <div className="vd-pod">{data.avg_check === null ? t("avgCheckNone") : t("avgCheckHint")}</div>
          </>
        );
      case "clients":
        return (
          <>
            <div className="vd-chislo">{data.clients_total}</div>
            <div className="vd-pod">
              {t("addedThisMonth", { n: data.clients_this_month })} · {t("dashClientsWeek", { n: data.clients_this_week })}
              {data.clients_without_deals > 0 && ` · ${t("dashClientsNoDeals", { n: data.clients_without_deals })}`}
            </div>
          </>
        );
      case "calls":
        return (
          <>
            <div className="vd-chislo">{data.calls_24h.vsego}</div>
            <div className="vd-pod" style={data.calls_24h.propushcheno > 0 ? { color: "var(--warning)" } : undefined}>
              {t("dashCallsMissed", { n: data.calls_24h.propushcheno })}
            </div>
          </>
        );
      case "lost_share":
        return (
          <>
            <div className="vd-chislo">
              {dolyaOtkazov === null ? "—" : `${dolyaOtkazov.toFixed(1).replace(".", locale === "ru" ? "," : ".")} %`}
            </div>
            <div className="vd-pod">
              {dolyaOtkazov === null ? t("dashLostShareNone") : t("dashLostShareHint", { n: zakryto })}
            </div>
          </>
        );
      case "funnel":
        // Воронка целиком, включая пустые этапы: «в согласовании ноль» — тоже
        // ответ, и провал в середине видно только когда пустой этап нарисован.
        return data.deals_by_stage.every((s: any) => s.count === 0) ? (
          <EmptyState
            icon="deals"
            title={t("dashNoDeals")}
            action={<Link to="/deals" className="btn btn-secondary btn-sm">{t("dashOpenDeals")}</Link>}
          />
        ) : (
          <div className="funnel">
            {data.deals_by_stage.map((stage: any) => (
              <Link
                to={`/deals?stage=${encodeURIComponent(stage.key)}`}
                key={stage.key}
                className={"funnel-step kind-" + stage.kind}
              >
                <span className="funnel-count">{stage.count}</span>
                <span className="funnel-name">{nazvanieEtapa(t, stage.name)}</span>
                {/* Сумма этапа — с правом на суммы; ноль не пишем: «на ноль» читается как беда. */}
                {typeof stage.amount === "number" && stage.amount > 0 && (
                  <span className="funnel-sum">{sum(stage.amount)}</span>
                )}
                <span className="funnel-bar" style={{ width: `${Math.round((stage.count / maxStage) * 100)}%` }} />
              </Link>
            ))}
          </div>
        );
      case "my_tasks":
        // Задачи того, кто смотрит: сводка отвечает на «с чего начать», а не
        // «что вообще есть в фирме». Просроченные — красным счётчиком рядом.
        return data.my_tasks.length === 0 ? (
          <EmptyState
            icon="clock"
            title={overdue > 0 ? t("dashOverdue", { n: overdue }) : t("myTasksNone")}
            action={<Link to="/tasks" className="btn btn-secondary btn-sm">{t("dashNewTask")}</Link>}
          />
        ) : (
          <div className="dash-tasks">
            {/* Просроченные — красным над списком: они и есть ответ на «с чего
                начать», и в общем счётчике они бы растворились. */}
            {overdue > 0 && (
              <div className="vd-trevoga">
                <Chip variant="danger">{t("dashOverdue", { n: overdue })}</Chip>
              </div>
            )}
            {data.my_tasks.map((task: any) => {
              const at = parseDate(task.due_at);
              const late = at && at.getTime() < Date.now();
              return (
                <Link to={task.deal_id ? `/deals/${task.deal_id}` : "/tasks"} key={task.id} className="dash-task">
                  <Icon name="clock" size={13} className={late ? "task-late" : undefined} />
                  <span style={{ flex: 1, minWidth: 0 }}>{task.title}</span>
                  <span className={"dash-task-due" + (late ? " task-late" : "")}>{formatDateTime(task.due_at, locale)}</span>
                </Link>
              );
            })}
          </div>
        );
      case "orders_week":
        // Заказы и возвраты за неделю плюс свежие заказы: у магазина это и
        // есть «как идут дела», и без них сводка отвечала только за заявки.
        return (
          <>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
              <Chip variant="success">{t("dashShipped", { n: data.orders_week.shipped_count })}</Chip>
              <Chip variant={data.orders_week.returns_count > 0 ? "warning" : undefined}>
                {t("dashReturns", { n: data.orders_week.returns_count })}
              </Chip>
              {data.orders_week.refund_amount !== null && data.orders_week.returns_count > 0 && (
                <Chip>{t("dashRefunded", { sum: sum(data.orders_week.refund_amount) })}</Chip>
              )}
              {/* Просроченные — красным и ссылкой на отбор: их разбирают первыми. */}
              {data.orders_week.overdue_count > 0 && (
                <Link to="/orders?overdue=1" style={{ textDecoration: "none" }}>
                  <Chip variant="danger">{t("dashOrdersOverdue", { n: data.orders_week.overdue_count })}</Chip>
                </Link>
              )}
            </div>
            {data.recent_orders.length === 0 ? (
              <div className="field-desc" style={{ marginTop: 0 }}>{t("dashNoOrders")}</div>
            ) : (
              <div className="dash-tasks">
                {data.recent_orders.map((order: any) => (
                  <Link to={`/orders/${order.id}`} key={order.id} className="dash-task">
                    <span style={{ fontFamily: "ui-monospace, monospace", color: "var(--faint)", fontSize: 12 }}>{order.number}</span>
                    <span className="truncate" style={{ flex: 1, minWidth: 0 }}>
                      {order.client_name ?? t("noClient")}
                    </span>
                    {order.total !== null && (
                      <span style={{ color: "var(--muted)", fontSize: 12.5, fontVariantNumeric: "tabular-nums" }}>{sum(order.total)}</span>
                    )}
                    <Chip variant={statusVariant(order.status)}>{orderStatusLabel(t, order.status, order.kind)}</Chip>
                  </Link>
                ))}
              </div>
            )}
          </>
        );
      case "low_stock":
        // Что пора закупать: закончилось или не выше порога. Список короткий
        // нарочно — за полным идут в склад по ссылке «ещё N».
        return data.low_stock.length === 0 ? (
          <div className="field-desc" style={{ marginTop: 0 }}>{t("dashStockOk")}</div>
        ) : (
          <>
            <div className="dash-tasks">
              {data.low_stock.map((item: any) => (
                <Link to={`/warehouse/${item.id}`} key={item.id} className="dash-task">
                  <Icon name="warehouse" size={13} className={item.out ? "task-late" : undefined} />
                  <span className="truncate" style={{ flex: 1, minWidth: 0 }}>{item.name}</span>
                  <Chip variant={item.out ? "danger" : "warning"}>
                    {item.out ? t("outOfStock") : t("lowStock")} · {formatQuantity(item.stock_milli)}
                  </Chip>
                </Link>
              ))}
            </div>
            {data.low_stock_total > data.low_stock.length && (
              <div className="field-desc">{t("dashStockMore", { n: data.low_stock_total - data.low_stock.length })}</div>
            )}
          </>
        );
      case "showcase_views":
        // Витрины ниже денег: это метрика портфолио, а не бизнеса. Два числа
        // рядом — сколько раз открывали и сколько людей открывало; подпись у
        // второго объясняет разницу.
        return (
          <div className="vitriny">
            <div className="vitriny-chisla">
              <div>
                <div className="vd-podpis">{t("showcaseViews")}</div>
                <div className="vd-chislo">{data.views_7d}</div>
                <div className="vd-pod">
                  {t("last7days")}
                  {growth !== null && (
                    <>
                      {" · "}
                      <span style={{ color: growth >= 0 ? "var(--success)" : "var(--warning)" }}>
                        {growth >= 0 ? "+" : ""}
                        {growth}%
                      </span>{" "}
                      {t("vsPrevWeek")}
                    </>
                  )}
                </div>
              </div>
              <div>
                <div className="vd-podpis">{t("uniqueViewersTitle")}</div>
                <div className="vd-chislo">{data.unique_viewers_7d ?? 0}</div>
                <div className="vd-pod">{t("uniqueViewersHint")}</div>
              </div>
              <div>
                <div className="vd-podpis">{t("metricBoards")}</div>
                <div className="vd-chislo">{data.boards_published}</div>
                <div className="vd-pod">
                  {t("ofTotal", { n: data.boards_total })}
                  {data.boards_total - data.boards_published > 0 &&
                    ` · ${data.boards_total - data.boards_published} ${t("drafts")}`}
                </div>
              </div>
            </div>
            <div className="bars">
              {data.views_by_day.map((d: any, i: number) => (
                <div className="bar-col" key={d.date}>
                  <div
                    className={"bar" + (d.count === maxDay && d.count > 0 ? " top" : "")}
                    style={{ height: Math.max(4, Math.round((d.count / maxDay) * 52)) }}
                    title={`${d.count}`}
                  />
                  <span className="bar-label">{dayLabels[i]}</span>
                </div>
              ))}
            </div>
          </div>
        );
      // Отчёт продаж двумя видами. Свои данные берёт сам: считает он окно в
      // год, и класть его в общий ответ сводки значило бы платить за него у
      // всех, включая тех, у кого виджета нет.
      case "sales_grid":
        return <OtchyotProdazh vid="setka" />;
      case "sales_matrix":
        return <OtchyotProdazh vid="matritsa" />;
      case "storage":
        return storage ? <StorageCard storage={storage} onPurged={() => void refreshStorage()} /> : null;
      case "money_flow":
        return <PotokDeneg />;
      case "client_sources":
        return <IstochnikiKlientov />;
      case "recent_boards":
        return data.recent_boards.length === 0 ? (
          <EmptyState icon="boards" title={t("noBoardsYet")} action={<NewBoardButton />} />
        ) : (
          <div className={"board-grid " + (v.w >= 9 ? "board-grid-4" : "board-grid-2")}>
            {data.recent_boards.map((board: any) => (
              <BoardCard key={board.id} board={board} compact />
            ))}
          </div>
        );
      case "recent_clients":
        // Строк столько, сколько влезает в высоту блока: панель, шапка таблицы
        // и подвал съедают примерно четыре строки сетки, дальше по одной.
        return <TablitsaKlientov strok={Math.max(3, v.h - 5)} />;
      case "api_key":
        return (
          <VidzhetKlyucha
            keyId={v.params.key_id ?? 0}
            klyuch={(klyuchi.items ?? []).find((k) => k.id === v.params.key_id)}
            spisokEdet={klyuchi.items === null && klyuchi.failure === null}
            spisokUpal={klyuchi.failure}
            povtorSpiska={klyuchi.reload}
          />
        );
      default:
        return null;
    }
  };

  /** Где блок лежит на полотне. Колонка считается процентами, зазор — в
   *  пикселях: сетка тянется за окном, а промежуток между блоками нет. */
  const mesto = (v: Vidzhet): React.CSSProperties | undefined => {
    if (!shirokiy) return undefined;
    const dolya = `(100% - ${ZAZOR * (kolonok - 1)}px) / ${kolonok}`;
    return {
      position: "absolute",
      left: `calc(${dolya} * ${v.x} + ${v.x * ZAZOR}px)`,
      width: `calc(${dolya} * ${v.w} + ${(v.w - 1) * ZAZOR}px)`,
      top: v.y * (SHAG + ZAZOR),
      height: v.h * SHAG + (v.h - 1) * ZAZOR,
    };
  };

  return (
    <div className="page page-summary">
      <div className="page-head" style={{ marginBottom: 18 }}>
        <div>
          <h1 className="page-title">
            {greeting}, {user?.name}
          </h1>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          {/* Отметка свежести. Обещать «в реальном времени» и молчать о том,
              когда числа взяты, значит обещать больше, чем есть: обновление
              идёт раз в две минуты, и человек вправе это видеть. */}
          {obnovleno && (
            <span className="dash-freshness">
              {t("updatedAt", {
                time: obnovleno.toLocaleTimeString(locale === "ru" ? "ru-RU" : "en-US", {
                  hour: "2-digit",
                  minute: "2-digit",
                }),
              })}
            </span>
          )}
          {nastroyka ? (
            <>
              <button type="button" className="btn btn-secondary" onClick={() => setDobavlenie(true)}>
                <Icon name="plus" />
                {t("dashAddWidget")}
              </button>
              <button type="button" className="btn btn-secondary" onClick={sbrosit} disabled={!raskladka.layout}>
                <Icon name="refresh" />
                {t("dashResetLayout")}
              </button>
              <button type="button" className="btn btn-primary" onClick={() => setNastroyka(false)}>
                <Icon name="check" />
                {t("dashDone")}
              </button>
            </>
          ) : (
            <>
              <Link to="/clients?new=1" className="btn btn-secondary">
                <Icon name="userPlus" />
                {t("newClient")}
              </Link>
              {boardsOn && <NewBoardButton />}
              <button type="button" className="btn btn-secondary" onClick={() => setNastroyka(true)} aria-label={t("dashCustomize")} title={t("dashCustomize")}>
                <Icon name="settings" />
              </button>
            </>
          )}
        </div>
      </div>

      {nastroyka && <div className="field-desc" style={{ marginTop: 0, marginBottom: 12 }}>{t("dashCustomizeHint")}</div>}

      <div
        className={"svodka" + (shirokiy ? " svodka-setka" : "") + (derzhim ? " svodka-derzhim" : "")}
        ref={polotno}
        style={shirokiy ? { height: strok * SHAG + Math.max(0, strok - 1) * ZAZOR } : undefined}
      >
        {razlozhennye.map((v) => {
          const svoya = SVOYA_KARTA.has(v.kind);
          const hod = HODY[v.kind];
          return (
            <div
              key={v.id}
              className={"vd" + (derzhim === v.id ? " vd-v-ruke" : "") + (svoya ? " vd-svoya" : "")}
              style={mesto(v)}
            >
              <div className="vd-karta">
                {!svoya && (
                  <div className="vd-shapka" onPointerDown={vzyat(v, "move")}>
                    {ZNACHKI[v.kind] && <Icon name={ZNACHKI[v.kind]} size={15} className="vd-znachok" />}
                    <span className="truncate vd-imya">{zagolovok(v)}</span>
                    {hod && (
                      <Link to={hod} className="vd-hod" onPointerDown={(e) => e.stopPropagation()}>
                        {t("viewAll")}
                      </Link>
                    )}
                    <span className="vd-ruchka" aria-hidden="true">
                      <Icon name="grip" size={13} />
                    </span>
                    {nastroyka && (
                      <button
                        type="button"
                        className="btn-icon vd-ubrat"
                        aria-label={t("dashRemoveWidget")}
                        title={t("dashRemoveWidget")}
                        onPointerDown={(e) => e.stopPropagation()}
                        onClick={() => ubrat(v.id)}
                      >
                        <Icon name="x" size={13} />
                      </button>
                    )}
                  </div>
                )}
                <div className="vd-telo">{soderzhimoe(v)}</div>
                {svoya && (
                  <span className="vd-ruchka vd-ruchka-svoya" onPointerDown={vzyat(v, "move")} title={zagolovok(v)}>
                    <Icon name="grip" size={13} />
                  </span>
                )}
                {svoya && nastroyka && (
                  <button
                    type="button"
                    className="btn-icon vd-ubrat vd-ubrat-svoya"
                    aria-label={t("dashRemoveWidget")}
                    title={t("dashRemoveWidget")}
                    onClick={() => ubrat(v.id)}
                  >
                    <Icon name="x" size={13} />
                  </button>
                )}
                {shirokiy && (
                  <span
                    className="vd-tyanut"
                    role="button"
                    tabIndex={-1}
                    aria-label={t("dashWidgetResize")}
                    title={t("dashWidgetResize")}
                    onPointerDown={vzyat(v, "size")}
                  >
                    <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round">
                      <path d="M9 3 L3 9" />
                      <path d="M9 7 L7 9" />
                    </svg>
                  </span>
                )}
              </div>
            </div>
          );
        })}
        {razlozhennye.length === 0 && (
          <div className="card">
            <EmptyState
              icon="dashboard"
              title={t("dashEmpty")}
              action={
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => { setNastroyka(true); setDobavlenie(true); }}>
                  {t("dashAddWidget")}
                </button>
              }
            />
          </div>
        )}
      </div>

      {dobavlenie && (
        <Modal title={t("dashAddWidget")} onClose={() => setDobavlenie(false)}>
          {klyuchi.failure !== null && <LoadFailed error={klyuchi.failure} onRetry={klyuchi.reload} />}
          {kandidaty.length === 0 && klyuchiBezVidzheta.length === 0 ? (
            <div className="field-desc" style={{ marginTop: 0 }}>{t("dashAllPlaced")}</div>
          ) : (
            <div className="preset-row">
              {kandidaty.map((kind) => (
                <button key={kind} type="button" className="preset-card" onClick={() => dobavit(kind)}>
                  <span className="preset-name">{t(kind === "money_won" && data.money_basis === "cash" ? "moneyWonValue" : ZAGOLOVKI[kind])}</span>
                </button>
              ))}
              {klyuchiBezVidzheta.map((k) => (
                <button key={`k${k.id}`} type="button" className="preset-card" onClick={() => dobavit("api_key", k.id)}>
                  <span className="preset-name">{t("dashApiKey")} {k.name}</span>
                  <span className="preset-hint">{t("dashApiKeyHint")}</span>
                </button>
              ))}
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
