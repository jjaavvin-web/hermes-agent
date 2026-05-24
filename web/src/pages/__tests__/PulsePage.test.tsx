import { render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PulsePage from "../PulsePage";

vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setTitle: vi.fn() }),
}));

const KPI_RESPONSE = {
  active_hives: 2,
  pending_cards: 7,
  max_usage_pct: null,
  today_spend_usd: 4.32,
  today_pr_merges: 3,
  last_completion: {
    slug: "infaudit",
    completed_at: new Date(Date.now() - 12 * 60 * 1000).toISOString(),
    summary: "Status: COMPLETE",
  },
};

describe("PulsePage", () => {
  beforeEach(() => {
    // Mock global fetch so PulseChips' /api/pulse/kpis call resolves.
    (globalThis as { fetch?: unknown }).fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        statusText: "OK",
        text: () => Promise.resolve(JSON.stringify(KPI_RESPONSE)),
        json: () => Promise.resolve(KPI_RESPONSE),
      }),
    ) as unknown as typeof fetch;
    (globalThis as { window?: unknown }).window =
      (globalThis as { window?: unknown }).window ?? {};
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders without crashing", () => {
    render(
      <MemoryRouter>
        <PulsePage />
      </MemoryRouter>,
    );
  });

  it("fires a fetch to /api/pulse/kpis on mount", async () => {
    render(
      <MemoryRouter>
        <PulsePage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(
      calls.some(([url]) =>
        typeof url === "string" && url.endsWith("/api/pulse/kpis"),
      ),
    ).toBe(true);
  });

  it("renders all four zone placeholders", () => {
    const { container, getByText } = render(
      <MemoryRouter>
        <PulsePage />
      </MemoryRouter>,
    );
    // The root scoping wrapper exists so the theme module can attach.
    expect(container.querySelector(".pulse-root")).not.toBeNull();
    // Top zone is the chips container — covered by the fetch assertion above
    // but we also assert its grid area is present.
    expect(container.querySelector(".pulse-zone-top")).not.toBeNull();
    expect(getByText(/Constellation graph — coming in H3/)).toBeTruthy();
    expect(getByText(/Live agent transcript — coming in H4/)).toBeTruthy();
    expect(getByText(/Task queue strip — coming in H4/)).toBeTruthy();
  });
});
