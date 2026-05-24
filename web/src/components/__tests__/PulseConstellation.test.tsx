import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PulseConstellation from "../PulseConstellation";

// react-force-graph-2d does its own canvas rendering, which jsdom can't
// satisfy. We stub it with a minimal element that exposes the same callback
// surface our tests need.
vi.mock("react-force-graph-2d", () => {
  type Node = { id: string; label?: string };
  return {
    default: function MockForceGraph2D(props: {
      graphData: { nodes: Node[]; links: unknown[] };
      onNodeClick?: (n: Node) => void;
      onBackgroundClick?: () => void;
    }) {
      return (
        <div data-testid="mock-force-graph">
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
    },
  };
});

interface GraphResponse {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
  degraded_mode?: string[];
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
      id: "hive:foo",
      label: "Foo hive",
      group: "hive-active",
      status: "running",
      kind: "hive",
      last_activity: new Date().toISOString(),
    },
    {
      id: "card:42",
      label: "ship the thing",
      group: "card-ready",
      status: "ready",
      kind: "card",
    },
  ],
  edges: [
    {
      id: "track:foo",
      source: "hive:foo",
      target: "card:42",
      kind: "tracking",
    },
  ],
};

beforeEach(() => {
  vi.useFakeTimers();
  (globalThis as { window?: unknown }).window =
    (globalThis as { window?: unknown }).window ?? {};
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("PulseConstellation", () => {
  it("renders the empty state when the response has no nodes", async () => {
    globalThis.fetch = mockFetch({ nodes: [], edges: [] }) as unknown as typeof fetch;
    const { getByText } = render(<PulseConstellation />);
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
    expect(getByText(/No active agents/)).toBeTruthy();
  });

  it("renders one marker per node returned by the API", async () => {
    globalThis.fetch = mockFetch(sampleResponse) as unknown as typeof fetch;
    const { findAllByTestId } = render(<PulseConstellation />);
    const markers = await findAllByTestId(/^mock-node-/);
    expect(markers).toHaveLength(2);
  });

  it("opens the detail panel on node click and closes it on background click", async () => {
    globalThis.fetch = mockFetch(sampleResponse) as unknown as typeof fetch;
    const { findByTestId, queryByTestId } = render(<PulseConstellation />);
    const node = await findByTestId("mock-node-hive:foo");
    fireEvent.click(node);
    expect(await findByTestId("pulse-detail-panel")).toBeTruthy();
    const bg = await findByTestId("mock-bg");
    fireEvent.click(bg);
    await waitFor(() => {
      expect(queryByTestId("pulse-detail-panel")).toBeNull();
    });
  });

  it("re-fetches every 15 seconds", async () => {
    const fetchMock = mockFetch(sampleResponse);
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<PulseConstellation />);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    await act(async () => {
      vi.advanceTimersByTime(15_000);
    });
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });
});
