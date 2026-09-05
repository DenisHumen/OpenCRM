import { Fragment, useCallback, useEffect, useState, type FormEvent } from "react";

import { CopyButton } from "../components/CopyButton";
import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDate, formatDateTime } from "../lib/format";
import { useGuard } from "../lib/guard";
import { useLiveTopic } from "../lib/live";
import type { TranslationKey } from "../lib/i18n";

interface ApiKey {
  id: number;
  name: string;
  prefix: string;
  scopes: string[];
  warehouse_id: number | null;
  stock_mode: string;
  few_threshold_milli: number;
  rate_per_min: number;
  max_reserve_minutes: number;
  ttl_sec: number;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string | null;
  last_used_at: string | null;
  last_used_ip: string;
  state: "active" | "expired" | "revoked";
  hits_30d: number;
  key?: string;
}

interface Spisok {
  items: ApiKey[];
  alive: number;
  scopes: string[];
  stock_modes: string[];
  header: string;
}

interface Sklad {
  id: number;
  name: string;
  kind: string;
  deleted_at: string | null;
}

const SCOPE_LABEL: Record<string, TranslationKey> = {
  "catalog.read": "apiKeyScopeCatalogRead",
  "stock.read": "apiKeyScopeStockRead",
  "orders.write": "apiKeyScopeOrdersWrite",
  "orders.read": "apiKeyScopeOrdersRead",
  "customers.write": "apiKeyScopeCustomersWrite",
  "leads.write": "apiKeyScopeLeadsWrite",
};

const STOCK_LABEL: Record<string, TranslationKey> = {
  exact: "apiKeyStockExact",
  bucket: "apiKeyStockBucket",
  boolean: "apiKeyStockBoolean",
};

const STATE_LABEL: Record<ApiKey["state"], TranslationKey> = {
  active: "apiKeyStateActive",
  expired: "apiKeyStateExpired",
  revoked: "apiKeyStateRevoked",
};

export function SettingsApiKeys() {
  const { t, locale, toastError } = useApp();
  const [data, setData] = useState<Spisok | null>(null);
  const [sklady, setSklady] = useState<Sklad[]>([]);
  const { failure, fail, clear } = useFailure();
  const guard = useGuard();
  const [creating, setCreating] = useState(false);
  // Чья сводка раскрыта: одна за раз — пять графиков подряд не читаются.
  const [stats, setStats] = useState<number | null>(null);
  const [shown, setShown] = useState<ApiKey | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [rotating, setRotating] = useState<ApiKey | null>(null);

  const load = useCallback(() => {
    clear();
    api.get<Spisok>("/settings/api-keys").then(setData).catch(fail);
    // Склады могут быть выключены блоком — тогда список пуст, и ключ без
    // `stock.read` всё равно выпускается: услуги нигде не лежат.
    api.get<{ items: Sklad[] }>("/warehouses").then((r) => setSklady(r.items)).catch(() => setSklady([]));
  }, [fail, clear]);

  useEffect(load, [load]);

  // Число обращений в строке ключа растёт вместе со сводкой: тот же намёк.
  useLiveTopic("api_keys", load);

  if (!data) return <ScreenLoading error={failure} onRetry={load} />;

  const revoke = async (key: ApiKey) => {
    if (!guard.take()) return;
    try {
      await api.post(`/settings/api-keys/${key.id}/revoke`);
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const rotate = async (key: ApiKey) => {
    if (!guard.take()) return;
    try {
      setShown(await api.post<ApiKey>(`/settings/api-keys/${key.id}/rotate`, { grace_hours: 24 }));
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const skladName = (id: number | null) => sklady.find((s) => s.id === id)?.name ?? (id ? `#${id}` : "—");

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("apiKeys")}</h1>
          <div className="page-sub">{t("apiKeysSub")}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          <Icon name="plus" size={14} />
          {t("apiKeyNew")}
        </button>
      </div>

      {/* Выключателя нет намеренно: «наружу открыто» решают живые ключи. Тот,
          кто ищет выключатель, должен найти ответ, а не пустое место. */}
      <div
        className="card"
        style={{ padding: "14px 18px", marginBottom: 16, color: data.alive ? "var(--warning)" : "var(--faint)", fontSize: 13 }}
      >
        {data.alive ? t("apiKeysOpen", { count: data.alive }) : t("apiKeysClosed")}
      </div>

      <div className="list-card">
        {data.items.map((key) => (
          <Fragment key={key.id}>
          <div
            className="list-row"
            style={{ alignItems: "flex-start", height: "auto", padding: "12px 16px", opacity: key.state === "active" ? 1 : 0.55 }}
          >
            <div style={{ flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.5 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <strong>{key.name}</strong>
                <code style={{ fontSize: 12, color: "var(--faint)" }}>{key.prefix}…</code>
                <Chip variant={key.state === "active" ? "success" : undefined}>{t(STATE_LABEL[key.state])}</Chip>
                <span style={{ color: key.expires_at ? "var(--faint)" : "var(--danger)", fontSize: 12.5 }}>
                  {key.expires_at ? t("apiKeyExpires", { t: formatDateTime(key.expires_at, locale) }) : t("apiKeyNeverExpires")}
                </span>
              </div>
              <div style={{ color: "var(--faint)", fontSize: 12.5 }}>
                {key.scopes.map((s) => t(SCOPE_LABEL[s] ?? "apiKeyScopes")).join(" · ")}
              </div>
              <div style={{ color: "var(--faint)", fontSize: 12.5 }}>
                {key.warehouse_id ? <>{t("apiKeyWarehouse")}: {skladName(key.warehouse_id)} · </> : null}
                {t(STOCK_LABEL[key.stock_mode] ?? "apiKeyStockBucket")} · {key.rate_per_min}/min ·{" "}
                {key.last_used_at
                  ? t("apiKeyLastUsed", { t: formatDateTime(key.last_used_at, locale), ip: key.last_used_ip || "—" })
                  : t("apiKeyNeverUsed")}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6 }}>
                <Chip variant={key.hits_30d > 0 ? "accent" : undefined}>{t("apiStatsHits", { n: key.hits_30d })}</Chip>
                <button type="button" className="text-link" onClick={() => setStats(stats === key.id ? null : key.id)}>
                  {stats === key.id ? t("apiStatsHide") : t("apiStats")}
                </button>
              </div>
            </div>
            {key.state === "active" && (
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => setRotating(key)}>
                  {t("apiKeyRotate")}
                </button>
                <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => setRevoking(key)}>
                  {t("apiKeyRevoke")}
                </button>
              </div>
            )}
          </div>
          {stats === key.id && <KlyuchStatistika klyuch={key} />}
          </Fragment>
        ))}
        {data.items.length === 0 && (
          <div style={{ padding: 18, color: "var(--faint)", fontSize: 13 }}>{t("apiKeyEmpty")}</div>
        )}
      </div>
      <div className="field-desc" style={{ marginTop: 12 }}>{t("apiKeyDocs")}</div>

      {creating && (
        <NewKeyModal
          scopes={data.scopes}
          stockModes={data.stock_modes}
          shops={sklady.filter((s) => s.kind === "shop" && !s.deleted_at)}
          onClose={() => setCreating(false)}
          onCreated={(key) => {
            setCreating(false);
            setShown(key);
            load();
          }}
        />
      )}
      {shown && shown.key && (
        <Modal title={shown.name} onClose={() => setShown(null)}>
          <div style={{ color: "var(--warning)", fontSize: 12.5, marginBottom: 10, lineHeight: 1.5 }}>{t("apiKeyShown")}</div>
          <KlyuchKarta klyuch={shown} />
          <div className="field-desc" style={{ marginTop: 10 }}>
            {t("apiKeyHeader")}: <code>{data.header}</code>
          </div>
        </Modal>
      )}
      {revoking && (
        <ConfirmModal
          text={t("apiKeyRevokeConfirm", { name: revoking.name })}
          confirmLabel={t("apiKeyRevoke")}
          danger
          onConfirm={() => { const k = revoking; setRevoking(null); void revoke(k); }}
          onClose={() => setRevoking(null)}
        />
      )}
      {rotating && (
        <ConfirmModal
          text={t("apiKeyRotateConfirm")}
          confirmLabel={t("apiKeyRotate")}
          onConfirm={() => { const k = rotating; setRotating(null); void rotate(k); }}
          onClose={() => setRotating(null)}
        />
      )}
    </div>
  );
}

function NewKeyModal({
  scopes,
  stockModes,
  shops,
  onClose,
  onCreated,
}: {
  scopes: string[];
  stockModes: string[];
  shops: Sklad[];
  onClose: () => void;
  onCreated: (key: ApiKey) => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [form, setForm] = useState({
    name: "",
    scopes: ["catalog.read", "stock.read"] as string[],
    warehouse_id: shops[0]?.id ?? null,
    days: "365",
    stock_mode: "bucket",
    few: "5",
    rate: "120",
    reserve_max: "1440",
    ttl: "60",
  });
  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const toggleScope = (scope: string) =>
    setForm((f) => ({
      ...f,
      scopes: f.scopes.includes(scope) ? f.scopes.filter((s) => s !== scope) : [...f.scopes, scope],
    }));
  const needsWarehouse = form.scopes.includes("stock.read");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      onCreated(
        await api.post<ApiKey>("/settings/api-keys", {
          name: form.name,
          scopes: form.scopes,
          warehouse_id: needsWarehouse ? form.warehouse_id : null,
          days: Number(form.days),
          stock_mode: form.stock_mode,
          // Порог вводится в единицах, уезжает в тысячных — как всё количество.
          few_threshold_milli: Math.round(Number(form.few) * 1000),
          rate_per_min: Number(form.rate),
          max_reserve_minutes: Number(form.reserve_max),
          ttl_sec: Number(form.ttl),
        }),
      );
    } catch (err) {
      toastError(err);
      guard.free();
    }
  };

  return (
    <Modal title={t("apiKeyNew")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("apiKeyName")}</label>
          <input className="input" value={form.name} onChange={set("name")} autoFocus required />
          <div className="field-desc">{t("apiKeyNameHint")}</div>
        </div>
        <div className="field">
          <label className="label">{t("apiKeyScopes")}</label>
          {scopes.map((scope) => (
            <label key={scope} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, marginBottom: 4 }}>
              <input type="checkbox" checked={form.scopes.includes(scope)} onChange={() => toggleScope(scope)} />
              {t(SCOPE_LABEL[scope] ?? "apiKeyScopes")}
            </label>
          ))}
        </div>
        {needsWarehouse && (
          <div className="field">
            <label className="label">{t("apiKeyWarehouse")}</label>
            {shops.length === 0 ? (
              <div className="field-desc" style={{ color: "var(--warning)" }}>{t("apiKeyNoShops")}</div>
            ) : (
              <select className="input" value={form.warehouse_id ?? ""} onChange={(e) => setForm((f) => ({ ...f, warehouse_id: Number(e.target.value) }))}>
                {shops.map((s) => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            )}
            <div className="field-desc">{t("apiKeyWarehouseHint")}</div>
          </div>
        )}
        <div className="field">
          <label className="label">{t("apiKeyStockMode")}</label>
          <select className="input" value={form.stock_mode} onChange={set("stock_mode")}>
            {stockModes.map((m) => (
              <option key={m} value={m}>{t(STOCK_LABEL[m] ?? "apiKeyStockBucket")}</option>
            ))}
          </select>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <div className="field">
            <label className="label">{t("apiKeyDays")}</label>
            <input className="input" type="number" min={0} value={form.days} onChange={set("days")} />
            <div className="field-desc">{t("apiKeyDaysHint")}</div>
          </div>
          <div className="field">
            <label className="label">{t("apiKeyFew")}</label>
            <input className="input" type="number" min={0} step="0.001" value={form.few} onChange={set("few")} />
          </div>
          <div className="field">
            <label className="label">{t("apiKeyRate")}</label>
            <input className="input" type="number" min={1} value={form.rate} onChange={set("rate")} />
          </div>
          <div className="field">
            <label className="label">{t("apiKeyReserveMax")}</label>
            <input className="input" type="number" min={1} value={form.reserve_max} onChange={set("reserve_max")} />
          </div>
          <div className="field">
            <label className="label">{t("apiKeyTtl")}</label>
            <input className="input" type="number" min={5} value={form.ttl} onChange={set("ttl")} />
          </div>
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy || (needsWarehouse && !form.warehouse_id)}>
          {t("apiKeyNew")}
        </button>
      </form>
    </Modal>
  );
}

/** Ключ как банковская карта: лицо с приставкой, оборот с самим ключом.
 *  Перевод uiverse.io/Praashoo7/black-lizard-62 (docs/18). По наведению
 *  переворачивается сам; на касании — по нажатию, иначе оборот не увидеть. */
function KlyuchKarta({ klyuch }: { klyuch: ApiKey }) {
  const { t, locale } = useApp();
  const [perevyornut, setPerevyornut] = useState(false);
  const srok = klyuch.expires_at ? formatDate(klyuch.expires_at, locale) : t("apiKeyNeverExpires");
  return (
    <>
      <div
        className={"klyuch" + (perevyornut ? " klyuch-flipped" : "")}
        role="button"
        tabIndex={0}
        aria-label={t("apiKeyFlipHint")}
        onClick={() => setPerevyornut((bylo) => !bylo)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setPerevyornut((bylo) => !bylo);
          }
        }}
      >
        <div className="klyuch-inner">
          <div className="klyuch-front">
            <span className="klyuch-head">OpenCRM · API</span>
            <span className="klyuch-chip" />
            <span className="klyuch-wave">
              <Icon name="globe" size={18} />
            </span>
            <span className="klyuch-num">{klyuch.prefix} •••• •••• ••••</span>
            <span className="klyuch-valid">{t("apiKeyValid")}</span>
            <span className="klyuch-date">{srok}</span>
            <span className="klyuch-name">{klyuch.name}</span>
          </div>
          <div className="klyuch-back">
            <div className="klyuch-strip" />
            <code className="klyuch-sign">{klyuch.key}</code>
            <span className="klyuch-copy" onClick={(e) => e.stopPropagation()}>
              <CopyButton text={klyuch.key ?? ""} />
            </span>
          </div>
        </div>
      </div>
      <div className="klyuch-hint">{t("apiKeyFlipHint")}</div>
    </>
  );
}

interface Svodka {
  today: number;
  week: number;
  month: number;
  rejected_month: number;
  avg_per_day: number;
  peak_hour: number;
  rate_per_min: number;
  by_category: { category: string; count: number; share: number }[];
  by_day: { date: string; count: number; rejected: number }[];
  by_hour: { hour: string; count: number }[];
}

function Plitka({ title, value, sub }: { title: string; value: number; sub?: string }) {
  return (
    <div className="card card-pad">
      <div className="metric-title">{title}</div>
      <div className="metric-value">{value}</div>
      {sub && <div className="metric-sub">{sub}</div>}
    </div>
  );
}

/** Сводка обращений по ключу: числа, по видам, по дням и часам. Живая: намёк
 *  темы `api_keys` с номером ключа — перечитать; сервер шлёт его не чаще раза
 *  в две секунды (`api_stats_service`), своей задержки здесь нет. */
function KlyuchStatistika({ klyuch }: { klyuch: ApiKey }) {
  const { t } = useApp();
  const [svodka, setSvodka] = useState<Svodka | null>(null);
  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setSvodka(await api.get<Svodka>(`/settings/api-keys/${klyuch.id}/stats`));
    } catch (e) {
      fail(e);
    }
  }, [klyuch.id, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  useLiveTopic("api_keys", (s) => {
    if (s.resync || s.hints.some((h) => h.id === klyuch.id)) void load();
  });

  if (failure !== null) {
    return (
      <div className="stat-blok">
        <LoadFailed error={failure} onRetry={() => void load()} />
      </div>
    );
  }
  if (svodka === null) {
    return <div className="stat-blok stat-tikho">{t("loading")}</div>;
  }
  const maxDen = Math.max(1, ...svodka.by_day.map((d) => d.count));
  const maxChas = Math.max(1, ...svodka.by_hour.map((h) => h.count));
  return (
    <div className="stat-blok">
      <div className="metric-grid stat-plitki">
        <Plitka title={t("apiStatsToday")} value={svodka.today} />
        <Plitka title={t("apiStatsWeek")} value={svodka.week} />
        <Plitka title={t("apiStatsMonth")} value={svodka.month} sub={t("apiStatsRejected", { n: svodka.rejected_month })} />
        <Plitka title={t("apiStatsAvg")} value={svodka.avg_per_day} />
        <Plitka title={t("apiStatsPeak")} value={svodka.peak_hour} sub={t("apiStatsPeakSub", { n: svodka.rate_per_min })} />
      </div>
      <div className="stat-ryad">
        <div className="metric-title">{t("apiStatsByDay")}</div>
        <div className="bars stat-bars">
          {svodka.by_day.map((d, i) => (
            <div className="bar-col" key={d.date}>
              <div
                className={"bar stat-bar" + (d.count === maxDen && d.count > 0 ? " top" : "")}
                style={{ height: Math.max(3, Math.round((d.count / maxDen) * 52)) }}
                title={`${d.date}: ${d.count}`}
              />
              <span className="bar-label">{i % 5 === 4 ? d.date.slice(8) : ""}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="stat-ryad">
        <div className="metric-title">{t("apiStatsByHour")}</div>
        <div className="bars stat-bars">
          {svodka.by_hour.map((h, i) => (
            <div className="bar-col" key={h.hour}>
              <div
                className={"bar stat-bar" + (h.count === maxChas && h.count > 0 ? " top" : "")}
                style={{ height: Math.max(3, Math.round((h.count / maxChas) * 52)) }}
                title={`${h.hour.slice(11, 16)}: ${h.count}`}
              />
              <span className="bar-label">{i % 6 === 5 ? h.hour.slice(11, 13) : ""}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="stat-ryad">
        <div className="metric-title">{t("apiStatsByCategory")}</div>
        {svodka.by_category.length === 0 && <div className="stat-tikho">{t("apiStatsEmpty")}</div>}
        <div className="src-table">
          {svodka.by_category.map((c) => (
            <div key={c.category} className="src-row">
              <div className="src-bar" style={{ width: `${Math.round(c.share * 100)}%` }} />
              <span className="src-name">{t(SCOPE_LABEL[c.category] ?? "apiKeyScopes")}</span>
              <span className="src-num">{c.count}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="field-desc">{t("apiStatsLive")}</div>
    </div>
  );
}
