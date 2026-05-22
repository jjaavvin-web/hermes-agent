import { memo, useEffect, useMemo, useRef } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  useNodesInitialized,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./system-health.css";
import type { NexusHealthResponse } from "@/lib/api";
import { COL_W, GROUP_ORDER, ROW_H, kindIcon, statusMeta } from "./constants";

interface HealthNodeData extends Record<string, unknown> {
  label: string;
  kind: string;
  status: string;
  summary: string;
  dim: boolean;
}

type HealthRFNode = Node<HealthNodeData, "health">;

const HANDLE_STYLE: React.CSSProperties = {
  width: 5,
  height: 5,
  background: "rgba(120,150,180,0.55)",
};

/** A single infrastructure node rendered on the graph canvas. */
const HealthFlowNode = memo(function HealthFlowNode({
  data,
  selected,
}: NodeProps<HealthRFNode>) {
  const meta = statusMeta(data.status);
  const Icon = kindIcon(data.kind);
  const unhealthy = data.status === "error" || data.status === "warn";

  return (
    <div
      title={data.summary}
      style={{
        width: 232,
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
        opacity: data.dim ? 0.3 : 1,
        transition: "opacity .2s ease, box-shadow .2s ease, border-color .2s ease",
        cursor: "pointer",
      }}
    >
      <Handle type="target" position={Position.Left} id="l" style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Left} id="lt" style={HANDLE_STYLE} />
      <Handle type="target" position={Position.Right} id="rt" style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Right} id="r" style={HANDLE_STYLE} />

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
        <Icon size={17} strokeWidth={2} />
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
        className={data.status === "error" ? "sh-pulse" : undefined}
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
      {unhealthy && (
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

const NODE_TYPES = { health: HealthFlowNode };

/** Lay nodes into columns by group and translate edges into React Flow edges. */
export function buildFlow(
  data: NexusHealthResponse,
  selectedId: string | null,
  isVisible: (nodeId: string) => boolean,
): { nodes: HealthRFNode[]; edges: Edge[] } {
  const groups = new Map<string, NexusHealthResponse["nodes"]>();
  for (const node of data.nodes) {
    const arr = groups.get(node.group);
    if (arr) arr.push(node);
    else groups.set(node.group, [node]);
  }
  const columns = [
    ...GROUP_ORDER,
    ...[...groups.keys()].filter((g) => !GROUP_ORDER.includes(g)),
  ];
  const maxRows = Math.max(
    1,
    ...columns.map((g) => groups.get(g)?.length ?? 0),
  );

  const colOf = new Map<string, number>();
  const posOf = new Map<string, { x: number; y: number }>();
  columns.forEach((groupId, colIndex) => {
    const arr = groups.get(groupId) ?? [];
    const offset = (maxRows - arr.length) / 2;
    arr.forEach((node, rowIndex) => {
      colOf.set(node.id, colIndex);
      posOf.set(node.id, {
        x: colIndex * COL_W,
        y: (offset + rowIndex) * ROW_H,
      });
    });
  });

  const neighbors = new Set<string>();
  if (selectedId) {
    neighbors.add(selectedId);
    for (const edge of data.edges) {
      if (edge.source === selectedId) neighbors.add(edge.target);
      if (edge.target === selectedId) neighbors.add(edge.source);
    }
  }

  const nodes: HealthRFNode[] = data.nodes.map((node) => ({
    id: node.id,
    type: "health",
    position: posOf.get(node.id) ?? { x: 0, y: 0 },
    width: 232,
    height: 56,
    selected: node.id === selectedId,
    draggable: false,
    hidden: !isVisible(node.id),
    data: {
      label: node.label,
      kind: node.kind,
      status: node.status,
      summary: node.summary,
      dim: selectedId !== null && !neighbors.has(node.id),
    },
  }));

  const edges: Edge[] = data.edges.map((edge) => {
    const sourceCol = colOf.get(edge.source) ?? 0;
    const targetCol = colOf.get(edge.target) ?? 0;
    const forward = sourceCol <= targetCol;
    const onSelected =
      selectedId !== null &&
      (edge.source === selectedId || edge.target === selectedId);
    const dimmed = selectedId !== null && !onSelected;
    const meta = statusMeta(edge.status);
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: forward ? "r" : "lt",
      targetHandle: forward ? "l" : "rt",
      type: "smoothstep",
      animated: onSelected,
      hidden: !(isVisible(edge.source) && isVisible(edge.target)),
      style: {
        stroke: meta.color,
        strokeWidth: onSelected ? 2.1 : 1.2,
        opacity: dimmed ? 0.1 : onSelected ? 0.95 : 0.42,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: meta.color,
        width: 13,
        height: 13,
      },
    };
  });

  return { nodes, edges };
}

/** Fits the viewport to the graph once the custom nodes have been measured. */
function FitView() {
  const initialized = useNodesInitialized();
  const { fitView } = useReactFlow();
  const fitted = useRef(false);
  useEffect(() => {
    if (initialized && !fitted.current) {
      fitted.current = true;
      void fitView({ padding: 0.16, duration: 260 });
    }
  }, [initialized, fitView]);
  return null;
}

interface HealthGraphProps {
  data: NexusHealthResponse;
  selectedId: string | null;
  visibleIds: Set<string>;
  onSelect: (id: string | null) => void;
}

/** Interactive, pan/zoom topology graph of the Hermes infrastructure. */
export function HealthGraph({
  data,
  selectedId,
  visibleIds,
  onSelect,
}: HealthGraphProps) {
  const { nodes, edges } = useMemo(
    () => buildFlow(data, selectedId, (id) => visibleIds.has(id)),
    [data, selectedId, visibleIds],
  );

  return (
    <ReactFlow
      className="sh-flow"
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onNodeClick={(_, node) => onSelect(node.id)}
      onPaneClick={() => onSelect(null)}
      fitView
      fitViewOptions={{ padding: 0.16 }}
      minZoom={0.2}
      maxZoom={1.9}
      nodesDraggable={false}
      nodesConnectable={false}
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
  );
}
