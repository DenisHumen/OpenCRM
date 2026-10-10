import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { CopyButton } from "../components/CopyButton";
import { Icon } from "../components/Icon";
import {
  Chip,
  ConfirmModal,
  KnopkaKorziny,
  LoadFailed,
  Modal,
  ScreenLoading,
  Spinner,
  Toggle,
} from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { fileSize, formatDateTime } from "../lib/format";
import { useGuard } from "../lib/guard";
import type { TranslationKey } from "../lib/i18n";
import { useLiveTopic } from "../lib/live";

/**
 * Свой почтовый сервер: мастер установки и карточка готового сервера.
 *
 * Разбор — docs/bloki/32-pochtovyy-server.md. Экран для того, кто в почте не
 * разбирается вовсе: каждый шаг проверки назван словами, у каждой беды —
 * готовое действие, а журнал установки идёт в том же окне, где нажали кнопку.
 */

type ShagSostoyanie = "ok" | "warn" | "fail" | "running" | "skip" | "pending";

interface Instrukciya {
  /** Код беды установщика, к которой инструкция. */
  code?: string;
  title_ru: string;
  title_en: string;
  steps_ru: string[];
  steps_en: string[];
}

interface Shag {
  code: string;
  status: ShagSostoyanie;
  detail?: string;
  /** Инструкция к беде шага; строкой — голый код беды без готовой инструкции. */
  hint?: Instrukciya | string | null;
}

interface Rabota {
  id: number;
  server_id: number;
  kind: "check" | "install" | "adopt" | "remove";
  status: "running" | "ok" | "failed" | "rolled_back";
  steps: Shag[];
  error_code: string;
  error_detail: string;
  hint?: Instrukciya | null;
  started_at: string | null;
  finished_at: string | null;
}

interface DnsZapis {
  type: string;
  name: string;
  expected: string;
  found: string[];
  status: "ok" | "missing" | "wrong" | "warn";
  required: boolean;
  /** Что сделать — на двух языках; строкой — от старых ответов. */
  hint?: { ru: string; en: string } | string | null;
}

interface DnsDomena {
  domain: string;
  records: DnsZapis[];
}

interface Domen {
  id: number;
  domain: string;
  dkim_selector: string;
  dkim_txt: string;
  dns_checked_at: string | null;
  dns_report: unknown;
}

interface Server {
  id: number;
  title: string;
  mode: "local" | "ssh";
  status: "draft" | "installing" | "ready" | "failed";
  hostname: string;
  public_ip: string;
  ssh_host: string;
  ssh_port: number;
  ssh_user: string;
  has_ssh_key: boolean;
  relay_host: string;
  relay_port: number;
  relay_user: string;
  has_relay_password: boolean;
  installed_at: string | null;
  checked_at: string | null;
  last_error: string | null;
  domains: Domen[];
  boxes_count: number;
}

interface Spisok {
  servers: Server[];
  obnovlyator: { zhiv: boolean; at: string | null };
}

interface Zdorove {
  services?: Record<string, boolean>;
  cert?: { not_after?: string | null; self_signed?: boolean; sha256?: string };
  queue?: number;
}

/** Раз в сколько спрашивать журнал идущей работы. */
const OPROS_MS = 1500;

/** Команда для того, у кого обновлятор на машине не запущен. */
const KOMANDA_RUKAMI = "./opencrm.sh mail install";

const SHAG_PODPIS: Record<string, TranslationKey> = {
  os: "msrvShagOs",
  privileges: "msrvShagPrivileges",
  docker: "msrvShagDocker",
  compose: "msrvShagCompose",
  ports: "msrvShagPorts",
  resources: "msrvShagResources",
  egress25: "msrvShagEgress25",
  mta: "msrvShagMta",
  existing: "msrvShagExisting",
  public_ip: "msrvShagPublicIp",
  port80: "msrvShagPort80",
  docker_install: "msrvShagDockerInstall",
  image: "msrvShagImage",
  dirs: "msrvShagDirs",
  tls: "msrvShagTls",
  config: "msrvShagConfig",
  accounts: "msrvShagAccounts",
  start: "msrvShagStart",
  healthy: "msrvShagHealthy",
  dkim: "msrvShagDkim",
  firewall: "msrvShagFirewall",
  selfcheck: "msrvShagSelfcheck",
  data: "msrvShagData",
  stop: "msrvShagStop",
  rollback: "msrvShagRollback",
  updater: "msrvShagUpdater",
  agent: "msrvShagAgent",
  master: "msrvShagMaster",
  find: "msrvShagFind",
  import: "msrvShagImport",
  dns: "msrvDnsTitle",
};

/** Беды установщика (`##FAIL <шаг> <беда>`) — одной простой фразой у шага. */
const BEDA_PODPIS: Record<string, TranslationKey> = {
  os_unsupported: "msrvBedaOsUnsupported",
  no_privileges: "msrvBedaNoPrivileges",
  sudo_password_needed: "msrvBedaSudoPasswordNeeded",
  docker_missing: "msrvBedaDockerMissing",
  docker_install_failed: "msrvBedaDockerInstallFailed",
  compose_missing: "msrvBedaComposeMissing",
  port_busy: "msrvBedaPortBusy",
  low_memory: "msrvBedaLowMemory",
  low_disk: "msrvBedaLowDisk",
  egress25_blocked: "msrvBedaEgress25Blocked",
  mta_running: "msrvBedaMtaRunning",
  dms_exists: "msrvBedaDmsExists",
  image_pull_failed: "msrvBedaImagePullFailed",
  tls_failed: "msrvBedaTlsFailed",
  start_failed: "msrvBedaStartFailed",
  unhealthy: "msrvBedaUnhealthy",
  dkim_failed: "msrvBedaDkimFailed",
  firewall_failed: "msrvBedaFirewallFailed",
  dms_not_found: "msrvBedaDmsNotFound",
  mail_updater_offline: "errMailUpdaterOffline",
  ssh_sudo_needed: "errSshSudoNeeded",
};

function kodBedy(hint: Shag["hint"]): string {
  if (!hint) return "";
  return typeof hint === "string" ? hint : hint.code ?? "";
}

const SOSTOYANIE_SHAGA: Record<ShagSostoyanie, TranslationKey> = {
  ok: "msrvStOk",
  warn: "msrvStWarn",
  fail: "msrvStFail",
  running: "msrvStRunning",
  skip: "msrvStSkip",
  pending: "msrvStPending",
};

const VID_RABOTY: Record<Rabota["kind"], TranslationKey> = {
  check: "msrvKindCheck",
  install: "msrvKindInstall",
  adopt: "msrvKindAdopt",
  remove: "msrvKindRemove",
};

const ITOG_RABOTY: Record<Rabota["status"], TranslationKey> = {
  running: "msrvJobRunning",
  ok: "msrvJobOk",
  failed: "msrvJobFailed",
  rolled_back: "msrvJobRolledBack",
};

const SOSTOYANIE_SERVERA: Record<Server["status"], TranslationKey> = {
  draft: "msrvStatusDraft",
  installing: "msrvStatusInstalling",
  ready: "msrvStatusReady",
  failed: "msrvStatusFailed",
};

const DNS_PODPIS: Record<DnsZapis["status"], TranslationKey> = {
  ok: "msrvDnsOk",
  missing: "msrvDnsMissing",
  wrong: "msrvDnsWrong",
  warn: "msrvDnsWarn",
};

/** Ответ опроса работы: `{job, log_chunk, log_size}`; терпим и поля работы в корне. */
function vynutRabotu(otvet: { job?: Rabota } & Partial<Rabota>): Rabota {
  return (otvet.job ?? otvet) as Rabota;
}

/** Список работ: массив или `{items}` — ручка пишется параллельно с экраном. */
function vynutSpisok<T>(otvet: unknown): T[] {
  if (Array.isArray(otvet)) return otvet as T[];
  const o = otvet as { items?: T[]; jobs?: T[] } | null;
  return o?.items ?? o?.jobs ?? [];
}

/** Записи DNS из сохранённого отчёта домена: объект `{records}` (или строка JSON). */
function zapisiOtchyota(report: unknown): DnsZapis[] {
  let o = report;
  if (typeof o === "string") {
    try {
      o = JSON.parse(o);
    } catch {
      return [];
    }
  }
  if (Array.isArray(o)) return o as DnsZapis[];
  const r = (o as { records?: DnsZapis[] } | null)?.records;
  return Array.isArray(r) ? r : [];
}

function kudaSsh(s: Server): string {
  return `${s.ssh_user || "root"}@${s.ssh_host}${s.ssh_port && s.ssh_port !== 22 ? ":" + s.ssh_port : ""}`;
}

export function MailServer() {
  const { t } = useApp();
  const [data, setData] = useState<Spisok | null>(null);
  const { failure, fail, clear } = useFailure();
  // Сервер, мастер которого держим на экране и после «готово»: иначе живой
  // намёк перерисует его карточкой, и пароль первого ящика пропадёт непрочитанным.
  const [master, setMaster] = useState<number | null>(null);
  const [novyy, setNovyy] = useState(false);
  // Сервер, который снимают с машины: окно журнала живёт здесь, а не в карточке.
  // Удаление переводит запись в черновик посреди работы, и живой намёк иначе
  // размонтировал бы карточку вместе с окном, показав мастер того же сервера.
  const [snimaem, setSnimaem] = useState<Server | null>(null);

  const load = useCallback(() => {
    clear();
    api.get<Spisok>("/mail/server").then(setData).catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);
  useLiveTopic("mail", load);

  if (!data) return <ScreenLoading error={failure} onRetry={load} />;

  const zakreplyon = (s: Server) => s.id === snimaem?.id;
  const gotovye = data.servers.filter((s) => (s.status === "ready" || zakreplyon(s)) && s.id !== master);
  const nedodelannye = data.servers.filter((s) => (s.status !== "ready" && !zakreplyon(s)) || s.id === master);
  const pokazatNovyy = (data.servers.length === 0 && !snimaem) || novyy;

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div>
          <h1 className="page-title">{t("msrvTitle")}</h1>
          <div className="page-sub">{t("msrvSub")}</div>
        </div>
        {!pokazatNovyy && nedodelannye.length === 0 && (
          <button className="btn btn-secondary msrv-nowrap" onClick={() => setNovyy(true)}>
            <Icon name="plus" stroke={2} />
            {t("msrvAddServer")}
          </button>
        )}
      </div>

      {gotovye.map((s) => (
        <KartochkaServera key={s.id} server={s} onChanged={load} onRemove={() => setSnimaem(s)} />
      ))}
      {nedodelannye.map((s) => (
        <Master
          key={s.id}
          server={s}
          obnovlyator={data.obnovlyator}
          onChanged={load}
          onPin={setMaster}
          onFinish={() => {
            setMaster(null);
            load();
          }}
        />
      ))}
      {pokazatNovyy && (
        <Master
          server={null}
          obnovlyator={data.obnovlyator}
          onChanged={() => {
            setNovyy(false);
            load();
          }}
          onPin={setMaster}
          onFinish={() => setNovyy(false)}
          onCancel={data.servers.length > 0 ? () => setNovyy(false) : undefined}
        />
      )}
      {snimaem && (
        <UdalenieServera
          server={snimaem}
          onChanged={load}
          onClose={() => {
            setSnimaem(null);
            load();
          }}
        />
      )}
    </div>
  );
}

// --- мастер -----------------------------------------------------------------

type MasterShag = "where" | "domain" | "ssh" | "check" | "install";

function Master({
  server,
  obnovlyator,
  onChanged,
  onPin,
  onFinish,
  onCancel,
}: {
  server: Server | null;
  obnovlyator: Spisok["obnovlyator"];
  onChanged: () => void;
  onPin: (id: number | null) => void;
  onFinish: () => void;
  onCancel?: () => void;
}) {
  const { t, toastError } = useApp();
  const [mode, setMode] = useState<Server["mode"]>(server?.mode ?? "local");
  const [shag, setShag] = useState<MasterShag>(() => {
    if (!server) return "where";
    if (server.mode === "ssh" && !server.has_ssh_key) return "ssh";
    return server.status === "installing" ? "install" : "check";
  });
  // Работа, за которой следим: последняя у сервера, пока человек не начал новую.
  const [rabotaId, setRabotaId] = useState<number | null>(null);
  const [parol, setParol] = useState<{ address: string; password: string } | null>(null);
  const [udalit, setUdalit] = useState(false);
  const guard = useGuard();

  // Вернулись к недоделанному серверу — показываем, чем кончилась последняя работа.
  useEffect(() => {
    if (!server) return;
    let zhivo = true;
    api
      .get(`/mail/server/${server.id}/jobs`)
      .then((otvet) => {
        if (!zhivo) return;
        const posledniy = vynutSpisok<Rabota>(otvet)[0];
        if (!posledniy) return;
        setRabotaId(posledniy.id);
        if (posledniy.kind === "install" || posledniy.kind === "adopt") setShag("install");
        else if (posledniy.kind === "check" && !(server.mode === "ssh" && !server.has_ssh_key)) setShag("check");
      })
      .catch(() => undefined);
    return () => {
      zhivo = false;
    };
    // Только при первом показе: дальше шаг ведёт человек.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [server?.id]);

  const shagi: { key: MasterShag; label: TranslationKey }[] = [
    { key: "where", label: "msrvStepWhere" },
    { key: "domain", label: "msrvStepDomain" },
    ...(mode === "ssh" ? [{ key: "ssh" as MasterShag, label: "msrvStepSsh" as TranslationKey }] : []),
    { key: "check", label: "msrvStepCheck" },
    { key: "install", label: "msrvStepInstall" },
  ];
  const nomer = shagi.findIndex((s) => s.key === shag);

  const udalitChernovik = async () => {
    if (!server || !guard.take()) return;
    try {
      await api.del(`/mail/server/${server.id}`);
      onPin(null);
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="card card-pad msrv-master" style={{ marginBottom: 18 }}>
      <ol className="msrv-stepper" aria-label={t("msrvTitle")}>
        {shagi.map((s, i) => {
          const gde = i === nomer ? "now" : i < nomer ? "passed" : "ahead";
          return (
            <li key={s.key} className={"step " + gde} aria-current={gde === "now" ? "step" : undefined}>
              <span className="step-circle">
                {gde === "passed" ? <Icon name="check" size={14} stroke={2} /> : i + 1}
              </span>
              <span className="step-title">{t(s.label)}</span>
            </li>
          );
        })}
      </ol>

      {server && (
        <div className="msrv-master-head">
          <span>
            <strong>{server.hostname || server.title}</strong>
            {server.mode === "ssh" && <span className="msrv-faint"> · {kudaSsh(server)}</span>}
          </span>
          <Chip variant={server.status === "failed" ? "danger" : undefined}>
            {t(SOSTOYANIE_SERVERA[server.status] ?? "msrvStatusDraft")}
          </Chip>
          {server.status !== "ready" && server.status !== "installing" && (
            <button type="button" className="text-link danger" style={{ marginLeft: "auto" }} onClick={() => setUdalit(true)}>
              {t("msrvDeleteDraft")}
            </button>
          )}
        </div>
      )}

      {shag === "where" && (
        <ShagGde
          mode={mode}
          setMode={setMode}
          obnovlyator={obnovlyator}
          onNext={() => setShag("domain")}
          onCancel={onCancel}
        />
      )}
      {shag === "domain" && (
        <ShagDomen
          mode={mode}
          onBack={() => setShag("where")}
          onCreated={onChanged}
        />
      )}
      {shag === "ssh" && server && (
        <ShagSsh server={server} onNext={() => { onChanged(); setShag("check"); }} />
      )}
      {shag === "check" && server && (
        <ShagProverka
          server={server}
          obnovlyator={obnovlyator}
          rabotaId={rabotaId}
          setRabotaId={setRabotaId}
          onBack={server.mode === "ssh" ? () => setShag("ssh") : undefined}
          onNext={() => { setRabotaId(null); setShag("install"); }}
          onAdopt={(id) => { onPin(server.id); setRabotaId(id); setShag("install"); }}
        />
      )}
      {shag === "install" && server && (
        <ShagUstanovka
          server={server}
          rabotaId={rabotaId}
          setRabotaId={setRabotaId}
          parol={parol}
          setParol={setParol}
          onStarted={() => onPin(server.id)}
          onBack={() => { setRabotaId(null); setShag("check"); }}
          onChanged={onChanged}
          onFinish={onFinish}
        />
      )}

      {udalit && server && (
        <ConfirmModal
          text={t("msrvDeleteDraftConfirm", { name: server.hostname || server.title })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void udalitChernovik()}
          onClose={() => setUdalit(false)}
        />
      )}
    </div>
  );
}

function ShagGde({
  mode,
  setMode,
  obnovlyator,
  onNext,
  onCancel,
}: {
  mode: Server["mode"];
  setMode: (m: Server["mode"]) => void;
  obnovlyator: Spisok["obnovlyator"];
  onNext: () => void;
  onCancel?: () => void;
}) {
  const { t } = useApp();
  const varianty: { key: Server["mode"]; title: TranslationKey; desc: TranslationKey; icon: string }[] = [
    { key: "local", title: "msrvModeLocal", desc: "msrvModeLocalDesc", icon: "database" },
    { key: "ssh", title: "msrvModeSsh", desc: "msrvModeSshDesc", icon: "link" },
  ];
  return (
    <div>
      <div className="msrv-q">{t("msrvWhereQ")}</div>
      <div className="msrv-choice" role="radiogroup" aria-label={t("msrvWhereQ")}>
        {varianty.map((v) => (
          <button
            key={v.key}
            type="button"
            role="radio"
            aria-checked={mode === v.key}
            className={"msrv-choice-item" + (mode === v.key ? " active" : "")}
            onClick={() => setMode(v.key)}
          >
            <Icon name={v.icon} size={18} />
            <span className="msrv-choice-title">{t(v.title)}</span>
            <span className="msrv-choice-desc">{t(v.desc)}</span>
          </button>
        ))}
      </div>
      {mode === "local" && !obnovlyator.zhiv && <ObnovlyatorMolchit />}
      <div className="msrv-actions">
        {onCancel && (
          <button className="btn btn-secondary btn-sm" onClick={onCancel}>
            {t("cancel")}
          </button>
        )}
        <button className="btn btn-primary btn-sm" onClick={onNext}>
          {t("msrvNext")}
          <Icon name="chevronRight" size={13} />
        </button>
      </div>
    </div>
  );
}

/** Обновлятор на хосте не отвечает: CRM сама ничего не поставит — даём команду. */
function ObnovlyatorMolchit() {
  const { t } = useApp();
  return (
    <div className="msrv-note msrv-note-warn">
      <Icon name="alert" size={15} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div>{t("msrvUpdaterOff")}</div>
        <div className="docs-code-wrap token-kod" style={{ marginTop: 8 }}>
          <pre className="code-block">{KOMANDA_RUKAMI}</pre>
          <CopyButton text={KOMANDA_RUKAMI} />
        </div>
      </div>
    </div>
  );
}

function ShagDomen({
  mode,
  onBack,
  onCreated,
}: {
  mode: Server["mode"];
  onBack: () => void;
  onCreated: () => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [domain, setDomain] = useState("");
  const [hostname, setHostname] = useState("");
  const [hostTronut, setHostTronut] = useState(false);
  const [title, setTitle] = useState("");
  const [sshHost, setSshHost] = useState("");
  const [sshPort, setSshPort] = useState("22");
  const [sshUser, setSshUser] = useState("root");

  const chistyy = domain.trim().toLowerCase().replace(/^@/, "");
  const gotovo = chistyy.includes(".") && hostname.trim() !== "" && (mode === "local" || sshHost.trim() !== "");

  const sozdat = async () => {
    if (!gotovo || !guard.take()) return;
    try {
      await api.post("/mail/server", {
        mode,
        title: title.trim() || undefined,
        hostname: hostname.trim().toLowerCase(),
        domain: chistyy,
        ...(mode === "ssh"
          ? { ssh_host: sshHost.trim(), ssh_port: Number(sshPort) || 22, ssh_user: sshUser.trim() || "root" }
          : {}),
      });
      onCreated();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void sozdat();
      }}
    >
      <Pole id="msrv-domain" label={t("msrvDomain")} desc={t("msrvDomainHint")}>
        <input
          id="msrv-domain"
          className="input"
          value={domain}
          placeholder="acme.com"
          autoFocus
          onChange={(e) => {
            setDomain(e.target.value);
            const d = e.target.value.trim().toLowerCase().replace(/^@/, "");
            if (!hostTronut) setHostname(d ? `mail.${d}` : "");
          }}
        />
      </Pole>
      <Pole id="msrv-hostname" label={t("msrvHostname")} desc={t("msrvHostnameHint")}>
        <input
          id="msrv-hostname"
          className="input"
          value={hostname}
          placeholder="mail.acme.com"
          onChange={(e) => {
            setHostTronut(true);
            setHostname(e.target.value);
          }}
        />
      </Pole>
      {mode === "ssh" && (
        <div className="msrv-grid3">
          <Pole id="msrv-ssh-host" label={t("msrvSshHost")} desc={t("msrvSshHostHint")}>
            <input
              id="msrv-ssh-host"
              className="input"
              value={sshHost}
              placeholder="203.0.113.10"
              onChange={(e) => setSshHost(e.target.value)}
            />
          </Pole>
          <Pole id="msrv-ssh-port" label={t("msrvSshPort")}>
            <input
              id="msrv-ssh-port"
              className="input"
              inputMode="numeric"
              value={sshPort}
              onChange={(e) => setSshPort(e.target.value.replace(/\D/g, ""))}
            />
          </Pole>
          <Pole id="msrv-ssh-user" label={t("msrvSshUser")} desc={t("msrvSshUserHint")}>
            <input
              id="msrv-ssh-user"
              className="input"
              value={sshUser}
              onChange={(e) => setSshUser(e.target.value)}
            />
          </Pole>
        </div>
      )}
      <Pole id="msrv-title" label={t("msrvServerTitle")}>
        <input id="msrv-title" className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
      </Pole>
      <div className="msrv-actions">
        <button type="button" className="btn btn-secondary btn-sm" onClick={onBack}>
          {t("back")}
        </button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!gotovo || guard.busy}>
          {t("msrvCreate")}
          <Icon name="chevronRight" size={13} />
        </button>
      </div>
    </form>
  );
}

interface Otpechatok {
  fingerprint: string;
  key_type: string;
  key_line: string;
  known?: boolean;
}

interface Podklyuchenie {
  ok: boolean;
  key_installed: boolean;
  docker: unknown;
  sudo: unknown;
}

function ShagSsh({ server, onNext }: { server: Server; onNext: () => void }) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [kak, setKak] = useState<"password" | "key">("password");
  const [user, setUser] = useState(server.ssh_user || "root");
  const [password, setPassword] = useState("");
  const [klyuch, setKlyuch] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [sudoHranit, setSudoHranit] = useState(false);
  const [sudo, setSudo] = useState("");
  const [otpechatok, setOtpechatok] = useState<Otpechatok | null>(null);
  const [itog, setItog] = useState<Podklyuchenie | null>(null);

  const gotovo = user.trim() !== "" && (kak === "password" ? password !== "" : klyuch.trim() !== "");

  // Шаг 1 из двух: сервер представляется отпечатком, человек его подтверждает.
  // Тот же ключ, что подтверждён раньше, второй раз не спрашиваем.
  const sprosit = async () => {
    if (!gotovo || !guard.take()) return;
    let otp: Otpechatok | null = null;
    try {
      otp = await api.post<Otpechatok>(`/mail/server/${server.id}/ssh/probe`, {});
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
    if (!otp) return;
    if (otp.known) void podklyuchit(otp);
    else setOtpechatok(otp);
  };

  const podklyuchit = async (otp: Otpechatok) => {
    if (!guard.take()) return;
    try {
      const otvet = await api.post<Podklyuchenie>(`/mail/server/${server.id}/ssh/connect`, {
        host_key_line: otp.key_line,
        user: user.trim(),
        ...(kak === "password"
          ? { password }
          : { private_key: klyuch, passphrase: passphrase || undefined }),
        keep_sudo: sudoHranit,
        sudo_password: sudoHranit && sudo ? sudo : undefined,
      });
      setOtpechatok(null);
      // Пароль больше не нужен: на сервере уже лежит ключ CRM.
      setPassword("");
      setKlyuch("");
      setPassphrase("");
      setSudo("");
      setItog(otvet);
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  if (itog) return <ItogSsh itog={itog} onNext={onNext} onRetry={() => setItog(null)} />;

  return (
    <div>
      {server.has_ssh_key && (
        <div className="msrv-note msrv-note-ok">
          <Icon name="check" size={15} stroke={2} />
          <div style={{ flex: 1 }}>{t("msrvConnected")}</div>
          <button className="btn btn-primary btn-sm" onClick={onNext}>
            {t("msrvNext")}
          </button>
        </div>
      )}
      <div className="msrv-q">{t("msrvSshAs", { target: kudaSsh(server) })}</div>
      <Pole id="msrv-ssh-login" label={t("msrvSshUser")} desc={t("msrvSshUserHint")}>
        <input id="msrv-ssh-login" className="input" value={user} onChange={(e) => setUser(e.target.value)} />
      </Pole>
      <div className="label">{t("msrvSshHow")}</div>
      <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
        <button
          type="button"
          className={"option-chip" + (kak === "password" ? " active" : "")}
          aria-pressed={kak === "password"}
          onClick={() => setKak("password")}
        >
          {t("msrvSshByPassword")}
        </button>
        <button
          type="button"
          className={"option-chip" + (kak === "key" ? " active" : "")}
          aria-pressed={kak === "key"}
          onClick={() => setKak("key")}
        >
          {t("msrvSshByKey")}
        </button>
      </div>
      {kak === "password" ? (
        <Pole id="msrv-ssh-password" label={t("msrvSshPassword")} desc={t("msrvSshPasswordHint")}>
          <input
            id="msrv-ssh-password"
            className="input"
            type="password"
            autoComplete="off"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Pole>
      ) : (
        <>
          <Pole id="msrv-ssh-key" label={t("msrvSshKey")} desc={t("msrvSshKeyHint")}>
            <textarea
              id="msrv-ssh-key"
              className="input msrv-key"
              rows={5}
              spellCheck={false}
              value={klyuch}
              placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
              onChange={(e) => setKlyuch(e.target.value)}
            />
          </Pole>
          <Pole id="msrv-ssh-pass" label={t("msrvSshPassphrase")}>
            <input
              id="msrv-ssh-pass"
              className="input"
              type="password"
              autoComplete="off"
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
            />
          </Pole>
        </>
      )}
      <div className="msrv-toggle-row">
        <Toggle on={sudoHranit} label={t("msrvSudoKeep")} onToggle={() => setSudoHranit((v) => !v)} />
        <div>
          <div style={{ fontSize: 13 }}>{t("msrvSudoKeep")}</div>
          <div className="field-desc" style={{ marginTop: 2 }}>{t("msrvSudoKeepHint")}</div>
        </div>
      </div>
      {sudoHranit && (
        <Pole id="msrv-sudo" label={t("msrvSudoPassword")}>
          <input
            id="msrv-sudo"
            className="input"
            type="password"
            autoComplete="off"
            value={sudo}
            onChange={(e) => setSudo(e.target.value)}
          />
        </Pole>
      )}
      <div className="msrv-actions">
        <button className="btn btn-primary btn-sm" disabled={!gotovo || guard.busy} onClick={() => void sprosit()}>
          {guard.busy && <Spinner />}
          {server.has_ssh_key ? t("msrvReconnect") : t("msrvConnect")}
        </button>
      </div>

      {otpechatok && (
        <Modal title={t("msrvFingerprintTitle")} onClose={() => setOtpechatok(null)}>
          <div style={{ fontSize: 13, lineHeight: 1.6 }}>{t("msrvFingerprintText")}</div>
          <div className="docs-code-wrap token-kod" style={{ marginTop: 10 }}>
            <pre className="code-block">{`${otpechatok.key_type}\n${otpechatok.fingerprint}`}</pre>
            <CopyButton text={otpechatok.fingerprint} />
          </div>
          <div className="docs-code-wrap token-kod" style={{ marginTop: 10 }}>
            <pre className="code-block">{"ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"}</pre>
            <CopyButton text="ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub" />
          </div>
          <div className="msrv-actions">
            <button className="btn btn-secondary btn-sm" onClick={() => setOtpechatok(null)}>
              {t("cancel")}
            </button>
            <button className="btn btn-primary btn-sm" disabled={guard.busy} onClick={() => void podklyuchit(otpechatok)}>
              {guard.busy && <Spinner />}
              {t("msrvFingerprintYes")}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

type ChipVid = "success" | "warning" | "danger";

/** Что сервер узнал о Docker (`mail_ssh.razvedat`): строка всегда непустая, судить по значению. */
const DOCKER_ITOG: Record<string, { vid: ChipVid; podpis: TranslationKey }> = {
  ok: { vid: "success", podpis: "msrvDockerOk" },
  missing: { vid: "warning", podpis: "msrvDockerMissing" },
  noaccess: { vid: "warning", podpis: "msrvDockerNoAccess" },
};

const SUDO_ITOG: Record<string, { vid: ChipVid; podpis: TranslationKey }> = {
  root: { vid: "success", podpis: "msrvSudoRoot" },
  nopasswd: { vid: "success", podpis: "msrvSudoNopasswd" },
  password_ok: { vid: "success", podpis: "msrvSudoPasswordOk" },
  password_bad: { vid: "danger", podpis: "msrvSudoPasswordBad" },
  password: { vid: "warning", podpis: "msrvSudoPasswordAsk" },
  none: { vid: "warning", podpis: "msrvSudoNone" },
};

function ItogSsh({ itog, onNext, onRetry }: { itog: Podklyuchenie; onNext: () => void; onRetry: () => void }) {
  const { t } = useApp();
  const docker = DOCKER_ITOG[String(itog.docker)];
  const sudo = SUDO_ITOG[String(itog.sudo)];
  // Ставить можно, если Docker пускает сам или есть права его поставить и позвать.
  const prava = sudo?.vid === "success";
  const sudoNeveren = itog.sudo === "password_bad";
  const netPrav = docker?.vid !== "success" && !prava && !sudoNeveren;
  const beda = sudoNeveren || netPrav;
  return (
    <div className={"msrv-note " + (beda ? "msrv-note-warn" : "msrv-note-ok")}>
      <Icon name={beda ? "alert" : "check"} size={15} stroke={2} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div>{t("msrvConnected")}</div>
        <div className="msrv-chips">
          <Chip variant={itog.key_installed ? "success" : "warning"}>{t("msrvKeyInstalled")}</Chip>
          <Chip variant={docker?.vid}>
            {t("msrvDockerFound")}: {docker ? t(docker.podpis) : String(itog.docker ?? "—")}
          </Chip>
          <Chip variant={sudo?.vid}>
            {t("msrvSudoFound")}: {sudo ? t(sudo.podpis) : String(itog.sudo ?? "—")}
          </Chip>
        </div>
        {sudoNeveren && <div className="field-desc">{t("msrvSudoBadHint")}</div>}
        {netPrav && <div className="field-desc">{t("msrvNeedRightsHint")}</div>}
      </div>
      <div className="msrv-actions" style={{ marginTop: 0 }}>
        {beda && (
          <button className="btn btn-secondary btn-sm" onClick={onRetry}>
            {t("msrvReconnect")}
          </button>
        )}
        <button className="btn btn-primary btn-sm" onClick={onNext}>
          {t("msrvNext")}
        </button>
      </div>
    </div>
  );
}

function ShagProverka({
  server,
  obnovlyator,
  rabotaId,
  setRabotaId,
  onBack,
  onNext,
  onAdopt,
}: {
  server: Server;
  obnovlyator: Spisok["obnovlyator"];
  rabotaId: number | null;
  setRabotaId: (id: number | null) => void;
  onBack?: () => void;
  onNext: () => void;
  onAdopt: (jobId: number) => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const rabota = useRabota(rabotaId);
  const [dns, setDns] = useState<DnsDomena[] | null>(null);
  const [dnsZanyat, setDnsZanyat] = useState(false);

  const zapustit = async () => {
    if (!guard.take()) return;
    try {
      const job = await api.post<Rabota>(`/mail/server/${server.id}/check`, {
        allow_docker_install: server.mode === "ssh",
      });
      setRabotaId(vynutRabotu(job).id);
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const proveritDns = async () => {
    if (!guard.take()) return;
    setDnsZanyat(true);
    try {
      setDns((await api.post<{ domains: DnsDomena[] }>(`/mail/server/${server.id}/dns`, {})).domains);
    } catch (e) {
      toastError(e);
    } finally {
      setDnsZanyat(false);
      guard.free();
    }
  };

  const podklyuchitSushchestvuyushchiy = async () => {
    if (!guard.take()) return;
    try {
      onAdopt(vynutRabotu(await api.post<Rabota>(`/mail/server/${server.id}/adopt`, {})).id);
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const job = rabota.job && rabota.job.kind === "check" ? rabota.job : null;
  const idyot = job?.status === "running";
  const proshla = job?.status === "ok";
  const preduprezhdeniya = !!job?.steps.some((s) => s.status === "warn");
  const najdenSushchestvuyushchiy =
    job?.error_code === "dms_exists" || !!job?.steps.some((s) => kodBedy(s.hint) === "dms_exists");

  // DNS после удачной проверки — сам: таблица записей и есть то, что человеку
  // делать дальше, и искать её кнопкой незачем.
  useEffect(() => {
    if (proshla) void proveritDns();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proshla]);

  return (
    <div>
      <div className="msrv-q">{t("msrvCheckIntro")}</div>
      {server.mode === "local" && !obnovlyator.zhiv && <ObnovlyatorMolchit />}

      {rabotaId !== null && <ZhurnalRaboty rabota={rabota} />}

      {proshla && (
        <div className={"msrv-note " + (preduprezhdeniya ? "msrv-note-warn" : "msrv-note-ok")}>
          <Icon name={preduprezhdeniya ? "alert" : "check"} size={15} stroke={2} />
          <div>{t(preduprezhdeniya ? "msrvCheckWarn" : "msrvCheckOk")}</div>
        </div>
      )}
      {najdenSushchestvuyushchiy && (
        <div className="msrv-note msrv-note-info">
          <Icon name="info" size={15} />
          <div style={{ flex: 1 }}>{t("msrvAdoptHint")}</div>
          <button className="btn btn-primary btn-sm" disabled={guard.busy} onClick={() => void podklyuchitSushchestvuyushchiy()}>
            {t("msrvAdopt")}
          </button>
        </div>
      )}

      {(dns || proshla) && (
        <TablitsaDns
          domains={dns}
          zanyat={dnsZanyat}
          onRecheck={() => void proveritDns()}
        />
      )}

      <div className="msrv-actions">
        {onBack && (
          <button className="btn btn-secondary btn-sm" disabled={idyot} onClick={onBack}>
            {t("back")}
          </button>
        )}
        {!proshla && (
          <button className="btn btn-secondary btn-sm" disabled={guard.busy || idyot} onClick={() => void proveritDns()}>
            {t("msrvDnsCheck")}
          </button>
        )}
        <button
          className={"btn btn-sm " + (proshla ? "btn-secondary" : "btn-primary")}
          disabled={guard.busy || idyot}
          onClick={() => void zapustit()}
        >
          {idyot && <Spinner />}
          {job ? t("msrvCheckAgain") : t("msrvRunCheck")}
        </button>
        {proshla && (
          <button className="btn btn-primary btn-sm" onClick={onNext}>
            {t("msrvGoInstall")}
            <Icon name="chevronRight" size={13} />
          </button>
        )}
      </div>
    </div>
  );
}

function ShagUstanovka({
  server,
  rabotaId,
  setRabotaId,
  parol,
  setParol,
  onStarted,
  onBack,
  onChanged,
  onFinish,
}: {
  server: Server;
  rabotaId: number | null;
  setRabotaId: (id: number | null) => void;
  parol: { address: string; password: string } | null;
  setParol: (p: { address: string; password: string } | null) => void;
  onStarted: () => void;
  onBack: () => void;
  onChanged: () => void;
  onFinish: () => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const rabota = useRabota(rabotaId);
  const domen = server.domains[0]?.domain ?? "";
  const [local, setLocal] = useState("admin");
  const [svoy, setSvoy] = useState(false);
  const [password, setPassword] = useState("");
  const [acme, setAcme] = useState("");
  const [docker, setDocker] = useState(true);
  const [samopodpis, setSamopodpis] = useState(false);
  const [dns, setDns] = useState<DnsDomena[] | null>(null);
  const [dnsZanyat, setDnsZanyat] = useState(false);

  const job = rabota.job;
  const idyot = job?.status === "running";
  const uspekh = job?.status === "ok";
  const gotovo = local.trim() !== "" && (!svoy || password.length >= 10);

  const ustanovit = async () => {
    if (!gotovo || !guard.take()) return;
    try {
      const otvet = await api.post<{ job: Rabota; first_address?: string; first_password?: string }>(
        `/mail/server/${server.id}/install`,
        {
          first_local: local.trim().toLowerCase(),
          ...(svoy ? { first_password: password } : { generate_password: true }),
          acme_email: acme.trim() || undefined,
          allow_docker_install: server.mode === "ssh" ? docker : undefined,
          tls: samopodpis ? "selfsigned" : undefined,
        },
      );
      if (otvet.first_address && otvet.first_password) {
        setParol({ address: otvet.first_address, password: otvet.first_password });
      }
      setPassword("");
      onStarted();
      setRabotaId(vynutRabotu(otvet).id);
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const proveritDns = async () => {
    if (!guard.take()) return;
    setDnsZanyat(true);
    try {
      setDns((await api.post<{ domains: DnsDomena[] }>(`/mail/server/${server.id}/dns`, {})).domains);
    } catch (e) {
      toastError(e);
    } finally {
      setDnsZanyat(false);
      guard.free();
    }
  };

  // Установилось — сразу показываем, какие записи DNS завести: это последний
  // шаг, без которого письма до Gmail не дойдут.
  useEffect(() => {
    if (!job || job.status === "running") return;
    onChanged();
    if (job.status === "ok") void proveritDns();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.status]);

  if (rabotaId === null) {
    return (
      <div>
        <div className="msrv-q">{t("msrvInstallIntro")}</div>
        <Pole id="msrv-first" label={t("msrvFirstBox")} desc={t("msrvFirstBoxHint")}>
          <div className="msrv-address">
            <input
              id="msrv-first"
              className="input"
              value={local}
              onChange={(e) => setLocal(e.target.value)}
            />
            <span className="msrv-at">@{domen}</span>
          </div>
        </Pole>
        <div className="label">{t("password")}</div>
        <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
          <button
            type="button"
            className={"option-chip" + (!svoy ? " active" : "")}
            aria-pressed={!svoy}
            onClick={() => setSvoy(false)}
          >
            {t("msrvPasswordAuto")}
          </button>
          <button
            type="button"
            className={"option-chip" + (svoy ? " active" : "")}
            aria-pressed={svoy}
            onClick={() => setSvoy(true)}
          >
            {t("msrvPasswordOwn")}
          </button>
        </div>
        {svoy && (
          <Pole id="msrv-first-password" label={t("msrvPasswordOwn")} desc={t("msrvPasswordOwnHint")}>
            <input
              id="msrv-first-password"
              className="input"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Pole>
        )}
        <Pole id="msrv-acme" label={t("msrvAcmeEmail")} desc={t("msrvAcmeEmailHint")}>
          <input
            id="msrv-acme"
            className="input"
            type="email"
            value={acme}
            placeholder="you@gmail.com"
            onChange={(e) => setAcme(e.target.value)}
          />
        </Pole>
        {server.mode === "ssh" && (
          <div className="msrv-toggle-row">
            <Toggle on={docker} label={t("msrvAllowDocker")} onToggle={() => setDocker((v) => !v)} />
            <div>
              <div style={{ fontSize: 13 }}>{t("msrvAllowDocker")}</div>
              <div className="field-desc" style={{ marginTop: 2 }}>{t("msrvAllowDockerHint")}</div>
            </div>
          </div>
        )}
        <details className="msrv-log-fold">
          <summary>{t("msrvAdvanced")}</summary>
          <div className="msrv-toggle-row" style={{ marginTop: 10 }}>
            <Toggle on={samopodpis} label={t("msrvSelfSigned")} onToggle={() => setSamopodpis((v) => !v)} />
            <div>
              <div style={{ fontSize: 13 }}>{t("msrvSelfSigned")}</div>
              <div className="field-desc" style={{ marginTop: 2 }}>{t("msrvSelfSignedHint")}</div>
            </div>
          </div>
        </details>
        <div className="msrv-actions">
          <button className="btn btn-secondary btn-sm" onClick={onBack}>
            {t("back")}
          </button>
          <button className="btn btn-primary btn-sm" disabled={!gotovo || guard.busy} onClick={() => void ustanovit()}>
            {guard.busy && <Spinner />}
            {t("msrvInstall")}
          </button>
        </div>
      </div>
    );
  }

  // Пароль первого ящика — сразу, как пришёл ответ, а не к концу установки:
  // экран честно говорит, что уйти можно, и ушедший иначе не увидел бы его нигде.
  const provalilas = job?.status === "failed" || job?.status === "rolled_back";

  return (
    <div>
      {idyot && <div className="msrv-q">{t("msrvInstalling")}</div>}
      {parol && !uspekh && !provalilas && (
        <PokazParolya address={parol.address} password={parol.password} srazu />
      )}
      <ZhurnalRaboty rabota={rabota} />

      {uspekh && (
        <>
          <div className="msrv-done">
            <span className="msrv-done-icon">
              <Icon name="check" size={18} stroke={2.2} />
            </span>
            <div>
              <div className="msrv-done-title">{t("msrvDoneTitle")}</div>
              <div className="field-desc" style={{ marginTop: 2 }}>{t("msrvDoneText")}</div>
            </div>
          </div>
          {parol && <PokazParolya address={parol.address} password={parol.password} />}
          <TablitsaDns domains={dns} zanyat={dnsZanyat} onRecheck={() => void proveritDns()} />
          <NastroykiTelefona hostname={server.hostname} address={parol?.address} />
        </>
      )}

      <div className="msrv-actions">
        {job && !idyot && !uspekh && (
          <button className="btn btn-secondary btn-sm" onClick={onBack}>
            {t("msrvTryAgain")}
          </button>
        )}
        {uspekh && (
          <button
            className="btn btn-primary btn-sm"
            onClick={() => {
              setParol(null);
              onFinish();
            }}
          >
            {t("msrvFinish")}
          </button>
        )}
      </div>
    </div>
  );
}

// --- работа и её журнал ----------------------------------------------------

interface SostoyanieRaboty {
  job: Rabota | null;
  log: string;
  failure: unknown;
  retry: () => void;
}

/**
 * Опрос идущей работы: журнал дочитывается с того байта, на котором остановились.
 *
 * Только пока вкладка видна: установка идёт минутами, и забытая вкладка иначе
 * спрашивала бы сервер раз в полторы секунды до вечера.
 */
function useRabota(jobId: number | null): SostoyanieRaboty {
  const [job, setJob] = useState<Rabota | null>(null);
  const [log, setLog] = useState("");
  const [failure, setFailure] = useState<unknown>(null);
  const [popytka, setPopytka] = useState(0);

  useEffect(() => {
    setJob(null);
    setLog("");
    setFailure(null);
    if (jobId === null) return;
    let zhivo = true;
    let otkuda = 0;
    let idyot = false;
    let konchilas = false;
    const poll = async () => {
      if (idyot || konchilas) return;
      idyot = true;
      try {
        const otvet = await api.get<{ job?: Rabota; log_chunk?: string; log_size?: number }>(
          `/mail/server/jobs/${jobId}?log_from=${otkuda}`,
        );
        if (!zhivo) return;
        const kusok = otvet.log_chunk ?? "";
        if (kusok) setLog((prezhniy) => prezhniy + kusok);
        // Смещение — в байтах и только от сервера: длина строки в JS считает
        // знаки, и на русском журнале дочитка съезжала бы в середину буквы.
        if (typeof otvet.log_size === "number") otkuda = otvet.log_size;
        const rabota = vynutRabotu(otvet);
        setJob(rabota);
        setFailure(null);
        if (rabota.status !== "running") konchilas = true;
      } catch (e) {
        if (zhivo) setFailure(e);
      } finally {
        idyot = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void poll();
    }, OPROS_MS);
    return () => {
      zhivo = false;
      window.clearInterval(timer);
    };
  }, [jobId, popytka]);

  const retry = useCallback(() => setPopytka((p) => p + 1), []);
  return { job, log, failure, retry };
}

function ZhurnalRaboty({ rabota }: { rabota: SostoyanieRaboty }) {
  const { t, locale } = useApp();
  const { job, log, failure, retry } = rabota;
  if (!job) {
    return failure ? (
      <LoadFailed error={failure} onRetry={retry} />
    ) : (
      <div className="msrv-faint" style={{ display: "flex", gap: 8, alignItems: "center", margin: "10px 0" }}>
        <Spinner /> {t("loading")}
      </div>
    );
  }
  const idyot = job.status === "running";
  const provalilas = job.status === "failed" || job.status === "rolled_back";
  const hint = job.hint;
  const shagiInstrukcii = hint ? (locale === "ru" ? hint.steps_ru : hint.steps_en) : [];
  return (
    <div className="msrv-job">
      {failure !== null && <LoadFailed error={failure} onRetry={retry} />}
      <SpisokShagov shagi={job.steps} glavnaya={job.error_code} />

      {provalilas && (
        <div className="msrv-fail" role="alert">
          <div className="msrv-fail-title">
            <Icon name="alert" size={16} />
            {hint ? (locale === "ru" ? hint.title_ru : hint.title_en) : t("msrvFailTitle")}
          </div>
          {(job.error_detail || BEDA_PODPIS[job.error_code]) && (
            <>
              <div className="label" style={{ marginTop: 10 }}>{t("msrvWhatWrong")}</div>
              {BEDA_PODPIS[job.error_code] && <div className="msrv-fail-text">{t(BEDA_PODPIS[job.error_code])}</div>}
              {job.error_detail && <pre className="code-block msrv-detail">{job.error_detail}</pre>}
            </>
          )}
          {shagiInstrukcii.length > 0 && (
            <>
              <div className="label" style={{ marginTop: 12 }}>{t("msrvHowFix")}</div>
              <ol className="msrv-fix">
                {shagiInstrukcii.map((s, i) => (
                  <li key={i}>
                    <InstrukciyaShag text={s} />
                  </li>
                ))}
              </ol>
            </>
          )}
          {job.status === "rolled_back" && <div className="field-desc">{t("msrvRolledBack")}</div>}
        </div>
      )}

      {idyot ? (
        <Zhurnal text={log} />
      ) : (
        <details className="msrv-log-fold">
          <summary>{t("msrvLogFull")}</summary>
          <Zhurnal text={log} />
        </details>
      )}
      {job.finished_at && (
        <div className="field-desc">
          {t(VID_RABOTY[job.kind] ?? "msrvKindCheck")} · {t(ITOG_RABOTY[job.status] ?? "msrvJobFailed")} · {formatDateTime(job.finished_at, locale)}
        </div>
      )}
    </div>
  );
}

/** Шаг инструкции: команда в обратных кавычках — отдельным блоком с копированием. */
function InstrukciyaShag({ text }: { text: string }) {
  const chasti = text.split(/`([^`]+)`/);
  return (
    <>
      {chasti.map((kusok, i) =>
        i % 2 === 1 ? (
          <span key={i} className="docs-code-wrap token-kod msrv-cmd">
            <pre className="code-block">{kusok}</pre>
            <CopyButton text={kusok} />
          </span>
        ) : (
          <span key={i}>{kusok}</span>
        ),
      )}
    </>
  );
}

function SpisokShagov({ shagi, glavnaya }: { shagi: Shag[]; glavnaya?: string }) {
  const { t, locale } = useApp();
  if (shagi.length === 0) return null;
  return (
    <ul className="msrv-steps">
      {shagi.map((s, i) => {
        const beda = kodBedy(s.hint);
        const instrukciya = s.hint && typeof s.hint === "object" ? s.hint : null;
        // Инструкция провала уже стоит под списком; у предупреждения — своя, здесь.
        const svoya = instrukciya && beda !== glavnaya && s.status === "warn";
        return (
          <li key={s.code + i} className={"msrv-step msrv-step-" + s.status}>
            <span className="msrv-step-icon" aria-label={t(SOSTOYANIE_SHAGA[s.status] ?? "msrvStPending")}>
              {s.status === "running" ? (
                <Spinner />
              ) : (
                <Icon
                  name={s.status === "ok" ? "check" : s.status === "fail" ? "x" : s.status === "warn" ? "alert" : "dots"}
                  size={13}
                  stroke={2}
                />
              )}
            </span>
            <span className="msrv-step-body">
              <span className="msrv-step-title">{SHAG_PODPIS[s.code] ? t(SHAG_PODPIS[s.code]) : s.code}</span>
              {beda && BEDA_PODPIS[beda] && (s.status === "fail" || s.status === "warn") && (
                <span className="msrv-step-why">{t(BEDA_PODPIS[beda])}</span>
              )}
              {beda && !BEDA_PODPIS[beda] && (
                <span className="msrv-step-why">
                  {instrukciya ? (locale === "ru" ? instrukciya.title_ru : instrukciya.title_en) : beda}
                </span>
              )}
              {s.detail && <span className="msrv-step-detail">{s.detail}</span>}
              {svoya && instrukciya && (
                <details className="msrv-step-fix">
                  <summary>{t("msrvHowFix")}</summary>
                  <ol className="msrv-fix">
                    {(locale === "ru" ? instrukciya.steps_ru : instrukciya.steps_en).map((x, j) => (
                      <li key={j}>
                        <InstrukciyaShag text={x} />
                      </li>
                    ))}
                  </ol>
                </details>
              )}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Сырой журнал: строки протокола `##…` подсвечены, остальное — как пришло. */
function Zhurnal({ text }: { text: string }) {
  const { t } = useApp();
  const ref = useRef<HTMLPreElement>(null);
  const vnizu = useRef(true);
  useEffect(() => {
    const el = ref.current;
    // Листает за журналом, только если человек сам не ушёл читать выше.
    if (el && vnizu.current) el.scrollTop = el.scrollHeight;
  }, [text]);
  const stroki = text ? text.split("\n") : [];
  return (
    <div className="docs-code-wrap msrv-log-wrap">
      <pre
        ref={ref}
        className="code-block msrv-log"
        tabIndex={0}
        aria-label={t("msrvLog")}
        aria-live="off"
        onScroll={(e) => {
          const el = e.currentTarget;
          vnizu.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
      >
        {stroki.length === 0
          ? t("msrvLogEmpty")
          : stroki.map((s, i) => (
              <span key={i} className={klassStroki(s)}>
                {s}
                {"\n"}
              </span>
            ))}
      </pre>
      {text && <CopyButton text={text} />}
    </div>
  );
}

function klassStroki(s: string): string | undefined {
  if (!s.startsWith("##")) return undefined;
  if (s.startsWith("##OK")) return "msrv-log-ok";
  if (s.startsWith("##WARN")) return "msrv-log-warn";
  if (s.startsWith("##FAIL") || s.startsWith("##ROLLBACK")) return "msrv-log-fail";
  return "msrv-log-mark";
}

// --- DNS --------------------------------------------------------------------

function TablitsaDns({
  domains,
  zanyat,
  onRecheck,
  proverenoV,
}: {
  domains: DnsDomena[] | null;
  zanyat: boolean;
  onRecheck: () => void;
  proverenoV?: string | null;
}) {
  const { t, locale } = useApp();
  return (
    <div className="msrv-section">
      <div className="msrv-section-head">
        <div className="section-title">{t("msrvDnsTitle")}</div>
        <button className="btn btn-secondary btn-sm" disabled={zanyat} onClick={onRecheck}>
          {zanyat ? <Spinner /> : <Icon name="refresh" size={13} />}
          {t("msrvDnsCheck")}
        </button>
      </div>
      <div className="field-desc" style={{ marginTop: 0, marginBottom: 10 }}>{t("msrvDnsIntro")}</div>
      {proverenoV && (
        <div className="field-desc" style={{ marginTop: 0, marginBottom: 8 }}>
          {t("msrvDnsChecked", { t: formatDateTime(proverenoV, locale) })}
        </div>
      )}
      {domains === null ? (
        zanyat ? (
          <div className="msrv-faint" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Spinner /> {t("loading")}
          </div>
        ) : (
          <div className="field-desc">{t("msrvDnsNever")}</div>
        )
      ) : (
        domains.map((d) => <DnsDomenaBlok key={d.domain} domain={d.domain} records={d.records} />)
      )}
    </div>
  );
}

function DnsDomenaBlok({ domain, records }: { domain: string; records: DnsZapis[] }) {
  const { t, locale } = useApp();
  return (
    <div className="msrv-dns">
      <div className="msrv-dns-domain">{domain}</div>
      <div className="msrv-table-wrap">
        <table className="msrv-table">
          <thead>
            <tr>
              <th>{t("msrvDnsStatus")}</th>
              <th>{t("msrvDnsType")}</th>
              <th>{t("msrvDnsName")}</th>
              <th>{t("msrvDnsValue")}</th>
            </tr>
          </thead>
          <tbody>
            {records.map((r, i) => (
              <tr key={r.type + r.name + i} className={"msrv-dns-" + r.status}>
                <td>
                  <Chip
                    variant={
                      r.status === "ok" ? "success" : r.status === "warn" || !r.required ? "warning" : "danger"
                    }
                  >
                    {t(DNS_PODPIS[r.status] ?? "msrvDnsWarn")}
                  </Chip>
                  {!r.required && <div className="msrv-faint msrv-small">{t("msrvDnsOptional")}</div>}
                </td>
                <td className="msrv-mono">{r.type}</td>
                <td>
                  <div className="msrv-copy-cell">
                    <code className="msrv-mono">{r.name}</code>
                    <CopyButton text={r.name} />
                  </div>
                </td>
                <td>
                  <div className="msrv-copy-cell">
                    <code className="msrv-mono msrv-value">{r.expected}</code>
                    <CopyButton text={r.expected} />
                  </div>
                  {r.status !== "ok" && (
                    <div className="msrv-small msrv-faint">
                      {t("msrvDnsFound")}: {r.found.length ? r.found.join(", ") : t("msrvDnsNothing")}
                    </div>
                  )}
                  {r.hint && r.status !== "ok" && (
                    <div className="msrv-small msrv-dns-hint">
                      {typeof r.hint === "string" ? r.hint : locale === "ru" ? r.hint.ru : r.hint.en}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --- готовый сервер ---------------------------------------------------------

function KartochkaServera({
  server,
  onChanged,
  onRemove,
}: {
  server: Server;
  onChanged: () => void;
  onRemove: () => void;
}) {
  const { t, locale, toast, toastError } = useApp();
  const guard = useGuard();
  const [dns, setDns] = useState<DnsDomena[] | null>(() => {
    const iz = server.domains
      .map((d) => ({ domain: d.domain, records: zapisiOtchyota(d.dns_report) }))
      .filter((d) => d.records.length > 0);
    return iz.length ? iz : null;
  });
  const [dnsZanyat, setDnsZanyat] = useState(false);
  const [novyyDomen, setNovyyDomen] = useState("");
  const [udalitDomen, setUdalitDomen] = useState<Domen | null>(null);
  const [testKomu, setTestKomu] = useState("");
  const [zhurnal, setZhurnal] = useState<number | null>(null);

  const proverenoV = server.domains.reduce<string | null>(
    (m, d) => (d.dns_checked_at && (!m || d.dns_checked_at > m) ? d.dns_checked_at : m),
    null,
  );

  const proveritDns = async () => {
    if (!guard.take()) return;
    setDnsZanyat(true);
    try {
      setDns((await api.post<{ domains: DnsDomena[] }>(`/mail/server/${server.id}/dns`, {})).domains);
    } catch (e) {
      toastError(e);
    } finally {
      setDnsZanyat(false);
      guard.free();
    }
  };

  const dobavitDomen = async () => {
    const domain = novyyDomen.trim().toLowerCase().replace(/^@/, "");
    if (!domain || !guard.take()) return;
    try {
      await api.post(`/mail/server/${server.id}/domains`, { domain });
      setNovyyDomen("");
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const ubratDomen = async (d: Domen) => {
    if (!guard.take()) return;
    try {
      await api.del(`/mail/server/${server.id}/domains/${d.id}`);
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  // Подпись, не созданная при установке (`dkim_failed` — только предупреждение),
  // иначе не восстановить: без неё письма уходят в спам.
  const sozdatDkim = async (d: Domen) => {
    if (!guard.take()) return;
    let vyshlo = false;
    try {
      await api.post(`/mail/server/${server.id}/domains/${d.id}/dkim`, {});
      toast(t("msrvDkimCreated", { domain: d.domain }));
      onChanged();
      vyshlo = true;
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
    // Запись DKIM появилась — в таблице DNS её значение, которое надо завести.
    if (vyshlo) void proveritDns();
  };

  const sverit = async () => {
    if (!guard.take()) return;
    try {
      const r = await api.post<{ added: unknown; missing: unknown; aliases: unknown }>(
        `/mail/server/${server.id}/sync`,
        {},
      );
      const n = (x: unknown): number =>
        Array.isArray(x)
          ? x.length
          : x && typeof x === "object"
            ? n((x as { added?: unknown }).added)
            : Number(x) || 0;
      toast(t("msrvSyncResult", { added: n(r.added), missing: n(r.missing), aliases: n(r.aliases) }));
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const otpravitTest = async () => {
    const to = testKomu.trim();
    if (!to || !guard.take()) return;
    try {
      await api.post(`/mail/server/${server.id}/test`, { to });
      toast(t("msrvTestSent"));
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="card card-pad msrv-card" style={{ marginBottom: 18 }}>
      <div className="msrv-card-head">
        <span className="msrv-card-icon">
          <Icon name="mail" size={18} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="msrv-card-title">{server.title || server.hostname}</div>
          <div className="msrv-faint msrv-small">
            {server.hostname}
            {" · "}
            {server.mode === "local" ? t("msrvModeLocalShort") : t("msrvModeSshShort", { target: kudaSsh(server) })}
            {server.public_ip && ` · ${server.public_ip}`}
            {server.installed_at && ` · ${t("msrvInstalledAt", { t: formatDateTime(server.installed_at, locale) })}`}
          </div>
        </div>
        <Chip variant={server.last_error ? "warning" : "success"}>{t(SOSTOYANIE_SERVERA[server.status] ?? "msrvStatusDraft")}</Chip>
      </div>
      {server.last_error && (
        <div className="msrv-note msrv-note-warn">
          <Icon name="alert" size={15} />
          <div style={{ minWidth: 0, overflowWrap: "anywhere" }}>{server.last_error}</div>
        </div>
      )}

      <Zdorovye serverId={server.id} />

      <TablitsaDns domains={dns} zanyat={dnsZanyat} onRecheck={() => void proveritDns()} proverenoV={proverenoV} />

      <div className="msrv-section">
        <div className="msrv-section-head">
          <div className="section-title">{t("msrvDomains")}</div>
        </div>
        <div className="list-card">
          {server.domains.map((d) => (
            <div key={d.id} className="list-row" style={{ height: "auto", padding: "10px 14px" }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13.5, fontWeight: 500 }}>{d.domain}</div>
                <div className="msrv-faint msrv-small">
                  DKIM: {d.dkim_txt ? d.dkim_selector : t("msrvDkimNone")}
                </div>
              </div>
              {!d.dkim_txt && (
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={guard.busy}
                  onClick={() => void sozdatDkim(d)}
                >
                  {t("msrvDkimCreate")}
                </button>
              )}
              {server.domains.length > 1 && (
                <KnopkaKorziny
                  title={t("msrvDomainDelete", { name: d.domain })}
                  onClick={() => setUdalitDomen(d)}
                />
              )}
            </div>
          ))}
        </div>
        <form
          className="msrv-inline"
          onSubmit={(e) => {
            e.preventDefault();
            void dobavitDomen();
          }}
        >
          <input
            className="input"
            value={novyyDomen}
            placeholder="second-brand.com"
            aria-label={t("msrvDomain")}
            onChange={(e) => setNovyyDomen(e.target.value)}
          />
          <button type="submit" className="btn btn-secondary btn-sm" disabled={guard.busy || !novyyDomen.trim()}>
            <Icon name="plus" size={13} />
            {t("msrvDomainAdd")}
          </button>
        </form>
      </div>

      <div className="msrv-section">
        <div className="msrv-section-head">
          <div className="section-title">{t("mailboxes")}</div>
          <span className="msrv-faint msrv-small">{t("msrvBoxes", { n: server.boxes_count })}</span>
        </div>
        <div className="msrv-inline">
          <Link className="btn btn-primary btn-sm" to="/settings/mailboxes">
            {t("msrvBoxesManage")}
          </Link>
          <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => void sverit()}>
            <Icon name="refresh" size={13} />
            {t("msrvSync")}
          </button>
        </div>
        <div className="field-desc">{t("msrvSyncHint")}</div>
      </div>

      <ArhivServera server={server} />

      <div className="msrv-section">
        <div className="section-title">{t("msrvTest")}</div>
        <div className="field-desc" style={{ marginTop: 4 }}>{t("msrvTestHint")}</div>
        <form
          className="msrv-inline"
          onSubmit={(e) => {
            e.preventDefault();
            void otpravitTest();
          }}
        >
          <input
            className="input"
            type="email"
            value={testKomu}
            placeholder="you@gmail.com"
            aria-label={t("msrvTestTo")}
            onChange={(e) => setTestKomu(e.target.value)}
          />
          <button type="submit" className="btn btn-secondary btn-sm" disabled={guard.busy || !testKomu.trim()}>
            <Icon name="send" size={13} />
            {t("msrvTestSend")}
          </button>
        </form>
      </div>

      <Posrednik server={server} onChanged={onChanged} />

      <NastroykiTelefona hostname={server.hostname} />

      <IstoriyaRabot serverId={server.id} onOpen={setZhurnal} />

      <div className="msrv-section msrv-danger-zone">
        <div style={{ flex: 1 }}>
          <div className="section-title">{t("msrvRemove")}</div>
          <div className="field-desc" style={{ marginTop: 4 }}>{t("msrvRemoveHint")}</div>
        </div>
        <button className="btn btn-secondary btn-sm msrv-danger-btn" onClick={onRemove}>
          {t("msrvRemove")}
        </button>
      </div>

      {udalitDomen && (
        <ConfirmModal
          text={t("msrvDomainDeleteConfirm", { name: udalitDomen.domain })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void ubratDomen(udalitDomen)}
          onClose={() => setUdalitDomen(null)}
        />
      )}
      {zhurnal !== null && (
        <Modal title={t("msrvLog")} wide onClose={() => setZhurnal(null)}>
          <OknoRaboty jobId={zhurnal} />
        </Modal>
      )}
    </div>
  );
}

interface ArhivYashchika {
  account_id: number | null;
  address: string;
  messages: number;
  bytes: number;
  /** Писем, которых на сервере уже нет: копия в CRM осталась. */
  removed?: number;
  synced_at: string | null;
}

/** Копия писем в CRM по ящикам этого сервера: сколько, сколько места, когда догнали. */
function ArhivServera({ server }: { server: Server }) {
  const { t, locale } = useApp();
  const [items, setItems] = useState<ArhivYashchika[] | null>(null);

  useEffect(() => {
    let zhivo = true;
    api
      .get<ArhivYashchika[]>("/mail/archive/stats")
      .then((otvet) => {
        if (zhivo) setItems(otvet);
      })
      // Архив — сведения, а не управление: без них карточка остаётся рабочей.
      .catch(() => undefined);
    return () => {
      zhivo = false;
    };
  }, [server.id]);

  if (!items) return null;
  const domeny = new Set(server.domains.map((d) => d.domain.toLowerCase()));
  // Сводка — по ящикам, без сервера: свои узнаём по домену адреса.
  const svoi = items.filter((b) => domeny.has((b.address ?? "").split("@")[1]?.toLowerCase() ?? ""));
  const pisem = svoi.reduce((s, b) => s + (Number(b.messages) || 0), 0);
  const bayt = svoi.reduce((s, b) => s + (Number(b.bytes) || 0), 0);
  const udaleno = svoi.reduce((s, b) => s + (Number(b.removed) || 0), 0);
  const kogda = svoi.reduce<string | null>((m, b) => (b.synced_at && (!m || b.synced_at > m) ? b.synced_at : m), null);
  return (
    <div className="msrv-section">
      <div className="section-title">{t("msrvArchive")}</div>
      <div className="field-desc" style={{ marginTop: 4 }}>{t("msrvArchiveHint")}</div>
      <div className="msrv-chips">
        <Chip>{t("msrvArchiveLetters", { n: pisem })}</Chip>
        <Chip>{fileSize(bayt)}</Chip>
        {udaleno > 0 && <Chip variant="warning">{t("msrvArchiveRemoved", { n: udaleno })}</Chip>}
        <Chip variant={kogda ? undefined : "warning"}>
          {kogda ? t("msrvArchiveSynced", { t: formatDateTime(kogda, locale) }) : t("msrvArchiveNever")}
        </Chip>
      </div>
    </div>
  );
}

function OknoRaboty({ jobId }: { jobId: number }) {
  const rabota = useRabota(jobId);
  return <ZhurnalRaboty rabota={rabota} />;
}

function Zdorovye({ serverId }: { serverId: number }) {
  const { t, locale } = useApp();
  const [data, setData] = useState<Zdorove | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [zanyat, setZanyat] = useState(false);

  const load = useCallback(() => {
    setFailure(null);
    setZanyat(true);
    api
      .get<Zdorove>(`/mail/server/${serverId}/health`)
      .then(setData)
      .catch(setFailure)
      .finally(() => setZanyat(false));
  }, [serverId]);

  useEffect(load, [load]);

  const sluzhby = Object.entries(data?.services ?? {});
  return (
    <div className="msrv-section">
      <div className="msrv-section-head">
        <div className="section-title">{t("msrvHealth")}</div>
        <button className="btn btn-secondary btn-sm" disabled={zanyat} onClick={load}>
          {zanyat ? <Spinner /> : <Icon name="refresh" size={13} />}
          {t("msrvCheckAgain")}
        </button>
      </div>
      {failure !== null ? (
        <LoadFailed error={failure} onRetry={load} />
      ) : !data ? (
        <div className="msrv-faint msrv-small">{t("loading")}</div>
      ) : (
        <div className="msrv-chips">
          {sluzhby.map(([imya, zhiv]) => (
            <Chip key={imya} variant={zhiv ? "success" : "danger"}>
              {zhiv ? "✓" : "✗"} {imya}
            </Chip>
          ))}
          {data.cert?.not_after && (
            <Chip variant={data.cert.self_signed ? "warning" : undefined}>
              {t("msrvCert", { t: formatDateTime(data.cert.not_after, locale) })}
              {data.cert.self_signed ? ` · ${t("msrvCertSelf")}` : ""}
            </Chip>
          )}
          {typeof data.queue === "number" && (
            <Chip variant={data.queue > 20 ? "warning" : undefined}>{t("msrvQueue", { n: data.queue })}</Chip>
          )}
        </div>
      )}
    </div>
  );
}

function Posrednik({ server, onChanged }: { server: Server; onChanged: () => void }) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [host, setHost] = useState(server.relay_host);
  const [port, setPort] = useState(String(server.relay_port || 587));
  const [user, setUser] = useState(server.relay_user);
  const [password, setPassword] = useState("");
  const [snyat, setSnyat] = useState(false);

  const sokhranit = async () => {
    if (!host.trim() || !guard.take()) return;
    try {
      await api.put(`/mail/server/${server.id}/relay`, {
        host: host.trim(),
        port: Number(port) || 587,
        user: user.trim(),
        // Пустое поле — «не менять»: сохранённый пароль обратно не приходит.
        password: password || undefined,
      });
      setPassword("");
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const ubrat = async () => {
    if (!guard.take()) return;
    try {
      await api.del(`/mail/server/${server.id}/relay`);
      setHost("");
      setUser("");
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <div className="msrv-section">
      <div className="section-title">{t("msrvRelay")}</div>
      <div className="field-desc" style={{ marginTop: 4, marginBottom: 10 }}>
        {server.relay_host ? t("msrvRelayOn", { host: server.relay_host }) : t("msrvRelayHint")}
      </div>
      <div className="msrv-grid3">
        <Pole id={`msrv-relay-host-${server.id}`} label={t("msrvRelayHost")}>
          <input
            id={`msrv-relay-host-${server.id}`}
            className="input"
            value={host}
            placeholder="smtp.mailgun.org"
            onChange={(e) => setHost(e.target.value)}
          />
        </Pole>
        <Pole id={`msrv-relay-port-${server.id}`} label={t("port")}>
          <input
            id={`msrv-relay-port-${server.id}`}
            className="input"
            inputMode="numeric"
            value={port}
            onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))}
          />
        </Pole>
        <Pole id={`msrv-relay-user-${server.id}`} label={t("msrvRelayUser")}>
          <input
            id={`msrv-relay-user-${server.id}`}
            className="input"
            value={user}
            autoComplete="off"
            onChange={(e) => setUser(e.target.value)}
          />
        </Pole>
      </div>
      <Pole id={`msrv-relay-password-${server.id}`} label={t("password")}>
        <input
          id={`msrv-relay-password-${server.id}`}
          className="input"
          type="password"
          autoComplete="new-password"
          value={password}
          placeholder={server.has_relay_password ? "••••••••" : ""}
          onChange={(e) => setPassword(e.target.value)}
        />
      </Pole>
      <div className="msrv-inline">
        <button className="btn btn-secondary btn-sm" disabled={guard.busy || !host.trim()} onClick={() => void sokhranit()}>
          {t("msrvRelaySave")}
        </button>
        {server.relay_host && (
          <button type="button" className="text-link danger" onClick={() => setSnyat(true)}>
            {t("msrvRelayOff")}
          </button>
        )}
      </div>
      {snyat && (
        <ConfirmModal
          text={t("msrvRelayOffConfirm", { host: server.relay_host })}
          confirmLabel={t("msrvRelayOff")}
          onConfirm={() => void ubrat()}
          onClose={() => setSnyat(false)}
        />
      )}
    </div>
  );
}

function IstoriyaRabot({ serverId, onOpen }: { serverId: number; onOpen: (id: number) => void }) {
  const { t, locale } = useApp();
  const [items, setItems] = useState<Rabota[] | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [otkryto, setOtkryto] = useState(false);

  const load = useCallback(() => {
    setFailure(null);
    api
      .get(`/mail/server/${serverId}/jobs`)
      .then((r) => setItems(vynutSpisok<Rabota>(r)))
      .catch(setFailure);
  }, [serverId]);

  useEffect(() => {
    if (otkryto) load();
  }, [otkryto, load]);

  return (
    <div className="msrv-section">
      <details className="msrv-log-fold" onToggle={(e) => setOtkryto((e.currentTarget as HTMLDetailsElement).open)}>
        <summary>{t("msrvJobs")}</summary>
        {failure !== null ? (
          <LoadFailed error={failure} onRetry={load} />
        ) : items === null ? (
          <div className="msrv-faint msrv-small">{t("loading")}</div>
        ) : items.length === 0 ? (
          <div className="msrv-faint msrv-small">{t("msrvJobsEmpty")}</div>
        ) : (
          <div className="list-card" style={{ marginTop: 8 }}>
            {items.map((j) => (
              <button
                key={j.id}
                type="button"
                className="list-row msrv-job-row"
                onClick={() => onOpen(j.id)}
              >
                <span style={{ flex: 1, textAlign: "left" }}>{t(VID_RABOTY[j.kind] ?? "msrvKindCheck")}</span>
                <Chip variant={j.status === "ok" ? "success" : j.status === "running" ? "accent" : "danger"}>
                  {t(ITOG_RABOTY[j.status] ?? "msrvJobFailed")}
                </Chip>
                <span className="msrv-faint msrv-small">{formatDateTime(j.started_at, locale)}</span>
              </button>
            ))}
          </div>
        )}
      </details>
    </div>
  );
}

function UdalenieServera({
  server,
  onClose,
  onChanged,
}: {
  server: Server;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [pochtu, setPochtu] = useState(false);
  const [imya, setImya] = useState("");
  const [rabotaId, setRabotaId] = useState<number | null>(null);
  const rabota = useRabota(rabotaId);
  const sovpalo = imya.trim().toLowerCase() === server.hostname.toLowerCase();

  const udalit = async () => {
    if (!sovpalo || !guard.take()) return;
    try {
      const job = await api.post<Rabota>(`/mail/server/${server.id}/remove`, {
        delete_mail: pochtu,
        confirm: imya.trim(),
      });
      setRabotaId(vynutRabotu(job).id);
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  // Сервер снят с машины — запись в CRM больше ни к чему не ведёт: работа
  // оставляет её черновиком, стираем отсюда. «Не найдено» — уже стёрта.
  const status = rabota.job?.status;
  useEffect(() => {
    if (status !== "ok") return;
    api
      .del(`/mail/server/${server.id}`)
      .catch(() => undefined)
      .finally(onChanged);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  return (
    <Modal title={t("msrvRemove")} wide={rabotaId !== null} onClose={onClose}>
      {status === "ok" && (
        <div className="msrv-note msrv-note-ok" role="status">
          <Icon name="check" size={15} stroke={2} />
          <div style={{ flex: 1 }}>{t("msrvRemoved", { name: server.hostname })}</div>
          <button className="btn btn-primary btn-sm" onClick={onClose}>
            {t("close")}
          </button>
        </div>
      )}
      {rabotaId === null ? (
        <>
          <div style={{ fontSize: 13, lineHeight: 1.6, marginBottom: 12 }}>{t("msrvRemoveHint")}</div>
          <div className="msrv-toggle-row">
            <Toggle on={pochtu} label={t("msrvRemoveMail")} onToggle={() => setPochtu((v) => !v)} />
            <div style={{ fontSize: 13, color: pochtu ? "var(--danger)" : undefined }}>{t("msrvRemoveMail")}</div>
          </div>
          <Pole id="msrv-remove-confirm" label={t("msrvRemoveConfirm", { name: server.hostname })}>
            <input
              id="msrv-remove-confirm"
              className="input"
              value={imya}
              autoComplete="off"
              onChange={(e) => setImya(e.target.value)}
            />
          </Pole>
          <div className="msrv-actions">
            <button className="btn btn-secondary btn-sm" onClick={onClose}>
              {t("cancel")}
            </button>
            <button
              className="btn btn-primary btn-sm msrv-danger-solid"
              disabled={!sovpalo || guard.busy}
              onClick={() => void udalit()}
            >
              {t("msrvRemove")}
            </button>
          </div>
        </>
      ) : (
        <ZhurnalRaboty rabota={rabota} />
      )}
    </Modal>
  );
}

// --- общие куски ------------------------------------------------------------

export function PokazParolya({
  address,
  password,
  srazu,
}: {
  address: string;
  password: string;
  /** Показан до конца установки: сохранить надо сейчас, потом его не будет нигде. */
  srazu?: boolean;
}) {
  const { t } = useApp();
  return (
    <div className="msrv-secret" role={srazu ? "alert" : undefined}>
      <div className="label">{t("msrvFirstPassword", { address })}</div>
      <div className="docs-code-wrap token-kod">
        <pre className="code-block">{password}</pre>
        <CopyButton text={password} />
      </div>
      <div className="msrv-secret-note">{t(srazu ? "msrvSaveNow" : "msrvShownOnce")}</div>
    </div>
  );
}

/** Что вписать в телефон и почтовую программу: везде одно и то же имя сервера. */
export function NastroykiTelefona({ hostname, address }: { hostname: string; address?: string }) {
  const { t } = useApp();
  const stroki: { label: string; value: string }[] = [
    { label: t("msrvPhoneIncoming"), value: `${hostname} · 993 · SSL/TLS` },
    { label: t("msrvPhoneOutgoing"), value: `${hostname} · 465 · SSL/TLS` },
    { label: t("msrvPhoneLogin"), value: address ?? t("msrvPhoneLoginAny") },
  ];
  return (
    <div className="msrv-section">
      <div className="section-title">{t("msrvPhone")}</div>
      <div className="field-desc" style={{ marginTop: 4, marginBottom: 8 }}>{t("msrvPhoneHint")}</div>
      <dl className="msrv-kv">
        {stroki.map((s) => (
          <div key={s.label} className="msrv-kv-row">
            <dt>{s.label}</dt>
            <dd>
              <code className="msrv-mono">{s.value}</code>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Pole({
  id,
  label,
  desc,
  children,
}: {
  id: string;
  label: string;
  desc?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label className="label" htmlFor={id}>
        {label}
      </label>
      {children}
      {desc && <div className="field-desc">{desc}</div>}
    </div>
  );
}
