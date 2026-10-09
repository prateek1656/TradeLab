import { motion } from "framer-motion";
import { Activity, CandlestickChart, Home, LogOut, Settings, Wallet } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { initials } from "../lib/format";
import { FundsAlerts } from "./FundsAlerts";
import { Brand } from "./Logo";

const NAV = [
  { to: "/", label: "Home", icon: Home },
  { to: "/accounts", label: "Accounts", icon: Wallet },
  { to: "/trade", label: "Trade", icon: CandlestickChart },
  { to: "/activity", label: "Activity", icon: Activity },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const { pathname } = useLocation();
  const isActive = (to: string) => (to === "/" ? pathname === "/" : pathname.startsWith(to));

  return (
    <div className="shell">
      <aside className="sidebar">
        <Brand />
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink key={to} to={to} className={`nav-item ${isActive(to) ? "active" : ""}`}>
            {isActive(to) && <motion.span layoutId="side-nav" className="nav-bg" transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
            <Icon size={19} />
            {label}
          </NavLink>
        ))}
        <div className="spacer" />
        <div className="card" style={{ padding: 12, borderRadius: 16 }}>
          <div className="row">
            <div className="avatar" style={{ width: 38, height: 38, fontSize: 13, borderRadius: 12 }}>{initials(user?.name ?? "")}</div>
            <div className="truncate" style={{ flex: 1 }}>
              <div className="truncate" style={{ fontWeight: 700, fontSize: 14 }}>{user?.name}</div>
              <div className="truncate muted" style={{ fontSize: 12 }}>{user?.email}</div>
            </div>
            <button className="icon-btn" onClick={logout} aria-label="Sign out" title="Sign out">
              <LogOut size={17} />
            </button>
          </div>
        </div>
      </aside>

      <main className="main">{children}</main>
      <FundsAlerts />

      <nav className="bottom-nav">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink key={to} to={to} className={isActive(to) ? "active" : ""}>
            {isActive(to) && (
              <motion.span layoutId="bottom-nav" className="pill" transition={{ type: "spring", stiffness: 500, damping: 40 }} />
            )}
            <Icon size={21} />
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}

export function PageHeader({ title, subtitle, action }: { title: string; subtitle?: ReactNode; action?: ReactNode }) {
  return (
    <motion.div className="topbar" initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }}>
      <div style={{ minWidth: 0 }}>
        <h1 className="page-title">{title}</h1>
        {subtitle && <p className="page-sub">{subtitle}</p>}
      </div>
      {action}
    </motion.div>
  );
}
