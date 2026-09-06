import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ContextMenu, punktyDlyaZapisi, useContextMenu } from "../components/ContextMenu";
import { Icon } from "../components/Icon";
import { Chip, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { can } from "../lib/permissions";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";

export interface Company {
  id: number;
  name: string;
  legal_name: string;
  is_default: boolean;
  tax_number: string;
  reg_number: string;
  vat_number: string;
  legal_address: string;
  actual_address: string;
  phone: string;
  email: string;
  website: string;
  bank_name: string;
  bank_account: string;
  bank_code: string;
  signatory_name: string;
  signatory_basis: string;
  signature_path: string;
  stamp_path: string;
  note: string;
}

export function Companies() {
  const { t, user, toastError } = useApp();
  const navigate = useNavigate();
  const kontekst = useContextMenu();
  const [items, setItems] = useState<Company[] | null>(null);
  const [showNew, setShowNew] = useState(false);

  // Право, а не «root». Сервер спрашивает `companies.edit` / `companies.delete`
  // (см. web/api/routes/companies.py), и менеджер, которому его выдали, видел
  // пункт меню, открывал карточку — и все поля были заперты с подписью «правит
  // владелец». Право выдано и работает, интерфейс его не признавал.
  const mayCreate = can(user, "companies.create");

  const { failure, fail, clear } = useFailure();

  const load = useCallback(async () => {
    clear();
    try {
      const data = await api.get<{ items: Company[] }>("/companies");
      setItems(data.items);
    } catch (e) {
      fail(e);
    }
  }, [fail, clear]);

  useEffect(() => {
    void load();
  }, [load]);

  useLiveTopic("companies", () => void load());

  if (!items) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  return (
    <div className="page page-narrow">
      <ContextMenu menu={kontekst.menu} zakryt={kontekst.zakryt} />
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("companies")}</h1>
          <div className="page-sub">{t("companiesSub", { total: items.length })}</div>
        </div>
        {/* Кнопка есть только у root: у остальных нажимать её было бы
            приглашением к отказу сервера. */}
        {mayCreate && (
          <button className="btn btn-primary" onClick={() => setShowNew(true)}>
            <Icon name="plus" stroke={2} />
            {t("newCompany")}
          </button>
        )}
      </div>

      {items.length === 0 ? (
        <EmptyState icon="building" title={t("noCompanies")} sub={t("noCompaniesHint")} />
      ) : (
        <div className="list-card">
          {items.map((company) => (
            <Link
              key={company.id}
              to={`/companies/${company.id}`}
              className="list-row hoverable"
              onContextMenu={(e) => kontekst.otkryt(e, punktyDlyaZapisi(`/companies/${company.id}`, t, navigate))}
            >
              <span className="module-icon">
                <Icon name="building" size={17} />
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                  {company.name}
                </div>
                <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>
                  {company.legal_name || company.tax_number || company.phone}
                </div>
              </div>
              {/* Признак основной — единственное, что важно увидеть в списке:
                  именно её реквизиты уйдут на бумагу, если не выбрать другую. */}
              {company.is_default && <Chip variant="brand">{t("companyDefaultTag")}</Chip>}
            </Link>
          ))}
        </div>
      )}

      {showNew && (
        <NewCompanyModal
          onClose={() => setShowNew(false)}
          onCreated={(company) => navigate(`/companies/${company.id}`)}
        />
      )}
    </div>
  );
}

/** Заводим фирму одним полем: реквизиты дописываются в карточке, когда дойдут
 *  руки до первого счёта. Форма на восемнадцать полей на этом шаге означала бы,
 *  что фирму не заведут вовсе. */
function NewCompanyModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (company: Company) => void;
}) {
  const { t, toastError } = useApp();
  const [name, setName] = useState("");
  // Засов, а не флаг: форма из одного поля отправляется Enter'ом, и вторая
  // фирма с тем же названием — это вторые реквизиты, которые однажды уедут на
  // бумагу вместо настоящих. Отпускаем только на отказе: при успехе уходим в
  // созданную карточку.
  const guard = useGuard();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      onCreated(await api.post<Company>("/companies", { name: name.trim() }));
    } catch (err) {
      toastError(err);
      guard.free();
    }
  };

  return (
    <Modal title={t("newCompany")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("companyShortName")}</label>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            required
          />
          <div className="field-desc">{t("companyShortNameHint")}</div>
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
