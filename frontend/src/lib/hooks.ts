import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

export function useApi<T>(path: string | null, opts: { interval?: number } = {}) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!path);
  const [fails, setFails] = useState(0);
  const pathRef = useRef(path);
  pathRef.current = path;

  const reload = useCallback(async () => {
    const p = pathRef.current;
    if (!p) return;
    try {
      const d = await api<T>(p);
      if (pathRef.current === p) {
        setData(d);
        setError(null);
        setFails(0);
      }
    } catch (e) {
      setError((e as Error).message);
      setFails((n) => n + 1);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!path) return;
    setLoading(true);
    reload();
    if (!opts.interval) return;
    const id = setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, opts.interval);
    const onShow = () => {
      if (document.visibilityState === "visible") reload();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onShow);
    };
  }, [path, opts.interval, reload]);

  // Nothing loaded yet because the server didn't answer: try again by itself (2 s, 4 s, 8 s … up to
  // every 30 s), so a brief hiccup never leaves a page empty or a button disabled until a refresh.
  useEffect(() => {
    if (!path || fails === 0 || data !== null) return;
    const id = setTimeout(reload, Math.min(2000 * 2 ** (fails - 1), 30000));
    return () => clearTimeout(id);
  }, [path, fails, data, reload]);

  useEffect(() => {
    if (!path) return;
    const onOnline = () => reload();
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, [path, reload]);

  return { data, error, loading, reload, setData };
}

export function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function useDebounced<T>(value: T, ms = 200) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}
