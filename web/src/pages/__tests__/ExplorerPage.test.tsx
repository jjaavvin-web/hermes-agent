// @ts-nocheck
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";
import ExplorerPage from "../ExplorerPage";

vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: {
      explorer: { title: "Code Explorer" },
      app: { nav: { explorer: "Explorer" } },
    },
  }),
}));

vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setEnd: vi.fn(), setAfterTitle: vi.fn() }),
}));

vi.mock("@/themes", () => ({
  useTheme: () => ({ theme: {} }),
}));

describe("ExplorerPage", () => {
  it("renders without crashing", () => {
    render(
      <MemoryRouter>
        <ExplorerPage />
      </MemoryRouter>,
    );
  });

  it("renders an iframe with src /explorer/", () => {
    const { container } = render(
      <MemoryRouter>
        <ExplorerPage />
      </MemoryRouter>,
    );
    const iframe = container.querySelector("iframe");
    expect(iframe).not.toBeNull();
    expect(iframe?.getAttribute("src")).toBe("/explorer/");
  });

  it("iframe has sandbox attribute", () => {
    const { container } = render(
      <MemoryRouter>
        <ExplorerPage />
      </MemoryRouter>,
    );
    const iframe = container.querySelector("iframe");
    expect(iframe?.getAttribute("sandbox")).toContain("allow-scripts");
    expect(iframe?.getAttribute("sandbox")).toContain("allow-same-origin");
  });
});
