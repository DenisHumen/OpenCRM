/**
 * Подсказки адреса и ссылка на точку в картах.
 *
 * Подсказка — необязательное улучшение: ручки может не быть вовсе (старый
 * сервер, выключенная служба), и карточка обязана работать без неё — поле
 * вводится руками, красной плашки здесь быть не должно.
 */
import { api, ApiError } from "./api";

export interface VariantAdresa {
  /** Строка целиком — то, что человек читает в списке. */
  label: string;
  country_code: string;
  city: string;
  postcode: string;
  street: string;
  lat: number | null;
  lon: number | null;
}

/** Короче трёх букв спрашивать нечего: в ответ приедет пол-справочника. */
export const MIN_DLINA_ADRESA = 3;

/** Ответ ручки. `held` — «придержали», а не «не нашли»: экран обязан
 *  различать эти два, иначе человек решит, что такого адреса нет. */
interface OtvetPodskazok {
  items?: VariantAdresa[];
  enabled?: boolean;
  held?: boolean;
}

/** До какого мгновения не спрашиваем. Отдых, а не приговор: право выдают в
 *  соседней вкладке, тумблер включают в настройках, прокси икает на секунду —
 *  во всех трёх случаях «до перезагрузки страницы» слишком долго. */
let molchim_do = 0;
const OTDYH_MS = 60_000;

export interface Podskazki {
  varianty: VariantAdresa[];
  /** Ответа не было вовсе: придержали, отказ или отдых. Старый список на
   *  экране в этом случае оставляют — он вернее пустого. */
  net_otveta: boolean;
}

export async function podskazki_adresa(
  zapros: string,
  clientId?: number,
): Promise<Podskazki> {
  const stroka = zapros.trim();
  if (stroka.length < MIN_DLINA_ADRESA) return { varianty: [], net_otveta: false };
  if (Date.now() < molchim_do) return { varianty: [], net_otveta: true };
  try {
    // Телом, а не строкой запроса: набранный адрес не должен оказаться в
    // журнале доступа (docs/bloki/26-adresa.md §5).
    const otvet = await api.post<OtvetPodskazok>("/clients/address/suggest", {
      q: stroka,
      client_id: clientId ?? null,
    });
    if (otvet.enabled === false) {
      // Подсказки выключены настройкой. Сервер сказал это прямо — незачем
      // спрашивать его о том же на каждой паузе в наборе.
      molchim_do = Date.now() + OTDYH_MS;
      return { varianty: [], net_otveta: true };
    }
    return { varianty: otvet.items ?? [], net_otveta: Boolean(otvet.held) };
  } catch (beda) {
    // 401 — протухшая сессия, а не «ручки нет»: молчать до перезагрузки было
    // бы враньём. Остальное — отдых на минуту, как и у службы на сервере.
    if (beda instanceof ApiError && beda.status !== 401) molchim_do = Date.now() + OTDYH_MS;
    return { varianty: [], net_otveta: true };
  }
}

/** Точка в гугловых картах. Этот вид ссылки открывается и без ключа API. */
export function ssylka_na_kartu(lat: number, lon: number): string {
  return `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`;
}
