import { useMemo, useState } from "react";
import type { JSX } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import {
  DiagnosticRow,
  SEVERITY_SCORE,
  STATUS_CFG,
  StatusChip,
  StatusDot,
  type StatusDiagnostic,
} from "@/components/StatusKit";
import type { OSItem, OSSection, OSStatus } from "@/lib/api";

const COLLAPSED_ITEM_COUNT = 5;
type CountedStatus = "green" | "amber" | "red" | "unknown";

type DisplayItem = OSItem & {
  label?: string;
  value?: string | number | null;
  updated_at?: string | null;
  last_seen?: string | null;
  learning?: {
    signal?: string | null;
    state?: string | null;
    confidence?: string | number | null;
    detail?: string | null;
  } | null;
};

export interface SectionCardProps {
  section: OSSection;
  expanded: boolean;
  onToggleExpand: () => void;
}

function statusScore(status: OSStatus): number {
  return SEVERITY_SCORE[status] ?? 0;
}

function itemLabel(item: DisplayItem): string {
  return item.label?.trim() || item.name?.trim() || "Unnamed probe";
}

function itemMetric(item: DisplayItem): string {
  const raw = item.metric ?? item.value ?? item.learning?.confidence;
  if (raw === null || raw === undefined || raw === "") return "--";
  return String(raw);
}

function itemDetail(item: DisplayItem): string {
  return (
    item.detail?.trim() ||
    item.reason?.trim() ||
    item.learning?.detail?.trim() ||
    item.learning?.signal?.trim() ||
    item.learning?.state?.trim() ||
    "--"
  );
}

function sortedBySeverity(items: OSItem[]): DisplayItem[] {
  return [...items]
    .map((item) => item as DisplayItem)
    .sort((a, b) => {
      const severity = statusScore(b.status) - statusScore(a.status);
      if (severity !== 0) return severity;
      const aHasMetric = itemMetric(a) !== "--";
      const bHasMetric = itemMetric(b) !== "--";
      if (aHasMetric !== bHasMetric) return Number(bHasMetric) - Number(aHasMetric);
      return itemLabel(a).localeCompare(itemLabel(b));
    });
}

function severitySummary(items: OSItem[]): string {
  const counts: Record<CountedStatus, number> = { green: 0, amber: 0, red: 0, unknown: 0 };

  for (const item of items) {
    if (item.status === "green" || item.status === "amber" || item.status === "red" || item.status === "unknown") {
      counts[item.status] += 1;
    }
  }

  const parts = [
    `${counts.green} green`,
    `${counts.amber} amber`,
    `${counts.red} red`,
  ];
  if (counts.unknown > 0) parts.push(`${counts.unknown} unknown`);
  return parts.join(" · ");
}

function diagnosticsFromItems(items: DisplayItem[]): StatusDiagnostic[] {
  return items.flatMap((item) => {
    const detail = itemDetail(item);
    if (item.status === "green" || detail === "--") return [];
    return [{
      severity: item.status,
      source: itemLabel(item),
      message: detail,
      hint: itemMetric(item) !== "--" ? itemMetric(item) : undefined,
      reason: item.reason,
    }];
  });
}

function ItemRow({ item, compact = false }: { item: DisplayItem; compact?: boolean }): JSX.Element {
  const cfg = STATUS_CFG[item.status];
  const isInfo = item.status === "info";
  const metric = itemMetric(item);
  const detail = itemDetail(item);

  return (
    <li
      className={`grid grid-cols-1 gap-1 border-border px-3 py-2.5 text-xs md:grid-cols-[minmax(0,1fr)_auto] md:items-start md:gap-3 ${
        compact ? "rounded-md border bg-background/35" : "border-b last:border-b-0"
      } ${isInfo ? "opacity-60" : ""}`}
      style={compact ? { borderColor: cfg.ring, background: cfg.soft } : undefined}
    >
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <StatusDot status={item.status} className="h-2 w-2" />
          <span className={`min-w-0 truncate font-semibold ${isInfo ? "text-text-secondary" : "text-text-primary"}`}>{itemLabel(item)}</span>
        </div>
        {!compact && <p className="mt-1 break-words leading-relaxed text-text-secondary">{detail}</p>}
      </div>
      <span
        className="min-w-0 truncate font-mono text-text-secondary md:max-w-[11rem] md:text-right"
        style={{ color: item.status === "green" || item.status === "unknown" || isInfo ? undefined : cfg.color }}
        title={metric}
      >
        {metric}
      </span>
    </li>
  );
}

export function SectionCard({ section, expanded, onToggleExpand }: SectionCardProps): JSX.Element {
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const cfg = STATUS_CFG[section.status];
  const sortedItems = useMemo(() => sortedBySeverity(section.items ?? []), [section.items]);
  const collapsedItems = useMemo(() => sortedItems.slice(0, COLLAPSED_ITEM_COUNT), [sortedItems]);
  const summary = useMemo(() => severitySummary(section.items ?? []), [section.items]);
  const diagnostics = useMemo(() => diagnosticsFromItems(sortedItems), [sortedItems]);
  const hiddenCount = Math.max(0, sortedItems.length - collapsedItems.length);
  const attention = section.status === "red" || section.status === "amber";

  return (
    <section
      id={`os-card-${section.id}`}
      className="flex min-w-0 scroll-mt-24 flex-col overflow-hidden rounded-lg border bg-card text-text-primary"
      style={{ borderColor: attention ? cfg.ring : undefined }}
      aria-label={`${section.label} status section`}
    >
      <button
        type="button"
        onClick={onToggleExpand}
        aria-expanded={expanded}
        className="flex min-h-[44px] w-full items-center gap-2.5 px-4 py-3 text-left transition hover:bg-accent/30"
      >
        <StatusDot status={section.status} />
        <span className="font-mondwest text-display min-w-0 flex-1 truncate text-xs tracking-[0.16em] text-text-primary">
          {section.label || "Untitled section"}
        </span>
        <StatusChip status={section.status} />
        {expanded ? <ChevronUp className="h-3.5 w-3.5 flex-shrink-0 text-text-tertiary" /> : <ChevronDown className="h-3.5 w-3.5 flex-shrink-0 text-text-tertiary" />}
      </button>

      <div className="border-t border-border px-4 py-2 text-[0.7rem] text-text-tertiary" aria-label="Severity summary">
        {summary}
      </div>

      {!expanded && (
        <div className="space-y-2 px-3 pb-3">
          {collapsedItems.length === 0 ? (
            <p className="rounded-md border border-border bg-background/35 px-3 py-2 text-xs text-text-tertiary">No probes reported.</p>
          ) : (
            <ul className="grid grid-cols-1 gap-1.5">
              {collapsedItems.map((item) => <ItemRow key={`${item.status}-${itemLabel(item)}`} item={item} compact />)}
            </ul>
          )}
          {hiddenCount > 0 && (
            <button
              type="button"
              onClick={onToggleExpand}
              className="text-xs font-semibold text-text-secondary underline-offset-4 transition hover:text-text-primary hover:underline"
            >
              Show all {sortedItems.length} items
            </button>
          )}
        </div>
      )}

      {expanded && (
        <div className="flex min-h-0 flex-col border-t border-border">
          <ul className="max-h-[22rem] overflow-y-auto overscroll-contain" aria-label={`${section.label} probes`}>
            {sortedItems.length === 0 ? (
              <li className="px-4 py-3 text-xs text-text-tertiary">No probes reported.</li>
            ) : (
              sortedItems.map((item) => <ItemRow key={`${item.status}-${itemLabel(item)}`} item={item} />)
            )}
          </ul>

          {diagnostics.length > 0 && (
            <div className="border-t border-border px-4 py-3">
              <button
                type="button"
                onClick={() => setShowDiagnostics((value) => !value)}
                className="mb-2 text-xs font-semibold text-text-secondary underline-offset-4 transition hover:text-text-primary hover:underline"
                aria-expanded={showDiagnostics}
              >
                {showDiagnostics ? "Hide" : "Show"} diagnostic detail
              </button>
              {showDiagnostics && (
                <ul className="max-h-48 space-y-1.5 overflow-y-auto overscroll-contain" aria-label={`${section.label} diagnostics`}>
                  {diagnostics.map((diag) => <DiagnosticRow key={`${diag.severity}-${diag.source}-${diag.message}`} diag={diag} />)}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export default SectionCard;
