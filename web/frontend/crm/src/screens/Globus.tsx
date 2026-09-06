import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type WheelEvent } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDateTime, formatMoney } from "../lib/format";
import { KONTURY } from "../lib/globus/mir";
import {
  korotkiy_povorot,
  obratno,
  plavno,
  svernut,
  type Vid,
} from "../lib/globus/proekciya";
import {
  klyuch,
  narisovat,
  pod_kursorom,
  vidno_sloyu,
  type Baza,
  type Cveta,
  type Svyaz,
  type Tochka,
} from "../lib/globus/risovanie";
import { useGuard } from "../lib/guard";
import type { TranslationKey } from "../lib/i18n";
import { useLiveTopic } from "../lib/live";
import { can } from "../lib/permissions";

interface Kartina {
  base: (Baza & { strana?: string }) | null;
  points: Tochka[];
  links: Svyaz[];
  layers: string[];
  countries: { code: string; name: string; clients: number }[];
  totals: { clients: number; no_place: number; countries: number; visitors: number; links: number };
  at: string;
}

interface Podrobno {
  wanted: boolean;
  ready: boolean;
  running: boolean;
  percent: number;
  error: string;
  rings: number;
  at: string;
}

/** Слои и их подписи. Порядок — тот же, что на панели у образца. */
const SLOI: { key: string; label: TranslationKey }[] = [
  { key: "clients", label: "globeLayerClients" },
  { key: "deals", label: "globeLayerDeals" },
  { key: "orders", label: "globeLayerOrders" },
  { key: "overdue", label: "globeLayerOverdue" },
  { key: "visitors", label: "globeLayerVisitors" },
  { key: "links", label: "globeLayerLinks" },
  { key: "grid", label: "globeLayerGrid" },
  { key: "labels", label: "globeLayerLabels" },
  { key: "night", label: "globeLayerNight" },
];

const TOCHNOST: Record<string, TranslationKey> = {
  tochka: "globePrecisionPoint",
  gorod: "globePrecisionCity",
  strana: "globePrecisionCountry",
};

const PAMYAT = "opencrm_globus_sloi";
/** Как часто спрашиваем ход докачки, пока она идёт. */
const OPROS_MS = 4000;
/** Медленное вращение, градусов в секунду: планета живая, но не мельтешит. */
const VRASHCHENIE = 2;

function pamyat_sloev(dostupnye: string[]): Set<string> {
  try {
    const sohraneno = localStorage.getItem(PAMYAT);
    if (sohraneno) return new Set(JSON.parse(sohraneno) as string[]);
  } catch {
    // Место занято или запрещено — покажем всё, это не беда.
  }
  return new Set(dostupnye);
}

function cveta_temy(): Cveta {
  const stil = getComputedStyle(document.documentElement);
  const vzyat = (imya: string) => stil.getPropertyValue(imya).trim();
  return {
    more: vzyat("--globus-more"),
    susha: vzyat("--globus-susha"),
    bereg: vzyat("--globus-bereg"),
    setka: vzyat("--globus-setka"),
    limb: vzyat("--globus-limb"),
    noch: vzyat("--globus-noch"),
    zoloto: vzyat("--globus-zoloto"),
    cian: vzyat("--globus-cian"),
    uspekh: vzyat("--globus-uspekh"),
    trevoga: vzyat("--globus-trevoga"),
    beda: vzyat("--globus-beda"),
    tekst: vzyat("--globus-tekst"),
    tusklo: vzyat("--globus-tusklo"),
  };
}

/** Экран «Глобус»: клиенты, связи и гости витрин на планете. */
export function Globus() {
  const { t, user, locale, workspace } = useApp();
  const { failure, fail, clear } = useFailure();
  const [dannye, setDannye] = useState<Kartina | null>(null);
  const [podrobno, setPodrobno] = useState<Podrobno | null>(null);
  const [sloi, setSloi] = useState<Set<string> | null>(null);
  const [vybrano, setVybrano] = useState<Tochka | null>(null);
  const [pod, setPod] = useState<{ lon: number; lat: number } | null>(null);
  const [zapros, setZapros] = useState(0);
  const guard = useGuard();

  const holst = useRef<HTMLCanvasElement | null>(null);
  const korob = useRef<HTMLDivElement | null>(null);
  const vid = useRef<Vid>({ lon0: 20, lat0: 25, r: 260, cx: 0, cy: 0 });
  const cel = useRef<{ lon0: number; lat0: number; nachato: number } | null>(null);
  const tyanem = useRef<{ x: number; y: number } | null>(null);
  const kadr = useRef<{ tochki: Tochka[]; svyazi: Svyaz[]; baza: Baza | null }>({
    tochki: [],
    svyazi: [],
    baza: null,
  });
  const vybrano_ref = useRef<string | null>(null);
  const navedeno = useRef<string | null>(null);
  const sloi_ref = useRef<Set<string>>(new Set());
  const kontury = useRef<readonly (readonly number[])[]>(KONTURY);
  const nachalo = useRef<number>(0);

  const zagruzit = useCallback(() => {
    clear();
    api
      .get<Kartina>("/globe")
      .then((svezhee) => {
        setDannye(svezhee);
        setSloi((bylo) => bylo ?? pamyat_sloev(svezhee.layers));
      })
      .catch(fail);
  }, [clear, fail]);

  useEffect(() => zagruzit(), [zagruzit, zapros]);
  useLiveTopic(["clients", "deals", "documents", "boards"], () => setZapros((n) => n + 1));

  // Ход докачки подробных очертаний. Спрашиваем, пока идёт или пока ждём сеть:
  // сама служба решает, не рано ли пробовать снова.
  useEffect(() => {
    let zhivo = true;
    const sprosit = () => {
      api
        .get<Podrobno>("/globe/detail")
        .then((s) => {
          if (zhivo) setPodrobno(s);
        })
        .catch(() => undefined);
    };
    sprosit();
    // Свёрнутую вкладку не закрывают — её забывают: спрашиваем, только пока
    // на экран смотрят, и перечитываем сразу при возвращении.
    const vidno = () => document.visibilityState === "visible";
    const chasy = window.setInterval(() => {
      if (vidno()) sprosit();
    }, OPROS_MS);
    const vernulis = () => {
      if (vidno()) sprosit();
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      zhivo = false;
      window.clearInterval(chasy);
      document.removeEventListener("visibilitychange", vernulis);
    };
  }, []);

  // Подробные очертания приезжают отдельным файлом и заменяют вшитые.
  useEffect(() => {
    if (!podrobno?.ready) return;
    let zhivo = true;
    api
      .get<{ rings: number[][] }>("/globe/map")
      .then((otvet) => {
        if (zhivo && otvet.rings?.length) kontury.current = otvet.rings;
      })
      .catch(() => undefined);
    return () => {
      zhivo = false;
    };
  }, [podrobno?.ready]);

  useEffect(() => {
    if (!sloi) return;
    sloi_ref.current = sloi;
    try {
      localStorage.setItem(PAMYAT, JSON.stringify([...sloi]));
    } catch {
      // Не сохранилось — слои останутся на этот заход, и только.
    }
  }, [sloi]);

  useEffect(() => {
    vybrano_ref.current = vybrano ? klyuch(vybrano) : null;
  }, [vybrano]);

  useEffect(() => {
    if (!dannye) return;
    kadr.current = { tochki: dannye.points, svyazi: dannye.links, baza: dannye.base };
    nachalo.current = performance.now();
  }, [dannye]);

  /** Доворот планеты к точке: то самое «проворачивание», о котором просили. */
  const poehali = useCallback((lon: number, lat: number) => {
    const dvizhenie = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!dvizhenie) {
      vid.current.lon0 = lon;
      vid.current.lat0 = lat;
      return;
    }
    cel.current = {
      lon0: korotkiy_povorot(vid.current.lon0, lon),
      lat0: Math.max(-80, Math.min(80, lat)),
      nachato: performance.now(),
    };
  }, []);

  // Один цикл отрисовки на весь экран: состояние планеты живёт в ссылках, а не
  // в состоянии React, — иначе каждый кадр перерисовывал бы панели.
  const est_dannye = dannye !== null;
  useEffect(() => {
    const canvas = holst.current;
    const korobka = korob.current;
    if (!canvas || !korobka) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let zhivo = true;
    let cveta = cveta_temy();
    let poslednee = performance.now();

    const razmer = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = korobka.clientWidth;
      const h = korobka.clientHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      vid.current.cx = w / 2;
      vid.current.cy = h / 2;
      vid.current.bazovyy = Math.min(w, h) * 0.42;
      vid.current.r = vid.current.bazovyy * (vid.current.masshtab ?? 1);
    };
    const nablyudatel = new ResizeObserver(razmer);
    nablyudatel.observe(korobka);
    razmer();

    const shag = (teper: number) => {
      if (!zhivo) return;
      const proshlo = (teper - poslednee) / 1000;
      poslednee = teper;
      const dvizhenie = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      if (cel.current) {
        const dolya = Math.min(1, (teper - cel.current.nachato) / 900);
        const k = plavno(dolya);
        vid.current.lon0 += (cel.current.lon0 - vid.current.lon0) * k * 0.35;
        vid.current.lat0 += (cel.current.lat0 - vid.current.lat0) * k * 0.35;
        if (dolya >= 1) {
          vid.current.lon0 = svernut(cel.current.lon0);
          vid.current.lat0 = cel.current.lat0;
          cel.current = null;
        }
      } else if (dvizhenie && !tyanem.current && !vybrano_ref.current) {
        vid.current.lon0 = svernut(vid.current.lon0 + VRASHCHENIE * proshlo);
      }

      const vozrast = (teper - nachalo.current) / 600;
      narisovat(ctx, {
        vid: vid.current,
        kontury: kontury.current,
        tochki: kadr.current.tochki,
        svyazi: kadr.current.svyazi,
        baza: kadr.current.baza,
        sloi: sloi_ref.current,
        vybrano: vybrano_ref.current,
        navedeno: navedeno.current,
        cveta,
        poyavlenie: dvizhenie ? Math.max(0, Math.min(1, vozrast)) : 1,
        kogda: new Date(),
        puls: dvizhenie && vybrano_ref.current ? (teper / 2400) % 1 : 0,
      });
      window.requestAnimationFrame(shag);
    };
    window.requestAnimationFrame(shag);

    const tema = new MutationObserver(() => {
      cveta = cveta_temy();
    });
    tema.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    return () => {
      zhivo = false;
      nablyudatel.disconnect();
      tema.disconnect();
    };
  }, [est_dannye]);

  const mesto = (sobytie: { clientX: number; clientY: number }) => {
    const canvas = holst.current;
    if (!canvas) return { x: 0, y: 0 };
    const ramka = canvas.getBoundingClientRect();
    return { x: sobytie.clientX - ramka.left, y: sobytie.clientY - ramka.top };
  };

  const nazhali = (sobytie: MouseEvent<HTMLCanvasElement>) => {
    const { x, y } = mesto(sobytie);
    const tochka = pod_kursorom(
      { ...kadrDlyaPoiska(), vid: vid.current, sloi: sloi_ref.current },
      x,
      y,
    );
    if (tochka) {
      setVybrano(tochka);
      poehali(tochka.lon, tochka.lat);
    } else {
      setVybrano(null);
    }
  };

  const kadrDlyaPoiska = () => ({
    kontury: kontury.current,
    tochki: kadr.current.tochki,
    svyazi: kadr.current.svyazi,
    baza: kadr.current.baza,
    vybrano: vybrano_ref.current,
    navedeno: navedeno.current,
    cveta: cveta_temy(),
    poyavlenie: 1,
    kogda: new Date(),
    puls: 0,
    vid: vid.current,
    sloi: sloi_ref.current,
  });

  const dvinuli = (sobytie: MouseEvent<HTMLCanvasElement>) => {
    const { x, y } = mesto(sobytie);
    if (tyanem.current) {
      const dx = x - tyanem.current.x;
      const dy = y - tyanem.current.y;
      tyanem.current = { x, y };
      vid.current.lon0 = svernut(vid.current.lon0 - (dx * 90) / vid.current.r);
      vid.current.lat0 = Math.max(-85, Math.min(85, vid.current.lat0 + (dy * 90) / vid.current.r));
      cel.current = null;
      return;
    }
    const tochka = pod_kursorom({ ...kadrDlyaPoiska(), vid: vid.current, sloi: sloi_ref.current }, x, y);
    navedeno.current = tochka ? klyuch(tochka) : null;
    const gde = obratno(x, y, vid.current);
    setPod(gde);
  };

  const koleso = (sobytie: WheelEvent<HTMLCanvasElement>) => {
    const bylo = vid.current.masshtab ?? 1;
    const stalo = Math.max(0.6, Math.min(6, bylo * (sobytie.deltaY < 0 ? 1.12 : 0.89)));
    vid.current.masshtab = stalo;
    vid.current.r = (vid.current.bazovyy ?? vid.current.r) * stalo;
  };

  const podrobnee = () => {
    if (!guard.take()) return;
    api
      .post<Podrobno>("/globe/detail")
      .then(setPodrobno)
      .catch(fail)
      .finally(() => guard.free());
  };

  const zabyt = () => {
    if (!guard.take()) return;
    api
      .del<Podrobno>("/globe/detail")
      .then((s) => {
        setPodrobno(s);
        kontury.current = KONTURY;
      })
      .catch(fail)
      .finally(() => guard.free());
  };

  const schyot = useMemo(() => {
    const itog = new Map<string, number>();
    for (const tochka of dannye?.points ?? []) {
      const kluchi =
        tochka.vid === "visitor"
          ? ["visitors"]
          : ["clients", ...(tochka.deals_open ? ["deals"] : []), ...(tochka.orders_open ? ["orders"] : []),
             ...(tochka.overdue ? ["overdue"] : [])];
      for (const k of kluchi) itog.set(k, (itog.get(k) ?? 0) + 1);
    }
    itog.set("links", dannye?.links.length ?? 0);
    return itog;
  }, [dannye]);

  if (!dannye || !sloi) return <ScreenLoading error={failure} onRetry={() => setZapros((n) => n + 1)} />;

  const dostupnye = new Set(dannye.layers);
  const vidimyh = dannye.points.filter((tochka) => vidno_sloyu(tochka, sloi)).length;
  const deneg = (summa: number | null | undefined) =>
    summa === null || summa === undefined ? "" : formatMoney(summa, workspace.currency || "USD", locale);

  return (
    <div className="globus">
      <div className="globus-shapka">
        <div className="globus-marka">
          <span className="globus-imya">{t("modGlobe")}</span>
          <span className="globus-podzagolovok">{t("globeSub")}</span>
        </div>
        <div className="globus-svodka">
          <span className="globus-metka">{t("globePoints")}</span>
          <span className="globus-znachenie">{dannye.totals.clients}</span>
          <span className="globus-metka">{t("globeLayerVisitors")}</span>
          <span className="globus-znachenie">{dannye.totals.visitors}</span>
          <span className="globus-metka">{t("globeLayerLinks")}</span>
          <span className="globus-znachenie">{dannye.totals.links}</span>
        </div>
      </div>

      <div className="globus-pole">
        <aside className="globus-panel globus-sleva">
          <div className="globus-zagolovok">{t("globeLayers")}</div>
          <div className="globus-sloi">
            {SLOI.filter((sloy) => dostupnye.has(sloy.key)).map((sloy) => (
              <button
                key={sloy.key}
                type="button"
                role="switch"
                aria-checked={sloi.has(sloy.key)}
                className={"globus-sloy" + (sloi.has(sloy.key) ? " vklyuchen" : "")}
                onClick={() =>
                  setSloi((bylo) => {
                    const stalo = new Set(bylo);
                    if (stalo.has(sloy.key)) stalo.delete(sloy.key);
                    else stalo.add(sloy.key);
                    return stalo;
                  })
                }
              >
                <span className="globus-tumbler" />
                <span className="globus-sloy-imya">{t(sloy.label)}</span>
                <span className="globus-sloy-schyot">{schyot.get(sloy.key) ?? ""}</span>
              </button>
            ))}
          </div>

          <div className="globus-zagolovok">{t("globeCountries")}</div>
          <div className="globus-strany">
            {dannye.countries.slice(0, 8).map((strana) => (
              <div key={strana.code} className="globus-strana">
                <span className="globus-kod">{strana.code}</span>
                <span className="globus-strana-imya">{strana.name}</span>
                <span className="globus-znachenie">{strana.clients}</span>
                <span
                  className="globus-polosa"
                  style={{
                    width: `${Math.round((strana.clients / (dannye.countries[0]?.clients || 1)) * 100)}%`,
                  }}
                />
              </div>
            ))}
            {dannye.totals.no_place > 0 && (
              <div className="globus-bez-mesta">{t("globeNoPlace", { n: dannye.totals.no_place })}</div>
            )}
          </div>
        </aside>

        <div className="globus-holst" ref={korob}>
          <canvas
            ref={holst}
            className="globus-canvas"
            aria-label={t("globeCanvas")}
            onMouseDown={(e) => {
              tyanem.current = mesto(e);
            }}
            onMouseUp={(e) => {
              const nachato = tyanem.current;
              tyanem.current = null;
              const teper = mesto(e);
              if (nachato && Math.abs(nachato.x - teper.x) + Math.abs(nachato.y - teper.y) < 4) nazhali(e);
            }}
            onMouseLeave={() => {
              tyanem.current = null;
              navedeno.current = null;
              setPod(null);
            }}
            onMouseMove={dvinuli}
            onWheel={koleso}
          />
          <div className="globus-ugol globus-ugol-lv" />
          <div className="globus-ugol globus-ugol-pv" />
          <div className="globus-ugol globus-ugol-ln" />
          <div className="globus-ugol globus-ugol-pn" />
        </div>

        <aside className="globus-panel globus-sprava">
          <div className="globus-zagolovok">{t("globeSelected")}</div>
          {vybrano ? (
            <div className="globus-kartochka">
              <div className="globus-kartochka-imya">{vybrano.imya}</div>
              <div className="globus-kartochka-podpis">{vybrano.podpis}</div>
              <div className="globus-para">
                <span className="globus-metka">{t("globePrecision")}</span>
                <span className="globus-znachenie">{t(TOCHNOST[vybrano.tochnost] ?? "globePrecisionCountry")}</span>
              </div>
              {vybrano.vid === "client" ? (
                <>
                  <div className="globus-para">
                    <span className="globus-metka">{t("globeLayerDeals")}</span>
                    <span className="globus-znachenie">{vybrano.deals_open ?? 0}</span>
                  </div>
                  <div className="globus-para">
                    <span className="globus-metka">{t("globeLayerOrders")}</span>
                    <span className="globus-znachenie">
                      {vybrano.orders_open ?? 0}
                      {vybrano.overdue ? ` · ${t("globeOverdue", { n: vybrano.overdue })}` : ""}
                    </span>
                  </div>
                  {deneg((vybrano as Tochka & { amount?: number }).amount) && (
                    <div className="globus-para">
                      <span className="globus-metka">{t("globeAmount")}</span>
                      <span className="globus-znachenie">
                        {deneg((vybrano as Tochka & { amount?: number }).amount)}
                      </span>
                    </div>
                  )}
                  {can(user, "clients.view") && (
                    <Link to={`/clients/${vybrano.id}`} className="globus-knopka">
                      {t("globeOpenCard")}
                    </Link>
                  )}
                </>
              ) : (
                <>
                  <div className="globus-para">
                    <span className="globus-metka">{t("globeBoard")}</span>
                    <span className="globus-znachenie">
                      {(vybrano as Tochka & { board?: string }).board ?? ""}
                    </span>
                  </div>
                  <div className="globus-para">
                    <span className="globus-metka">{t("globeSeenAt")}</span>
                    <span className="globus-znachenie">
                      {formatDateTime((vybrano as Tochka & { at?: string }).at ?? null, locale)}
                    </span>
                  </div>
                </>
              )}
            </div>
          ) : (
            <div className="globus-pusto">{t("globeNothingPicked")}</div>
          )}

          <div className="globus-zagolovok">{t("globeDetail")}</div>
          <div className="globus-podrobno">
            {podrobno?.ready ? (
              <>
                <div className="globus-para">
                  <span className="globus-metka">{t("globeDetailReady")}</span>
                  <span className="globus-znachenie">{podrobno.rings}</span>
                </div>
                {can(user, "settings.manage") && (
                  <button type="button" className="globus-knopka" disabled={guard.busy} onClick={zabyt}>
                    {t("globeDetailOff")}
                  </button>
                )}
              </>
            ) : podrobno?.running ? (
              <>
                <div className="globus-para">
                  <span className="globus-metka">{t("globeDetailRunning")}</span>
                  <span className="globus-znachenie">{podrobno.percent}%</span>
                </div>
                <div className="globus-hod">
                  <span className="globus-hod-polosa" style={{ width: `${podrobno.percent}%` }} />
                </div>
              </>
            ) : (
              <>
                <div className="globus-pusto">
                  {podrobno?.wanted ? t("globeDetailWaiting") : t("globeDetailHint")}
                </div>
                {can(user, "settings.manage") && !podrobno?.wanted && (
                  <button type="button" className="globus-knopka" disabled={guard.busy} onClick={podrobnee}>
                    <Icon name="download" size={13} />
                    {t("globeDetailOn")}
                  </button>
                )}
              </>
            )}
          </div>
        </aside>
      </div>

      <div className="globus-planka">
        <span className="globus-metka">{t("globeCoords")}</span>
        <span className="globus-znachenie">
          {pod ? `${pod.lat.toFixed(2)}° ${pod.lon.toFixed(2)}°` : "—"}
        </span>
        <span className="globus-metka">{t("globeBase")}</span>
        <span className="globus-znachenie">{dannye.base ? dannye.base.imya || "—" : t("globeNoBase")}</span>
        <span className="globus-metka">{t("globeLayersOn")}</span>
        <span className="globus-znachenie">
          {sloi.size}/{dannye.layers.length}
        </span>
        <span className="globus-metka">{t("globePoints")}</span>
        <span className="globus-znachenie">{vidimyh}</span>
        <span className="globus-metka">{t("globeUpdated")}</span>
        <span className="globus-znachenie">{formatDateTime(dannye.at, locale)}</span>
      </div>
    </div>
  );
}
