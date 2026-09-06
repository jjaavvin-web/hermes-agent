/**
 * HTML Gallery — curated Hermes-generated HTML reports in three tabs.
 *
 *  "Favorites" — MANUAL: reports starred to keep around (the default tab).
 *  "Latest"    — AUTO: the five newest unassigned reports, one per audit
 *                group.  Moving an item out lets the next-newest take its
 *                place, so this doubles as the triage stream for daily
 *                cron-generated reports.  The list is re-sorted by mtime
 *                CLIENT-SIDE: the backend floats legacy "featured"
 *                path-fragments above genuinely newer files.
 *  "Old"       — MANUAL: reports dismissed out of the way.
 *
 *  Assignments persist in localStorage keyed by artifact id (stable across
 *  in-place overwrites).  Every card and viewer has move controls plus a
 *  Download action: authed fetch → Blob → <a download> (token never in a
 *  URL); filenames derive from decoding the opaque id — display only.
 *
 *  Rendering: <iframe srcDoc=...> bypasses the global X-Frame-Options: deny
 *  header.  Thumbnails run with sandbox="" and scripts stripped (inert +
 *  quiet); the full-size viewer keeps sandbox="allow-scripts" in an opaque
 *  origin and AUTO-FITS: content wider than the pane renders at its design
 *  width and is transform-scaled down so nothing scrolls horizontally.
 *
 * Data:  GET /api/dashboard/artifacts
 * Serve: GET /api/dashboard/artifacts/raw?id=<opaque>  (header-authed)
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  LayoutGrid,
  Star,
  Undo2,
} from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { fetchJSON } from "@/lib/api";
import {
  bucketize,
  cacheKeyFor,
  fetchArtifactHtml,
  filenameFor,
  loadAssignments,
  saveAssignments,
  stripScripts,
  type ArtifactItem,
  type Assignments,
  type Bucket,
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

type ViewMode = "latest" | "favorites" | "old";

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

const ICON_BTN =
  "inline-flex items-center gap-1 rounded border transition-colors shrink-0 " +
  "text-text-secondary border-current/20 hover:text-midground hover:border-midground/40";

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
        compact ? "px-1.5 py-1" : "px-2 py-1 text-xs font-medium",
        state === "error"
          ? "inline-flex items-center gap-1 rounded border transition-colors shrink-0 text-destructive border-destructive/40"
          : ICON_BTN,
      ].join(" ")}
    >
      <Download className="h-3.5 w-3.5" />
      {!compact && (state === "busy" ? "Saving…" : state === "error" ? "Failed" : "Download")}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Move controls
// ---------------------------------------------------------------------------

/** The two move targets shown for an item, given the tab it's sitting in.
 *  "latest" clears the assignment (returns the item to the auto flow). */
const MOVE_TARGETS: Record<ViewMode, Array<Bucket | "latest">> = {
  latest: ["favorites", "old"],
  favorites: ["latest", "old"],
  old: ["latest", "favorites"],
};

const TARGET_META: Record<Bucket | "latest", { label: string; Icon: typeof Star }> = {
  latest: { label: "Return to Latest", Icon: Undo2 },
  favorites: { label: "Move to Favorites", Icon: Star },
  old: { label: "Move to Old", Icon: Archive },
};

function MoveButtons({
  item,
  view,
  onMove,
  compact = false,
}: {
  item: ArtifactItem;
  view: ViewMode;
  onMove: (id: string, target: Bucket | "latest") => void;
  compact?: boolean;
}) {
  return (
    <>
      {MOVE_TARGETS[view].map((target) => {
        const { label, Icon } = TARGET_META[target];
        return (
          <button
            key={target}
            onClick={(e) => {
              e.stopPropagation();
              onMove(item.id, target);
            }}
            aria-label={`${label}: ${item.title}`}
            title={label}
            className={[ICON_BTN, compact ? "px-1.5 py-1" : "px-2 py-1 text-xs font-medium"].join(
              " ",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {!compact && label.replace("Move to ", "").replace("Return to ", "")}
          </button>
        );
      })}
    </>
  );
}

// ---------------------------------------------------------------------------
// Fit-to-container measurement
// ---------------------------------------------------------------------------

/** Design width the fixed-width iframes render at before being scaled. */
const DESIGN_WIDTH = 1280;

/** Track a container's rendered size so fixed-width iframes can be
 *  transform-scaled to fit it exactly. */
function useFitBox(): {
  ref: React.RefObject<HTMLDivElement | null>;
  width: number;
  height: number;
} {
  const ref = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect && rect.width > 0) setBox({ width: rect.width, height: rect.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return { ref, width: box.width, height: box.height };
}

// ---------------------------------------------------------------------------
// Thumbnail card
// ---------------------------------------------------------------------------

const THUMB_ASPECT = 16 / 10;

function ThumbCard({
  item,
  view,
  onOpen,
  onMove,
}: {
  item: ArtifactItem;
  view: ViewMode;
  onOpen: (id: string) => void;
  onMove: (id: string, target: Bucket | "latest") => void;
}) {
  const { html, failed, loading } = useArtifactHtml(item);
  const { ref, width } = useFitBox();
  const scale = width > 0 ? width / DESIGN_WIDTH : 0;

  // Scripts can't run in the fully-sandboxed thumbnail anyway; stripping
  // them silences per-script console errors (see stripScripts).
  const thumbHtml = useMemo(() => (html === null ? null : stripScripts(html)), [html]);

  // Height of the un-scaled iframe: fill the thumbnail box's aspect ratio.
  const frameHeight = Math.round(DESIGN_WIDTH / THUMB_ASPECT);

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
        style={{ aspectRatio: String(THUMB_ASPECT) }}
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
              width: DESIGN_WIDTH,
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
      <div className="flex items-center gap-1.5 px-3 py-2">
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium leading-snug text-xs">{item.title}</p>
          <p className="truncate text-[0.65rem] text-text-secondary mt-0.5">
            {item.group} · {relativeTime(item.mtime)} · {fmtSize(item.size)}
          </p>
        </div>
        <MoveButtons item={item} view={view} onMove={onMove} compact />
        <DownloadButton item={item} compact />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Full-size viewer pane — auto-fits wide content to the pane
// ---------------------------------------------------------------------------

function ViewerPane({ item }: { item: ArtifactItem }) {
  const { html, failed, loading } = useArtifactHtml(item);
  const { ref, width, height } = useFitBox();

  const doc =
    failed || html === null
      ? '<p style="font-family:sans-serif;padding:1rem">Failed to load this artifact.</p>'
      : html;

  // Auto-fit: pane narrower than the design width → render at DESIGN_WIDTH
  // and scale down so nothing scrolls horizontally.  Wide-enough panes (and
  // the pre-measure first frame, width 0) render natively.
  const fitScale = width > 0 && width < DESIGN_WIDTH ? width / DESIGN_WIDTH : 1;

  // The measured container must ALWAYS render — an early return here would
  // mount useFitBox's effect against a null ref and the observer would never
  // attach (the fit branch would silently stay disabled).
  return (
    <div
      ref={ref}
      className="relative w-full flex-1 min-h-0 overflow-hidden rounded border border-current/10 bg-white"
    >
      {loading ? (
        <div className="absolute inset-0 flex items-center justify-center text-text-secondary text-sm bg-midground/5">
          Loading…
        </div>
      ) : fitScale === 1 ? (
        <iframe
          key={item.id}
          srcDoc={doc}
          sandbox="allow-scripts"
          className="absolute inset-0 w-full h-full border-0"
          title={item.title}
        />
      ) : (
        <iframe
          key={item.id}
          srcDoc={doc}
          sandbox="allow-scripts"
          className="absolute top-0 left-0 border-0"
          style={{
            width: DESIGN_WIDTH,
            height: Math.max(1, Math.round(height / fitScale)),
            transform: `scale(${fitScale})`,
            transformOrigin: "top left",
          }}
          title={item.title}
        />
      )}
    </div>
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
  const [view, setView] = useState<ViewMode>("favorites");
  const [assignments, setAssignments] = useState<Assignments>(() => loadAssignments());

  // Id of the card opened full-size in the current tab (null = grid).
  const [openId, setOpenId] = useState<string | null>(null);

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

  // Persist assignments whenever they change.
  useEffect(() => {
    saveAssignments(assignments);
  }, [assignments]);

  const items = useMemo<ArtifactItem[]>(() => data?.items ?? [], [data]);
  const buckets = useMemo(() => bucketize(items, assignments), [items, assignments]);
  const visible = buckets[view];

  const move = useCallback((id: string, target: Bucket | "latest") => {
    setAssignments((prev) => {
      const next = { ...prev };
      if (target === "latest") delete next[id];
      else next[id] = target;
      return next;
    });
  }, []);

  // ----- Viewer selection within the active tab -----
  const openIdx = useMemo(() => visible.findIndex((i) => i.id === openId), [visible, openId]);
  const openItem = openIdx >= 0 ? visible[openIdx] : null;

  const goNext = useCallback(() => {
    if (visible.length === 0 || openIdx < 0) return;
    setOpenId(visible[(openIdx + 1) % visible.length].id);
  }, [visible, openIdx]);

  const goPrev = useCallback(() => {
    if (visible.length === 0 || openIdx < 0) return;
    setOpenId(visible[(openIdx - 1 + visible.length) % visible.length].id);
  }, [visible, openIdx]);

  // ----- Keyboard navigation (viewer only) -----
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      if (!openItem) return;
      if (e.key === "ArrowRight") goNext();
      if (e.key === "ArrowLeft") goPrev();
      if (e.key === "Escape") setOpenId(null);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [openItem, goNext, goPrev]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  const TAB_META: Array<{ mode: ViewMode; label: string; Icon: typeof Star }> = [
    { mode: "favorites", label: "Favorites", Icon: Star },
    { mode: "latest", label: "Latest", Icon: LayoutGrid },
    { mode: "old", label: "Old", Icon: Archive },
  ];

  const EMPTY_COPY: Record<ViewMode, string> = {
    latest: "No HTML reports found.",
    favorites: "Nothing here yet — hit the ★ on any report to keep it handy.",
    old: "Nothing here yet — move stale reports here to clear them out of Latest.",
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col" style={{ height: "calc(100vh - 5rem)" }}>
      {/* Tabs */}
      <div className="flex items-center gap-2 pb-3 flex-wrap">
        {TAB_META.map(({ mode, label, Icon }) => {
          const active = view === mode;
          return (
            <button
              key={mode}
              onClick={() => {
                setView(mode);
                setOpenId(null);
              }}
              className={[
                "inline-flex items-center gap-1.5 px-3 py-1 rounded text-xs font-medium border transition-colors",
                active
                  ? "bg-midground/20 text-midground border-midground/40"
                  : "text-text-secondary border-current/20 hover:text-midground hover:border-midground/30",
              ].join(" ")}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
              {!loading && data ? <span className="opacity-60">({buckets[mode].length})</span> : null}
            </button>
          );
        })}
      </div>

      {/* Loading / error states */}
      {loading && (
        <div className="flex-1 flex items-center justify-center text-text-secondary text-sm">
          Loading…
        </div>
      )}
      {!loading && fetchErr && <p className="text-xs text-destructive px-1">{fetchErr}</p>}

      {/* Viewer or grid for the active tab */}
      {!loading && !fetchErr && (
        openItem ? (
          <div className="flex min-h-0 flex-1 flex-col">
            {/* Viewer toolbar */}
            <div className="flex items-center gap-2 pb-2 flex-wrap">
              <button
                onClick={() => setOpenId(null)}
                aria-label="Back to grid"
                className={`${ICON_BTN} px-2 py-1 text-xs font-medium`}
              >
                <ArrowLeft className="h-3.5 w-3.5" />
                Back
              </button>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{openItem.title}</p>
                <p className="truncate text-[0.65rem] text-text-secondary">
                  {openItem.group} · {relativeTime(openItem.mtime)} · {fmtSize(openItem.size)}
                </p>
              </div>
              <div className="flex items-center gap-2 text-xs text-text-secondary">
                <button
                  onClick={goPrev}
                  aria-label="Previous"
                  className="p-1 hover:text-midground transition-colors"
                >
                  <ChevronLeft className="h-4 w-4" />
                </button>
                <span>
                  {openIdx + 1} / {visible.length}
                </span>
                <button
                  onClick={goNext}
                  aria-label="Next"
                  className="p-1 hover:text-midground transition-colors"
                >
                  <ChevronRight className="h-4 w-4" />
                </button>
              </div>
              <MoveButtons item={openItem} view={view} onMove={move} />
              <DownloadButton item={openItem} />
            </div>
            <ViewerPane item={openItem} />
          </div>
        ) : visible.length === 0 ? (
          <div className="flex-1 flex items-center justify-center text-text-secondary text-sm px-6 text-center">
            {EMPTY_COPY[view]}
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto pr-1">
            <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-3">
              {visible.map((item) => (
                <ThumbCard key={item.id} item={item} view={view} onOpen={setOpenId} onMove={move} />
              ))}
            </div>
            {view === "latest" && (
              <p className="text-[0.65rem] text-text-secondary mt-3 px-1">
                Five most recent reports, one per audit group — ★ keeps one in Favorites,
                the box moves it to Old, and the next-newest takes its slot.
              </p>
            )}
          </div>
        )
      )}
    </div>
  );
}
