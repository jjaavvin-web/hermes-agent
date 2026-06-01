import { useEffect, useState } from "react";
import { AlertTriangle, Loader2, Sparkles } from "lucide-react";
import WorkNexus from "@/components/WorkNexus";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON } from "@/lib/api";
import "@/theme/pulse.css";

type ProjectsResponse = {
  scanned_at: string;
  projects: ProjectSummary[];
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
};

function ProjectCard({ project }: { project: ProjectSummary }) {
  const pct = Math.max(0, Math.min(100, project.completion_pct || 0));
  const color = project.color || "#76e4f7";
  return (
    <article className="get-some-project-card">
      <div className="get-some-project-card__identity">
        <span
          className="get-some-project-card__dot"
          style={{ backgroundColor: color, boxShadow: `0 0 14px ${color}` }}
          aria-hidden="true"
        />
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
        <span className="get-some-chip get-some-chip--cyan">active {project.active}</span>
        <span className="get-some-chip get-some-chip--red">blocked {project.blocked}</span>
        <span className="get-some-chip">total {project.total}</span>
      </div>
    </article>
  );
}

export default function GetSomePage() {
  const { setTitle } = usePageHeader();
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
              <ProjectCard key={project.slug} project={project} />
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
    </div>
  );
}
