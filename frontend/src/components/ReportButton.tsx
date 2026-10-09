import { FileSpreadsheet } from "lucide-react";
import { useState } from "react";
import { download, reportToast } from "../lib/api";
import { useToast } from "../lib/toast";
import { Button } from "./ui";

/** Downloads the Excel P/L report for one day (YYYY-MM-DD). */
export function ReportButton({ day, label = "Excel report", className, onDone }: {
  day: string;
  label?: string;
  className?: string;
  onDone?: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    try {
      const headers = await download(`/reports/${day}.xlsx`, `TradeLab-PnL-${day}.xlsx`);
      toast(...reportToast(headers, "Report downloaded"));
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
      onDone?.();
    }
  };
  return (
    <Button variant="ghost" size="sm" onClick={go} loading={busy} className={className} aria-label={`Download P/L report for ${day}`}>
      {!busy && <FileSpreadsheet size={16} />} {label}
    </Button>
  );
}
