import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Icon } from "../components/Icon";
import { Avatar, ConfirmModal, KnopkaKorziny, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { copyText } from "../lib/clipboard";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { can } from "../lib/permissions";
import { useVspyshkaNa } from "../lib/vspyshka";

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
  /** Коды лежат, но нынешним ключом шифрования не открываются. */
  backup_zakryty: boolean;
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

/** Код, лежащий на экране: цифры сервера плюс МОМЕНТ, когда они перестанут
 *  годиться. Отсчёт ведётся от момента, а не вычитанием единицы на тик:
 *  вкладка в фоне тиков не получает, и вычитание оставляло на экране код
 *  пятиминутной давности с полным кольцом — его копировали, сервис отказывал,
 *  и признака просрочки на экране не было никакого. */
interface KodNaEkrane extends Kod {
  istekaet: number;
  /** Ушли за новым: прежние цифры уже не годятся, а новых ещё нет. */
  zhdyom: boolean;
}

function naEkran(kod: Kod): KodNaEkrane {
  return { ...kod, istekaet: Date.now() + kod.ostalos * 1000, zhdyom: false };
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
const OTTENKI = ["", "keys-icon-brand", "keys-icon-violet", "keys-icon-teal", "keys-icon-success"];

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
  const [kody, setKody] = useState<Record<number, KodNaEkrane>>({});
  const [novyy, setNovyy] = useState(false);
  const [novaya, setNovaya] = useState(false);
  const [tolkoTrevozhnye, setTolkoTrevozhnye] = useState(false);
  const [prava, setPrava] = useState<{ klyuch: Klyuch; lyudi: Chelovek[] } | null>(null);
  const [zapasnye, setZapasnye] = useState<{
    klyuch: Klyuch;
    items: Zapasnoy[];
    zakryty: boolean;
  } | null>(null);
  const [perenos, setPerenos] = useState<{
    klyuch: Klyuch;
    secret: string;
    qr: string;
    zakroetsya: number;
  } | null>(null);
  const [dostupKategorii, setDostupKategorii] = useState<{ kat: Kategoriya; lyudi: Chelovek[] } | null>(null);
  const [menyu, setMenyu] = useState<Klyuch | null>(null);
  const [udalenie, setUdalenie] = useState<Klyuch | null>(null);
  const [nasovsem, setNasovsem] = useState<Klyuch | null>(null);
  const [ubratKat, setUbratKat] = useState<Kategoriya | null>(null);
  // Плашка «скопировано» гаснет сама. Общим крючком, а не парой «отметка +
  // пауза ввода»: та пара гасила свежее нажатие отставшим значением, и второе
  // нажатие подряд по той же кнопке не показывало ничего.
  const [skopirovan, otmetitKopiyu] = useVspyshkaNa<number>();

  const vKorzine = polka.vid === "korzina";
  const rootLi = user?.role === "root";

  const otlozhennyyPoisk = useDebounced(poisk.trim());
  const zapros = useMemo(() => {
    const p = new URLSearchParams();
    if (polka.vid === "kategoriya") p.set("category_id", String(polka.id));
    if (polka.vid === "moi") p.set("mine", "1");
    if (polka.vid === "korzina") p.set("trash", "1");
    if (otlozhennyyPoisk) p.set("q", otlozhennyyPoisk);
    if (tolkoTrevozhnye) p.set("alarm", "1");
    const stroka = p.toString();
    return stroka ? `/keys?${stroka}` : "/keys";
  }, [polka, otlozhennyyPoisk, tolkoTrevozhnye]);

  // Полка и поиск снимают отбор по тревоге. Число тревоги — по ВСЕМУ разделу, и
  // оставленный отбор показывал бы под ним пересечение с полкой: одна и та же
  // единица в шапке и пустой список под ней.
  const vybratPolku = useCallback((chto: Polka) => {
    setTolkoTrevozhnye(false);
    setPolka(chto);
  }, []);

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

  // Код кончился — идём за новым. Отказ закрывает сейф, а не оставляет на
  // экране прежние цифры: проглоченный отказ (ключ унесли в корзину, оборвалась
  // сеть) ничем не отличался от удавшегося запроса.
  const obnovit = useCallback(
    async (id: number) => {
      try {
        const svezhiy = await api.post<Kod>(`/keys/${id}/code`);
        setKody((bylo) => (bylo[id] ? { ...bylo, [id]: naEkran(svezhiy) } : bylo));
      } catch (e) {
        setKody((bylo) => {
          if (!bylo[id]) return bylo;
          const stalo = { ...bylo };
          delete stalo[id];
          return stalo;
        });
        toastError(e);
      }
    },
    [toastError],
  );

  // Свежий снимок кодов для тика. Через ссылку, а не через зависимость: иначе
  // отсчёт пересоздавал бы таймер каждую секунду и сам себя сбивал.
  const poslednie = useRef(kody);
  poslednie.current = kody;

  // Пересчёт остатка от настоящего времени. Не вычитание единицы: см. `KodNaEkrane`.
  //
  // Запрос за новым кодом стоит СНАРУЖИ обновления состояния: обновление обязано
  // быть чистым, а в строгом режиме React зовёт его дважды — и запросов ушло бы
  // тоже два.
  const peresobrat = useCallback(() => {
    const teper = Date.now();
    const stalo: Record<number, KodNaEkrane> = {};
    const prosit: number[] = [];
    let menyalos = false;
    for (const [id, k] of Object.entries(poslednie.current)) {
      const nomer = Number(id);
      const ostalos = Math.max(0, Math.ceil((k.istekaet - teper) / 1000));
      if (ostalos === 0 && !k.zhdyom) {
        prosit.push(nomer);
        stalo[nomer] = { ...k, ostalos: 0, zhdyom: true };
        menyalos = true;
        continue;
      }
      stalo[nomer] = ostalos === k.ostalos ? k : { ...k, ostalos };
      if (ostalos !== k.ostalos) menyalos = true;
    }
    if (menyalos) setKody(stalo);
    for (const nomer of prosit) void obnovit(nomer);
  }, [obnovit]);

  // Окно переноса закрывается само — срок называет сервер (`zakroetsya_cherez`).
  // Тикает и в скрытой вкладке нарочно: беда именно в том, что открытый секрет
  // остаётся на незапертом экране, пока человек занят другой вкладкой.
  const [perenosOstalos, setPerenosOstalos] = useState(0);
  useEffect(() => {
    if (!perenos) return;
    const schitat = () => {
      const ostalos = Math.max(0, Math.ceil((perenos.zakroetsya - Date.now()) / 1000));
      setPerenosOstalos(ostalos);
      if (ostalos === 0) setPerenos(null);
    };
    schitat();
    // таймер-без-сети: только счёт секунд до закрытия окна. Видимость он
    // нарочно НЕ спрашивает — беда именно в том, что человек ушёл в другую
    // вкладку, а открытый секрет остался на незапертом экране.
    const timer = window.setInterval(schitat, TIK_MS);
    return () => window.clearInterval(timer);
  }, [perenos]);

  // Тикаем только когда вкладка на виду: скрытая всё равно ничего не
  // показывает, а таймер там дёргал бы сервер. Возврат на вкладку пересчитывает
  // сразу, не дожидаясь своего тика.
  const estKody = Object.keys(kody).length > 0;
  useEffect(() => {
    if (!estKody) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") peresobrat();
    }, TIK_MS);
    document.addEventListener("visibilitychange", peresobrat);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", peresobrat);
    };
  }, [estKody, peresobrat]);

  if (!dannye) return <ScreenLoading error={failure} onRetry={load} />;

  const pokazyvaem = dannye.items;

  const mozhnoZavodit = can(user, "keys.create");
  const mozhnoUpravlyat = can(user, "keys.manage");

  const pokazat = async (klyuch: Klyuch) => {
    if (!guard.take()) return;
    try {
      const kod = await api.post<Kod>(`/keys/${klyuch.id}/code`);
      setKody((bylo) => ({ ...bylo, [klyuch.id]: naEkran(kod) }));
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
    otmetitKopiyu(id);
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

  const otkrytDostupKategorii = async (kat: Kategoriya) => {
    try {
      const otvet = await api.get<{ people: Chelovek[] }>(`/keys/categories/${kat.id}/access`);
      setDostupKategorii({ kat, lyudi: otvet.people });
    } catch (e) {
      toastError(e);
    }
  };

  const perekluchitKategoriyu = async (chelovek: Chelovek) => {
    if (!dostupKategorii || !guard.take()) return;
    try {
      const otvet = await api.post<{ people: Chelovek[] }>(
        `/keys/categories/${dostupKategorii.kat.id}/access`,
        { user_id: chelovek.id, otkryt: !chelovek.otkryt },
      );
      setDostupKategorii({ ...dostupKategorii, lyudi: otvet.people });
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
      const otvet = await api.get<{ items: Zapasnoy[]; zakryty: boolean }>(
        `/keys/${klyuch.id}/backup-codes`,
      );
      setZapasnye({ klyuch, items: otvet.items, zakryty: otvet.zakryty });
    } catch (e) {
      toastError(e);
    }
  };

  const potratit = async (nomer: number) => {
    if (!zapasnye || !guard.take()) return;
    try {
      const otvet = await api.post<{ items: Zapasnoy[]; zakryty: boolean }>(
        `/keys/${zapasnye.klyuch.id}/backup-codes/${nomer}/spend`,
      );
      setZapasnye({ ...zapasnye, items: otvet.items, zakryty: otvet.zakryty });
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
      const otvet = await api.post<{ secret: string; qr: string; zakroetsya_cherez: number }>(
        `/keys/${klyuch.id}/secret`,
      );
      setPerenos({
        klyuch,
        secret: otvet.secret,
        qr: otvet.qr,
        zakroetsya: Date.now() + otvet.zakroetsya_cherez * 1000,
      });
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
    <div className={`keys-icon ${klyuch.znak ? "" : ottenokZnaka(klyuch.issuer || klyuch.title)}`}>
      {klyuch.znak ? (
        // Значок отдаёт сервер по одному: их три с половиной тысячи, и разом
        // это четыре мегабайта на каждое открытие экрана.
        <span
          className="keys-icon-image"
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
        <div className="keys-safe">
          <div className="keys-dots">{"•".repeat(Math.min(klyuch.digits, 4))} {"•".repeat(3)}</div>
          <button
            type="button"
            className="keys-safe-btn"
            disabled={guard.busy || vKorzine}
            onClick={() => void pokazat(klyuch)}
          >
            <Icon name="eye" size={13} />
            {t("keysShow")}
          </button>
        </div>
      );
    }
    if (kod.zhdyom) {
      // Прежние цифры уже не годятся, новых ещё нет. Показать их с полным
      // кольцом значило бы соврать ровно в тот момент, когда человек копирует.
      return (
        <div className="keys-safe">
          <div className="keys-dots">{"•".repeat(Math.min(klyuch.digits, 4))} {"•".repeat(3)}</div>
          <span className="keys-safe-wait">{t("keysRefreshing")}</span>
        </div>
      );
    }
    const dolya = kod.ostalos / kod.period;
    const stupen = kod.ostalos <= 5 ? "keys-end" : kod.ostalos <= 10 ? "keys-soon" : "";
    const cifry = kod.code.split("");
    const seredina = Math.ceil(cifry.length / 2);
    return (
      <div className="keys-safe">
        <div className="keys-digits" aria-label={t("keysCodeAria", { code: kod.code })}>
          {cifry.map((c, i) => (
            <span key={i}>
              {i === seredina && <span className="keys-space" />}
              <span className="keys-digit" style={{ ["--i" as string]: i }}>{c}</span>
            </span>
          ))}
        </div>
        <div className={`keys-ring ${stupen}`} style={{ ["--dolya" as string]: dolya }}>
          <span className="keys-ring-number">{kod.ostalos}</span>
        </div>
        <button
          type="button"
          className={`keys-safe-btn keys-copy ${skopirovan === klyuch.id ? "keys-copied" : ""}`}
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
      <div className="keys-header">
        <div className="keys-header-text">
          <h1 className="keys-title">{t("modKeys")}</h1>
          <div className="keys-summary">
            <span className="keys-summary-number">
              {t("keysSummary", { n: dannye.total, k: dannye.categories.length })}
            </span>
            {dannye.prosyat > 0 && (
              <button
                type="button"
                className="keys-summary-alarm"
                aria-pressed={tolkoTrevozhnye}
                onClick={() => {
                  // Тревога — это вид, а не подмешанный к полке фильтр: включаясь,
                  // она открывает весь раздел и снимает поиск, иначе число и
                  // список снова считались бы по разным наборам. Выключаясь — не
                  // трогает ничего: человек возвращается туда, где стоял.
                  if (!tolkoTrevozhnye) {
                    setPolka({ vid: "vse" });
                    setPoisk("");
                  }
                  setTolkoTrevozhnye((b) => !b);
                }}
              >
                <Icon name="alert" size={11} />
                {t("keysNeedRotation", { n: dannye.prosyat })}
              </button>
            )}
          </div>
        </div>
        <div className="keys-header-btns">
          <div className="keys-search">
            <Icon name="search" size={14} />
            <input
              type="text"
              value={poisk}
              placeholder={t("keysSearch")}
              aria-label={t("keysSearch")}
              onChange={(e) => {
                setTolkoTrevozhnye(false);
                setPoisk(e.target.value);
              }}
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

      <div className="keys-body">
        <nav className="keys-folders" aria-label={t("keysCategories")}>
          <div className="keys-folders-heading">
            {t("keysCategories")}
            <span className="keys-folder-count">{dannye.categories.length}</span>
          </div>
          <div className="keys-folders-list">
            <button
              type="button"
              className={`keys-folder ${polkaVybrana({ vid: "vse" }) ? "keys-selected" : ""}`}
              onClick={() => vybratPolku({ vid: "vse" })}
            >
              <Icon name="database" size={15} />
              <span className="keys-folder-name">{t("keysAll")}</span>
              <span className="keys-folder-count">{dannye.total}</span>
            </button>
            <button
              type="button"
              className={`keys-folder ${polkaVybrana({ vid: "moi" }) ? "keys-selected" : ""}`}
              onClick={() => vybratPolku({ vid: "moi" })}
            >
              <Icon name="user" size={15} />
              <span className="keys-folder-name">{t("keysMine")}</span>
              <span className="keys-folder-count">{dannye.mine}</span>
            </button>
            {dannye.categories.map((kat) => (
              <div key={kat.id} className="keys-folder-row">
                <button
                  type="button"
                  className={`keys-folder ${kat.zakryta_dlya_menya ? "keys-closed" : ""} ${
                    polkaVybrana({ vid: "kategoriya", id: kat.id }) ? "keys-selected" : ""
                  }`}
                  disabled={kat.zakryta_dlya_menya}
                  title={kat.zakryta_dlya_menya ? t("keysCategoryClosedHint") : undefined}
                  onClick={() => vybratPolku({ vid: "kategoriya", id: kat.id })}
                >
                  <Icon name={kat.zakrytaya ? "lock" : "folder"} size={15} />
                  <span className="keys-folder-name truncate">{kat.name}</span>
                  <span className="keys-folder-count">{kat.count}</span>
                </button>
                {(rootLi || kat.mine) && !kat.zakryta_dlya_menya && (
                  <div className="keys-folder-tools">
                    {/* Закрытая списков не слушает — это её определение, и
                        кнопка, заводящая доступ, который ничего не откроет,
                        была бы обманом. */}
                    {mozhnoUpravlyat && !kat.zakrytaya && (
                      <button
                        type="button"
                        className="btn-icon keys-folder-act keys-folder-share"
                        title={t("keysCategoryAccess")}
                        aria-label={t("keysCategoryAccess")}
                        disabled={guard.busy}
                        onClick={() => void otkrytDostupKategorii(kat)}
                      >
                        <Icon name="staff" size={13} />
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn-icon keys-folder-act keys-folder-remove"
                      title={t("keysCategoryRemove")}
                      aria-label={t("keysCategoryRemove")}
                      disabled={guard.busy}
                      onClick={() => setUbratKat(kat)}
                    >
                      <Icon name="trash" size={13} />
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="keys-folders-below">
            <button
              type="button"
              className={`keys-folder ${polkaVybrana({ vid: "korzina" }) ? "keys-selected" : ""}`}
              onClick={() => vybratPolku({ vid: "korzina" })}
            >
              <Icon name="trash" size={15} />
              <span className="keys-folder-name">{t("keysTrash")}</span>
              <span className="keys-folder-count">{dannye.trash}</span>
            </button>
          </div>
        </nav>

        <div className="keys-grid">
          {pokazyvaem.length === 0 ? (
            <div className="keys-empty">
              <div className="keys-empty-slab">
                <Icon name="lock" size={26} />
              </div>
              <div className="keys-empty-title">{vKorzine ? t("keysTrashEmpty") : t("keysEmpty")}</div>
              <div className="keys-empty-caption">{t("keysEmptyHint")}</div>
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
                className={`keys-card ${klyuch.vazhnost === "urgent" ? "keys-urgent" : ""}`}
                style={{ ["--n" as string]: i }}
              >
                <div className="keys-top">
                  {znakKarty(klyuch)}
                  <div className="keys-names">
                    <div className="keys-name">{klyuch.title}</div>
                    {klyuch.account && <div className="keys-account truncate">{klyuch.account}</div>}
                  </div>
                  {klyuch.can_edit && !vKorzine && (
                    <button
                      type="button"
                      className="btn-icon keys-menu"
                      title={t("keysMore")}
                      aria-label={t("keysMore")}
                      onClick={() => setMenyu(klyuch)}
                    >
                      <Icon name="dots" size={14} />
                    </button>
                  )}
                </div>

                {vKorzine ? (
                  <div className="keys-actions">
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
                  <details className="keys-note">
                    <summary className="keys-note-edge">{klyuch.note}</summary>
                    <div className="keys-note-body">{klyuch.note}</div>
                  </details>
                )}

                <div className="keys-bottom">
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
                  {klyuch.backup_zakryty && (
                    <button
                      type="button"
                      className="chip chip-warning"
                      onClick={() => void otkrytZapasnye(klyuch)}
                    >
                      {t("keysBackupLocked")}
                    </button>
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
                  <div className="keys-row">
                    <a className={`keys-link ${klyuch.task.done ? "" : "keys-alarm"}`} href={`/tasks?id=${klyuch.task.id}`}>
                      <Icon name="clock" size={13} />
                      <span className="truncate">{klyuch.task.title}</span>
                    </a>
                    {klyuch.task.due_at && (
                      <span className="keys-when">{formatData(klyuch.task.due_at, locale)}</span>
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
          <div className="keys-menu-list">
            {mozhnoUpravlyat && (
              <button type="button" className="keys-menu-item" onClick={() => void otkrytPrava(menyu)}>
                <Icon name="staff" size={15} />
                {t("keysWhoSees")}
              </button>
            )}
            <button type="button" className="keys-menu-item" onClick={() => void otkrytZapasnye(menyu)}>
              <Icon name="clipboard" size={15} />
              {t("keysBackupCodes")}
            </button>
            {rootLi && (
              <button type="button" className="keys-menu-item" onClick={() => void otkrytPerenos(menyu)}>
                <Icon name="scan" size={15} />
                {t("keysToPhone")}
              </button>
            )}
            <button
              type="button"
              className="keys-menu-item keys-danger"
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
          <div className="keys-notice keys-notice-danger">
            <Icon name="alert" size={15} />
            <span>{t("keysAccessWarn")}</span>
          </div>
          <div className="keys-rights-list">
            {prava.lyudi.map((chelovek) => (
              <div key={chelovek.id} className="keys-rights-row">
                <Avatar text={chelovek.name} />
                <div className="keys-rights-who">
                  <div className="keys-rights-name">{chelovek.name}</div>
                  <div className="keys-rights-role">
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

      {dostupKategorii && (
        <Modal
          title={t("keysCategoryAccessTitle", { name: dostupKategorii.kat.name })}
          onClose={() => setDostupKategorii(null)}
        >
          <div className="keys-notice">
            <Icon name="alert" size={15} />
            <span>{t("keysCategoryAccessWarn")}</span>
          </div>
          <div className="keys-rights-list">
            {dostupKategorii.lyudi.map((chelovek) => (
              <div key={chelovek.id} className="keys-rights-row">
                <Avatar text={chelovek.name} />
                <div className="keys-rights-who">
                  <div className="keys-rights-name">{chelovek.name}</div>
                  <div className="keys-rights-role">
                    {chelovek.always ? t("keysSeesAlways") : t(chelovek.role === "root" ? "root" : "manager")}
                  </div>
                </div>
                <button
                  type="button"
                  className={`toggle-track ${chelovek.otkryt ? "on" : ""}`}
                  aria-pressed={chelovek.otkryt}
                  aria-label={chelovek.name}
                  disabled={chelovek.always || guard.busy}
                  onClick={() => void perekluchitKategoriyu(chelovek)}
                />
              </div>
            ))}
          </div>
        </Modal>
      )}

      {zapasnye && (
        <Modal title={t("keysBackupTitle", { name: zapasnye.klyuch.title })} onClose={() => setZapasnye(null)}>
          <div className="field-desc" style={{ marginTop: 0, marginBottom: 12 }}>
            {t("keysBackupHint")}
          </div>
          {zapasnye.zakryty ? (
            <div className="keys-notice keys-notice-danger">
              <Icon name="alert" size={15} />
              <span>{t("keysBackupLockedHint")}</span>
            </div>
          ) : zapasnye.items.length === 0 ? (
            <div className="field-desc">{t("keysBackupNone")}</div>
          ) : (
            <>
              <div className="keys-backups">
                {zapasnye.items.map((z, i) => (
                  <button
                    key={i}
                    type="button"
                    className={`keys-backup ${z.potrachen ? "keys-spent" : ""}`}
                    disabled={z.potrachen || guard.busy}
                    title={z.potrachen ? t("keysBackupSpent") : t("keysBackupSpend")}
                    onClick={() => void potratit(i)}
                  >
                    {z.kod}
                  </button>
                ))}
              </div>
              <div className="keys-backups-total">
                <span className="keys-scale">
                  <span
                    className={`keys-scale-bar ${
                      zapasnye.items.filter((z) => !z.potrachen).length < 3 ? "keys-low" : ""
                    }`}
                    style={{
                      width: `${Math.round(
                        (zapasnye.items.filter((z) => !z.potrachen).length / zapasnye.items.length) * 100,
                      )}%`,
                    }}
                  />
                </span>
                <span className="keys-backups-count">
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
          <div className="keys-notice keys-notice-danger">
            <Icon name="alert" size={15} />
            <span>{t("keysSecretWarn")}</span>
          </div>
          <div className="keys-qr-row">
            <div className="keys-qr" role="img" aria-label={t("keysQrAria")} dangerouslySetInnerHTML={{ __html: perenos.qr }} />
            <div className="keys-qr-side">
              <span className="label">{t("keysSecretString")}</span>
              <div className="keys-qr-line">
                <div className="keys-qr-code truncate">{perenos.secret}</div>
                <button
                  type="button"
                  className={`btn btn-sm keys-copy ${skopirovan === -perenos.klyuch.id ? "keys-copied" : ""}`}
                  data-podskazka={t("keysCopied")}
                  title={t("keysCopy")}
                  aria-label={t("keysCopy")}
                  onClick={() => void skopirovat(-perenos.klyuch.id, perenos.secret)}
                >
                  <Icon name="copy" size={14} />
                </button>
              </div>
              <div className="field-desc">{t("keysQrHint")}</div>
              <div className="keys-qr-timer">
                <Icon name="clock" size={13} />
                <span>{t("keysTransferCloses", { n: perenosOstalos })}</span>
              </div>
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
      <div className="keys-field">
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
        <div className="keys-field">
          <span className="label">{t("keysParsed")}</span>
          {razbor ? (
            <div className="keys-breakdown">
              <div className="keys-breakdown-row">
                <span className="keys-breakdown-name">{t("keysService")}</span>
                <span className="keys-breakdown-value">{razbor.issuer || "—"}</span>
              </div>
              <div className="keys-breakdown-row">
                <span className="keys-breakdown-name">{t("keysAccount")}</span>
                <span className="keys-breakdown-value truncate">{razbor.account || "—"}</span>
              </div>
              <div className="keys-breakdown-row">
                <span className="keys-breakdown-name">{t("keysDigits")}</span>
                <span className="keys-breakdown-value">{razbor.digits}</span>
              </div>
              <div className="keys-breakdown-row">
                <span className="keys-breakdown-name">{t("keysPeriod")}</span>
                <span className="keys-breakdown-value">{razbor.period}</span>
              </div>
              <div className="keys-breakdown-row keys-breakdown-total">
                <span className="keys-breakdown-name">{t("keysCheck")}</span>
                <span className="keys-breakdown-value keys-breakdown-good">
                  <Icon name="check" size={12} />
                  {razbor.proverka}
                </span>
              </div>
            </div>
          ) : (
            <div className="keys-breakdown">
              <div className="keys-breakdown-row">
                <span className="keys-breakdown-name">{t("keysCheck")}</span>
                <span className="keys-breakdown-value keys-breakdown-bad">{otkaz}</span>
              </div>
            </div>
          )}
        </div>
      )}

      <div className="keys-field">
        <label className="label" htmlFor="keys-name">{t("keysName")}</label>
        <input id="keys-name" className="input" value={nazvanie} onChange={(e) => setNazvanie(e.target.value)} />
        <div className="field-desc">{t("keysNameHint")}</div>
      </div>

      <div className="keys-field">
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

      <div className="keys-field">
        <label className="label" htmlFor="kl-vazhnost">{t("keysImportance")}</label>
        <select id="kl-vazhnost" className="input" value={vazhnost} onChange={(e) => setVazhnost(e.target.value)}>
          <option value="urgent">{t("vazhnostUrgent")}</option>
          <option value="high">{t("vazhnostHigh")}</option>
          <option value="normal">{t("vazhnostNormal")}</option>
          <option value="low">{t("vazhnostLow")}</option>
        </select>
      </div>

      <div className="keys-field">
        <label className="label" htmlFor="keys-note">{t("keysNote")}</label>
        <textarea
          id="keys-note"
          className="input"
          rows={3}
          placeholder={t("keysNotePlaceholder")}
          value={zametka}
          onChange={(e) => setZametka(e.target.value)}
        />
      </div>

      <div className="keys-field">
        <span className="label">{t("keysReminder")}</span>
        <div className="keys-rights-row" style={{ borderBottom: 0, paddingLeft: 0 }}>
          <span className="keys-rights-who">
            <span className="keys-rights-name">{t("keysReminderHalfYear")}</span>
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

      <div className="keys-actions">
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
      <div className="keys-field">
        <label className="label" htmlFor="kl-kat-imya">{t("keysCategoryName")}</label>
        <input
          id="kl-kat-imya"
          className="input"
          autoFocus
          value={imya}
          onChange={(e) => setImya(e.target.value)}
        />
      </div>

      <div className="keys-field">
        <span className="label">{t("keysCategoryWho")}</span>
        <div className="keys-rights-row" style={{ borderBottom: 0, paddingLeft: 0 }}>
          <span className="keys-rights-who">
            <span className="keys-rights-name">{t("keysCategoryClosed")}</span>
            <span className="keys-rights-role">{t("keysCategoryClosedHint")}</span>
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

      <div className="keys-actions">
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
