/**
 * OS — one tab that visualizes the entire AI infrastructure.
 *
 * Renders /api/dashboard/os: a hero overall-status row (the <10s diagnosis
 * surface) with consolidated attention/repo/work/activity chips above a
 * toggleable body: the Nexus architecture-flow graph (default) or a responsive
 * grid of expandable section cards. The choice persists in localStorage; both
 * views share the same polled snapshot.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  GitBranch,
  LayoutGrid,
  RefreshCw,
  Server,
  Workflow,
} from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useBelowBreakpoint } from "@nous-research/ui/hooks/use-below-breakpoint";
import { OSNexus } from "@/components/os/OSNexus";
import { GitTopology } from "@/components/os/GitTopology";
import {
  api,
  type OSActivitySnapshot,
  type OSDiagnostic,
  type OSItem,
  type OSSection,
  type OSSnapshot,
  type OSStatus,
  type OSWorkSnapshot,
} from "@/lib/api";

const SNAPSHOT_POLL_MS = 15_000;
const HEADLINE_METRIC_COUNT = 3;
type OSView = "nexus" | "grid" | "git";
const VIEW_STORAGE_KEY = "os-view";

function loadStoredView(): OSView {
  if (typeof window === "undefined") return "nexus";
  try {
    const stored = window.localStorage.getItem(VIEW_STORAGE_KEY);
    if (stored === "grid") return "grid";
    if (stored === "nexus") return "nexus";
    if (stored === "git") return "git";
    return window.innerWidth < 1024 ? "grid" : "nexus";
  } catch {
    return window.innerWidth < 1024 ? "grid" : "nexus";
  }
}

const STATUS_CFG: Record<
  OSStatus,
  { label: string; color: string; dot: string; chip: string; ring: string; soft: string }
> = {
  green: {
    label: "Nominal",
    color: "#4ade80",
    dot: "bg-[#4ade80] shadow-[0_0_7px_#4ade80]",
    chip: "bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)]",
    ring: "rgba(74,222,128,0.35)",
    soft: "rgba(74,222,128,0.08)",
  },
  amber: {
    label: "Degraded",
    color: "#ffbd38",
    dot: "bg-[#ffbd38] shadow-[0_0_7px_#ffbd38]",
    chip: "bg-[rgba(255,189,56,0.12)] text-[#ffbd38] border border-[rgba(255,189,56,0.35)]",
    ring: "rgba(255,189,56,0.45)",
    soft: "rgba(255,189,56,0.07)",
  },
  red: {
    label: "Critical",
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
  info: {
    label: "Info",
    color: "#6b9bd1",
    dot: "bg-[#6b9bd1]",
    chip: "bg-[rgba(107,155,209,0.10)] text-[#6b9bd1] border border-[rgba(107,155,209,0.30)]",
    ring: "rgba(107,155,209,0.30)",
    soft: "rgba(107,155,209,0.06)",
  },
};

const SEVERITY_SCORE: Record<OSStatus, number> = { red: 3, amber: 2, unknown: 1, green: 0, info: 0 };

function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

function headlineItems(items: OSItem[]): OSItem[] {
  return [...items]
    .sort((a, b) => {
      const sev = SEVERITY_SCORE[b.status] - SEVERITY_SCORE[a.status];
      if (sev !== 0) return sev;
      return Number(Boolean(b.metric)) - Number(Boolean(a.metric));
    })
    .slice(0, HEADLINE_METRIC_COUNT);
}

function findSection(snapshot: OSSnapshot, id: string): OSSection | undefined {
  return snapshot.sections.find((section) => section.id === id);
}

function StatusDot({ status, className = "" }: { status: OSStatus; className?: string }) {
  const cfg = STATUS_CFG[status];
  return <span className={`inline-block h-2.5 w-2.5 flex-shrink-0 rounded-full ${cfg.dot} ${className}`} aria-label={cfg.label} />;
}

function StatusChip({ status }: { status: OSStatus }) {
  const cfg = STATUS_CFG[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-[0.12em] ${cfg.chip}`}>
      <StatusDot status={status} className="h-1.5 w-1.5" />
      {cfg.label}
    </span>
  );
}

function MetricChip({
  label,
  value,
  status = "green",
  onClick,
}: {
  label: string;
  value: string | number;
  status?: OSStatus;
  onClick: () => void;
}) {
  const cfg = STATUS_CFG[status];
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex min-h-[44px] min-w-0 max-sm:flex-shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-left text-xs transition hover:bg-accent/30 sm:min-h-0"
      style={{ borderColor: cfg.ring, background: cfg.soft }}
      title={`${label}: ${value}`}
    >
      <StatusDot status={status} className="h-1.5 w-1.5" />
      <span className="max-w-[9rem] truncate text-text-tertiary">{label}</span>
      <span className="font-mono font-semibold text-text-primary">{value}</span>
    </button>
  );
}

function DiagnosticRow({ diag }: { diag: OSDiagnostic }) {
  const cfg = STATUS_CFG[diag.severity];
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-md border px-3 py-2" style={{ borderColor: cfg.ring, background: cfg.soft }}>
      <StatusDot status={diag.severity} className="self-center" />
      <span className="rounded px-1.5 py-0.5 font-mono text-xs font-semibold" style={{ color: cfg.color, background: `${cfg.color}1f` }}>
        {diag.source}
      </span>
      <span className="min-w-0 flex-1 text-xs text-text-primary">
        {diag.message}
        {diag.hint && <span className="text-text-secondary"> — {diag.hint}</span>}
      </span>
    </li>
  );
}

function Sparkline({ points }: { points: Array<{ date?: string; count?: number }> }) {
  // dup of LearningSparkline -- see P6
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

interface HeroProps {
  snapshot: OSSnapshot;
  loading: boolean;
  isMobile: boolean;
  onRefresh: () => void;
  onFocusSection: (id: string) => void;
}

function Hero({ snapshot, loading, isMobile, onRefresh, onFocusSection }: HeroProps) {
  const overall = STATUS_CFG[snapshot.overall];
  const redCount = snapshot.diagnostics.filter((d) => d.severity === "red").length;
  const amberCount = snapshot.diagnostics.length - redCount;
  const repo = snapshot.repo;
  const repoSummary = repo?.summary;
  const work = snapshot.work;
  const workCounts = work?.counts;
  const activity = snapshot.activity;
  const attention = snapshot.attention?.chips ?? [];
  const bestMove = String(repo?.best_move?.text ?? "No git move available");

  return (
    <section className="flex-shrink-0 rounded-lg border bg-card p-4" style={{ borderColor: snapshot.overall === "green" ? undefined : overall.ring }} aria-label="Overall status">
      <div className="flex flex-wrap items-center gap-3">
        <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border" style={{ borderColor: overall.ring, background: overall.soft }}>
          {snapshot.overall === "green" ? <CheckCircle2 className="h-5 w-5" style={{ color: overall.color }} /> : <AlertTriangle className="h-5 w-5" style={{ color: overall.color }} />}
        </span>
        <div className="min-w-[180px] flex-1">
          <h2 className="font-mondwest text-display text-base tracking-[0.12em]" style={{ color: overall.color }}>
            {snapshot.diagnostics.length === 0 ? "All systems nominal" : `${snapshot.diagnostics.length} finding${snapshot.diagnostics.length === 1 ? "" : "s"} — ${redCount} red · ${amberCount} amber`}
          </h2>
          <p className="mt-0.5 text-xs text-text-tertiary">{snapshot.sections.length} cards · updated {fmtTs(snapshot.generated_at)}</p>
        </div>
        <StatusChip status={snapshot.overall} />
        <button type="button" onClick={onRefresh} aria-label="Refresh OS snapshot" className="min-h-[44px] min-w-[44px] flex-shrink-0 rounded-md border border-border p-2.5 text-text-secondary transition hover:text-text-primary sm:min-h-0 sm:min-w-0 sm:p-1.5">
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
        </button>
      </div>

      <div className="mt-3 flex flex-wrap gap-2.5 max-sm:flex-nowrap max-sm:overflow-x-auto sm:gap-2" aria-label="OS consolidated hero chips">
        <MetricChip label="posture" value={STATUS_CFG[snapshot.attention?.posture ?? snapshot.overall].label} status={snapshot.attention?.posture ?? snapshot.overall} onClick={() => onFocusSection("gateway")} />
        {attention.length === 0 ? (
          <MetricChip label="attention" value="0" status="green" onClick={() => onFocusSection("gateway")} />
        ) : attention.map((chip) => (
          <MetricChip key={`${chip.source}-${chip.detail}`} label={chip.label} value="needs eyes" status={chip.status} onClick={() => onFocusSection(chip.section_id)} />
        ))}
        <MetricChip label="git readiness" value={`${repo?.readiness_pct ?? 0}%`} status={findSection(snapshot, "repo")?.status ?? "unknown"} onClick={() => onFocusSection("repo")} />
        <MetricChip label="dirty" value={repoSummary?.total_uncommitted ?? 0} status={(repoSummary?.total_uncommitted ?? 0) > 0 ? "amber" : "green"} onClick={() => onFocusSection("repo")} />
        <MetricChip label="best move" value={bestMove.length > 34 ? `${bestMove.slice(0, 34)}…` : bestMove} status={findSection(snapshot, "repo")?.status ?? "unknown"} onClick={() => onFocusSection("repo")} />
        <MetricChip label="projects" value={`${work?.projects_completion_pct ?? 0}%`} status={findSection(snapshot, "work")?.status ?? "unknown"} onClick={() => onFocusSection("work")} />
        <MetricChip label="decisions" value={workCounts?.decisions ?? 0} status={(workCounts?.decisions ?? 0) > 0 ? "amber" : "green"} onClick={() => onFocusSection("work")} />
        <MetricChip label="live" value={workCounts?.live_runtimes ?? 0} status={(workCounts?.live_runtimes ?? 0) > 0 ? "green" : "amber"} onClick={() => onFocusSection("work")} />
        <MetricChip label="stalled" value={workCounts?.stalled ?? 0} status={(workCounts?.stalled ?? 0) > 0 ? "amber" : "green"} onClick={() => onFocusSection("work")} />
        <MetricChip label="7d tasks" value={activity?.created_7d ?? 0} status={findSection(snapshot, "activity")?.status ?? "unknown"} onClick={() => onFocusSection("activity")} />
      </div>

      {snapshot.diagnostics.length > 0 && (
        <ul className="mt-3 space-y-1.5" aria-label="Diagnostics">
          {snapshot.diagnostics.slice(0, isMobile ? 2 : 4).map((diag, i) => <DiagnosticRow key={`${diag.source}-${i}`} diag={diag} />)}
        </ul>
      )}
    </section>
  );
}

interface SectionCardProps {
  section: OSSection;
  expanded: boolean;
  extra?: OSSnapshot;
  onToggle: () => void;
}

function ItemRow({ item }: { item: OSItem }) {
  const cfg = STATUS_CFG[item.status];
  return (
    <li className="flex items-start gap-2.5 px-4 py-2.5">
      <StatusDot status={item.status} className="mt-1" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="truncate text-xs font-semibold text-text-primary">{item.name}</span>
          {item.metric && <span className="flex-shrink-0 font-mono text-xs" style={{ color: item.status === "green" ? undefined : cfg.color }}>{item.metric}</span>}
        </div>
        {item.detail && <p className="mt-0.5 break-words text-xs leading-relaxed text-text-secondary">{item.detail}</p>}
      </div>
    </li>
  );
}

function WorkDetails({ work }: { work?: OSWorkSnapshot }) {
  const projects = work?.projects ?? [];
  const decisions = work?.decisions ?? [];
  const stalled = work?.stalled ?? [];
  return (
    <div className="space-y-3 border-t border-border px-4 py-3 text-xs text-text-secondary">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {projects.slice(0, 4).map((project) => (
          <div key={project.slug} className="rounded-md border border-border bg-background/40 p-2">
            <div className="flex items-center justify-between gap-2 text-text-primary">
              <span className="truncate font-semibold">{project.icon || "◆"} {project.name}</span>
              <span className="font-mono">{project.completion_pct}%</span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-border">
              <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.max(0, Math.min(100, project.completion_pct || 0))}%` }} />
            </div>
            <div className="mt-1 flex justify-between text-text-tertiary"><span>left {project.remaining_count}</span><span>blocked {project.blocked}</span></div>
          </div>
        ))}
      </div>
      {decisions.length > 0 && <p><strong className="text-text-primary">Decisions:</strong> {decisions.slice(0, 3).map((d) => d.title).join(" · ")}</p>}
      {stalled.length > 0 && <p><strong className="text-warning">Stalled:</strong> {stalled.slice(0, 3).map((s) => `${s.project}: ${s.title}`).join(" · ")}</p>}
    </div>
  );
}

function ActivityDetails({ activity }: { activity?: OSActivitySnapshot }) {
  return (
    <div className="space-y-3 border-t border-border px-4 py-3 text-xs text-text-secondary">
      <Sparkline points={activity?.queue_7d?.points ?? []} />
      <div className="grid grid-cols-2 gap-2">
        <span className="rounded-md border border-border p-2">Open now <strong className="text-text-primary">{activity?.open_now ?? 0}</strong></span>
        <span className="rounded-md border border-border p-2">Queued cards <strong className="text-text-primary">{activity?.cards?.length ?? 0}</strong></span>
      </div>
      {(activity?.cards?.length ?? 0) > 0 && <p>{activity?.cards?.slice(0, 4).map((card) => `${card.board}:${card.title}`).join(" · ")}</p>}
    </div>
  );
}

function RepoDetails({ snapshot }: { snapshot: OSSnapshot }) {
  const repo = snapshot.repo;
  const lanes = repo?.lanes ?? [];
  const rows = repo?.rows ?? [];
  return (
    <div className="space-y-2 border-t border-border px-4 py-3 text-xs text-text-secondary">
      <p><strong className="text-text-primary">Recommended:</strong> {repo?.best_move?.text ?? "No git move available"}</p>
      <div className="grid grid-cols-2 gap-2">
        <span className="rounded-md border border-border p-2">Rows <strong className="text-text-primary">{rows.length}</strong></span>
        <span className="rounded-md border border-border p-2">Lanes <strong className="text-text-primary">{lanes.length}</strong></span>
      </div>
    </div>
  );
}

function SectionCard({ section, expanded, extra, onToggle }: SectionCardProps) {
  const cfg = STATUS_CFG[section.status];
  const attention = section.status === "red" || section.status === "amber";
  const headline = headlineItems(section.items);

  return (
    <div id={`os-card-${section.id}`} className="flex scroll-mt-24 flex-col self-start overflow-hidden rounded-lg border bg-card" style={{ borderColor: attention ? cfg.ring : undefined }}>
      <button type="button" onClick={onToggle} aria-expanded={expanded} className="flex w-full items-center gap-2.5 px-4 py-3 text-left transition hover:bg-accent/30">
        <StatusDot status={section.status} />
        <span className="font-mondwest text-display min-w-0 flex-1 truncate text-xs tracking-[0.16em] text-text-primary">{section.label}</span>
        {attention && <StatusChip status={section.status} />}
        {expanded ? <ChevronUp className="h-3.5 w-3.5 flex-shrink-0 text-text-tertiary" /> : <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-text-tertiary" />}
      </button>

      {!expanded && (
        <div className="space-y-1 px-4 pb-3">
          {headline.length === 0 && <p className="text-xs text-text-tertiary">No probes reported.</p>}
          {headline.map((item) => (
            <div key={item.name} className="flex items-baseline justify-between gap-2 text-xs">
              <span className="flex items-baseline gap-1.5 truncate text-text-tertiary">{item.status !== "green" && <StatusDot status={item.status} className="h-1.5 w-1.5 self-center" />}{item.name}</span>
              <span className="flex-shrink-0 truncate font-mono text-text-secondary" style={{ color: item.status === "green" || item.status === "unknown" ? undefined : STATUS_CFG[item.status].color }}>{item.metric ?? STATUS_CFG[item.status].label}</span>
            </div>
          ))}
          {section.items.length > headline.length && <p className="pt-0.5 text-xs text-text-tertiary">+{section.items.length - headline.length} more…</p>}
        </div>
      )}

      {expanded && (
        <>
          <ul className="divide-y divide-border border-t border-border">
            {section.items.length === 0 && <li className="px-4 py-2.5 text-xs text-text-tertiary">No probes reported.</li>}
            {section.items.map((item) => <ItemRow key={item.name} item={item} />)}
          </ul>
          {section.id === "repo" && extra && <RepoDetails snapshot={extra} />}
          {section.id === "work" && <WorkDetails work={extra?.work} />}
          {section.id === "activity" && <ActivityDetails activity={extra?.activity} />}
        </>
      )}
    </div>
  );
}

export default function OSPage() {
  const { setTitle } = usePageHeader();
  const isMobile = useBelowBreakpoint(1024);
  const [snapshot, setSnapshot] = useState<OSSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [view, setViewState] = useState<OSView>(loadStoredView);

  const setView = useCallback((next: OSView) => {
    setViewState(next);
    try {
      window.localStorage.setItem(VIEW_STORAGE_KEY, next);
    } catch {
      // view still switches for the session
    }
  }, []);

  useEffect(() => { setTitle("OS"); }, [setTitle]);

  const loadSnapshot = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getOSSnapshot();
      setSnapshot(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "OS snapshot unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSnapshot();
    const timer = window.setInterval(() => void loadSnapshot(), SNAPSHOT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadSnapshot]);

  const toggleExpand = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const focusSection = useCallback((id: string) => {
    setView("grid");
    setExpanded((prev) => new Set(prev).add(id));
    window.setTimeout(() => document.getElementById(`os-card-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  }, [setView]);

  const coreFirstSections = useMemo(() => {
    if (!snapshot) return [];
    const preferred = ["repo", "work", "activity"];
    return [...snapshot.sections].sort((a, b) => {
      const ai = preferred.indexOf(a.id);
      const bi = preferred.indexOf(b.id);
      if (ai !== -1 || bi !== -1) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
      return 0;
    });
  }, [snapshot]);

  if (!snapshot && !error) {
    return <div className="flex min-h-[300px] items-center justify-center bg-background text-sm text-text-secondary"><RefreshCw className="mr-2 h-4 w-4 animate-spin" />Loading OS…</div>;
  }

  if (!snapshot && error) {
    return (
      <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 bg-background">
        <p className="text-sm text-destructive">{error}</p>
        <button type="button" onClick={() => void loadSnapshot()} className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary transition hover:text-text-primary">Retry</button>
      </div>
    );
  }

  const data = snapshot as OSSnapshot;

  return (
    <div className={`bg-background p-4 text-text-primary ${view === "nexus" ? "flex min-h-[540px] flex-col lg:h-[calc(100dvh-112px)]" : "min-h-0"}`}>
      {/* 152/160/112px offsets are top nav + page header + p-4 shell padding; lg drops the mobile bottom-nav height. */}
      <div className="mb-3 flex flex-shrink-0 flex-wrap items-center gap-x-1.5 gap-y-2 text-xs text-text-tertiary">
        <Server className="h-3.5 w-3.5" />
        <span className="font-mondwest text-display tracking-[0.16em]">Infrastructure Operating Status</span>
        {error && <span className="min-w-0 truncate text-warning">· refresh failed: {error}</span>}
        <div className="ml-auto flex flex-shrink-0 overflow-hidden rounded-md border border-border" role="group" aria-label="OS view">
          <button type="button" onClick={() => setView("nexus")} aria-pressed={view === "nexus"} className={`flex min-h-[44px] items-center gap-1.5 px-2.5 py-1 text-xs font-semibold transition sm:min-h-0 ${view === "nexus" ? "bg-accent/40 text-text-primary" : "text-text-tertiary hover:text-text-primary"}`}><Workflow className="h-3.5 w-3.5" />Nexus</button>
          <button type="button" onClick={() => setView("grid")} aria-pressed={view === "grid"} className={`flex min-h-[44px] items-center gap-1.5 border-l border-border px-2.5 py-1 text-xs font-semibold transition sm:min-h-0 ${view === "grid" ? "bg-accent/40 text-text-primary" : "text-text-tertiary hover:text-text-primary"}`}><LayoutGrid className="h-3.5 w-3.5" />Grid</button><button type="button" onClick={() => setView("git")} aria-pressed={view === "git"} className={`flex min-h-[44px] items-center gap-1.5 border-l border-border px-2.5 py-1 text-xs font-semibold transition sm:min-h-0 ${view === "git" ? "bg-accent/40 text-text-primary" : "text-text-tertiary hover:text-text-primary"}`}><GitBranch className="h-3.5 w-3.5" />Git</button>
        </div>
      </div>

      <Hero snapshot={data} loading={loading} isMobile={isMobile} onRefresh={() => void loadSnapshot()} onFocusSection={focusSection} />

      {view === "nexus" ? (
        <div className="mt-3 h-[70vh] min-h-[420px] min-w-0 lg:h-auto lg:flex-1"><OSNexus snapshot={data} /></div>
      ) : view === "git" ? (
        <GitTopology snapshot={data} />
      ) : (
        <>
          <div className="mt-4 grid grid-cols-1 items-start gap-3 pb-8 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
            {coreFirstSections.map((section) => <SectionCard key={section.id} section={section} expanded={expanded.has(section.id)} extra={data} onToggle={() => toggleExpand(section.id)} />)}
          </div>
          {data.sections.length === 0 && <div className="flex min-h-[160px] items-center justify-center text-sm text-text-tertiary">No sections in snapshot.</div>}
        </>
      )}
    </div>
  );
}
