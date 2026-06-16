import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw, ShieldCheck, Sparkles } from "lucide-react";
import type { DistillerStatus, LearningResponse, LoopCriticStatus, RecallFilter } from "@/lib/api";
import { STATUS_CFG, SEVERITY_SCORE, fmtTs, type Status } from "@/components/StatusKit/constants";
import { MetricChip } from "@/components/StatusKit/MetricChip";
import { StatusChip } from "@/components/StatusKit/StatusChip";
import { StatusDot } from "@/components/StatusKit/StatusDot";
import { LearningSparkline, Sparkline } from "@/components/StatusKit/Sparkline";

function normalizeStatus(status: LearningResponse["status"]): Status {
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

function fmtBool(value: boolean | null | undefined): string {
  if (value === true) return "true";
  if (value === false) return "false";
  return "—";
}

function fmtDuration(secondsValue: unknown): string {
  const seconds = finiteNumber(secondsValue);
  if (seconds === null) return "—";
  const clamped = Math.max(0, Math.floor(seconds));
  const days = Math.floor(clamped / 86_400);
  const hours = Math.floor((clamped % 86_400) / 3_600);
  const minutes = Math.floor((clamped % 3_600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function ageFromNow(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "—";
  return fmtDuration(Math.max(0, Math.floor((Date.now() - ts) / 1000)));
}

function noop(): void {
  // MetricChip is button-shaped in StatusKit for OS focus affordances; this panel is read-only.
}

function statusMax(statuses: Status[]): Status {
  return statuses.reduce<Status>((worst, next) => (SEVERITY_SCORE[next] > SEVERITY_SCORE[worst] ? next : worst), "green");
}

function PanelCard({
  title,
  kicker,
  status = "info",
  children,
}: {
  title: string;
  kicker?: string;
  status?: Status;
  children: ReactNode;
}) {
  const cfg = STATUS_CFG[status];
  return (
    <section className="min-w-[250px] rounded-lg border bg-card p-4" style={{ borderColor: cfg.ring, background: status === "info" ? undefined : cfg.soft }}>
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-mondwest text-display text-xs tracking-[0.16em] text-text-primary">{title}</h3>
          {kicker && <p className="mt-1 text-[11px] uppercase tracking-[0.14em] text-text-tertiary">{kicker}</p>}
        </div>
        <StatusDot status={status} className="mt-1" />
      </div>
      {children}
    </section>
  );
}

function DataRow({ label, value, emphasis = false }: { label: string; value: ReactNode; emphasis?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/50 py-1.5 last:border-b-0">
      <span className="text-xs text-text-tertiary">{label}</span>
      <span className={`${emphasis ? "text-sm" : "text-xs"} max-w-[60%] text-right font-mono text-text-primary`}>{value}</span>
    </div>
  );
}

function BooleanBadge({ label, value }: { label: string; value: boolean | null | undefined }) {
  const status: Status = value === true ? "green" : value === false ? "amber" : "unknown";
  const cfg = STATUS_CFG[status];
  return (
    <span className="inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[11px]" style={{ borderColor: cfg.ring, background: cfg.soft, color: cfg.color }}>
      <StatusDot status={status} className="h-1.5 w-1.5" />
      {label}: {fmtBool(value)}
    </span>
  );
}

function SignalPanel({ latest }: { latest: LearningResponse["snapshot_latest"] }) {
  const signalScore = finiteNumber(latest?.SIGNAL_SCORE);
  const actionableScore = finiteNumber(latest?.ACTIONABLE_SIGNAL_SCORE);
  const gap = signalScore !== null && actionableScore !== null ? actionableScore - signalScore : null;
  const trustedCount = finiteNumber(latest?.trusted_count);
  const lessonsTotal = finiteNumber(latest?.lessons_total);
  const trustedRatio = finiteNumber(latest?.trusted_ratio) ?? (trustedCount !== null && lessonsTotal ? trustedCount / lessonsTotal : null);
  const dupRatio = finiteNumber(latest?.dup_ratio);
  const gapStatus: Status = gap !== null && gap >= 5 ? "amber" : "green";

  return (
    <PanelCard title="1 · SIGNAL" kicker="pollution gap headline" status={gapStatus}>
      <div className="grid grid-cols-2 gap-2">
        <div className="rounded-md border border-border bg-background/35 p-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-text-tertiary">SIGNAL_SCORE</p>
          <p className="mt-1 font-mono text-3xl font-semibold text-text-primary">{fmtNumber(signalScore, 3)}</p>
        </div>
        <div className="rounded-md border border-[rgba(255,189,56,0.45)] bg-[rgba(255,189,56,0.07)] p-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-warning">ACTIONABLE</p>
          <p className="mt-1 font-mono text-3xl font-semibold text-warning">{fmtNumber(actionableScore, 3)}</p>
        </div>
      </div>
      <p className="mt-2 text-xs text-text-secondary">1-vs-8 pollution gap: <span className="font-mono text-text-primary">{gap === null ? "—" : fmtNumber(gap, 3)}</span></p>
      <div className="mt-3 space-y-1">
        <DataRow label="trusted_count / ratio" value={`${fmtNumber(trustedCount)} / ${fmtPct(trustedRatio)}`} />
        <DataRow label="dup_ratio" value={fmtPct(dupRatio)} />
        <DataRow label="lessons total / 7d" value={`${fmtNumber(latest?.lessons_total)} / ${fmtNumber(latest?.lessons_last_7d)}`} />
        <DataRow label="authored_by_agent" value={fmtNumber(latest?.lessons_authored_by_agent)} />
        <DataRow label="completions_total" value={fmtNumber(latest?.actionable_lessons_total)} />
      </div>
    </PanelCard>
  );
}

function CanaryPanel({ snapshot }: { snapshot: LearningResponse }) {
  const canary = snapshot.canary ?? null;
  const result = snapshot.result_latest ?? null;
  const passed = canary?.pass ?? result?.pass;
  const status: Status = passed === true ? "green" : passed === false ? "red" : "unknown";
  const embedPresent = canary?.embed_present;
  const confidenceStatus: Status = embedPresent === true ? "green" : embedPresent === false ? "amber" : "unknown";
  const confidence = embedPresent === true ? "Confidence: semantic" : embedPresent === false ? "Confidence: hybrid (text+kanban, no vector embedding)" : "Confidence: unknown";

  return (
    <PanelCard title="2 · CANARY VECTOR-CONFIDENCE" kicker="trap honesty" status={status}>
      <div className="flex items-center gap-2">
        <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border" style={{ borderColor: STATUS_CFG[status].ring, color: STATUS_CFG[status].color }}>
          {passed === true ? <ShieldCheck className="h-5 w-5" /> : <AlertTriangle className="h-5 w-5" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-mondwest text-display text-sm tracking-[0.14em]" style={{ color: STATUS_CFG[status].color }}>
            Canary {passed === true ? "passed" : passed === false ? "failed" : "unknown"}
          </p>
          <p className="mt-0.5 text-xs text-text-tertiary">rank <span className="font-mono text-text-primary">{fmtNumber(canary?.rank ?? result?.rank)}</span></p>
        </div>
      </div>
      <div className="mt-3 rounded-md border px-3 py-2 text-xs" style={{ borderColor: STATUS_CFG[confidenceStatus].ring, background: STATUS_CFG[confidenceStatus].soft, color: STATUS_CFG[confidenceStatus].color }}>
        {confidence}
      </div>
      <div className="mt-3 space-y-1">
        <DataRow label="recalled" value={fmtNumber(canary?.recalled)} />
        <DataRow label="avoided_mistake" value={fmtBool(canary?.avoided_mistake)} />
        <DataRow label="mode" value={canary?.mode ?? "—"} />
        <DataRow label="planted_uuid" value={<span className="break-all">{result?.planted_uuid ?? "—"}</span>} />
      </div>
    </PanelCard>
  );
}

function RecallPanel({ filters, latest }: { filters: RecallFilter | null | undefined; latest: LearningResponse["snapshot_latest"] }) {
  const autoBridged = finiteNumber(latest?.auto_bridged_count) ?? 0;
  const quarantine = finiteNumber(latest?.quarantine_count) ?? 0;
  const lessonsTotal = finiteNumber(latest?.lessons_total) ?? 0;
  const excludedTotal = autoBridged + quarantine;
  const denominator = lessonsTotal > 0 ? lessonsTotal : excludedTotal;
  const autoPct = denominator > 0 ? autoBridged / denominator : null;

  return (
    <PanelCard title="3 · RECALL QUALITY" kicker="filters + impact" status={filters ? "green" : "unknown"}>
      {!filters ? (
        <p className="rounded-md border border-border bg-background/35 p-3 text-xs text-text-tertiary">No recall filters configured</p>
      ) : (
        <>
          <div className="flex flex-wrap gap-2">
            <BooleanBadge label="include_quarantine" value={filters?.include_quarantine} />
            <BooleanBadge label="exclude_auto_bridged" value={filters?.exclude_auto_bridged} />
          </div>
          <div className="mt-3 space-y-1">
            <DataRow label="effective" value={filters?.effective ?? "—"} />
            <DataRow label="excluded auto-bridged" value={`${fmtNumber(autoBridged)} (${fmtPct(autoPct)})`} />
            <DataRow label="excluded quarantine" value={fmtNumber(quarantine)} />
          </div>
          <p className="mt-3 text-xs text-text-secondary">
            Impact: excluded {fmtNumber(autoBridged)} auto-bridged ({fmtPct(autoPct)}), {fmtNumber(quarantine)} quarantine.
          </p>
        </>
      )}
    </PanelCard>
  );
}

function DistillerPanel({ distiller }: { distiller: DistillerStatus | null | undefined }) {
  const frozen = distiller?.frozen_since === "2026-05-15";
  return (
    <PanelCard title="4 · DISTILLER STATUS" kicker="queue freeze visibility" status={frozen ? "amber" : distiller ? "green" : "unknown"}>
      {frozen && (
        <div className="mb-3 rounded-md border border-[rgba(255,189,56,0.45)] bg-[rgba(255,189,56,0.09)] px-3 py-2 text-xs font-semibold uppercase tracking-[0.12em] text-warning">
          DISTILLER FROZEN · open inbox
        </div>
      )}
      {!distiller ? (
        <p className="rounded-md border border-border bg-background/35 p-3 text-xs text-text-tertiary">Distiller data unavailable</p>
      ) : (
        <div className="space-y-1">
          <DataRow label="pending / approved / rejected" value={`${fmtNumber(distiller?.pending)} / ${fmtNumber(distiller?.approved)} / ${fmtNumber(distiller?.rejected)}`} emphasis />
          <DataRow label="oldest pending age" value={ageFromNow(distiller?.oldest_pending_ts)} />
          <DataRow label="oldest pending ts" value={fmtTs(distiller?.oldest_pending_ts)} />
          <DataRow label="last promotion" value={fmtTs(distiller?.last_promotion_ts)} />
          <DataRow label="stale_count" value={fmtNumber(distiller?.stale_count)} />
          <DataRow label="frozen_since" value={distiller?.frozen_since ?? "—"} />
        </div>
      )}
    </PanelCard>
  );
}

function LoopCriticPanel({ critic }: { critic: LoopCriticStatus | null | undefined }) {
  const checks = Object.entries(critic?.checks ?? {});
  const checkStatuses = checks.map(([, pass]) => (pass ? "green" : "red") as Status);
  return (
    <PanelCard title="5 · LOOP-CRITIC" kicker="7 hard checks" status={critic ? statusMax(checkStatuses) : "unknown"}>
      {!critic ? (
        <p className="rounded-md border border-border bg-background/35 p-3 text-xs text-text-tertiary">Loop critic data unavailable</p>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {checks.length === 0 && <p className="text-xs text-text-tertiary">No critic checks reported.</p>}
            {checks.map(([name, pass]) => (
              <div key={name} className="flex items-center gap-2 rounded-md border border-border bg-background/35 px-2 py-1.5 text-xs">
                <StatusDot status={pass ? "green" : "red"} className="h-1.5 w-1.5" />
                <span className="truncate text-text-secondary" title={name}>{name}</span>
              </div>
            ))}
          </div>
          <div className="mt-3 space-y-1">
            <DataRow label="quarantine_last_7d" value={fmtNumber(critic?.quarantine_last_7d)} />
            <DataRow label="next run countdown" value={fmtDuration(critic?.next_run_countdown_seconds)} />
          </div>
        </>
      )}
    </PanelCard>
  );
}

function ImportanceAndEmbedPanel({ latest }: { latest: LearningResponse["snapshot_latest"] }) {
  const coverage = latest?.embed_coverage ?? null;
  const entries = Object.entries(coverage ?? {}).sort(([a], [b]) => {
    if (a === "completion") return -1;
    if (b === "completion") return 1;
    return a.localeCompare(b);
  });
  const histRows = ["2", "3", "4", "5"].map((key) => ({ key, value: finiteNumber(latest?.importance_hist?.[key]) ?? 0 }));
  const histMax = Math.max(1, ...histRows.map((row) => row.value));

  return (
    <PanelCard title="6 · HISTOGRAM + EMBED COVERAGE" kicker="completion laggard vs 95% gate" status="info">
      <div className="space-y-2">
        {histRows.map((row) => (
          <div key={row.key} className="grid grid-cols-[2.5rem_1fr_3rem] items-center gap-2 text-xs">
            <span className="font-mono text-text-tertiary">imp {row.key}</span>
            <div className="h-2.5 overflow-hidden rounded-full bg-border">
              <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.max(3, (row.value / histMax) * 100)}%` }} />
            </div>
            <span className="text-right font-mono text-text-primary">{fmtNumber(row.value)}</span>
          </div>
        ))}
      </div>
      <div className="mt-4 space-y-3">
        {entries.length === 0 && <p className="text-xs text-text-tertiary">No embed_coverage data.</p>}
        {entries.map(([kind, item]) => {
          const embedded = finiteNumber(item?.embedded) ?? 0;
          const total = finiteNumber(item?.total) ?? 0;
          const ratio = finiteNumber(item?.ratio) ?? (total > 0 ? embedded / total : null);
          const pct = Math.max(0, Math.min(100, Math.abs(ratio ?? 0) <= 1 ? (ratio ?? 0) * 100 : ratio ?? 0));
          const coverageStatus: Status = pct >= 95 ? "green" : kind === "completion" ? "amber" : "info";
          return (
            <div key={kind}>
              <div className="mb-1 flex items-baseline justify-between gap-2 text-xs">
                <span className="truncate font-semibold text-text-primary">{kind}{kind === "completion" ? " · laggard" : ""}</span>
                <span className="font-mono text-text-secondary">{fmtNumber(embedded)} / {fmtNumber(total)} · {fmtPct(ratio)}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-border">
                <span className="block h-full rounded-full" style={{ width: `${pct}%`, background: STATUS_CFG[coverageStatus].color }} />
              </div>
            </div>
          );
        })}
      </div>
    </PanelCard>
  );
}

function HistoryPanel({ history }: { history: LearningResponse["history_tail"] }) {
  return (
    <PanelCard title="7 · LEARNING HISTORY" kicker="trusted_count + dup_ratio" status="info">
      <LearningSparkline points={history ?? []} />
    </PanelCard>
  );
}

function WeeklyHygienePanel({ weekly }: { weekly: LearningResponse["weekly_hygiene_latest"] }) {
  return (
    <PanelCard title="8 · WEEKLY-HYGIENE DIGEST" kicker="latest hygiene pulse" status={weekly ? "green" : "unknown"}>
      {!weekly ? (
        <p className="rounded-md border border-border bg-background/35 p-3 text-xs text-text-tertiary">Weekly hygiene data unavailable</p>
      ) : (
        <div className="space-y-1">
          <DataRow label="lesson_completion_ratio" value={fmtPct(weekly?.lesson_completion_ratio)} />
          <DataRow label="embedding coverage" value={weekly?.embedding_coverage_summary ?? "—"} />
          <DataRow label="stuck_ready" value={fmtNumber(weekly?.stuck_ready)} />
          <DataRow label="ts" value={fmtTs(weekly?.ts)} />
        </div>
      )}
    </PanelCard>
  );
}

function ThroughputPanel({ distiller }: { distiller: DistillerStatus | null | undefined }) {
  const pending = finiteNumber(distiller?.pending);
  const approved = finiteNumber(distiller?.approved);
  const rejected = finiteNumber(distiller?.rejected);
  const total = (pending ?? 0) + (approved ?? 0) + (rejected ?? 0);
  const approvalRatio = total > 0 && approved !== null ? approved / total : null;
  const rejectionRatio = total > 0 && rejected !== null ? rejected / total : null;
  const sparkPoints = [
    { date: "pending", count: pending ?? 0 },
    { date: "approved", count: approved ?? 0 },
    { date: "rejected", count: rejected ?? 0 },
  ];

  return (
    <PanelCard title="9 · LOOP THROUGHPUT" kicker="velocity, not vibes" status={distiller ? "info" : "unknown"}>
      <div className="space-y-1">
        <DataRow label="scan queue" value={distiller ? `${fmtNumber(total)} items observed` : "TBD"} />
        <DataRow label="promote rate" value={distiller ? fmtPct(approvalRatio) : "TBD"} />
        <DataRow label="reject rate" value={distiller ? fmtPct(rejectionRatio) : "TBD"} />
        <DataRow label="pending pressure" value={distiller ? fmtNumber(pending) : "TBD"} />
      </div>
      {distiller && <div className="mt-3"><Sparkline points={sparkPoints} /></div>}
      <p className="mt-3 text-xs text-text-tertiary">
        Rates are derived from distiller pending/approved/rejected counts until backend exposes real scan/promote/reject deltas.
      </p>
    </PanelCard>
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
          <StatusChip status={status} />
          <button type="button" onClick={onRefresh} aria-label="Refresh learning snapshot" className="flex-shrink-0 rounded-md border border-border p-1.5 text-text-secondary transition hover:text-text-primary">
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>

        <div className="mt-3 flex flex-wrap gap-2" aria-label="Learning metric chips">
          <MetricChip label="SIGNAL_SCORE" value={fmtNumber(latest?.SIGNAL_SCORE, 3)} status={status} onClick={noop} />
          <MetricChip label="ACTIONABLE_SIGNAL_SCORE" value={fmtNumber(latest?.ACTIONABLE_SIGNAL_SCORE, 3)} status="amber" onClick={noop} />
          <MetricChip label="trusted_count" value={fmtNumber(latest?.trusted_count)} status="green" onClick={noop} />
          <MetricChip label="dup_ratio" value={fmtPct(dupRatio)} status={dupRatio > 0.18 ? "amber" : "green"} onClick={noop} />
          <MetricChip label="auto_bridged noise" value={fmtNumber(bridged)} status={bridged > 0 ? "amber" : "green"} onClick={noop} />
          <MetricChip label="quarantine_count" value={fmtNumber(quarantine)} status={quarantine > 0 ? "amber" : "green"} onClick={noop} />
          <MetricChip label="trusted_ratio" value={fmtPct(latest?.trusted_ratio)} status="green" onClick={noop} />
          <MetricChip label="lessons_total" value={fmtNumber(latest?.lessons_total)} status="unknown" onClick={noop} />
        </div>
      </section>

      <div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-3" aria-label="Learning 9-panel cockpit">
        <SignalPanel latest={latest} />
        <CanaryPanel snapshot={snapshot} />
        <RecallPanel filters={snapshot.recall_filters ?? null} latest={latest} />
        <DistillerPanel distiller={snapshot.distiller ?? null} />
        <LoopCriticPanel critic={snapshot.loop_critic ?? null} />
        <ImportanceAndEmbedPanel latest={latest} />
        <HistoryPanel history={history} />
        <WeeklyHygienePanel weekly={snapshot.weekly_hygiene_latest ?? null} />
        <ThroughputPanel distiller={snapshot.distiller ?? null} />
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
