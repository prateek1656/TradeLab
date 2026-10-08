import { motion } from "framer-motion";
import { useMemo } from "react";

interface Candle { o: number; c: number; h: number; l: number }

function series(n: number, seed: number): Candle[] {
  let x = seed;
  const rand = () => {
    x = (x * 16807) % 2147483647;
    return (x - 1) / 2147483646;
  };
  const out: Candle[] = [];
  let price = 100;
  for (let i = 0; i < n; i++) {
    const bullish = rand() < 0.68;
    const body = 1.5 + rand() * 4;
    const o = price;
    const c = bullish ? o + body : o - body * 0.7;
    const h = Math.max(o, c) + rand() * 2.4;
    const l = Math.min(o, c) - rand() * 2.4;
    out.push({ o, c, h, l });
    price = c;
  }
  return out;
}

/** Decorative, animated bullish candlestick chart. */
export function Candles({ count = 28, seed = 7, className = "" }: { count?: number; seed?: number; className?: string }) {
  const data = useMemo(() => series(count, seed), [count, seed]);
  const W = 600;
  const H = 300;
  const min = Math.min(...data.map((d) => d.l));
  const max = Math.max(...data.map((d) => d.h));
  const y = (v: number) => H - 14 - ((v - min) / (max - min)) * (H - 28);
  const step = W / count;
  const bw = step * 0.56;
  const trend = data.map((d, i) => `${i === 0 ? "M" : "L"}${i * step + step / 2},${y((d.o + d.c) / 2)}`).join(" ");

  return (
    <svg className={className} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id="cdl-up" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#4dffb8" />
          <stop offset="1" stopColor="#0c9a5f" />
        </linearGradient>
        <linearGradient id="cdl-area" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#2ff5a5" stopOpacity="0.28" />
          <stop offset="1" stopColor="#2ff5a5" stopOpacity="0" />
        </linearGradient>
      </defs>
      <motion.path
        d={`${trend} L${W - step / 2},${H} L${step / 2},${H} Z`}
        fill="url(#cdl-area)"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.6, duration: 1.2 }}
      />
      {data.map((d, i) => {
        const up = d.c >= d.o;
        const cx = i * step + step / 2;
        const top = y(Math.max(d.o, d.c));
        const bottom = y(Math.min(d.o, d.c));
        return (
          <motion.g
            key={i}
            initial={{ opacity: 0, scaleY: 0.2 }}
            animate={{ opacity: up ? 1 : 0.55, scaleY: 1 }}
            transition={{ delay: 0.15 + i * 0.035, type: "spring", stiffness: 200, damping: 18 }}
            style={{ originY: `${bottom}px`, originX: `${cx}px` }}
          >
            <line x1={cx} x2={cx} y1={y(d.h)} y2={y(d.l)} stroke={up ? "#2ff5a5" : "#ff5470"} strokeWidth={1.6} strokeLinecap="round" />
            <rect x={cx - bw / 2} y={top} width={bw} height={Math.max(2, bottom - top)} rx={2.5} fill={up ? "url(#cdl-up)" : "#ff5470"} />
          </motion.g>
        );
      })}
      <motion.path
        d={trend}
        fill="none"
        stroke="#2ff5a5"
        strokeWidth={2}
        strokeLinecap="round"
        strokeDasharray="1 0"
        initial={{ pathLength: 0, opacity: 0 }}
        animate={{ pathLength: 1, opacity: 0.85 }}
        transition={{ delay: 0.5, duration: 1.6, ease: "easeInOut" }}
        style={{ filter: "drop-shadow(0 0 6px rgba(47,245,165,.6))" }}
      />
    </svg>
  );
}
