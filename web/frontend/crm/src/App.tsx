import { Fragment, useEffect, useState, type ReactNode } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useParams } from "react-router-dom";

import { CommandPalette } from "./components/CommandPalette";
import { Icon } from "./components/Icon";
import { Sidebar } from "./components/Sidebar";
import { ScreenLoading, Toasts } from "./components/ui";
import { useApp } from "./lib/app";
import { moduleOn } from "./lib/modules";
import { podpisatsya } from "./lib/tgpotok";
import {
  chat_na_vidu,
  nachat_vybory,
  signal_o_soobshchenii,
} from "./lib/signaly";
import { can } from "./lib/permissions";
import { Audit } from "./screens/Audit";
import { AuthScreen, ForcePasswordChange } from "./screens/Auth";
import { BoardEditor } from "./screens/BoardEditor";
import { Boards } from "./screens/Boards";
import { Calls } from "./screens/Calls";
import { Telegram } from "./screens/Telegram";
import { ClientCard } from "./screens/ClientCard";
import { Clients } from "./screens/Clients";
import { Companies } from "./screens/Companies";
import { CompanyCard } from "./screens/CompanyCard";
import { Dashboard } from "./screens/Dashboard";
import { DealCard } from "./screens/DealCard";
import { Deals } from "./screens/Deals";
import { DocumentCard } from "./screens/DocumentCard";
import { Documents } from "./screens/Documents";
import { Files } from "./screens/Files";
import { SettingsLabels } from "./screens/LabelSettings";
import { SettingsLeads } from "./screens/LeadsSettings";
import { Mail } from "./screens/Mail";
import { Monitoring } from "./screens/Monitoring";
import { Templates } from "./screens/Templates";
import { Mailboxes } from "./screens/Mailboxes";
import { ProductCard } from "./screens/ProductCard";
import { Profile } from "./screens/Profile";
import { Docs } from "./screens/Docs";
import { Finance } from "./screens/Finance";
import { FinanceSettings } from "./screens/FinanceSettings";
import { Reports } from "./screens/Reports";
import {
  SettingsBrand,
  SettingsContacts,
  SettingsLayout,
  SettingsMaintenance,
  SettingsReturnButton,
  SettingsShowcase,
} from "./screens/Settings";
import { SettingsModules } from "./screens/SettingsModules";
import { OrderCard } from "./screens/OrderCard";
import { WaybillCard } from "./screens/WaybillCard";
import { Orders } from "./screens/Orders";
import { Waybills } from "./screens/Waybills";
import { SettingsRoles } from "./screens/SettingsRoles";
import { Setup } from "./screens/Setup";
import { SettingsWarehouses } from "./screens/SettingsWarehouses";
import { Staff } from "./screens/Staff";
import { Tasks } from "./screens/Tasks";
import { SettingsTelegram } from "./screens/TelegramSettings";
import { SettingsTelephony } from "./screens/TelephonySettings";
import { Warehouse } from "./screens/Warehouse";

function Protected() {
  const { user, ready, t } = useApp();
  const location = useLocation();
  const [searchOpen, setSearchOpen] = useState(false);
  // Меню выезжает поверх содержимого только на узком окне (см. медиазапрос в
  // styles.css). На широком класс `open` ни на что не влияет — сайдбар и так
  // стоит на месте, и второе состояние ему не нужно.
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((open) => !open);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Переход по пункту меню закрывает само меню: иначе на телефоне открытый
  // экран остаётся под шторкой, и кажется, что нажатие не сработало.
  useEffect(() => setNavOpen(false), [location.pathname]);

  if (!ready) return <ScreenLoading />;
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />;
  if (user.must_change_password) return <ForcePasswordChange />;
  return (
    // Полоса — над всей оболочкой, а не внутри: .app-shell это flex-строка
    // (сайдбар и содержимое), и любой её ребёнок становится ещё одной колонкой.
    <>
      <MaintenanceBar />
      <SignalyTelegrama />
      <div className="app-shell">
        <Sidebar open={navOpen} onOpenSearch={() => setSearchOpen(true)} />
        {navOpen && (
          <button
            type="button"
            className="side-scrim"
            aria-label={t("closeMenu")}
            onClick={() => setNavOpen(false)}
          />
        )}
        <main className="app-main">
          {/* Полоса с кнопкой меню видна только на узком окне: там сайдбар
              уехал за край, и попасть в навигацию больше неоткуда. */}
          <div className="app-topbar">
            <button
              type="button"
              className="topbar-btn"
              aria-label={t("menu")}
              onClick={() => setNavOpen(true)}
            >
              <Icon name="menu" size={18} />
            </button>
            <span className="topbar-title">OpenCRM</span>
          </div>
          <Outlet />
        </main>
        {searchOpen && <CommandPalette onClose={() => setSearchOpen(false)} />}
      </div>
    </>
  );
}

/** Полоса «сайт закрыт» поверх интерфейса.
 *
 * Режим видит только root, и снаружи он выглядит как обычная работа: CRM
 * открывается, доски редактируются. Забыть его включённым — значит молча
 * держать закрытыми и витрины клиентов, и вход остальным сотрудникам. Поэтому
 * напоминание висит на каждом экране, а не только в настройках. */
function MaintenanceBar() {
  const { maintenance, t } = useApp();
  if (!maintenance?.enabled) return null;
  return (
    <Link className="maintenance-bar" to="/settings/maintenance">
      <span className="dot" />
      {t("closedBanner")}
    </Link>
  );
}

/** Маршруты выключенного блока.
 *
 * Сервер такой раздел всё равно закроет, но упереться в пустой экран с ошибкой —
 * плохой ответ на переход по старой закладке. Уводим на главную: раздела просто
 * нет, как нет его и в меню. */
function ModuleRoute({ module }: { module: string }) {
  const { modules } = useApp();
  if (!moduleOn(modules, module)) return <Navigate to="/" replace />;
  return <Outlet />;
}

/**
 * Карточка записи, собираемая заново при смене номера в адресе.
 *
 * Переход с клиента А на клиента Б — это тот же маршрут, и React оставляет
 * карточку смонтированной со всем её состоянием. Полсекунды до ответа сервера
 * на экране висят имя, телефон и лента предыдущего клиента — то есть данные А
 * под адресом Б.
 *
 * Хуже с полями, которые держат содержимое сами: у уже смонтированного поля
 * `defaultValue` ничего не меняет, и название заявки А оставалось в поле над
 * заявкой Б — а по потере фокуса уезжало на сервер как правка Б. Ровно так же
 * вёл себя черновик названия доски в редакторе.
 *
 * Ключ по номеру рвёт эту связь в одном месте на все карточки: у другой записи
 * другая карточка, а не та же с другими данными. Пока новая грузится, экран
 * показывает вертушку — честный ответ вместо чужих данных.
 */
function ById({ children }: { children: ReactNode }) {
  const { id } = useParams();
  return <Fragment key={id}>{children}</Fragment>;
}

/** Маршруты, на которые не хватает прав.
 *
 * Ровно как `ModuleRoute` и ровно с тем же весом: это удобство, а не защита.
 * Сервер откажет и без этого (`require_perm`), но человек, перешедший по старой
 * закладке, упёрся бы в экран с ошибкой вместо того раздела, куда шёл. Уводим
 * на главную — раздела для него просто нет, как нет его и в меню. */
function PermRoute({ perm }: { perm: string }) {
  const { user } = useApp();
  if (!can(user, perm)) return <Navigate to="/" replace />;
  return <Outlet />;
}

/**
 * Сигналы о письмах клиентов — на уровне оболочки, а не экрана мессенджера.
 *
 * Иначе уведомление получал бы ровно тот, кто и так смотрит в переписку.
 * Менеджер работает в заявках, и услышать он должен оттуда.
 *
 * Соединение общее (`lib/tgpotok`): экран мессенджера подписывается на тот же
 * источник, а не открывает второй.
 */
function SignalyTelegrama() {
  const { modules, user } = useApp();
  const dostupen = moduleOn(modules, "telegram") && can(user, "telegram.view");

  useEffect(() => {
    if (!dostupen) return;
    // Выборы «сигналящей» вкладки начинаем сразу: замок достаётся первой
    // открытой, а не той, где успели нажать кнопку.
    nachat_vybory();
    return podpisatsya((sobytie) => {
      if (sobytie.type !== "message" || sobytie.direction !== "in") return;
      if (sobytie.chat_id == null) return;
      signal_o_soobshchenii({
        zagolovok: sobytie.title || "Telegram",
        telo: sobytie.preview || "",
        na_vidu: chat_na_vidu(sobytie.chat_id),
      });
    });
  }, [dostupen]);

  return null;
}

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/login" element={<AuthScreen />} />
        <Route element={<Protected />}>
          <Route path="/" element={<Dashboard />} />
          {/* Порядок обёрток тот же, что порядок проверок на сервере: сначала
              блок, потом право. Выключенный блок исчезает для всех, включая
              того, у кого право на него есть. */}
          <Route element={<PermRoute perm="clients.view" />}>
            <Route path="/clients" element={<Clients />} />
            <Route path="/clients/:id" element={<ById><ClientCard /></ById>} />
          </Route>
          <Route element={<PermRoute perm="deals.view" />}>
            <Route path="/deals" element={<Deals />} />
            <Route path="/deals/:id" element={<ById><DealCard /></ById>} />
          </Route>
          <Route element={<ModuleRoute module="tasks" />}>
            <Route element={<PermRoute perm="tasks.view" />}>
              <Route path="/tasks" element={<Tasks />} />
            </Route>
          </Route>
          {/* Фирмы открыты не только тому, кто их правит: читать реквизиты
              должен любой, кому надо видеть, от кого ведётся заявка. Правка
              закрыта на сервере, а карточка показывает поля недоступными. */}
          <Route element={<ModuleRoute module="companies" />}>
            <Route element={<PermRoute perm="companies.view" />}>
              <Route path="/companies" element={<Companies />} />
              <Route path="/companies/:id" element={<ById><CompanyCard /></ById>} />
            </Route>
          </Route>
          <Route element={<ModuleRoute module="documents" />}>
            <Route element={<PermRoute perm="documents.view" />}>
              <Route path="/documents" element={<Documents />} />
              <Route path="/documents/:id" element={<ById><DocumentCard /></ById>} />
            </Route>
          </Route>
          <Route element={<ModuleRoute module="boards" />}>
            <Route element={<PermRoute perm="boards.view" />}>
              <Route path="/boards" element={<Boards />} />
              <Route path="/boards/:id" element={<ById><BoardEditor /></ById>} />
            </Route>
          </Route>

          <Route element={<ModuleRoute module="warehouse" />}>
            <Route element={<PermRoute perm="warehouse.view" />}>
              <Route path="/warehouse" element={<Warehouse />} />
              <Route path="/warehouse/:id" element={<ById><ProductCard /></ById>} />
            </Route>
            <Route element={<ModuleRoute module="orders" />}>
              <Route path="/orders" element={<Orders />} />
              <Route path="/orders/:id" element={<ById><OrderCard /></ById>} />
            </Route>
            <Route element={<ModuleRoute module="waybills" />}>
              <Route path="/waybills" element={<Waybills />} />
              <Route path="/waybills/:id" element={<ById><WaybillCard /></ById>} />
            </Route>
          </Route>
          {/* Деньги отделены от отчётов правом, а не только блоком: отчёты
              показывают, как идут дела, а этот раздел — сколько заработали и
              кому заплатили. Второе видно не всем, кому видно первое. */}
          <Route element={<ModuleRoute module="finance" />}>
            <Route element={<PermRoute perm="finance.view" />}>
              <Route path="/finance" element={<Finance />} />
            </Route>
          </Route>
          <Route element={<ModuleRoute module="reports" />}>
            <Route element={<PermRoute perm="reports.view" />}>
              <Route path="/reports" element={<Reports />} />
            </Route>
          </Route>
          <Route element={<ModuleRoute module="templates" />}>
            <Route element={<PermRoute perm="templates.view" />}>
              <Route path="/templates" element={<Templates />} />
            </Route>
          </Route>
          <Route element={<ModuleRoute module="mail" />}>
            <Route element={<PermRoute perm="mail.view" />}>
              <Route path="/mail" element={<Mail />} />
            </Route>
          </Route>
          <Route element={<ModuleRoute module="telephony" />}>
            <Route element={<PermRoute perm="telephony.view" />}>
              <Route path="/calls" element={<Calls />} />
            </Route>
          </Route>
          <Route element={<ModuleRoute module="telegram" />}>
            <Route element={<PermRoute perm="telegram.view" />}>
              <Route path="/telegram" element={<Telegram />} />
            </Route>
          </Route>
          {/* Мониторинг живёт по адресу `/server`, а НЕ по `/monitoring`:
              последний перехватывает nginx (`location = /monitoring` отдаёт 301
              на `/monitoring/`, то есть в Grafana). Переход внутри SPA работал
              бы, а F5, закладка и ссылка из письма уводили бы в панель —
              поломка тихая и вылезла бы только на боевом. */}
          <Route element={<ModuleRoute module="monitoring" />}>
            <Route element={<PermRoute perm="monitoring.view" />}>
              <Route path="/server" element={<Monitoring />} />
            </Route>
          </Route>
          <Route path="/profile" element={<Profile />} />
          <Route path="/docs" element={<Docs />} />
          <Route element={<PermRoute perm="staff.view" />}>
            <Route path="/staff" element={<Staff />} />
          </Route>
          {/* Журнал — своё право: за ним приходят не работать, а разбираться,
              когда что-то не сошлось, и пускать туда всех, кто видит список
              сотрудников, незачем. */}
          <Route element={<PermRoute perm="audit.view" />}>
            <Route path="/audit" element={<Audit />} />
          </Route>
          {/* Роли — своё право, а не часть настроек: решать, кому что можно,
              и менять логотип сайта — разные по весу решения. */}
          <Route element={<PermRoute perm="roles.view" />}>
            <Route path="/settings/roles" element={<SettingsRoles />} />
          </Route>
          {/* Склады — своё право и свой блок. Не внутри группы `settings.manage`:
              завести склад и поменять логотип сайта — разные по весу решения, и
              кладовщику первое нужно, а второе нет. */}
          <Route element={<ModuleRoute module="warehouse" />}>
            <Route element={<PermRoute perm="warehouse.manage" />}>
              <Route path="/settings/warehouses" element={<SettingsWarehouses />} />
            </Route>
          </Route>
          {/* Статьи и планы правит тот, кто отвечает за деньги, а не всякий, кто
              их видит: переименованная статья меняет прошлые отчёты. */}
          <Route element={<ModuleRoute module="finance" />}>
            <Route element={<PermRoute perm="finance.manage" />}>
              <Route path="/settings/finance" element={<FinanceSettings />} />
            </Route>
          </Route>
          {/* Настройки бота — на праве КАНАЛА, как и спрашивает сервер
              (`require_perm("telegram", "manage")` у всех ручек настроек).
              Стояли они в общей группе `settings.manage`, и расходилось это в
              обе стороны: тому, кому канал доверили, экран был недоступен, а
              тот, кто правит логотип сайта, доходил до экрана и получал отказ
              на первом же запросе. Токен бота и логотип витрины — решения
              разного веса, ровно как склады и статьи денег выше. */}
          <Route element={<ModuleRoute module="telegram" />}>
            <Route element={<PermRoute perm="telegram.manage" />}>
              <Route path="/settings/telegram" element={<SettingsTelegram />} />
            </Route>
          </Route>
          <Route element={<PermRoute perm="settings.manage" />}>
            <Route element={<ModuleRoute module="boards" />}>
              <Route path="/files" element={<Files />} />
            </Route>
            {/* Модули стоят отдельным маршрутом, а не разделом SettingsLayout:
                там одна кнопка «Сохранить» на всю группу, а переключатель блока
                применяется сразу — общая кнопка вводила бы в заблуждение. */}
            <Route path="/settings/modules" element={<SettingsModules />} />
            {/* Экран первого входа. Живёт маршрутом, а не всплывающим окном:
                на него надо уметь вернуться из настроек и дать ссылку. */}
            <Route path="/setup" element={<Setup />} />

            {/* Настройки блока закрыты вместе с блоком. Раньше ящики и
                подключение к АТС были открыты всегда — «иначе перед включением
                нечего настраивать», — но выключенный блок обязан исчезать
                отовсюду, включая настройки. Порядок для пользователя:
                включить блок → настроить его. */}
            <Route element={<ModuleRoute module="mail" />}>
              <Route path="/settings/mailboxes" element={<Mailboxes />} />
            </Route>
            {/* Подключение к АТС — вне SettingsLayout: у него свои поля, своя
                кнопка сохранения и секрет, который показывается один раз. */}
            <Route element={<ModuleRoute module="telephony" />}>
              <Route path="/settings/telephony" element={<SettingsTelephony />} />
            </Route>
            {/* Приём заявок с сайта — без `ModuleRoute`: своего блока у него нет
                и не нужно. Форма заводит клиента и работу, а оба эти блока
                несущие (`core/modules.py`) — выключить их нельзя, значит и
                прятать экран не от чего. Выключателем служит сам ключ: пустой
                означает, что приёма не существует. */}
            <Route path="/settings/leads" element={<SettingsLeads />} />
            {/* разделов настроек будет больше — каждый своим маршрутом,
                чтобы на них можно было сослаться и открыть из сайдбара */}
            <Route path="/settings" element={<SettingsLayout />}>
              <Route index element={<Navigate to="brand" replace />} />
              <Route path="brand" element={<SettingsBrand />} />
              {/* Наклейка — настройка ПЕЧАТИ, а не витрины, и живёт она здесь
                  по одной причине: сохранение у раздела общее. Своего экрана у
                  неё не было вовсе, хотя ключи в настройках лежали с самого
                  начала, — то есть размер рулона и состав полей задать было
                  нечем. */}
              <Route path="labels" element={<SettingsLabels />} />
              <Route path="contacts" element={<SettingsContacts />} />
              <Route path="showcase" element={<SettingsShowcase />} />
              <Route path="return-button" element={<SettingsReturnButton />} />
              <Route path="maintenance" element={<SettingsMaintenance />} />
            </Route>
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
      <Toasts />
    </>
  );
}
