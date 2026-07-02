/**
 * HTML Gallery — browse and preview Hermes-generated HTML files.
 *
 * Left sidebar: scrollable list of artifacts (reports from audits/ and
 * session replays from sessions/artifacts/).  Filter chips select kind.
 * Prev/Next buttons + ArrowLeft/ArrowRight keyboard navigation cycle
 * through the filtered list with wrap-around.
 *
 * Main pane: <iframe srcDoc=...> that renders fetched HTML inline.
 * srcdoc bypasses the global X-Frame-Options: deny header (which blocks
 * same-origin <iframe src=...> navigations).  The auth header is sent in
 * the fetch() call so the token never appears in any URL.  sandbox is
 * "allow-scripts" only (no allow-same-origin) so scripts run in an opaque
 * origin and cannot touch the parent dashboard.
 *
 * Data: GET /api/dashboard/artifacts
 * Serve: GET /api/dashboard/artifacts/raw?id=<opaque>  (header-authed)
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ArtifactItem {
  id: string;
  title: string;
  kind: "report" | "replay";
  group: string;
  mtime: number;
  size: number;
}

interface ArtifactsResponse {
  items: ArtifactItem[];
  counts: {
    reports?: number;
    replays?: number;
    total?: number;
  };
  replays_truncated: number;
  replay_total?: number;
  error?: string;
}

type FilterKind = "report" | "replay" | "all";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(mtime: number): string {
  const diff = Math.max(0, Date.now() / 1000 - mtime);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d ago`;
  const d = new Date(mtime * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function HtmlGalleryPage() {
  const { setTitle } = usePageHeader();
  useEffect(() => {
    setTitle("HTML");
  }, [setTitle]);

  // Start loading=true so the fetch effect doesn't need a synchronous setState.
  const [data, setData] = useState<ArtifactsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchErr, setFetchErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterKind>("report");

  // User-chosen id; null until user clicks something (auto-select via memo below).
  const [chosenId, setChosenId] = useState<string | null>(null);

  // Viewer state.
  // viewerLoadedId tracks which artifact's HTML is currently in viewerHtml.
  // viewerLoading is DERIVED (not stored): it's true whenever selectedId exists
  // but doesn't match what's already loaded — this avoids any synchronous
  // setState inside the fetch effect, which the set-state-in-effect rule bans.
  const [viewerHtml, setViewerHtml] = useState<string | null>(null);
  const [viewerLoadedId, setViewerLoadedId] = useState<string | null>(null);

  // Fetch artifact list once on mount.
  useEffect(() => {
    let cancelled = false;
    fetchJSON<ArtifactsResponse>("/api/dashboard/artifacts")
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setFetchErr(d.error ?? null);
      })
      .catch((e: Error) => {
        if (!cancelled) setFetchErr(e.message || "fetch failed");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Filtered list based on active chip.
  const filtered = useMemo<ArtifactItem[]>(() => {
    if (!data) return [];
    if (filter === "all") return data.items;
    return data.items.filter((item) => item.kind === filter);
  }, [data, filter]);

  // Effective selection: use chosenId if it's in the filtered list,
  // otherwise fall back to the first item.  Pure derivation — no useEffect.
  const selectedId = useMemo<string | null>(() => {
    if (filtered.length === 0) return null;
    if (chosenId !== null && filtered.some((i) => i.id === chosenId)) return chosenId;
    return filtered[0].id;
  }, [filtered, chosenId]);

  const selectedIdx = useMemo(
    () => filtered.findIndex((i) => i.id === selectedId),
    [filtered, selectedId],
  );
  const selected = selectedIdx >= 0 ? filtered[selectedIdx] : null;

  // viewerLoading is derived: we're loading when a selection exists but the
  // content for it hasn't arrived yet.  No setState call required.
  const viewerLoading = selectedId !== null && viewerLoadedId !== selectedId;

  const goNext = useCallback(() => {
    if (filtered.length === 0) return;
    setChosenId(filtered[(selectedIdx + 1) % filtered.length].id);
  }, [filtered, selectedIdx]);

  const goPrev = useCallback(() => {
    if (filtered.length === 0) return;
    setChosenId(filtered[(selectedIdx - 1 + filtered.length) % filtered.length].id);
  }, [filtered, selectedIdx]);

  // Keyboard navigation.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      if (e.key === "ArrowRight") goNext();
      if (e.key === "ArrowLeft") goPrev();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [goNext, goPrev]);

  // Fetch the selected artifact's HTML when the selection changes.
  // All setState calls are inside async .then/.catch callbacks — never
  // synchronously in the effect body — to satisfy react-hooks/set-state-in-effect.
  // window.__HERMES_SESSION_TOKEN__ is declared in api.ts's global Window augmentation.
  useEffect(() => {
    if (!selectedId) return;
    const id = selectedId;
    let cancelled = false;
    const token = window.__HERMES_SESSION_TOKEN__ ?? "";
    fetch(`/api/dashboard/artifacts/raw?id=${encodeURIComponent(id)}`, {
      headers: { "X-Hermes-Session-Token": token },
    })
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((html) => {
        if (!cancelled) {
          setViewerHtml(html);
          setViewerLoadedId(id);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setViewerHtml('<p style="font-family:sans-serif;padding:1rem">Failed to load this artifact.</p>');
          setViewerLoadedId(id);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const replayNote =
    filter === "replay" &&
    data?.replays_truncated != null &&
    data.replays_truncated > 0 ? (
      <p className="text-xs text-text-secondary mt-1 px-1">
        showing 500 most recent of {data.replay_total ?? 500 + data.replays_truncated}
      </p>
    ) : null;

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div className="flex min-h-0 flex-1 flex-col" style={{ height: "calc(100vh - 5rem)" }}>
      {/* Filter chips */}
      <div className="flex items-center gap-2 pb-3 flex-wrap">
        {(["report", "replay", "all"] as FilterKind[]).map((kind) => {
          const labels: Record<FilterKind, string> = {
            report: "Reports & Mockups",
            replay: "Session Replays",
            all: "All",
          };
          const counts: Record<FilterKind, number> = {
            report: data?.counts.reports ?? 0,
            replay: data?.counts.replays ?? 0,
            all: data?.counts.total ?? 0,
          };
          const active = filter === kind;
          return (
            <button
              key={kind}
              onClick={() => setFilter(kind)}
              className={[
                "px-3 py-1 rounded text-xs font-medium border transition-colors",
                active
                  ? "bg-midground/20 text-midground border-midground/40"
                  : "text-text-secondary border-current/20 hover:text-midground hover:border-midground/30",
              ].join(" ")}
            >
              {labels[kind]}
              {!loading && data ? (
                <span className="ml-1 opacity-60">({counts[kind]})</span>
              ) : null}
            </button>
          );
        })}

        {/* Nav controls */}
        {filtered.length > 0 && (
          <div className="ml-auto flex items-center gap-2 text-xs text-text-secondary">
            <button
              onClick={goPrev}
              aria-label="Previous"
              className="p-1 hover:text-midground transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <span>
              {selectedIdx + 1} / {filtered.length}
            </span>
            <button
              onClick={goNext}
              aria-label="Next"
              className="p-1 hover:text-midground transition-colors"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>

      {/* Body: sidebar + preview */}
      <div className="flex min-h-0 flex-1 gap-3">
        {/* Sidebar */}
        <aside className="w-64 shrink-0 flex flex-col min-h-0 border-r border-current/10 pr-2">
          {loading && (
            <p className="text-xs text-text-secondary mt-4 text-center">Loading…</p>
          )}
          {!loading && fetchErr && (
            <p className="text-xs text-destructive mt-4 px-1">{fetchErr}</p>
          )}
          {!loading && !fetchErr && filtered.length === 0 && (
            <p className="text-xs text-text-secondary mt-4 text-center">
              No HTML artifacts found.
            </p>
          )}
          {replayNote}
          <ul className="flex-1 overflow-y-auto space-y-0.5">
            {filtered.map((item) => {
              const active = item.id === selectedId;
              return (
                <li key={item.id}>
                  <button
                    onClick={() => setChosenId(item.id)}
                    className={[
                      "w-full text-left px-2 py-2 rounded text-xs transition-colors",
                      active
                        ? "bg-midground/15 text-midground"
                        : "text-text-secondary hover:bg-midground/8 hover:text-midground",
                    ].join(" ")}
                  >
                    <p className="truncate font-medium leading-snug">{item.title}</p>
                    <p className="truncate text-[0.65rem] opacity-60 mt-0.5">
                      {item.group} · {relativeTime(item.mtime)} · {fmtSize(item.size)}
                    </p>
                  </button>
                </li>
              );
            })}
          </ul>
        </aside>

        {/* Preview pane — srcdoc iframe bypasses X-Frame-Options: deny */}
        <div className="flex-1 min-h-0 min-w-0 flex flex-col">
          {selected ? (
            viewerLoading ? (
              <div className="flex-1 flex items-center justify-center text-text-secondary text-sm">
                Loading…
              </div>
            ) : (
              <iframe
                key={selected.id}
                srcDoc={viewerHtml ?? ""}
                sandbox="allow-scripts"
                className="w-full flex-1 min-h-0 rounded border border-current/10 bg-white"
                title={selected.title}
              />
            )
          ) : (
            <div className="flex-1 flex items-center justify-center text-text-secondary text-sm">
              {loading ? "Loading…" : "Select an item to preview"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
