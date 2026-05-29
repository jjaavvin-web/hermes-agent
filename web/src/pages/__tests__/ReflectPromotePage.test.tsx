import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createRoot } from "react-dom/client";
import { ReflectPromotePage } from "../ReflectPromotePage";

const sample = {
  candidates: [
    {
      id: "lesson-1",
      project: "hermes",
      situation: "Goal resume",
      mistake_or_insight: "Durable anchors beat memory.",
      correction: "Cross-check git before ledger trust.",
      status: "pending",
      tags: ["ops"],
    },
  ],
};

describe("ReflectPromotePage", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    vi.restoreAllMocks();
  });

  it("renders candidates and sends explicit approve/reject actions", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      calls.push({ url: String(url), init });
      if (String(url).endsWith("/api/reflect-promote/candidates")) {
        return new Response(JSON.stringify(sample), { status: 200 });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });

    const root = createRoot(document.getElementById("root")!);
    root.render(<ReflectPromotePage />);

    await vi.waitFor(() => {
      expect(document.body.textContent).toContain("Goal resume");
      expect(document.body.textContent).toContain("Durable anchors beat memory.");
    });

    const buttons = Array.from(document.querySelectorAll("button"));
    buttons.find((button) => button.textContent === "Approve")?.click();
    await vi.waitFor(() => {
      expect(calls.some((call) => call.url.endsWith("/api/reflect-promote/candidates/lesson-1/approve"))).toBe(true);
    });

    buttons.find((button) => button.textContent === "Reject")?.click();
    await vi.waitFor(() => {
      expect(calls.some((call) => call.url.endsWith("/api/reflect-promote/candidates/lesson-1/reject"))).toBe(true);
    });
  });
});
