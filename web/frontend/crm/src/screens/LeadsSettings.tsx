import { useCallback, useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { ConfirmModal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { copyText } from "../lib/clipboard";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";

/** Что сервер рассказывает о приёме. Самого ключа здесь нет и быть не может. */
interface LeadConfig {
  has_intake_key: boolean;
  /** Кто получит заявку СЕЙЧАС: выбранный, а если он отключён — владелец системы. */
  manager_id: number | null;
  manager_name: string;
  /** Что записано в настройке. С `manager_*` расходится, когда выбранный отключён. */
  chosen_id: number | null;
  chosen_name: string;
  intake_path: string;
  intake_header: string;
  /** Поля, которые принимает ручка. Приходят с сервера — см. `lead_service`. */
  intake_fields: string[];
  honeypot_field: string;
}

interface Person {
  id: number;
  name: string;
}

/**
 * Значения для примера запроса.
 *
 * Не переводятся намеренно: это содержимое чужой формы, а не текст продукта.
 * Переведённый пример пришлось бы переводить обратно тому, кто вставляет его в
 * код сайта, — а половина таких людей вообще не читает по-русски.
 *
 * Поля берутся с сервера, значения — отсюда; незнакомому полю достаётся пустая
 * строка, и пример остаётся верным (место видно, вписать есть что).
 */
const SAMPLE: Record<string, string> = {
  name: "Anna Petrenko",
  email: "anna@example.com",
  phone: "+380 67 123 45 67",
  message: "Please call me back about a new order",
};

/** Заглушка на месте ключа, пока свежесозданного под рукой нет. */
const KEY_PLACEHOLDER = "YOUR-INTAKE-KEY";

/**
 * Приём заявок с сайта.
 *
 * Отдельным маршрутом, а не разделом общих настроек: там одна кнопка
 * «Сохранить» на всю группу, а здесь ключ, который показывается **один раз**, и
 * действия, которые применяются сразу. По той же причине отдельно живут
 * подключение к АТС и бот фирмы.
 *
 * До этого экрана приём настраивался только из консоли: сервер, ручки и
 * проверки были написаны, а нажать на них было негде — то есть функции, которой
 * нельзя воспользоваться, всё равно что нет. Пустой ключ означает, что приёма
 * не существует вовсе, и свежая установка приходит именно в этом состоянии.
 */
export function SettingsLeads() {
  const { t, toast, toastError } = useApp();
  const [config, setConfig] = useState<LeadConfig | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  /** Ключ, показываемый один раз. Живёт до перезагрузки экрана и нигде больше. */
  const [freshKey, setFreshKey] = useState("");
  const [confirmNew, setConfirmNew] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  // Засов на создание ключа: второе нажатие выдало бы второй ключ, и человек
  // унёс бы к себе на сайт первый — тот, который уже не работает.
  const guard = useGuard();

  const { failure, fail, clear } = useFailure();

  const load = useCallback(() => {
    clear();
    Promise.all([
      api.get<LeadConfig>("/leads/settings"),
      api.get<{ items: Person[] }>("/people"),
    ])
      .then(([svezhee, spisok]) => {
        setConfig(svezhee);
        setPeople(spisok.items);
      })
      .catch(fail);
  }, [fail, clear]);

  useEffect(load, [load]);

  if (!config) return <ScreenLoading error={failure} onRetry={load} />;

  const createKey = async () => {
    if (!guard.take()) return;
    try {
      const created = await api.post<{ key: string }>("/leads/settings/key");
      setFreshKey(created.key);
      setConfig((prev) => ({ ...prev!, has_intake_key: true }));
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const closeIntake = async () => {
    try {
      setConfig(await api.del<LeadConfig>("/leads/settings/key"));
      setFreshKey("");
    } catch (e) {
      toastError(e);
    }
  };

  const chooseManager = async (value: string) => {
    try {
      setConfig(
        await api.patch<LeadConfig>("/leads/settings/manager", {
          manager_id: value ? Number(value) : null,
        }),
      );
    } catch (e) {
      toastError(e);
    }
  };

  const intakeUrl = window.location.origin + config.intake_path;
  const body = JSON.stringify(
    Object.fromEntries(config.intake_fields.map((field) => [field, SAMPLE[field] ?? ""])),
    null,
    2,
  );
  // Свежий ключ подставляем прямо в пример: пока он на экране, отсюда получается
  // команда, которую можно выполнить не редактируя. Показан он всё равно строкой
  // выше, так что нового секрета этот кусок не открывает.
  const snippet = [
    `curl -X POST ${intakeUrl} \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -H "${config.intake_header}: ${freshKey || KEY_PLACEHOLDER}" \\`,
    `  -d '${body}'`,
  ].join("\n");

  const copy = async (text: string) => {
    if (await copyText(text)) toast(t("copied"));
    else toast(t("copyFailed"), true);
  };

  // Выбранный сотрудник отключён: настройка говорит одно, заявки уходят другому.
  // Молчать об этом нельзя — обращения с сайта продолжают приходить, а человек,
  // которому их поручили, их не видит.
  const substituted = config.chosen_id !== null && config.chosen_id !== config.manager_id;
  // Отключённого нет в списке коллег (`/people` отдаёт активных) — иначе выбор
  // показывал бы пустую строку вместо человека, которого туда вписали.
  const missing = config.chosen_id !== null && !people.some((p) => p.id === config.chosen_id);

  return (
    <div className="page page-narrow">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div>
          <h1 className="page-title">{t("leads")}</h1>
          <div className="page-sub">{t("leadsSub")}</div>
        </div>
      </div>

      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 13, fontWeight: 500 }}>{t("leadsIntake")}</span>
          <span className={"chip " + (config.has_intake_key ? "chip-success" : "chip-warning")}>
            {config.has_intake_key ? t("leadsIntakeOpen") : t("leadsIntakeClosed")}
          </span>
          <button
            className="text-link"
            style={{ marginLeft: "auto" }}
            disabled={guard.busy}
            onClick={() => (config.has_intake_key ? setConfirmNew(true) : void createKey())}
          >
            {config.has_intake_key ? t("leadsKeyNew") : t("leadsKeyCreate")}
          </button>
        </div>
        <div className="field-desc">
          {config.has_intake_key ? t("leadsIntakeOpenDesc") : t("leadsIntakeClosedDesc")}
        </div>

        {freshKey && (
          <div style={{ marginTop: 12 }}>
            <button
              type="button"
              className="input"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                cursor: "pointer",
                width: "100%",
                textAlign: "left",
              }}
              onClick={() => void copy(freshKey)}
            >
              <span
                style={{
                  flex: 1,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {freshKey}
              </span>
              <Icon name="copy" size={13} />
            </button>
            <div className="field-desc">{t("leadsKeyOnce")}</div>
          </div>
        )}

        {config.has_intake_key && (
          <button
            className="text-link danger"
            style={{ marginTop: 14 }}
            onClick={() => setConfirmClose(true)}
          >
            {t("leadsCloseIntake")}
          </button>
        )}
      </div>

      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <label className="label" htmlFor="leads-manager">
          {t("leadsManager")}
        </label>
        <select
          id="leads-manager"
          className="input"
          value={config.chosen_id ?? ""}
          onChange={(e) => void chooseManager(e.target.value)}
        >
          <option value="">{t("leadsManagerOwner")}</option>
          {missing && <option value={config.chosen_id!}>{config.chosen_name}</option>}
          {people.map((person) => (
            <option key={person.id} value={person.id}>
              {person.name}
            </option>
          ))}
        </select>
        <div className="field-desc">{t("leadsManagerDesc")}</div>
        {substituted && (
          <div className="field-desc" style={{ color: "var(--warning)" }}>
            {t("leadsManagerSubstituted", { name: config.manager_name })}
          </div>
        )}
      </div>

      <div className="card card-pad">
        <label className="label">{t("leadsEndpoint")}</label>
        {/* Адрес копируют, чтобы вставить в код сайта, и проверяют это уже там.
            Через `copyText`: без HTTPS буфера обмена у браузера нет вовсе, и
            подсказка «Скопировано» появлялась бы над пустым буфером. */}
        <button
          type="button"
          className="input"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            cursor: "pointer",
            width: "100%",
            textAlign: "left",
          }}
          onClick={() => void copy(intakeUrl)}
        >
          <Icon name="link" size={13} />
          <span
            style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {intakeUrl}
          </span>
          <Icon name="copy" size={13} />
        </button>
        <div className="field-desc">{t("leadsEndpointDesc", { header: config.intake_header })}</div>

        <div style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 13, fontWeight: 500 }}>{t("leadsSnippet")}</span>
          <button className="text-link" style={{ marginLeft: "auto" }} onClick={() => void copy(snippet)}>
            {t("leadsSnippetCopy")}
          </button>
        </div>
        <pre className="code-block">{snippet}</pre>
        <div className="field-desc">{t("leadsSnippetDesc")}</div>
        <div className="field-desc">
          {t("leadsHoneypotDesc", { field: config.honeypot_field })}
        </div>
      </div>

      {confirmNew && (
        <ConfirmModal
          text={t("leadsKeyNewConfirm")}
          confirmLabel={t("confirm")}
          onConfirm={() => void createKey()}
          onClose={() => setConfirmNew(false)}
        />
      )}
      {confirmClose && (
        <ConfirmModal
          text={t("leadsCloseIntakeConfirm")}
          confirmLabel={t("confirm")}
          danger
          onConfirm={() => void closeIntake()}
          onClose={() => setConfirmClose(false)}
        />
      )}
    </div>
  );
}
