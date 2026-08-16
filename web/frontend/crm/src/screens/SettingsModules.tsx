import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { ConfirmModal, ScreenLoading, Toggle } from "../components/ui";
import { api } from "../lib/api";
import { useApp, type ModuleInfo } from "../lib/app";
import { useFailure } from "../lib/failure";
import { formatDateTime } from "../lib/format";
import type { TranslationKey } from "../lib/i18n";

/** Подписи блоков. Ключи приходят с сервера из реестра (core/modules.py). */
/** Подписи блоков. Экспортируется, потому что тем же списком пользуется экран
 *  первого входа: две копии одной карты разъезжаются молча — так уже разошлись
 *  значки блоков между меню и настройками. */
export const LABEL: Record<string, TranslationKey> = {
  clients: "clients",
  deals: "deals",
  companies: "companies",
  documents: "documents",
  tasks: "tasks",
  boards: "boards",
  warehouse: "modWarehouse",
  labels: "modLabels",
  orders: "modOrders",
  reports: "modReports",
  mail: "modMail",
  templates: "templates",
  monitoring: "modMonitoring",
  telephony: "modTelephony",
  telegram: "modTelegram",
  finance: "modFinance",
};

const ABOUT: Record<string, TranslationKey> = {
  clients: "modClientsAbout",
  deals: "modDealsAbout",
  companies: "modCompaniesAbout",
  documents: "modDocumentsAbout",
  tasks: "modTasksAbout",
  boards: "modBoardsAbout",
  warehouse: "modWarehouseAbout",
  labels: "modLabelsAbout",
  orders: "modOrdersAbout",
  reports: "modReportsAbout",
  mail: "modMailAbout",
  templates: "modTemplatesAbout",
  monitoring: "modMonitoringAbout",
  telephony: "modTelephonyAbout",
  telegram: "modTelegramAbout",
  finance: "modFinanceAbout",
};

const ICON: Record<string, string> = {
  clients: "clients",
  deals: "deals",
  companies: "building",
  documents: "receipt",
  tasks: "clock",
  boards: "boards",
  warehouse: "warehouse",
  labels: "scan",
  orders: "receipt",
  reports: "analytics",
  mail: "email",
  templates: "note",
  monitoring: "alert",
  telephony: "call",
  telegram: "send",
  // «receipt» — тот же значок, что у бланков и заказов, и это не оплошность:
  // ближе к деньгам в наборе ничего нет, а свой значок ради одного блока
  // означал бы правку `Icon.PATHS` — общего файла, куда финансам лезть незачем.
  // Меню обязано взять тот же ключ: две карты значков уже расходились молча.
  finance: "receipt",
};

export function SettingsModules() {
  const { t, locale, refreshModules, toast, toastError } = useApp();
  const [items, setItems] = useState<ModuleInfo[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Блок, выключение которого уведёт за собой соседей: спрашиваем ДО нажатия.
  // Забрать разделы с данными одним движением и молча — самый быстрый способ
  // потерять доверие к настройкам.
  const [asking, setAsking] = useState<ModuleInfo | null>(null);

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      const data = await api.get<{ items: ModuleInfo[] }>("/modules");
      setItems(data.items);
    } catch (e) {
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!items) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const names = (keys: string[]) => keys.map((k) => t(LABEL[k] ?? "modules")).join(", ");

  const askThenSwitch = (item: ModuleInfo) => {
    if (item.core || !item.ready || busy) return;
    // Спрашиваем только про выключение с каскадом: оно убирает разделы.
    // Включение тоже тянет соседей, но добавляет, а не отнимает — про него
    // достаточно строки в карточке.
    if (item.enabled && item.off_takes.length > 0) {
      setAsking(item);
      return;
    }
    void switchModule(item);
  };

  const switchModule = async (item: ModuleInfo) => {
    if (item.core || !item.ready || busy) return;
    setBusy(item.key);
    try {
      const data = await api.post<{ items: ModuleInfo[] }>(`/modules/${item.key}`, {
        enabled: !item.enabled,
      });
      setItems(data.items);
      // Меню и маршруты читают состав из общего состояния — обновляем сразу,
      // иначе выключенный раздел остаётся в сайдбаре до перезагрузки страницы.
      await refreshModules();
      toast(item.enabled ? t("moduleOff") : t("moduleOn"));
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div>
          <h1 className="page-title">{t("modules")}</h1>
          <div className="page-sub">{t("modulesSub")}</div>
        </div>
        {/* Вернуться к вопросу «чем вы занимаетесь» можно всегда: человек
            выбрал набор в первый день, а через месяц открыл магазин. */}
        <Link className="btn btn-secondary btn-sm" to="/setup">
          {t("setupTitle")}
        </Link>
      </div>

      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <div className="field-desc" style={{ marginTop: 0 }}>{t("modulesHint")}</div>
      </div>

      <div className="list-card">
        {items.map((item) => {
          const locked = item.core || !item.ready;
          return (
            <div
              key={item.key}
              className={"module-row" + (locked ? " locked" : "")}
              onClick={() => askThenSwitch(item)}
            >
              <span className="module-icon">
                <Icon name={ICON[item.key] ?? "docs"} size={17} />
              </span>
              <span className="module-text">
                <span className="module-name">
                  {t(LABEL[item.key] ?? "modules")}
                  {item.core && <span className="module-tag">{t("moduleCore")}</span>}
                  {!item.ready && <span className="module-tag soon">{t("moduleSoon")}</span>}
                </span>
                <span className="module-about">{t(ABOUT[item.key] ?? "modulesSub")}</span>
                {/* Почему переключатель не поддаётся — объясняем на месте, а не
                    ошибкой после нажатия. */}
                {item.core && <span className="module-why">{t("moduleCoreWhy")}</span>}
                {!item.ready && <span className="module-why">{t("moduleSoonWhy")}</span>}
                {/* Связь показываем ВСЕГДА, а не только когда она мешает.
                    Человек должен видеть устройство набора до того, как
                    упрётся в последствие: «наклейки стоят на складе» понятнее
                    из спокойной строки, чем из вопроса при выключении. */}
                {item.requires.length > 0 && (
                  <span className="module-why">
                    {t("moduleRestsOn", { list: names(item.requires) })}
                  </span>
                )}
                {item.required_by.length > 0 && !item.core && (
                  <span className="module-why">
                    {t("moduleNeededBy", { list: names(item.required_by) })}
                  </span>
                )}
                {item.enabled && item.off_takes.length > 0 && (
                  <span className="module-why">
                    {t("moduleOffTakes", { list: names(item.off_takes) })}
                  </span>
                )}
                {!item.enabled && item.on_needs.length > 0 && (
                  <span className="module-why">
                    {t("moduleOnNeeds", { list: names(item.on_needs) })}
                  </span>
                )}
                {item.updated_by_name && item.updated_at && (
                  <span className="module-why">
                    {t("moduleChangedBy", {
                      who: item.updated_by_name,
                      when: formatDateTime(item.updated_at, locale),
                    })}
                  </span>
                )}
              </span>
              {/* Строка целиком ловит нажатие мышью — так попасть проще, чем
                  в переключатель 34×20. Но у самого переключателя обработчик
                  свой и настоящий: `div` под Tab не попадает, и без него ни
                  один блок нельзя было включить без мыши. */}
              <Toggle
                on={item.enabled}
                label={t(LABEL[item.key] ?? "modules")}
                disabled={locked}
                onToggle={() => askThenSwitch(item)}
              />
            </div>
          );
        })}
      </div>

      {asking && (
        <ConfirmModal
          text={t("moduleOffConfirm", {
            name: t(LABEL[asking.key] ?? "modules"),
            list: names(asking.off_takes),
          })}
          confirmLabel={t("moduleOffConfirmYes")}
          danger
          onConfirm={() => void switchModule(asking)}
          onClose={() => setAsking(null)}
        />
      )}
    </div>
  );
}
