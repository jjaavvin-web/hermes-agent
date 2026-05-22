import { useState, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  Copy,
  ExternalLink,
  Lock,
  Sparkles,
  Wrench,
  X,
} from "lucide-react";
import { QueueSparkline, SpendSparkline } from "@/components/mission";
import type { QueuePoint, SpendPoint } from "@/components/mission";
import type {
  NexusHealthNodeDetail,
  NexusHealthRecommendation,
  NexusHealthResponse,
} from "@/lib/api";
import { kindIcon, statusMeta } from "./constants";

interface DetailPanelProps {
  data: NexusHealthResponse;
  selectedId: string | null;
  detail: NexusHealthNodeDetail | null;
  detailLoading: boolean;
  detailError: string | null;
  onClose: () => void;
  onNavigate: (path: string) => void;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-t border-white/10 px-4 py-3.5">
      <div className="mb-2.5 text-[9.5px] font-semibold uppercase tracking-[0.16em] text-white/35">
        {title}
      </div>
      {children}
    </section>
  );
}

function StatusPill({ status }: { status: string }) {
  const meta = statusMeta(status);
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
      style={{ background: meta.soft, color: meta.color, border: `1px solid ${meta.ring}` }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: meta.color, boxShadow: `0 0 6px ${meta.color}` }}
      />
      {meta.label}
    </span>
  );
}

function CopyButton({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(text);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
      }}
      className="flex items-center gap-1 text-[10px] text-white/45 transition hover:text-white/85"
    >
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      {label ?? (copied ? "Copied" : "Copy")}
    </button>
  );
}

function CommandBlock({ command }: { command: string }) {
  return (
    <div className="mt-2 flex items-center justify-between gap-2 rounded-md border border-white/10 bg-black/45 px-2.5 py-1.5">
      <code className="overflow-x-auto whitespace-nowrap font-mono text-[10.5px] text-cyan-200/85">
        {command}
      </code>
      <CopyButton text={command} label="" />
    </div>
  );
}

function RecommendationCard({ rec }: { rec: NexusHealthRecommendation }) {
  const isFix = rec.kind === "fix";
  const accent = isFix ? "#ffbd38" : "#4ade80";
  const Icon = isFix ? Wrench : Sparkles;
  return (
    <div
      className="rounded-lg border p-3"
      style={{
        borderColor: `${accent}38`,
        background: `${accent}0f`,
      }}
    >
      <div className="flex items-center gap-2">
        <Icon className="h-3.5 w-3.5" style={{ color: accent }} />
        <span className="text-[12px] font-semibold text-white/90">
          {rec.title}
        </span>
        <span
          className="ml-auto text-[8.5px] font-semibold uppercase tracking-wider"
          style={{ color: accent }}
        >
          {isFix ? "Fix" : "Optimize"}
        </span>
      </div>
      <p className="mt-1.5 text-[11.5px] leading-relaxed text-white/60">
        {rec.detail}
      </p>
      {rec.command && <CommandBlock command={rec.command} />}
    </div>
  );
}

/** Right panel — graph overview when nothing is selected, node detail otherwise. */
export function DetailPanel({
  data,
  selectedId,
  detail,
  detailLoading,
  detailError,
  onClose,
  onNavigate,
}: DetailPanelProps) {
  if (!selectedId) {
    return (
      <div className="flex h-full flex-col overflow-y-auto border-l border-white/10 bg-white/[0.02]">
        <div className="px-4 pb-1 pt-4">
          <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-white/70">
            Overview
          </div>
          <p className="mt-2 text-[12px] leading-relaxed text-white/55">
            {data.summary}
          </p>
          <p className="mt-2 text-[10.5px] text-white/35">
            Click any node to inspect its health, metrics and recommendations.
          </p>
        </div>

        <Section title={`Needs Joseph · ${data.needs_joseph.length}`}>
          {data.needs_joseph.length === 0 ? (
            <p className="text-[11.5px] text-white/45">
              No human gate is currently active.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {data.needs_joseph.map((gate) => (
                <div
                  key={gate.id}
                  className="rounded-lg border border-amber-300/30 bg-amber-500/[0.07] p-2.5"
                >
                  <div className="flex items-center gap-1.5 text-[12px] font-semibold text-amber-100">
                    <AlertTriangle className="h-3.5 w-3.5" />
                    {gate.label}
                  </div>
                  <div className="mt-1 text-[11px] text-white/55">
                    {gate.reason}
                  </div>
                  <div className="mt-1.5 text-[10px] text-amber-100/70">
                    {gate.gate}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Section>

        <Section title="Safe Actions">
          <div className="flex flex-col gap-1.5">
            {data.safe_actions.map((action) => (
              <button
                key={action.id}
                type="button"
                onClick={() => {
                  if (action.kind === "copy") {
                    void navigator.clipboard?.writeText(action.payload);
                  } else {
                    onNavigate(action.payload);
                  }
                }}
                className="flex items-center justify-between rounded-lg border border-white/10 px-2.5 py-2 text-left text-[11.5px] text-white/75 transition hover:border-white/25 hover:bg-white/[0.04]"
              >
                <span>{action.label}</span>
                {action.kind === "copy" ? (
                  <Copy className="h-3.5 w-3.5 text-white/40" />
                ) : (
                  <ExternalLink className="h-3.5 w-3.5 text-white/40" />
                )}
              </button>
            ))}
          </div>
        </Section>

        <Section title="Locked Actions">
          <div className="flex flex-col gap-2">
            {data.locked_actions.map((gate) => (
              <div
                key={gate.id}
                className="rounded-lg border border-white/10 bg-black/25 p-2.5"
              >
                <div className="flex items-center gap-1.5 text-[11.5px] font-medium text-white/70">
                  <Lock className="h-3 w-3 text-white/35" />
                  {gate.label}
                </div>
                <div className="mt-1 text-[10.5px] text-white/45">
                  {gate.reason}
                </div>
              </div>
            ))}
          </div>
        </Section>

        <Section title="Evidence">
          <div className="flex flex-col gap-1.5">
            {data.evidence.map((item) => (
              <div key={`${item.source}-${item.detail}`} className="text-[11px]">
                <span className="font-semibold text-white/70">
                  {item.source}
                </span>
                <span className="text-white/45"> — {item.detail}</span>
              </div>
            ))}
          </div>
          <div className="mt-3 text-[10px] text-white/30">
            Snapshot generated {new Date(data.generated_at).toLocaleString()}
          </div>
        </Section>
      </div>
    );
  }

  const fallbackNode = data.nodes.find((n) => n.id === selectedId);
  const meta = statusMeta(detail?.status ?? fallbackNode?.status ?? "unknown");
  const Icon = kindIcon(detail?.kind ?? fallbackNode?.kind ?? "");
  const label = detail?.label ?? fallbackNode?.label ?? selectedId;
  const fixes = detail?.recommendations.filter((r) => r.kind === "fix") ?? [];
  const opts =
    detail?.recommendations.filter((r) => r.kind === "optimization") ?? [];

  return (
    <div className="flex h-full flex-col overflow-y-auto border-l border-white/10 bg-white/[0.02]">
      <div className="flex items-start gap-2.5 px-4 pb-3 pt-4">
        <div
          className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg"
          style={{ background: meta.soft, color: meta.color }}
        >
          <Icon size={18} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-semibold text-white/95">
            {label}
          </div>
          <div className="mt-1 flex items-center gap-2">
            <StatusPill status={detail?.status ?? fallbackNode?.status ?? "unknown"} />
            <span className="text-[10px] uppercase tracking-wider text-white/40">
              {(detail?.kind ?? fallbackNode?.kind ?? "").replace(/-/g, " ")}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close node detail"
          className="flex-shrink-0 rounded-md border border-white/10 p-1 text-white/45 transition hover:border-white/25 hover:text-white/85"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      {detailLoading && !detail && (
        <div className="px-4 py-6 text-[12px] text-white/45">
          Loading node detail…
        </div>
      )}
      {detailError && (
        <div className="mx-4 mb-3 rounded-lg border border-rose-400/40 bg-rose-500/10 p-2.5 text-[11.5px] text-rose-100">
          {detailError}
        </div>
      )}

      {detail && (
        <>
          <div className="px-4 pb-3">
            <p className="text-[12px] leading-relaxed text-white/70">
              {detail.summary}
            </p>
            <p className="mt-2 font-mono text-[10.5px] leading-relaxed text-white/45">
              {detail.details}
            </p>
          </div>

          {detail.metric_cards.length > 0 && (
            <Section title="Key Metrics">
              <div className="grid grid-cols-2 gap-2">
                {detail.metric_cards.map((card) => (
                  <div
                    key={card.label}
                    className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-2"
                  >
                    <div className="text-[9px] uppercase tracking-wider text-white/40">
                      {card.label}
                    </div>
                    <div className="mt-0.5 truncate text-[13px] font-semibold text-white/90">
                      {card.value}
                    </div>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {detail.history.length > 0 && (
            <Section title="History">
              <div className="flex flex-col gap-3">
                {detail.history.map((series) => (
                  <div key={series.label}>
                    <div className="mb-1 text-[10.5px] text-white/55">
                      {series.label}
                    </div>
                    <div className="h-[78px] rounded-lg border border-white/10 bg-black/30 p-1">
                      {series.kind === "queue" ? (
                        <QueueSparkline
                          points={series.points as unknown as QueuePoint[]}
                          openNow={series.openNow ?? 0}
                          label="tasks"
                        />
                      ) : (
                        <SpendSparkline
                          points={series.points as unknown as SpendPoint[]}
                          label="spend"
                        />
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </Section>
          )}

          <Section
            title={
              fixes.length > 0
                ? `Fix Recommendations · ${fixes.length}`
                : `Optimizations · ${opts.length}`
            }
          >
            <div className="flex flex-col gap-2">
              {[...fixes, ...opts].map((rec, index) => (
                <RecommendationCard key={`${rec.title}-${index}`} rec={rec} />
              ))}
            </div>
          </Section>

          <Section title="Safe Next Check">
            <div className="rounded-lg border border-cyan-300/25 bg-cyan-500/[0.06] p-2.5 text-[11.5px] text-cyan-100/85">
              {detail.safe_next_check}
            </div>
          </Section>

          {detail.connections.length > 0 && (
            <Section title={`Connections · ${detail.connections.length}`}>
              <div className="flex flex-col gap-1">
                {detail.connections.map((conn) => {
                  const cm = statusMeta(conn.status);
                  return (
                    <div
                      key={conn.id}
                      className="flex items-center gap-2 rounded-md px-1.5 py-1 text-[11px]"
                    >
                      {conn.direction === "out" ? (
                        <ArrowRight className="h-3 w-3 flex-shrink-0 text-white/35" />
                      ) : (
                        <ArrowLeft className="h-3 w-3 flex-shrink-0 text-white/35" />
                      )}
                      <span
                        className="h-1.5 w-1.5 flex-shrink-0 rounded-full"
                        style={{ background: cm.color }}
                      />
                      <span className="truncate text-white/70">{conn.peer}</span>
                      <span className="ml-auto truncate text-[10px] text-white/35">
                        {conn.label}
                      </span>
                    </div>
                  );
                })}
              </div>
            </Section>
          )}

          <Section title="Provenance">
            <div className="flex flex-col gap-1.5">
              {detail.provenance.map((item) => (
                <div key={`${item.source}-${item.detail}`} className="text-[10.5px]">
                  <span className="font-semibold text-white/65">
                    {item.source}
                  </span>
                  <span className="text-white/40"> — {item.detail}</span>
                </div>
              ))}
            </div>
          </Section>
        </>
      )}
    </div>
  );
}
