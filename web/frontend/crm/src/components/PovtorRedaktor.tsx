import { useEffect, useState } from "react";

import { useApp } from "../lib/app";
import { mestnyyDen } from "../lib/format";
import { DNI, GOTOVYE, type Den, type Pravilo, opisat, pustoe, razobrat, sobrat } from "../lib/povtor";

const GOTOVYE_LABEL = {
  "": "povtorNet",
  "FREQ=DAILY": "povtorKazhdyyDen",
  "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR": "povtorPoBudnyam",
  "FREQ=WEEKLY": "povtorKazhduyuNedelyu",
  "FREQ=WEEKLY;INTERVAL=2": "povtorKazhdyeNedel",
  "FREQ=MONTHLY": "povtorKazhdyyMesyats",
  "FREQ=YEARLY": "povtorKazhdyyGod",
} as const;

const DEN_KRATKO = {
  MO: "denPn", TU: "denVt", WE: "denSr", TH: "denCht", FR: "denPt", SA: "denSb", SU: "denVs",
} as const;

/**
 * Повтор напоминания: быстрые варианты, как у будильника телефона, и полная
 * настройка, как в календаре — «каждые 28 дней», «по пн, ср, пт», «в последнюю
 * пятницу месяца», конец по дате или числу раз, «считать от выполнения».
 */
export function PovtorRedaktor({
  povtor,
  posle,
  srok,
  disabled,
  onChange,
}: {
  povtor: string | null;
  posle: boolean;
  srok: Date | null;
  disabled?: boolean;
  onChange: (povtor: string | null, posle: boolean) => void;
}) {
  const { t } = useApp();
  const gotovyy = GOTOVYE.includes((povtor ?? "") as (typeof GOTOVYE)[number]);
  const [svoy, setSvoy] = useState(!gotovyy);
  const [p, setP] = useState<Pravilo>(() => razobrat(povtor, srok) ?? pustoe(srok));
  useEffect(() => {
    setP(razobrat(povtor, srok) ?? pustoe(srok));
  }, [povtor, srok]);

  const primenit = (novoe: Pravilo, novoePosle = posle) => {
    setP(novoe);
    onChange(sobrat(novoe), novoePosle);
  };

  return (
    <div className="povtor">
      <select
        className="input"
        value={svoy ? "svoy" : povtor ?? ""}
        disabled={disabled}
        aria-label={t("napomPovtor")}
        onChange={(e) => {
          if (e.target.value === "svoy") {
            setSvoy(true);
            primenit(razobrat(povtor, srok) ?? pustoe(srok));
            return;
          }
          setSvoy(false);
          onChange(e.target.value || null, e.target.value ? posle : false);
        }}
      >
        {GOTOVYE.map((g) => (
          <option key={g} value={g}>
            {g === "FREQ=WEEKLY;INTERVAL=2" ? t("povtorKazhdyeNedel", { n: 2 }) : t(GOTOVYE_LABEL[g])}
          </option>
        ))}
        <option value="svoy">{t("povtorSvoy")}</option>
      </select>

      {svoy && (
        <div className="povtor-svoy">
          <div className="povtor-stroka">
            <span>{t("povtorKazhdye")}</span>
            <input
              className="input povtor-chislo"
              type="number"
              min={1}
              max={999}
              defaultValue={p.interval}
              key={`i-${p.interval}`}
              disabled={disabled}
              onBlur={(e) => primenit({ ...p, interval: Math.max(1, Math.min(999, Number(e.target.value) || 1)) })}
            />
            <select
              className="input"
              value={p.chastota}
              disabled={disabled}
              onChange={(e) => primenit({ ...p, chastota: e.target.value as Pravilo["chastota"] })}
            >
              <option value="DAILY">{t("povtorEdDni")}</option>
              <option value="WEEKLY">{t("povtorEdNedeli")}</option>
              <option value="MONTHLY">{t("povtorEdMesyatsy")}</option>
              <option value="YEARLY">{t("povtorEdGody")}</option>
            </select>
          </div>

          {p.chastota === "WEEKLY" && (
            <div className="povtor-dni" role="group" aria-label={t("povtorEdNedeli")}>
              {DNI.map((d: Den) => {
                const vybran = p.dni.includes(d);
                return (
                  <button
                    key={d}
                    type="button"
                    aria-pressed={vybran}
                    disabled={disabled}
                    className={"povtor-den" + (vybran ? " active" : "")}
                    onClick={() => {
                      const dni = vybran ? p.dni.filter((x) => x !== d) : [...p.dni, d];
                      if (dni.length) primenit({ ...p, dni });
                    }}
                  >
                    {t(DEN_KRATKO[d])}
                  </button>
                );
              })}
            </div>
          )}

          {p.chastota === "MONTHLY" && (
            <div className="povtor-mesyats">
              <label className="povtor-variant">
                <input
                  type="radio"
                  checked={p.mesyachno === "chislo"}
                  disabled={disabled}
                  onChange={() => primenit({ ...p, mesyachno: "chislo" })}
                />
                {t("povtorPoChislu")}
                <input
                  className="input povtor-chislo"
                  type="number"
                  min={1}
                  max={31}
                  defaultValue={p.chislo}
                  key={`c-${p.chislo}`}
                  disabled={disabled || p.mesyachno !== "chislo"}
                  onBlur={(e) => primenit({ ...p, chislo: Math.max(1, Math.min(31, Number(e.target.value) || 1)) })}
                />
              </label>
              <label className="povtor-variant">
                <input
                  type="radio"
                  checked={p.mesyachno === "den_nedeli"}
                  disabled={disabled}
                  onChange={() => primenit({ ...p, mesyachno: "den_nedeli" })}
                />
                {t("povtorPoDnyuNedeli")}
                <select
                  className="input"
                  value={p.nomer}
                  disabled={disabled || p.mesyachno !== "den_nedeli"}
                  onChange={(e) => primenit({ ...p, nomer: Number(e.target.value) })}
                >
                  {[1, 2, 3, 4].map((n) => (
                    <option key={n} value={n}>
                      {t("povtorNomer", { n })}
                    </option>
                  ))}
                  <option value={-1}>{t("povtorPosledniy")}</option>
                </select>
                <select
                  className="input"
                  value={p.denNedeli}
                  disabled={disabled || p.mesyachno !== "den_nedeli"}
                  onChange={(e) => primenit({ ...p, denNedeli: e.target.value as Den })}
                >
                  {DNI.map((d) => (
                    <option key={d} value={d}>
                      {t(DEN_KRATKO[d])}
                    </option>
                  ))}
                </select>
              </label>
              <label className="povtor-variant">
                <input
                  type="radio"
                  checked={p.mesyachno === "posledniy"}
                  disabled={disabled}
                  onChange={() => primenit({ ...p, mesyachno: "posledniy" })}
                />
                {t("povtorVPosledniyDen")}
              </label>
            </div>
          )}

          <div className="povtor-konets">
            <label className="povtor-variant">
              <input
                type="radio"
                checked={p.konec === "nikogda"}
                disabled={disabled}
                onChange={() => primenit({ ...p, konec: "nikogda" })}
              />
              {t("povtorKonetsNikogda")}
            </label>
            <label className="povtor-variant">
              <input
                type="radio"
                checked={p.konec === "data"}
                disabled={disabled}
                onChange={() => primenit({ ...p, konec: "data", do: p.do || mestnyyDen(new Date(Date.now() + 30 * 86400000)) })}
              />
              {t("povtorKonetsData")}
              <input
                className="input"
                type="date"
                value={p.do}
                disabled={disabled || p.konec !== "data"}
                onChange={(e) => e.target.value && primenit({ ...p, do: e.target.value })}
              />
            </label>
            <label className="povtor-variant">
              <input
                type="radio"
                checked={p.konec === "raz"}
                disabled={disabled}
                onChange={() => primenit({ ...p, konec: "raz" })}
              />
              {t("povtorKonetsRaz")}
              <input
                className="input povtor-chislo"
                type="number"
                min={1}
                max={1000}
                defaultValue={p.raz}
                key={`r-${p.raz}`}
                disabled={disabled || p.konec !== "raz"}
                onBlur={(e) => primenit({ ...p, raz: Math.max(1, Math.min(1000, Number(e.target.value) || 1)) })}
              />
              {t("povtorRazEd")}
            </label>
          </div>

          <label className="povtor-posle" title={t("povtorOtVypolneniyaHint")}>
            <input
              type="checkbox"
              checked={posle}
              disabled={disabled || p.chastota === "WEEKLY" && p.dni.length > 1}
              onChange={(e) => primenit(p, e.target.checked)}
            />
            {t("povtorOtVypolneniya")}
          </label>
        </div>
      )}

      {povtor && <div className="povtor-itog">{opisat(povtor, posle, t)}</div>}
    </div>
  );
}
