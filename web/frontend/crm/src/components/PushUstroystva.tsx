import { useCallback, useEffect, useState } from "react";

import { Chip, LoadFailed } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDateTime } from "../lib/format";
import { useGuard } from "../lib/guard";
import {
  otklyuchitPush,
  otpechatok,
  pushPodderzhan,
  tekushchaya,
  vklyuchitPush,
  type PushPodpiska,
} from "../lib/push";

/** Звонок напоминания при закрытой вкладке — устройства сотрудника (docs/bloki/31-web-push.md). */
export function PushUstroystva() {
  const { t, locale, toast, toastError } = useApp();
  const guard = useGuard();
  const { failure, fail, clear } = useFailure();
  const [spisok, setSpisok] = useState<PushPodpiska[] | null>(null);
  const [etot, setEtot] = useState<string | null>(null);

  const load = useCallback(() => {
    clear();
    Promise.all([api.get<{ items: PushPodpiska[] }>("/push/subscriptions"), tekushchaya()])
      .then(async ([otvet, sub]) => {
        setSpisok(otvet.items);
        setEtot(sub ? await otpechatok(sub.endpoint) : null);
      })
      .catch(fail);
  }, [clear, fail]);

  useEffect(load, [load]);

  const vklyuchit = async () => {
    if (!guard.take()) return;
    try {
      const itog = await vklyuchitPush();
      if (itog === "denied") toast(t("pushDenied"));
      else if (itog === "unsupported") toast(t("pushUnsupported"));
      else toast(t("pushEnabled"));
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const otklyuchit = async (podpiska: PushPodpiska) => {
    if (!guard.take()) return;
    try {
      await otklyuchitPush(podpiska);
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const zdesVklyucheno = !!spisok?.some((p) => p.endpoint_hash === etot);

  return (
    <div className="card card-pad" style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 6 }}>{t("pushTitle")}</div>
      <div style={{ color: "var(--faint)", fontSize: 11.5, marginBottom: 12 }}>{t("pushHint")}</div>
      {!pushPodderzhan() ? (
        <div style={{ color: "var(--warning)", fontSize: 12.5 }}>{t("pushUnsupported")}</div>
      ) : (
        !zdesVklyucheno && (
          <button type="button" className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => void vklyuchit()}>
            {t("pushOn")}
          </button>
        )
      )}
      {failure ? (
        <LoadFailed error={failure} onRetry={load} />
      ) : (
        spisok &&
        spisok.length > 0 && (
          <div className="list-card" style={{ marginTop: 12 }}>
            {spisok.map((p) => (
              <div key={p.id} className="list-row" style={{ height: "auto", padding: "10px 14px", gap: 10 }}>
                <div style={{ flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.5 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <strong>{p.nazvanie || t("pushDevice")}</strong>
                    {p.endpoint_hash === etot && <Chip variant="success">{t("pushThisDevice")}</Chip>}
                  </div>
                  <div style={{ color: "var(--faint)", fontSize: 12 }}>
                    {p.last_ok_at ? t("pushLastOk", { t: formatDateTime(p.last_ok_at, locale) }) : t("pushNeverSent")}
                  </div>
                </div>
                <button type="button" className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={() => void otklyuchit(p)}>
                  {t("pushOff")}
                </button>
              </div>
            ))}
          </div>
        )
      )}
    </div>
  );
}
