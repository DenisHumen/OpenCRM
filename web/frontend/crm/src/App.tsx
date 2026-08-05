import { useEffect, useState } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { CommandPalette } from "./components/CommandPalette";
import { Sidebar } from "./components/Sidebar";
import { ScreenLoading, Toasts } from "./components/ui";
import { useApp } from "./lib/app";
import { moduleOn } from "./lib/modules";
import { AuthScreen, ForcePasswordChange } from "./screens/Auth";
import { BoardEditor } from "./screens/BoardEditor";
import { Boards } from "./screens/Boards";
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
import { Profile } from "./screens/Profile";
import {
  SettingsBrand,
  SettingsContacts,
  SettingsLayout,
  SettingsMaintenance,
  SettingsReturnButton,
  SettingsShowcase,
} from "./screens/Settings";
import { SettingsModules } from "./screens/SettingsModules";
import { Staff } from "./screens/Staff";
import { Tasks } from "./screens/Tasks";

function Protected() {
  const { user, ready } = useApp();
  const location = useLocation();
  const [searchOpen, setSearchOpen] = useState(false);

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

  if (!ready) return <ScreenLoading />;
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />;
  if (user.must_change_password) return <ForcePasswordChange />;
  return (
    // Полоса — над всей оболочкой, а не внутри: .app-shell это flex-строка
    // (сайдбар и содержимое), и любой её ребёнок становится ещё одной колонкой.
    <>
      <MaintenanceBar />
      <div className="app-shell">
        <Sidebar onOpenSearch={() => setSearchOpen(true)} />
        <main className="app-main">
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

function RootOnly() {
  const { user } = useApp();
  if (user?.role !== "root") return <Navigate to="/" replace />;
  return <Outlet />;
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

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/login" element={<AuthScreen />} />
        <Route element={<Protected />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/clients" element={<Clients />} />
          <Route path="/clients/:id" element={<ClientCard />} />
          <Route path="/deals" element={<Deals />} />
          <Route path="/deals/:id" element={<DealCard />} />
          <Route element={<ModuleRoute module="tasks" />}>
            <Route path="/tasks" element={<Tasks />} />
          </Route>
          {/* Фирмы не под RootOnly: читать реквизиты должен любой сотрудник —
              менеджеру надо видеть, от кого ведётся заявка. Правка закрыта на
              сервере, а карточка показывает менеджеру поля недоступными. */}
          <Route element={<ModuleRoute module="companies" />}>
            <Route path="/companies" element={<Companies />} />
            <Route path="/companies/:id" element={<CompanyCard />} />
          </Route>
          <Route element={<ModuleRoute module="documents" />}>
            <Route path="/documents" element={<Documents />} />
            <Route path="/documents/:id" element={<DocumentCard />} />
          </Route>
          <Route element={<ModuleRoute module="boards" />}>
            <Route path="/boards" element={<Boards />} />
            <Route path="/boards/:id" element={<BoardEditor />} />
          </Route>
          <Route path="/profile" element={<Profile />} />
          <Route element={<RootOnly />}>
            <Route path="/staff" element={<Staff />} />
            <Route element={<ModuleRoute module="boards" />}>
              <Route path="/files" element={<Files />} />
            </Route>
            {/* Модули стоят отдельным маршрутом, а не разделом SettingsLayout:
                там одна кнопка «Сохранить» на всю группу, а переключатель блока
                применяется сразу — общая кнопка вводила бы в заблуждение. */}
            <Route path="/settings/modules" element={<SettingsModules />} />
            {/* разделов настроек будет больше — каждый своим маршрутом,
                чтобы на них можно было сослаться и открыть из сайдбара */}
            <Route path="/settings" element={<SettingsLayout />}>
              <Route index element={<Navigate to="brand" replace />} />
              <Route path="brand" element={<SettingsBrand />} />
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
