import { useCallback, useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { ScreenLoading, Toggle } from "../components/ui";
import { api } from "../lib/api";
import { useApp, type ModuleInfo } from "../lib/app";
import { formatDateTime } from "../lib/format";
import type { TranslationKey } from "../lib/i18n";

/** Подписи блоков. Ключи приходят с сервера из реестра (core/modules.py). */
const LABEL: Record<string, TranslationKey> = {
  clients: "clients",
  deals: "deals",
  documents: "documents",
  boards: "boards",
  warehouse: "modWarehouse",
  reports: "modReports",
  mail: "modMail",
  telephony: "modTelephony",
};

const ABOUT: Record<string, TranslationKey> = {
  clients: "modClientsAbout",
  deals: "modDealsAbout",
  documents: "modDocumentsAbout",
  boards: "modBoardsAbout",
  warehouse: "modWarehouseAbout",
  reports: "modReportsAbout",
  mail: "modMailAbout",
  telephony: "modTelephonyAbout",
};

const ICON: Record<string, string> = {
  clients: "clients",
  deals: "deals",
  documents: "receipt",
  boards: "boards",
  warehouse: "database",
  reports: "analytics",
  mail: "email",
  telephony: "call",
};

export function SettingsModules() {
  const { t, locale, refreshModules, toast, toastError } = useApp();
  const [items, setItems] = useState<ModuleInfo[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ items: ModuleInfo[] }>("/modules");
      setItems(data.items);
    } catch (e) {
      toastError(e);
    }
  }, [toastError]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!items) return <ScreenLoading />;

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
              onClick={() => void switchModule(item)}
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
                {item.required_by.length > 0 && !item.core && (
                  <span className="module-why">
                    {t("moduleNeededBy", {
                      list: item.required_by.map((k) => t(LABEL[k] ?? "modules")).join(", "),
                    })}
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
              <Toggle on={item.enabled} onToggle={() => undefined} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
