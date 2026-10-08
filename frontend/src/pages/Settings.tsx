import { motion } from "framer-motion";
import { CalendarOff, Clock, Database, Rocket, Download, KeyRound, LogOut, Monitor, Moon, Palette, Plus, RefreshCw, ShieldCheck, Smartphone, Sun, Trash2 } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { PageHeader } from "../components/AppShell";
import { Button, Field, Input, Pill, Segmented, Skeleton, Switch, stagger } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dateOf, hm, timeOf } from "../lib/format";
import { useApi } from "../lib/hooks";
import { applyTheme, getThemePref, type ThemePref } from "../lib/theme";
import { useToast } from "../lib/toast";
import type { AutomationSettings, Holiday, HolidayList, StockListStatus, User } from "../lib/types";

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
      <PageHeader title="Settings" subtitle={user?.email} />
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
                <div className="banner" style={{ fontSize: 13.5 }}>
                  <Rocket size={17} />
                  <span>Buying happens only on days you confirm with <b>Let's Trade</b> on the Home screen.</span>
                </div>
                <div className="row between">
                  <div><b>Automatic square-off</b><div className="muted" style={{ fontSize: 13 }}>Close positions at {hm(s.exit_time)}</div></div>
                  <Switch checked={s.auto_exit} onChange={(v) => save({ auto_exit: v })} label="Automatic square-off" />
                </div>
                <div className="field">
                  <span className="label-text">Square-off closes</span>
                  <Segmented block tone="bull" value={s.squareoff_scope} onChange={(v) => save({ squareoff_scope: v })} options={[{ value: "all", label: "Everything" }, { value: "intraday", label: "Intraday (MIS) only" }]} />
                  <span className="help">{s.squareoff_scope === "all" ? "In accounts TradeLab bought in that day, every open position is closed — including delivery shares." : "In accounts TradeLab bought in that day, only MIS/intraday positions are closed; delivery stays."}</span>
                </div>
              </div>
            )}
          </motion.div>

          <motion.div className="card pad-lg" {...stagger(1)}>
            <Holidays />
          </motion.div>

          <motion.div className="card pad-lg" {...stagger(2)}>
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
            <StockList />
          </motion.div>

          <motion.div className="card pad-lg" {...stagger(4)}>
            <div className="card-title"><span className="row"><span className="icon-bubble"><KeyRound size={18} /></span> Password</span></div>
            <ChangePassword />
          </motion.div>

          <motion.div {...stagger(5)}>
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

const todayIst = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });

function Holidays() {
  const toast = useToast();
  const { user } = useAuth();
  const canEdit = !!user?.is_owner; // the list is shared by everyone on this server
  const holidays = useApi<HolidayList>("/holidays");
  const [day, setDay] = useState("");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/holidays", { body: { day, description: desc.trim() || "Market holiday" } });
      setDay("");
      setDesc("");
      holidays.reload();
      toast("Holiday added — nothing will trade that day");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (h: Holiday) => {
    try {
      await api(`/holidays/${h.day}`, { method: "DELETE" });
      holidays.reload();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const sync = async () => {
    setSyncing(true);
    try {
      const st = await api<HolidayList["nse"]>("/holidays/sync", { method: "POST" });
      holidays.reload();
      toast(st.last_error ? st.last_error : `NSE calendar updated (${st.count} holidays)`, st.last_error ? "error" : "success");
    } finally {
      setSyncing(false);
    }
  };

  const nse = holidays.data?.nse;
  const sourceLabel = { nse: "NSE", detected: "Detected", manual: "Added by you" } as const;

  return (
    <>
      <div className="card-title">
        <span className="row"><span className="icon-bubble warn"><CalendarOff size={18} /></span> Market holidays</span>
        {canEdit && <Button size="sm" variant="ghost" onClick={sync} loading={syncing}><RefreshCw size={14} /> Check now</Button>}
      </div>
      <div className="banner" style={{ fontSize: 13, marginTop: -4, marginBottom: 12 }}>
        <ShieldCheck size={17} />
        <span>
          Automatic: TradeLab downloads NSE's holiday calendar every morning
          {nse?.last_success ? ` (last updated ${dateOf(nse.last_success)}, ${timeOf(nse.last_success)})` : ""}, and just before every buy it also checks live prices. If the market is closed, nothing is bought.
          {nse?.last_error && <><br /><span style={{ color: "var(--warn)" }}>{nse.last_error}. You can add holidays by hand below.</span></>}
        </span>
      </div>
      {!holidays.data ? (
        <Skeleton h={50} />
      ) : holidays.data.items.length > 0 ? (
        <div className="list" style={{ marginBottom: 12, maxHeight: 280, overflowY: "auto" }}>
          {holidays.data.items.map((h) => (
            <div className="list-item" key={h.day} style={{ padding: "9px 2px" }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <b>{dateOf(h.day)}</b>
                <div className="muted truncate" style={{ fontSize: 13 }}>{h.description}</div>
              </div>
              <Pill>{sourceLabel[h.source] ?? h.source}</Pill>
              {canEdit && <button className="icon-btn danger" onClick={() => remove(h)} aria-label={`Remove ${h.description}`}><Trash2 size={17} /></button>}
            </div>
          ))}
        </div>
      ) : (
        <div className="muted" style={{ fontSize: 13.5, marginBottom: 12 }}>No upcoming holidays known yet.</div>
      )}
      {!canEdit ? (
        <div className="muted" style={{ fontSize: 13 }}>Only the owner of this TradeLab (the first account created) can change this shared list.</div>
      ) : (
      <form className="form-grid two" onSubmit={add}>
        <Field label="Date"><Input type="date" value={day} min={todayIst()} onChange={(e) => setDay(e.target.value)} required /></Field>
        <Field label="Name"><Input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="e.g. Diwali" maxLength={120} /></Field>
        <div style={{ gridColumn: "1 / -1" }}><Button type="submit" variant="ghost" block loading={busy}><Plus size={17} /> Add a holiday by hand</Button></div>
      </form>
      )}
    </>
  );
}

function StockList() {
  const toast = useToast();
  const status = useApi<StockListStatus>("/stocks/status");
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try {
      const s = await api<StockListStatus>("/stocks/refresh", { method: "POST" });
      status.setData(s);
      toast(s.source === "kotak" ? `Loaded ${s.count.toLocaleString("en-IN")} NSE stocks` : s.message ?? "Couldn't load the full list yet", s.source === "kotak" ? "success" : "error");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const s = status.data;
  return (
    <>
      <div className="card-title"><span className="row"><span className="icon-bubble"><Database size={18} /></span> NSE stock list</span></div>
      {!s ? <Skeleton h={40} /> : (
        <div className="stack" style={{ gap: 12 }}>
          <div className="muted" style={{ fontSize: 14 }}>
            {s.source === "kotak"
              ? `${s.count.toLocaleString("en-IN")} stocks from Kotak Neo${s.loaded_on ? `, updated ${dateOf(s.loaded_on)}` : ""}. Refreshes automatically every morning.`
              : s.message ?? `Showing ${s.count} popular stocks until a Kotak Neo account is connected.`}
          </div>
          <Button variant="ghost" onClick={refresh} loading={busy}><RefreshCw size={16} /> Refresh now</Button>
        </div>
      )}
    </>
  );
}
