import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { DealStock } from "../components/DealStock";
import { Feed } from "../components/Feed";
import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { statusLabel, statusVariant } from "../lib/documents";
import { formatDate, formatDateTime, formatMoney } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { term } from "../lib/terms";
import { NewDocumentModal } from "./Documents";
import { MailCompose, type MailAccount } from "./Mail";
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
  const { t, locale, modules, workspace, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [deal, setDeal] = useState<any>(null);
  const [stages, setStages] = useState<Stage[]>([]);
  const [people, setPeople] = useState<any[]>([]);
  const [clients, setClients] = useState<any[]>([]);
  const [companies, setCompanies] = useState<any[]>([]);
  const [docs, setDocs] = useState<any[]>([]);
  const [tasks, setTasks] = useState<any[]>([]);
  const [issuing, setIssuing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [askReason, setAskReason] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [composing, setComposing] = useState(false);
  const [mailAccounts, setMailAccounts] = useState<MailAccount[]>([]);

  const load = useCallback(async () => {
    try {
      setDeal(await api.get(`/deals/${id}`));
    } catch (e) {
      toastError(e);
      navigate("/deals");
    }
  }, [id, toastError, navigate]);

  // Блок бланков могли выключить — тогда и не спрашиваем: запрос всё равно
  // вернёт отказ, а раздел в карточке показывать нечему.
  const hasDocuments = moduleOn(modules, "documents");

  const loadDocs = useCallback(() => {
    if (!hasDocuments) return;
    api.get(`/documents?deal_id=${id}`).then((d) => setDocs(d.items)).catch(() => undefined);
  }, [id, hasDocuments]);

  const hasCompanies = moduleOn(modules, "companies");

  const hasTasks = moduleOn(modules, "tasks");

  const loadTasks = useCallback(() => {
    if (!hasTasks) return;
    api.get(`/tasks?deal_id=${id}`).then((d) => setTasks(d.items)).catch(() => undefined);
  }, [id, hasTasks]);

  const hasMail = moduleOn(modules, "mail");

  // Ящики нужны только выбору отправителя и доступны только root. Не ответило —
  // форма работает: сервер возьмёт первый активный ящик сам.
  useEffect(() => {
    if (!hasMail) return;
    api.get("/mail/accounts").then((d) => setMailAccounts(d.items)).catch(() => undefined);
  }, [hasMail]);

  useEffect(() => {
    void load();
    loadDocs();
    loadTasks();
    api.get("/pipeline/stages").then((d) => setStages(d.items)).catch(() => undefined);
    api.get("/people").then((d) => setPeople(d.items)).catch(() => undefined);
    api.get("/clients?per_page=200").then((d) => setClients(d.items)).catch(() => undefined);
    if (hasCompanies) {
      api.get("/companies").then((d) => setCompanies(d.items)).catch(() => undefined);
    }
  }, [load, loadDocs, loadTasks, hasCompanies]);

  if (!deal) return <ScreenLoading />;

  const currency: string = deal.currency || "USD";
  // Адрес берём из уже загруженного списка клиентов: отдельный запрос ради
  // одной строки в форме отправки — лишний круг к серверу на каждой карточке.
  const dealClientEmail: string = clients.find((c) => c.id === deal.client_id)?.email || "";
  const stage: Stage | undefined = stages.find((s) => s.key === deal.stage);
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
    const target = stages.find((s) => s.key === key);
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
    }
  };

  const confirmLost = async () => {
    try {
      setDeal(await api.post(`/deals/${id}/move`, { stage: askReason, lost_reason: reason }));
      setAskReason(null);
    } catch (e) {
      toastError(e);
    }
  };

  const nextOpen = stages.filter((s) => s.kind === "open");
  const closers = stages.filter((s) => s.kind !== "open");

  return (
    <div className="page page-narrow">
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
        <button className="btn btn-secondary" onClick={() => setConfirmDelete(true)}>
          <Icon name="trash" size={14} />
          {t("delete")}
        </button>
      </div>

      {/* Действия — первым делом. Главный вопрос к открытой сделке «что
          дальше», и ответ не должен требовать возврата на доску. */}
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="metric-title" style={{ marginBottom: 12 }}>{t("whatNext")}</div>
        <div className="stage-actions">
          {nextOpen.map((s) => (
            <button
              key={s.key}
              className={"stage-btn" + (s.key === deal.stage ? " current" : "")}
              disabled={s.key === deal.stage}
              onClick={() => void moveTo(s.key)}
            >
              {s.name}
            </button>
          ))}
          <span className="stage-sep" />
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
              {people.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="label">{t("client")}</label>
            <select
              className="input"
              value={deal.client_id}
              onChange={(e) => void patch({ client_id: Number(e.target.value) })}
            >
              {clients.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          </div>
          {/* Спрашиваем, только когда есть из чего выбирать. У большинства
              фирма одна, и поле с единственным вариантом — вопрос ради ответа,
              который всегда один и тот же. Пусто означает «от основной». */}
          {hasCompanies && companies.length > 1 && (
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
                {companies.map((c: any) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
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
            перевод делаем здесь, на краю, а не в базе. */}
        <div className="deal-fields" style={{ marginTop: 4 }}>
          <div className="field">
            <label className="label">{t("dealAmount")}</label>
            <input
              className="input"
              type="number"
              min={0}
              step="0.01"
              defaultValue={asMoneyInput(deal.amount)}
              onBlur={(e) => {
                const next = toMinor(e.target.value);
                if (next !== deal.amount) void patch({ amount: next });
              }}
            />
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

      {/* Напоминание прямо отсюда: «перезвонить в четверг» придумывается во
          время разговора о заявке, а не потом на отдельном экране. */}
      {moduleOn(modules, "tasks") && (
        <div className="card card-pad" style={{ marginBottom: 20 }}>
          <div className="metric-title" style={{ marginBottom: 12 }}>{t("tasks")}</div>
          {tasks.map((task: any) => (
            <div key={task.id} className="doc-mini">
              <span className="truncate" style={{ flex: 1, minWidth: 0 }}>{task.title}</span>
              {task.due_at && (
                <span style={{ color: "var(--faint)", fontSize: 12 }}>
                  {formatDateTime(task.due_at, locale)}
                </span>
              )}
            </div>
          ))}
          <QuickTask dealId={deal.id} clientId={deal.client_id} onCreated={loadTasks} />
        </div>
      )}

      {/* Что ушло со склада под эту заявку и во сколько это обошлось. Стоит
          рядом с суммой не случайно: выручка без себестоимости не отвечает на
          вопрос, заработали мы на этой работе или нет. */}
      <DealStock dealId={deal.id} />

      {/* Доски, сделанные по этой заявке. Раньше доска знала только клиента,
          и у клиента с пятью заказами за год все они лежали одной кучей. */}
      {(deal.boards ?? []).length > 0 && (
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

      {/* Бланки этой сделки. Приняли вещь — выдали бумагу; искать её потом в
          общем списке значит потерять связь с работой, ради которой её выдали. */}
      {hasDocuments && (
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="page-head" style={{ marginBottom: 12 }}>
          <div className="metric-title">{t("docOfDeal")}</div>
          <button className="btn btn-secondary btn-sm" onClick={() => setIssuing(true)}>
            <Icon name="printer" size={13} />
            {t("issueDocument")}
          </button>
        </div>
        {docs.length === 0 ? (
          <div className="field-desc" style={{ marginTop: 0 }}>{t("noDocuments")}</div>
        ) : (
          <div className="doc-mini-list">
            {docs.map((doc) => (
              <Link key={doc.id} to={`/documents/${doc.id}`} className="doc-mini">
                <span className="doc-number">{doc.number}</span>
                <span className="truncate" style={{ flex: 1, minWidth: 0 }}>
                  {doc.payload?.fields?.item || "—"}
                </span>
                <Chip variant={statusVariant(doc.status)}>{statusLabel(t, doc.status)}</Chip>
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
          {(deal.stage_history ?? []).map((h: any) => (
            <li key={h.id}>
              <span className="stage-log-when">{formatDateTime(h.changed_at, locale)}</span>
              <span className="stage-log-what">
                {h.from_name ? `${h.from_name} → ${h.to_name}` : h.to_name}
              </span>
              <span className="stage-log-who">{h.author_name || "—"}</span>
            </li>
          ))}
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
          <button className="btn btn-primary" style={{ width: "100%" }} onClick={() => void confirmLost()}>
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
          accounts={mailAccounts}
          to={dealClientEmail}
          clientId={deal.client_id}
          dealId={deal.id}
          onClose={() => setComposing(false)}
          onSent={() => void load()}
        />
      )}

      {confirmDelete && (
        <ConfirmModal
          text={t("deleteDealConfirm")}
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
