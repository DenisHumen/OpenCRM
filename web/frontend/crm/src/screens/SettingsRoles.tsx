import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Icon } from "../components/Icon";
import { ConfirmModal, EmptyState, ScreenLoading } from "../components/ui";
import {
  api,
  type PermissionArea,
  type Role,
  type RolePreset,
} from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import type { TranslationKey } from "../lib/i18n";
import { moduleOn } from "../lib/modules";

/** Подписи столбцов. Ключи действий приходят с сервера (core/permissions.py). */
const ACTION_LABEL: Record<string, TranslationKey> = {
  view: "permView",
  create: "permCreate",
  edit: "permEdit",
  delete: "permDelete",
  restore: "permRestore",
  issue: "permIssue",
  move_stage: "permMoveStage",
  view_others: "permViewOthers",
  view_amounts: "permViewAmounts",
  manage: "permManage",
};

/** Подписи строк. Блоки берут название своего раздела, системные — своё. */
const AREA_LABEL: Record<string, TranslationKey> = {
  backups: "areaBackups",
  clients: "clients",
  deals: "deals",
  companies: "companies",
  documents: "documents",
  tasks: "tasks",
  boards: "boards",
  warehouse: "modWarehouse",
  labels: "modLabels",
  orders: "modOrders",
  waybills: "modWaybills",
  reports: "modReports",
  mail: "modMail",
  templates: "templates",
  monitoring: "modMonitoring",
  telephony: "modTelephony",
  telegram: "modTelegram",
  finance: "modFinance",
  staff: "areaStaff",
  roles: "areaRoles",
  settings: "areaSettings",
  audit: "areaAudit",
};

interface Matrix {
  areas: PermissionArea[];
  actions: string[];
  presets: RolePreset[];
}

const code = (area: string, action: string) => `${area}.${action}`;

export function SettingsRoles() {
  const { t, modules, toast, toastError } = useApp();
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [roles, setRoles] = useState<Role[] | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [draft, setDraft] = useState<{ name: string; codes: Set<string> } | null>(null);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<Role | null>(null);
  // Засов, а не флаг состояния: должность заводят кнопкой «Сохранить», и
  // вторая должность с тем же названием и тем же набором прав — это набор,
  // который потом правят в одной копии, а выдан он из другой.
  const guard = useGuard();

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      const [m, r] = await Promise.all([
        api.get<Matrix>("/roles/matrix"),
        api.get<{ items: Role[] }>("/roles"),
      ]);
      setMatrix(m);
      setRoles(r.items);
    } catch (e) {
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveTopic("roles", () => void load());

  // Всего прав в системе — для подписи «12 из 51». Считается по матрице, а не
  // константой: появился блок — число выросло само.
  const totalCodes = useMemo(
    () => (matrix ? matrix.areas.reduce((sum, area) => sum + area.actions.length, 0) : 0),
    [matrix],
  );

  if (!matrix || !roles) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const open = (role: Role) => {
    setCreating(false);
    setOpenId(role.id);
    setDraft({ name: role.name, codes: new Set(role.permissions ?? []) });
  };

  const startNew = (preset?: RolePreset) => {
    setCreating(true);
    setOpenId(null);
    setDraft({
      name: preset ? preset.name : "",
      codes: new Set(preset ? preset.permissions : []),
    });
  };

  const close = () => {
    setOpenId(null);
    setCreating(false);
    setDraft(null);
  };

  const toggle = (value: string) => {
    if (!draft) return;
    const next = new Set(draft.codes);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    setDraft({ ...draft, codes: next });
  };

  const setRow = (area: PermissionArea, on: boolean) => {
    if (!draft) return;
    const next = new Set(draft.codes);
    for (const action of area.actions) {
      if (on) next.add(code(area.key, action));
      else next.delete(code(area.key, action));
    }
    setDraft({ ...draft, codes: next });
  };

  const save = async () => {
    if (!draft || !guard.take()) return;
    try {
      const body = { name: draft.name, permissions: [...draft.codes] };
      if (creating) {
        await api.post<Role>("/roles", body);
        toast(t("roleCreated"));
      } else {
        await api.patch<Role>(`/roles/${openId}`, body);
        toast(t("roleSaved"));
      }
      await load();
      close();
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const makeDefault = async (role: Role) => {
    try {
      await api.post(`/roles/${role.id}/default`, {});
      await load();
      toast(t("roleSaved"));
    } catch (e) {
      toastError(e);
    }
  };

  const remove = async () => {
    if (!removing) return;
    try {
      await api.del(`/roles/${removing.id}`);
      await load();
      toast(t("roleDeleted"));
      if (openId === removing.id) close();
    } catch (e) {
      toastError(e);
    } finally {
      setRemoving(null);
    }
  };

  return (
    <div className="page page-wide">
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 22 }}>
        <div>
          <h1 className="page-title">{t("roles")}</h1>
          <div className="page-sub">
            {t("rolesSub")}{" "}
            <Link to="/docs?statya=prava" className="text-link">{t("rolesGuideLink")}</Link>
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => startNew()}>
          <Icon name="plus" size={15} />
          {t("newRole")}
        </button>
      </div>

      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <div className="field-desc" style={{ marginTop: 0 }}>{t("rolesHint")}</div>
      </div>

      {/* Пресеты: готовое начало вместо пустого конструктора. То же решение,
          что с пресетами воронки, — и по той же причине: собирать матрицу из
          полусотни клеток вместо того, чтобы работать, никто не станет. */}
      {!draft && (
        <div style={{ marginBottom: 18 }}>
          <div className="label" style={{ marginBottom: 8 }}>{t("rolePresets")}</div>
          <div className="preset-row">
            {matrix.presets.map((preset) => (
              <button key={preset.key} className="preset-card" onClick={() => startNew(preset)}>
                <span className="preset-name">{preset.name}</span>
                <span className="preset-hint">{preset.hint}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {draft ? (
        <div className="card card-pad">
          <div className="field" style={{ marginBottom: 18 }}>
            <label className="label" htmlFor="role-name">{t("roleName")}</label>
            <input
              id="role-name"
              className="input"
              value={draft.name}
              placeholder={t("roleNamePlaceholder")}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </div>

          <div className="field-desc" style={{ marginTop: 0, marginBottom: 14 }}>
            {t("permMatrixHint")}
          </div>

          <div className="perm-matrix-wrap">
            <table className="perm-matrix">
              <thead>
                <tr>
                  <th />
                  {matrix.actions.map((action) => (
                    <th key={action}>{t(ACTION_LABEL[action] ?? "permView")}</th>
                  ))}
                  <th />
                </tr>
              </thead>
              <tbody>
                {matrix.areas.map((area) => {
                  // Строка блока, который выключен, остаётся видимой: право на
                  // него выдать можно — просто оно ничего не даёт, пока блок не
                  // включат. Прятать строку значило бы заставить владельца
                  // вспоминать про права после каждого включения блока.
                  const off = area.module !== null && !moduleOn(modules, area.module);
                  const all = area.actions.every((a) => draft.codes.has(code(area.key, a)));
                  return (
                    <tr key={area.key} className={off ? "perm-row-off" : ""}>
                      <th scope="row">
                        <span className="perm-area">{t(AREA_LABEL[area.key] ?? "roles")}</span>
                        {off && <span className="perm-why">{t("permModuleOff")}</span>}
                      </th>
                      {matrix.actions.map((action) => {
                        const value = code(area.key, action);
                        const exists = area.actions.includes(action);
                        return (
                          <td key={action}>
                            {exists ? (
                              <input
                                type="checkbox"
                                aria-label={`${area.key}.${action}`}
                                checked={draft.codes.has(value)}
                                onChange={() => toggle(value)}
                              />
                            ) : (
                              // Пустая клетка, а не выключенная галочка:
                              // «этого действия здесь не бывает» и «не выдано»
                              // должны выглядеть по-разному.
                              <span className="perm-na">—</span>
                            )}
                          </td>
                        );
                      })}
                      <td>
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => setRow(area, !all)}
                        >
                          {all ? t("permRowNone") : t("permRowAll")}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div style={{ display: "flex", gap: 8, marginTop: 18 }}>
            <button className="btn btn-primary" disabled={guard.busy} onClick={() => void save()}>
              {t("save")}
            </button>
            <button className="btn" onClick={close}>{t("cancel")}</button>
            <span style={{ flex: 1 }} />
            <span className="field-desc" style={{ margin: 0 }}>
              {t("rolePermissionsCount", { n: draft.codes.size, total: totalCodes })}
            </span>
          </div>
        </div>
      ) : roles.length === 0 ? (
        <EmptyState icon="lock" title={t("noRoles")} sub={t("noRolesHint")} />
      ) : (
        <div className="list-card">
          {roles.map((role) => (
            <div key={role.id} className="module-row" onClick={() => open(role)}>
              <span className="module-icon">
                <Icon name="staff" size={17} />
              </span>
              <span className="module-text">
                <span className="module-name">
                  {role.name}
                  {role.is_default && <span className="module-tag">{t("roleDefault")}</span>}
                </span>
                <span className="module-about">
                  {t("rolePermissionsCount", {
                    n: role.permissions?.length ?? 0,
                    total: totalCodes,
                  })}
                  {" · "}
                  {role.users_count
                    ? t("roleUsersCount", { n: role.users_count })
                    : t("roleUsersNone")}
                </span>
              </span>
              {!role.is_default && (
                <button
                  className="btn btn-secondary btn-sm"
                  title={t("roleMakeDefault")}
                  onClick={(e) => {
                    e.stopPropagation();
                    void makeDefault(role);
                  }}
                >
                  <Icon name="check" size={14} />
                </button>
              )}
              <button
                className="btn btn-secondary btn-sm"
                aria-label={t("delete")}
                onClick={(e) => {
                  e.stopPropagation();
                  setRemoving(role);
                }}
              >
                <Icon name="trash" size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      {removing && (
        <ConfirmModal
          text={t("roleDeleteConfirm", { name: removing.name })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void remove()}
          onClose={() => setRemoving(null)}
        />
      )}
    </div>
  );
}
