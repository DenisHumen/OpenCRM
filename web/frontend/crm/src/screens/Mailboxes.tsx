import { useCallback, useEffect, useState, type ReactNode } from "react";

import { Icon } from "../components/Icon";
import { ConfirmModal, EmptyState, Modal, ScreenLoading, Toggle } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDateTime } from "../lib/format";
import { moduleOn } from "../lib/modules";

/**
 * Почтовые ящики фирмы (root).
 *
 * Сам блок включается в «Настройки → Модули», как и все остальные; здесь только
 * его настройка. Два переключателя одного и того же на разных экранах — верный
 * способ развести состояние в голове у человека с состоянием в базе.
 */

interface Account {
  id: number;
  title: string;
  address: string;
  imap_host: string;
  imap_port: number;
  imap_ssl: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_ssl: boolean;
  login: string;
  has_password: boolean;
  is_active: boolean;
  last_sync_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
}

type Draft = Partial<Account> & { password?: string };

const BLANK: Draft = {
  title: "",
  address: "",
  imap_host: "",
  imap_port: 993,
  imap_ssl: true,
  smtp_host: "",
  smtp_port: 465,
  smtp_ssl: true,
  login: "",
  password: "",
  is_active: true,
};

export function Mailboxes() {
  const { t, locale, modules, toast, toastError } = useApp();
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Account | null>(null);
  const guard = useGuard();

  // Экран открыт и при выключенном блоке: в него приходят из настроек модулей,
  // чтобы понять, что тут вообще настраивается. API при этом закрыт, поэтому
  // вместо пустого списка показываем, где включается сам блок.
  const enabled = moduleOn(modules, "mail");

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setAccounts((await api.get("/mail/accounts")).items);
    } catch (e) {
      // Пустой список тут врал: «ящиков нет» вместо «не спросили». Выключенный
      // блок — случай ниже, он в отказ не превращается: там список пуст по делу.
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    if (enabled) void load();
    else setAccounts([]);
  }, [enabled, load]);

  const save = async () => {
    // Засов, а не флаг состояния: пока сервер проверяет подключение (а он
    // ходит к IMAP), кнопка выглядит неотвеченной, и второе нажатие заводило
    // второй ящик с тем же адресом. Дальше почта тянулась дважды.
    if (!draft || !guard.take()) return;
    try {
      if (draft.id) await api.patch(`/mail/accounts/${draft.id}`, draft);
      else await api.post("/mail/accounts", draft);
      setDraft(null);
      await load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const check = async (account: Account) => {
    try {
      const result = await api.post(`/mail/accounts/${account.id}/check`);
      toast(result.ok ? t("connectionOk") : result.error, !result.ok);
      await load();
    } catch (e) {
      toastError(e);
    }
  };

  const sync = async (account: Account) => {
    try {
      toast(t("syncResult", await api.post(`/mail/accounts/${account.id}/sync`)));
      await load();
    } catch (e) {
      toastError(e);
    }
  };

  if (!accounts) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div>
          <h1 className="page-title">{t("mailboxes")}</h1>
          <div className="page-sub">{t("mailboxesSub")}</div>
        </div>
        {enabled && (
          <button className="btn btn-primary" onClick={() => setDraft({ ...BLANK })}>
            <Icon name="plus" stroke={2} />
            {t("addMailbox")}
          </button>
        )}
      </div>

      {!enabled ? (
        <div className="card card-pad">
          <div className="field-desc" style={{ marginTop: 0 }}>{t("mailModuleOffHint")}</div>
        </div>
      ) : (
        <div className="list-card">
          {accounts.map((account) => (
            <div key={account.id} className="list-row" style={{ height: "auto", padding: "14px 16px" }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                  {account.title || account.address}
                  {!account.is_active && (
                    <span style={{ color: "var(--faint)", fontWeight: 400 }}> · {t("off")}</span>
                  )}
                </div>
                <div style={{ color: "var(--faint)", fontSize: 12, marginTop: 2 }}>
                  {account.address} · {account.imap_host || "—"} / {account.smtp_host || "—"} ·{" "}
                  {account.has_password ? t("mailPasswordSet") : t("mailPasswordNotSet")}
                </div>
                <div style={{ color: "var(--faint)", fontSize: 11.5, marginTop: 2 }}>
                  {t("lastSync")}:{" "}
                  {account.last_sync_at
                    ? formatDateTime(account.last_sync_at, locale)
                    : t("neverSynced")}
                </div>
                {/* Последняя ошибка ящика — здесь, а не только в логе: разбираться
                    с «почта не приходит» будет root, а не тот, кто читает journalctl. */}
                {account.last_error && (
                  <div style={{ color: "var(--danger)", fontSize: 11.5, marginTop: 4 }}>
                    {account.last_error}
                  </div>
                )}
              </div>
              <button className="btn btn-secondary btn-sm" onClick={() => void check(account)}>
                {t("checkConnection")}
              </button>
              <button className="btn btn-secondary btn-sm" onClick={() => void sync(account)}>
                <Icon name="refresh" size={13} />
                {t("syncNow")}
              </button>
              <button
                className="btn-icon"
                aria-label={t("edit")}
                onClick={() => setDraft({ ...account, password: "" })}
              >
                <Icon name="note" size={14} />
              </button>
              <button
                className="btn-icon"
                aria-label={t("delete")}
                onClick={() => setConfirmDelete(account)}
              >
                <Icon name="trash" size={14} />
              </button>
            </div>
          ))}
          {accounts.length === 0 && <EmptyState icon="email" title={t("noMailboxesYet")} />}
        </div>
      )}

      {draft && (
        <Modal title={draft.id ? draft.address : t("addMailbox")} onClose={() => setDraft(null)} wide>
          <Field label={t("mailTitleField")}>
            <input
              className="input"
              value={draft.title ?? ""}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            />
          </Field>
          <Field label={t("mailAddress")}>
            <input
              className="input"
              value={draft.address ?? ""}
              onChange={(e) => setDraft({ ...draft, address: e.target.value })}
              placeholder="office@studio.com"
            />
          </Field>
          <Field label={t("mailLogin")} desc={t("mailLoginHint")}>
            <input
              className="input"
              value={draft.login ?? ""}
              onChange={(e) => setDraft({ ...draft, login: e.target.value })}
            />
          </Field>
          <Field
            label={t("mailPassword")}
            desc={t("mailPasswordHint")}
            hint={
              draft.id
                ? draft.has_password
                  ? t("mailPasswordSet")
                  : t("mailPasswordNotSet")
                : undefined
            }
          >
            {/* Поле всегда пустое, и это не недоделка: текущий пароль сюда не
                приходит и прийти не может — сервер его не отдаёт никому. */}
            <input
              className="input"
              type="password"
              autoComplete="new-password"
              value={draft.password ?? ""}
              onChange={(e) => setDraft({ ...draft, password: e.target.value })}
            />
          </Field>
          <ServerRow
            label={t("imapServer")}
            host={draft.imap_host ?? ""}
            port={draft.imap_port ?? 993}
            ssl={draft.imap_ssl ?? true}
            portLabel={t("port")}
            sslLabel={t("useSsl")}
            onChange={(host, port, ssl) =>
              setDraft({ ...draft, imap_host: host, imap_port: port, imap_ssl: ssl })
            }
          />
          <ServerRow
            label={t("smtpServer")}
            host={draft.smtp_host ?? ""}
            port={draft.smtp_port ?? 465}
            ssl={draft.smtp_ssl ?? true}
            portLabel={t("port")}
            sslLabel={t("useSsl")}
            onChange={(host, port, ssl) =>
              setDraft({ ...draft, smtp_host: host, smtp_port: port, smtp_ssl: ssl })
            }
          />
          {/* Действие у переключателя своё, а не только у обёртки. Пустой
              `onToggle` выглядел безобидно — рядом же стоит обработчик на всей
              строке, — но `Toggle` останавливает всплытие (иначе одно нажатие
              срабатывало бы дважды). То есть нажатие ПО САМОМУ переключателю не
              делало ничего, и с клавиатуры ящик было не включить вовсе: Tab
              приводит фокус именно на кнопку, а пробел на ней — тот же клик. */}
          <div
            style={{ display: "flex", alignItems: "center", gap: 11, cursor: "pointer", marginTop: 4 }}
            onClick={() => setDraft({ ...draft, is_active: !(draft.is_active ?? true) })}
          >
            <Toggle
              on={draft.is_active ?? true}
              label={t("mailboxActive")}
              onToggle={() => setDraft({ ...draft, is_active: !(draft.is_active ?? true) })}
            />
            <div style={{ fontSize: 13 }}>{t("mailboxActive")}</div>
          </div>
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 20 }}>
            <button className="btn btn-secondary btn-sm" onClick={() => setDraft(null)}>
              {t("cancel")}
            </button>
            <button className="btn btn-primary btn-sm" disabled={guard.busy} onClick={() => void save()}>
              {t("save")}
            </button>
          </div>
        </Modal>
      )}

      {confirmDelete && (
        <ConfirmModal
          text={t("deleteMailboxConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/mail/accounts/${confirmDelete.id}`);
              await load();
            } catch (e) {
              toastError(e);
            }
          }}
          onClose={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}

function Field({
  label,
  desc,
  hint,
  children,
}: {
  label: string;
  desc?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label className="label">
        {label}
        {hint && <span className="label-hint"> · {hint}</span>}
      </label>
      {children}
      {desc && <div className="field-desc">{desc}</div>}
    </div>
  );
}

function ServerRow({
  label,
  host,
  port,
  ssl,
  portLabel,
  sslLabel,
  onChange,
}: {
  label: string;
  host: string;
  port: number;
  ssl: boolean;
  portLabel: string;
  sslLabel: string;
  onChange: (host: string, port: number, ssl: boolean) => void;
}) {
  return (
    <div className="field">
      <label className="label">{label}</label>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <input
          className="input"
          style={{ flex: 1 }}
          value={host}
          onChange={(e) => onChange(e.target.value, port, ssl)}
          placeholder="imap.example.com"
        />
        <input
          className="input"
          style={{ width: 90 }}
          type="number"
          value={port}
          aria-label={portLabel}
          onChange={(e) => onChange(host, Number(e.target.value) || 0, ssl)}
        />
        <div
          style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}
          onClick={() => onChange(host, port, !ssl)}
        >
          {/* Своё действие, а не только у обёртки: см. переключатель активности
              ящика выше. Здесь цена ошибки больше — с клавиатуры нельзя было
              СНЯТЬ шифрование там, где сервер его не держит, и ящик молча не
              подключался. Имя переключателя — вместе с сервером: их на форме
              два, и «Использовать SSL» без «IMAP» ни о чём не говорит. */}
          <Toggle
            on={ssl}
            label={`${label} · ${sslLabel}`}
            onToggle={() => onChange(host, port, !ssl)}
          />
          <span style={{ fontSize: 12.5 }}>{sslLabel}</span>
        </div>
      </div>
    </div>
  );
}
