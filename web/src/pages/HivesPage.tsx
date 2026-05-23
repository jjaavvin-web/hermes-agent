import { useCallback, useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  Clock,
  FileText,
  FolderOpen,
  RefreshCw,
  TerminalSquare,
  Workflow,
} from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api, type HiveRun, type HivesSnapshot, type HiveLogResponse } from "@/lib/api";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SNAPSHOT_POLL_MS = 15_000;
const LOG_POLL_MS = 5_000;
const LOG_TAIL_LINES = 80;

// Status visual configuration
type HiveStatus = HiveRun["status"];

const STATUS_CFG: Record<
  HiveStatus,
  { label: string; color: string; dot: string; chip: string }
> = {
  running: {
    label: "Running",
    color: "#4ade80",
    dot: "bg-[#4ade80] shadow-[0_0_6px_#4ade80]",
    chip: "bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)]",
  },
  completed: {
    label: "Completed",
    color: "#7c91a8",
    dot: "bg-[#7c91a8]",
    chip: "bg-[rgba(124,145,168,0.10)] text-[#7c91a8] border border-[rgba(124,145,168,0.3)]",
  },
  blocked: {
    label: "Blocked",
    color: "#fb2c36",
    dot: "bg-[#fb2c36] shadow-[0_0_5px_#fb2c36]",
    chip: "bg-[rgba(251,44,54,0.12)] text-[#fb2c36] border border-[rgba(251,44,54,0.35)]",
  },
  stale: {
    label: "Stale",
    color: "#ffbd38",
    dot: "bg-[#ffbd38]",
    chip: "bg-[rgba(255,189,56,0.10)] text-[#ffbd38] border border-[rgba(255,189,56,0.3)]",
  },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
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

// ---------------------------------------------------------------------------
// HiveCard
// ---------------------------------------------------------------------------

interface HiveCardProps {
  hive: HiveRun;
  expanded: boolean;
  onToggle: () => void;
}

function HiveCard({ hive, expanded, onToggle }: HiveCardProps) {
  const cfg = STATUS_CFG[hive.status];

  const [log, setLog] = useState<HiveLogResponse | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);
  const logIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const logAreaRef = useRef<HTMLDivElement | null>(null);

  const fetchLog = useCallback(async () => {
    try {
      const data = await api.getHiveLog(hive.id, LOG_TAIL_LINES);
      setLog(data);
      setLogError(null);
    } catch (err) {
      setLogError(err instanceof Error ? err.message : "Log unavailable");
    } finally {
      setLogLoading(false);
    }
  }, [hive.id]);

  useEffect(() => {
    if (!expanded) {
      if (logIntervalRef.current) {
        clearInterval(logIntervalRef.current);
        logIntervalRef.current = null;
      }
      return;
    }
    setLogLoading(true);
    void fetchLog();
    logIntervalRef.current = setInterval(() => void fetchLog(), LOG_POLL_MS);
    return () => {
      if (logIntervalRef.current) {
        clearInterval(logIntervalRef.current);
        logIntervalRef.current = null;
      }
    };
  }, [expanded, fetchLog]);

  // Auto-scroll log to bottom when new lines arrive
  useEffect(() => {
    if (expanded && log && logAreaRef.current) {
      logAreaRef.current.scrollTop = logAreaRef.current.scrollHeight;
    }
  }, [expanded, log]);

  const displayTitle = hive.track_title || hive.objective_summary || hive.id;

  return (
    <div className="rounded-lg border border-white/10 bg-[#0a0f18] overflow-hidden">
      {/* Card header — always visible */}
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-left px-4 py-3 flex items-start gap-3 hover:bg-white/[0.03] transition-colors"
      >
        {/* Status dot */}
        <span
          className={`mt-1.5 h-2.5 w-2.5 flex-shrink-0 rounded-full ${cfg.dot}`}
          aria-label={cfg.label}
        />

        {/* Main info */}
        <div className="flex-1 min-w-0">
          {/* Row 1: id chip, session chip, elapsed, kanban chip */}
          <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
            <span className="font-mono text-white/80 truncate max-w-[280px]">{hive.id}</span>

            {hive.session && (
              <span className="flex-shrink-0 rounded px-1.5 py-0.5 bg-white/8 text-white/50 border border-white/10 text-[10px]">
                tmux: {hive.session}
              </span>
            )}

            <span className="flex-shrink-0 flex items-center gap-1 text-white/40">
              <Clock className="h-3 w-3" />
              {fmtElapsed(hive.elapsed_seconds)}
            </span>

            {hive.tracking_card && (
              <a
                href={`/kanban?card=${encodeURIComponent(hive.tracking_card)}`}
                onClick={(e) => e.stopPropagation()}
                className="flex-shrink-0 rounded px-1.5 py-0.5 bg-white/8 text-white/50 border border-white/10 text-[10px] hover:text-white/80 transition-colors"
              >
                {hive.tracking_card}
              </a>
            )}
          </div>

          {/* Row 2: title / objective */}
          <p className="mt-1 text-[12px] text-white/65 truncate leading-snug">
            {displayTitle}
          </p>
        </div>

        {/* Status chip + expand toggle */}
        <div className="flex-shrink-0 flex items-center gap-2 ml-2">
          <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${cfg.chip}`}>
            {cfg.label}
          </span>
          {expanded ? (
            <ChevronUp className="h-3.5 w-3.5 text-white/40" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 text-white/40" />
          )}
        </div>
      </button>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-white/8 px-4 py-3 space-y-3">
          {/* Meta row */}
          <div className="flex flex-wrap gap-x-6 gap-y-1.5 text-[11px] text-white/50">
            <span>
              Started: <span className="text-white/70">{fmtTs(hive.started_at)}</span>
            </span>
            {hive.updated_at && (
              <span>
                Updated: <span className="text-white/70">{fmtTs(hive.updated_at)}</span>
              </span>
            )}
            {hive.log_mtime && (
              <span>
                Log activity: <span className="text-white/70">{fmtTs(hive.log_mtime)}</span>
              </span>
            )}
            {hive.log_size_bytes > 0 && (
              <span>
                Log size: <span className="text-white/70">{fmtBytes(hive.log_size_bytes)}</span>
              </span>
            )}
          </div>

          {/* Workdir (copyable) */}
          <div className="flex items-center gap-2 text-[11px]">
            <FolderOpen className="h-3.5 w-3.5 text-white/30 flex-shrink-0" />
            <button
              type="button"
              className="font-mono text-white/45 hover:text-white/70 transition-colors truncate text-left"
              title="Click to copy path"
              onClick={() => navigator.clipboard.writeText(hive.workdir).catch(() => undefined)}
            >
              {hive.workdir}
            </button>
          </div>

          {/* Final report link */}
          {hive.final_report_path && (
            <div className="flex items-center gap-2 text-[11px]">
              <FileText className="h-3.5 w-3.5 text-white/30 flex-shrink-0" />
              <span className="text-white/50">Final report:</span>
              <button
                type="button"
                className="font-mono text-white/45 hover:text-white/70 transition-colors truncate text-left"
                title="Click to copy path"
                onClick={() =>
                  navigator.clipboard
                    .writeText(hive.final_report_path!)
                    .catch(() => undefined)
                }
              >
                {hive.final_report_path}
              </button>
              {hive.final_report_status && (
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                    hive.final_report_status === "COMPLETE"
                      ? "bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)]"
                      : "bg-[rgba(251,44,54,0.12)] text-[#fb2c36] border border-[rgba(251,44,54,0.35)]"
                  }`}
                >
                  {hive.final_report_status}
                </span>
              )}
            </div>
          )}

          {/* Log tail */}
          <div>
            <div className="flex items-center gap-1.5 mb-1.5 text-[10px] text-white/40 uppercase tracking-wider">
              <TerminalSquare className="h-3 w-3" />
              <span>hive-mind.log (last {LOG_TAIL_LINES} lines)</span>
              {logLoading && <RefreshCw className="h-3 w-3 animate-spin" />}
            </div>

            {logError && (
              <div className="text-[11px] text-rose-300/70 py-1">{logError}</div>
            )}

            {!logError && (!log || log.lines.length === 0) && !logLoading && (
              <div className="text-[11px] text-white/30 py-1">
                {hive.log_path ? "Log is empty." : "No hive-mind.log found."}
              </div>
            )}

            {!logError && log && log.lines.length > 0 && (
              <div
                ref={logAreaRef}
                className="max-h-[280px] overflow-y-auto rounded bg-black/40 border border-white/8 p-2"
              >
                <pre className="font-mono text-[10.5px] text-white/60 whitespace-pre-wrap break-all leading-[1.45]">
                  {log.lines.join("\n")}
                </pre>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section header
// ---------------------------------------------------------------------------

function SectionHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-center gap-2 mb-2">
      <span className="text-[10px] font-semibold uppercase tracking-[0.15em] text-white/40">
        {title}
      </span>
      <span className="rounded-full px-1.5 py-0.5 text-[10px] bg-white/8 text-white/40 border border-white/10">
        {count}
      </span>
      <div className="flex-1 h-px bg-white/8" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// HivesPage
// ---------------------------------------------------------------------------

export default function HivesPage() {
  const { setTitle } = usePageHeader();

  const [snapshot, setSnapshot] = useState<HivesSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    setTitle("Hives");
  }, [setTitle]);

  const loadSnapshot = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getHivesSnapshot();
      setSnapshot(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Hives snapshot unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSnapshot();
    const timer = setInterval(() => void loadSnapshot(), SNAPSHOT_POLL_MS);
    return () => clearInterval(timer);
  }, [loadSnapshot]);

  const toggleExpand = useCallback((id: string) => {
    setExpandedId((prev) => (prev === id ? null : id));
  }, []);

  // ------------------------------------------------------------------ Loading
  if (!snapshot && !error) {
    return (
      <div className="flex min-h-[300px] items-center justify-center text-sm text-white/45 bg-[#070b11]">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
        Loading Hives…
      </div>
    );
  }

  // ------------------------------------------------------------------- Error
  if (!snapshot && error) {
    return (
      <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 bg-[#070b11] text-white">
        <p className="text-sm text-rose-200">{error}</p>
        <button
          type="button"
          onClick={() => void loadSnapshot()}
          className="rounded-md border border-white/15 px-3 py-1.5 text-xs text-white/75 transition hover:border-white/35"
        >
          Retry
        </button>
      </div>
    );
  }

  const data = snapshot as HivesSnapshot;
  const activeHives = data.hives.filter((h) => h.status === "running");
  const recentHives = data.hives.filter((h) => h.status === "completed" || h.status === "blocked");
  const staleHives = data.hives.filter((h) => h.status === "stale");

  // ------------------------------------------------------------------ Content
  return (
    <div className="bg-[#070b11] text-white min-h-0">
      {/* Page header */}
      <header className="flex items-center gap-4 border-b border-white/10 px-4 py-3 mb-4">
        <div className="flex-shrink-0">
          <div className="flex items-center gap-1.5 text-[9.5px] font-semibold uppercase tracking-[0.18em] text-white/40">
            <Workflow className="h-3 w-3" />
            Ruflo Hives
          </div>
          <h1 className="mt-0.5 text-[15px] font-semibold tracking-tight">
            Hive Run Status
          </h1>
        </div>

        {/* Summary chips */}
        <div className="flex flex-wrap gap-2 text-[11px]">
          {data.active_count > 0 && (
            <span className="rounded-full px-2.5 py-1 bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)] font-semibold">
              {data.active_count} active
            </span>
          )}
          {data.completed_count > 0 && (
            <span className="rounded-full px-2.5 py-1 bg-white/8 text-white/50 border border-white/10 font-semibold">
              {data.completed_count} completed
            </span>
          )}
          {data.stale_count > 0 && (
            <span className="rounded-full px-2.5 py-1 bg-[rgba(255,189,56,0.10)] text-[#ffbd38] border border-[rgba(255,189,56,0.3)] font-semibold">
              {data.stale_count} stale
            </span>
          )}
        </div>

        <div className="flex-1" />

        {/* Scanned at + refresh */}
        <span className="text-[10.5px] text-white/35 flex-shrink-0 hidden sm:inline">
          Scanned {fmtTs(data.scanned_at)}
        </span>
        <button
          type="button"
          onClick={() => void loadSnapshot()}
          aria-label="Refresh hives"
          className="flex-shrink-0 rounded-md border border-white/12 p-1.5 text-white/65 transition hover:border-white/30 hover:text-white"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
        </button>
      </header>

      {/* No hives at all */}
      {data.hives.length === 0 && (
        <div className="flex min-h-[200px] items-center justify-center text-sm text-white/35 px-4">
          No hive runs found in ~/.hermes/ruflo-work/
        </div>
      )}

      <div className="px-4 pb-8 space-y-6">
        {/* Active section */}
        {activeHives.length > 0 && (
          <section>
            <SectionHeader title="Active" count={activeHives.length} />
            <div className="space-y-2">
              {activeHives.map((hive) => (
                <HiveCard
                  key={hive.id}
                  hive={hive}
                  expanded={expandedId === hive.id}
                  onToggle={() => toggleExpand(hive.id)}
                />
              ))}
            </div>
          </section>
        )}

        {/* Recent (completed / blocked) section */}
        {recentHives.length > 0 && (
          <section>
            <SectionHeader title="Recent" count={recentHives.length} />
            <div className="space-y-2">
              {recentHives.map((hive) => (
                <HiveCard
                  key={hive.id}
                  hive={hive}
                  expanded={expandedId === hive.id}
                  onToggle={() => toggleExpand(hive.id)}
                />
              ))}
            </div>
          </section>
        )}

        {/* Stale section */}
        {staleHives.length > 0 && (
          <section>
            <SectionHeader title="Stale" count={staleHives.length} />
            <div className="space-y-2">
              {staleHives.map((hive) => (
                <HiveCard
                  key={hive.id}
                  hive={hive}
                  expanded={expandedId === hive.id}
                  onToggle={() => toggleExpand(hive.id)}
                />
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
