import type { JSX } from "react";
import type { LearningHistoryPoint } from "@/lib/api";
import { fmtTs } from "./constants";

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function linePath(values: number[], width: number, height: number): string {
  if (values.length === 0) return "";
  const max = Math.max(1, ...values);
  return values
    .map((value, index) => {
      const x = values.length === 1 ? width / 2 : (index / (values.length - 1)) * width;
      const y = height - (value / max) * height;
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

export function Sparkline({ points }: { points: Array<{ date?: string; count?: number }> }): JSX.Element {
  const history = points.filter(Boolean).slice(-24);
  const values = history.map((point) => Number(point.count || 0));
  const max = Math.max(1, ...values);

  if (history.length === 0) {
    return <div className="flex h-44 items-center justify-center rounded-lg border border-border bg-background/35 text-sm text-text-tertiary">No recent task creation</div>;
  }

  return (
    <div className="rounded-lg border border-border bg-background/35 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-3 text-xs text-text-tertiary">
        <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-[#4ade80]" />created</span>
        <span className="ml-auto font-mono">{history.at(-1)?.date ?? "latest"}</span>
      </div>
      <svg viewBox="0 0 320 128" className="h-40 w-full overflow-visible" role="img" aria-label="OS task creation history">
        <defs>
          <linearGradient id="osActivityFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#4ade80" stopOpacity="0.38" />
            <stop offset="100%" stopColor="#4ade80" stopOpacity="0.06" />
          </linearGradient>
        </defs>
        {[0, 32, 64, 96, 128].map((y) => (
          <line key={y} x1="0" x2="320" y1={y} y2={y} stroke="rgba(124,145,168,0.16)" strokeWidth="1" />
        ))}
        {values.map((value, index) => {
          const x = values.length === 1 ? 156 : (index / (values.length - 1)) * 312;
          const height = Math.max(5, (value / max) * 96);
          return (
            <rect
              key={`${history[index]?.date ?? index}-created`}
              x={x}
              y={124 - height}
              width="8"
              height={height}
              rx="3"
              fill="url(#osActivityFill)"
            />
          );
        })}
      </svg>
    </div>
  );
}

export function LearningSparkline({ points }: { points: LearningHistoryPoint[] }): JSX.Element {
  const history = points.filter(Boolean).slice(-24);
  const trusted = history.map((point) => finiteNumber(point.trusted_count) ?? 0);
  const dupPct = history.map((point) => {
    const ratio = finiteNumber(point.dup_ratio) ?? 0;
    return Math.max(0, Math.min(100, Math.abs(ratio) <= 1 ? ratio * 100 : ratio));
  });
  const trustedMax = Math.max(1, ...trusted);

  if (history.length === 0) {
    return <div className="flex h-44 items-center justify-center rounded-lg border border-border bg-background/35 text-sm text-text-tertiary">No history_tail yet.</div>;
  }

  return (
    <div className="rounded-lg border border-border bg-background/35 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-3 text-xs text-text-tertiary">
        <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-[#4ade80]" />trusted_count</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-[#ffbd38]" />dup_ratio %</span>
        <span className="ml-auto font-mono">{fmtTs(history.at(-1)?.generated_at)}</span>
      </div>
      <svg viewBox="0 0 320 128" className="h-40 w-full overflow-visible" role="img" aria-label="Learning trusted count and duplicate ratio history">
        <defs>
          <linearGradient id="learningTrustedFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#4ade80" stopOpacity="0.38" />
            <stop offset="100%" stopColor="#4ade80" stopOpacity="0.06" />
          </linearGradient>
        </defs>
        {[0, 32, 64, 96, 128].map((y) => (
          <line key={y} x1="0" x2="320" y1={y} y2={y} stroke="rgba(124,145,168,0.16)" strokeWidth="1" />
        ))}
        {trusted.map((value, index) => {
          const x = trusted.length === 1 ? 156 : (index / (trusted.length - 1)) * 312;
          const height = Math.max(5, (value / trustedMax) * 96);
          return (
            <rect
              key={`${history[index]?.generated_at ?? index}-trusted`}
              x={x}
              y={124 - height}
              width="8"
              height={height}
              rx="3"
              fill="url(#learningTrustedFill)"
            />
          );
        })}
        <path d={linePath(dupPct, 320, 112)} fill="none" stroke="#ffbd38" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}
