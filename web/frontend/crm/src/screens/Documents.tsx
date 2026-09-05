import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ContextMenu, punktyDlyaZapisi, useContextMenu } from "../components/ContextMenu";
import { Icon } from "../components/Icon";
import { VyborKlienta } from "../components/VyborKlienta";
import { Chip, Dochitat, EmptyState, Modal, ScreenLoading } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useDebounced } from "../lib/debounce";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { formatDate } from "../lib/format";
import { SpisokPoKategoriyam } from "../components/SpisokPoKategoriyam";
import {
  DOC_KINDS,
  DOC_SORTS,
  DOC_STATUSES,
  kindLabel,
  sortLabel,
  statusLabel,
  statusVariant,
} from "../lib/documents";

/** По скольку бланков дочитывается список. */
const NA_STRANITSE = 100;

export function Documents() {
  const { t, locale, toast, toastError } = useApp();
  const navigate = useNavigate();
  const kontekst = useContextMenu();
  const [params] = useSearchParams();
  const [data, setData] = useState<any>(null);
  const [status, setStatus] = useState("");
  // Виды, СНЯТЫЕ с показа. Храним снятые, а не выбранные: пустое множество тогда
  // означает «показываем всё», и новый вид бумаги появляется в списке сам, а не
  // пропадает до тех пор, пока кто-нибудь не допишет его в перечень.
  const [snyaty, setSnyaty] = useState<string[]>([]);
  const [poryadok, setPoryadok] = useState("new");
  const [query, setQuery] = useState("");
  const [scan, setScan] = useState("");
  // До какой страницы дочитан список. Прежде экран просил сотню бланков и на
  // этом заканчивался — а в подзаголовке честно писал «всего N». Сам сообщал,
  // что показывает часть, и ничего с этим сделать не давал.
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
  useLiveTopic(["documents", "waybills"], () => setAttempt((a) => a + 1));
  const scanInput = useRef<HTMLInputElement | null>(null);
  const focused = useRef(false);

  const { failure, fail, clear } = useFailure();

  const search = useDebounced(query);

  // Отбор без номера страницы: положи страницу сюда — и смена отбора станет
  // неотличима от перехода на следующую. Один и тот же отбор берут и загрузка,
  // и дочитка, разнятся они только номером страницы.
  const otbor = useMemo(() => {
    const args = new URLSearchParams({ per_page: String(NA_STRANITSE) });
    if (search.trim()) args.set("search", search.trim());
    if (status) args.set("status", status);
    if (poryadok !== "new") args.set("sort", poryadok);
    // Снятые виды убираются НА СЕРВЕРЕ, а не прячутся на экране: спрятанная
    // строка продолжала бы занимать место в дочитанной сотне и считаться в
    // «всего N» — то есть «снял заказы» давало бы семь строк под подписью
    // «всего 100».
    if (snyaty.length) {
      for (const vid of DOC_KINDS) {
        if (!snyaty.includes(vid)) args.append("kind", vid);
      }
    }
    return `/documents?${args}`;
  }, [search, status, poryadok, snyaty]);

  useEffect(() => {
    // Отбор переключают быстрее, чем отвечает сервер: без этого счётчика ответ
    // на прошлый набор мог бы лечь поверх текущего, и на экране оказался бы
    // список позапрошлого фильтра. Приём тот же, что в отчётах и палитре.
    let current = true;
    otbor_spiska.current = otbor;
    clear();
    api
      .get(`${otbor}&page=1`)
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
  }, [otbor, attempt, fail, clear]);

  /** Дочитать список.
   *
   * Отдельным действием, а не номером страницы в пути загрузки, и номер растёт
   * ПОСЛЕ удачного ответа. Иначе отказ на второй странице оставлял бы счётчик
   * на двойке, а следующее нажатие просило бы третью — вторая пропускалась бы
   * навсегда, и список молча недосчитывался бы сотни бланков.
   *
   * Отказ говорит о себе всплывающей жалобой, а не через `fail`: `fail` рисует
   * экран «не удалось загрузить», а он виден только пока показывать нечего.
   * После первой удачной загрузки отказ дочитки не показал бы ничего вовсе —
   * кнопка просто переставала бы отвечать.
   */
  const dochitat = async () => {
    if (dochityvaem) return;
    setDochityvaem(true);
    const sprosheno = otbor;
    try {
      const dalshe = await api.get<{ items: any[]; total: number }>(
        `${otbor}&page=${stranitsa + 1}`,
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

  // Сканер работает как клавиатура: набирает номер и жмёт Enter. Поле должно
  // ждать его сразу — иначе первый скан уходит в пустоту, и приёмщик решает,
  // что сканер сломан.
  //
  // Фокус ставит ref, а не эффект на монтировании: до прихода списка экран
  // отдаёт заглушку, поля ещё нет, и `ref.current` в таком эффекте пуст. Так
  // это и было — первый скан молча пропадал. Ref срабатывает ровно тогда,
  // когда поле появилось; флаг не даёт перехватывать фокус потом, иначе он
  // уводил бы курсор из поиска при каждой перезагрузке списка.
  const catchScanner = (el: HTMLInputElement | null) => {
    scanInput.current = el;
    if (el && !focused.current) {
      focused.current = true;
      el.focus();
    }
  };

  const lookup = async () => {
    const number = scan.trim();
    if (!number) return;
    setScan("");
    try {
      const doc = await api.get(`/documents/by-number/${encodeURIComponent(number)}`);
      navigate(`/documents/${doc.id}`);
    } catch (e) {
      // «Такого бланка нет» и «сервер не ответил» — разные беды, и решения у
      // приёмщика разные. Пока любой отказ читался как первое, упавший сервер
      // отправлял человека искать бумагу, которая у него в руках. Различаем по
      // ответу сервера — ровно как сканер штрихкодов.
      if (e instanceof ApiError && e.status === 404) {
        toast(t("docNotFound", { code: number }), true);
      } else {
        toastError(e);
      }
      scanInput.current?.focus();
    }
  };

  return (
    <div className="page page-wide">
      <ContextMenu menu={kontekst.menu} zakryt={kontekst.zakryt} />
      <div className="page-head">
        <div>
          <h1 className="page-title">{t("documents")}</h1>
          <div className="page-sub">{t("documentsSub", { total: data.total })}</div>
        </div>
        <button className="btn btn-primary" onClick={() => setShowNew(true)}>
          <Icon name="plus" stroke={2} />
          {t("newDocument")}
        </button>
      </div>

      {/* Скан — главный способ найти бланк за стойкой, поэтому он отдельным
          блоком сверху, а не одним из полей фильтра. */}
      <div className="scan-box">
        <Icon name="scan" size={18} className="scan-icon" />
        <input
          ref={catchScanner}
          className="scan-input"
          placeholder={t("scanPlaceholder")}
          value={scan}
          onChange={(e) => setScan(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void lookup();
          }}
        />
        <span className="scan-hint">{t("scanHint")}</span>
      </div>

      <div className="doc-toolbar">
        <div className="search-field">
          <Icon name="search" size={15} className="" />
          <input
            placeholder={t("docSearchPlaceholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">{t("allStatuses")}</option>
          {DOC_STATUSES.map((s) => (
            <option key={s} value={s}>
              {statusLabel(t, s)}
            </option>
          ))}
        </select>
        {/* Порядок — закрытым перечнем, как и на сервере: имя колонки из
            запроса означало бы `ORDER BY` по чему угодно. */}
        <select
          className="input sort-select"
          value={poryadok}
          onChange={(e) => setPoryadok(e.target.value)}
          aria-label={t("sortLabel")}
        >
          {DOC_SORTS.map((s) => (
            <option key={s} value={s}>
              {sortLabel(t, s)}
            </option>
          ))}
        </select>
      </div>

      {/* Чипы видов: нажатие снимает вид с показа, повторное возвращает. Число
          рядом — серверное и НЕ меняется от снятия: считается оно без отбора по
          виду, иначе, сняв квитанции, человек потерял бы и число рядом с ними,
          то есть способ их вернуть. */}
      <div className="kind-chips">
        <button
          className={"filter-chip" + (snyaty.length === 0 ? " active" : "")}
          onClick={() => setSnyaty([])}
        >
          {t("allKinds")}
        </button>
        {DOC_KINDS.map((vid) => (
          <button
            key={vid}
            className={"filter-chip" + (snyaty.includes(vid) ? "" : " active")}
            onClick={() =>
              setSnyaty((bylo) =>
                bylo.includes(vid) ? bylo.filter((v) => v !== vid) : [...bylo, vid],
              )
            }
          >
            {kindLabel(t, vid)}
            <span className="chip-schyot">{data.counts?.[vid] ?? 0}</span>
          </button>
        ))}
      </div>

      <div className="list-card">
        <SpisokPoKategoriyam
          pamyat="documents:kind"
          kategorii={DOC_KINDS.map((vid) => ({ key: vid, label: kindLabel(t, vid) }))}
          stroki={data.items as any[]}
          kategoriyaStroki={(doc: any) => doc.kind}
          vsego={data.counts}
          klyuchStroki={(doc: any) => doc.id}
          render={(doc: any) => (
            <Link
              to={`/documents/${doc.id}`}
              className="list-row hoverable"
              onContextMenu={(e) => kontekst.otkryt(e, punktyDlyaZapisi(`/documents/${doc.id}`, t, navigate))}
            >
              <span className="doc-number">{doc.number}</span>
              <div className="list-row-text">
                <div className="truncate" style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                  {doc.payload?.fields?.item || "—"}
                </div>
                <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>
                  {doc.payload?.client?.name || "—"}
                </div>
              </div>
              <span className="doc-row-date" style={{ width: 90, textAlign: "right", color: "var(--faint)", fontSize: 12, flexShrink: 0 }}>
                {formatDate(doc.created_at, locale)}
              </span>
              <span className="doc-row-status" style={{ width: 130, flexShrink: 0, display: "flex", justifyContent: "flex-end" }}>
                <Chip variant={statusVariant(doc.status)}>{statusLabel(t, doc.status)}</Chip>
              </span>
            </Link>
          )}
        />
        <Dochitat
          pokazano={data.items.length}
          vsego={data.total}
          zanyat={dochityvaem}
          onClick={() => void dochitat()}
        />
        {data.items.length === 0 && (
          <EmptyState icon="receipt"
            title={query || status ? t("nothingFound", { q: query }) : t("noDocuments")}
            sub={query || status ? t("tryDifferent") : t("noDocumentsHint")}
          />
        )}
      </div>

      {showNew && (
        <NewDocumentModal
          onClose={() => setShowNew(false)}
          onCreated={(doc) => navigate(`/documents/${doc.id}`)}
        />
      )}
    </div>
  );
}

const FIELDS = ["serial", "condition", "accessories", "problem", "estimate", "terms"] as const;

const FIELD_LABEL = {
  item: "docItem",
  serial: "docSerial",
  condition: "docCondition",
  accessories: "docAccessories",
  problem: "docProblem",
  estimate: "docEstimate",
  terms: "docTerms",
} as const;

export function NewDocumentModal({
  dealId,
  clientId,
  onClose,
  onCreated,
}: {
  dealId?: number;
  clientId?: number;
  onClose: () => void;
  onCreated: (doc: any) => void;
}) {
  const { t, user, toastError } = useApp();
  // Список клиентов формы: `null` — не приехал. Пустой выпадающий список молча
  // превращал бы бланк для клиента из справочника в бланк «для прохожего».
  // Имя выбранного клиента держим отдельно от бланка: в самом бланке
  // `client_name` — это имя человека БЕЗ карточки, и попади туда имя
  // выбранного, оно уехало бы на сервер вместе с его же номером.
  const [imya_klienta, setImyaKlienta] = useState("");
  // Засов, а не флаг: бланк получает номер, и второй бланк на ту же вещь —
  // это вторая бумага с другим номером. На руках у клиента останется одна, а в
  // системе будут висеть обе, и закроют потом не ту. Отпускаем только на
  // отказе: при успехе уходим в созданный бланк.
  const guard = useGuard();
  const [form, setForm] = useState<Record<string, string>>({
    client_id: clientId ? String(clientId) : "",
    client_name: "",
    client_phone: "",
    item: "",
    serial: "",
    condition: "",
    accessories: "",
    problem: "",
    estimate: "",
    terms: "",
    // Бумагу печатают под клиента, поэтому язык по умолчанию — язык сотрудника,
    // но менять его можно на любой из трёх.
    locale: user?.locale === "en" ? "en" : "ru",
  });

  const set = (key: string) => (e: any) => setForm((f) => ({ ...f, [key]: e.target.value }));

  // Клиента может не быть в базе: человек стоит у стойки, и заводить карточку
  // до квитанции — лишний шаг. Сервер это принимает, значит принимает и форма.
  const walkIn = !clientId && !form.client_id;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!guard.take()) return;
    try {
      onCreated(
        await api.post("/documents", {
          ...form,
          client_id: form.client_id ? Number(form.client_id) : null,
          deal_id: dealId ?? null,
        }),
      );
    } catch (err) {
      toastError(err);
      guard.free();
    }
  };

  return (
    <Modal title={t("newDocument")} onClose={onClose} wide>
      <form onSubmit={submit}>
        <div className="deal-fields">
          {!clientId && (
            <div className="field">
              <label className="label">{t("client")}</label>
              <VyborKlienta
                value={form.client_id ? Number(form.client_id) : null}
                imya={imya_klienta || null}
                pustoy
                onPick={(kto, imya) => {
                  setImyaKlienta(imya ?? "");
                  setForm((f) => ({ ...f, client_id: kto ? String(kto) : "" }));
                }}
              />
            </div>
          )}
          {walkIn && (
            <>
              <div className="field">
                <label className="label">{t("name")}</label>
                <input className="input" value={form.client_name} onChange={set("client_name")} required />
              </div>
              <div className="field">
                <label className="label">{t("phone")}</label>
                <input className="input" value={form.client_phone} onChange={set("client_phone")} />
              </div>
            </>
          )}
          <div className="field">
            <label className="label">{t("docLanguage")}</label>
            <select className="input" value={form.locale} onChange={set("locale")}>
              <option value="ru">Русский</option>
              <option value="en">English</option>
              <option value="uk">Українська</option>
            </select>
            <div className="field-desc">{t("docLanguageHint")}</div>
          </div>
        </div>

        <div className="field" style={{ marginTop: 4 }}>
          <label className="label">{t("docItem")} *</label>
          <input className="input" maxLength={160} value={form.item} onChange={set("item")} autoFocus required />
        </div>

        <div className="deal-fields">
          {FIELDS.map((key) => (
            <div className="field" key={key}>
              <label className="label">{t(FIELD_LABEL[key])}</label>
              <input className="input" maxLength={160} value={form[key]} onChange={set(key)} />
            </div>
          ))}
        </div>

        <div className="field-desc" style={{ margin: "2px 0 18px" }}>{t("docFieldLimit")}</div>

        <button className="btn btn-primary" style={{ width: "100%" }} disabled={guard.busy}>
          {t("create")}
        </button>
      </form>
    </Modal>
  );
}
