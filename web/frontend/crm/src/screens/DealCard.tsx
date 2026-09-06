import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { DealLines } from "../components/DealLines";
import { DealStock } from "../components/DealStock";
import { CallButton, CallsPanel } from "../components/CallsPanel";
import { Feed } from "../components/Feed";
import { Icon } from "../components/Icon";
import { VyborKlienta } from "../components/VyborKlienta";
import { Chip, ConfirmModal, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic, useNachatayaPravka } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { kindLabel, paperLink, statusLabel, statusVariant } from "../lib/documents";
import { formatDate, formatDateTime, formatMoney, formatSpan, parseDate } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import { term } from "../lib/terms";
import { OrdersOfCard } from "../components/OrdersOfCard";
import { NewDocumentModal } from "./Documents";
import { MailCompose, type MailSender } from "./Mail";
import { QuickTask } from "./Tasks";

type Stage = { key: string; name: string; kind: "open" | "won" | "lost" };

/** Дата в поле ввода — «ГГГГ-ММ-ДД», сервер отдаёт ISO с временем. */
const asDateInput = (iso: string | null) => (iso ? iso.slice(0, 10) : "");

/** Минимальные единицы → поле ввода. Пусто, если суммы нет: ноль в поле
 *  выглядел бы как «работа бесплатная», а это другое состояние. */
const asMoneyInput = (minor: number | null | undefined) =>
  minor === null || minor === undefined ? "" : String(minor / 100);

/** Поле ввода → минимальные единицы. Округляем, а не отбрасываем дробь:
 *  «10.999» от быстрого набора должно стать 11.00, а не 10.99. */
function toMinor(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? Math.round(parsed * 100) : null;
}

export function DealCard() {
  const { id } = useParams();
  const { t, locale, user, modules, workspace, toast, toastError } = useApp();
  const seesMoney = can(user, "deals.view_amounts");
  const navigate = useNavigate();
  const [deal, setDeal] = useState<any>(null);
  // Сколько строк набрано. Со строками сумма — итог, а не поле ввода (§Р5), и
  // сервер правку отказывает: поле, которое всегда отвечает отказом, выглядит
  // сломанной карточкой. Счёт приходит из раздела строк, он его уже загрузил.
  const [strok, setStrok] = useState(0);
  // Сумма производна от строк — и остаётся такой при ВЫКЛЮЧЕННОМ складе:
  // блок прячет строки, но данные не стирает, и сервер по-прежнему отвечает
  // `amount_from_lines`. Пока склад включён, судим по живому счёту от
  // `DealLines` (он приходит сразу после правки), иначе — по ответу ручки.
  const sostavOtkryt = moduleOn(modules, "warehouse");
  const summaIzStrok = sostavOtkryt ? strok > 0 : Boolean(deal?.has_lines);
  const [issuing, setIssuing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [askReason, setAskReason] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const guard = useGuard();
  // Свой засов у акта: с засовом причины отказа он не общий — это разные окна
  // и разные действия, и запирать одно другим значило бы связать их без нужды.
  const actGuard = useGuard();
  const [composing, setComposing] = useState(false);

  const { failure, fail, clear } = useFailure();

  // Живое обновление не пишет в то, что человек уже трогал (docs/12 §8):
  // чистая карточка перечитывается молча, начатая правка получает полосу
  // «данные изменились — показать». Признак — хук по форме, полями не управляет.
  const koren = useRef<HTMLDivElement>(null);
  const nachata = useNachatayaPravka(koren, deal?.updated_at);
  const [ustarelo, setUstarelo] = useState(false);
  useLiveTopic("deals", (s) => {
    if (!s.resync && !s.hints.some((h) => h.id === Number(id))) return;
    if (nachata) setUstarelo(true);
    else void load();
  });
  // Авто-акт заводится правкой строк, отменяется закрытием заявки, накладная
  // проводится закрытием заказа — врезка бланков обязана это видеть сама.
  useLiveTopic(["documents", "orders", "waybills"], () => docs.reload());

  const load = useCallback(async () => {
    clear();
    try {
      setDeal(await api.get(`/deals/${id}`));
    } catch (e) {
      // Записи нет или она не наша: показывать «попробуйте ещё раз» тут не о
      // чем — повтор вернёт тот же ответ. Возвращаемся в список, как и раньше.
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/deals");
        return;
      }
      // Всё остальное — беда связи или сервера. Карточку не бросаем: адрес в
      // строке верный, и повторить имеет смысл именно его, а не список.
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  // Врезка живёт по двум условиям сразу: блок включён И право есть. Порядок тот
  // же, что на сервере. Без права раздел не просто пуст — его не показываем
  // вовсе: врезка «Бланки» с кнопкой, которая отвечает отказом, хуже её
  // отсутствия, а лишний запрос на каждой карточке заявки — ещё и шум в журнале.
  const hasDocuments = moduleOn(modules, "documents") && can(user, "documents.view");
  const hasCompanies = moduleOn(modules, "companies") && can(user, "companies.view");
  const hasTasks = moduleOn(modules, "tasks") && can(user, "tasks.view");
  const hasMail = moduleOn(modules, "mail") && can(user, "mail.create");

  // Справочники карточки — через общий крючок, а не своим `catch(() => [])` на
  // каждый. Пустой массив вместо отказа врал по-разному, но всегда молча:
  // «что дальше» оставалось без единой кнопки этапа, ответственный и клиент
  // исчезали из своих списков вместе с текущим значением, а во врезке бланков
  // стояло «Бланков нет». `null` в пути — «спрашивать нечего»: блок выключен
  // или права нет, и это не отказ.
  const stages = useReference<Stage>("/pipeline/stages");
  const people = useReference<any>("/people");
  const companies = useReference<any>(hasCompanies ? "/companies" : null);
  const docs = useReference<any>(hasDocuments ? `/documents?deal_id=${id}` : null);
  const tasks = useReference<any>(hasTasks ? `/tasks?deal_id=${id}` : null);
  // Ящики нужны только выбору отправителя и доступны только root. Не ответило —
  // форма работает: сервер возьмёт первый активный ящик сам.
  const mailAccounts = useReference<MailSender>(hasMail ? "/mail/senders" : null);

  useEffect(() => {
    void load();
  }, [load]);

  if (!deal) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const currency: string = deal.currency || "USD";
  // Адрес приходит вместе с заявкой. Прежде он выуживался из справочника,
  // загруженного в поле выбора первыми двумя сотнями, — и у клиента за этим
  // пределом «кому» оказывалось пустым.
  const dealClientEmail: string = deal.client_email || "";
  const stage: Stage | undefined = (stages.items ?? []).find((s) => s.key === deal.stage);
  const overdue =
    deal.due_at && !deal.closed_at && new Date(deal.due_at) < new Date();

  const patch = async (data: Record<string, unknown>) => {
    try {
      setDeal(await api.patch(`/deals/${id}`, data));
    } catch (e) {
      toastError(e);
      void load();
    }
  };

  const moveTo = async (key: string) => {
    const target = (stages.items ?? []).find((s) => s.key === key);
    // У проигранного этапа спрашиваем причину: без неё отчёт по потерям
    // показывает число и ничем не помогает.
    if (target?.kind === "lost") {
      setReason("");
      setAskReason(key);
      return;
    }
    try {
      setDeal(await api.post(`/deals/${id}/move`, { stage: key }));
    } catch (e) {
      toastError(e);
      // Этап уже сменил кто-то другой — показываем, каким он стал на самом
      // деле, а не тот, что был открыт у нас на экране.
      if (e instanceof ApiError && e.code === "stage_moved_meanwhile") void load();
    }
  };

  const confirmLost = async () => {
    // Причину подтверждают Enter'ом и кнопкой сразу — два перехода этапа
    // подряд оставляли бы в истории заявки две одинаковые записи.
    if (!guard.take()) return;
    try {
      setDeal(await api.post(`/deals/${id}/move`, { stage: askReason, lost_reason: reason }));
      setAskReason(null);
    } catch (e) {
      toastError(e);
      if (e instanceof ApiError && e.code === "stage_moved_meanwhile") {
        setAskReason(null);
        void load();
      }
    } finally {
      guard.free();
    }
  };

  const nextOpen = (stages.items ?? []).filter((s) => s.kind === "open");
  // У закрытой сделки текущего открытого этапа нет. Пройденные при этом не
  // додумываем: закрыть могли и с первого этапа, а выдуманный путь врёт.
  const tekushchiy = nextOpen.findIndex((s) => s.key === deal.stage);
  const closers = (stages.items ?? []).filter((s) => s.kind !== "open");

  return (
    <div className="page page-narrow" ref={koren}>
      {ustarelo && (
        <div className="maintenance-bar" style={{ marginBottom: 12 }}>
          <span className="dot" />
          {t("liveStale")}
          <button className="btn btn-secondary btn-sm" style={{ marginLeft: 12 }} onClick={() => { setUstarelo(false); void load(); }}>
            {t("liveShow")}
          </button>
        </div>
      )}
      <Link to="/deals" className="back-link">
        <Icon name="arrowLeft" size={14} />
        {term(workspace.deal_term, locale, "many")}
      </Link>

      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* Название правится прямо здесь: заходить в отдельную форму ради
              одной строки — лишний шаг в ежедневной работе. */}
          <input
            className="title-input"
            defaultValue={deal.title}
            onBlur={(e) => {
              const value = e.target.value.trim();
              if (value && value !== deal.title) void patch({ title: value });
            }}
          />
          <div className="page-sub" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Link to={`/clients/${deal.client_id}`} className="text-link">
              {deal.client_name || t("noClient")}
            </Link>
            {stage && (
              <Chip variant={stage.kind === "won" ? "success" : stage.kind === "lost" ? "warning" : undefined}>
                {stage.name}
              </Chip>
            )}
          </div>
        </div>
        {/* Письмо по заявке уходит отсюда и попадает в ленту ЭТОЙ заявки —
            ради этого в записи ленты и есть deal_id. */}
        {hasMail && dealClientEmail && (
          <button className="btn btn-secondary" onClick={() => setComposing(true)}>
            <Icon name="send" size={14} />
            {t("compose")}
          </button>
        )}
        <div style={{ display: "flex", gap: 10 }}>
          {/* Звонок из карточки уходит в эту же заявку: набравший и так знает,
              о чём разговор, — незачем потом привязывать звонок руками. */}
          <CallButton number={deal.client_phone ?? ""} dealId={deal.id} />
          <button className="btn btn-secondary" onClick={() => setConfirmDelete(true)}>
            <Icon name="trash" size={14} />
            {t("delete")}
          </button>
        </div>
      </div>

      {/* Итоги плитками — то, что спрашивают, открыв заявку: сумма, оплачено,
          остаток, срок. Своей строкой под шапкой, а не в ней: на узкой карточке
          плитки отжимали название до нечитаемого. Поля для правки — ниже. */}
      <DealItogi deal={deal} seesMoney={seesMoney} currency={currency} />

      {/* Действия — первым делом. Главный вопрос к открытой сделке «что
          дальше», и ответ не должен требовать возврата на доску. */}
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="metric-title" style={{ marginBottom: 12 }}>{t("whatNext")}</div>
        {/* Закрытая заявка: исход словами — когда и кем, — а не одни серые шаги. */}
        {deal.closed_at && stage && (stage.kind === "won" || stage.kind === "lost") && (
          <div
            className={"field-desc"}
            style={{ marginTop: 0, marginBottom: 12, color: stage.kind === "won" ? "var(--success)" : "var(--danger)", fontWeight: 500 }}
          >
            {t(stage.kind === "won" ? "dealOutcomeWon" : "dealOutcomeLost", {
              when: formatDateTime(deal.closed_at, locale),
              who: (deal.stage_history ?? []).slice(-1)[0]?.author_name || "—",
            })}
          </div>
        )}
        <ol className="shagi">
          {nextOpen.map((s, i) => {
            const gde =
              i === tekushchiy ? "seychas" : tekushchiy >= 0 && i < tekushchiy ? "proyden" : "vperedi";
            return (
              <li key={s.key} className={"shag " + gde}>
                <span className="shag-liniya" />
                <button
                  className="shag-knopka"
                  disabled={s.key === deal.stage}
                  onClick={() => void moveTo(s.key)}
                >
                  <span className="shag-krug">
                    {gde === "proyden" ? <Icon name="check" size={14} stroke={2} /> : i + 1}
                  </span>
                  <span className="shag-telo">
                    <span className="shag-nazvanie">{s.name}</span>
                    <span className="shag-metka">
                      {t(gde === "proyden" ? "stagePassed" : gde === "seychas" ? "stageNow" : "stageAhead")}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
        <div className="stage-actions">
          {closers.map((s) => (
            <button
              key={s.key}
              className={
                "stage-btn " + (s.kind === "won" ? "stage-won" : "stage-lost") +
                (s.key === deal.stage ? " current" : "")
              }
              disabled={s.key === deal.stage}
              onClick={() => void moveTo(s.key)}
            >
              {s.kind === "won" && <Icon name="check" size={13} stroke={2} />}
              {s.name}
            </button>
          ))}
        </div>
        {/* Без воронки кнопок этапа нет вовсе — а «что дальше» без единой
            кнопки читается как «дальше ничего», то есть как ответ. */}
        {stages.failure !== null && (
          <LoadFailed error={stages.failure} onRetry={stages.reload} />
        )}
        {deal.lost_reason && (
          <div style={{ color: "var(--danger)", fontSize: 12.5, marginTop: 10 }}>
            {t("lostReason")}: {deal.lost_reason}
          </div>
        )}
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="deal-fields">
          <div className="field">
            <label className="label">{t("responsible")}</label>
            <select
              className="input"
              value={deal.manager_id ?? ""}
              onChange={(e) => void patch({ manager_id: e.target.value ? Number(e.target.value) : null })}
            >
              <option value="">{t("nobody")}</option>
              {(people.items ?? []).map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
            {/* Список не приехал — в нём нет и текущего ответственного, то есть
                выбор молча показывает «Никто» у заявки, у которой он есть. */}
            {people.failure !== null && (
              <LoadFailed error={people.failure} onRetry={people.reload} />
            )}
          </div>
          <div className="field">
            <label className="label">{t("client")}</label>
            <VyborKlienta
              value={deal.client_id}
              imya={deal.client_name}
              onPick={(kto) => kto !== null && void patch({ client_id: kto })}
            />
          </div>
          {/* Спрашиваем, только когда есть из чего выбирать. У большинства
              фирма одна, и поле с единственным вариантом — вопрос ради ответа,
              который всегда один и тот же. Пусто означает «от основной». */}
          {hasCompanies && (companies.items?.length ?? 0) > 1 && (
            <div className="field">
              <label className="label">{t("companyOfDeal")}</label>
              <select
                className="input"
                value={deal.company_id ?? ""}
                onChange={(e) =>
                  void patch({ company_id: e.target.value ? Number(e.target.value) : null })
                }
              >
                <option value="">{t("companyOfDealDefault")}</option>
                {(companies.items ?? []).map((c: any) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            </div>
          )}
          {/* Отказ прячет сам выбор: список короче двух — это «фирма одна», и
              поле не показывается по делу. Молча совпасть эти два состояния не
              должны — иначе заявка уедет на реквизиты основной фирмы, а
              выбирали другую. */}
          {hasCompanies && companies.failure !== null && (
            <div className="field">
              <LoadFailed error={companies.failure} onRetry={companies.reload} />
            </div>
          )}
          <div className="field">
            <label className="label">{t("dueDate")}</label>
            <input
              className={"input" + (overdue ? " overdue" : "")}
              type="date"
              defaultValue={asDateInput(deal.due_at)}
              onChange={(e) => void patch({ due_at: e.target.value ? `${e.target.value}T12:00:00` : null })}
            />
            {overdue && <div className="field-desc" style={{ color: "var(--danger)" }}>{t("overdue")}</div>}
          </div>
        </div>
        {/* Деньги. Вводятся в обычных единицах, хранятся в минимальных —
            перевод делаем здесь, на краю, а не в базе.

            Без права `deals.view_amounts` блока нет вовсе. Показать пустые поля
            было бы хуже прочерка: человек вписал бы туда сумму, получил отказ
            сервера и решил, что карточка сломана. Заявка при этом ведётся как
            обычно — в этом и смысл отдельного права на деньги. */}
        {seesMoney && (
        <div className="deal-fields" style={{ marginTop: 4 }}>
          <div className="field">
            <label className="label">{t("dealAmount")}</label>
            <input
              className="input"
              type="number"
              min={0}
              step="0.01"
              readOnly={summaIzStrok}
              // `readOnly`, а не `disabled`: серое нечитаемое поле прячет саму
              // сумму, а её как раз и смотрят. Значение остаётся выделяемым и
              // копируемым, править нельзя.
              // `key` держит поле в согласии с суммой: у неуправляемого поля
              // `defaultValue` после первой отрисовки не читается вовсе, и без
              // пересоздания карточка показывала бы прежнее число.
              key={`amount-${deal.amount}`}
              defaultValue={asMoneyInput(deal.amount)}
              onBlur={(e) => {
                const next = toMinor(e.target.value);
                if (next !== deal.amount) void patch({ amount: next });
              }}
            />
            {summaIzStrok && <div className="field-desc">{t("amountFromLines")}</div>}
          </div>
          <div className="field">
            <label className="label">{t("dealPrepaid")}</label>
            <input
              className="input"
              type="number"
              min={0}
              step="0.01"
              defaultValue={asMoneyInput(deal.prepaid)}
              onBlur={(e) => {
                const next = toMinor(e.target.value) ?? 0;
                if (next !== deal.prepaid) void patch({ prepaid: next });
              }}
            />
          </div>
          <div className="field">
            <label className="label">{t("dealRemainder")}</label>
            <div className={"money-readout" + (deal.is_paid ? " paid" : "")}>
              {deal.is_paid ? t("dealPaidInFull") : formatMoney(deal.remainder, currency, locale)}
            </div>
            {/* Переплату не прячем: клиент округлил вверх или доплатил за
                срочность — это надо видеть, а не молча считать нулём. */}
            {deal.remainder !== null && deal.remainder < 0 && (
              <div className="field-desc">
                {t("dealOverpaid", { sum: formatMoney(-deal.remainder, currency, locale) })}
              </div>
            )}
          </div>
        </div>
        )}

        <div className="field" style={{ marginTop: 4 }}>
          <label className="label">{t("dealDetails")}</label>
          <textarea
            className="input"
            rows={4}
            placeholder={t("dealDetailsHint")}
            defaultValue={deal.description}
            onBlur={(e) => {
              if (e.target.value !== deal.description) void patch({ description: e.target.value });
            }}
          />
        </div>
      </div>

      {/* Лента: звонки, письма, встречи и заметки одним потоком. Стоит
          сразу после полей — это то, что читают, открыв заявку. */}
      <Feed dealId={deal.id} clientId={deal.client_id} />

      {/* Звонки по этой заявке. Сам разговор уже стоит в ленте выше — здесь
          то, что в строку ленты не влезает: длительность, итог, запись. */}
      {moduleOn(modules, "telephony") && can(user, "telephony.view") && (
        <div className="card card-pad" style={{ marginBottom: 20 }}>
          <div className="metric-title" style={{ marginBottom: 12 }}>{t("calls")}</div>
          <CallsPanel dealId={deal.id} />
        </div>
      )}

      {/* Напоминание прямо отсюда: «перезвонить в четверг» придумывается во
          время разговора о заявке, а не потом на отдельном экране. */}
      {hasTasks && (
        <div className="card card-pad" style={{ marginBottom: 20 }}>
          <div className="metric-title" style={{ marginBottom: 12 }}>{t("tasks")}</div>
          {(tasks.items ?? []).map((task: any) => (
            <div key={task.id} className="doc-mini">
              <span className="truncate" style={{ flex: 1, minWidth: 0 }}>{task.title}</span>
              {task.due_at && (
                <span style={{ color: "var(--faint)", fontSize: 12 }}>
                  {formatDateTime(task.due_at, locale)}
                </span>
              )}
            </div>
          ))}
          {tasks.failure !== null && (
            <LoadFailed error={tasks.failure} onRetry={tasks.reload} />
          )}
          <QuickTask dealId={deal.id} clientId={deal.client_id} onCreated={tasks.reload} />
        </div>
      )}

      {/* Что ушло со склада под эту заявку и во сколько это обошлось. Стоит
          рядом с суммой не случайно: выручка без себестоимости не отвечает на
          вопрос, заработали мы на этой работе или нет. */}
      <DealLines
        dealId={deal.id}
        closed={deal.closed_at !== null}
        onOrder={() => void load()}
        onSostav={(skolko, itog) => {
          setStrok(skolko);
          // Правка строк меняет сумму НА СЕРВЕРЕ, а карточка держит свою копию:
          // без перечитывания она показывала $408 у заявки, у которой суммы уже
          // нет вовсе. Перечитываем целиком, а не подменяем одно поле: остаток к
          // оплате и «оплачено» считаются вместе с суммой, и порознь однажды
          // разойдутся.
          if (deal && deal.amount !== itog) void load();
        }}
      />
      <DealStock dealId={deal.id} />

      {/* Доски, сделанные по этой заявке. Раньше доска знала только клиента,
          и у клиента с пятью заказами за год все они лежали одной кучей.
          Условие на блок не лишнее рядом с проверкой длины: сервер перестаёт
          класть доски в ответ сразу, а уже загруженная карточка держит их в
          состоянии до следующего запроса. */}
      {moduleOn(modules, "boards") && can(user, "boards.view") && (deal.boards ?? []).length > 0 && (
        <div className="card card-pad" style={{ marginBottom: 20 }}>
          <div className="metric-title" style={{ marginBottom: 12 }}>{t("boards")}</div>
          <div className="doc-mini-list">
            {(deal.boards ?? []).map((board: any) => (
              <Link key={board.id} to={`/boards/${board.id}`} className="doc-mini">
                <span className="truncate" style={{ flex: 1, minWidth: 0 }}>{board.title}</span>
                <Chip variant={board.is_published ? "success" : undefined}>
                  {board.is_published ? t("published") : t("draft")}
                </Chip>
              </Link>
            ))}
          </div>
        </div>
      )}

      {/* Заказы этой заявки. Заказ может принадлежать заявке, но не заменяет
          её: заявка — это работа, заказ — перечень позиций. */}
      <OrdersOfCard dealId={Number(id)} />

      {/* Бланки этой сделки. Приняли вещь — выдали бумагу; искать её потом в
          общем списке значит потерять связь с работой, ради которой её выдали. */}
      {hasDocuments && (
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="page-head" style={{ marginBottom: 12 }}>
          <div className="metric-title">{t("docOfDeal")}</div>
          <div style={{ display: "flex", gap: 8 }}>
            {/* Акт заводится отсюда, а не из общего списка бланков: он закрывает
                РАБОТУ, и без заявки ему нечего закрывать и некуда переводить.
                Кнопка на карточке — единственное место, где заявка известна
                заранее, и спрашивать её ещё раз формой было бы вопросом ради
                ответа, который уже дан. */}
            {can(user, "documents.create") && (
              <button
                className="btn btn-secondary btn-sm"
                disabled={actGuard.busy}
                onClick={() => {
                  void (async () => {
                    // Акт заводится пустым и сразу открывается: пока сервер
                    // отдаёт номер, кнопка выглядит неотвеченной, и второе
                    // нажатие заводило второй акт по той же работе. Уходили в
                    // один, а закрывали заявку потом другим — с пустым
                    // перечнем. Отпускать нечего: при успехе экран уезжает.
                    if (!actGuard.take()) return;
                    try {
                      const act = await api.post("/documents/acts", { deal_id: deal.id });
                      navigate(`/documents/${act.id}`);
                    } catch (e) {
                      toastError(e);
                      actGuard.free();
                    }
                  })();
                }}
              >
                <Icon name="check" size={13} stroke={2} />
                {t("actNew")}
              </button>
            )}
            <button className="btn btn-secondary btn-sm" onClick={() => setIssuing(true)}>
              <Icon name="printer" size={13} />
              {t("issueDocument")}
            </button>
          </div>
        </div>
        {/* «Бланков нет» — только когда их действительно нет: выданную бумагу
            легко выдать второй раз, поверив пустой врезке. */}
        {docs.failure !== null ? (
          <LoadFailed error={docs.failure} onRetry={docs.reload} />
        ) : (docs.items?.length ?? 0) === 0 ? (
          <div className="field-desc" style={{ marginTop: 0 }}>{t("noDocuments")}</div>
        ) : (
          <div className="doc-mini-list">
            {(docs.items ?? []).map((doc) => (
              <Link key={doc.id} to={paperLink(doc)} className="doc-mini">
                <span className="doc-number">{doc.number}</span>
                <span className="truncate" style={{ flex: 1, minWidth: 0 }}>
                  {doc.payload?.fields?.item || kindLabel(t, doc.kind)}
                </span>
                <Chip variant={statusVariant(doc.status, doc.kind)}>{statusLabel(t, doc.status, doc.kind)}</Chip>
              </Link>
            ))}
          </div>
        )}
      </div>
      )}

      <div className="card card-pad">
        <div className="metric-title" style={{ marginBottom: 12 }}>{t("stageHistory")}</div>
        {/* `?? []` — не перестраховка: неполный ответ уже отправлял этот экран
            в белое. Пустая история читается, отсутствующий экран — нет. */}
        <ol className="stage-log">
          {(deal.stage_history ?? []).map((h: any, i: number, vse: any[]) => {
            // Сколько простояла в этапе: до следующего перехода, у последнего —
            // до сих пор (закрытой — до закрытия).
            const ot = parseDate(h.changed_at);
            const do_ = i + 1 < vse.length ? parseDate(vse[i + 1].changed_at) : deal.closed_at ? parseDate(deal.closed_at) : new Date();
            const span = ot && do_ && !(i + 1 === vse.length && deal.closed_at) ? formatSpan(do_.getTime() - ot.getTime(), locale) : "";
            return (
              <li key={h.id}>
                <span className="stage-log-when">{formatDateTime(h.changed_at, locale)}</span>
                <span className="stage-log-what">
                  {h.from_name ? `${h.from_name} → ${h.to_name}` : h.to_name}
                </span>
                <span className="stage-log-span" title={span ? t("stageSpanHint") : undefined}>{span}</span>
                <span className="stage-log-who">{h.author_name || "—"}</span>
              </li>
            );
          })}
        </ol>
        <div className="field-desc" style={{ marginTop: 10 }}>
          {t("createdAt", { t: formatDate(deal.created_at, locale) })}
          {deal.closed_at && ` · ${t("closedAt", { t: formatDate(deal.closed_at, locale) })}`}
        </div>
      </div>

      {/* Причина отказа — это ввод, а не подтверждение, поэтому обычное окно.
          Пропустить можно: заставлять писать причину, когда клиент просто
          пропал, — способ получить сто отписок «нет» вместо данных. */}
      {askReason && (
        <Modal title={t("whyLost")} onClose={() => setAskReason(null)}>
          <div className="field">
            <input
              className="input"
              autoFocus
              placeholder={t("whyLostPlaceholder")}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void confirmLost();
              }}
            />
            <div className="field-desc">{t("whyLostHint")}</div>
          </div>
          <button
            className="btn btn-primary"
            style={{ width: "100%" }}
            disabled={guard.busy}
            onClick={() => void confirmLost()}
          >
            {t("confirm")}
          </button>
        </Modal>
      )}

      {issuing && hasDocuments && (
        <NewDocumentModal
          dealId={deal.id}
          clientId={deal.client_id}
          onClose={() => setIssuing(false)}
          onCreated={(doc) => navigate(`/documents/${doc.id}`)}
        />
      )}

      {composing && (
        <MailCompose
          accounts={mailAccounts.items ?? []}
          to={dealClientEmail}
          clientId={deal.client_id}
          dealId={deal.id}
          onClose={() => setComposing(false)}
          onSent={() => void load()}
        />
      )}

      {confirmDelete && (
        <ConfirmModal
          text={t("deleteDealConfirm", { name: deal.title })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => {
            void (async () => {
              try {
                await api.del(`/deals/${id}`);
                toast(t("deleted"));
                navigate("/deals");
              } catch (e) {
                toastError(e);
              }
            })();
          }}
          onClose={() => setConfirmDelete(false)}
        />
      )}
    </div>
  );
}


/** Итоги заявки в шапке: сумма, оплачено, остаток, срок. Без права на суммы —
 *  только срок: пустые плитки читались бы как «денег нет». */
function DealItogi({ deal, seesMoney, currency }: { deal: any; seesMoney: boolean; currency: string }) {
  const { t, locale } = useApp();
  const money = (value: number | null) => formatMoney(value, currency, locale);
  const srok = parseDate(deal.due_at);
  const dney = srok ? Math.round((srok.getTime() - Date.now()) / 86_400_000) : null;
  const prosrocheno = dney !== null && dney < 0 && !deal.closed_at;
  return (
    <div className="svodka-plitki szhato" style={{ marginBottom: 20 }}>
      {seesMoney && (
        <>
          <div className="svodka-plitka">
            <div className="svodka-l">{t("dealAmount")}</div>
            <div className="svodka-v">{deal.amount === null ? "—" : money(deal.amount)}</div>
            <div className="svodka-sub">{deal.amount === null ? t("dealNoAmount") : t("dealPrepaid").toLowerCase() + ": " + money(deal.prepaid)}</div>
          </div>
          <div className={"svodka-plitka" + (deal.is_paid ? " horosho" : "")}>
            <div className="svodka-l">{t("dealRemainder")}</div>
            <div className="svodka-v">{deal.is_paid ? t("dealPaidInFull") : deal.remainder === null ? "—" : money(deal.remainder)}</div>
            <div className="svodka-sub">
              {deal.remainder !== null && deal.remainder < 0 ? t("dealOverpaid", { sum: money(-deal.remainder) }) : t("dealRemainderHint")}
            </div>
          </div>
        </>
      )}
      <div className={"svodka-plitka" + (prosrocheno ? " beda" : "")}>
        <div className="svodka-l">{t("dueDate")}</div>
        <div className="svodka-v">{srok ? formatDate(deal.due_at, locale) : "—"}</div>
        <div className="svodka-sub">
          {dney === null
            ? t("dealNoDue")
            : deal.closed_at
              ? t("closedAt", { t: formatDate(deal.closed_at, locale) })
              : dney < 0
                ? t("dealOverdueDays", { n: -dney })
                : dney === 0
                  ? t("dealDueToday")
                  : t("dealDueIn", { n: dney })}
        </div>
      </div>
    </div>
  );
}
