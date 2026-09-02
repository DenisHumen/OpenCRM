import { useState, type ReactNode } from "react";

import { Icon } from "./Icon";

/** Категория списка: ключ, подпись и сколько их всего по мнению СЕРВЕРА. */
export type Kategoriya = { key: string; label: string };

/**
 * Список, разложенный по категориям, с сворачиванием каждой.
 *
 * Заказ владельца 02.09.2026: «создать выпадающие списки по категориям и
 * возможность свернуть выпадающий список». Общий для бланков и заказов, потому
 * что вопрос у них один, а категория разная: у бланков это ВИД бумаги (их шесть,
 * и в списке они лежали вперемешку, различить нечем), у заказов — СОСТОЯНИЕ (вид
 * там выбран чипами, а «что делать сейчас» отвечает статус).
 *
 * **Заголовок говорит два числа, а не одно.** «7 из 41» — семь приехало, сорок
 * одна всего. Одно число здесь врало бы: список дочитывается сотнями, и
 * посчитанное по приехавшему — это не «сколько квитанций», а «сколько квитанций
 * попало в первую сотню». Когда дочитано всё, числа совпадают, и второе
 * пропадает само.
 *
 * **Свёрнутое состояние помнится**, иначе список схлопывался бы на каждой
 * перезагрузке. Ключ памяти — свой у каждой категории каждого экрана: общий дал
 * бы одну задвижку на все категории сразу. Приём тот же, что у меню
 * (`Sidebar.tsx`, `NavGroup`).
 *
 * **Свёртка — это про место на экране, а не про отбор.** Свёрнутая категория
 * остаётся в «всего N» и продолжает занимать место в дочитанной сотне; убрать её
 * из выдачи — дело чипов над списком, и там это видно, потому что меняется само
 * «всего». Смешать одно с другим значило бы завести способ незаметно спрятать
 * от себя данные: человек свернул «отменённые», забыл — и через месяц уверен,
 * что отменённых нет.
 */
export function SpisokPoKategoriyam<T>({
  pamyat,
  kategorii,
  stroki,
  kategoriyaStroki,
  vsego,
  render,
  klyuchStroki,
}: {
  /** Приставка ключа в localStorage: `<pamyat>:<категория>`. */
  pamyat: string;
  kategorii: Kategoriya[];
  stroki: T[];
  kategoriyaStroki: (stroka: T) => string;
  /** Сколько их всего по мнению сервера: {категория: число}. */
  vsego: Record<string, number> | undefined;
  render: (stroka: T) => ReactNode;
  klyuchStroki: (stroka: T) => string | number;
}) {
  const po_kategoriyam = new Map<string, T[]>();
  for (const stroka of stroki) {
    const klyuch = kategoriyaStroki(stroka);
    const bylo = po_kategoriyam.get(klyuch);
    if (bylo) bylo.push(stroka);
    else po_kategoriyam.set(klyuch, [stroka]);
  }

  // Порядок категорий задан списком, а не порядком приезда: иначе он менялся бы
  // от сортировки, и человек искал бы «Квитанции» каждый раз в новом месте.
  const vidimye = kategorii.filter((k) => (po_kategoriyam.get(k.key) ?? []).length > 0);
  // Категория, которой нет в перечне, всё равно показывается: вид, заведённый
  // позже перечня, обязан быть видимым, а не пропасть из списка молча.
  const chuzhie = [...po_kategoriyam.keys()]
    .filter((k) => !kategorii.some((izvestnaya) => izvestnaya.key === k))
    .map((k) => ({ key: k, label: k }));

  return (
    <>
      {[...vidimye, ...chuzhie].map((kategoriya) => (
        <Kategoriya
          key={kategoriya.key}
          klyuch={`${pamyat}:${kategoriya.key}`}
          label={kategoriya.label}
          priehalo={(po_kategoriyam.get(kategoriya.key) ?? []).length}
          vsego={vsego?.[kategoriya.key]}
        >
          {(po_kategoriyam.get(kategoriya.key) ?? []).map((stroka) => (
            <div key={klyuchStroki(stroka)}>{render(stroka)}</div>
          ))}
        </Kategoriya>
      ))}
    </>
  );
}

function Kategoriya({
  klyuch,
  label,
  priehalo,
  vsego,
  children,
}: {
  klyuch: string;
  label: string;
  priehalo: number;
  vsego: number | undefined;
  children: ReactNode;
}) {
  const [svyornuta, setSvyornuta] = useState(
    () => localStorage.getItem(klyuch) === "1",
  );

  const perevernut = () => {
    const teper = !svyornuta;
    setSvyornuta(teper);
    localStorage.setItem(klyuch, teper ? "1" : "0");
  };

  return (
    <div className={"spisok-kategoriya" + (svyornuta ? " svyornuta" : "")}>
      <button
        type="button"
        className="spisok-kategoriya-head"
        aria-expanded={!svyornuta}
        onClick={perevernut}
      >
        <Icon name="chevronDown" size={13} className="spisok-kategoriya-chevron" />
        <span className="spisok-kategoriya-imya">{label}</span>
        <span className="spisok-kategoriya-schyot">
          {vsego !== undefined && vsego > priehalo ? `${priehalo} / ${vsego}` : priehalo}
        </span>
      </button>
      {!svyornuta && children}
    </div>
  );
}
