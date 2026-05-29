/**
 * Git Health — dashboard tab.
 *
 * One row per tracked codex session: uncommitted count, reviewable diff size
 * (3-dot vs fork/main), and the recommended next move. The top banner is the
 * single best move across all sessions. Data: /api/dashboard/git-health.
 * Light 15s poll + manual refresh; no SSE (kept deliberately simple).
 */

import { useEffect, useState } from "react";
import { GitCommit, GitPullRequest, RefreshCw } from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON } from "@/lib/api";

type Severity = "ready" | "warn" | "bad" | "idle";

interface Row {
  slug: string;
  thread_id: string | null;
  worktree: boolean;
  uncommitted: number | null;
  files_changed: number | null;
  recommendation: string;
  severity: Severity;
}

interface GitHealth {
  scanned_at: string;
  best_move: { text: string; severity: Severity };
  rows: Row[];
}

const SEV: Record<Severity, { chip: string; dot: string; label: string }> = {
  ready: { chip: "bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)]", dot: "bg-[#4ade80] shadow-[0_0_6px_#4ade80]", label: "Ready" },
  warn:  { chip: "bg-[rgba(255,189,56,0.12)] text-[#ffbd38] border border-[rgba(255,189,56,0.35)]", dot: "bg-[#ffbd38]", label: "Commit first" },
  bad:   { chip: "bg-[rgba(251,44,54,0.12)] text-[#fb2c36] border border-[rgba(251,44,54,0.35)]", dot: "bg-[#fb2c36]", label: "Needs fixing" },
  idle:  { chip: "bg-white/[0.04] text-white/50 border border-white/10", dot: "bg-[#7c91a8]", label: "Idle" },
};

function fmtTs(iso?: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

export default function GitHealthPage() {
  const [data, setData] = useState<GitHealth | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const { setTitle } = usePageHeader();
  useEffect(() => {
    setTitle("Git Health");
  }, [setTitle]);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    fetchJSON<GitHealth>("/api/dashboard/git-health")
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: Error) => {
        if (!cancelled) setErr(e.message || "fetch failed");
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  // Light auto-refresh every 15s.
  useEffect(() => {
    const id = window.setInterval(() => setReloadKey((k) => k + 1), 15000);
    return () => window.clearInterval(id);
  }, []);

  const best = data?.best_move;
  const bestSev = best ? SEV[best.severity] : SEV.idle;

  return (
    <div className="px-6 py-4 max-w-[1100px] mx-auto">
      <div className="flex items-center gap-3 mb-4 text-sm">
        <button
          onClick={() => setReloadKey((k) => k + 1)}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded border border-white/10 bg-white/[0.03] hover:bg-white/[0.06] text-white/70"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </button>
        {data && (
          <span className="text-white/40 ml-auto">
            {data.rows.length} session{data.rows.length === 1 ? "" : "s"} · checked {fmtTs(data.scanned_at)}
          </span>
        )}
      </div>

      {/* Best recommended move */}
      {best && (
        <div className={`mb-5 px-4 py-3 rounded-lg text-sm flex items-start gap-2.5 ${bestSev.chip}`}>
          <span className={`mt-1 h-2 w-2 rounded-full ${bestSev.dot}`} />
          <div>
            <div className="text-[11px] uppercase tracking-wide opacity-70">Recommended move</div>
            <div className="font-medium">{best.text}</div>
          </div>
        </div>
      )}

      {err && (
        <div className="mb-4 px-3 py-2 rounded border border-[rgba(251,44,54,0.35)] bg-[rgba(251,44,54,0.08)] text-[#fb2c36] text-sm">
          Could not load /api/dashboard/git-health: {err}
        </div>
      )}

      {data && data.rows.length === 0 && (
        <div className="text-center py-16 text-white/40 text-sm">No tracked codex sessions.</div>
      )}

      <div className="space-y-2">
        {data?.rows.map((r) => {
          const sev = SEV[r.severity];
          return (
            <div
              key={r.thread_id || r.slug}
              className="px-4 py-3 rounded-lg border border-white/10 bg-white/[0.02] hover:bg-white/[0.04] transition-colors"
            >
              <div className="flex items-center gap-3 flex-wrap">
                <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs ${sev.chip}`}>
                  <span className={`h-1.5 w-1.5 rounded-full ${sev.dot}`} />
                  {sev.label}
                </span>
                <span className="font-mono text-sm text-white/85">{r.slug}</span>
                {r.worktree ? (
                  <span className="text-xs text-white/45 inline-flex items-center gap-3 flex-wrap">
                    <span className="inline-flex items-center gap-1">
                      <GitCommit className="h-3.5 w-3.5" />
                      {r.uncommitted} uncommitted
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <GitPullRequest className="h-3.5 w-3.5" />
                      {r.files_changed} file{r.files_changed === 1 ? "" : "s"} vs fork/main
                    </span>
                  </span>
                ) : (
                  <span className="text-xs text-white/30">no worktree</span>
                )}
                {r.thread_id && (
                  <a
                    href={`https://discord.com/channels/@me/${r.thread_id}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-white/40 hover:text-white/70 ml-auto"
                  >
                    open thread →
                  </a>
                )}
              </div>
              <div className="mt-1.5 text-xs text-white/60">{r.recommendation}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
