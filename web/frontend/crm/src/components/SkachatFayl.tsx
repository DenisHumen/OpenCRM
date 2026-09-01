import { fileSize } from "../lib/format";
import { Icon } from "./Icon";

/**
 * Ссылка «скачать» с размером файла.
 *
 * Размер — не украшение: до нажатия человек решает, качать ли сейчас, и по
 * телефону это решение стоит трафика. Показывается он подсказкой снизу, а не
 * строкой рядом: в подвале работы и без него тесно, а нужен он ровно в тот
 * миг, когда курсор уже на кнопке.
 *
 * Скачивание — обычной ссылкой, а не запросом через fetch: имя файла, прогресс
 * и папку «Загрузки» браузер делает сам и лучше.
 *
 * Образец движения — uiverse.io/EcheverriaJesus/chatty-lizard-18.
 */
export function SkachatFayl({
  href,
  bytes,
  label,
}: {
  href: string;
  bytes: number;
  label: string;
}) {
  return (
    <a className="skachat" href={href} aria-label={label} title={label}>
      <Icon name="download" size={13} />
      {bytes > 0 && <span className="skachat-razmer">{fileSize(bytes)}</span>}
    </a>
  );
}
