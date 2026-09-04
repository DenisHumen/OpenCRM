import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { BoardCard } from "../components/BoardCard";
import { Icon } from "../components/Icon";
import { NewBoardButton } from "../components/NewBoardButton";
import { StorageCard } from "../components/StorageCard";
import { Avatar, Chip, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLive, useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { formatDateTime, formatMoney, initials, parseDate, relativeDay } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";

/** Через сколько сводка перечитывается сама, пока вкладка на переднем плане.
 *
 * **Это запасной путь, когда живые обновления выключены** (`docs/12-realtime.md`
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

export function Dashboard() {
  const { user, t, locale, storage, modules, refreshStorage, toastError } = useApp();
  const seesMoney = can(user, "deals.view_amounts");
  // Раздел «последние клиенты» — только тому, кому карточки открыты. Сервер
  // теперь отдаёт пустой список без права, и без этой проверки на экране
  // осталась бы надпись «клиентов пока нет» и ссылка в раздел, куда не пускают.
  const seesClients = can(user, "clients.view");
  const navigate = useNavigate();
  const [data, setData] = useState<any>(null);

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

  // Сводка живая по намёкам: из тем, из которых она считается, тем же
  // обработчиком — значит и права те же. Перезапрос идёт после склейки, и
  // двадцать правок подряд дают одно чтение самой дорогой ручки.
  useLiveTopic(["deals", "clients", "tasks", "finance", "documents", "orders", "boards"], () => load(true));
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

  if (!data) return <ScreenLoading error={failure} onRetry={load} />;

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

  return (
    <div className="page page-wide">
      <div className="page-head" style={{ marginBottom: 26 }}>
        <div>
          <h1 className="page-title">
            {greeting}, {user?.name}
          </h1>
          {/* Отметка свежести. Обещать «в реальном времени» и молчать о том,
              когда числа взяты, значит обещать больше, чем есть: обновление
              идёт раз в две минуты, и человек вправе это видеть. */}
          {obnovleno && (
            <div className="page-sub">
              {t("updatedAt", {
                time: obnovleno.toLocaleTimeString(locale === "ru" ? "ru-RU" : "en-US", {
                  hour: "2-digit",
                  minute: "2-digit",
                }),
              })}
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <Link to="/clients?new=1" className="btn btn-secondary">
            <Icon name="userPlus" />
            {t("newClient")}
          </Link>
          {moduleOn(modules, "boards") && <NewBoardButton />}
        </div>
      </div>

      <div className="metric-grid">
        {/* Деньги первыми: владелец открывает сводку ради них, а не ради
            количества карточек. «Закрыто» считаем с начала месяца, а не за
            последние 30 дней — иначе число не сходится с месячной отчётностью.

            Без права на суммы плиток нет вовсе, а не три прочерка подряд:
            прочерк читается как «данных нет», и человек пойдёт искать, почему
            не считается, вместо того чтобы понять, что ему это не показывают.
            Сервер их всё равно не отдаст (`deals.view_amounts`). */}
        {seesMoney && (
          <>
            <div className="card card-pad">
              <div className="metric-title" style={{ marginBottom: 14 }}>
                {t("moneyInWork")}
              </div>
              <div className="metric-value money-value">
                {formatMoney(data.money_in_work, data.currency, locale)}
              </div>
              <div className="metric-sub">{t("dealsOpenNow", { n: openDeals })}</div>
            </div>
            {/* Плитка денег — первой из двух, и это то самое место, где
                владелец видел пустой месяц при полной кассе. Считаем по кассе
                (решение «банк один»): заказ, оплаченный мимо заявки, виден
                только так.

                «Закрыто за месяц» осталось рядом, а не заменено: это два разных
                вопроса — «получили» и «продали на», — и расхождение между ними
                полезное число, а не ошибка. Ось берём из ответа сервера, а не
                из своей карты блоков. */}
            {data.money_basis === "cash" && (
              <div className="card card-pad">
                <div className="metric-title" style={{ marginBottom: 14 }}>
                  {t("moneyReceivedThisMonth")}
                </div>
                <div className="metric-value money-value">
                  {formatMoney(data.money_received_this_month, data.currency, locale)}
                </div>
                <div className="metric-sub">{t("moneyReceivedHint")}</div>
              </div>
            )}
            <div className="card card-pad">
              <div className="metric-title" style={{ marginBottom: 14 }}>
                {data.money_basis === "cash" ? t("moneyWonValue") : t("moneyWonThisMonth")}
              </div>
              <div className="metric-value money-value">
                {formatMoney(data.money_won_this_month, data.currency, locale)}
              </div>
              <div className="metric-sub">
                {data.money_basis === "cash"
                  ? t("moneyWonValueHint", { n: data.won_count_this_month })
                  : t("dealsWonThisMonth", { n: data.won_count_this_month })}
              </div>
            </div>
            <div className="card card-pad">
              <div className="metric-title" style={{ marginBottom: 14 }}>
                {t("avgCheck")}
              </div>
              {/* Без единой сделки с ценой средний чек — прочерк, а не ноль: ноль
                  прочитают как «работаем даром». */}
              <div className="metric-value money-value">
                {data.avg_check === null ? "—" : formatMoney(data.avg_check, data.currency, locale)}
              </div>
              <div className="metric-sub">
                {data.avg_check === null ? t("avgCheckNone") : t("avgCheckHint")}
              </div>
            </div>
          </>
        )}
        <div className="card card-pad">
          <div className="metric-title" style={{ marginBottom: 14 }}>
            {t("metricClients")}
          </div>
          <div className="metric-value">{data.clients_total}</div>
          <div className="metric-sub">{t("addedThisMonth", { n: data.clients_this_month })}</div>
        </div>
      </div>

      {/* Воронка целиком, включая пустые этапы: «в согласовании ноль» — тоже
          ответ, и провал в середине видно только когда пустой этап нарисован. */}
      <div className="card card-pad" style={{ marginBottom: 28 }}>
        <div className="section-head" style={{ marginBottom: 14 }}>
          <div className="metric-title">{t("funnel")}</div>
          <Link to="/deals" className="section-link">
            {t("viewAll")}
          </Link>
        </div>
        <div className="funnel">
          {data.deals_by_stage.map((stage: any) => (
            <Link
              to={`/deals?stage=${encodeURIComponent(stage.key)}`}
              key={stage.key}
              className={"funnel-step kind-" + stage.kind}
            >
              <span className="funnel-count">{stage.count}</span>
              <span className="funnel-name">{stage.name}</span>
              <span
                className="funnel-bar"
                style={{ width: `${Math.round((stage.count / maxStage) * 100)}%` }}
              />
            </Link>
          ))}
        </div>
      </div>

      {/* Задачи того, кто смотрит: сводка отвечает на «с чего начать», а не
          «что вообще есть в фирме». */}
      {moduleOn(modules, "tasks") && (
        <div className="card card-pad" style={{ marginBottom: 28 }}>
          <div className="section-head" style={{ marginBottom: 14 }}>
            <div className="metric-title">{t("myTasksToday")}</div>
            <Link to="/tasks" className="section-link">
              {t("viewAll")}
            </Link>
          </div>
          {data.my_tasks.length === 0 ? (
            <div className="field-desc">{t("myTasksNone")}</div>
          ) : (
            <div className="dash-tasks">
              {data.my_tasks.map((task: any) => {
                const at = parseDate(task.due_at);
                const late = at && at.getTime() < Date.now();
                return (
                  <Link
                    to={task.deal_id ? `/deals/${task.deal_id}` : "/tasks"}
                    key={task.id}
                    className="dash-task"
                  >
                    <Icon name="clock" size={13} className={late ? "task-late" : undefined} />
                    <span style={{ flex: 1, minWidth: 0 }}>{task.title}</span>
                    <span className={"dash-task-due" + (late ? " task-late" : "")}>
                      {formatDateTime(task.due_at, locale)}
                    </span>
                  </Link>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Витрины ниже денег: это метрика портфолио, а не бизнеса. Выключены
          доски — вместе с ними уходит и весь блок про просмотры. */}
      {moduleOn(modules, "boards") && (
        <div className="card" style={{ padding: "18px 20px", display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 28, marginBottom: 28 }}>
          {/* Два числа рядом: сколько раз открывали и сколько людей открывало.
              Подпись у второго объясняет разницу — иначе «просмотров 108, а
              посетителей 3» читается как ошибка. */}
          <div style={{ display: "flex", gap: 40, flexWrap: "wrap" }}>
            <div>
              <div className="metric-title" style={{ marginBottom: 14 }}>
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
                {t("uniqueViewersTitle")}
              </div>
              <div className="metric-value">{data.unique_viewers_7d ?? 0}</div>
              <div className="metric-sub">{t("uniqueViewersHint")}</div>
            </div>
            <div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 14 }}>
                <div className="metric-title">{t("metricBoards")}</div>
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
      )}

      {storage && (
        <div style={{ marginBottom: 28 }}>
          <StorageCard storage={storage} onPurged={() => void refreshStorage()} />
        </div>
      )}

      {moduleOn(modules, "boards") && (
        <>
          <div className="section-head">
            <h2 className="section-title">{t("recentBoards")}</h2>
            <Link to="/boards" className="section-link">
              {t("viewAll")}
            </Link>
          </div>
          {data.recent_boards.length === 0 ? (
            <div className="card" style={{ marginBottom: 28 }}>
              <EmptyState title={t("noBoardsYet")} />
            </div>
          ) : (
            <div className="board-grid board-grid-4" style={{ marginBottom: 28 }}>
              {data.recent_boards.map((board: any) => (
                <BoardCard key={board.id} board={board} compact />
              ))}
            </div>
          )}
        </>
      )}

      {seesClients && (
        <>
          <div className="section-head" style={{ marginBottom: 12 }}>
            <h2 className="section-title">{t("recentClients")}</h2>
            <Link to="/clients" className="section-link">
              {t("allClients")}
            </Link>
          </div>
          <div className="list-card">
            {data.recent_clients.length === 0 && <EmptyState title={t("noClientsYet")} />}
            {data.recent_clients.map((client: any) => (
              <Link to={`/clients/${client.id}`} key={client.id} className="list-row hoverable">
                <Avatar text={initials(client.name)} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{client.name}</div>
                  <div style={{ color: "var(--faint)", fontSize: 12 }}>{client.company}</div>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {client.tags.slice(0, 2).map((tag: string) => (
                    <Chip key={tag}>{tag}</Chip>
                  ))}
                </div>
                <div style={{ color: "var(--faint)", fontSize: 12, width: 150, textAlign: "right", flexShrink: 0 }}>
                  {relativeDay(client.updated_at, locale)}
                </div>
              </Link>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
