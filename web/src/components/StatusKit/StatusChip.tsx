import type { JSX } from "react";
import { STATUS_CFG, type Status } from "./constants";
import { StatusDot } from "./StatusDot";

export function StatusChip({ status }: { status: Status }): JSX.Element {
  const cfg = STATUS_CFG[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-[0.12em] ${cfg.chip}`}>
      <StatusDot status={status} className="h-1.5 w-1.5" />
      {cfg.label}
    </span>
  );
}
