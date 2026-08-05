import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Avatar, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDate, formatMoney, initials } from "../lib/format";
import { term } from "../lib/terms";


/** Названия этапов приходят с сервера: у ремонта техники «диагностика», у
 *  салона «клиент пришёл», у магазина «отправлен» — общего списка не бывает.
 *  Здесь их не переводим и не подменяем: это слова конкретного бизнеса. */
type Deal = {
  id: number;
  title: string;
  client_id: number;
  client_name: string | null;
  manager_id: number | null;
  manager_name: string | null;
  manager_avatar: string | null;
  stage: string;
  sort_order: number;
  due_at: string | null;
  lost_reason: string;
  /** Деньги в минимальных единицах. null — сумму ещё не называли; это не ноль:
   *  ноль означает «работа бесплатная», и в отчёте они считаются по-разному. */
  amount: number | null;
  prepaid: number;
  remainder: number | null;
  is_paid: boolean;
};

type Column = {
  key: string;
  name: string;
  kind: "open" | "won" | "lost";
  color: string;
  /** Сумма по всем сделкам этапа — считает сервер: колонка отдаётся с
   *  пределом, и сложение показанных карточек занижало бы итог. */
  amount_total: number;
  deals: Deal[];
};

export function Deals() {
  const { t, locale, workspace, toastError } = useApp();
  const navigate = useNavigate();
  const [columns, setColumns] = useState<Column[] | null>(null);
  const [clients, setClients] = useState<any[]>([]);
  const [dragId, setDragId] = useState<number | null>(null);
  const [overStage, setOverStage] = useState<string | null>(null);
  const [people, setPeople] = useState<any[]>([]);
  const [creating, setCreating] = useState(false);
  // Валюта одна на систему и приходит вместе с доской: настройки читает
  // только root, а суммы видят все.
  const [currency, setCurrency] = useState("USD");
  const [draft, setDraft] = useState({
    title: "",
    client_id: "",
    manager_id: "",
    due_at: "",
    description: "",
  });

  const load = useCallback(async () => {
    try {
      const board = await api.get("/deals/board");
      setColumns(board.columns);
      setCurrency(board.currency);
    } catch (e) {
      toastError(e);
    }
  }, [toastError]);

  useEffect(() => {
    void load();
    api.get("/clients?per_page=200").then((d) => setClients(d.items)).catch(() => undefined);
    api.get("/people").then((d) => setPeople(d.items)).catch(() => undefined);
  }, [load]);

  if (!columns) return <ScreenLoading />;

  const total = columns.reduce((sum, c) => sum + c.deals.length, 0);

  const drop = async (stage: string) => {
    const id = dragId;
    setDragId(null);
    setOverStage(null);
    if (!id) return;
    const from = columns.find((c) => c.deals.some((d) => d.id === id));
    if (from?.key === stage) return;

    // Двигаем карточку сразу, не дожидаясь сервера: перетаскивание, которое
    // «думает» полсекунды, ощущается сломанным. При ошибке вернём как было.
    const before = columns;
    setColumns((prev) =>
      (prev ?? []).map((c) => {
        if (c.key === from?.key) return { ...c, deals: c.deals.filter((d) => d.id !== id) };
        if (c.key === stage) {
          const moved = from?.deals.find((d) => d.id === id);
          return moved ? { ...c, deals: [...c.deals, { ...moved, stage }] } : c;
        }
        return c;
      }),
    );
    try {
      await api.post(`/deals/${id}/move`, { stage });
      void load();
    } catch (e) {
      setColumns(before);
      toastError(e);
    }
  };

  const create = async () => {
    if (!draft.title.trim() || !draft.client_id) return;
    try {
      const deal = await api.post("/deals", {
        title: draft.title.trim(),
        client_id: Number(draft.client_id),
        manager_id: draft.manager_id ? Number(draft.manager_id) : null,
        due_at: draft.due_at ? `${draft.due_at}T12:00:00` : null,
        description: draft.description.trim(),
      });
      setCreating(false);
      setDraft({ title: "", client_id: "", manager_id: "", due_at: "", description: "" });
      navigate(`/deals/${deal.id}`);
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="page page-wide">
      <div className="page-head">
        <div>
          <h1 className="page-title">{term(workspace.deal_term, locale, "many")}</h1>
          <div className="page-sub">{t("dealsSub", { total })}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          <Icon name="plus" stroke={2} />
          {term(workspace.deal_term, locale, "new")}
        </button>
      </div>

      {total === 0 ? (
        <EmptyState
          title={term(workspace.deal_term, locale, "none")}
          sub={term(workspace.deal_term, locale, "noneHint")}
        />
      ) : (
        <div className="kanban">
          {columns.map((column) => (
            <div
              key={column.key}
              className={
                "kanban-col kanban-" + column.kind + (overStage === column.key ? " over" : "")
              }
              onDragOver={(e) => {
                e.preventDefault();
                setOverStage(column.key);
              }}
              onDragLeave={() => setOverStage((s) => (s === column.key ? null : s))}
              onDrop={() => void drop(column.key)}
            >
              <div className="kanban-head">
                {/* Название пришло из воронки этого бизнеса — показываем как есть */}
                <span style={column.color ? { color: column.color } : undefined}>
                  {column.name}
                </span>
                <span className="kanban-count">{column.deals.length}</span>
              </div>
              {/* Сумма по колонке: малый бизнес смотрит на деньги, а не на
                  количество карточек. Ноль не показываем — пустая строка
                  честнее нуля, которого никто не называл. */}
              {column.amount_total > 0 && (
                <div className="kanban-money">
                  {formatMoney(column.amount_total, currency, locale)}
                </div>
              )}
              <div className="kanban-body">
                {column.deals.length === 0 && (
                  <div className="kanban-empty">{t("dragHere")}</div>
                )}
                {column.deals.map((deal) => {
                  const overdue =
                    deal.due_at && column.kind === "open" && new Date(deal.due_at) < new Date();
                  return (
                    <button
                      key={deal.id}
                      className={"deal-card" + (dragId === deal.id ? " dragging" : "")}
                      draggable
                      onDragStart={() => setDragId(deal.id)}
                      onDragEnd={() => setDragId(null)}
                      onClick={() => navigate(`/deals/${deal.id}`)}
                    >
                      <span className="deal-title">{deal.title}</span>
                      {deal.amount !== null && (
                        <span className={"deal-money" + (deal.is_paid ? " paid" : "")}>
                          {formatMoney(deal.amount, currency, locale)}
                          {deal.prepaid > 0 && !deal.is_paid && (
                            <span className="deal-owed">
                              {" · "}
                              {formatMoney(deal.remainder, currency, locale)}
                            </span>
                          )}
                        </span>
                      )}
                      {deal.client_name && <span className="deal-client">{deal.client_name}</span>}
                      <span className="deal-foot">
                        {/* Кто ведёт — первое, что спрашивают у доски. Без
                            ответственного показываем это явно, а не пустотой:
                            ничейная сделка и есть проблема. */}
                        {deal.manager_id ? (
                          <span className="deal-who" title={deal.manager_name ?? ""}>
                            <Avatar
                              small
                              text={initials(deal.manager_name ?? "?")}
                              src={deal.manager_avatar ?? undefined}
                            />
                            {deal.manager_name}
                          </span>
                        ) : (
                          <span className="deal-nobody">{t("nobody")}</span>
                        )}
                        {deal.due_at && (
                          <span className={"deal-due" + (overdue ? " overdue" : "")}>
                            <Icon name="clock" size={11} />
                            {formatDate(deal.due_at, locale)}
                          </span>
                        )}
                      </span>
                      {deal.lost_reason && <span className="deal-lost">{deal.lost_reason}</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {creating && (
        <Modal title={t("newDeal")} onClose={() => setCreating(false)}>
          <div className="field">
            <label className="label">{t("dealTitle")}</label>
            <input
              className="input"
              autoFocus
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            />
          </div>
          <div className="deal-fields">
            <div className="field">
              <label className="label">{t("client")}</label>
              <select
                className="input"
                value={draft.client_id}
                onChange={(e) => setDraft({ ...draft, client_id: e.target.value })}
              >
                <option value="">—</option>
                {clients.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              {clients.length === 0 && <div className="field-desc">{t("noClientsForDeal")}</div>}
            </div>
            {/* Ответственный и срок — прямо при заведении. Проставлять их потом
                по одной сделке никто не будет, и доска зарастает ничейными. */}
            <div className="field">
              <label className="label">{t("responsible")}</label>
              <select
                className="input"
                value={draft.manager_id}
                onChange={(e) => setDraft({ ...draft, manager_id: e.target.value })}
              >
                <option value="">{t("nobody")}</option>
                {people.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label className="label">{t("dueDate")}</label>
              <input
                className="input"
                type="date"
                value={draft.due_at}
                onChange={(e) => setDraft({ ...draft, due_at: e.target.value })}
              />
            </div>
          </div>
          <div className="field">
            <label className="label">{t("dealDetails")}</label>
            <textarea
              className="input"
              rows={3}
              placeholder={t("dealDetailsHint")}
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            />
          </div>
          <button
            className="btn btn-primary"
            style={{ width: "100%" }}
            disabled={!draft.title.trim() || !draft.client_id}
            onClick={() => void create()}
          >
            {t("create")}
          </button>
        </Modal>
      )}
    </div>
  );
}
