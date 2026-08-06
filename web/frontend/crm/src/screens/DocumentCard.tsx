import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, ScreenLoading } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { copyText } from "../lib/clipboard";
import { isFinished, nextStatuses, statusLabel, statusVariant } from "../lib/documents";
import { formatDateTime } from "../lib/format";

/** Языки печати. Бумагу печатают под клиента, а не под сотрудника: приехал
 *  турист — печатаем по-английски, ничего в базе не меняя. */
const PRINT_LANGS = [
  { id: "ru", label: "Рус" },
  { id: "en", label: "Eng" },
  { id: "uk", label: "Укр" },
];

const ROWS = [
  ["item", "docItem"],
  ["serial", "docSerial"],
  ["condition", "docCondition"],
  ["accessories", "docAccessories"],
  ["problem", "docProblem"],
  ["estimate", "docEstimate"],
  ["terms", "docTerms"],
] as const;

export function DocumentCard() {
  const { id } = useParams();
  const { t, locale, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [doc, setDoc] = useState<any>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setDoc(await api.get(`/documents/${id}`));
    } catch (e) {
      // Записи нет или она не наша: показывать «попробуйте ещё раз» тут не о
      // чем — повтор вернёт тот же ответ. Возвращаемся в список, как и раньше.
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/documents");
        return;
      }
      // Всё остальное — беда связи или сервера. Карточку не бросаем: адрес в
      // строке верный, и повторить имеет смысл именно его, а не список.
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!doc) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const fields = doc.payload?.fields ?? {};
  const client = doc.payload?.client ?? {};
  const publicUrl = `${window.location.origin}/d/${doc.number}`;
  const finished = isFinished(doc.status);

  const move = async (status: string) => {
    try {
      // Смена состояния возвращает бланк без истории — перечитываем целиком,
      // иначе журнал внизу застынет на том, что было при открытии.
      await api.post(`/documents/${id}/status`, { status });
      await load();
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="page page-narrow">
      <Link to="/documents" className="back-link">
        <Icon name="arrowLeft" size={14} />
        {t("documents")}
      </Link>

      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 className="page-title doc-number-title">{doc.number}</h1>
          <div className="page-sub" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span>{fields.item || "—"}</span>
            <Chip variant={statusVariant(doc.status)}>{statusLabel(t, doc.status)}</Chip>
          </div>
        </div>
        {/* Печать — то, ради чего сюда заходят чаще всего: бумагу теряют, мнут
            и заливают кофе, поэтому кнопка на виду, а не в меню. */}
        <div className="print-actions">
          <span className="print-label">
            <Icon name="printer" size={14} />
            {t("docPrint")}
          </span>
          {PRINT_LANGS.map((lang) => (
            <a
              key={lang.id}
              className={"btn btn-secondary btn-sm" + (lang.id === doc.locale ? " btn-current" : "")}
              href={`/api/v1/documents/${doc.id}/print?locale=${lang.id}`}
              target="_blank"
              rel="noreferrer"
            >
              {lang.label}
            </a>
          ))}
        </div>
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="metric-title" style={{ marginBottom: 12 }}>{t("docWhatNext")}</div>
        {finished ? (
          <div className="field-desc" style={{ marginTop: 0 }}>{t("docFinished")}</div>
        ) : (
          <div className="stage-actions">
            {nextStatuses(doc.status).map((status) =>
              status === "cancelled" ? (
                <button key={status} className="stage-btn stage-lost" onClick={() => setConfirmCancel(true)}>
                  {statusLabel(t, status)}
                </button>
              ) : (
                <button
                  key={status}
                  className={"stage-btn" + (status === "closed" ? " stage-won" : "")}
                  onClick={() => void move(status)}
                >
                  {status === "closed" && <Icon name="check" size={13} stroke={2} />}
                  {statusLabel(t, status)}
                </button>
              ),
            )}
          </div>
        )}
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <table className="doc-table">
          <tbody>
            <tr>
              <th>{t("client")}</th>
              <td>
                {/* Клиента могли удалить — тогда остаётся имя со снимка, без ссылки. */}
                {doc.client_id ? (
                  <Link to={`/clients/${doc.client_id}`} className="text-link">
                    {client.name || "—"}
                  </Link>
                ) : (
                  client.name || "—"
                )}
                {client.phone && <span style={{ color: "var(--faint)" }}> · {client.phone}</span>}
              </td>
            </tr>
            {doc.deal_id && (
              <tr>
                <th>{t("deal")}</th>
                <td>
                  <Link to={`/deals/${doc.deal_id}`} className="text-link">
                    {doc.payload?.deal?.title || `#${doc.deal_id}`}
                  </Link>
                </td>
              </tr>
            )}
            {ROWS.map(([key, label]) => (
              <tr key={key}>
                <th>{t(label)}</th>
                <td>{fields[key] || <span style={{ color: "var(--faint)" }}>—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {/* Поля не редактируются намеренно: у клиента на руках половина этой же
            бумаги. Правка задним числом означала бы, что база и бумага
            разошлись, а спор «что вы у меня приняли» решать станет нечем. */}
        <div className="field-desc" style={{ marginTop: 12 }}>
          {t("docSnapshotHint", { date: formatDateTime(doc.created_at, locale) })}
        </div>
      </div>

      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="metric-title" style={{ marginBottom: 10 }}>{t("docClientLink")}</div>
        <div className="share-action-row">
          <input className="input" readOnly value={publicUrl} onFocus={(e) => e.target.select()} />
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => {
              // Не получилось скопировать — показываем адрес, а не молчим.
              void copyText(publicUrl).then((ok) => toast(ok ? t("copied") : publicUrl));
            }}
          >
            <Icon name="copy" size={13} />
            {t("copyLink")}
          </button>
        </div>
        <div className="field-desc">{t("docClientLinkHint")}</div>
      </div>

      <div className="card card-pad">
        <div className="metric-title" style={{ marginBottom: 12 }}>{t("history")}</div>
        <ol className="stage-log">
          {(doc.events ?? []).map((event: any) => (
            <li key={event.id}>
              <span className="stage-log-when">{formatDateTime(event.created_at, locale)}</span>
              <span className="stage-log-what">
                {event.from_status
                  ? `${statusLabel(t, event.from_status)} → ${statusLabel(t, event.to_status)}`
                  : statusLabel(t, event.to_status)}
                {event.note && <span style={{ color: "var(--faint)" }}> · {event.note}</span>}
              </span>
              <span className="stage-log-who">{event.author_name || "—"}</span>
            </li>
          ))}
        </ol>
      </div>

      {confirmCancel && (
        <ConfirmModal
          text={t("docCancelConfirm")}
          confirmLabel={t("confirm")}
          danger
          onConfirm={() => void move("cancelled")}
          onClose={() => setConfirmCancel(false)}
        />
      )}
    </div>
  );
}
