import { useMemo, useState } from "react";
import { GitBranch, GitCommit, ArrowUp, ArrowDown, FileDiff } from "lucide-react";
import type { OSSnapshot } from "@/lib/api";
import { StatusDot } from "@/components/StatusKit";
import type { Status } from "@/components/StatusKit";

/**
 * Git Topology view (third OS view, peer to Nexus/Grid).
 * Visualizes the live branch/lane topology from the OS snapshot's `repo` payload
 * (already computed server-side via git_health()/git_graph() — no new backend infra):
 * trunk + per-lane swimlanes with ahead/behind, uncommitted heat, divergence, lead
 * commits and merge-base age. Severity uses the corrected info-aware tiers so idle/
 * clean lanes read info-green (never a false amber).
 */

interface LeadCommit {
  sha?: string;
  subject?: string;
  date?: string;
}
interface RepoLane {
  id: string;
  label?: string;
  owner?: string;
  branch?: string;
  head?: string;
  merge_base?: string;
  on_trunk_tip?: boolean;
  ahead?: number;
  behind?: number;
  diverged?: boolean;
  uncommitted?: number;
  files_changed?: number;
  insertions?: number;
  deletions?: number;
  lead_commits?: LeadCommit[];
  stage?: string;
}

// Uncommitted-work heat: clean/small = info (NOT amber — avoids false positives), large = amber/red.
function laneStatus(lane: RepoLane): Status {
  const u = lane.uncommitted ?? 0;
  if (u >= 40) return "red";
  if (u >= 20) return "amber";
  return "info"; // idle / clean / small-uncommitted is normal
}

function heatClass(u: number): string {
  if (u >= 40) return "bg-[#fb2c36]";
  if (u >= 20) return "bg-[#ffbd38]";
  if (u > 0) return "bg-[#6b9bd1]";
  return "bg-[rgba(124,145,168,0.3)]";
}

function fmtNum(n: number | undefined): string {
  if (!n) return "0";
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(n);
}

export function GitTopology({ snapshot }: { snapshot: OSSnapshot }) {
  const repo = (snapshot?.repo ?? {}) as {
    readiness_pct?: number;
    summary?: Record<string, unknown>;
    best_move?: { text?: string; severity?: string; slug?: string };
    lanes?: RepoLane[];
    counts?: Record<string, unknown>;
  };
  const lanes = useMemo(() => (repo.lanes ?? []).slice(), [repo.lanes]);
  const [selected, setSelected] = useState<string | null>(null);

  // Trunk = the lane currently on the trunk tip, else the first diverged trunk-ish lane, else lane[0].
  const trunk = lanes.find((l) => l.on_trunk_tip) ?? lanes.find((l) => l.branch?.includes("deploy/")) ?? lanes[0];
  const others = lanes.filter((l) => l.id !== trunk?.id);
  const maxAhead = Math.max(1, ...lanes.map((l) => l.ahead ?? 0));

  const summary = repo.summary ?? {};
  const totalUncommitted = (summary["total_uncommitted"] as number) ?? lanes.reduce((s, l) => s + (l.uncommitted ?? 0), 0);
  const actionable = (summary["actionable"] as number) ?? 0;
  const bySev = (summary["by_severity"] as Record<string, number>) ?? {};

  if (!lanes.length) {
    return (
      <div className="mt-3 flex min-h-[300px] items-center justify-center rounded-lg border border-border bg-card/40 text-sm text-text-tertiary">
        <GitBranch className="mr-2 h-4 w-4" /> Git topology data not ready — refresh in a moment.
      </div>
    );
  }

  const LaneRow = ({ lane, isTrunk }: { lane: RepoLane; isTrunk?: boolean }) => {
    const st = laneStatus(lane);
    const u = lane.uncommitted ?? 0;
    const ahead = lane.ahead ?? 0;
    const behind = lane.behind ?? 0;
    const isSel = selected === lane.id;
    const lead = lane.lead_commits ?? [];
    return (
      <button
        type="button"
        onClick={() => setSelected(isSel ? null : lane.id)}
        className={`group flex w-full min-w-0 flex-col gap-1.5 rounded-md border px-3 py-2 text-left transition ${
          isSel ? "border-accent/60 bg-accent/10" : "border-border bg-card/40 hover:border-accent/40"
        } ${isTrunk ? "ring-1 ring-[#4ade80]/30" : ""}`}
      >
        <div className="flex min-w-0 items-center gap-2">
          <StatusDot status={st} />
          {isTrunk ? <GitCommit className="h-3.5 w-3.5 flex-shrink-0 text-[#4ade80]" /> : <GitBranch className="h-3.5 w-3.5 flex-shrink-0 text-text-tertiary" />}
          <span className="min-w-0 truncate font-mono text-xs text-text-primary">{lane.branch ?? lane.label ?? lane.id}</span>
          {isTrunk && <span className="flex-shrink-0 rounded-full bg-[rgba(74,222,128,0.12)] px-1.5 py-0.5 text-[10px] uppercase text-[#4ade80]">trunk</span>}
          {lane.owner && <span className="flex-shrink-0 text-[10px] uppercase text-text-tertiary">{lane.owner}</span>}
          <span className="ml-auto flex flex-shrink-0 items-center gap-2 text-[11px] font-mono text-text-tertiary">
            {ahead > 0 && <span className="flex items-center text-[#4ade80]"><ArrowUp className="h-3 w-3" />{fmtNum(ahead)}</span>}
            {behind > 0 && <span className="flex items-center text-[#ffbd38]"><ArrowDown className="h-3 w-3" />{fmtNum(behind)}</span>}
            {lane.diverged && <span className="rounded bg-[rgba(255,189,56,0.12)] px-1 text-[10px] text-[#ffbd38]">diverged</span>}
          </span>
        </div>
        {/* ahead bar (relative to busiest lane) + uncommitted heat */}
        <div className="flex items-center gap-2">
          <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-[rgba(124,145,168,0.12)]">
            <div className="h-full rounded-full bg-[#4ade80]/50" style={{ width: `${Math.min(100, ((ahead || 0) / maxAhead) * 100)}%` }} />
          </div>
          <span className={`flex flex-shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] ${u > 0 ? "text-text-secondary" : "text-text-tertiary"}`}>
            <span className={`h-2 w-2 rounded-full ${heatClass(u)}`} />
            {u} uncommitted
          </span>
          {lane.files_changed ? <span className="flex flex-shrink-0 items-center gap-0.5 text-[10px] text-text-tertiary"><FileDiff className="h-3 w-3" />{fmtNum(lane.files_changed)}</span> : null}
        </div>
        {isSel && lead.length > 0 && (
          <div className="mt-1 flex flex-col gap-0.5 border-t border-border pt-1.5">
            {lead.slice(0, 4).map((c, i) => (
              <div key={c.sha ?? i} className="flex min-w-0 items-baseline gap-1.5 text-[10px]">
                <span className="flex-shrink-0 font-mono text-text-tertiary">{(c.sha ?? "").slice(0, 7)}</span>
                <span className="min-w-0 truncate text-text-secondary">{c.subject}</span>
              </div>
            ))}
            <div className="text-[10px] text-text-tertiary">stage: {lane.stage ?? "—"} · merge-base {(lane.merge_base ?? "").slice(0, 7)}</div>
          </div>
        )}
      </button>
    );
  };

  return (
    <div className="mt-3 flex min-h-0 flex-col gap-3 pb-8">
      {/* summary header */}
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card/40 px-3 py-2 text-xs">
        <GitBranch className="h-4 w-4 text-text-tertiary" />
        <span className="font-mondwest tracking-[0.14em] text-text-primary">GIT TOPOLOGY</span>
        <span className="text-text-tertiary">· {lanes.length} lanes</span>
        <span className="rounded-full bg-[rgba(124,145,168,0.10)] px-2 py-0.5 text-text-secondary">{fmtNum(totalUncommitted)} uncommitted</span>
        <span className="rounded-full bg-[rgba(124,145,168,0.10)] px-2 py-0.5 text-text-secondary">{actionable} actionable</span>
        {typeof bySev["idle"] === "number" && <span className="rounded-full bg-[rgba(107,155,209,0.10)] px-2 py-0.5 text-[#6b9bd1]">{bySev["idle"]} idle</span>}
        {repo.best_move?.text && (
          <span className="ml-auto min-w-0 truncate rounded-md border border-[rgba(255,189,56,0.3)] bg-[rgba(255,189,56,0.07)] px-2 py-0.5 text-[#ffbd38]">
            best move: {repo.best_move.text}
          </span>
        )}
      </div>

      {/* trunk swimlane */}
      {trunk && (
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wider text-text-tertiary">Trunk</div>
          <LaneRow lane={trunk} isTrunk />
        </div>
      )}

      {/* feature/wip/deploy lane swimlanes */}
      <div>
        <div className="mb-1 text-[10px] uppercase tracking-wider text-text-tertiary">Lanes ({others.length})</div>
        <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
          {others.map((lane) => (
            <LaneRow key={lane.id} lane={lane} />
          ))}
        </div>
      </div>
    </div>
  );
}

export default GitTopology;
