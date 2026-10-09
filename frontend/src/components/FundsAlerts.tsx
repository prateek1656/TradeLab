import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";
import { api } from "../lib/api";
import { timeOf } from "../lib/format";
import { useApi } from "../lib/hooks";
import type { FundsAlert } from "../lib/types";

const SECTION_LABEL: Record<string, string> = { intraday: "Intraday", holding: "Holding", fno: "F&O", commodity: "Commodity" };

/** Small red pop-up naming the accounts that couldn't trade for lack of money. Stays until dismissed. */
export function FundsAlerts() {
  const alerts = useApi<FundsAlert[]>("/alerts", { interval: 30000 });
  const list = alerts.data ?? [];

  const dismiss = async (runId: number) => {
    alerts.setData(list.filter((a) => a.run_id !== runId));
    try {
      await api(`/alerts/${runId}/seen`, { method: "POST" });
    } catch {
      alerts.reload();
    }
  };

  return (
    <div className="funds-alerts" aria-live="assertive">
      <AnimatePresence>
        {list.slice(0, 3).map((a) => (
          <motion.div
            key={a.run_id}
            className="funds-alert"
            role="alert"
            initial={{ opacity: 0, y: -12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.98 }}
          >
            <AlertTriangle size={18} className="fa-icon" />
            <div className="fa-body">
              <b>Not enough funds</b>
              <span>
                {SECTION_LABEL[a.section] ?? a.section} trade at {timeOf(a.started_at)} skipped in{" "}
                <b>{a.accounts.join(", ")}</b>
              </span>
            </div>
            <button className="fa-close" onClick={() => dismiss(a.run_id)} aria-label="Dismiss">
              <X size={16} />
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
