/**
 * HTML Gallery — browse, preview, and download Hermes-generated HTML files.
 *
 * Two views:
 *
 *  "Latest" (default) — the five newest reports, at most one per audit group,
 *  rendered as live thumbnail cards (newest gets a hero slot).  The list is
 *  re-sorted by mtime CLIENT-SIDE: the backend floats legacy "featured"
 *  path-fragments to the top, which buries genuinely new reports (e.g. the
 *  daily morning config/health + work-recap pages) under stale mockups.
 *  Clicking a card opens a full-size viewer with prev/next.
 *
 *  "Archive" — the complete list in the original sidebar + preview layout,
 *  for digging through history.
 *
 *  Every card and every viewer has a Download action: the HTML is fetched
 *  with the auth header (token never in a URL), wrapped in a Blob, and saved
 *  via a temporary <a download>.  Filenames come from decoding the opaque
 *  artifact id (url-safe base64 of the ~/.hermes-relative path) — generic
 *  basenames like index.html fall back to the group name.
 *
 *  Rendering: <iframe srcDoc=...> bypasses the global X-Frame-Options: deny
 *  header.  Thumbnails run with sandbox="" (no scripts — cheap + inert);
 *  full viewers use sandbox="allow-scripts" in an opaque origin, so embedded
 *  scripts cannot touch the parent dashboard.
 *
 * Data:  GET /api/dashboard/artifacts
 * Serve: GET /api/dashboard/artifacts/raw?id=<opaque>  (header-authed)
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  LayoutGrid,
  List,
} from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON } from "@/lib/api";
import {
  cacheKeyFor,
  fetchArtifactHtml,
  filenameFor,
  pickLatest,
  stripScripts,
  type ArtifactItem,
} from "@/lib/htmlArtifacts";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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

type ViewMode = "latest" | "archive";

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
// Artifact HTML loading (fetch + cache live in lib/htmlArtifacts)
// ---------------------------------------------------------------------------

/** Load an artifact's HTML (cached). */
function useArtifactHtml(item: ArtifactItem | null): {
  html: string | null;
  failed: boolean;
  loading: boolean;
} {
  const key = item ? cacheKeyFor(item) : null;
  const [state, setState] = useState<{ key: string | null; html: string | null; failed: boolean }>({
    key: null,
    html: null,
    failed: false,
  });

  useEffect(() => {
    if (!item || !key) return;
    let cancelled = false;
    fetchArtifactHtml(item)
      .then((html) => {
        if (!cancelled) setState({ key, html, failed: false });
      })
      .catch(() => {
        if (!cancelled) setState({ key, html: null, failed: true });
      });
    return () => {
      cancelled = true;
    };
  }, [item, key]);

  const current = state.key === key;
  return {
    html: current ? state.html : null,
    failed: current && state.failed,
    loading: key !== null && !current,
  };
}

// ---------------------------------------------------------------------------
// Download
// ---------------------------------------------------------------------------

type DownloadState = "idle" | "busy" | "error";

function useDownload(item: ArtifactItem): { state: DownloadState; trigger: () => void } {
  const [state, setState] = useState<DownloadState>("idle");

  const trigger = useCallback(() => {
    if (state === "busy") return;
    setState("busy");
    fetchArtifactHtml(item)
      .then((html) => {
        const blob = new Blob([html], { type: "text/html" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filenameFor(item);
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        setState("idle");
      })
      .catch(() => {
        setState("error");
        setTimeout(() => setState("idle"), 2500);
      });
  }, [item, state]);

  return { state, trigger };
}

function DownloadButton({ item, compact = false }: { item: ArtifactItem; compact?: boolean }) {
  const { state, trigger } = useDownload(item);
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        trigger();
      }}
      aria-label={`Download ${filenameFor(item)}`}
      title={`Download ${filenameFor(item)}`}
      className={[
        "inline-flex items-center gap-1 rounded border transition-colors shrink-0",
        compact ? "px-1.5 py-1" : "px-2 py-1 text-xs font-medium",
        state === "error"
          ? "text-destructive border-destructive/40"
          : "text-text-secondary border-current/20 hover:text-midground hover:border-midground/40",
      ].join(" ")}
    >
      <Download className="h-3.5 w-3.5" />
      {!compact && (state === "busy" ? "Saving…" : state === "error" ? "Failed" : "Download")}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Thumbnail card
// ---------------------------------------------------------------------------

/** Design width the thumbnail iframe renders at before being scaled down. */
const THUMB_DESIGN_WIDTH = 1280;

/** Track the rendered width of a container so the fixed-width iframe can be
 *  transform-scaled to fit it exactly. */
function useFitScale(): { ref: React.RefObject<HTMLDivElement | null>; scale: number } {
  const ref = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      if (width > 0) setScale(width / THUMB_DESIGN_WIDTH);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return { ref, scale };
}

function ThumbCard({
  item,
  hero = false,
  onOpen,
}: {
  item: ArtifactItem;
  hero?: boolean;
  onOpen: (id: string) => void;
}) {
  const { html, failed, loading } = useArtifactHtml(item);
  const { ref, scale } = useFitScale();

  // Scripts can't run in the fully-sandboxed thumbnail anyway; stripping
  // them silences per-script console errors (see stripScripts).
  const thumbHtml = useMemo(() => (html === null ? null : stripScripts(html)), [html]);

  // Height of the un-scaled iframe: fill the thumbnail box's aspect ratio.
  const aspect = hero ? 16 / 9 : 16 / 10;
  const frameHeight = Math.round(THUMB_DESIGN_WIDTH / aspect);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(item.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(item.id);
        }
      }}
      aria-label={`Open ${item.title}`}
      className={[
        "group flex flex-col rounded-lg border border-current/10 overflow-hidden",
        "cursor-pointer transition-colors hover:border-midground/40",
        "focus-visible:outline-2 focus-visible:outline-midground",
      ].join(" ")}
    >
      {/* Thumbnail */}
      <div
        ref={ref}
        className="relative w-full overflow-hidden bg-white"
        style={{ aspectRatio: String(aspect) }}
      >
        {thumbHtml !== null && scale > 0 ? (
          <iframe
            srcDoc={thumbHtml}
            sandbox=""
            scrolling="no"
            tabIndex={-1}
            aria-hidden="true"
            title={`Preview of ${item.title}`}
            /* absolute: the 1280px layout box must not influence the grid
               track width — only the scaled visual matters. */
            className="absolute top-0 left-0 pointer-events-none select-none border-0"
            style={{
              width: THUMB_DESIGN_WIDTH,
              height: frameHeight,
              transform: `scale(${scale})`,
              transformOrigin: "top left",
            }}
          />
        ) : (
          <div
            className={[
              "absolute inset-0 flex items-center justify-center text-xs",
              failed ? "bg-destructive/10 text-destructive" : "bg-midground/10 text-text-secondary",
              loading && !failed ? "animate-pulse" : "",
            ].join(" ")}
          >
            {failed ? "Preview unavailable" : "Rendering…"}
          </div>
        )}
        {/* Hover affordance */}
        <div className="absolute inset-0 bg-midground/0 group-hover:bg-midground/5 transition-colors" />
      </div>

      {/* Caption row */}
      <div className="flex items-center gap-2 px-3 py-2">
        <div className="min-w-0 flex-1">
          <p className={["truncate font-medium leading-snug", hero ? "text-sm" : "text-xs"].join(" ")}>
            {item.title}
          </p>
          <p className="truncate text-[0.65rem] text-text-secondary mt-0.5">
            {item.group} · {relativeTime(item.mtime)} · {fmtSize(item.size)}
          </p>
        </div>
        <DownloadButton item={item} compact />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Full-size viewer pane (shared by Latest viewer and Archive)
// ---------------------------------------------------------------------------

function ViewerPane({ item }: { item: ArtifactItem }) {
  const { html, failed, loading } = useArtifactHtml(item);

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-text-secondary text-sm">
        Loading…
      </div>
    );
  }
  return (
    <iframe
      key={item.id}
      srcDoc={
        failed || html === null
          ? '<p style="font-family:sans-serif;padding:1rem">Failed to load this artifact.</p>'
          : html
      }
      sandbox="allow-scripts"
      className="w-full flex-1 min-h-0 rounded border border-current/10 bg-white"
      title={item.title}
    />
  );
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
  const [view, setView] = useState<ViewMode>("latest");

  // Latest view: id of the card opened full-size (null = grid showing).
  const [openLatestId, setOpenLatestId] = useState<string | null>(null);

  // Archive view: user-chosen id; null until user clicks (auto-select below).
  const [chosenId, setChosenId] = useState<string | null>(null);

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

  const items = useMemo<ArtifactItem[]>(() => data?.items ?? [], [data]);
  const latest = useMemo(() => pickLatest(items), [items]);

  // ----- Latest viewer selection -----
  const openLatestIdx = useMemo(
    () => latest.findIndex((i) => i.id === openLatestId),
    [latest, openLatestId],
  );
  const openLatest = openLatestIdx >= 0 ? latest[openLatestIdx] : null;

  const latestNext = useCallback(() => {
    if (latest.length === 0 || openLatestIdx < 0) return;
    setOpenLatestId(latest[(openLatestIdx + 1) % latest.length].id);
  }, [latest, openLatestIdx]);

  const latestPrev = useCallback(() => {
    if (latest.length === 0 || openLatestIdx < 0) return;
    setOpenLatestId(latest[(openLatestIdx - 1 + latest.length) % latest.length].id);
  }, [latest, openLatestIdx]);

  // ----- Archive selection (unchanged behaviour from the original page) -----
  const selectedId = useMemo<string | null>(() => {
    if (items.length === 0) return null;
    if (chosenId !== null && items.some((i) => i.id === chosenId)) return chosenId;
    return items[0].id;
  }, [items, chosenId]);

  const selectedIdx = useMemo(
    () => items.findIndex((i) => i.id === selectedId),
    [items, selectedId],
  );
  const selected = selectedIdx >= 0 ? items[selectedIdx] : null;

  const archiveNext = useCallback(() => {
    if (items.length === 0) return;
    setChosenId(items[(selectedIdx + 1) % items.length].id);
  }, [items, selectedIdx]);

  const archivePrev = useCallback(() => {
    if (items.length === 0) return;
    setChosenId(items[(selectedIdx - 1 + items.length) % items.length].id);
  }, [items, selectedIdx]);

  // ----- Keyboard navigation -----
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      if (view === "archive") {
        if (e.key === "ArrowRight") archiveNext();
        if (e.key === "ArrowLeft") archivePrev();
      } else if (openLatest) {
        if (e.key === "ArrowRight") latestNext();
        if (e.key === "ArrowLeft") latestPrev();
        if (e.key === "Escape") setOpenLatestId(null);
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [view, openLatest, archiveNext, archivePrev, latestNext, latestPrev]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  const total = data?.counts.total ?? items.length;

  return (
    <div className="flex min-h-0 flex-1 flex-col" style={{ height: "calc(100vh - 5rem)" }}>
      {/* View toggle */}
      <div className="flex items-center gap-2 pb-3 flex-wrap">
        {(
          [
            { mode: "latest", label: "Latest", icon: LayoutGrid, count: latest.length },
            { mode: "archive", label: "Archive", icon: List, count: total },
          ] as const
        ).map(({ mode, label, icon: Icon, count }) => {
          const active = view === mode;
          return (
            <button
              key={mode}
              onClick={() => setView(mode)}
              className={[
                "inline-flex items-center gap-1.5 px-3 py-1 rounded text-xs font-medium border transition-colors",
                active
                  ? "bg-midground/20 text-midground border-midground/40"
                  : "text-text-secondary border-current/20 hover:text-midground hover:border-midground/30",
              ].join(" ")}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
              {!loading && data ? <span className="opacity-60">({count})</span> : null}
            </button>
          );
        })}

        {/* Archive nav controls */}
        {view === "archive" && items.length > 0 && (
          <div className="ml-auto flex items-center gap-2 text-xs text-text-secondary">
            <button
              onClick={archivePrev}
              aria-label="Previous"
              className="p-1 hover:text-midground transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <span>
              {selectedIdx + 1} / {items.length}
            </span>
            <button
              onClick={archiveNext}
              aria-label="Next"
              className="p-1 hover:text-midground transition-colors"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>

      {/* Loading / error / empty states */}
      {loading && (
        <div className="flex-1 flex items-center justify-center text-text-secondary text-sm">
          Loading…
        </div>
      )}
      {!loading && fetchErr && <p className="text-xs text-destructive px-1">{fetchErr}</p>}
      {!loading && !fetchErr && items.length === 0 && (
        <div className="flex-1 flex items-center justify-center text-text-secondary text-sm">
          No HTML artifacts found.
        </div>
      )}

      {/* ----- LATEST: card grid or full-size viewer ----- */}
      {!loading && !fetchErr && items.length > 0 && view === "latest" && (
        openLatest ? (
          <div className="flex min-h-0 flex-1 flex-col">
            {/* Viewer toolbar */}
            <div className="flex items-center gap-2 pb-2">
              <button
                onClick={() => setOpenLatestId(null)}
                aria-label="Back to Latest grid"
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium border text-text-secondary border-current/20 hover:text-midground hover:border-midground/40 transition-colors"
              >
                <ArrowLeft className="h-3.5 w-3.5" />
                Back
              </button>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{openLatest.title}</p>
                <p className="truncate text-[0.65rem] text-text-secondary">
                  {openLatest.group} · {relativeTime(openLatest.mtime)} · {fmtSize(openLatest.size)}
                </p>
              </div>
              <div className="flex items-center gap-2 text-xs text-text-secondary">
                <button
                  onClick={latestPrev}
                  aria-label="Previous"
                  className="p-1 hover:text-midground transition-colors"
                >
                  <ChevronLeft className="h-4 w-4" />
                </button>
                <span>
                  {openLatestIdx + 1} / {latest.length}
                </span>
                <button
                  onClick={latestNext}
                  aria-label="Next"
                  className="p-1 hover:text-midground transition-colors"
                >
                  <ChevronRight className="h-4 w-4" />
                </button>
              </div>
              <DownloadButton item={openLatest} />
            </div>
            <ViewerPane item={openLatest} />
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto pr-1">
            <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-6">
              {latest.map((item, idx) => (
                <div
                  key={item.id}
                  className={idx === 0 ? "sm:col-span-2 xl:col-span-4" : "xl:col-span-2"}
                >
                  <ThumbCard item={item} hero={idx === 0} onOpen={setOpenLatestId} />
                </div>
              ))}
            </div>
            <p className="text-[0.65rem] text-text-secondary mt-3 px-1">
              Five most recent reports, one per audit group — the full history ({total}) lives
              in Archive.
            </p>
          </div>
        )
      )}

      {/* ----- ARCHIVE: sidebar + preview ----- */}
      {!loading && !fetchErr && items.length > 0 && view === "archive" && (
        <div className="flex min-h-0 flex-1 gap-3">
          <aside className="w-64 shrink-0 flex flex-col min-h-0 border-r border-current/10 pr-2">
            <ul className="flex-1 overflow-y-auto space-y-0.5">
              {items.map((item) => {
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

          <div className="flex-1 min-h-0 min-w-0 flex flex-col">
            {selected ? (
              <>
                <div className="flex items-center gap-2 pb-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{selected.title}</p>
                    <p className="truncate text-[0.65rem] text-text-secondary">
                      {selected.group} · {relativeTime(selected.mtime)} · {fmtSize(selected.size)}
                    </p>
                  </div>
                  <DownloadButton item={selected} />
                </div>
                <ViewerPane item={selected} />
              </>
            ) : (
              <div className="flex-1 flex items-center justify-center text-text-secondary text-sm">
                Select an item to preview
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
