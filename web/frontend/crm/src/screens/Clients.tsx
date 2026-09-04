import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  ContextMenu,
  type PunktMenyu,
  punktyDlyaZapisi,
  useContextMenu,
} from "../components/ContextMenu";
import { Icon } from "../components/Icon";
import { SourcePicker } from "../components/SourcePicker";
import { Avatar, Chip, Dochitat, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { copyText } from "../lib/clipboard";
import { flagStrany } from "../lib/strany";
import { initials, relativeDay } from "../lib/format";

/** По скольку клиентов дочитывается список. */
const NA_STRANITSE = 100;

export function Clients() {
  const { t, locale, toastError } = useApp();
  const navigate = useNavigate();
  const kontekst = useContextMenu();

  const punktyKlienta = (client: any): PunktMenyu[] => {
    const punkty = punktyDlyaZapisi(`/clients/${client.id}`, t, navigate);
    if (client.email)
      punkty.push({ key: "email", label: t("copyEmail"), icon: "mail", run: () => void copyText(client.email) });
    if (client.phone)
      punkty.push({ key: "phone", label: t("copyPhone"), icon: "call", run: () => void copyText(client.phone) });
    return punkty;
  };
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [data, setData] = useState<any>(null);
  // До какой страницы дочитан список. Прежде экран просил сотню и на этом
  // заканчивался — а в подзаголовке писал «всего 3400». Сам сообщал, что
  // показывает тридцатую часть, и ничего с этим сделать не давал.
  const [stranitsa, setStranitsa] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Отбор, которому принадлежит показанный список. Ставится загрузкой,
  // сверяется дочиткой: пока вторая страница по «иван» едет, человек успевает
  // набрать «п», первая страница «п» заменяет список — и опоздавшая страница
  // дописывается к чужим находкам. На экране два отбора вперемешку, а «всего»
  // от прошлого.
  const otbor_spiska = useRef("");
  const [showNew, setShowNew] = useState(params.get("new") === "1");
  const [attempt, setAttempt] = useState(0);
  // Намёк живых обновлений — тот же перезапрос, что и по кнопке «повторить».
  useLiveTopic("clients", () => setAttempt((a) => a + 1));

  const { failure, fail, clear } = useFailure();

  const search = useDebounced(query);

  useEffect(() => {
    // Набирают быстрее, чем отвечает сервер: без этого счётчика ответ на
    // «Ив» ложился поверх ответа на «Иванов», и человек видел выдачу
    // позапрошлого запроса. Приём тот же, что в отчётах и палитре команд.
    let current = true;
    otbor_spiska.current = search;
    clear();
    api
      .get(`/clients?search=${encodeURIComponent(search)}&page=1&per_page=${NA_STRANITSE}`)
      .then((found) => {
        if (!current) return;
        setData(found);
        setStranitsa(1);
      })
      .catch((e) => {
        if (current) fail(e);
      });
    return () => {
      current = false;
    };
  }, [search, attempt, fail, clear]);

  /** Дочитать список.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер
   * растёт ПОСЛЕ удачного ответа. Иначе отказ на второй странице оставлял бы
   * счётчик на двойке, а следующее нажатие просило бы третью — вторая
   * пропускалась бы навсегда, и список молча недосчитывался бы сотни записей.
   *
   * Отказ говорит о себе всплывающей жалобой, а не через `fail`: `fail`
   * рисует экран «не удалось загрузить», а он виден только пока показывать
   * нечего. После первой удачной загрузки отказ дочитки не показал бы ничего
   * вовсе — кнопка просто переставала бы отвечать.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const sprosheno = search;
    try {
      const dalshe = await api.get<{ items: any[]; total: number }>(
        `/clients?search=${encodeURIComponent(search)}` +
          `&page=${stranitsa + 1}&per_page=${NA_STRANITSE}`,
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setData((bylo: any) =>
        bylo ? { ...dalshe, items: [...bylo.items, ...dalshe.items] } : dalshe,
      );
      setStranitsa((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  if (!data) {
    return <ScreenLoading error={failure} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("clients")}</h1>
          <div className="page-sub">{t("clientsSub", { total: data.total })}</div>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {/* Скачивание — работа браузера: он сам покажет ход, положит файл в
              «Загрузки» и возьмёт имя из Content-Disposition. Тянуть файл в
              память и собирать Blob значило бы делать всё это руками и хуже.
              Тот же приём, что в отчётах.

              Отбор уезжает В ССЫЛКЕ: файл обязан содержать то, что человек
              сейчас видит. Выгрузка «всего списка» с открытым поиском — это
              файл, которого он не просил, и понял бы он это, только открыв. */}
          <a
            className="btn"
            href={`/api/v1/clients/export.csv?search=${encodeURIComponent(search)}`}
          >
            <Icon name="download" size={13} />
            {t("exportCsv")}
          </a>
          <button className="btn btn-primary" onClick={() => setShowNew(true)}>
            <Icon name="plus" stroke={2} />
            {t("newClient")}
          </button>
        </div>
      </div>

      <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
        <div
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "0 12px",
            height: 36,
            border: "1px solid var(--border)",
            borderRadius: 8,
            background: "var(--surface)",
          }}
        >
          <Icon name="search" size={15} className="" />
          <input
            style={{ flex: 1, background: "none", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5, fontFamily: "var(--sans)" }}
            placeholder={t("searchClients")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
        </div>
      </div>

      <div className="list-card">
        <div className="list-header">
          <span style={{ width: 30 }} />
          <span style={{ flex: 1 }}>{t("client")}</span>
          <span style={{ width: 190 }}>{t("contact")}</span>
          <span style={{ width: 170 }}>{t("tags")}</span>
          <span style={{ width: 90, textAlign: "right" }}>{t("activity")}</span>
        </div>
        {data.items.map((client: any) => (
          <Link
            to={`/clients/${client.id}`}
            key={client.id}
            className="list-row hoverable"
            onContextMenu={(e) => kontekst.otkryt(e, punktyKlienta(client))}
          >
            <Avatar text={initials(client.name)} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{client.name}</div>
              <div style={{ color: "var(--faint)", fontSize: 12 }}>{client.company}</div>
            </div>
            <div style={{ width: 190, flexShrink: 0 }}>
              <div style={{ color: "var(--muted)", fontSize: 12.5 }}>
                {/* В списке — флагом и кодом: строка узкая, а название страны
                    съело бы место у телефона, ради которого столбец и заведён. */}
                {client.country && (
                  <span style={{ color: "var(--faint)" }}>
                    {flagStrany(client.country)} {client.country}{" "}
                  </span>
                )}
                {client.phone}
              </div>
              <div style={{ color: "var(--faint)", fontSize: 12 }}>{client.email}</div>
            </div>
            <div style={{ width: 170, display: "flex", gap: 6, flexShrink: 0, flexWrap: "wrap" }}>
              {client.tags.slice(0, 3).map((tag: string) => (
                <Chip key={tag}>{tag}</Chip>
              ))}
            </div>
            <div style={{ width: 90, textAlign: "right", color: "var(--faint)", fontSize: 12, flexShrink: 0 }}>
              {relativeDay(client.updated_at, locale)}
            </div>
          </Link>
        ))}
        <ContextMenu menu={kontekst.menu} zakryt={kontekst.zakryt} />
        <Dochitat
          pokazano={data.items.length}
          vsego={data.total}
          zanyat={dochityvaem}
          onClick={() => void dochitat()}
        />
        {data.items.length === 0 && (
          <EmptyState
            title={query ? t("nothingFound", { q: query }) : t("noClientsYet")}
            sub={query ? t("tryDifferent") : undefined}
          />
        )}
      </div>

      {showNew && (
        <NewClientModal
          onClose={() => {
            setShowNew(false);
            params.delete("new");
            setParams(params, { replace: true });
          }}
          onCreated={(client) => navigate(`/clients/${client.id}`)}
        />
      )}
    </div>
  );
}

function NewClientModal({ onClose, onCreated }: { onClose: () => void; onCreated: (c: any) => void }) {
  const { t, toastError } = useApp();
  const [form, setForm] = useState({
    name: "", company: "", phone: "", email: "", messenger: "", tags: "", source: "",
  });
  // Засов, а не флаг состояния: Enter в поле имени отправляет форму, и жмут
  // его дважды. Второй клиент с тем же именем расщепляет историю — половина
  // заявок и писем уезжает в карточку, которую никто больше не откроет.
  // Отпускаем только на отказе: при успехе уходим в созданную карточку.
  const guard = useGuard();

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      const client = await api.post("/clients", form);
      onCreated(client);
    } catch (err) {
      toastError(err);
      guard.free();
    }
  };

  return (
    <Modal title={t("newClient")} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="field">
          <label className="label">{t("name")}</label>
          <input className="input" value={form.name} onChange={set("name")} autoFocus required />
        </div>
        <div className="field">
          <label className="label">{t("company")}</label>
          <input className="input" value={form.company} onChange={set("company")} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <div className="field">
            <label className="label">{t("phone")}</label>
            <input className="input" value={form.phone} onChange={set("phone")} />
          </div>
          <div className="field">
            <label className="label">{t("email")}</label>
            <input className="input" value={form.email} onChange={set("email")} />
          </div>
        </div>
        <div className="field">
          <label className="label">{t("telegram")}</label>
          <input className="input" value={form.messenger} onChange={set("messenger")} placeholder="@username" />
        </div>
        <div className="field">
          <label className="label">{t("tagsCommaHint")}</label>
          <input className="input" value={form.tags} onChange={set("tags")} placeholder="branding, web" />
        </div>
        {/* Источник спрашивают один раз — при заведении карточки. Через неделю
            «откуда он пришёл» уже никто не вспомнит, и отчёт по рекламе
            превращается в столбик «не указан». */}
        <div className="field" style={{ marginBottom: 20 }}>
          <label className="label">{t("clientSource")}</label>
          <SourcePicker value={form.source} onChange={(next) => setForm((f) => ({ ...f, source: next }))} />
          <div className="field-desc">{t("clientSourceHint")}</div>
        </div>
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
