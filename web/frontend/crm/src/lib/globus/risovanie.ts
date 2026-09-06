/**
 * Отрисовка планеты на холсте: шар, суша, сетка, ночь, связи, точки.
 *
 * Холст, а не SVG: точек с гостями бывает несколько сотен, и каждая — три
 * фигуры; в SVG это тысяча узлов, которые браузер пересобирает на каждом
 * повороте. Разбор — `docs/bloki/25-globus.md` §3.
 */
import {
  duga,
  osveshcheno,
  podsolnechnaya,
  proekciya,
  razvernut,
  terminator,
  type Vid,
} from "./proekciya";

export interface Tochka {
  vid: "client" | "visitor";
  id: number;
  imya: string;
  podpis: string;
  lat: number;
  lon: number;
  tochnost: string;
  state: string;
  deals_open?: number;
  orders_open?: number;
  overdue?: number;
}

export interface Svyaz {
  ot: string;
  k: string;
  vid: string;
  ves: number;
}

export interface Baza {
  lat: number;
  lon: number;
  imya: string;
  tochnost: string;
}

/** Цвета берутся из токенов один раз за кадр: чтение стилей стоит дорого. */
export interface Cveta {
  more: string;
  susha: string;
  bereg: string;
  setka: string;
  limb: string;
  noch: string;
  zoloto: string;
  cian: string;
  uspekh: string;
  trevoga: string;
  beda: string;
  tekst: string;
  tusklo: string;
}

export interface Kadr {
  vid: Vid;
  kontury: readonly (readonly number[])[];
  tochki: Tochka[];
  svyazi: Svyaz[];
  baza: Baza | null;
  sloi: Set<string>;
  vybrano: string | null;
  navedeno: string | null;
  cveta: Cveta;
  /** 0…1 — проявление точек при первой отрисовке. */
  poyavlenie: number;
  /** Мгновение для дня и ночи. */
  kogda: Date;
  /** Пульс выбранной точки, 0…1 по кругу. */
  puls: number;
}

const RAD = Math.PI / 180;

export function klyuch(tochka: Tochka): string {
  return `${tochka.vid}:${tochka.id}`;
}

function cvet_tochki(tochka: Tochka, cveta: Cveta): string {
  if (tochka.vid === "visitor") return cveta.cian;
  if (tochka.state === "prosrochka") return cveta.beda;
  if (tochka.state === "rabota") return cveta.uspekh;
  return cveta.zoloto;
}

/** Ореол тем шире, чем грубее известно место: страна — большой, точка — нет. */
function oreol(tochnost: string): number {
  if (tochnost === "strana") return 13;
  if (tochnost === "gorod") return 8;
  return 5;
}

export function narisovat(ctx: CanvasRenderingContext2D, kadr: Kadr): void {
  const { vid, cveta } = kadr;
  ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);

  atmosfera(ctx, vid, cveta);
  shar(ctx, vid, cveta);

  ctx.save();
  ctx.beginPath();
  ctx.arc(vid.cx, vid.cy, vid.r, 0, Math.PI * 2);
  ctx.clip();

  susha(ctx, kadr);
  if (kadr.sloi.has("grid")) setka(ctx, vid, cveta);
  if (kadr.sloi.has("night")) noch(ctx, kadr);
  ctx.restore();

  limb(ctx, vid, cveta);
  if (kadr.sloi.has("links")) svyazi(ctx, kadr);
  tochki(ctx, kadr);
  if (kadr.sloi.has("labels")) podpisi(ctx, kadr);
}

function atmosfera(ctx: CanvasRenderingContext2D, vid: Vid, cveta: Cveta): void {
  const svet = ctx.createRadialGradient(vid.cx, vid.cy, vid.r * 0.92, vid.cx, vid.cy, vid.r * 1.35);
  svet.addColorStop(0, cveta.cian);
  svet.addColorStop(1, "transparent");
  ctx.globalAlpha = 0.13;
  ctx.fillStyle = svet;
  ctx.beginPath();
  ctx.arc(vid.cx, vid.cy, vid.r * 1.35, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalAlpha = 1;
}

function shar(ctx: CanvasRenderingContext2D, vid: Vid, cveta: Cveta): void {
  const zalivka = ctx.createRadialGradient(
    vid.cx - vid.r * 0.3,
    vid.cy - vid.r * 0.35,
    vid.r * 0.1,
    vid.cx,
    vid.cy,
    vid.r,
  );
  zalivka.addColorStop(0, cveta.more);
  zalivka.addColorStop(1, cveta.noch);
  ctx.fillStyle = zalivka;
  ctx.beginPath();
  ctx.arc(vid.cx, vid.cy, vid.r, 0, Math.PI * 2);
  ctx.fill();
}

function limb(ctx: CanvasRenderingContext2D, vid: Vid, cveta: Cveta): void {
  ctx.strokeStyle = cveta.limb;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(vid.cx, vid.cy, vid.r, 0, Math.PI * 2);
  ctx.stroke();
}

/**
 * Суша кусками видимой половины: кольцо, уходящее за край, рвётся на дуги, и
 * каждая заливается своей. Так берег не заворачивается через полшара.
 */
function susha(ctx: CanvasRenderingContext2D, kadr: Kadr): void {
  const { vid, cveta } = kadr;
  ctx.fillStyle = cveta.susha;
  ctx.strokeStyle = cveta.bereg;
  ctx.lineWidth = 0.7;
  for (const upakovannoe of kadr.kontury) {
    const pary = razvernut(upakovannoe);
    let put: Path2D | null = null;
    for (let i = 0; i < pary.length; i += 2) {
      const t = proekciya(pary[i], pary[i + 1], vid);
      if (!t.vidno) {
        if (put) {
          ctx.fill(put);
          ctx.stroke(put);
          put = null;
        }
        continue;
      }
      if (!put) {
        put = new Path2D();
        put.moveTo(t.x, t.y);
      } else {
        put.lineTo(t.x, t.y);
      }
    }
    if (put) {
      ctx.fill(put);
      ctx.stroke(put);
    }
  }
}

function setka(ctx: CanvasRenderingContext2D, vid: Vid, cveta: Cveta): void {
  ctx.strokeStyle = cveta.setka;
  ctx.lineWidth = 0.5;
  for (let lon = -180; lon < 180; lon += 30) linia(ctx, vid, (t) => ({ lon, lat: -90 + t * 180 }));
  for (let lat = -60; lat <= 60; lat += 30) linia(ctx, vid, (t) => ({ lon: -180 + t * 360, lat }));
}

function linia(
  ctx: CanvasRenderingContext2D,
  vid: Vid,
  tochka: (t: number) => { lon: number; lat: number },
): void {
  ctx.beginPath();
  let veli = false;
  for (let i = 0; i <= 90; i++) {
    const { lon, lat } = tochka(i / 90);
    const t = proekciya(lon, lat, vid);
    if (!t.vidno) {
      veli = false;
      continue;
    }
    if (veli) ctx.lineTo(t.x, t.y);
    else ctx.moveTo(t.x, t.y);
    veli = true;
  }
  ctx.stroke();
}

/**
 * Ночная сторона: край освещённости — большой круг, его видимая часть режет
 * диск надвое. Половину, где солнца нет, затеняем.
 */
function noch(ctx: CanvasRenderingContext2D, kadr: Kadr): void {
  const { vid, cveta } = kadr;
  const solnce = podsolnechnaya(kadr.kogda);
  const kraya = terminator(solnce)
    .map((t) => ({ ...t, ekran: proekciya(t.lon, t.lat, vid) }))
    .filter((t) => t.ekran.vidno);
  ctx.save();
  ctx.globalAlpha = 0.42;
  ctx.fillStyle = cveta.noch;
  if (kraya.length < 3) {
    // Терминатора не видно вовсе: либо перед нами весь день, либо вся ночь.
    if (!osveshcheno(vid.lon0, vid.lat0, solnce)) {
      ctx.beginPath();
      ctx.arc(vid.cx, vid.cy, vid.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
    return;
  }
  const pervaya = kraya[0].ekran;
  const poslednyaya = kraya[kraya.length - 1].ekran;
  ctx.beginPath();
  ctx.moveTo(pervaya.x, pervaya.y);
  for (const t of kraya) ctx.lineTo(t.ekran.x, t.ekran.y);
  // Замыкаем по краю диска — той дугой, что лежит на ночной стороне.
  const ugol_ot = Math.atan2(poslednyaya.y - vid.cy, poslednyaya.x - vid.cx);
  const ugol_do = Math.atan2(pervaya.y - vid.cy, pervaya.x - vid.cx);
  const seredina = (ugol_ot + ugol_do) / 2;
  const proba = {
    x: vid.cx + vid.r * 0.98 * Math.cos(seredina),
    y: vid.cy + vid.r * 0.98 * Math.sin(seredina),
  };
  const protiv = !nochnaya(proba, vid, solnce);
  ctx.arc(vid.cx, vid.cy, vid.r, ugol_ot, ugol_do, protiv);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

function nochnaya(tochka: { x: number; y: number }, vid: Vid, solnce: { lon: number; lat: number }): boolean {
  const dx = (tochka.x - vid.cx) / vid.r;
  const dy = (vid.cy - tochka.y) / vid.r;
  const z = Math.sqrt(Math.max(0, 1 - dx * dx - dy * dy));
  const f0 = vid.lat0 * RAD;
  const lat = Math.asin(dy * Math.cos(f0) + z * Math.sin(f0)) / RAD;
  const lon = vid.lon0 + Math.atan2(dx, z * Math.cos(f0) - dy * Math.sin(f0)) / RAD;
  return !osveshcheno(lon, lat, solnce);
}

function svyazi(ctx: CanvasRenderingContext2D, kadr: Kadr): void {
  const { vid, cveta } = kadr;
  const po_klyuchu = new Map(kadr.tochki.map((t) => [klyuch(t), t]));
  for (const svyaz of kadr.svyazi) {
    const ot =
      svyaz.ot === "base"
        ? kadr.baza && { lat: kadr.baza.lat, lon: kadr.baza.lon }
        : po_klyuchu.get(svyaz.ot);
    const k = po_klyuchu.get(svyaz.k);
    if (!ot || !k) continue;
    const svoya = kadr.vybrano === svyaz.k || kadr.vybrano === svyaz.ot;
    ctx.strokeStyle = svyaz.vid === "prosmotr" ? cveta.cian : cveta.zoloto;
    ctx.globalAlpha = svoya ? 0.95 : kadr.vybrano ? 0.12 : 0.4;
    ctx.lineWidth = svoya ? 1.8 : Math.min(2, 0.6 + svyaz.ves * 0.2);
    ctx.beginPath();
    let veli = false;
    for (const tochka of duga(ot, k)) {
      const e = proekciya(tochka.lon, tochka.lat, { ...vid, r: vid.r * (1 + tochka.podnyatie) });
      const na_share = proekciya(tochka.lon, tochka.lat, vid);
      if (!na_share.vidno) {
        veli = false;
        continue;
      }
      if (veli) ctx.lineTo(e.x, e.y);
      else ctx.moveTo(e.x, e.y);
      veli = true;
    }
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

function tochki(ctx: CanvasRenderingContext2D, kadr: Kadr): void {
  const { vid, cveta } = kadr;
  if (kadr.baza) {
    const b = proekciya(kadr.baza.lon, kadr.baza.lat, vid);
    if (b.vidno) baza_znak(ctx, b.x, b.y, cveta);
  }
  for (const tochka of kadr.tochki) {
    if (!vidno_sloyu(tochka, kadr.sloi)) continue;
    const e = proekciya(tochka.lon, tochka.lat, vid);
    if (!e.vidno) continue;
    const cvet = cvet_tochki(tochka, cveta);
    const svoya = kadr.vybrano === klyuch(tochka);
    const pod = kadr.navedeno === klyuch(tochka);
    const rost = kadr.poyavlenie;

    ctx.globalAlpha = 0.12 * rost;
    ctx.fillStyle = cvet;
    ctx.beginPath();
    ctx.arc(e.x, e.y, oreol(tochka.tochnost) * rost, 0, Math.PI * 2);
    ctx.fill();

    ctx.globalAlpha = rost;
    ctx.beginPath();
    ctx.arc(e.x, e.y, (svoya || pod ? 4.2 : 3) * rost, 0, Math.PI * 2);
    ctx.fill();

    if (svoya) {
      ctx.globalAlpha = (1 - kadr.puls) * 0.9;
      ctx.strokeStyle = cvet;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.arc(e.x, e.y, 5 + kadr.puls * 26, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
}

function baza_znak(ctx: CanvasRenderingContext2D, x: number, y: number, cveta: Cveta): void {
  ctx.strokeStyle = cveta.zoloto;
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.moveTo(x - 6, y);
  ctx.lineTo(x + 6, y);
  ctx.moveTo(x, y - 6);
  ctx.lineTo(x, y + 6);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(x, y, 4.5, 0, Math.PI * 2);
  ctx.stroke();
}

export function vidno_sloyu(tochka: Tochka, sloi: Set<string>): boolean {
  if (tochka.vid === "visitor") return sloi.has("visitors");
  if (!sloi.has("clients")) return false;
  if (tochka.state === "prosrochka" && !sloi.has("overdue")) return sloi.has("orders");
  return true;
}

function podpisi(ctx: CanvasRenderingContext2D, kadr: Kadr): void {
  const { vid, cveta } = kadr;
  ctx.font = "600 10px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textBaseline = "middle";
  const zanyato: { x: number; y: number }[] = [];
  for (const tochka of kadr.tochki) {
    if (!vidno_sloyu(tochka, kadr.sloi)) continue;
    const svoya = kadr.vybrano === klyuch(tochka) || kadr.navedeno === klyuch(tochka);
    if (!svoya && tochka.vid === "visitor") continue;
    const e = proekciya(tochka.lon, tochka.lat, vid);
    if (!e.vidno) continue;
    // Подпись рядом с занятым местом не читается: соседнюю пропускаем, а
    // выбранную показываем всегда — её и просили показать.
    if (!svoya && zanyato.some((z) => Math.abs(z.x - e.x) < 70 && Math.abs(z.y - e.y) < 13)) continue;
    zanyato.push({ x: e.x, y: e.y });
    ctx.fillStyle = svoya ? cveta.tekst : cveta.tusklo;
    ctx.globalAlpha = svoya ? 1 : 0.75;
    ctx.fillText(tochka.imya.slice(0, 22), e.x + 8, e.y - 7);
  }
  if (kadr.baza) {
    const b = proekciya(kadr.baza.lon, kadr.baza.lat, vid);
    if (b.vidno) {
      ctx.fillStyle = cveta.zoloto;
      ctx.globalAlpha = 1;
      ctx.fillText(kadr.baza.imya.slice(0, 22), b.x + 10, b.y + 9);
    }
  }
  ctx.globalAlpha = 1;
}

/** Ближайшая точка к месту нажатия. `null` — мимо всех. */
export function pod_kursorom(kadr: Kadr, x: number, y: number, predel = 16): Tochka | null {
  let luchshaya: Tochka | null = null;
  let bliz = predel * predel;
  for (const tochka of kadr.tochki) {
    if (!vidno_sloyu(tochka, kadr.sloi)) continue;
    const e = proekciya(tochka.lon, tochka.lat, kadr.vid);
    if (!e.vidno) continue;
    const d = (e.x - x) * (e.x - x) + (e.y - y) * (e.y - y);
    if (d < bliz) {
      bliz = d;
      luchshaya = tochka;
    }
  }
  return luchshaya;
}
