/**
 * Планета: ортографическая проекция, дуги большого круга, попадание по точке.
 *
 * Своя, а не библиотека: карта обязана работать в установке без интернета, а
 * готовые глобусы тянут базовый слой с чужого сервера (разбор —
 * `docs/bloki/25-globus.md` §2).
 */

export interface Vid {
  /** Долгота и широта в центре круга, градусы. */
  lon0: number;
  lat0: number;
  /** Радиус планеты в пикселях. */
  r: number;
  /** Центр круга на холсте. */
  cx: number;
  cy: number;
  /** Радиус при масштабе ×1: от него считается `r` при приближении. */
  bazovyy?: number;
  masshtab?: number;
}

export interface NaEkrane {
  x: number;
  y: number;
  /** Точка на видимой половине шара. */
  vidno: boolean;
}

const RAD = Math.PI / 180;

/** Кольцо из приращений в сотых долях градуса — в пары долгота/широта. */
export function razvernut(upakovannoe: readonly number[]): number[] {
  const tochki: number[] = [];
  let lon = 0;
  let lat = 0;
  for (let i = 0; i < upakovannoe.length; i += 2) {
    lon += upakovannoe[i];
    lat += upakovannoe[i + 1];
    tochki.push(lon / 100, lat / 100);
  }
  return tochki;
}

export function proekciya(lon: number, lat: number, vid: Vid): NaEkrane {
  const f = lat * RAD;
  const l = (lon - vid.lon0) * RAD;
  const f0 = vid.lat0 * RAD;
  const cosF = Math.cos(f);
  const sinF = Math.sin(f);
  const cosL = Math.cos(l);
  const kosinus = Math.sin(f0) * sinF + Math.cos(f0) * cosF * cosL;
  return {
    x: vid.cx + vid.r * cosF * Math.sin(l),
    y: vid.cy - vid.r * (Math.cos(f0) * sinF - Math.sin(f0) * cosF * cosL),
    vidno: kosinus > 0,
  };
}

/** Точка холста обратно в долготу и широту. `null` — мимо шара. */
export function obratno(x: number, y: number, vid: Vid): { lon: number; lat: number } | null {
  const dx = x - vid.cx;
  const dy = vid.cy - y;
  const rho = Math.sqrt(dx * dx + dy * dy);
  if (rho > vid.r) return null;
  const c = Math.asin(Math.min(1, rho / vid.r));
  const f0 = vid.lat0 * RAD;
  if (rho === 0) return { lon: vid.lon0, lat: vid.lat0 };
  const lat = Math.asin(Math.cos(c) * Math.sin(f0) + (dy * Math.sin(c) * Math.cos(f0)) / rho);
  const lon =
    vid.lon0 +
    Math.atan2(dx * Math.sin(c), rho * Math.cos(c) * Math.cos(f0) - dy * Math.sin(c) * Math.sin(f0)) /
      RAD;
  return { lon: svernut(lon), lat: lat / RAD };
}

/** Долгота за 180° — тот же меридиан с другой стороны. */
export function svernut(lon: number): number {
  let itog = lon;
  while (itog > 180) itog -= 360;
  while (itog < -180) itog += 360;
  return itog;
}

/** Единичный вектор точки на шаре. */
export function vektor(lon: number, lat: number): [number, number, number] {
  const f = lat * RAD;
  const l = lon * RAD;
  return [Math.cos(f) * Math.cos(l), Math.cos(f) * Math.sin(l), Math.sin(f)];
}

export function iz_vektora(v: [number, number, number]): { lon: number; lat: number } {
  return { lon: Math.atan2(v[1], v[0]) / RAD, lat: Math.asin(Math.max(-1, Math.min(1, v[2]))) / RAD };
}

/** Угол между точками, радианы. Им же меряется длина дуги. */
export function ugol(a: [number, number, number], b: [number, number, number]): number {
  return Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));
}

/**
 * Дуга большого круга: кратчайший путь по шару, приподнятый над поверхностью.
 *
 * Приподнятый — потому что линия Киев → Сидней иначе прошла бы сквозь планету
 * и читалась бы как прямая через ядро. Подъём растёт с длиной: близкие точки
 * соединяет почти прямая.
 */
export function duga(
  ot: { lon: number; lat: number },
  k: { lon: number; lat: number },
  shagov = 48,
): { lon: number; lat: number; podnyatie: number }[] {
  const a = vektor(ot.lon, ot.lat);
  const b = vektor(k.lon, k.lat);
  const razmah = ugol(a, b);
  const tochki: { lon: number; lat: number; podnyatie: number }[] = [];
  if (razmah < 1e-6) return [{ ...ot, podnyatie: 0 }];
  const sin = Math.sin(razmah);
  const vysota = Math.min(0.35, razmah / Math.PI);
  for (let i = 0; i <= shagov; i++) {
    const t = i / shagov;
    const ka = Math.sin((1 - t) * razmah) / sin;
    const kb = Math.sin(t * razmah) / sin;
    const v: [number, number, number] = [
      ka * a[0] + kb * b[0],
      ka * a[1] + kb * b[1],
      ka * a[2] + kb * b[2],
    ];
    const dlina = Math.hypot(v[0], v[1], v[2]) || 1;
    tochki.push({
      ...iz_vektora([v[0] / dlina, v[1] / dlina, v[2] / dlina]),
      podnyatie: vysota * Math.sin(Math.PI * t),
    });
  }
  return tochki;
}

/**
 * Где сейчас солнце в зените. Точность около половины градуса — на глобусе
 * это меньше толщины линии терминатора, а точная астрономия здесь ни к чему.
 */
export function podsolnechnaya(kogda: Date): { lon: number; lat: number } {
  const sutki = (kogda.getTime() - Date.UTC(kogda.getUTCFullYear(), 0, 0)) / 86_400_000;
  const naklon = -23.44 * Math.cos(((360 / 365.24) * (sutki + 10) * Math.PI) / 180);
  const chasy =
    kogda.getUTCHours() + kogda.getUTCMinutes() / 60 + kogda.getUTCSeconds() / 3600;
  return { lon: svernut(180 - chasy * 15), lat: naklon };
}

/** Кольцо терминатора: точки, равноудалённые от подсолнечной на 90°. */
export function terminator(solnce: { lon: number; lat: number }, shagov = 180): { lon: number; lat: number }[] {
  const s = vektor(solnce.lon, solnce.lat);
  // Любая пара, перпендикулярная солнцу: берём ось, наименее с ним совпадающую,
  // иначе на полюсе векторное произведение вырождается в ноль.
  const opora: [number, number, number] = Math.abs(s[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
  const u = normalizovat(vektornoe(s, opora));
  const v = normalizovat(vektornoe(s, u));
  const tochki: { lon: number; lat: number }[] = [];
  for (let i = 0; i <= shagov; i++) {
    const t = (i / shagov) * 2 * Math.PI;
    tochki.push(
      iz_vektora([
        u[0] * Math.cos(t) + v[0] * Math.sin(t),
        u[1] * Math.cos(t) + v[1] * Math.sin(t),
        u[2] * Math.cos(t) + v[2] * Math.sin(t),
      ]),
    );
  }
  return tochki;
}

function vektornoe(a: [number, number, number], b: [number, number, number]): [number, number, number] {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function normalizovat(v: [number, number, number]): [number, number, number] {
  const d = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / d, v[1] / d, v[2] / d];
}

/** Освещена ли точка: скалярное произведение с направлением на солнце. */
export function osveshcheno(lon: number, lat: number, solnce: { lon: number; lat: number }): boolean {
  const a = vektor(lon, lat);
  const s = vektor(solnce.lon, solnce.lat);
  return a[0] * s[0] + a[1] * s[1] + a[2] * s[2] > 0;
}

/** Плавность доворота: медленно в начале и в конце, быстро в середине. */
export function plavno(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** Кратчайший путь по долготе: из 179° в -179° поворот на 2°, а не на 358°. */
export function korotkiy_povorot(ot: number, k: number): number {
  return ot + svernut(k - ot);
}
