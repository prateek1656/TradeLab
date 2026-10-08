const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });
const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });

export const money = (v: number | null | undefined, decimals = true) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : (decimals ? inr : inr0).format(v);

export const signedMoney = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${inr.format(Math.abs(v))}`;

export const pnlClass = (v: number | null | undefined) => (!v ? "" : v > 0 ? "up" : "down");

export const hm = (hms: string) => hms.slice(0, 5);

export function timeOf(iso: string | null | undefined) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: "Asia/Kolkata" });
}

export function dateOf(iso: string) {
  const d = new Date(iso.length === 10 ? iso + "T00:00:00+05:30" : iso);
  return d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", timeZone: "Asia/Kolkata" });
}

export function relative(iso: string | null | undefined) {
  if (!iso) return "never";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return `${Math.floor(diff / 86400)} d ago`;
}

export function countdown(ms: number) {
  if (ms <= 0) return "00:00:00";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return d > 0 ? `${d}d ${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(h)}:${pad(m)}:${pad(sec)}`;
}

export const initials = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]!.toUpperCase())
    .join("") || "?";

export const sizingLabel = (mode: string, value: number) =>
  mode === "qty" ? `${value} share${value === 1 ? "" : "s"}` : mode === "amount" ? `${money(value, false)} each` : `${value}% of funds`;
