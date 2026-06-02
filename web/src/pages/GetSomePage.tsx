import { useEffect, useState, type CSSProperties } from "react";
import { AlertTriangle, Loader2, Sparkles } from "lucide-react";
import WorkNexus from "@/components/WorkNexus";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON } from "@/lib/api";
import "@/theme/pulse.css";

type ProjectsResponse = {
  scanned_at: string;
  projects: ProjectSummary[];
};

type RemainingWorkItem = {
  id?: string;
  status: string;
  title: string;
};

type ProjectSummary = {
  slug: string;
  name: string;
  icon: string;
  color: string;
  archived: boolean;
  total: number;
  completion_pct: number;
  by_status: Record<string, number>;
  active: number;
  blocked: number;
  last_activity: number | null;
  remaining: RemainingWorkItem[];
  remaining_count: number;
  remaining_by_status: Record<string, number>;
  remaining_more: number;
};

const STATUS_ORDER = ["running", "review", "blocked", "ready", "scheduled", "triage", "todo"];

function statusRank(status: string): number {
  const idx = STATUS_ORDER.indexOf(status);
  return idx === -1 ? STATUS_ORDER.length : idx;
}

function projectRemainingCount(project: ProjectSummary): number {
  return Number(project.remaining_count ?? 0);
}

function ProjectCard({
  project,
  selected,
  onOpen,
}: {
  project: ProjectSummary;
  selected: boolean;
  onOpen: (project: ProjectSummary) => void;
}) {
  const pct = Math.max(0, Math.min(100, project.completion_pct || 0));
  const color = project.color || "#76e4f7";
  const remainingCount = projectRemainingCount(project);
  const hasRemaining = remainingCount > 0 || pct < 100;
  const style = {
    "--get-some-project-color": color,
  } as CSSProperties;
  return (
    <button
      type="button"
      className={`get-some-project-card${hasRemaining ? " get-some-project-card--active" : " get-some-project-card--complete"}`}
      style={style}
      onClick={() => onOpen(project)}
      aria-haspopup="dialog"
      aria-expanded={selected}
      data-testid={`get-some-project-card-${project.slug}`}
    >
      <div className="get-some-project-card__identity">
        <span className="get-some-project-card__dot" aria-hidden="true" />
        <span className="get-some-project-card__icon" aria-hidden="true">
          {project.icon || "◆"}
        </span>
        <div className="get-some-project-card__name-wrap">
          <h2 className="get-some-project-card__name">{project.name}</h2>
          <span className="get-some-project-card__slug">{project.slug}</span>
        </div>
      </div>
      <div className="get-some-project-card__meter" aria-label={`${project.name} completion ${pct}%`}>
        <div className="get-some-project-card__bar-shell">
          <span className="get-some-project-card__bar" style={{ width: `${pct}%`, backgroundColor: color }} />
        </div>
        <span className="get-some-project-card__pct">{pct}%</span>
      </div>
      <div className="get-some-project-card__chips" aria-label={`${project.name} work counts`}>
        <span className="get-some-chip get-some-chip--cyan">left {remainingCount}</span>
        <span className="get-some-chip get-some-chip--red">blocked {project.blocked}</span>
        <span className="get-some-chip">total {project.total}</span>
      </div>
    </button>
  );
}

function RemainingWorkPanel({ project, onClose }: { project: ProjectSummary | null; onClose: () => void }) {
  useEffect(() => {
    if (!project) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [project, onClose]);

  if (!project) return null;

  const grouped = new Map<string, RemainingWorkItem[]>();
  for (const item of project.remaining ?? []) {
    const status = item.status || "unknown";
    grouped.set(status, [...(grouped.get(status) ?? []), item]);
  }
  const statusCounts = project.remaining_by_status ?? {};
  const statuses = Array.from(new Set([...Object.keys(statusCounts), ...grouped.keys()]))
    .filter((status) => (statusCounts[status] ?? grouped.get(status)?.length ?? 0) > 0)
    .sort((a, b) => statusRank(a) - statusRank(b) || a.localeCompare(b));
  const total = projectRemainingCount(project);

  return (
    <div className="get-some-remaining-backdrop" onClick={onClose} role="presentation">
      <aside
        className="get-some-remaining-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="get-some-remaining-title"
        data-testid="get-some-remaining-panel"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="get-some-remaining-panel__header">
          <div>
            <span className="get-some-remaining-panel__eyebrow">what's left</span>
            <h2 id="get-some-remaining-title">What's left in {project.name}</h2>
            <p>
              {total} non-terminal task{total === 1 ? "" : "s"} · {project.completion_pct}% complete
            </p>
          </div>
          <button
            type="button"
            className="get-some-remaining-panel__close"
            onClick={onClose}
            aria-label="Close remaining work panel"
          >
            ×
          </button>
        </div>
        <div className="get-some-remaining-panel__body">
          {total <= 0 && <div className="get-some-remaining-empty">No remaining kanban tasks — this project is fully complete.</div>}
          {statuses.map((status) => {
            const items = grouped.get(status) ?? [];
            const count = statusCounts[status] ?? items.length;
            const hidden = Math.max(0, count - items.length);
            return (
              <section key={status} className="get-some-remaining-group">
                <div className="get-some-remaining-group__title">
                  <span>{status}</span>
                  <strong>{count}</strong>
                </div>
                {items.length > 0 && (
                  <ul className="get-some-remaining-list">
                    {items.map((item) => (
                      <li key={`${item.status}:${item.id ?? item.title}`}>{item.title}</li>
                    ))}
                  </ul>
                )}
                {hidden > 0 && <div className="get-some-remaining-more">+{hidden} more</div>}
              </section>
            );
          })}
          {project.remaining_more > 0 && <div className="get-some-remaining-more get-some-remaining-more--total">+{project.remaining_more} more across the project</div>}
        </div>
      </aside>
    </div>
  );
}

export default function GetSomePage() {
  const { setTitle } = usePageHeader();
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedProject, setSelectedProject] = useState<ProjectSummary | null>(null);

  useEffect(() => {
    setTitle("Get Some");
  }, [setTitle]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchJSON<ProjectsResponse>("/api/dashboard/projects")
      .then((payload) => {
        if (cancelled) return;
        setProjects(payload.projects ?? []);
        setLoading(false);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setError(err.message || "Project roster unreachable");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="pulse-root get-some-root min-h-0 flex-1 flex flex-col">
      <section className="get-some-hero" aria-labelledby="get-some-title">
        <div>
          <div className="get-some-kicker">
            <Sparkles size={14} aria-hidden="true" />
            Get Some
          </div>
          <h1 id="get-some-title">Project roster + living work nexus</h1>
          <p>Boards across the top, shipped work blooming into the neon web below.</p>
        </div>
      </section>

      <section className="get-some-roster" aria-label="Project roster">
        {loading && (
          <div className="get-some-state" aria-busy="true">
            <Loader2 size={16} className="get-some-spin" aria-hidden="true" />
            Loading projects…
          </div>
        )}
        {!loading && error && (
          <div className="get-some-state get-some-state--error">
            <AlertTriangle size={16} aria-hidden="true" />
            {error}
          </div>
        )}
        {!loading && !error && projects.length === 0 && (
          <div className="get-some-state">No active kanban boards found.</div>
        )}
        {!loading && !error && projects.length > 0 && (
          <div className="get-some-roster__scroller">
            {projects.map((project) => (
              <ProjectCard
                key={project.slug}
                project={project}
                selected={selectedProject?.slug === project.slug}
                onOpen={setSelectedProject}
              />
            ))}
          </div>
        )}
      </section>

      <section className="get-some-nexus" aria-label="Work nexus">
        <div className="get-some-section-header">
          <span>Work Nexus</span>
          <small>polls every 15s · grows without resetting node positions</small>
        </div>
        <div className="get-some-nexus__canvas">
          <WorkNexus />
        </div>
      </section>
      <RemainingWorkPanel project={selectedProject} onClose={() => setSelectedProject(null)} />
    </div>
  );
}
