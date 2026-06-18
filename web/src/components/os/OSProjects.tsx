import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Archive, CheckCircle2, FolderKanban, RefreshCw } from "lucide-react";
import { api, type ProjectSnapshot, type ProjectsSnapshot } from "@/lib/api";

function fmtNum(value: number | undefined): string {
  const n = value ?? 0;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(n);
}

function fmtRelativeTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  const then = epochSeconds * 1000;
  const deltaSeconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (deltaSeconds < 60) return "just now";
  const units: Array<[string, number]> = [
    ["y", 365 * 24 * 60 * 60],
    ["mo", 30 * 24 * 60 * 60],
    ["d", 24 * 60 * 60],
    ["h", 60 * 60],
    ["m", 60],
  ];
  const [suffix, seconds] = units.find(([, seconds]) => deltaSeconds >= seconds) ?? ["m", 60];
  return `${Math.floor(deltaSeconds / seconds)}${suffix} ago`;
}

function statusClass(status: string): string {
  if (status === "blocked") return "border-[rgba(255,189,56,0.35)] bg-[rgba(255,189,56,0.10)] text-[#ffbd38]";
  if (status === "done" || status === "archived") return "border-[rgba(74,222,128,0.25)] bg-[rgba(74,222,128,0.08)] text-[#4ade80]";
  if (status === "running" || status === "ready" || status === "review") return "border-[rgba(107,155,209,0.35)] bg-[rgba(107,155,209,0.10)] text-[#6b9bd1]";
  return "border-border bg-background/35 text-text-secondary";
}

function sortedProjects(projects: ProjectSnapshot[], showArchived: boolean): ProjectSnapshot[] {
  return projects
    .filter((project) => showArchived || !project.archived)
    .slice()
    .sort((a, b) => {
      const aActive = Number(a.active > 0 || a.blocked > 0 || a.remaining_count > 0);
      const bActive = Number(b.active > 0 || b.blocked > 0 || b.remaining_count > 0);
      if (aActive !== bActive) return bActive - aActive;
      if (a.blocked !== b.blocked) return b.blocked - a.blocked;
      if (a.active !== b.active) return b.active - a.active;
      return (b.last_activity ?? 0) - (a.last_activity ?? 0);
    });
}

function ProgressBar({ pct, warn }: { pct: number; warn: boolean }) {
  const value = Math.max(0, Math.min(100, pct));
  return (
    <div className="h-2 overflow-hidden rounded-full bg-[rgba(124,145,168,0.14)]" aria-label={`${value}% complete`}>
      <div
        className={`h-full rounded-full ${warn ? "bg-[#ffbd38]" : "bg-[#4ade80]"}`}
        style={{ width: `${value}%` }}
      />
    </div>
  );
}

function ProjectCard({ project }: { project: ProjectSnapshot }) {
  const done = project.by_status.done ?? 0;
  const warn = project.blocked > 0;
  const color = project.color || "#6b9bd1";
  const statusEntries = Object.entries(project.by_status).sort((a, b) => b[1] - a[1]);
  const remaining = project.remaining ?? [];

  return (
    <article
      className={`flex min-w-0 flex-col gap-3 rounded-xl border bg-card/45 p-4 shadow-sm transition hover:border-accent/40 ${
        warn ? "border-[rgba(255,189,56,0.45)] ring-1 ring-[rgba(255,189,56,0.16)]" : "border-border"
      }`}
    >
      <div className="flex min-w-0 items-start gap-3">
        <div
          className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg border text-lg"
          style={{ borderColor: `${color}66`, background: `${color}18` }}
        >
          {project.icon || <FolderKanban className="h-4 w-4" style={{ color }} />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="min-w-0 truncate text-sm font-semibold text-text-primary">{project.name}</h3>
            {project.archived && (
              <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full border border-border bg-background/35 px-1.5 py-0.5 text-[10px] uppercase text-text-tertiary">
                <Archive className="h-3 w-3" /> archived
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-text-tertiary">
            <span className="font-mono">{project.slug}</span>
            <span>· {fmtRelativeTime(project.last_activity)}</span>
          </div>
        </div>
        {warn ? (
          <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full border border-[rgba(255,189,56,0.35)] bg-[rgba(255,189,56,0.10)] px-2 py-1 text-[11px] font-semibold text-[#ffbd38]">
            <AlertTriangle className="h-3 w-3" /> {project.blocked} blocked
          </span>
        ) : (
          <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full border border-[rgba(74,222,128,0.28)] bg-[rgba(74,222,128,0.08)] px-2 py-1 text-[11px] font-semibold text-[#4ade80]">
            <CheckCircle2 className="h-3 w-3" /> clear
          </span>
        )}
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-text-tertiary">Completion</span>
          <span className="font-mono font-semibold text-text-primary">{project.completion_pct}%</span>
        </div>
        <ProgressBar pct={project.completion_pct} warn={warn} />
      </div>

      <div className="grid grid-cols-4 gap-2 text-center text-xs">
        <div className="rounded-lg border border-border bg-background/30 px-2 py-1.5"><div className="font-mono text-text-primary">{fmtNum(project.active)}</div><div className="text-[10px] uppercase text-text-tertiary">active</div></div>
        <div className="rounded-lg border border-[rgba(255,189,56,0.25)] bg-[rgba(255,189,56,0.06)] px-2 py-1.5"><div className="font-mono text-[#ffbd38]">{fmtNum(project.blocked)}</div><div className="text-[10px] uppercase text-text-tertiary">blocked</div></div>
        <div className="rounded-lg border border-border bg-background/30 px-2 py-1.5"><div className="font-mono text-text-primary">{fmtNum(done)}</div><div className="text-[10px] uppercase text-text-tertiary">done</div></div>
        <div className="rounded-lg border border-border bg-background/30 px-2 py-1.5"><div className="font-mono text-text-primary">{fmtNum(project.total)}</div><div className="text-[10px] uppercase text-text-tertiary">total</div></div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {statusEntries.map(([status, count]) => (
          <span key={status} className={`rounded-full border px-2 py-0.5 text-[11px] ${statusClass(status)}`}>
            {status} <span className="font-mono">{count}</span>
          </span>
        ))}
      </div>

      {remaining.length > 0 && (
        <details className="group rounded-lg border border-border bg-background/25 px-3 py-2">
          <summary className="cursor-pointer list-none text-xs font-semibold text-text-secondary transition hover:text-text-primary">
            {project.remaining_count} remaining work item{project.remaining_count === 1 ? "" : "s"}
            {project.remaining_more > 0 ? ` · +${project.remaining_more} more` : ""}
          </summary>
          <div className="mt-2 flex flex-col gap-1.5">
            {remaining.map((item) => (
              <div key={item.id} className="flex min-w-0 items-center gap-2 text-[11px]">
                <span className={`flex-shrink-0 rounded border px-1.5 py-0.5 ${statusClass(item.status)}`}>{item.status}</span>
                <span className="min-w-0 truncate text-text-secondary">{item.title}</span>
              </div>
            ))}
          </div>
        </details>
      )}
    </article>
  );
}

export function OSProjects() {
  const [snapshot, setSnapshot] = useState<ProjectsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.getProjectsSnapshot()
      .then((data) => {
        if (cancelled) return;
        setSnapshot(data);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const projects = useMemo(() => sortedProjects(snapshot?.projects ?? [], showArchived), [snapshot?.projects, showArchived]);
  const hiddenArchived = (snapshot?.projects ?? []).filter((project) => project.archived).length;
  const activeTotal = projects.reduce((sum, project) => sum + project.active, 0);
  const blockedTotal = projects.reduce((sum, project) => sum + project.blocked, 0);

  if (loading && !snapshot) {
    return <div className="mt-3 flex min-h-[300px] items-center justify-center rounded-lg border border-border bg-card/40 text-sm text-text-tertiary"><RefreshCw className="mr-2 h-4 w-4 animate-spin" />Loading projects…</div>;
  }

  if (error && !snapshot) {
    return <div className="mt-3 rounded-lg border border-[rgba(255,189,56,0.35)] bg-[rgba(255,189,56,0.08)] p-4 text-sm text-[#ffbd38]">Project snapshot unavailable: {error}</div>;
  }

  return (
    <div className="mt-3 flex min-h-0 flex-col gap-3 pb-8">
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card/40 px-3 py-2 text-xs">
        <FolderKanban className="h-4 w-4 text-text-tertiary" />
        <span className="font-mondwest tracking-[0.14em] text-text-primary">PROJECTS</span>
        <span className="text-text-tertiary">· {projects.length} boards</span>
        <span className="rounded-full bg-[rgba(107,155,209,0.10)] px-2 py-0.5 text-[#6b9bd1]">{fmtNum(activeTotal)} active</span>
        <span className="rounded-full bg-[rgba(255,189,56,0.10)] px-2 py-0.5 text-[#ffbd38]">{fmtNum(blockedTotal)} blocked</span>
        {snapshot?.scanned_at && <span className="text-text-tertiary">scanned {new Date(snapshot.scanned_at).toLocaleTimeString()}</span>}
        {hiddenArchived > 0 && (
          <button
            type="button"
            onClick={() => setShowArchived((value) => !value)}
            className="ml-auto rounded-md border border-border px-2 py-1 text-text-tertiary transition hover:text-text-primary"
          >
            {showArchived ? "Hide" : "Show"} archived ({hiddenArchived})
          </button>
        )}
      </div>

      {error && <div className="rounded-md border border-[rgba(255,189,56,0.3)] bg-[rgba(255,189,56,0.07)] px-3 py-2 text-xs text-[#ffbd38]">Refresh failed: {error}</div>}

      <div className="grid grid-cols-1 items-start gap-3 lg:grid-cols-2 2xl:grid-cols-3">
        {projects.map((project) => <ProjectCard key={project.slug} project={project} />)}
      </div>
      {projects.length === 0 && <div className="flex min-h-[160px] items-center justify-center rounded-lg border border-border bg-card/40 text-sm text-text-tertiary">No live project boards found.</div>}
    </div>
  );
}

export default OSProjects;
