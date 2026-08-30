import { useMemo, useState } from "react";

import { CopyButton } from "../components/CopyButton";
import { Icon } from "../components/Icon";
import { useApp } from "../lib/app";
import { RUKOVODSTVO, type Kusok, type Yazyk } from "../lib/rukovodstvo";

/** Руководство по продукту, внутри самого продукта.
 *
 * Внутри, а не ссылкой наружу: человек, у которого вопрос, уже сидит в системе,
 * и отправлять его читать в другое место — это отправлять его закрывать вкладку.
 */
export function Docs() {
  const { t, locale } = useApp();
  const yaz: Yazyk = locale === "ru" ? "ru" : "en";
  const [razdelId, setRazdelId] = useState(RUKOVODSTVO[0].id);
  const [iskat, setIskat] = useState("");

  const razdel = useMemo(
    () => RUKOVODSTVO.find((r) => r.id === razdelId) ?? RUKOVODSTVO[0],
    [razdelId],
  );

  // Поиск идёт по названию и короткому описанию: полнотекстовый по всему
  // руководству дал бы совпадения в середине абзаца, куда всё равно не
  // перепрыгнуть.
  const nayden = iskat.trim().toLowerCase();
  const statyi = razdel.statyi.filter(
    (s) =>
      !nayden ||
      s.nazvanie[yaz].toLowerCase().includes(nayden) ||
      s.kratko[yaz].toLowerCase().includes(nayden),
  );

  return (
    <div className="page page-wide">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("documentation")}</h1>
          <div className="page-sub">{t("docsSub")}</div>
        </div>
        <div className="docs-search">
          <Icon name="search" size={14} />
          <input
            value={iskat}
            onChange={(e) => setIskat(e.target.value)}
            placeholder={t("search")}
          />
        </div>
      </div>

      <div className="docs-body">
        <nav className="docs-rail">
          {RUKOVODSTVO.map((r) => (
            <button
              key={r.id}
              type="button"
              className={"docs-rail-item" + (r.id === razdel.id ? " active" : "")}
              onClick={() => setRazdelId(r.id)}
            >
              <Icon name={r.znachok} size={15} />
              <span>{r.nazvanie[yaz]}</span>
            </button>
          ))}
        </nav>

        {/* `key` по разделу — чтобы появление проигрывалось заново при каждом
            переключении: без него React переиспользует узлы и движения нет. */}
        <div className="docs-content" key={razdel.id}>
          {statyi.length === 0 && <div className="field-desc">{t("nothingFound", { q: iskat })}</div>}
          {statyi.map((s, i) => (
            <article className="docs-article" key={s.id} style={{ animationDelay: `${i * 60}ms` }}>
              <h2>{s.nazvanie[yaz]}</h2>
              <p className="docs-lead">{s.kratko[yaz]}</p>
              {s.kuski.map((kusok, j) => (
                <Blok key={j} kusok={kusok} yaz={yaz} />
              ))}
            </article>
          ))}
        </div>
      </div>
    </div>
  );
}

/* Пример из руководства нужен в терминале, а выделять мышью многострочный
   запрос — промахиваться по краям. */
function Kod({ tekst }: { tekst: string }) {
  return (
    <div className="docs-code-wrap">
      <pre className="docs-code">
        <code>{tekst}</code>
      </pre>
      <CopyButton text={tekst} />
    </div>
  );
}

function Blok({ kusok, yaz }: { kusok: Kusok; yaz: Yazyk }) {
  if (kusok.vid === "abzats") return <p>{kusok.tekst[yaz]}</p>;

  if (kusok.vid === "spisok")
    return (
      <ul className="docs-list">
        {kusok.punkty.map((p, i) => (
          <li key={i}>{p[yaz]}</li>
        ))}
      </ul>
    );

  if (kusok.vid === "shagi")
    return (
      <ol className="docs-steps">
        {kusok.punkty.map((p, i) => (
          <li key={i}>
            <span className="docs-step-no">{i + 1}</span>
            <span>{p[yaz]}</span>
          </li>
        ))}
      </ol>
    );

  if (kusok.vid === "vazhno")
    return (
      <div className="docs-note">
        <Icon name="info" size={14} />
        <span>{kusok.tekst[yaz]}</span>
      </div>
    );

  if (kusok.vid === "kod") return <Kod tekst={kusok.tekst} />;

  // Ручка API. Поля таблицей, запрос и ответ примерами — как у взрослых
  // проектов: без примера описание поля читается, а повторить его нельзя.
  return (
    <div className="docs-endpoint">
      <div className="docs-endpoint-head">
        <span className={"docs-method m-" + kusok.metod.toLowerCase()}>{kusok.metod}</span>
        <code>{kusok.put}</code>
      </div>
      <p>{kusok.opisanie[yaz]}</p>
      {!!kusok.polya?.length && (
        <table className="docs-fields">
          <thead>
            <tr>
              <th>{yaz === "ru" ? "Поле" : "Field"}</th>
              <th>{yaz === "ru" ? "Тип" : "Type"}</th>
              <th>{yaz === "ru" ? "Описание" : "Description"}</th>
            </tr>
          </thead>
          <tbody>
            {kusok.polya.map((p) => (
              <tr key={p.imya}>
                <td>
                  <code>{p.imya}</code>
                  {p.obyazatelno && <span className="docs-req">*</span>}
                </td>
                <td className="docs-type">{p.tip}</td>
                <td>{p.opisanie[yaz]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {kusok.zapros && (
        <>
          <div className="docs-code-label">{yaz === "ru" ? "Запрос" : "Request"}</div>
          <Kod tekst={kusok.zapros} />
        </>
      )}
      {kusok.otvet && (
        <>
          <div className="docs-code-label">{yaz === "ru" ? "Ответ" : "Response"}</div>
          <Kod tekst={kusok.otvet} />
        </>
      )}
    </div>
  );
}
