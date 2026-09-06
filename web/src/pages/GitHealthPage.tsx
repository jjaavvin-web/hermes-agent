/**
 * Git Health — dashboard tab.
 *
 * A visual map of the repo's git tree: the fork/main trunk plus one railroad
 * "lane" per tracked unit of work — every codex-session worktree AND the local
 * checkout where Claude works for you. Each lane shows where it forked, how far
 * it has diverged (commit beads + a "+N" pill for the rest), ahead/behind, and
 * who owns it. Above the map: a readiness summary + the single best next move.
 *
 * Data: /api/dashboard/git-graph (the map) + /api/dashboard/git-health
 * (severity summary + recommendation). Light 15s poll + manual refresh.
 *
 * Visual language borrowed from Josep's git-branching reference: cyan trunk,
 * colored branch lanes, beads = commits, "+748"-style many-pill for big gaps.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  GitBranch,
  GitCommit,
  GitPullRequest,
  RefreshCw,
  GitFork,
  Network,
} from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON } from "@/lib/api";
import GitTree from "./GitTree";

type Severity = "ready" | "warn" | "bad" | "idle";

// Reference palette (kept literal so the railroad reads the same as the guide).
const C = {
  trunk: "#74d7ff",
  ready: "#7ef0a2",
  warn: "#ffca6e",
  bad: "#ff7a8a",
  claude: "#caa2ff",
  idle: "rgba(255,255,255,0.30)",
} as const;

const SEV_COLOR: Record<Severity, string> = {
  ready: C.ready,
  warn: C.warn,
  bad: C.bad,
  idle: C.idle,
};
const SEV_LABEL: Record<Severity, string> = {
  ready: "Ready",
  warn: "Commit first",
  bad: "Needs fixing",
  idle: "Idle",
};

const MAX_BEADS = 5; // beads drawn before collapsing the rest into a "+N" pill

interface Commit {
  sha: string;
  subject: string;
  date: string;
}
interface Lane {
  id: string;
  label: string;
  owner: "claude" | "codex";
  branch: string;
  head: string | null;
  merge_base: string | null;
  on_trunk_tip: boolean;
  ahead: number;
  behind: number;
  diverged: boolean;
  uncommitted: number;
  files_changed: number;
  insertions: number;
  deletions: number;
  lead_commits: Commit[];
  lead_truncated: boolean;
  severity: Severity;
  thread_id: string | null;
  pr_number: number | null;
  pr_url: string | null;
  pr_state: string | null;
  merged: boolean;
  state: string | null;
  active: boolean;
  isa_phase: string | null;
  last_activity: string | null;
}
interface GitGraph {
  scanned_at: string;
  base: {
    ref: string | null;
    sha: string | null;
    commits: Commit[];
    upstream_behind: { ref: string; behind: number } | null;
  };
  lanes: Lane[];
  counts: { lanes: number; diverged: number; ahead_total: number; max_ahead: number };
}

interface HealthSummary {
  total: number;
  by_severity: Record<Severity, number>;
  ready: number;
  actionable: number;
  total_uncommitted: number;
  total_files_changed: number;
}
interface GitHealth {
  scanned_at: string;
  best_move: { text: string; severity: Severity; slug: string | null; thread_id: string | null };
  summary: HealthSummary;
}

function fmtTs(iso?: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}
function fmtNum(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}
function discordHref(threadId: string): string {
  return `https://discord.com/channels/@me/${threadId}`;
}

// A single round commit bead.
function Bead({ color, title, filled }: { color: string; title?: string; filled?: boolean }) {
  return (
    <span
      title={title}
      className="relative z-[2] h-[18px] w-[18px] rounded-full shrink-0"
      style={{ border: `3px solid ${color}`, background: filled ? color : "#0a1524" }}
    />
  );
}

// The railroad line for one lane: fork bead → commit beads → "+N" many-pill.
function LaneRail({ lane }: { lane: Lane }) {
  const color = SEV_COLOR[lane.severity];
  const beads = Math.min(lane.ahead, MAX_BEADS);
  const remainder = lane.ahead - beads;
  const idle = lane.ahead === 0 && lane.files_changed === 0;

  return (
    <div className="relative flex items-center gap-2 min-h-[34px] pl-1">
      {/* baseline track */}
      <div
        className="absolute left-2 right-2 top-1/2 -translate-y-1/2 h-[3px] rounded-full"
        style={
          idle
            ? { backgroundImage: `repeating-linear-gradient(90deg, ${C.idle} 0 7px, transparent 7px 15px)` }
            : { background: `linear-gradient(90deg, ${C.trunk} 0%, ${color} 22%)` }
        }
      />
      {/* fork point — sits on the trunk */}
      <Bead color={C.trunk} title={`forked from ${lane.merge_base ?? "trunk"}`} />
      {idle ? (
        <span className="relative z-[2] text-[11px] text-white/40 pl-1">on trunk tip · nothing new</span>
      ) : (
        <>
          {lane.lead_commits.slice(0, beads).map((c, i) => (
            <Bead key={c.sha + i} color={color} title={`${c.sha}  ${c.subject}`} filled={i === 0} />
          ))}
          {/* if ahead but commits list shorter than beads (shouldn't happen), pad */}
          {remainder > 0 && (
            <span
              className="relative z-[2] inline-grid place-items-center h-[26px] min-w-[58px] px-2 rounded-full text-[12px] font-bold shrink-0"
              style={{ border: `2px solid ${color}`, background: "rgba(255,255,255,0.06)", color: "#f4f7fb" }}
              title={`${lane.ahead} commits ahead of trunk (${beads} shown)`}
            >
              +{fmtNum(remainder)}
            </span>
          )}
          {/* HEAD marker */}
          <span
            className="relative z-[2] ml-1 px-1.5 py-0.5 rounded text-[10px] font-mono shrink-0"
            style={{ border: `1px solid ${color}55`, color, background: `${color}14` }}
          >
            {lane.head ?? "HEAD"}
          </span>
        </>
      )}
    </div>
  );
}

type ViewMode = "tree" | "map";

export default function GitHealthPage() {
  const [graph, setGraph] = useState<GitGraph | null>(null);
  const [health, setHealth] = useState<GitHealth | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [view, setView] = useState<ViewMode>("tree");

  const { setTitle } = usePageHeader();
  useEffect(() => {
    setTitle("Git Health");
  }, [setTitle]);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    Promise.all([
      fetchJSON<GitGraph>("/api/dashboard/git-graph"),
      fetchJSON<GitHealth>("/api/dashboard/git-health"),
    ])
      .then(([g, h]) => {
        if (cancelled) return;
        setGraph(g);
        setHealth(h);
      })
      .catch((e: Error) => {
        if (!cancelled) setErr(e.message || "fetch failed");
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  useEffect(() => {
    const id = window.setInterval(() => setReloadKey((k) => k + 1), 15000);
    return () => window.clearInterval(id);
  }, []);

  const sum = health?.summary;
  const best = health?.best_move;
  const base = graph?.base;
  const readyPct = sum && sum.total > 0 ? Math.round((sum.ready / sum.total) * 100) : 0;

  // Keep server order (already attention-sorted, Claude pinned within tier).
  const lanes = useMemo(() => graph?.lanes ?? [], [graph]);

  return (
    <div className="px-4 sm:px-6 py-4 max-w-[1320px] mx-auto">
      <div className="flex items-center gap-3 mb-4 text-sm">
        <button
          onClick={() => setReloadKey((k) => k + 1)}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded border border-white/10 bg-white/[0.03] hover:bg-white/[0.06] text-white/70"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </button>
        {/* View toggle: animated constellation vs. railroad map */}
        <div className="inline-flex items-center rounded-md border border-white/10 overflow-hidden">
          <button
            onClick={() => setView("tree")}
            className={`inline-flex items-center gap-1.5 px-2.5 py-1 text-xs ${
              view === "tree" ? "bg-[rgba(116,215,255,0.16)] text-[#74d7ff]" : "text-white/55 hover:bg-white/[0.05]"
            }`}
          >
            <GitFork className="h-3.5 w-3.5" />
            Tree
          </button>
          <button
            onClick={() => setView("map")}
            className={`inline-flex items-center gap-1.5 px-2.5 py-1 text-xs border-l border-white/10 ${
              view === "map" ? "bg-[rgba(116,215,255,0.16)] text-[#74d7ff]" : "text-white/55 hover:bg-white/[0.05]"
            }`}
          >
            <Network className="h-3.5 w-3.5" />
            Map
          </button>
        </div>
        {graph && (
          <span className="text-white/40 ml-auto">
            {lanes.length} lane{lanes.length === 1 ? "" : "s"} · checked {fmtTs(graph.scanned_at)}
          </span>
        )}
      </div>

      {/* Readiness summary */}
      {sum && (
        <div className="mb-4 rounded-xl border border-white/10 bg-white/[0.02] px-4 py-3.5">
          <div className="flex items-center gap-6 flex-wrap">
            <div className="min-w-[200px] flex-1">
              <div className="flex items-center justify-between text-[11px] mb-1.5">
                <span className="uppercase tracking-wide text-white/45">Readiness</span>
                <span className="text-white/60">{sum.ready}/{sum.total} ready · {readyPct}%</span>
              </div>
              <div className="h-2 rounded-full bg-white/[0.06] overflow-hidden flex">
                {(["ready", "warn", "bad", "idle"] as Severity[]).map((s) =>
                  sum.by_severity[s] > 0 ? (
                    <div
                      key={s}
                      style={{ width: `${(sum.by_severity[s] / sum.total) * 100}%`, background: SEV_COLOR[s] }}
                      title={`${sum.by_severity[s]} ${SEV_LABEL[s]}`}
                    />
                  ) : null,
                )}
              </div>
            </div>
            <div className="flex items-center gap-3 text-xs">
              {(["ready", "warn", "bad", "idle"] as Severity[]).map((s) => (
                <span key={s} className="inline-flex items-center gap-1.5 text-white/55">
                  <span className="h-2 w-2 rounded-full" style={{ background: SEV_COLOR[s] }} />
                  {sum.by_severity[s]} {SEV_LABEL[s].toLowerCase()}
                </span>
              ))}
            </div>
            <div className="flex items-center gap-4 text-xs text-white/45 ml-auto">
              <span className="inline-flex items-center gap-1">
                <GitCommit className="h-3.5 w-3.5" />
                {sum.total_uncommitted} uncommitted
              </span>
              {graph && (
                <span className="inline-flex items-center gap-1">
                  <GitBranch className="h-3.5 w-3.5" />
                  {fmtNum(graph.counts.ahead_total)} commits ahead total
                </span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Best recommended move */}
      {best && (
        <div
          className="mb-5 px-4 py-3 rounded-lg text-sm flex items-start gap-2.5"
          style={{
            background: `${SEV_COLOR[best.severity]}1f`,
            border: `1px solid ${SEV_COLOR[best.severity]}59`,
            color: SEV_COLOR[best.severity],
          }}
        >
          <span className="mt-1 h-2 w-2 rounded-full" style={{ background: SEV_COLOR[best.severity] }} />
          <div className="flex-1">
            <div className="text-[11px] uppercase tracking-wide opacity-70">Recommended move</div>
            <div className="font-medium">{best.text}</div>
          </div>
          {best.thread_id && (
            <a href={discordHref(best.thread_id)} target="_blank" rel="noopener noreferrer"
               className="text-xs opacity-70 hover:opacity-100 whitespace-nowrap">
              open thread →
            </a>
          )}
        </div>
      )}

      {err && (
        <div className="mb-4 px-3 py-2 rounded border border-[rgba(251,44,54,0.35)] bg-[rgba(251,44,54,0.08)] text-[#fb2c36] text-sm">
          Could not load git data: {err}
        </div>
      )}

      {/* Git-history river view (self-fetches /api/dashboard/git-river) */}
      {view === "tree" && (
        <GitTree reloadKey={reloadKey} />
      )}

      {/* The railroad map */}
      {graph && view === "map" && (
        <div
          className="rounded-2xl border border-white/10 p-4 sm:p-5"
          style={{
            backgroundImage:
              "linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px)",
            backgroundSize: "28px 28px",
          }}
        >
          {/* Trunk header */}
          <div className="flex items-center gap-3 flex-wrap mb-1">
            <span className="inline-flex items-center gap-2 text-sm">
              <span className="h-[18px] w-[18px] rounded-full shrink-0" style={{ border: `3px solid ${C.trunk}`, background: C.trunk }} />
              <span className="font-mono font-semibold" style={{ color: C.trunk }}>{base?.ref ?? "trunk"}</span>
              {base?.sha && <span className="font-mono text-xs text-white/40">@ {base.sha}</span>}
            </span>
            <span className="text-xs text-white/35">the trunk every lane forks from</span>
            {base?.upstream_behind && base.upstream_behind.behind > 0 && (
              <span
                className="ml-auto inline-flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-mono"
                style={{ border: `1px solid ${C.bad}66`, background: `${C.bad}1c`, color: C.bad }}
                title={`${base.ref} is ${base.upstream_behind.behind} commits behind ${base.upstream_behind.ref} — your fork is stale vs upstream`}
              >
                <AlertTriangle className="h-3 w-3" />
                {fmtNum(base.upstream_behind.behind)} behind {base.upstream_behind.ref}
              </span>
            )}
          </div>
          {/* trunk line */}
          <div className="relative h-6 mb-2">
            <div className="absolute left-2 right-2 top-1/2 -translate-y-1/2 h-[4px] rounded-full" style={{ background: C.trunk, opacity: 0.55 }} />
            {base?.upstream_behind && base.upstream_behind.behind > 0 && (
              <div
                className="absolute right-2 top-1/2 -translate-y-1/2 h-[4px] w-[120px] rounded-full"
                style={{ backgroundImage: `repeating-linear-gradient(90deg, ${C.bad}99 0 8px, transparent 8px 18px)` }}
              />
            )}
          </div>

          {/* Lanes */}
          <div className="space-y-1.5">
            {lanes.map((lane) => {
              const color = SEV_COLOR[lane.severity];
              return (
                <div
                  key={lane.id}
                  className="grid grid-cols-[230px_1fr] gap-3 items-center rounded-lg px-2 py-2 hover:bg-white/[0.03] transition-colors"
                >
                  {/* Lane label column */}
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span
                        className="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide shrink-0"
                        style={
                          lane.owner === "claude"
                            ? { border: `1px solid ${C.claude}66`, color: C.claude, background: `${C.claude}1a` }
                            : { border: `1px solid ${C.trunk}55`, color: C.trunk, background: `${C.trunk}14` }
                        }
                      >
                        {lane.owner === "claude" ? "YOU + CLAUDE" : "CODEX"}
                      </span>
                      <span className="font-mono text-sm text-white/85 truncate">{lane.label}</span>
                    </div>
                    <div className="font-mono text-[10px] text-white/35 truncate mt-0.5" title={lane.branch}>
                      {lane.branch}
                    </div>
                  </div>

                  {/* Rail + meta */}
                  <div className="min-w-0">
                    <LaneRail lane={lane} />
                    <div className="flex items-center gap-2.5 flex-wrap mt-1 text-[11px]">
                      <span className="inline-flex items-center gap-1" style={{ color }}>
                        <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
                        {SEV_LABEL[lane.severity]}
                      </span>
                      <span className="text-white/45 font-mono">
                        ↑{fmtNum(lane.ahead)}
                        {lane.behind > 0 && <span className="text-white/35"> ↓{fmtNum(lane.behind)}</span>}
                      </span>
                      {(lane.insertions > 0 || lane.deletions > 0) && (
                        <span className="font-mono">
                          <span style={{ color: C.ready }}>+{fmtNum(lane.insertions)}</span>{" "}
                          <span style={{ color: C.bad }}>−{fmtNum(lane.deletions)}</span>
                        </span>
                      )}
                      {lane.uncommitted > 0 && (
                        <span className="font-mono" style={{ color: C.warn }}>{lane.uncommitted} uncommitted</span>
                      )}
                      {lane.diverged && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded"
                              style={{ border: `1px solid ${C.bad}59`, color: C.bad, background: `${C.bad}1a` }}>
                          <AlertTriangle className="h-3 w-3" /> diverged base
                        </span>
                      )}
                      {lane.pr_number != null ? (
                        <a href={lane.pr_url || "#"} target="_blank" rel="noopener noreferrer"
                           className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded"
                           style={{ border: `1px solid ${(lane.merged ? C.claude : C.ready)}59`, color: lane.merged ? C.claude : C.ready, background: `${(lane.merged ? C.claude : C.ready)}14` }}>
                          <GitPullRequest className="h-3 w-3" /> #{lane.pr_number} {lane.merged ? "merged" : lane.pr_state || "open"}
                        </a>
                      ) : null}
                      {lane.thread_id && (
                        <a href={discordHref(lane.thread_id)} target="_blank" rel="noopener noreferrer"
                           className="text-white/35 hover:text-white/70 ml-auto">
                          open thread →
                        </a>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Legend */}
          <div className="flex items-center gap-4 flex-wrap mt-4 pt-3 border-t border-white/8 text-[11px] font-mono text-white/45">
            <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full" style={{ background: C.trunk }} /> trunk / fork point</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full" style={{ background: C.ready }} /> ready</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full" style={{ background: C.warn }} /> commit first</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full" style={{ background: C.bad }} /> diverged / needs fixing</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full" style={{ background: C.claude }} /> Claude's lane</span>
            <span className="ml-auto">bead = commit · +N = more commits than shown</span>
          </div>
        </div>
      )}

      {graph && lanes.length === 0 && (
        <div className="text-center py-16 text-white/40 text-sm">No tracked lanes.</div>
      )}
    </div>
  );
}
