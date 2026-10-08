import { AnimatePresence, motion } from "framer-motion";
import { CalendarOff, Check, Clock3, Cpu, Crown, Plus, RefreshCw, ShieldCheck, ShieldOff, Trash2, UserCheck, Users, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { PageHeader } from "../components/AppShell";
import { Confirm } from "../components/Modal";
import { Button, Empty, Field, Input, Pill, Segmented, Skeleton, stagger } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dateOf, initials, relative, timeOf } from "../lib/format";
import { useApi } from "../lib/hooks";
import { useToast } from "../lib/toast";
import type { AdminUser, Holiday, SystemInfo } from "../lib/types";

export function AdminPage() {
  const [tab, setTab] = useState<"users" | "holidays" | "system">("users");
  return (
    <>
      <PageHeader title="Admin" subtitle="Decide who can use TradeLab" />
      <div style={{ marginBottom: 16 }}>
        <Segmented tone="bull" value={tab} onChange={setTab} options={[
          { value: "users", label: <span className="row" style={{ gap: 6 }}><Users size={15} /> Users</span> },
          { value: "holidays", label: <span className="row" style={{ gap: 6 }}><CalendarOff size={15} /> Holidays</span> },
          { value: "system", label: <span className="row" style={{ gap: 6 }}><Cpu size={15} /> System</span> },
        ]} />
      </div>
      {tab === "users" ? <UsersTab /> : tab === "holidays" ? <HolidaysTab /> : <SystemTab />}
    </>
  );
}

function UsersTab() {
  const { user: me } = useAuth();
  const toast = useToast();
  const users = useApi<AdminUser[]>("/admin/users", { interval: 20000 });
  const [busyId, setBusyId] = useState<number | null>(null);
  const [toDelete, setToDelete] = useState<AdminUser | null>(null);
  const [deleteErr, setDeleteErr] = useState<string | null>(null);

  const update = async (u: AdminUser, body: Record<string, string>, msg: string) => {
    setBusyId(u.id);
    try {
      await api(`/admin/users/${u.id}`, { method: "PATCH", body });
      toast(msg);
      users.reload();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusyId(null);
    }
  };

  const remove = async () => {
    if (!toDelete) return;
    setDeleteErr(null);
    try {
      await api(`/admin/users/${toDelete.id}`, { method: "DELETE" });
      toast(`${toDelete.name} deleted`);
      setToDelete(null);
      users.reload();
    } catch (e) {
      setDeleteErr((e as Error).message);
    }
  };

  if (!users.data) return <div className="stack"><Skeleton h={80} r={20} /><Skeleton h={80} r={20} /></div>;
  const pending = users.data.filter((u) => u.status === "pending");
  const others = users.data.filter((u) => u.status !== "pending");

  return (
    <div className="stack">
      <motion.div className="card" {...stagger(0)}>
        <div className="card-title">
          <span className="row"><span className="icon-bubble warn"><Clock3 size={18} /></span> Waiting for approval</span>
          {pending.length > 0 && <Pill tone="warn">{pending.length}</Pill>}
        </div>
        {pending.length === 0 ? (
          <div className="muted" style={{ fontSize: 14 }}>No pending requests. New sign-ups appear here and can't log in until you approve them.</div>
        ) : (
          <div className="list">
            <AnimatePresence>
              {pending.map((u) => (
                <motion.div key={u.id} className="list-item" layout exit={{ opacity: 0, x: 30 }}>
                  <div className="avatar" style={{ background: "linear-gradient(135deg,#ffd98a,#d99400)" }}>{initials(u.name)}</div>
                  <div className="truncate" style={{ flex: 1 }}>
                    <b>{u.name}</b>
                    <div className="muted truncate" style={{ fontSize: 13 }}>{u.email} · asked {relative(u.created_at)}</div>
                  </div>
                  <Button size="sm" onClick={() => update(u, { status: "approved" }, `${u.name} can now log in`)} loading={busyId === u.id}><Check size={16} /> <span className="hide-sm">Approve</span></Button>
                  <button className="icon-btn danger" onClick={() => update(u, { status: "revoked" }, `${u.name}'s request declined`)} aria-label="Decline"><X size={18} /></button>
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        )}
      </motion.div>

      <motion.div className="card" {...stagger(1)}>
        <div className="card-title"><span className="row"><span className="icon-bubble"><UserCheck size={18} /></span> Members</span></div>
        <div className="list">
          {others.map((u) => {
            const self = u.id === me?.id;
            return (
              <div key={u.id} className="list-item" style={{ flexWrap: "wrap" }}>
                <div className={`avatar ${u.status === "revoked" ? "err" : ""}`}>{initials(u.name)}</div>
                <div className="truncate" style={{ flex: 1, minWidth: 160 }}>
                  <div className="row" style={{ gap: 8 }}>
                    <b className="truncate">{u.name}{self && " (you)"}</b>
                    {u.role === "admin" && <Pill tone="bull"><Crown size={12} /> Admin</Pill>}
                    {u.status === "revoked" && <Pill tone="bear">Revoked</Pill>}
                  </div>
                  <div className="muted truncate" style={{ fontSize: 13 }}>{u.email} · {u.account_count} account{u.account_count === 1 ? "" : "s"} · last login {relative(u.last_login_at)}</div>
                </div>
                {!self && (
                  <div className="row" style={{ gap: 6 }}>
                    {u.status === "approved" ? (
                      <>
                        <Button size="sm" variant="ghost" onClick={() => update(u, { role: u.role === "admin" ? "member" : "admin" }, u.role === "admin" ? `${u.name} is now a member` : `${u.name} is now an admin`)}>
                          <Crown size={15} /> <span className="hide-sm">{u.role === "admin" ? "Remove admin" : "Make admin"}</span>
                        </Button>
                        <Button size="sm" variant="danger" onClick={() => update(u, { status: "revoked" }, `${u.name}'s access revoked`)}><ShieldOff size={15} /> <span className="hide-sm">Revoke</span></Button>
                      </>
                    ) : (
                      <>
                        <Button size="sm" variant="ghost" onClick={() => update(u, { status: "approved" }, `${u.name}'s access restored`)}><ShieldCheck size={15} /> <span className="hide-sm">Restore</span></Button>
                        <button className="icon-btn danger" onClick={() => { setDeleteErr(null); setToDelete(u); }} aria-label="Delete user"><Trash2 size={17} /></button>
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </motion.div>

      <Confirm open={!!toDelete} onClose={() => setToDelete(null)} onConfirm={remove} tone="danger" title={`Delete ${toDelete?.name}?`} confirmLabel="Delete user" error={deleteErr}
        body="Removes this person completely. Users who still have trading accounts can only be revoked, not deleted." />
    </div>
  );
}

function HolidaysTab() {
  const toast = useToast();
  const holidays = useApi<Holiday[]>("/admin/holidays");
  const [day, setDay] = useState("");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/admin/holidays", { body: { day, description: desc || "Market holiday" } });
      setDay("");
      setDesc("");
      holidays.reload();
      toast("Holiday added — no trades will run that day");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (h: Holiday) => {
    await api(`/admin/holidays/${h.day}`, { method: "DELETE" });
    holidays.reload();
  };

  return (
    <div className="grid grid-main">
      <motion.div className="card pad-lg" {...stagger(0)}>
        <div className="card-title">Exchange holidays</div>
        <p className="muted" style={{ marginTop: -6, fontSize: 14 }}>Weekends are skipped automatically. Add NSE trading holidays so the 09:15 and 14:30 jobs stay quiet.</p>
        {!holidays.data ? <Skeleton h={80} /> : holidays.data.length === 0 ? (
          <Empty icon={<CalendarOff size={24} />} title="No holidays added" />
        ) : (
          <div className="list">
            {holidays.data.map((h) => (
              <div className="list-item" key={h.day}>
                <span className="icon-bubble warn"><CalendarOff size={17} /></span>
                <div style={{ flex: 1 }}><b>{dateOf(h.day)}</b><div className="muted" style={{ fontSize: 13 }}>{h.description}</div></div>
                <button className="icon-btn danger" onClick={() => remove(h)} aria-label="Remove holiday"><Trash2 size={17} /></button>
              </div>
            ))}
          </div>
        )}
      </motion.div>
      <motion.form className="card pad-lg stack" onSubmit={add} {...stagger(1)}>
        <div className="card-title" style={{ margin: 0 }}>Add a holiday</div>
        <Field label="Date"><Input type="date" value={day} onChange={(e) => setDay(e.target.value)} required /></Field>
        <Field label="Name"><Input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="e.g. Diwali Laxmi Pujan" maxLength={120} /></Field>
        <Button type="submit" loading={busy}><Plus size={17} /> Add holiday</Button>
      </motion.form>
    </div>
  );
}

function SystemTab() {
  const toast = useToast();
  const sys = useApi<SystemInfo>("/admin/system", { interval: 15000 });
  const [refreshing, setRefreshing] = useState(false);
  const refresh = async () => {
    setRefreshing(true);
    try {
      await api("/admin/stocks/refresh", { method: "POST" });
      sys.reload();
      toast("Stock list refreshed");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setRefreshing(false);
    }
  };
  if (!sys.data) return <Skeleton h={200} r={20} />;
  const s = sys.data;
  const label = (id: string) => (id === "warmup" ? "Morning login warm-up" : id.startsWith("entry:") ? `Entry prep · user #${id.slice(6)}` : `Square-off · user #${id.slice(5)}`);
  return (
    <div className="grid grid-main">
      <motion.div className="card pad-lg" {...stagger(0)}>
        <div className="card-title">Scheduled jobs <span className="muted">server time {timeOf(s.server_time)} IST</span></div>
        {s.jobs.length === 0 ? <div className="banner warn">Scheduler is not running.</div> : (
          <table className="table">
            <tbody>
              {s.jobs.map((j) => (
                <tr key={j.id}><td>{label(j.id)}</td><td className="r muted num">{j.next_run ? `${dateOf(j.next_run)}, ${timeOf(j.next_run)}` : "—"}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </motion.div>
      <motion.div className="card pad-lg stack" {...stagger(1)}>
        <div className="card-title" style={{ margin: 0 }}>Overview</div>
        <div className="row between"><span className="muted">Users</span><b>{s.users}</b></div>
        <div className="row between"><span className="muted">Trading accounts</span><b>{s.accounts}</b></div>
        <div className="row between"><span className="muted">Stock list</span><b>{s.stocks.source === "kotak" ? `${s.stocks.count.toLocaleString("en-IN")} NSE stocks` : "Offline list"}</b></div>
        {s.stocks.loaded_on && <div className="row between"><span className="muted">Updated</span><b>{dateOf(s.stocks.loaded_on)}</b></div>}
        {s.stocks.message && <div className="banner warn" style={{ fontSize: 13 }}>{s.stocks.message}</div>}
        <Button variant="ghost" onClick={refresh} loading={refreshing}><RefreshCw size={16} /> Refresh stock list</Button>
      </motion.div>
    </div>
  );
}
