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

export type PulseNodeGroup =
  | "hive-active"
  | "hive-idle"
  | "hive-blocked"
  | "card-ready"
  | "card-running"
  | "card-blocked"
  | "card-triage";

export type PulseEdgeKind = "tracking" | "blocked_by" | "handoff";

export interface PulseGraphNode {
  id: string;
  label: string;
  group: PulseNodeGroup;
  status: string;
  kind: "hive" | "card";
  last_activity?: string | null;
  model?: string;
  workers?: number;
  priority?: number;
  age_seconds?: number;
  board?: string;
  assignee?: string;
  [k: string]: unknown;
}

export interface PulseGraphEdge {
  id: string;
  source: string;
  target: string;
  kind: PulseEdgeKind;
}

export interface PulseGraphResponse {
  nodes: PulseGraphNode[];
  edges: PulseGraphEdge[];
  degraded_mode?: string[];
}

// ForceGraph mutates nodes in place adding these — we type them as optional
// so our PulseGraphNode satisfies ForceGraph2D's expectations.
interface SimNode extends PulseGraphNode {
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
}

interface SimLink {
  id: string;
  source: string;
  target: string;
  kind: PulseEdgeKind;
}

// Fallback hex values — used when CSS vars cannot be resolved (e.g. test env).
// Keep in sync with web/src/theme/pulse.css.
const FALLBACK_COLORS: Record<PulseNodeGroup, string> = {
  "hive-active": "#b794f4",
  "hive-idle": "#76e4f7",
  "hive-blocked": "#fc8181",
  "card-ready": "#f6e05e",
  "card-running": "#f687b3",
  "card-blocked": "#fc8181",
  "card-triage": "#4a5568",
};

const FALLBACK_EDGE_COLORS = {
  cyan: "#76e4f7",
  red: "#fc8181",
  gray: "#4a5568",
};

function resolveColors(root: HTMLElement | null): Record<PulseNodeGroup, string> {
  if (!root || typeof window === "undefined") return FALLBACK_COLORS;
  const style = window.getComputedStyle(root);
  const pick = (name: string, fallback: string) =>
    (style.getPropertyValue(name).trim() || fallback);
  return {
    "hive-active": pick("--pulse-purple", FALLBACK_COLORS["hive-active"]),
    "hive-idle": pick("--pulse-cyan", FALLBACK_COLORS["hive-idle"]),
    "hive-blocked": pick("--pulse-red", FALLBACK_COLORS["hive-blocked"]),
    "card-ready": pick("--pulse-yellow", FALLBACK_COLORS["card-ready"]),
    "card-running": pick("--pulse-pink", FALLBACK_COLORS["card-running"]),
    "card-blocked": pick("--pulse-red", FALLBACK_COLORS["card-blocked"]),
    "card-triage": pick("--pulse-gray", FALLBACK_COLORS["card-triage"]),
  };
}

function resolveEdgeColors(root: HTMLElement | null) {
  if (!root || typeof window === "undefined") return FALLBACK_EDGE_COLORS;
  const style = window.getComputedStyle(root);
  return {
    cyan: style.getPropertyValue("--pulse-cyan").trim() || FALLBACK_EDGE_COLORS.cyan,
    red:  style.getPropertyValue("--pulse-red").trim()  || FALLBACK_EDGE_COLORS.red,
    gray: style.getPropertyValue("--pulse-gray").trim() || FALLBACK_EDGE_COLORS.gray,
  };
}

// rgba helper — accept a hex or fall through.
function hexWithAlpha(hex: string, alpha: number): string {
  const m = hex.match(/^#?([0-9a-f]{6})$/i);
  if (!m) return hex;
  const num = parseInt(m[1], 16);
  const r = (num >> 16) & 0xff;
  const g = (num >> 8) & 0xff;
  const b = num & 0xff;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function lastActivityMs(node: PulseGraphNode): number | null {
  if (!node.last_activity) return null;
  const t = new Date(node.last_activity).getTime();
  return Number.isFinite(t) ? t : null;
}

function nodeRadius(node: PulseGraphNode, nowMs: number): number {
  // Hives are hubs; cards are leaves. The 47 Industries reference uses ~3x
  // size separation between hubs and leaves to make the hierarchy readable
  // at density, so we do the same — cards at 4 baseline, hives at 9
  // baseline. Recency bumps still apply on top.
  const base = node.kind === "hive" ? 9 : 4;
  const ts = lastActivityMs(node);
  if (ts === null) return base;
  const ageMs = nowMs - ts;
  if (ageMs <= 5 * 60_000) return base + 3;
  if (ageMs <= 60 * 60_000) return base + 2;
  return base;
}

function fmtRelative(iso: string | null | undefined, nowMs: number): string {
  if (!iso) return "—";
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return "—";
  const sec = Math.max(0, Math.round((nowMs - ts) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

interface DetailPanelProps {
  node: PulseGraphNode | null;
  nowMs: number;
  onClose: () => void;
}

function DetailPanel({ node, nowMs, onClose }: DetailPanelProps) {
  useEffect(() => {
    if (!node) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [node, onClose]);

  if (!node) return null;

  // Pretty-print all node fields except internals from force-graph.
  const SKIP = new Set([
    "x", "y", "vx", "vy", "fx", "fy", "index", "__indexColor",
  ]);
  const rows = Object.entries(node)
    .filter(([k]) => !SKIP.has(k))
    .map(([k, v]) => [k, v]);

  return (
    <div
      className="pulse-detail-panel"
      role="dialog"
      aria-label="Node details"
      data-testid="pulse-detail-panel"
    >
      <div className="pulse-detail-panel__header">
        <span className="pulse-detail-panel__title">{node.label}</span>
        <button
          type="button"
          className="pulse-detail-panel__close"
          onClick={onClose}
          aria-label="Close detail panel"
        >
          ×
        </button>
      </div>
      <div className="pulse-detail-panel__body">
        <div className="pulse-detail-panel__row">
          <span className="pulse-detail-panel__k">kind</span>
          <span className="pulse-detail-panel__v">{node.kind}</span>
        </div>
        <div className="pulse-detail-panel__row">
          <span className="pulse-detail-panel__k">status</span>
          <span className="pulse-detail-panel__v">{node.status}</span>
        </div>
        {node.last_activity && (
          <div className="pulse-detail-panel__row">
            <span className="pulse-detail-panel__k">last_activity</span>
            <span className="pulse-detail-panel__v">
              {fmtRelative(node.last_activity, nowMs)}
            </span>
          </div>
        )}
        <div className="pulse-detail-panel__divider" />
        {rows.map(([k, v]) => (
          <div key={String(k)} className="pulse-detail-panel__row">
            <span className="pulse-detail-panel__k">{String(k)}</span>
            <span className="pulse-detail-panel__v">
              {typeof v === "string" || typeof v === "number"
                ? String(v)
                : JSON.stringify(v)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

interface TooltipState {
  x: number;
  y: number;
  node: PulseGraphNode;
}

export default function PulseConstellation() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraphMethods<SimNode, SimLink> | undefined>(undefined);
  const nodeMapRef = useRef<Map<string, SimNode>>(new Map());

  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [graph, setGraph] = useState<{ nodes: SimNode[]; links: SimLink[] }>({
    nodes: [],
    links: [],
  });
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastSuccess, setLastSuccess] = useState<number | null>(null);
  const [degraded, setDegraded] = useState<string[]>([]);
  const [selected, setSelected] = useState<PulseGraphNode | null>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  // Resolve theme palette once on mount via getComputedStyle. The seed
  // matches FALLBACK_COLORS so the first paint is correct even before the
  // effect runs; the effect then re-reads in case the user's :root sheet
  // overrides our defaults.
  const [colors, setColors] = useState<Record<PulseNodeGroup, string>>(
    () => FALLBACK_COLORS,
  );
  const [edgeColors, setEdgeColors] = useState(FALLBACK_EDGE_COLORS);
  useEffect(() => {
    setColors(resolveColors(containerRef.current));
    setEdgeColors(resolveEdgeColors(containerRef.current));
  }, []);

  // Size container so ForceGraph2D fills it.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect();
      setSize({ w: Math.max(0, rect.width), h: Math.max(0, rect.height) });
    };
    measure();
    const ResizeObs =
      typeof ResizeObserver !== "undefined" ? ResizeObserver : null;
    if (ResizeObs) {
      const ro = new ResizeObs(() => measure());
      ro.observe(el);
      return () => ro.disconnect();
    }
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  // Diff-merge response into existing node map so x/y survive across polls.
  const applyResponse = useCallback((resp: PulseGraphResponse) => {
    const incoming = resp.nodes ?? [];
    const nextMap = new Map<string, SimNode>();
    const nextNodes: SimNode[] = [];
    for (const n of incoming) {
      const existing = nodeMapRef.current.get(n.id);
      if (existing) {
        // Preserve x/y/vx/vy; refresh data fields in place.
        Object.assign(existing, n);
        nextMap.set(n.id, existing);
        nextNodes.push(existing);
      } else {
        const fresh: SimNode = { ...n };
        nextMap.set(n.id, fresh);
        nextNodes.push(fresh);
      }
    }
    nodeMapRef.current = nextMap;

    // If the selected node disappeared, drop the panel.
    if (selected && !nextMap.has(selected.id)) {
      setSelected(null);
    } else if (selected) {
      // Keep selection pointing at the refreshed object.
      const refreshed = nextMap.get(selected.id);
      if (refreshed) setSelected(refreshed);
    }

    const nextLinks: SimLink[] = (resp.edges ?? []).map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      kind: e.kind,
    }));
    setGraph({ nodes: nextNodes, links: nextLinks });
    setDegraded(resp.degraded_mode ?? []);
  }, [selected]);

  const load = useCallback(async () => {
    try {
      const data = await fetchJSON<PulseGraphResponse>("/api/pulse/graph");
      applyResponse(data);
      setError(null);
      setLastSuccess(Date.now());
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Graph unreachable");
      setLoaded(true);
    }
  }, [applyResponse]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    const timer = setInterval(() => void load(), GRAPH_POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  // Tick clock so "Xm ago" stays fresh.
  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  // Node-styling closures keyed by current palette.
  const nodeColorAccessor = useCallback(
    (node: SimNode) => colors[node.group] ?? FALLBACK_COLORS["card-triage"],
    [colors],
  );
  const nodeValAccessor = useCallback(
    (node: SimNode) => nodeRadius(node, nowMs),
    [nowMs],
  );

  const nodeCanvasObject = useCallback(
    (node: SimNode, ctx: CanvasRenderingContext2D) => {
      if (typeof node.x !== "number" || typeof node.y !== "number") return;
      const color = colors[node.group] ?? FALLBACK_COLORS["card-triage"];
      const r = nodeRadius(node, nowMs);

      // Glow halo via shadowBlur — cheap bloom effect.
      ctx.save();
      // Three-pass bloom — wide soft halo, mid-blur lobe, then a sharper
      // colored core. Hives get a bigger second halo so they read as hubs
      // even at density (per visual review against the 47 Industries demo).
      const halo = node.kind === "hive" ? 28 : 16;
      ctx.shadowBlur = halo;
      ctx.shadowColor = color;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.shadowBlur = halo * 0.5;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
      ctx.fill();
      ctx.shadowBlur = 6;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
      ctx.fill();
      // Bright inner dot to keep the centre legible above the bloom.
      ctx.shadowBlur = 0;
      ctx.beginPath();
      ctx.arc(node.x, node.y, Math.max(1.5, r - 2.5), 0, 2 * Math.PI);
      ctx.fillStyle = "rgba(255,255,255,0.92)";
      ctx.fill();
      ctx.restore();

      // Label below.
      const label = (node.label ?? "").slice(0, 24);
      if (label) {
        ctx.fillStyle = "rgba(229,229,229,0.85)";
        ctx.font = '10px ui-monospace, "JetBrains Mono", monospace';
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(label, node.x, node.y + r + 4);
      }
    },
    [colors, nowMs],
  );

  const linkColorAccessor = useCallback(
    (link: SimLink) => {
      switch (link.kind) {
        case "tracking":
          return hexWithAlpha(edgeColors.cyan, 0.4);
        case "blocked_by":
          return hexWithAlpha(edgeColors.red, 0.5);
        case "handoff":
          return hexWithAlpha(edgeColors.gray, 0.3);
        default:
          return hexWithAlpha(edgeColors.gray, 0.3);
      }
    },
    [edgeColors],
  );

  const linkLineDashAccessor = useCallback(
    (link: SimLink) => (link.kind === "blocked_by" ? [4, 4] : null),
    [],
  );

  const onNodeHover = useCallback((node: SimNode | null) => {
    if (!node) {
      setTooltip(null);
      return;
    }
    // Position via current pointer tracked on container — see mousemove below.
    setTooltip((prev) =>
      prev && prev.node.id === node.id
        ? prev
        : { x: prev?.x ?? 0, y: prev?.y ?? 0, node },
    );
  }, []);

  // Track pointer position over the container so tooltip can follow it.
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

  const onNodeClick = useCallback((node: SimNode) => {
    setSelected(node);
  }, []);

  const onBackgroundClick = useCallback(() => {
    setSelected(null);
  }, []);

  // Tune the d3-force charge strength once the ForceGraph instance is ready.
  // The default (~-30) is too gentle for our dataset, which is often hundreds
  // of disconnected nodes (no edges to spring them apart); a stronger
  // negative charge produces the dense-but-readable cloud the design calls
  // for instead of a collapsed central blob.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || graph.nodes.length === 0) return;
    type ForceTuner = { strength?: (v: number) => unknown; distance?: (v: number) => unknown };
    // Charge: chosen by eye against a 144-node real dataset. Too negative
    // (-180) and nodes fly off-screen between zoomToFit calls; too gentle
    // (-30 default) and the cloud collapses into a knot. -80 lands in the
    // dense-but-readable sweet spot the design calls for.
    const charge = fg.d3Force("charge") as unknown as ForceTuner | undefined;
    if (charge && typeof charge.strength === "function") {
      charge.strength(-120);
    }
    const link = fg.d3Force("link") as unknown as ForceTuner | undefined;
    if (link && typeof link.distance === "function") {
      link.distance(80);
    }
    // Reduce the centre force so dense clouds don't collapse into the middle —
    // we rely on zoomToFit to frame the simulation, not on a centre tug.
    type Strength = { strength?: (v: number) => unknown };
    const center = fg.d3Force("center") as unknown as Strength | undefined;
    if (center && typeof center.strength === "function") {
      center.strength(0.3);
    }
    fg.d3ReheatSimulation();
  }, [graph.nodes.length]);

  // Camera framing — call zoomToFit once the engine stops so the constellation
  // always lands in view, and again whenever the node count materially changes
  // (additions/removals would otherwise leave drifted nodes outside the frame).
  const handleEngineStop = useCallback(() => {
    const fg = fgRef.current;
    if (!fg) return;
    try {
      fg.zoomToFit(400, 40);
    } catch {
      // Method is missing in some build modes — non-fatal.
    }
  }, []);

  // Belt-and-suspenders camera framing — onEngineStop doesn't always fire
  // when alpha hovers above the stop threshold or when the canvas isn't on
  // an active rAF tick (e.g. tab-hidden). Re-frame on a wall-clock cadence
  // proportional to the cooldown so the cloud reliably lands in view.
  useEffect(() => {
    if (graph.nodes.length === 0) return;
    const id = window.setTimeout(() => {
      const fg = fgRef.current;
      if (!fg) return;
      try {
        fg.zoomToFit(600, 50);
      } catch {
        // ignore
      }
    }, 3000);
    return () => window.clearTimeout(id);
  }, [graph.nodes.length]);

  // ── Render states ─────────────────────────────────────────────────────
  const hasData = graph.nodes.length > 0;
  const isLoading = !loaded;
  const isEmpty = loaded && hasData === false && !error;
  const isError = loaded && !!error && !hasData;
  const lastSuccessIso = lastSuccess
    ? new Date(lastSuccess).toLocaleTimeString()
    : null;

  return (
    <div ref={containerRef} className="pulse-constellation">
      {degraded.includes("gitnexus_unreachable") && (
        <div className="pulse-constellation__banner">
          ⚠ GitNexus offline — showing hives + cards only
        </div>
      )}

      {isLoading && (
        <div className="pulse-constellation__overlay" aria-busy="true">
          <div className="pulse-constellation__pulse-dot" />
          <div className="pulse-constellation__overlay-text">
            Loading constellation…
          </div>
        </div>
      )}

      {isEmpty && (
        <div className="pulse-constellation__overlay">
          <div className="pulse-constellation__overlay-text pulse-constellation__overlay-text--dim">
            No active agents — <code>hermes kanban dispatch</code> to start one
          </div>
        </div>
      )}

      {isError && (
        <div className="pulse-constellation__overlay">
          <div className="pulse-constellation__overlay-text pulse-constellation__overlay-text--dim">
            {lastSuccessIso ? (
              <>
                Graph endpoint unreachable — last data shown
                <br />
                <span className="pulse-constellation__overlay-meta">
                  last update {lastSuccessIso}
                </span>
              </>
            ) : (
              <>Graph endpoint unreachable (retrying)</>
            )}
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
          nodeColor={nodeColorAccessor}
          nodeVal={nodeValAccessor}
          nodeRelSize={1}
          nodeLabel={() => ""}
          nodeCanvasObjectMode={() => "replace"}
          nodeCanvasObject={nodeCanvasObject}
          linkColor={linkColorAccessor}
          linkLineDash={linkLineDashAccessor}
          linkWidth={1}
          // Cool slowly so the layout has time to spread; stronger negative
          // charge keeps the cloud from collapsing to a horizontal band when
          // the graph has many disconnected nodes (the common case in our
          // dataset — kanban cards with no `blocked_by` links).
          d3AlphaDecay={0.02}
          d3VelocityDecay={0.3}
          warmupTicks={60}
          cooldownTicks={300}
          onEngineStop={handleEngineStop}
          onNodeHover={onNodeHover}
          onNodeClick={onNodeClick}
          onBackgroundClick={onBackgroundClick}
          enableNodeDrag={true}
        />
      )}

      {tooltip && (
        <div
          className="pulse-constellation__tooltip"
          style={{
            left: Math.min(tooltip.x + 14, Math.max(0, size.w - 240)),
            top: Math.min(tooltip.y + 14, Math.max(0, size.h - 80)),
          }}
        >
          <div className="pulse-constellation__tooltip-label">
            {tooltip.node.label}
          </div>
          <div className="pulse-constellation__tooltip-meta">
            {tooltip.node.kind} · {tooltip.node.status}
          </div>
          {tooltip.node.last_activity && (
            <div className="pulse-constellation__tooltip-meta">
              last activity {fmtRelative(tooltip.node.last_activity, nowMs)}
            </div>
          )}
        </div>
      )}

      <DetailPanel
        node={selected}
        nowMs={nowMs}
        onClose={() => setSelected(null)}
      />

      {/* Hidden node list — gives tests + a11y a way to enumerate nodes. */}
      <ul className="pulse-constellation__sr-list" aria-hidden="true" hidden>
        {graph.nodes.map((n) => (
          <li key={n.id} data-testid="pulse-node-marker" data-node-id={n.id}>
            {n.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
