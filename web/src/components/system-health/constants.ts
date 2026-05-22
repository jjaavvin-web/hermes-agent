import {
  Boxes,
  Box,
  Circle,
  ClipboardCheck,
  Container,
  Cpu,
  Database,
  FolderGit2,
  LayoutDashboard,
  Layers,
  Network,
  Plug,
  Puzzle,
  Radio,
  Share2,
  SquareKanban,
  Timer,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import type { NexusHealthStatus } from "@/lib/api";

/** Visual metadata for each health status, aligned with the dashboard theme. */
export interface StatusMeta {
  label: string;
  color: string;
  soft: string;
  ring: string;
}

export const STATUS_META: Record<NexusHealthStatus, StatusMeta> = {
  ok: { label: "Healthy", color: "#4ade80", soft: "rgba(74,222,128,0.12)", ring: "rgba(74,222,128,0.45)" },
  warn: { label: "Degraded", color: "#ffbd38", soft: "rgba(255,189,56,0.13)", ring: "rgba(255,189,56,0.5)" },
  error: { label: "Down", color: "#fb2c36", soft: "rgba(251,44,54,0.15)", ring: "rgba(251,44,54,0.55)" },
  auth_gated: { label: "Auth gate", color: "#38bdf8", soft: "rgba(56,189,248,0.13)", ring: "rgba(56,189,248,0.5)" },
  unknown: { label: "Unknown", color: "#7c91a8", soft: "rgba(124,145,168,0.12)", ring: "rgba(124,145,168,0.4)" },
};

export const STATUS_ORDER: NexusHealthStatus[] = [
  "ok",
  "warn",
  "error",
  "auth_gated",
  "unknown",
];

export function statusMeta(status: string): StatusMeta {
  return STATUS_META[status as NexusHealthStatus] ?? STATUS_META.unknown;
}

/** Icon per node kind. */
const KIND_ICON: Record<string, LucideIcon> = {
  dashboard: LayoutDashboard,
  runtime: Cpu,
  gateway: Radio,
  kanban: SquareKanban,
  "control-plane": Timer,
  "agent-lane": Workflow,
  "service-group": Layers,
  service: Box,
  "network-group": Network,
  port: Plug,
  "container-group": Boxes,
  container: Container,
  gitnexus: Share2,
  memory: Database,
  mcp: Puzzle,
  source: FolderGit2,
  audit: ClipboardCheck,
};

export function kindIcon(kind: string): LucideIcon {
  return KIND_ICON[kind] ?? Circle;
}

/** Column groups, in left-to-right layout order. */
export interface GroupMeta {
  id: string;
  label: string;
}

export const GROUPS: GroupMeta[] = [
  { id: "core", label: "Core" },
  { id: "control", label: "Control Plane" },
  { id: "services", label: "systemd Units" },
  { id: "network", label: "Listening Ports" },
  { id: "containers", label: "MVMS Containers" },
  { id: "integrations", label: "Integrations" },
  { id: "data", label: "Data Plane" },
];

export const GROUP_ORDER: string[] = GROUPS.map((g) => g.id);

export function groupLabel(id: string): string {
  return GROUPS.find((g) => g.id === id)?.label ?? id;
}

/** Layout geometry for the layered node graph. */
export const COL_W = 296;
export const ROW_H = 108;
