import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";

const QUEUE_POLL_MS = 10_000;
const NOW_TICK_MS = 30_000;
const KANBAN_FALLBACK_URL = "http://127.0.0.1:9119/kanban";
const TOAST_MS = 2_400;
const SKELETON_COUNT = 6;

type QueueStatus = "ready" | "running" | "blocked" | "triage" | string;

export interface PulseQueueCard {
  id: string;
  title: string;
  status: QueueStatus;
  board: string;
  priority: number;
  assignee: string;
  age_seconds: number;
  created_at?: number | null;
}

export interface PulseQueueResponse {
  cards: PulseQueueCard[];
}

function statusAccent(status: QueueStatus): "yellow" | "pink" | "red" | "gray" {
  switch (status) {
    case "ready":
      return "yellow";
    case "running":
      return "pink";
    case "blocked":
      return "red";
    default:
      return "gray";
  }
}

function fmtAge(ageSeconds: number): string {
  const s = Math.max(0, Math.round(ageSeconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86_400)}d`;
}

function truncateId(id: string, max = 8): string {
  if (id.length <= max) return id;
  return id.slice(0, max);
}

function openKanbanFor(card: PulseQueueCard, setToast: (s: string | null) => void) {
  // 1st choice: in-app /kanban/<board>/<id> — no such route currently exists
  //             in BUILTIN_ROUTES_CORE, so we skip.
  // 2nd choice: open the standalone kanban server in a new tab.
  // 3rd choice: clipboard fallback if the popup is blocked. Clipboard writes
  // are async; attach a rejection handler so browser-denied permissions do not
  // surface as unhandled page errors during stress tests.
  try {
    const win = window.open(KANBAN_FALLBACK_URL, "_blank", "noopener,noreferrer");
    if (win) return;
  } catch {
    // fall through to clipboard
  }

  const blockedMessage = `Open blocked · ${card.id}`;
  try {
    const writeText = navigator.clipboard?.writeText;
    if (typeof writeText === "function") {
      void writeText
        .call(navigator.clipboard, card.id)
        .then(() => setToast(`Copied ${card.id}`))
        .catch(() => setToast(blockedMessage));
      return;
    }
  } catch {
    // fall through to blocked toast
  }
  setToast(blockedMessage);
}

export default function PulseQueue() {
  const [cards, setCards] = useState<PulseQueueCard[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastSuccess, setLastSuccess] = useState<number | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [, setNowMs] = useState<number>(() => Date.now());

  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchJSON<PulseQueueResponse>("/api/pulse/queue");
      setCards(Array.isArray(data.cards) ? data.cards : []);
      setError(null);
      setLastSuccess(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Queue unavailable");
    }
  }, []);

  useEffect(() => {
    // Mirrors the PulseChips polling pattern — fire-and-forget on mount,
    // then refresh every QUEUE_POLL_MS.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    const t = setInterval(() => void load(), QUEUE_POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  // Tick the clock so "12m" age strings stay fresh between polls.
  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), NOW_TICK_MS);
    return () => clearInterval(t);
  }, []);

  const showToast = useCallback((msg: string | null) => {
    setToast(msg);
    if (toastTimerRef.current) {
      clearTimeout(toastTimerRef.current);
      toastTimerRef.current = null;
    }
    if (msg) {
      toastTimerRef.current = setTimeout(() => setToast(null), TOAST_MS);
    }
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    };
  }, []);

  // ── Render states ────────────────────────────────────────────────────
  const isLoading = cards === null && !error;
  const isError = !!error && cards === null;
  const isEmpty = Array.isArray(cards) && cards.length === 0 && !error;
  const isErrorWithLast = !!error && Array.isArray(cards) && cards.length > 0;
  const lastSuccessIso = lastSuccess
    ? new Date(lastSuccess).toLocaleTimeString()
    : null;

  return (
    <div className="pulse-queue" data-testid="pulse-queue">
      {isLoading && (
        <div className="pulse-queue__strip" aria-busy="true">
          {Array.from({ length: SKELETON_COUNT }).map((_, i) => (
            <div
              key={i}
              className="pulse-queue__chip pulse-queue__chip--skeleton"
              aria-hidden="true"
            />
          ))}
        </div>
      )}

      {isError && (
        <div className="pulse-queue__empty pulse-queue__empty--error">
          Queue unreachable — retrying
        </div>
      )}

      {isEmpty && (
        <div className="pulse-queue__empty">
          Queue empty — no ready/running cards
        </div>
      )}

      {Array.isArray(cards) && cards.length > 0 && (
        <div className="pulse-queue__strip">
          {cards.map((card) => {
            const accent = statusAccent(card.status);
            return (
              <button
                type="button"
                key={`${card.board}/${card.id}`}
                className={`pulse-queue__chip pulse-queue__chip--${accent}`}
                onClick={() => openKanbanFor(card, showToast)}
                title={`${card.id} — ${card.title} (${card.status} · ${card.board} · ${card.assignee})`}
                data-testid="pulse-queue-chip"
                data-status={card.status}
              >
                <span className="pulse-queue__chip-dot" aria-hidden="true" />
                <span className="pulse-queue__chip-id">
                  {truncateId(card.id)}
                </span>
                <span className="pulse-queue__chip-title">{card.title}</span>
                <span className="pulse-queue__chip-age">
                  {fmtAge(card.age_seconds)}
                </span>
              </button>
            );
          })}
        </div>
      )}

      {isErrorWithLast && (
        <div className="pulse-queue__error-banner" role="status">
          Queue unreachable — last data shown · {lastSuccessIso ?? "—"}
        </div>
      )}

      {toast && (
        <div className="pulse-queue__toast" role="status" data-testid="pulse-queue-toast">
          {toast}
        </div>
      )}
    </div>
  );
}
