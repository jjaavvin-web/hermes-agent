import type { JSX } from "react";
import type { OSDiagnostic } from "@/lib/api";
import { STATUS_CFG, type Status } from "./constants";
import { StatusDot } from "./StatusDot";

export type StatusDiagnostic = Omit<OSDiagnostic, "severity"> & { severity: Status };

export function DiagnosticRow({ diag }: { diag: StatusDiagnostic }): JSX.Element {
  const cfg = STATUS_CFG[diag.severity];
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-md border px-3 py-2" style={{ borderColor: cfg.ring, background: cfg.soft }}>
      <StatusDot status={diag.severity} className="self-center" />
      <span className="rounded px-1.5 py-0.5 font-mono text-xs font-semibold" style={{ color: cfg.color, background: `${cfg.color}1f` }}>
        {diag.source}
      </span>
      <span className="min-w-0 flex-1 text-xs text-text-primary">
        {diag.message}
        {diag.hint && <span className="text-text-secondary"> — {diag.hint}</span>}
      </span>
    </li>
  );
}
