/** Папка, раскрывающаяся по наведению: задник, три листа, передняя стенка.
 *  Перевод uiverse.io/Cobp/mighty-pig-13 — docs/18. Чисто картинка: без
 *  роли и без имени, чтобы читалка не объявляла пять пустых блоков. */
export function Papka() {
  return (
    <div className="papka" aria-hidden="true">
      <div className="papka-zad" />
      <div className="papka-list papka-l1" />
      <div className="papka-list papka-l2" />
      <div className="papka-list papka-l3" />
      <div className="papka-pered" />
    </div>
  );
}
