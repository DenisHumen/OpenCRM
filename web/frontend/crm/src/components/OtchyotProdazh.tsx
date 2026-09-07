import { useCallback, useEffect, useState } from "react";

import { LoadFailed, Spinner } from "./ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDate, formatMoney, formatRate } from "../lib/format";
import { useFailure } from "../lib/failure";
import { useLiveTopic } from "../lib/live";

/**
 * Отчёт продаж — виджет сводки двумя видами: тепловая карта по дням и точечная
 * матрица по месяцам. Общий низ у них один: месяц, год и города.
 *
 * Перевод внешнего образца по docs/dizayn/18-chuzhie-komponenty.md; разбор
 * виджета целиком — docs/dizayn/27-otchyot-prodazh.md.
 *
 * Чем меряется выручка — решает не виджет: касса, если блок финансов включён,
 * иначе выигранные заявки. Ответ приходит полем `money_basis` и называется на
 * экране словами; тем же счётом живут плитки денег рядом.
 */

/** Событие дня — строка всплывающего поля: выигранная заявка либо поступление
 *  в кассу, смотря чем меряется выручка. */
export interface SobytieDnya {
  nomer: string;
  nazvanie: string;
  summa_minor: number;
}

/** Клетка тепловой карты: один день. */
export interface KletkaOtchyota {
  den: string;
  /** Сколько событий выручки за день — это число и печатается в клетке. */
  znachenie: number;
  /** Сумма ВСЕХ событий дня, а не показанных: в клетке бывает и пятьдесят. */
  summa_minor: number;
  sobytiya: SobytieDnya[];
}

export interface MesyatsOtchyota {
  /** Первый день месяца, ISO. Подпись собирает экран: месяцы у браузера свои. */
  mesyats: string;
  summa_minor: number;
  /** Суммы по дням месяца. Длина = число дней в месяце. */
  dni: number[];
}

export interface ItogOtchyota {
  summa_minor: number;
  /** Рост к тому же числу прошлого периода, базисные пункты. `null` — сравнить
   *  не с чем: прошлый период пуст. */
  rost_bp: number | null;
  bylo_minor: number;
}

export interface DannyeOtchyota {
  currency: string;
  /** Чем меряется выручка: `cash` — касса, `deals` — выигранные заявки.
   *  Экран называет это словами: молчаливая подмена одного счёта другим и есть
   *  та беда, ради которой базис живёт в одном месте на всю систему. */
  money_basis: "cash" | "deals";
  /** Последние 64 дня, свежий первым: восемь клеток в ряд, восемь рядов. */
  kletki: KletkaOtchyota[];
  mesyatsy: MesyatsOtchyota[];
  za_mesyats: ItogOtchyota;
  za_god: ItogOtchyota;
  goroda: { gorod: string; summa_minor: number }[];
}

/** Ступень шкалы 1…12 по доле от лучшего дня. Цвет ступени живёт в токене. */
function stupen(znachenie: number, maks: number): number {
  if (maks <= 0) return 1;
  const dolya = Math.max(0, Math.min(1, znachenie / maks));
  return Math.max(1, Math.min(12, Math.round(dolya * 11) + 1));
}

/** Метка «за месяц»: четырёхлучевая звезда. */
function ZnakMesyatsa() {
  return (
    <svg width={13} height={13} viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <path d="M7 0.6 8.3 4.8 12.5 6.1 8.3 7.4 7 11.6 5.7 7.4 1.5 6.1 5.7 4.8Z" fill="var(--otchyot-rost)" />
    </svg>
  );
}

/** Метка «за год»: секундомер. */
function ZnakGoda() {
  return (
    <svg width={13} height={13} viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <circle cx="7" cy="8.4" r="4.6" stroke="var(--otchyot-god)" strokeWidth="1.6" />
      <circle cx="4.9" cy="1.7" r="1.3" fill="var(--otchyot-god)" />
      <circle cx="9.1" cy="1.7" r="1.3" fill="var(--otchyot-god)" />
    </svg>
  );
}

/**
 * Всплывающее поле дня. Живёт только у наведённой клетки — это состояние, а не
 * `:hover` в вёрстке: иначе в разметке лежало бы шестьдесят четыре скрытых
 * поля вместо одного (docs/18, «чего в образцах брать не надо»).
 */
function VsplyvDnya({
  kletka,
  currency,
  kassa,
  vniz,
  kray,
}: {
  kletka: KletkaOtchyota;
  currency: string;
  /** Считаем кассой: подпись счётчика называет, что именно сосчитано. */
  kassa: boolean;
  /** Верхние ряды раскрываются вниз: вверху поле ждала бы шапка карточки. */
  vniz: boolean;
  /** У краёв поле прижимается к краю, иначе уезжает за карточку. */
  kray: "vlevo" | "vpravo" | null;
}) {
  const { t, locale } = useApp();
  const ostalnye = kletka.znachenie - kletka.sobytiya.length;
  const klassy = [
    "otchyot-vsplyv",
    vniz ? "otchyot-vsplyv-vniz" : "",
    kray ? `otchyot-vsplyv-${kray}` : "",
  ].filter(Boolean);

  return (
    <div className={klassy.join(" ")} role="tooltip">
      <div className="otchyot-vsplyv-shapka">
        <span className="otchyot-vsplyv-data">{formatDate(kletka.den, locale)}</span>
        <span>{t(kassa ? "salesCashCount" : "salesDealsCount", { count: kletka.znachenie })}</span>
      </div>
      <div className="otchyot-vsplyv-zakazy">
        {kletka.sobytiya.map((z) => (
          <div className="otchyot-vsplyv-zakaz" key={z.nomer}>
            <span className="truncate" title={z.nomer}>
              {z.nazvanie || z.nomer}
            </span>
            <span>{formatMoney(z.summa_minor, currency, locale)}</span>
          </div>
        ))}
      </div>
      {ostalnye > 0 && (
        <div className="otchyot-vsplyv-eshchyo">{t("salesMore", { count: ostalnye })}</div>
      )}
      <div className="otchyot-vsplyv-itogo">
        <span className="otchyot-vsplyv-itogo-imya">{t("salesTotal")}</span>
        <span className="otchyot-vsplyv-itogo-summa">
          {formatMoney(kletka.summa_minor, currency, locale)}
        </span>
      </div>
    </div>
  );
}

/** Рост со стрелкой по знаку. У образца стрелка только вверх — на живых данных
 *  месяц ниже прошлого случается, и красить его зелёным нельзя. */
function Rost({ itog }: { itog: ItogOtchyota }) {
  const { t } = useApp();
  if (itog.rost_bp === null) return <span className="otchyot-bylo">{t("salesNoBase")}</span>;
  const vniz = itog.rost_bp < 0;
  return (
    <span className={vniz ? "otchyot-rost otchyot-spad" : "otchyot-rost"}>
      {vniz ? "↓" : "↑"}
      {formatRate(Math.abs(itog.rost_bp), "en").replace("%", "")}%
    </span>
  );
}

/** Низ карточки: месяц, год и города. Один и тот же у обоих видов. */
function PodvalOtchyota({ data }: { data: DannyeOtchyota }) {
  const { t, locale } = useApp();
  const dengi = (minor: number) => formatMoney(minor, data.currency, locale);

  return (
    <>
      <div className="otchyot-kpi" style={{ "--zaderzhka": "640ms" } as React.CSSProperties}>
        <div>
          <div className="otchyot-kpi-imya">
            <ZnakMesyatsa />
            {t("salesMonthly")}
          </div>
          <div className="otchyot-kpi-znachenie">{dengi(data.za_mesyats.summa_minor)}</div>
          <div className="otchyot-kpi-podpis">
            <Rost itog={data.za_mesyats} />
            {/* Прошлый период печатается, только если с ним сравнивали: рядом
                со словами «не с чем сравнить» ноль читается как настоящий. */}
            {data.za_mesyats.rost_bp !== null && (
              <span className="otchyot-bylo">{dengi(data.za_mesyats.bylo_minor)}</span>
            )}
          </div>
        </div>
        <div>
          <div className="otchyot-kpi-imya">
            <ZnakGoda />
            {t("salesYearly")}
          </div>
          <div className="otchyot-kpi-znachenie">{dengi(data.za_god.summa_minor)}</div>
          <div className="otchyot-kpi-podpis">
            <Rost itog={data.za_god} />
            {data.za_god.rost_bp !== null && (
              <span className="otchyot-bylo">{dengi(data.za_god.bylo_minor)}</span>
            )}
          </div>
        </div>
      </div>
      <div className="otchyot-goroda">
        {data.goroda.map((g, i) => (
          <div
            className="otchyot-gorod"
            key={g.gorod}
            style={{ "--zaderzhka": `${720 + i * 70}ms` } as React.CSSProperties}
          >
            <span className="truncate">{g.gorod}</span>
            <span className="otchyot-gorod-summa">{dengi(g.summa_minor)}</span>
          </div>
        ))}
      </div>
    </>
  );
}

/** Тепловая карта: 64 дня по восемь клеток в ряд. */
function Setka({ data }: { data: DannyeOtchyota }) {
  const [navedeno, setNavedeno] = useState<string | null>(null);
  const kletki = data.kletki.slice(0, 64);
  const maks = kletki.reduce((m, k) => Math.max(m, k.znachenie), 0);
  const ryady: KletkaOtchyota[][] = [];
  for (let i = 0; i < kletki.length; i += 8) ryady.push(kletki.slice(i, i + 8));

  return (
    <div className="otchyot-setka">
      {ryady.map((ryad, y) => (
        <div className="otchyot-setka-ryad" key={ryad[0]?.den ?? y}>
          {ryad.map((kletka, x) => (
            <div
              className={
                `otchyot-kletka otchyot-kletka-${stupen(kletka.znachenie, maks)}` +
                // Наведённая клетка поднимается над соседями: подсказка лежит
                // внутри неё, а соседние клетки рисуются позже и накрывали её.
                (navedeno === kletka.den ? " otchyot-kletka-navedena" : "")
              }
              key={kletka.den}
              style={{ "--zaderzhka": `${60 + (y * 8 + x) * 7}ms` } as React.CSSProperties}
              tabIndex={0}
              onMouseEnter={() => setNavedeno(kletka.den)}
              onMouseLeave={() => setNavedeno(null)}
              onFocus={() => setNavedeno(kletka.den)}
              onBlur={() => setNavedeno(null)}
            >
              {String(kletka.znachenie).padStart(2, "0")}
              {navedeno === kletka.den && (
                <VsplyvDnya
                  kletka={kletka}
                  currency={data.currency}
                  kassa={data.money_basis === "cash"}
                  vniz={y <= 2}
                  kray={x <= 1 ? "vlevo" : x >= 6 ? "vpravo" : null}
                />
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * Точечная матрица: три месяца, столбец — день месяца.
 *
 * Сколько точек в столбце зажжено (0…5) — доля дня от лучшего дня месяца, то
 * есть настоящая величина. КАКИЕ из пяти зажглись — раскладка по дате, а не
 * данные: у образца точки рассыпаны, и рассыпанность сохранена, потому что
 * ряды матрицы ничего не означают. Раскладка постоянна для одной и той же
 * даты, поэтому от перерисовки рисунок не меняется.
 */
function Matritsa({ data }: { data: DannyeOtchyota }) {
  const { locale } = useApp();
  const imya_mesyatsa = (iso: string) => {
    const kogda = new Date(`${iso}T00:00:00Z`);
    if (Number.isNaN(kogda.getTime())) return iso;
    // Имя месяца собирает браузер: свой список названий на сервере — это
    // двадцать четыре строки текста интерфейса вне i18n.
    return new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", {
      month: "long",
      timeZone: "UTC",
    }).format(kogda);
  };

  return (
    <div className="otchyot-mesyatsy">
      {data.mesyatsy.map((m, mi) => {
        const maks = m.dni.reduce((a, b) => Math.max(a, b), 0);
        return (
          <div key={m.mesyats}>
            <div className="otchyot-mesyats-shapka">
              <span className="otchyot-mesyats-imya">{imya_mesyatsa(m.mesyats)}</span>
              <span className="otchyot-mesyats-summa">
                {formatMoney(m.summa_minor, data.currency, locale)}
              </span>
            </div>
            <div className="otchyot-matritsa">
              {[0, 1, 2, 3, 4].map((ryad) => (
                <div
                  className="otchyot-matritsa-ryad"
                  key={ryad}
                  style={{ "--stolbtsov": m.dni.length } as React.CSSProperties}
                >
                  {m.dni.map((summa, den) => {
                    // Гамма 2.2: при ровных суммах доля у всех дней высокая, и
                    // панель светилась бы целиком, скрывая как раз пики.
                    const zazhech = maks > 0 ? Math.round(Math.pow(summa / maks, 2.2) * 5) : 0;
                    const poryadok = (den * 7 + ryad * 3) % 5;
                    const yarko = poryadok < zazhech;
                    const srednee = !yarko && poryadok < zazhech + 1;
                    return (
                      <span
                        className={
                          yarko
                            ? "otchyot-tochka otchyot-tochka-yarkaya"
                            : srednee
                              ? "otchyot-tochka otchyot-tochka-srednyaya"
                              : "otchyot-tochka"
                        }
                        key={den}
                        style={
                          { "--zaderzhka": `${80 + mi * 140 + den * 7 + ryad * 3}ms` } as React.CSSProperties
                        }
                      />
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Виджет целиком. Свои данные берёт сам: отчёт считает восемь запросов по окну
 * в год, и класть его в общий ответ сводки значило бы платить за него у всех,
 * включая тех, у кого виджета нет.
 */
export function OtchyotProdazh({ vid }: { vid: "setka" | "matritsa" }) {
  const { t } = useApp();
  const [data, setData] = useState<DannyeOtchyota | null>(null);
  const [zapros, setZapros] = useState(0);
  const { failure, fail, clear } = useFailure();

  // Обе темы, а не одна: чем меряется выручка, решает базис — при включённой
  // кассе отчёт меняют проводки, а не закрытые заявки. Живым событием, а не
  // таймером: спрашивать сервер по часам ради этого незачем.
  useLiveTopic(["deals", "finance"], () => setZapros((n) => n + 1));

  const perechitat = useCallback(() => setZapros((n) => n + 1), []);

  useEffect(() => {
    let zhivo = true;
    clear();
    api
      .get<DannyeOtchyota>("/dashboard/sales-report")
      .then((otvet) => zhivo && setData(otvet))
      .catch((beda) => zhivo && fail(beda));
    return () => {
      zhivo = false;
    };
  }, [zapros, fail, clear]);

  return (
    <div className="otchyot">
      <h2 className="otchyot-titul">{t("salesReport")}</h2>
      {failure ? (
        <LoadFailed error={failure} onRetry={perechitat} />
      ) : !data ? (
        <div className="otchyot-zhdyom">
          <Spinner />
        </div>
      ) : (
        <>
          {vid === "setka" ? <Setka data={data} /> : <Matritsa data={data} />}
          <PodvalOtchyota data={data} />
        </>
      )}
    </div>
  );
}
