// @vitest-environment jsdom
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { act, forwardRef, useImperativeHandle, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as forceGraphModule from "react-force-graph-2d";
import WorkNexus from "../WorkNexus";

vi.mock("react-force-graph-2d", () => {
  type Node = { id: string; label?: string };
  type Link = { id?: string; kind?: string; source: string; target: string };
  const refControls = {
    pauseAnimation: vi.fn(() => refControls),
    resumeAnimation: vi.fn(() => refControls),
    d3ReheatSimulation: vi.fn(() => refControls),
    zoomToFit: vi.fn(() => refControls),
    d3Force: vi.fn((forceName: string) => {
      if (forceName === "charge") return { strength: vi.fn() };
      if (forceName === "link") return { distance: vi.fn() };
      return undefined;
    }),
  };
  const MockForceGraph2D = forwardRef(function MockForceGraph2D(props: {
    graphData: { nodes: Node[]; links: Link[] };
    onNodeClick?: (n: Node) => void;
    onBackgroundClick?: () => void;
    onEngineStop?: () => void;
  }, ref) {
    useImperativeHandle(ref, () => refControls);
    return (
      <div data-testid="mock-work-nexus-graph" data-links={props.graphData.links.length}>
        <button
          type="button"
          data-testid="mock-engine-stop"
          onClick={() => props.onEngineStop?.()}
        >
          stop
        </button>
        <button
          type="button"
          data-testid="mock-bg"
          onClick={() => props.onBackgroundClick?.()}
        >
          bg
        </button>
        {props.graphData.nodes.map((n) => (
          <button
            key={n.id}
            type="button"
            data-testid={`mock-node-${n.id}`}
            onClick={() => props.onNodeClick?.(n)}
          >
            {n.label ?? n.id}
          </button>
        ))}
      </div>
    );
  });
  return {
    __refControls: refControls,
    default: MockForceGraph2D,
  };
});

interface GraphResponse {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
  degraded_mode?: string[];
}

interface MockForceGraphControls {
  pauseAnimation: ReturnType<typeof vi.fn>;
  resumeAnimation: ReturnType<typeof vi.fn>;
  d3ReheatSimulation: ReturnType<typeof vi.fn>;
  zoomToFit: ReturnType<typeof vi.fn>;
  d3Force: ReturnType<typeof vi.fn>;
}

const forceGraphControls = (forceGraphModule as unknown as { __refControls: MockForceGraphControls }).__refControls;

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
  return { container, cleanup };
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

async function findAllByTestId(container: Element, testId: string): Promise<Element[]> {
  let matches: Element[] = [];
  await waitFor(() => {
    matches = Array.from(container.querySelectorAll(`[data-testid="${testId}"]`));
    expect(matches.length).toBeGreaterThan(0);
  });
  return matches;
}

async function findByTestId(container: Element, testId: string): Promise<Element> {
  let match: Element | null = null;
  await waitFor(() => {
    match = container.querySelector(`[data-testid="${testId}"]`);
    expect(match).not.toBeNull();
  });
  return match as Element;
}

async function findMarkerByNodeId(container: Element, nodeId: string): Promise<Element> {
  let match: Element | null = null;
  await waitFor(() => {
    match = container.querySelector(`[data-testid="work-nexus-node-marker"][data-node-id="${nodeId}"]`);
    expect(match).not.toBeNull();
  });
  if (!match) throw new Error(`Node marker not found: ${nodeId}`);
  return match;
}

async function findByText(container: Element, pattern: RegExp): Promise<Element> {
  let match: Element | null = null;
  await waitFor(() => {
    match = Array.from(container.querySelectorAll("*")).find((el) => pattern.test(el.textContent ?? "")) ?? null;
    expect(match).not.toBeNull();
  });
  return match as Element;
}

function mockFetch(response: GraphResponse) {
  return vi.fn(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      text: () => Promise.resolve(JSON.stringify(response)),
      json: () => Promise.resolve(response),
    }),
  );
}

const sampleResponse: GraphResponse = {
  nodes: [
    {
      id: "project:alpha",
      kind: "project",
      label: "Alpha Ship",
      color: "#76e4f7",
      icon: "🚀",
    },
    {
      id: "task:alpha:parent",
      kind: "task",
      label: "Parent blocker",
      status: "running",
      board: "alpha",
      priority: 2,
      completed: false,
    },
    {
      id: "task:alpha:child",
      kind: "task",
      label: "Child delivery",
      status: "done",
      board: "alpha",
      completed: true,
      url: "https://example.test/pr/42",
    },
  ],
  edges: [
    {
      id: "contains:alpha:parent",
      kind: "contains",
      source: "project:alpha",
      target: "task:alpha:parent",
    },
    {
      id: "blocks:alpha:parent:child",
      kind: "blocks",
      source: "task:alpha:parent",
      target: "task:alpha:child",
    },
  ],
};

beforeEach(() => {
  vi.useFakeTimers();
  Element.prototype.getBoundingClientRect = vi.fn(() => ({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: 900,
    bottom: 520,
    width: 900,
    height: 520,
    toJSON: () => ({}),
  })) as unknown as typeof Element.prototype.getBoundingClientRect;
  window.matchMedia = vi.fn(() => ({
    matches: false,
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
});

afterEach(() => {
  while (cleanupFns.length > 0) cleanupFns.pop()?.();
  vi.useRealTimers();
  vi.restoreAllMocks();
  Object.values(forceGraphControls).forEach((fn) => fn.mockClear());
});

describe("WorkNexus", () => {
  it("renders the empty state when the API returns no nodes", async () => {
    globalThis.fetch = mockFetch({ nodes: [], edges: [] }) as unknown as typeof fetch;
    const { container } = render(<WorkNexus />);
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
    expect(await findByText(container, /No kanban work found/)).toBeTruthy();
  });

  it("renders nodes and degraded overlay returned by the API", async () => {
    globalThis.fetch = mockFetch({ ...sampleResponse, degraded_mode: ["codex_pr_overlay"] }) as unknown as typeof fetch;
    const { container } = render(<WorkNexus />);
    const markers = await findAllByTestId(container, "work-nexus-node-marker");
    expect(markers).toHaveLength(3);
    expect(await findByText(container, /Overlay degraded: codex_pr_overlay/)).toBeTruthy();
  });

  it("marks active task nodes for subtle glow while completed tasks stay plain", async () => {
    globalThis.fetch = mockFetch(sampleResponse) as unknown as typeof fetch;
    const { container } = render(<WorkNexus />);
    const active = await findMarkerByNodeId(container, "task:alpha:parent");
    const completed = await findMarkerByNodeId(container, "task:alpha:child");

    expect(active.getAttribute("data-node-active-work")).toBe("true");
    expect(active.getAttribute("data-node-completed")).toBe("false");
    expect(completed.getAttribute("data-node-active-work")).toBe("false");
    expect(completed.getAttribute("data-node-completed")).toBe("true");
  });

  it("opens the detail panel on node click and closes it on background click", async () => {
    globalThis.fetch = mockFetch(sampleResponse) as unknown as typeof fetch;
    const { container } = render(<WorkNexus />);
    const node = await findByTestId(container, "mock-node-task:alpha:parent");
    act(() => {
      node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(await findByTestId(container, "work-nexus-detail-panel")).toBeTruthy();
    const bg = await findByTestId(container, "mock-bg");
    act(() => {
      bg.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await waitFor(() => {
      expect(container.querySelector('[data-testid="work-nexus-detail-panel"]')).toBeNull();
    });
  });

  it("pauses animation once the force layout converges", async () => {
    globalThis.fetch = mockFetch(sampleResponse) as unknown as typeof fetch;
    const { container } = render(<WorkNexus />);
    const engineStop = await findByTestId(container, "mock-engine-stop");
    await waitFor(() => {
      expect(forceGraphControls.d3ReheatSimulation).toHaveBeenCalled();
    });
    expect(forceGraphControls.pauseAnimation).not.toHaveBeenCalled();

    act(() => {
      engineStop.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(forceGraphControls.pauseAnimation).toHaveBeenCalled();
  });

  it("polls every 15 seconds", async () => {
    const fetchMock = mockFetch(sampleResponse);
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<WorkNexus />);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });
});
