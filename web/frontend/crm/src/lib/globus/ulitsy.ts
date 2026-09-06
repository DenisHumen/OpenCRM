/**
 * Улицы и дома: какие плитки нужны виду и что с них рисовать.
 *
 * Плитки те же, что у любой карты (slippy z/x/y), и номера считаются одинаково
 * здесь и на сервере (`core/services/globus_ulitsy_service.py`). Держать эту
 * арифметику в одном месте нельзя — она нужна обеим сторонам, — поэтому она
 * повторена дословно, и сторож сверяет числа: `tests/test_globus.py`.
 */
import { obratno, type Vid } from "./proekciya";

/** Уровень плитки. Тот же, что у сервера: чужие номера он не примет.
 *  Четырнадцатый — не наш выбор: дома в `shortbread` есть только с него. */
export const PLITKA_Z = 14;

/** Точность упаковки: стотысячная градуса, около метра. */
export const TOCHNOST = 100_000;

/**
 * С какого масштаба улицы вообще спрашиваются.
 *
 * Считаем метрами на пиксель: пока в пикселе больше восьми метров, улица
 * тоньше пикселя, и город превращается в серое пятно — рисовать нечего, а
 * плиток на экран пришлось бы полсотни.
 */
export const PREDEL_METROV = 8;

/** Больше этого за раз не просим: человек возит планету, а не изучает квартал. */
export const PREDEL_PLITOK = 16;

const RADIUS_ZEMLI = 6_371_000;

export interface Plitka {
  x: number;
  y: number;
  gotovo: boolean;
  idet: boolean;
  oshibka: string;
  /** `[вид, lon, lat, dlon, dlat, …]` — разности в сотых тысячных градуса. */
  dorogi?: number[][];
  doma?: number[][];
}

/** Сколько метров в пикселе при этом радиусе планеты. */
export function metrov_na_piksel(vid: Vid): number {
  return RADIUS_ZEMLI / Math.max(1, vid.r);
}

/** Пора ли рисовать улицы. */
export function pora(vid: Vid): boolean {
  return metrov_na_piksel(vid) <= PREDEL_METROV;
}

/** Номер плитки, в которую попала точка. */
export function nomer(lon: number, lat: number, z = PLITKA_Z): { x: number; y: number } {
  const n = 2 ** z;
  const shirota = Math.max(-85.05, Math.min(85.05, lat));
  const rad = (shirota * Math.PI) / 180;
  const x = Math.floor(((lon + 180) / 360) * n);
  const doba = Math.log(Math.tan(rad) + 1 / Math.cos(rad));
  const y = Math.floor(((1 - doba / Math.PI) / 2) * n);
  return { x: ((x % n) + n) % n, y: Math.max(0, Math.min(n - 1, y)) };
}

/**
 * Какие плитки покрывают видимое.
 *
 * Углы холста, а не весь диск: под нужным приближением видно квартал, и
 * спрашивать полушарие незачем. Точка за краем шара (`obratno` вернул `null`)
 * заменяется центром — иначе на самом краю список оказывался бы пустым.
 */
export function vidimye(vid: Vid, shirina: number, vysota: number): Array<{ x: number; y: number }> {
  const ugly: Array<{ lon: number; lat: number }> = [];
  for (const [x, y] of [
    [0, 0],
    [shirina, 0],
    [0, vysota],
    [shirina, vysota],
    [shirina / 2, vysota / 2],
  ]) {
    ugly.push(obratno(x, y, vid) ?? { lon: vid.lon0, lat: vid.lat0 });
  }
  const lony = ugly.map((u) => u.lon);
  const laty = ugly.map((u) => u.lat);
  const levo = nomer(Math.min(...lony), Math.max(...laty));
  const pravo = nomer(Math.max(...lony), Math.min(...laty));

  const itog: Array<{ x: number; y: number }> = [];
  for (let y = levo.y; y <= pravo.y && itog.length < PREDEL_PLITOK; y++) {
    for (let x = levo.x; x <= pravo.x && itog.length < PREDEL_PLITOK; x++) {
      itog.push({ x, y });
    }
  }
  return itog;
}

/** Ключ плитки для хранилища и для сравнения списков. */
export function klyuch(plitka: { x: number; y: number }): string {
  return `${PLITKA_Z}/${plitka.x}/${plitka.y}`;
}

/** Разности обратно в градусы. Первое число дороги — её вид, не координата. */
export function razvernut(upakovannoe: readonly number[], s = 0): number[] {
  const tochki: number[] = [];
  let lon = 0;
  let lat = 0;
  for (let i = s; i < upakovannoe.length; i += 2) {
    lon += upakovannoe[i];
    lat += upakovannoe[i + 1];
    tochki.push(lon / TOCHNOST, lat / TOCHNOST);
  }
  return tochki;
}
