import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity,
  ChevronDown,
  ChevronUp,
  OctagonAlert,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useMissionStream } from "@/components/mission/useMissionStream";
import {
  api,
  type NexusHealthNodeDetail,
  type NexusHealthResponse,
  type NexusHealthStatus,
} from "@/lib/api";
import { GROUP_ORDER } from "@/components/system-health/constants";
import { STATUS_ORDER } from "@/components/system-health/constants";
import { FilterRail } from "@/components/system-health/FilterRail";
import { HealthGraph } from "@/components/system-health/HealthGraph";
import { DetailPanel } from "@/components/system-health/DetailPanel";

const POLL_MS = 30_000;
const MOBILE_BREAKPOINT = 768;

const POSTURE_META: Record<
  NexusHealthResponse["posture"],
  { label: string; color: string; icon: typeof ShieldCheck }
> = {
  safe: { label: "Safe", color: "#4ade80", icon: ShieldCheck },
  caution: { label: "Caution", color: "#ffbd38", icon: ShieldAlert },
  stop: { label: "Stop", color: "#fb2c36", icon: OctagonAlert },
};

const SSE_NODE_MAP: Record<string, string> = {
  hermes: "hermes",
  kanban: "kanban",
  cron: "cron-watchdogs",
};
const SSE_STATUS_MAP: Record<string, NexusHealthStatus> = {
  online: "ok",
  degraded: "warn",
  offline: "error",
  unknown: "unknown",
};

/** Returns true when the viewport is narrow (phone). Reacts to resize. */
function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.innerWidth <= MOBILE_BREAKPOINT,
  );
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT}px)`);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    setIsMobile(mq.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isMobile;
}

export default function NexusHealthPage() {
  const { setTitle } = usePageHeader();
  const navigate = useNavigate();
  const isMobile = useIsMobile();

  const [data, setData] = useState<NexusHealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<NexusHealthNodeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [statusFilter, setStatusFilter] = useState<Set<NexusHealthStatus>>(
    () => new Set(STATUS_ORDER),
  );
  const [groupFilter, setGroupFilter] = useState<Set<string>>(
    () => new Set(GROUP_ORDER),
  );
  const [lastLiveAt, setLastLiveAt] = useState(0);

  // Mobile collapsible panel state
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [detailSheetOpen, setDetailSheetOpen] = useState(false);

  const liveChips = useMissionStream();
  const detailReqId = useRef(0);

  useEffect(() => {
    setTitle("System Health");
  }, [setTitle]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await api.getNexusHealth();
      setData(next);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "System Health unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  // Live status deltas from the shared SSE stream (instant node recolour).
  useEffect(() => {
    if (liveChips.length === 0) return;
    setLastLiveAt(Date.now());
    setData((prev) => {
      if (!prev) return prev;
      let changed = false;
      const nodes = prev.nodes.map((node) => {
        const chip = liveChips.find((c) => SSE_NODE_MAP[c.name] === node.id);
        if (!chip) return node;
        const mapped = SSE_STATUS_MAP[chip.status] ?? "unknown";
        if (mapped === node.status) return node;
        changed = true;
        return { ...node, status: mapped };
      });
      return changed ? { ...prev, nodes } : prev;
    });
  }, [liveChips]);

  const selectNode = useCallback(async (id: string | null) => {
    setSelectedId(id);
    if (!id) {
      setDetail(null);
      setDetailError(null);
      setDetailSheetOpen(false);
      return;
    }
    // On mobile, open the detail sheet when a node is tapped.
    setDetailSheetOpen(true);
    const reqId = ++detailReqId.current;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const next = await api.getNexusHealthNode(id);
      if (reqId === detailReqId.current) setDetail(next);
    } catch (exc) {
      if (reqId === detailReqId.current) {
        setDetailError(
          exc instanceof Error ? exc.message : "Node detail unavailable",
        );
        setDetail(null);
      }
    } finally {
      if (reqId === detailReqId.current) setDetailLoading(false);
    }
  }, []);

  const toggleStatus = useCallback((status: NexusHealthStatus) => {
    setStatusFilter((prev) => {
      const next = new Set(prev);
      if (next.has(status)) next.delete(status);
      else next.add(status);
      return next.size === 0 ? new Set(STATUS_ORDER) : next;
    });
  }, []);

  const toggleGroup = useCallback((group: string) => {
    setGroupFilter((prev) => {
      const next = new Set(prev);
      if (next.has(group)) next.delete(group);
      else next.add(group);
      return next.size === 0 ? new Set(GROUP_ORDER) : next;
    });
  }, []);

  const resetFilters = useCallback(() => {
    setStatusFilter(new Set(STATUS_ORDER));
    setGroupFilter(new Set(GROUP_ORDER));
  }, []);

  const visibleIds = useMemo(() => {
    const ids = new Set<string>();
    if (!data) return ids;
    for (const node of data.nodes) {
      if (statusFilter.has(node.status) && groupFilter.has(node.group)) {
        ids.add(node.id);
      }
    }
    return ids;
  }, [data, statusFilter, groupFilter]);

  if (!data && !error) {
    return (
      <div className="flex h-full items-center justify-center bg-[#070b11] text-sm text-white/55">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
        Loading System Health…
      </div>
    );
  }

  if (!data && error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 bg-[#070b11] text-white">
        <div className="text-sm text-rose-200">{error}</div>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded-md border border-white/15 px-3 py-1.5 text-xs text-white/75 transition hover:border-white/35"
        >
          Retry
        </button>
      </div>
    );
  }

  const snapshot = data as NexusHealthResponse;
  const posture = POSTURE_META[snapshot.posture];
  const PostureIcon = posture.icon;
  const live = lastLiveAt > 0 && Date.now() - lastLiveAt < 40_000;

  const headerEl = (
    <header className="flex items-center gap-4 border-b border-white/10 px-4 py-3">
      <div className="flex-shrink-0">
        <div className="flex items-center gap-1.5 text-[9.5px] font-semibold uppercase tracking-[0.18em] text-white/40">
          <Activity className="h-3 w-3" />
          System Health
        </div>
        <h1 className="mt-0.5 text-[15px] font-semibold tracking-tight">
          Infrastructure Command Center
        </h1>
      </div>

      <p className="min-w-0 flex-1 truncate text-[12px] text-white/55">
        {snapshot.summary}
      </p>

      <div
        className="flex flex-shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold"
        style={{
          color: posture.color,
          background: `${posture.color}1a`,
          border: `1px solid ${posture.color}55`,
        }}
      >
        <PostureIcon className="h-3.5 w-3.5" />
        {posture.label}
      </div>

      <div className="flex flex-shrink-0 items-center gap-1.5 text-[10.5px] text-white/45">
        <span
          className={`h-2 w-2 rounded-full ${live ? "sh-pulse" : ""}`}
          style={{
            background: live ? "#4ade80" : "#64748b",
            boxShadow: live ? "0 0 8px #4ade80" : "none",
          }}
        />
        {live ? "Live" : "Idle"}
      </div>

      <button
        type="button"
        onClick={() => void load()}
        aria-label="Refresh"
        className="flex-shrink-0 rounded-md border border-white/12 p-1.5 text-white/65 transition hover:border-white/30 hover:text-white"
      >
        <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
      </button>
    </header>
  );

  const graphEl = (
    <div className="relative min-h-0 overflow-hidden bg-[#070a0f]">
      {visibleIds.size === 0 ? (
        <div className="flex h-full items-center justify-center text-sm text-white/45">
          No nodes match the current filters.
        </div>
      ) : (
        <HealthGraph
          data={snapshot}
          selectedId={selectedId}
          visibleIds={visibleIds}
          onSelect={(id) => void selectNode(id)}
        />
      )}
    </div>
  );

  // ------------------------------------------------------------------ Mobile
  if (isMobile) {
    return (
      <div className="flex h-full flex-col bg-[#070b11] text-white">
        {headerEl}

        {/* Collapsible filter bar */}
        <div className="border-b border-white/10">
          <button
            type="button"
            onClick={() => setFiltersOpen((v) => !v)}
            className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-[12px] text-white/70 transition hover:bg-white/[0.03]"
            aria-expanded={filtersOpen}
          >
            <SlidersHorizontal className="h-3.5 w-3.5 flex-shrink-0" />
            <span className="flex-1 font-semibold uppercase tracking-[0.12em]">
              System Map Filters
            </span>
            <span className="text-[10px] text-white/40">
              {visibleIds.size}/{snapshot.nodes.length} nodes
            </span>
            {filtersOpen ? (
              <ChevronUp className="h-3.5 w-3.5 flex-shrink-0" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5 flex-shrink-0" />
            )}
          </button>
          {filtersOpen && (
            <div className="max-h-[60vh] overflow-y-auto border-t border-white/10">
              <FilterRail
                data={snapshot}
                statusFilter={statusFilter}
                groupFilter={groupFilter}
                visibleCount={visibleIds.size}
                onToggleStatus={toggleStatus}
                onToggleGroup={toggleGroup}
                onReset={resetFilters}
              />
            </div>
          )}
        </div>

        {/* Full-width graph — primary view on mobile */}
        <div className="min-h-0 flex-1">
          {graphEl}
        </div>

        {/* Detail bottom sheet — slides up when a node is selected */}
        {detailSheetOpen && selectedId && (
          <>
            {/* Scrim */}
            <div
              className="fixed inset-0 z-40 bg-black/50"
              onClick={() => void selectNode(null)}
              aria-hidden="true"
            />
            {/* Sheet */}
            <div className="fixed inset-x-0 bottom-0 z-50 flex max-h-[80vh] flex-col rounded-t-2xl border-t border-white/15 bg-[#0a0f18] shadow-2xl">
              {/* Drag handle */}
              <div className="flex justify-center pt-2.5 pb-1">
                <div className="h-1 w-10 rounded-full bg-white/20" />
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto">
                <DetailPanel
                  data={snapshot}
                  selectedId={selectedId}
                  detail={detail}
                  detailLoading={detailLoading}
                  detailError={detailError}
                  onClose={() => void selectNode(null)}
                  onNavigate={(path) => {
                    void selectNode(null);
                    navigate(path);
                  }}
                />
              </div>
            </div>
          </>
        )}
      </div>
    );
  }

  // ----------------------------------------------------------------- Desktop
  return (
    <div className="flex h-full flex-col bg-[#070b11] text-white">
      {headerEl}

      <div className="grid min-h-0 flex-1 grid-cols-[236px_1fr_368px]">
        <FilterRail
          data={snapshot}
          statusFilter={statusFilter}
          groupFilter={groupFilter}
          visibleCount={visibleIds.size}
          onToggleStatus={toggleStatus}
          onToggleGroup={toggleGroup}
          onReset={resetFilters}
        />

        {graphEl}

        <DetailPanel
          data={snapshot}
          selectedId={selectedId}
          detail={detail}
          detailLoading={detailLoading}
          detailError={detailError}
          onClose={() => void selectNode(null)}
          onNavigate={(path) => navigate(path)}
        />
      </div>
    </div>
  );
}
