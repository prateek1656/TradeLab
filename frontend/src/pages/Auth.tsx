import { AnimatePresence, motion } from "framer-motion";
import { ArrowRight, Clock3, Eye, EyeOff, Lock, Mail, ShieldCheck, Timer, User as UserIcon, Zap } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Candles } from "../components/Candles";
import { Brand } from "../components/Logo";
import { Button, Field, Input } from "../components/ui";
import { useAuth } from "../lib/auth";

function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="auth">
      <section className="auth-hero">
        <Brand />
        <motion.h2 className="tagline" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }}>
          Every account. <br />
          <em>One bullish autopilot.</em>
        </motion.h2>
        <motion.p className="sub" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.25 }}>
          Market orders across all your Kotak Neo accounts at 09:15 sharp, hard square-off at 14:30. Private, secure and hands-free.
        </motion.p>
        <div className="feature-list">
          <span><Zap size={16} className="up" /> 09:15 auto entry</span>
          <span><Timer size={16} className="up" /> 14:30 hard exit</span>
          <span><ShieldCheck size={16} className="up" /> Admin-approved access</span>
        </div>
        <Candles className="auth-candles" count={34} seed={11} />
      </section>
      <section className="auth-form">
        <motion.div className="card pad-lg auth-card" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ type: "spring", stiffness: 220, damping: 24 }}>
          {children}
        </motion.div>
      </section>
    </div>
  );
}

function PasswordInput(props: { value: string; onChange: (v: string) => void; autoComplete: string; placeholder?: string }) {
  const [show, setShow] = useState(false);
  return (
    <Input
      icon={<Lock size={18} />}
      type={show ? "text" : "password"}
      value={props.value}
      onChange={(e) => props.onChange(e.target.value)}
      autoComplete={props.autoComplete}
      placeholder={props.placeholder ?? "••••••••"}
      required
      suffix={
        <button type="button" className="icon-btn" onClick={() => setShow(!show)} aria-label={show ? "Hide password" : "Show password"}>
          {show ? <EyeOff size={18} /> : <Eye size={18} />}
        </button>
      }
    />
  );
}

export function LoginPage() {
  const { login } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
      nav("/", { replace: true });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthLayout>
      <h1>Welcome back</h1>
      <p className="muted" style={{ margin: "6px 0 22px" }}>Log in to your private trading desk.</p>
      <form className="stack" onSubmit={submit}>
        <Field label="Email">
          <Input icon={<Mail size={18} />} type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" placeholder="you@example.com" required />
        </Field>
        <Field label="Password">
          <PasswordInput value={password} onChange={setPassword} autoComplete="current-password" />
        </Field>
        <AnimatePresence>
          {error && (
            <motion.div className="banner error" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }}>
              {error}
            </motion.div>
          )}
        </AnimatePresence>
        <Button type="submit" size="lg" block loading={busy}>
          Log in <ArrowRight size={18} />
        </Button>
      </form>
      <p className="muted" style={{ textAlign: "center", margin: "20px 0 0", fontSize: 14 }}>
        New here? <Link to="/signup">Request access</Link>
      </p>
    </AuthLayout>
  );
}

export function SignupPage() {
  const { signup } = useAuth();
  const nav = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const status = await signup(name.trim(), email.trim(), password);
      if (status === "approved") nav("/", { replace: true });
      else setPending(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (pending) {
    return (
      <AuthLayout>
        <div className="empty" style={{ padding: "10px 0" }}>
          <motion.div className="icon-bubble warn" style={{ width: 64, height: 64, borderRadius: 20 }} initial={{ scale: 0.6, rotate: -20 }} animate={{ scale: 1, rotate: 0 }}>
            <Clock3 size={30} />
          </motion.div>
          <h3 style={{ fontSize: 21 }}>Request sent</h3>
          <p>
            TradeLab is private. An admin has to approve <b>{email}</b> before you can log in. You'll be able to sign in as soon as they do.
          </p>
          <Link to="/login" className="btn btn-ghost" style={{ marginTop: 8 }}>Back to log in</Link>
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout>
      <h1>Request access</h1>
      <p className="muted" style={{ margin: "6px 0 22px" }}>An admin approves every new member.</p>
      <form className="stack" onSubmit={submit}>
        <Field label="Full name">
          <Input icon={<UserIcon size={18} />} value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" placeholder="Your name" required minLength={2} />
        </Field>
        <Field label="Email">
          <Input icon={<Mail size={18} />} type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" placeholder="you@example.com" required />
        </Field>
        <Field label="Password" help="At least 8 characters.">
          <PasswordInput value={password} onChange={setPassword} autoComplete="new-password" />
        </Field>
        {error && <div className="banner error">{error}</div>}
        <Button type="submit" size="lg" block loading={busy}>
          Create account <ArrowRight size={18} />
        </Button>
      </form>
      <p className="muted" style={{ textAlign: "center", margin: "20px 0 0", fontSize: 14 }}>
        Already approved? <Link to="/login">Log in</Link>
      </p>
    </AuthLayout>
  );
}
