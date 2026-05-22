import { useMemo } from "react";
import { RotateCcw } from "lucide-react";
import type { NexusHealthResponse, NexusHealthStatus } from "@/lib/api";
import { GROUPS, STATUS_META, STATUS_ORDER, kindIcon } from "./constants";

interface FilterRailProps {
  data: NexusHealthResponse;
  statusFilter: Set<NexusHealthStatus>;
  groupFilter: Set<string>;
  visibleCount: number;
  onToggleStatus: (status: NexusHealthStatus) => void;
  onToggleGroup: (group: string) => void;
  onReset: () => void;
}

const SECTION =
  "text-[9.5px] font-semibold uppercase tracking-[0.16em] text-white/35 mb-2";

/** Left rail: status legend + status/category filters for the graph. */
export function FilterRail({
  data,
  statusFilter,
  groupFilter,
  visibleCount,
  onToggleStatus,
  onToggleGroup,
  onReset,
}: FilterRailProps) {
  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const node of data.nodes) {
      counts[node.status] = (counts[node.status] ?? 0) + 1;
    }
    return counts;
  }, [data.nodes]);

  const groupCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const node of data.nodes) {
      counts[node.group] = (counts[node.group] ?? 0) + 1;
    }
    return counts;
  }, [data.nodes]);

  const dirty =
    statusFilter.size !== STATUS_ORDER.length ||
    groupFilter.size !== GROUPS.length;

  return (
    <div className="flex h-full flex-col overflow-y-auto bg-white/[0.015] px-3 py-4">
      <div className="mb-4 flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-white/70">
          System Map
        </span>
        {dirty && (
          <button
            type="button"
            onClick={onReset}
            className="flex items-center gap-1 rounded-md border border-white/10 px-1.5 py-1 text-[10px] text-white/55 transition hover:border-white/25 hover:text-white/85"
          >
            <RotateCcw className="h-3 w-3" />
            Reset
          </button>
        )}
      </div>

      <section className="mb-5">
        <div className={SECTION}>Status</div>
        <div className="flex flex-col gap-1">
          {STATUS_ORDER.map((status) => {
            const meta = STATUS_META[status];
            const count = statusCounts[status] ?? 0;
            const active = statusFilter.has(status);
            return (
              <button
                key={status}
                type="button"
                onClick={() => onToggleStatus(status)}
                className="group flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-left transition hover:bg-white/[0.05]"
                style={{ opacity: active ? 1 : 0.4 }}
              >
                <span
                  className="h-2.5 w-2.5 flex-shrink-0 rounded-full"
                  style={{
                    background: active ? meta.color : "transparent",
                    boxShadow: active ? `0 0 8px ${meta.color}` : "none",
                    border: `1.5px solid ${meta.color}`,
                  }}
                />
                <span className="flex-1 text-[12px] text-white/80">
                  {meta.label}
                </span>
                <span
                  className="text-[11px] font-semibold tabular-nums"
                  style={{ color: count > 0 ? meta.color : "rgba(255,255,255,0.3)" }}
                >
                  {count}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="mb-5">
        <div className={SECTION}>Categories</div>
        <div className="flex flex-col gap-1">
          {GROUPS.map((group) => {
            const count = groupCounts[group.id] ?? 0;
            const active = groupFilter.has(group.id);
            return (
              <button
                key={group.id}
                type="button"
                onClick={() => onToggleGroup(group.id)}
                className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-left transition hover:bg-white/[0.05]"
                style={{ opacity: active ? 1 : 0.4 }}
              >
                <span
                  className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-md border text-[10px]"
                  style={{
                    borderColor: active
                      ? "rgba(120,180,255,0.5)"
                      : "rgba(255,255,255,0.15)",
                    background: active ? "rgba(120,180,255,0.12)" : "transparent",
                    color: active ? "#7cb8ff" : "rgba(255,255,255,0.4)",
                  }}
                >
                  {active ? "✓" : ""}
                </span>
                <span className="flex-1 text-[12px] text-white/80">
                  {group.label}
                </span>
                <span className="text-[11px] font-semibold tabular-nums text-white/45">
                  {count}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="mb-5">
        <div className={SECTION}>Legend</div>
        <div className="grid grid-cols-2 gap-1.5">
          {["runtime", "service", "container", "port", "mcp", "gateway"].map(
            (kind) => {
              const Icon = kindIcon(kind);
              return (
                <div
                  key={kind}
                  className="flex items-center gap-1.5 text-[10px] text-white/45"
                >
                  <Icon className="h-3 w-3" />
                  <span className="capitalize">{kind}</span>
                </div>
              );
            },
          )}
        </div>
      </section>

      <div className="mt-auto border-t border-white/10 pt-3 text-[10px] text-white/40">
        Showing{" "}
        <span className="font-semibold text-white/70">{visibleCount}</span> of{" "}
        {data.nodes.length} nodes · {data.edges.length} links
      </div>
    </div>
  );
}
