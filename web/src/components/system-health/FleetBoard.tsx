import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type { CronContractRow } from "@/lib/api";
import { Card, CardContent } from "@/components/ui-shims";
import { cn } from "@/lib/utils";

const POLL_MS = 30_000;

function formatTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

/** Tone for the achieved-vs-quota chip. */
function achievedTone(row: CronContractRow): "success" | "warning" | "destructive" {
  if (row.hardFloor || row.achieved === 0) return "destructive";
  if (row.quota !== null && row.achieved < row.quota) return "warning";
  return "success";
}

function achievedLabel(row: CronContractRow): string {
  return `${row.achieved}/${row.quota ?? "?"}`;
}

/**
 * Pull-side fleet board for the self-reporting cron contracts (Card 64).
 *
 * Reads the aggregate ledger via api.getDashboardCronContracts(): the latest
 * contract per cron with its quota, achieved count, gaps, retries, consecutive
 * miss-streak, and a hard-floor badge. Purely a view — no alerting; push stays
 * pull-side by default.
 */
export function FleetBoard() {
  const [rows, setRows] = useState<CronContractRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      api
        .getDashboardCronContracts()
        .then((res) => {
          if (cancelled) return;
          setRows(res.contracts ?? []);
          setError(null);
        })
        .catch((err) => {
          if (cancelled) return;
          setError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    };
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const breaching = rows.filter((r) => r.hardFloor).length;

  return (
    <Card>
      <CardContent className="py-4">
        <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <H2 variant="sm" className="flex items-center gap-2 text-muted-foreground">
              <Activity className="h-4 w-4" />
              Contract fleet
            </H2>
            <p className="text-xs text-muted-foreground">
              {loading
                ? "Loading cron contracts..."
                : rows.length === 0
                  ? "No self-reporting crons yet"
                  : `${rows.length} cron${rows.length === 1 ? "" : "s"} reporting / ${breaching} below hard floor`}
            </p>
          </div>
          {!loading && rows.length > 0 && (
            <Badge tone={breaching ? "destructive" : "success"}>
              {breaching} hard-floor
            </Badge>
          )}
        </div>

        {error && (
          <p className="mb-3 border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
            Contracts unavailable: {error}
          </p>
        )}

        {!loading && !error && rows.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No cron has self-reported a contract yet. Set <code>contract: true</code> on a
            job to start tracking its quota.
          </p>
        )}

        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Cron</th>
                  <th className="py-2 pr-3 font-medium">Achieved</th>
                  <th className="py-2 pr-3 font-medium">Gaps</th>
                  <th className="py-2 pr-3 font-medium">Retries</th>
                  <th className="py-2 pr-3 font-medium">Misses</th>
                  <th className="py-2 pr-3 font-medium">Floor</th>
                  <th className="py-2 pr-3 font-medium">Last run</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.name}
                    className={cn(
                      "border-b border-border/50 align-top",
                      row.hardFloor && "bg-destructive/5",
                    )}
                  >
                    <td className="py-2 pr-3 font-mono-ui font-medium">{row.name}</td>
                    <td className="py-2 pr-3">
                      <Badge tone={achievedTone(row)}>{achievedLabel(row)}</Badge>
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {row.gaps.length === 0 ? (
                        <span className="text-success">none</span>
                      ) : (
                        <span className="text-warning" title={row.gaps.join("; ")}>
                          {row.gaps.length === 1
                            ? row.gaps[0]
                            : `${row.gaps.length} gaps`}
                        </span>
                      )}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">{row.retries}</td>
                    <td className="py-2 pr-3">
                      {row.missStreak > 0 ? (
                        <Badge tone={row.missStreak >= 3 ? "destructive" : "warning"}>
                          {row.missStreak}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">0</span>
                      )}
                    </td>
                    <td className="py-2 pr-3">
                      {row.hardFloor ? (
                        <Badge tone="destructive">hard floor</Badge>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">{formatTime(row.lastRun)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default FleetBoard;
