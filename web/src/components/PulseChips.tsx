import { useCallback, useEffect, useState } from "react";
import { fetchJSON } from "@/lib/api";

const KPI_POLL_MS = 10_000;
const RECENT_COMPLETION_WINDOW_MS = 60 * 60 * 1000;

export interface PulseKpisLastCompletion {
  slug: string;
  completed_at: string | null;
  summary: string;
}

export interface PulseKpisResponse {
  active_hives: number;
  pending_cards: number;
  max_usage_pct: number | null;
  today_spend_usd: number;
  today_pr_merges: number;
  last_completion: PulseKpisLastCompletion | null;
}

type ChipAccent =
  | "purple"
  | "cyan"
  | "pink"
  | "yellow"
  | "red"
  | "green"
  | "gray";

interface ChipProps {
  accent: ChipAccent;
  label: string;
  value: string;
  suffix?: string;
  retry?: boolean;
  title?: string;
}

function Chip({ accent, label, value, suffix, retry, title }: ChipProps) {
  return (
    <div className={`pulse-chip pulse-chip--${accent}`} title={title}>
      <span className="pulse-chip__label">{label}</span>
      <span className="pulse-chip__value">{value}</span>
      {suffix && <span className="pulse-chip__suffix">{suffix}</span>}
      {retry && <span className="pulse-chip__retry">(retry)</span>}
      <span aria-hidden className="pulse-chip__spark" />
    </div>
  );
}

function fmtRelative(iso: string | null | undefined, nowMs: number): string {
  if (!iso) return "never";
  let ts: number;
  try {
    ts = new Date(iso).getTime();
  } catch {
    return "never";
  }
  if (Number.isNaN(ts)) return "never";
  const diffSec = Math.max(0, Math.round((nowMs - ts) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86_400) return `${Math.floor(diffSec / 3600)}h ago`;
  return `${Math.floor(diffSec / 86_400)}d ago`;
}

export default function PulseChips() {
  const [kpis, setKpis] = useState<PulseKpisResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  const load = useCallback(async () => {
    try {
      const data = await fetchJSON<PulseKpisResponse>("/api/pulse/kpis");
      setKpis(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "KPIs unavailable");
    }
  }, []);

  useEffect(() => {
    // Polling pattern mirrors HivesPage: fire-and-forget the async loader
    // then re-fire every KPI_POLL_MS. The set-state-in-effect lint rule
    // traces the async resolution back into load(); the actual setState
    // happens after the fetch promise resolves (not synchronously), so
    // suppressing here is safe and matches sibling pages.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    const timer = setInterval(() => void load(), KPI_POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  // Tick the clock once per minute so the "Xm ago" label stays fresh between
  // KPI polls — the backend's last_completion timestamp doesn't change but
  // the human-readable delta should.
  useEffect(() => {
    const tick = setInterval(() => setNowMs(Date.now()), 60_000);
    return () => clearInterval(tick);
  }, []);

  if (error && !kpis) {
    return (
      <div className="pulse-chips" role="status" aria-live="polite">
        <Chip accent="purple" label="hives" value="?" retry title={error} />
        <Chip accent="yellow" label="pending" value="?" retry title={error} />
        <Chip accent="cyan" label="spend" value="?" retry title={error} />
        <Chip accent="pink" label="merges" value="?" retry title={error} />
        <Chip accent="gray" label="last" value="?" retry title={error} />
      </div>
    );
  }

  if (!kpis) {
    return (
      <div className="pulse-chips" aria-busy="true">
        <Chip accent="purple" label="hives" value="—" />
        <Chip accent="yellow" label="pending" value="—" />
        <Chip accent="cyan" label="spend" value="—" />
        <Chip accent="pink" label="merges" value="—" />
        <Chip accent="gray" label="last" value="—" />
      </div>
    );
  }

  const spend = `$${kpis.today_spend_usd.toFixed(2)}`;
  const lc = kpis.last_completion;
  let lcAccent: ChipAccent = "gray";
  let lcValue = "never";
  if (lc) {
    const ts = lc.completed_at ? new Date(lc.completed_at).getTime() : NaN;
    const isRecent =
      !Number.isNaN(ts) && nowMs - ts <= RECENT_COMPLETION_WINDOW_MS;
    lcAccent = isRecent ? "green" : "gray";
    lcValue = `${lc.slug} · ${fmtRelative(lc.completed_at, nowMs)}`;
  }

  return (
    <div className="pulse-chips" role="status" aria-live="polite">
      <Chip
        accent="purple"
        label="hives"
        value={String(kpis.active_hives)}
      />
      <Chip
        accent="yellow"
        label="pending"
        value={String(kpis.pending_cards)}
      />
      <Chip accent="cyan" label="spend" value={spend} />
      <Chip
        accent="pink"
        label="merges"
        value={String(kpis.today_pr_merges)}
        suffix="merged"
      />
      <Chip
        accent={lcAccent}
        label="last"
        value={lcValue}
        title={lc?.summary || undefined}
      />
    </div>
  );
}
