import { useCallback, useEffect, useState } from "react";

import { Chip, ConfirmModal, LoadFailed } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDateTime } from "../lib/format";
import { useGuard } from "../lib/guard";

interface Chistka {
  at: string;
  kak: "avto" | "vruchnuyu";
  kto: string;
  ok: boolean;
  oshibka: string;
  osvobozhdeno: number | null;
}

interface Sostoyanie {
  sluzhba: boolean;
  izmereno: string | null;
  razmer: number | null;
  osvobodimo: number | null;
  svobodno_mb: number | null;
  ostavlyat_gb: number | null;
  poslednyaya: Chistka | null;
  zapros: { at: string; kto: string } | null;
  zapros_zabyt: boolean;
}

/** Кэш сборки docker: чистит служба обновления, экран только просит (docs/ekspluatatsiya/08, «Кэш сборки»). */
export function KeshSborki() {
  const { t, locale, toastError } = useApp();
  const guard = useGuard();
  const { failure, fail, clear } = useFailure();
  const [data, setData] = useState<Sostoyanie | null>(null);
  const [sprosit, setSprosit] = useState(false);

  const load = useCallback(() => {
    clear();
    api.get<Sostoyanie>("/system/build-cache").then(setData).catch(fail);
  }, [clear, fail]);

  useEffect(load, [load]);

  // Пока просьба лежит, служба заберёт её за секунды — перечитываем, пока вкладку видно.
  const zhdyom = !!data?.zapros;
  useEffect(() => {
    if (!zhdyom) return;
    const tik = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 5000);
    return () => window.clearInterval(tik);
  }, [zhdyom, load]);

  // Байты — десятичными гигабайтами, как их пишет docker и сообщение бота.
  const razmer = (bayt: number | null | undefined) => {
    if (bayt === null || bayt === undefined) return "—";
    const gb = bayt >= 1e9;
    const chislo = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(bayt / (gb ? 1e9 : 1e6));
    return `${chislo} ${gb ? (locale === "ru" ? "ГБ" : "GB") : locale === "ru" ? "МБ" : "MB"}`;
  };

  const ochistit = async () => {
    if (!guard.take()) return;
    try {
      setData(await api.post<Sostoyanie>("/system/build-cache/purge"));
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const posl = data?.poslednyaya;
  return (
    <div className="card card-pad" style={{ marginTop: 20 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginBottom: 4, flexWrap: "wrap" }}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>{t("buildCache")}</div>
        {data?.sluzhba && !data.zapros && (
          <button type="button" className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => setSprosit(true)}>
            {t("buildCachePurge")}
          </button>
        )}
        {data?.zapros && <Chip variant="warning">{t("buildCacheWaiting")}</Chip>}
      </div>
      <div style={{ color: "var(--faint)", fontSize: 12.5, marginBottom: 14, lineHeight: 1.5 }}>
        {t("buildCacheSub", { n: data?.ostavlyat_gb ?? 5 })}
      </div>
      {failure ? (
        <LoadFailed error={failure} onRetry={load} />
      ) : !data ? (
        <div style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("loading")}</div>
      ) : !data.sluzhba ? (
        <div style={{ color: "var(--warning)", fontSize: 12.5, lineHeight: 1.5 }}>{t("buildCacheNoService")}</div>
      ) : (
        <div style={{ fontSize: 13, lineHeight: 1.7 }}>
          <div>
            {t("buildCacheSize")}: <strong>{razmer(data.razmer)}</strong>
            {" · "}
            {t("buildCacheReclaimable")}: {razmer(data.osvobodimo)}
            {data.svobodno_mb !== null && (
              <>
                {" · "}
                {t("buildCacheDiskFree")}: {razmer(data.svobodno_mb * 1024 * 1024)}
              </>
            )}
          </div>
          {data.izmereno && (
            <div style={{ color: "var(--faint)", fontSize: 12 }}>
              {t("buildCacheMeasured", { t: formatDateTime(data.izmereno, locale) })}
            </div>
          )}
          {posl && (
            <div style={{ color: posl.ok ? "var(--faint)" : "var(--danger)", fontSize: 12.5, marginTop: 6 }}>
              {!posl.ok
                ? t("buildCacheLastFailed", { t: formatDateTime(posl.at, locale), why: posl.oshibka })
                : posl.kak === "avto"
                  ? t("buildCacheLastAuto", { t: formatDateTime(posl.at, locale), n: razmer(posl.osvobozhdeno) })
                  : t("buildCacheLastManual", { t: formatDateTime(posl.at, locale), n: razmer(posl.osvobozhdeno), who: posl.kto || "—" })}
            </div>
          )}
          {data.zapros_zabyt && (
            <div style={{ color: "var(--warning)", fontSize: 12.5, marginTop: 6 }}>{t("buildCacheStuck")}</div>
          )}
        </div>
      )}
      {sprosit && (
        <ConfirmModal
          text={t("buildCachePurgeConfirm")}
          confirmLabel={t("buildCachePurge")}
          onConfirm={() => { setSprosit(false); void ochistit(); }}
          onClose={() => setSprosit(false)}
        />
      )}
    </div>
  );
}
