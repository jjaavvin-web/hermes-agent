/**
 * OS Nexus — flowchart view of how the whole AI infrastructure interconnects.
 *
 * Renders snapshot.graph (static topology, live health bound onto nodes by the
 * backend) as a left-to-right React Flow diagram: columns by group in
 * architecture order (surfaces → control → providers → memory → protection →
 * host), directional smoothstep edges with arrowheads, edge styling by link
 * state (live / disabled / gated / broken), a legend chip row, and a
 * click-to-inspect side panel backed by snapshot.sections + diagnostics.
 *
 * Adapted from components/system-health/HealthGraph.tsx and reuses its
 * statusMeta()/kindIcon() visual language so the two graph tabs feel related.
 * Layout hints below are presentation-only — topology comes from the API.
 */

import { memo, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  BaseEdge,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  getSmoothStepPath,
  useNodesInitialized,
  useReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "@/components/system-health/system-health.css";
import { X } from "lucide-react";
import type {
  OSDiagnostic,
  OSGraphEdge,
  OSGraphNode,
  OSSection,
  OSSnapshot,
  OSStatus,
} from "@/lib/api";
import {
  COL_W,
  ROW_H,
  kindIconElement,
  statusMeta,
  type StatusMeta,
} from "@/components/system-health/constants";

// ---------------------------------------------------------------------------
// Status adapters — reuse the system-health palette for OS statuses
// ---------------------------------------------------------------------------

const OS_TO_HEALTH: Record<OSStatus, string> = {
  green: "ok",
  amber: "warn",
  red: "error",
  unknown: "unknown",
};

/** statusMeta() adapter: OS snapshot statuses → system-health palette. */
function osMeta(status: string): StatusMeta {
  return statusMeta(OS_TO_HEALTH[status as OSStatus] ?? "unknown");
}

const OS_STATUS_LABEL: Record<OSStatus, string> = {
  green: "Nominal",
  amber: "Degraded",
  red: "Critical",
  unknown: "Unknown",
};

const SEVERITY: Record<OSStatus, number> = {
  red: 3,
  amber: 2,
  unknown: 1,
  green: 0,
};

// ---------------------------------------------------------------------------
// Layout constants + presentation-only hints (topology itself is API-driven)
// ---------------------------------------------------------------------------

const NODE_W = 232;
const NODE_H = 56;
const GAP_W = COL_W - NODE_W; // horizontal corridor between columns
const GROUP_LABEL_Y = -72;
/** Groups with more than this many nodes split into two sub-columns. */
const SPLIT_THRESHOLD = 6;
/** Minimum vertical clearance between edge labels sharing a corridor. */
const LABEL_MIN_GAP = 22;

const GROUP_ORDER: string[] = [
  "surfaces",
  "control",
  "providers",
  "memory",
  "protection",
  "host",
];

const GROUP_LABEL: Record<string, string> = {
  surfaces: "Surfaces",
  control: "Control Plane",
  providers: "Providers",
  memory: "Memory Stores",
  protection: "Protection",
  host: "Host",
};

/**
 * Within-column row order (layout polish only — unknown ids fall back to API
 * order). Tuned so heavily-linked nodes sit adjacent to their peers and the
 * long left-to-right corridors thread between rows instead of across them.
 */
const ROW_HINT: Record<string, number> = {
  // surfaces
  "claude-code": 0,
  discord: 1,
  dashboard: 2,
  "codex-pipeline": 3,
  // control — timers (no edges) takes row 0 so the claude-code→memory bus
  // corridor passes a leaf node, not the gateway hub.
  timers: 0,
  watchdog: 1,
  gateway: 2,
  "hermes-cron": 3,
  // providers
  "chatgpt-backend": 0,
  "claude-max": 1,
  openrouter: 2,
  // memory, sub-column 0 (consumers / APIs)
  "state-db": 0,
  "kanban-db": 1,
  mvms: 2,
  "honcho-api": 3,
  "honcho-deriver": 4,
  // memory, sub-column 1 (stores)
  "claude-memory": 0,
  "supabase-db": 1,
  "honcho-redis": 2,
  "honcho-db": 3,
  "hermes-memories": 4,
  // protection
  "nightly-backup": 0,
  veracrypt: 1,
  "backups-dir": 2,
  // host
  "wsl-host": 0,
};

/** Sub-column assignment for the split memory group (left = consumers/APIs). */
const LANE_HINT: Record<string, number> = {
  "state-db": 0,
  "kanban-db": 0,
  mvms: 0,
  "honcho-api": 0,
  "honcho-deriver": 0,
  "claude-memory": 1,
  "supabase-db": 1,
  "honcho-redis": 1,
  "honcho-db": 1,
  "hermes-memories": 1,
};

/**
 * Corridor hints for forward edges spanning multiple columns: turn vertical in
 * the first gap after the source ("first") or the last gap before the target
 * ("last", default). Keyed `source>target` with an optional `:state` suffix to
 * split parallel edges between the same pair.
 */
const ROUTE_HINT: Record<string, "first" | "last"> = {
  "codex-pipeline>chatgpt-backend": "first",
  "gateway>state-db": "first",
  "claude-code>mvms:gated": "first",
};

// ---------------------------------------------------------------------------
// Edge state visuals
// ---------------------------------------------------------------------------

interface EdgeStateMeta {
  label: string;
  /** Fixed stroke color; live edges inherit the worst endpoint status color. */
  color?: string;
  dash?: string;
}

const EDGE_STATE_META: Record<string, EdgeStateMeta> = {
  live: { label: "Live" },
  disabled: { label: "Disabled", color: "#7c91a8", dash: "7 5" },
  gated: { label: "Human-gated", color: "#ffbd38", dash: "7 5" },
  broken: { label: "Broken", color: "#fb2c36", dash: "5 4" },
};

// ---------------------------------------------------------------------------
// React Flow node + edge types
// ---------------------------------------------------------------------------

interface OSNodeData extends Record<string, unknown> {
  label: string;
  kind: string;
  status: OSStatus;
  detail: string;
}

type OSRFNode = Node<OSNodeData, "os">;

interface GroupLabelData extends Record<string, unknown> {
  title: string;
  count: number;
  width: number;
}

type GroupLabelRFNode = Node<GroupLabelData, "os-group">;
type CanvasNode = OSRFNode | GroupLabelRFNode;

interface OSEdgeData extends Record<string, unknown> {
  centerX?: number;
  labelX?: number;
  labelY?: number;
  labelText?: string;
  labelColor?: string;
}

type OSRFEdge = Edge<OSEdgeData, "os">;

const HANDLE_STYLE: React.CSSProperties = {
  width: 5,
  height: 5,
  background: "rgba(120,150,180,0.55)",
};

/** A single infrastructure node rendered on the nexus canvas. */
const OSFlowNode = memo(function OSFlowNode({
  data,
  selected,
}: NodeProps<OSRFNode>) {
  const meta = osMeta(data.status);
  const attention = data.status === "red" || data.status === "amber";

  return (
    <div
      title={data.detail || data.label}
      style={{
        width: NODE_W,
        height: NODE_H,
        boxSizing: "border-box",
        position: "relative",
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "9px 12px",
        borderRadius: 12,
        background:
          "linear-gradient(180deg, rgba(17,25,35,0.97) 0%, rgba(10,15,22,0.97) 100%)",
        border: `1px solid ${selected ? meta.color : meta.ring}`,
        boxShadow: selected
          ? `0 0 0 1.5px ${meta.color}, 0 12px 34px -10px ${meta.color}55`
          : "0 6px 18px -10px rgba(0,0,0,0.85)",
        transition: "box-shadow .2s ease, border-color .2s ease",
        cursor: "pointer",
      }}
    >
      <Handle type="target" position={Position.Left} id="l" style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Left} id="lt" style={HANDLE_STYLE} />
      <Handle type="target" position={Position.Right} id="rt" style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Right} id="r" style={HANDLE_STYLE} />
      <Handle type="target" position={Position.Top} id="t" style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Top} id="ts" style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Bottom} id="b" style={HANDLE_STYLE} />
      <Handle type="target" position={Position.Bottom} id="bt" style={HANDLE_STYLE} />

      <div
        style={{
          flexShrink: 0,
          width: 32,
          height: 32,
          borderRadius: 9,
          background: meta.soft,
          color: meta.color,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {kindIconElement(data.kind, { size: 17, strokeWidth: 2 })}
      </div>

      <div style={{ minWidth: 0, flex: 1 }}>
        <div
          style={{
            fontSize: 12.5,
            fontWeight: 600,
            color: "#e9eff6",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {data.label}
        </div>
        <div
          style={{
            marginTop: 2,
            fontSize: 9,
            letterSpacing: "0.09em",
            textTransform: "uppercase",
            color: "rgba(168,192,214,0.5)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {data.kind.replace(/-/g, " ")}
        </div>
      </div>

      <span
        className={data.status === "red" ? "sh-pulse" : undefined}
        style={{
          position: "absolute",
          top: 9,
          right: 9,
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: meta.color,
          boxShadow: `0 0 9px ${meta.color}`,
        }}
      />
      {attention && (
        <span
          style={{
            position: "absolute",
            left: 0,
            top: 10,
            bottom: 10,
            width: 3,
            borderRadius: 3,
            background: meta.color,
          }}
        />
      )}
    </div>
  );
});

/** Non-interactive column caption above each architecture group. */
const GroupLabelNode = memo(function GroupLabelNode({
  data,
}: NodeProps<GroupLabelRFNode>) {
  return (
    <div style={{ width: data.width, textAlign: "center", pointerEvents: "none" }}>
      <div
        style={{
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: "0.22em",
          textTransform: "uppercase",
          color: "rgba(168,192,214,0.62)",
          whiteSpace: "nowrap",
        }}
      >
        {data.title}
        <span
          style={{
            marginLeft: 7,
            letterSpacing: "0.05em",
            color: "rgba(168,192,214,0.35)",
          }}
        >
          {data.count}
        </span>
      </div>
      <div
        style={{
          margin: "6px auto 0",
          height: 1,
          width: "78%",
          background:
            "linear-gradient(90deg, transparent, rgba(168,192,214,0.35), transparent)",
        }}
      />
    </div>
  );
});

const NODE_TYPES = { os: OSFlowNode, "os-group": GroupLabelNode };

/**
 * Directional smoothstep edge with a controllable vertical-turn x (so long
 * edges turn inside column gaps, never through a column of nodes) and a
 * pre-computed, de-overlapped label position.
 */
const OSFlowEdge = memo(function OSFlowEdge({
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  style,
  markerEnd,
}: EdgeProps<OSRFEdge>) {
  const [path, defaultLabelX, defaultLabelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 10,
    centerX: data?.centerX,
  });

  return (
    <BaseEdge
      path={path}
      style={style}
      markerEnd={markerEnd}
      label={data?.labelText}
      labelX={data?.labelX ?? defaultLabelX}
      labelY={data?.labelY ?? defaultLabelY}
      labelStyle={{
        fill: data?.labelColor ?? "rgba(205,222,238,0.75)",
        fontSize: 8.5,
        fontWeight: 600,
        letterSpacing: "0.04em",
      }}
      labelShowBg
      labelBgStyle={{
        fill: "#0a0f16",
        fillOpacity: 0.92,
        stroke: "rgba(120,150,180,0.18)",
        strokeWidth: 0.5,
      }}
      labelBgPadding={[4, 2]}
      labelBgBorderRadius={4}
    />
  );
});

const EDGE_TYPES = { os: OSFlowEdge };

// ---------------------------------------------------------------------------
// Layout — columns by group (memory splits into two sub-columns), routed edges
// ---------------------------------------------------------------------------

interface PlacedNode {
  node: OSGraphNode;
  col: number;
  row: number;
  x: number;
  y: number;
}

interface PreEdge {
  edge: OSGraphEdge;
  src: PlacedNode;
  tgt: PlacedNode;
  sourceHandle: string;
  targetHandle: string;
  centerX?: number;
  /** Label anchor; maxY bounds the de-overlap shift along the edge. */
  label: { x: number; y: number; maxY: number } | null;
}

/** Lay graph nodes into group columns and translate edges into routed RF edges. */
function buildFlow(
  graph: { nodes: OSGraphNode[]; edges: OSGraphEdge[] },
  selectedId: string | null,
): { nodes: CanvasNode[]; edges: OSRFEdge[] } {
  // 1. Bucket nodes by group, in architecture order (unknown groups appended).
  const byGroup = new Map<string, OSGraphNode[]>();
  for (const node of graph.nodes) {
    const arr = byGroup.get(node.group);
    if (arr) arr.push(node);
    else byGroup.set(node.group, [node]);
  }
  const groupIds = [
    ...GROUP_ORDER.filter((g) => byGroup.has(g)),
    ...[...byGroup.keys()].filter((g) => !GROUP_ORDER.includes(g)),
  ];

  // 2. Split big groups into two sub-columns and order rows by hint.
  const placed = new Map<string, PlacedNode>();
  const lanes: { col: number; nodes: OSGraphNode[] }[] = [];
  const geoms: { id: string; startCol: number; laneCount: number }[] = [];
  let col = 0;
  for (const groupId of groupIds) {
    const members = byGroup.get(groupId) ?? [];
    const laneCount = members.length > SPLIT_THRESHOLD ? 2 : 1;
    const laneMembers: OSGraphNode[][] = Array.from({ length: laneCount }, () => []);
    for (const member of members) {
      const hinted =
        laneCount === 1
          ? 0
          : (LANE_HINT[member.id] ??
            (laneMembers[0].length <= laneMembers[1].length ? 0 : 1));
      laneMembers[Math.min(hinted, laneCount - 1)].push(member);
    }
    laneMembers.forEach((arr, laneIndex) => {
      arr.sort((a, b) => (ROW_HINT[a.id] ?? 99) - (ROW_HINT[b.id] ?? 99));
      lanes.push({ col: col + laneIndex, nodes: arr });
    });
    geoms.push({ id: groupId, startCol: col, laneCount });
    col += laneCount;
  }

  // 3. Position nodes: x by column, y row-stacked + vertically centered.
  const maxRows = Math.max(1, ...lanes.map((lane) => lane.nodes.length));
  for (const lane of lanes) {
    const offset = (maxRows - lane.nodes.length) / 2;
    lane.nodes.forEach((node, row) => {
      placed.set(node.id, {
        node,
        col: lane.col,
        row,
        x: lane.col * COL_W,
        y: (offset + row) * ROW_H,
      });
    });
  }

  const rfNodes: CanvasNode[] = [];
  for (const geom of geoms) {
    rfNodes.push({
      id: `group:${geom.id}`,
      type: "os-group",
      position: { x: geom.startCol * COL_W, y: GROUP_LABEL_Y },
      selectable: false,
      draggable: false,
      focusable: false,
      data: {
        title: GROUP_LABEL[geom.id] ?? geom.id,
        count: (byGroup.get(geom.id) ?? []).length,
        width: geom.laneCount * COL_W - GAP_W,
      },
    });
  }
  for (const p of placed.values()) {
    rfNodes.push({
      id: p.node.id,
      type: "os",
      position: { x: p.x, y: p.y },
      width: NODE_W,
      height: NODE_H,
      selected: p.node.id === selectedId,
      draggable: false,
      data: {
        label: p.node.label,
        kind: p.node.kind,
        status: p.node.status,
        detail: p.node.detail ?? "",
      },
    });
  }

  // 4. Route edges. Same-column links go vertical; cross-column links turn
  //    inside a column gap (deterministic per-edge jitter fans out parallel
  //    corridors so verticals and labels do not stack).
  const pre: PreEdge[] = [];
  for (const edge of graph.edges) {
    const src = placed.get(edge.source);
    const tgt = placed.get(edge.target);
    if (!src || !tgt) continue;
    const srcCY = src.y + NODE_H / 2;
    const tgtCY = tgt.y + NODE_H / 2;

    let sourceHandle: string;
    let targetHandle: string;
    let centerX: number | undefined;
    let label: PreEdge["label"] = null;

    if (src.col === tgt.col) {
      const down = src.y < tgt.y;
      sourceHandle = down ? "b" : "ts";
      targetHandle = down ? "t" : "bt";
      const fromY = down ? src.y + NODE_H : src.y;
      const toY = down ? tgt.y : tgt.y + NODE_H;
      if (edge.label) {
        label = {
          x: src.x + NODE_W / 2,
          y: (fromY + toY) / 2,
          maxY: Math.max(fromY, toY),
        };
      }
    } else {
      const forward = src.col < tgt.col;
      const jitter = -16 + ((src.row * 13 + tgt.row * 7) % 5) * 8;
      if (forward) {
        sourceHandle = "r";
        targetHandle = "l";
        const hint =
          ROUTE_HINT[`${edge.source}>${edge.target}:${edge.state}`] ??
          ROUTE_HINT[`${edge.source}>${edge.target}`] ??
          "last";
        const base =
          hint === "first" ? src.x + NODE_W + GAP_W / 2 : tgt.x - GAP_W / 2;
        centerX = base + jitter;
      } else {
        sourceHandle = "lt";
        targetHandle = "rt";
        centerX = tgt.x + NODE_W + GAP_W / 2 + jitter;
      }
      if (edge.label) {
        label =
          srcCY === tgtCY
            ? { x: centerX, y: srcCY, maxY: srcCY }
            : {
                x: centerX,
                y: (srcCY + tgtCY) / 2,
                maxY: Math.max(srcCY, tgtCY) - 12,
              };
      }
    }

    pre.push({ edge, src, tgt, sourceHandle, targetHandle, centerX, label });
  }

  // 5. De-overlap labels sharing a corridor: bucket by x, then enforce a
  //    minimum vertical gap by sliding labels along their edge segment.
  const buckets = new Map<number, PreEdge[]>();
  for (const p of pre) {
    if (!p.label) continue;
    const key = Math.round(p.label.x / (COL_W / 2));
    const arr = buckets.get(key);
    if (arr) arr.push(p);
    else buckets.set(key, [p]);
  }
  for (const arr of buckets.values()) {
    arr.sort((a, b) => (a.label?.y ?? 0) - (b.label?.y ?? 0));
    for (let i = 1; i < arr.length; i++) {
      const prev = arr[i - 1].label;
      const cur = arr[i].label;
      if (!prev || !cur) continue;
      if (cur.y - prev.y < LABEL_MIN_GAP) {
        cur.y = Math.min(prev.y + LABEL_MIN_GAP, cur.maxY);
      }
    }
  }

  // 6. Materialize React Flow edges with per-state styling.
  const rfEdges: OSRFEdge[] = pre.map((p) => {
    const stateMeta = EDGE_STATE_META[p.edge.state] ?? EDGE_STATE_META.live;
    const worst =
      (SEVERITY[p.src.node.status] ?? 1) >= (SEVERITY[p.tgt.node.status] ?? 1)
        ? p.src.node.status
        : p.tgt.node.status;
    const color = stateMeta.color ?? osMeta(worst).color;
    const onSelected =
      selectedId !== null &&
      (p.edge.source === selectedId || p.edge.target === selectedId);
    return {
      id: p.edge.id,
      type: "os",
      source: p.edge.source,
      target: p.edge.target,
      sourceHandle: p.sourceHandle,
      targetHandle: p.targetHandle,
      animated: onSelected,
      data: {
        centerX: p.centerX,
        labelX: p.label?.x,
        labelY: p.label?.y,
        labelText: p.edge.label,
        labelColor:
          p.edge.state !== "live" && stateMeta.color
            ? stateMeta.color
            : undefined,
      },
      style: {
        stroke: color,
        strokeWidth: onSelected ? 2.2 : 1.4,
        strokeDasharray: stateMeta.dash,
        opacity: onSelected ? 0.95 : p.edge.state === "disabled" ? 0.45 : 0.62,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color,
        width: 12,
        height: 12,
      },
    };
  });

  return { nodes: rfNodes, edges: rfEdges };
}

// ---------------------------------------------------------------------------
// Inspect panel — node identity + backing section items + matching diagnostics
// ---------------------------------------------------------------------------

/** Diagnostics for a node: item-level matches first, else section-level. */
function diagnosticsFor(
  node: OSGraphNode,
  all: OSDiagnostic[],
): OSDiagnostic[] {
  const nodeRefs = [node.id, node.label].map((v) => v.toLowerCase());
  const sectionRef = node.section_ref?.toLowerCase();
  const nodeHits: OSDiagnostic[] = [];
  const sectionHits: OSDiagnostic[] = [];
  for (const diag of all) {
    const src = diag.source.toLowerCase();
    if (nodeRefs.some((ref) => src.includes(ref) || ref.includes(src))) {
      nodeHits.push(diag);
    } else if (
      sectionRef &&
      (src.includes(sectionRef) || sectionRef.includes(src))
    ) {
      sectionHits.push(diag);
    }
  }
  return nodeHits.length > 0 ? nodeHits : sectionHits;
}

interface InspectPanelProps {
  node: OSGraphNode;
  section: OSSection | null;
  diagnostics: OSDiagnostic[];
  onClose: () => void;
}

function InspectPanel({ node, section, diagnostics, onClose }: InspectPanelProps) {
  const meta = osMeta(node.status);

  return (
    <aside
      className="absolute inset-y-0 right-0 z-20 flex w-[min(340px,90%)] flex-col border-l border-white/10 bg-[#0a0f18]/[0.97] text-white backdrop-blur-sm"
      aria-label={`Inspect ${node.label}`}
    >
      <div className="flex items-start gap-2.5 border-b border-white/10 px-4 py-3">
        <span
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg"
          style={{ background: meta.soft, color: meta.color }}
        >
          {kindIconElement(node.kind, { size: 16, strokeWidth: 2 })}
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold">{node.label}</h3>
          <p className="mt-0.5 text-[10px] uppercase tracking-[0.14em] text-white/40">
            {node.kind.replace(/-/g, " ")} · {node.group}
          </p>
        </div>
        <span
          className="flex-shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold"
          style={{ color: meta.color, borderColor: meta.ring, background: meta.soft }}
        >
          {OS_STATUS_LABEL[node.status] ?? node.status}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close inspector"
          className="flex-shrink-0 rounded-md p-1 text-white/50 transition hover:bg-white/10 hover:text-white"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {node.detail && (
          <p className="text-xs leading-relaxed text-white/70">{node.detail}</p>
        )}

        {diagnostics.length > 0 && (
          <div className="mt-3">
            <h4 className="text-[10px] font-semibold uppercase tracking-[0.16em] text-white/40">
              Diagnostics
            </h4>
            <ul className="mt-1.5 space-y-1.5">
              {diagnostics.map((diag, i) => {
                const dm = osMeta(diag.severity);
                return (
                  <li
                    key={`${diag.source}-${i}`}
                    className="rounded-md border px-2.5 py-2"
                    style={{ borderColor: dm.ring, background: dm.soft }}
                  >
                    <span
                      className="font-mono text-[10px] font-semibold"
                      style={{ color: dm.color }}
                    >
                      {diag.source}
                    </span>
                    <p className="mt-0.5 text-xs leading-relaxed text-white/80">
                      {diag.message}
                    </p>
                    {diag.hint && (
                      <p className="mt-0.5 text-xs leading-relaxed text-white/50">
                        {diag.hint}
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {section ? (
          <div className="mt-3">
            <h4 className="text-[10px] font-semibold uppercase tracking-[0.16em] text-white/40">
              Section · {section.label}
            </h4>
            <ul className="mt-1.5 divide-y divide-white/[0.06] rounded-md border border-white/10">
              {section.items.length === 0 && (
                <li className="px-2.5 py-2 text-xs text-white/45">
                  No probes reported.
                </li>
              )}
              {section.items.map((item) => {
                const im = osMeta(item.status);
                return (
                  <li key={item.name} className="px-2.5 py-2">
                    <div className="flex items-baseline gap-2">
                      <span
                        className="h-1.5 w-1.5 flex-shrink-0 self-center rounded-full"
                        style={{
                          background: im.color,
                          boxShadow:
                            item.status === "green"
                              ? undefined
                              : `0 0 6px ${im.color}`,
                        }}
                      />
                      <span className="min-w-0 flex-1 truncate text-xs font-semibold text-white/85">
                        {item.name}
                      </span>
                      {item.metric && (
                        <span
                          className="flex-shrink-0 font-mono text-[10px]"
                          style={{
                            color:
                              item.status === "green"
                                ? "rgba(255,255,255,0.55)"
                                : im.color,
                          }}
                        >
                          {item.metric}
                        </span>
                      )}
                    </div>
                    {item.detail && (
                      <p className="mt-0.5 break-words pl-3.5 text-[11px] leading-relaxed text-white/55">
                        {item.detail}
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        ) : (
          <p className="mt-3 text-xs text-white/45">
            No section detail bound to this node.
          </p>
        )}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Legend — edge-state chip row
// ---------------------------------------------------------------------------

const LEGEND_ENTRIES: { label: string; color: string; dash?: string }[] = [
  { label: "Live", color: "#4ade80" },
  { label: "Disabled", color: "#7c91a8", dash: "5 4" },
  { label: "Gated · human", color: "#ffbd38", dash: "5 4" },
  { label: "Broken", color: "#fb2c36", dash: "4 3" },
];

function EdgeLegend() {
  return (
    <div
      className="pointer-events-none absolute left-3 top-3 z-10 flex flex-wrap gap-1.5"
      aria-label="Edge state legend"
    >
      {LEGEND_ENTRIES.map((entry) => (
        <span
          key={entry.label}
          className="flex items-center gap-1.5 rounded-full border border-white/10 bg-[#0a0f16]/85 px-2 py-1 text-[10px] font-medium text-white/65"
        >
          <svg width="20" height="6" aria-hidden="true">
            <line
              x1="0"
              y1="3"
              x2="20"
              y2="3"
              stroke={entry.color}
              strokeWidth="1.6"
              strokeDasharray={entry.dash}
            />
          </svg>
          {entry.label}
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// FitView — fits the viewport once nodes are measured (timing-race safe)
// ---------------------------------------------------------------------------

function FitView() {
  const { fitView } = useReactFlow();
  const initialized = useNodesInitialized();
  const fitted = useRef(false);
  useEffect(() => {
    if (fitted.current) return;
    const fit = () => {
      if (fitted.current) return;
      fitted.current = true;
      void fitView({ padding: 0.14, duration: 240 });
    };
    if (initialized) requestAnimationFrame(fit);
    const fallback = setTimeout(fit, 500);
    return () => clearTimeout(fallback);
  }, [initialized, fitView]);
  return null;
}

// ---------------------------------------------------------------------------
// OSNexus
// ---------------------------------------------------------------------------

interface OSNexusProps {
  snapshot: OSSnapshot;
}

/** Interactive, pan/zoom architecture-flow view of the OS snapshot graph. */
export function OSNexus({ snapshot }: OSNexusProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const graph = snapshot.graph as OSSnapshot["graph"] | undefined;
  const hasGraph = Boolean(graph && graph.nodes.length > 0);

  const { nodes, edges } = useMemo(
    () =>
      hasGraph && graph
        ? buildFlow(graph, selectedId)
        : { nodes: [] as CanvasNode[], edges: [] as OSRFEdge[] },
    [hasGraph, graph, selectedId],
  );

  const selected = useMemo(
    () => graph?.nodes.find((n) => n.id === selectedId) ?? null,
    [graph, selectedId],
  );
  const selectedSection = useMemo(
    () =>
      selected?.section_ref
        ? snapshot.sections.find((s) => s.id === selected.section_ref) ?? null
        : null,
    [selected, snapshot.sections],
  );
  const selectedDiagnostics = useMemo(
    () => (selected ? diagnosticsFor(selected, snapshot.diagnostics) : []),
    [selected, snapshot.diagnostics],
  );

  if (!hasGraph) {
    return (
      <div className="flex h-full min-h-[300px] items-center justify-center rounded-lg border border-border bg-card px-6 text-center text-sm text-text-tertiary">
        Topology graph not present in the snapshot yet — waiting on the backend
        /api/dashboard/os graph payload.
      </div>
    );
  }

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border border-border bg-[#070a0f]">
      <ReactFlow
        className="sh-flow"
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodeClick={(_, node) => {
          if (node.type === "os") setSelectedId(node.id);
        }}
        onPaneClick={() => setSelectedId(null)}
        minZoom={0.15}
        maxZoom={1.9}
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        proOptions={{ hideAttribution: false }}
        colorMode="dark"
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={24}
          size={1}
          color="rgba(120,150,180,0.16)"
        />
        <Controls showInteractive={false} position="bottom-left" />
        <FitView />
      </ReactFlow>

      <EdgeLegend />

      {selected && (
        <InspectPanel
          node={selected}
          section={selectedSection}
          diagnostics={selectedDiagnostics}
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  );
}
