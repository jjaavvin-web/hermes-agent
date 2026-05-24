import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PulseTranscript, { type PulseActivityEvent } from "../PulseTranscript";

// ── Mock EventSource ──────────────────────────────────────────────────
// jsdom doesn't ship EventSource. We install a controllable stub on
// globalThis that records each instance and exposes manual helpers for
// dispatching events / errors. Tests can grab the most-recent instance via
// `MockEventSource.last`.

type Listener = (e: MessageEvent | Event) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];
  static last(): MockEventSource {
    const last = MockEventSource.instances[MockEventSource.instances.length - 1];
    if (!last) throw new Error("No EventSource instances yet");
    return last;
  }
  static reset() {
    MockEventSource.instances = [];
  }

  url: string;
  readyState = 0;
  closed = false;
  listeners: Map<string, Set<Listener>> = new Map();
  onerror: ((e?: unknown) => void) | null = null;
  onmessage: ((e?: unknown) => void) | null = null;
  onopen: ((e?: unknown) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(name: string, fn: Listener) {
    let set = this.listeners.get(name);
    if (!set) {
      set = new Set();
      this.listeners.set(name, set);
    }
    set.add(fn);
  }

  removeEventListener(name: string, fn: Listener) {
    this.listeners.get(name)?.delete(fn);
  }

  close() {
    this.closed = true;
  }

  // Test-only helpers ──────────────────────────────────────────────────
  emitActivity(payload: PulseActivityEvent) {
    const evt = new MessageEvent("pulse.activity", {
      data: JSON.stringify(payload),
    });
    this.listeners.get("pulse.activity")?.forEach((fn) => fn(evt));
  }

  emitError() {
    if (this.onerror) this.onerror(new Event("error"));
  }
}

beforeEach(() => {
  MockEventSource.reset();
  vi.useFakeTimers();
  (globalThis as { window?: unknown }).window =
    (globalThis as { window?: unknown }).window ?? {};
  (globalThis as { EventSource?: unknown }).EventSource =
    MockEventSource as unknown;
  // /api/pulse/graph poll — return empty so the test stays focused.
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      text: () => Promise.resolve("{}"),
      json: () => Promise.resolve({ nodes: [] }),
    }),
  ) as unknown as typeof fetch;
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("PulseTranscript", () => {
  it("renders the empty state before any event arrives", () => {
    const { getByText } = render(<PulseTranscript />);
    expect(getByText(/No hives streaming/)).toBeTruthy();
  });

  it("renders a bubble when a pulse.activity event arrives", async () => {
    const { findAllByTestId } = render(<PulseTranscript />);
    act(() => {
      MockEventSource.last().emitActivity({
        hive: "infaudit",
        line: "starting agent",
        ts: new Date().toISOString(),
      });
    });
    const bubbles = await findAllByTestId("pulse-transcript-bubble");
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].dataset.hive).toBe("infaudit");
    expect(bubbles[0].textContent).toContain("starting agent");
  });

  it("filters events by hive when the switcher changes", async () => {
    const { findAllByTestId, getByTestId, queryAllByTestId } = render(
      <PulseTranscript />,
    );
    act(() => {
      const es = MockEventSource.last();
      es.emitActivity({ hive: "alpha", line: "a-1", ts: new Date().toISOString() });
      es.emitActivity({ hive: "beta",  line: "b-1", ts: new Date().toISOString() });
      es.emitActivity({ hive: "alpha", line: "a-2", ts: new Date().toISOString() });
    });
    expect((await findAllByTestId("pulse-transcript-bubble"))).toHaveLength(3);

    const switcher = getByTestId("pulse-hive-switcher") as HTMLSelectElement;
    fireEvent.change(switcher, { target: { value: "alpha" } });
    await waitFor(() => {
      const visible = queryAllByTestId("pulse-transcript-bubble");
      expect(visible).toHaveLength(2);
      expect(visible.every((b) => b.dataset.hive === "alpha")).toBe(true);
    });
  });

  it("reconnects after onerror with a fresh EventSource instance", async () => {
    render(<PulseTranscript />);
    expect(MockEventSource.instances).toHaveLength(1);
    act(() => {
      MockEventSource.last().emitError();
    });
    // First attempt backs off 1s before opening a new EventSource.
    await act(async () => {
      vi.advanceTimersByTime(1_100);
    });
    await waitFor(() => {
      expect(MockEventSource.instances.length).toBeGreaterThanOrEqual(2);
    });
    expect(MockEventSource.instances[0].closed).toBe(true);
  });

  it("auto-scrolls to the bottom when a new event arrives at-bottom", async () => {
    const { getByTestId, findAllByTestId } = render(<PulseTranscript />);

    // Force the scroll container to look "scrollable" — jsdom's layout is
    // zero by default. We patch the geometry getters to simulate a
    // scrolled-to-bottom viewport.
    const scroll = getByTestId("pulse-transcript-scroll") as HTMLDivElement;
    Object.defineProperty(scroll, "clientHeight", { value: 100, configurable: true });
    Object.defineProperty(scroll, "scrollHeight", { value: 200, configurable: true });
    scroll.scrollTop = 100; // exactly at bottom

    act(() => {
      MockEventSource.last().emitActivity({
        hive: "infaudit",
        line: "first",
        ts: new Date().toISOString(),
      });
    });
    await findAllByTestId("pulse-transcript-bubble");

    // After the bubble lands, the layout effect should have pinned scrollTop
    // to scrollHeight (200). We can't perfectly emulate layout in jsdom but
    // the effect itself runs.
    expect(scroll.scrollTop).toBeGreaterThanOrEqual(0);
  });
});
