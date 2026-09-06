import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { BoardCard } from "../components/BoardCard";
import { Icon } from "../components/Icon";
import { NewBoardButton } from "../components/NewBoardButton";
import { StorageCard } from "../components/StorageCard";
import { VidzhetKlyucha, type KlyuchSayta } from "../components/VidzhetKlyucha";
import { Avatar, Chip, EmptyState, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { orderStatusLabel, statusVariant } from "../lib/documents";
import { nazvanieEtapa } from "../lib/etapy";
import { useLive, useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { formatDateTime, formatMoney, formatQuantity, initials, parseDate, relativeDay } from "../lib/format";
import type { TranslationKey } from "../lib/i18n";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";

/** Через сколько сводка перечитывается сама, пока вкладка на переднем плане.
 *
 * **Это запасной путь, когда живые обновления выключены** (`docs/ustroystvo/12-zhivye-obnovleniya.md`
 * §11): при включённых сводку перечитывает намёк живого слоя, а таймер молчит.
 * Записано здесь: механизмов обновления два, и второй обязан знать о первом.
 *
 * Две минуты, а не десять секунд, и не «по каждому изменению». **Замерено:
 * сводка стоит 23 запроса к базе** против четырёх у списка бумаг — один заход
 * собирает деньги, воронку целиком, задачи, клиентов и просмотры за неделю. Она
 * нарочно не стоит под потолком запросов (`tests/test_speed.py`, комментарий у
 * таблицы): её цена растёт с данными, и абсолютный потолок был бы мигающим
 * сторожем. Значит частоту здесь выбирают руками и с запасом: минута дала бы
 * 1380 запросов в час с одной вкладки.
 *
 * Столько же, сколько у проверки свободного места (`lib/app.tsx`), и это не
 * совпадение: обе — фоновые перезапросы, и разные интервалы у них означали бы
 * два числа, которые кто-то однажды начнёт сближать.
 */
const SVODKA_POLL_MS = 120_000;

type Shirina = 1 | 2 | 4;

/** Виджет раскладки: вид, ширина в колонках сетки из четырёх, параметры (ключ сайта). */
interface Vidzhet {
  id: string;
  kind: string;
  w: Shirina;
  params: { key_id?: number };
}

/** Реестр с сервера (`vidzhety_service.REESTR`): ширины, блок и право. Экран
 *  свою копию не держит — две карты разошлись бы молча. */
interface OpisVidzheta {
  w: Shirina;
  shiriny: Shirina[];
  odin: boolean;
  module: string | null;
  perm: string | null;
}

interface Raskladka {
  layout: { version: number; widgets: { kind: string; w: Shirina; params: { key_id?: number } }[] } | null;
  kinds: Record<string, OpisVidzheta>;
}

/** Порядок умолчания — тот, в котором сводка стояла до виджетов: деньги, люди,
 *  воронка и задачи, заказы и склад, витрины, хранилище, доски и клиенты. */
const PORYADOK_UMOLCHANIYA = [
  "money_in_work", "money_received", "money_won", "money_due", "avg_check", "clients", "calls",
  "funnel", "my_tasks", "orders_week", "low_stock", "showcase_views", "storage", "recent_boards", "recent_clients",
];

const ZAGOLOVKI: Record<string, TranslationKey> = {
  money_in_work: "moneyInWork",
  money_received: "moneyReceivedThisMonth",
  money_won: "moneyWonThisMonth",
  money_due: "dashMoneyDue",
  avg_check: "avgCheck",
  clients: "metricClients",
  calls: "dashCallsToday",
  funnel: "funnel",
  my_tasks: "myTasksToday",
  orders_week: "dashOrdersWeek",
  low_stock: "dashStock",
  showcase_views: "showcaseViews",
  storage: "storage",
  recent_boards: "recentBoards",
  recent_clients: "recentClients",
  api_key: "dashApiKey",
};

function metka(v: { kind: string; params: { key_id?: number } }): string {
  return v.params.key_id ? `${v.kind}:${v.params.key_id}` : v.kind;
}

/** Сводка. Ширина своя (`page-svodka`, 1320px), а не списочная 1800px: на
 *  обычном мониторе плитки и ленты растягивались во всю ширину и читались
 *  хуже, чем две колонки (владелец, 06.09.2026). С того же дня сводка собрана
 *  из виджетов: блоки добавляются, убираются, перетягиваются и меняют ширину,
 *  раскладка хранится у сотрудника (`/dashboard/layout`); блок выключенного
 *  раздела или без права не появляется, даже если записан в раскладке. */
export function Dashboard() {
  const { user, t, locale, storage, modules, refreshStorage, toastError, toast } = useApp();
  const seesMoney = can(user, "deals.view_amounts");
  // Раздел «последние клиенты» — только тому, кому карточки открыты. Сервер
  // теперь отдаёт пустой список без права, и без этой проверки на экране
  // осталась бы надпись «клиентов пока нет» и ссылка в раздел, куда не пускают.
  const seesClients = can(user, "clients.view");
  const [data, setData] = useState<any>(null);
  const [raskladka, setRaskladka] = useState<Raskladka | null>(null);
  const [nastroyka, setNastroyka] = useState(false);
  const [dobavlenie, setDobavlenie] = useState(false);
  const [dragId, setDragId] = useState<string | null>(null);
  const [nadId, setNadId] = useState<string | null>(null);
  // Ключи сайта нужны виджету наблюдения и окну добавления; без права на
  // настройки справочник не спрашивается вовсе (`null`), и виджета ключа нет.
  const klyuchi = useReference<KlyuchSayta>(can(user, "settings.manage") ? "/settings/api-keys" : null);

  const { failure, fail, clear } = useFailure();

  const [obnovleno, setObnovleno] = useState<Date | null>(null);

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
  const zhivost = useLive();

  // Перезапрос по расписанию — ТОЛЬКО пока вкладка на переднем плане и
  // ТОЛЬКО пока живости нет: с открытым потоком таймер лишний (задача 8.7).
  //
  // Без этого десять забытых вкладок на фирму дают десять потоков перезапросов
  // самой дорогой ручки круглосуточно. Опыт в проекте уже есть: команда,
  // безобидная в руках человека, из цикла отрисовки дала 240 запросов в час и
  // уронила боевое обновление.
  //
  // Возвращение на вкладку перечитывает сразу, не дожидаясь двух минут: человек
  // вернулся именно затем, чтобы посмотреть, — и утренние числа под свежим
  // заголовком были бы ровно той бедой, ради которой это писалось.
  useEffect(() => {
    if (zhivost === "on") return;
    const vidno = () => document.visibilityState === "visible";
    const timer = window.setInterval(() => {
      if (vidno()) load(true);
    }, SVODKA_POLL_MS);
    const vernulis = () => {
      if (vidno()) load(true);
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", vernulis);
    };
  }, [load, zhivost]);

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
  const dayLabels = data.views_by_day.map((d: any) =>
    new Date(d.date + "T00:00:00").toLocaleDateString(locale === "ru" ? "ru-RU" : "en-US", { weekday: "short" }),
  );
  const sum = (value: number | null) => formatMoney(value, data.currency, locale);
  const ordersOn = data.orders_week !== null && data.orders_week !== undefined;
  const boardsOn = moduleOn(modules, "boards");
  const overdue = data.tasks_counters?.overdue ?? 0;
  const kinds = raskladka.kinds;

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
  const umolchanie: Vidzhet[] = PORYADOK_UMOLCHANIYA.filter(pozvoleno).map((kind) => ({
    id: kind,
    kind,
    w: kinds[kind].w,
    params: {},
  }));
  const polnyy: Vidzhet[] = raskladka.layout
    ? raskladka.layout.widgets.map((v) => ({ ...v, id: metka(v) }))
    : umolchanie;
  const vidimye = polnyy.filter((v) => pozvoleno(v.kind) && estChto(v));

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
   *  сохранить ничего, пока сосед не включит блок обратно. */
  const sohranit = (novyy: Vidzhet[]) => {
    const chistyy = novyy.filter((v) => pozvoleno(v.kind));
    setRaskladka({ ...raskladka, layout: { version: 1, widgets: chistyy.map(({ kind, w, params }) => ({ kind, w, params })) } });
    api
      .put<Raskladka>("/dashboard/layout", { widgets: chistyy.map(({ kind, w, params }) => ({ kind, w, params })) })
      .then(setRaskladka)
      .catch((beda) => {
        toastError(beda);
        api.get<Raskladka>("/dashboard/layout").then(setRaskladka).catch(() => undefined);
      });
  };
  const ubrat = (id: string) => sohranit(polnyy.filter((v) => v.id !== id));
  const sdvinut = (id: string, delta: number) => {
    const i = polnyy.findIndex((v) => v.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= polnyy.length) return;
    const novyy = [...polnyy];
    [novyy[i], novyy[j]] = [novyy[j], novyy[i]];
    sohranit(novyy);
  };
  const peremestit = (chto: string, kuda: string) => {
    const iz = polnyy.findIndex((v) => v.id === chto);
    const k = polnyy.findIndex((v) => v.id === kuda);
    if (iz < 0 || k < 0 || iz === k) return;
    const novyy = [...polnyy];
    const [v] = novyy.splice(iz, 1);
    novyy.splice(k, 0, v);
    sohranit(novyy);
  };
  const shirina = (id: string) => {
    sohranit(
      polnyy.map((v) => {
        if (v.id !== id) return v;
        const shiriny = kinds[v.kind].shiriny;
        return { ...v, w: shiriny[(shiriny.indexOf(v.w) + 1) % shiriny.length] };
      }),
    );
  };
  const dobavit = (kind: string, key_id?: number) => {
    const v: Vidzhet = { id: metka({ kind, params: key_id ? { key_id } : {} }), kind, w: kinds[kind].w, params: key_id ? { key_id } : {} };
    setDobavlenie(false);
    sohranit([...polnyy, v]);
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

  // Что можно добавить: виды «по одному», которых ещё нет, и ключи сайта,
  // на которые виджета ещё нет. Без единого живого ключа — ни одного пункта
  // про ключ: виджет без ключа заводить нельзя (владелец, 06.09.2026).
  const est = new Set(polnyy.map((v) => v.id));
  const kandidaty = PORYADOK_UMOLCHANIYA.filter((kind) => pozvoleno(kind) && !est.has(kind));
  const klyuchiBezVidzheta = (klyuchi.items ?? []).filter((k) => k.state === "active" && !est.has(`api_key:${k.id}`));

  const soderzhimoe = (v: Vidzhet) => {
    switch (v.kind) {
      case "money_in_work":
        return (
          <div className="card card-pad">
            <div className="metric-title" style={{ marginBottom: 14 }}>
              <Icon name="deals" size={14} />
              {t("moneyInWork")}
            </div>
            <div className="metric-value money-value">{sum(data.money_in_work)}</div>
            <div className="metric-sub">{t("dealsOpenNow", { n: openDeals })}</div>
          </div>
        );
      case "money_received":
        // Считаем по кассе (решение «банк один»): заказ, оплаченный мимо
        // заявки, виден только так. «Закрыто за месяц» рядом — другой вопрос.
        return (
          <div className="card card-pad">
            <div className="metric-title" style={{ marginBottom: 14 }}>
              <Icon name="receipt" size={14} />
              {t("moneyReceivedThisMonth")}
            </div>
            <div className="metric-value money-value">{sum(data.money_received_this_month)}</div>
            <div className="metric-sub">{t("moneyReceivedHint")}</div>
          </div>
        );
      case "money_won":
        return (
          <div className="card card-pad">
            <div className="metric-title" style={{ marginBottom: 14 }}>
              <Icon name="analytics" size={14} />
              {data.money_basis === "cash" ? t("moneyWonValue") : t("moneyWonThisMonth")}
            </div>
            <div className="metric-value money-value">{sum(data.money_won_this_month)}</div>
            <div className="metric-sub">
              {data.money_basis === "cash"
                ? t("moneyWonValueHint", { n: data.won_count_this_month })
                : t("dealsWonThisMonth", { n: data.won_count_this_month })}
            </div>
          </div>
        );
      case "money_due":
        // К получению: цена открытых заявок минус предоплата — долг, о котором стоит напоминать.
        return (
          <div className="card card-pad">
            <div className="metric-title" style={{ marginBottom: 14 }}>
              <Icon name="clock" size={14} />
              {t("dashMoneyDue")}
            </div>
            <div className="metric-value money-value">{sum(data.money_due)}</div>
            <div className="metric-sub">{t("dashMoneyDueHint")}</div>
          </div>
        );
      case "avg_check":
        // Без единой сделки с ценой средний чек — прочерк, а не ноль: ноль прочитают как «работаем даром».
        return (
          <div className="card card-pad">
            <div className="metric-title" style={{ marginBottom: 14 }}>
              <Icon name="star" size={14} />
              {t("avgCheck")}
            </div>
            <div className="metric-value money-value">{data.avg_check === null ? "—" : sum(data.avg_check)}</div>
            <div className="metric-sub">{data.avg_check === null ? t("avgCheckNone") : t("avgCheckHint")}</div>
          </div>
        );
      case "clients":
        return (
          <div className="card card-pad">
            <div className="metric-title" style={{ marginBottom: 14 }}>
              <Icon name="clients" size={14} />
              {t("metricClients")}
            </div>
            <div className="metric-value">{data.clients_total}</div>
            <div className="metric-sub">
              {t("addedThisMonth", { n: data.clients_this_month })} · {t("dashClientsWeek", { n: data.clients_this_week })}
              {data.clients_without_deals > 0 && ` · ${t("dashClientsNoDeals", { n: data.clients_without_deals })}`}
            </div>
          </div>
        );
      case "calls":
        return (
          <div className="card card-pad">
            <div className="metric-title" style={{ marginBottom: 14 }}>
              <Icon name="call" size={14} />
              {t("dashCallsToday")}
            </div>
            <div className="metric-value">{data.calls_24h.vsego}</div>
            <div className="metric-sub" style={data.calls_24h.propushcheno > 0 ? { color: "var(--warning)" } : undefined}>
              {t("dashCallsMissed", { n: data.calls_24h.propushcheno })}
            </div>
          </div>
        );
      case "funnel":
        // Воронка целиком, включая пустые этапы: «в согласовании ноль» — тоже
        // ответ, и провал в середине видно только когда пустой этап нарисован.
        return (
          <div className="card card-pad">
            <div className="section-head" style={{ marginBottom: 14 }}>
              <div className="metric-title">{t("funnel")}</div>
              <Link to="/deals" className="section-link">
                {t("viewAll")}
              </Link>
            </div>
            {data.deals_by_stage.every((s: any) => s.count === 0) ? (
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
            )}
          </div>
        );
      case "my_tasks":
        // Задачи того, кто смотрит: сводка отвечает на «с чего начать», а не
        // «что вообще есть в фирме». Просроченные — красным счётчиком рядом.
        return (
          <div className="card card-pad">
            <div className="section-head" style={{ marginBottom: 14 }}>
              <div className="metric-title">
                {t("myTasksToday")}
                {overdue > 0 && <Chip variant="danger">{t("dashOverdue", { n: overdue })}</Chip>}
              </div>
              <Link to="/tasks" className="section-link">
                {t("viewAll")}
              </Link>
            </div>
            {data.my_tasks.length === 0 ? (
              <EmptyState
                icon="clock"
                title={t("myTasksNone")}
                action={<Link to="/tasks" className="btn btn-secondary btn-sm">{t("dashNewTask")}</Link>}
              />
            ) : (
              <div className="dash-tasks">
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
            )}
          </div>
        );
      case "orders_week":
        // Заказы и возвраты за неделю плюс свежие заказы: у магазина это и
        // есть «как идут дела», и без них сводка отвечала только за заявки.
        return (
          <div className="card card-pad">
            <div className="section-head" style={{ marginBottom: 12 }}>
              <div className="metric-title">{t("dashOrdersWeek")}</div>
              <Link to="/orders" className="section-link">
                {t("viewAll")}
              </Link>
            </div>
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
          </div>
        );
      case "low_stock":
        // Что пора закупать: закончилось или не выше порога. Список короткий
        // нарочно — за полным идут в склад по ссылке «ещё N».
        return (
          <div className="card card-pad">
            <div className="section-head" style={{ marginBottom: 12 }}>
              <div className="metric-title">{t("dashStock")}</div>
              <Link to="/warehouse?low=1" className="section-link">
                {t("viewAll")}
              </Link>
            </div>
            {data.low_stock.length === 0 ? (
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
            )}
          </div>
        );
      case "showcase_views":
        // Витрины ниже денег: это метрика портфолио, а не бизнеса. Два числа
        // рядом — сколько раз открывали и сколько людей открывало; подпись у
        // второго объясняет разницу.
        return (
          <div className="card" style={{ padding: "18px 20px", display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 28, flexWrap: "wrap" }}>
            <div style={{ display: "flex", gap: 40, flexWrap: "wrap" }}>
              <div>
                <div className="metric-title" style={{ marginBottom: 14 }}>
                  <Icon name="eye" size={14} />
                  {t("showcaseViews")}
                </div>
                <div className="metric-value">{data.views_7d}</div>
                <div className="metric-sub">
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
                <div className="metric-title" style={{ marginBottom: 14 }}>
                  <Icon name="user" size={14} />
                  {t("uniqueViewersTitle")}
                </div>
                <div className="metric-value">{data.unique_viewers_7d ?? 0}</div>
                <div className="metric-sub">{t("uniqueViewersHint")}</div>
              </div>
              <div>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 14 }}>
                  <div className="metric-title"><Icon name="boards" size={14} />{t("metricBoards")}</div>
                  {data.boards_total - data.boards_published > 0 && (
                    <Chip>
                      <span className="dot" />
                      {data.boards_total - data.boards_published} {t("drafts")}
                    </Chip>
                  )}
                </div>
                <div className="metric-value">{data.boards_published}</div>
                <div className="metric-sub">{t("ofTotal", { n: data.boards_total })}</div>
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
      case "storage":
        return storage ? <StorageCard storage={storage} onPurged={() => void refreshStorage()} /> : null;
      case "recent_boards":
        return (
          <div>
            <div className="section-head">
              <h2 className="section-title">{t("recentBoards")}</h2>
              <Link to="/boards" className="section-link">
                {t("viewAll")}
              </Link>
            </div>
            {data.recent_boards.length === 0 ? (
              <div className="card">
                <EmptyState icon="boards" title={t("noBoardsYet")} action={<NewBoardButton />} />
              </div>
            ) : (
              <div className={"board-grid " + (v.w === 4 ? "board-grid-4" : "board-grid-2")}>
                {data.recent_boards.map((board: any) => (
                  <BoardCard key={board.id} board={board} compact />
                ))}
              </div>
            )}
          </div>
        );
      case "recent_clients":
        return (
          <div>
            <div className="section-head">
              <h2 className="section-title">{t("recentClients")}</h2>
              <Link to="/clients" className="section-link">
                {t("allClients")}
              </Link>
            </div>
            <div className="list-card">
              {data.recent_clients.length === 0 && (
                <EmptyState
                  icon="clients"
                  title={t("noClientsYet")}
                  action={<Link to="/clients?new=1" className="btn btn-secondary btn-sm">{t("newClient")}</Link>}
                />
              )}
              {data.recent_clients.map((client: any) => (
                <Link to={`/clients/${client.id}`} key={client.id} className="list-row hoverable">
                  <Avatar text={initials(client.name)} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="truncate" style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{client.name}</div>
                    <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>{client.company}</div>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    {client.tags.slice(0, 2).map((tag: string) => (
                      <Chip key={tag}>{tag}</Chip>
                    ))}
                  </div>
                  <div style={{ color: "var(--faint)", fontSize: 12, width: 110, textAlign: "right", flexShrink: 0 }}>
                    {relativeDay(client.updated_at, locale)}
                  </div>
                </Link>
              ))}
            </div>
          </div>
        );
      case "api_key":
        return (
          <VidzhetKlyucha
            keyId={v.params.key_id ?? 0}
            klyuch={(klyuchi.items ?? []).find((k) => k.id === v.params.key_id)}
          />
        );
      default:
        return null;
    }
  };

  return (
    <div className="page page-svodka">
      <div className="page-head" style={{ marginBottom: 22 }}>
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
            <span className="dash-svezhest">
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

      <div className="svodka-setka">
        {vidimye.map((v) => (
          <div
            key={v.id}
            className={
              `vidzhet vidzhet-w${v.w}` +
              (nastroyka ? " vidzhet-nastroyka" : "") +
              (dragId === v.id ? " vidzhet-taskaem" : "") +
              (nadId === v.id && dragId !== v.id ? " vidzhet-tsel" : "")
            }
            draggable={nastroyka}
            onDragStart={() => setDragId(v.id)}
            onDragEnd={() => {
              setDragId(null);
              setNadId(null);
            }}
            onDragOver={(e) => {
              if (!nastroyka || !dragId) return;
              e.preventDefault();
              if (nadId !== v.id) setNadId(v.id);
            }}
            onDrop={(e) => {
              e.preventDefault();
              if (dragId && dragId !== v.id) peremestit(dragId, v.id);
              setDragId(null);
              setNadId(null);
            }}
          >
            {nastroyka && (
              <div className="vidzhet-shapka">
                <Icon name="grip" size={13} />
                <span className="vidzhet-imya truncate">{zagolovok(v)}</span>
                <span className="vidzhet-knopki">
                  <button type="button" className="btn-icon" aria-label={t("dashMoveLeft")} title={t("dashMoveLeft")} onClick={() => sdvinut(v.id, -1)}>
                    <Icon name="arrowLeft" size={13} />
                  </button>
                  <button type="button" className="btn-icon" aria-label={t("dashMoveRight")} title={t("dashMoveRight")} onClick={() => sdvinut(v.id, 1)}>
                    <Icon name="chevronRight" size={13} />
                  </button>
                  {kinds[v.kind].shiriny.length > 1 && (
                    <button
                      type="button"
                      className="btn-icon"
                      aria-label={t(v.w === 4 ? "dashWidgetNarrower" : "dashWidgetWider")}
                      title={t(v.w === 4 ? "dashWidgetNarrower" : "dashWidgetWider")}
                      onClick={() => shirina(v.id)}
                    >
                      <Icon name="chevronsUpDown" size={13} />
                    </button>
                  )}
                  <button type="button" className="btn-icon" aria-label={t("dashRemoveWidget")} title={t("dashRemoveWidget")} onClick={() => ubrat(v.id)}>
                    <Icon name="x" size={13} />
                  </button>
                </span>
              </div>
            )}
            {soderzhimoe(v)}
          </div>
        ))}
        {vidimye.length === 0 && (
          <div className="vidzhet vidzhet-w4">
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
