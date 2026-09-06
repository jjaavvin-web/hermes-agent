import type { JSX } from "react";
import { STATUS_CFG, type Status } from "./constants";
import { StatusDot } from "./StatusDot";

export function MetricChip({
  label,
  value,
  status = "green",
  onClick,
}: {
  label: string;
  value: string | number;
  status?: Status;
  onClick: () => void;
}): JSX.Element {
  const cfg = STATUS_CFG[status];
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex min-h-[44px] min-w-0 max-sm:flex-shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-left text-xs transition hover:bg-accent/30 sm:min-h-0"
      style={{ borderColor: cfg.ring, background: cfg.soft }}
      title={`${label}: ${value}`}
    >
      <StatusDot status={status} className="h-1.5 w-1.5" />
      <span className="max-w-[9rem] truncate text-text-tertiary">{label}</span>
      <span className="font-mono font-semibold text-text-primary">{value}</span>
    </button>
  );
}
