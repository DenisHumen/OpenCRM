import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { SourcePicker } from "../components/SourcePicker";
import { Avatar, Chip, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { initials, relativeDay } from "../lib/format";

export function Clients() {
  const { t, locale, toastError } = useApp();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [data, setData] = useState<any>(null);
  const [showNew, setShowNew] = useState(params.get("new") === "1");
  const [attempt, setAttempt] = useState(0);

  const { failure, fail, clear } = useFailure();

  const search = useDebounced(query);

  useEffect(() => {
    // Набирают быстрее, чем отвечает сервер: без этого счётчика ответ на
    // «Ив» ложился поверх ответа на «Иванов», и человек видел выдачу
    // позапрошлого запроса. Приём тот же, что в отчётах и палитре команд.
    let current = true;
    clear();
    api
      .get(`/clients?search=${encodeURIComponent(search)}&per_page=100`)
      .then((found) => {
        if (current) setData(found);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [search, attempt, fail, clear]);

  if (!data) {
    return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("clients")}</h1>
          <div className="page-sub">{t("clientsSub", { total: data.total })}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setShowNew(true)}>
          <Icon name="plus" stroke={2} />
          {t("newClient")}
        </button>
      </div>

      <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
        <div
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "0 12px",
            height: 36,
            border: "1px solid var(--border)",
            borderRadius: 8,
            background: "var(--surface)",
          }}
        >
          <Icon name="search" size={15} className="" />
          <input
            style={{ flex: 1, background: "none", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5, fontFamily: "var(--sans)" }}
            placeholder={t("searchClients")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
        </div>
      </div>

      <div className="list-card">
        <div className="list-header">
          <span style={{ width: 30 }} />
          <span style={{ flex: 1 }}>{t("client")}</span>
          <span style={{ width: 190 }}>{t("contact")}</span>
          <span style={{ width: 170 }}>{t("tags")}</span>
          <span style={{ width: 90, textAlign: "right" }}>{t("activity")}</span>
        </div>
        {data.items.map((client: any) => (
          <Link to={`/clients/${client.id}`} key={client.id} className="list-row hoverable">
            <Avatar text={initials(client.name)} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{client.name}</div>
              <div style={{ color: "var(--faint)", fontSize: 12 }}>{client.company}</div>
            </div>
            <div style={{ width: 190, flexShrink: 0 }}>
              <div style={{ color: "var(--muted)", fontSize: 12.5 }}>{client.phone}</div>
              <div style={{ color: "var(--faint)", fontSize: 12 }}>{client.email}</div>
            </div>
            <div style={{ width: 170, display: "flex", gap: 6, flexShrink: 0, flexWrap: "wrap" }}>
              {client.tags.slice(0, 3).map((tag: string) => (
                <Chip key={tag}>{tag}</Chip>
              ))}
            </div>
            <div style={{ width: 90, textAlign: "right", color: "var(--faint)", fontSize: 12, flexShrink: 0 }}>
              {relativeDay(client.updated_at, locale)}
            </div>
          </Link>
        ))}
        {data.items.length === 0 && (
          <EmptyState
            title={query ? t("nothingFound", { q: query }) : t("noClientsYet")}
            sub={query ? t("tryDifferent") : undefined}
          />
        )}
      </div>

      {showNew && (
        <NewClientModal
          onClose={() => {
            setShowNew(false);
            params.delete("new");
            setParams(params, { replace: true });
          }}
          onCreated={(client) => navigate(`/clients/${client.id}`)}
        />
      )}
    </div>
  );
}

function NewClientModal({ onClose, onCreated }: { onClose: () => void; onCreated: (c: any) => void }) {
  const { t, toastError } = useApp();
  const [form, setForm] = useState({
    name: "", company: "", phone: "", email: "", messenger: "", tags: "", source: "",
  });
  const [busy, setBusy] = useState(false);

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const client = await api.post("/clients", form);
      onCreated(client);
    } catch (err) {
      toastError(err);
      setBusy(false);
    }
  };

  return (
    <Modal title={t("newClient")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("name")}</label>
          <input className="input" value={form.name} onChange={set("name")} autoFocus required />
        </div>
        <div className="field">
          <label className="label">{t("company")}</label>
          <input className="input" value={form.company} onChange={set("company")} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <div className="field">
            <label className="label">{t("phone")}</label>
            <input className="input" value={form.phone} onChange={set("phone")} />
          </div>
          <div className="field">
            <label className="label">{t("email")}</label>
            <input className="input" value={form.email} onChange={set("email")} />
          </div>
        </div>
        <div className="field">
          <label className="label">{t("telegram")}</label>
          <input className="input" value={form.messenger} onChange={set("messenger")} placeholder="@username" />
        </div>
        <div className="field">
          <label className="label">{t("tagsCommaHint")}</label>
          <input className="input" value={form.tags} onChange={set("tags")} placeholder="branding, web" />
        </div>
        {/* Источник спрашивают один раз — при заведении карточки. Через неделю
            «откуда он пришёл» уже никто не вспомнит, и отчёт по рекламе
            превращается в столбик «не указан». */}
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("clientSource")}</label>
          <SourcePicker value={form.source} onChange={(next) => setForm((f) => ({ ...f, source: next }))} />
          <div className="field-desc">{t("clientSourceHint")}</div>
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
