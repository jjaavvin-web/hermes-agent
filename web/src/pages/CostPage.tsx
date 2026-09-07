import { useEffect, useState } from "react";
import { AlertTriangle, DollarSign, RefreshCw } from "lucide-react";
import { api, type CostRollup, type CostSnapshot } from "@/lib/api";

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtUsd(n: number): string {
  return `$${n.toFixed(n > 0 && n < 1 ? 4 : 2)}`;
}

function fmtMs(n: number): string {
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k ms`;
  return `${n.toFixed(1)} ms`;
}

function RollupCard({ title, rollup }: { title: string; rollup: CostRollup }) {
  return (
    <div className="rounded-lg border border-border bg-card p-5">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{title}</div>
      <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-3xl font-semibold text-foreground">{fmtUsd(rollup.totalCostUsd)}</span>
        <span className="text-sm text-muted-foreground">
          {rollup.totalTurns} turns · {fmtTokens(rollup.totalTokens)} tokens
        </span>
      </div>
    </div>
  );
}

export default function CostPage() {
  const [data, setData] = useState<CostSnapshot | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  function load() {
    setLoading(true);
    api
      .getCost()
      .then((d) => {
        setData(d);
        setErr(null);
      })
      .catch((e) => setErr(String((e as Error)?.message ?? e)))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  if (loading && !data) {
    return <div className="p-6 text-muted-foreground">Loading cost ledger…</div>;
  }
  if (err) {
    return <div className="p-6 text-destructive">Failed to load cost ledger: {err}</div>;
  }
  if (!data) return null;

  const groups = data.last7d.groups;
  const empty = data.today.totalTurns === 0 && data.last7d.totalTurns === 0;
  const dailySeries = data.dailySeries ?? [];
  const maxDailyCost = Math.max(...dailySeries.map((p) => p.costUsd), 0);
  const cacheLatency = data.cacheLatency7d ?? { cacheHitRatio: 0, avgLatencyMs: 0, p95LatencyMs: 0 };

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-xl font-semibold text-foreground">
          <DollarSign className="h-5 w-5" /> Cost ledger
        </h1>
        <button
          type="button"
          onClick={load}
          className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <RefreshCw className="h-4 w-4" /> Refresh
        </button>
      </div>

      <p className="text-sm text-muted-foreground">
        Per-turn token spend recorded in <code className="font-mono">turn_usage</code> (the WC-3/GWR-4
        measurement keystone).
        {empty ? " No turns recorded yet — this populates as turns flow." : null}
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <RollupCard title="Today" rollup={data.today} />
        <RollupCard title="Last 7 days" rollup={data.last7d} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(280px,1fr)]">
        <div className="rounded-lg border border-border bg-card p-5">
          <div className="flex items-baseline justify-between gap-3">
            <div>
              <div className="text-xs uppercase tracking-wide text-muted-foreground">30-day daily spend</div>
              <div className="mt-1 text-sm text-muted-foreground">
                Credit burn by recorded day; short until more ledger days accrue.
              </div>
            </div>
            <div className="shrink-0 text-sm font-medium text-foreground">
              {dailySeries.length ? `${dailySeries.length} day${dailySeries.length === 1 ? "" : "s"}` : "no spend yet"}
            </div>
          </div>
          {dailySeries.length ? (
            <svg
              data-testid="cost-daily-spark"
              viewBox="0 0 300 96"
              role="img"
              aria-label="30-day daily spend bars"
              className="mt-4 h-24 w-full overflow-visible"
              preserveAspectRatio="none"
            >
              {dailySeries.map((point, i) => {
                const gap = 4;
                const barWidth = Math.max(4, (300 - gap * Math.max(dailySeries.length - 1, 0)) / dailySeries.length);
                const scaled = maxDailyCost > 0 ? point.costUsd / maxDailyCost : 0;
                const height = Math.max(3, scaled * 82);
                const x = i * (barWidth + gap);
                const y = 92 - height;
                return (
                  <rect
                    key={point.date}
                    x={x}
                    y={y}
                    width={barWidth}
                    height={height}
                    rx="2"
                    className="fill-emerald-400/80"
                  />
                );
              })}
            </svg>
          ) : (
            <div className="mt-4 rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
              No daily spend recorded in the last 30 days.
            </div>
          )}
        </div>

        <div className="rounded-lg border border-border bg-card p-5">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">Last 7 days quality</div>
          <div className="mt-4 flex flex-wrap gap-2">
            <span
              data-testid="cost-cache-chip"
              className="rounded-full border border-border bg-background px-3 py-1 text-sm text-foreground"
            >
              Cache hit {(cacheLatency.cacheHitRatio * 100).toFixed(1)}%
            </span>
            <span
              data-testid="cost-latency-chip"
              className="rounded-full border border-border bg-background px-3 py-1 text-sm text-foreground"
            >
              avg {fmtMs(cacheLatency.avgLatencyMs)} · p95 {fmtMs(cacheLatency.p95LatencyMs)}
            </span>
          </div>
        </div>
      </div>

      {data.meteredLeakCount > 0 ? (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-4">
          <div className="flex items-center gap-2 font-medium text-amber-400">
            <AlertTriangle className="h-4 w-4" /> Metered-spend leak: {data.meteredLeakCount} turn(s) ·{" "}
            {fmtUsd(data.meteredLeakCostUsd)}
          </div>
          <div className="mt-1 text-sm text-muted-foreground">
            Turns billed to metered Anthropic/OpenRouter — the doctrine is unmetered
            claude-cli-subprocess / codex. Top offenders:
          </div>
          <ul className="mt-2 space-y-1 text-sm">
            {data.meteredLeak.slice(0, 8).map((l) => (
              <li key={l.turnId} className="flex justify-between gap-4">
                <span className="truncate text-foreground">
                  {l.provider}/{l.model}
                </span>
                <span className="shrink-0 text-amber-400">
                  {fmtUsd(l.costUsd)} · {fmtTokens(l.totalTokens)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="rounded-lg border border-border bg-card px-4 py-2 text-sm text-muted-foreground">
          ✓ No metered-spend leaks in the last 7 days — all turns on unmetered providers.
        </div>
      )}

      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-card text-muted-foreground">
            <tr>
              <th className="px-4 py-2 text-left font-medium">Provider / Model</th>
              <th className="px-4 py-2 text-right font-medium">Turns</th>
              <th className="px-4 py-2 text-right font-medium">Input</th>
              <th className="px-4 py-2 text-right font-medium">Output</th>
              <th className="px-4 py-2 text-right font-medium">Total</th>
              <th className="px-4 py-2 text-right font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {groups.length === 0 ? (
              <tr>
                <td colSpan={6} className="py-6 text-center text-muted-foreground">
                  No spend in the last 7 days.
                </td>
              </tr>
            ) : (
              groups.map((g, i) => (
                <tr key={`${g.provider}/${g.model}/${g.promptVersion}/${i}`} className="border-t border-border">
                  <td className="px-4 py-2 text-foreground">
                    {g.provider}/{g.model}
                    {g.promptVersion && g.promptVersion !== "unknown" ? (
                      <span className="text-muted-foreground"> · {g.promptVersion}</span>
                    ) : null}
                  </td>
                  <td className="px-4 py-2 text-right">{g.turns}</td>
                  <td className="px-4 py-2 text-right">{fmtTokens(g.inputTokens)}</td>
                  <td className="px-4 py-2 text-right">{fmtTokens(g.outputTokens)}</td>
                  <td className="px-4 py-2 text-right">{fmtTokens(g.totalTokens)}</td>
                  <td className="px-4 py-2 text-right">{fmtUsd(g.costUsd)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="font-mono text-xs text-muted-foreground">source: {data.dbPath}</div>
    </div>
  );
}
