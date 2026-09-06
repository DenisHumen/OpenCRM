import { useCallback, useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useGuard } from "../lib/guard";
import { can } from "../lib/permissions";

interface Sostoyanie {
  wanted: boolean;
  ready: boolean;
  running: boolean;
  percent: number;
  error: string;
  rings: number;
}

/** Пока качается — спрашиваем чаще: полоса должна ехать, а не прыгать. */
const CHASTO_MS = 3000;
const REDKO_MS = 30_000;

/**
 * Ход докачки подробных очертаний планеты на сводке.
 *
 * Заведён по просьбе владельца: «если нужно что-то подкачивать, на главной
 * страничке появляется прогресс-бар и уведомление». Планета работает и без
 * этого — виджет показывает необязательное улучшение, а не беду.
 */
export function VidzhetKarty() {
  const { t, user } = useApp();
  const [sostoyanie, setSostoyanie] = useState<Sostoyanie | null>(null);
  const guard = useGuard();

  const sprosit = useCallback(async () => {
    try {
      setSostoyanie(await api.get<Sostoyanie>("/globe/detail"));
    } catch {
      // Блок выключили или сети нет — виджет молчит, сводка от этого не рушится.
    }
  }, []);

  useEffect(() => {
    void sprosit();
    // Только пока вкладка на переднем плане: забытая сводка не должна
    // спрашивать сервер круглосуточно (приём тот же, что у самой сводки).
    const vidno = () => document.visibilityState === "visible";
    const chasy = window.setInterval(() => {
      if (vidno()) void sprosit();
    }, sostoyanie?.running || sostoyanie?.wanted ? CHASTO_MS : REDKO_MS);
    const vernulis = () => {
      if (vidno()) void sprosit();
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      window.clearInterval(chasy);
      document.removeEventListener("visibilitychange", vernulis);
    };
  }, [sprosit, sostoyanie?.running, sostoyanie?.wanted]);

  const vklyuchit = () => {
    if (!guard.take()) return;
    api
      .post<Sostoyanie>("/globe/detail")
      .then(setSostoyanie)
      .catch(() => undefined)
      .finally(() => guard.free());
  };

  return (
    <div className="card card-pad">
      <div className="metric-title" style={{ marginBottom: 12 }}>
        <Icon name="globe" size={14} />
        {t("globeDetail")}
      </div>
      {sostoyanie?.running ? (
        <>
          <div className="metric-value">{sostoyanie.percent}%</div>
          <div className="karta-hod">
            <span className="karta-hod-polosa" style={{ width: `${sostoyanie.percent}%` }} />
          </div>
          <div className="metric-sub">{t("globeDetailRunning")}</div>
        </>
      ) : sostoyanie?.ready ? (
        <>
          <div className="metric-value">{sostoyanie.rings}</div>
          <div className="metric-sub">{t("globeDetailReady")}</div>
        </>
      ) : (
        <>
          <div className="metric-sub" style={{ marginBottom: 10 }}>
            {sostoyanie?.wanted ? t("globeDetailWaiting") : t("globeDetailHint")}
          </div>
          {!sostoyanie?.wanted && can(user, "settings.manage") && (
            <button type="button" className="btn btn-secondary btn-sm" disabled={guard.busy} onClick={vklyuchit}>
              <Icon name="download" size={13} />
              {t("globeDetailOn")}
            </button>
          )}
        </>
      )}
    </div>
  );
}
