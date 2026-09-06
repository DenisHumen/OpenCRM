import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Chip, LoadFailed } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useLiveTopic } from "../lib/live";

export interface KlyuchSayta {
  id: number;
  name: string;
  state: string;
  rate_per_min: number;
}

interface Svodka {
  today: number;
  week: number;
  month: number;
  rejected_month: number;
  peak_hour: number;
  rate_per_min: number;
  by_hour: { hour: string; count: number }[];
}

/** Виджет сводки «ключ сайта»: обращения за сегодня, неделю, месяц, отказы и
 *  ряд по часам — то же, что в настройках ключа, только под рукой. Ключ
 *  приходит из справочника сводки: виджет без ключа (удалили, отозвали)
 *  говорит об этом словами и просит убрать себя, а не показывает нули. */
export function VidzhetKlyucha({ klyuch, keyId }: { klyuch: KlyuchSayta | undefined; keyId: number }) {
  const { t } = useApp();
  const [svodka, setSvodka] = useState<Svodka | null>(null);
  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    if (!klyuch) return;
    clear();
    try {
      setSvodka(await api.get<Svodka>(`/settings/api-keys/${keyId}/stats`));
    } catch (e) {
      fail(e);
    }
  }, [keyId, klyuch, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  useLiveTopic("api_keys", (s) => {
    if (s.resync || s.hints.some((h) => h.id === keyId)) void load();
  });

  const maxChas = svodka ? Math.max(1, ...svodka.by_hour.map((h) => h.count)) : 1;
  return (
    <div className="card card-pad">
      <div className="section-head" style={{ marginBottom: 12 }}>
        <div className="metric-title">
          {t("dashApiKey")}
          <span style={{ color: "var(--text)" }}>{klyuch ? klyuch.name : `#${keyId}`}</span>
          {klyuch && klyuch.state !== "active" && (
            <Chip variant="warning">{t(klyuch.state === "revoked" ? "apiKeyStateRevoked" : "apiKeyStateExpired")}</Chip>
          )}
        </div>
        <Link to="/settings/api-keys" className="section-link">
          {t("viewAll")}
        </Link>
      </div>
      {!klyuch ? (
        <div className="field-desc" style={{ marginTop: 0 }}>{t("dashApiKeyGone")}</div>
      ) : failure !== null ? (
        <LoadFailed error={failure} onRetry={() => void load()} />
      ) : svodka === null ? (
        <div className="field-desc" style={{ marginTop: 0 }}>{t("loading")}</div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 28, flexWrap: "wrap", marginBottom: 12 }}>
            <div>
              <div className="metric-title">{t("apiStatsToday")}</div>
              <div className="metric-value">{svodka.today}</div>
            </div>
            <div>
              <div className="metric-title">{t("apiStatsWeek")}</div>
              <div className="metric-value">{svodka.week}</div>
            </div>
            <div>
              <div className="metric-title">{t("apiStatsMonth")}</div>
              <div className="metric-value">{svodka.month}</div>
              <div className="metric-sub" style={svodka.rejected_month > 0 ? { color: "var(--warning)" } : undefined}>
                {t("apiStatsRejected", { n: svodka.rejected_month })}
              </div>
            </div>
            <div>
              <div className="metric-title">{t("apiStatsPeak")}</div>
              <div className="metric-value">{svodka.peak_hour}</div>
              <div className="metric-sub">{t("apiStatsPeakSub", { n: svodka.rate_per_min })}</div>
            </div>
          </div>
          <div className="metric-title">{t("apiStatsByHour")}</div>
          <div className="bars stat-bars">
            {svodka.by_hour.map((h, i) => (
              <div className="bar-col" key={h.hour}>
                <div
                  className={"bar stat-bar" + (h.count === maxChas && h.count > 0 ? " top" : "")}
                  style={{ height: Math.max(3, Math.round((h.count / maxChas) * 44)) }}
                  title={`${h.hour.slice(11, 16)}: ${h.count}`}
                />
                <span className="bar-label">{i % 6 === 5 ? h.hour.slice(11, 13) : ""}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
