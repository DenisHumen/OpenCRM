import { useEffect, useState } from "react";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { CommandPalette } from "./components/CommandPalette";
import { Sidebar } from "./components/Sidebar";
import { ScreenLoading, Toasts } from "./components/ui";
import { useApp } from "./lib/app";
import { AuthScreen, ForcePasswordChange } from "./screens/Auth";
import { BoardEditor } from "./screens/BoardEditor";
import { Boards } from "./screens/Boards";
import { ClientCard } from "./screens/ClientCard";
import { Clients } from "./screens/Clients";
import { Dashboard } from "./screens/Dashboard";
import { Files } from "./screens/Files";
import { Profile } from "./screens/Profile";
import { Settings } from "./screens/Settings";
import { Staff } from "./screens/Staff";

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
    <div className="app-shell">
      <Sidebar onOpenSearch={() => setSearchOpen(true)} />
      <main className="app-main">
        <Outlet />
      </main>
      {searchOpen && <CommandPalette onClose={() => setSearchOpen(false)} />}
    </div>
  );
}

function RootOnly() {
  const { user } = useApp();
  if (user?.role !== "root") return <Navigate to="/" replace />;
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
          <Route path="/boards" element={<Boards />} />
          <Route path="/boards/:id" element={<BoardEditor />} />
          <Route path="/profile" element={<Profile />} />
          <Route element={<RootOnly />}>
            <Route path="/staff" element={<Staff />} />
            <Route path="/files" element={<Files />} />
            <Route path="/settings" element={<Settings />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
      <Toasts />
    </>
  );
}
