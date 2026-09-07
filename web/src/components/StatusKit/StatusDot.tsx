import type { JSX } from "react";
import { STATUS_CFG, type Status } from "./constants";

export function StatusDot({ status, className = "" }: { status: Status; className?: string }): JSX.Element {
  const cfg = STATUS_CFG[status];
  return <span className={`inline-block h-2.5 w-2.5 flex-shrink-0 rounded-full ${cfg.dot} ${className}`} aria-label={cfg.label} />;
}
