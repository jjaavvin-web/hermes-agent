// @vitest-environment jsdom
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import GetSomePage from "../GetSomePage";

vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setTitle: vi.fn() }),
}));

vi.mock("@/components/WorkNexus", () => ({
  default: () => <div data-testid="mock-work-nexus">Work Nexus</div>,
}));

const PROJECTS_RESPONSE = {
  scanned_at: "2026-06-01T00:00:00+00:00",
  projects: [
    {
      slug: "alpha",
      name: "Alpha Ship",
      icon: "🚀",
      color: "#76e4f7",
      archived: false,
      total: 4,
      completion_pct: 42,
      by_status: { ready: 2, blocked: 1, done: 1 },
      active: 2,
      blocked: 1,
      last_activity: 1_700_000_000,
      remaining_count: 3,
      remaining_by_status: { ready: 2, blocked: 1 },
      remaining_more: 1,
      remaining: [
        { status: "blocked", title: "Fix blocker" },
        { status: "ready", title: "Wire dashboard" },
      ],
    },
  ],
};

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

function mockFetch() {
  return vi.fn(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      text: () => Promise.resolve(JSON.stringify(PROJECTS_RESPONSE)),
      json: () => Promise.resolve(PROJECTS_RESPONSE),
    }),
  );
}

afterEach(() => {
  while (cleanupFns.length > 0) cleanupFns.pop()?.();
  vi.restoreAllMocks();
});

describe("GetSomePage", () => {
  it("opens a closeable what's-left panel from a project card", async () => {
    globalThis.fetch = mockFetch() as unknown as typeof fetch;
    const { container } = render(<GetSomePage />);

    let card: HTMLButtonElement | null = null;
    await waitFor(() => {
      card = container.querySelector<HTMLButtonElement>("[data-testid='get-some-project-card-alpha']");
      expect(card).not.toBeNull();
    });

    act(() => {
      card?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await waitFor(() => {
      const panel = container.querySelector("[data-testid='get-some-remaining-panel']");
      expect(panel).not.toBeNull();
      expect(panel?.textContent).toContain("What's left in Alpha Ship");
      expect(panel?.textContent).toContain("blocked");
      expect(panel?.textContent).toContain("1");
      expect(panel?.textContent).toContain("Fix blocker");
      expect(panel?.textContent).toContain("ready");
      expect(panel?.textContent).toContain("Wire dashboard");
      expect(panel?.textContent).toContain("+1 more");
    });

    const close = container.querySelector<HTMLButtonElement>("[aria-label='Close remaining work panel']");
    act(() => {
      close?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await waitFor(() => {
      expect(container.querySelector("[data-testid='get-some-remaining-panel']")).toBeNull();
    });
  });
});
