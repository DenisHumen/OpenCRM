import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";

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
  const [items, setItems] = useState<Company[] | null>(null);
  const [showNew, setShowNew] = useState(false);

  const isRoot = user?.role === "root";

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ items: Company[] }>("/companies");
      setItems(data.items);
    } catch (e) {
      toastError(e);
    }
  }, [toastError]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!items) return <ScreenLoading />;

  return (
    <div className="page page-narrow">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("companies")}</h1>
          <div className="page-sub">{t("companiesSub", { total: items.length })}</div>
        </div>
        {/* Кнопка есть только у root: у остальных нажимать её было бы
            приглашением к отказу сервера. */}
        {isRoot && (
          <button className="btn btn-primary" onClick={() => setShowNew(true)}>
            <Icon name="plus" stroke={2} />
            {t("newCompany")}
          </button>
        )}
      </div>

      {items.length === 0 ? (
        <EmptyState title={t("noCompanies")} sub={t("noCompaniesHint")} />
      ) : (
        <div className="list-card">
          {items.map((company) => (
            <Link key={company.id} to={`/companies/${company.id}`} className="list-row hoverable">
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
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      onCreated(await api.post<Company>("/companies", { name: name.trim() }));
    } catch (err) {
      toastError(err);
      setBusy(false);
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
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
