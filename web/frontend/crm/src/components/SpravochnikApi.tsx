import { useMemo, useState, type ReactNode } from "react";

import { Icon } from "./Icon";
import { useApp } from "../lib/app";
import { SPRAVOCHNIK_API, type Ruchka } from "../lib/spravochnik_api";

/** Ручки API сайта одним списком с поиском.
 *
 * Список не пишется руками: он порождён из docs/osnovy/04-api.md скриптом
 * scripts/spravochnik_api.py, и свежесть его стережёт тест. Здесь только
 * отбор и раскладка по разделам справочника.
 */
export function SpravochnikApi() {
  const { t } = useApp();
  const [iskat, setIskat] = useState("");
  const [otkrytye, setOtkrytye] = useState<ReadonlySet<string>>(() => new Set());

  const nayden = iskat.trim().toLowerCase();
  const vsego = useMemo(() => SPRAVOCHNIK_API.reduce((s, r) => s + r.ruchki.length, 0), []);
  const razdely = useMemo(
    () =>
      SPRAVOCHNIK_API.map((r) => ({
        ...r,
        ruchki: r.ruchki.filter((h) => !nayden || podkhodit(r.nazvanie, h, nayden)),
      })).filter((r) => r.ruchki.length > 0),
    [nayden],
  );
  const pokazano = razdely.reduce((s, r) => s + r.ruchki.length, 0);

  const pereklyuchit = (imya: string) =>
    setOtkrytye((bylo) => {
      const stalo = new Set(bylo);
      if (stalo.has(imya)) stalo.delete(imya);
      else stalo.add(imya);
      return stalo;
    });

  return (
    <div className="docs-ref">
      <div className="docs-ref-head">
        <div className="docs-search">
          <Icon name="search" size={14} />
          <input
            value={iskat}
            onChange={(e) => setIskat(e.target.value)}
            placeholder={t("apiRefSearch")}
          />
        </div>
        <span className="docs-ref-count">{t("apiRefShown", { shown: pokazano, total: vsego })}</span>
      </div>
      {razdely.length === 0 && <div className="field-desc">{t("nothingFound", { q: iskat })}</div>}
      {razdely.map((r) => {
        // При поиске раскрыто всё: свёрнутая находка — не находка.
        const raskryt = !!nayden || otkrytye.has(r.nazvanie);
        return (
          <div key={r.nazvanie} className={"docs-ref-group" + (raskryt ? " open" : "")}>
            <button
              type="button"
              className="docs-ref-group-head"
              aria-expanded={raskryt}
              onClick={() => pereklyuchit(r.nazvanie)}
            >
              <Icon name="chevronDown" size={13} className="docs-fold-chevron" />
              <span className="docs-ref-group-name">{razmetka(r.nazvanie)}</span>
              <span className="docs-ref-group-n">{r.ruchki.length}</span>
            </button>
            {raskryt && (
              <div className="docs-ref-rows">
                {r.ruchki.map((h, i) => (
                  <Stroka key={h.metod + h.put + i} ruchka={h} pred={i > 0 ? r.ruchki[i - 1] : null} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Stroka({ ruchka, pred }: { ruchka: Ruchka; pred: Ruchka | null }) {
  const { t } = useApp();
  const polnyyPut = (ruchka.vne_api ? "" : "/api/v1") + ruchka.put;
  return (
    <>
      {ruchka.podrazdel && ruchka.podrazdel !== (pred?.podrazdel ?? "") && (
        <div className="docs-ref-sub">{razmetka(ruchka.podrazdel)}</div>
      )}
      <div className="docs-ref-row">
        <div className="docs-ref-line">
          <span className={"docs-method m-" + ruchka.metod.toLowerCase()}>{ruchka.metod}</span>
          <code className="docs-ref-path">{polnyyPut}</code>
          <Dostup ruchka={ruchka} t={t} />
        </div>
        <div className="docs-ref-desc">{razmetka(ruchka.opisanie)}</div>
      </div>
    </>
  );
}

function Dostup({ ruchka, t }: { ruchka: Ruchka; t: ReturnType<typeof useApp>["t"] }) {
  if (ruchka.vid === "otkryto")
    return <span className="docs-ref-access a-otkryto">{t("apiRefPublic")}</span>;
  if (ruchka.vid === "sotrudnik")
    return <span className="docs-ref-access">{t("apiRefStaff")}</span>;
  if (ruchka.vid === "pravo")
    return <span className="docs-ref-access"><code>{ruchka.dostup}</code></span>;
  if (ruchka.vid === "klyuch")
    return (
      <span className="docs-ref-access">
        {t("apiRefKey")} <code>{ruchka.dostup}</code>
      </span>
    );
  return <span className="docs-ref-access">{razmetka(ruchka.dostup)}</span>;
}

function podkhodit(razdel: string, h: Ruchka, nayden: string): boolean {
  return (
    h.put.toLowerCase().includes(nayden) ||
    h.metod.toLowerCase().includes(nayden) ||
    h.opisanie.toLowerCase().includes(nayden) ||
    h.dostup.toLowerCase().includes(nayden) ||
    razdel.toLowerCase().includes(nayden)
  );
}

/* Текст из справочника несёт разметку markdown: `код`, **жирный** и ссылки на
   документы. Ссылки внутри приложения вести некуда, от них остаётся подпись. */
function razmetka(tekst: string): ReactNode[] {
  const chisto = tekst
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1");
  return chisto.split(/(`[^`]*`)/g).map((kusok, i) =>
    kusok.startsWith("`") && kusok.endsWith("`") && kusok.length > 1
      ? <code key={i}>{kusok.slice(1, -1)}</code>
      : <span key={i}>{kusok}</span>,
  );
}
