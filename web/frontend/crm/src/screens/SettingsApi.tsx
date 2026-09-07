import { useApp } from "../lib/app";
import { PriyomZayavok } from "./LeadsSettings";
import { KlyuchiSayta } from "./SettingsApiKeys";

/** Один экран на всё, чем сайт разговаривает с системой: приём заявок и ключи
 *  доступа.
 *
 *  Раньше это были два пункта настроек, и владелец сказал прямо (07.09.2026):
 *  «почти одно и то же». Так и есть — оба про чужую программу, которая стучится
 *  к нам снаружи, и оба настраивает один человек за один заход: завёл ключ
 *  приёма, тут же выпустил ключ каталога. Два пункта заставляли ходить между
 *  ними и запоминать, где что.
 *
 *  Прежние адреса не пропали, а перенаправляют сюда (`App.tsx`): ссылка из
 *  руководства и из виджета ключа, а также заложенная в закладки, обязана
 *  открывать то же место.
 */
export function SettingsApi() {
  const { t } = useApp();
  return (
    <div className="page">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div>
          <h1 className="page-title">{t("apiSite")}</h1>
          <div className="page-sub">{t("apiSiteSub")}</div>
        </div>
      </div>
      <PriyomZayavok />
      <KlyuchiSayta />
    </div>
  );
}
