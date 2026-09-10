import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { LoadFailed, Spinner } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatMoney, formatMoneyShort } from "../lib/format";
import { useLiveTopic } from "../lib/live";

/**
 * Поток денег — виджет сводки: оборот по месяцам кривой и рекорд месяца врезкой.
 *
 * Данные берёт у отчёта (`/reports/revenue`), а не у сводки: считать оборот за
 * год на каждой загрузке сводки — платить за него всем, включая тех, у кого
 * этого блока нет. Отсюда и право у виджета отчётное.
 *
 * Точка кривой — МЕСЯЦ, потому что отчёт складывает деньги по месяцам. Поэтому
 * и переключатель периода в месяцах: «за 30 дней» дало бы кривую из одной
 * точки, то есть не кривую.
 */

interface MesyatsDeneg {
  month: string;
  won_amount: number | null;
  received_amount: number | null;
}

interface OtvetOborota {
  currency: string;
  basis: "cash" | "deals";
  months: MesyatsDeneg[];
}

/** Сколько месяцев показываем. Год последним — на нём кривая читается лучше
 *  всего, но ждать год данных от новой фирмы неоткуда, отсюда и три месяца. */
const OKNA = [3, 6, 12] as const;
type Okno = (typeof OKNA)[number];

function isoDen(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Деньги месяца тем счётом, которым их меряет система: касса, если она есть.
 *  Два числа под одной подписью — та самая беда, ради которой в отчёте живёт
 *  `basis`; здесь мы просто спрашиваем у него. */
function summa(m: MesyatsDeneg, kassa: boolean): number {
  const znachenie = kassa ? m.received_amount : m.won_amount;
  return znachenie ?? 0;
}

export function PotokDeneg() {
  const { t, locale } = useApp();
  const [okno, setOkno] = useState<Okno>(12);
  const [data, setData] = useState<OtvetOborota | null>(null);
  const [zapros, setZapros] = useState(0);
  const [navedena, setNavedena] = useState<number | null>(null);
  const { failure, fail, clear } = useFailure();

  useLiveTopic(["deals", "finance"], () => setZapros((n) => n + 1));
  const perechitat = useCallback(() => setZapros((n) => n + 1), []);

  const zapros_stroka = useMemo(() => {
    const teper = new Date();
    const ot = new Date(teper.getFullYear(), teper.getMonth() - (okno - 1), 1);
    return new URLSearchParams({
      from: isoDen(ot),
      to: isoDen(teper),
      tz_offset: String(teper.getTimezoneOffset()),
    }).toString();
  }, [okno]);

  useEffect(() => {
    let zhivo = true;
    clear();
    api
      .get<OtvetOborota>(`/reports/revenue?${zapros_stroka}`)
      .then((otvet) => zhivo && setData(otvet))
      .catch((beda) => zhivo && fail(beda));
    return () => {
      zhivo = false;
    };
  }, [zapros_stroka, zapros, fail, clear]);

  const kassa = data?.basis === "cash";
  const tochki = useMemo(
    () => (data ? data.months.map((m) => ({ mesyats: m.month, summa: summa(m, kassa) })) : []),
    [data, kassa],
  );
  const itogo = tochki.reduce((s, x) => s + x.summa, 0);
  const maks = Math.max(1, ...tochki.map((x) => x.summa));
  const rekord = tochki.reduce<{ mesyats: string; summa: number } | null>(
    (luchshiy, x) => (luchshiy === null || x.summa > luchshiy.summa ? x : luchshiy),
    null,
  );
  // Отклонение — последний месяц к предыдущему, и подписано именно так. Итог
  // периода к «такому же прошлому» потребовал бы второго запроса, а называть
  // одно другим — тот же обман, что и два числа под одной подписью.
  const posledniy = tochki.length > 0 ? tochki[tochki.length - 1].summa : 0;
  const predydushchiy = tochki.length > 1 ? tochki[tochki.length - 2].summa : null;
  const otklonenie =
    predydushchiy === null || predydushchiy === 0
      ? null
      : Math.round(((posledniy - predydushchiy) / predydushchiy) * 100);

  const nazvanieMesyatsa = (iso: string, dlinnoe = false) =>
    new Date(`${iso}-01T00:00:00`).toLocaleDateString(locale === "ru" ? "ru-RU" : "en-US", {
      month: dlinnoe ? "long" : "short",
      year: dlinnoe ? "numeric" : "2-digit",
    });

  if (failure) return <LoadFailed error={failure} onRetry={perechitat} />;
  if (!data) {
    return (
      <div className="potok-zhdyom">
        <Spinner />
      </div>
    );
  }

  // Кривая рисуется в своей системе координат 0…100 по обеим осям и тянется
  // за виджетом: `preserveAspectRatio="none"` — тот же приём, что у полосок
  // просмотров, и он избавляет от пересчёта на каждом изменении размера.
  const shag = tochki.length > 1 ? 100 / (tochki.length - 1) : 0;
  const koordinaty = tochki.map((x, i) => ({
    x: tochki.length > 1 ? i * shag : 50,
    y: 100 - (x.summa / maks) * 88 - 6,
  }));
  const liniya = koordinaty.map((k) => `${k.x.toFixed(2)},${k.y.toFixed(2)}`).join(" ");
  const zalivka = `0,100 ${liniya} 100,100`;
  const setka = [0, 25, 50, 75, 100];

  return (
    <div className="potok">
      <div className="potok-verh">
        <div className="potok-itog">
          <div className="potok-summa">{formatMoney(itogo, data.currency, locale)}</div>
          <div className="potok-pod">
            <span>{t(kassa ? "flowReceived" : "flowWon", { n: okno })}</span>
            {otklonenie !== null && (
              <>
                <span className="potok-tochka" />
                <span className={otklonenie >= 0 ? "potok-rost" : "potok-spad"}>
                  {otklonenie >= 0 ? "+" : "−"}
                  {Math.abs(otklonenie)} %
                </span>
                <span className="potok-tishe">{t("flowVsPrevMonth")}</span>
              </>
            )}
          </div>
        </div>
        <div className="potok-okna">
          {OKNA.map((n) => (
            <button
              key={n}
              type="button"
              className={"potok-okno" + (okno === n ? " potok-okno-on" : "")}
              onClick={() => setOkno(n)}
            >
              {t("flowMonths", { n })}
            </button>
          ))}
        </div>
      </div>

      {rekord !== null && rekord.summa > 0 && (
        // Врезка внутри блока, а не отдельным виджетом: «лучший месяц» без
        // кривой рядом — число, которое не с чем сравнить.
        <Link to="/reports" className="potok-rekord">
          <span className="potok-rekord-podpis">{t("flowRecord")}</span>
          <span className="potok-rekord-tekst">
            {t("flowRecordLine", {
              month: nazvanieMesyatsa(rekord.mesyats, true),
              sum: formatMoney(rekord.summa, data.currency, locale),
            })}
          </span>
        </Link>
      )}

      <div className="potok-grafik">
        <div className="potok-os">
          {[maks, maks * 0.75, maks * 0.5, maks * 0.25].map((v, i) => (
            <span key={i}>{formatMoneyShort(Math.round(v), data.currency, locale)}</span>
          ))}
        </div>
        <div className="potok-pole">
          <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="potok-holst" aria-hidden="true">
            {setka.map((y) => (
              <line key={y} x1="0" y1={y} x2="100" y2={y} className="potok-setka" vectorEffect="non-scaling-stroke" />
            ))}
            {tochki.length > 1 && (
              <>
                <polygon points={zalivka} className="potok-zalivka" />
                <polyline points={liniya} className="potok-krivaya" vectorEffect="non-scaling-stroke" />
              </>
            )}
          </svg>
          {/* Точки — обычными кнопками поверх холста, а не элементами SVG:
              растянутый viewBox сплющил бы кружок в овал, и попасть по нему
              пальцем было бы нечем. */}
          {koordinaty.map((k, i) => (
            <button
              key={tochki[i].mesyats}
              type="button"
              className={"potok-tochka-krivoy" + (navedena === i ? " potok-tochka-on" : "")}
              style={{ left: `${k.x}%`, top: `${k.y}%` }}
              onMouseEnter={() => setNavedena(i)}
              onMouseLeave={() => setNavedena((bylo) => (bylo === i ? null : bylo))}
              onFocus={() => setNavedena(i)}
              onBlur={() => setNavedena((bylo) => (bylo === i ? null : bylo))}
              aria-label={`${nazvanieMesyatsa(tochki[i].mesyats, true)}: ${formatMoney(tochki[i].summa, data.currency, locale)}`}
            />
          ))}
          {navedena !== null && tochki[navedena] && (
            <div
              className={
                "potok-vsplyv" +
                (koordinaty[navedena].x > 66 ? " potok-vsplyv-vlevo" : "") +
                (koordinaty[navedena].x < 34 ? " potok-vsplyv-vpravo" : "")
              }
              style={{ left: `${koordinaty[navedena].x}%`, top: `${koordinaty[navedena].y}%` }}
              role="tooltip"
            >
              <div className="potok-vsplyv-den">{nazvanieMesyatsa(tochki[navedena].mesyats, true)}</div>
              <div className="potok-vsplyv-summa">
                {formatMoney(tochki[navedena].summa, data.currency, locale)}
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="potok-niz">
        <span>{tochki.length > 0 ? nazvanieMesyatsa(tochki[0].mesyats) : ""}</span>
        <span>{tochki.length > 0 ? nazvanieMesyatsa(tochki[tochki.length - 1].mesyats) : ""}</span>
      </div>
    </div>
  );
}
