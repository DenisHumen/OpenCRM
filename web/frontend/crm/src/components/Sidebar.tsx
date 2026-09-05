import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatBytes, initials } from "../lib/format";
import { allowed, can, type Guarded } from "../lib/permissions";
import { nazvanieZakazov, term } from "../lib/terms";
import { Icon } from "./Icon";
import { Kolokolchik } from "./Kolokolchik";
import { Avatar } from "./ui";

/** Ссылка в меню. */
type NavLinkItem = { to: string; label: string };
/** Категория второго уровня: своё имя, свой ключ памяти, свои разделы. */
type NavCategory = { key: string; label: string; items: NavLinkItem[] };
type NavEntry = NavLinkItem | NavCategory;

const eto_kategoriya = (entry: NavEntry): entry is NavCategory => "items" in entry;

/** Все ссылки списка, включая лежащие в категориях. */
function vse_ssylki(items: NavEntry[]): NavLinkItem[] {
  return items.flatMap((entry) => (eto_kategoriya(entry) ? entry.items : [entry]));
}

/**
 * Категория внутри группы — второй уровень.
 *
 * Своё открытое состояние и СВОЙ ключ памяти: при общем ключе две категории
 * открывались бы и закрывались вместе, а третья — вместе с самой группой.
 *
 * Раскрытие идёт по всей цепочке: заход на `/settings/return-button` открывает
 * и группу «Настройки сайта», и категорию «Витрина». Иначе открытый раздел
 * прятался бы внутри свёрнутого родителя — на экране это выглядит как «пункт
 * пропал», хотя он ровно там, куда человек и перешёл.
 */
function NavCategoryBlock({
  klyuch,
  label,
  items,
}: {
  klyuch: string;
  label: string;
  items: NavLinkItem[];
}) {
  const { pathname } = useLocation();
  const inside = items.some(
    (item) => pathname === item.to || pathname.startsWith(item.to + "/"),
  );
  const [open, setOpen] = useState(
    () => inside || localStorage.getItem(klyuch) === "1",
  );

  useEffect(() => {
    if (inside) setOpen(true);
  }, [inside]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    localStorage.setItem(klyuch, next ? "1" : "0");
  };

  // Имена классов свои, а не `nav-cat`: тот уже занят СЕКЦИЕЙ меню («Работа»,
  // «Админ»), и её правила стрелки поймали бы и эту.
  return (
    <div className={"nav-sub-cat" + (open ? " open" : "")}>
      <button
        type="button"
        className={"nav-item nav-sub-cat-head" + (inside ? " active" : "")}
        aria-expanded={open}
        onClick={toggle}
      >
        <span style={{ flex: 1, textAlign: "left" }}>{label}</span>
        <Icon name="chevronDown" size={12} className="nav-sub-chevron" />
      </button>
      {open && (
        <div className="nav-sub nav-sub-deep">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}
            >
              {item.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Пункт навигации с вложенным списком.
 *
 * Заголовок только раскрывает группу и никуда не ведёт: разделов внутри уже
 * пять и будет больше, и «главного» среди них нет. Открытое состояние живёт в
 * localStorage — иначе список схлопывался бы на каждом переходе; при заходе на
 * любой вложенный маршрут группа раскрывается сама.
 *
 * Пустая группа не рисуется, как и пустая категория: заголовок без пунктов —
 * обещание раздела, которого нет. Считается это СНИЗУ ВВЕРХ: пустеют
 * категории, от них пустеет группа.
 *
 * **Категория из одного раздела показывается самим разделом**, без заголовка и
 * без второго нажатия. Причина не в экономии места: заголовок, за которым
 * лежит ровно один пункт, обещает выбор, которого нет, — а с выключёнными
 * блоками такие категории получаются сами. «Склад и товар» у того, кто выключил
 * наклейки, — это просто «Склады».
 */
function NavGroup({
  icon,
  label,
  base,
  items,
}: {
  icon: string;
  label: string;
  base: string;
  items: NavEntry[];
}) {
  const { pathname } = useLocation();
  const inside = pathname === base || pathname.startsWith(base + "/");
  const [open, setOpen] = useState(
    () => inside || localStorage.getItem(`nav:${base}`) === "1",
  );

  useEffect(() => {
    if (inside) setOpen(true);
  }, [inside]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    localStorage.setItem(`nav:${base}`, next ? "1" : "0");
  };

  if (vse_ssylki(items).length === 0) return null;

  return (
    <div className={"nav-group" + (open ? " open" : "")}>
      <button
        type="button"
        className={"nav-item nav-group-head" + (inside ? " active" : "")}
        aria-expanded={open}
        onClick={toggle}
      >
        <Icon name={icon} size={16} />
        <span style={{ flex: 1, textAlign: "left" }}>{label}</span>
        <Icon name="chevronDown" size={13} className="nav-chevron" />
      </button>
      {open && (
        <div className="nav-sub">
          {items.map((entry) => {
            if (!eto_kategoriya(entry)) {
              return (
                <NavLink
                  key={entry.to}
                  to={entry.to}
                  className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}
                >
                  {entry.label}
                </NavLink>
              );
            }
            if (entry.items.length === 0) return null;
            if (entry.items.length === 1) {
              const odin = entry.items[0];
              return (
                <NavLink
                  key={odin.to}
                  to={odin.to}
                  className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}
                >
                  {odin.label}
                </NavLink>
              );
            }
            return (
              // Поля перечисляются, а не разворачиваются: у категории есть
              // своё поле `key`, и `{...entry}` перебивало бы им служебный
              // ключ React — молча, если бы не типы.
              <NavCategoryBlock
                key={entry.key}
                klyuch={`nav:${base}/${entry.key}`}
                label={entry.label}
                items={entry.items}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * Пункт меню. Счётчик показывается только когда ему есть что сказать, и всегда
 * с подписью: голое число рядом с «Сотрудники» читается как их количество, а
 * не как «столько ждут решения».
 */
type NavItem = Guarded & {
  to: string;
  label: string;
  icon: string;
  end?: boolean;
  badge?: number;
  badgeTitle?: string;
};

function NavRow({ item }: { item: NavItem }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}
    >
      <Icon name={item.icon} size={16} />
      <span style={{ flex: 1 }}>{item.label}</span>
      {!!item.badge && (
        <span className="nav-badge" title={item.badgeTitle}>
          {item.badge}
        </span>
      )}
    </NavLink>
  );
}

/**
 * Категория меню: заголовок раздела, сворачивающий свои пункты.
 *
 * Плоский список перестал читаться, когда разделов стало семь. Раскрыто по
 * умолчанию и запомнено в localStorage: свернул один раз — осталось свёрнутым,
 * иначе категория не экономит ничего.
 *
 * Пустая категория не рисуется вовсе: заголовок без пунктов — обещание
 * раздела, которого нет. Так бывает, когда все модули внутри выключены.
 */
function NavSection({
  id,
  label,
  items,
  children,
}: {
  id: string;
  label: string;
  items: NavItem[];
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(() => localStorage.getItem(`nav:cat:${id}`) !== "0");

  if (items.length === 0 && !children) return null;

  const toggle = () => {
    const next = !open;
    setOpen(next);
    localStorage.setItem(`nav:cat:${id}`, next ? "1" : "0");
  };

  // Свёрнутая категория забирает счётчики своих пунктов: иначе свернул один
  // раз — и перестал видеть, что там кого-то ждут. Сворачивание убирает
  // подробности, но не сам сигнал.
  const counted = open ? [] : items.filter((item) => !!item.badge);
  const hidden = counted.reduce((sum, item) => sum + (item.badge ?? 0), 0);

  return (
    <div className={"nav-cat" + (open ? " open" : "")}>
      <button type="button" className="nav-section" aria-expanded={open} onClick={toggle}>
        <span style={{ flex: 1, textAlign: "left" }}>{label}</span>
        {hidden > 0 && (
          <span
            className="nav-badge"
            title={counted.map((item) => `${item.label}: ${item.badgeTitle}`).join(", ")}
          >
            {hidden}
          </span>
        )}
        <Icon name="chevronDown" size={11} className="nav-chevron" />
      </button>
      {open && (
        <div className="nav-cat-items">
          {items.map((item) => (
            <NavRow key={item.to} item={item} />
          ))}
          {children}
        </div>
      )}
    </div>
  );
}

export function Sidebar({
  onOpenSearch,
  open = false,
}: {
  onOpenSearch: () => void;
  /** Выехало ли меню поверх содержимого. Смысл имеет только на узком окне —
   *  на широком сайдбар и так стоит на месте (см. медиазапрос в styles.css). */
  open?: boolean;
}) {
  const {
    user, t, locale, settings, storage, modules, workspace, overdueTasks,
    setUser, logout, toastError,
  } = useApp();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [sozdatOpen, setSozdatOpen] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);

  // Одна кнопка «Создать» на всё: клиента, заявку, бланк, заказ, напоминание.
  // До неё первый шаг искали по разделам — а спрашивают «где завести», а не
  // «в каком разделе кнопка». Показывается только то, на что есть право.
  const sozdat = allowed<NavItem>(user, modules, [
    { perm: "clients.create", to: "/clients?new=1", label: t("newClient"), icon: "userPlus" },
    { perm: "deals.create", to: "/deals?new=1", label: term(workspace.deal_term, locale, "new"), icon: "deals" },
    { module: "documents", perm: "documents.create", to: "/documents?new=1", label: t("newDocument"), icon: "receipt" },
    { module: "orders", perm: "orders.create", to: "/orders", label: t("quickOrder"), icon: "receipt" },
    { module: "tasks", perm: "tasks.create", to: "/tasks", label: t("quickTask"), icon: "clock" },
  ]);


  // Счётчик заявок на регистрацию нужен только тому, кто их разбирает.
  // Спрашивать его без права — стучаться в закрытую дверь на каждой загрузке.
  const seesStaff = can(user, "staff.view");

  useEffect(() => {
    if (!seesStaff) return;
    api
      .get("/staff?status=pending")
      .then((data) => setPendingCount(data.items.length))
      // Единственное место, где отказ так и остаётся проглоченным, и намеренно:
      // счётчик — это метка на пункте меню, показать в ней «не загрузилось»
      // некуда, а самому пункту отказ ничем не мешает. Заявки на регистрацию
      // при этом никуда не деваются: их видно на самом экране сотрудников,
      // который об отказе сказать умеет.
      .catch(() => undefined);
  }, [seesStaff]);

  // язык интерфейса у каждого свой и хранится в аккаунте (users.locale),
  // поэтому переключение — обычный PATCH профиля, а не настройка браузера
  const setLocale = async (next: string) => {
    if (!user || user.locale === next) return;
    try {
      setUser(await api.patch("/auth/me", { locale: next }));
    } catch (e) {
      toastError(e);
    }
  };

  const brandName = settings.brand_name || "OpenCRM";
  // Звёзды спрашиваем у СВОЕГО сервера: он держит их в базе и обновляет раз в
  // сутки. Из браузера на api.github.com не ходим — это был бы чужой сервер в
  // странице CRM. `null` — «не знаем», и тогда числа просто нет.
  const [zvyozdy, setZvyozdy] = useState<number | null>(null);
  useEffect(() => {
    let zhivo = true;
    api
      .get<{ stars: number | null }>("/system/github")
      .then((o) => zhivo && setZvyozdy(o.stars))
      .catch(() => undefined);
    return () => {
      zhivo = false;
    };
  }, []);

  // Меню собирается списком, а не разметкой: так «выключенный блок не
  // показываем», «нет права — не показываем» и «пустую категорию не показываем»
  // остаются одним правилом, а не тремя условиями в разных местах.
  // Принадлежность блоку — поле `module`, требуемое право — `perm`; отбирает их
  // `allowed`, один раз на весь файл.
  const daily = allowed<NavItem>(user, modules, [
    { to: "/", label: t("dashboard"), icon: "dashboard", end: true },
    {
      module: "tasks",
      perm: "tasks.view",
      to: "/tasks",
      label: t("tasks"),
      icon: "clock",
      badge: overdueTasks,
      badgeTitle: t("tasksOverdue"),
    },
  ]);

  const work = allowed<NavItem>(user, modules, [
    { perm: "clients.view", to: "/clients", label: t("clients"), icon: "clients" },
    {
      perm: "deals.view",
      to: "/deals",
      label: term(workspace.deal_term, locale, "many"),
      icon: "deals",
    },
    { module: "documents", perm: "documents.view", to: "/documents", label: t("documents"), icon: "receipt" },
    { module: "mail", perm: "mail.view", to: "/mail", label: t("mail"), icon: "email" },
    // Шаблоны — рядом с почтой: за ними приходят в тот момент, когда пишут
    // клиенту, а не отдельным делом.
    { module: "templates", perm: "templates.view", to: "/templates", label: t("templates"), icon: "note" },
    { module: "boards", perm: "boards.view", to: "/boards", label: t("boards"), icon: "boards" },
    // Склад по умолчанию выключен: он нужен магазину и мастерской, а студии нет.
    // Магазин зовёт заявки «заказами», и рядом стоял второй пункт «Заказы» —
    // блок заказов. Два одинаковых слова в одном меню — угадывание.
    { module: "orders", perm: "orders.view", to: "/orders", label: nazvanieZakazov(t, term(workspace.deal_term, locale, "many")), icon: "receipt" },
    { module: "waybills", perm: "waybills.view", to: "/waybills", label: t("waybills"), icon: "arrowOut" },
    { module: "warehouse", perm: "warehouse.view", to: "/warehouse", label: t("warehouse"), icon: "warehouse" },
    // Деньги — перед отчётами: отчёты отвечают «как идут дела», а этот раздел
    // «сколько заработали». Второй вопрос задают первым, когда сводят месяц.
    { module: "finance", perm: "finance.view", to: "/finance", label: t("modFinance"), icon: "receipt" },
    // Отчёты последними в «Работе»: за ними приходят не каждый день, а когда
    // сводят месяц, — и они читают то, что накопили разделы выше.
    { module: "reports", perm: "reports.view", to: "/reports", label: t("reports"), icon: "analytics" },
    // Журнал звонков — рядом с почтой: и то и другое про разговоры с клиентом,
    // а подробности каждого разговора всё равно живут в ленте заявки.
    { module: "telephony", perm: "telephony.view", to: "/calls", label: t("calls"), icon: "call" },
    { module: "telegram", perm: "telegram.view", to: "/telegram", label: t("modTelegram"), icon: "send" },
  ]);

  const admin = allowed<NavItem>(user, modules, [
    {
      perm: "staff.view",
      to: "/staff",
      label: t("staff"),
      icon: "staff",
      badge: pendingCount,
      badgeTitle: t("signupRequests"),
    },
    // Фирмы — в «Админ», а не в «Работу»: реквизиты правят раз в несколько лет.
    // Пункт показываем тому, кто их правит; тот, кто только читает, попадает
    // сюда из заявки, и отдельная строка в меню была бы шумом.
    { module: "companies", perm: "companies.edit", to: "/companies", label: t("companies"), icon: "building" },
    // Файлы — это медиа досок, отдельного смысла без них не имеют.
    { module: "boards", perm: "settings.manage", to: "/files", label: t("files"), icon: "folder" },
    // Мониторинг — между «Файлами» и «Журналом»: оба соседа про «разбираться,
    // когда не сошлось», а не про работу с клиентом. Своё право, а не
    // `settings.manage`: дежурного по серверу можно пустить к панели, не отдавая
    // ему логотип сайта и переключатели блоков. Значок обязан совпадать с тем,
    // что стоит у блока в настройках, — две карты значков уже расходились молча.
    { module: "monitoring", perm: "monitoring.view", to: "/server", label: t("modMonitoring"), icon: "alert" },
    // Журнал последним: за ним приходят не работать, а разбираться, когда
    // что-то не сошлось. Замок — потому что это единственный раздел, где
    // ничего нельзя изменить, и это его главное свойство.
    { perm: "audit.view", to: "/audit", label: t("auditLog"), icon: "lock" },
  ]);

  // Настройки блока видны только когда блок включён. Раньше ящики и подключение
  // к АТС показывались всегда — «иначе перед включением нечего настраивать», — и
  // это противоречило правилу «выключено значит не видно»: у того, кто почтой не
  // пользуется, в настройках всё равно висел раздел про ящики. Порядок для
  // пользователя обратный: включить блок → настроить его.
  //
  // Разделы разложены по КАТЕГОРИЯМ (заказ владельца 02.09.2026): плоский
  // список из четырнадцати пунктов не отвечал на вопрос «где искать», а порядок
  // в нём сложился исторически. Категория складывается тем же `allowed`, что и
  // всё остальное, — то есть пустеет вместе с блоками и правами, а пустая
  // исчезает целиком.
  const kategoriya = (
    key: string,
    label: string,
    items: (Guarded & { to: string; label: string })[],
  ) => ({ key, label, items: allowed(user, modules, items) });

  const settingsItems: NavEntry[] = [
    // Блоки — первым пунктом и ВНЕ категорий. Не украшение порядка: они решают,
    // какие категории вообще существуют, поэтому лежать внутри одной из них не
    // могут. Заодно это единственный раздел, где переключатель применяется
    // сразу, а не по кнопке «Сохранить», — и рядом с теми, где по кнопке, он
    // обещал бы не то (разбор — App.tsx у маршрута `/settings/modules`).
    ...allowed<Guarded & { to: string; label: string }>(user, modules, [
      { perm: "settings.manage", to: "/settings/modules", label: t("modules") },
    ]),
    // Роли на своём праве: тот, кто раздаёт доступы, не обязательно правит
    // логотип сайта, и наоборот. Категория из одного раздела показывается самим
    // разделом, поэтому лишнего нажатия здесь не появится.
    kategoriya("access", t("catAccess"), [
      { perm: "roles.view", to: "/settings/roles", label: t("roles") },
    ]),
    // Как фирма называется и как с ней связаться. Оба раздела — про саму
    // фирму, а не про то, чем она пользуется.
    kategoriya("company", t("catCompany"), [
      { perm: "settings.manage", to: "/settings/brand", label: t("brand") },
      { perm: "settings.manage", to: "/settings/contacts", label: t("contacts") },
    ]),
    // Витрина и ссылка на сайт — под блоком `boards`: витрина показывает ДОСКИ,
    // и с выключенным блоком показывать ей нечего. Пункты при этом оставались, и
    // настроить можно было оформление того, чего в системе нет.
    kategoriya("showcase", t("catShowcase"), [
      { module: "boards", perm: "settings.manage", to: "/settings/showcase", label: t("showcase") },
      {
        module: "boards",
        perm: "settings.manage",
        to: "/settings/return-button",
        label: t("returnButtonShort"),
      },
    ]),
    // Склады как места и наклейки на коробки. Своё право у складов: их заводит
    // тот, кто отвечает за структуру, а не тот, кто правит логотип.
    kategoriya("stock", t("catStock"), [
      { module: "warehouse", perm: "warehouse.manage", to: "/settings/warehouses", label: t("warehouses") },
      { module: "labels", perm: "settings.manage", to: "/settings/labels", label: t("labelSettings") },
    ]),
    // Все способы, которыми клиент до нас достучится. Заявки с сайта — без
    // блока: они держатся на несущих (клиент и работа), выключить которые
    // нельзя, а выключателем служит сам ключ приёма.
    //
    // Настройки бота — на праве КАНАЛА, а не на общем `settings.manage`. Так
    // спрашивает сервер (`require_perm("telegram", "manage")`), и расхождение
    // было двусторонним: тот, кому канал доверили, пункта не видел вовсе, а
    // тот, кто правит логотип сайта, видел пункт и получал отказ на первом же
    // открытии.
    kategoriya("channels", t("catChannels"), [
      { module: "mail", perm: "settings.manage", to: "/settings/mailboxes", label: t("mailboxes") },
      { module: "telephony", perm: "settings.manage", to: "/settings/telephony", label: t("telephony") },
      { module: "telegram", perm: "telegram.manage", to: "/settings/telegram", label: t("modTelegram") },
      { perm: "settings.manage", to: "/settings/leads", label: t("leads") },
      { perm: "settings.manage", to: "/settings/api-keys", label: t("apiKeys") },
    ]),
    // Статьи и планы: справочник, который заводят один раз и правят редко, а
    // последствия правки видны во всех прошлых отчётах.
    kategoriya("money", t("catMoney"), [
      { module: "finance", perm: "finance.manage", to: "/settings/finance", label: t("finCategories") },
    ]),
    // Обслуживание — последним: за ним приходят, когда надо закрыть сайт, а не
    // когда настраивают работу.
    kategoriya("system", t("catSystem"), [
      { perm: "settings.manage", to: "/settings/maintenance", label: t("maintenance") },
      { perm: "settings.manage", to: "/settings/automation", label: t("automation") },
      { perm: "settings.manage", to: "/settings/backups", label: t("backups") },
    ]),
  ];

  return (
    <aside className={"sidebar" + (open ? " open" : "")}>
      <div className="side-top">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <NavLink to="/" className="side-brand">
            {/* Знак продукта, а не буква: ниже, в «рабочем пространстве»,
                стоит первая буква названия студии, и две буквы подряд читались
                как опечатка. Файл тот же, что у иконки вкладки. */}
            <img className="side-logo" src="/static/favicon.svg" alt="" width={20} height={20} />
            <span className="side-brand-name">OpenCRM</span>
          </NavLink>
        </div>
        {/* Только когда название конторы вправду задано. Без него сюда
            подставлялось «OpenCRM», и знак продукта дублировался строкой ниже —
            человек читал это как ошибку, а не как имя своей конторы. */}
        {!!settings.brand_name && (
        <div className="side-workspace">
          <div
            style={{
              width: 22,
              height: 22,
              borderRadius: 5,
              background: "var(--tint-brand)",
              color: "var(--brand)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 11,
              fontWeight: 700,
            }}
          >
            {brandName[0]?.toUpperCase() ?? "S"}
          </div>
          <div style={{ flex: 1, minWidth: 0, color: "var(--text)", fontSize: 13, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {brandName}
          </div>
        </div>
        )}
        {sozdat.length > 0 && (
          <div className="side-create-wrap">
            <button
              type="button"
              className="side-create"
              aria-expanded={sozdatOpen}
              onClick={() => setSozdatOpen((bylo) => !bylo)}
            >
              <Icon name="plus" size={14} stroke={2} />
              <span>{t("create")}</span>
            </button>
            {sozdatOpen && (
              <div className="side-create-menu" role="menu">
                {sozdat.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    className="side-create-item"
                    role="menuitem"
                    onClick={() => setSozdatOpen(false)}
                  >
                    <Icon name={item.icon} size={14} />
                    {item.label}
                  </NavLink>
                ))}
              </div>
            )}
          </div>
        )}
        <button type="button" className="side-search" onClick={onOpenSearch}>
          <Icon name="search" size={14} />
          <span style={{ flex: 1, color: "var(--faint)", fontSize: 13, textAlign: "left" }}>
            {t("search")}
          </span>
          <span style={{ display: "flex", gap: 3 }}>
            <span className="kbd">{navigator.platform.includes("Mac") ? "⌘" : "Ctrl"}</span>
            <span className="kbd">K</span>
          </span>
        </button>
      </div>
      <nav className="side-nav">
        {/* Наверху — то, с чего начинают день, и оно вне категорий: свернуть
            его нельзя, иначе просроченное напоминание можно спрятать от себя. */}
        {daily.map((item) => (
          <NavRow key={item.to} item={item} />
        ))}
        <NavSection id="work" label={t("navWork")} items={work} />
        <NavSection id="admin" label={t("admin")} items={admin}>
          {/* Условие именно на число ССЫЛОК, а не на длину списка: пустой
              `NavGroup` не рисует себя, но для `NavSection` он всё равно
              остаётся ребёнком — и категория «Админ» показывала бы заголовок
              без единого пункта тому, у кого нет ни одного из этих прав.
              Считать надо ссылки, а не пункты: список из семи категорий, в
              каждой из которых ничего не осталось, длину имеет, а показывать
              ему нечего. */}
          {vse_ssylki(settingsItems).length > 0 && (
            <NavGroup
              icon="settings"
              label={t("siteSettings")}
              base="/settings"
              items={settingsItems}
            />
          )}
        </NavSection>
      </nav>
      <div className="side-bottom">
        {storage && storage.level !== "ok" && (
          <NavLink
            to={can(user, "settings.manage") ? "/settings/maintenance" : "/"}
            className={"side-banner" + (storage.level === "critical" ? " critical" : "")}
          >
            <span style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
              <Icon name="alert" size={13} />
              <strong style={{ fontWeight: 600 }}>{t("storage")}</strong>
            </span>
            {storage.level === "critical"
              ? t("diskBannerCritical", { free: formatBytes(storage.free_bytes) })
              : t("diskBannerWarning", {
                  percent: storage.percent_used,
                  free: formatBytes(storage.free_bytes),
                })}
            {storage.uploads_blocked && <>. {t("uploadsBlocked")}</>}{" "}
            <span style={{ textDecoration: "underline", textUnderlineOffset: 2 }}>
              {t("diskBannerAction")}
            </span>
          </NavLink>
        )}
        <Kolokolchik />
        {/* Внутрь продукта, а не на GitHub: у человека вопрос прямо сейчас, и
            отправлять его читать в другое место — отправлять закрывать вкладку. */}
        <NavLink
          to="/docs"
          className="nav-item"
          style={{ color: "var(--muted)", fontSize: 13 }}
        >
          <Icon name="docs" size={15} />
          {t("documentation")}
        </NavLink>
        <div style={{ position: "relative" }}>
          {menuOpen && (
            <div className="user-menu">
              <NavLink to="/profile" className="user-menu-item" onClick={() => setMenuOpen(false)}>
                <Icon name="user" size={15} className="" />
                {t("profile")}
              </NavLink>
              <div className="user-menu-item" style={{ cursor: "default" }}>
                <Icon name="globe" size={15} />
                <span style={{ flex: 1 }}>{t("language")}</span>
                <span className="lang-pick">
                  {[
                    { id: "en", label: "EN" },
                    { id: "ru", label: "RU" },
                  ].map((lang) => (
                    <button
                      key={lang.id}
                      className={user?.locale === lang.id ? "active" : ""}
                      onClick={() => void setLocale(lang.id)}
                    >
                      {lang.label}
                    </button>
                  ))}
                </span>
              </div>
              <div className="user-menu-sep" />
              <button
                className="user-menu-item"
                style={{ color: "var(--danger)" }}
                onClick={() => {
                  void logout().then(() => navigate("/login"));
                }}
              >
                <Icon name="logout" size={15} />
                {t("signOut")}
              </button>
            </div>
          )}
          {/* Вне `.side-nav`: тот прокручивается, и кнопка внутри него уезжала бы
              из виду. Счётчика звёзд нет намеренно — за ним пришлось бы ходить на
              api.github.com, а панель на чужие серверы не ходит
              (`tests/test_monitoring.py`, CSP). */}
          <a
            className="side-github"
            href="https://github.com/DenisHumen/OpenCRM"
            target="_blank"
            rel="noopener noreferrer"
          >
            <span className="side-github-name">
              <Icon name="github" size={15} />
              github
            </span>
            {zvyozdy !== null && (
              <span className="side-github-stars">
                <Icon name="star" size={12} />
                {zvyozdy}
              </span>
            )}
          </a>
          <div className="side-user" onClick={() => setMenuOpen((open) => !open)}>
            <Avatar text={initials(user?.name ?? "?")} src={user?.avatar_url} online />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: "var(--text)", fontSize: 13, fontWeight: 500 }}>{user?.name}</div>
              {/* Должность, а не «менеджер»: ролей теперь столько, сколько их
                  завели, и подпись обязана называть ту, что у человека на
                  самом деле. */}
              <div style={{ color: "var(--faint)", fontSize: 11 }}>
                {user?.role === "root" ? t("root") : user?.role_name || t("noRole")}
              </div>
            </div>
            <Icon name="chevronsUpDown" size={14} />
          </div>
        </div>
      </div>
    </aside>
  );
}
