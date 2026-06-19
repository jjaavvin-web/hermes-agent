import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
} from "react-force-graph-2d";
import fixture from "./connectome.fixture.json";

type ConnectomeNodeKind = "hub" | "leaf" | "halo";
type ConnectomeLinkKind = "membership" | "bridge";

interface ConnectomeNode {
  id: string;
  label: string;
  kind: ConnectomeNodeKind;
  hub: string;
  count: number;
  metric: number;
  status?: string;
  accent?: boolean;
  opacity?: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
}

interface ConnectomeLink {
  id: string;
  source: string | ConnectomeNode;
  target: string | ConnectomeNode;
  kind: ConnectomeLinkKind;
  label: string;
}

interface ConnectomeFixture {
  meta: {
    width: number;
    height: number;
    source: string;
    note: string;
  };
  nodes: ConnectomeNode[];
  links: ConnectomeLink[];
}

const DATA = fixture as ConnectomeFixture;
const AMBER = "#f6c453";
const BG = "#0a0b0d";
const WHITE = "#f4f7fb";
const HUB_IDS = new Set(DATA.nodes.filter((node) => node.kind === "hub").map((node) => node.id));

function useElementSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      const rect = el.getBoundingClientRect();
      setSize({ w: Math.max(1, Math.round(rect.width)), h: Math.max(1, Math.round(rect.height)) });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return { ref, size };
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return reduced;
}

function endpointId(endpoint: string | ConnectomeNode): string {
  return typeof endpoint === "string" ? endpoint : endpoint.id;
}

function nodeRadius(node: ConnectomeNode): number {
  if (node.kind === "halo") return 0.7 + (node.opacity ?? 0.2) * 1.4;
  if (node.kind === "hub") return 7 + Math.min(15, Math.log10(Math.max(10, node.metric)) * 3.2);
  return node.accent ? 3.4 : 2.2 + Math.min(2.4, Math.log10(Math.max(1, node.metric)) * 0.6);
}

function nodeColor(node: ConnectomeNode): string {
  if (node.kind === "halo") return `rgba(255,255,255,${node.opacity ?? 0.18})`;
  if (node.accent) return AMBER;
  if (node.kind === "hub") return "rgba(248,251,255,0.94)";
  if (node.status === "queued") return "rgba(230,234,240,0.66)";
  return "rgba(214,220,228,0.54)";
}

function linkColor(link: ConnectomeLink): string {
  if (link.kind === "bridge") {
    const touchesLanes = endpointId(link.source) === "lanes" || endpointId(link.target) === "lanes";
    return touchesLanes ? "rgba(246,196,83,0.34)" : "rgba(255,255,255,0.16)";
  }
  return "rgba(255,255,255,0.045)";
}

function drawLabel(
  ctx: CanvasRenderingContext2D,
  node: ConnectomeNode,
  radius: number,
  globalScale: number,
): void {
  if (node.kind !== "hub" || typeof node.x !== "number" || typeof node.y !== "number") return;
  const fontSize = Math.max(8.5, 12 / globalScale);
  ctx.font = `${fontSize}px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.letterSpacing = "2px";
  ctx.fillStyle = node.id === "lanes" ? "rgba(246,196,83,0.74)" : "rgba(231,238,251,0.40)";
  ctx.fillText(node.label.toUpperCase(), node.x, node.y + radius + 8 / globalScale);
  ctx.fillStyle = "rgba(231,238,251,0.26)";
  ctx.font = `${Math.max(7.5, 10 / globalScale)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.fillText(String(node.metric), node.x, node.y + radius + 24 / globalScale);
}

function ConnectomeHud({ nodeCount, linkCount }: { nodeCount: number; linkCount: number }) {
  return (
    <>
      <div className="pointer-events-none absolute left-8 top-7 text-slate-200">
        <div className="text-[15px] font-extrabold tracking-[0.08em] opacity-85">◈ HERMES · OS</div>
        <div className="mt-1 text-[11px] tracking-[0.22em] opacity-45">NEURAL MODE · LIVE</div>
      </div>
      <div className="pointer-events-none absolute right-8 top-8 text-right text-xs leading-5 text-slate-200/55">
        <span className="mr-1 inline-block h-2 w-2 rounded-full bg-emerald-300 shadow-[0_0_8px_rgba(74,214,160,0.75)]" />
        9 systems · {nodeCount} real nodes
        <br />
        <span className="text-slate-200/35">{linkCount} links · cooled static fixture</span>
      </div>
      <div className="pointer-events-none absolute bottom-7 left-8 text-[12.5px] tracking-[0.02em] text-slate-200/50">
        <b className="text-slate-100/90">brain</b> dominant · <b className="text-slate-100/90">code</b> dense ·{" "}
        <b className="text-[#f6c453]">lanes</b> live accent · halo excluded from counts
      </div>
    </>
  );
}

export default function Connectome() {
  const { ref, size } = useElementSize<HTMLDivElement>();
  const reducedMotion = useReducedMotion();
  const fgRef = useRef<ForceGraphMethods<ConnectomeNode, ConnectomeLink> | undefined>(undefined);
  const [settled, setSettled] = useState(false);

  const graph = useMemo(() => {
    const nodes = DATA.nodes.map((node) => ({ ...node }));
    const links = DATA.links.map((link) => ({ ...link }));
    return { nodes, links };
  }, []);

  const realNodeCount = useMemo(
    () => graph.nodes.filter((node) => node.kind !== "halo").length,
    [graph.nodes],
  );

  const realLinkCount = useMemo(
    () => graph.links.filter((link) => link.kind !== "membership" || HUB_IDS.has(endpointId(link.source))).length,
    [graph.links],
  );

  const handleEngineStop = useCallback(() => {
    setSettled(true);
    const fg = fgRef.current;
    if (!fg) return;
    try {
      fg.zoomToFit(500, 42, (node) => node.kind !== "halo");
      fg.pauseAnimation();
    } catch {
      // Non-fatal in tests/headless modes.
    }
  }, []);

  const nodeCanvasObject = useCallback(
    (node: ConnectomeNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      if (typeof node.x !== "number" || typeof node.y !== "number") return;
      const radius = nodeRadius(node);

      ctx.save();
      if (node.kind !== "halo") {
        ctx.shadowBlur = node.kind === "hub" ? 22 : 9;
        ctx.shadowColor = node.accent ? "rgba(246,196,83,0.75)" : "rgba(235,241,255,0.30)";
      }
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = nodeColor(node);
      ctx.fill();

      if (node.kind === "hub") {
        ctx.shadowBlur = 0;
        ctx.lineWidth = node.accent ? 1.35 : 0.85;
        ctx.strokeStyle = node.accent ? "rgba(246,196,83,0.88)" : "rgba(255,255,255,0.34)";
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(node.x, node.y, Math.max(2.5, radius * 0.32), 0, 2 * Math.PI);
        ctx.fillStyle = WHITE;
        ctx.fill();
      }

      if (node.accent && node.kind !== "halo") {
        ctx.shadowBlur = 0;
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + 7, 0, 2 * Math.PI);
        ctx.strokeStyle = "rgba(246,196,83,0.38)";
        ctx.lineWidth = 1;
        ctx.stroke();
      }

      drawLabel(ctx, node, radius, globalScale);
      ctx.restore();
    },
    [],
  );

  const nodePointerAreaPaint = useCallback(
    (node: ConnectomeNode, paintColor: string, ctx: CanvasRenderingContext2D) => {
      if (node.kind === "halo" || typeof node.x !== "number" || typeof node.y !== "number") return;
      ctx.fillStyle = paintColor;
      ctx.beginPath();
      ctx.arc(node.x, node.y, Math.max(8, nodeRadius(node) + 4), 0, 2 * Math.PI);
      ctx.fill();
    },
    [],
  );

  return (
    <div
      ref={ref}
      className="relative min-h-[620px] w-full overflow-hidden rounded-[32px] border border-white/10 bg-[#0a0b0d] shadow-[inset_0_0_120px_rgba(255,255,255,0.035)]"
      aria-label="Hermes OS neural connectome static spike"
    >
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_51%,rgba(20,23,30,0.92),rgba(8,9,11,1)_72%)]" />
      {size.w > 0 && size.h > 0 && (
        <ForceGraph2D<ConnectomeNode, ConnectomeLink>
          ref={fgRef}
          width={size.w}
          height={size.h}
          graphData={graph}
          backgroundColor={BG}
          nodeId="id"
          nodeVal={nodeRadius}
          nodeRelSize={1}
          nodeLabel={(node) => (node.kind === "halo" ? "" : `${node.label} · ${node.metric}`)}
          nodeVisibility={() => true}
          nodeCanvasObjectMode={() => "replace"}
          nodeCanvasObject={nodeCanvasObject}
          nodePointerAreaPaint={nodePointerAreaPaint}
          linkColor={linkColor}
          linkWidth={(link) => (link.kind === "bridge" ? 0.7 : 0.18)}
          linkDirectionalParticles={(link) => (link.kind === "bridge" && !reducedMotion ? 1 : 0)}
          linkDirectionalParticleWidth={(link) => (link.kind === "bridge" ? 1.2 : 0)}
          linkDirectionalParticleSpeed={0.0025}
          d3AlphaDecay={reducedMotion ? 1 : 0.075}
          d3VelocityDecay={0.58}
          warmupTicks={reducedMotion ? 0 : 40}
          cooldownTicks={reducedMotion ? 0 : 120}
          cooldownTime={reducedMotion ? 0 : 4500}
          autoPauseRedraw={true}
          enableNodeDrag={false}
          enablePointerInteraction={true}
          onEngineStop={handleEngineStop}
        />
      )}
      <div className="pointer-events-none absolute inset-0 ring-1 ring-inset ring-white/[0.03]" />
      <ConnectomeHud nodeCount={realNodeCount} linkCount={realLinkCount} />
      <div className="pointer-events-none absolute left-[calc(45%_-_14px)] top-[calc(80%_-_14px)] h-7 w-7 rounded-full border border-[#f6c453] bg-[#f6c453]/10 shadow-[0_0_18px_rgba(246,196,83,0.55)]" />
      <div className="pointer-events-none absolute bottom-7 right-8 text-[11px] uppercase tracking-[0.2em] text-slate-200/30">
        {settled || reducedMotion ? "frozen" : "settling"}
      </div>
      <ul hidden aria-hidden="true">
        {graph.nodes.filter((node) => node.kind !== "halo").map((node) => (
          <li key={node.id}>{node.label}</li>
        ))}
      </ul>
    </div>
  );
}
