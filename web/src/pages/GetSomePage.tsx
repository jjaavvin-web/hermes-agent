import { useEffect, useState, type CSSProperties } from "react";
import { AlertTriangle, Loader2, Sparkles } from "lucide-react";
import WorkNexus from "@/components/WorkNexus";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON } from "@/lib/api";
import "@/theme/pulse.css";

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

type RuntimeSummary = {
  name?: string;
  label?: string;
  status?: string;
  detail?: string;
  latencyMs?: number | null;
};

type LiveSession = {
  id?: string;
  preview?: string;
  title?: string;
  modelUsed?: string;
};

type LiveSnapshot = {
  runtimes?: RuntimeSummary[];
  active_sessions?: LiveSession[];
  recentSessions?: LiveSession[];
  swarm?: Record<string, unknown> | null;
  nextCron?: Record<string, unknown> | null;
  spendToday?: number | null;
  spendWeek?: number | null;
  streakDays?: number | null;
  model?: string | null;
};

type DecisionItem = {
  title: string;
  source: string;
  reason: string;
  link_or_id?: string | null;
};

type StalledItem = {
  title: string;
  project: string;
  status: string;
  idle_for: string;
  why: string;
};

type ResumeSnapshot =
  | string
  | {
      summary?: string;
      text?: string;
      preview?: string;
      title?: string;
      session_id?: string;
      sessionId?: string;
      [key: string]: unknown;
    };

type CommandCenterResponse = {
  projects: ProjectSummary[];
  live: LiveSnapshot;
  decisions: DecisionItem[];
  stalled: StalledItem[];
  resume: ResumeSnapshot | null;
};

const STATUS_ORDER = ["running", "review", "blocked", "ready", "scheduled", "triage", "todo"];
const POLL_MS = 15_000;

function statusRank(status: string): number {
  const idx = STATUS_ORDER.indexOf(status);
  return idx === -1 ? STATUS_ORDER.length : idx;
}

function projectRemainingCount(project: ProjectSummary): number {
  return Number(project.remaining_count ?? 0);
}

function runtimeTone(status?: string): string {
  const normalized = String(status || "unknown").toLowerCase();
  if (["online", "running", "active", "enabled", "ok"].includes(normalized)) return "ok";
  if (["degraded", "auth_gated", "stopped", "warn"].includes(normalized)) return "warn";
  if (["offline", "error", "failed", "bad"].includes(normalized)) return "bad";
  return "unknown";
}

function displayResume(resume: ResumeSnapshot | null | undefined): string {
  if (!resume) return "No resume context available yet.";
  if (typeof resume === "string") return resume;
  return (
    String(resume.summary || resume.text || resume.preview || resume.title || "") ||
    "Resume context is available — open Hermes resume for the full thread."
  );
}

function displaySession(session: LiveSession): string {
  return String(session.preview || session.title || session.id || "Recent session");
}

function actionHref(value?: string | null): string | null {
  const text = String(value || "").trim();
  if (!text) return null;
  if (text.startsWith("/") || text.startsWith("http://") || text.startsWith("https://")) return text;
  return null;
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

function DecisionsPanel({ decisions }: { decisions: DecisionItem[] }) {
  return (
    <section className="get-some-zone get-some-decisions" aria-labelledby="get-some-decisions-title">
      <div className="get-some-zone__header">
        <span>Zone ③</span>
        <h2 id="get-some-decisions-title">③ Decisions waiting on you</h2>
        <small>{decisions.length} action{decisions.length === 1 ? "" : "s"}</small>
      </div>
      {decisions.length === 0 ? (
        <div className="get-some-empty">No blocked cards or PR reviews waiting right now.</div>
      ) : (
        <ul className="get-some-action-list">
          {decisions.map((item, idx) => {
            const href = actionHref(item.link_or_id);
            const body = (
              <>
                <strong>{item.title}</strong>
                <span>{item.reason}</span>
                <small>{item.source}</small>
              </>
            );
            return (
              <li key={`${item.source}:${item.title}:${idx}`}>
                {href ? (
                  <a href={href} target={href.startsWith("http") ? "_blank" : undefined} rel={href.startsWith("http") ? "noreferrer" : undefined}>
                    {body}
                  </a>
                ) : (
                  <div>{body}</div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function LiveStalledPanel({ live, stalled }: { live: LiveSnapshot; stalled: StalledItem[] }) {
  const runtimes = live.runtimes ?? [];
  const sessions = live.active_sessions ?? live.recentSessions ?? [];
  return (
    <section className="get-some-zone get-some-live" aria-labelledby="get-some-live-title">
      <div className="get-some-zone__header">
        <span>Zone ④</span>
        <h2 id="get-some-live-title">④ Live + Stalled</h2>
        <small>{stalled.length} stalled</small>
      </div>
      <div className="get-some-live__runtimes" aria-label="Live runtimes">
        {runtimes.length === 0 ? (
          <span className="get-some-empty get-some-empty--compact">No runtime probes returned.</span>
        ) : (
          runtimes.map((runtime) => (
            <span key={runtime.name || runtime.label} className={`get-some-runtime get-some-runtime--${runtimeTone(runtime.status)}`}>
              <i aria-hidden="true" />
              {runtime.label || runtime.name || "runtime"}
              <small>{runtime.status || "unknown"}</small>
            </span>
          ))
        )}
      </div>
      <div className="get-some-live__sessions">
        <h3>Active sessions</h3>
        {sessions.length === 0 ? (
          <div className="get-some-empty get-some-empty--compact">No recent sessions surfaced by Mission Control.</div>
        ) : (
          <ul>
            {sessions.slice(0, 4).map((session, idx) => (
              <li key={session.id || idx}>{displaySession(session)}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="get-some-live__stalled">
        <h3>Stalled / idle workers</h3>
        {stalled.length === 0 ? (
          <div className="get-some-empty get-some-empty--compact">No stale heartbeat, dead PID, or failure signals.</div>
        ) : (
          <ul>
            {stalled.map((item, idx) => (
              <li key={`${item.project}:${item.title}:${idx}`}>
                <strong>{item.title}</strong>
                <span>{item.project} · {item.status} · idle {item.idle_for}</span>
                <small>{item.why}</small>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

export default function GetSomePage() {
  const { setTitle } = usePageHeader();
  const [snapshot, setSnapshot] = useState<CommandCenterResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedProject, setSelectedProject] = useState<ProjectSummary | null>(null);

  useEffect(() => {
    setTitle("Command Center");
  }, [setTitle]);

  useEffect(() => {
    let cancelled = false;
    const load = (initial = false) => {
      if (initial) setLoading(true);
      fetchJSON<CommandCenterResponse>("/api/dashboard/command-center")
        .then((payload) => {
          if (cancelled) return;
          setSnapshot({
            projects: payload.projects ?? [],
            live: payload.live ?? {},
            decisions: payload.decisions ?? [],
            stalled: payload.stalled ?? [],
            resume: payload.resume ?? null,
          });
          setError(null);
          setLoading(false);
        })
        .catch((err: Error) => {
          if (cancelled) return;
          setError(err.message || "Command Center snapshot unreachable");
          setLoading(false);
        });
    };
    load(true);
    const timer = window.setInterval(() => load(false), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const projects = snapshot?.projects ?? [];
  const live = snapshot?.live ?? {};
  const decisions = snapshot?.decisions ?? [];
  const stalled = snapshot?.stalled ?? [];
  const resumeText = displayResume(snapshot?.resume);

  return (
    <div className="pulse-root get-some-root min-h-0 flex-1 flex flex-col">
      <section className="get-some-hero" aria-labelledby="get-some-title">
        <div>
          <div className="get-some-kicker">
            <Sparkles size={14} aria-hidden="true" />
            Command Center
          </div>
          <h1 id="get-some-title">Get Some — unified operator view</h1>
          <p>Architecture, project status, decisions, live runtime, and stalled work in one cockpit.</p>
        </div>
        <div className="get-some-hero__poll">polls every 15s</div>
      </section>

      {loading && !snapshot && (
        <div className="get-some-state" aria-busy="true">
          <Loader2 size={16} className="get-some-spin" aria-hidden="true" />
          Loading Command Center…
        </div>
      )}
      {!loading && error && !snapshot && (
        <div className="get-some-state get-some-state--error">
          <AlertTriangle size={16} aria-hidden="true" />
          {error}
        </div>
      )}

      {snapshot && (
        <>
          {error && (
            <div className="get-some-state get-some-state--error get-some-state--compact">
              <AlertTriangle size={16} aria-hidden="true" />
              Showing last snapshot · {error}
            </div>
          )}

          <section className="get-some-resume-strip" aria-label="Where we left off">
            <span>Where we left off</span>
            <strong>{resumeText}</strong>
          </section>

          <div className="get-some-command-grid">
            <section className="get-some-zone get-some-topology" aria-labelledby="get-some-topology-title">
              <div className="get-some-zone__header">
                <span>Zone ①</span>
                <h2 id="get-some-topology-title">① Architecture topology</h2>
                <small>Phase 1 linkout</small>
              </div>
              <p>The OS tab owns the full topology map. Phase 1 keeps this card as the fast route into that view.</p>
              <a className="get-some-topology__link" href="/os">
                Open OS →
              </a>
            </section>

            <section className="get-some-zone get-some-project-zone" aria-labelledby="get-some-project-title">
              <div className="get-some-zone__header">
                <span>Zone ②</span>
                <h2 id="get-some-project-title">② Project status</h2>
                <small>{projects.length} board{projects.length === 1 ? "" : "s"}</small>
              </div>
              <div className="get-some-roster" aria-label="Project roster">
                {projects.length === 0 ? (
                  <div className="get-some-state">No active kanban boards found.</div>
                ) : (
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
              </div>
              <div className="get-some-nexus" aria-label="Work nexus">
                <div className="get-some-section-header">
                  <span>Work Nexus</span>
                  <small>living work graph · keeps node positions stable</small>
                </div>
                <div className="get-some-nexus__canvas">
                  <WorkNexus />
                </div>
              </div>
            </section>

            <DecisionsPanel decisions={decisions} />
            <LiveStalledPanel live={live} stalled={stalled} />
          </div>
        </>
      )}
      <RemainingWorkPanel project={selectedProject} onClose={() => setSelectedProject(null)} />
    </div>
  );
}
