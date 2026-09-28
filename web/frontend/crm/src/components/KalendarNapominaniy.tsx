import { useCallback, useEffect, useMemo, useState } from "react";

import { Icon } from "./Icon";
import { StrokaNapominaniya } from "./StrokaNapominaniya";
import { EmptyState, LoadFailed, Spinner } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { parseDate } from "../lib/format";
import { useLiveTopic } from "../lib/live";
import { vazhnost } from "../lib/vazhnost";

interface Raz {
  srok: string;
  sdelan: boolean;
  budushchiy: boolean;
  task: any;
}

/** Местная дата ключом «2026-09-28»: день в сетке — местный, а не UTC. */
function klyuchDnya(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Шесть недель от понедельника до воскресенья вокруг месяца — сетка не прыгает по высоте. */
function setkaMesyatsa(mesyats: Date): Date[] {
  const pervoe = new Date(mesyats.getFullYear(), mesyats.getMonth(), 1);
  const nachalo = new Date(pervoe);
  nachalo.setDate(1 - ((pervoe.getDay() + 6) % 7));
  return Array.from({ length: 42 }, (_, i) => new Date(nachalo.getFullYear(), nachalo.getMonth(), nachalo.getDate() + i));
}

/** «сентябрь 2026 г.» → «Сентябрь 2026 г.»: заглавная только первая, как в русском. */
function sZaglavnoy(tekst: string): string {
  return tekst.charAt(0).toUpperCase() + tekst.slice(1);
}

/** Больше кружков в клетке не рисуем: дальше — «ещё N». */
const TOCHEK = 4;

/**
 * Календарь напоминаний: месяц с кружками по дням и панель выбранного дня.
 *
 * Просьба владельца 28.09.2026: «на датах кружочки с задачами, нажал на дату —
 * сбоку всё красиво разложено, с переходами в заявки». Разы повторяющихся
 * считает сервер (`GET /tasks/calendar`): будущие раскладываются по правилу.
 */
export function KalendarNapominaniy({
  kto,
  onOpen,
  versiya,
}: {
  kto: string;
  onOpen: (id: number) => void;
  /** Растёт после правки снаружи (карточка, быстрый ввод) — календарь перечитывается. */
  versiya: number;
}) {
  const { t, locale, user, toastError, refreshTasks } = useApp();
  const [mesyats, setMesyats] = useState(() => {
    const d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), 1);
  });
  const [vybran, setVybran] = useState(() => klyuchDnya(new Date()));
  const [razy, setRazy] = useState<Raz[] | null>(null);
  const [popytka, setPopytka] = useState(0);
  const [nazvanie, setNazvanie] = useState("");
  const [chas, setChas] = useState("09:00");
  const { failure, fail, clear } = useFailure();
  const guard = useGuard();
  const perechitat = useCallback(() => setPopytka((n) => n + 1), []);
  useLiveTopic("tasks", perechitat);

  const dni = useMemo(() => setkaMesyatsa(mesyats), [mesyats]);

  useEffect(() => {
    let tekushchiy = true;
    clear();
    const s = dni[0];
    const po = new Date(dni[41].getFullYear(), dni[41].getMonth(), dni[41].getDate() + 1);
    api
      .get<{ items: Raz[] }>(`/tasks/calendar?s=${encodeURIComponent(s.toISOString())}&po=${encodeURIComponent(po.toISOString())}`)
      .then((otvet) => {
        if (tekushchiy) setRazy(otvet.items);
      })
      .catch((e) => {
        if (tekushchiy) fail(e);
      });
    return () => {
      tekushchiy = false;
    };
  }, [dni, popytka, versiya, clear, fail]);

  // Отбор «чьи» — здесь, а не запросом: разы уже посчитаны, а переключают его часто.
  const vidimye = useMemo(() => {
    if (!razy) return [];
    if (kto === "moi") return razy.filter((r) => r.task.moyo);
    if (kto === "polka") return razy.filter((r) => r.task.obshchee);
    if (kto === "postavil") {
      return razy.filter((r) => r.task.created_by === user?.id && r.task.lyudi?.some((c: any) => c.poluchaet && c.user_id !== user?.id));
    }
    return razy;
  }, [razy, kto, user]);

  const poDnyam = useMemo(() => {
    const karta = new Map<string, Raz[]>();
    for (const raz of vidimye) {
      const klyuch = klyuchDnya(parseDate(raz.srok)!);
      if (!karta.has(klyuch)) karta.set(klyuch, []);
      karta.get(klyuch)!.push(raz);
    }
    return karta;
  }, [vidimye]);

  const segodnya = klyuchDnya(new Date());
  const naDen = poDnyam.get(vybran) ?? [];
  const nazvanieMesyatsa = sZaglavnoy(new Intl.DateTimeFormat(locale, { month: "long", year: "numeric" }).format(mesyats));
  const dniNedeli = Array.from({ length: 7 }, (_, i) =>
    new Intl.DateTimeFormat(locale, { weekday: "short" }).format(new Date(2026, 8, 28 + i)),
  );
  const [god, mes, chislo] = vybran.split("-").map(Number);
  const vybranDate = new Date(god, mes - 1, chislo);
  const zagolovokDnya = sZaglavnoy(new Intl.DateTimeFormat(locale, { weekday: "long", day: "numeric", month: "long" }).format(vybranDate));

  const listat = (sdvig: number) => setMesyats((m) => new Date(m.getFullYear(), m.getMonth() + sdvig, 1));
  const kSegodnya = () => {
    const d = new Date();
    setMesyats(new Date(d.getFullYear(), d.getMonth(), 1));
    setVybran(klyuchDnya(d));
  };

  const dobavit = async () => {
    const tekst = nazvanie.trim();
    if (!tekst || !guard.take()) return;
    const [h, m] = chas.split(":").map(Number);
    const srok = new Date(god, mes - 1, chislo, h || 0, m || 0);
    try {
      await api.post("/tasks", {
        title: tekst,
        due_at: srok.toISOString(),
        poyas: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
      setNazvanie("");
      perechitat();
      void refreshTasks();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const izmeneno = () => {
    perechitat();
    void refreshTasks();
  };

  return (
    <div className="kalendar-napom">
      <section className="kalendar-mesyats" aria-label={nazvanieMesyatsa}>
        <div className="kalendar-shapka">
          <button type="button" className="btn-icon" onClick={() => listat(-1)} aria-label={t("napomRanshe")}>
            <Icon name="chevronLeft" size={16} />
          </button>
          <h2 className="kalendar-nazvanie">{nazvanieMesyatsa}</h2>
          <button type="button" className="btn-icon" onClick={() => listat(1)} aria-label={t("napomPozzhe")}>
            <Icon name="chevronRight" size={16} />
          </button>
          <button type="button" className="btn btn-secondary btn-sm kalendar-segodnya" onClick={kSegodnya}>
            {t("napomSegodnya")}
          </button>
        </div>
        {failure ? (
          <LoadFailed error={failure} onRetry={perechitat} />
        ) : (
          <div className="kalendar-setka" role="grid">
            {dniNedeli.map((imya) => (
              <div key={imya} className="kalendar-den-nedeli" role="columnheader">
                {imya}
              </div>
            ))}
            {dni.map((den) => {
              const klyuch = klyuchDnya(den);
              const svoi = poDnyam.get(klyuch) ?? [];
              const prosrocheno = svoi.some((r) => !r.sdelan && !r.budushchiy && parseDate(r.srok)!.getTime() < Date.now());
              const klass =
                "kalendar-den" +
                (den.getMonth() !== mesyats.getMonth() ? " chuzhoy" : "") +
                (klyuch === segodnya ? " segodnya" : "") +
                (klyuch === vybran ? " vybran" : "") +
                (prosrocheno ? " prosrocheno" : "");
              return (
                <button
                  key={klyuch}
                  type="button"
                  role="gridcell"
                  aria-selected={klyuch === vybran}
                  aria-label={`${den.toLocaleDateString(locale)} · ${svoi.length}`}
                  className={klass}
                  onClick={() => setVybran(klyuch)}
                >
                  <span className="kalendar-chislo">{den.getDate()}</span>
                  {svoi.length > 0 && (
                    <span className="kalendar-tochki">
                      {svoi.slice(0, TOCHEK).map((r, i) => (
                        <i
                          key={i}
                          className={"kalendar-tochka " + vazhnost(r.task.vazhnost) + (r.sdelan ? " sdelan" : "")}
                        />
                      ))}
                      {svoi.length > TOCHEK && <span className="kalendar-eshchyo">{t("napomEshchyo", { n: svoi.length - TOCHEK })}</span>}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}
        {!razy && !failure && (
          <div className="kalendar-zhdyom">
            <Spinner />
          </div>
        )}
      </section>

      <aside className="kalendar-panel" aria-label={zagolovokDnya}>
        <div className="kalendar-panel-shapka">
          <div className="kalendar-panel-den">{zagolovokDnya}</div>
          <div className="kalendar-panel-schyot">{naDen.length}</div>
        </div>
        <div className="kalendar-panel-vvod">
          <input
            className="input"
            placeholder={t("napomNaDen")}
            value={nazvanie}
            onChange={(e) => setNazvanie(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void dobavit();
            }}
          />
          <input
            className="input kalendar-chas"
            type="time"
            value={chas}
            aria-label={t("tasksDueAt")}
            onChange={(e) => setChas(e.target.value)}
          />
          <button
            className="btn btn-primary btn-sm"
            aria-label={t("add")}
            disabled={guard.busy || !nazvanie.trim()}
            onClick={() => void dobavit()}
          >
            <Icon name="plus" stroke={2} />
          </button>
        </div>
        {naDen.length === 0 ? (
          <EmptyState icon="calendar" title={t("napomDenPusto")} />
        ) : (
          <div className="list-card kalendar-lenta">
            {naDen.map((raz) => (
              <StrokaNapominaniya
                key={`${raz.task.id}-${raz.srok}`}
                task={raz.task}
                sdelan={raz.sdelan}
                budushchiy={raz.budushchiy}
                sSrokom={false}
                vremya={
                  raz.task.ves_den
                    ? t("napomVesDen")
                    : new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(parseDate(raz.srok)!)
                }
                onOpen={onOpen}
                onChanged={izmeneno}
              />
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}
