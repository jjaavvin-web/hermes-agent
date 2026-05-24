import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PulseQueue, { type PulseQueueCard } from "../PulseQueue";

const CARDS: PulseQueueCard[] = [
  {
    id: "card-aaaaa",
    title: "ship the thing",
    status: "ready",
    board: "kanban-control",
    priority: 5,
    assignee: "queen",
    age_seconds: 720,
  },
  {
    id: "card-bbbbb",
    title: "review pulse PR",
    status: "running",
    board: "kanban-control",
    priority: 4,
    assignee: "coder",
    age_seconds: 90,
  },
  {
    id: "card-ccccc",
    title: "investigate flake",
    status: "blocked",
    board: "kanban-control",
    priority: 3,
    assignee: "tester",
    age_seconds: 24 * 3600,
  },
];

function mockFetchWith(cards: PulseQueueCard[]) {
  return vi.fn(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      text: () => Promise.resolve(JSON.stringify({ cards })),
      json: () => Promise.resolve({ cards }),
    }),
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  (globalThis as { window?: unknown }).window =
    (globalThis as { window?: unknown }).window ?? {};
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("PulseQueue", () => {
  it("renders one chip per card returned by the API", async () => {
    globalThis.fetch = mockFetchWith(CARDS) as unknown as typeof fetch;
    const { findAllByTestId } = render(<PulseQueue />);
    const chips = await findAllByTestId("pulse-queue-chip");
    expect(chips).toHaveLength(3);
    expect(chips.map((c) => c.dataset.status)).toEqual([
      "ready",
      "running",
      "blocked",
    ]);
  });

  it("opens the kanban fallback URL in a new tab on click", async () => {
    globalThis.fetch = mockFetchWith(CARDS) as unknown as typeof fetch;
    const openSpy = vi
      .spyOn(window, "open")
      .mockImplementation(
        () => ({ focus: () => undefined }) as unknown as Window,
      );
    const { findAllByTestId } = render(<PulseQueue />);
    const chips = await findAllByTestId("pulse-queue-chip");
    fireEvent.click(chips[0]);
    expect(openSpy).toHaveBeenCalledTimes(1);
    const [url, target] = openSpy.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:9119/kanban");
    expect(target).toBe("_blank");
  });

  it("renders the empty state when the API returns no cards", async () => {
    globalThis.fetch = mockFetchWith([]) as unknown as typeof fetch;
    const { findByText } = render(<PulseQueue />);
    expect(
      await findByText(/Queue empty — no ready\/running cards/),
    ).toBeTruthy();
  });

  it("re-fetches /api/pulse/queue every 10 seconds", async () => {
    const fetchMock = mockFetchWith(CARDS);
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    render(<PulseQueue />);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });
});
