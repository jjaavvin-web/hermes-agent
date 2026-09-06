// @vitest-environment jsdom
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import HtmlGalleryPage from "../HtmlGalleryPage";
import {
  bucketize,
  clearArtifactHtmlCache,
  decodeIdToRel,
  filenameFor,
  pickLatest,
  stripScripts,
  type ArtifactItem,
  type Assignments,
} from "@/lib/htmlArtifacts";

vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setTitle: vi.fn() }),
}));

// jsdom has no ResizeObserver; thumbnails fall back to their placeholder,
// which is all the render assertions need.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** Encode a relative path the way the backend does: url-safe b64, no padding. */
function encodeId(rel: string): string {
  return btoa(rel).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function item(
  rel: string,
  group: string,
  mtime: number,
  title = rel.split("/").pop() ?? rel,
): ArtifactItem {
  return {
    id: encodeId(rel),
    title,
    kind: "report",
    group,
    mtime,
    size: 1234,
  };
}

// Seven reports across six groups; "alpha" has two files so group-dedup is
// observable: only its newest may appear in Latest.
const T0 = 1_790_000_000;
const ITEMS: ArtifactItem[] = [
  item("audits/alpha/day-report.html", "alpha", T0 - 10, "Alpha Day Report"),
  item("audits/alpha/older-draft.html", "alpha", T0 - 500, "Alpha Older Draft"),
  item("audits/bravo/index.html", "bravo", T0 - 20, "Bravo Index"),
  item("audits/charlie/health.html", "charlie", T0 - 30, "Charlie Health"),
  item("audits/delta/map.html", "delta", T0 - 40, "Delta Map"),
  item("audits/echo/summary.html", "echo", T0 - 50, "Echo Summary"),
  item("audits/foxtrot/extra.html", "foxtrot", T0 - 60, "Foxtrot Extra"),
];

const LIST_RESPONSE = {
  items: ITEMS,
  counts: { reports: ITEMS.length, replays: 0, total: ITEMS.length },
  replays_truncated: 0,
  replay_total: 0,
};

function mockFetch() {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/dashboard/artifacts/raw")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        statusText: "OK",
        text: () => Promise.resolve("<html><body>artifact body</body></html>"),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      text: () => Promise.resolve(JSON.stringify(LIST_RESPONSE)),
      json: () => Promise.resolve(LIST_RESPONSE),
    });
  });
}

const cleanupFns: Array<() => void> = [];

function render(ui: ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root | null = createRoot(container);
  act(() => {
    root?.render(ui);
  });
  const cleanup = () => {
    if (!root) return;
    act(() => {
      root?.unmount();
    });
    root = null;
    container.remove();
  };
  cleanupFns.push(cleanup);
  return { container };
}

async function flushReact() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function waitFor(assertion: () => void) {
  let lastError: unknown;
  for (let i = 0; i < 30; i += 1) {
    try {
      assertion();
      return;
    } catch (err) {
      lastError = err;
      await flushReact();
    }
  }
  throw lastError;
}

beforeEach(() => {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = ResizeObserverStub;
  clearArtifactHtmlCache();
  localStorage.clear();
});

afterEach(() => {
  while (cleanupFns.length > 0) cleanupFns.pop()?.();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("decodeIdToRel", () => {
  it("round-trips a url-safe base64 id back to the relative path", () => {
    expect(decodeIdToRel(encodeId("audits/foo/bar.html"))).toBe("audits/foo/bar.html");
  });

  it("returns null on malformed input", () => {
    expect(decodeIdToRel("!!!not-base64!!!")).toBeNull();
    expect(decodeIdToRel("")).toBeNull();
  });
});

describe("filenameFor", () => {
  it("uses the real basename when it is specific", () => {
    expect(filenameFor(item("audits/alpha/day-report.html", "alpha", 1))).toBe(
      "day-report.html",
    );
  });

  it("falls back to the group name for generic basenames like index.html", () => {
    expect(filenameFor(item("audits/some-audit-dir/index.html", "some-audit-dir", 1))).toBe(
      "some-audit-dir.html",
    );
  });

  it("falls back to a slug of the title when the id does not decode", () => {
    const broken: ArtifactItem = {
      id: "!!!",
      title: "My Report: Final (v2)",
      kind: "report",
      group: "g",
      mtime: 1,
      size: 1,
    };
    expect(filenameFor(broken)).toBe("my-report-final-v2.html");
  });
});

describe("stripScripts", () => {
  it("removes script blocks but keeps markup and styles", () => {
    const html =
      '<html><head><style>.a{}</style><script src="x.js"></script></head>' +
      "<body><h1>Hi</h1><script>alert(1)</script></body></html>";
    const out = stripScripts(html);
    expect(out).not.toContain("<script");
    expect(out).toContain("<style>.a{}</style>");
    expect(out).toContain("<h1>Hi</h1>");
  });
});

describe("pickLatest", () => {
  it("returns the newest five, at most one per group, newest first", () => {
    const picked = pickLatest(ITEMS);
    expect(picked.map((i) => i.title)).toEqual([
      "Alpha Day Report",
      "Bravo Index",
      "Charlie Health",
      "Delta Map",
      "Echo Summary",
    ]);
  });

  it("never includes an older file from an already-represented group", () => {
    const picked = pickLatest(ITEMS);
    expect(picked.some((i) => i.title === "Alpha Older Draft")).toBe(false);
  });

  it("ignores the incoming order (server featured-first) and sorts by mtime", () => {
    const shuffled = [...ITEMS].reverse();
    expect(pickLatest(shuffled).map((i) => i.title)).toEqual(
      pickLatest(ITEMS).map((i) => i.title),
    );
  });

  it("excludes replays even when the backend includes them", () => {
    const replay: ArtifactItem = {
      ...item("sessions/artifacts/x/replay.html", "Session replays", T0, "Replay newest"),
      kind: "replay",
    };
    const picked = pickLatest([replay, ...ITEMS]);
    expect(picked.some((i) => i.kind === "replay")).toBe(false);
    expect(picked.length).toBe(5);
  });

  it("breaks exact mtime ties deterministically, independent of input order", () => {
    const a = item("audits/g1/a.html", "g1", T0, "Tie A");
    const b = item("audits/g2/b.html", "g2", T0, "Tie B");
    expect(pickLatest([a, b]).map((i) => i.title)).toEqual(
      pickLatest([b, a]).map((i) => i.title),
    );
  });
});

describe("bucketize", () => {
  it("routes assigned items to their buckets and backfills Latest", () => {
    const assignments: Assignments = {
      [ITEMS[0].id]: "favorites", // Alpha Day Report
      [ITEMS[2].id]: "old", // Bravo Index
    };
    const b = bucketize(ITEMS, assignments);
    expect(b.favorites.map((i) => i.title)).toEqual(["Alpha Day Report"]);
    expect(b.old.map((i) => i.title)).toEqual(["Bravo Index"]);
    // With alpha's newest assigned away, its group frees up and the older
    // alpha draft becomes eligible again; foxtrot backfills too.
    expect(b.latest.map((i) => i.title)).toEqual([
      "Charlie Health",
      "Delta Map",
      "Echo Summary",
      "Foxtrot Extra",
      "Alpha Older Draft",
    ]);
  });

  it("returns everything unassigned to the auto flow", () => {
    const b = bucketize(ITEMS, {});
    expect(b.favorites).toEqual([]);
    expect(b.old).toEqual([]);
    expect(b.latest.length).toBe(5);
  });
});

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

/** Click a tab chip by its label. */
function clickTab(container: HTMLElement, label: string) {
  const chip = Array.from(container.querySelectorAll("button")).find((b) =>
    b.textContent?.includes(label),
  );
  expect(chip).toBeTruthy();
  act(() => {
    chip?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

describe("HtmlGalleryPage", () => {
  it("defaults to the Favorites tab with its empty state", async () => {
    globalThis.fetch = mockFetch() as unknown as typeof fetch;
    const { container } = render(<HtmlGalleryPage />);

    await waitFor(() => {
      expect(container.textContent).toContain("Nothing here yet — hit the ★");
      // No cards on the empty default tab.
      expect(container.querySelectorAll("[role='button'][aria-label^='Open ']").length).toBe(0);
    });
  });

  it("shows five deduped cards with download buttons on the Latest tab", async () => {
    globalThis.fetch = mockFetch() as unknown as typeof fetch;
    const { container } = render(<HtmlGalleryPage />);

    await waitFor(() => {
      expect(container.textContent).toContain("Favorites");
    });
    clickTab(container, "Latest");

    await waitFor(() => {
      expect(container.textContent).toContain("Alpha Day Report");
      expect(container.textContent).toContain("Echo Summary");
    });

    // Five cards, group-deduped: foxtrot (6th group) and the older alpha
    // draft are excluded from Latest.
    const cards = container.querySelectorAll("[role='button'][aria-label^='Open ']");
    expect(cards.length).toBe(5);
    expect(container.textContent).not.toContain("Alpha Older Draft");
    expect(container.textContent).not.toContain("Foxtrot Extra");

    // Every card carries a download action with a real filename.
    const downloads = container.querySelectorAll("button[aria-label^='Download ']");
    expect(downloads.length).toBe(5);
    expect(
      container.querySelector("button[aria-label='Download day-report.html']"),
    ).not.toBeNull();
    // bravo's index.html falls back to its group name.
    expect(container.querySelector("button[aria-label='Download bravo.html']")).not.toBeNull();
  });

  it("moves a card to Favorites, backfills Latest, and persists the assignment", async () => {
    globalThis.fetch = mockFetch() as unknown as typeof fetch;
    const { container } = render(<HtmlGalleryPage />);

    await waitFor(() => {
      expect(container.textContent).toContain("Favorites");
    });
    clickTab(container, "Latest");

    let moveBtn: Element | null = null;
    await waitFor(() => {
      moveBtn = container.querySelector(
        "button[aria-label='Move to Favorites: Alpha Day Report']",
      );
      expect(moveBtn).not.toBeNull();
    });

    act(() => {
      moveBtn?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await waitFor(() => {
      // The moved card left Latest and the next-newest group backfilled.
      expect(container.textContent).not.toContain("Alpha Day Report");
      expect(container.textContent).toContain("Foxtrot Extra");
    });

    // Assignment persisted for future sessions.
    const stored = JSON.parse(
      localStorage.getItem("hermes-html-gallery-assignments-v1") ?? "{}",
    ) as Record<string, string>;
    expect(Object.values(stored)).toEqual(["favorites"]);

    // The Favorites tab shows the moved card, with a return-to-Latest control.
    const favChip = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Favorites"),
    );
    act(() => {
      favChip?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await waitFor(() => {
      expect(container.textContent).toContain("Alpha Day Report");
      expect(
        container.querySelector("button[aria-label='Return to Latest: Alpha Day Report']"),
      ).not.toBeNull();
    });
  });

  it("shows the Old tab empty state until something is moved there", async () => {
    globalThis.fetch = mockFetch() as unknown as typeof fetch;
    const { container } = render(<HtmlGalleryPage />);

    await waitFor(() => {
      expect(container.textContent).toContain("Favorites");
    });
    clickTab(container, "Old");
    await waitFor(() => {
      expect(container.textContent).toContain("Nothing here yet — move stale reports");
    });
  });

  it("downloads an artifact as a blob with the derived filename", async () => {
    const fetchMock = mockFetch();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    const createObjectURL = vi.fn(() => "blob:mock-url");
    const revokeObjectURL = vi.fn();
    (URL as unknown as { createObjectURL: unknown }).createObjectURL = createObjectURL;
    (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = revokeObjectURL;
    // jsdom can't navigate; the anchor click just needs to be observed.
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    const { container } = render(<HtmlGalleryPage />);

    await waitFor(() => {
      expect(container.textContent).toContain("Favorites");
    });
    clickTab(container, "Latest");

    let btn: Element | null = null;
    await waitFor(() => {
      btn = container.querySelector("button[aria-label='Download day-report.html']");
      expect(btn).not.toBeNull();
    });

    act(() => {
      btn?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await waitFor(() => {
      // The raw endpoint was fetched with the opaque id, and a blob URL was
      // minted for the anchor click.
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/dashboard/artifacts/raw?id="),
        ),
      ).toBe(true);
      expect(createObjectURL).toHaveBeenCalledTimes(1);
      expect(anchorClick).toHaveBeenCalled();
    });
  });

  it("shows the failure placeholder when an artifact fetch fails", async () => {
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/dashboard/artifacts/raw")) {
        return Promise.resolve({
          ok: false,
          status: 500,
          statusText: "ERR",
          text: () => Promise.resolve("boom"),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        statusText: "OK",
        text: () => Promise.resolve(JSON.stringify(LIST_RESPONSE)),
        json: () => Promise.resolve(LIST_RESPONSE),
      });
    }) as unknown as typeof fetch;
    const { container } = render(<HtmlGalleryPage />);

    await waitFor(() => {
      expect(container.textContent).toContain("Favorites");
    });
    clickTab(container, "Latest");

    await waitFor(() => {
      expect(container.textContent).toContain("Preview unavailable");
    });
  });

  it("opens the full-size viewer when a Latest card is clicked", async () => {
    globalThis.fetch = mockFetch() as unknown as typeof fetch;
    const { container } = render(<HtmlGalleryPage />);

    await waitFor(() => {
      expect(container.textContent).toContain("Favorites");
    });
    clickTab(container, "Latest");

    let card: Element | null = null;
    await waitFor(() => {
      card = container.querySelector("[role='button'][aria-label='Open Alpha Day Report']");
      expect(card).not.toBeNull();
    });

    act(() => {
      card?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await waitFor(() => {
      // Viewer toolbar appears with Back + position within the latest five.
      expect(container.querySelector("button[aria-label='Back to grid']")).not.toBeNull();
      expect(container.textContent).toContain("1 / 5");
      const iframe = container.querySelector("iframe[sandbox='allow-scripts']");
      expect(iframe).not.toBeNull();
    });
  });
});
