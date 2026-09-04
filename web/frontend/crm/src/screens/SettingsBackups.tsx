import { useCallback, useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatBytes, formatDateTime } from "../lib/format";
import { useGuard } from "../lib/guard";
import { can } from "../lib/permissions";

/** Пока работа идёт, экран перечитывает её состояние с таким шагом. */
const KOPIYA_POLL_MS = 3000;

type Kind = "db" | "storage";

interface Proverka {
  ok: boolean;
  problems: string[];
  checked_at: string;
  kind?: string;
}

interface Job {
  id: string;
  kind: Kind | "restore-db" | "restore-storage";
  status: "running" | "done" | "failed";
  started_at: string;
  finished_at: string | null;
  actor: string;
  error: string | null;
  filename?: string;
  size?: number;
  tables?: number;
  rows?: number;
  files?: number;
  revision?: string;
  downloaded_at?: string;
  check?: Proverka;
  snapshot?: string;
  copy_taken_at?: string | null;
}

interface Status {
  key: { exists: boolean; pending: boolean; created_at: string | null; fragment_length: number };
  jobs: Job[];
  busy: boolean;
  last_check: Proverka | null;
}

export function SettingsBackups() {
  const { t, locale, user, toast, toastError } = useApp();
  const [status, setStatus] = useState<Status | null>(null);
  const { failure, fail, clear } = useFailure();
  const guard = useGuard();

  const [shownKey, setShownKey] = useState<string | null>(null);
  const [fragment, setFragment] = useState("");
  const [replaceAsk, setReplaceAsk] = useState(false);

  const [restoreKind, setRestoreKind] = useState<Kind>("db");
  const [restoreFile, setRestoreFile] = useState<File | null>(null);
  const [restoreAsk, setRestoreAsk] = useState(false);
  const [restoreProgress, setRestoreProgress] = useState<number | null>(null);

  const load = useCallback(() => {
    clear();
    api.get<Status>("/system/backups").then(setStatus).catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);

  const running = status?.jobs.some((j) => j.status === "running") ?? false;

  // Перечитывать — только пока идёт работа и вкладка на виду. Восстановление
  // базы уносит и нашу сессию: первый 401 после него — не беда, а ожидаемый
  // конец, и экран входа покажет себя сам.
  useEffect(() => {
    if (!running) return;
    const vidno = () => document.visibilityState === "visible";
    const timer = window.setInterval(() => {
      if (vidno()) api.get<Status>("/system/backups").then(setStatus).catch(() => undefined);
    }, KOPIYA_POLL_MS);
    return () => window.clearInterval(timer);
  }, [running]);

  if (!status) return <ScreenLoading error={failure} onRetry={load} />;

  const createKey = async (replace: boolean) => {
    if (!guard.take()) return;
    try {
      const r = await api.post<{ key: string }>("/system/backups/key", { replace });
      setShownKey(r.key);
      setFragment("");
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const confirmKey = async () => {
    if (!guard.take()) return;
    try {
      await api.post("/system/backups/key/confirm", { fragment });
      setShownKey(null);
      setFragment("");
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const take = async (kind: Kind) => {
    if (!guard.take()) return;
    try {
      await api.post(`/system/backups/${kind}`);
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const check = async (job: Job) => {
    if (!guard.take()) return;
    try {
      await api.post(`/system/backups/jobs/${job.id}/check`);
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const restore = async () => {
    if (!restoreFile) return;
    if (!guard.take()) return;
    setRestoreProgress(0);
    try {
      await api.zagruzka("/system/backups/restore", restoreFile, (k) => {
        setRestoreProgress(k.vsego ? Math.round((k.ushlo / k.vsego) * 100) : 0);
      }, { kind: restoreKind }).gotovo;
      toast(t("backupRestoreStarted"));
      setRestoreFile(null);
      load();
    } catch (e) {
      toastError(e);
    } finally {
      setRestoreProgress(null);
      guard.free();
    }
  };

  const kindLabel = (kind: Job["kind"]) =>
    kind === "db"
      ? t("backupKindDb")
      : kind === "storage"
        ? t("backupKindStorage")
        : kind === "restore-db"
          ? t("backupKindRestoreDb")
          : t("backupKindRestoreStorage");

  const key = status.key;
  const canRestore = can(user, "backups.manage");

  return (
    <>
      <div className="card" style={{ padding: "20px 22px", marginBottom: 20 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("backupKeyTitle")}</div>
        {shownKey ? (
          <>
            <div style={{ color: "var(--warning)", fontSize: 12.5, marginBottom: 10, lineHeight: 1.5 }}>
              {t("backupKeyShown")}
            </div>
            <code
              style={{ display: "block", wordBreak: "break-all", fontSize: 13, padding: "10px 12px", marginBottom: 14, background: "var(--bg-2)", borderRadius: 8, userSelect: "all" }}
            >
              {shownKey}
            </code>
            <label className="label" style={{ marginBottom: 6 }}>
              {t("backupKeyConfirmLabel", { n: key.fragment_length })}
            </label>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                className="input"
                style={{ maxWidth: 220 }}
                maxLength={key.fragment_length}
                value={fragment}
                onChange={(e) => setFragment(e.target.value.trim())}
                autoComplete="off"
              />
              <button
                className="btn btn-primary"
                disabled={guard.busy || fragment.length !== key.fragment_length}
                onClick={() => void confirmKey()}
              >
                {t("backupKeyConfirm")}
              </button>
            </div>
          </>
        ) : key.exists ? (
          <>
            <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 12, lineHeight: 1.5 }}>
              {t("backupKeyReady", { t: formatDateTime(key.created_at, locale) })}
            </div>
            <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => setReplaceAsk(true)}>
              {t("backupKeyReplace")}
            </button>
          </>
        ) : (
          <>
            <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 12, lineHeight: 1.5 }}>
              {key.pending ? t("backupKeyPending") : t("backupKeyNone")}
            </div>
            <button className="btn btn-primary" disabled={guard.busy} onClick={() => void createKey(false)}>
              {t("backupKeyCreate")}
            </button>
          </>
        )}
      </div>

      <div className="card" style={{ padding: "20px 22px", marginBottom: 20 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("backupTakeTitle")}</div>
        <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 14, lineHeight: 1.5 }}>{t("backupsSub")}</div>
        <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
          <div style={{ flex: "1 1 260px" }}>
            <button
              className="btn btn-primary"
              disabled={guard.busy || !key.exists || running}
              onClick={() => void take("db")}
            >
              <Icon name="database" size={14} />
              {t("backupTakeDb")}
            </button>
            <div className="field-desc" style={{ marginTop: 8 }}>{t("backupDbNote")}</div>
          </div>
          <div style={{ flex: "1 1 260px" }}>
            <button
              className="btn btn-secondary"
              disabled={guard.busy || !key.exists || running}
              onClick={() => void take("storage")}
            >
              <Icon name="download" size={14} />
              {t("backupTakeStorage")}
            </button>
            <div className="field-desc" style={{ marginTop: 8 }}>{t("backupStorageNote")}</div>
          </div>
        </div>
        {running && (
          <div style={{ color: "var(--warning)", fontSize: 12.5, marginTop: 12 }}>{t("backupBusy")}</div>
        )}
        {status.last_check && (
          <div style={{ color: status.last_check.ok ? "var(--faint)" : "var(--danger)", fontSize: 12.5, marginTop: 12 }}>
            {t("backupLastCheck", {
              t: formatDateTime(status.last_check.checked_at, locale),
              result: status.last_check.ok
                ? t("backupCheckOk")
                : t("backupCheckFail", { why: status.last_check.problems.join("; ") }),
            })}
          </div>
        )}
      </div>

      <div className="card" style={{ padding: "20px 22px", marginBottom: 20 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>{t("backupJobsTitle")}</div>
        {status.jobs.length === 0 ? (
          <div style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("backupNoJobs")}</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {status.jobs.map((job) => (
              <div key={job.id} style={{ display: "flex", gap: 12, alignItems: "flex-start", flexWrap: "wrap", borderTop: "1px solid var(--line)", paddingTop: 10 }}>
                <div style={{ flex: "1 1 320px", fontSize: 12.5, lineHeight: 1.5 }}>
                  <div>
                    <strong>{kindLabel(job.kind)}</strong> · {formatDateTime(job.started_at, locale)} · {job.actor}{" "}
                    <Chip variant={job.status === "failed" ? undefined : job.status === "done" ? "success" : "warning"}>
                      {job.status === "running" ? t("backupRunning") : job.status === "done" ? t("backupDone") : t("backupFailed")}
                    </Chip>
                  </div>
                  <div style={{ color: "var(--faint)" }}>
                    {job.filename && <span>{job.filename}{job.size !== undefined ? ` · ${formatBytes(job.size)}` : ""} </span>}
                    {job.tables !== undefined && job.rows !== undefined && (
                      <span>· {t("backupTables", { tables: job.tables, rows: job.rows })} </span>
                    )}
                    {job.files !== undefined && <span>· {t("backupFiles", { count: job.files })} </span>}
                    {job.copy_taken_at && <span>· {t("backupCopyTakenAt", { t: job.copy_taken_at })} </span>}
                    {job.snapshot && <span>· {t("backupRestoreSnapshot", { name: job.snapshot })} </span>}
                    {job.downloaded_at && <span>· {t("backupDownloadedAt", { t: formatDateTime(job.downloaded_at, locale) })} </span>}
                    {job.check && (
                      <span style={{ color: job.check.ok ? undefined : "var(--danger)" }}>
                        · {job.check.ok ? t("backupCheckOk") : t("backupCheckFail", { why: job.check.problems.join("; ") })}
                      </span>
                    )}
                  </div>
                  {job.error && <div style={{ color: "var(--danger)" }}>{job.error}</div>}
                </div>
                {job.status === "done" && (job.kind === "db" || job.kind === "storage") && (
                  <div style={{ display: "flex", gap: 8 }}>
                    <a className="btn btn-secondary btn-sm" href={`/api/v1/system/backups/jobs/${job.id}/file`}>
                      <Icon name="download" size={13} />
                      {t("backupDownload")}
                    </a>
                    <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => void check(job)}>
                      {t("backupCheck")}
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {canRestore && (
        <div className="card" style={{ padding: "20px 22px", borderColor: "var(--danger)" }}>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>{t("backupRestoreTitle")}</div>
          <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 14, lineHeight: 1.5 }}>
            {restoreKind === "db" ? t("backupRestoreSub") : t("backupRestoreStorageSub")}
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
            <select className="input" style={{ maxWidth: 160 }} value={restoreKind} onChange={(e) => setRestoreKind(e.target.value as Kind)}>
              <option value="db">{t("backupTakeDb")}</option>
              <option value="storage">{t("backupTakeStorage")}</option>
            </select>
            <input
              type="file"
              aria-label={t("backupRestoreFile")}
              onChange={(e) => setRestoreFile(e.target.files?.[0] ?? null)}
            />
            <button
              className="btn btn-danger"
              disabled={guard.busy || !restoreFile || !key.exists || running}
              onClick={() => setRestoreAsk(true)}
            >
              {t("backupRestoreStart")}
            </button>
            {restoreProgress !== null && <span style={{ fontSize: 12.5, color: "var(--faint)" }}>{restoreProgress}%</span>}
          </div>
        </div>
      )}

      {replaceAsk && (
        <ConfirmModal
          text={t("backupKeyReplaceConfirm")}
          confirmLabel={t("backupKeyReplace")}
          danger
          onConfirm={() => { setReplaceAsk(false); void createKey(true); }}
          onClose={() => setReplaceAsk(false)}
        />
      )}
      {restoreAsk && (
        <ConfirmModal
          text={restoreKind === "db" ? t("backupRestoreConfirm") : t("backupRestoreConfirmStorage")}
          confirmLabel={t("backupRestoreStart")}
          danger
          onConfirm={() => { setRestoreAsk(false); void restore(); }}
          onClose={() => setRestoreAsk(false)}
        />
      )}
    </>
  );
}
