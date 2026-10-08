import { motion } from "framer-motion";
import { Clock, Download, KeyRound, LogOut, Monitor, Moon, Palette, ShieldCheck, Smartphone, Sun } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "../components/AppShell";
import { Button, Field, Input, Segmented, Skeleton, Switch, stagger } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { hm } from "../lib/format";
import { useApi } from "../lib/hooks";
import { applyTheme, getThemePref, type ThemePref } from "../lib/theme";
import { useToast } from "../lib/toast";
import type { AutomationSettings, User } from "../lib/types";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
}
let deferredPrompt: BeforeInstallPromptEvent | null = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e as BeforeInstallPromptEvent;
});

export function SettingsPage() {
  const { user, logout } = useAuth();
  const toast = useToast();
  const settings = useApi<AutomationSettings>("/automation/settings");
  const [entry, setEntry] = useState("09:15");
  const [exit, setExit] = useState("14:30");
  const [saving, setSaving] = useState(false);
  const [theme, setTheme] = useState<ThemePref>(getThemePref());
  const [canInstall, setCanInstall] = useState(!!deferredPrompt);
  const standalone = window.matchMedia("(display-mode: standalone)").matches;

  useEffect(() => {
    if (settings.data) {
      setEntry(hm(settings.data.entry_time));
      setExit(hm(settings.data.exit_time));
    }
  }, [settings.data]);

  useEffect(() => {
    const on = () => setCanInstall(true);
    window.addEventListener("beforeinstallprompt", on);
    return () => window.removeEventListener("beforeinstallprompt", on);
  }, []);

  const save = async (patch: Partial<AutomationSettings>) => {
    setSaving(true);
    try {
      const s = await api<AutomationSettings>("/automation/settings", { method: "PUT", body: patch });
      settings.setData(s);
      toast("Schedule saved");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setSaving(false);
    }
  };

  const install = async () => {
    if (!deferredPrompt) return;
    await deferredPrompt.prompt();
    deferredPrompt = null;
    setCanInstall(false);
  };

  const s = settings.data;
  const dirty = s && (entry !== hm(s.entry_time) || exit !== hm(s.exit_time));

  return (
    <>
      <PageHeader title="Settings" subtitle={user?.email} action={user?.role === "admin" ? <Link to="/admin" className="btn btn-ghost sm"><ShieldCheck size={16} /> Admin panel</Link> : null} />
      <div className="grid grid-main">
        <div className="stack">
          <motion.div className="card pad-lg" {...stagger(0)}>
            <div className="card-title"><span className="row"><span className="icon-bubble"><Clock size={18} /></span> Daily schedule (IST)</span></div>
            {!s ? (
              <Skeleton h={160} />
            ) : (
              <div className="stack">
                <div className="form-grid two">
                  <Field label="Market buy at" help="Market opens 09:15">
                    <Input type="time" value={entry} min="09:15" max="15:29" onChange={(e) => setEntry(e.target.value)} />
                  </Field>
                  <Field label="Hard square-off at" help="Before broker auto square-off (~15:20)">
                    <Input type="time" value={exit} min="09:16" max="15:29" onChange={(e) => setExit(e.target.value)} />
                  </Field>
                </div>
                {dirty && (
                  <Button onClick={() => save({ entry_time: entry, exit_time: exit })} loading={saving}>Save times</Button>
                )}
                <hr className="divider" style={{ margin: "4px 0" }} />
                <div className="row between">
                  <div><b>Automatic entry</b><div className="muted" style={{ fontSize: 13 }}>Buy selected stocks at {hm(s.entry_time)} on trading days</div></div>
                  <Switch checked={s.auto_entry} onChange={(v) => save({ auto_entry: v })} label="Automatic entry" />
                </div>
                <div className="row between">
                  <div><b>Automatic square-off</b><div className="muted" style={{ fontSize: 13 }}>Close positions at {hm(s.exit_time)}</div></div>
                  <Switch checked={s.auto_exit} onChange={(v) => save({ auto_exit: v })} label="Automatic square-off" />
                </div>
                <div className="field">
                  <span className="label-text">Square-off closes</span>
                  <Segmented block tone="bull" value={s.squareoff_scope} onChange={(v) => save({ squareoff_scope: v })} options={[{ value: "all", label: "Everything" }, { value: "intraday", label: "Intraday (MIS) only" }]} />
                  <span className="help">{s.squareoff_scope === "all" ? "Every open position — including delivery bought today — is sold." : "Only MIS/intraday positions are closed; delivery stays."}</span>
                </div>
              </div>
            )}
          </motion.div>

          <motion.div className="card pad-lg" {...stagger(1)}>
            <div className="card-title"><span className="row"><span className="icon-bubble"><Palette size={18} /></span> Appearance</span></div>
            <Segmented
              block
              tone="bull"
              value={theme}
              onChange={(t) => {
                setTheme(t);
                applyTheme(t);
              }}
              options={[
                { value: "dark", label: <span className="row" style={{ gap: 6, justifyContent: "center" }}><Moon size={15} /> Dark</span> },
                { value: "light", label: <span className="row" style={{ gap: 6, justifyContent: "center" }}><Sun size={15} /> Light</span> },
                { value: "system", label: <span className="row" style={{ gap: 6, justifyContent: "center" }}><Monitor size={15} /> Auto</span> },
              ]}
            />
          </motion.div>
        </div>

        <div className="stack">
          <motion.div className="card pad-lg" {...stagger(2)}>
            <div className="card-title"><span className="row"><span className="icon-bubble"><Download size={18} /></span> Install the app</span></div>
            {standalone ? (
              <div className="banner bull"><ShieldCheck size={17} /> You're using the installed app.</div>
            ) : (
              <div className="stack" style={{ gap: 12, fontSize: 14, color: "var(--text-2)" }}>
                {canInstall && <Button onClick={install} block><Download size={17} /> Install TradeLab</Button>}
                <div className="row" style={{ alignItems: "flex-start" }}><Smartphone size={18} className="up" style={{ flexShrink: 0, marginTop: 2 }} /><span><b>Android:</b> open in Chrome → ⋮ menu → <i>Install app</i> / <i>Add to Home screen</i>.</span></div>
                <div className="row" style={{ alignItems: "flex-start" }}><Monitor size={18} className="up" style={{ flexShrink: 0, marginTop: 2 }} /><span><b>Windows:</b> open in Edge or Chrome → click the install icon in the address bar. It then opens from the Start menu like any app.</span></div>
              </div>
            )}
          </motion.div>

          <motion.div className="card pad-lg" {...stagger(3)}>
            <div className="card-title"><span className="row"><span className="icon-bubble"><KeyRound size={18} /></span> Password</span></div>
            <ChangePassword />
          </motion.div>

          <motion.div {...stagger(4)}>
            <Button variant="danger" block onClick={logout}><LogOut size={18} /> Sign out</Button>
          </motion.div>
        </div>
      </div>
    </>
  );
}

function ChangePassword() {
  const { setSession } = useAuth();
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await api<{ token: string; user: User }>("/auth/change-password", { body: { current_password: current, new_password: next } });
      setSession(r.token, r.user);
      setCurrent("");
      setNext("");
      toast("Password changed — other devices were signed out");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="stack" onSubmit={submit}>
      <Input type="password" placeholder="Current password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" required />
      <Input type="password" placeholder="New password (8+ characters)" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" minLength={8} required />
      <Button type="submit" variant="ghost" loading={busy}>Change password</Button>
    </form>
  );
}
