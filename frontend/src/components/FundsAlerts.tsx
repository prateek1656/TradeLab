import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";
import { api } from "../lib/api";
import { timeOf } from "../lib/format";
import { useApi } from "../lib/hooks";
import type { FundsAlert } from "../lib/types";

const SECTION_LABEL: Record<string, string> = { intraday: "Intraday", holding: "Holding", fno: "F&O", commodity: "Commodity", all: "Square off everything" };

/** Small red pop-ups that stay until dismissed: accounts that couldn't trade for lack of money, and
 *  square-offs or exits that failed somewhere (positions may still be open). */
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
            {a.kind === "exit" ? (
              <div className="fa-body">
                <b>Positions may still be open</b>
                <span>
                  {a.section === "all" ? "Square off everything" : `${SECTION_LABEL[a.section] ?? a.section} square-off / exit`} at {timeOf(a.started_at)} didn't fully go through
                  {a.accounts.length ? <> in <b>{a.accounts.join(", ")}</b></> : null}. Check Activity and the Kotak Neo app.
                </span>
              </div>
            ) : (
              <div className="fa-body">
                <b>Not enough funds</b>
                <span>
                  {SECTION_LABEL[a.section] ?? a.section} buy at {timeOf(a.started_at)} couldn't go through in{" "}
                  <b>{a.accounts.join(", ")}</b>
                </span>
              </div>
            )}
            <button className="fa-close" onClick={() => dismiss(a.run_id)} aria-label="Dismiss">
              <X size={16} />
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
