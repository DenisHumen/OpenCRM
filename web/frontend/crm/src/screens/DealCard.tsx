import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { statusLabel, statusVariant } from "../lib/documents";
import { formatDate, formatDateTime } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { NewDocumentModal } from "./Documents";

type Stage = { key: string; name: string; kind: "open" | "won" | "lost" };

/** Дата в поле ввода — «ГГГГ-ММ-ДД», сервер отдаёт ISO с временем. */
const asDateInput = (iso: string | null) => (iso ? iso.slice(0, 10) : "");

export function DealCard() {
  const { id } = useParams();
  const { t, locale, modules, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [deal, setDeal] = useState<any>(null);
  const [stages, setStages] = useState<Stage[]>([]);
  const [people, setPeople] = useState<any[]>([]);
  const [clients, setClients] = useState<any[]>([]);
  const [docs, setDocs] = useState<any[]>([]);
  const [issuing, setIssuing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [askReason, setAskReason] = useState<string | null>(null);
  const [reason, setReason] = useState("");

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

  useEffect(() => {
    void load();
    loadDocs();
    api.get("/pipeline/stages").then((d) => setStages(d.items)).catch(() => undefined);
    api.get("/people").then((d) => setPeople(d.items)).catch(() => undefined);
    api.get("/clients?per_page=200").then((d) => setClients(d.items)).catch(() => undefined);
  }, [load, loadDocs]);

  if (!deal) return <ScreenLoading />;

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
        {t("deals")}
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
