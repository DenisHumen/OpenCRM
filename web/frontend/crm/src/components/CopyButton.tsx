import { useApp } from "../lib/app";
import { copyText } from "../lib/clipboard";
import { useVspyshka } from "../lib/vspyshka";
import { Icon } from "./Icon";

/** Кнопка «скопировать» у блока кода.
 *
 * Значок меняется на галочку и возвращается назад: без ответа щелчок повторяют,
 * а на неудачном копировании (`copyText` умеет отказать) галочки не будет вовсе.
 */
export function CopyButton({ text }: { text: string }) {
  const { t } = useApp();
  const [gotovo, otmetit] = useVspyshka();

  async function nazhali() {
    if (!(await copyText(text))) return;
    otmetit();
  }

  const podpis = t(gotovo ? "copied" : "copy");
  return (
    <button
      type="button"
      className={"copy-btn" + (gotovo ? " done" : "")}
      onClick={() => void nazhali()}
      aria-label={podpis}
    >
      <span className="copy-tip">{podpis}</span>
      <Icon name={gotovo ? "check" : "clipboard"} size={15} stroke={1.8} />
    </button>
  );
}
