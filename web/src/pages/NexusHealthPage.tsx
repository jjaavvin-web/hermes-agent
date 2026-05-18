import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Copy, ExternalLink, Lock, RefreshCw, ShieldCheck } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useNavigate } from "react-router-dom";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api, type NexusHealthNode, type NexusHealthResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  dashboard: { x: 50, y: 12 },
  hermes: { x: 50, y: 32 },
  gateway: { x: 18, y: 30 },
  kanban: { x: 82, y: 30 },
  "cron-watchdogs": { x: 18, y: 58 },
  "gitnexus-explorer": { x: 50, y: 58 },
  "mcp-memory": { x: 82, y: 58 },
  "agent-lanes": { x: 50, y: 78 },
  "audit-store": { x: 24, y: 86 },
  "source-tree": { x: 76, y: 86 },
};

const statusClasses: Record<NexusHealthNode["status"], string> = {
  ok: "border-emerald-400/70 bg-emerald-500/10 text-emerald-100",
  warn: "border-amber-300/70 bg-amber-500/10 text-amber-100",
  error: "border-rose-400/80 bg-rose-500/15 text-rose-100",
  unknown: "border-slate-300/45 bg-slate-400/10 text-slate-100",
  auth_gated: "border-sky-300/70 bg-sky-500/10 text-sky-100",
};

function shortStatus(status: string): string {
  return status.replace("_", " ");
}

function Graph({ data }: { data: NexusHealthResponse }) {
  const nodeMap = useMemo(
    () => new Map(data.nodes.map((node) => [node.id, node])),
    [data.nodes],
  );

  return (
    <section className="relative min-h-[560px] overflow-hidden border border-white/10 bg-[#06080d]">
      <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
        <defs>
          <filter id="nexus-glow">
            <feGaussianBlur stdDeviation="0.5" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        {data.edges.map((edge) => {
          const from = NODE_POSITIONS[edge.source] ?? NODE_POSITIONS.dashboard;
          const to = NODE_POSITIONS[edge.target] ?? NODE_POSITIONS.hermes;
          const color =
            edge.status === "error"
              ? "#fb7185"
              : edge.status === "warn"
                ? "#fbbf24"
                : edge.status === "ok"
                  ? "#34d399"
                  : "#94a3b8";
          return (
            <g key={edge.id} filter="url(#nexus-glow)">
              <line
                x1={from.x}
                y1={from.y}
                x2={to.x}
                y2={to.y}
                stroke={color}
                strokeOpacity="0.55"
                strokeWidth="0.35"
              />
              <text
                x={(from.x + to.x) / 2}
                y={(from.y + to.y) / 2 - 1}
                fill={color}
                fontSize="1.8"
                textAnchor="middle"
                className="normal-case"
              >
                {edge.label.slice(0, 28)}
              </text>
            </g>
          );
        })}
      </svg>
      {data.nodes.map((node) => {
        const pos = NODE_POSITIONS[node.id] ?? { x: 50, y: 50 };
        return (
          <article
            key={node.id}
            className={cn(
              "absolute w-40 -translate-x-1/2 -translate-y-1/2 border p-2 shadow-[0_0_24px_rgba(255,255,255,0.05)]",
              statusClasses[node.status],
            )}
            style={{ left: `${pos.x}%`, top: `${pos.y}%` }}
          >
            <div className="flex items-center justify-between gap-2">
              <h3 className="truncate text-[11px] font-semibold tracking-normal">{node.label}</h3>
              <span className="shrink-0 text-[9px]">{shortStatus(node.status)}</span>
            </div>
            <p className="mt-1 line-clamp-2 text-[10px] normal-case leading-snug text-white/75">
              {node.summary}
            </p>
          </article>
        );
      })}
      <div className="absolute bottom-3 left-3 max-w-sm border border-white/10 bg-black/70 p-3 normal-case text-xs text-white/70">
        {nodeMap.get("gitnexus-explorer")?.details}
      </div>
    </section>
  );
}

export default function NexusHealthPage() {
  const { setTitle } = usePageHeader();
  const navigate = useNavigate();
  const [data, setData] = useState<NexusHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getNexusHealth());
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Nexus Health unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setTitle("Nexus Health");
  }, [setTitle]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [load]);

  if (!data && !error) {
    return (
      <div className="flex h-full items-center justify-center bg-[#05070b] text-sm text-white/60">
        Loading...
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto bg-[#05070b] text-white">
      <div className="mx-auto flex max-w-7xl flex-col gap-4 p-4">
        <header className="flex flex-col gap-3 border-b border-white/10 pb-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="flex items-center gap-2 text-xs text-white/50">
              <ShieldCheck className="h-4 w-4" />
              <span>Nexus Health</span>
            </div>
            <h1 className="mt-2 text-2xl font-semibold tracking-normal">Command Health Map</h1>
            <p className="mt-2 max-w-3xl normal-case text-sm text-white/65">
              {data?.summary ?? error}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {data && (
              <span
                className={cn(
                  "border px-3 py-2 text-sm",
                  data.posture === "safe" && "border-emerald-400/70 text-emerald-100",
                  data.posture === "caution" && "border-amber-300/70 text-amber-100",
                  data.posture === "stop" && "border-rose-400/80 text-rose-100",
                )}
              >
                {data.posture}
              </span>
            )}
            <Button outlined size="sm" onClick={() => void load()} aria-label="Refresh">
              <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
            </Button>
          </div>
        </header>

        {error && (
          <div className="border border-rose-400/50 bg-rose-500/10 p-3 normal-case text-sm text-rose-100">
            {error}
          </div>
        )}

        {data && (
          <>
            <Graph data={data} />

            <div className="grid gap-4 lg:grid-cols-[1.3fr_0.7fr]">
              <section className="border border-white/10 bg-white/[0.03] p-4">
                <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold tracking-normal">
                  <AlertTriangle className="h-4 w-4" />
                  Needs Joseph
                </h2>
                <div className="grid gap-2">
                  {data.needs_joseph.length === 0 ? (
                    <p className="normal-case text-sm text-white/55">No human gate is active.</p>
                  ) : (
                    data.needs_joseph.map((item) => (
                      <div key={item.id} className="border border-amber-300/30 p-3 normal-case">
                        <div className="text-sm font-medium text-amber-100">{item.label}</div>
                        <div className="mt-1 text-xs text-white/60">{item.reason}</div>
                        <div className="mt-2 text-[11px] text-amber-100/80">{item.gate}</div>
                      </div>
                    ))
                  )}
                </div>
              </section>

              <section className="border border-white/10 bg-white/[0.03] p-4">
                <h2 className="mb-3 text-sm font-semibold tracking-normal">Safe Actions</h2>
                <div className="grid gap-2">
                  {data.safe_actions.map((action) => (
                    <button
                      key={action.id}
                      className="flex items-center justify-between border border-white/10 px-3 py-2 text-left text-xs text-white/75 hover:border-white/30"
                      onClick={() => {
                        if (action.kind === "copy") {
                          void navigator.clipboard?.writeText(action.payload);
                        } else {
                          navigate(action.payload);
                        }
                      }}
                    >
                      <span>{action.label}</span>
                      {action.kind === "copy" ? <Copy className="h-3.5 w-3.5" /> : <ExternalLink className="h-3.5 w-3.5" />}
                    </button>
                  ))}
                </div>
              </section>
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              <section className="border border-white/10 bg-white/[0.03] p-4 lg:col-span-2">
                <h2 className="mb-3 text-sm font-semibold tracking-normal">Node Evidence</h2>
                <div className="grid gap-2 md:grid-cols-2">
                  {data.nodes.map((node) => (
                    <div key={node.id} className="border border-white/10 p-3 normal-case">
                      <div className="flex items-center justify-between gap-2 text-sm">
                        <span className="font-medium">{node.label}</span>
                        <span className="text-xs text-white/45">{node.kind}</span>
                      </div>
                      <p className="mt-2 text-xs text-white/60">{node.details}</p>
                      <p className="mt-2 text-xs text-cyan-100/75">{node.safe_next_check}</p>
                    </div>
                  ))}
                </div>
              </section>

              <section className="border border-white/10 bg-white/[0.03] p-4">
                <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold tracking-normal">
                  <Lock className="h-4 w-4" />
                  Locked Packets
                </h2>
                <div className="grid gap-2">
                  {data.locked_actions.map((item) => (
                    <div key={item.id} className="border border-white/10 p-3 normal-case">
                      <div className="text-sm text-white/80">{item.label}</div>
                      <div className="mt-1 text-[11px] text-white/45">{item.gate}</div>
                      <div className="mt-2 text-xs text-white/60">{item.reason}</div>
                    </div>
                  ))}
                </div>
              </section>
            </div>

            <section className="border border-white/10 bg-white/[0.03] p-4">
              <h2 className="mb-3 text-sm font-semibold tracking-normal">Provenance</h2>
              <div className="grid gap-2 md:grid-cols-3">
                {data.evidence.map((item) => (
                  <div key={`${item.source}-${item.detail}`} className="border border-white/10 p-3 normal-case">
                    <div className="text-xs font-medium text-white/80">{item.source}</div>
                    <div className="mt-1 text-xs text-white/55">{item.detail}</div>
                  </div>
                ))}
              </div>
              <div className="mt-3 text-xs normal-case text-white/35">
                Generated {new Date(data.generated_at).toLocaleString()}
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
