/**
 * Повтор напоминания — правило RFC 5545 (RRULE) в виде, удобном экрану.
 *
 * Сервер принимает то же подмножество (`core/services/povtor_service.py`) и
 * отвергает остальное: экран не должен собирать того, что будет отвергнуто.
 */
import type { TFunc } from "./i18n";

/** Пояс браузера: по нему сервер считает «каждый день в 9:00» (docs/bloki/29 §4). */
export function poyasBrauzera(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Kyiv";
  } catch {
    return "Europe/Kyiv";
  }
}

export type Chastota = "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY";
export const DNI = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] as const;
export type Den = (typeof DNI)[number];

export interface Pravilo {
  chastota: Chastota;
  interval: number;
  /** Дни недели у еженедельного. */
  dni: Den[];
  /** Как повторяется месячное: по числу, «второй вторник» или «последний день». */
  mesyachno: "chislo" | "den_nedeli" | "posledniy";
  chislo: number;
  /** 1–4 или -1 («последний»). */
  nomer: number;
  denNedeli: Den;
  konec: "nikogda" | "data" | "raz";
  /** YYYY-MM-DD включительно. */
  do: string;
  raz: number;
}

/** Быстрые варианты, как у будильника и календаря телефона. */
export const GOTOVYE = ["", "FREQ=DAILY", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "FREQ=WEEKLY", "FREQ=WEEKLY;INTERVAL=2", "FREQ=MONTHLY", "FREQ=YEARLY"] as const;

export function pustoe(srok: Date | null): Pravilo {
  const den = srok ?? new Date();
  const denNedeli = DNI[(den.getDay() + 6) % 7];
  return {
    chastota: "DAILY",
    interval: 1,
    dni: [denNedeli],
    mesyachno: "chislo",
    chislo: den.getDate(),
    nomer: Math.min(4, Math.ceil(den.getDate() / 7)),
    denNedeli,
    konec: "nikogda",
    do: "",
    raz: 10,
  };
}

export function sobrat(p: Pravilo): string {
  const chasti = [`FREQ=${p.chastota}`];
  if (p.interval > 1) chasti.push(`INTERVAL=${p.interval}`);
  if (p.chastota === "WEEKLY" && p.dni.length) {
    chasti.push(`BYDAY=${DNI.filter((d) => p.dni.includes(d)).join(",")}`);
  }
  if (p.chastota === "MONTHLY") {
    if (p.mesyachno === "chislo") chasti.push(`BYMONTHDAY=${p.chislo}`);
    if (p.mesyachno === "posledniy") chasti.push("BYMONTHDAY=-1");
    if (p.mesyachno === "den_nedeli") chasti.push(`BYDAY=${p.nomer}${p.denNedeli}`);
  }
  if (p.konec === "data" && p.do) chasti.push(`UNTIL=${p.do.replaceAll("-", "")}`);
  if (p.konec === "raz" && p.raz > 0) chasti.push(`COUNT=${p.raz}`);
  return chasti.join(";");
}

export function razobrat(pravilo: string | null | undefined, srok: Date | null = null): Pravilo | null {
  if (!pravilo) return null;
  const p = pustoe(srok);
  for (const kusok of pravilo.split(";")) {
    const [imya, znachenie = ""] = kusok.split("=");
    if (imya === "FREQ") p.chastota = znachenie as Chastota;
    if (imya === "INTERVAL") p.interval = Number(znachenie) || 1;
    if (imya === "COUNT") {
      p.konec = "raz";
      p.raz = Number(znachenie) || 1;
    }
    if (imya === "UNTIL") {
      p.konec = "data";
      p.do = `${znachenie.slice(0, 4)}-${znachenie.slice(4, 6)}-${znachenie.slice(6, 8)}`;
    }
    if (imya === "BYMONTHDAY") {
      if (znachenie === "-1") p.mesyachno = "posledniy";
      else {
        p.mesyachno = "chislo";
        p.chislo = Number(znachenie) || 1;
      }
    }
    if (imya === "BYDAY") {
      const nayden = znachenie.match(/^(-?\d)([A-Z]{2})$/);
      if (nayden) {
        p.mesyachno = "den_nedeli";
        p.nomer = Number(nayden[1]);
        p.denNedeli = nayden[2] as Den;
      } else {
        p.dni = znachenie.split(",") as Den[];
      }
    }
  }
  return p;
}

/** «Каждые N …» — каждая подпись своим вызовом: сторож словаря сверяет вызовы со счётом. */
function kazhdye(chastota: Chastota, n: number, t: TFunc): string {
  if (chastota === "DAILY") return t("povtorKazhdyeDney", { n });
  if (chastota === "WEEKLY") return t("povtorKazhdyeNedel", { n });
  if (chastota === "MONTHLY") return t("povtorKazhdyeMesyatsev", { n });
  return t("povtorKazhdyeLet", { n });
}

const ODIN = {
  DAILY: "povtorKazhdyyDen",
  WEEKLY: "povtorKazhduyuNedelyu",
  MONTHLY: "povtorKazhdyyMesyats",
  YEARLY: "povtorKazhdyyGod",
} as const;

const DEN_KRATKO = {
  MO: "denPn", TU: "denVt", WE: "denSr", TH: "denCht", FR: "denPt", SA: "denSb", SU: "denVs",
} as const;

/** «Каждые 28 дней», «По будням», «Каждый месяц в последнюю пятницу · 10 раз». */
export function opisat(pravilo: string | null | undefined, posle: boolean, t: TFunc): string {
  const p = razobrat(pravilo);
  if (!p) return "";
  let tekst: string;
  if (p.chastota === "WEEKLY" && p.interval === 1 && p.dni.join(",") === "MO,TU,WE,TH,FR") {
    tekst = t("povtorPoBudnyam");
  } else {
    tekst = p.interval > 1 ? kazhdye(p.chastota, p.interval, t) : t(ODIN[p.chastota]);
    if (p.chastota === "WEEKLY" && p.dni.length && pravilo?.includes("BYDAY")) {
      tekst += ": " + p.dni.map((d) => t(DEN_KRATKO[d])).join(", ");
    }
    if (p.chastota === "MONTHLY" && pravilo?.includes("BYMONTHDAY=-1")) tekst += ", " + t("povtorPosledniyDen");
    else if (p.chastota === "MONTHLY" && pravilo?.includes("BYMONTHDAY")) {
      tekst += ", " + t("povtorChisla", { n: p.chislo });
    } else if (p.chastota === "MONTHLY" && p.mesyachno === "den_nedeli") {
      tekst += ", " + t(p.nomer === -1 ? "povtorPosledniy" : "povtorNomer", { n: p.nomer }) + " " + t(DEN_KRATKO[p.denNedeli]);
    }
  }
  if (posle) tekst += " " + t("povtorPosleVypolneniya");
  if (p.konec === "raz") tekst += " · " + t("povtorRaz", { n: p.raz });
  if (p.konec === "data" && p.do) tekst += " · " + t("povtorDo", { data: p.do.split("-").reverse().join(".") });
  return tekst;
}
