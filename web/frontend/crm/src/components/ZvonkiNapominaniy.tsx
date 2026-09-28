import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Icon } from "./Icon";
import { otlozhitDo } from "./StrokaNapominaniya";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useGuard } from "../lib/guard";
import { formatDateTime } from "../lib/format";
import { useLiveTopic } from "../lib/live";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { nachat_vybory, signal_napominaniya } from "../lib/signaly";
import { srochno } from "../lib/vazhnost";

interface Zvonok {
  id: number;
  task_id: number;
  title: string;
  vazhnost: string;
  vid: "early" | "due" | "nag" | "snooze";
  srok: string;
  moment: string;
}

const VID_ZVONKA = {
  early: "zvonokVidEarly",
  due: "zvonokVidDue",
  nag: "zvonokVidNag",
  snooze: "zvonokVidSnooze",
} as const;

/** Запасной опрос: живой поток мог оборваться, а звонок пропускать нельзя. */
const OPROS_MS = 60_000;

/**
 * Звонок напоминания в приложении: окошко внизу справа, звук и системное окно.
 *
 * Сервер записывает звонок в его минуту (docs/bloki/29 §6) и шлёт намёк только
 * адресату; вкладка забирает несыгранные и показывает. «Готово», «Отложить» и
 * «Открыть» отмечают звонок увиденным — настойчивость смолкает.
 */
export function ZvonkiNapominaniy() {
  const { t, locale, modules, user, toastError } = useApp();
  const navigate = useNavigate();
  const dostupen = moduleOn(modules, "tasks") && can(user, "tasks.view");
  const [zvonki, setZvonki] = useState<Zvonok[]>([]);
  const vidennye = useRef(new Set<number>());
  const guard = useGuard();

  const zabrat = useCallback(async () => {
    if (!dostupen) return;
    try {
      const otvet = await api.get<{ items: Zvonok[] }>("/tasks/signals");
      const novye = otvet.items.filter((z) => !vidennye.current.has(z.id));
      for (const z of novye) {
        vidennye.current.add(z.id);
        signal_napominaniya({
          zagolovok: `${t(VID_ZVONKA[z.vid])} · ${t("zvonokZagolovok")}`,
          telo: z.title,
          metka: `opencrm-napom-${z.task_id}`,
          srochno: srochno(z.vazhnost),
          onOpen: () => navigate(`/tasks?open=${z.task_id}`),
        });
      }
      setZvonki(otvet.items);
    } catch {
      /* звонок придёт со следующим опросом — ронять экран незачем */
    }
  }, [dostupen, t, navigate]);

  useLiveTopic("task_signals", () => void zabrat());

  useEffect(() => {
    if (!dostupen) return;
    nachat_vybory();
    void zabrat();
    // Фоновую вкладку будит живой намёк; опрос — только видимой, чтобы
    // забытая вкладка не ходила на сервер круглосуточно.
    const opros = window.setInterval(() => {
      if (document.visibilityState === "visible") void zabrat();
    }, OPROS_MS);
    const vernulis = () => {
      if (document.visibilityState === "visible") void zabrat();
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      window.clearInterval(opros);
      document.removeEventListener("visibilitychange", vernulis);
    };
  }, [dostupen, zabrat]);

  const ubrat = (task_id: number) => setZvonki((spisok) => spisok.filter((z) => z.task_id !== task_id));

  const prinyat = async (z: Zvonok) => {
    ubrat(z.task_id);
    const svoi = zvonki.filter((x) => x.task_id === z.task_id).map((x) => x.id);
    await api.post("/tasks/signals/ack", { ids: svoi }).catch(() => undefined);
  };

  const deystvie = async (z: Zvonok, put: string, telo?: unknown) => {
    if (!guard.take()) return;
    ubrat(z.task_id);
    try {
      await api.post(`/tasks/${z.task_id}/${put}`, telo);
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
    await prinyat(z);
  };

  if (!dostupen || zvonki.length === 0) return null;
  // Одно окошко на напоминание: настойчивый повтор не должен множить окна.
  const poOdnomu = zvonki.filter((z, i) => zvonki.findIndex((x) => x.task_id === z.task_id) === i).slice(0, 4);

  return (
    <div className="zvonki" role="region" aria-live="assertive" aria-label={t("zvonokZagolovok")}>
      {poOdnomu.map((z) => (
        <div key={z.task_id} className={"zvonok" + (srochno(z.vazhnost) ? " urgent" : "")} role="alertdialog" aria-label={z.title}>
          <div className="zvonok-shapka">
            <Icon name="bell" size={14} />
            <span>{t(VID_ZVONKA[z.vid])}</span>
            <span className="zvonok-kogda">{formatDateTime(z.srok, locale)}</span>
            <button type="button" className="napom-snyat" aria-label="×" onClick={() => void prinyat(z)}>
              ×
            </button>
          </div>
          <div className="zvonok-nazvanie">{z.title}</div>
          <div className="zvonok-knopki">
            <button type="button" className="btn btn-primary btn-sm" onClick={() => void deystvie(z, "done", { srok: z.srok })}>
              <Icon name="check" size={13} stroke={2} />
              {t("napomGotovo")}
            </button>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => void deystvie(z, "snooze", { do: otlozhitDo("10") })}>
              {t("otlozhit10")}
            </button>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => void deystvie(z, "snooze", { do: otlozhitDo("60") })}>
              {t("otlozhit60")}
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                void prinyat(z);
                navigate(`/tasks?open=${z.task_id}`);
              }}
            >
              {t("napomOtkryt")}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
