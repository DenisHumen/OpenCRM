import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { OrdersOfCard } from "../components/OrdersOfCard";
import { BoardCard } from "../components/BoardCard";
import { CallButton, CallsPanel } from "../components/CallsPanel";
import { Icon } from "../components/Icon";
import { NewBoardButton } from "../components/NewBoardButton";
import { SourcePicker } from "../components/SourcePicker";
import { Avatar, Chip, ConfirmModal, Dochitat, EmptyState, ItogSpiska, LoadFailed, Modal, ScreenLoading } from "../components/ui";
import { api, ApiError } from "../lib/api";
import { dropTarget } from "../lib/dnd";
import { kindLabel, paperLink, statusLabel, statusVariant } from "../lib/documents";
import { useApp } from "../lib/app";
import { useLiveTopic, useNachatayaPravka } from "../lib/live";
import { flagStrany, nazvanieStrany } from "../lib/strany";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import type { TranslationKey } from "../lib/i18n";
import {
  fileExt,
  fileSize,
  formatDate,
  formatDateTime,
  formatMoney,
  initials,
  relativeDay,
} from "../lib/format";
import { type Gated, moduleOn, shown } from "../lib/modules";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import { podpisSistemnoy } from "../lib/sistemnye_zapisi";
import { term } from "../lib/terms";
import { MailCompose, type MailSender } from "./Mail";
import { QuickTask } from "./Tasks";

/** Виды записей, которые ставит система, а не человек.
 *
 *  Список повторяет `SYSTEM_NOTE_KINDS` из `database/models/client.py`. Держать
 *  его здесь второй раз неприятно, но альтернатива — гонять состав видов
 *  отдельным запросом ради трёх строк, которые меняются раз в полгода вместе с
 *  новым блоком. Разъедется — тест на сервере всё равно откажет в удалении. */
const SYSTEM_NOTE_KINDS = new Set(["stage", "document", "stock"]);

const NOTE_ICONS: Record<string, string> = {
  note: "note", call: "call", meeting: "meeting", email: "email",
  // Переписка через бота фирмы — третий канал общения в той же ленте, рядом со
  // звонком и письмом. Значок тот же, что у раздела мессенджера.
  telegram: "send",
  // Смену этапа ставит подписчик на событие, а не человек: в списке «добавить»
  // её нет, а в ленте она обязана быть — иначе карточка клиента молчит о том,
  // что заявка доехала до следующего этапа.
  stage: "deals",
  // Бланк и списание приходят оттуда же — от подписчиков на события
  // (`core/subscriptions.py`). Значки те же, что в ленте заявки и у самих
  // разделов: строку узнаёшь, не читая её.
  document: "receipt",
  stock: "warehouse",
};

/**
 * Как называется вид записи.
 *
 * Отдельная таблица, а не `t(note.kind)`: ключ вида и ключ перевода совпадают
 * не у всех. У письма он свой (`emailNote`), а у записей, которые появляются
 * сами, — общий с лентой заявки. Пока подпись бралась прямо из вида, «stock»
 * попадал в ключ `stock` («Остаток») из словаря склада, а `document` перевода
 * не имел вовсе и показывался как есть, латиницей, в обеих локалях.
 *
 * Виды заданы сервером закрытым списком (`NOTE_KINDS` + `SYSTEM_NOTE_KINDS` в
 * `database/models/client.py`), и таблица покрывает их все. Заметить пропуск
 * здесь придётся глазами: записи ленты приходят нетипизированными, поэтому
 * `tsc` про новый вид не скажет — он и не сказал про два предыдущих.
 */
const NOTE_LABELS: Record<string, TranslationKey> = {
  note: "note",
  call: "call",
  meeting: "meeting",
  email: "emailNote",
  telegram: "modTelegram",
  stage: "feedStage",
  document: "feedDocument",
  stock: "feedStock",
};

type TabKey = "history" | "calls" | "files" | "papers" | "boards" | "deals";

/** Иконка записи ленты. У звонка она заодно показывает направление. */
function noteIcon(note: { kind: string; direction?: string | null }): string {
  if (note.kind !== "call") return NOTE_ICONS[note.kind] ?? "note";
  return note.direction === "out" ? "callOut" : "callIn";
}

/** По скольку заметок дочитывается карточка. */
const ZAMETOK_NA_STRANITSE = 100;

export function ClientCard() {
  const { id } = useParams();
  const { t, locale, user, workspace, modules, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [client, setClient] = useState<any>(null);
  const [notes, setNotes] = useState<any[]>([]);
  // Заметок у давнего клиента бывает больше сотни: карточка брала первую
  // сотню и молчала об остальных — самые ранние записи о нём просто
  // переставали существовать.
  const [vsegoZametok, setVsegoZametok] = useState(0);
  const [stranitsaZametok, setStranitsaZametok] = useState(1);
  const [dochityvaem, setDochityvaem] = useState(false);
  // Чему принадлежит показанное. Ставит загрузка, сверяет дочитка: пока
  // страница едет, можно уйти на другую карточку — и опоздавший ответ
  // дописал бы чужие строки к чужому же списку, молча.
  const otbor_spiska = useRef("");
  const [files, setFiles] = useState<any[]>([]);
  const [deals, setDeals] = useState<any[]>([]);
  const [tab, setTab] = useState<TabKey>("history");
  const [draft, setDraft] = useState("");
  const [draftKind, setDraftKind] = useState("note");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [composing, setComposing] = useState(false);
  // Один вид записей ленты: «когда мы ему звонили» ищут среди сотни заметок.
  const [vidZapisey, setVidZapisey] = useState("");
  const [napominanie, setNapominanie] = useState(false);
  const guard = useGuard();
  const hasTasks = moduleOn(modules, "tasks") && can(user, "tasks.create");
  const hasOrders = moduleOn(modules, "orders") && can(user, "orders.create");
  const fileInput = useRef<HTMLInputElement>(null);
  const hasMail = moduleOn(modules, "mail") && can(user, "mail.create");
  const hasBoards = moduleOn(modules, "boards") && can(user, "boards.view");

  // Доски — отдельным запросом и вне общего try. Пока они грузились вместе с
  // карточкой, выключенный блок досок отвечал 403 на середине загрузки, и
  // карточка клиента целиком уезжала обратно в список: выключение одного блока
  // закрывало соседний, несущий раздел.
  //
  // Отказ здесь больше не сводится к пустому списку: «Досок пока нет» — это
  // ответ, за которым идут заводить новую, и заводили бы вторую поверх первой.
  const boards = useReference<any>(hasBoards ? `/boards?client_id=${id}` : null);
  // Бланки и накладные клиента — те, что искали по ленте: «а квитанцию ему
  // выдавали?» отвечалось только скроллом истории. Заказы — на своей вкладке.
  const hasDocuments = moduleOn(modules, "documents");
  const papers = useReference<any>(hasDocuments ? `/documents?client_id=${id}&per_page=100` : null);
  const bumagi = (papers.items ?? []).filter((d: any) => d.kind !== "sales_order" && d.kind !== "purchase_order");
  useLiveTopic(["documents", "waybills"], () => papers.reload());
  // Список ящиков нужен только выбору отправителя и доступен только root.
  // Не ответило — форма всё равно работает: сервер возьмёт первый активный.
  const mailAccounts = useReference<MailSender>(hasMail ? "/mail/senders" : null);

  const { failure, fail, clear } = useFailure();

  // Тот же приём, что у карточки заявки: чистая форма перечитывается молча,
  // начатая правка получает полосу. Лента (`client_notes`) — отдельной темой:
  // в неё пишут и почта, и телефония, и подписчики событий.
  const koren = useRef<HTMLDivElement>(null);
  const nachata = useNachatayaPravka(koren, client?.updated_at);
  const [ustarelo, setUstarelo] = useState(false);
  useLiveTopic(["clients", "client_notes"], (s) => {
    if (!s.resync && !s.hints.some((h) => h.id === Number(id))) return;
    if (nachata) setUstarelo(true);
    else void load();
  });

  const load = useCallback(async () => {
    clear();
    otbor_spiska.current = String(id);
    try {
      const data = await api.get(`/clients/${id}`);
      setClient(data);
      setFiles(data.files);
      const notesData = await api.get<{ items: any[]; total: number }>(
        `/clients/${id}/notes?page=1&per_page=${ZAMETOK_NA_STRANITSE}${vidZapisey ? `&kind=${vidZapisey}` : ""}`,
      );
      setNotes(notesData.items);
      setVsegoZametok(notesData.total);
      setStranitsaZametok(1);
      // Заявки приходят вместе с карточкой — отдельный запрос не нужен.
      setDeals(data.deals ?? []);
    } catch (e) {
      // Записи нет или она не наша: показывать «попробуйте ещё раз» тут не о
      // чем — повтор вернёт тот же ответ. Возвращаемся в список, как и раньше.
      if (e instanceof ApiError && (e.status === 404 || e.status === 403)) {
        toastError(e);
        navigate("/clients");
        return;
      }
      // Всё остальное — беда связи или сервера. Карточку не бросаем: адрес в
      // строке верный, и повторить имеет смысл именно его, а не список.
      fail(e);
    }
  }, [id, vidZapisey, toastError, navigate, fail, clear]);

  /** Дочитать заметки. Дописывает страницу, а не перезагружает карточку:
   * от дочитки меняется только лента внизу.
   */
  const dochitat_zametki = async () => {
    if (dochityvaem) return;
    const sprosheno = String(id);
    setDochityvaem(true);
    try {
      const dalshe = await api.get<{ items: any[]; total: number }>(
        `/clients/${id}/notes?page=${stranitsaZametok + 1}&per_page=${ZAMETOK_NA_STRANITSE}${vidZapisey ? `&kind=${vidZapisey}` : ""}`,
      );
      // Отбор сменился, пока страница ехала, — ответ чужой.
      if (otbor_spiska.current !== sprosheno) return;
      setNotes((bylo) => [...bylo, ...dalshe.items]);
      setVsegoZametok(dalshe.total);
      setStranitsaZametok((bylo) => bylo + 1);
    } catch (e) {
      toastError(e);
    } finally {
      setDochityvaem(false);
    }
  };

  useEffect(() => {
    void load();
  }, [load]);

  if (!client) return <ScreenLoading error={failure} onRetry={() => void load()} />;

  const addNote = async () => {
    const body = draft.trim();
    // Enter в этом поле нажимают дважды — от нетерпения и просто с руки. Без
    // засова в ленте клиента появлялись две одинаковые записи, и убирать
    // вторую приходилось вручную.
    if (!body || !guard.take()) return;
    try {
      const note = await api.post(`/clients/${id}/notes`, { kind: draftKind, body });
      setNotes((prev) => [note, ...prev]);
      setDraft("");
    } catch (e) {
      toastError(e);
    } finally {
      guard.free();
    }
  };

  const deleteNote = async (noteId: number) => {
    try {
      await api.del(`/clients/${id}/notes/${noteId}`);
      setNotes((prev) => prev.filter((n) => n.id !== noteId));
    } catch (e) {
      toastError(e);
    }
  };

  const uploadFiles = async (list: FileList | null) => {
    if (!list) return;
    for (const file of Array.from(list)) {
      try {
        const record = await api.upload(`/clients/${id}/files`, file);
        setFiles((prev) => [record, ...prev]);
      } catch (e) {
        toastError(e);
      }
    }
  };

  const saveContact = async (field: string, value: string) => {
    try {
      const updated = await api.patch(`/clients/${id}`, { [field]: value });
      setClient((prev: any) => ({ ...prev, ...updated }));
    } catch (e) {
      toastError(e);
      void load();
    }
  };

  const contacts = [
    { field: "phone", label: t("phone"), value: client.phone },
    { field: "email", label: t("email"), value: client.email },
    { field: "messenger", label: t("telegram"), value: client.messenger },
    { field: "company", label: t("company"), value: client.company },
  ];

  // Адрес отдельно от контактов: контакты отвечают «как дозвониться», адрес —
  // «куда везти», и спрашивают их в разные моменты разговора.
  const adres = [
    {
      field: "country",
      label: t("country"),
      value: client.country,
      // В карточке — названием целиком: здесь на страну смотрят, когда решают,
      // как везти, и `PL` в этот момент требует лишнего усилия.
      display: client.country
        ? `${flagStrany(client.country)} ${nazvanieStrany(client.country, locale)}`
        : "",
    },
    { field: "city", label: t("city"), value: client.city },
    { field: "zip_code", label: t("zipCode"), value: client.zip_code },
    { field: "address", label: t("streetAddress"), value: client.address },
  ];

  // Вкладки — списком, тем же правилом, что и меню: вкладка выключенного блока
  // исчезает целиком, а не остаётся заголовком над пустотой.
  const tabs = shown<Gated & { key: TabKey; label: string; count?: number }>(modules, [
    { key: "history", label: t("history") },
    // Звонки отдельной вкладкой, а не вместо ленты: сам разговор в ленту уже
    // попал записью, здесь — длительность, итог и запись разговора.
    { module: "telephony", key: "calls", label: t("calls") },
    { key: "files", label: t("files"), count: files.length },
    { module: "documents", key: "papers", label: t("documents"), count: bumagi.length },
    { module: "boards", key: "boards", label: t("boards"), count: boards.items?.length ?? 0 },
    // Заявки клиента: за год их бывает пять, и «что мы для него делали»
    // должно быть вопросом к системе, а не к памяти.
    {
      key: "deals",
      label: term(workspace.deal_term, locale, "many"),
      count: deals.length,
    },
  ]);
  // Блок могли выключить, пока карточка открыта на его вкладке — тогда
  // показываем ленту, а не пустой экран под исчезнувшим заголовком.
  const activeTab = tabs.some((item) => item.key === tab) ? tab : "history";
  const schyotchiki = tabs.filter((item) => item.count !== undefined).slice(0, 3);

  return (
    <div className="page" ref={koren}>
      {ustarelo && (
        <div className="maintenance-bar" style={{ marginBottom: 12 }}>
          <span className="dot" />
          {t("liveStale")}
          <button className="btn btn-secondary btn-sm" style={{ marginLeft: 12 }} onClick={() => { setUstarelo(false); void load(); }}>
            {t("liveShow")}
          </button>
        </div>
      )}
      <Link to="/clients" style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)", fontSize: 13, marginBottom: 20 }}>
        <Icon name="arrowLeft" size={14} />
        {t("clients")}
      </Link>
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 24 }}>
        {/* Паспорт — перевод uiverse.io/WattoRex/odd-fish-37 (docs/18): то же имя,
            метки и «добавлен», плюс счётчики вкладок и ход к заявкам, которых
            в шапке прежде не было. */}
        <div className="pasport">
          <div className="pasport-shapka">
            <div className="pasport-fon" aria-hidden="true">{initials(client.name)}</div>
            <div className="pasport-ava" aria-hidden="true">{initials(client.name)}</div>
            <div className="pasport-status">{client.tags[0] ?? t("clientBadge")}</div>
          </div>
          <div className="pasport-telo">
            <div className="pasport-ruchka">{client.phone || client.email || `#${client.id}`}</div>
            <h1 className="pasport-imya">{client.name}</h1>
            <div className="pasport-bio">
              {client.company && <>{client.company} · </>}
              {t("added")} {formatDate(client.created_at, locale)}
              {client.updated_at && formatDate(client.updated_at, locale) !== formatDate(client.created_at, locale) && (
                <> · {t("clientEdited", { date: formatDate(client.updated_at, locale) })}</>
              )}
              {client.tags.length > 1 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
                  {client.tags.slice(1).map((tag: string) => (
                    <Chip key={tag}>{tag}</Chip>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div
            className="pasport-stats"
            style={{ gridTemplateColumns: `repeat(${schyotchiki.length}, 1fr)` }}
          >
            {schyotchiki.map((item) => (
              <div key={item.key} className="pasport-stat">
                <span className="pasport-stat-v">{item.count}</span>
                <span className="pasport-stat-l">{item.label}</span>
              </div>
            ))}
          </div>
          <button type="button" className="pasport-btn" onClick={() => setTab("deals")}>
            + {term(workspace.deal_term, locale, "many")}
          </button>
        </div>
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 12 }}>
        {client.svodka && <KlientSvodka svodka={client.svodka} currency={client.currency} />}
        <div style={{ display: "flex", gap: 10, position: "relative", flexWrap: "wrap" }}>
          {/* Письмо пишем прямо отсюда: отправленное ляжет в эту же ленту
              строкой «письмо · исходящее», а не в отдельную переписку. */}
          {hasMail && client.email && (
            <button className="btn btn-secondary" onClick={() => setComposing(true)}>
              <Icon name="send" size={14} />
              {t("compose")}
            </button>
          )}
          <CallButton number={client.phone} />
          {/* Напоминание и заказ — отсюда же: после разговора первое, что
              делают, — «перезвонить в четверг» и «выписать заказ», а не
              переход в раздел ради одной строки (владелец, 06.09.2026). */}
          {hasTasks && (
            <button className="btn btn-secondary" onClick={() => setNapominanie(true)}>
              <Icon name="clock" size={14} />
              {t("clientNewReminder")}
            </button>
          )}
          {hasOrders && (
            <button
              className="btn btn-secondary"
              disabled={guard.busy}
              onClick={async () => {
                if (!guard.take()) return;
                try {
                  const order = await api.post<{ id: number }>("/orders", { kind: "sales_order", client_id: client.id });
                  navigate(`/orders/${order.id}`);
                } catch (e) {
                  toastError(e);
                } finally {
                  guard.free();
                }
              }}
            >
              <Icon name="receipt" size={14} />
              {t("newSalesOrder")}
            </button>
          )}
          {/* Кнопка уходит вместе с блоком: предлагать создать доску там, где
              раздела досок нет, — обещание, ведущее в отказ сервера. */}
          {hasBoards && <NewBoardButton clientId={client.id} />}
          <button
            className="btn-icon"
            aria-label={t("actions")}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <Icon name="dots" />
          </button>
          {menuOpen && (
            <div className="user-menu" style={{ position: "absolute", top: 42, right: 0, bottom: "auto", left: "auto", width: 200 }}>
              <button
                className="user-menu-item"
                style={{ color: "var(--danger)" }}
                onClick={() => {
                  setMenuOpen(false);
                  setConfirmDelete(true);
                }}
              >
                <Icon name="trash" size={14} />
                {t("deleteClient")}
              </button>
            </div>
          )}
        </div>
        </div>
      </div>

      {/* Заказы клиента. Врезка та же, что в заявке: список один, и второй его
          экземпляр разошёлся бы с первым при первой же правке. */}
      <OrdersOfCard clientId={Number(id)} />

      <div className="card contact-grid" style={{ marginBottom: 28 }}>
        {contacts.map((contact) => (
          <EditableContact key={contact.field} {...contact} onSave={saveContact} />
        ))}
        {/* Источник стоит рядом с контактами, а не в отдельной вкладке: его
            дописывают ровно тогда, когда выясняют — по ходу первого разговора,
            в котором уточняют и телефон. */}
        <div className="contact-cell">
          <div className="contact-label">{t("clientSource")}</div>
          <SourcePicker
            value={client.source ?? ""}
            onCommit={(next) => saveContact("source", next)}
          />
        </div>
      </div>

      <div className="card" style={{ marginBottom: 28 }}>
        <div className="metric-title" style={{ padding: "16px 18px 0" }}>
          {t("shippingAddress")}
        </div>
        <div className="contact-grid">
          {adres.map((pole) => (
            <EditableContact key={pole.field} {...pole} onSave={saveContact} />
          ))}
        </div>
      </div>

      <div className="tabs">
        {tabs.map((item) => (
          <button
            key={item.key}
            className={"tab" + (activeTab === item.key ? " active" : "")}
            onClick={() => setTab(item.key)}
          >
            {item.label}
            {!!item.count && <span className="count">{item.count}</span>}
          </button>
        ))}
      </div>

      {activeTab === "history" && (
        <>
          <div className="card" style={{ padding: "14px 16px", marginBottom: 20 }}>
            <input
              style={{ width: "100%", background: "none", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5, fontFamily: "var(--sans)", padding: "2px 0 10px" }}
              placeholder={t("addNotePlaceholder")}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void addNote();
              }}
            />
            {/* Переносится по строкам: на узком экране четыре вида записи и
                кнопка в одну строку не помещаются, и без переноса ряд утаскивал
                вбок всю карточку клиента. На широком строка одна, как и была. */}
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {(["note", "call", "meeting", "email"] as const).map((kind) => (
                  <button
                    key={kind}
                    className={"option-chip" + (draftKind === kind ? " active" : "")}
                    onClick={() => setDraftKind(kind)}
                  >
                    <Icon name={NOTE_ICONS[kind]} size={13} />
                    {t(kind === "email" ? "emailNote" : kind)}
                  </button>
                ))}
              </div>
              <button className="btn btn-primary" style={{ height: 28, padding: "0 12px", fontSize: 12.5 }} disabled={guard.busy} onClick={() => void addNote()}>
                {t("add")}
              </button>
            </div>
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10 }}>
            {(["", "note", "call", "meeting", "email"] as const).map((vid) => (
              <button
                key={vid || "all"}
                type="button"
                className={"filter-chip" + (vidZapisey === vid ? " active" : "")}
                onClick={() => setVidZapisey(vid)}
              >
                {t(vid ? VID_ZAPISI[vid] : "feedAll")}
              </button>
            ))}
          </div>
          <div>
            {/* Полосы по дням, как в напоминаниях: «сегодня», «вчера», дата —
                в сплошной ленте день терялся между отметками времени. */}
            {notes.map((note, i) => (
              <Fragment key={note.id}>
                {(i === 0 || relativeDay(notes[i - 1].happened_at, locale) !== relativeDay(note.happened_at, locale)) && (
                  <div className="spisok-polosa">{relativeDay(note.happened_at, locale)}</div>
                )}
              <div className="feed-item">
                <div className="feed-icon">
                  <Icon name={noteIcon(note)} size={14} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 4 }}>
                    <span style={{ color: "var(--muted)", fontSize: 12.5, fontWeight: 500 }}>
                      {t(NOTE_LABELS[note.kind])}
                    </span>
                    <span style={{ color: "var(--faint)", fontSize: 12 }}>{formatDateTime(note.happened_at, locale)}</span>
                    {/* Кто — половина смысла ленты. Пусто у письма и звонка
                        извне: там живого автора действительно не было. */}
                    {note.author_name && (
                      <span style={{ color: "var(--faint)", fontSize: 12 }}>{note.author_name}</span>
                    )}
                    {/* У записи о событии кнопки удаления нет ни у кого, включая
                        root: сервер такое удаление отклоняет, и показывать
                        действие, которое гарантированно ответит отказом, хуже,
                        чем не показывать его вовсе. Заметку автор убирает —
                        ошибся при вводе; смена этапа либо была, либо нет. */}
                    {!SYSTEM_NOTE_KINDS.has(note.kind) &&
                      (user?.role === "root" || note.author_id === user?.id) && (
                        <button className="text-link" style={{ marginLeft: "auto", fontSize: 11.5 }} onClick={() => void deleteNote(note.id)}>
                          {t("delete")}
                        </button>
                      )}
                  </div>
                  <div style={{ color: "var(--text)", fontSize: 13.5, lineHeight: 1.55 }}>
                    {/* Системные записи хранятся по-английски — на экране их подписываем словами интерфейса. */}
                    {SYSTEM_NOTE_KINDS.has(note.kind) ? podpisSistemnoy(note.body, t) : note.body}
                  </div>
                </div>
              </div>
              </Fragment>
            ))}
            <Dochitat
              pokazano={notes.length}
              vsego={vsegoZametok}
              zanyat={dochityvaem}
              onClick={() => void dochitat_zametki()}
            />
            {notes.length === 0 && <EmptyState title={t("addNotePlaceholder")} />}
          </div>
        </>
      )}

      {/* Заявки клиента передаём сюда: у звонка с незнакомого номера заявки
          нет, и привязать его к заказу удобнее там же, где он виден. */}
      {activeTab === "calls" && <CallsPanel clientId={client.id} deals={deals} limit={50} />}

      {activeTab === "files" && (
        <>
          <div
            className="dropzone"
            style={{ marginBottom: 20 }}
            onClick={() => fileInput.current?.click()}
            {...dropTarget((e) => void uploadFiles(e.dataTransfer.files))}
          >
            {t("dropFiles")} <span style={{ color: "var(--accent)", textDecoration: "underline", textUnderlineOffset: 2 }}>{t("browse")}</span>{" "}
            {t("filesInternal")}
            <input ref={fileInput} type="file" multiple hidden onChange={(e) => void uploadFiles(e.target.files)} />
          </div>
          <div className="list-card">
            {files.map((file) => (
              <div key={file.id} className="list-row hoverable" style={{ height: 52 }}>
                <div className="file-ext">{fileExt(file.original_name)}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{file.original_name}</div>
                  <div style={{ color: "var(--faint)", fontSize: 12 }}>
                    {fileSize(file.size_bytes)} · {formatDate(file.created_at, locale)}
                  </div>
                </div>
                <a
                  href={file.download_url}
                  aria-label={t("download")}
                  style={{ color: "var(--muted)", display: "flex" }}
                >
                  <Icon name="download" />
                </a>
                <button
                  className="text-link"
                  style={{ display: "flex", color: "var(--faint)" }}
                  aria-label={t("delete")}
                  onClick={async () => {
                    try {
                      await api.del(`/clients/${id}/files/${file.id}`);
                      setFiles((prev) => prev.filter((f) => f.id !== file.id));
                    } catch (e) {
                      toastError(e);
                    }
                  }}
                >
                  <Icon name="trash" />
                </button>
              </div>
            ))}
            {files.length === 0 && <EmptyState title={t("dropFiles") + " " + t("browse")} />}
          </div>
        </>
      )}

      {activeTab === "papers" && (
        papers.failure !== null ? (
          <LoadFailed error={papers.failure} onRetry={papers.reload} />
        ) : bumagi.length === 0 ? (
          <EmptyState icon="receipt" title={t("noDocuments")} />
        ) : (
          <div className="list-card">
            {bumagi.map((doc: any) => (
              <Link key={doc.id} to={paperLink(doc)} className="list-row hoverable">
                <span className="doc-number">{doc.number}</span>
                <div className="list-row-text">
                  <div className="truncate" style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                    {doc.payload?.fields?.item || kindLabel(t, doc.kind)}
                  </div>
                  <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>{kindLabel(t, doc.kind)}</div>
                </div>
                <span style={{ width: 90, textAlign: "right", color: "var(--faint)", fontSize: 12, flexShrink: 0 }}>
                  {formatDate(doc.created_at, locale)}
                </span>
                <span style={{ width: 130, flexShrink: 0, display: "flex", justifyContent: "flex-end" }}>
                  <Chip variant={statusVariant(doc.status, doc.kind)}>{statusLabel(t, doc.status, doc.kind)}</Chip>
                </span>
              </Link>
            ))}
            <ItogSpiska
              pokazano={bumagi.length}
              vsego={bumagi.length}
              summa={bumagi.reduce((s: number, d: any) => s + (d.total ?? 0), 0)}
              currency={workspace.currency}
            />
          </div>
        )
      )}

      {activeTab === "boards" && (
        <div className="board-grid">
          {(boards.items ?? []).map((board) => (
            <BoardCard key={board.id} board={board} />
          ))}
          {boards.failure !== null ? (
            <div className="card card-pad" style={{ gridColumn: "1 / -1" }}>
              <LoadFailed error={boards.failure} onRetry={boards.reload} />
            </div>
          ) : (
            boards.items?.length === 0 && (
              <div className="card" style={{ gridColumn: "1 / -1" }}>
                <EmptyState title={t("noBoardsYet")} />
              </div>
            )
          )}
        </div>
      )}

      {activeTab === "deals" && client.svodka && deals.length > 0 && (
        <div className="field-desc" style={{ marginTop: 0, marginBottom: 10 }}>
          {t("clientDealsTotals", { open: client.svodka.open_count, won: client.svodka.won_count })}
          {client.svodka.open_amount !== null &&
            ` · ${t("clientDealsOpenSum", { sum: formatMoney(client.svodka.open_amount, client.currency, locale) })}`}
        </div>
      )}
      {activeTab === "deals" && (
        <div className="list-card">
          {deals.map((deal) => (
            <Link to={`/deals/${deal.id}`} key={deal.id} className="list-row hoverable">
              <div className="list-row-text">
                <div className="truncate" style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                  {deal.title}
                </div>
                <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>
                  {deal.manager_name || t("nobody")}
                </div>
              </div>
              {deal.amount !== null && (
                <span style={{ color: "var(--muted)", fontSize: 12.5, flexShrink: 0, fontVariantNumeric: "tabular-nums" }}>
                  {formatMoney(deal.amount, workspace.currency, locale)}
                </span>
              )}
              <span style={{ width: 90, textAlign: "right", color: "var(--faint)", fontSize: 12, flexShrink: 0 }}>
                {relativeDay(deal.created_at, locale)}
              </span>
            </Link>
          ))}
          {deals.length === 0 && <EmptyState title={term(workspace.deal_term, locale, "none")} />}
        </div>
      )}

      {composing && (
        <MailCompose
          accounts={mailAccounts.items ?? []}
          to={client.email}
          clientId={client.id}
          onClose={() => setComposing(false)}
          onSent={() => void load()}
        />
      )}

      {napominanie && (
        <Modal title={t("clientNewReminder")} onClose={() => setNapominanie(false)}>
          <QuickTask
            clientId={client.id}
            onCreated={() => {
              setNapominanie(false);
              toast(t("feedTaskCreated"));
            }}
          />
        </Modal>
      )}

      {confirmDelete && (
        <ConfirmModal
          text={t("deleteClientConfirm", { name: client.name })}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/clients/${id}`);
              toast(t("deleteClient") + " ✓");
              navigate("/clients");
            } catch (e) {
              toastError(e);
            }
          }}
          onClose={() => setConfirmDelete(false)}
        />
      )}
    </div>
  );
}

function EditableContact({
  field,
  label,
  value,
  display,
  onSave,
}: {
  field: string;
  label: string;
  value: string;
  /** Что показать вместо значения, пока не правят: флаг и название страны.
   *  Правится всё равно `value` — код, а не название. */
  display?: string;
  onSave: (field: string, value: string) => void;
}) {
  const { t } = useApp();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  // Внешнее обновление (живой намёк перечитал карточку) не затирает набранное:
  // пока поле правят, черновик остаётся, новое значение придёт после сохранения.
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);

  return (
    <div className="contact-cell" onClick={() => !editing && setEditing(true)} style={{ cursor: editing ? "auto" : "text" }}>
      <div className="contact-label">{label}</div>
      {editing ? (
        <input
          className="contact-input"
          value={draft}
          autoFocus
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            setEditing(false);
            if (draft !== value) onSave(field, draft);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            if (e.key === "Escape") {
              setDraft(value);
              setEditing(false);
            }
          }}
        />
      ) : (
        <div className="contact-value">
          {/* Пустое поле зовёт дописать, а не молчит прочерком: прочерк
              читается как «нет и не надо». */}
          {display || value || <span className="contact-add">+ {t("contactAdd")}</span>}
        </div>
      )}
    </div>
  );
}


interface Svodka {
  open_count: number;
  open_amount: number | null;
  won_count: number;
  won_amount: number | null;
  lost_count: number;
  received_12m: number | null;
  last_contact: { kind: string; at: string | null; body: string } | null;
  last_call_at: string | null;
  papers: Record<string, number> | null;
  papers_total: number;
  manager_name: string | null;
}

/** Подписи видов записей ленты — те же ключи, что у самой ленты (`Feed`). */
const VID_ZAPISI: Record<string, TranslationKey> = {
  note: "feedNote",
  call: "feedCall",
  meeting: "feedMeeting",
  email: "feedEmail",
  stage: "feedStage",
  document: "feedDocument",
  stock: "feedStock",
};

/** Что справа от паспорта. Считает сервер (`client_service.svodka`): чужие
 *  заявки не в счёт, суммы пустеют без права, выключенный блок — плитки нет. */
function KlientSvodka({ svodka, currency }: { svodka: Svodka; currency: string }) {
  const { t, locale } = useApp();
  const money = (value: number | null) => formatMoney(value, currency, locale);
  const kontakt = svodka.last_contact;
  const kontaktAt = [kontakt?.at, svodka.last_call_at].filter(Boolean).sort().pop() ?? null;
  return (
    <div className="svodka-plitki">
      <div className="svodka-plitka">
        <div className="svodka-l">{t("clientSummaryOpen")}</div>
        <div className="svodka-v">{svodka.open_count}</div>
        <div className="svodka-sub">{svodka.open_amount !== null ? money(svodka.open_amount) : t("clientSummaryNoAmounts")}</div>
      </div>
      <div className="svodka-plitka">
        <div className="svodka-l">{t("clientSummaryWon")}</div>
        <div className="svodka-v">{svodka.won_count}</div>
        <div className="svodka-sub">
          {svodka.won_amount !== null ? money(svodka.won_amount) : t("clientSummaryLost", { n: svodka.lost_count })}
        </div>
      </div>
      {svodka.received_12m !== null && (
        <div className="svodka-plitka">
          <div className="svodka-l">{t("clientSummaryReceived")}</div>
          <div className="svodka-v">{money(svodka.received_12m)}</div>
          <div className="svodka-sub">{t("clientSummaryReceivedHint")}</div>
        </div>
      )}
      <div className="svodka-plitka">
        <div className="svodka-l">{t("clientSummaryLastContact")}</div>
        <div className="svodka-v">{kontaktAt ? relativeDay(kontaktAt, locale) : "—"}</div>
        <div className="svodka-sub" title={kontakt?.body}>
          {kontakt ? kontakt.body || t(VID_ZAPISI[kontakt.kind] ?? "feedNote") : t("clientSummaryNoContact")}
        </div>
      </div>
      {svodka.papers !== null && (
        <div className="svodka-plitka">
          <div className="svodka-l">{t("clientSummaryPapers")}</div>
          <div className="svodka-v">{svodka.papers_total}</div>
          <div className="svodka-sub">
            {Object.entries(svodka.papers)
              .map(([kind, n]) => `${kindLabel(t, kind)} ${n}`)
              .join(" · ") || t("noDocuments")}
          </div>
        </div>
      )}
      <div className="svodka-plitka">
        <div className="svodka-l">{t("clientSummaryManager")}</div>
        <div className="svodka-v">{svodka.manager_name ?? "—"}</div>
        <div className="svodka-sub">{svodka.manager_name ? t("responsible") : t("clientSummaryNobody")}</div>
      </div>
    </div>
  );
}
