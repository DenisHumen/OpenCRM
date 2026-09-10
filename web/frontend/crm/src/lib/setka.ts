/** Сетка сводки: место блока, тяжесть вверх, расталкивание вокруг взятого.
 *
 *  Отдельно от экрана, потому что это арифметика без React: её проверяют
 *  чтением и живой пробой, а не догадкой по разметке. Колонки и потолок задаёт
 *  сервер (`/dashboard/layout` → `grid`) — он же по ним и проверяет запись.
 */

export interface Mesto {
  /** Ключ блока: `kind` или `kind:key_id`. Совпадает с `id` виджета экрана. */
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export function nakladyvayutsya(a: Mesto, b: Mesto): boolean {
  return (
    a.i !== b.i &&
    a.x + a.w > b.x &&
    b.x + b.w > a.x &&
    a.y + a.h > b.y &&
    b.y + b.h > a.y
  );
}

/** Сверху вниз, слева направо — порядок, в котором блоки падают. */
function poMestu(spisok: Mesto[]): Mesto[] {
  return [...spisok].sort((a, b) => a.y - b.y || a.x - b.x);
}

/** Уронить всё вверх: между блоками не остаётся пустых строк. */
export function podzhat(spisok: Mesto[]): Mesto[] {
  const legli: Mesto[] = [];
  for (const blok of poMestu(spisok)) {
    const kandidat = { ...blok, y: 0 };
    while (legli.some((chuzhoy) => nakladyvayutsya(kandidat, chuzhoy))) kandidat.y += 1;
    legli.push(kandidat);
  }
  return legli;
}

/** То же, но вокруг блока, который держит рука: он стоит там, где его держат.
 *
 *  Без этого перетаскивание работало бы наоборот: взятый блок уезжал бы вверх
 *  из-под пальца, потому что тяжесть сильнее руки.
 */
export function obtech(spisok: Mesto[], derzhim: Mesto): Mesto[] {
  const legli: Mesto[] = [derzhim];
  for (const blok of poMestu(spisok.filter((b) => b.i !== derzhim.i))) {
    const kandidat = { ...blok, y: 0 };
    while (legli.some((chuzhoy) => nakladyvayutsya(kandidat, chuzhoy))) kandidat.y += 1;
    legli.push(kandidat);
  }
  return legli;
}

/** Сколько строк занимает раскладка. Нужна высота полотна: без неё страница
 *  не прокручивается до нижнего блока, потому что блоки лежат абсолютом. */
export function vysota(spisok: Mesto[]): number {
  return spisok.reduce((itog, b) => Math.max(itog, b.y + b.h), 0);
}

/** Куда положить новый блок: под всем, что уже лежит, слева. Тяжесть потом
 *  поднимет его в первую же дырку, куда он влезет. */
export function svobodnoeMesto(spisok: Mesto[]): { x: number; y: number } {
  return { x: 0, y: vysota(spisok) };
}

/** Зажать размер в границах сетки и наименьшего размера вида. */
export function vpredelah(
  mesto: Mesto,
  predel: { kolonok: number; strok: number; minW: number; minH: number },
): Mesto {
  const w = Math.min(predel.kolonok, Math.max(predel.minW, mesto.w));
  const h = Math.min(predel.strok, Math.max(predel.minH, mesto.h));
  const x = Math.min(Math.max(0, mesto.x), predel.kolonok - w);
  const y = Math.min(Math.max(0, mesto.y), predel.strok - 1);
  return { ...mesto, x, y, w, h };
}
