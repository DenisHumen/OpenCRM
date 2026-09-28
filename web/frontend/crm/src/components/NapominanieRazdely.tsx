import { useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "./Icon";
import { PovtorRedaktor } from "./PovtorRedaktor";
import { putBumagi } from "./StrokaNapominaniya";
import { VyborKlienta } from "./VyborKlienta";
import { KnopkaKorziny, LoadFailed } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { kindLabel } from "../lib/documents";
import { formatDateTime, parseDate } from "../lib/format";
import { moduleOn } from "../lib/modules";
import { useGuard } from "../lib/guard";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";

/** Ранние оповещения на выбор — как у календаря телефона, без «своего» поля:
 *  пять вариантов закрывают все случаи из разбора платформ (docs/bloki/29 §2). */
const OPOVESHENIYA = [
  [0, "zvonokVSrok"],
  [5, "zvonokZa5"],
  [15, "zvonokZa15"],
  [60, "zvonokZa60"],
  [1440, "zvonokZa1440"],
  [10080, "zvonokZa10080"],
] as const;
const NASTOYCHIVO = [5, 10, 15, 30, 60];

type Pravit = (data: Record<string, unknown>) => Promise<void> | void;

const VID_SOBYTIYA = {
  created: "istoriyaCreated",
  done: "istoriyaDone",
  reopened: "istoriyaReopened",
  skipped: "istoriyaSkipped",
  snoozed: "istoriyaSnoozed",
  assigned: "istoriyaAssigned",
} as const;

function minuty(opovesheniya: string): number[] {
  return opovesheniya ? opovesheniya.split(",").filter(Boolean).map(Number) : [];
}

/** «Когда»: весь день, повтор, оповещения и настойчивость. */
export function RazdelKogda({ task, pravit, mozhno }: { task: any; pravit: Pravit; mozhno: boolean }) {
  const { t } = useApp();
  const est = minuty(task.opovesheniya);
  return (
    <div className="napom-razdel">
      <div className="metric-title">
        <Icon name="clock" size={13} />
        {t("napomKogda")}
      </div>
      <div className="napom-stroka">
        <label className="napom-galka">
          <input
            type="checkbox"
            checked={task.ves_den}
            disabled={!mozhno || !task.due_at}
            onChange={(e) => void pravit({ ves_den: e.target.checked, due_at: task.due_at })}
          />
          {t("napomVesDen")}
        </label>
        <span className="napom-poyas" title={t("napomPoyas")}>
          <Icon name="globe" size={12} /> {task.poyas}
        </span>
      </div>

      <div className="label">{t("napomPovtor")}</div>
      <PovtorRedaktor
        povtor={task.povtor}
        posle={task.povtor_posle}
        srok={parseDate(task.due_at)}
        disabled={!mozhno || !task.due_at}
        onChange={(povtor, posle) => void pravit({ povtor: povtor ?? "", povtor_posle: posle, due_at: task.due_at })}
      />

      <div className="label">{t("napomZvonki")}</div>
      <div className="napom-chipy" role="group" aria-label={t("napomZvonki")}>
        {OPOVESHENIYA.map(([minut, podpis]) => {
          const vybrano = est.includes(minut);
          return (
            <button
              key={minut}
              type="button"
              aria-pressed={vybrano}
              disabled={!mozhno}
              className={"option-chip" + (vybrano ? " active" : "")}
              onClick={() => {
                const novye = vybrano ? est.filter((m) => m !== minut) : [...est, minut];
                void pravit({ opovesheniya: novye.join(",") });
              }}
            >
              {t(podpis)}
            </button>
          );
        })}
      </div>
      <label className="napom-stroka">
        <span>{t("zvonokNastoychivo")}</span>
        <select
          className="input napom-uzkiy"
          value={task.nastoychivo ?? 0}
          disabled={!mozhno}
          onChange={(e) => void pravit({ nastoychivo: Number(e.target.value) || null })}
        >
          <option value={0}>{t("nastoychivoNet")}</option>
          {NASTOYCHIVO.map((n) => (
            <option key={n} value={n}>
              {t("nastoychivoKazhdye", { n })}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

/** «Люди»: кому звонит, кто наблюдает, общая полка, «каждому своё». */
export function RazdelLyudi({
  task,
  pravit,
  onRazoslano,
}: {
  task: any;
  pravit: Pravit;
  onRazoslano: () => void;
}) {
  const { t, user, toastError } = useApp();
  const lyudi = useReference<{ id: number; name: string }>("/people");
  const guard = useGuard();
  const [komu, setKomu] = useState("");
  const [kto, setKto] = useState("");
  const vse = can(user, "tasks.view_others");
  const vladelets = vse || task.obshchee || (task.lyudi ?? []).some((c: any) => c.user_id === user?.id && c.vladelets);
  const mozhnoStavit = vladelets && can(user, "tasks.edit") && can(user, "tasks.assign");
  const poluchateli = (task.lyudi ?? []).filter((c: any) => c.poluchaet);
  const nablyudateli = (task.lyudi ?? []).filter((c: any) => !c.poluchaet && !c.vladelets);
  const zanyato = new Set((task.lyudi ?? []).filter((c: any) => c.poluchaet || !c.vladelets).map((c: any) => c.user_id));
  const svobodnye = (lyudi.items ?? []).filter((p) => !zanyato.has(p.id));

  const zadat = (spisok: { poluchateli?: number[]; nablyudateli?: number[] }) =>
    void pravit({
      poluchateli: spisok.poluchateli ?? poluchateli.map((c: any) => c.user_id),
      nablyudateli: spisok.nablyudateli ?? nablyudateli.map((c: any) => c.user_id),
    });

  // «Каждому своё» для уже заведённого: копия каждому получателю, а общее — долой.
  const razoslat = async () => {
    if (!guard.take()) return;
    try {
      await api.post("/tasks", {
        title: task.title,
        vazhnost: task.vazhnost,
        due_at: task.due_at,
        ves_den: task.ves_den,
        poyas: task.poyas,
        povtor: task.povtor,
        povtor_posle: task.povtor_posle,
        opovesheniya: task.opovesheniya,
        nastoychivo: task.nastoychivo,
        client_id: task.client_id,
        deal_id: task.deal_id,
        document_id: task.document_id,
        product_id: task.product_id,
        board_id: task.board_id,
        poluchateli: poluchateli.map((c: any) => c.user_id),
        kazhdomu: true,
      });
      await api.del(`/tasks/${task.id}`);
      onRazoslano();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const vzyat = async () => {
    if (!guard.take()) return;
    try {
      await api.post(`/tasks/${task.id}/take`);
      onRazoslano();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const chelovek = (c: any, spisok: "poluchateli" | "nablyudateli") => (
    <span key={c.user_id} className="napom-chelovek">
      <Icon name="user" size={11} />
      {c.name}
      {c.vladelets && <span className="napom-rol">· {t("napomVladelets")}</span>}
      {mozhnoStavit && !(spisok === "poluchateli" && poluchateli.length === 1 && c.user_id === user?.id) && (
        <button
          type="button"
          className="napom-snyat"
          aria-label="×"
          onClick={() =>
            zadat({ [spisok]: (spisok === "poluchateli" ? poluchateli : nablyudateli).filter((x: any) => x.user_id !== c.user_id).map((x: any) => x.user_id) })
          }
        >
          ×
        </button>
      )}
    </span>
  );

  return (
    <div className="napom-razdel">
      <div className="metric-title">
        <Icon name="user" size={13} />
        {t("napomLyudi")}
      </div>
      {lyudi.failure !== null && <LoadFailed error={lyudi.failure} onRetry={lyudi.reload} />}
      <div className="label">{t("napomPoluchateli")}</div>
      <div className="napom-lyudi">
        {poluchateli.map((c: any) => chelovek(c, "poluchateli"))}
        {mozhnoStavit && svobodnye.length > 0 && (
          <select
            className="input napom-uzkiy"
            value={komu}
            aria-label={t("napomDobavitCheloveka")}
            onChange={(e) => {
              setKomu("");
              if (e.target.value) zadat({ poluchateli: [...poluchateli.map((c: any) => c.user_id), Number(e.target.value)] });
            }}
          >
            <option value="">+ {t("napomDobavitCheloveka")}</option>
            {svobodnye.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        )}
      </div>
      <div className="label">{t("napomNablyudateli")}</div>
      <div className="napom-lyudi">
        {nablyudateli.map((c: any) => chelovek(c, "nablyudateli"))}
        {mozhnoStavit && svobodnye.length > 0 && (
          <select
            className="input napom-uzkiy"
            value={kto}
            aria-label={t("napomDobavitCheloveka")}
            onChange={(e) => {
              setKto("");
              if (e.target.value) zadat({ nablyudateli: [...nablyudateli.map((c: any) => c.user_id), Number(e.target.value)] });
            }}
          >
            <option value="">+ {t("napomDobavitCheloveka")}</option>
            {svobodnye.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        )}
      </div>
      <div className="napom-stroka">
        {mozhnoStavit ? (
          <label className="napom-galka" title={t("napomObshcheeHint")}>
            <input type="checkbox" checked={task.obshchee} onChange={(e) => void pravit({ obshchee: e.target.checked })} />
            {t("napomObshchee")}
          </label>
        ) : (
          task.obshchee && <span className="napom-polka">{t("napomNaPolke")}</span>
        )}
        {task.obshchee && can(user, "tasks.edit") && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => void vzyat()}
          >
            {t("napomVzyat")}
          </button>
        )}
        {mozhnoStavit && poluchateli.length > 1 && (
          <button type="button" className="btn btn-secondary btn-sm" title={t("napomKazhdomuHint")} onClick={() => void razoslat()}>
            {t("napomKazhdomu")}
          </button>
        )}
      </div>
    </div>
  );
}

/** «Привязано к»: клиент и заявка выбираются здесь, бумага, товар и доска —
 *  приходят с их карточек кнопкой «Напомнить» и здесь только отвязываются. */
export function RazdelPrivyazki({ task, pravit, mozhno, onClose }: { task: any; pravit: Pravit; mozhno: boolean; onClose: () => void }) {
  const { t, modules } = useApp();
  const zayavki = useReference<any>(task.client_id ? `/deals?client_id=${task.client_id}&per_page=100` : null);
  const otvyazat = (kolonka: string) =>
    mozhno && (
      <button type="button" className="napom-snyat" aria-label="×" onClick={() => void pravit({ [kolonka]: null })}>
        ×
      </button>
    );
  return (
    <div className="napom-razdel">
      <div className="metric-title">
        <Icon name="link" size={13} />
        {t("napomPrivyazki")}
      </div>
      <div className="napom-privyazki">
        <label className="label">{t("client")}</label>
        {mozhno ? (
          <VyborKlienta
            value={task.client_id ?? null}
            imya={task.client_name ?? null}
            pustoy
            onPick={(kto) => void pravit({ client_id: kto, deal_id: kto === task.client_id ? task.deal_id : null })}
          />
        ) : (
          task.client_id && (
            <Link to={`/clients/${task.client_id}`} className="text-link" onClick={onClose}>
              {task.client_name || t("client")}
            </Link>
          )
        )}
        <label className="label">{t("deal")}</label>
        <div className="napom-stroka">
          <select
            className="input"
            value={task.deal_id ?? ""}
            disabled={!mozhno || !task.client_id}
            onChange={(e) => void pravit({ deal_id: e.target.value ? Number(e.target.value) : null })}
          >
            <option value="">—</option>
            {task.deal_id && !(zayavki.items ?? []).some((d: any) => d.id === task.deal_id) && (
              <option value={task.deal_id}>{task.deal_title || t("deal")}</option>
            )}
            {(zayavki.items ?? []).map((d: any) => (
              <option key={d.id} value={d.id}>
                {d.title}
              </option>
            ))}
          </select>
          {task.deal_id && (
            <Link to={`/deals/${task.deal_id}`} className="text-link" onClick={onClose} aria-label={t("deal")}>
              <Icon name="external" size={12} />
            </Link>
          )}
        </div>
        {zayavki.failure !== null && <LoadFailed error={zayavki.failure} onRetry={zayavki.reload} />}
        {task.document_id && task.document_title && (
          <div className="napom-stroka">
            <Link to={putBumagi(task.document_kind, task.document_id)} className="text-link" onClick={onClose}>
              {kindLabel(t, task.document_kind)} {task.document_title}
            </Link>
            {otvyazat("document_id")}
          </div>
        )}
        {task.product_id && task.product_name && moduleOn(modules, "warehouse") && (
          <div className="napom-stroka">
            <Link to={`/warehouse/${task.product_id}`} className="text-link" onClick={onClose}>
              {task.product_name}
            </Link>
            {otvyazat("product_id")}
          </div>
        )}
        {task.board_id && task.board_title && moduleOn(modules, "boards") && (
          <div className="napom-stroka">
            <Link to={`/boards/${task.board_id}`} className="text-link" onClick={onClose}>
              {task.board_title}
            </Link>
            {otvyazat("board_id")}
          </div>
        )}
      </div>
    </div>
  );
}

/** «Шаги»: чек-лист внутри напоминания. */
export function RazdelShagi({ task, mozhno, onChanged }: { task: any; mozhno: boolean; onChanged: () => void }) {
  const { t, toastError } = useApp();
  const [tekst, setTekst] = useState("");
  const guard = useGuard();
  const zapros = async (put: Promise<unknown>) => {
    try {
      await put;
      onChanged();
    } catch (e) {
      toastError(e);
    }
  };
  const dobavit = async (text: string) => {
    if (!guard.take()) return;
    try {
      await api.post(`/tasks/${task.id}/steps`, { text });
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };
  return (
    <div className="napom-razdel">
      <div className="metric-title">
        <Icon name="check" size={13} />
        {t("napomShagi")}
      </div>
      {(task.shagi_spisok ?? []).map((s: any) => (
        <div key={s.id} className="napom-shag">
          <input
            type="checkbox"
            checked={s.sdelan}
            disabled={!mozhno}
            aria-label={s.text}
            onChange={(e) => void zapros(api.patch(`/tasks/${task.id}/steps/${s.id}`, { sdelan: e.target.checked }))}
          />
          <span className={s.sdelan ? "napom-shag-sdelan" : undefined}>{s.text}</span>
          {mozhno && <KnopkaKorziny onClick={() => void zapros(api.del(`/tasks/${task.id}/steps/${s.id}`))} />}
        </div>
      ))}
      {mozhno && (
        <input
          className="input"
          placeholder={t("napomShagDobavit")}
          value={tekst}
          onChange={(e) => setTekst(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && tekst.trim()) {
              const text = tekst.trim();
              setTekst("");
              void dobavit(text);
            }
          }}
        />
      )}
    </div>
  );
}

/** «Ссылки»: договор в облаке, страница поставщика, трекинг. */
export function RazdelSsylki({ task, mozhno, onChanged }: { task: any; mozhno: boolean; onChanged: () => void }) {
  const { t, toastError } = useApp();
  const [url, setUrl] = useState("");
  const [podpis, setPodpis] = useState("");
  const guard = useGuard();
  const dobavit = async () => {
    if (!url.trim() || !guard.take()) return;
    try {
      await api.post(`/tasks/${task.id}/links`, { url: url.trim(), title: podpis.trim() || null });
      setUrl("");
      setPodpis("");
      onChanged();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };
  return (
    <div className="napom-razdel">
      <div className="metric-title">
        <Icon name="link" size={13} />
        {t("napomSsylki")}
      </div>
      {(task.ssylki ?? []).map((s: any) => (
        <div key={s.id} className="napom-shag">
          <Icon name="external" size={12} />
          <a href={s.url} target="_blank" rel="noreferrer noopener" className="text-link truncate">
            {s.title || s.url}
          </a>
          {mozhno && (
            <KnopkaKorziny
              onClick={() => void api.del(`/tasks/${task.id}/links/${s.id}`).then(onChanged, (e) => toastError(e))}
            />
          )}
        </div>
      ))}
      {mozhno && (
        <div className="napom-stroka">
          <input className="input" placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} />
          <input
            className="input"
            placeholder={t("napomSsylkaTitle")}
            value={podpis}
            onChange={(e) => setPodpis(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void dobavit();
            }}
          />
          <button type="button" className="btn btn-secondary btn-sm" disabled={!url.trim()} onClick={() => void dobavit()}>
            {t("napomSsylkaDobavit")}
          </button>
        </div>
      )}
    </div>
  );
}

/** «История»: кто завёл, кто и какой раз закрыл, кто отложил. */
export function RazdelIstoriya({ task }: { task: any }) {
  const { t, locale } = useApp();
  if (!task.istoriya?.length) return null;
  return (
    <div className="napom-razdel">
      <div className="metric-title">
        <Icon name="refresh" size={13} />
        {t("napomIstoriya")}
      </div>
      <ul className="napom-istoriya">
        {task.istoriya.map((e: any, i: number) => (
          <li key={i}>
            <span className="napom-istoriya-kto">{e.kto || "—"}</span> {t(VID_SOBYTIYA[e.vid as keyof typeof VID_SOBYTIYA] ?? "istoriyaAssigned")}
            {e.srok && e.vid !== "created" && <span className="napom-istoriya-srok"> · {formatDateTime(e.srok, locale)}</span>}
            <span className="napom-istoriya-kogda">{formatDateTime(e.created_at, locale)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
