// @vitest-environment jsdom
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { AuthWidget } from "../AuthWidget";

const cleanupFns: Array<() => void> = [];

function render(ui: ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root | null = createRoot(container);
  act(() => root?.render(ui));
  cleanupFns.push(() => {
    if (!root) return;
    act(() => root?.unmount());
    root = null;
    container.remove();
  });
  return container;
}

async function flushReact() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

afterEach(() => {
  while (cleanupFns.length > 0) cleanupFns.pop()?.();
  delete window.__HERMES_AUTH_REQUIRED__;
  vi.restoreAllMocks();
});

describe("AuthWidget", () => {
  it("does not probe /api/auth/me in loopback session-token mode", async () => {
    window.__HERMES_AUTH_REQUIRED__ = false;
    const getAuthMe = vi.spyOn(api, "getAuthMe");

    const container = render(<AuthWidget />);
    await flushReact();

    expect(getAuthMe).not.toHaveBeenCalled();
    expect(container.textContent).toBe("");
  });

  it("loads and renders identity when the auth gate is active", async () => {
    window.__HERMES_AUTH_REQUIRED__ = true;
    const getAuthMe = vi.spyOn(api, "getAuthMe").mockResolvedValue({
      user_id: "user-123456789012345",
      email: "",
      display_name: "",
      provider: "portal",
    });

    const container = render(<AuthWidget />);
    await flushReact();

    expect(getAuthMe).toHaveBeenCalledOnce();
    expect(container.textContent).toContain("user-123456789…");
    expect(container.textContent).toContain("via portal");
  });
});