/**
 * Codex Sessions — dashboard tab (P4 Wave 2).
 *
 * Renders one row per tracked codex session from /api/dashboard/codex-sessions
 * and subscribes to /api/pulse/stream for live `codex.session` events
 * (appeared / changed / removed) shipped by P4.1.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  CheckCircle2,
  Circle,
  ExternalLink,
  GitPullRequest,
  PauseCircle,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON, HERMES_BASE_PATH } from "@/lib/api";

// ─── Types ─────────────────────────────────────────────────────────────

type SessionState =
  | "CLAIMED"
  | "EXECUTING"
  | "MERGING"
  | "COMPLETE"
  | "ESCALATED"
  | "ORPHANED"
  | "PAUSED"
  | "UNKNOWN";

type IsaPhase = "scaffold" | "execute" | "verify" | "complete" | string | null;

interface CodexSession {
  thread_id: string;
  session_id: string;
  state: SessionState | string;
  paused: boolean;
  isa_id: string | null;
  isa_phase: IsaPhase;
  worktree_path: string;
  worktree_alive: boolean;
  port: number | null;
  channel_id: string;
  last_message_at: string | null;
  review_iterations: number;
  reviews_today: number;
  last_verdict: string | null;
  last_review_at: string | null;
  created_at: string;
  pr_number: number | null;
  pr_url: string | null;
  pr_state: "OPEN" | "MERGED" | "CLOSED" | null;
  head_branch: string | null;
  merge_label: "auto-merge" | "needs-human" | null;
  merge_requested_at: string | null;
  merged_at: string | null;
  merge_commit_oid: string | null;
  closed_at: string | null;
}

interface CodexSessionsSnapshot {
  scanned_at: string;
  sessions: CodexSession[];
  counts: {
    total: number;
    by_state: Record<string, number>;
    ports_claimed: number;
    ports_free: number;
  };
  review_pool: {
    size: number;
    daily_cap_per_sid: number;
    iteration_cap: number;
  };
}

interface CodexSessionEvent {
  kind: "codex-session";
  type: "appeared" | "changed" | "removed";
  thread_id: string;
  sid?: string | null;
  state?: string | null;
  isa_phase?: string | null;
  pr_number?: number | null;
  pr_url?: string | null;
  last_state?: string | null;
  changes?: Record<string, { from: unknown; to: unknown }>;
  ts: string;
}

// ─── Style helpers ─────────────────────────────────────────────────────

const STATE_STYLE: Record<string, { dot: string; chip: string; label: string }> = {
  CLAIMED:    { dot: "bg-[#7c91a8]",                              chip: "bg-[rgba(124,145,168,0.10)] text-[#7c91a8] border border-[rgba(124,145,168,0.3)]",  label: "Claimed" },
  EXECUTING:  { dot: "bg-[#4ade80] shadow-[0_0_6px_#4ade80]",     chip: "bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)]",   label: "Executing" },
  MERGING:    { dot: "bg-[#60a5fa] shadow-[0_0_6px_#60a5fa]",     chip: "bg-[rgba(96,165,250,0.12)] text-[#60a5fa] border border-[rgba(96,165,250,0.35)]",   label: "Merging" },
  COMPLETE:   { dot: "bg-[#22c55e]",                              chip: "bg-[rgba(34,197,94,0.10)] text-[#22c55e] border border-[rgba(34,197,94,0.3)]",       label: "Complete" },
  ESCALATED:  { dot: "bg-[#fb2c36] shadow-[0_0_5px_#fb2c36]",     chip: "bg-[rgba(251,44,54,0.12)] text-[#fb2c36] border border-[rgba(251,44,54,0.35)]",     label: "Escalated" },
  ORPHANED:   { dot: "bg-[#ffbd38]",                              chip: "bg-[rgba(255,189,56,0.10)] text-[#ffbd38] border border-[rgba(255,189,56,0.3)]",   label: "Orphaned" },
  PAUSED:     { dot: "bg-[#a78bfa]",                              chip: "bg-[rgba(167,139,250,0.10)] text-[#a78bfa] border border-[rgba(167,139,250,0.3)]", label: "Paused" },
  UNKNOWN:    { dot: "bg-[#7c91a8]",                              chip: "bg-[rgba(124,145,168,0.10)] text-[#7c91a8] border border-[rgba(124,145,168,0.3)]",  label: "Unknown" },
};

function stateChip(state: string | null | undefined) {
  const key = (state || "UNKNOWN").toUpperCase();
  return STATE_STYLE[key] ?? STATE_STYLE.UNKNOWN;
}

const PR_STATE_STYLE: Record<string, string> = {
  OPEN:   "text-[#60a5fa]",
  MERGED: "text-[#22c55e]",
  CLOSED: "text-[#fb2c36]",
};

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

function shortSid(sid: string | null | undefined): string {
  if (!sid) return "—";
  return sid.slice(0, 8);
}

// ─── SSE plumbing ──────────────────────────────────────────────────────

function buildPulseStreamUrl(): string {
  const token =
    typeof window !== "undefined" ? window.__HERMES_SESSION_TOKEN__ ?? "" : "";
  const base = `${HERMES_BASE_PATH}/api/pulse/stream`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

// ─── Main component ────────────────────────────────────────────────────

export default function CodexSessionsPage() {
  const [snapshot, setSnapshot] = useState<CodexSessionsSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<"connecting" | "open" | "offline">("connecting");
  const [reloadKey, setReloadKey] = useState(0);
  const esRef = useRef<EventSource | null>(null);
  const sessionsByThread = useRef<Map<string, CodexSession>>(new Map());

  const { setTitle } = usePageHeader();
  useEffect(() => {
    setTitle("Codex Sessions");
  }, [setTitle]);

  // Initial snapshot fetch + on reload-key changes.
  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    fetchJSON<CodexSessionsSnapshot>("/api/dashboard/codex-sessions")
      .then((snap) => {
        if (cancelled) return;
        setSnapshot(snap);
        sessionsByThread.current = new Map(
          snap.sessions.map((s) => [s.thread_id, s]),
        );
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setLoadError(err.message || "snapshot fetch failed");
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  // Mutate one session row in the snapshot — used by SSE handlers.
  const upsertSession = useCallback((updater: (m: Map<string, CodexSession>) => void) => {
    setSnapshot((prev) => {
      if (!prev) return prev;
      const next = new Map(sessionsByThread.current);
      updater(next);
      sessionsByThread.current = next;
      // Rebuild counts.
      const by_state: Record<string, number> = {};
      next.forEach((s) => {
        const k = s.state || "UNKNOWN";
        by_state[k] = (by_state[k] || 0) + 1;
      });
      return {
        ...prev,
        sessions: Array.from(next.values()),
        counts: {
          ...prev.counts,
          total: next.size,
          by_state,
        },
      };
    });
  }, []);

  // SSE subscription for live codex.session deltas.
  useEffect(() => {
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      setStreamState("connecting");
      try {
        const es = new EventSource(buildPulseStreamUrl());
        esRef.current = es;

        es.addEventListener("open", () => {
          if (!cancelled) setStreamState("open");
        });

        es.addEventListener("codex.session", (evt: MessageEvent) => {
          try {
            const ev = JSON.parse(evt.data) as CodexSessionEvent;
            if (ev.kind !== "codex-session") return;
            if (ev.type === "removed") {
              upsertSession((m) => {
                m.delete(ev.thread_id);
              });
              return;
            }
            // appeared or changed → refresh the row (light approach:
            // re-fetch the snapshot to keep all fields in sync; we
            // could do field-level patching but the snapshot fetch is
            // cheap and avoids drift).
            setReloadKey((k) => k + 1);
          } catch {
            // Ignore malformed payloads.
          }
        });

        es.addEventListener("error", () => {
          if (cancelled) return;
          setStreamState("offline");
          try {
            es.close();
          } catch { /* noop */ }
          esRef.current = null;
          window.setTimeout(connect, 3000);
        });
      } catch {
        if (!cancelled) {
          setStreamState("offline");
          window.setTimeout(connect, 3000);
        }
      }
    };

    connect();

    return () => {
      cancelled = true;
      const es = esRef.current;
      if (es) {
        try {
          es.close();
        } catch { /* noop */ }
        esRef.current = null;
      }
    };
  }, [upsertSession]);

  // Sort sessions: active states first (EXECUTING, MERGING), then by created_at desc.
  const sortedSessions = useMemo(() => {
    if (!snapshot) return [];
    const order: Record<string, number> = {
      EXECUTING: 0, MERGING: 1, CLAIMED: 2, PAUSED: 3,
      ESCALATED: 4, ORPHANED: 5, COMPLETE: 6, UNKNOWN: 7,
    };
    return [...snapshot.sessions].sort((a, b) => {
      const oa = order[a.state] ?? 99;
      const ob = order[b.state] ?? 99;
      if (oa !== ob) return oa - ob;
      return (b.created_at || "").localeCompare(a.created_at || "");
    });
  }, [snapshot]);

  return (
    <div className="px-6 py-4 max-w-[1400px] mx-auto">
      {/* Status strip */}
      <div className="flex items-center gap-3 mb-4 text-sm">
        <StreamBadge state={streamState} />
        <button
          onClick={() => setReloadKey((k) => k + 1)}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded border border-white/10 bg-white/[0.03] hover:bg-white/[0.06] text-white/70"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </button>
        {snapshot && (
          <span className="text-white/40 ml-auto">
            {snapshot.counts.total} session{snapshot.counts.total === 1 ? "" : "s"}
            {" · "}
            ports {snapshot.counts.ports_claimed}/{snapshot.counts.ports_claimed + snapshot.counts.ports_free}
            {" · "}
            snapshot {fmtTs(snapshot.scanned_at)}
          </span>
        )}
      </div>

      {/* State counts */}
      {snapshot && (
        <div className="flex flex-wrap gap-2 mb-4">
          {Object.entries(snapshot.counts.by_state).map(([state, n]) => {
            const cfg = stateChip(state);
            return (
              <span
                key={state}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs ${cfg.chip}`}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${cfg.dot}`} />
                {cfg.label} · {n}
              </span>
            );
          })}
        </div>
      )}

      {/* Error banner */}
      {loadError && (
        <div className="mb-4 px-3 py-2 rounded border border-[rgba(251,44,54,0.35)] bg-[rgba(251,44,54,0.08)] text-[#fb2c36] text-sm">
          Could not load /api/dashboard/codex-sessions: {loadError}
        </div>
      )}

      {/* Empty state */}
      {snapshot && snapshot.sessions.length === 0 && (
        <div className="text-center py-16 text-white/40 text-sm">
          No tracked codex sessions. Open a Discord thread on the codex channel
          to allocate one.
        </div>
      )}

      {/* Session cards */}
      <div className="space-y-2">
        {sortedSessions.map((s) => (
          <SessionCard key={s.thread_id} s={s} />
        ))}
      </div>
    </div>
  );
}

// ─── Subcomponents ─────────────────────────────────────────────────────

function StreamBadge({ state }: { state: "connecting" | "open" | "offline" }) {
  if (state === "open") {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs bg-[rgba(74,222,128,0.10)] text-[#4ade80] border border-[rgba(74,222,128,0.30)]">
        <span className="h-1.5 w-1.5 rounded-full bg-[#4ade80] shadow-[0_0_6px_#4ade80]" />
        Live
      </span>
    );
  }
  if (state === "offline") {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs bg-[rgba(251,44,54,0.10)] text-[#fb2c36] border border-[rgba(251,44,54,0.30)]">
        <XCircle className="h-3 w-3" />
        Offline — reconnecting
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs bg-white/[0.04] text-white/50 border border-white/10">
      <Circle className="h-3 w-3" />
      Connecting…
    </span>
  );
}

function SessionCard({ s }: { s: CodexSession }) {
  const cfg = stateChip(s.state);
  const prStateColor = s.pr_state ? PR_STATE_STYLE[s.pr_state] ?? "text-white/60" : "text-white/40";

  return (
    <div className="px-4 py-3 rounded-lg border border-white/10 bg-white/[0.02] hover:bg-white/[0.04] transition-colors">
      <div className="flex items-center gap-3 flex-wrap">
        <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs ${cfg.chip}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${cfg.dot}`} />
          {cfg.label}
        </span>

        <span className="font-mono text-xs text-white/80">{shortSid(s.session_id)}</span>

        {s.isa_id && (
          <span className="text-xs text-white/50">
            {s.isa_id}
            {s.isa_phase && (
              <span className="text-white/30"> · {s.isa_phase}</span>
            )}
          </span>
        )}

        {s.paused && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-[rgba(167,139,250,0.10)] text-[#a78bfa] border border-[rgba(167,139,250,0.30)]">
            <PauseCircle className="h-3 w-3" />
            Paused
          </span>
        )}

        <span className="text-white/30 text-xs ml-auto">
          {fmtTs(s.last_message_at || s.created_at)}
        </span>
      </div>

      <div className="mt-2 flex items-center gap-4 flex-wrap text-xs">
        {s.pr_number != null ? (
          <a
            href={s.pr_url ?? "#"}
            target="_blank"
            rel="noopener noreferrer"
            className={`inline-flex items-center gap-1 hover:underline ${prStateColor}`}
          >
            <GitPullRequest className="h-3.5 w-3.5" />
            PR #{s.pr_number}
            {s.pr_state && <span className="text-white/40">· {s.pr_state}</span>}
            <ExternalLink className="h-3 w-3" />
          </a>
        ) : (
          <span className="inline-flex items-center gap-1 text-white/30">
            <GitPullRequest className="h-3.5 w-3.5" />
            No PR yet
          </span>
        )}

        {s.merge_label && (
          <span
            className={
              s.merge_label === "auto-merge"
                ? "px-1.5 py-0.5 rounded text-[10px] bg-[rgba(34,197,94,0.10)] text-[#22c55e] border border-[rgba(34,197,94,0.30)]"
                : "px-1.5 py-0.5 rounded text-[10px] bg-[rgba(255,189,56,0.10)] text-[#ffbd38] border border-[rgba(255,189,56,0.30)]"
            }
          >
            {s.merge_label}
          </span>
        )}

        {s.review_iterations > 0 && (
          <span className="text-white/40">
            review iter {s.review_iterations}
            {s.last_verdict && <span className="text-white/30"> · {s.last_verdict}</span>}
          </span>
        )}

        <span className={s.worktree_alive ? "text-white/40" : "text-[#ffbd38]"}>
          worktree {s.worktree_alive ? "live" : "missing"}
        </span>

        {s.thread_id && (
          <a
            href={`https://discord.com/channels/@me/${s.thread_id}`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-white/40 hover:text-white/70"
          >
            <Activity className="h-3 w-3" />
            thread
          </a>
        )}

        {s.merged_at && (
          <span className="text-[#22c55e]">
            <CheckCircle2 className="h-3 w-3 inline -mt-0.5" /> merged {fmtTs(s.merged_at)}
          </span>
        )}
      </div>
    </div>
  );
}
