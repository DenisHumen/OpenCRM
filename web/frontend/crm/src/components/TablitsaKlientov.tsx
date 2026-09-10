import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "./Icon";
import { Avatar, LoadFailed, Spinner } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDate, initials } from "../lib/format";
import { useLiveTopic } from "../lib/live";
import type { TranslationKey } from "../lib/i18n";
import { sourceLabel } from "../lib/sources";

/**
 * Клиенты таблицей — виджет сводки со своей панелью: поиск, отбор, выгрузка.
 *
 * Состояние клиента НЕ хранится и не приходит с сервера отдельным полем: его
 * видно по заявкам, которые у него есть. «В работе» — есть открытая, «спит» —
 * были выигранные, но сейчас ничего, «без заявок» — не было ни одной. Хранить
 * такое поле значило бы завести второй ответ на вопрос, у которого уже есть
 * первый, и однажды получить два разных.
 */

type Sostoyanie = "work" | "sleep" | "none";

interface Klient {
  id: number;
  name: string;
  company: string | null;
  email: string | null;
  source: string | null;
  created_at: string;
  deals_open: number;
  deals_won: number;
}

const OTBORY: { klyuch: Sostoyanie | "all"; podpis: TranslationKey }[] = [
  { klyuch: "all", podpis: "clientsAll" },
  { klyuch: "work", podpis: "clientsInWork" },
  { klyuch: "sleep", podpis: "clientsAsleep" },
  { klyuch: "none", podpis: "clientsNoDeals" },
];

function sostoyanie(k: Klient): Sostoyanie {
  if (k.deals_open > 0) return "work";
  return k.deals_won > 0 ? "sleep" : "none";
}

export function TablitsaKlientov({ strok }: { strok: number }) {
  const { t, locale } = useApp();
  const [data, setData] = useState<{ items: Klient[]; total: number } | null>(null);
  const [zapros, setZapros] = useState(0);
  const [poisk, setPoisk] = useState("");
  const [otbor, setOtbor] = useState<Sostoyanie | "all">("all");
  const { failure, fail, clear } = useFailure();

  useLiveTopic(["clients", "deals"], () => setZapros((n) => n + 1));
  const perechitat = useCallback(() => setZapros((n) => n + 1), []);

  // Строк берём с запасом на отбор: отбирать по состоянию на экране из тех
  // тридцати, что пришли, — единственный честный способ, пока сервер такого
  // отбора не знает. Запас, а не вся база: страница у списка своя.
  const skolko = Math.min(200, Math.max(10, strok * 3));

  useEffect(() => {
    let zhivo = true;
    clear();
    api
      .get<{ items: Klient[]; total: number }>(`/clients?page=1&per_page=${skolko}`)
      .then((otvet) => zhivo && setData(otvet))
      .catch((beda) => zhivo && fail(beda));
    return () => {
      zhivo = false;
    };
  }, [skolko, zapros, fail, clear]);

  const stroki = useMemo(() => {
    if (!data) return [];
    const slovo = poisk.trim().toLowerCase();
    return data.items
      .filter((k) => otbor === "all" || sostoyanie(k) === otbor)
      .filter(
        (k) =>
          !slovo ||
          [k.name, k.company, k.email].some((x) => (x ?? "").toLowerCase().includes(slovo)),
      )
      .slice(0, strok);
  }, [data, otbor, poisk, strok]);

  if (failure) return <LoadFailed error={failure} onRetry={perechitat} />;
  if (!data) {
    return (
      <div className="potok-zhdyom">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="klienty-vidzhet">
      <div className="klienty-panel">
        <label className="klienty-poisk">
          <Icon name="search" size={13} />
          <input
            className="input input-sm"
            value={poisk}
            placeholder={t("findClient")}
            aria-label={t("findClient")}
            onChange={(e) => setPoisk(e.target.value)}
          />
        </label>
        <div className="klienty-chipy">
          {OTBORY.map((o) => (
            <button
              key={o.klyuch}
              type="button"
              className={"staff-chip" + (otbor === o.klyuch ? " staff-chip-on" : " ")}
              onClick={() => setOtbor(o.klyuch)}
            >
              {t(o.podpis)}
            </button>
          ))}
        </div>
        <a className="btn btn-secondary btn-sm klienty-vygruzka" href="/api/v1/clients/export.csv">
          <Icon name="download" size={13} />
          {t("exportCsv")}
        </a>
      </div>

      {stroki.length === 0 ? (
        <div className="klienty-pusto">{t("clientsNothingFound")}</div>
      ) : (
        <div className="klienty-svitok">
          <div className="klienty-setka">
            <div className="klienty-th">{t("colClientNo")}</div>
            <div className="klienty-th">{t("client")}</div>
            <div className="klienty-th">{t("colEmail")}</div>
            <div className="klienty-th">{t("colState")}</div>
            <div className="klienty-th">{t("colJoined")}</div>
            <div className="klienty-th">{t("colSource")}</div>
            {stroki.map((k) => {
              const s = sostoyanie(k);
              return (
                <Link to={`/clients/${k.id}`} key={k.id} className="klienty-tr" role="row">
                  <span className="klienty-td klienty-nomer">{String(k.id).padStart(3, "0")}</span>
                  <span className="klienty-td klienty-kto">
                    <Avatar text={initials(k.name)} />
                    <span className="truncate">{k.name}</span>
                  </span>
                  <span className="klienty-td truncate klienty-tishe">{k.email || "—"}</span>
                  <span className="klienty-td">
                    <span className={`klienty-pilyulya klienty-${s}`}>
                      {t(s === "work" ? "clientsInWork" : s === "sleep" ? "clientsAsleep" : "clientsNoDeals")}
                    </span>
                  </span>
                  <span className="klienty-td klienty-tishe">{formatDate(k.created_at, locale)}</span>
                  <span className="klienty-td truncate klienty-tishe">{sourceLabel(k.source, t)}</span>
                </Link>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
