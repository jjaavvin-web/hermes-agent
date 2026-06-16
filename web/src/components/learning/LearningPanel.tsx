import { AlertTriangle, CheckCircle2, RefreshCw, ShieldCheck, Sparkles } from "lucide-react";
import type { LearningEmbedCoverage, LearningHistoryPoint, LearningResponse, LearningStatus } from "@/lib/api";

const STATUS_CFG: Record<
  LearningStatus,
  { label: string; color: string; dot: string; chip: string; ring: string; soft: string }
> = {
  green: {
    label: "Green",
    color: "#4ade80",
    dot: "bg-[#4ade80] shadow-[0_0_7px_#4ade80]",
    chip: "bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)]",
    ring: "rgba(74,222,128,0.35)",
    soft: "rgba(74,222,128,0.08)",
  },
  amber: {
    label: "Amber",
    color: "#ffbd38",
    dot: "bg-[#ffbd38] shadow-[0_0_7px_#ffbd38]",
    chip: "bg-[rgba(255,189,56,0.12)] text-[#ffbd38] border border-[rgba(255,189,56,0.35)]",
    ring: "rgba(255,189,56,0.45)",
    soft: "rgba(255,189,56,0.07)",
  },
  red: {
    label: "Red",
    color: "#fb2c36",
    dot: "bg-[#fb2c36] shadow-[0_0_7px_#fb2c36]",
    chip: "bg-[rgba(251,44,54,0.12)] text-[#fb2c36] border border-[rgba(251,44,54,0.40)]",
    ring: "rgba(251,44,54,0.55)",
    soft: "rgba(251,44,54,0.07)",
  },
  unknown: {
    label: "Unknown",
    color: "#7c91a8",
    dot: "bg-[#7c91a8]",
    chip: "bg-[rgba(124,145,168,0.10)] text-[#7c91a8] border border-[rgba(124,145,168,0.30)]",
    ring: "rgba(124,145,168,0.30)",
    soft: "rgba(124,145,168,0.06)",
  },
};

function normalizeStatus(status: LearningResponse["status"]): LearningStatus {
  return status === "green" || status === "amber" || status === "red" || status === "unknown" ? status : "unknown";
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function fmtNumber(value: unknown, digits = 0): string {
  const num = finiteNumber(value);
  if (num === null) return "—";
  return num.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function fmtPct(value: unknown, digits = 1): string {
  const num = finiteNumber(value);
  if (num === null) return "—";
  const pct = Math.abs(num) <= 1 ? num * 100 : num;
  return `${pct.toLocaleString(undefined, { maximumFractionDigits: digits })}%`;
}

function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function StatusDot({ status, className = "" }: { status: LearningStatus; className?: string }) {
  return <span className={`inline-block h-2.5 w-2.5 flex-shrink-0 rounded-full ${STATUS_CFG[status].dot} ${className}`} />;
}

function StatusBadge({ status }: { status: LearningStatus }) {
  const cfg = STATUS_CFG[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-[0.12em] ${cfg.chip}`}>
      <StatusDot status={status} className="h-1.5 w-1.5" />
      {cfg.label}
    </span>
  );
}

function MetricChip({ label, value, status = "green" }: { label: string; value: string | number; status?: LearningStatus }) {
  const cfg = STATUS_CFG[status];
  return (
    <div
      className="inline-flex min-w-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs"
      style={{ borderColor: cfg.ring, background: cfg.soft }}
      title={`${label}: ${value}`}
    >
      <StatusDot status={status} className="h-1.5 w-1.5" />
      <span className="max-w-[9rem] truncate text-text-tertiary">{label}</span>
      <span className="font-mono font-semibold text-text-primary">{value}</span>
    </div>
  );
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

function LearningSparkline({ points }: { points: LearningHistoryPoint[] }) {
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

function CanaryBadge({ result }: { result: LearningResponse["result_latest"] }) {
  const passed = result?.pass === true;
  const failed = result?.pass === false;
  const status: LearningStatus = passed ? "green" : failed ? "red" : "unknown";
  const cfg = STATUS_CFG[status];
  return (
    <div className="rounded-lg border bg-card p-4" style={{ borderColor: cfg.ring, background: cfg.soft }}>
      <div className="flex items-center gap-2">
        <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border" style={{ borderColor: cfg.ring, color: cfg.color }}>
          {passed ? <ShieldCheck className="h-5 w-5" /> : <AlertTriangle className="h-5 w-5" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-mondwest text-display text-sm tracking-[0.14em]" style={{ color: cfg.color }}>Canary {passed ? "passed" : failed ? "failed" : "unknown"}</p>
          <p className="mt-0.5 text-xs text-text-tertiary">rank <span className="font-mono text-text-primary">{fmtNumber(result?.rank)}</span></p>
        </div>
      </div>
      <p className="mt-3 break-all font-mono text-[11px] text-text-secondary">{result?.planted_uuid ?? "No planted UUID reported"}</p>
    </div>
  );
}

function ImportanceHistogram({ hist }: { hist?: Record<string, number | undefined> | null }) {
  const rows = ["2", "3", "4", "5"].map((key) => ({ key, value: finiteNumber(hist?.[key]) ?? 0 }));
  const max = Math.max(1, ...rows.map((row) => row.value));
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="font-mondwest text-display text-xs tracking-[0.16em] text-text-primary">Importance histogram</h3>
      <div className="mt-4 space-y-3">
        {rows.map((row) => (
          <div key={row.key} className="grid grid-cols-[2.5rem_1fr_3rem] items-center gap-2 text-xs">
            <span className="font-mono text-text-tertiary">imp {row.key}</span>
            <div className="h-3 overflow-hidden rounded-full bg-border">
              <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.max(3, (row.value / max) * 100)}%` }} />
            </div>
            <span className="text-right font-mono text-text-primary">{fmtNumber(row.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function EmbedCoverage({ coverage }: { coverage?: LearningEmbedCoverage | null }) {
  const entries = Object.entries(coverage ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="font-mondwest text-display text-xs tracking-[0.16em] text-text-primary">Embed coverage</h3>
      <div className="mt-4 space-y-3">
        {entries.length === 0 && <p className="text-xs text-text-tertiary">No embed_coverage data.</p>}
        {entries.map(([kind, item]) => {
          const embedded = finiteNumber(item?.embedded) ?? 0;
          const total = finiteNumber(item?.total) ?? 0;
          const ratio = finiteNumber(item?.ratio) ?? (total > 0 ? embedded / total : 0);
          const pct = Math.max(0, Math.min(100, Math.abs(ratio) <= 1 ? ratio * 100 : ratio));
          return (
            <div key={kind}>
              <div className="mb-1 flex items-baseline justify-between gap-2 text-xs">
                <span className="truncate font-semibold text-text-primary">{kind}</span>
                <span className="font-mono text-text-secondary">{fmtNumber(embedded)} / {fmtNumber(total)} · {fmtPct(ratio)}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-border">
                <span className="block h-full rounded-full bg-[#4ade80]" style={{ width: `${pct}%` }} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface LearningPanelProps {
  snapshot: LearningResponse;
  loading: boolean;
  error?: string | null;
  onRefresh: () => void;
}

export function LearningPanel({ snapshot, loading, error, onRefresh }: LearningPanelProps) {
  const status = normalizeStatus(snapshot.status);
  const cfg = STATUS_CFG[status];
  const latest = snapshot.snapshot_latest ?? null;
  const history = snapshot.history_tail ?? [];
  const errors = snapshot.errors ?? [];
  const dupRatio = finiteNumber(latest?.dup_ratio) ?? 0;
  const bridged = finiteNumber(latest?.auto_bridged_count) ?? 0;
  const quarantine = finiteNumber(latest?.quarantine_count) ?? 0;

  return (
    <div className="bg-background p-4 text-text-primary">
      <div className="mb-3 flex flex-shrink-0 items-center gap-1.5 text-xs text-text-tertiary">
        <Sparkles className="h-3.5 w-3.5" />
        <span className="font-mondwest text-display tracking-[0.16em]">Learning Loop Signal</span>
        {error && <span className="text-warning">· refresh failed: {error}</span>}
      </div>

      <section className="rounded-lg border bg-card p-4" style={{ borderColor: status === "green" ? undefined : cfg.ring }} aria-label="Learning status">
        <div className="flex flex-wrap items-center gap-3">
          <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border" style={{ borderColor: cfg.ring, background: cfg.soft }}>
            {status === "green" ? <CheckCircle2 className="h-5 w-5" style={{ color: cfg.color }} /> : <AlertTriangle className="h-5 w-5" style={{ color: cfg.color }} />}
          </span>
          <div className="min-w-[180px] flex-1">
            <h2 className="font-mondwest text-display text-base tracking-[0.12em]" style={{ color: cfg.color }}>
              Learning status · {cfg.label}
            </h2>
            <p className="mt-0.5 text-xs text-text-tertiary">read-only MVMS lesson quality + canary surface</p>
          </div>
          <StatusBadge status={status} />
          <button type="button" onClick={onRefresh} aria-label="Refresh learning snapshot" className="flex-shrink-0 rounded-md border border-border p-1.5 text-text-secondary transition hover:text-text-primary">
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>

        <div className="mt-3 flex flex-wrap gap-2" aria-label="Learning metric chips">
          <MetricChip label="SIGNAL_SCORE" value={fmtNumber(latest?.SIGNAL_SCORE, 3)} status={status} />
          <MetricChip label="trusted_count" value={fmtNumber(latest?.trusted_count)} status="green" />
          <MetricChip label="dup_ratio" value={fmtPct(dupRatio)} status={dupRatio > 0.18 ? "amber" : "green"} />
          <MetricChip label="auto_bridged noise" value={fmtNumber(bridged)} status={bridged > 0 ? "amber" : "green"} />
          <MetricChip label="quarantine_count" value={fmtNumber(quarantine)} status={quarantine > 0 ? "amber" : "green"} />
          <MetricChip label="trusted_ratio" value={fmtPct(latest?.trusted_ratio)} status="green" />
          <MetricChip label="lessons_total" value={fmtNumber(latest?.lessons_total)} status="unknown" />
        </div>
      </section>

      <div className="mt-4 grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.5fr)_minmax(280px,0.75fr)]">
        <LearningSparkline points={history} />
        <CanaryBadge result={snapshot.result_latest} />
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        <ImportanceHistogram hist={latest?.importance_hist} />
        <EmbedCoverage coverage={latest?.embed_coverage} />
      </div>

      {(errors.length > 0 || latest === null) && (
        <div className="mt-3 rounded-lg border border-[rgba(255,189,56,0.35)] bg-[rgba(255,189,56,0.07)] p-3 text-xs text-warning">
          {latest === null && <p>snapshot_latest missing — endpoint returned a graceful empty payload.</p>}
          {errors.map((item, index) => <p key={`${item}-${index}`}>{item}</p>)}
        </div>
      )}
    </div>
  );
}
