import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw, ShieldCheck, Sparkles } from "lucide-react";
import type { LearningResponse } from "@/lib/api";
import { STATUS_CFG, fmtTs, type Status } from "@/components/StatusKit/constants";
import { MetricChip } from "@/components/StatusKit/MetricChip";
import { StatusChip } from "@/components/StatusKit/StatusChip";
import { StatusDot } from "@/components/StatusKit/StatusDot";

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

function fmtBool(value: unknown): string {
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

function basename(path?: string | null): string {
  if (!path) return "—";
  return path.split("/").filter(Boolean).slice(-3).join("/");
}

function noop(): void {
  // MetricChip is button-shaped in StatusKit for OS focus affordances; this panel is read-only.
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
      <span className={`${emphasis ? "text-sm" : "text-xs"} max-w-[62%] text-right font-mono text-text-primary`}>{value}</span>
    </div>
  );
}

function Provenance({ source, text }: { source?: string | null; text?: string | null }) {
  return (
    <p className="mt-3 rounded-md border border-border bg-background/35 p-2 text-[11px] leading-relaxed text-text-tertiary">
      source: <span className="font-mono text-text-secondary">{basename(source)}</span>
      {text ? <> · {text}</> : null}
    </p>
  );
}

function Unmeasured({ source }: { source?: string | null }) {
  return (
    <div className="rounded-md border border-[rgba(255,189,56,0.45)] bg-[rgba(255,189,56,0.08)] p-3 text-xs text-warning">
      unmeasured — no reliable artifact found{source ? <> at <span className="font-mono">{basename(source)}</span></> : null}
    </div>
  );
}

function RecallEvalPanel({ recallEval }: { recallEval: LearningResponse["recall_eval"] }) {
  const measured = recallEval?.status === "measured";
  const recall = recallEval?.recall_at_k;
  const self = recallEval?.self_seeded_latest;
  return (
    <PanelCard title="1 · RECALL@K" kicker="blind held-out, not vanity" status={measured ? "green" : "amber"}>
      {!measured ? <Unmeasured source={recallEval?.source} /> : (
        <>
          <div className="rounded-md border border-[rgba(69,211,137,0.35)] bg-[rgba(69,211,137,0.08)] p-3">
            <p className="text-[11px] uppercase tracking-[0.14em] text-text-tertiary">{recallEval?.label ?? "blind held-out RECALL@K"}</p>
            <p className="mt-1 font-mono text-4xl font-semibold text-text-primary">{fmtNumber(recall, 4)}</p>
            <p className="mt-1 text-xs text-text-secondary">{fmtPct(recall)} · n={fmtNumber(recallEval?.n)} · k={fmtNumber(recallEval?.k)}</p>
          </div>
          <div className="mt-3 space-y-1">
            <DataRow label="MRR / nDCG" value={`${fmtNumber(recallEval?.mrr, 4)} / ${fmtNumber(recallEval?.ndcg_at_k, 4)}`} />
            <DataRow label="holdout" value={basename(recallEval?.holdout_file)} />
            <DataRow label="measured at" value={fmtTs(recallEval?.ts)} />
            <DataRow label="self-seeded contrast" value={self ? `${fmtNumber(self.recall_at_k, 4)} (${basename(self.holdout_file)})` : "—"} />
          </div>
          <Provenance source={recallEval?.source} text={recallEval?.provenance} />
        </>
      )}
    </PanelCard>
  );
}

function RecallActivityPanel({ activity }: { activity: LearningResponse["recall_activity"] }) {
  const measured = activity?.status === "measured";
  return (
    <PanelCard title="2 · RECALL ACTIVITY" kicker="actual recall-events.jsonl" status={measured ? "green" : "amber"}>
      {!measured ? <Unmeasured source={activity?.source} /> : (
        <>
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-md border border-border bg-background/35 p-3"><p className="text-[11px] text-text-tertiary">1h</p><p className="font-mono text-2xl">{fmtNumber(activity?.recent_1h)}</p></div>
            <div className="rounded-md border border-border bg-background/35 p-3"><p className="text-[11px] text-text-tertiary">24h</p><p className="font-mono text-2xl">{fmtNumber(activity?.recent_24h)}</p></div>
            <div className="rounded-md border border-border bg-background/35 p-3"><p className="text-[11px] text-text-tertiary">7d</p><p className="font-mono text-2xl">{fmtNumber(activity?.recent_7d)}</p></div>
          </div>
          <div className="mt-3 space-y-1">
            <DataRow label="total hand-count" value={fmtNumber(activity?.total_events)} />
            <DataRow label="freshness" value={`${fmtDuration(activity?.latest_age_seconds)} ago`} />
            <DataRow label="latest" value={fmtTs(activity?.latest_at)} />
            <DataRow label="sources" value={(activity?.sources ?? []).join(", ") || "—"} />
          </div>
          <Provenance source={activity?.source} text={activity?.provenance} />
        </>
      )}
    </PanelCard>
  );
}

function PromotionPanel({ promotion }: { promotion: LearningResponse["promotion"] }) {
  const measured = promotion?.status === "measured";
  const latest = promotion?.latest ?? {};
  return (
    <PanelCard title="3 · PROMOTION" kicker="learning-loop-promote.timer" status={measured ? "green" : "amber"}>
      {!measured ? <Unmeasured source={promotion?.source} /> : (
        <>
          <div className="space-y-1">
            <DataRow label="timer" value={`${promotion?.timer?.enabled ?? "—"} / ${promotion?.timer?.active ?? "—"}`} />
            <DataRow label="service" value={promotion?.service?.active ?? "—"} />
            <DataRow label="processed" value={fmtNumber(latest.processed)} emphasis />
            <DataRow label="store_writes" value={fmtNumber(latest.store_writes)} />
            <DataRow label="queue_sha256" value={<span className="break-all">{latest.queue_sha256 ?? "—"}</span>} />
            <DataRow label="at" value={fmtTs(typeof latest.at === "string" ? latest.at : null)} />
          </div>
          <Provenance source={promotion?.source} text={promotion?.provenance} />
        </>
      )}
    </PanelCard>
  );
}

function VerifyPanel({ verify }: { verify: LearningResponse["verify"] }) {
  const measured = verify?.status === "measured";
  return (
    <PanelCard title="4 · VERIFY" kicker="learning-verify.timer verdict" status={verify?.critic_status === "PASS" ? "green" : measured ? "amber" : "amber"}>
      {!measured ? <Unmeasured source={verify?.source} /> : (
        <>
          <div className="space-y-1">
            <DataRow label="timer" value={`${verify?.timer?.enabled ?? "—"} / ${verify?.timer?.active ?? "—"}`} />
            <DataRow label="critic" value={`${verify?.critic_status ?? "—"} · hard_failures=${fmtNumber(verify?.hard_failures)}`} emphasis />
            <DataRow label="default verify recall" value={`${fmtNumber(verify?.default_recall_at_k, 4)} @${fmtNumber(verify?.default_recall_k)}`} />
            <DataRow label="default MRR / nDCG" value={`${fmtNumber(verify?.default_mrr, 4)} / ${fmtNumber(verify?.default_ndcg, 4)}`} />
          </div>
          <p className="mt-3 rounded-md border border-[rgba(255,189,56,0.35)] bg-[rgba(255,189,56,0.07)] p-2 text-xs text-warning">
            Verify log's default recall can be the older default holdout. The headline RECALL@K above is the blind held-out number.
          </p>
          <Provenance source={verify?.source} text={verify?.provenance} />
        </>
      )}
    </PanelCard>
  );
}

function LessonsPanel({ lessons }: { lessons: LearningResponse["mvms_lessons"] }) {
  const measured = lessons?.status === "measured";
  return (
    <PanelCard title="5 · MVMS LESSONS" kicker="durable lesson store" status={measured ? "info" : "amber"}>
      {!measured ? <Unmeasured source={lessons?.source} /> : (
        <>
          <div className="space-y-1">
            <DataRow label="lessons_total" value={fmtNumber(lessons?.lessons_total)} emphasis />
            <DataRow label="trusted_count / ratio" value={`${fmtNumber(lessons?.trusted_count)} / ${fmtPct(lessons?.trusted_ratio)}`} />
            <DataRow label="actionable_total / trusted" value={`${fmtNumber(lessons?.actionable_lessons_total)} / ${fmtPct(lessons?.trusted_actionable_ratio)}`} />
            <DataRow label="auto_bridged excluded" value={fmtNumber(lessons?.auto_bridged_count)} />
            <DataRow label="quarantine excluded" value={fmtNumber(lessons?.quarantine_count)} />
            <DataRow label="dup_ratio" value={fmtPct(lessons?.dup_ratio)} />
          </div>
          <Provenance source={lessons?.source} text={lessons?.provenance} />
        </>
      )}
    </PanelCard>
  );
}

function CanaryPanel({ canary }: { canary: LearningResponse["canary"] }) {
  const measured = canary?.status === "measured";
  const status: Status = canary?.pass === true ? "green" : measured ? "red" : "amber";
  return (
    <PanelCard title="6 · CANARY" kicker="production recall trap" status={status}>
      {!measured ? <Unmeasured source={canary?.source} /> : (
        <>
          <div className="flex items-center gap-2">
            <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border" style={{ borderColor: STATUS_CFG[status].ring, color: STATUS_CFG[status].color }}>
              {canary?.pass === true ? <ShieldCheck className="h-5 w-5" /> : <AlertTriangle className="h-5 w-5" />}
            </span>
            <div>
              <p className="font-mondwest text-display text-sm tracking-[0.14em]" style={{ color: STATUS_CFG[status].color }}>Canary {canary?.pass === true ? "passed" : "failed"}</p>
              <p className="text-xs text-text-tertiary">rank <span className="font-mono text-text-primary">{fmtNumber(canary?.rank)}</span></p>
            </div>
          </div>
          <div className="mt-3 space-y-1">
            <DataRow label="recalled" value={fmtBool(canary?.recalled)} />
            <DataRow label="avoided_mistake" value={fmtBool(canary?.avoided_mistake)} />
            <DataRow label="ranker" value={canary?.probe_ranker ?? "—"} />
            <DataRow label="mode" value={canary?.probe_mode_used ?? "—"} />
            <DataRow label="ts" value={fmtTs(canary?.ts)} />
          </div>
          <Provenance source={canary?.source} text={canary?.provenance} />
        </>
      )}
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
  const errors = snapshot.errors ?? [];
  const recall = snapshot.recall_eval?.recall_at_k;
  const activity = snapshot.recall_activity;
  const lessons = snapshot.mvms_lessons;

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
              Learning status · {status === "green" ? "honest/live" : "partial/unmeasured"}
            </h2>
            <p className="mt-0.5 text-xs text-text-tertiary">live file-backed recall, promotion, verify, and MVMS lesson metrics — no simulated tiles</p>
          </div>
          <StatusChip status={status} />
          <button type="button" onClick={onRefresh} aria-label="Refresh learning snapshot" className="flex-shrink-0 rounded-md border border-border p-1.5 text-text-secondary transition hover:text-text-primary">
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>

        <div className="mt-3 flex flex-wrap gap-2" aria-label="Learning metric chips">
          <MetricChip label="blind RECALL@10" value={fmtNumber(recall, 4)} status={snapshot.recall_eval?.status === "measured" ? "green" : "amber"} onClick={noop} />
          <MetricChip label="recall events 24h" value={fmtNumber(activity?.recent_24h)} status={activity?.status === "measured" ? "green" : "amber"} onClick={noop} />
          <MetricChip label="latest recall age" value={fmtDuration(activity?.latest_age_seconds)} status="info" onClick={noop} />
          <MetricChip label="lessons_total" value={fmtNumber(lessons?.lessons_total)} status="info" onClick={noop} />
          <MetricChip label="trusted_ratio" value={fmtPct(lessons?.trusted_ratio)} status="info" onClick={noop} />
          <MetricChip label="promote timer" value={snapshot.promotion?.timer?.active ?? "—"} status={snapshot.promotion?.status === "measured" ? "green" : "amber"} onClick={noop} />
          <MetricChip label="verify critic" value={snapshot.verify?.critic_status ?? "—"} status={snapshot.verify?.critic_status === "PASS" ? "green" : "amber"} onClick={noop} />
        </div>
      </section>

      <div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-3" aria-label="Learning live cockpit">
        <RecallEvalPanel recallEval={snapshot.recall_eval} />
        <RecallActivityPanel activity={snapshot.recall_activity} />
        <PromotionPanel promotion={snapshot.promotion} />
        <VerifyPanel verify={snapshot.verify} />
        <LessonsPanel lessons={snapshot.mvms_lessons} />
        <CanaryPanel canary={snapshot.canary} />
      </div>

      {errors.length > 0 && (
        <div className="mt-3 rounded-lg border border-[rgba(255,189,56,0.35)] bg-[rgba(255,189,56,0.07)] p-3 text-xs text-warning">
          {errors.map((item, index) => <p key={`${item}-${index}`}>{item}</p>)}
        </div>
      )}
    </div>
  );
}
