/**
 * Миниатюра карты на холсте — своя, как и планета глобуса.
 *
 * Картинка с чужого сервера дала бы в карточке клиента крестик на установке
 * без интернета, а карточку обязаны показывать целиком (тот же довод, что в
 * `docs/bloki/25-globus.md` §2). Очертания берём вшитые, `globus/mir.ts`.
 *
 * Проекция здесь прямоугольная, а не ортографическая: миниатюру читают как
 * «где это на карте» — север сверху, и кривизна шара на паре тысяч километров
 * только мешает узнать очертания.
 */
import { KONTURY } from "./globus/mir";
import { razvernut, svernut } from "./globus/proekciya";

export interface CvetaKarty {
  more: string;
  susha: string;
  bereg: string;
  setka: string;
  tochka: string;
}

export interface ZakazKarty {
  /** Точка в центре миниатюры. */
  lat: number;
  lon: number;
  /** Размер холста в единицах CSS, без учёта плотности точек. */
  shirina: number;
  vysota: number;
  /** Сколько градусов широты видно по высоте. */
  ohvat: number;
  cveta: CvetaKarty;
}

const RAD = Math.PI / 180;

/** Отрисовать миниатюру. Холст уже приведён к плотности точек экрана. */
export function narisovat_minikartu(ctx: CanvasRenderingContext2D, zakaz: ZakazKarty): void {
  const { shirina, vysota, cveta } = zakaz;
  const na_shirotu = vysota / zakaz.ohvat;
  // Градус долготы короче градуса широты в cos(широты) раз, у полюса — почти
  // ноль. Подпёрт снизу: без этого карта Мурманска растянулась бы в полосу.
  const na_dolgotu = na_shirotu * Math.max(0.15, Math.cos(zakaz.lat * RAD));
  const pol_shiroty = vysota / 2 / na_shirotu;
  const pol_dolgoty = shirina / 2 / na_dolgotu;

  ctx.clearRect(0, 0, shirina, vysota);
  ctx.fillStyle = cveta.more;
  ctx.fillRect(0, 0, shirina, vysota);

  ctx.fillStyle = cveta.susha;
  ctx.strokeStyle = cveta.bereg;
  // Не тоньше пикселя: 0.8 размазывается сглаживанием по двум пикселям с
  // частичной прозрачностью, и посчитанный контраст на экране не достигается.
  ctx.lineWidth = 1.2;
  ctx.lineJoin = "round";
  for (const upakovannoe of KONTURY) {
    const kolco = vidnoe_kolco(razvernut(upakovannoe), zakaz, pol_dolgoty, pol_shiroty);
    if (!kolco) continue;
    const put = new Path2D();
    for (let i = 0; i < kolco.length; i += 2) {
      const x = shirina / 2 + kolco[i] * na_dolgotu;
      const y = vysota / 2 - (kolco[i + 1] - zakaz.lat) * na_shirotu;
      if (i === 0) put.moveTo(x, y);
      else put.lineTo(x, y);
    }
    put.closePath();
    ctx.fill(put);
    ctx.stroke(put);
  }

  setka(ctx, zakaz, na_shirotu, na_dolgotu, pol_shiroty, pol_dolgoty);
  tochka(ctx, shirina, vysota, cveta);
}

/**
 * Кольцо в координатах относительно центра — или `null`, если его тут не видно.
 *
 * Отбор по охватывающему прямоугольнику: колец под три сотни, а в окошко на
 * пару тысяч километров попадает пяток. Заодно он убирает кольца, обходящие
 * планету кругом (Антарктида): их долготы рвутся на шве, и заливка разорванного
 * кольца провела бы хорду через всю миниатюру.
 */
function vidnoe_kolco(
  pary: number[],
  zakaz: ZakazKarty,
  pol_dolgoty: number,
  pol_shiroty: number,
): number[] | null {
  const zapas = 1.5;
  const kolco: number[] = [];
  let lon_ot = Infinity;
  let lon_do = -Infinity;
  let lat_ot = Infinity;
  let lat_do = -Infinity;
  for (let i = 0; i < pary.length; i += 2) {
    const dolgota = svernut(pary[i] - zakaz.lon);
    const shirota = pary[i + 1];
    kolco.push(dolgota, shirota);
    if (dolgota < lon_ot) lon_ot = dolgota;
    if (dolgota > lon_do) lon_do = dolgota;
    if (shirota < lat_ot) lat_ot = shirota;
    if (shirota > lat_do) lat_do = shirota;
  }
  if (lon_do - lon_ot > 180) return null;
  if (lon_ot > pol_dolgoty + zapas || lon_do < -pol_dolgoty - zapas) return null;
  if (lat_ot > zakaz.lat + pol_shiroty + zapas || lat_do < zakaz.lat - pol_shiroty - zapas) {
    return null;
  }
  return kolco;
}

/** Сетка: без неё на однотонной суше не видно ни масштаба, ни направления. */
function setka(
  ctx: CanvasRenderingContext2D,
  zakaz: ZakazKarty,
  na_shirotu: number,
  na_dolgotu: number,
  pol_shiroty: number,
  pol_dolgoty: number,
): void {
  const shag = zakaz.ohvat <= 12 ? 2 : zakaz.ohvat <= 30 ? 5 : 10;
  ctx.strokeStyle = zakaz.cveta.setka;
  ctx.lineWidth = 1;
  ctx.beginPath();
  const pervaya = Math.ceil((zakaz.lat - pol_shiroty) / shag) * shag;
  for (let lat = pervaya; lat <= zakaz.lat + pol_shiroty; lat += shag) {
    const y = Math.round(zakaz.vysota / 2 - (lat - zakaz.lat) * na_shirotu) + 0.5;
    ctx.moveTo(0, y);
    ctx.lineTo(zakaz.shirina, y);
  }
  const pervyy = Math.ceil((zakaz.lon - pol_dolgoty) / shag) * shag;
  for (let lon = pervyy; lon <= zakaz.lon + pol_dolgoty; lon += shag) {
    const x = Math.round(zakaz.shirina / 2 + svernut(lon - zakaz.lon) * na_dolgotu) + 0.5;
    ctx.moveTo(x, 0);
    ctx.lineTo(x, zakaz.vysota);
  }
  ctx.stroke();
}

/** Сама точка: кружок и перекрестье во всю миниатюру. */
function tochka(ctx: CanvasRenderingContext2D, shirina: number, vysota: number, cveta: CvetaKarty): void {
  const cx = Math.round(shirina / 2) + 0.5;
  const cy = Math.round(vysota / 2) + 0.5;
  ctx.strokeStyle = cveta.tochka;
  ctx.lineWidth = 1;
  ctx.globalAlpha = 0.35;
  ctx.beginPath();
  ctx.moveTo(0, cy);
  ctx.lineTo(shirina, cy);
  ctx.moveTo(cx, 0);
  ctx.lineTo(cx, vysota);
  ctx.stroke();
  ctx.globalAlpha = 1;
  ctx.beginPath();
  ctx.arc(cx, cy, 7.5, 0, Math.PI * 2);
  ctx.stroke();
  ctx.fillStyle = cveta.tochka;
  ctx.beginPath();
  ctx.arc(cx, cy, 3.6, 0, Math.PI * 2);
  ctx.fill();
}
