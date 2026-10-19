import { Ban, Check, LogOut, ShieldCheck, Trash2, UserCheck } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Brand } from "../components/Logo";
import { Button, Field, Input, Pill, Spinner, Switch } from "../components/ui";
import { ApiError, api } from "../lib/api";
import { useToast } from "../lib/toast";

interface AdminUser {
  id: number;
  name: string;
  email: string;
  status: "active" | "pending" | "blocked";
  created_at: string;
  last_login_at: string | null;
  accounts: number;
  is_owner: boolean;
}

// Only for this browser tab: closing the tab ends the admin session.
const TOKEN_KEY = "tl-admin";
const adminToken = {
  get: () => {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set: (t: string | null) => {
    try {
      if (t) sessionStorage.setItem(TOKEN_KEY, t);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* private mode: the session simply isn't kept */
    }
  },
};

function when(iso: string | null) {
  if (!iso) return "never";
  return new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short" });
}

/** The admin page: reached only through its secret address; decides who may use TradeLab. */
export function AdminPage() {
  const location = useLocation();
  const nav = useNavigate();
  const key = location.pathname.split("/")[2] ?? "";
  const base = `/admin/${encodeURIComponent(key)}`;
  const [phase, setPhase] = useState<"checking" | "login" | "ready">("checking");
  const [token, setToken] = useState<string | null>(adminToken.get());

  useEffect(() => {
    api(`${base}/check`)
      .then(() => setPhase(adminToken.get() ? "ready" : "login"))
      .catch(() => nav("/", { replace: true })); // a wrong address looks like any unknown page
  }, [base, nav]);

  const signOut = useCallback(() => {
    adminToken.set(null);
    setToken(null);
    setPhase("login");
  }, []);

  if (phase === "checking") {
    return (
      <div style={{ height: "100%", display: "grid", placeItems: "center" }}>
        <Spinner size={28} />
      </div>
    );
  }
  if (phase === "login" || !token) {
    return (
      <AdminLogin
        base={base}
        onIn={(t) => {
          adminToken.set(t);
          setToken(t);
          setPhase("ready");
        }}
      />
    );
  }
  return <AdminConsole base={base} token={token} onOut={signOut} />;
}

function AdminLogin({ base, onIn }: { base: string; onIn: (token: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ token: string }>(`${base}/login`, { body: { username, password } });
      setPassword("");
      onIn(r.token);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: "100%", display: "grid", placeItems: "center", padding: 16 }}>
      <form className="card pad-lg stack" style={{ width: "100%", maxWidth: 400 }} onSubmit={submit}>
        <Brand />
        <h1 style={{ margin: "8px 0 0", fontSize: 22 }}>Administrator</h1>
        <p className="muted" style={{ margin: 0 }}>Decide who may use TradeLab.</p>
        <Field label="Username">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoCapitalize="none" autoCorrect="off" spellCheck={false} required />
        </Field>
        <Field label="Password">
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </Field>
        {error && <div className="banner error">{error}</div>}
        <Button type="submit" size="lg" block loading={busy}>
          <ShieldCheck size={18} /> Log in
        </Button>
      </form>
    </div>
  );
}

function AdminConsole({ base, token, onOut }: { base: string; token: string; onOut: () => void }) {
  const toast = useToast();
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [signupsOpen, setSignupsOpen] = useState<boolean | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const call = useCallback(
    async <T,>(path: string, opts: { method?: string; body?: unknown } = {}) => {
      try {
        return await api<T>(`${base}${path}`, { ...opts, authToken: token });
      } catch (e) {
        if (e instanceof ApiError && (e.status === 401 || e.status === 404)) onOut();
        throw e;
      }
    },
    [base, token, onOut],
  );

  const load = useCallback(async () => {
    try {
      const [u, s] = await Promise.all([call<AdminUser[]>("/users"), call<{ signups_open: boolean }>("/settings")]);
      setUsers(u);
      setSignupsOpen(s.signups_open);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }, [call, toast]);

  useEffect(() => {
    load();
  }, [load]);

  const setStatus = async (u: AdminUser, status: "active" | "blocked") => {
    if (status === "blocked" && !window.confirm(`Block ${u.name}? They are signed out at once and today's confirmed buys are cancelled. Automatic square-offs still close what's open.`)) return;
    setBusy(u.id);
    try {
      await call(`/users/${u.id}/status`, { body: { status } });
      toast(status === "active" ? `${u.name} can use TradeLab` : `${u.name} is blocked`);
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const reject = async (u: AdminUser) => {
    if (!window.confirm(`Turn down ${u.name}'s request? Their sign-up is deleted.`)) return;
    setBusy(u.id);
    try {
      await call(`/users/${u.id}`, { method: "DELETE" });
      toast("Request turned down");
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const toggleSignups = async (open: boolean) => {
    try {
      const r = await call<{ signups_open: boolean }>("/settings", { method: "PUT", body: { signups_open: open } });
      setSignupsOpen(r.signups_open);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const pending = users?.filter((u) => u.status === "pending") ?? [];
  const others = users?.filter((u) => u.status !== "pending") ?? [];

  return (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: "20px 16px 40px" }} className="stack">
      <div className="row between">
        <Brand />
        <Button variant="ghost" onClick={onOut}>
          <LogOut size={17} /> Log out
        </Button>
      </div>
      <h1 style={{ margin: 0 }}>Administrator</h1>

      <div className="card pad-lg row between" style={{ gap: 16 }}>
        <div>
          <div style={{ fontWeight: 700 }}>New sign-up requests</div>
          <div className="muted" style={{ fontSize: 13 }}>
            {signupsOpen === false
              ? "Closed: nobody can sign up."
              : "Open: anyone with the address can ask, and only you can let them in."}
          </div>
        </div>
        <Switch checked={!!signupsOpen} onChange={toggleSignups} disabled={signupsOpen === null} label="Allow sign-up requests" />
      </div>

      <div className="card pad-lg stack">
        <div style={{ fontWeight: 700 }}>Waiting for approval {pending.length ? `(${pending.length})` : ""}</div>
        {users === null && <Spinner />}
        {users !== null && pending.length === 0 && <div className="muted">No requests.</div>}
        {pending.map((u) => (
          <div key={u.id} className="row between" style={{ gap: 12, flexWrap: "wrap" }}>
            <div>
              <div style={{ fontWeight: 600 }}>{u.name}</div>
              <div className="muted" style={{ fontSize: 13 }}>{u.email} · asked {when(u.created_at)}</div>
            </div>
            <div className="row" style={{ gap: 8 }}>
              <Button onClick={() => setStatus(u, "active")} loading={busy === u.id}>
                <Check size={17} /> Approve
              </Button>
              <Button variant="ghost" onClick={() => reject(u)} disabled={busy === u.id}>
                <Trash2 size={17} /> Turn down
              </Button>
            </div>
          </div>
        ))}
      </div>

      <div className="card pad-lg stack">
        <div style={{ fontWeight: 700 }}>People with access</div>
        {users !== null && others.length === 0 && <div className="muted">Nobody yet.</div>}
        {others.map((u) => (
          <div key={u.id} className="row between" style={{ gap: 12, flexWrap: "wrap" }}>
            <div>
              <div className="row" style={{ gap: 8 }}>
                <span style={{ fontWeight: 600 }}>{u.name}</span>
                {u.status === "blocked" ? <Pill tone="bear">Blocked</Pill> : <Pill tone="bull">Active</Pill>}
                {u.is_owner && <Pill tone="info">Owner</Pill>}
              </div>
              <div className="muted" style={{ fontSize: 13 }}>
                {u.email} · {u.accounts} Kotak account{u.accounts === 1 ? "" : "s"} · last login {when(u.last_login_at)}
              </div>
            </div>
            {u.status === "blocked" ? (
              <Button variant="ghost" onClick={() => setStatus(u, "active")} loading={busy === u.id}>
                <UserCheck size={17} /> Unblock
              </Button>
            ) : (
              <Button variant="danger" onClick={() => setStatus(u, "blocked")} loading={busy === u.id}>
                <Ban size={17} /> Block
              </Button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
