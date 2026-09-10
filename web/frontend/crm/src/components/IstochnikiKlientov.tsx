import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { LoadFailed, Spinner } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { useLiveTopic } from "../lib/live";
import { sourceLabel } from "../lib/sources";

/**
 * Откуда пришли клиенты — виджет сводки: кольцо долей и легенда.
 *
 * Данные у отчёта (`/reports/sources`) по той же причине, что у «Потока
 * денег»: считать источники за год на каждой загрузке сводки — платить за них
 * всем. Деньги здесь не показываются вовсе, поэтому и право отчётное, но без
 * сумм: вопрос блока — «откуда приходят», а не «сколько приносят».
 */

interface StrokaIstochnika {
  source: string | null;
  clients: number;
}

interface OtvetIstochnikov {
  items: StrokaIstochnika[];
  clients_total: number;
}

/** Цвета долей. Пять своих токенов по кругу — шестой источник берёт первый
 *  цвет снова, и это лучше, чем считать оттенок из имени: посчитанный цвет
 *  однажды совпал бы с фоном. */
const TSVETA = ["var(--brand)", "var(--accent)", "var(--violet)", "var(--teal)", "var(--success)"];

/** Сколько долей рисуем отдельно. Остальные складываются в «прочее»: кольцо из
 *  двадцати волосков не отвечает ни на один вопрос. */
const DOLEY = 5;

const RADIUS = 42;
const DLINA = 2 * Math.PI * RADIUS;

function isoDen(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${String(d.getDate()).padStart(2, "0")}`;
}

export function IstochnikiKlientov() {
  const { t } = useApp();
  const [data, setData] = useState<OtvetIstochnikov | null>(null);
  const [zapros, setZapros] = useState(0);
  const { failure, fail, clear } = useFailure();

  useLiveTopic(["clients", "deals"], () => setZapros((n) => n + 1));
  const perechitat = useCallback(() => setZapros((n) => n + 1), []);

  const stroka = useMemo(() => {
    const teper = new Date();
    return new URLSearchParams({
      from: isoDen(new Date(teper.getFullYear(), 0, 1)),
      to: isoDen(teper),
      tz_offset: String(teper.getTimezoneOffset()),
    }).toString();
  }, []);

  useEffect(() => {
    let zhivo = true;
    clear();
    api
      .get<OtvetIstochnikov>(`/reports/sources?${stroka}`)
      .then((otvet) => zhivo && setData(otvet))
      .catch((beda) => zhivo && fail(beda));
    return () => {
      zhivo = false;
    };
  }, [stroka, zapros, fail, clear]);

  const doli = useMemo(() => {
    if (!data) return [];
    // Источник без клиентов в кольцо не идёт: нулевая дуга — это ничего, а
    // строка легенды к ней — место, занятое пустотой.
    const est = data.items.filter((x) => x.clients > 0).sort((a, b) => b.clients - a.clients);
    const vidnye = est.slice(0, DOLEY);
    const ostalnye = est.slice(DOLEY).reduce((s, x) => s + x.clients, 0);
    const spisok = vidnye.map((x, i) => ({
      imya: sourceLabel(x.source, t),
      skolko: x.clients,
      tsvet: TSVETA[i % TSVETA.length],
    }));
    if (ostalnye > 0) {
      spisok.push({ imya: t("srcRest"), skolko: ostalnye, tsvet: "var(--faint)" });
    }
    return spisok;
  }, [data, t]);

  if (failure) return <LoadFailed error={failure} onRetry={perechitat} />;
  if (!data) {
    return (
      <div className="potok-zhdyom">
        <Spinner />
      </div>
    );
  }

  const vsego = doli.reduce((s, x) => s + x.skolko, 0);
  let proydeno = 0;

  return (
    <div className="istochniki">
      {vsego === 0 ? (
        <div className="istochniki-pusto">{t("srcNobody")}</div>
      ) : (
        <>
          <div className="istochniki-koltso">
            <svg viewBox="0 0 100 100" aria-hidden="true">
              <circle cx="50" cy="50" r={RADIUS} className="istochniki-fon" />
              {doli.map((d) => {
                const dolya = d.skolko / vsego;
                const duga = (
                  <circle
                    key={d.imya}
                    cx="50"
                    cy="50"
                    r={RADIUS}
                    className="istochniki-dolya"
                    stroke={d.tsvet}
                    strokeDasharray={`${(dolya * DLINA).toFixed(2)} ${DLINA.toFixed(2)}`}
                    strokeDashoffset={(-proydeno * DLINA).toFixed(2)}
                  />
                );
                proydeno += dolya;
                return duga;
              })}
            </svg>
            <div className="istochniki-tsentr">
              <div className="istochniki-vsego">{data.clients_total}</div>
              <div className="istochniki-podpis">{t("srcTotalClients")}</div>
            </div>
          </div>
          <div className="istochniki-legenda">
            {doli.map((d) => (
              <div className="istochniki-stroka" key={d.imya}>
                <span className="istochniki-metka" style={{ background: d.tsvet }} />
                <span className="truncate istochniki-imya">{d.imya}</span>
                <span className="istochniki-chislo">{d.skolko}</span>
              </div>
            ))}
          </div>
        </>
      )}
      <Link to="/reports" className="btn btn-secondary btn-sm istochniki-hod">
        {t("srcReport")}
      </Link>
    </div>
  );
}
