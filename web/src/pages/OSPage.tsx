/**
 * OS — one tab that visualizes the entire AI infrastructure.
 *
 * Renders /api/dashboard/os: a hero overall-status row (the <10s diagnosis
 * surface — "All systems nominal" when clean, otherwise the red→amber
 * diagnostics list) above a toggleable body: the Nexus architecture-flow
 * graph (default) or a responsive grid of 8 expandable section cards. The
 * choice persists in localStorage; both views share the same polled snapshot.
 */

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  LayoutGrid,
  RefreshCw,
  Server,
  Workflow,
} from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { OSNexus } from "@/components/os/OSNexus";
import {
  api,
  type OSDiagnostic,
  type OSItem,
  type OSSection,
  type OSSnapshot,
  type OSStatus,
} from "@/lib/api";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SNAPSHOT_POLL_MS = 15_000;
const HEADLINE_METRIC_COUNT = 3;

type OSView = "nexus" | "grid";
const VIEW_STORAGE_KEY = "os-view";

function loadStoredView(): OSView {
  try {
    return window.localStorage.getItem(VIEW_STORAGE_KEY) === "grid"
      ? "grid"
      : "nexus";
  } catch {
    return "nexus";
  }
}

/** Status visual configuration — shared dashboard status palette. */
const STATUS_CFG: Record<
  OSStatus,
  { label: string; color: string; dot: string; chip: string; ring: string; soft: string }
> = {
  green: {
    label: "Nominal",
    color: "#4ade80",
    dot: "bg-[#4ade80]",
    chip: "bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)]",
    ring: "rgba(74,222,128,0.35)",
    soft: "rgba(74,222,128,0.08)",
  },
  amber: {
    label: "Degraded",
    color: "#ffbd38",
    dot: "bg-[#ffbd38] shadow-[0_0_6px_#ffbd38]",
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
};

const SEVERITY_SCORE: Record<OSStatus, number> = {
  red: 3,
  amber: 2,
  unknown: 1,
  green: 0,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

/** Worst-first headline rows for a collapsed card (sort is stable, so API order breaks ties). */
function headlineItems(items: OSItem[]): OSItem[] {
  return [...items]
    .sort((a, b) => {
      const sev = SEVERITY_SCORE[b.status] - SEVERITY_SCORE[a.status];
      if (sev !== 0) return sev;
      return Number(Boolean(b.metric)) - Number(Boolean(a.metric));
    })
    .slice(0, HEADLINE_METRIC_COUNT);
}

// ---------------------------------------------------------------------------
// Status atoms
// ---------------------------------------------------------------------------

function StatusDot({ status, className = "" }: { status: OSStatus; className?: string }) {
  const cfg = STATUS_CFG[status];
  return (
    <span
      className={`inline-block h-2.5 w-2.5 flex-shrink-0 rounded-full ${cfg.dot} ${className}`}
      aria-label={cfg.label}
    />
  );
}

function StatusChip({ status }: { status: OSStatus }) {
  const cfg = STATUS_CFG[status];
  return (
    <span
      className={`flex-shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold ${cfg.chip}`}
    >
      {cfg.label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Hero — overall status + diagnostics (the <10s diagnosis surface)
// ---------------------------------------------------------------------------

interface HeroProps {
  snapshot: OSSnapshot;
  loading: boolean;
  onRefresh: () => void;
}

function DiagnosticRow({ diag }: { diag: OSDiagnostic }) {
  const cfg = STATUS_CFG[diag.severity];
  return (
    <li
      className="flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-md border px-3 py-2"
      style={{ borderColor: cfg.ring, background: cfg.soft }}
    >
      <StatusDot status={diag.severity} className="self-center" />
      <span
        className="rounded px-1.5 py-0.5 font-mono text-xs font-semibold"
        style={{ color: cfg.color, background: `${cfg.color}1f` }}
      >
        {diag.source}
      </span>
      <span className="min-w-0 flex-1 text-xs text-text-primary">
        {diag.message}
        {diag.hint && (
          <span className="text-text-secondary"> — {diag.hint}</span>
        )}
      </span>
    </li>
  );
}

function Hero({ snapshot, loading, onRefresh }: HeroProps) {
  const overall = STATUS_CFG[snapshot.overall];
  const clean = snapshot.diagnostics.length === 0;
  const redCount = snapshot.diagnostics.filter((d) => d.severity === "red").length;
  const amberCount = snapshot.diagnostics.length - redCount;

  return (
    <section
      className="rounded-lg border bg-card p-4"
      style={{ borderColor: snapshot.overall === "green" ? undefined : overall.ring }}
      aria-label="Overall status"
    >
      <div className="flex items-center gap-3">
        <span
          className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border"
          style={{ borderColor: overall.ring, background: overall.soft }}
        >
          {clean ? (
            <CheckCircle2 className="h-5 w-5" style={{ color: overall.color }} />
          ) : (
            <AlertTriangle className="h-5 w-5" style={{ color: overall.color }} />
          )}
        </span>

        <div className="min-w-0 flex-1">
          <h2
            className="font-mondwest text-display text-base tracking-[0.12em]"
            style={{ color: overall.color }}
          >
            {clean
              ? "All systems nominal"
              : `${snapshot.diagnostics.length} finding${snapshot.diagnostics.length === 1 ? "" : "s"} — ${redCount} red · ${amberCount} amber`}
          </h2>
          <p className="mt-0.5 text-xs text-text-tertiary">
            {snapshot.sections.length} sections probed · updated {fmtTs(snapshot.generated_at)}
          </p>
        </div>

        <StatusChip status={snapshot.overall} />

        <button
          type="button"
          onClick={onRefresh}
          aria-label="Refresh OS snapshot"
          className="flex-shrink-0 rounded-md border border-border p-1.5 text-text-secondary transition hover:text-text-primary"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
        </button>
      </div>

      {!clean && (
        <ul className="mt-3 space-y-1.5" aria-label="Diagnostics">
          {snapshot.diagnostics.map((diag, i) => (
            <DiagnosticRow key={`${diag.source}-${i}`} diag={diag} />
          ))}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Section card — status dot + headline metrics, click to expand full items
// ---------------------------------------------------------------------------

interface SectionCardProps {
  section: OSSection;
  expanded: boolean;
  onToggle: () => void;
}

function ItemRow({ item }: { item: OSItem }) {
  const cfg = STATUS_CFG[item.status];
  return (
    <li className="flex items-start gap-2.5 px-4 py-2.5">
      <StatusDot status={item.status} className="mt-1" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="truncate text-xs font-semibold text-text-primary">
            {item.name}
          </span>
          {item.metric && (
            <span
              className="flex-shrink-0 font-mono text-xs"
              style={{ color: item.status === "green" ? undefined : cfg.color }}
            >
              {item.metric}
            </span>
          )}
        </div>
        {item.detail && (
          <p className="mt-0.5 break-words text-xs leading-relaxed text-text-secondary">
            {item.detail}
          </p>
        )}
      </div>
    </li>
  );
}

function SectionCard({ section, expanded, onToggle }: SectionCardProps) {
  const cfg = STATUS_CFG[section.status];
  const attention = section.status === "red" || section.status === "amber";
  const headline = headlineItems(section.items);

  return (
    <div
      className="flex flex-col self-start overflow-hidden rounded-lg border bg-card"
      style={{ borderColor: attention ? cfg.ring : undefined }}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2.5 px-4 py-3 text-left transition hover:bg-accent/30"
      >
        <StatusDot status={section.status} />
        <span className="font-mondwest text-display min-w-0 flex-1 truncate text-xs tracking-[0.12em] text-text-primary">
          {section.label}
        </span>
        {attention && <StatusChip status={section.status} />}
        {expanded ? (
          <ChevronUp className="h-3.5 w-3.5 flex-shrink-0 text-text-tertiary" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-text-tertiary" />
        )}
      </button>

      {/* Headline metrics — always visible */}
      {!expanded && (
        <div className="space-y-1 px-4 pb-3">
          {headline.length === 0 && (
            <p className="text-xs text-text-tertiary">No probes reported.</p>
          )}
          {headline.map((item) => (
            <div
              key={item.name}
              className="flex items-baseline justify-between gap-2 text-xs"
            >
              <span className="flex items-baseline gap-1.5 truncate text-text-tertiary">
                {item.status !== "green" && (
                  <StatusDot status={item.status} className="h-1.5 w-1.5 self-center" />
                )}
                {item.name}
              </span>
              <span
                className="flex-shrink-0 truncate font-mono text-text-secondary"
                style={{
                  color: item.status === "green" || item.status === "unknown"
                    ? undefined
                    : STATUS_CFG[item.status].color,
                }}
              >
                {item.metric ?? STATUS_CFG[item.status].label}
              </span>
            </div>
          ))}
          {section.items.length > headline.length && (
            <p className="pt-0.5 text-xs text-text-tertiary">
              +{section.items.length - headline.length} more…
            </p>
          )}
        </div>
      )}

      {/* Expanded — full item list */}
      {expanded && (
        <ul className="divide-y divide-border border-t border-border">
          {section.items.length === 0 && (
            <li className="px-4 py-2.5 text-xs text-text-tertiary">
              No probes reported.
            </li>
          )}
          {section.items.map((item) => (
            <ItemRow key={item.name} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// OSPage
// ---------------------------------------------------------------------------

export default function OSPage() {
  const { setTitle } = usePageHeader();

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
      // Storage unavailable (private mode) — view still switches for the session.
    }
  }, []);

  useEffect(() => {
    setTitle("OS");
  }, [setTitle]);

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

  // ------------------------------------------------------------------ Loading
  if (!snapshot && !error) {
    return (
      <div className="flex min-h-[300px] items-center justify-center bg-background text-sm text-text-secondary">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
        Loading OS…
      </div>
    );
  }

  // ------------------------------------------------------------------- Error
  if (!snapshot && error) {
    return (
      <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 bg-background">
        <p className="text-sm text-destructive">{error}</p>
        <button
          type="button"
          onClick={() => void loadSnapshot()}
          className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary transition hover:text-text-primary"
        >
          Retry
        </button>
      </div>
    );
  }

  const data = snapshot as OSSnapshot;

  // ------------------------------------------------------------------ Content
  return (
    <div className="min-h-0 bg-background p-4 text-text-primary">
      {/* Page chrome */}
      <div className="mb-3 flex items-center gap-1.5 text-xs text-text-tertiary">
        <Server className="h-3.5 w-3.5" />
        <span className="font-mondwest text-display tracking-[0.16em]">
          Infrastructure Operating Status
        </span>
        {error && (
          <span className="text-warning">· refresh failed: {error}</span>
        )}

        {/* View toggle — Nexus (architecture flow) | Grid (section cards) */}
        <div
          className="ml-auto flex flex-shrink-0 overflow-hidden rounded-md border border-border"
          role="group"
          aria-label="OS view"
        >
          <button
            type="button"
            onClick={() => setView("nexus")}
            aria-pressed={view === "nexus"}
            className={`flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold transition ${
              view === "nexus"
                ? "bg-accent/40 text-text-primary"
                : "text-text-tertiary hover:text-text-primary"
            }`}
          >
            <Workflow className="h-3.5 w-3.5" />
            Nexus
          </button>
          <button
            type="button"
            onClick={() => setView("grid")}
            aria-pressed={view === "grid"}
            className={`flex items-center gap-1.5 border-l border-border px-2.5 py-1 text-xs font-semibold transition ${
              view === "grid"
                ? "bg-accent/40 text-text-primary"
                : "text-text-tertiary hover:text-text-primary"
            }`}
          >
            <LayoutGrid className="h-3.5 w-3.5" />
            Grid
          </button>
        </div>
      </div>

      {/* Hero stays visible on BOTH views — it is the <10s diagnosis surface */}
      <Hero snapshot={data} loading={loading} onRefresh={() => void loadSnapshot()} />

      {view === "nexus" ? (
        /* Nexus — architecture-flow graph over the same polled snapshot */
        <div className="mt-4 h-[calc(100dvh-300px)] min-h-[480px] pb-8">
          <OSNexus snapshot={data} />
        </div>
      ) : (
        <>
          {/* Section grid */}
          <div className="mt-4 grid grid-cols-1 items-start gap-3 pb-8 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
            {data.sections.map((section) => (
              <SectionCard
                key={section.id}
                section={section}
                expanded={expanded.has(section.id)}
                onToggle={() => toggleExpand(section.id)}
              />
            ))}
          </div>

          {data.sections.length === 0 && (
            <div className="flex min-h-[160px] items-center justify-center text-sm text-text-tertiary">
              No sections in snapshot.
            </div>
          )}
        </>
      )}
    </div>
  );
}
