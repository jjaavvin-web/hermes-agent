import { useMemo, useState } from "react";
import type { OSDiagnostic } from "@/lib/api";
import { DiagnosticRow, STATUS_CFG } from "@/components/StatusKit";

type SeverityFilter = "all" | OSDiagnostic["severity"];

interface DiagnosticsDrawerProps {
  diagnostics: OSDiagnostic[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  redCount: number;
  amberCount: number;
  infoCount?: number;
}

const SEVERITY_FILTERS: SeverityFilter[] = ["all", "red", "amber", "info"];

function severityLabel(severity: SeverityFilter): string {
  if (severity === "all") return "All";
  return STATUS_CFG[severity].label;
}

export function DiagnosticsDrawer({
  diagnostics,
  open,
  onOpenChange,
  redCount,
  amberCount,
  infoCount = 0,
}: DiagnosticsDrawerProps) {
  const [severityFilter, setSeverityFilter] = useState<SeverityFilter>("all");
  const [selectedSources, setSelectedSources] = useState<Set<string>>(() => new Set());

  const sources = useMemo(() => {
    return Array.from(new Set(diagnostics.map((diag) => diag.source || "unknown"))).sort((a, b) => a.localeCompare(b));
  }, [diagnostics]);

  const filtered = useMemo(() => {
    return diagnostics.filter((diag) => {
      const source = diag.source || "unknown";
      const severityOk = severityFilter === "all" || diag.severity === severityFilter;
      const sourceOk = selectedSources.size === 0 || selectedSources.has(source);
      return severityOk && sourceOk;
    });
  }, [diagnostics, selectedSources, severityFilter]);

  if (!open) return null;

  const findingCount = redCount + amberCount;

  return (
    <div className="mt-3 rounded-lg border border-border bg-background/70 p-3 shadow-lg" role="region" aria-label="All OS diagnostics">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-2">
        <div>
          <h3 className="font-mondwest text-display text-sm tracking-[0.14em] text-text-primary">
            {findingCount === 0 ? "All systems nominal" : `${findingCount} finding${findingCount === 1 ? "" : "s"}`}
          </h3>
          <p className="mt-0.5 text-xs text-text-tertiary">
            {redCount} red · {amberCount} amber · {infoCount} info muted/excluded
          </p>
        </div>
        <button
          type="button"
          onClick={() => onOpenChange(false)}
          className="rounded-md border border-border px-2 py-1 text-xs text-text-tertiary transition hover:text-text-primary"
        >
          Close
        </button>
      </div>

      <div className="mt-3 flex flex-wrap gap-3 text-xs">
        <div className="flex flex-wrap items-center gap-1.5" aria-label="Filter diagnostics by severity">
          {SEVERITY_FILTERS.map((severity) => (
            <button
              key={severity}
              type="button"
              onClick={() => setSeverityFilter(severity)}
              aria-pressed={severityFilter === severity}
              className={`rounded-full border px-2 py-1 transition ${severityFilter === severity ? "border-accent bg-accent/30 text-text-primary" : "border-border text-text-tertiary hover:text-text-primary"}`}
            >
              {severityLabel(severity)}
            </button>
          ))}
        </div>

        {sources.length > 0 && (
          <div className="flex min-w-0 flex-wrap items-center gap-1.5" aria-label="Filter diagnostics by source">
            <button
              type="button"
              onClick={() => setSelectedSources(new Set())}
              aria-pressed={selectedSources.size === 0}
              className={`rounded-full border px-2 py-1 transition ${selectedSources.size === 0 ? "border-accent bg-accent/30 text-text-primary" : "border-border text-text-tertiary hover:text-text-primary"}`}
            >
              All sources
            </button>
            {sources.map((source) => (
              <label key={source} className="inline-flex max-w-[12rem] items-center gap-1 rounded-full border border-border px-2 py-1 text-text-tertiary">
                <input
                  type="checkbox"
                  className="h-3 w-3 accent-accent"
                  checked={selectedSources.has(source)}
                  onChange={(event) => {
                    setSelectedSources((prev) => {
                      const next = new Set(prev);
                      if (event.target.checked) next.add(source);
                      else next.delete(source);
                      return next;
                    });
                  }}
                />
                <span className="truncate">{source}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      <div className="mt-3 max-h-80 overflow-y-auto pr-1">
        {filtered.length === 0 ? (
          <div className="rounded-md border border-border bg-card/60 px-3 py-4 text-center text-xs text-text-tertiary">
            No diagnostics match the current filters.
          </div>
        ) : (
          <div className="space-y-2">
            {filtered.map((diag, index) => {
              const isInfo = diag.severity === "info";
              return (
                <div key={`${diag.severity}-${diag.source}-${index}`} className={isInfo ? "opacity-70" : undefined}>
                  <ul aria-label={`${diag.severity} diagnostic from ${diag.source}`}>
                    <DiagnosticRow diag={diag} />
                  </ul>
                  <details className="mt-1 rounded-md border border-border bg-card/35 px-3 py-1.5 text-xs text-text-tertiary">
                    <summary className="cursor-pointer list-none transition hover:text-text-secondary">why</summary>
                    <p className="mt-1 leading-relaxed">{diag.reason || "No details available"}</p>
                  </details>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
