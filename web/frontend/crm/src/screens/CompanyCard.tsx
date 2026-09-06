import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, ScreenLoading } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { can } from "../lib/permissions";
import { useFailure } from "../lib/failure";
import type { TranslationKey } from "../lib/i18n";
import type { Company } from "./Companies";

/** Поля карточки по разделам.
 *
 * Списком, а не разметкой: полей восемнадцать, и каждое, набранное руками,
 * означало бы восемь строк JSX с одинаковым обработчиком. Разделы здесь не
 * украшение — реквизиты ищут группами: «где у нас счёт» и «кто подписывает».
 */
type Field = { key: keyof Company; label: TranslationKey; hint?: TranslationKey; wide?: boolean };

const SECTIONS: { title: TranslationKey; fields: Field[] }[] = [
  {
    title: "companyRequisites",
    fields: [
      { key: "legal_name", label: "companyLegalName", hint: "companyLegalNameHint", wide: true },
      { key: "tax_number", label: "companyTaxNumber" },
      { key: "reg_number", label: "companyRegNumber" },
      { key: "vat_number", label: "companyVatNumber" },
      { key: "legal_address", label: "companyLegalAddress", wide: true },
      {
        key: "actual_address",
        label: "companyActualAddress",
        hint: "companyActualAddressHint",
        wide: true,
      },
    ],
  },
  {
    title: "contacts",
    fields: [
      { key: "phone", label: "phone" },
      { key: "email", label: "email" },
      { key: "website", label: "website", wide: true },
    ],
  },
  {
    title: "companyBankSection",
    fields: [
      { key: "bank_name", label: "companyBankName", wide: true },
      { key: "bank_account", label: "companyAccount" },
      { key: "bank_code", label: "companyBankCode", hint: "companyBankCodeHint" },
    ],
  },
  {
    title: "companySignSection",
    fields: [
      { key: "signatory_name", label: "companySignatory" },
      {
        key: "signatory_basis",
        label: "companySignatoryBasis",
        hint: "companySignatoryBasisHint",
      },
      { key: "signature_path", label: "companySignatureImage", hint: "companyImagesHint" },
      { key: "stamp_path", label: "companyStampImage" },
    ],
  },
];

export function CompanyCard() {
  const { id } = useParams();
  const { t, user, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [company, setCompany] = useState<Company | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Право, а не «root». Сервер спрашивает `companies.edit` / `companies.delete`
  // (см. web/api/routes/companies.py), и менеджер, которому его выдали, видел
  // пункт меню, открывал карточку — и все поля были заперты с подписью «правит
  // владелец». Право выдано и работает, интерфейс его не признавал.
  const mayEdit = can(user, "companies.edit");
  const mayDelete = can(user, "companies.delete");

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      setCompany(await api.get<Company>(`/companies/${id}`));
    } catch (e) {
      // Записи нет или она не наша: показывать «попробуйте ещё раз» тут не о
      // чем — повтор вернёт тот же ответ. Возвращаемся в список, как и раньше.
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/companies");
        return;
      }
      // Всё остальное — беда связи или сервера. Карточку не бросаем: адрес в
      // строке верный, и повторить имеет смысл именно его, а не список.
      fail(e);
    }
  }, [id, toastError, navigate, fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!company) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const patch = async (data: Record<string, unknown>) => {
    try {
      setCompany(await api.patch<Company>(`/companies/${id}`, data));
    } catch (e) {
      toastError(e);
      void load();
    }
  };

  const makeDefault = async () => {
    try {
      await api.post(`/companies/${id}/default`);
      await load();
      toast(t("saved"));
    } catch (e) {
      toastError(e);
    }
  };

  return (
    <div className="page page-narrow">
      <Link to="/companies" className="back-link">
        <Icon name="arrowLeft" size={14} />
        {t("companies")}
      </Link>

      <div className="page-head" style={{ alignItems: "flex-start" }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <input
            className="title-input"
            defaultValue={company.name}
            disabled={!mayEdit}
            onBlur={(e) => {
              const value = e.target.value.trim();
              if (value && value !== company.name) void patch({ name: value });
            }}
          />
          <div className="page-sub" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {company.is_default ? (
              <Chip variant="brand">{t("companyDefaultTag")}</Chip>
            ) : (
              mayEdit && (
                <button className="btn btn-secondary btn-sm" onClick={() => void makeDefault()}>
                  {t("companyMakeDefault")}
                </button>
              )
            )}
          </div>
        </div>
        {mayDelete && (
          <button className="btn btn-secondary" onClick={() => setConfirmDelete(true)}>
            <Icon name="trash" size={14} />
            {t("delete")}
          </button>
        )}
      </div>

      {/* Менеджеру объясняем, почему поля не поддаются, — на месте, а не
          отказом сервера после того, как он набрал новый номер счёта. */}
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="field-desc" style={{ marginTop: 0 }}>
          {mayEdit ? t("companyDefaultHint") : t("companyReadOnly")}
        </div>
      </div>

      {SECTIONS.map((section) => (
        <div key={section.title} className="card card-pad" style={{ marginBottom: 20 }}>
          <div className="metric-title" style={{ marginBottom: 12 }}>
            {t(section.title)}
          </div>
          <div className="deal-fields">
            {section.fields.map((field) => (
              <div
                key={field.key}
                className="field"
                style={field.wide ? { gridColumn: "1 / -1" } : undefined}
              >
                <label className="label">{t(field.label)}</label>
                <input
                  className="input"
                  disabled={!mayEdit}
                  defaultValue={String(company[field.key] ?? "")}
                  onBlur={(e) => {
                    if (e.target.value !== company[field.key]) {
                      void patch({ [field.key]: e.target.value });
                    }
                  }}
                />
                {field.hint && <div className="field-desc">{t(field.hint)}</div>}
              </div>
            ))}
          </div>
        </div>
      ))}

      <div className="card card-pad">
        <div className="field">
          <label className="label">{t("companyNote")}</label>
          <textarea
            className="input"
            rows={3}
            disabled={!mayEdit}
            defaultValue={company.note}
            onBlur={(e) => {
              if (e.target.value !== company.note) void patch({ note: e.target.value });
            }}
          />
          <div className="field-desc">{t("companyNoteHint")}</div>
        </div>
      </div>

      {confirmDelete && (
        <ConfirmModal
          text={t("deleteCompanyConfirm", { name: company?.name ?? "" })}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => {
            void (async () => {
              try {
                await api.del(`/companies/${id}`);
                toast(t("deleted"));
                navigate("/companies");
              } catch (e) {
                toastError(e);
              }
            })();
          }}
          onClose={() => setConfirmDelete(false)}
        />
      )}
    </div>
  );
}
