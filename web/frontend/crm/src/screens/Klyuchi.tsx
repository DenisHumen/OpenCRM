import { useCallback, useEffect, useMemo, useState } from "react";

import { Icon } from "../components/Icon";
import { Avatar, ConfirmModal, KnopkaKorziny, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { copyText } from "../lib/clipboard";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { can } from "../lib/permissions";

/** Знак сервиса из вшитого набора. `null` — рисуем буквы названия. */
interface Znak {
  slug: string;
  title: string;
  hex: string;
}

interface Klyuch {
  id: number;
  title: string;
  issuer: string;
  account: string;
  category_id: number | null;
  vazhnost: string;
  digits: number;
  period: number;
  znak: Znak | null;
  mine: boolean;
  can_edit: boolean;
  backup_total: number;
  backup_left: number;
  note: string;
  seen_by: number;
  task: { id: number; title: string; due_at: string | null; done: boolean } | null;
  created_at: string | null;
  deleted_at: string | null;
}

interface Kategoriya {
  id: number;
  name: string;
  zakrytaya: boolean;
  zakryta_dlya_menya: boolean;
  count: number;
  mine: boolean;
}

interface Spisok {
  items: Klyuch[];
  categories: Kategoriya[];
  total: number;
  mine: number;
  trash: number;
  bez_kategorii: number;
  prosyat: number;
}

interface Razbor {
  issuer: string;
  account: string;
  digits: number;
  period: number;
  algorithm: string;
  znak: Znak | null;
  proverka: string;
}

interface Kod {
  code: string;
  ostalos: number;
  period: number;
}

interface Chelovek {
  id: number;
  name: string;
  role: string;
  always: boolean;
  otkryt: boolean;
}

interface Zapasnoy {
  kod: string;
  potrachen: boolean;
}

/** Выбранная полка слева. */
type Polka = { vid: "vse" | "moi" | "korzina" } | { vid: "kategoriya"; id: number };

/** Как часто пересчитывается остаток секунд. Отсчёт местный, а МОМЕНТ приходит
 *  с сервера: часы на машине человека врут чаще, чем кажется. */
const TIK_MS = 1000;

/** Через сколько дней напомнить сменить ключ. Полгода — не наука, а привычка
 *  большинства сервисов; человек правит срок в самом напоминании. */
const NAPOMNIT_CHEREZ = 180;

/** Цвет плитки знака по имени сервиса. Не случайный: у одного сервиса он
 *  обязан быть одним и тем же на всех экранах и после перезагрузки. */
const OTTENKI = ["", "kl-znak-brand", "kl-znak-violet", "kl-znak-teal", "kl-znak-success"];

function ottenokZnaka(imya: string): string {
  let summa = 0;
  for (let i = 0; i < imya.length; i += 1) summa = (summa * 31 + imya.charCodeAt(i)) % 100000;
  return OTTENKI[summa % OTTENKI.length];
}

function bukvy(klyuch: Klyuch): string {
  const istochnik = (klyuch.issuer || klyuch.title || "").trim();
  const slova = istochnik.split(/[\s—–-]+/).filter(Boolean);
  if (slova.length >= 2) return (slova[0][0] + slova[1][0]).toUpperCase();
  return istochnik.slice(0, 2).toUpperCase();
}

export function Klyuchi() {
  const { t, locale, user, toast, toastError } = useApp();
  const [dannye, setDannye] = useState<Spisok | null>(null);
  const { failure, fail, clear } = useFailure();
  const guard = useGuard();

  const [polka, setPolka] = useState<Polka>({ vid: "vse" });
  const [poisk, setPoisk] = useState("");
  const [kody, setKody] = useState<Record<number, Kod>>({});
  const [novyy, setNovyy] = useState(false);
  const [novaya, setNovaya] = useState(false);
  const [tolkoTrevozhnye, setTolkoTrevozhnye] = useState(false);
  const [prava, setPrava] = useState<{ klyuch: Klyuch; lyudi: Chelovek[] } | null>(null);
  const [zapasnye, setZapasnye] = useState<{ klyuch: Klyuch; items: Zapasnoy[] } | null>(null);
  const [perenos, setPerenos] = useState<{ klyuch: Klyuch; secret: string; qr: string } | null>(null);
  const [menyu, setMenyu] = useState<Klyuch | null>(null);
  const [udalenie, setUdalenie] = useState<Klyuch | null>(null);
  const [nasovsem, setNasovsem] = useState<Klyuch | null>(null);
  const [ubratKat, setUbratKat] = useState<Kategoriya | null>(null);
  const [skopirovan, setSkopirovan] = useState<number | null>(null);

  // Плашка «скопировано» гаснет сама: держим отметку, а гасит её общий
  // крючок паузы — своих таймеров на экранах не заводим.
  const pogaslo = useDebounced(skopirovan, 1400);
  useEffect(() => {
    if (pogaslo !== null && pogaslo === skopirovan) setSkopirovan(null);
  }, [pogaslo, skopirovan]);

  const vKorzine = polka.vid === "korzina";
  const rootLi = user?.role === "root";

  const otlozhennyyPoisk = useDebounced(poisk.trim());
  const zapros = useMemo(() => {
    const p = new URLSearchParams();
    if (polka.vid === "kategoriya") p.set("category_id", String(polka.id));
    if (polka.vid === "moi") p.set("mine", "1");
    if (polka.vid === "korzina") p.set("trash", "1");
    if (otlozhennyyPoisk) p.set("q", otlozhennyyPoisk);
    const stroka = p.toString();
    return stroka ? `/keys?${stroka}` : "/keys";
  }, [polka, otlozhennyyPoisk]);

  const load = useCallback(() => {
    clear();
    api.get<Spisok>(zapros).then(setDannye).catch(fail);
  }, [zapros, fail, clear]);

  // Ответ на прошлый отбор не должен лечь поверх нового: медленный ответ по
  // «GitH» иначе затирал бы быстрый по «GitHub».
  useEffect(() => {
    let alive = true;
    clear();
    api
      .get<Spisok>(zapros)
      .then((otvet) => { if (alive) setDannye(otvet); })
      .catch((e) => { if (alive) fail(e); });
    return () => { alive = false; };
  }, [zapros, fail, clear]);

  // Отсчёт кодов. Тикаем только когда вкладка на виду: скрытая вкладка всё
  // равно ничего не показывает, а таймер там продолжал бы дёргать сервер.
  useEffect(() => {
    if (Object.keys(kody).length === 0) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      setKody((bylo) => {
        const stalo: Record<number, Kod> = {};
        let menyalos = false;
        for (const [id, k] of Object.entries(bylo)) {
          const ostalos = k.ostalos - 1;
          if (ostalos <= 0) {
            // Код кончился — просим новый молча: в журнал идёт нажатие
            // «показать», а не каждая пересборка раз в тридцать секунд.
            void api
              .post<Kod>(`/keys/${id}/code?silent=1`)
              .then((svezhiy) => setKody((b) => (b[Number(id)] ? { ...b, [Number(id)]: svezhiy } : b)))
              .catch(() => undefined);
            stalo[Number(id)] = { ...k, ostalos: k.period };
          } else {
            stalo[Number(id)] = { ...k, ostalos };
          }
          menyalos = true;
        }
        return menyalos ? stalo : bylo;
      });
    }, TIK_MS);
    return () => window.clearInterval(timer);
  }, [kody]);

  if (!dannye) return <ScreenLoading error={failure} onRetry={load} />;

  // Отбор «просят обновления» делается здесь, а не запросом: полка уже
  // пришла целиком, и лишний заход к серверу ради вычитания ничего не даст.
  const prosrochen = (k: Klyuch) =>
    !!k.task && !k.task.done && !!k.task.due_at && new Date(k.task.due_at) < new Date();
  const pokazyvaem = tolkoTrevozhnye ? dannye.items.filter(prosrochen) : dannye.items;

  const mozhnoZavodit = can(user, "keys.create");
  const mozhnoUpravlyat = can(user, "keys.manage");

  const pokazat = async (klyuch: Klyuch) => {
    if (!guard.take()) return;
    try {
      const kod = await api.post<Kod>(`/keys/${klyuch.id}/code`);
      setKody((bylo) => ({ ...bylo, [klyuch.id]: kod }));
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const skopirovat = async (id: number, tekst: string) => {
    // Через общий `copyText`: по HTTP (сайт в локальной сети без сертификата)
    // `navigator.clipboard` не существует вовсе, и кнопка молчала бы.
    if (!(await copyText(tekst))) {
      toastError(new Error(t("keysCopyFailed")));
      return;
    }
    setSkopirovan(id);
  };

  const otkrytPrava = async (klyuch: Klyuch) => {
    setMenyu(null);
    try {
      const otvet = await api.get<{ people: Chelovek[] }>(`/keys/${klyuch.id}/access`);
      setPrava({ klyuch, lyudi: otvet.people });
    } catch (e) {
      toastError(e);
    }
  };

  const perekluchit = async (chelovek: Chelovek) => {
    if (!prava || !guard.take()) return;
    try {
      const otvet = await api.post<{ people: Chelovek[] }>(`/keys/${prava.klyuch.id}/access`, {
        user_id: chelovek.id,
        otkryt: !chelovek.otkryt,
      });
      setPrava({ ...prava, lyudi: otvet.people });
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const otkrytZapasnye = async (klyuch: Klyuch) => {
    setMenyu(null);
    try {
      const otvet = await api.get<{ items: Zapasnoy[] }>(`/keys/${klyuch.id}/backup-codes`);
      setZapasnye({ klyuch, items: otvet.items });
    } catch (e) {
      toastError(e);
    }
  };

  const potratit = async (nomer: number) => {
    if (!zapasnye || !guard.take()) return;
    try {
      const otvet = await api.post<{ items: Zapasnoy[] }>(
        `/keys/${zapasnye.klyuch.id}/backup-codes/${nomer}/spend`,
      );
      setZapasnye({ ...zapasnye, items: otvet.items });
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const otkrytPerenos = async (klyuch: Klyuch) => {
    setMenyu(null);
    if (!guard.take()) return;
    try {
      const otvet = await api.post<{ secret: string; qr: string }>(`/keys/${klyuch.id}/secret`);
      setPerenos({ klyuch, secret: otvet.secret, qr: otvet.qr });
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const udalit = async (klyuch: Klyuch) => {
    setUdalenie(null);
    setMenyu(null);
    if (!guard.take()) return;
    try {
      await api.del(`/keys/${klyuch.id}`);
      toast(t("keysMovedToTrash"));
      setKody((bylo) => {
        const stalo = { ...bylo };
        delete stalo[klyuch.id];
        return stalo;
      });
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const vernut = async (klyuch: Klyuch) => {
    if (!guard.take()) return;
    try {
      await api.post(`/keys/${klyuch.id}/restore`);
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const steret = async (klyuch: Klyuch) => {
    setNasovsem(null);
    if (!guard.take()) return;
    try {
      await api.del(`/keys/${klyuch.id}/forever`);
      toast(t("keysErased"));
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const ubratKategoriyu = async (kat: Kategoriya) => {
    setUbratKat(null);
    if (!guard.take()) return;
    try {
      await api.del(`/keys/categories/${kat.id}`);
      // Полка под ногами: если смотрели именно её, возвращаемся ко всем.
      if (polka.vid === "kategoriya" && polka.id === kat.id) setPolka({ vid: "vse" });
      toast(t("keysCategoryRemoved"));
      load();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const polkaVybrana = (p: Polka) =>
    p.vid === polka.vid && (p.vid !== "kategoriya" || (polka.vid === "kategoriya" && p.id === polka.id));

  const znakKarty = (klyuch: Klyuch) => (
    <div className={`kl-znak ${klyuch.znak ? "" : ottenokZnaka(klyuch.issuer || klyuch.title)}`}>
      {klyuch.znak ? (
        // Значок отдаёт сервер по одному: их три с половиной тысячи, и разом
        // это четыре мегабайта на каждое открытие экрана.
        <span
          className="kl-znak-kartinka"
          style={{ ["--znak" as string]: `url(/api/v1/keys/znak/${klyuch.znak.slug}.svg)` }}
        />
      ) : (
        bukvy(klyuch)
      )}
    </div>
  );

  const seyf = (klyuch: Klyuch) => {
    const kod = kody[klyuch.id];
    if (!kod) {
      return (
        <div className="kl-seyf">
          <div className="kl-tochki">{"•".repeat(Math.min(klyuch.digits, 4))} {"•".repeat(3)}</div>
          <button
            type="button"
            className="kl-seyf-knopka"
            disabled={guard.busy || vKorzine}
            onClick={() => void pokazat(klyuch)}
          >
            <Icon name="eye" size={13} />
            {t("keysShow")}
          </button>
        </div>
      );
    }
    const dolya = kod.ostalos / kod.period;
    const stupen = kod.ostalos <= 5 ? "kl-konets" : kod.ostalos <= 10 ? "kl-skoro" : "";
    const cifry = kod.code.split("");
    const seredina = Math.ceil(cifry.length / 2);
    return (
      <div className="kl-seyf">
        <div className="kl-cifry" aria-label={t("keysCodeAria", { code: kod.code })}>
          {cifry.map((c, i) => (
            <span key={i}>
              {i === seredina && <span className="kl-probel" />}
              <span className="kl-cifra" style={{ ["--i" as string]: i }}>{c}</span>
            </span>
          ))}
        </div>
        <div className={`kl-koltso ${stupen}`} style={{ ["--dolya" as string]: dolya }}>
          <span className="kl-koltso-chislo">{kod.ostalos}</span>
        </div>
        <button
          type="button"
          className={`kl-seyf-knopka kl-kopiya ${skopirovan === klyuch.id ? "kl-skopirovano" : ""}`}
          data-podskazka={t("keysCopied")}
          onClick={() => void skopirovat(klyuch.id, kod.code)}
        >
          <Icon name={skopirovan === klyuch.id ? "check" : "copy"} size={13} />
          {t("keysCopy")}
        </button>
      </div>
    );
  };

  return (
    <div className="page page-wide">
      <div className="kl-shapka">
        <div className="kl-shapka-tekst">
          <h1 className="kl-titul">{t("modKeys")}</h1>
          <div className="kl-svodka">
            <span className="kl-svodka-chislo">
              {t("keysSummary", { n: dannye.total, k: dannye.categories.length })}
            </span>
            {dannye.prosyat > 0 && (
              <button
                type="button"
                className="kl-svodka-trevoga"
                aria-pressed={tolkoTrevozhnye}
                onClick={() => setTolkoTrevozhnye((b) => !b)}
              >
                <Icon name="alert" size={11} />
                {t("keysNeedRotation", { n: dannye.prosyat })}
              </button>
            )}
          </div>
        </div>
        <div className="kl-shapka-knopki">
          <div className="kl-poisk">
            <Icon name="search" size={14} />
            <input
              type="text"
              value={poisk}
              placeholder={t("keysSearch")}
              aria-label={t("keysSearch")}
              onChange={(e) => setPoisk(e.target.value)}
            />
          </div>
          {mozhnoUpravlyat && (
            <button className="btn" onClick={() => setNovaya(true)}>
              <Icon name="plus" size={14} />
              {t("keysCategory")}
            </button>
          )}
          {mozhnoZavodit && (
            <button className="btn btn-primary" onClick={() => setNovyy(true)}>
              <Icon name="plus" size={14} />
              {t("keysAdd")}
            </button>
          )}
        </div>
      </div>

      <div className="kl-telo">
        <nav className="kl-papki" aria-label={t("keysCategories")}>
          <div className="kl-papki-zagolovok">
            {t("keysCategories")}
            <span className="kl-papka-schyot">{dannye.categories.length}</span>
          </div>
          <div className="kl-papki-spisok">
            <button
              type="button"
              className={`kl-papka ${polkaVybrana({ vid: "vse" }) ? "kl-vybrana" : ""}`}
              onClick={() => setPolka({ vid: "vse" })}
            >
              <Icon name="database" size={15} />
              <span className="kl-papka-imya">{t("keysAll")}</span>
              <span className="kl-papka-schyot">{dannye.total}</span>
            </button>
            <button
              type="button"
              className={`kl-papka ${polkaVybrana({ vid: "moi" }) ? "kl-vybrana" : ""}`}
              onClick={() => setPolka({ vid: "moi" })}
            >
              <Icon name="user" size={15} />
              <span className="kl-papka-imya">{t("keysMine")}</span>
              <span className="kl-papka-schyot">{dannye.mine}</span>
            </button>
            {dannye.categories.map((kat) => (
              <div key={kat.id} className="kl-papka-ryad">
                <button
                  type="button"
                  className={`kl-papka ${kat.zakryta_dlya_menya ? "kl-zakryta" : ""} ${
                    polkaVybrana({ vid: "kategoriya", id: kat.id }) ? "kl-vybrana" : ""
                  }`}
                  disabled={kat.zakryta_dlya_menya}
                  title={kat.zakryta_dlya_menya ? t("keysCategoryClosedHint") : undefined}
                  onClick={() => setPolka({ vid: "kategoriya", id: kat.id })}
                >
                  <Icon name={kat.zakrytaya ? "lock" : "folder"} size={15} />
                  <span className="kl-papka-imya truncate">{kat.name}</span>
                  <span className="kl-papka-schyot">{kat.count}</span>
                </button>
                {(rootLi || kat.mine) && !kat.zakryta_dlya_menya && (
                  <button
                    type="button"
                    className="btn-icon kl-papka-ubrat"
                    title={t("keysCategoryRemove")}
                    aria-label={t("keysCategoryRemove")}
                    disabled={guard.busy}
                    onClick={() => setUbratKat(kat)}
                  >
                    <Icon name="trash" size={13} />
                  </button>
                )}
              </div>
            ))}
          </div>
          <div className="kl-papki-nizhe">
            <button
              type="button"
              className={`kl-papka ${polkaVybrana({ vid: "korzina" }) ? "kl-vybrana" : ""}`}
              onClick={() => setPolka({ vid: "korzina" })}
            >
              <Icon name="trash" size={15} />
              <span className="kl-papka-imya">{t("keysTrash")}</span>
              <span className="kl-papka-schyot">{dannye.trash}</span>
            </button>
          </div>
        </nav>

        <div className="kl-setka">
          {pokazyvaem.length === 0 ? (
            <div className="kl-pusto">
              <div className="kl-pusto-plita">
                <Icon name="lock" size={26} />
              </div>
              <div className="kl-pusto-titul">{vKorzine ? t("keysTrashEmpty") : t("keysEmpty")}</div>
              <div className="kl-pusto-podpis">{t("keysEmptyHint")}</div>
              {mozhnoZavodit && !vKorzine && (
                <button className="btn btn-primary" onClick={() => setNovyy(true)}>
                  <Icon name="plus" size={14} />
                  {t("keysAdd")}
                </button>
              )}
            </div>
          ) : (
            pokazyvaem.map((klyuch, i) => (
              <article
                key={klyuch.id}
                className={`kl-kartochka ${klyuch.vazhnost === "urgent" ? "kl-srochnyy" : ""}`}
                style={{ ["--n" as string]: i }}
              >
                <div className="kl-verh">
                  {znakKarty(klyuch)}
                  <div className="kl-imena">
                    <div className="kl-imya">{klyuch.title}</div>
                    {klyuch.account && <div className="kl-uchyotka truncate">{klyuch.account}</div>}
                  </div>
                  {klyuch.can_edit && !vKorzine && (
                    <button
                      type="button"
                      className="btn-icon kl-menyu"
                      title={t("keysMore")}
                      aria-label={t("keysMore")}
                      onClick={() => setMenyu(klyuch)}
                    >
                      <Icon name="dots" size={14} />
                    </button>
                  )}
                </div>

                {vKorzine ? (
                  <div className="kl-deystviya">
                    <button className="btn btn-sm" disabled={guard.busy} onClick={() => void vernut(klyuch)}>
                      <Icon name="refresh" size={13} />
                      {t("keysRestore")}
                    </button>
                    {klyuch.can_edit && (
                      <KnopkaKorziny
                        title={t("keysEraseTitle")}
                        podpis={t("keysEraseShort")}
                        disabled={guard.busy}
                        onClick={() => setNasovsem(klyuch)}
                      />
                    )}
                  </div>
                ) : (
                  seyf(klyuch)
                )}

                {klyuch.note && (
                  <details className="kl-zametka">
                    <summary className="kl-zametka-krai">{klyuch.note}</summary>
                    <div className="kl-zametka-telo">{klyuch.note}</div>
                  </details>
                )}

                <div className="kl-niz">
                  {klyuch.vazhnost === "urgent" && (
                    <span className="chip chip-danger">{t("vazhnostUrgent")}</span>
                  )}
                  {klyuch.vazhnost === "high" && (
                    <span className="chip chip-warning">{t("vazhnostHigh")}</span>
                  )}
                  {klyuch.category_id !== null && (
                    <span className="chip">
                      {dannye.categories.find((k) => k.id === klyuch.category_id)?.name ?? ""}
                    </span>
                  )}
                  {klyuch.seen_by > 0 && (
                    <span className="chip">{t("keysSeenBy", { n: klyuch.seen_by })}</span>
                  )}
                  {klyuch.backup_total > 0 && (
                    <button
                      type="button"
                      className="chip chip-accent"
                      onClick={() => void otkrytZapasnye(klyuch)}
                    >
                      {t("keysBackupChip", { left: klyuch.backup_left, total: klyuch.backup_total })}
                    </button>
                  )}
                </div>

                {klyuch.task && (
                  <div className="kl-stroka">
                    <a className={`kl-svyaz ${klyuch.task.done ? "" : "kl-trevoga"}`} href={`/tasks?id=${klyuch.task.id}`}>
                      <Icon name="clock" size={13} />
                      <span className="truncate">{klyuch.task.title}</span>
                    </a>
                    {klyuch.task.due_at && (
                      <span className="kl-kogda">{formatData(klyuch.task.due_at, locale)}</span>
                    )}
                  </div>
                )}
              </article>
            ))
          )}
        </div>
      </div>

      {novyy && (
        <OknoNovogo
          kategorii={dannye.categories}
          onClose={() => setNovyy(false)}
          onSaved={() => {
            setNovyy(false);
            load();
          }}
        />
      )}

      {novaya && (
        <OknoKategorii
          onClose={() => setNovaya(false)}
          onSaved={() => {
            setNovaya(false);
            load();
          }}
        />
      )}

      {menyu && (
        <Modal title={menyu.title} onClose={() => setMenyu(null)}>
          <div className="kl-menyu-spisok">
            {mozhnoUpravlyat && (
              <button type="button" className="kl-menyu-punkt" onClick={() => void otkrytPrava(menyu)}>
                <Icon name="staff" size={15} />
                {t("keysWhoSees")}
              </button>
            )}
            <button type="button" className="kl-menyu-punkt" onClick={() => void otkrytZapasnye(menyu)}>
              <Icon name="clipboard" size={15} />
              {t("keysBackupCodes")}
            </button>
            {rootLi && (
              <button type="button" className="kl-menyu-punkt" onClick={() => void otkrytPerenos(menyu)}>
                <Icon name="scan" size={15} />
                {t("keysToPhone")}
              </button>
            )}
            <button
              type="button"
              className="kl-menyu-punkt kl-opasno"
              onClick={() => {
                setUdalenie(menyu);
                setMenyu(null);
              }}
            >
              <Icon name="trash" size={15} />
              {t("delete")}
            </button>
          </div>
        </Modal>
      )}

      {prava && (
        <Modal title={t("keysWhoSeesTitle", { name: prava.klyuch.title })} onClose={() => setPrava(null)}>
          <div className="kl-zamechanie kl-zamechanie-opasno">
            <Icon name="alert" size={15} />
            <span>{t("keysAccessWarn")}</span>
          </div>
          <div className="kl-prava-spisok">
            {prava.lyudi.map((chelovek) => (
              <div key={chelovek.id} className="kl-prava-stroka">
                <Avatar text={chelovek.name} />
                <div className="kl-prava-kto">
                  <div className="kl-prava-imya">{chelovek.name}</div>
                  <div className="kl-prava-rol">
                    {chelovek.always ? t("keysSeesAlways") : t(chelovek.role === "root" ? "root" : "manager")}
                  </div>
                </div>
                <button
                  type="button"
                  className={`toggle-track ${chelovek.otkryt ? "on" : ""}`}
                  aria-pressed={chelovek.otkryt}
                  aria-label={chelovek.name}
                  disabled={chelovek.always || guard.busy}
                  onClick={() => void perekluchit(chelovek)}
                />
              </div>
            ))}
          </div>
          <div className="field-desc">{t("keysAccessHint")}</div>
        </Modal>
      )}

      {zapasnye && (
        <Modal title={t("keysBackupTitle", { name: zapasnye.klyuch.title })} onClose={() => setZapasnye(null)}>
          <div className="field-desc" style={{ marginTop: 0, marginBottom: 12 }}>
            {t("keysBackupHint")}
          </div>
          {zapasnye.items.length === 0 ? (
            <div className="field-desc">{t("keysBackupNone")}</div>
          ) : (
            <>
              <div className="kl-zapasnye">
                {zapasnye.items.map((z, i) => (
                  <button
                    key={i}
                    type="button"
                    className={`kl-zapasnoy ${z.potrachen ? "kl-potrachen" : ""}`}
                    disabled={z.potrachen || guard.busy}
                    title={z.potrachen ? t("keysBackupSpent") : t("keysBackupSpend")}
                    onClick={() => void potratit(i)}
                  >
                    {z.kod}
                  </button>
                ))}
              </div>
              <div className="kl-zapasnye-itog">
                <span className="kl-shkala">
                  <span
                    className={`kl-shkala-polosa ${
                      zapasnye.items.filter((z) => !z.potrachen).length < 3 ? "kl-taet" : ""
                    }`}
                    style={{
                      width: `${Math.round(
                        (zapasnye.items.filter((z) => !z.potrachen).length / zapasnye.items.length) * 100,
                      )}%`,
                    }}
                  />
                </span>
                <span className="kl-zapasnye-schyot">
                  {t("keysBackupLeft", {
                    left: zapasnye.items.filter((z) => !z.potrachen).length,
                    total: zapasnye.items.length,
                  })}
                </span>
              </div>
              <div className="field-desc">{t("keysBackupStrike")}</div>
            </>
          )}
        </Modal>
      )}

      {perenos && (
        <Modal title={t("keysToPhoneTitle", { name: perenos.klyuch.title })} onClose={() => setPerenos(null)}>
          <div className="kl-zamechanie kl-zamechanie-opasno">
            <Icon name="alert" size={15} />
            <span>{t("keysSecretWarn")}</span>
          </div>
          <div className="kl-qr-ryad">
            <div className="kl-qr" role="img" aria-label={t("keysQrAria")} dangerouslySetInnerHTML={{ __html: perenos.qr }} />
            <div className="kl-qr-bok">
              <span className="label">{t("keysSecretString")}</span>
              <div className="kl-qr-stroka">
                <div className="kl-qr-kod truncate">{perenos.secret}</div>
                <button
                  type="button"
                  className={`btn btn-sm kl-kopiya ${skopirovan === -perenos.klyuch.id ? "kl-skopirovano" : ""}`}
                  data-podskazka={t("keysCopied")}
                  title={t("keysCopy")}
                  aria-label={t("keysCopy")}
                  onClick={() => void skopirovat(-perenos.klyuch.id, perenos.secret)}
                >
                  <Icon name="copy" size={14} />
                </button>
              </div>
              <div className="field-desc">{t("keysQrHint")}</div>
            </div>
          </div>
        </Modal>
      )}

      {udalenie && (
        <ConfirmModal
          text={t("keysDeleteConfirm", { name: udalenie.title })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void udalit(udalenie)}
          onClose={() => setUdalenie(null)}
        />
      )}

      {ubratKat && (
        <ConfirmModal
          text={t("keysCategoryRemoveConfirm", { name: ubratKat.name, n: ubratKat.count })}
          confirmLabel={t("keysCategoryRemove")}
          danger
          onConfirm={() => void ubratKategoriyu(ubratKat)}
          onClose={() => setUbratKat(null)}
        />
      )}

      {nasovsem && (
        <ConfirmModal
          text={t("keysEraseConfirm", { name: nasovsem.title })}
          confirmLabel={t("keysErase")}
          danger
          onConfirm={() => void steret(nasovsem)}
          onClose={() => setNasovsem(null)}
        />
      )}
    </div>
  );
}

function formatData(kogda: string, locale: string): string {
  try {
    return new Date(kogda).toLocaleDateString(locale, { day: "numeric", month: "short" });
  } catch {
    return kogda;
  }
}

/** Окно заведения ключа. Разбор строки показывается сразу: вставил не то —
 *  видно ДО сохранения, а не через тридцать секунд по неподходящему коду. */
function OknoNovogo({
  kategorii,
  onClose,
  onSaved,
}: {
  kategorii: Kategoriya[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [stroka, setStroka] = useState("");
  const [nazvanie, setNazvanie] = useState("");
  const [kategoriya, setKategoriya] = useState("");
  const [zametka, setZametka] = useState("");
  const [vazhnost, setVazhnost] = useState("normal");
  const [napomnit, setNapomnit] = useState(false);
  const [razbor, setRazbor] = useState<Razbor | null>(null);
  const [otkaz, setOtkaz] = useState("");

  // Разбор просим с паузой общим крючком: строку вставляют целиком, но
  // набирают её и руками, а запрос на каждую букву — это запрос на каждую
  // букву. Номер попытки — против гонки ответов: медленный первый ответ иначе
  // затирал бы быстрый второй.
  const otlozhennaya = useDebounced(stroka.trim());
  useEffect(() => {
    if (!otlozhennaya) {
      setRazbor(null);
      setOtkaz("");
      return;
    }
    let alive = true;
    api
      .post<Razbor>("/keys/parse", { secret: otlozhennaya })
      .then((otvet) => {
        if (!alive) return;
        setRazbor(otvet);
        setOtkaz("");
        setNazvanie((bylo) => bylo || otvet.issuer || otvet.account);
      })
      .catch((e) => {
        if (!alive) return;
        setRazbor(null);
        setOtkaz(e instanceof Error ? e.message : String(e));
      });
    return () => { alive = false; };
  }, [otlozhennaya]);

  const sohranit = async () => {
    if (!guard.take()) return;
    try {
      const klyuch = await api.post<{ id: number }>("/keys", {
        secret: stroka.trim(),
        title: nazvanie.trim(),
        category: kategoriya.trim(),
        note: zametka,
        vazhnost,
      });
      // Напоминание — отдельным вызовом и НЕ ломает заведение ключа: блок
      // напоминаний бывает выключен, а ключ при этом заведён и работает.
      if (napomnit) {
        try {
          await api.post(`/keys/${klyuch.id}/reminder`, { cherez_dney: NAPOMNIT_CHEREZ });
        } catch (e) {
          toastError(e);
        }
      }
      onSaved();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <Modal title={t("keysNew")} onClose={onClose}>
      <div className="kl-pole">
        <label className="label" htmlFor="kl-sekret">{t("keysSecretField")}</label>
        <input
          id="kl-sekret"
          className="input"
          autoFocus
          autoComplete="off"
          value={stroka}
          onChange={(e) => setStroka(e.target.value)}
        />
        <div className="field-desc">{t("keysSecretHint")}</div>
      </div>

      {(razbor || otkaz) && (
        <div className="kl-pole">
          <span className="label">{t("keysParsed")}</span>
          {razbor ? (
            <div className="kl-razbor">
              <div className="kl-razbor-stroka">
                <span className="kl-razbor-imya">{t("keysService")}</span>
                <span className="kl-razbor-znachenie">{razbor.issuer || "—"}</span>
              </div>
              <div className="kl-razbor-stroka">
                <span className="kl-razbor-imya">{t("keysAccount")}</span>
                <span className="kl-razbor-znachenie truncate">{razbor.account || "—"}</span>
              </div>
              <div className="kl-razbor-stroka">
                <span className="kl-razbor-imya">{t("keysDigits")}</span>
                <span className="kl-razbor-znachenie">{razbor.digits}</span>
              </div>
              <div className="kl-razbor-stroka">
                <span className="kl-razbor-imya">{t("keysPeriod")}</span>
                <span className="kl-razbor-znachenie">{razbor.period}</span>
              </div>
              <div className="kl-razbor-stroka kl-razbor-itog">
                <span className="kl-razbor-imya">{t("keysCheck")}</span>
                <span className="kl-razbor-znachenie kl-razbor-horosho">
                  <Icon name="check" size={12} />
                  {razbor.proverka}
                </span>
              </div>
            </div>
          ) : (
            <div className="kl-razbor">
              <div className="kl-razbor-stroka">
                <span className="kl-razbor-imya">{t("keysCheck")}</span>
                <span className="kl-razbor-znachenie kl-razbor-ploho">{otkaz}</span>
              </div>
            </div>
          )}
        </div>
      )}

      <div className="kl-pole">
        <label className="label" htmlFor="kl-imya">{t("keysName")}</label>
        <input id="kl-imya" className="input" value={nazvanie} onChange={(e) => setNazvanie(e.target.value)} />
        <div className="field-desc">{t("keysNameHint")}</div>
      </div>

      <div className="kl-pole">
        <label className="label" htmlFor="kl-kat">{t("keysCategory")}</label>
        <input
          id="kl-kat"
          className="input"
          list="kl-kategorii"
          value={kategoriya}
          onChange={(e) => setKategoriya(e.target.value)}
        />
        <datalist id="kl-kategorii">
          {kategorii.filter((k) => !k.zakryta_dlya_menya).map((k) => (
            <option key={k.id} value={k.name} />
          ))}
        </datalist>
        <div className="field-desc">{t("keysCategoryHint")}</div>
      </div>

      <div className="kl-pole">
        <label className="label" htmlFor="kl-vazhnost">{t("keysImportance")}</label>
        <select id="kl-vazhnost" className="input" value={vazhnost} onChange={(e) => setVazhnost(e.target.value)}>
          <option value="urgent">{t("vazhnostUrgent")}</option>
          <option value="high">{t("vazhnostHigh")}</option>
          <option value="normal">{t("vazhnostNormal")}</option>
          <option value="low">{t("vazhnostLow")}</option>
        </select>
      </div>

      <div className="kl-pole">
        <label className="label" htmlFor="kl-zametka">{t("keysNote")}</label>
        <textarea
          id="kl-zametka"
          className="input"
          rows={3}
          placeholder={t("keysNotePlaceholder")}
          value={zametka}
          onChange={(e) => setZametka(e.target.value)}
        />
      </div>

      <div className="kl-pole">
        <span className="label">{t("keysReminder")}</span>
        <div className="kl-prava-stroka" style={{ borderBottom: 0, paddingLeft: 0 }}>
          <span className="kl-prava-kto">
            <span className="kl-prava-imya">{t("keysReminderHalfYear")}</span>
          </span>
          <button
            type="button"
            className={`toggle-track ${napomnit ? "on" : ""}`}
            aria-pressed={napomnit}
            aria-label={t("keysReminderHalfYear")}
            onClick={() => setNapomnit((b) => !b)}
          />
        </div>
      </div>

      <div className="kl-deystviya">
        <button className="btn btn-secondary" onClick={onClose}>{t("cancel")}</button>
        <button
          className="btn btn-primary"
          disabled={guard.busy || !razbor}
          onClick={() => void sohranit()}
        >
          {t("save")}
        </button>
      </div>
    </Modal>
  );
}

/** Окно заведения категории. Закрытая — та, куда пускают только её создателя и
 *  root: она видна всем по имени и числу ключей, но не по содержимому.
 *  Спрячь её целиком — и рядом заведут вторую такую же. */
function OknoKategorii({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const { t, toastError } = useApp();
  const guard = useGuard();
  const [imya, setImya] = useState("");
  const [zakrytaya, setZakrytaya] = useState(false);

  const sohranit = async () => {
    if (!guard.take()) return;
    try {
      await api.post("/keys/categories", { name: imya.trim(), zakrytaya });
      onSaved();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <Modal title={t("keysNewCategory")} onClose={onClose}>
      <div className="kl-pole">
        <label className="label" htmlFor="kl-kat-imya">{t("keysCategoryName")}</label>
        <input
          id="kl-kat-imya"
          className="input"
          autoFocus
          value={imya}
          onChange={(e) => setImya(e.target.value)}
        />
      </div>

      <div className="kl-pole">
        <span className="label">{t("keysCategoryWho")}</span>
        <div className="kl-prava-stroka" style={{ borderBottom: 0, paddingLeft: 0 }}>
          <span className="kl-prava-kto">
            <span className="kl-prava-imya">{t("keysCategoryClosed")}</span>
            <span className="kl-prava-rol">{t("keysCategoryClosedHint")}</span>
          </span>
          <button
            type="button"
            className={`toggle-track ${zakrytaya ? "on" : ""}`}
            aria-pressed={zakrytaya}
            aria-label={t("keysCategoryClosed")}
            onClick={() => setZakrytaya((b) => !b)}
          />
        </div>
      </div>

      <div className="kl-deystviya">
        <button className="btn btn-secondary" onClick={onClose}>{t("cancel")}</button>
        <button
          className="btn btn-primary"
          disabled={guard.busy || !imya.trim()}
          onClick={() => void sohranit()}
        >
          {t("save")}
        </button>
      </div>
    </Modal>
  );
}
