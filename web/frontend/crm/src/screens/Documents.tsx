import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDate } from "../lib/format";
import { DOC_STATUSES, statusLabel, statusVariant } from "../lib/documents";

export function Documents() {
  const { t, locale, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [data, setData] = useState<any>(null);
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [scan, setScan] = useState("");
  const [showNew, setShowNew] = useState(params.get("new") === "1");
  const scanInput = useRef<HTMLInputElement | null>(null);
  const focused = useRef(false);
  const timer = useRef<number>();

  const load = useCallback(
    async (q: string, only: string) => {
      const search = new URLSearchParams({ per_page: "100" });
      if (q.trim()) search.set("search", q.trim());
      if (only) search.set("status", only);
      try {
        setData(await api.get(`/documents?${search}`));
      } catch (e) {
        toastError(e);
      }
    },
    [toastError],
  );

  useEffect(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void load(query, status), 250);
    return () => window.clearTimeout(timer.current);
  }, [query, status, load]);

  if (!data) return <ScreenLoading />;

  // Сканер работает как клавиатура: набирает номер и жмёт Enter. Поле должно
  // ждать его сразу — иначе первый скан уходит в пустоту, и приёмщик решает,
  // что сканер сломан.
  //
  // Фокус ставит ref, а не эффект на монтировании: до прихода списка экран
  // отдаёт заглушку, поля ещё нет, и `ref.current` в таком эффекте пуст. Так
  // это и было — первый скан молча пропадал. Ref срабатывает ровно тогда,
  // когда поле появилось; флаг не даёт перехватывать фокус потом, иначе он
  // уводил бы курсор из поиска при каждой перезагрузке списка.
  const catchScanner = (el: HTMLInputElement | null) => {
    scanInput.current = el;
    if (el && !focused.current) {
      focused.current = true;
      el.focus();
    }
  };

  const lookup = async () => {
    const number = scan.trim();
    if (!number) return;
    setScan("");
    try {
      const doc = await api.get(`/documents/by-number/${encodeURIComponent(number)}`);
      navigate(`/documents/${doc.id}`);
    } catch {
      toast(t("docNotFound", { code: number }), true);
      scanInput.current?.focus();
    }
  };

  return (
    <div className="page page-wide">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("documents")}</h1>
          <div className="page-sub">{t("documentsSub", { total: data.total })}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setShowNew(true)}>
          <Icon name="plus" stroke={2} />
          {t("newDocument")}
        </button>
      </div>

      {/* Скан — главный способ найти бланк за стойкой, поэтому он отдельным
          блоком сверху, а не одним из полей фильтра. */}
      <div className="scan-box">
        <Icon name="scan" size={18} className="scan-icon" />
        <input
          ref={catchScanner}
          className="scan-input"
          placeholder={t("scanPlaceholder")}
          value={scan}
          onChange={(e) => setScan(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void lookup();
          }}
        />
        <span className="scan-hint">{t("scanHint")}</span>
      </div>

      <div className="doc-toolbar">
        <div className="search-field">
          <Icon name="search" size={15} className="" />
          <input
            placeholder={t("docSearchPlaceholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">{t("allStatuses")}</option>
          {DOC_STATUSES.map((s) => (
            <option key={s} value={s}>
              {statusLabel(t, s)}
            </option>
          ))}
        </select>
      </div>

      <div className="list-card">
        {data.items.map((doc: any) => (
          <Link to={`/documents/${doc.id}`} key={doc.id} className="list-row hoverable">
            <span className="doc-number">{doc.number}</span>
            <div className="list-row-text">
              <div className="truncate" style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                {doc.payload?.fields?.item || "—"}
              </div>
              <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>
                {doc.payload?.client?.name || "—"}
              </div>
            </div>
            <span style={{ width: 90, textAlign: "right", color: "var(--faint)", fontSize: 12, flexShrink: 0 }}>
              {formatDate(doc.created_at, locale)}
            </span>
            <span style={{ width: 130, flexShrink: 0, display: "flex", justifyContent: "flex-end" }}>
              <Chip variant={statusVariant(doc.status)}>{statusLabel(t, doc.status)}</Chip>
            </span>
          </Link>
        ))}
        {data.items.length === 0 && (
          <EmptyState
            title={query || status ? t("nothingFound", { q: query }) : t("noDocuments")}
            sub={query || status ? t("tryDifferent") : t("noDocumentsHint")}
          />
        )}
      </div>

      {showNew && (
        <NewDocumentModal
          onClose={() => setShowNew(false)}
          onCreated={(doc) => navigate(`/documents/${doc.id}`)}
        />
      )}
    </div>
  );
}

const FIELDS = ["serial", "condition", "accessories", "problem", "estimate", "terms"] as const;

const FIELD_LABEL = {
  item: "docItem",
  serial: "docSerial",
  condition: "docCondition",
  accessories: "docAccessories",
  problem: "docProblem",
  estimate: "docEstimate",
  terms: "docTerms",
} as const;

export function NewDocumentModal({
  dealId,
  clientId,
  onClose,
  onCreated,
}: {
  dealId?: number;
  clientId?: number;
  onClose: () => void;
  onCreated: (doc: any) => void;
}) {
  const { t, user, toastError } = useApp();
  const [clients, setClients] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({
    client_id: clientId ? String(clientId) : "",
    client_name: "",
    client_phone: "",
    item: "",
    serial: "",
    condition: "",
    accessories: "",
    problem: "",
    estimate: "",
    terms: "",
    // Бумагу печатают под клиента, поэтому язык по умолчанию — язык сотрудника,
    // но менять его можно на любой из трёх.
    locale: user?.locale === "en" ? "en" : "ru",
  });

  useEffect(() => {
    if (clientId) return;
    api.get("/clients?per_page=200").then((d) => setClients(d.items)).catch(() => undefined);
  }, [clientId]);

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  // Клиента может не быть в базе: человек стоит у стойки, и заводить карточку
  // до квитанции — лишний шаг. Сервер это принимает, значит принимает и форма.
  const walkIn = !clientId && !form.client_id;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      onCreated(
        await api.post("/documents", {
          ...form,
          client_id: form.client_id ? Number(form.client_id) : null,
          deal_id: dealId ?? null,
        }),
      );
    } catch (err) {
      toastError(err);
      setBusy(false);
    }
  };

  return (
    <Modal title={t("newDocument")} onClose={onClose} wide>
      <form onSubmit={submit}>
        <div className="deal-fields">
          {!clientId && (
            <div className="field">
              <label className="label">{t("client")}</label>
              <select className="input" value={form.client_id} onChange={set("client_id")}>
                <option value="">—</option>
                {clients.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          {walkIn && (
            <>
              <div className="field">
                <label className="label">{t("name")}</label>
                <input className="input" value={form.client_name} onChange={set("client_name")} required />
              </div>
              <div className="field">
                <label className="label">{t("phone")}</label>
                <input className="input" value={form.client_phone} onChange={set("client_phone")} />
              </div>
            </>
          )}
          <div className="field">
            <label className="label">{t("docLanguage")}</label>
            <select className="input" value={form.locale} onChange={set("locale")}>
              <option value="ru">Русский</option>
              <option value="en">English</option>
              <option value="uk">Українська</option>
            </select>
            <div className="field-desc">{t("docLanguageHint")}</div>
          </div>
        </div>

        <div className="field" style={{ marginTop: 4 }}>
          <label className="label">{t("docItem")} *</label>
          <input className="input" maxLength={160} value={form.item} onChange={set("item")} autoFocus required />
        </div>

        <div className="deal-fields">
          {FIELDS.map((key) => (
            <div className="field" key={key}>
              <label className="label">{t(FIELD_LABEL[key])}</label>
              <input className="input" maxLength={160} value={form[key]} onChange={set(key)} />
            </div>
          ))}
        </div>

        <div className="field-desc" style={{ margin: "2px 0 18px" }}>{t("docFieldLimit")}</div>

        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
