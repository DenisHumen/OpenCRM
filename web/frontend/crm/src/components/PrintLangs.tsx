import { Icon } from "./Icon";
import { useApp } from "../lib/app";

/** Языки печати. Бумагу печатают под КЛИЕНТА, а не под сотрудника: приехал
 *  турист — печатаем по-английски, ничего в базе не меняя. */
const PRINT_LANGS = [
  { id: "ru", label: "Рус" },
  { id: "en", label: "Eng" },
  { id: "uk", label: "Укр" },
];

/** Ряд кнопок печати с выбором языка бумаги.
 *
 * Общий на квитанцию, акт и накладную: список языков, разъехавшийся между
 * формами, дал бы бумагу, которую по-украински печатает одна из трёх.
 */
export function PrintLangs({ base, current }: { base: string; current: string }) {
  const { t } = useApp();
  return (
    <div className="print-actions">
      <span className="print-label">
        <Icon name="printer" size={14} />
        {t("docPrint")}
      </span>
      {PRINT_LANGS.map((lang) => (
        <a
          key={lang.id}
          className={"btn btn-secondary btn-sm" + (lang.id === current ? " btn-current" : "")}
          href={`${base}?locale=${lang.id}`}
          target="_blank"
          rel="noreferrer"
        >
          {lang.label}
        </a>
      ))}
    </div>
  );
}
