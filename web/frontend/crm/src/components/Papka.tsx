/** Папка, раскрывающаяся по наведению: задник, три листа, передняя стенка.
 *  Перевод uiverse.io/Cobp/mighty-pig-13 — docs/18. Чисто картинка: без
 *  роли и без имени, чтобы читалка не объявляла пять пустых блоков. */
export function Papka() {
  return (
    <div className="folder" aria-hidden="true">
      <div className="folder-back" />
      <div className="folder-list folder-l1" />
      <div className="folder-list folder-l2" />
      <div className="folder-list folder-l3" />
      <div className="folder-front" />
    </div>
  );
}
