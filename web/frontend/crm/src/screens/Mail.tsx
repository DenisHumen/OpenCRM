import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Avatar, ConfirmModal, Dochitat, EmptyState, LoadFailed, Modal, ScreenLoading, Spinner } from "../components/ui";
import { api, type Zalivka } from "../lib/api";
import { useApp } from "../lib/app";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { fileSize, formatDateTime, initials } from "../lib/format";
import type { Locale, TFunc, TranslationKey } from "../lib/i18n";
import { useLiveTopic, useNachatayaPravka } from "../lib/live";
import { moduleOn } from "../lib/modules";
import { can } from "../lib/permissions";
import { signal_o_soobshchenii } from "../lib/signaly";

/**
 * Веб-почта: ящики и папки | список писем | письмо. Устройство и доводы —
 * docs/bloki/32-pochtovyy-server.md, API — `/mail/boxes/*`.
 *
 * Адрес `/mail?box=&folder=&uid=` — источник правды о том, что открыто: по нему
 * приходит нажатие на пуш-уведомление, и он же даёт кнопку «назад» на телефоне.
 */

/** Ящик, как его отдаёт `/mail/boxes` — без сети, из базы. */
export interface WmBox {
  id: number;
  address: string;
  title: string;
  server_id: number | null;
  unseen: number;
  can_send: boolean;
  is_shared: boolean;
  /** UIDNEXT «Входящих» от наблюдателя: растёт только с приходом письма. */
  inbox_uidnext?: number | null;
  inbox_uidvalidity?: number | null;
}

type Osobaya = "inbox" | "sent" | "drafts" | "trash" | "junk" | "archive" | null;

interface WmFolder {
  name: string;
  path: string;
  special: Osobaya;
  total: number;
  unseen: number;
  /** Разделитель уровней папок у этого сервера: «.» у одних, «/» у других. */
  delimiter?: string | null;
}

interface WmAdres {
  name: string;
  email: string;
}

interface WmStroka {
  uid: number;
  subject: string;
  from: WmAdres | null;
  to: WmAdres[];
  date: string | null;
  seen: boolean;
  flagged: boolean;
  answered: boolean;
  has_attachments: boolean;
  size: number;
  preview: string;
}

interface WmVlozhenie {
  part: string;
  name: string;
  size: number;
  content_type: string;
  inline: boolean;
}

interface WmPismo {
  uid: number;
  subject: string;
  from: WmAdres | null;
  to: WmAdres[];
  cc: WmAdres[];
  reply_to: WmAdres[];
  date: string | null;
  text: string;
  html: string;
  /** Сервер спрятал внешние картинки в `data-src` — есть что показать по кнопке. */
  remote_images: boolean;
  attachments: WmVlozhenie[];
  flags: string[];
  client: { id: number; name: string } | null;
}

interface WmStranitsa {
  items: unknown[];
  total: number;
}

/** С чего начинается окно «Написать». */
interface Nachalo {
  boxId: number | null;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  html: string;
  imena: Record<string, string>;
  reply_to?: { folder: string; uid: number };
  forward_of?: { folder: string; uid: number };
  draft?: Chernovik;
  client_id?: number | null;
  zametka?: string;
}

/** Ответ отправки: `refused` — адреса, которые сервер получателя не принял. */
interface OtvetOtpravki {
  ok: boolean;
  refused?: Record<string, unknown> | string[];
}

/** Где лежит сохранённый черновик: папку отдаёт сервер, без неё стереть его было бы нечем. */
interface Chernovik {
  box: number;
  uid: number;
  folder: string | null;
}

/** По скольку писем дочитывается список. */
const NA_STRANITSE = 50;
/** Черновик сохраняется не чаще раза в пять секунд: чаще — это запись в IMAP на каждую букву. */
const PAUZA_CHERNOVIKA = 5000;
/** Пределы почтовых серверов: файл больше 25 МБ и письмо больше 35 МБ большинство не примет. */
const PREDEL_FAYLA = 25 * 1024 * 1024;
const PREDEL_PISMA = 35 * 1024 * 1024;
const ADRES = /^[^\s@<>(),;:"]+@[^\s@<>(),;:"]+\.[^\s@<>(),;:"]+$/;

const PORYADOK: Record<string, number> = { inbox: 0, drafts: 1, sent: 2, archive: 3, junk: 4, trash: 5 };
const ZNACHOK_PAPKI: Record<string, string> = {
  inbox: "inbox",
  drafts: "note",
  sent: "send",
  archive: "archive",
  junk: "alert",
  trash: "trash",
};
const IMYA_PAPKI: Record<string, TranslationKey> = {
  inbox: "wmInbox",
  drafts: "wmDrafts",
  sent: "wmSent",
  archive: "wmArchive",
  junk: "wmJunk",
  trash: "wmTrash",
};

// --- разбор ответов -----------------------------------------------------------
// Сервер пишет адреса объектами, но строку «Имя <адрес>» тоже понимаем: письмо
// без имени отправителя не должно ронять экран.

function razobratAdres(s: string): WmAdres {
  const m = s.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
  return m ? { name: m[1].trim(), email: m[2].trim() } : { name: "", email: s.trim() };
}

function adresa(x: unknown): WmAdres[] {
  if (!x) return [];
  if (Array.isArray(x)) return x.flatMap(adresa);
  if (typeof x === "string") return x.split(",").map(razobratAdres).filter((a) => a.email);
  if (typeof x === "object") {
    const o = x as { name?: unknown; email?: unknown; address?: unknown };
    const email = String(o.email ?? o.address ?? "");
    return email ? [{ name: String(o.name ?? ""), email }] : [];
  }
  return [];
}

/** Дата письма к виду с явным поясом: `parseDate` дописывает Z, и «-05:00Z» стало бы Invalid Date. */
function normDate(iso: unknown): string | null {
  if (typeof iso !== "string" || !iso) return null;
  const d = new Date(/[zZ]$|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function normStroka(raw: any): WmStroka {
  return {
    uid: Number(raw?.uid),
    subject: String(raw?.subject ?? ""),
    from: adresa(raw?.from)[0] ?? null,
    to: adresa(raw?.to),
    date: normDate(raw?.date),
    seen: !!raw?.seen,
    flagged: !!raw?.flagged,
    answered: !!raw?.answered,
    has_attachments: !!raw?.has_attachments,
    size: Number(raw?.size ?? 0),
    preview: String(raw?.preview ?? ""),
  };
}

function normPismo(raw: any): WmPismo {
  return {
    uid: Number(raw?.uid),
    subject: String(raw?.subject ?? ""),
    from: adresa(raw?.from)[0] ?? null,
    to: adresa(raw?.to),
    cc: adresa(raw?.cc),
    reply_to: adresa(raw?.reply_to),
    date: normDate(raw?.date),
    text: String(raw?.text ?? ""),
    html: String(raw?.html ?? ""),
    remote_images: raw?.remote_images === undefined ? String(raw?.html ?? "").includes("data-src") : !!raw.remote_images,
    attachments: Array.isArray(raw?.attachments) ? raw.attachments : [],
    flags: Array.isArray(raw?.flags) ? raw.flags.map(String) : [],
    client: raw?.client && raw.client.id ? { id: Number(raw.client.id), name: String(raw.client.name ?? "") } : null,
  };
}

// --- папки ---------------------------------------------------------------------

function nazvaniePapki(f: WmFolder, t: TFunc): string {
  if (f.special && IMYA_PAPKI[f.special]) return t(IMYA_PAPKI[f.special]);
  if (f.path.toUpperCase() === "INBOX") return t("wmInbox");
  return f.name;
}

function znachokPapki(f: WmFolder): string {
  if (f.special && ZNACHOK_PAPKI[f.special]) return ZNACHOK_PAPKI[f.special];
  if (f.path.toUpperCase() === "INBOX") return "inbox";
  return "folder";
}

/** Вложенность по пути: разделитель у Dovecot бывает и «.», и «/». */
function glubina(f: WmFolder): number {
  if (f.delimiter) return Math.min(4, f.path.split(f.delimiter).length - 1);
  if (f.path === f.name || !f.path.endsWith(f.name)) return 0;
  const pristavka = f.path.slice(0, -f.name.length);
  return Math.min(4, (pristavka.match(/[./]/g) ?? []).length);
}

function uporyadochit(papki: WmFolder[]): WmFolder[] {
  const ves = (f: WmFolder) =>
    f.special ? PORYADOK[f.special] ?? 6 : f.path.toUpperCase() === "INBOX" ? 0 : 7;
  return [...papki].sort((a, b) => ves(a) - ves(b) || a.path.localeCompare(b.path));
}

function sluzhebnaya(f: WmFolder): boolean {
  return !!f.special || f.path.toUpperCase() === "INBOX";
}

// --- текст и разметка ---------------------------------------------------------

function ekran(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function tekstIzHtml(html: string): string {
  if (!html) return "";
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("br").forEach((br) => br.replaceWith("\n"));
  doc.querySelectorAll("p, div, li, tr, h1, h2, h3, h4, blockquote").forEach((el) => el.append("\n"));
  return (doc.body.textContent ?? "").replace(/\n{3,}/g, "\n\n").trim();
}

const OPASNYE = new Set([
  "SCRIPT", "STYLE", "IFRAME", "FRAME", "OBJECT", "EMBED", "LINK", "META", "BASE", "FORM", "INPUT",
  "BUTTON", "TEXTAREA", "SELECT", "SVG", "MATH", "NOSCRIPT", "TEMPLATE", "AUDIO", "VIDEO",
]);
const ATRIBUTY = new Set(["href", "src", "alt", "title", "colspan", "rowspan", "align", "data-src"]);

/**
 * Разметка черновика для редактора. Сервер её уже чистил, но `innerHTML`
 * редактора — это наш документ, и `<img onerror>` в нём выполнился бы. Вторая
 * чистка дешевле одного такого случая.
 */
function dlyaRedaktora(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const obkhod = (el: Element) => {
    for (const ditya of Array.from(el.children)) {
      if (OPASNYE.has(ditya.tagName)) {
        ditya.remove();
        continue;
      }
      for (const attr of Array.from(ditya.attributes)) {
        const imya = attr.name.toLowerCase();
        const plokhoy = (imya === "href" || imya === "src") && /^\s*(javascript|vbscript|data:text)/i.test(attr.value);
        if (!ATRIBUTY.has(imya) || plokhoy) ditya.removeAttribute(attr.name);
      }
      obkhod(ditya);
    }
  };
  obkhod(doc.body);
  return doc.body.innerHTML;
}

/** Файл из строки `data:` — вставленная картинка уходит вложением, а не телом. */
function faylIzDataUrl(src: string, nomer: number): File | null {
  const m = src.trim().match(/^data:([^;,]*)((?:;[^;,]*)*),([\s\S]*)$/i);
  if (!m) return null;
  const tip = (m[1] || "application/octet-stream").toLowerCase();
  let bayty = new Uint8Array(0);
  try {
    if (/;base64/i.test(m[2])) {
      const s = atob(m[3].replace(/\s+/g, ""));
      bayty = new Uint8Array(s.length);
      for (let i = 0; i < s.length; i++) bayty[i] = s.charCodeAt(i);
    } else {
      bayty = new Uint8Array(new TextEncoder().encode(decodeURIComponent(m[3])));
    }
  } catch {
    return null;
  }
  const rasshirenie = (tip.split("/")[1] ?? "").replace(/\+.*$/, "").replace("jpeg", "jpg") || "bin";
  return new File([bayty], `image-${nomer}.${rasshirenie}`, { type: tip });
}

function tekstVHtml(tekst: string): string {
  return ekran(tekst).replace(/\r?\n/g, "<br>");
}

function kakAdres(a: WmAdres | null): string {
  if (!a) return "";
  return a.name ? `${a.name} <${a.email}>` : a.email;
}

function sPristavkoy(pristavka: "Re" | "Fwd", tema: string): string {
  const uzhe = pristavka === "Re" ? /^\s*re\s*:/i : /^\s*(fwd?|fw)\s*:/i;
  return uzhe.test(tema) ? tema : `${pristavka}: ${tema}`;
}

/** Время в строке списка: сегодня — часы, в этом году — день и месяц, раньше — дата целиком. */
function kogdaKoroche(iso: string | null, locale: Locale): string {
  if (!iso) return "";
  const d = new Date(iso);
  const lang = locale === "ru" ? "ru-RU" : "en-US";
  const seychas = new Date();
  if (d.toDateString() === seychas.toDateString()) {
    return d.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
  }
  if (d.getFullYear() === seychas.getFullYear()) {
    return d.toLocaleDateString(lang, { day: "numeric", month: "short" });
  }
  return d.toLocaleDateString(lang, { day: "2-digit", month: "2-digit", year: "numeric" });
}

/** Дата в шапке цитаты: письмо уйдёт и будет прочитано завтра, и «сегодня» в нём соврёт. */
function polnayaData(iso: string | null, locale: Locale): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(locale === "ru" ? "ru-RU" : "en-US", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Текст письма со ссылками, которые можно нажать. Разметку не строим — только `<a>`. */
function sSsylkami(tekst: string): ReactNode[] {
  return tekst.split(/(https?:\/\/[^\s<>"']+)/g).map((kusok, i) =>
    i % 2 === 1 ? (
      <a key={i} href={kusok} target="_blank" rel="noopener noreferrer">
        {kusok}
      </a>
    ) : (
      kusok
    ),
  );
}

/** Свежая первая страница поверх показанного: дочитанный хвост остаётся, старые
 *  строки первой страницы заменяются. Сортировка — по приходу, а UID в папке
 *  растёт с приходом, поэтому «старше первой страницы» — это UID меньше её
 *  наименьшего. */
function slitSverhu(bylo: WmStroka[], svezhie: WmStroka[]): WmStroka[] {
  if (svezhie.length === 0) return [];
  const naimenshiy = Math.min(...svezhie.map((s) => s.uid));
  return [...svezhie, ...bylo.filter((s) => s.uid < naimenshiy)];
}

/** Следующая страница: письма, пришедшие между запросами, сдвигают страницы, и повторы отбрасываются. */
function slitSnizu(bylo: WmStroka[], dalshe: WmStroka[]): WmStroka[] {
  const est = new Set(bylo.map((s) => s.uid));
  return [...bylo, ...dalshe.filter((s) => !est.has(s.uid))];
}

function unikalnye(spisok: string[]): string[] {
  const vidano = new Set<string>();
  return spisok.filter((a) => {
    const k = a.toLowerCase();
    if (!a || vidano.has(k)) return false;
    vidano.add(k);
    return true;
  });
}

// =============================================================================

export function Mail() {
  const { t, locale, toast, toastError, user } = useApp();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const pishet = can(user, "mail.create");
  const upravlyaet = can(user, "mail.manage");

  // --- ящики ---
  const [boxes, setBoxes] = useState<WmBox[] | null>(null);
  const [popytka, setPopytka] = useState(0);
  const { failure, fail, clear } = useFailure();
  /** UIDNEXT «Входящих» по ящикам на прошлом чтении: рост — повод для сигнала. */
  const proshlye = useRef<Map<number, { next: number; validity: number | null }> | null>(null);

  const boxParam = Number(params.get("box")) || null;
  const folder = params.get("folder") || "INBOX";
  const uid = Number(params.get("uid")) || null;
  // Архив CRM — копия писем в базе: читается и тогда, когда почтового сервера уже нет.
  const arhiv = params.get("arhiv") === "1";
  const aid = Number(params.get("aid")) || null;
  const boxId = boxes ? (boxes.some((b) => b.id === boxParam) ? boxParam : boxes[0]?.id ?? null) : null;
  const box = boxes?.find((b) => b.id === boxId) ?? null;
  const otpraviteli = useMemo(() => (boxes ?? []).filter((b) => b.can_send), [boxes]);
  const mozhnoPisat = pishet && otpraviteli.length > 0;

  // Что на экране — для сигнала, который приходит из фонового перечитывания.
  const naEkrane = useRef({ box: boxId, folder });
  naEkrane.current = { box: boxId, folder };

  const prinyatYashchiki = useCallback(
    (spisok: WmBox[]) => {
      setBoxes(spisok);
      const bylo = proshlye.current;
      // Сигнал — по росту UIDNEXT, а не непрочитанных: их растит и коллега,
      // пометивший письмо непрочитанным, и письмо, вернувшееся из «Спама».
      const seychas = new Map<number, { next: number; validity: number | null }>();
      for (const b of spisok) {
        if (typeof b.inbox_uidnext === "number") {
          seychas.set(b.id, { next: b.inbox_uidnext, validity: b.inbox_uidvalidity ?? null });
        }
      }
      if (bylo) {
        for (const b of spisok) {
          const ran = bylo.get(b.id);
          const teper = seychas.get(b.id);
          if (!ran || !teper || teper.validity !== ran.validity || teper.next <= ran.next) continue;
          // На виду — когда открыты именно эти «Входящие» в видимой вкладке:
          // новое письмо и так появится в списке перед глазами.
          const na_vidu =
            document.visibilityState === "visible" &&
            naEkrane.current.box === b.id &&
            naEkrane.current.folder.toUpperCase() === "INBOX";
          signal_o_soobshchenii({
            zagolovok: b.title || b.address,
            telo: t("wmNewMail", { n: teper.next - ran.next }),
            na_vidu,
            // Метка пуша того же ящика: окно вкладки заменяет пуш, а не встаёт вторым.
            tag: `opencrm-mail-${b.id}`,
          });
        }
      }
      proshlye.current = seychas;
    },
    [t],
  );

  useEffect(() => {
    let current = true;
    clear();
    api
      .get<WmBox[]>("/mail/boxes")
      .then((spisok) => {
        if (current) prinyatYashchiki(spisok);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [popytka, prinyatYashchiki, fail, clear]);

  /** Фоновое перечитывание: отказ молча — на экране остаётся то, что было. */
  const perechitatYashchiki = useCallback(() => {
    api.get<WmBox[]>("/mail/boxes").then(prinyatYashchiki, () => undefined);
  }, [prinyatYashchiki]);

  // --- папки ---
  const [papki, setPapki] = useState<Record<number, WmFolder[]>>({});
  const [papkiBeda, setPapkiBeda] = useState<{ box: number; e: unknown } | null>(null);

  const zagruzitPapki = useCallback((b: number) => {
    api
      .get<WmFolder[]>(`/mail/boxes/${b}/folders`)
      .then((spisok) => {
        setPapki((vse) => ({ ...vse, [b]: uporyadochit(spisok) }));
        setPapkiBeda((beda) => (beda?.box === b ? null : beda));
      })
      .catch((e) => setPapkiBeda({ box: b, e }));
  }, []);

  useEffect(() => {
    if (boxId) zagruzitPapki(boxId);
  }, [boxId, zagruzitPapki]);

  const papkiYashchika = boxId ? papki[boxId] ?? null : null;
  const tekPapka = papkiYashchika?.find((f) => f.path === folder) ?? null;
  const papkaChernovikov = (b: number) => papki[b]?.find((f) => f.special === "drafts")?.path ?? null;
  const iskhodyashchaya = tekPapka?.special === "sent" || tekPapka?.special === "drafts";

  // --- список ---
  const [poisk, setPoisk] = useState("");
  const [neprochitannye, setNeprochitannye] = useState(false);
  const [pomechennye, setPomechennye] = useState(false);
  const typed = useDebounced(poisk);
  const [spisok, setSpisok] = useState<{
    otbor: string;
    papka: string;
    stroki: WmStroka[];
    vsego: number;
    stranitsa: number;
  } | null>(null);
  const [spisokBeda, setSpisokBeda] = useState<unknown>(null);
  const [perezapros, setPerezapros] = useState(0);
  const [dochityvaem, setDochityvaem] = useState(false);
  const [vybrano, setVybrano] = useState<Set<number>>(new Set());
  const poslednyayaGalka = useRef<number | null>(null);
  // Отбор, которому принадлежит показанный список: дочитка и фоновое
  // перечитывание сверяются с ним, иначе страница прошлого отбора допишется к
  // находкам нового.
  const otbor_spiska = useRef("");

  const papkaKlyuch = boxId ? `${boxId}|${folder}` : "";
  const otbor = useMemo(() => {
    if (!boxId || arhiv) return "";
    const p = new URLSearchParams({ folder, per_page: String(NA_STRANITSE) });
    if (typed.trim()) p.set("q", typed.trim());
    if (neprochitannye) p.set("unread", "true");
    if (pomechennye) p.set("flagged", "true");
    return `/mail/boxes/${boxId}/messages?${p}`;
  }, [boxId, arhiv, folder, typed, neprochitannye, pomechennye]);

  useEffect(() => {
    otbor_spiska.current = otbor;
    if (!otbor) return;
    let current = true;
    setSpisokBeda(null);
    api
      .get<WmStranitsa>(`${otbor}&page=1`)
      .then((d) => {
        if (!current) return;
        setSpisok({ otbor, papka: papkaKlyuch, stroki: d.items.map(normStroka), vsego: d.total, stranitsa: 1 });
        setVybrano((bylo) => (bylo.size ? new Set() : bylo));
      })
      .catch((e) => {
        if (current) setSpisokBeda(e);
      });
    return () => {
      current = false;
    };
  }, [otbor, papkaKlyuch, perezapros]);

  /** Перечитать первую страницу, не теряя дочитанного хвоста и выделения. */
  const obnovitSpisok = useCallback(() => {
    const sprosheno = otbor_spiska.current;
    if (!sprosheno) return;
    api.get<WmStranitsa>(`${sprosheno}&page=1`).then(
      (d) => {
        if (otbor_spiska.current !== sprosheno) return;
        const svezhie = d.items.map(normStroka);
        setSpisok((s) => (s && s.otbor === sprosheno ? { ...s, stroki: slitSverhu(s.stroki, svezhie), vsego: d.total } : s));
      },
      () => undefined,
    );
  }, []);

  const dochitat = async () => {
    if (dochityvaem || !spisok) return;
    setDochityvaem(true);
    const sprosheno = otbor;
    try {
      const d = await api.get<WmStranitsa>(`${otbor}&page=${spisok.stranitsa + 1}`);
      if (otbor_spiska.current !== sprosheno) return;
      const dalshe = d.items.map(normStroka);
      setSpisok((s) =>
        s && s.otbor === sprosheno
          ? { ...s, stroki: slitSnizu(s.stroki, dalshe), vsego: d.total, stranitsa: s.stranitsa + 1 }
          : s,
      );
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  const obnovitSchyotchiki = useCallback(() => {
    perechitatYashchiki();
    const b = naEkrane.current.box;
    if (b) zagruzitPapki(b);
  }, [perechitatYashchiki, zagruzitPapki]);

  // Новое письмо, чужая пометка, сосед переложил письмо — перечитать ящики,
  // папки и список. Открытое письмо и начатое письмо не трогаем: первое
  // потеряло бы прокрутку, второе — набранное.
  useLiveTopic("mail", () => {
    obnovitSchyotchiki();
    obnovitSpisok();
  });

  const stroki = spisok && spisok.papka === papkaKlyuch ? spisok.stroki : null;
  const gruzim = !spisok || spisok.otbor !== otbor;

  // --- навигация ---
  const otkrytoNami = useRef<number | null>(null);
  const [bokovaya, setBokovaya] = useState(false);

  const adresEkrana = (b: number | null, f: string, u: number | null) => {
    const p = new URLSearchParams();
    if (b) p.set("box", String(b));
    if (f && f !== "INBOX") p.set("folder", f);
    if (u) p.set("uid", String(u));
    return p;
  };

  const otkrytPapku = (b: number, f: string) => {
    setBokovaya(false);
    setVybrano(new Set());
    setPoisk("");
    otkrytoNami.current = null;
    setParams(adresEkrana(b, f, null), { replace: true });
  };

  const otkrytPismo = (u: number) => {
    // Из списка в письмо — новая запись истории (телефонное «назад» вернёт к
    // списку), из письма в письмо — замена: иначе «назад» листал бы прочитанное.
    const zamena = uid !== null;
    if (!zamena) otkrytoNami.current = u;
    setParams(adresEkrana(boxId, folder, u), { replace: zamena });
  };

  const zakrytPismo = () => {
    if (otkrytoNami.current !== null) {
      otkrytoNami.current = null;
      navigate(-1);
      return;
    }
    setParams(adresEkrana(boxId, folder, null), { replace: true });
  };

  const adresArhiva = (b: number, a: number | null) => {
    const p = new URLSearchParams({ box: String(b), arhiv: "1" });
    if (a) p.set("aid", String(a));
    return p;
  };

  const otkrytArhiv = (b: number) => {
    setBokovaya(false);
    setVybrano(new Set());
    otkrytoNami.current = null;
    setParams(adresArhiva(b, null), { replace: true });
  };

  const otkrytArhivPismo = (a: number) => {
    if (!boxId) return;
    const zamena = aid !== null;
    if (!zamena) otkrytoNami.current = a;
    setParams(adresArhiva(boxId, a), { replace: zamena });
  };

  const zakrytArhivPismo = () => {
    if (otkrytoNami.current !== null) {
      otkrytoNami.current = null;
      navigate(-1);
      return;
    }
    if (boxId) setParams(adresArhiva(boxId, null), { replace: true });
  };

  // --- письмо ---
  const [pismo, setPismo] = useState<WmPismo | null>(null);
  const [pismoBeda, setPismoBeda] = useState<unknown>(null);
  const [pismoPopytka, setPismoPopytka] = useState(0);
  const [kartinki, setKartinki] = useState(false);

  useEffect(() => {
    setPismo(null);
    setPismoBeda(null);
    setKartinki(false);
    if (!boxId || !uid) return;
    let current = true;
    api
      .get(`/mail/boxes/${boxId}/messages/${uid}?folder=${encodeURIComponent(folder)}`)
      .then((raw) => {
        if (!current) return;
        setPismo(normPismo(raw));
        // Сервер поставил \Seen, когда отдал письмо: строка в списке перестаёт
        // быть жирной сразу, счётчики — следующим чтением.
        setSpisok((s) => (s ? { ...s, stroki: s.stroki.map((r) => (r.uid === uid ? { ...r, seen: true } : r)) } : s));
        obnovitSchyotchiki();
      })
      .catch((e) => {
        if (current) setPismoBeda(e);
      });
    return () => {
      current = false;
    };
  }, [boxId, folder, uid, pismoPopytka, obnovitSchyotchiki]);

  // --- действия над письмами ---
  const deystvieGuard = useGuard();
  const papkaGuard = useGuard();
  const [udalitNavsegda, setUdalitNavsegda] = useState<number[] | null>(null);

  const izmenitStroki = (uids: number[], izm: Partial<WmStroka>) =>
    setSpisok((s) => (s ? { ...s, stroki: s.stroki.map((r) => (uids.includes(r.uid) ? { ...r, ...izm } : r)) } : s));

  const ubratStroki = (uids: number[]) => {
    setSpisok((s) =>
      s
        ? {
            ...s,
            stroki: s.stroki.filter((r) => !uids.includes(r.uid)),
            vsego: Math.max(0, s.vsego - uids.length),
          }
        : s,
    );
    setVybrano((bylo) => new Set([...bylo].filter((u) => !uids.includes(u))));
  };

  /** Флажки — состояние, а не запись: повтор ставит то же самое (POST_WITHOUT_LATCH). */
  const flagi = async (uids: number[], izm: { seen?: boolean; flagged?: boolean }) => {
    if (!boxId || uids.length === 0) return;
    izmenitStroki(uids, izm);
    try {
      await api.post(`/mail/boxes/${boxId}/messages/flags`, { folder, uids, ...izm });
      obnovitSchyotchiki();
    } catch (e) {
      toastError(e);
      obnovitSpisok();
    }
  };

  const peremestit = async (uids: number[], kuda: string) => {
    if (!boxId || uids.length === 0) return;
    if (!deystvieGuard.take()) return;
    try {
      await api.post(`/mail/boxes/${boxId}/messages/move`, { folder, uids, to: kuda });
      ubratStroki(uids);
      const tuda = papkiYashchika?.find((f) => f.path === kuda);
      toast(t("wmMoved", { n: uids.length, folder: tuda ? nazvaniePapki(tuda, t) : kuda }));
      if (uid && uids.includes(uid)) zakrytPismo();
      obnovitSchyotchiki();
    } catch (e) {
      toastError(e);
    } finally {
      deystvieGuard.free();
    }
  };

  const udalit = async (uids: number[]) => {
    if (!boxId || uids.length === 0) return;
    if (!deystvieGuard.take()) return;
    try {
      await api.post(`/mail/boxes/${boxId}/messages/delete`, { folder, uids });
      ubratStroki(uids);
      toast(tekPapka?.special === "trash" ? t("wmDeletedForever", { n: uids.length }) : t("wmDeleted", { n: uids.length }));
      if (uid && uids.includes(uid)) zakrytPismo();
      obnovitSchyotchiki();
    } catch (e) {
      toastError(e);
    } finally {
      deystvieGuard.free();
    }
  };

  /** Из корзины — насовсем, и об этом спрашиваем; из остальных папок — в корзину без вопросов. */
  const prosimUdalit = (uids: number[]) => {
    if (tekPapka?.special === "trash") setUdalitNavsegda(uids);
    else void udalit(uids);
  };

  const pereklyuchitGalku = (u: number, sDiapazonom: boolean) => {
    // Начало диапазона — до setVybrano: функция-обновитель выполнится позже,
    // когда отметка уже переедет на эту строку, и диапазон схлопнулся бы в одну.
    const ot = poslednyayaGalka.current;
    setVybrano((bylo) => {
      const novoe = new Set(bylo);
      const vklyuchit = !novoe.has(u);
      if (sDiapazonom && ot !== null && stroki) {
        // Shift — диапазон от прошлой галки, как в любой почте.
        const a = stroki.findIndex((s) => s.uid === ot);
        const b = stroki.findIndex((s) => s.uid === u);
        if (a >= 0 && b >= 0) {
          for (const s of stroki.slice(Math.min(a, b), Math.max(a, b) + 1)) {
            if (vklyuchit) novoe.add(s.uid);
            else novoe.delete(s.uid);
          }
          return novoe;
        }
      }
      if (vklyuchit) novoe.add(u);
      else novoe.delete(u);
      return novoe;
    });
    poslednyayaGalka.current = u;
  };

  const vseVybrany = !!stroki && stroki.length > 0 && stroki.every((s) => vybrano.has(s.uid));
  const chastichno = vybrano.size > 0 && !vseVybrany;

  // --- папки: завести, переименовать, удалить ---
  const [papkaOkno, setPapkaOkno] = useState<{ staraya: WmFolder | null; imya: string } | null>(null);
  const [papkaUdalit, setPapkaUdalit] = useState<WmFolder | null>(null);

  const sokhranitPapku = async () => {
    if (!boxId || !papkaOkno || !papkaOkno.imya.trim()) return;
    if (!papkaGuard.take()) return;
    try {
      if (papkaOkno.staraya) {
        await api.patch(`/mail/boxes/${boxId}/folders`, { path: papkaOkno.staraya.path, new_name: papkaOkno.imya.trim() });
        if (papkaOkno.staraya.path === folder) otkrytPapku(boxId, "INBOX");
      } else {
        await api.post(`/mail/boxes/${boxId}/folders`, { name: papkaOkno.imya.trim() });
        toast(t("wmFolderCreated"));
      }
      setPapkaOkno(null);
      zagruzitPapki(boxId);
    } catch (e) {
      toastError(e);
    } finally {
      papkaGuard.free();
    }
  };

  const udalitPapku = async (f: WmFolder) => {
    if (!boxId) return;
    try {
      await api.del(`/mail/boxes/${boxId}/folders?path=${encodeURIComponent(f.path)}`);
      if (f.path === folder) otkrytPapku(boxId, "INBOX");
      zagruzitPapki(boxId);
    } catch (e) {
      toastError(e);
    }
  };

  // --- письмо новое ---
  const [pishem, setPishem] = useState<{ klyuch: number; nachalo: Nachalo } | null>(null);

  const nachatPismo = (nachalo: Partial<Nachalo>) => {
    const ot = nachalo.boxId ?? (box?.can_send ? box.id : otpraviteli[0]?.id ?? null);
    setPishem({
      klyuch: Date.now(),
      nachalo: { to: [], cc: [], bcc: [], subject: "", html: "", imena: {}, ...nachalo, boxId: ot },
    });
  };

  const otvetit = (p: WmPismo, kak: "reply" | "all" | "forward") => {
    // «Свой» — только ящик, в котором письмо читают. Коллега из соседнего
    // общего ящика — такой же собеседник: ответ ему не должен уйти себе.
    const svoi = new Set(box ? [box.address.toLowerCase()] : []);
    const ot = box?.can_send ? box.id : otpraviteli[0]?.id ?? null;
    const imena: Record<string, string> = {};
    const zapomnit = (a: WmAdres) => {
      if (a.name) imena[a.email.toLowerCase()] = a.name;
      return a.email;
    };
    const tekst = p.text || tekstIzHtml(p.html);
    const vlozheniyaEst = p.attachments.some((v) => !v.inline);
    if (kak === "forward") {
      const shapka = [
        `---------- ${t("wmForwarded")} ----------`,
        `${t("wmFromLabel")}: ${kakAdres(p.from)}`,
        `${t("wmDate")}: ${polnayaData(p.date, locale)}`,
        `${t("subjectField")}: ${p.subject}`,
        `${t("toField")}: ${p.to.map(kakAdres).join(", ")}`,
      ];
      nachatPismo({
        boxId: ot,
        subject: sPristavkoy("Fwd", p.subject),
        html: `<p><br></p><p>${shapka.map(ekran).join("<br>")}</p><p>${tekstVHtml(tekst)}</p>`,
        forward_of: { folder, uid: p.uid },
        client_id: p.client?.id ?? null,
        zametka: vlozheniyaEst ? t("wmForwardFiles") : undefined,
      });
      return;
    }
    const otpravitel = p.reply_to.length ? p.reply_to : p.from ? [p.from] : [];
    // Ответ на своё же письмо (из «Отправленных») уходит его адресатам, а не себе.
    const svoyo = otpravitel.length > 0 && otpravitel.every((a) => svoi.has(a.email.toLowerCase()));
    const to = unikalnye((svoyo ? p.to : otpravitel).map(zapomnit));
    const uzhe = new Set(to.map((a) => a.toLowerCase()));
    const cc =
      kak === "all"
        ? unikalnye([...p.to, ...p.cc].map(zapomnit)).filter(
            (a) => !svoi.has(a.toLowerCase()) && !uzhe.has(a.toLowerCase()),
          )
        : [];
    const kto = kakAdres(p.from);
    nachatPismo({
      boxId: ot,
      to,
      cc,
      imena,
      subject: sPristavkoy("Re", p.subject),
      html:
        `<p><br></p><p>${ekran(t("wmWrote", { date: polnayaData(p.date, locale), who: kto }))}</p>` +
        `<blockquote>${tekstVHtml(tekst)}</blockquote>`,
      reply_to: { folder, uid: p.uid },
      client_id: p.client?.id ?? null,
    });
  };

  /** Черновик открывается сразу в окне «Написать» — читать его незачем. */
  const prodolzhitChernovik = (p: WmPismo) => {
    if (!boxId) return;
    const imena: Record<string, string> = {};
    for (const a of [...p.to, ...p.cc]) if (a.name) imena[a.email.toLowerCase()] = a.name;
    nachatPismo({
      boxId,
      to: p.to.map((a) => a.email),
      cc: p.cc.map((a) => a.email),
      imena,
      subject: p.subject,
      html: p.html ? dlyaRedaktora(p.html) : tekstVHtml(p.text),
      draft: { box: boxId, uid: p.uid, folder },
      zametka: p.attachments.some((v) => !v.inline) ? t("wmDraftFilesLost") : undefined,
    });
  };

  const otkrytStroku = async (s: WmStroka) => {
    if (tekPapka?.special !== "drafts" || !mozhnoPisat || !boxId) {
      otkrytPismo(s.uid);
      return;
    }
    try {
      const raw = await api.get(`/mail/boxes/${boxId}/messages/${s.uid}?folder=${encodeURIComponent(folder)}`);
      prodolzhitChernovik(normPismo(raw));
    } catch (e) {
      toastError(e);
    }
  };

  if (!boxes) return <ScreenLoading error={failure} onRetry={() => setPopytka((n) => n + 1)} />;

  if (boxes.length === 0) {
    return (
      <div className="page">
        <EmptyState
          icon="mail"
          title={t("wmNoBoxes")}
          sub={upravlyaet ? t("wmNoBoxesManage") : t("wmNoBoxesAsk")}
          action={
            upravlyaet ? (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "center" }}>
                <Link className="btn btn-primary" to="/settings/mail-server">
                  {t("wmSetUpServer")}
                </Link>
                <Link className="btn btn-secondary" to="/settings/mailboxes">
                  {t("wmConnectBox")}
                </Link>
              </div>
            ) : undefined
          }
        />
      </div>
    );
  }

  const klass =
    "wm-screen" + ((arhiv ? aid : uid) ? " is-reading" : "") + (bokovaya ? " is-side" : "");
  const nazvanieTekushchey = tekPapka ? nazvaniePapki(tekPapka, t) : folder.toUpperCase() === "INBOX" ? t("wmInbox") : folder;
  const vybranoSpisok = [...vybrano];
  const vneshnieKartinki = !!pismo?.html && pismo.remote_images;
  const vlozheniya = pismo?.attachments.filter((v) => !v.inline) ?? [];
  const adresVlozheniya = (v: WmVlozhenie) =>
    `/api/v1/mail/boxes/${boxId}/messages/${uid}/attachments/${encodeURIComponent(v.part)}?folder=${encodeURIComponent(folder)}`;

  return (
    <div className={klass}>
      {bokovaya && <div className="wm-veil" aria-hidden="true" onClick={() => setBokovaya(false)} />}

      <aside className="wm-side" aria-label={t("wmBoxes")}>
        {mozhnoPisat && (
          <button type="button" className="btn btn-primary wm-write" onClick={() => nachatPismo({})}>
            <Icon name="note" size={14} stroke={2} />
            {t("compose")}
          </button>
        )}
        <nav className="wm-boxes">
          {boxes.map((b) => (
            <div key={b.id}>
              <button
                type="button"
                className={"wm-box" + (b.id === boxId ? " is-active" : "")}
                title={b.address}
                onClick={() => otkrytPapku(b.id, "INBOX")}
              >
                <Avatar small text={initials(b.title || b.address)} />
                <span className="wm-box-name">
                  <span>{b.title || b.address}</span>
                  {b.title && <small>{b.address}</small>}
                </span>
                {b.unseen > 0 && <span className="wm-count">{b.unseen}</span>}
              </button>
              {b.id === boxId && (
                <>
                  {papkiBeda?.box === b.id && !papkiYashchika && (
                    <LoadFailed error={papkiBeda.e} onRetry={() => zagruzitPapki(b.id)} />
                  )}
                  {papkiYashchika && (
                    <ul className="wm-folders">
                      {papkiYashchika.map((f) => (
                        <li key={f.path}>
                          <button
                            type="button"
                            className={"wm-folder" + (f.path === folder && !arhiv ? " is-active" : "")}
                            style={{ paddingLeft: 8 + glubina(f) * 12 }}
                            onClick={() => otkrytPapku(b.id, f.path)}
                          >
                            <Icon name={znachokPapki(f)} size={14} />
                            <span className="wm-folder-name">{nazvaniePapki(f, t)}</span>
                            {f.special === "drafts"
                              ? f.total > 0 && <span className="wm-folder-num">{f.total}</span>
                              : f.unseen > 0 && <span className="wm-count">{f.unseen}</span>}
                          </button>
                          {!sluzhebnaya(f) && (
                            <span className="wm-folder-tools">
                              <button
                                type="button"
                                className="wm-icon-btn wm-icon-sm"
                                aria-label={t("wmFolderRename")}
                                title={t("wmFolderRename")}
                                onClick={() => setPapkaOkno({ staraya: f, imya: f.name })}
                              >
                                <Icon name="note" size={12} />
                              </button>
                              <button
                                type="button"
                                className="wm-icon-btn wm-icon-sm"
                                aria-label={t("wmFolderDelete")}
                                title={t("wmFolderDelete")}
                                onClick={() => setPapkaUdalit(f)}
                              >
                                <Icon name="trash" size={12} />
                              </button>
                            </span>
                          )}
                        </li>
                      ))}
                      <li>
                        <button
                          type="button"
                          className="wm-folder wm-folder-new"
                          onClick={() => setPapkaOkno({ staraya: null, imya: "" })}
                        >
                          <Icon name="plus" size={13} />
                          <span className="wm-folder-name">{t("wmFolderNew")}</span>
                        </button>
                      </li>
                    </ul>
                  )}
                  {/* Вне списка папок: папки идут с сервера, а архив — из базы, и
                      он нужен как раз тогда, когда сервер молчит. */}
                  <ul className="wm-folders">
                    <li>
                      <button
                        type="button"
                        className={"wm-folder" + (arhiv ? " is-active" : "")}
                        onClick={() => otkrytArhiv(b.id)}
                      >
                        <Icon name="database" size={14} />
                        <span className="wm-folder-name">{t("wmCrmArchive")}</span>
                      </button>
                    </li>
                  </ul>
                </>
              )}
            </div>
          ))}
        </nav>
        {upravlyaet && (
          <Link className="wm-side-link" to="/settings/mailboxes">
            <Icon name="settings" size={13} />
            {t("wmManageBoxes")}
          </Link>
        )}
      </aside>

      {arhiv && boxId ? (
        <ArhivCrm
          boxId={boxId}
          address={box?.address ?? ""}
          aid={aid}
          onSide={() => setBokovaya(true)}
          onOpen={otkrytArhivPismo}
          onClose={zakrytArhivPismo}
        />
      ) : (
      <>
      <section className="wm-list" aria-label={nazvanieTekushchey}>
        <header className="wm-list-head">
          <button
            type="button"
            className="wm-icon-btn wm-menu-btn"
            aria-label={t("wmBoxes")}
            title={t("wmBoxes")}
            onClick={() => setBokovaya(true)}
          >
            {/* Не «гамбургер»: на телефоне он уже стоит выше — меню CRM. */}
            <Icon name="inbox" size={16} />
          </button>
          <div className="wm-list-title">
            <div className="truncate">{nazvanieTekushchey}</div>
            <small className="truncate">{box?.address}</small>
          </div>
          {mozhnoPisat && (
            <button
              type="button"
              className="wm-icon-btn wm-write-sm"
              aria-label={t("compose")}
              title={t("compose")}
              onClick={() => nachatPismo({})}
            >
              <Icon name="note" size={16} />
            </button>
          )}
          <button
            type="button"
            className="wm-icon-btn"
            aria-label={t("wmRefresh")}
            title={t("wmRefresh")}
            onClick={() => {
              setPerezapros((n) => n + 1);
              obnovitSchyotchiki();
            }}
          >
            <Icon name="refresh" size={15} />
          </button>
        </header>

        <div className="wm-search">
          <Icon name="search" size={14} />
          <input
            type="search"
            value={poisk}
            placeholder={t("searchMail")}
            aria-label={t("search")}
            onChange={(e) => setPoisk(e.target.value)}
          />
          {poisk && (
            <button type="button" className="wm-icon-btn wm-icon-sm" aria-label={t("wmClearSearch")} onClick={() => setPoisk("")}>
              <Icon name="x" size={12} />
            </button>
          )}
        </div>

        <div className="wm-filters">
          <span className="wm-check">
            <input
              type="checkbox"
              checked={vseVybrany}
              disabled={!stroki || stroki.length === 0}
              ref={(el) => {
                if (el) el.indeterminate = chastichno;
              }}
              aria-label={t("wmSelectAll")}
              onChange={() => setVybrano(vseVybrany ? new Set() : new Set((stroki ?? []).map((s) => s.uid)))}
            />
          </span>
          {vybrano.size === 0 ? (
            <>
              <button
                type="button"
                className={"filter-chip" + (neprochitannye ? " active" : "")}
                aria-pressed={neprochitannye}
                onClick={() => setNeprochitannye((v) => !v)}
              >
                {t("unreadOnly")}
              </button>
              <button
                type="button"
                className={"filter-chip" + (pomechennye ? " active" : "")}
                aria-pressed={pomechennye}
                onClick={() => setPomechennye((v) => !v)}
              >
                {t("wmFlagged")}
              </button>
              {spisok && <span className="wm-total">{t("mailSub", { total: spisok.vsego })}</span>}
            </>
          ) : (
            <div className="wm-bulk">
              <span className="wm-bulk-n">{t("wmSelected", { n: vybrano.size })}</span>
              <button
                type="button"
                className="wm-icon-btn"
                aria-label={t("wmMarkRead")}
                title={t("wmMarkRead")}
                onClick={() => void flagi(vybranoSpisok, { seen: true })}
              >
                <Icon name="mailOpen" size={15} />
              </button>
              <button
                type="button"
                className="wm-icon-btn"
                aria-label={t("wmMarkUnread")}
                title={t("wmMarkUnread")}
                onClick={() => void flagi(vybranoSpisok, { seen: false })}
              >
                <Icon name="mail" size={15} />
              </button>
              <button
                type="button"
                className="wm-icon-btn"
                aria-label={t("wmFlag")}
                title={t("wmFlag")}
                onClick={() => void flagi(vybranoSpisok, { flagged: true })}
              >
                <Icon name="star" size={15} />
              </button>
              <VPapku
                papki={papkiYashchika ?? []}
                tekushchaya={folder}
                disabled={deystvieGuard.busy}
                onPick={(kuda) => void peremestit(vybranoSpisok, kuda)}
              />
              <button
                type="button"
                className="wm-icon-btn"
                aria-label={t("delete")}
                title={t("delete")}
                disabled={deystvieGuard.busy}
                onClick={() => prosimUdalit(vybranoSpisok)}
              >
                <Icon name="trash" size={15} />
              </button>
            </div>
          )}
        </div>

        <div className={"wm-rows" + (gruzim && stroki ? " is-busy" : "")}>
          {spisokBeda && !stroki ? (
            <div className="wm-pad">
              <LoadFailed error={spisokBeda} onRetry={() => setPerezapros((n) => n + 1)} />
            </div>
          ) : !stroki ? (
            <div className="wm-read-empty">
              <Spinner />
            </div>
          ) : stroki.length === 0 ? (
            <EmptyState
              icon={typed.trim() ? "search" : "inbox"}
              title={typed.trim() ? t("nothingFound", { q: typed.trim() }) : t("wmFolderEmpty")}
            />
          ) : (
            <ul className="wm-row-list">
              {stroki.map((s) => {
                const kto = iskhodyashchaya
                  ? `${t("toField")}: ${s.to.map((a) => a.name || a.email).join(", ") || "—"}`
                  : s.from?.name || s.from?.email || "—";
                return (
                  <li
                    key={s.uid}
                    className={
                      "wm-row" +
                      (s.seen ? "" : " is-unread") +
                      (s.uid === uid ? " is-active" : "") +
                      (vybrano.has(s.uid) ? " is-picked" : "")
                    }
                  >
                    <input
                      type="checkbox"
                      className="wm-row-check"
                      checked={vybrano.has(s.uid)}
                      aria-label={t("wmSelectLetter", { subject: s.subject || t("noSubject") })}
                      onChange={(e) => pereklyuchitGalku(s.uid, (e.nativeEvent as MouseEvent).shiftKey === true)}
                    />
                    <button type="button" className="wm-row-open" onClick={() => void otkrytStroku(s)}>
                      <span className="wm-row-who truncate">{kto}</span>
                      <span className="wm-row-date">{kogdaKoroche(s.date, locale)}</span>
                      <span className="wm-row-subj truncate">{s.subject || t("noSubject")}</span>
                      <span className="wm-row-icons">
                        {s.answered && <Icon name="reply" size={12} />}
                        {s.has_attachments && <Icon name="paperclip" size={12} />}
                      </span>
                      {s.preview && <span className="wm-row-prev truncate">{s.preview}</span>}
                    </button>
                    <button
                      type="button"
                      className={"wm-star" + (s.flagged ? " on" : "")}
                      aria-label={s.flagged ? t("wmUnflag") : t("wmFlag")}
                      aria-pressed={s.flagged}
                      title={s.flagged ? t("wmUnflag") : t("wmFlag")}
                      onClick={() => void flagi([s.uid], { flagged: !s.flagged })}
                    >
                      <Icon name="star" size={14} fill={s.flagged ? "currentColor" : undefined} />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {stroki && spisok && (
            <div className="wm-pad">
              <Dochitat
                pokazano={stroki.length}
                vsego={spisok.vsego}
                zanyat={dochityvaem}
                onClick={() => void dochitat()}
              />
            </div>
          )}
        </div>
      </section>

      <section className="wm-read" aria-label={pismo?.subject || t("wmPickLetter")}>
        {!uid ? (
          <div className="wm-read-empty">
            <EmptyState icon="mail" title={t("wmPickLetter")} />
          </div>
        ) : !pismo ? (
          <>
            <header className="wm-read-bar">
              <button type="button" className="wm-icon-btn wm-back" aria-label={t("back")} onClick={zakrytPismo}>
                <Icon name="arrowLeft" size={16} />
              </button>
            </header>
            <div className="wm-read-empty">
              {pismoBeda ? (
                <LoadFailed error={pismoBeda} onRetry={() => setPismoPopytka((n) => n + 1)} />
              ) : (
                <Spinner />
              )}
            </div>
          </>
        ) : (
          <>
            <header className="wm-read-bar">
              <button type="button" className="wm-icon-btn wm-back" aria-label={t("back")} onClick={zakrytPismo}>
                <Icon name="arrowLeft" size={16} />
              </button>
              {mozhnoPisat && (
                <>
                  <button
                    type="button"
                    className="wm-icon-btn"
                    aria-label={t("reply")}
                    title={t("reply")}
                    onClick={() => otvetit(pismo, "reply")}
                  >
                    <Icon name="reply" size={16} />
                  </button>
                  <button
                    type="button"
                    className="wm-icon-btn"
                    aria-label={t("wmReplyAll")}
                    title={t("wmReplyAll")}
                    onClick={() => otvetit(pismo, "all")}
                  >
                    <Icon name="replyAll" size={16} />
                  </button>
                  <button
                    type="button"
                    className="wm-icon-btn"
                    aria-label={t("wmForward")}
                    title={t("wmForward")}
                    onClick={() => otvetit(pismo, "forward")}
                  >
                    <Icon name="forward" size={16} />
                  </button>
                </>
              )}
              <span className="wm-bar-gap" />
              <VPapku
                papki={papkiYashchika ?? []}
                tekushchaya={folder}
                disabled={deystvieGuard.busy}
                onPick={(kuda) => void peremestit([pismo.uid], kuda)}
              />
              <button
                type="button"
                className="wm-icon-btn"
                aria-label={t("wmMarkUnread")}
                title={t("wmMarkUnread")}
                onClick={() => {
                  void flagi([pismo.uid], { seen: false });
                  zakrytPismo();
                }}
              >
                <Icon name="mail" size={16} />
              </button>
              {(() => {
                const pomecheno =
                  stroki?.find((s) => s.uid === pismo.uid)?.flagged ?? pismo.flags.includes("\\Flagged");
                return (
                  <button
                    type="button"
                    className={"wm-icon-btn" + (pomecheno ? " is-on" : "")}
                    aria-label={pomecheno ? t("wmUnflag") : t("wmFlag")}
                    aria-pressed={pomecheno}
                    title={pomecheno ? t("wmUnflag") : t("wmFlag")}
                    onClick={() => {
                      setPismo((p) =>
                        p
                          ? {
                              ...p,
                              flags: pomecheno
                                ? p.flags.filter((f) => f !== "\\Flagged")
                                : [...p.flags, "\\Flagged"],
                            }
                          : p,
                      );
                      void flagi([pismo.uid], { flagged: !pomecheno });
                    }}
                  >
                    <Icon name="star" size={16} fill={pomecheno ? "currentColor" : undefined} />
                  </button>
                );
              })()}
              <a
                className="wm-icon-btn"
                href={`/api/v1/mail/boxes/${boxId}/messages/${pismo.uid}/raw?folder=${encodeURIComponent(folder)}`}
                download={`${pismo.uid}.eml`}
                aria-label={t("wmDownloadOriginal")}
                title={t("wmDownloadOriginal")}
              >
                <Icon name="download" size={16} />
              </a>
              <button
                type="button"
                className="wm-icon-btn"
                aria-label={t("delete")}
                title={t("delete")}
                disabled={deystvieGuard.busy}
                onClick={() => prosimUdalit([pismo.uid])}
              >
                <Icon name="trash" size={16} />
              </button>
            </header>

            <div className="wm-read-head">
              <h2 className="wm-subject">{pismo.subject || t("noSubject")}</h2>
              <div className="wm-meta">
                <Avatar text={initials(pismo.from?.name || pismo.from?.email || "?")} />
                <div className="wm-meta-who">
                  <div className="truncate">
                    <b>{pismo.from?.name || pismo.from?.email || "—"}</b>
                    {pismo.from?.name && <span className="wm-addr"> &lt;{pismo.from.email}&gt;</span>}
                  </div>
                  <div className="wm-meta-to">
                    {t("toField")}: {pismo.to.map(kakAdres).join(", ") || "—"}
                    {pismo.cc.length > 0 && (
                      <>
                        {" · "}
                        {t("wmCc")}: {pismo.cc.map(kakAdres).join(", ")}
                      </>
                    )}
                  </div>
                </div>
                <div className="wm-meta-date">{formatDateTime(pismo.date, locale)}</div>
              </div>
              {(pismo.client || (tekPapka?.special === "drafts" && mozhnoPisat)) && (
                <div className="wm-tags">
                  {pismo.client && (
                    <Link className="wm-client" to={`/clients/${pismo.client.id}`} title={t("openClient")}>
                      <Icon name="user" size={13} />
                      {pismo.client.name}
                    </Link>
                  )}
                  {tekPapka?.special === "drafts" && mozhnoPisat && (
                    <button type="button" className="btn btn-primary btn-sm" onClick={() => prodolzhitChernovik(pismo)}>
                      <Icon name="note" size={13} />
                      {t("wmDraftEdit")}
                    </button>
                  )}
                </div>
              )}
            </div>

            {vneshnieKartinki && !kartinki && (
              <div className="wm-images" role="status">
                <Icon name="image" size={14} />
                <span>{t("wmImagesHidden")}</span>
                <button type="button" className="text-link" onClick={() => setKartinki(true)}>
                  {t("wmShowImages")}
                </button>
              </div>
            )}

            {vlozheniya.length > 0 && (
              <ul className="wm-files" aria-label={t("wmAttachments", { n: vlozheniya.length })}>
                {vlozheniya.map((v) => (
                  <li key={v.part}>
                    <a className="wm-file" href={adresVlozheniya(v)} download={v.name || true} title={v.name}>
                      <Icon name="paperclip" size={13} />
                      <span className="truncate">{v.name || t("wmAttachment")}</span>
                      <small>{fileSize(v.size)}</small>
                    </a>
                  </li>
                ))}
              </ul>
            )}

            {pismo.html ? (
              // Без allow-scripts и allow-same-origin: письмо чужое, и ни скрипт,
              // ни доступ к нашим cookie ему не положены. Попапы — чтобы ссылки
              // открывались в новой вкладке, а не внутри рамки.
              <iframe
                key={`${pismo.uid}-${kartinki}`}
                className="wm-frame"
                title={pismo.subject || t("noSubject")}
                sandbox="allow-popups allow-popups-to-escape-sandbox"
                referrerPolicy="no-referrer"
                src={`/api/v1/mail/boxes/${boxId}/messages/${uid}/html?folder=${encodeURIComponent(folder)}&images=${kartinki ? 1 : 0}`}
              />
            ) : (
              <div className="wm-text">{sSsylkami(pismo.text || "")}</div>
            )}

            {mozhnoPisat && (
              <div className="wm-quick">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => otvetit(pismo, "reply")}>
                  <Icon name="reply" size={14} />
                  {t("reply")}
                </button>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => otvetit(pismo, "all")}>
                  <Icon name="replyAll" size={14} />
                  {t("wmReplyAll")}
                </button>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => otvetit(pismo, "forward")}>
                  <Icon name="forward" size={14} />
                  {t("wmForward")}
                </button>
              </div>
            )}
          </>
        )}
      </section>
      </>
      )}

      {pishem && (
        <PismoNovoe
          key={pishem.klyuch}
          nachalo={pishem.nachalo}
          otpraviteli={otpraviteli}
          papkaChernovikov={papkaChernovikov}
          onClose={() => setPishem(null)}
          onDone={() => {
            obnovitSchyotchiki();
            obnovitSpisok();
          }}
        />
      )}

      {papkaOkno && (
        <Modal
          title={papkaOkno.staraya ? t("wmFolderRename") : t("wmFolderNew")}
          onClose={() => setPapkaOkno(null)}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void sokhranitPapku();
            }}
          >
            <div className="field">
              <label className="label" htmlFor="wm-folder-name">
                {t("wmFolderName")}
              </label>
              <input
                id="wm-folder-name"
                className="input"
                autoFocus
                maxLength={100}
                value={papkaOkno.imya}
                onChange={(e) => setPapkaOkno({ ...papkaOkno, imya: e.target.value })}
              />
            </div>
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 16 }}>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => setPapkaOkno(null)}>
                {t("cancel")}
              </button>
              <button
                type="submit"
                className="btn btn-primary btn-sm"
                disabled={papkaGuard.busy || !papkaOkno.imya.trim()}
              >
                {t("save")}
              </button>
            </div>
          </form>
        </Modal>
      )}

      {papkaUdalit && (
        <ConfirmModal
          text={t("wmFolderDeleteConfirm", { name: papkaUdalit.name })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void udalitPapku(papkaUdalit)}
          onClose={() => setPapkaUdalit(null)}
        />
      )}

      {udalitNavsegda && (
        <ConfirmModal
          text={t("wmDeleteForeverConfirm", { n: udalitNavsegda.length })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void udalit(udalitNavsegda)}
          onClose={() => setUdalitNavsegda(null)}
        />
      )}
    </div>
  );
}

/** Выбор папки для переноса: выпадающий список, а не своё меню — он и с клавиатуры, и на телефоне родной. */
function VPapku({
  papki,
  tekushchaya,
  disabled,
  onPick,
}: {
  papki: WmFolder[];
  tekushchaya: string;
  disabled?: boolean;
  onPick: (path: string) => void;
}) {
  const { t } = useApp();
  const kuda = papki.filter((f) => f.path !== tekushchaya);
  if (kuda.length === 0) return null;
  return (
    <select
      className="input input-sm wm-move"
      value=""
      disabled={disabled}
      aria-label={t("wmMoveTo")}
      title={t("wmMoveTo")}
      onChange={(e) => {
        if (e.target.value) onPick(e.target.value);
      }}
    >
      <option value="">{t("wmMoveTo")}</option>
      {kuda.map((f) => (
        <option key={f.path} value={f.path}>
          {" ".repeat(glubina(f) * 2)}
          {nazvaniePapki(f, t)}
        </option>
      ))}
    </select>
  );
}

// =============================================================================
// Архив CRM: копия писем в базе (`/mail/archive`). Переживает удаление письма на
// сервере и гибель самого сервера; разбор — docs/bloki/32-pochtovyy-server.md.

interface ArhivStroka {
  id: number;
  folder: string;
  subject: string;
  from: WmAdres | null;
  to: WmAdres[];
  date: string | null;
  has_attachments: boolean;
  size: number;
  removed: boolean;
}

interface ArhivPismoT extends ArhivStroka {
  cc: WmAdres[];
  text: string;
  html: string;
  attachments: WmVlozhenie[];
  removed_at: string | null;
  /** Сырой файл лёг в storage; без места на диске строка есть, а файла нет. */
  has_raw: boolean;
}

/** Адреса архива: список, строка через запятую или JSON-строка списка. */
function adresaArhiva(x: unknown): WmAdres[] {
  if (typeof x === "string" && x.trim().startsWith("[")) {
    try {
      return adresa(JSON.parse(x));
    } catch {
      /* не JSON — разберём строкой */
    }
  }
  return adresa(x);
}

function vlozheniyaArhiva(x: unknown): WmVlozhenie[] {
  let o = x;
  if (typeof o === "string") {
    try {
      o = JSON.parse(o || "[]");
    } catch {
      return [];
    }
  }
  return Array.isArray(o) ? (o as WmVlozhenie[]) : [];
}

function normArhiv(raw: any): ArhivStroka {
  return {
    id: Number(raw?.id),
    folder: String(raw?.folder ?? ""),
    subject: String(raw?.subject ?? ""),
    from: adresaArhiva(raw?.from ?? raw?.from_addr)[0] ?? null,
    to: adresaArhiva(raw?.to ?? raw?.to_addrs),
    date: normDate(raw?.received_at ?? raw?.sent_at ?? raw?.date),
    has_attachments: !!raw?.has_attachments,
    size: Number(raw?.size ?? 0),
    removed: !!raw?.removed_at,
  };
}

function normArhivPismo(raw: any): ArhivPismoT {
  return {
    ...normArhiv(raw),
    cc: adresaArhiva(raw?.cc ?? raw?.cc_addrs),
    text: String(raw?.text ?? raw?.body_text ?? ""),
    html: String(raw?.html ?? raw?.body_html ?? ""),
    attachments: vlozheniyaArhiva(raw?.attachments),
    removed_at: typeof raw?.removed_at === "string" ? raw.removed_at : null,
    has_raw: raw?.has_raw !== false,
  };
}

/**
 * Документ рамки письма из архива. Своей страницы у архива нет, поэтому
 * `srcDoc` — со своей политикой внутри: рамка наследует политику CRM, где можно
 * грузить с нашего адреса, а письму положены только картинки `data:`.
 */
function ramkaArhiva(html: string): string {
  return (
    "<!doctype html><html><head><meta charset=\"utf-8\">" +
    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline'\">" +
    "<meta name=\"referrer\" content=\"no-referrer\"><base target=\"_blank\">" +
    // Тот же вид, что у страницы письма с сервера (STIL_PISMA): бумага рамки — `--paper`.
    "<style>html{color-scheme:light}body{margin:0;padding:18px 20px;" +
    "font:14px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif;overflow-wrap:anywhere}" +
    "img{max-width:100%;height:auto}table{max-width:100%}pre{white-space:pre-wrap}" +
    "blockquote{margin:0 0 0 4px;padding-left:12px;border-left:2px solid}</style></head><body>" +
    html +
    "</body></html>"
  );
}

function ArhivCrm({
  boxId,
  address,
  aid,
  onSide,
  onOpen,
  onClose,
}: {
  boxId: number;
  address: string;
  aid: number | null;
  onSide: () => void;
  onOpen: (id: number) => void;
  onClose: () => void;
}) {
  const { t, locale, toastError } = useApp();
  const [poisk, setPoisk] = useState("");
  const typed = useDebounced(poisk);
  const [udalyonnye, setUdalyonnye] = useState(false);
  const [spisok, setSpisok] = useState<{ otbor: string; stroki: ArhivStroka[]; vsego: number; stranitsa: number } | null>(
    null,
  );
  const [beda, setBeda] = useState<unknown>(null);
  const [perezapros, setPerezapros] = useState(0);
  const [dochityvaem, setDochityvaem] = useState(false);

  const otbor = useMemo(() => {
    const p = new URLSearchParams({ box: String(boxId), per_page: String(NA_STRANITSE) });
    if (typed.trim()) p.set("q", typed.trim());
    if (udalyonnye) p.set("removed", "1");
    return `/mail/archive?${p}`;
  }, [boxId, typed, udalyonnye]);

  useEffect(() => {
    let current = true;
    setBeda(null);
    api
      .get<WmStranitsa>(`${otbor}&page=1`)
      .then((d) => {
        if (current) setSpisok({ otbor, stroki: d.items.map(normArhiv), vsego: d.total, stranitsa: 1 });
      })
      .catch((e) => {
        if (current) setBeda(e);
      });
    return () => {
      current = false;
    };
  }, [otbor, perezapros]);

  // Наблюдатель дописал архив — первая страница свежеет, дочитанное не трогаем.
  useLiveTopic("mail", () => {
    if (spisok && spisok.stranitsa === 1) setPerezapros((n) => n + 1);
  });

  const dochitat = async () => {
    if (dochityvaem || !spisok) return;
    setDochityvaem(true);
    const sprosheno = spisok.otbor;
    try {
      const d = await api.get<WmStranitsa>(`${sprosheno}&page=${spisok.stranitsa + 1}`);
      setSpisok((s) => {
        if (!s || s.otbor !== sprosheno) return s;
        const est = new Set(s.stroki.map((x) => x.id));
        const dalshe = d.items.map(normArhiv).filter((x) => !est.has(x.id));
        return { ...s, stroki: [...s.stroki, ...dalshe], vsego: d.total, stranitsa: s.stranitsa + 1 };
      });
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  const stroki = spisok && spisok.otbor === otbor ? spisok.stroki : null;

  return (
    <>
      <section className="wm-list" aria-label={t("wmCrmArchive")}>
        <header className="wm-list-head">
          <button
            type="button"
            className="wm-icon-btn wm-menu-btn"
            aria-label={t("wmBoxes")}
            title={t("wmBoxes")}
            onClick={onSide}
          >
            <Icon name="inbox" size={16} />
          </button>
          <div className="wm-list-title">
            <div className="truncate">{t("wmCrmArchive")}</div>
            <small className="truncate">{address}</small>
          </div>
          <button
            type="button"
            className="wm-icon-btn"
            aria-label={t("wmRefresh")}
            title={t("wmRefresh")}
            onClick={() => setPerezapros((n) => n + 1)}
          >
            <Icon name="refresh" size={15} />
          </button>
        </header>

        <div className="wm-search">
          <Icon name="search" size={14} />
          <input
            type="search"
            value={poisk}
            placeholder={t("wmArchiveSearch")}
            aria-label={t("search")}
            onChange={(e) => setPoisk(e.target.value)}
          />
          {poisk && (
            <button type="button" className="wm-icon-btn wm-icon-sm" aria-label={t("wmClearSearch")} onClick={() => setPoisk("")}>
              <Icon name="x" size={12} />
            </button>
          )}
        </div>

        <div className="wm-filters">
          <button
            type="button"
            className={"filter-chip" + (udalyonnye ? " active" : "")}
            aria-pressed={udalyonnye}
            onClick={() => setUdalyonnye((v) => !v)}
          >
            {t("wmArchiveRemovedOnly")}
          </button>
          {spisok && <span className="wm-total">{t("mailSub", { total: spisok.vsego })}</span>}
        </div>

        <div className={"wm-rows" + (!stroki && spisok ? " is-busy" : "")}>
          {beda && !stroki ? (
            <div className="wm-pad">
              <LoadFailed error={beda} onRetry={() => setPerezapros((n) => n + 1)} />
            </div>
          ) : !stroki ? (
            <div className="wm-read-empty">
              <Spinner />
            </div>
          ) : stroki.length === 0 ? (
            <EmptyState
              icon={typed.trim() ? "search" : "database"}
              title={typed.trim() ? t("nothingFound", { q: typed.trim() }) : t("wmArchiveEmpty")}
              sub={typed.trim() ? undefined : t("wmArchiveEmptyHint")}
            />
          ) : (
            <ul className="wm-row-list">
              {stroki.map((s) => (
                <li key={s.id} className={"wm-row" + (s.id === aid ? " is-active" : "")}>
                  <button type="button" className="wm-row-open" onClick={() => onOpen(s.id)}>
                    <span className="wm-row-who truncate">{s.from?.name || s.from?.email || "—"}</span>
                    <span className="wm-row-date">{kogdaKoroche(s.date, locale)}</span>
                    <span className="wm-row-subj truncate">{s.subject || t("noSubject")}</span>
                    <span className="wm-row-icons">
                      {s.has_attachments && <Icon name="paperclip" size={12} />}
                    </span>
                    <span className="wm-row-prev truncate">
                      {s.folder}
                      {s.removed && <span className="wm-removed"> · {t("wmArchiveRemoved")}</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {stroki && spisok && (
            <div className="wm-pad">
              <Dochitat pokazano={stroki.length} vsego={spisok.vsego} zanyat={dochityvaem} onClick={() => void dochitat()} />
            </div>
          )}
        </div>
      </section>

      <ArhivPismo aid={aid} onClose={onClose} />
    </>
  );
}

function ArhivPismo({ aid, onClose }: { aid: number | null; onClose: () => void }) {
  const { t, locale } = useApp();
  const [pismo, setPismo] = useState<ArhivPismoT | null>(null);
  const [beda, setBeda] = useState<unknown>(null);
  const [popytka, setPopytka] = useState(0);

  useEffect(() => {
    setPismo(null);
    setBeda(null);
    if (!aid) return;
    let current = true;
    api
      .get(`/mail/archive/${aid}`)
      .then((raw) => {
        if (current) setPismo(normArhivPismo(raw));
      })
      .catch((e) => {
        if (current) setBeda(e);
      });
    return () => {
      current = false;
    };
  }, [aid, popytka]);

  if (!aid) {
    return (
      <section className="wm-read" aria-label={t("wmPickLetter")}>
        <div className="wm-read-empty">
          <EmptyState icon="database" title={t("wmPickLetter")} sub={t("wmArchiveHint")} />
        </div>
      </section>
    );
  }

  const nazad = (
    <button type="button" className="wm-icon-btn wm-back" aria-label={t("back")} onClick={onClose}>
      <Icon name="arrowLeft" size={16} />
    </button>
  );

  if (!pismo) {
    return (
      <section className="wm-read" aria-label={t("wmCrmArchive")}>
        <header className="wm-read-bar">{nazad}</header>
        <div className="wm-read-empty">
          {beda ? <LoadFailed error={beda} onRetry={() => setPopytka((n) => n + 1)} /> : <Spinner />}
        </div>
      </section>
    );
  }

  const vlozheniya = pismo.attachments.filter((v) => !v.inline);
  return (
    <section className="wm-read" aria-label={pismo.subject || t("noSubject")}>
      <header className="wm-read-bar">
        {nazad}
        <span className="wm-bar-gap" />
        {pismo.has_raw && (
          <a
            className="wm-icon-btn"
            href={`/api/v1/mail/archive/${pismo.id}/raw`}
            download={`${pismo.id}.eml`}
            aria-label={t("wmDownloadOriginal")}
            title={t("wmDownloadOriginal")}
          >
            <Icon name="download" size={16} />
          </a>
        )}
      </header>

      <div className="wm-read-head">
        <h2 className="wm-subject">{pismo.subject || t("noSubject")}</h2>
        <div className="wm-meta">
          <Avatar text={initials(pismo.from?.name || pismo.from?.email || "?")} />
          <div className="wm-meta-who">
            <div className="truncate">
              <b>{pismo.from?.name || pismo.from?.email || "—"}</b>
              {pismo.from?.name && <span className="wm-addr"> &lt;{pismo.from.email}&gt;</span>}
            </div>
            <div className="wm-meta-to">
              {t("toField")}: {pismo.to.map(kakAdres).join(", ") || "—"}
              {pismo.cc.length > 0 && (
                <>
                  {" · "}
                  {t("wmCc")}: {pismo.cc.map(kakAdres).join(", ")}
                </>
              )}
            </div>
          </div>
          <div className="wm-meta-date">{formatDateTime(pismo.date, locale)}</div>
        </div>
        <div className="wm-tags">
          <span className="wm-client">
            <Icon name="folder" size={13} />
            {pismo.folder}
          </span>
        </div>
      </div>

      {pismo.removed_at && (
        <div className="wm-images" role="status">
          <Icon name="alert" size={14} />
          <span>{t("wmArchiveRemovedAt", { t: formatDateTime(pismo.removed_at, locale) })}</span>
        </div>
      )}

      {vlozheniya.length > 0 && (
        <ul className="wm-files" aria-label={t("wmAttachments", { n: vlozheniya.length })}>
          {vlozheniya.map((v) => (
            <li key={v.part}>
              <a
                className="wm-file"
                href={`/api/v1/mail/archive/${pismo.id}/attachments/${encodeURIComponent(v.part)}`}
                download={v.name || true}
                title={v.name}
              >
                <Icon name="paperclip" size={13} />
                <span className="truncate">{v.name || t("wmAttachment")}</span>
                <small>{fileSize(v.size)}</small>
              </a>
            </li>
          ))}
        </ul>
      )}

      {pismo.html ? (
        // Без allow-scripts и allow-same-origin, как у письма с сервера; политика
        // — внутри документа рамки (ramkaArhiva): картинки только data:.
        <iframe
          key={pismo.id}
          className="wm-frame"
          title={pismo.subject || t("noSubject")}
          sandbox="allow-popups allow-popups-to-escape-sandbox"
          referrerPolicy="no-referrer"
          srcDoc={ramkaArhiva(pismo.html)}
        />
      ) : (
        <div className="wm-text">{sSsylkami(pismo.text || "")}</div>
      )}
    </section>
  );
}

// =============================================================================
// Окно «Написать»

interface Vlozh {
  klyuch: string;
  imya: string;
  razmer: number;
  id: string | number | null;
  ushlo: number;
  zalivka: Zalivka<unknown> | null;
}

function PismoNovoe({
  nachalo,
  otpraviteli,
  papkaChernovikov,
  onClose,
  onDone,
}: {
  nachalo: Nachalo;
  otpraviteli: WmBox[];
  papkaChernovikov: (box: number) => string | null;
  onClose: () => void;
  /** Письмо ушло или черновик лёг в папку — счётчикам и списку пора перечитаться. */
  onDone: () => void;
}) {
  const { t, locale, toast, toastError, user, modules } = useApp();
  const iskatKlientov = moduleOn(modules, "clients") && can(user, "clients.view");
  const [ot, setOt] = useState<number | null>(
    otpraviteli.some((b) => b.id === nachalo.boxId) ? nachalo.boxId : otpraviteli[0]?.id ?? null,
  );
  const [komu, setKomu] = useState<string[]>(nachalo.to);
  const [kopiya, setKopiya] = useState<string[]>(nachalo.cc);
  const [skrytaya, setSkrytaya] = useState<string[]>(nachalo.bcc);
  const [kopii, setKopii] = useState(nachalo.cc.length + nachalo.bcc.length > 0);
  const [tema, setTema] = useState(nachalo.subject);
  const [html, setHtml] = useState(nachalo.html);
  const [vlozheniya, setVlozheniya] = useState<Vlozh[]>([]);
  const [vid, setVid] = useState<"obychnyy" | "svyornut" | "bolshoy">("obychnyy");
  const [sokhraneno, setSokhraneno] = useState<Date | null>(null);
  const [chernovikBeda, setChernovikBeda] = useState(false);
  const [ssylka, setSsylka] = useState<string | null>(null);
  const [nadFaylami, setNadFaylami] = useState(false);
  const [stertSprosit, setStertSprosit] = useState(false);
  const [imena, setImena] = useState<Record<string, string>>(nachalo.imena);

  const redaktor = useRef<HTMLDivElement>(null);
  const koren = useRef<HTMLDivElement>(null);
  const faylVvod = useRef<HTMLInputElement>(null);
  const vydelenie = useRef<Range | null>(null);
  const chernovik = useRef<Chernovik | null>(nachalo.draft ?? null);
  const ochered = useRef(0);
  /** Идущее сохранение черновика: отправка ждёт его, иначе снимет не тот черновик. */
  const idushchee = useRef<Promise<unknown> | null>(null);
  const zakryto = useRef<"net" | "otpravleno" | "steryto">("net");
  const guard = useGuard();
  const chernovikGuard = useGuard();
  const stiratGuard = useGuard();
  // Курсор в поле или поле отличается от начального — для вопроса браузера при
  // уходе со страницы. Тело письма (contenteditable) считаем сами — снимком.
  const nachata = useNachatayaPravka(koren, nachalo);

  useEffect(() => {
    if (redaktor.current) redaktor.current.innerHTML = nachalo.html;
    // Новое письмо начинают с адреса, ответ — с текста над цитатой.
    if (nachalo.to.length === 0) {
      koren.current?.querySelector<HTMLInputElement>(".wm-addr-field input")?.focus();
    } else if (redaktor.current) {
      redaktor.current.focus();
      const r = document.createRange();
      r.setStart(redaktor.current, 0);
      r.collapse(true);
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(r);
    }
  }, [nachalo]);

  const gotovye = vlozheniya.filter((v) => v.id !== null).map((v) => v.id as string | number);
  const idut = vlozheniya.some((v) => v.id === null);
  const snimok = JSON.stringify([ot, komu, kopiya, skrytaya, tema, html, gotovye]);
  const pusto =
    komu.length + kopiya.length + skrytaya.length === 0 &&
    !tema.trim() &&
    !tekstIzHtml(html).trim() &&
    vlozheniya.length === 0;
  const sokhranyonnyy = useRef(snimok);
  const izmeneno = snimok !== sokhranyonnyy.current;

  const telo = () => ({
    to: komu,
    cc: kopiya,
    bcc: skrytaya,
    subject: tema,
    // Только разметка: текст сервер выводит из неё сам. Оба сразу — тело
    // дважды и вдвое ближе к пределу запроса в 1 МБ (body_too_large).
    ...(html.trim() ? { html } : { text: redaktor.current?.innerText ?? "" }),
    attachments: gotovye,
    reply_to: nachalo.reply_to ?? null,
    forward_of: nachalo.forward_of ?? null,
    client_id: nachalo.client_id ?? null,
  });

  // Самое свежее — для сохранения из таймера паузы и при закрытии окна, где
  // замыкание видело бы состояние своего рендера.
  const poslednee = useRef({ snimok, pusto, ot, idut, telo, nachata });
  poslednee.current = { snimok, pusto, ot, idut, telo, nachata };

  const stertChernovik = async (staryy: Chernovik) => {
    const papka = staryy.folder ?? papkaChernovikov(staryy.box);
    if (!papka) return;
    if (!stiratGuard.take()) return;
    try {
      await api.post(`/mail/boxes/${staryy.box}/messages/delete`, { folder: papka, uids: [staryy.uid] });
    } catch {
      /* старый черновик останется в папке — это не потеря письма */
    } finally {
      stiratGuard.free();
    }
  };

  /** «ok» — лежит в «Черновиках» (или сохранять нечего), «zanyato» — идёт
   *  прошлое сохранение и это встало за ним в очередь, «beda» — не вышло. */
  const sokhranitChernovik = async (): Promise<"ok" | "zanyato" | "beda"> => {
    const s = poslednee.current;
    // Письмо уходит или ушло: новый черновик лёг бы в папку копией отправленного.
    if (zakryto.current === "otpravleno") return "ok";
    if (s.snimok === sokhranyonnyy.current) return "ok";
    // Пустое письмо не сохраняем вовсе: черновик из ничего — мусор в папке.
    if (s.pusto) return "ok";
    if (s.ot === null || s.idut) return "beda";
    if (!chernovikGuard.take()) {
      ochered.current += 1;
      return "zanyato";
    }
    let otpustit: () => void = () => undefined;
    idushchee.current = new Promise<void>((r) => (otpustit = r));
    let vyshlo: "ok" | "beda" = "beda";
    try {
      const staryy = chernovik.current;
      const otvet = await api.post<{ uid: number; folder?: string }>(`/mail/boxes/${s.ot}/drafts`, {
        ...s.telo(),
        draft_uid: staryy && staryy.box === s.ot ? staryy.uid : null,
      });
      // Сменили ящик-отправитель — прошлый черновик лежит в чужих «Черновиках».
      if (staryy && staryy.box !== s.ot) void stertChernovik(staryy);
      chernovik.current = { box: s.ot, uid: otvet.uid, folder: otvet.folder ?? null };
      sokhranyonnyy.current = s.snimok;
      setSokhraneno(new Date());
      setChernovikBeda(false);
      onDone();
      vyshlo = "ok";
    } catch {
      setChernovikBeda(true);
    } finally {
      idushchee.current = null;
      otpustit();
      chernovikGuard.free();
    }
    if (ochered.current > 0) {
      ochered.current = 0;
      if (zakryto.current === "net" && poslednee.current.snimok !== sokhranyonnyy.current) {
        return sokhranitChernovik();
      }
    }
    return vyshlo;
  };
  const sokhranitRef = useRef(sokhranitChernovik);
  sokhranitRef.current = sokhranitChernovik;

  // Автосохранение: снимок отстаёт на паузу, и запись уходит не чаще её.
  const otlozhennyy = useDebounced(snimok, PAUZA_CHERNOVIKA);
  useEffect(() => {
    if (zakryto.current === "net") void sokhranitRef.current();
  }, [otlozhennyy]);

  // Ушли со страницы почты или окно заменили новым — набранное уходит в черновик.
  useEffect(
    () => () => {
      if (zakryto.current === "net") void sokhranitRef.current();
    },
    [],
  );

  useEffect(() => {
    const derzhat = (e: BeforeUnloadEvent) => {
      const s = poslednee.current;
      if (zakryto.current === "net" && !s.pusto && (s.snimok !== sokhranyonnyy.current || s.nachata)) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", derzhat);
    return () => window.removeEventListener("beforeunload", derzhat);
  }, []);

  const zakryt = async () => {
    // Письмо уходит: окно закроет сама отправка, а провал оставит его на месте.
    if (zakryto.current === "otpravleno") return;
    if (!pusto && izmeneno) {
      if (idut) {
        toast(t("wmUploading"), true);
        return;
      }
      const itog = await sokhranitChernovik();
      // Не сохранилось — окно остаётся: закрыть значило бы потерять письмо.
      // «Занято» — сохранение уже идёт, и свежий снимок встал за ним в очередь.
      if (itog === "beda") {
        toast(t("wmDraftNotSaved"), true);
        return;
      }
      toast(t("wmDraftSavedShort"));
    }
    zakryto.current = "steryto";
    onClose();
  };

  const vybrosit = async () => {
    zakryto.current = "steryto";
    vlozheniya.forEach((v) => v.zalivka?.otmenit());
    const staryy = chernovik.current;
    if (staryy) {
      await stertChernovik(staryy);
      onDone();
    }
    onClose();
  };

  const otpravit = async () => {
    if (ot === null) return;
    if (komu.length + kopiya.length + skrytaya.length === 0) {
      toast(t("wmNoRecipientsYet"), true);
      return;
    }
    const plokhie = [...komu, ...kopiya, ...skrytaya].filter((a) => !ADRES.test(a));
    if (plokhie.length) {
      toast(t("wmBadAddress", { list: plokhie.join(", ") }), true);
      return;
    }
    // Ctrl+Enter не уводит фокус из редактора — вставленные картинки уносим здесь.
    if (vynestiKartinki()) {
      prinyatRedaktor();
      toast(t("wmUploading"), true);
      return;
    }
    if (idut) {
      toast(t("wmUploading"), true);
      return;
    }
    if (!guard.take()) return;
    // С этой минуты автосохранение и уход со страницы черновик не пишут, а
    // идущее сохранение дождёмся: его uid — тот черновик, что снимет отправка.
    zakryto.current = "otpravleno";
    try {
      if (idushchee.current) await idushchee.current;
      const staryy = chernovik.current;
      const otvet = await api.post<OtvetOtpravki>(`/mail/boxes/${ot}/send`, {
        ...telo(),
        draft_uid: staryy && staryy.box === ot ? staryy.uid : null,
      });
      if (staryy && staryy.box !== ot) void stertChernovik(staryy);
      const refused = otvet?.refused ?? {};
      const otkazali = Array.isArray(refused) ? refused.map(String) : Object.keys(refused);
      if (otkazali.length) toast(t("wmRefused", { list: otkazali.join(", ") }), true);
      else toast(t("letterSent"));
      onDone();
      onClose();
    } catch (e) {
      zakryto.current = "net";
      toastError(e);
      guard.free();
    }
  };

  // --- редактор ---
  /** Картинки строкой `data:` из вставки — во вложения: в теле они раздувают
   *  запрос за предел в 1 МБ, и письмо не уходит и не сохраняется. */
  const vynestiKartinki = (): boolean => {
    const el = redaktor.current;
    if (!el) return false;
    const kartinki = Array.from(el.querySelectorAll("img")).filter((img) =>
      /^\s*data:/i.test(img.getAttribute("src") ?? ""),
    );
    if (kartinki.length === 0) return false;
    const fayly: File[] = [];
    for (const img of kartinki) {
      const fayl = faylIzDataUrl(img.getAttribute("src") ?? "", fayly.length + 1);
      img.remove();
      if (fayl) fayly.push(fayl);
    }
    if (fayly.length) dobavitFayly(fayly);
    return true;
  };

  const prinyatRedaktor = () => {
    vynestiKartinki();
    setHtml(redaktor.current?.innerHTML ?? "");
  };

  const komanda = (cmd: string, arg?: string) => {
    redaktor.current?.focus();
    document.execCommand(cmd, false, arg);
    prinyatRedaktor();
  };

  const nachatSsylku = () => {
    const sel = window.getSelection();
    vydelenie.current =
      sel && sel.rangeCount && redaktor.current?.contains(sel.anchorNode) ? sel.getRangeAt(0).cloneRange() : null;
    setSsylka("https://");
  };

  const vstavitSsylku = () => {
    let adres = (ssylka ?? "").trim();
    setSsylka(null);
    if (!adres || adres === "https://") return;
    if (!/^(https?:|mailto:)/i.test(adres)) adres = "https://" + adres;
    redaktor.current?.focus();
    const sel = window.getSelection();
    if (vydelenie.current && sel) {
      sel.removeAllRanges();
      sel.addRange(vydelenie.current);
    }
    if (!vydelenie.current || vydelenie.current.collapsed) {
      document.execCommand("insertHTML", false, `<a href="${ekran(adres)}">${ekran(adres)}</a>`);
    } else {
      document.execCommand("createLink", false, adres);
    }
    prinyatRedaktor();
  };

  // --- вложения ---
  const dobavitFayly = (fayly: FileList | File[]) => {
    let vsego = vlozheniya.reduce((s, v) => s + v.razmer, 0);
    for (const fayl of Array.from(fayly)) {
      if (fayl.size > PREDEL_FAYLA) {
        toast(t("wmFileTooBig", { name: fayl.name }), true);
        continue;
      }
      if (vsego + fayl.size > PREDEL_PISMA) {
        toast(t("wmLetterTooBig"), true);
        break;
      }
      vsego += fayl.size;
      const klyuch = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const zalivka = api.zagruzka<{ id: string | number; name: string; size: number }>(
        "/mail/boxes/uploads",
        fayl,
        (k) =>
          setVlozheniya((vse) =>
            vse.map((v) => (v.klyuch === klyuch ? { ...v, ushlo: k.vsego ? k.ushlo / k.vsego : 0 } : v)),
          ),
      );
      setVlozheniya((vse) => [
        ...vse,
        { klyuch, imya: fayl.name, razmer: fayl.size, id: null, ushlo: 0, zalivka: zalivka as Zalivka<unknown> },
      ]);
      zalivka.gotovo.then(
        (otvet) =>
          setVlozheniya((vse) =>
            vse.map((v) => (v.klyuch === klyuch ? { ...v, id: otvet.id, ushlo: 1, zalivka: null } : v)),
          ),
        (e) => {
          setVlozheniya((vse) => vse.filter((v) => v.klyuch !== klyuch));
          if ((e as { code?: string })?.code !== "canceled") toastError(e);
        },
      );
    }
  };

  const ubratVlozhenie = (v: Vlozh) => {
    v.zalivka?.otmenit();
    setVlozheniya((vse) => vse.filter((x) => x.klyuch !== v.klyuch));
  };

  const zagolovok = tema.trim() || t("wmNewLetter");
  const podpisSokhraneniya = chernovikBeda
    ? t("wmDraftNotSaved")
    : sokhraneno
      ? t("wmDraftSaved", {
          time: sokhraneno.toLocaleTimeString(locale === "ru" ? "ru-RU" : "en-US", { hour: "2-digit", minute: "2-digit" }),
        })
      : "";

  return (
    <div
      ref={koren}
      className={
        "wm-compose" +
        (vid === "svyornut" ? " is-min" : "") +
        (vid === "bolshoy" ? " is-max" : "") +
        (nadFaylami ? " is-drop" : "")
      }
      role="dialog"
      aria-label={zagolovok}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes("Files")) {
          e.preventDefault();
          setNadFaylami(true);
        }
      }}
      onDragLeave={(e) => {
        if (!koren.current?.contains(e.relatedTarget as Node | null)) setNadFaylami(false);
      }}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return;
        e.preventDefault();
        setNadFaylami(false);
        dobavitFayly(e.dataTransfer.files);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          void otpravit();
        }
      }}
    >
      <header className="wm-compose-head">
        <button
          type="button"
          className="wm-compose-title"
          onClick={() => setVid((v) => (v === "svyornut" ? "obychnyy" : "svyornut"))}
        >
          {zagolovok}
        </button>
        <button
          type="button"
          className="wm-icon-btn wm-icon-sm"
          aria-label={vid === "svyornut" ? t("wmExpand") : t("wmMinimize")}
          title={vid === "svyornut" ? t("wmExpand") : t("wmMinimize")}
          onClick={() => setVid((v) => (v === "svyornut" ? "obychnyy" : "svyornut"))}
        >
          <Icon name={vid === "svyornut" ? "chevronsUpDown" : "chevronDown"} size={14} />
        </button>
        <button
          type="button"
          className="wm-icon-btn wm-icon-sm wm-max-btn"
          aria-label={vid === "bolshoy" ? t("wmExitFullscreen") : t("wmFullscreen")}
          title={vid === "bolshoy" ? t("wmExitFullscreen") : t("wmFullscreen")}
          onClick={() => setVid((v) => (v === "bolshoy" ? "obychnyy" : "bolshoy"))}
        >
          <Icon name={vid === "bolshoy" ? "minimize" : "maximize"} size={13} />
        </button>
        <button
          type="button"
          className="wm-icon-btn wm-icon-sm"
          aria-label={t("close")}
          title={t("close")}
          onClick={() => void zakryt()}
        >
          <Icon name="x" size={14} />
        </button>
      </header>

      <div className="wm-compose-body">
        {nachalo.zametka && <div className="wm-note">{nachalo.zametka}</div>}
        {otpraviteli.length > 1 ? (
          <div className="wm-line">
            <label className="wm-line-label" htmlFor="wm-from">
              {t("fromMailbox")}
            </label>
            <select id="wm-from" value={ot ?? ""} onChange={(e) => setOt(Number(e.target.value))}>
              {otpraviteli.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.title ? `${b.title} <${b.address}>` : b.address}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <div className="wm-line">
            <span className="wm-line-label">{t("fromMailbox")}</span>
            <span className="wm-line-value truncate">{otpraviteli[0]?.address}</span>
          </div>
        )}
        <div className="wm-line">
          <PoleAdresov
            label={t("toField")}
            value={komu}
            onChange={setKomu}
            imena={imena}
            onImya={(email, imya) => setImena((v) => ({ ...v, [email.toLowerCase()]: imya }))}
            iskat={iskatKlientov}
          />
          {!kopii && (
            <button type="button" className="wm-cc-toggle" onClick={() => setKopii(true)}>
              {t("wmShowCc")}
            </button>
          )}
        </div>
        {kopii && (
          <>
            <div className="wm-line">
              <PoleAdresov
                label={t("wmCc")}
                value={kopiya}
                onChange={setKopiya}
                imena={imena}
                onImya={(email, imya) => setImena((v) => ({ ...v, [email.toLowerCase()]: imya }))}
                iskat={iskatKlientov}
              />
            </div>
            <div className="wm-line">
              <PoleAdresov
                label={t("wmBcc")}
                value={skrytaya}
                onChange={setSkrytaya}
                imena={imena}
                onImya={(email, imya) => setImena((v) => ({ ...v, [email.toLowerCase()]: imya }))}
                iskat={iskatKlientov}
              />
            </div>
          </>
        )}
        <div className="wm-line">
          <input
            className="wm-subj"
            value={tema}
            maxLength={300}
            placeholder={t("subjectField")}
            aria-label={t("subjectField")}
            onChange={(e) => setTema(e.target.value)}
          />
        </div>

        <div className="wm-tools" role="toolbar" aria-label={t("wmFormatting")}>
          {(
            [
              ["bold", "bold", "wmBold"],
              ["italic", "italic", "wmItalic"],
              ["insertUnorderedList", "list", "wmBullets"],
              ["insertOrderedList", "listNumbered", "wmNumbers"],
              ["removeFormat", "eraser", "wmClearFormat"],
            ] as const
          ).map(([cmd, ikonka, podpis]) => (
            <button
              key={cmd}
              type="button"
              className="wm-icon-btn wm-icon-sm"
              aria-label={t(podpis)}
              title={t(podpis)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => komanda(cmd)}
            >
              <Icon name={ikonka} size={14} />
            </button>
          ))}
          <button
            type="button"
            className="wm-icon-btn wm-icon-sm"
            aria-label={t("wmLink")}
            title={t("wmLink")}
            onMouseDown={(e) => e.preventDefault()}
            onClick={nachatSsylku}
          >
            <Icon name="link" size={14} />
          </button>
          <span className="wm-tools-sep" aria-hidden="true" />
          <button
            type="button"
            className="wm-icon-btn wm-icon-sm"
            aria-label={t("wmAttach")}
            title={t("wmAttach")}
            onClick={() => faylVvod.current?.click()}
          >
            <Icon name="paperclip" size={14} />
          </button>
          <input
            ref={faylVvod}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) dobavitFayly(e.target.files);
              e.target.value = "";
            }}
          />
        </div>

        {ssylka !== null && (
          <form
            className="wm-link-row"
            onSubmit={(e) => {
              e.preventDefault();
              vstavitSsylku();
            }}
          >
            <input
              className="input input-sm"
              autoFocus
              value={ssylka}
              aria-label={t("wmLink")}
              onChange={(e) => setSsylka(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.preventDefault();
                  setSsylka(null);
                }
              }}
            />
            <button type="submit" className="btn btn-secondary btn-sm">
              {t("wmLinkApply")}
            </button>
          </form>
        )}

        <div
          ref={redaktor}
          className="wm-editor"
          contentEditable
          suppressContentEditableWarning
          role="textbox"
          aria-multiline="true"
          aria-label={t("bodyField")}
          data-placeholder={t("bodyField")}
          onInput={prinyatRedaktor}
          onBlur={prinyatRedaktor}
          onPaste={(e) => {
            // Снимок экрана — файлом во вложения. Вставка из Word и таблиц несёт
            // и текст, и картинку-копию: её оставляем вставке, а data: уберёт prinyatRedaktor.
            const fayly = Array.from(e.clipboardData.files);
            const tekst = e.clipboardData.types.includes("text/plain") || e.clipboardData.types.includes("text/html");
            if (fayly.length && !tekst) {
              e.preventDefault();
              dobavitFayly(fayly);
            }
          }}
        />

        {vlozheniya.length > 0 && (
          <ul className="wm-uploads">
            {vlozheniya.map((v) => (
              <li key={v.klyuch} className="wm-upload">
                <Icon name="paperclip" size={13} />
                <span className="truncate">{v.imya}</span>
                <small>{v.id === null ? `${Math.round(v.ushlo * 100)}%` : fileSize(v.razmer)}</small>
                <button
                  type="button"
                  className="wm-icon-btn wm-icon-sm"
                  aria-label={v.id === null ? t("wmUploadCancel") : t("wmRemoveAttachment", { name: v.imya })}
                  title={v.id === null ? t("wmUploadCancel") : t("wmRemoveAttachment", { name: v.imya })}
                  onClick={() => ubratVlozhenie(v)}
                >
                  <Icon name="x" size={12} />
                </button>
                {v.id === null && (
                  <span className="wm-upload-bar" aria-hidden="true">
                    <span className="wm-upload-fill" style={{ width: `${Math.round(v.ushlo * 100)}%` }} />
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}

        <footer className="wm-compose-foot">
          <button
            type="button"
            className="btn btn-primary"
            disabled={guard.busy || ot === null}
            title={t("wmSendShortcut")}
            onClick={() => void otpravit()}
          >
            <Icon name="send" size={14} stroke={2} />
            {t("sendLetter")}
          </button>
          <span className={"wm-draft-state" + (chernovikBeda ? " is-bad" : "")} role="status">
            {podpisSokhraneniya}
          </span>
          <button
            type="button"
            className="wm-icon-btn"
            aria-label={t("wmDiscard")}
            title={t("wmDiscard")}
            onClick={() => (pusto && !chernovik.current ? void vybrosit() : setStertSprosit(true))}
          >
            <Icon name="trash" size={15} />
          </button>
        </footer>
      </div>

      {stertSprosit && (
        <ConfirmModal
          text={t("wmDiscardConfirm")}
          confirmLabel={t("wmDiscard")}
          danger
          onConfirm={() => void vybrosit()}
          onClose={() => setStertSprosit(false)}
        />
      )}
    </div>
  );
}

/**
 * Поле адресов: адреса — плашками, подсказки — из клиентов CRM.
 *
 * Адрес становится плашкой по запятой, точке с запятой, пробелу, Enter и уходу
 * из поля: письмо, у которого адрес остался недонабранным текстом, ушло бы без
 * этого адресата.
 */
function PoleAdresov({
  label,
  value,
  onChange,
  imena,
  onImya,
  iskat,
}: {
  label: string;
  value: string[];
  onChange: (v: string[]) => void;
  imena: Record<string, string>;
  onImya: (email: string, imya: string) => void;
  iskat: boolean;
}) {
  const { t } = useApp();
  const id = useId();
  const [stroka, setStroka] = useState("");
  const [podskazki, setPodskazki] = useState<WmAdres[]>([]);
  const [otkryto, setOtkryto] = useState(false);
  const [podsvechen, setPodsvechen] = useState(0);
  const zapros = useDebounced(stroka);

  useEffect(() => {
    const q = zapros.trim();
    if (!iskat || q.length < 2) {
      setPodskazki([]);
      return;
    }
    let alive = true;
    api
      .get<{ items: { id: number; name: string; email?: string | null }[] }>(
        `/clients?search=${encodeURIComponent(q)}&per_page=8`,
      )
      .then((otvet) => {
        if (!alive) return;
        setPodskazki(
          otvet.items.filter((k) => k.email && ADRES.test(k.email)).map((k) => ({ name: k.name, email: k.email! })),
        );
        setPodsvechen(0);
      })
      .catch(() => {
        /* подсказка не обязательна: адрес можно набрать руками */
      });
    return () => {
      alive = false;
    };
  }, [zapros, iskat]);

  const dobavit = (syroe: string[]) => {
    const novye = syroe
      .map((a) => a.trim())
      .filter(Boolean)
      .map((a) => {
        const r = razobratAdres(a);
        if (r.name) onImya(r.email, r.name);
        return r.email;
      });
    if (novye.length) onChange(unikalnye([...value, ...novye]));
  };

  // «Имя <адрес>» режется только запятой: пробел внутри имени — не граница.
  const zafiksirovat = () => {
    const s = stroka.trim();
    if (s) dobavit(s.includes("<") ? s.split(/[,;]+/) : s.split(/[,;\s]+/));
    setStroka("");
  };

  const vzyat = (a: WmAdres) => {
    onImya(a.email, a.name);
    onChange(unikalnye([...value, a.email]));
    setStroka("");
    setOtkryto(false);
  };

  const pokazany = otkryto && podskazki.length > 0;

  return (
    <div className="wm-addr-field">
      <label className="wm-line-label" htmlFor={id}>
        {label}
      </label>
      {value.map((a, i) => {
        const imya = imena[a.toLowerCase()];
        return (
          <span key={a} className={"wm-chip" + (ADRES.test(a) ? "" : " is-bad")} title={a}>
            <span className="truncate">{imya || a}</span>
            <button
              type="button"
              aria-label={t("wmRemoveAddress", { address: a })}
              onClick={() => onChange(value.filter((_, j) => j !== i))}
            >
              <Icon name="x" size={10} />
            </button>
          </span>
        );
      })}
      <input
        id={id}
        value={stroka}
        autoComplete="off"
        spellCheck={false}
        role="combobox"
        aria-expanded={pokazany}
        aria-autocomplete="list"
        onChange={(e) => {
          const v = e.target.value;
          if (/[,;]\s*$/.test(v)) {
            dobavit(v.split(/[,;]+/));
            setStroka("");
            return;
          }
          setStroka(v);
          setOtkryto(true);
        }}
        onPaste={(e) => {
          const vstavka = e.clipboardData.getData("text");
          if (/[,;\n]/.test(vstavka)) {
            e.preventDefault();
            dobavit(vstavka.split(/[,;\n]+/));
          }
        }}
        onBlur={() => {
          setOtkryto(false);
          zafiksirovat();
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape" && pokazany) {
            e.preventDefault();
            setOtkryto(false);
            return;
          }
          if (pokazany && e.key === "ArrowDown") {
            e.preventDefault();
            setPodsvechen((b) => Math.min(b + 1, podskazki.length - 1));
            return;
          }
          if (pokazany && e.key === "ArrowUp") {
            e.preventDefault();
            setPodsvechen((b) => Math.max(b - 1, 0));
            return;
          }
          if ((e.key === "Enter" || e.key === "Tab") && pokazany && podskazki[podsvechen]) {
            e.preventDefault();
            vzyat(podskazki[podsvechen]);
            return;
          }
          // Enter ставит адрес, а не отправляет письмо. Пробел — только после
          // готового адреса: до того он нужен поиску клиента по имени.
          if ((e.key === "Enter" && stroka.trim()) || (e.key === " " && ADRES.test(stroka.trim()))) {
            e.preventDefault();
            zafiksirovat();
            return;
          }
          if (e.key === "Backspace" && !stroka && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
      />
      {pokazany && (
        <div className="card popover-card wm-suggest" role="listbox" aria-label={label}>
          {podskazki.map((p, i) => (
            <button
              key={p.email}
              type="button"
              role="option"
              aria-selected={i === podsvechen}
              className={i === podsvechen ? "is-lit" : ""}
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setPodsvechen(i)}
              onClick={() => vzyat(p)}
            >
              <span>{p.name}</span>
              <small>{p.email}</small>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Отправка из карточки клиента и заявки: зеркало писем (`/mail/send`), а не
// веб-почта. Живёт здесь, потому что карточки импортируют её отсюда.

export interface MailMessage {
  id: number;
  account_id: number;
  direction: "in" | "out";
  subject: string;
  from_addr: string;
  to_addrs: string[];
  sent_at: string | null;
  has_attachments: boolean;
  client_id: number | null;
  is_read: boolean;
  body_text?: string;
  body_html?: string;
}

/**
 * Ящик, ИЗ КОТОРОГО можно писать. Три поля и только они.
 *
 * Не путать с настройкой ящика (`Mailboxes.tsx`): там хосты, порты и логины,
 * и закрыты они правом `settings.manage`. Чтобы отправить письмо, всего этого
 * знать не надо — нужен выбор из двух-трёх адресов, и его отдаёт `/mail/senders`
 * тому, у кого есть право писать.
 */
export interface MailSender {
  id: number;
  title: string;
  address: string;
}

/** Форма отправки. Живёт отдельно: её открывают из карточки клиента и заявки. */
export function MailCompose({
  accounts,
  // Без умолчания "" намеренно: ниже стоит `to ?? …`, а пустая строка не
  // нулевая — с умолчанием ветка ответа не срабатывала НИКОГДА, и поле адреса
  // оставалось пустым. Поймано живым осмотром экрана.
  to,
  clientId,
  dealId,
  replyTo,
  onClose,
  onSent,
}: {
  accounts: MailSender[];
  to?: string;
  clientId?: number;
  dealId?: number;
  /** Письмо, на которое отвечаем. Оно задаёт адрес, тему и цепочку. */
  replyTo?: MailMessage;
  onClose: () => void;
  onSent?: () => void;
}) {
  const { t, toast, toastError } = useApp();
  // Отвечают ОТПРАВИТЕЛЮ входящего и ПОЛУЧАТЕЛЮ исходящего: во втором случае
  // человек дописывает собеседнику, а не самому себе.
  const komu =
    to ??
    (replyTo ? (replyTo.direction === "in" ? replyTo.from_addr : replyTo.to_addrs.join(", ")) : "");
  // «Re:» не удваивается: на «Re: Заявка» ответ остаётся «Re: Заявка».
  const tema = replyTo
    ? /^\s*re\s*:/i.test(replyTo.subject)
      ? replyTo.subject
      : `Re: ${replyTo.subject}`
    : "";
  const [form, setForm] = useState({ to: komu, subject: tema, body: "" });
  const [accountId, setAccountId] = useState<number | "">(
    // Отвечаем ИЗ ТОГО ЯЩИКА, куда письмо пришло. Ответ с другого адреса
    // приходит собеседнику от незнакомца и в переписку не встаёт.
    replyTo?.account_id ?? accounts[0]?.id ?? "",
  );
  // Засов, а не флаг состояния: отправка идёт через SMTP и отвечает не сразу.
  // Второе нажатие по «неответившей» кнопке слало клиенту второе такое же
  // письмо — и отозвать его уже нельзя ничем.
  const guard = useGuard();

  const send = async () => {
    if (!guard.take()) return;
    try {
      await api.post("/mail/send", {
        to: form.to.split(",").map((a) => a.trim()).filter(Boolean),
        subject: form.subject,
        body: form.body,
        account_id: accountId || null,
        client_id: clientId ?? null,
        deal_id: dealId ?? null,
        reply_to_id: replyTo?.id ?? null,
      });
      toast(t("letterSent"));
      onSent?.();
      onClose();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  return (
    <Modal title={replyTo ? t("reply") : t("compose")} onClose={onClose} wide>
      {accounts.length > 1 && (
        <div className="field">
          <label className="label">{t("fromMailbox")}</label>
          <select
            className="input"
            value={accountId}
            onChange={(e) => setAccountId(Number(e.target.value))}
          >
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.title || account.address}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="field">
        <label className="label">{t("toField")}</label>
        <input
          className="input"
          value={form.to}
          onChange={(e) => setForm({ ...form, to: e.target.value })}
          placeholder="client@example.com"
        />
      </div>
      <div className="field">
        <label className="label">{t("subjectField")}</label>
        <input
          className="input"
          value={form.subject}
          onChange={(e) => setForm({ ...form, subject: e.target.value })}
        />
      </div>
      <div className="field">
        <label className="label">{t("bodyField")}</label>
        <textarea
          className="textarea"
          rows={8}
          value={form.body}
          onChange={(e) => setForm({ ...form, body: e.target.value })}
        />
      </div>
      <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 16 }}>
        <button className="btn btn-secondary btn-sm" onClick={onClose}>
          {t("cancel")}
        </button>
        <button className="btn btn-primary btn-sm" disabled={guard.busy} onClick={() => void send()}>
          {t("sendLetter")}
        </button>
      </div>
    </Modal>
  );
}
