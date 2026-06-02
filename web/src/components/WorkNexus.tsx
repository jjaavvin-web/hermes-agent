import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
} from "react-force-graph-2d";
import { fetchJSON } from "@/lib/api";

const GRAPH_POLL_MS = 15_000;

type WorkNexusNodeKind = "project" | "task" | "pr";
type WorkNexusEdgeKind = "contains" | "blocks" | "delivered_by";

type TaskStatus =
  | "triage"
  | "todo"
  | "scheduled"
  | "ready"
  | "running"
  | "blocked"
  | "review"
  | "done"
  | "archived"
  | string;

export interface WorkNexusNode {
  id: string;
  kind: WorkNexusNodeKind;
  label: string;
  color?: string;
  icon?: string;
  status?: TaskStatus;
  board?: string;
  priority?: number;
  completed?: boolean;
  ts?: number | string | null;
  state?: string | null;
  url?: string | null;
  merged_at?: string | null;
  [key: string]: unknown;
}

export interface WorkNexusEdge {
  id?: string;
  kind: WorkNexusEdgeKind;
  source: string;
  target: string;
}

export interface WorkNexusResponse {
  scanned_at?: string;
  nodes: WorkNexusNode[];
  edges: WorkNexusEdge[];
  degraded_mode?: string[];
}

interface SimNode extends WorkNexusNode {
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
  bornAt?: number;
}

interface SimLink {
  id: string;
  source: string | SimNode;
  target: string | SimNode;
  kind: WorkNexusEdgeKind;
}

interface TooltipState {
  x: number;
  y: number;
  node: WorkNexusNode;
}

const FALLBACK_COLORS = {
  project: "#e8fbff",
  completed: "#76e4f7",
  running: "#f6e05e",
  blocked: "#fc8181",
  review: "#68d391",
  aggregate: "#b794f4",
  dim: "#4a5568",
  pr: "#f687b3",
  white: "#f8fdff",
};

function resolvePalette(root: HTMLElement | null) {
  if (!root || typeof window === "undefined") return FALLBACK_COLORS;
  const style = window.getComputedStyle(root);
  const pick = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback;
  return {
    project: pick("--pulse-cyan", FALLBACK_COLORS.project),
    completed: pick("--pulse-cyan", FALLBACK_COLORS.completed),
    running: pick("--pulse-yellow", FALLBACK_COLORS.running),
    blocked: pick("--pulse-red", FALLBACK_COLORS.blocked),
    review: pick("--pulse-green", FALLBACK_COLORS.review),
    aggregate: pick("--pulse-purple", FALLBACK_COLORS.aggregate),
    dim: pick("--pulse-gray", FALLBACK_COLORS.dim),
    pr: pick("--pulse-pink", FALLBACK_COLORS.pr),
    white: FALLBACK_COLORS.white,
  };
}

function hexWithAlpha(hex: string, alpha: number): string {
  const m = hex.match(/^#?([0-9a-f]{6})$/i);
  if (!m) return hex;
  const num = parseInt(m[1], 16);
  const r = (num >> 16) & 0xff;
  const g = (num >> 8) & 0xff;
  const b = num & 0xff;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function easeOutCubic(t: number): number {
  return 1 - Math.pow(1 - t, 3);
}

function nodeColor(node: WorkNexusNode, palette: typeof FALLBACK_COLORS): string {
  if (node.kind === "project") return node.color || palette.project;
  if (node.kind === "pr") return palette.pr;
  if (node.aggregate === true) return palette.aggregate;
  if (node.completed || node.status === "done" || node.status === "archived") {
    return palette.completed;
  }
  switch (node.status) {
    case "running":
      return palette.running;
    case "blocked":
      return palette.blocked;
    case "review":
      return palette.review;
    default:
      return palette.dim;
  }
}

function nodeRadius(node: WorkNexusNode): number {
  if (node.kind === "project") return 16;
  if (node.kind === "pr") return 9;
  if (node.aggregate === true) return 9.5;
  if (node.status === "blocked") return 8.5;
  if (node.status === "running" || node.status === "review") return 8;
  if (node.completed || node.status === "done" || node.status === "archived") return 7;
  return 6;
}

function nodeImportance(node: WorkNexusNode): number {
  if (node.kind === "project") return 1;
  if (node.aggregate === true) return 0.82;
  if (node.kind === "pr") return 0.78;
  if (node.status === "running" || node.status === "review" || node.status === "blocked") return 0.72;
  return 0.48;
}

function stableHash(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
  }
  return hash;
}

function endpointId(endpoint: string | SimNode): string {
  return typeof endpoint === "string" ? endpoint : String(endpoint.id ?? "");
}

function endpointNode(endpoint: string | SimNode): SimNode | null {
  return typeof endpoint === "object" && endpoint !== null ? endpoint : null;
}

function isAggregateLink(edge: SimLink): boolean {
  return endpointId(edge.source).startsWith("aggregate:") || endpointId(edge.target).startsWith("aggregate:");
}

function fmtRelative(value: number | string | null | undefined, nowMs: number): string {
  if (value === null || value === undefined || value === "") return "—";
  let ts: number;
  if (typeof value === "number") {
    ts = value < 10_000_000_000 ? value * 1000 : value;
  } else {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      ts = numeric < 10_000_000_000 ? numeric * 1000 : numeric;
    } else {
      ts = new Date(value).getTime();
    }
  }
  if (!Number.isFinite(ts)) return "—";
  const sec = Math.max(0, Math.round((nowMs - ts) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

function DetailPanel({ node, onClose }: { node: WorkNexusNode | null; onClose: () => void }) {
  useEffect(() => {
    if (!node) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [node, onClose]);

  if (!node) return null;

  const fields = [
    ["kind", node.kind],
    ["status", node.status ?? node.state ?? "—"],
    ["board", node.board ?? "—"],
    ["priority", node.priority ?? "—"],
  ];

  return (
    <div className="pulse-detail-panel" role="dialog" aria-label="Work nexus node details" data-testid="work-nexus-detail-panel">
      <div className="pulse-detail-panel__header">
        <span className="pulse-detail-panel__title">{node.label}</span>
        <button type="button" className="pulse-detail-panel__close" onClick={onClose} aria-label="Close detail panel">
          ×
        </button>
      </div>
      <div className="pulse-detail-panel__body">
        {fields.map(([key, value]) => (
          <div key={key} className="pulse-detail-panel__row">
            <span className="pulse-detail-panel__k">{key}</span>
            <span className="pulse-detail-panel__v">{String(value)}</span>
          </div>
        ))}
        {node.url && (
          <div className="pulse-detail-panel__row">
            <span className="pulse-detail-panel__k">url</span>
            <span className="pulse-detail-panel__v">{node.url}</span>
          </div>
        )}
      </div>
    </div>
  );
}

export default function WorkNexus() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraphMethods<SimNode, SimLink> | undefined>(undefined);
  const nodeMapRef = useRef<Map<string, SimNode>>(new Map());
  const zoomedRef = useRef(false);
  const reducedMotionRef = useRef(prefersReducedMotion());

  const [size, setSize] = useState({ w: 0, h: 0 });
  const [graph, setGraph] = useState<{ nodes: SimNode[]; links: SimLink[] }>({ nodes: [], links: [] });
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState<string[]>([]);
  const [lastSuccess, setLastSuccess] = useState<number | null>(null);
  const [selected, setSelected] = useState<WorkNexusNode | null>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [palette, setPalette] = useState(FALLBACK_COLORS);

  useEffect(() => {
    setPalette(resolvePalette(containerRef.current));
  }, []);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect();
      setSize({ w: Math.max(0, rect.width), h: Math.max(0, rect.height) });
    };
    measure();
    const ResizeObs = typeof ResizeObserver !== "undefined" ? ResizeObserver : null;
    if (ResizeObs) {
      const ro = new ResizeObs(() => measure());
      ro.observe(el);
      return () => ro.disconnect();
    }
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  const applyResponse = useCallback((resp: WorkNexusResponse) => {
    const incoming = resp.nodes ?? [];
    const nextMap = new Map<string, SimNode>();
    const nextNodes: SimNode[] = [];
    const bornAt = performance.now();
    for (const n of incoming) {
      const existing = nodeMapRef.current.get(n.id);
      if (existing) {
        Object.assign(existing, n);
        nextMap.set(n.id, existing);
        nextNodes.push(existing);
      } else {
        const fresh: SimNode = { ...n, bornAt };
        nextMap.set(n.id, fresh);
        nextNodes.push(fresh);
      }
    }
    const projectNodes = nextNodes.filter((node) => node.kind === "project");
    const anchorRadius = Math.max(62, Math.min(138, projectNodes.length * 15));
    const projectByBoard = new Map<string, SimNode>();
    projectNodes.forEach((node, idx) => {
      const angle = (idx / Math.max(1, projectNodes.length)) * Math.PI * 2 - Math.PI / 2;
      node.fx = Math.cos(angle) * anchorRadius;
      node.fy = Math.sin(angle) * anchorRadius;
      node.x = node.x ?? node.fx;
      node.y = node.y ?? node.fy;
      if (typeof node.board === "string") projectByBoard.set(node.board, node);
    });
    nextNodes.forEach((node) => {
      if (node.kind === "project") return;
      const board = typeof node.board === "string" ? node.board : "";
      const project = projectByBoard.get(board);
      if (!project || typeof project.fx !== "number" || typeof project.fy !== "number") return;
      if (typeof node.x === "number" && typeof node.y === "number") return;
      const hash = stableHash(node.id);
      const angle = ((hash % 360) / 360) * Math.PI * 2;
      const orbit = node.aggregate === true ? 48 : 16 + ((hash >>> 8) % 26);
      node.x = project.fx + Math.cos(angle) * orbit;
      node.y = project.fy + Math.sin(angle) * orbit;
    });
    nodeMapRef.current = nextMap;
    setSelected((prev) => (prev && nextMap.has(prev.id) ? nextMap.get(prev.id) ?? prev : null));
    setGraph({
      nodes: nextNodes,
      links: (resp.edges ?? []).map((edge) => ({
        id: edge.id ?? `${edge.kind}:${edge.source}:${edge.target}`,
        source: edge.source,
        target: edge.target,
        kind: edge.kind,
      })),
    });
    setDegraded(resp.degraded_mode ?? []);
  }, []);

  const load = useCallback(async () => {
    try {
      const data = await fetchJSON<WorkNexusResponse>("/api/dashboard/work-nexus");
      applyResponse(data);
      setError(null);
      setLastSuccess(Date.now());
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Work nexus unreachable");
      setLoaded(true);
    }
  }, [applyResponse]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), GRAPH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onMove = (e: MouseEvent) => {
      const rect = el.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      setTooltip((prev) => (prev ? { ...prev, x, y } : prev));
    };
    el.addEventListener("mousemove", onMove);
    return () => el.removeEventListener("mousemove", onMove);
  }, []);

  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || graph.nodes.length === 0) return;
    type ForceTuner = {
      strength?: (v: number | ((link: SimLink) => number)) => unknown;
      distance?: (fn: (link: SimLink) => number) => unknown;
      iterations?: (v: number) => unknown;
    };
    const charge = fg.d3Force("charge") as unknown as ForceTuner | undefined;
    if (charge?.strength) charge.strength(-18);
    const link = fg.d3Force("link") as unknown as ForceTuner | undefined;
    if (link?.distance) {
      link.distance((edge: SimLink) => {
        if (edge.kind === "contains") return isAggregateLink(edge) ? 32 : 16;
        if (edge.kind === "blocks") return 24;
        return 22;
      });
    }
    if (link?.strength) {
      link.strength((edge: SimLink) => {
        if (edge.kind === "contains") return isAggregateLink(edge) ? 0.66 : 0.92;
        if (edge.kind === "blocks") return 0.82;
        return 0.58;
      });
    }
    if (link?.iterations) link.iterations(3);
    if (reducedMotionRef.current) {
      fg.pauseAnimation();
    } else {
      fg.resumeAnimation();
      fg.d3ReheatSimulation();
    }
  }, [graph.nodes.length, graph.links.length]);

  const handleEngineStop = useCallback(() => {
    fgRef.current?.pauseAnimation();
  }, []);

  useEffect(() => {
    if (zoomedRef.current || graph.nodes.length === 0) return;
    const id = window.setTimeout(() => {
      const fg = fgRef.current;
      if (!fg) return;
      try {
        fg.zoomToFit(600, 56);
        zoomedRef.current = true;
      } catch {
        // Optional in test/mocked renderers.
      }
    }, reducedMotionRef.current ? 50 : 1200);
    return () => window.clearTimeout(id);
  }, [graph.nodes.length]);

  const nodeCanvasObject = useCallback(
    (node: SimNode, ctx: CanvasRenderingContext2D) => {
      if (typeof node.x !== "number" || typeof node.y !== "number") return;
      const color = nodeColor(node, palette);
      const bornAt = typeof node.bornAt === "number" ? node.bornAt : performance.now();
      const age = performance.now() - bornAt;
      const grow = reducedMotionRef.current ? 1 : easeOutCubic(Math.max(0.15, Math.min(1, age / 1100)));
      const r = nodeRadius(node) * grow;
      const importance = nodeImportance(node);
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      const halo = node.kind === "project" ? 74 : node.aggregate === true ? 52 : node.kind === "pr" ? 44 : node.completed ? 34 : 40;
      ctx.shadowColor = color;
      ctx.shadowBlur = halo;
      ctx.globalAlpha = 0.36 + importance * 0.24;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r * 1.68, 0, 2 * Math.PI);
      ctx.fill();
      ctx.globalAlpha = 0.8;
      ctx.shadowBlur = halo * 0.75;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r * 1.18, 0, 2 * Math.PI);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.shadowBlur = halo * 0.42;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
      ctx.fill();
      ctx.shadowBlur = 8;
      ctx.beginPath();
      ctx.arc(node.x, node.y, Math.max(2.8, r * 0.44), 0, 2 * Math.PI);
      ctx.fillStyle = node.kind === "project" ? palette.white : hexWithAlpha(palette.white, 0.86);
      ctx.fill();
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      ctx.lineWidth = node.kind === "project" ? 1.4 : 0.9;
      ctx.strokeStyle = hexWithAlpha(palette.white, node.kind === "project" ? 0.9 : 0.58);
      ctx.beginPath();
      ctx.arc(node.x, node.y, r * 1.18, 0, 2 * Math.PI);
      ctx.stroke();
      const label = node.kind === "project" || node.aggregate === true
        ? `${node.kind === "project" && node.icon ? `${node.icon} ` : ""}${node.label}`
        : nodeImportance(node) >= 0.72
          ? node.label
          : "";
      if (label) {
        const projectLabel = node.kind === "project";
        ctx.shadowColor = projectLabel ? palette.project : color;
        ctx.shadowBlur = projectLabel ? 14 : 9;
        ctx.fillStyle = projectLabel ? "rgba(248,253,255,0.98)" : "rgba(238,246,255,0.86)";
        ctx.font = `${projectLabel ? 13 : node.aggregate === true ? 11.5 : 10.5}px ui-monospace, "JetBrains Mono", monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(label.slice(0, projectLabel ? 30 : 24), node.x, node.y + r + 7);
      }
      ctx.restore();
    },
    [palette],
  );

  const linkColor = useCallback(
    (edge: SimLink) => {
      if (edge.kind === "blocks") return hexWithAlpha(palette.blocked, 0.86);
      if (edge.kind === "delivered_by") return hexWithAlpha(palette.pr, 0.84);
      if (isAggregateLink(edge)) return hexWithAlpha(palette.aggregate, 0.72);
      return hexWithAlpha(palette.completed, 0.62);
    },
    [palette],
  );

  const linkWidth = useCallback((edge: SimLink) => {
    if (edge.kind === "blocks") return 2.2;
    if (edge.kind === "delivered_by") return 1.9;
    return isAggregateLink(edge) ? 1.65 : 1.28;
  }, []);
  const linkLineDash = useCallback((edge: SimLink) => (edge.kind === "blocks" ? [6, 4] : null), []);
  const linkCurvature = useCallback((edge: SimLink) => {
    if (edge.kind === "contains") return isAggregateLink(edge) ? 0.22 : 0.14;
    return edge.kind === "blocks" ? 0.24 : 0.18;
  }, []);

  const linkCanvasObject = useCallback(
    (edge: SimLink, ctx: CanvasRenderingContext2D) => {
      const source = endpointNode(edge.source);
      const target = endpointNode(edge.target);
      if (!source || !target) return;
      if (typeof source.x !== "number" || typeof source.y !== "number") return;
      if (typeof target.x !== "number" || typeof target.y !== "number") return;
      const color = linkColor(edge);
      const width = linkWidth(edge);
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      ctx.strokeStyle = color;
      ctx.lineCap = "round";
      ctx.shadowColor = color;
      ctx.shadowBlur = edge.kind === "contains" ? 18 : 24;
      if (edge.kind === "blocks") ctx.setLineDash([6, 4]);
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const curve = linkCurvature(edge);
      const cx = (source.x + target.x) / 2 - dy * curve;
      const cy = (source.y + target.y) / 2 + dx * curve;
      ctx.globalAlpha = edge.kind === "contains" ? 0.28 : 0.34;
      ctx.lineWidth = width * 4.2;
      ctx.beginPath();
      ctx.moveTo(source.x, source.y);
      ctx.quadraticCurveTo(cx, cy, target.x, target.y);
      ctx.stroke();
      ctx.globalAlpha = edge.kind === "contains" ? 0.86 : 0.95;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(source.x, source.y);
      ctx.quadraticCurveTo(cx, cy, target.x, target.y);
      ctx.stroke();
      ctx.restore();
    },
    [linkColor, linkCurvature, linkWidth],
  );

  const drawNexusBackdrop = useCallback(
    (ctx: CanvasRenderingContext2D, globalScale: number) => {
      const projectNodes = graph.nodes.filter(
        (node) => node.kind === "project" && typeof node.x === "number" && typeof node.y === "number",
      );
      if (projectNodes.length < 2) return;
      const scale = Math.max(globalScale || 1, 0.25);
      const hairline = 1 / scale;
      const sorted = [...projectNodes].sort((a, b) => Math.atan2(a.y ?? 0, a.x ?? 0) - Math.atan2(b.y ?? 0, b.x ?? 0));
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      ctx.lineCap = "round";
      ctx.shadowColor = hexWithAlpha(palette.completed, 0.78);
      ctx.shadowBlur = 16 * hairline;
      ctx.strokeStyle = hexWithAlpha(palette.completed, 0.28);
      ctx.lineWidth = 1.25 * hairline;
      sorted.forEach((node) => {
        if (typeof node.x !== "number" || typeof node.y !== "number") return;
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(node.x, node.y);
        ctx.stroke();
      });
      ctx.strokeStyle = hexWithAlpha(palette.pr, 0.3);
      ctx.lineWidth = 1.35 * hairline;
      sorted.forEach((node, idx) => {
        const next = sorted[(idx + 1) % sorted.length];
        if (typeof node.x !== "number" || typeof node.y !== "number") return;
        if (typeof next.x !== "number" || typeof next.y !== "number") return;
        const cx = ((node.x + next.x) / 2) * 1.24;
        const cy = ((node.y + next.y) / 2) * 1.24;
        ctx.beginPath();
        ctx.moveTo(node.x, node.y);
        ctx.quadraticCurveTo(cx, cy, next.x, next.y);
        ctx.stroke();
      });
      ctx.strokeStyle = hexWithAlpha(palette.aggregate, 0.26);
      ctx.lineWidth = 1.05 * hairline;
      sorted.forEach((node, idx) => {
        const next = sorted[(idx + 2) % sorted.length];
        if (!next || typeof node.x !== "number" || typeof node.y !== "number") return;
        if (typeof next.x !== "number" || typeof next.y !== "number") return;
        ctx.beginPath();
        ctx.moveTo(node.x * 0.58, node.y * 0.58);
        ctx.quadraticCurveTo(0, 0, next.x * 0.58, next.y * 0.58);
        ctx.stroke();
      });
      const yValues = sorted.map((node) => node.y ?? 0);
      ctx.shadowColor = palette.completed;
      ctx.shadowBlur = 22 * hairline;
      ctx.strokeStyle = hexWithAlpha(palette.completed, 0.42);
      ctx.lineWidth = 1.8 * hairline;
      ctx.beginPath();
      ctx.moveTo(0, Math.min(...yValues) - 34);
      ctx.lineTo(0, Math.max(...yValues) + 34);
      ctx.stroke();
      ctx.fillStyle = hexWithAlpha(palette.white, 0.76);
      ctx.shadowBlur = 18 * hairline;
      ctx.beginPath();
      ctx.arc(0, 0, 5.5 * hairline, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    },
    [graph.nodes, palette],
  );

  const onNodeHover = useCallback((node: SimNode | null) => {
    if (!node) {
      setTooltip(null);
      return;
    }
    setTooltip((prev) =>
      prev && prev.node.id === node.id ? prev : { x: prev?.x ?? 0, y: prev?.y ?? 0, node },
    );
  }, []);

  const onNodeClick = useCallback((node: SimNode) => {
    if (node.kind === "task" && typeof node.url === "string" && node.url) {
      window.open(node.url, "_blank", "noopener,noreferrer");
      return;
    }
    setSelected(node);
  }, []);

  const hasData = graph.nodes.length > 0;
  const isLoading = !loaded;
  const isEmpty = loaded && !hasData && !error;
  const isError = loaded && !!error && !hasData;
  const lastSuccessIso = lastSuccess ? new Date(lastSuccess).toLocaleTimeString() : null;

  return (
    <div ref={containerRef} className="work-nexus pulse-constellation">
      {degraded.length > 0 && (
        <div className="pulse-constellation__banner">
          ⚠ Overlay degraded: {degraded.join(", ")}
        </div>
      )}
      {isLoading && (
        <div className="pulse-constellation__overlay" aria-busy="true">
          <div className="pulse-constellation__pulse-dot" />
          <div className="pulse-constellation__overlay-text">Spinning up work nexus…</div>
        </div>
      )}
      {isEmpty && (
        <div className="pulse-constellation__overlay">
          <div className="pulse-constellation__overlay-text pulse-constellation__overlay-text--dim">
            No kanban work found — ship something and the web grows here.
          </div>
        </div>
      )}
      {isError && (
        <div className="pulse-constellation__overlay">
          <div className="pulse-constellation__overlay-text pulse-constellation__overlay-text--dim">
            {lastSuccessIso ? `Work nexus endpoint unreachable — last update ${lastSuccessIso}` : "Work nexus endpoint unreachable (retrying)"}
          </div>
        </div>
      )}
      {hasData && size.w > 0 && size.h > 0 && (
        <ForceGraph2D<SimNode, SimLink>
          ref={fgRef}
          width={size.w}
          height={size.h}
          backgroundColor="rgba(0,0,0,0)"
          graphData={graph}
          nodeId="id"
          nodeColor={(node) => nodeColor(node, palette)}
          nodeVal={nodeRadius}
          nodeRelSize={1}
          nodeLabel={() => ""}
          nodeCanvasObjectMode={() => "replace"}
          nodeCanvasObject={nodeCanvasObject}
          linkColor={linkColor}
          linkWidth={linkWidth}
          linkLineDash={linkLineDash}
          linkCurvature={linkCurvature}
          linkCanvasObjectMode={() => "replace"}
          linkCanvasObject={linkCanvasObject}
          linkDirectionalParticles={(edge) => (edge.kind === "delivered_by" ? 2 : edge.kind === "blocks" ? 1 : 0)}
          linkDirectionalParticleWidth={(edge) => (edge.kind === "delivered_by" ? 2.4 : 1.8)}
          linkDirectionalParticleColor={linkColor}
          onRenderFramePre={drawNexusBackdrop}
          d3AlphaDecay={reducedMotionRef.current ? 1 : 0.026}
          d3VelocityDecay={0.24}
          warmupTicks={reducedMotionRef.current ? 0 : 100}
          cooldownTicks={reducedMotionRef.current ? 0 : 260}
          onEngineStop={handleEngineStop}
          onNodeHover={onNodeHover}
          onNodeClick={onNodeClick}
          onBackgroundClick={() => setSelected(null)}
          enableNodeDrag={true}
        />
      )}
      {tooltip && (
        <div
          className="pulse-constellation__tooltip"
          style={{
            left: Math.min(tooltip.x + 14, Math.max(0, size.w - 250)),
            top: Math.min(tooltip.y + 14, Math.max(0, size.h - 96)),
          }}
        >
          <div className="pulse-constellation__tooltip-label">{tooltip.node.label}</div>
          <div className="pulse-constellation__tooltip-meta">
            {tooltip.node.kind} · {tooltip.node.status ?? tooltip.node.state ?? "anchor"}
          </div>
          {tooltip.node.ts && (
            <div className="pulse-constellation__tooltip-meta">
              activity {fmtRelative(tooltip.node.ts, nowMs)}
            </div>
          )}
        </div>
      )}
      <DetailPanel node={selected} onClose={() => setSelected(null)} />
      <ul className="pulse-constellation__sr-list" aria-hidden="true" hidden>
        {graph.nodes.map((n) => (
          <li key={n.id} data-testid="work-nexus-node-marker" data-node-id={n.id}>
            {n.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
