import { AnimatePresence, motion } from "framer-motion";
import { X } from "lucide-react";
import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "./ui";

export function Sheet({
  open,
  onClose,
  title,
  subtitle,
  children,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div className="overlay" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
          <motion.div
            className={`sheet ${wide ? "wide" : ""}`}
            role="dialog"
            aria-modal="true"
            initial={{ y: 60, opacity: 0, scale: 0.98 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 40, opacity: 0, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 380, damping: 34 }}
          >
            <div className="grabber" />
            <div className="sheet-head">
              <div>
                <h2>{title}</h2>
                {subtitle && <p>{subtitle}</p>}
              </div>
              <button className="icon-btn" onClick={onClose} aria-label="Close">
                <X size={20} />
              </button>
            </div>
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

export function Confirm({
  open,
  onClose,
  onConfirm,
  title,
  body,
  confirmLabel,
  tone = "primary",
  loading,
  error,
}: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger" | "warn";
  loading?: boolean;
  error?: string | null;
}) {
  return (
    <Sheet open={open} onClose={onClose} title={title}>
      <div className="stack">
        <div style={{ color: "var(--text-2)" }}>{body}</div>
        {error && <div className="banner error">{error}</div>}
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant={tone} onClick={onConfirm} loading={loading}>{confirmLabel}</Button>
        </div>
      </div>
    </Sheet>
  );
}
