import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { ContextMenu, useContextMenu, type PunktMenyu } from "../components/ContextMenu";
import { CopyButton } from "../components/CopyButton";
import { Icon } from "../components/Icon";
import {
  Chip,
  ConfirmModal,
  EmptyState,
  KnopkaKorziny,
  LoadFailed,
  Modal,
  ScreenLoading,
  Spinner,
  Toggle,
} from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { copyText } from "../lib/clipboard";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { fileSize, formatDateTime } from "../lib/format";
import type { TranslationKey } from "../lib/i18n";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import { PokazParolya } from "./MailServer";

/**
 * Почтовые ящики фирмы: на своём сервере и внешние (чужой IMAP/SMTP).
 *
 * Сам блок включается в «Настройки → Модули», как и все остальные; здесь только
 * его настройка. Два переключателя одного и того же на разных экранах — верный
 * способ развести состояние в голове у человека с состоянием в базе.
 */

type Vkladka = "server" | "aliases" | "external" | "archive";

export function Mailboxes() {
  const { t, user, modules } = useApp();
  const enabled = moduleOn(modules, "mail");
  // Ящики своего сервера заводит управляющий почтой: ручки `/mail/admin` на его праве.
  const upravlyaet = can(user, "mail.manage");
  const [vkladka, setVkladka] = useState<Vkladka>(upravlyaet ? "server" : "external");

  const vkladki: { key: Vkladka; label: string }[] = [
    ...(upravlyaet
      ? [
          { key: "server" as Vkladka, label: t("mbxTabServer") },
          { key: "aliases" as Vkladka, label: t("mbxTabAliases") },
        ]
      : []),
    { key: "external", label: t("mbxTabExternal") },
    ...(upravlyaet ? [{ key: "archive" as Vkladka, label: t("mbxTabArchive") }] : []),
  ];

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 18 }}>
        <div>
          <h1 className="page-title">{t("mailboxes")}</h1>
          <div className="page-sub">{t("mailboxesSub")}</div>
        </div>
      </div>

      {!enabled ? (
        <div className="card card-pad">
          <div className="field-desc" style={{ marginTop: 0 }}>{t("mailModuleOffHint")}</div>
        </div>
      ) : (
        <>
          {vkladki.length > 1 && (
            <div className="tabs" role="tablist">
              {vkladki.map((v) => (
                <button
                  key={v.key}
                  type="button"
                  role="tab"
                  aria-selected={vkladka === v.key}
                  className={"tab" + (vkladka === v.key ? " active" : "")}
                  onClick={() => setVkladka(v.key)}
                >
                  {v.label}
                </button>
              ))}
            </div>
          )}
          {vkladka === "server" && <YashchikiServera />}
          {vkladka === "aliases" && <Psevdonimy />}
          {vkladka === "external" && <VneshnieYashchiki />}
          {vkladka === "archive" && <KopiyaVCrm />}
        </>
      )}
    </div>
  );
}

// --- копия писем в CRM -------------------------------------------------------

interface KopiyaYashchika {
  account_id: number | null;
  address: string;
  title: string;
  archive: boolean;
  messages: number;
  bytes: number;
  removed: number;
  synced_at: string | null;
}

/**
 * Копия писем в базе CRM по ящикам: сколько, сколько места, когда догнали — и
 * вести ли её. Ящики своего сервера ведутся сразу, внешние — по выбору.
 * Выключение копию не стирает: она для того и заведена, чтобы пережить сервер.
 */
function KopiyaVCrm() {
  const { t, locale, toastError } = useApp();
  const [items, setItems] = useState<KopiyaYashchika[] | null>(null);
  const { failure, fail, clear } = useFailure();
  const guard = useGuard();

  const load = useCallback(() => {
    clear();
    api.get<KopiyaYashchika[]>("/mail/archive/stats").then(setItems).catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);
  useLiveTopic("mail", load);

  const pereklyuchit = async (k: KopiyaYashchika) => {
    if (k.account_id === null || !guard.take()) return;
    try {
      await api.put(`/mail/archive/boxes/${k.account_id}`, { archive: !k.archive });
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  if (!items) return <ScreenLoading error={failure} onRetry={load} />;
  return (
    <>
      <div className="field-desc" style={{ marginTop: 0, marginBottom: 12 }}>{t("mbxArchiveIntro")}</div>
      {items.length === 0 ? (
        <div className="list-card">
          <EmptyState icon="database" title={t("mbxArchiveEmpty")} />
        </div>
      ) : (
        <div className="list-card">
          {items.map((k) => (
            <div key={k.account_id ?? `x-${k.address}`} className="list-row" style={{ height: "auto", padding: "10px 14px", gap: 12 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13.5, fontWeight: 500, overflowWrap: "anywhere" }}>{k.title || k.address}</div>
                <div className="msrv-faint msrv-small">
                  {k.title ? `${k.address} · ` : ""}
                  {t("msrvArchiveLetters", { n: k.messages })} · {fileSize(k.bytes)}
                  {k.removed > 0 && ` · ${t("msrvArchiveRemoved", { n: k.removed })}`}
                  {" · "}
                  {k.synced_at ? t("msrvArchiveSynced", { t: formatDateTime(k.synced_at, locale) }) : t("msrvArchiveNever")}
                </div>
              </div>
              {k.account_id === null ? (
                <Chip>{t("mbxArchiveBoxGone")}</Chip>
              ) : (
                <Toggle
                  on={k.archive}
                  label={t("mbxArchiveKeep", { address: k.address })}
                  onToggle={() => void pereklyuchit(k)}
                />
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// --- ящики своего сервера ---------------------------------------------------

interface Dostup {
  user_id: number;
  name?: string;
  can_send: boolean;
}

interface Yashchik {
  id: number;
  address: string;
  title: string;
  server_id: number | null;
  is_shared: boolean;
  quota_mb: number | null;
  is_active: boolean;
  unseen?: number;
  access?: Dostup[];
  users?: Dostup[];
}

interface ServerKratko {
  id: number;
  status: string;
  hostname: string;
  title: string;
  domains: { id: number; domain: string }[];
}

interface Person {
  id: number;
  name: string;
}

function vynutSpisok<T>(otvet: unknown): T[] {
  if (Array.isArray(otvet)) return otvet as T[];
  const o = otvet as { items?: T[]; boxes?: T[] } | null;
  return o?.items ?? o?.boxes ?? [];
}

function razmer(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(mb % 1024 ? 1 : 0)} GB` : `${mb} MB`;
}

/** Случайные имя и пароль — сервер их только придумывает, ничего не заводя. */
async function predlozhit(domain: string): Promise<{ local_part: string; password: string }> {
  return api.get(`/mail/admin/suggest?domain=${encodeURIComponent(domain)}`);
}

function YashchikiServera() {
  const { t, toastError } = useApp();
  const [boxes, setBoxes] = useState<Yashchik[] | null>(null);
  const [servers, setServers] = useState<ServerKratko[] | null>(null);
  const { failure, fail, clear } = useFailure();
  const kontekst = useContextMenu();
  const guard = useGuard();

  const [novyy, setNovyy] = useState(false);
  const [parol, setParol] = useState<{ address: string; password: string } | null>(null);
  const [smenaParolya, setSmenaParolya] = useState<Yashchik | null>(null);
  const [pereimenovat, setPereimenovat] = useState<Yashchik | null>(null);
  const [pravka, setPravka] = useState<Yashchik | null>(null);
  const [dostup, setDostup] = useState<Yashchik | null>(null);
  const [telefon, setTelefon] = useState<Yashchik | null>(null);
  const [udalit, setUdalit] = useState<Yashchik | null>(null);

  const load = useCallback(async () => {
    clear();
    try {
      const [b, s] = await Promise.all([
        api.get("/mail/admin/boxes"),
        api.get<{ servers: ServerKratko[] }>("/mail/server"),
      ]);
      setBoxes(vynutSpisok<Yashchik>(b).filter((x) => x.server_id !== null && x.server_id !== undefined));
      setServers(s.servers.filter((x) => x.status === "ready"));
    } catch (e) {
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveTopic("mail", () => void load());

  if (!boxes || !servers) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  if (servers.length === 0) {
    return (
      <div className="list-card">
        <EmptyState
          icon="mail"
          title={t("mbxNoServerTitle")}
          sub={t("mbxNoServer")}
          action={
            <Link className="btn btn-primary btn-sm" to="/settings/mail-server">
              {t("mbxSetupServer")}
            </Link>
          }
        />
      </div>
    );
  }

  const vklyuchit = async (box: Yashchik) => {
    if (!guard.take()) return;
    try {
      await api.patch(`/mail/admin/boxes/${box.id}`, { is_active: !box.is_active });
      await load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const punkty = (box: Yashchik): PunktMenyu[] => [
    { key: "password", label: t("mbxPassword"), icon: "lock", run: () => setSmenaParolya(box) },
    { key: "rename", label: t("mbxRename"), icon: "note", run: () => setPereimenovat(box) },
    { key: "edit", label: t("mbxEdit"), icon: "settings", run: () => setPravka(box) },
    { key: "access", label: t("mbxAccess"), icon: "staff", run: () => setDostup(box) },
    { key: "phone", label: t("mbxPhone"), icon: "call", run: () => setTelefon(box) },
    {
      key: "active",
      label: box.is_active ? t("mbxTurnOff") : t("mbxTurnOn"),
      icon: "eye",
      run: () => void vklyuchit(box),
    },
    { key: "copy", label: t("mbxCopyAddress"), icon: "copy", run: () => void copyText(box.address) },
    { key: "delete", label: t("delete"), icon: "trash", opasno: true, razdel: true, run: () => setUdalit(box) },
  ];

  return (
    <div>
      <div className="mbx-head">
        <div className="field-desc" style={{ marginTop: 0, flex: 1 }}>{t("mbxServerSub")}</div>
        <button className="btn btn-primary btn-sm" onClick={() => setNovyy(true)}>
          <Icon name="plus" size={13} stroke={2} />
          {t("mbxNew")}
        </button>
      </div>
      <div className="list-card">
        {boxes.map((box) => {
          const lyudi = box.access ?? box.users ?? [];
          return (
            <div
              key={box.id}
              className="list-row mbx-row"
              onContextMenu={(e) => kontekst.otkryt(e, punkty(box))}
            >
              <span className="mbx-avatar" aria-hidden="true">
                <Icon name="mail" size={15} />
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="mbx-address">
                  {box.address}
                  {box.title && <span className="mbx-faint"> · {box.title}</span>}
                </div>
                <div className="mbx-chips">
                  {!box.is_active && <Chip variant="warning">{t("mbxOff")}</Chip>}
                  {box.is_shared ? (
                    <Chip variant="accent">{t("mbxSharedChip")}</Chip>
                  ) : (
                    <Chip>{t("mbxPeopleN", { n: lyudi.length })}</Chip>
                  )}
                  <Chip>
                    {box.quota_mb ? razmer(box.quota_mb) : t("mbxQuotaNone")}
                  </Chip>
                  {!!box.unseen && <Chip variant="brand">{t("mbxUnseen", { n: box.unseen })}</Chip>}
                </div>
              </div>
              <button
                className="btn btn-secondary btn-sm mbx-hide-narrow"
                onClick={() => setSmenaParolya(box)}
              >
                <Icon name="lock" size={13} />
                {t("mbxPassword")}
              </button>
              <button
                className="btn-icon"
                aria-label={t("actions")}
                title={t("actions")}
                onClick={(e) => kontekst.otkryt(e, punkty(box))}
              >
                <Icon name="dots" size={15} />
              </button>
            </div>
          );
        })}
        {boxes.length === 0 && (
          <EmptyState
            icon="mail"
            title={t("mbxEmpty")}
            action={
              <button className="btn btn-primary btn-sm" onClick={() => setNovyy(true)}>
                {t("mbxNew")}
              </button>
            }
          />
        )}
      </div>
      <ContextMenu menu={kontekst.menu} zakryt={kontekst.zakryt} />

      {novyy && (
        <NovyyYashchik
          servers={servers}
          onClose={() => setNovyy(false)}
          onCreated={(address, password) => {
            setNovyy(false);
            setParol({ address, password });
            void load();
          }}
        />
      )}
      {parol && (
        <Modal title={t("mbxCreatedTitle")} onClose={() => setParol(null)}>
          <PokazParolya address={parol.address} password={parol.password} />
          <div className="mbx-actions">
            <button className="btn btn-primary btn-sm" onClick={() => setParol(null)}>
              {t("mbxSaved")}
            </button>
          </div>
        </Modal>
      )}
      {smenaParolya && (
        <SmenaParolya
          box={smenaParolya}
          onClose={() => setSmenaParolya(null)}
          onDone={(password) => {
            const address = smenaParolya.address;
            setSmenaParolya(null);
            setParol({ address, password });
          }}
        />
      )}
      {pereimenovat && (
        <Pereimenovanie
          box={pereimenovat}
          servers={servers}
          onClose={() => setPereimenovat(null)}
          onDone={(address, password) => {
            setPereimenovat(null);
            if (password) setParol({ address, password });
            void load();
          }}
        />
      )}
      {pravka && (
        <PravkaYashchika
          box={pravka}
          onClose={() => setPravka(null)}
          onDone={() => {
            setPravka(null);
            void load();
          }}
        />
      )}
      {dostup && (
        <DostupYashchika
          box={dostup}
          onClose={() => setDostup(null)}
          onDone={() => {
            setDostup(null);
            void load();
          }}
        />
      )}
      {telefon && <NastroykiKlienta box={telefon} onClose={() => setTelefon(null)} />}
      {udalit && (
        <UdalenieYashchika
          box={udalit}
          onClose={() => setUdalit(null)}
          onDone={() => {
            setUdalit(null);
            void load();
          }}
        />
      )}
    </div>
  );
}

/** Поле пароля: свой или 🎲, с показом и копированием — пароль увидят один раз. */
function PoleParolya({
  id,
  value,
  onChange,
  onRandom,
  zanyat,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  onRandom: () => void;
  zanyat?: boolean;
}) {
  const { t } = useApp();
  const [vidno, setVidno] = useState(false);
  return (
    <div className="field">
      <label className="label" htmlFor={id}>
        {t("password")}
      </label>
      <div className="mbx-input-row">
        <input
          id={id}
          className="input mbx-mono"
          type={vidno ? "text" : "password"}
          autoComplete="new-password"
          value={value}
          placeholder={t("mbxPasswordAutoPh")}
          onChange={(e) => onChange(e.target.value)}
        />
        <button
          type="button"
          className="btn-icon"
          aria-label={vidno ? t("mbxHide") : t("mbxShow")}
          title={vidno ? t("mbxHide") : t("mbxShow")}
          onClick={() => setVidno((v) => !v)}
        >
          <Icon name="eye" size={14} />
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-sm mbx-dice"
          disabled={zanyat}
          title={t("mbxPasswordGen")}
          onClick={() => {
            setVidno(true);
            onRandom();
          }}
        >
          <span aria-hidden="true">🎲</span>
          {t("mbxPasswordGen")}
        </button>
        {value && (
          <span className="mbx-copy">
            <CopyButton text={value} />
          </span>
        )}
      </div>
      <div className="field-desc">{t("mbxPasswordHint")}</div>
    </div>
  );
}

function VyborLyudey({
  shared,
  setShared,
  lyudi,
  setLyudi,
}: {
  shared: boolean;
  setShared: (v: boolean) => void;
  lyudi: Dostup[];
  setLyudi: (v: Dostup[]) => void;
}) {
  const { t } = useApp();
  const spravochnik = useReference<Person>(shared ? null : "/people");
  const vybran = (id: number) => lyudi.find((l) => l.user_id === id);
  return (
    <>
      <div className="mbx-toggle-row">
        <Toggle on={shared} label={t("mbxShared")} onToggle={() => setShared(!shared)} />
        <div>
          <div style={{ fontSize: 13 }}>{t("mbxShared")}</div>
          <div className="field-desc" style={{ marginTop: 2 }}>{t("mbxSharedHint")}</div>
        </div>
      </div>
      {!shared && (
        <div className="field">
          <div className="label">{t("mbxWho")}</div>
          {spravochnik.failure ? (
            <LoadFailed error={spravochnik.failure} onRetry={spravochnik.reload} />
          ) : spravochnik.items === null ? (
            <Spinner />
          ) : (
            <div className="mbx-people">
              {spravochnik.items.map((p) => {
                const v = vybran(p.id);
                return (
                  <div key={p.id} className="mbx-person">
                    <label className="mbx-person-name">
                      <input
                        type="checkbox"
                        checked={!!v}
                        onChange={(e) =>
                          setLyudi(
                            e.target.checked
                              ? [...lyudi, { user_id: p.id, name: p.name, can_send: true }]
                              : lyudi.filter((l) => l.user_id !== p.id),
                          )
                        }
                      />
                      {p.name}
                    </label>
                    {v && (
                      <label className="mbx-person-send">
                        <input
                          type="checkbox"
                          checked={v.can_send}
                          onChange={(e) =>
                            setLyudi(
                              lyudi.map((l) => (l.user_id === p.id ? { ...l, can_send: e.target.checked } : l)),
                            )
                          }
                        />
                        {t("mbxCanSend")}
                      </label>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          <div className="field-desc">{t("mbxWhoHint")}</div>
        </div>
      )}
    </>
  );
}

function NovyyYashchik({
  servers,
  onClose,
  onCreated,
}: {
  servers: ServerKratko[];
  onClose: () => void;
  onCreated: (address: string, password: string) => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [serverId, setServerId] = useState(servers[0].id);
  const server = servers.find((s) => s.id === serverId) ?? servers[0];
  const [domain, setDomain] = useState(server.domains[0]?.domain ?? "");
  const [local, setLocal] = useState("");
  const [password, setPassword] = useState("");
  const [title, setTitle] = useState("");
  const [quota, setQuota] = useState("");
  const [shared, setShared] = useState(false);
  const [lyudi, setLyudi] = useState<Dostup[]>([]);
  const [dumaet, setDumaet] = useState(false);

  const sluchaynoe = async (chto: "local" | "password") => {
    setDumaet(true);
    try {
      const s = await predlozhit(domain);
      if (chto === "local") setLocal(s.local_part);
      else setPassword(s.password);
    } catch (e) {
      toastError(e);
    } finally {
      setDumaet(false);
    }
  };

  const sozdat = async () => {
    if (!local.trim() || !domain || !guard.take()) return;
    try {
      const otvet = await api.post<{ box: Yashchik; password: string }>("/mail/admin/boxes", {
        server_id: server.id,
        domain,
        local_part: local.trim().toLowerCase(),
        ...(password ? { password } : { generate_password: true }),
        quota_mb: quota ? Number(quota) : null,
        title: title.trim(),
        is_shared: shared,
        users: shared ? [] : lyudi.map((l) => ({ user_id: l.user_id, can_send: l.can_send })),
      });
      onCreated(otvet.box?.address ?? `${local.trim().toLowerCase()}@${domain}`, otvet.password);
    } catch (e) {
      toastError(e);
      guard.free();
    }
  };

  return (
    <Modal title={t("mbxNew")} onClose={onClose} wide>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void sozdat();
        }}
      >
        {servers.length > 1 && (
          <div className="field">
            <label className="label" htmlFor="mbx-server">{t("msrvTitle")}</label>
            <select
              id="mbx-server"
              className="input"
              value={serverId}
              onChange={(e) => {
                const id = Number(e.target.value);
                setServerId(id);
                setDomain(servers.find((s) => s.id === id)?.domains[0]?.domain ?? "");
              }}
            >
              {servers.map((s) => (
                <option key={s.id} value={s.id}>{s.title || s.hostname}</option>
              ))}
            </select>
          </div>
        )}
        <div className="field">
          <label className="label" htmlFor="mbx-local">{t("mbxLocal")}</label>
          <div className="mbx-input-row">
            <input
              id="mbx-local"
              className="input"
              value={local}
              autoFocus
              placeholder="office"
              onChange={(e) => setLocal(e.target.value)}
            />
            <span className="mbx-at">@</span>
            {server.domains.length > 1 ? (
              <select
                className="input mbx-domain"
                aria-label={t("msrvDomain")}
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
              >
                {server.domains.map((d) => (
                  <option key={d.id} value={d.domain}>{d.domain}</option>
                ))}
              </select>
            ) : (
              <span className="mbx-domain-text">{domain}</span>
            )}
            <button
              type="button"
              className="btn btn-secondary btn-sm mbx-dice"
              disabled={dumaet}
              title={t("mbxRandom")}
              onClick={() => void sluchaynoe("local")}
            >
              <span aria-hidden="true">🎲</span>
              {t("mbxRandom")}
            </button>
          </div>
          <div className="field-desc">{t("mbxLocalHint")}</div>
        </div>
        <PoleParolya
          id="mbx-password"
          value={password}
          onChange={setPassword}
          zanyat={dumaet}
          onRandom={() => void sluchaynoe("password")}
        />
        <div className="mbx-grid2">
          <div className="field">
            <label className="label" htmlFor="mbx-title">{t("mailTitleField")}</label>
            <input
              id="mbx-title"
              className="input"
              value={title}
              placeholder={t("mbxTitlePh")}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="mbx-quota">{t("mbxQuota")}</label>
            <input
              id="mbx-quota"
              className="input"
              inputMode="numeric"
              value={quota}
              placeholder={t("mbxQuotaNone")}
              onChange={(e) => setQuota(e.target.value.replace(/\D/g, ""))}
            />
            <div className="field-desc">{t("mbxQuotaHint")}</div>
          </div>
        </div>
        <VyborLyudey shared={shared} setShared={setShared} lyudi={lyudi} setLyudi={setLyudi} />
        <div className="mbx-actions">
          <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
            {t("cancel")}
          </button>
          <button type="submit" className="btn btn-primary btn-sm" disabled={guard.busy || !local.trim() || !domain}>
            {guard.busy && <Spinner />}
            {t("mbxCreate")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function SmenaParolya({
  box,
  onClose,
  onDone,
}: {
  box: Yashchik;
  onClose: () => void;
  onDone: (password: string) => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [password, setPassword] = useState("");
  const [dumaet, setDumaet] = useState(false);

  const sluchaynyy = async () => {
    setDumaet(true);
    try {
      setPassword((await predlozhit(box.address.split("@")[1] ?? "")).password);
    } catch (e) {
      toastError(e);
    } finally {
      setDumaet(false);
    }
  };

  const smenit = async () => {
    if (!guard.take()) return;
    try {
      const otvet = await api.post<{ password: string }>(
        `/mail/admin/boxes/${box.id}/password`,
        password ? { password } : { generate: true },
      );
      onDone(otvet.password);
    } catch (e) {
      toastError(e);
      guard.free();
    }
  };

  return (
    <Modal title={`${t("mbxPassword")} · ${box.address}`} onClose={onClose}>
      <PoleParolya id="mbx-new-password" value={password} onChange={setPassword} zanyat={dumaet} onRandom={() => void sluchaynyy()} />
      <div className="field-desc">{t("mbxPasswordChangeHint")}</div>
      <div className="mbx-actions">
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm" disabled={guard.busy} onClick={() => void smenit()}>
          {t("mbxPassword")}
        </button>
      </div>
    </Modal>
  );
}

function Pereimenovanie({
  box,
  servers,
  onClose,
  onDone,
}: {
  box: Yashchik;
  servers: ServerKratko[];
  onClose: () => void;
  onDone: (address: string, password: string | null) => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [staryyLocal, staryyDomen] = box.address.split("@");
  const server = servers.find((s) => s.id === box.server_id) ?? servers[0];
  const [local, setLocal] = useState(staryyLocal ?? "");
  const [domain, setDomain] = useState(staryyDomen ?? "");
  const [psevdonim, setPsevdonim] = useState(true);
  const [password, setPassword] = useState("");
  const [dumaet, setDumaet] = useState(false);

  const novyy = `${local.trim().toLowerCase()}@${domain}`;
  const izmenilsya = novyy !== box.address;

  const sluchaynoe = async (chto: "local" | "password") => {
    setDumaet(true);
    try {
      const s = await predlozhit(domain);
      if (chto === "local") setLocal(s.local_part);
      else setPassword(s.password);
    } catch (e) {
      toastError(e);
    } finally {
      setDumaet(false);
    }
  };

  const pereimenovat = async () => {
    if (!izmenilsya || !local.trim() || !guard.take()) return;
    try {
      const otvet = await api.post<{ box: Yashchik; password?: string | null }>(
        `/mail/admin/boxes/${box.id}/rename`,
        {
          local_part: local.trim().toLowerCase(),
          domain,
          ...(password ? { password } : { generate_password: true }),
          keep_alias: psevdonim,
        },
      );
      onDone(otvet.box?.address ?? novyy, otvet.password ?? null);
    } catch (e) {
      toastError(e);
      guard.free();
    }
  };

  return (
    <Modal title={`${t("mbxRename")} · ${box.address}`} onClose={onClose} wide>
      <div className="field-desc" style={{ marginTop: 0, marginBottom: 12 }}>{t("mbxRenameHint")}</div>
      <div className="field">
        <label className="label" htmlFor="mbx-rename-local">{t("mbxLocal")}</label>
        <div className="mbx-input-row">
          <input
            id="mbx-rename-local"
            className="input"
            value={local}
            autoFocus
            onChange={(e) => setLocal(e.target.value)}
          />
          <span className="mbx-at">@</span>
          {server && server.domains.length > 1 ? (
            <select
              className="input mbx-domain"
              aria-label={t("msrvDomain")}
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
            >
              {server.domains.map((d) => (
                <option key={d.id} value={d.domain}>{d.domain}</option>
              ))}
            </select>
          ) : (
            <span className="mbx-domain-text">{domain}</span>
          )}
          <button
            type="button"
            className="btn btn-secondary btn-sm mbx-dice"
            disabled={dumaet}
            title={t("mbxRandom")}
            onClick={() => void sluchaynoe("local")}
          >
            <span aria-hidden="true">🎲</span>
            {t("mbxRandom")}
          </button>
        </div>
      </div>
      <PoleParolya
        id="mbx-rename-password"
        value={password}
        onChange={setPassword}
        zanyat={dumaet}
        onRandom={() => void sluchaynoe("password")}
      />
      <div className="mbx-toggle-row">
        <Toggle on={psevdonim} label={t("mbxKeepAlias")} onToggle={() => setPsevdonim((v) => !v)} />
        <div>
          <div style={{ fontSize: 13 }}>{t("mbxKeepAlias")}</div>
          <div className="field-desc" style={{ marginTop: 2 }}>
            {t("mbxKeepAliasHint", { old: box.address, new: novyy })}
          </div>
        </div>
      </div>
      <div className="mbx-actions">
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button
          className="btn btn-primary btn-sm"
          disabled={guard.busy || !izmenilsya || !local.trim()}
          onClick={() => void pereimenovat()}
        >
          {guard.busy && <Spinner />}
          {t("mbxRename")}
        </button>
      </div>
    </Modal>
  );
}

function PravkaYashchika({ box, onClose, onDone }: { box: Yashchik; onClose: () => void; onDone: () => void }) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [title, setTitle] = useState(box.title);
  const [quota, setQuota] = useState(box.quota_mb ? String(box.quota_mb) : "");
  const [active, setActive] = useState(box.is_active);

  const sokhranit = async () => {
    if (!guard.take()) return;
    try {
      await api.patch(`/mail/admin/boxes/${box.id}`, {
        title: title.trim(),
        quota_mb: quota ? Number(quota) : null,
        is_active: active,
      });
      onDone();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <Modal title={box.address} onClose={onClose}>
      <div className="field">
        <label className="label" htmlFor="mbx-edit-title">{t("mailTitleField")}</label>
        <input id="mbx-edit-title" className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
      </div>
      <div className="field">
        <label className="label" htmlFor="mbx-edit-quota">{t("mbxQuota")}</label>
        <input
          id="mbx-edit-quota"
          className="input"
          inputMode="numeric"
          value={quota}
          placeholder={t("mbxQuotaNone")}
          onChange={(e) => setQuota(e.target.value.replace(/\D/g, ""))}
        />
        <div className="field-desc">{t("mbxQuotaHint")}</div>
      </div>
      <div className="mbx-toggle-row">
        <Toggle on={active} label={t("mailboxActive")} onToggle={() => setActive((v) => !v)} />
        <div>
          <div style={{ fontSize: 13 }}>{t("mailboxActive")}</div>
          <div className="field-desc" style={{ marginTop: 2 }}>{t("mbxActiveHint")}</div>
        </div>
      </div>
      <div className="mbx-actions">
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm" disabled={guard.busy} onClick={() => void sokhranit()}>
          {t("save")}
        </button>
      </div>
    </Modal>
  );
}

function DostupYashchika({ box, onClose, onDone }: { box: Yashchik; onClose: () => void; onDone: () => void }) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [shared, setShared] = useState(box.is_shared);
  const [lyudi, setLyudi] = useState<Dostup[] | null>(null);
  const [failure, setFailure] = useState<unknown>(null);

  const load = useCallback(() => {
    setFailure(null);
    api
      .get(`/mail/admin/boxes/${box.id}/access`)
      .then((r) => setLyudi(vynutSpisok<Dostup>(r)))
      .catch(setFailure);
  }, [box.id]);

  useEffect(load, [load]);

  const sokhranit = async () => {
    if (!lyudi || !guard.take()) return;
    try {
      if (shared !== box.is_shared) await api.patch(`/mail/admin/boxes/${box.id}`, { is_shared: shared });
      await api.put(
        `/mail/admin/boxes/${box.id}/access`,
        lyudi.map((l) => ({ user_id: l.user_id, can_send: l.can_send })),
      );
      onDone();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <Modal title={`${t("mbxAccess")} · ${box.address}`} onClose={onClose} wide>
      {failure !== null ? (
        <LoadFailed error={failure} onRetry={load} />
      ) : lyudi === null ? (
        <Spinner />
      ) : (
        <VyborLyudey shared={shared} setShared={setShared} lyudi={lyudi} setLyudi={setLyudi} />
      )}
      <div className="mbx-actions">
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm" disabled={guard.busy || lyudi === null} onClick={() => void sokhranit()}>
          {t("save")}
        </button>
      </div>
    </Modal>
  );
}

interface Podklyuchenie {
  host: string;
  port: number;
  security: string;
}

interface NastroykiPochty {
  address: string;
  login: string;
  imap: Podklyuchenie;
  smtp: Podklyuchenie;
  submission?: Podklyuchenie | null;
  password_hint?: string;
}

const SHIFROVANIE: Record<string, string> = { ssl: "SSL/TLS", starttls: "STARTTLS", plain: "—" };

const RAZDELY_KLIENTA: { key: "imap" | "smtp" | "submission"; label: TranslationKey }[] = [
  { key: "imap", label: "msrvPhoneIncoming" },
  { key: "smtp", label: "msrvPhoneOutgoing" },
  { key: "submission", label: "mbxCsSubmission" },
];

function NastroykiKlienta({ box, onClose }: { box: Yashchik; onClose: () => void }) {
  const { t } = useApp();
  const [data, setData] = useState<NastroykiPochty | null>(null);
  const [failure, setFailure] = useState<unknown>(null);

  const load = useCallback(() => {
    setFailure(null);
    api.get<NastroykiPochty>(`/mail/admin/boxes/${box.id}/client-settings`).then(setData).catch(setFailure);
  }, [box.id]);

  useEffect(load, [load]);

  const stroka = (label: string, value: string | number, kopirovat = true): ReactNode => (
    <div key={label} className="mbx-kv-row">
      <dt>{label}</dt>
      <dd>
        <code className="mbx-mono">{value}</code>
        {kopirovat && value !== "" && (
          <span className="mbx-copy">
            <CopyButton text={String(value)} />
          </span>
        )}
      </dd>
    </div>
  );

  return (
    <Modal title={`${t("mbxPhone")} · ${box.address}`} onClose={onClose}>
      <div className="field-desc" style={{ marginTop: 0, marginBottom: 12 }}>{t("msrvPhoneHint")}</div>
      {failure !== null ? (
        <LoadFailed error={failure} onRetry={load} />
      ) : data === null ? (
        <Spinner />
      ) : (
        <>
          <dl className="mbx-kv">
            {stroka(t("mailAddress"), data.address)}
            {stroka(t("mbxCsLogin"), data.login)}
          </dl>
          {RAZDELY_KLIENTA.map(({ key, label }) => {
            const r = data[key];
            if (!r) return null;
            return (
              <div key={key} className="mbx-kv-group">
                <div className="label">{t(label)}</div>
                <dl className="mbx-kv">
                  {stroka(t("mbxCsHost"), r.host)}
                  {stroka(t("port"), r.port)}
                  {stroka(t("mbxCsSecurity"), SHIFROVANIE[r.security] ?? r.security, false)}
                </dl>
              </div>
            );
          })}
        </>
      )}
      <div className="field-desc">{t("mbxCsPasswordHint")}</div>
    </Modal>
  );
}

function UdalenieYashchika({ box, onClose, onDone }: { box: Yashchik; onClose: () => void; onDone: () => void }) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [ostavit, setOstavit] = useState(false);
  const [sKopiey, setSKopiey] = useState(false);

  const udalit = async () => {
    if (!guard.take()) return;
    try {
      // Копию — до ящика: после него строки копии теряют ящик, и стереть их
      // по нему уже нельзя.
      if (!ostavit && sKopiey) await api.del(`/mail/archive/boxes/${box.id}`);
      await api.del(`/mail/admin/boxes/${box.id}?keep_data=${ostavit ? 1 : 0}`);
      onDone();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <Modal label={t("confirm")} onClose={onClose}>
      <div style={{ fontSize: 13.5, lineHeight: 1.6, marginBottom: 14 }}>
        {t("mbxDeleteConfirm", { address: box.address })}
      </div>
      <div className="mbx-toggle-row">
        <Toggle on={ostavit} label={t("mbxKeepData")} onToggle={() => setOstavit((v) => !v)} />
        <div style={{ fontSize: 13 }}>{t("mbxKeepData")}</div>
      </div>
      {!ostavit && (
        <div className="mbx-toggle-row">
          <Toggle on={sKopiey} label={t("mbxDeleteArchive")} onToggle={() => setSKopiey((v) => !v)} />
          <div style={{ fontSize: 13, color: sKopiey ? "var(--danger)" : undefined }}>{t("mbxDeleteArchive")}</div>
        </div>
      )}
      <div className="mbx-actions">
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm mbx-danger-solid" disabled={guard.busy} onClick={() => void udalit()}>
          {t("delete")}
        </button>
      </div>
    </Modal>
  );
}

// --- псевдонимы -------------------------------------------------------------

interface Psevdonim {
  id: number;
  server_id: number;
  source: string;
  destination: string;
}

function Psevdonimy() {
  const { t, toastError } = useApp();
  const [servers, setServers] = useState<ServerKratko[] | null>(null);
  const [items, setItems] = useState<Psevdonim[] | null>(null);
  const [boxes, setBoxes] = useState<Yashchik[]>([]);
  const { failure, fail, clear } = useFailure();
  const guard = useGuard();
  const [local, setLocal] = useState("");
  const [domain, setDomain] = useState("");
  const [kuda, setKuda] = useState("");
  const [udalit, setUdalit] = useState<Psevdonim | null>(null);

  const load = useCallback(async () => {
    clear();
    try {
      const s = (await api.get<{ servers: ServerKratko[] }>("/mail/server")).servers.filter(
        (x) => x.status === "ready",
      );
      const [spiski, b] = await Promise.all([
        Promise.all(s.map((x) => api.get(`/mail/admin/aliases?server_id=${x.id}`))),
        api.get("/mail/admin/boxes"),
      ]);
      setServers(s);
      setItems(spiski.flatMap((r) => vynutSpisok<Psevdonim>(r)));
      setBoxes(vynutSpisok<Yashchik>(b).filter((x) => x.server_id !== null && x.server_id !== undefined));
    } catch (e) {
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveTopic("mail", () => void load());

  useEffect(() => {
    if (servers && servers[0] && !domain) setDomain(servers[0].domains[0]?.domain ?? "");
  }, [servers, domain]);

  if (!servers || !items) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  if (servers.length === 0) {
    return (
      <div className="list-card">
        <EmptyState
          icon="mail"
          title={t("mbxNoServerTitle")}
          sub={t("mbxNoServer")}
          action={
            <Link className="btn btn-primary btn-sm" to="/settings/mail-server">
              {t("mbxSetupServer")}
            </Link>
          }
        />
      </div>
    );
  }

  const domeny = servers.flatMap((s) => s.domains.map((d) => ({ server: s.id, domain: d.domain })));
  const serverDomena = domeny.find((d) => d.domain === domain)?.server ?? servers[0].id;

  const dobavit = async () => {
    if (!local.trim() || !kuda.trim() || !guard.take()) return;
    try {
      await api.post("/mail/admin/aliases", {
        server_id: serverDomena,
        source: `${local.trim().toLowerCase()}@${domain}`,
        destination: kuda.trim().toLowerCase(),
      });
      setLocal("");
      await load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const ubrat = async (p: Psevdonim) => {
    if (!guard.take()) return;
    try {
      await api.del(`/mail/admin/aliases/${p.id}`);
      await load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div>
      <div className="field-desc" style={{ marginTop: 0, marginBottom: 12 }}>{t("mbxAliasHint")}</div>
      <form
        className="card card-pad mbx-alias-form"
        onSubmit={(e) => {
          e.preventDefault();
          void dobavit();
        }}
      >
        <div className="field" style={{ marginBottom: 0 }}>
          <label className="label" htmlFor="mbx-alias-local">{t("mbxAliasSource")}</label>
          <div className="mbx-input-row">
            <input
              id="mbx-alias-local"
              className="input"
              value={local}
              placeholder="sales"
              onChange={(e) => setLocal(e.target.value)}
            />
            <span className="mbx-at">@</span>
            {domeny.length > 1 ? (
              <select
                className="input mbx-domain"
                aria-label={t("msrvDomain")}
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
              >
                {domeny.map((d) => (
                  <option key={d.domain} value={d.domain}>{d.domain}</option>
                ))}
              </select>
            ) : (
              <span className="mbx-domain-text">{domain}</span>
            )}
          </div>
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label className="label" htmlFor="mbx-alias-dest">{t("mbxAliasDest")}</label>
          <input
            id="mbx-alias-dest"
            className="input"
            list="mbx-alias-boxes"
            value={kuda}
            placeholder="office@acme.com"
            onChange={(e) => setKuda(e.target.value)}
          />
          <datalist id="mbx-alias-boxes">
            {boxes.map((b) => (
              <option key={b.id} value={b.address} />
            ))}
          </datalist>
        </div>
        <button type="submit" className="btn btn-primary btn-sm" disabled={guard.busy || !local.trim() || !kuda.trim()}>
          <Icon name="plus" size={13} stroke={2} />
          {t("mbxAliasAdd")}
        </button>
      </form>
      <div className="list-card" style={{ marginTop: 14 }}>
        {items.map((p) => (
          <div key={p.id} className="list-row" style={{ height: "auto", padding: "10px 14px" }}>
            <div style={{ flex: 1, minWidth: 0, fontSize: 13 }}>
              <span className="mbx-mono">{p.source}</span>
              <span className="mbx-faint"> → </span>
              <span className="mbx-mono">{p.destination}</span>
            </div>
            <KnopkaKorziny title={t("mbxAliasDelete", { name: p.source })} onClick={() => setUdalit(p)} />
          </div>
        ))}
        {items.length === 0 && <EmptyState icon="mail" title={t("mbxAliasEmpty")} />}
      </div>
      {udalit && (
        <ConfirmModal
          text={t("mbxAliasDeleteConfirm", { name: udalit.source })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void ubrat(udalit)}
          onClose={() => setUdalit(null)}
        />
      )}
    </div>
  );
}

// --- внешние ящики (чужой IMAP/SMTP) ----------------------------------------

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
  /** Ящик своего сервера: живёт на соседней вкладке. */
  server_id?: number | null;
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

function VneshnieYashchiki() {
  const { t, locale, toast, toastError } = useApp();
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Account | null>(null);
  const guard = useGuard();

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      const items: Account[] = (await api.get("/mail/accounts")).items;
      setAccounts(items.filter((a) => a.server_id === null || a.server_id === undefined));
    } catch (e) {
      // Пустой список тут врал: «ящиков нет» вместо «не спросили».
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveTopic("mail", () => void load());

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
    <div>
      <div className="mbx-head">
        <div className="field-desc" style={{ marginTop: 0, flex: 1 }}>{t("mbxExternalSub")}</div>
        <button className="btn btn-primary btn-sm" onClick={() => setDraft({ ...BLANK })}>
          <Icon name="plus" size={13} stroke={2} />
          {t("addMailbox")}
        </button>
      </div>
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
            <KnopkaKorziny onClick={() => setConfirmDelete(account)} />
          </div>
        ))}
        {accounts.length === 0 && <EmptyState icon="email" title={t("noMailboxesYet")} />}
      </div>

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
          {/* Действие у переключателя своё, а не только у обёртки: `Toggle`
              останавливает всплытие, и пустой `onToggle` делал переключатель
              мёртвым именно там, куда Tab приводит фокус. */}
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
          text={t("deleteMailboxConfirm", { name: confirmDelete.title || confirmDelete.address })}
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
          {/* Своё действие, а не только у обёртки: с клавиатуры иначе нельзя
              было снять шифрование там, где сервер его не держит. Имя — вместе
              с сервером: «SSL» без «IMAP» ни о чём не говорит. */}
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
