import { useCallback, useEffect, useState, type FormEvent } from "react";

import { CopyButton } from "../components/CopyButton";
import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDateTime } from "../lib/format";
import { useGuard } from "../lib/guard";
import { useReference } from "../lib/reference";
import type { TranslationKey } from "../lib/i18n";

interface Token {
  id: number;
  name: string;
  prefix: string;
  user_id: number;
  user_name: string;
  user_email: string;
  tolko_chtenie: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  state: "active" | "expired" | "revoked";
  token?: string;
}

interface Spisok {
  items: Token[];
  alive: number;
  sroki: number[];
  v_minutu: number;
}

interface Person {
  id: number;
  name: string;
}

const STATE_LABEL: Record<Token["state"], TranslationKey> = {
  active: "apiKeyStateActive",
  expired: "apiKeyStateExpired",
  revoked: "apiKeyStateRevoked",
};

const SROK_LABEL: Record<number, TranslationKey> = {
  30: "tokenSrok30",
  90: "tokenSrok90",
  180: "tokenSrok180",
  365: "tokenSrok365",
  0: "tokenSrokNikogda",
};

/** Токены сотрудников к `/api/v1` — для программ и агентов (docs/bloki/30-tokeny-i-mcp.md). */
export function TokenySotrudnikov() {
  const { t, locale, toastError } = useApp();
  const [data, setData] = useState<Spisok | null>(null);
  const { failure, fail, clear } = useFailure();
  const guard = useGuard();
  const [creating, setCreating] = useState(false);
  const [shown, setShown] = useState<Token | null>(null);
  const [revoking, setRevoking] = useState<Token | null>(null);

  const load = useCallback(() => {
    clear();
    api.get<Spisok>("/tokens").then(setData).catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);

  if (!data) return <ScreenLoading error={failure} onRetry={load} />;

  const revoke = async (tok: Token) => {
    if (!guard.take()) return;
    try {
      await api.post(`/tokens/${tok.id}/revoke`);
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="section-api">
      <div className="section-api-header">
        <div>
          <h2 className="section-api-title">{t("tokens")}</h2>
          <div className="page-sub">{t("tokensSub")}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          <Icon name="plus" size={14} />
          {t("tokenNew")}
        </button>
      </div>

      <div className="list-card">
        {data.items.map((tok) => (
          <div
            key={tok.id}
            className="list-row"
            style={{ alignItems: "flex-start", height: "auto", padding: "12px 16px", opacity: tok.state === "active" ? 1 : 0.55 }}
          >
            <div style={{ flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.5 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <strong>{tok.name}</strong>
                <code style={{ fontSize: 12, color: "var(--faint)" }}>{tok.prefix}…</code>
                <Chip variant={tok.state === "active" ? "success" : undefined}>{t(STATE_LABEL[tok.state])}</Chip>
                {tok.tolko_chtenie && <Chip>{t("tokenReadOnly")}</Chip>}
              </div>
              <div style={{ color: "var(--faint)", fontSize: 12.5 }}>
                {t("tokenActsAs", { name: tok.user_name, email: tok.user_email })}
              </div>
              <div style={{ fontSize: 12.5 }}>
                <span style={{ color: tok.expires_at ? "var(--faint)" : "var(--danger)" }}>
                  {tok.expires_at ? t("apiKeyExpires", { t: formatDateTime(tok.expires_at, locale) }) : t("apiKeyNeverExpires")}
                </span>
                <span style={{ color: "var(--faint)" }}>
                  {" · "}
                  {tok.last_used_at ? t("tokenLastUsed", { t: formatDateTime(tok.last_used_at, locale) }) : t("apiKeyNeverUsed")}
                </span>
              </div>
            </div>
            {tok.state === "active" && (
              <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => setRevoking(tok)}>
                {t("apiKeyRevoke")}
              </button>
            )}
          </div>
        ))}
        {data.items.length === 0 && (
          <div style={{ padding: 18, color: "var(--faint)", fontSize: 13 }}>{t("tokenEmpty")}</div>
        )}
      </div>
      <div className="field-desc" style={{ marginTop: 12 }}>{t("tokenRules", { n: data.v_minutu })}</div>

      {creating && (
        <NovyyToken
          sroki={data.sroki}
          onClose={() => setCreating(false)}
          onCreated={(tok) => {
            setCreating(false);
            setShown(tok);
            load();
          }}
        />
      )}
      {shown && shown.token && <PokazannyyToken tok={shown} onClose={() => setShown(null)} />}
      {revoking && (
        <ConfirmModal
          text={t("tokenRevokeConfirm", { name: revoking.name })}
          confirmLabel={t("apiKeyRevoke")}
          danger
          onConfirm={() => { const tok = revoking; setRevoking(null); void revoke(tok); }}
          onClose={() => setRevoking(null)}
        />
      )}
    </div>
  );
}

function NovyyToken({
  sroki,
  onClose,
  onCreated,
}: {
  sroki: number[];
  onClose: () => void;
  onCreated: (tok: Token) => void;
}) {
  const { t, user, toastError } = useApp();
  const guard = useGuard();
  const lyudi = useReference<Person>("/people");
  const [form, setForm] = useState({ name: "", user_id: user?.id ?? 0, days: 90, tolko_chtenie: false });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      onCreated(await api.post<Token>("/tokens", form));
    } catch (err) {
      toastError(err);
      guard.free();
    }
  };

  return (
    <Modal title={t("tokenNew")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label" htmlFor="token-name">{t("apiKeyName")}</label>
          <input
            id="token-name"
            className="input"
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            autoFocus
            required
          />
          <div className="field-desc">{t("tokenNameHint")}</div>
        </div>
        <div className="field">
          <label className="label" htmlFor="token-user">{t("tokenUser")}</label>
          {lyudi.failure ? (
            <LoadFailed error={lyudi.failure} onRetry={lyudi.reload} />
          ) : (
            <select
              id="token-user"
              className="input"
              value={form.user_id}
              onChange={(e) => setForm((f) => ({ ...f, user_id: Number(e.target.value) }))}
            >
              {(lyudi.items ?? []).map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          )}
          <div className="field-desc">{t("tokenUserHint")}</div>
        </div>
        <div className="field">
          <label className="label" htmlFor="token-days">{t("tokenSrok")}</label>
          <select
            id="token-days"
            className="input"
            value={form.days}
            onChange={(e) => setForm((f) => ({ ...f, days: Number(e.target.value) }))}
          >
            {sroki.map((d) => (
              <option key={d} value={d}>{t(SROK_LABEL[d] ?? "tokenSrok90")}</option>
            ))}
          </select>
        </div>
        <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13, marginBottom: 16 }}>
          <input
            type="checkbox"
            checked={form.tolko_chtenie}
            onChange={(e) => setForm((f) => ({ ...f, tolko_chtenie: e.target.checked }))}
          />
          <span>
            {t("tokenReadOnly")}
            <span className="field-desc" style={{ display: "block" }}>{t("tokenReadOnlyHint")}</span>
          </span>
        </label>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy || !form.user_id}>
          {t("tokenNew")}
        </button>
      </form>
    </Modal>
  );
}

function PokazannyyToken({ tok, onClose }: { tok: Token; onClose: () => void }) {
  const { t } = useApp();
  const adres = `${window.location.origin}/api/v1`;
  const primer = `curl -H "Authorization: Bearer ${tok.token}" \\\n  ${adres}/auth/me`;
  return (
    <Modal title={tok.name} onClose={onClose}>
      <div style={{ color: "var(--warning)", fontSize: 12.5, marginBottom: 10, lineHeight: 1.5 }}>{t("tokenShown")}</div>
      <div className="docs-code-wrap token-kod">
        <pre className="code-block">{tok.token}</pre>
        <CopyButton text={tok.token ?? ""} />
      </div>
      <div className="label" style={{ marginTop: 16 }}>{t("tokenExample")}</div>
      <div className="docs-code-wrap token-kod">
        <pre className="code-block">{primer}</pre>
        <CopyButton text={primer} />
      </div>
      <div className="label" style={{ marginTop: 16 }}>{t("tokenOpenapi")}</div>
      <div className="docs-code-wrap token-kod">
        <pre className="code-block">{adres}/system/openapi.json</pre>
        <CopyButton text={`${adres}/system/openapi.json`} />
      </div>
      <div className="field-desc" style={{ marginTop: 8 }}>{t("tokenOpenapiHint")}</div>
    </Modal>
  );
}
