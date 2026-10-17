import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

type BtnVariant = "primary" | "ghost" | "danger" | "warn";

export function Button({
  variant = "primary",
  size,
  block,
  loading,
  children,
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant; size?: "sm" | "lg"; block?: boolean; loading?: boolean }) {
  return (
    <button
      className={`btn btn-${variant} ${size ?? ""} ${block ? "block" : ""} ${className}`}
      disabled={loading || rest.disabled}
      {...rest}
    >
      {loading ? <span className="spinner" /> : null}
      {children}
    </button>
  );
}

export function Switch({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={`switch ${checked ? "on" : ""}`}
      onClick={() => onChange(!checked)}
    >
      <span className="knob" style={{ left: checked ? 23 : 3, transition: "left .18s ease" }} />
    </button>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  block,
  tone,
}: {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (v: T) => void;
  block?: boolean;
  tone?: "bull";
}) {
  return (
    <div className={`segmented ${block ? "block" : ""} ${tone ?? ""}`} role="tablist">
      {options.map((o) => (
        <button key={o.value} type="button" role="tab" aria-selected={value === o.value} className={value === o.value ? "active" : ""} onClick={() => onChange(o.value)}>
          {value === o.value && <span className="seg-bg" />}
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Field({ label, help, error, children }: { label?: ReactNode; help?: ReactNode; error?: string | null; children: ReactNode }) {
  return (
    <div className="field">
      {label && <label>{label}</label>}
      {children}
      {error ? <span className="error">{error}</span> : help ? <span className="help">{help}</span> : null}
    </div>
  );
}

export function Input({ icon, suffix, className = "", ...rest }: InputHTMLAttributes<HTMLInputElement> & { icon?: ReactNode; suffix?: ReactNode }) {
  return (
    <div className="input-wrap">
      {icon}
      <input className={`input ${icon ? "with-icon" : ""} ${suffix ? "with-suffix" : ""} ${className}`} {...rest} />
      {suffix && <span className="suffix">{suffix}</span>}
    </div>
  );
}

export function Pill({ tone, dot, pulse, children }: { tone?: "bull" | "bear" | "warn" | "info"; dot?: boolean; pulse?: boolean; children: ReactNode }) {
  return (
    <span className={`pill-tag ${tone ?? ""}`}>
      {dot && <span className={`dot ${pulse ? "pulse" : ""}`} />}
      {children}
    </span>
  );
}

export function Empty({ icon, title, children, action }: { icon: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="icon-bubble" style={{ width: 56, height: 56, borderRadius: 18 }}>{icon}</div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Skeleton({ h = 18, w = "100%", r = 12 }: { h?: number; w?: number | string; r?: number }) {
  return <div className="skeleton" style={{ height: h, width: w, borderRadius: r }} />;
}

export function Spinner({ size = 18 }: { size?: number }) {
  return <span className="spinner" style={{ width: size, height: size }} />;
}

export const fadeUp = {
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { type: "spring", stiffness: 260, damping: 26 },
} as const;

export function stagger(i: number) {
  return { ...fadeUp, transition: { ...fadeUp.transition, delay: Math.min(i, 10) * 0.04 } };
}
