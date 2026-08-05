import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDateTime } from "../lib/format";

export interface MailMessage {
  id: number;
  account_id: number;
  direction: "in" | "out";
  subject: string;
  from_addr: string;
  to_addrs: string[];
  sent_at: string | null;
  has_attachments: boolean;
  client_id: number | null;
  is_read: boolean;
  body_text?: string;
  body_html?: string;
}

export interface MailAccount {
  id: number;
  title: string;
  address: string;
  is_active: boolean;
  has_password: boolean;
  last_sync_at: string | null;
  last_error: string | null;
}

type Filter = "all" | "in" | "out" | "unread";

export function Mail() {
  const { t, locale, toastError } = useApp();
  const [messages, setMessages] = useState<MailMessage[] | null>(null);
  const [total, setTotal] = useState(0);
  const [accounts, setAccounts] = useState<MailAccount[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState<MailMessage | null>(null);
  const [composing, setComposing] = useState(false);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ per_page: "100" });
    if (filter === "in" || filter === "out") params.set("direction", filter);
    if (filter === "unread") params.set("unread", "true");
    if (search.trim()) params.set("search", search.trim());
    try {
      const data = await api.get(`/mail/messages?${params}`);
      setMessages(data.items);
      setTotal(data.total);
    } catch (e) {
      toastError(e);
      setMessages([]);
    }
  }, [filter, search, toastError]);

  useEffect(() => {
    void load();
  }, [load]);

  // Список ящиков нужен форме отправки: с какого адреса уходит письмо.
  // Менеджеру он недоступен (это настройка root), поэтому молча пропускаем.
  useEffect(() => {
    api
      .get("/mail/accounts")
      .then((data) => setAccounts(data.items))
      .catch(() => setAccounts([]));
  }, []);

  const openMessage = async (message: MailMessage) => {
    try {
      const full = await api.get<MailMessage>(`/mail/messages/${message.id}`);
      setOpen(full);
      if (!message.is_read) {
        await api.post(`/mail/messages/${message.id}/read`, { is_read: true });
        setMessages((prev) =>
          (prev ?? []).map((m) => (m.id === message.id ? { ...m, is_read: true } : m)),
        );
      }
    } catch (e) {
      toastError(e);
    }
  };

  if (!messages) return <ScreenLoading />;

  const filters: { id: Filter; label: string }[] = [
    { id: "all", label: t("all") },
    { id: "in", label: t("incoming") },
    { id: "out", label: t("outgoing") },
    { id: "unread", label: t("unreadOnly") },
  ];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("mail")}</h1>
          <div className="page-sub">{t("mailSub", { total })}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setComposing(true)}>
          <Icon name="send" stroke={2} />
          {t("compose")}
        </button>
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", margin: "18px 0" }}>
        <input
          className="input"
          style={{ flex: 1 }}
          placeholder={t("searchMail")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div style={{ display: "flex", gap: 6 }}>
          {filters.map((item) => (
            <button
              key={item.id}
              className={"filter-chip" + (filter === item.id ? " active" : "")}
              onClick={() => setFilter(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="list-card">
        {messages.map((message) => (
          <div
            key={message.id}
            className="list-row hoverable"
            style={{ height: 56, cursor: "pointer" }}
            onClick={() => void openMessage(message)}
          >
            <div className="feed-icon">
              <Icon name={message.direction === "in" ? "inbox" : "send"} size={14} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                className="truncate"
                style={{
                  color: "var(--text)",
                  fontSize: 13.5,
                  fontWeight: message.is_read ? 500 : 600,
                }}
              >
                {message.subject || t("noSubject")}
              </div>
              <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>
                {message.direction === "in" ? message.from_addr : message.to_addrs.join(", ")}
              </div>
            </div>
            {message.has_attachments && <Icon name="folder" size={13} />}
            {message.client_id ? (
              <Link
                to={`/clients/${message.client_id}`}
                className="text-link"
                style={{ fontSize: 11.5 }}
                onClick={(e) => e.stopPropagation()}
              >
                {t("openClient")}
              </Link>
            ) : (
              <span style={{ color: "var(--faint)", fontSize: 11.5 }}>{t("unlinkedLetter")}</span>
            )}
            <div style={{ color: "var(--faint)", fontSize: 12, whiteSpace: "nowrap" }}>
              {formatDateTime(message.sent_at, locale)}
            </div>
          </div>
        ))}
        {messages.length === 0 && <EmptyState title={t("mailEmpty")} />}
      </div>

      {open && (
        <Modal title={open.subject || t("noSubject")} onClose={() => setOpen(null)} wide>
          <div style={{ color: "var(--faint)", fontSize: 12, marginBottom: 14 }}>
            {open.from_addr} → {open.to_addrs.join(", ")} · {formatDateTime(open.sent_at, locale)}
          </div>
          {/* html-тело письма приходит из внешнего мира и здесь не рендерится:
              вставить чужую разметку в интерфейс CRM — это XSS. Показываем текст;
              html лежит в базе и ждёт нормального санитайзера. */}
          <div style={{ whiteSpace: "pre-wrap", fontSize: 13.5, lineHeight: 1.6 }}>
            {open.body_text || "—"}
          </div>
        </Modal>
      )}

      {composing && (
        <MailCompose
          accounts={accounts}
          onClose={() => setComposing(false)}
          onSent={() => void load()}
        />
      )}
    </div>
  );
}

/** Форма отправки. Живёт отдельно: её открывают и из карточки клиента. */
export function MailCompose({
  accounts,
  to = "",
  clientId,
  dealId,
  onClose,
  onSent,
}: {
  accounts: MailAccount[];
  to?: string;
  clientId?: number;
  dealId?: number;
  onClose: () => void;
  onSent?: () => void;
}) {
  const { t, toast, toastError } = useApp();
  const [form, setForm] = useState({ to, subject: "", body: "" });
  const [accountId, setAccountId] = useState<number | "">(accounts[0]?.id ?? "");
  const [busy, setBusy] = useState(false);

  const send = async () => {
    setBusy(true);
    try {
      await api.post("/mail/send", {
        to: form.to.split(",").map((a) => a.trim()).filter(Boolean),
        subject: form.subject,
        body: form.body,
        account_id: accountId || null,
        client_id: clientId ?? null,
        deal_id: dealId ?? null,
      });
      toast(t("letterSent"));
      onSent?.();
      onClose();
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={t("compose")} onClose={onClose} wide>
      {accounts.length > 1 && (
        <div className="field">
          <label className="label">{t("fromMailbox")}</label>
          <select
            className="input"
            value={accountId}
            onChange={(e) => setAccountId(Number(e.target.value))}
          >
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.title || account.address}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="field">
        <label className="label">{t("toField")}</label>
        <input
          className="input"
          value={form.to}
          onChange={(e) => setForm({ ...form, to: e.target.value })}
          placeholder="client@example.com"
        />
      </div>
      <div className="field">
        <label className="label">{t("subjectField")}</label>
        <input
          className="input"
          value={form.subject}
          onChange={(e) => setForm({ ...form, subject: e.target.value })}
        />
      </div>
      <div className="field">
        <label className="label">{t("bodyField")}</label>
        <textarea
          className="textarea"
          rows={8}
          value={form.body}
          onChange={(e) => setForm({ ...form, body: e.target.value })}
        />
      </div>
      <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 16 }}>
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void send()}>
          {t("sendLetter")}
        </button>
      </div>
    </Modal>
  );
}
