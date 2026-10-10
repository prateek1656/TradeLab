import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle, BadgeCheck, ChevronDown, CircleHelp, Coins, Flame, KeyRound, ListOrdered, MoreVertical,
  Pencil, Plus, QrCode, RefreshCw, ShieldCheck, Smartphone, Trash2, Wallet,
} from "lucide-react";
import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { PageHeader } from "../components/AppShell";
import { Confirm, Sheet } from "../components/Modal";
import { Button, Empty, Field, Input, Pill, Segmented, Skeleton, Switch, stagger } from "../components/ui";
import { api } from "../lib/api";
import { initials, money, pnlClass, relative, signedMoney } from "../lib/format";
import { useApi } from "../lib/hooks";
import { useToast } from "../lib/toast";
import type { Account, LiveSnapshot, Order, Position } from "../lib/types";

type Dialog =
  | { kind: "add" }
  | { kind: "rename"; acc: Account }
  | { kind: "creds"; acc: Account }
  | { kind: "funds"; acc: Account }
  | { kind: "detail"; acc: Account }
  | { kind: "delete"; acc: Account }
  | { kind: "squareoff"; acc: Account };

export function AccountsPage() {
  const accounts = useApi<Account[]>("/accounts");
  const live = useApi<LiveSnapshot[]>("/accounts/live", { interval: 30000 });
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const close = () => setDialog(null);
  const refresh = () => {
    accounts.reload();
    live.reload();
  };

  return (
    <>
      <PageHeader
        title="Accounts"
        subtitle={accounts.data ? `${accounts.data.length} account${accounts.data.length === 1 ? "" : "s"} · Kotak Neo logins renew automatically every trading day` : "Your demat accounts"}
        action={
          <Button onClick={() => setDialog({ kind: "add" })}>
            <Plus size={18} /> <span className="hide-sm">Add account</span>
          </Button>
        }
      />

      {!accounts.data ? (
        <div className="grid-auto">{[0, 1, 2].map((i) => <div key={i} className="card"><Skeleton h={140} /></div>)}</div>
      ) : accounts.data.length === 0 ? (
        <div className="card">
          <Empty
            icon={<Wallet size={26} />}
            title="No accounts yet"
            action={<Button onClick={() => setDialog({ kind: "add" })}><Plus size={18} /> Add your first account</Button>}
          >
            Connect each Kotak Neo demat account once. TradeLab securely stores the login and signs in by itself every morning.
          </Empty>
        </div>
      ) : (
        <div className="grid-auto">
          {accounts.data.map((a, i) => (
            <AccountCard key={a.id} acc={a} live={live.data?.find((x) => x.id === a.id)} index={i} onAction={setDialog} onChanged={refresh} />
          ))}
        </div>
      )}

      <AddAccountSheet open={dialog?.kind === "add"} onClose={close} onDone={refresh} />
      {dialog?.kind === "rename" && <RenameSheet acc={dialog.acc} onClose={close} onDone={refresh} />}
      {dialog?.kind === "creds" && <CredentialsSheet acc={dialog.acc} onClose={close} onDone={refresh} />}
      {dialog?.kind === "funds" && <PaperFundsSheet acc={dialog.acc} onClose={close} onDone={refresh} />}
      {dialog?.kind === "detail" && <DetailSheet acc={dialog.acc} onClose={close} />}
      {dialog?.kind === "delete" && <DeleteDialog acc={dialog.acc} onClose={close} onDone={refresh} />}
      {dialog?.kind === "squareoff" && <SquareOffDialog acc={dialog.acc} onClose={close} onDone={refresh} />}
    </>
  );
}

function AccountCard({ acc, live, index, onAction, onChanged }: { acc: Account; live?: LiveSnapshot; index: number; onAction: (d: Dialog) => void; onChanged: () => void }) {
  const toast = useToast();
  const [menu, setMenu] = useState(false);
  const [auto, setAuto] = useState(acc.automation_enabled);
  const [reconnecting, setReconnecting] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => setAuto(acc.automation_enabled), [acc.automation_enabled]);
  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => !menuRef.current?.contains(e.target as Node) && setMenu(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [menu]);

  const toggleAuto = async (v: boolean) => {
    setAuto(v);
    try {
      await api(`/accounts/${acc.id}`, { method: "PATCH", body: { automation_enabled: v } });
      toast(v ? `${acc.name} is on autopilot` : `${acc.name} removed from autopilot`);
      onChanged();
    } catch (e) {
      setAuto(!v);
      toast((e as Error).message, "error");
    }
  };

  const reconnect = async () => {
    setReconnecting(true);
    try {
      await api(`/accounts/${acc.id}/reconnect`, { method: "POST" });
      toast("Logged in to Kotak Neo");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setReconnecting(false);
      onChanged();
    }
  };

  const hasError = acc.status === "error" || (live && !live.ok);
  const act = (d: Dialog) => {
    setMenu(false);
    onAction(d);
  };

  return (
    <motion.div className="card" {...stagger(index)} style={{ zIndex: menu ? 5 : undefined }}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className={`avatar ${acc.broker === "paper" ? "paper" : ""} ${hasError ? "err" : ""}`}>{initials(acc.name)}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ gap: 8 }}>
            <b className="truncate" style={{ fontSize: 16.5 }}>{acc.name}</b>
          </div>
          <div className="muted truncate" style={{ fontSize: 13 }}>
            {acc.broker === "paper" ? "Paper · simulated fills" : `${acc.client_name ?? "Kotak Neo"} · ${acc.ucc_masked ?? ""}`}
          </div>
        </div>
        <div style={{ position: "relative" }} ref={menuRef}>
          <button className="icon-btn" onClick={() => setMenu(!menu)} aria-label="Account actions"><MoreVertical size={19} /></button>
          <AnimatePresence>
            {menu && (
              <motion.div className="menu" initial={{ opacity: 0, y: -6, scale: 0.97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -6, scale: 0.97 }} transition={{ duration: 0.14 }}>
                <button onClick={() => act({ kind: "rename", acc })}><Pencil size={16} /> Rename</button>
                <button onClick={() => act({ kind: "detail", acc })}><ListOrdered size={16} /> Positions & orders</button>
                {acc.broker === "kotak" ? (
                  <button onClick={() => act({ kind: "creds", acc })}><KeyRound size={16} /> Update login details</button>
                ) : (
                  <button onClick={() => act({ kind: "funds", acc })}><Coins size={16} /> Set paper funds</button>
                )}
                <button onClick={() => act({ kind: "squareoff", acc })}><Flame size={16} /> Square off now</button>
                <button className="danger" onClick={() => act({ kind: "delete", acc })}><Trash2 size={16} /> Delete account</button>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>

      <div className="grid-2" style={{ margin: "16px 0 14px", gap: 10 }}>
        <div className="card" style={{ padding: 12, borderRadius: 16, boxShadow: "none", background: "var(--surface-2)" }}>
          <div className="muted" style={{ fontSize: 12, fontWeight: 700 }}>AVAILABLE</div>
          <div className="num" style={{ fontWeight: 800, fontSize: 18 }}>{live ? (live.ok ? money(live.available) : "—") : <Skeleton h={20} w="80%" />}</div>
        </div>
        <div className="card" style={{ padding: 12, borderRadius: 16, boxShadow: "none", background: "var(--surface-2)" }}>
          <div className="muted" style={{ fontSize: 12, fontWeight: 700 }}>TODAY P&L</div>
          <div className={`num ${pnlClass(live?.pnl)}`} style={{ fontWeight: 800, fontSize: 18 }}>
            {live ? (live.ok ? signedMoney(live.pnl) : "—") : <Skeleton h={20} w="70%" />}
          </div>
        </div>
      </div>

      {hasError && (
        <div className="banner error" style={{ marginBottom: 12, fontSize: 13 }}>
          <AlertTriangle size={17} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 700 }}>Login problem</div>
            <div style={{ wordBreak: "break-word" }}>{acc.last_error ?? live?.error ?? "Could not reach Kotak Neo"}</div>
            <div className="row" style={{ marginTop: 8 }}>
              <Button size="sm" variant="ghost" onClick={reconnect} loading={reconnecting}><RefreshCw size={14} /> Retry</Button>
              {acc.broker === "kotak" && <Button size="sm" variant="ghost" onClick={() => onAction({ kind: "creds", acc })}><KeyRound size={14} /> Update login</Button>}
            </div>
          </div>
        </div>
      )}

      <div className="row between">
        <div className="row" style={{ gap: 8 }}>
          {hasError ? <Pill tone="bear" dot>Needs attention</Pill> : <Pill tone="bull" dot>Connected</Pill>}
          {live?.ok && (live.open_positions ?? 0) > 0 && <Pill tone="warn">{live.open_positions} open</Pill>}
        </div>
        <label className="row" style={{ gap: 8, fontSize: 13, fontWeight: 700, color: "var(--text-2)" }}>
          Autopilot
          <Switch checked={auto} onChange={toggleAuto} label="Include in autopilot" />
        </label>
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
        {acc.broker === "kotak" ? `Last login ${relative(acc.last_login_at)}` : "Practice account"}
      </div>
    </motion.div>
  );
}

/* ───────────── add account ───────────── */

const EMPTY_CREDS = { consumer_key: "", mobile: "", ucc: "", mpin: "", totp_secret: "" };

async function decodeQr(file: File): Promise<string | null> {
  const { default: jsQR } = await import("jsqr");
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, 1400 / Math.max(img.width, img.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      const ctx = canvas.getContext("2d");
      if (!ctx) return resolve(null);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const code = jsQR(data.data, data.width, data.height, { inversionAttempts: "attemptBoth" });
      URL.revokeObjectURL(img.src);
      resolve(code?.data ?? null);
    };
    img.onerror = () => resolve(null);
    img.src = URL.createObjectURL(file);
  });
}

function CredentialFields({ value, onChange, optional }: { value: typeof EMPTY_CREDS; onChange: (v: typeof EMPTY_CREDS) => void; optional?: boolean }) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [qrOk, setQrOk] = useState(false);
  const set = (k: keyof typeof EMPTY_CREDS) => (e: ChangeEvent<HTMLInputElement>) => onChange({ ...value, [k]: e.target.value });
  const ph = (p: string) => (optional ? "Leave blank to keep current" : p);

  const onQr = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const text = await decodeQr(file);
    if (text && /secret=/i.test(text)) {
      onChange({ ...value, totp_secret: text });
      setQrOk(true);
      toast("TOTP secret read from QR code");
    } else {
      toast("Couldn't find a TOTP QR code in that image", "error");
    }
  };

  return (
    <div className="form-grid two">
      <Field label="Consumer key (API token)" help="Neo app → More → Trade API → your application">
        <Input icon={<KeyRound size={17} />} value={value.consumer_key} onChange={set("consumer_key")} placeholder={ph("Paste token")} autoComplete="off" required={!optional} />
      </Field>
      <Field label="Registered mobile">
        <Input icon={<Smartphone size={17} />} value={value.mobile} onChange={set("mobile")} placeholder={ph("98765 43210")} inputMode="tel" autoComplete="off" required={!optional} />
      </Field>
      <Field label="UCC (client code)" help="Neo app → Profile">
        <Input icon={<BadgeCheck size={17} />} value={value.ucc} onChange={set("ucc")} placeholder={ph("AB1C2")} autoComplete="off" style={{ textTransform: "uppercase" }} required={!optional} />
      </Field>
      <Field label="MPIN">
        <Input icon={<ShieldCheck size={17} />} type="password" value={value.mpin} onChange={set("mpin")} placeholder={ph("••••••")} inputMode="numeric" autoComplete="off" required={!optional} />
      </Field>
      <div style={{ gridColumn: "1 / -1" }}>
        <Field label="TOTP secret" help={qrOk ? "✓ Secret read from QR — never shown again." : "The key behind your Kotak authenticator QR. Paste it, or upload a screenshot of the QR."}>
          <Input
            icon={<QrCode size={17} />}
            value={qrOk ? "•••• read from QR code ••••" : value.totp_secret}
            onChange={(e) => {
              setQrOk(false);
              set("totp_secret")(e);
            }}
            placeholder={ph("e.g. JBSWY3DPEHPK3PXP")}
            autoComplete="off"
            required={!optional}
            suffix={
              <button type="button" className="btn btn-ghost sm" style={{ height: 36 }} onClick={() => fileRef.current?.click()}>
                <QrCode size={15} /> Scan
              </button>
            }
            style={{ paddingRight: 96 }}
          />
          <input ref={fileRef} type="file" accept="image/*" hidden onChange={onQr} />
        </Field>
      </div>
    </div>
  );
}

function HelpBox() {
  const [open, setOpen] = useState(false);
  return (
    <div className="banner" style={{ flexDirection: "column", gap: 8 }}>
      <button type="button" onClick={() => setOpen(!open)} className="row between" style={{ width: "100%", background: "none", border: 0, padding: 0, cursor: "pointer", fontWeight: 700 }}>
        <span className="row" style={{ gap: 8 }}><CircleHelp size={17} className="up" /> Where do I find these?</span>
        <ChevronDown size={18} style={{ transform: open ? "rotate(180deg)" : undefined, transition: "transform .2s" }} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.ol initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} style={{ margin: 0, paddingLeft: 18, overflow: "hidden", color: "var(--text-2)", fontSize: 13.5 }}>
            <li>In the Kotak Neo app open <b>More → Trade API</b> and create an application. Copy its token — that's the <b>consumer key</b>.</li>
            <li>On the same Trade API screen, register <b>TOTP</b>. When the QR code appears, take a screenshot (or copy the key shown under it) <i>before</i> scanning it into Google Authenticator.</li>
            <li>Your <b>UCC</b> is in Profile. The <b>MPIN</b> is the PIN you use to log in to the Neo app.</li>
            <li>If your Kotak API dashboard asks for a static IP, add the public IP of the server that runs TradeLab.</li>
          </motion.ol>
        )}
      </AnimatePresence>
    </div>
  );
}

function AddAccountSheet({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [type, setType] = useState<"kotak" | "paper">("kotak");
  const [name, setName] = useState("");
  const [creds, setCreds] = useState(EMPTY_CREDS);
  const [paperFunds, setPaperFunds] = useState("100000");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setCreds(EMPTY_CREDS);
      setError(null);
      setType("kotak");
    }
  }, [open]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body = type === "kotak" ? { name, broker: "kotak", ...creds } : { name, broker: "paper", paper_funds: Number(paperFunds) || 0 };
      const acc = await api<Account>("/accounts", { body });
      toast(type === "kotak" ? `${acc.name} connected${acc.client_name ? ` — hi ${acc.client_name}!` : ""}` : `${acc.name} created`);
      onDone();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet open={open} onClose={onClose} title="Add account" subtitle="Log in once — TradeLab signs in by itself every trading day after this." wide>
      <form className="stack" onSubmit={submit}>
        <Segmented
          block
          tone="bull"
          value={type}
          onChange={setType}
          options={[
            { value: "kotak", label: "Kotak Neo (live)" },
            { value: "paper", label: "Paper (practice)" },
          ]}
        />
        <Field label="Account nickname">
          <Input icon={<Wallet size={17} />} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Rahul — Main" maxLength={80} required />
        </Field>
        {type === "kotak" ? (
          <>
            <CredentialFields value={creds} onChange={setCreds} />
            <HelpBox />
            <div className="banner bull" style={{ fontSize: 13 }}>
              <ShieldCheck size={17} />
              <span>Your MPIN and TOTP secret are encrypted on your server and never shown again. They're only used to log this account in each morning.</span>
            </div>
          </>
        ) : (
          <Field label="Starting paper funds (₹)" help="Simulated money for dry runs. Fills use live prices when a Kotak account is connected.">
            <Input icon={<Coins size={17} />} type="number" min={0} value={paperFunds} onChange={(e) => setPaperFunds(e.target.value)} />
          </Field>
        )}
        {error && <div className="banner error"><AlertTriangle size={17} /> {error}</div>}
        <Button type="submit" size="lg" block loading={busy}>
          {busy ? (type === "kotak" ? "Logging in to Kotak Neo…" : "Creating…") : type === "kotak" ? "Connect & log in" : "Create paper account"}
        </Button>
      </form>
    </Sheet>
  );
}

/* ───────────── manage ───────────── */

function RenameSheet({ acc, onClose, onDone }: { acc: Account; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [name, setName] = useState(acc.name);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api(`/accounts/${acc.id}`, { method: "PATCH", body: { name } });
      toast("Renamed");
      onDone();
      onClose();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet open onClose={onClose} title="Rename account">
      <form className="stack" onSubmit={submit}>
        <Input icon={<Pencil size={17} />} value={name} onChange={(e) => setName(e.target.value)} maxLength={80} autoFocus required />
        <Button type="submit" block loading={busy}>Save name</Button>
      </form>
    </Sheet>
  );
}

function CredentialsSheet({ acc, onClose, onDone }: { acc: Account; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [creds, setCreds] = useState(EMPTY_CREDS);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body = Object.fromEntries(Object.entries(creds).filter(([, v]) => v.trim()));
      await api(`/accounts/${acc.id}/credentials`, { method: "PUT", body });
      toast("Login details updated");
      onDone();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet open onClose={onClose} title="Update login details" subtitle={`Only fill what changed for ${acc.name}. We'll test the login before saving.`} wide>
      <form className="stack" onSubmit={submit}>
        <CredentialFields value={creds} onChange={setCreds} optional />
        {error && <div className="banner error"><AlertTriangle size={17} /> {error}</div>}
        <Button type="submit" block size="lg" loading={busy}>Test & save</Button>
      </form>
    </Sheet>
  );
}

function PaperFundsSheet({ acc, onClose, onDone }: { acc: Account; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [funds, setFunds] = useState("0");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api(`/accounts/${acc.id}`, { method: "PATCH", body: { paper_funds: Number(funds) || 0 } });
      toast("Paper funds updated");
      onDone();
      onClose();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet open onClose={onClose} title="Set paper funds">
      <form className="stack" onSubmit={submit}>
        <Input icon={<Coins size={17} />} type="number" min={0} value={funds} onChange={(e) => setFunds(e.target.value)} autoFocus />
        <Button type="submit" block loading={busy}>Save</Button>
      </form>
    </Sheet>
  );
}

function DetailSheet({ acc, onClose }: { acc: Account; onClose: () => void }) {
  const [tab, setTab] = useState<"positions" | "orders">("positions");
  const positions = useApi<Position[]>(`/accounts/${acc.id}/positions`, { interval: 20000 });
  const orders = useApi<Order[]>(tab === "orders" ? `/accounts/${acc.id}/orders` : null);
  return (
    <Sheet open onClose={onClose} title={acc.name} subtitle="Today's positions and orders" wide>
      <div className="stack">
        <Segmented value={tab} onChange={setTab} options={[{ value: "positions", label: "Positions" }, { value: "orders", label: "Orders" }]} />
        {tab === "positions" ? (
          positions.error ? <div className="banner error">{positions.error}</div> : !positions.data ? <Skeleton h={120} /> : positions.data.length === 0 ? (
            <Empty icon={<ListOrdered size={24} />} title="No positions today" />
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table className="table num">
                <thead><tr><th>Symbol</th><th className="r">Net</th><th className="r hide-sm">Avg</th><th className="r">LTP</th><th className="r">P&L</th></tr></thead>
                <tbody>
                  {positions.data.map((p) => (
                    <tr key={p.trading_symbol + p.product}>
                      <td><b>{p.trading_symbol}</b><div className="muted" style={{ fontSize: 12 }}>{p.product}</div></td>
                      <td className={`r ${p.net_qty > 0 ? "up" : p.net_qty < 0 ? "down" : "muted"}`}>{p.net_qty}</td>
                      <td className="r hide-sm">{money(p.net_qty >= 0 ? p.buy_avg : p.sell_avg)}</td>
                      <td className="r">{money(p.ltp)}</td>
                      <td className={`r ${pnlClass(p.pnl)}`} style={{ fontWeight: 700 }}>{signedMoney(p.pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        ) : orders.error ? <div className="banner error">{orders.error}</div> : !orders.data ? <Skeleton h={120} /> : orders.data.length === 0 ? (
          <Empty icon={<ListOrdered size={24} />} title="No orders today" />
        ) : (
          <div className="list">
            {orders.data.map((o) => (
              <div className="list-item" key={o.order_id}>
                <Pill tone={o.side === "B" ? "bull" : "bear"}>{o.side === "B" ? "BUY" : "SELL"}</Pill>
                <div className="truncate" style={{ flex: 1 }}>
                  <b>{o.trading_symbol}</b> <span className="muted num">× {o.quantity}</span>
                  <div className="muted truncate" style={{ fontSize: 12.5 }}>{o.message ?? `${o.order_type} · ${o.product} · ${o.placed_at ?? ""}`}</div>
                </div>
                <Pill tone={o.status.toLowerCase().startsWith("complete") ? "bull" : o.status.toLowerCase().startsWith("reject") ? "bear" : o.is_open ? "warn" : undefined}>{o.status}</Pill>
              </div>
            ))}
          </div>
        )}
      </div>
    </Sheet>
  );
}

function DeleteDialog({ acc, onClose, onDone }: { acc: Account; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await api(`/accounts/${acc.id}`, { method: "DELETE" });
      toast(`${acc.name} deleted`);
      onDone();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Confirm
      open
      onClose={onClose}
      onConfirm={run}
      loading={busy}
      error={error}
      tone="danger"
      title={`Delete ${acc.name}?`}
      confirmLabel={busy ? "Checking balance…" : "Check balance & delete"}
      body={<>For safety, an account can only be deleted when its balance is exactly <b>₹0</b> and it has no open positions. We'll check with {acc.broker === "kotak" ? "Kotak Neo" : "the paper ledger"} first.</>}
    />
  );
}

function SquareOffDialog({ acc, onClose, onDone }: { acc: Account; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      await api(`/accounts/${acc.id}/square-off`, { method: "POST" });
      toast(`Squaring off ${acc.name}…`);
      setTimeout(onDone, 2500);
      onClose();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Confirm open onClose={onClose} onConfirm={run} loading={busy} tone="warn" title={`Square off ${acc.name}?`} confirmLabel="Square off now"
      body="In this account: cancels pending intraday orders, closes every intraday (MIS) position at market price, and closes the carry-forward F&O and Commodity positions TradeLab opened (selling what it bought, buying back what it sold first). Delivery holdings and the account holder's own carry-forward positions are not touched." />
  );
}
