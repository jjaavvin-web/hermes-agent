/**
 * Pure helpers for the HTML Gallery page (see pages/HtmlGalleryPage.tsx).
 * Split out so the page file only exports components (react-refresh rule)
 * and the logic stays unit-testable.
 */

export interface ArtifactItem {
  id: string;
  title: string;
  kind: "report" | "replay";
  group: string;
  mtime: number;
  size: number;
}

export const LATEST_COUNT = 5;

/** Decode the opaque artifact id (url-safe base64, no padding) back to the
 *  ~/.hermes-relative path.  Returns null on any malformed input. */
export function decodeIdToRel(id: string): string | null {
  try {
    const b64 =
      id.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (id.length % 4)) % 4);
    const bin = atob(b64);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    const rel = new TextDecoder().decode(bytes);
    return rel.length > 0 ? rel : null;
  } catch {
    return null;
  }
}

/** Basenames too generic to be useful as a downloaded filename. */
const GENERIC_BASENAMES = new Set(["index", "report", "page", "output", "artifact"]);

function slugify(text: string): string {
  const slug = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "artifact";
}

/** Pick the filename a download should save as.  Prefers the real basename
 *  from the decoded relative path; generic names (index.html …) fall back to
 *  the audit-group name so files stay identifiable in a Downloads folder. */
export function filenameFor(item: ArtifactItem): string {
  const rel = decodeIdToRel(item.id);
  const basename = rel?.split("/").pop();
  if (basename && basename.toLowerCase().endsWith(".html")) {
    const stem = basename.slice(0, -".html".length).toLowerCase();
    if (!GENERIC_BASENAMES.has(stem)) return basename;
    return `${slugify(item.group)}.html`;
  }
  return `${slugify(item.title)}.html`;
}

// ---------------------------------------------------------------------------
// Artifact HTML fetch + cache
// ---------------------------------------------------------------------------

// Cap bounds entry COUNT, not bytes — fine for typical KB-scale reports; if
// multi-MB artifacts become common this needs a byte budget instead.
const HTML_CACHE_CAP = 30;
const htmlCache = new Map<string, string>();

/** Cache key folds in mtime+size: daily reports are OVERWRITTEN in place at
 *  stable paths (stable id), so an id-only key would silently serve the
 *  previous day's content for the rest of the session. */
export function cacheKeyFor(item: ArtifactItem): string {
  return `${item.id}:${item.mtime}:${item.size}`;
}

/** Fetch an artifact's HTML with header auth (token never in a URL). */
export async function fetchArtifactHtml(item: ArtifactItem): Promise<string> {
  const key = cacheKeyFor(item);
  const hit = htmlCache.get(key);
  if (hit !== undefined) return hit;
  const token = window.__HERMES_SESSION_TOKEN__ ?? "";
  const r = await fetch(`/api/dashboard/artifacts/raw?id=${encodeURIComponent(item.id)}`, {
    headers: { "X-Hermes-Session-Token": token },
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const html = await r.text();
  htmlCache.set(key, html);
  // Evict oldest insertions past the cap (Map preserves insertion order).
  while (htmlCache.size > HTML_CACHE_CAP) {
    const oldest = htmlCache.keys().next().value;
    if (oldest === undefined) break;
    htmlCache.delete(oldest);
  }
  return html;
}

/** Reset the module cache — needed by unit tests to isolate fetch behavior. */
export function clearArtifactHtmlCache(): void {
  htmlCache.clear();
}

/** Remove <script> blocks for thumbnail rendering.  Thumbnails run in a
 *  fully sandboxed iframe where scripts are blocked anyway — stripping them
 *  silences the per-script console errors and skips pointless fetches.  The
 *  full-size viewer renders the ORIGINAL html with allow-scripts. */
export function stripScripts(html: string): string {
  return html.replace(/<script\b[\s\S]*?<\/script\s*>/gi, "");
}

/** The N newest reports, at most one per group — so one audit directory can
 *  never occupy every Latest slot.  Ignores the server's featured-first
 *  ordering (the backend floats legacy path-fragments above newer files).
 *  Replays are excluded even if the backend re-enables them: their shared
 *  "Session replays" group would otherwise claim a curated slot.  The id
 *  tiebreaker keeps equal-mtime ordering independent of arrival order —
 *  stable sort would otherwise leak the server's featured bias back in. */
export function pickLatest(items: ArtifactItem[], n: number = LATEST_COUNT): ArtifactItem[] {
  const byNewest = items
    .filter((i) => i.kind === "report")
    .sort((a, b) => (a.mtime !== b.mtime ? b.mtime - a.mtime : a.id.localeCompare(b.id)));
  const seen = new Set<string>();
  const picked: ArtifactItem[] = [];
  for (const item of byNewest) {
    if (seen.has(item.group)) continue;
    seen.add(item.group);
    picked.push(item);
    if (picked.length >= n) break;
  }
  return picked;
}
