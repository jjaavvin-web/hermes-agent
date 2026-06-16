export type Status = "green" | "amber" | "red" | "unknown" | "info";

export interface StatusConfig {
  label: string;
  color: string;
  dot: string;
  chip: string;
  ring: string;
  soft: string;
}

export const STATUS_CFG: Record<Status, StatusConfig> = {
  green: {
    label: "Nominal",
    color: "#4ade80",
    dot: "bg-[#4ade80] shadow-[0_0_7px_#4ade80]",
    chip: "bg-[rgba(74,222,128,0.12)] text-[#4ade80] border border-[rgba(74,222,128,0.35)]",
    ring: "rgba(74,222,128,0.35)",
    soft: "rgba(74,222,128,0.08)",
  },
  amber: {
    label: "Degraded",
    color: "#ffbd38",
    dot: "bg-[#ffbd38] shadow-[0_0_7px_#ffbd38]",
    chip: "bg-[rgba(255,189,56,0.12)] text-[#ffbd38] border border-[rgba(255,189,56,0.35)]",
    ring: "rgba(255,189,56,0.45)",
    soft: "rgba(255,189,56,0.07)",
  },
  red: {
    label: "Critical",
    color: "#fb2c36",
    dot: "bg-[#fb2c36] shadow-[0_0_7px_#fb2c36]",
    chip: "bg-[rgba(251,44,54,0.12)] text-[#fb2c36] border border-[rgba(251,44,54,0.40)]",
    ring: "rgba(251,44,54,0.55)",
    soft: "rgba(251,44,54,0.07)",
  },
  unknown: {
    label: "Unknown",
    color: "#7c91a8",
    dot: "bg-[#7c91a8]",
    chip: "bg-[rgba(124,145,168,0.10)] text-[#7c91a8] border border-[rgba(124,145,168,0.30)]",
    ring: "rgba(124,145,168,0.30)",
    soft: "rgba(124,145,168,0.06)",
  },
  info: {
    label: "Informational",
    color: "#7c91a8",
    dot: "bg-[#7c91a8]",
    chip: "bg-[rgba(124,145,168,0.10)] text-[#7c91a8] border border-[rgba(124,145,168,0.30)]",
    ring: "rgba(124,145,168,0.30)",
    soft: "rgba(124,145,168,0.06)",
  },
};

export const SEVERITY_SCORE: Record<Status, number> = {
  red: 3,
  amber: 2,
  unknown: 1,
  info: 0,
  green: 0,
};

export function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}
