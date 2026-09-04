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
  const [deleteAsk, setDeleteAsk] = useState<Job | null>(null);

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

  const remove = async (job: Job) => {
    if (!guard.take()) return;
    try {
      await api.del(`/system/backups/jobs/${job.id}`);
      toast(t("backupDeleted"));
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

  const kindIcon = (kind: Job["kind"]) =>
    kind === "db" ? "database" : kind === "storage" ? "folder" : "refresh";

  const key = status.key;
  const canRestore = can(user, "backups.manage");
  const canTake = key.exists && !running && !guard.busy;

  return (
    <>
      {/* Ключ: одна строка состояния и одна кнопка. Ключ заводят один раз, и
          занимать под него целую карточку — отвлекать от того, ради чего пришли. */}
      <div className="card backup-card">
        <div className="backup-card-head">
          <div>
            <div className="backup-card-title">{t("backupKeyTitle")}</div>
            {!shownKey && (
              <div className="field-desc">
                {key.exists
                  ? t("backupKeyReady", { t: formatDateTime(key.created_at, locale) })
                  : key.pending
                    ? t("backupKeyPending")
                    : t("backupKeyNone")}
              </div>
            )}
          </div>
          {!shownKey && (key.exists ? (
            <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => setReplaceAsk(true)}>
              {t("backupKeyReplace")}
            </button>
          ) : (
            <button className="btn btn-primary" disabled={guard.busy} onClick={() => void createKey(false)}>
              {t("backupKeyCreate")}
            </button>
          ))}
        </div>
        {shownKey && (
          <div className="backup-key-shown">
            <div className="backup-key-warn">{t("backupKeyShown")}</div>
            <code className="backup-key-code">{shownKey}</code>
            <label className="label">{t("backupKeyConfirmLabel", { n: key.fragment_length })}</label>
            <div className="backup-key-confirm">
              <input
                className="input"
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
          </div>
        )}
      </div>

      {/* Две плитки одного вида: база и файлы — равноправные половины одной
          копии, и разные по виду кнопки читались как «главная и запасная». */}
      <div className="card backup-card">
        <div className="backup-card-title">{t("backupTakeTitle")}</div>
        <div className="field-desc">{t("backupBothNote")}</div>
        <div className="backup-tiles">
          <div className="backup-tile">
            <div className="backup-tile-head">
              <Icon name="database" size={16} />
              <span>{t("backupTakeDb")}</span>
            </div>
            <div className="field-desc">{t("backupDbNote")}</div>
            <button className="btn btn-primary" disabled={!canTake} onClick={() => void take("db")}>
              {t("backupTakeDbBtn")}
            </button>
          </div>
          <div className="backup-tile">
            <div className="backup-tile-head">
              <Icon name="folder" size={16} />
              <span>{t("backupTakeStorage")}</span>
            </div>
            <div className="field-desc">{t("backupStorageNote")}</div>
            <button className="btn btn-primary" disabled={!canTake} onClick={() => void take("storage")}>
              {t("backupTakeStorageBtn")}
            </button>
          </div>
        </div>
        {!key.exists && <div className="backup-note warn">{t("backupNeedKey")}</div>}
        {running && <div className="backup-note warn">{t("backupBusy")}</div>}
        {status.last_check && (
          <div className={"backup-note" + (status.last_check.ok ? "" : " bad")}>
            {t("backupLastCheck", {
              t: formatDateTime(status.last_check.checked_at, locale),
              result: status.last_check.ok
                ? t("backupCheckOk")
                : t("backupCheckFail", { why: status.last_check.problems.join("; ") }),
            })}
          </div>
        )}
      </div>

      <div className="card backup-card">
        <div className="backup-card-title">{t("backupJobsTitle")}</div>
        <div className="field-desc">{t("backupJobsNote")}</div>
        {status.jobs.length === 0 ? (
          <div className="backup-note">{t("backupNoJobs")}</div>
        ) : (
          <div className="backup-rows">
            {status.jobs.map((job) => (
              <div key={job.id} className="backup-row">
                <div className="backup-row-icon">
                  <Icon name={kindIcon(job.kind)} size={16} />
                </div>
                <div className="backup-row-main">
                  <div className="backup-row-title">
                    <strong>{kindLabel(job.kind)}</strong>
                    <Chip variant={job.status === "failed" ? undefined : job.status === "done" ? "success" : "warning"}>
                      {job.status === "running" ? t("backupRunning") : job.status === "done" ? t("backupDone") : t("backupFailed")}
                    </Chip>
                  </div>
                  <div className="backup-row-meta">
                    <span>{formatDateTime(job.started_at, locale)}</span>
                    <span>{job.actor}</span>
                    {job.size !== undefined && <span>{formatBytes(job.size)}</span>}
                    {job.tables !== undefined && job.rows !== undefined && (
                      <span>{t("backupTables", { tables: job.tables, rows: job.rows })}</span>
                    )}
                    {job.files !== undefined && <span>{t("backupFiles", { count: job.files })}</span>}
                    {job.copy_taken_at && <span>{t("backupCopyTakenAt", { t: job.copy_taken_at })}</span>}
                    {job.snapshot && <span>{t("backupRestoreSnapshot", { name: job.snapshot })}</span>}
                    {job.downloaded_at && <span>{t("backupDownloadedAt", { t: formatDateTime(job.downloaded_at, locale) })}</span>}
                    {job.check && (
                      <span className={job.check.ok ? undefined : "bad"}>
                        {job.check.ok ? t("backupCheckOk") : t("backupCheckFail", { why: job.check.problems.join("; ") })}
                      </span>
                    )}
                  </div>
                  {job.filename && <div className="backup-row-file">{job.filename}</div>}
                  {job.error && <div className="backup-row-error">{job.error}</div>}
                </div>
                {job.status !== "running" && (
                  <div className="backup-row-actions">
                    {job.status === "done" && (job.kind === "db" || job.kind === "storage") && (
                      <>
                        <a className="btn btn-primary btn-sm" href={`/api/v1/system/backups/jobs/${job.id}/file`}>
                          <Icon name="download" size={13} />
                          {t("backupDownload")}
                        </a>
                        <button className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => void check(job)}>
                          {t("backupCheck")}
                        </button>
                      </>
                    )}
                    <button className="text-link danger" disabled={guard.busy} onClick={() => setDeleteAsk(job)}>
                      {t("backupDelete")}
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {canRestore && (
        <div className="card backup-card backup-card-danger">
          <div className="backup-card-title">{t("backupRestoreTitle")}</div>
          <div className="field-desc">
            {restoreKind === "db" ? t("backupRestoreSub") : t("backupRestoreStorageSub")}
          </div>
          <div className="backup-restore">
            <div>
              <label className="label">{t("backupRestoreWhat")}</label>
              <select className="input" value={restoreKind} onChange={(e) => setRestoreKind(e.target.value as Kind)}>
                <option value="db">{t("backupTakeDb")}</option>
                <option value="storage">{t("backupTakeStorage")}</option>
              </select>
            </div>
            <div>
              <label className="label">{t("backupRestoreFile")}</label>
              <input type="file" onChange={(e) => setRestoreFile(e.target.files?.[0] ?? null)} />
            </div>
            <div className="backup-restore-go">
              <button
                className="btn btn-secondary btn-danger-outline"
                disabled={guard.busy || !restoreFile || !key.exists || running}
                onClick={() => setRestoreAsk(true)}
              >
                {t("backupRestoreStart")}
              </button>
              {restoreProgress !== null && <span className="backup-note">{restoreProgress}%</span>}
            </div>
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
      {deleteAsk && (
        <ConfirmModal
          text={t("backupDeleteConfirm")}
          confirmLabel={t("backupDelete")}
          danger
          onConfirm={() => { const job = deleteAsk; setDeleteAsk(null); void remove(job); }}
          onClose={() => setDeleteAsk(null)}
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
