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
import { api, type ConnectomeEdge, type ConnectomeNode } from "@/lib/api";
import { kindIconElement, statusMeta } from "@/components/system-health/constants";

type SimNodeKind = "hub" | "leaf" | "halo";

interface SimNode extends ConnectomeNode {
  simKind: SimNodeKind;
  hub: string;
  metricValue: number;
  accent?: boolean;
  opacity?: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
}

interface SimLink extends Omit<ConnectomeEdge, "source" | "target"> {
  source: string | SimNode;
  target: string | SimNode;
}

interface FixtureNode {
  id: string;
  kind: string;
  fx?: number;
  fy?: number;
  x?: number;
  y?: number;
  opacity?: number;
}

interface ConnectomeFixture {
  meta: { width: number; height: number };
  nodes: FixtureNode[];
}

const ANCHOR_FIXTURE = fixture as ConnectomeFixture;
const ANCHORS = new Map(
  ANCHOR_FIXTURE.nodes
    .filter((node) => node.kind === "hub")
    .map((node) => [node.id, { x: Number(node.fx ?? node.x ?? 0), y: Number(node.fy ?? node.y ?? 0) }]),
);
const DESIGN_W = ANCHOR_FIXTURE.meta.width || 1440;
const DESIGN_H = ANCHOR_FIXTURE.meta.height || 900;

const BG = "#0a0b0d";
const WHITE = "#f4f7fb";
const SUMMARY_POLL_MS = 20_000;
const CLUSTER_LOAD_ZOOM = 1.35;
const ACTIVE_PARTICLE_CAP = 15;
const REAL_LEAF_MIN_OPACITY = 0.58;
const HALO_MAX_OPACITY = 0.6;
const HUB_LABELS: Record<string, string> = {
  projects: "Projects",
  brain: "Brain",
  code: "Code",
  infra: "Infra",
  learning: "Learning",
  lanes: "Lanes",
  config: "Config",
  programs: "Programs",
  deploy: "Deploy",
};

function useElementSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      const rect = el.getBoundingClientRect();
      const w = Math.max(1, Math.round(rect.width));
      const h = Math.max(1, Math.round(rect.height));
      // Only emit a NEW size object when dimensions actually change — otherwise a
      // fresh {w,h} on every ResizeObserver tick rebuilds the graph memo and
      // perpetually restarts the force simulation (canvas never settles).
      setSize((prev) => (prev.w === w && prev.h === h ? prev : { w, h }));
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

function endpointId(endpoint: string | SimNode): string {
  return typeof endpoint === "string" ? endpoint : endpoint.id;
}

function metricValue(node: ConnectomeNode): number {
  if (typeof node.count === "number") return node.count;
  const metric = node.metric;
  if (typeof metric === "number") return metric;
  if (metric && typeof metric === "object") {
    for (const key of ["signal", "clean_total", "node_total", "total", "active", "repos", "units"]) {
      const value = (metric as Record<string, unknown>)[key];
      if (typeof value === "number") return value;
    }
  }
  return 1;
}

function statusKey(status: string | undefined): string {
  if (!status) return "unknown";
  if (status === "ok" || status === "completed" || status === "complete" || status === "live" || status === "warm" || status === "nominal" || status === "active" || status === "serving" || status === "indexed") return "ok";
  if (status === "blocked" || status === "error" || status === "source unreachable") return "error";
  if (status === "queued" || status === "in-progress" || status === "running" || status === "scheduled" || status === "warn" || status === "degraded") return "warn";
  return "unknown";
}

function scaleAnchor(id: string, size: { w: number; h: number }): { x: number; y: number } {
  // Centered at the graph origin (0,0) — react-force-graph's default camera looks
  // at (0,0), so centered coords render centered without fighting zoomToFit.
  const anchor = ANCHORS.get(id) ?? { x: DESIGN_W / 2, y: DESIGN_H / 2 };
  const usableW = Math.max(1, size.w * 0.86);
  const usableH = Math.max(1, size.h * 0.86);
  return {
    x: (anchor.x / DESIGN_W - 0.5) * usableW,
    y: (anchor.y / DESIGN_H - 0.5) * usableH,
  };
}

function formatMetric(node: SimNode): string {
  const metric = node.metric;
  if (metric && typeof metric === "object") {
    const record = metric as Record<string, unknown>;
    if (typeof record.signal === "number" && typeof record.clean_total === "number") return `${record.signal}/${record.clean_total}`;
    if (typeof record.node_total === "number") return `${record.node_total}`;
    if (typeof record.total === "number") return `${record.total}`;
  }
  return String(node.count ?? node.metricValue ?? "—");
}

function provenancePredicate(node: ConnectomeNode | null): string {
  if (!node) return "—";
  const source = node.provSource || node.provenance?.source || "unknown source";
  const query = node.provQuery || node.provenance?.query || "unknown query";
  const field = node.provField || node.provenance?.field || "unknown field";
  const value = node.provValue || node.provenance?.value || "unknown value";
  return `${source} :: ${query} :: ${field} = ${value}`;
}

function nodeRadius(node: SimNode): number {
  if (node.simKind === "halo") return 0.9 + (node.opacity ?? 0.18) * 1.8;
  if (node.simKind === "hub") return 8 + Math.min(18, Math.log10(Math.max(10, node.metricValue)) * 3.4);
  return (node.accent ? 3.8 : 3.0) + Math.min(3.2, Math.log10(Math.max(1, node.metricValue)) * 0.7);
}

function isLiveNode(node: SimNode): boolean {
  return node.accent === true || ["running", "in-progress", "blocked", "source unreachable", "warm", "active", "serving"].includes(node.status ?? "");
}

function nodeColor(node: SimNode, selectedId: string | null, neighborIds: Set<string>): string {
  if (node.simKind === "halo") return `rgba(255,255,255,${Math.min(HALO_MAX_OPACITY, node.opacity ?? 0.16)})`;
  const selected = selectedId === node.id;
  const neighbor = neighborIds.has(node.id);
  const dimmed = selectedId !== null && !selected && !neighbor;
  const live = isLiveNode(node);
  const alpha = dimmed ? 0.16 : selected ? 1 : node.simKind === "hub" ? 0.94 : live ? 0.9 : REAL_LEAF_MIN_OPACITY;
  if (node.hub === "lanes" && (node.simKind === "hub" || live || selected || neighbor)) return `rgba(246,196,83,${alpha})`;
  if (selected || neighbor) return `rgba(255,255,255,${alpha})`;
  if (node.status === "blocked" || node.status === "source unreachable") return `rgba(255,255,255,${alpha})`;
  return `rgba(220,226,236,${alpha})`;
}

function linkColor(link: SimLink, selectedId: string | null, neighborEdgeIds: Set<string>): string {
  const source = endpointId(link.source);
  const target = endpointId(link.target);
  const selected = selectedId !== null && (source === selectedId || target === selectedId || neighborEdgeIds.has(link.id));
  const dimmed = selectedId !== null && !selected;
  if (dimmed) return "rgba(255,255,255,0.025)";
  if (link.kind === "bridge") {
    const touchesLanes = source === "lanes" || target === "lanes";
    return touchesLanes ? "rgba(246,196,83,0.38)" : selected ? "rgba(255,255,255,0.34)" : "rgba(255,255,255,0.15)";
  }
  return selected ? "rgba(255,255,255,0.18)" : "rgba(255,255,255,0.05)";
}

function drawHubLabel(
  ctx: CanvasRenderingContext2D,
  node: SimNode,
  radius: number,
  globalScale: number,
): void {
  if (node.simKind !== "hub" || typeof node.x !== "number" || typeof node.y !== "number") return;
  const fontSize = Math.max(8.5, 12 / globalScale);
  ctx.font = `${fontSize}px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillStyle = node.id === "lanes" ? "rgba(246,196,83,0.78)" : "rgba(231,238,251,0.42)";
  ctx.fillText((HUB_LABELS[node.id] ?? node.label).toUpperCase(), node.x, node.y + radius + 8 / globalScale);
  ctx.fillStyle = "rgba(244,247,251,0.72)";
  ctx.font = `${Math.max(8, 10.5 / globalScale)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.fillText(formatMetric(node), node.x, node.y + radius + 24 / globalScale);
}

function atmosphereNode(hub: SimNode, key: string, x: number, y: number, opacity: number): SimNode {
  // Pinned (fx/fy) so the static organism never churns the simulation.
  return {
    id: `halo:${hub.id}:${key}`,
    label: "atmosphere",
    cluster: hub.cluster,
    hub: hub.hub,
    kind: "halo",
    simKind: "halo",
    status: "atmosphere",
    real: false,
    metricValue: 0,
    opacity,
    provenance: { source: "visual atmosphere", query: "excluded from counts/focus", field: "real", value: "false" },
    provSource: "visual atmosphere",
    provQuery: "excluded from counts/focus",
    provField: "real",
    provValue: "false",
    lastSeen: "",
    x,
    y,
    fx: x,
    fy: y,
  };
}

function makeHaloNodes(hubs: SimNode[], size: { w: number; h: number }): SimNode[] {
  // Dense dandelion corona per hub, sized by the hub's REAL metric (so a big hub
  // reads as a dense neuron, a small hub as a sparse one) — the mockup's organism.
  return hubs.flatMap((hub) => {
    const value = Math.max(1, hub.metricValue);
    const count = Math.max(38, Math.min(220, Math.round(Math.sqrt(value) * 6.6 + 28)));
    const maxRing = 40 + Math.min(172, Math.sqrt(value) * 5.4);
    const cx = hub.fx ?? size.w / 2;
    const cy = hub.fy ?? size.h / 2;
    const rings = 8;
    return Array.from({ length: count }, (_, i): SimNode => {
      const ringIdx = 1 + (i % rings);
      const ring = maxRing * Math.pow(ringIdx / rings, 0.8) * (0.86 + ((i * 17) % 28) / 100);
      const angle = (i / count) * Math.PI * 2 * 3.2 + (hub.id.length % 7) * 0.42;
      const opacity = Math.min(HALO_MAX_OPACITY, (0.52 - (ringIdx / (rings + 2)) * 0.34) * (0.72 + ((i * 13) % 30) / 100));
      return atmosphereNode(hub, String(i), cx + Math.cos(angle) * ring, cy + Math.sin(angle) * ring, opacity);
    });
  });
}

function makeFieldNodes(hubs: SimNode[], size: { w: number; h: number }): SimNode[] {
  // A unifying low-opacity field that ties the hubs into one cohesive disc.
  if (size.w < 2 || size.h < 2 || hubs.length === 0) return [];
  const cx = 0;
  const cy = 0;
  const R = Math.min(size.w, size.h) * 0.44;
  const ghost = hubs[0];
  const N = 820;
  return Array.from({ length: N }, (_, i): SimNode => {
    const a = (i * 137.508) * (Math.PI / 180); // golden-angle spread
    const r = R * Math.pow(0.18 + ((i * 0.61803) % 1) * 0.82, 0.6);
    const opacity = Math.min(HALO_MAX_OPACITY * 0.66, 0.05 + ((i * 7) % 22) / 220);
    return atmosphereNode(ghost, `field${i}`, cx + Math.cos(a) * r, cy + Math.sin(a) * r, opacity);
  });
}

function toSimNode(node: ConnectomeNode, size: { w: number; h: number }, simKind: SimNodeKind): SimNode {
  const hub = node.cluster || node.id.split(":")[0] || node.id;
  const anchor = scaleAnchor(hub, size);
  const value = metricValue(node);
  const isHub = simKind === "hub";
  return {
    ...node,
    simKind,
    hub,
    metricValue: value,
    accent: hub === "lanes" || node.id === "lanes" || ["running", "in-progress", "blocked"].includes(node.status ?? ""),
    fx: isHub ? anchor.x : undefined,
    fy: isHub ? anchor.y : undefined,
    x: isHub ? anchor.x : anchor.x + (((node.id.length * 37) % 80) - 40),
    y: isHub ? anchor.y : anchor.y + (((node.id.length * 53) % 80) - 40),
  };
}

function DetailCard({ node }: { node: ConnectomeNode | null }) {
  if (!node) return null;
  const meta = statusMeta(statusKey(node.status));
  return (
    <div className="pointer-events-none absolute left-8 top-24 z-20 max-w-[31rem] rounded-2xl border border-white/10 bg-black/40 p-4 text-xs text-slate-100 shadow-2xl shadow-black/40 ring-1 ring-white/10 backdrop-blur-xl">
      <div className="mb-2 flex items-start gap-2">
        <span className="mt-0.5 inline-flex h-8 w-8 items-center justify-center rounded-lg" style={{ background: meta.soft, color: meta.color }}>
          {kindIconElement(node.kind || "service", { size: 16, strokeWidth: 2 })}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-white">{node.label}</div>
          <div className="font-mono uppercase tracking-[0.18em]" style={{ color: meta.color }}>{node.status ?? "unknown"}</div>
        </div>
      </div>
      <div className="rounded-xl border border-white/10 bg-white/[0.035] p-3 font-mono leading-relaxed text-slate-200/75">
        {provenancePredicate(node)}
      </div>
      {node.detail && <p className="mt-2 text-slate-300/70">{node.detail}</p>}
    </div>
  );
}

function Inspector({ node, onClose }: { node: ConnectomeNode | null; onClose: () => void }) {
  if (!node) return null;
  const meta = statusMeta(statusKey(node.status));
  const metricRows = node.metric && typeof node.metric === "object" ? Object.entries(node.metric as Record<string, unknown>).slice(0, 10) : [];
  return (
    <aside className="absolute bottom-5 right-5 top-20 z-20 flex w-[23rem] max-w-[calc(100%-2.5rem)] flex-col rounded-3xl border border-white/10 bg-[#090a0c]/85 p-4 text-xs text-slate-200 shadow-2xl shadow-black/50 ring-1 ring-white/10 backdrop-blur-xl">
      <button type="button" onClick={onClose} className="absolute right-4 top-3 text-slate-400 transition hover:text-white" aria-label="Close Connectome inspector">×</button>
      <div className="flex items-start gap-3 pr-7">
        <span className="inline-flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl" style={{ background: meta.soft, color: meta.color }}>
          {kindIconElement(node.kind || "service", { size: 18, strokeWidth: 2 })}
        </span>
        <div className="min-w-0">
          <div className="text-sm font-semibold text-white">{node.label}</div>
          <div className="mt-1 font-mono uppercase tracking-[0.18em]" style={{ color: meta.color }}>{node.status ?? "unknown"}</div>
        </div>
      </div>
      <div className="mt-4 rounded-2xl border border-white/10 bg-white/[0.035] p-3">
        <div className="mb-1 text-[10px] uppercase tracking-[0.2em] text-slate-400">Re-runnable predicate</div>
        <div className="break-words font-mono leading-relaxed text-slate-200/80">{provenancePredicate(node)}</div>
      </div>
      {metricRows.length > 0 && (
        <div className="mt-4 grid grid-cols-2 gap-2">
          {metricRows.map(([key, value]) => (
            <div key={key} className="rounded-xl border border-white/10 bg-white/[0.025] p-2">
              <div className="truncate text-[10px] uppercase tracking-[0.16em] text-slate-500">{key}</div>
              <div className="truncate font-mono text-slate-200">{String(value)}</div>
            </div>
          ))}
        </div>
      )}
      <div className="mt-auto pt-4 text-[11px] leading-relaxed text-slate-500">
        Click focus is active: non-neighborhood nodes are dimmed; real 1-hop edges stay bright. OSNexus remains a separate topology view.
      </div>
    </aside>
  );
}

function ConnectomeHud({
  nodeCount,
  edgeCount,
  status,
  generatedAt,
  loading,
  error,
}: {
  nodeCount: number;
  edgeCount: number;
  status?: string;
  generatedAt?: string;
  loading: boolean;
  error: string | null;
}) {
  return (
    <>
      <div className="pointer-events-none absolute left-8 top-7 text-slate-200">
        <div className="text-[15px] font-extrabold tracking-[0.08em] opacity-85">◈ HERMES · OS</div>
        <div className="mt-1 text-[11px] tracking-[0.22em] opacity-45">NEURAL MODE · LIVE</div>
      </div>
      <div className="pointer-events-none absolute right-8 top-8 text-right text-xs leading-5 text-slate-200/55">
        <span className={`mr-1 inline-block h-2 w-2 rounded-full ${error ? "bg-red-300" : "bg-emerald-300 shadow-[0_0_8px_rgba(74,214,160,0.75)]"}`} />
        9 systems · {nodeCount} real nodes
        <br />
        <span className="text-slate-200/35">{edgeCount} verified links · {loading ? "refreshing" : status ?? "ok"}</span>
      </div>
      <div className="pointer-events-none absolute bottom-7 left-8 text-[12.5px] tracking-[0.02em] text-slate-200/50">
        hover = provenance · click = 1-hop focus · zoom = lazy leaves · halo excluded
      </div>
      <div className="pointer-events-none absolute bottom-7 right-8 max-w-[18rem] text-right text-[11px] uppercase tracking-[0.16em] text-slate-200/30">
        {error ? `error ${error}` : generatedAt ? `updated ${new Date(generatedAt).toLocaleTimeString()}` : "live endpoint"}
      </div>
    </>
  );
}

export default function Connectome() {
  const { ref, size } = useElementSize<HTMLDivElement>();
  const reducedMotion = useReducedMotion();
  const fgRef = useRef<ForceGraphMethods<SimNode, SimLink> | undefined>(undefined);
  const [summary, setSummary] = useState<{ nodes: ConnectomeNode[]; edges: ConnectomeEdge[]; meta?: Record<string, unknown> } | null>(null);
  const [clusterNodes, setClusterNodes] = useState<Record<string, ConnectomeNode[]>>({});
  const [loadedClusters, setLoadedClusters] = useState<Set<string>>(() => new Set());
  const [hovered, setHovered] = useState<ConnectomeNode | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [settled, setSettled] = useState(false);

  const loadSummary = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getConnectome();
      setSummary(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "connectome unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSummary();
    const timer = window.setInterval(() => void loadSummary(), SUMMARY_POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadSummary]);

  const loadCluster = useCallback(async (clusterId: string) => {
    if (loadedClusters.has(clusterId)) return;
    try {
      const page = await api.getConnectomeCluster(clusterId, { limit: 80 });
      setClusterNodes((prev) => ({ ...prev, [clusterId]: page.leaves ?? [] }));
      setLoadedClusters((prev) => new Set(prev).add(clusterId));
    } catch (err) {
      setError(err instanceof Error ? err.message : `cluster ${clusterId} unavailable`);
    }
  }, [loadedClusters]);

  useEffect(() => {
    if (zoom < CLUSTER_LOAD_ZOOM) return;
    const hubs = summary?.nodes?.filter((node) => node.kind === "hub") ?? [];
    const ranked = [...hubs].sort((a, b) => metricValue(b) - metricValue(a)).slice(0, 3);
    for (const hub of ranked) void loadCluster(hub.cluster || hub.id);
  }, [loadCluster, summary?.nodes, zoom]);

  useEffect(() => {
    if (!selectedId) return;
    const hubId = selectedId.split(":")[0];
    if (ANCHORS.has(hubId)) void loadCluster(hubId);
  }, [loadCluster, selectedId]);

  const graph = useMemo(() => {
    const hubNodes = (summary?.nodes ?? []).filter((node) => node.kind === "hub").map((node) => toSimNode(node, size, "hub"));
    const leaves = Object.values(clusterNodes).flat().map((node) => toSimNode(node, size, "leaf"));
    const halo = makeHaloNodes(hubNodes, size);
    const field = makeFieldNodes(hubNodes, size);
    const nodes = [...hubNodes, ...leaves, ...field, ...halo];
    const leafLinks: SimLink[] = leaves.map((node) => ({
      id: `membership:${node.hub}:${node.id}`,
      source: node.hub,
      target: node.id,
      kind: "membership",
      label: "cluster membership",
      mechanism: "lazy /connectome/cluster membership",
      verified: true,
      provenance: node.provenance,
      provSource: node.provSource,
      provQuery: node.provQuery,
      provField: node.provField,
      provValue: node.provValue,
      lastSeen: node.lastSeen,
    }));
    const links = [...(summary?.edges ?? []), ...leafLinks].map((edge) => ({ ...edge })) as SimLink[];
    return { nodes, links };
  }, [clusterNodes, size, summary?.edges, summary?.nodes]);

  const nodeById = useMemo(() => new Map(graph.nodes.filter((node) => node.simKind !== "halo").map((node) => [node.id, node])), [graph.nodes]);

  const { neighborIds, neighborEdgeIds } = useMemo(() => {
    const ids = new Set<string>();
    const edgeIds = new Set<string>();
    if (selectedId) {
      ids.add(selectedId);
      for (const link of graph.links) {
        const source = endpointId(link.source);
        const target = endpointId(link.target);
        if (source === selectedId || target === selectedId) {
          ids.add(source);
          ids.add(target);
          edgeIds.add(link.id);
        }
      }
    }
    return { neighborIds: ids, neighborEdgeIds: edgeIds };
  }, [graph.links, selectedId]);

  const selectedNode = selectedId ? nodeById.get(selectedId) ?? null : null;
  const realNodeCount = graph.nodes.filter((node) => node.simKind !== "halo").length;
  const realLinkCount = graph.links.length;

  const handleEngineStop = useCallback(() => {
    setSettled(true);
  }, []);

  // Fallback: center the camera on the real nodes once data has loaded, even if
  // the simulation never emits onEngineStop (pinned hubs can settle without it,
  // and an unstable engine would otherwise leave the camera off the nodes).
  useEffect(() => {
    if (size.w <= 0 || size.h <= 0 || realNodeCount === 0) return;
    const applyFrame = () => {
      const fg = fgRef.current;
      if (!fg) return;
      const xs: number[] = [];
      const ys: number[] = [];
      for (const n of graph.nodes) {
        if (typeof n.x === "number" && Number.isFinite(n.x)) xs.push(n.x);
        if (typeof n.y === "number" && Number.isFinite(n.y)) ys.push(n.y);
      }
      if (!xs.length || !ys.length) return;
      // Nodes are centered at the origin → center at (0,0) and zoom to the
      // symmetric extent so the organism fills the frame.
      const ext = Math.max(...xs.map(Math.abs), ...ys.map(Math.abs), 1);
      try {
        fg.centerAt(0, 0, 0);
        fg.zoom(Math.min(size.w, size.h) / (ext * 2 + 56), 0);
      } catch {
        // headless-safe
      }
    };
    // Re-assert the frame a few times to override react-force-graph's own
    // auto-zoom on first data load, then freeze the engine.
    const t1 = window.setTimeout(applyFrame, 450);
    const t2 = window.setTimeout(applyFrame, 1000);
    const t3 = window.setTimeout(() => {
      applyFrame();
      try {
        fgRef.current?.pauseAnimation();
      } catch {
        // headless-safe
      }
    }, 1700);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      window.clearTimeout(t3);
    };
  }, [graph, realNodeCount, size.w, size.h]);

  const nodeCanvasObject = useCallback(
    (node: SimNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      if (typeof node.x !== "number" || typeof node.y !== "number") return;
      const radius = nodeRadius(node);
      const selected = selectedId === node.id;
      const neighbor = neighborIds.has(node.id);
      const dimmed = selectedId !== null && !selected && !neighbor && node.simKind !== "halo";

      ctx.save();
      if (node.simKind !== "halo") {
        ctx.globalAlpha = dimmed ? 0.45 : 1;
        ctx.shadowBlur = selected ? 24 : node.simKind === "hub" ? 18 : isLiveNode(node) ? 12 : 0;
        ctx.shadowColor = node.hub === "lanes" ? "rgba(246,196,83,0.58)" : "rgba(235,241,255,0.24)";
      }
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = nodeColor(node, selectedId, neighborIds);
      ctx.fill();

      if (node.simKind === "hub") {
        ctx.shadowBlur = 0;
        ctx.lineWidth = node.id === "lanes" || selected ? 1.45 : 0.85;
        ctx.strokeStyle = node.id === "lanes" ? "rgba(246,196,83,0.82)" : "rgba(255,255,255,0.32)";
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(node.x, node.y, Math.max(2.5, radius * 0.32), 0, 2 * Math.PI);
        ctx.fillStyle = WHITE;
        ctx.fill();
      }

      if ((selected || neighbor || node.id === "lanes") && node.simKind !== "halo") {
        ctx.shadowBlur = 0;
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + (selected ? 9 : 6), 0, 2 * Math.PI);
        ctx.strokeStyle = node.hub === "lanes" ? "rgba(246,196,83,0.42)" : "rgba(255,255,255,0.24)";
        ctx.lineWidth = selected ? 1.25 : 0.8;
        ctx.stroke();
      }

      drawHubLabel(ctx, node, radius, globalScale);
      ctx.restore();
    },
    [neighborIds, selectedId],
  );

  const nodePointerAreaPaint = useCallback(
    (node: SimNode, paintColor: string, ctx: CanvasRenderingContext2D) => {
      if (node.simKind === "halo" || typeof node.x !== "number" || typeof node.y !== "number") return;
      ctx.fillStyle = paintColor;
      ctx.beginPath();
      ctx.arc(node.x, node.y, Math.max(10, nodeRadius(node) + 5), 0, 2 * Math.PI);
      ctx.fill();
    },
    [],
  );

  const handleNodeClick = useCallback((node: SimNode) => {
    if (node.simKind === "halo") return;
    setSelectedId((prev) => (prev === node.id ? null : node.id));
    if (node.simKind === "hub") void loadCluster(node.id);
  }, [loadCluster]);

  const particleBudget = useMemo(() => {
    const active = graph.links.filter((link) => {
      const source = endpointId(link.source);
      const target = endpointId(link.target);
      return link.kind === "bridge" || source === selectedId || target === selectedId || source === "lanes" || target === "lanes";
    }).slice(0, ACTIVE_PARTICLE_CAP);
    return new Set(active.map((link) => link.id));
  }, [graph.links, selectedId]);

  return (
    <div
      ref={ref}
      className="relative min-h-[620px] w-full overflow-hidden rounded-[32px] border border-white/10 bg-[#0a0b0d] shadow-[inset_0_0_120px_rgba(255,255,255,0.035)]"
      aria-label="Hermes OS neural connectome live view"
    >
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_51%,rgba(20,23,30,0.92),rgba(8,9,11,1)_72%)]" />
      {size.w > 0 && size.h > 0 && (
        <ForceGraph2D<SimNode, SimLink>
          ref={fgRef}
          width={size.w}
          height={size.h}
          graphData={graph}
          backgroundColor={BG}
          nodeId="id"
          nodeVal={nodeRadius}
          nodeRelSize={1}
          nodeLabel={(node) => (node.simKind === "halo" ? "" : `${node.label} · ${node.status ?? "unknown"} · ${provenancePredicate(node)}`)}
          nodeVisibility={() => true}
          nodeCanvasObjectMode={() => "replace"}
          nodeCanvasObject={nodeCanvasObject}
          nodePointerAreaPaint={nodePointerAreaPaint}
          linkColor={(link) => linkColor(link, selectedId, neighborEdgeIds)}
          linkWidth={(link) => (link.kind === "bridge" ? 0.75 : 0.22)}
          linkDirectionalParticles={(link) => (!reducedMotion && particleBudget.has(link.id) ? 1 : 0)}
          linkDirectionalParticleWidth={(link) => (particleBudget.has(link.id) ? 1.15 : 0)}
          linkDirectionalParticleSpeed={0.0025}
          d3AlphaDecay={reducedMotion ? 1 : 0.09}
          d3VelocityDecay={0.62}
          warmupTicks={reducedMotion ? 0 : 25}
          cooldownTicks={reducedMotion ? 0 : 90}
          cooldownTime={reducedMotion ? 0 : 3200}
          autoPauseRedraw={true}
          enableNodeDrag={false}
          enablePointerInteraction={true}
          onEngineStop={handleEngineStop}
          onZoom={(transform) => setZoom(transform.k)}
          onNodeHover={(node) => setHovered(node && node.simKind !== "halo" ? node : null)}
          onNodeClick={handleNodeClick}
          onBackgroundClick={() => setSelectedId(null)}
        />
      )}
      <div className="pointer-events-none absolute inset-0 ring-1 ring-inset ring-white/[0.03]" />
      <ConnectomeHud
        nodeCount={realNodeCount}
        edgeCount={realLinkCount}
        status={typeof summary?.meta?.status === "string" ? summary.meta.status : undefined}
        generatedAt={typeof summary?.meta?.generated_at === "string" ? summary.meta.generated_at : undefined}
        loading={loading}
        error={error}
      />
      <DetailCard node={hovered} />
      <Inspector node={selectedNode} onClose={() => setSelectedId(null)} />
      <div className="pointer-events-none absolute left-[calc(45%_-_14px)] top-[calc(80%_-_14px)] h-7 w-7 rounded-full border border-[#f6c453] bg-[#f6c453]/10 shadow-[0_0_18px_rgba(246,196,83,0.55)]" />
      <div className="pointer-events-none absolute bottom-12 right-8 text-[11px] uppercase tracking-[0.2em] text-slate-200/30">
        {settled || reducedMotion ? "cooled" : "settling"} · zoom {zoom.toFixed(2)}
      </div>
      {!summary && !error && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-slate-300/60">Loading live connectome…</div>
      )}
      <ul hidden aria-hidden="true">
        {graph.nodes.filter((node) => node.simKind !== "halo").map((node) => (
          <li key={node.id}>{node.label}</li>
        ))}
      </ul>
    </div>
  );
}
