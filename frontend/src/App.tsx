import { motion } from "framer-motion";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { LogoMark } from "./components/Logo";
import { useAuth } from "./lib/auth";
import { AccountsPage } from "./pages/Accounts";
import { ActivityPage } from "./pages/Activity";
import { LoginPage, SignupPage } from "./pages/Auth";
import { DashboardPage } from "./pages/Dashboard";
import { SettingsPage } from "./pages/Settings";
import { TradePage } from "./pages/Trade";

function Splash() {
  return (
    <div style={{ height: "100%", display: "grid", placeItems: "center" }}>
      <motion.div animate={{ scale: [1, 1.08, 1] }} transition={{ repeat: Infinity, duration: 1.4 }}>
        <LogoMark size={64} />
      </motion.div>
    </div>
  );
}

export default function App() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <Splash />;

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <AppShell>
        <motion.div key={location.pathname.startsWith("/trade") ? "/trade" : location.pathname} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.18 }}>
          <ErrorBoundary key={location.pathname}>
          <Routes location={location}>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/trade/:section" element={<TradePage />} />
            <Route path="/trade" element={<Navigate to="/trade/intraday" replace />} />
            <Route path="/stocks" element={<Navigate to="/trade/intraday" replace />} />
            <Route path="/activity" element={<ActivityPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          </ErrorBoundary>
        </motion.div>
    </AppShell>
  );
}
