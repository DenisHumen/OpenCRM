import { useMemo, useState } from "react";

import { CopyButton } from "../components/CopyButton";
import { Icon } from "../components/Icon";
import { SpravochnikApi } from "../components/SpravochnikApi";
import { useApp } from "../lib/app";
import { Link } from "react-router-dom";

import { allowed } from "../lib/permissions";
import { RUKOVODSTVO, type Kusok, type Razdel, type Yazyk } from "../lib/rukovodstvo";

/** Руководство по продукту, внутри самого продукта.
 *
 * Внутри, а не ссылкой наружу: человек, у которого вопрос, уже сидит в системе,
 * и отправлять его читать в другое место — это отправлять его закрывать вкладку.
 */
export function Docs() {
  const { t, locale, user, modules } = useApp();
  const yaz: Yazyk = locale === "ru" ? "ru" : "en";
  const [iskat, setIskat] = useState("");

  // Читателю показываем только то, что у него есть. То же правило, что у меню:
  // у кого выключен склад — у того нет ни пункта, ни статьи про склад, иначе
  // руководство описывает чужую систему. Считается СНИЗУ ВВЕРХ: пустеют статьи,
  // от них пустеет раздел.
  const vidimo: Razdel[] = useMemo(
    () =>
      allowed(user, modules, RUKOVODSTVO)
        .map((r) => ({ ...r, statyi: allowed(user, modules, r.statyi) }))
        .filter((r) => r.statyi.length > 0),
    [user, modules],
  );

  const [razdelId, setRazdelId] = useState(vidimo[0]?.id ?? "");

  const razdel = useMemo(
    () => vidimo.find((r) => r.id === razdelId) ?? vidimo[0],
    [razdelId, vidimo],
  );

  // Ни одного видимого раздела не бывает — общие статьи стоят без признаков, —
  // но выключить их когда-нибудь смогут, и пустой экран лучше поломки.
  if (!razdel) return <div className="page page-docs"><div className="field-desc">{t("nothingFound", { q: "" })}</div></div>;

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
    <div className="page page-docs">
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
          {vidimo.map((r) => (
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
          {/* Оглавление раздела. Появляется от трёх статей: на двух оно длиннее
              того, что оглавляет. Якорь ведёт к статье — по такой ссылке можно
              позвать коллегу, а не объяснять ему, куда прокрутить. */}
          {statyi.length > 2 && (
            <nav className="docs-toc">
              {statyi.map((s) => (
                <a key={s.id} href={`#statya-${s.id}`} className="docs-toc-item">
                  {s.nazvanie[yaz]}
                </a>
              ))}
            </nav>
          )}
          {statyi.map((s, i) => (
            <article
              className="docs-article"
              key={s.id}
              id={`statya-${s.id}`}
              style={{ animationDelay: `${i * 60}ms` }}
            >
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

  if (kusok.vid === "vnimanie")
    return (
      <div className="docs-warn">
        <Icon name="alert" size={14} />
        <span>{kusok.tekst[yaz]}</span>
      </div>
    );

  if (kusok.vid === "kod") return <Kod tekst={kusok.tekst} />;

  if (kusok.vid === "svyortka") return <Svyortka kusok={kusok} yaz={yaz} />;

  if (kusok.vid === "spravochnik") return <SpravochnikApi />;

  if (kusok.vid === "tablitsa")
    return (
      <div className="docs-table-wrap">
        <table className="docs-fields">
          <thead>
            <tr>
              {kusok.shapka.map((h, i) => (
                <th key={i}>{h[yaz]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {kusok.ryady.map((ryad, i) => (
              <tr key={i}>
                {ryad.map((yacheyka, j) => (
                  <td key={j}>{yacheyka[yaz]}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  // Ссылка на место в системе: читатель уже внутри, и после рассказа о разделе
  // отправлять его искать этот раздел глазами — потеря половины пользы.
  if (kusok.vid === "ekran")
    return (
      <Link className="docs-screen-link" to={kusok.put}>
        <Icon name="chevronRight" size={14} />
        <span>{kusok.podpis[yaz]}</span>
      </Link>
    );

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


/** Длинный разбор под заголовком: открывается тем, кому он нужен.
 *
 * Открытое состояние НЕ помнится, в отличие от меню и списков бумаг. Там
 * человек настраивает себе рабочее место и возвращается в него каждый день, а
 * статью читают подряд: развёрнутая с прошлого раза подробность у следующего
 * вопроса оказывается посреди дороги.
 */
function Svyortka({
  kusok,
  yaz,
}: {
  kusok: Extract<Kusok, { vid: "svyortka" }>;
  yaz: Yazyk;
}) {
  const [otkryto, setOtkryto] = useState(false);
  return (
    <div className={"docs-fold" + (otkryto ? " open" : "")}>
      <button
        type="button"
        className="docs-fold-head"
        aria-expanded={otkryto}
        onClick={() => setOtkryto((bylo) => !bylo)}
      >
        <Icon name="chevronDown" size={13} className="docs-fold-chevron" />
        <span>{kusok.zagolovok[yaz]}</span>
      </button>
      {otkryto && (
        <div className="docs-fold-body">
          {kusok.kuski.map((vnutri, i) => (
            <Blok key={i} kusok={vnutri} yaz={yaz} />
          ))}
        </div>
      )}
    </div>
  );
}
