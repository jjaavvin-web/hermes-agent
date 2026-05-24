import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { HERMES_BASE_PATH, fetchJSON } from "@/lib/api";

const STREAM_URL = `${HERMES_BASE_PATH}/api/pulse/stream`;
const BUFFER_CAP = 500;
const RENDER_CAP = 100;
const BACKOFF_START_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const MAX_RECONNECTS = 5;
const SCROLL_STICKY_PX = 100;
const HIVE_DROPDOWN_REFRESH_MS = 15_000;
const NOW_TICK_MS = 30_000;
const ALL_HIVES = "__all__";

// Same palette as the constellation — we hash the hive slug into one of these
// accents so a given hive always gets the same bubble tint across reloads.
const BUBBLE_ACCENTS = [
  "purple",
  "cyan",
  "pink",
  "yellow",
  "green",
] as const;
type BubbleAccent = (typeof BUBBLE_ACCENTS)[number];

export interface PulseActivityEvent {
  hive: string;
  line: string;
  ts: string;
}

interface BufferedEvent extends PulseActivityEvent {
  // Local monotonically increasing id so React keys stay stable even when
  // two events share a ts. The backend doesn't emit one.
  _seq: number;
}

type ConnState =
  | { kind: "connecting" }
  | { kind: "open" }
  | { kind: "reconnecting"; attemptsSoFar: number; retryAtMs: number }
  | { kind: "offline" };

// Stable FNV-1a-ish hash → pick one of the BUBBLE_ACCENTS.
function hiveAccent(slug: string): BubbleAccent {
  let h = 2166136261;
  for (let i = 0; i < slug.length; i++) {
    h ^= slug.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return BUBBLE_ACCENTS[h % BUBBLE_ACCENTS.length];
}

function fmtRelative(iso: string, nowMs: number): string {
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return "just now";
  const sec = Math.max(0, Math.round((nowMs - ts) / 1000));
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export default function PulseTranscript() {
  const [events, setEvents] = useState<BufferedEvent[]>([]);
  const [conn, setConn] = useState<ConnState>({ kind: "connecting" });
  const [selectedHive, setSelectedHive] = useState<string>(ALL_HIVES);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());
  const [allHives, setAllHives] = useState<string[]>([]);
  const [atBottom, setAtBottom] = useState<boolean>(true);

  const esRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const countdownTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const backoffMsRef = useRef<number>(BACKOFF_START_MS);
  const attemptsRef = useRef<number>(0);
  const seqRef = useRef<number>(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef<boolean>(true);
  // Mutual recursion between connect() and scheduleReconnect() — we break the
  // cycle by going through a ref. Both functions are re-created when their
  // deps change; the ref is patched in a layout effect below.
  const connectRef = useRef<() => void>(() => undefined);
  const scheduleReconnectRef = useRef<() => void>(() => undefined);

  // Re-derive the hive list from buffered events (most recent first wins) so
  // the dropdown stays accurate even if the /graph poll fails.
  const observedHives = useMemo(() => {
    const seen = new Set<string>();
    // Walk newest → oldest so insertion order in `seen` reflects "most recent".
    for (let i = events.length - 1; i >= 0; i--) {
      const h = events[i].hive;
      if (h) seen.add(h);
    }
    return Array.from(seen);
  }, [events]);

  const hiveOptions = useMemo(() => {
    const merged = new Set<string>();
    for (const h of observedHives) merged.add(h);
    for (const h of allHives) merged.add(h);
    return Array.from(merged).sort();
  }, [observedHives, allHives]);

  const filteredEvents = useMemo(() => {
    const slice =
      selectedHive === ALL_HIVES
        ? events
        : events.filter((e) => e.hive === selectedHive);
    // Render at most the last RENDER_CAP events in DOM.
    return slice.length <= RENDER_CAP
      ? slice
      : slice.slice(slice.length - RENDER_CAP);
  }, [events, selectedHive]);

  const closeStream = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
  }, []);

  const clearReconnectTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (countdownTimerRef.current) {
      clearInterval(countdownTimerRef.current);
      countdownTimerRef.current = null;
    }
  }, []);

  // EventSource doesn't take custom headers — pass the session token as a
  // query param if present (matches useMissionStream). The /api/pulse/stream
  // endpoint is currently public, so this is forward-compat only.
  const buildStreamUrl = useCallback((): string => {
    const token =
      typeof window !== "undefined"
        ? window.__HERMES_SESSION_TOKEN__ ?? ""
        : "";
    return token
      ? `${STREAM_URL}?token=${encodeURIComponent(token)}`
      : STREAM_URL;
  }, []);

  const connect = useCallback(() => {
    closeStream();
    clearReconnectTimers();
    setConn({ kind: "connecting" });

    let es: EventSource;
    try {
      es = new EventSource(buildStreamUrl());
    } catch {
      // Browser refused the URL (rare). Treat as a soft error and queue a
      // retry rather than entering the offline state immediately.
      scheduleReconnectRef.current();
      return;
    }
    esRef.current = es;

    es.addEventListener("open", () => {
      // EventSource fires `open` before any messages; the spec also re-fires
      // it on reconnect. We don't flip to "open" until the first activity
      // event lands because the backend may keep the connection alive with
      // only heartbeats while no hives are running.
      setConn((prev) =>
        prev.kind === "connecting" || prev.kind === "reconnecting"
          ? prev
          : prev,
      );
    });

    es.addEventListener("pulse.activity", (evt: MessageEvent) => {
      try {
        const payload = JSON.parse(evt.data) as PulseActivityEvent;
        if (
          !payload ||
          typeof payload.hive !== "string" ||
          typeof payload.line !== "string"
        ) {
          return;
        }
        const ts =
          typeof payload.ts === "string" && payload.ts
            ? payload.ts
            : new Date().toISOString();
        const seq = ++seqRef.current;
        setEvents((prev) => {
          const next = [...prev, { hive: payload.hive, line: payload.line, ts, _seq: seq }];
          return next.length <= BUFFER_CAP
            ? next
            : next.slice(next.length - BUFFER_CAP);
        });
        // Success → reset failure bookkeeping and flag connection as open.
        attemptsRef.current = 0;
        backoffMsRef.current = BACKOFF_START_MS;
        setConn({ kind: "open" });
      } catch {
        // Ignore malformed payloads.
      }
    });

    es.onerror = () => {
      scheduleReconnectRef.current();
    };
  }, [buildStreamUrl, clearReconnectTimers, closeStream]);

  const scheduleReconnect = useCallback(() => {
    closeStream();
    clearReconnectTimers();
    attemptsRef.current += 1;
    if (attemptsRef.current > MAX_RECONNECTS) {
      setConn({ kind: "offline" });
      return;
    }
    const delay = Math.min(backoffMsRef.current, BACKOFF_MAX_MS);
    backoffMsRef.current = Math.min(backoffMsRef.current * 2, BACKOFF_MAX_MS);
    const retryAtMs = Date.now() + delay;
    setConn({
      kind: "reconnecting",
      attemptsSoFar: attemptsRef.current,
      retryAtMs,
    });
    reconnectTimerRef.current = setTimeout(() => {
      connectRef.current();
    }, delay);
    // Tick once per second so the "Reconnecting in Xs..." label counts down.
    countdownTimerRef.current = setInterval(() => {
      setNowMs(Date.now());
    }, 1_000);
  }, [clearReconnectTimers, closeStream]);

  // Patch the refs so connect/scheduleReconnect can call each other through
  // them. The pair is mutually recursive; refs break the cycle and keep both
  // functions in sync with their latest closures.
  useEffect(() => {
    connectRef.current = connect;
    scheduleReconnectRef.current = scheduleReconnect;
  }, [connect, scheduleReconnect]);

  const manualRetry = useCallback(() => {
    attemptsRef.current = 0;
    backoffMsRef.current = BACKOFF_START_MS;
    connectRef.current();
  }, []);

  // Open the stream on mount; tear down on unmount.
  useEffect(() => {
    connectRef.current();
    return () => {
      closeStream();
      clearReconnectTimers();
    };
    // connect/closeStream/clearReconnectTimers are stable per the deps they
    // close over (buildStreamUrl is a constant function in practice). We
    // deliberately only run this once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Tick the relative-time clock so "Xs ago" stays fresh between events.
  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), NOW_TICK_MS);
    return () => clearInterval(t);
  }, []);

  // Refresh the dropdown options from /api/pulse/graph every 15s so hives
  // that haven't streamed recently can still be selected.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        // Use fetchJSON so the X-Hermes-Session-Token header is injected;
        // the bare fetch() global would 401 silently and the dropdown would
        // never populate (see H5 DEFECTS.md MAJ-1).
        const data = await fetchJSON<{
          nodes?: Array<{ id: string; kind?: string; label?: string }>;
        }>("/api/pulse/graph");
        if (cancelled) return;
        const hives = (data.nodes ?? [])
          .filter((n) => n.kind === "hive")
          .map((n) => n.label || n.id.replace(/^hive:/, ""))
          .filter((s): s is string => typeof s === "string" && s.length > 0);
        setAllHives(hives);
      } catch {
        // Non-fatal — observed-from-events fallback still populates the menu.
      }
    };
    void load();
    const t = setInterval(load, HIVE_DROPDOWN_REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  // If the selected hive vanishes from the option list, snap back to "All".
  useEffect(() => {
    if (selectedHive === ALL_HIVES) return;
    if (!hiveOptions.includes(selectedHive)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSelectedHive(ALL_HIVES);
    }
  }, [hiveOptions, selectedHive]);

  // Auto-scroll: when the user is already at (or near) the bottom and a new
  // event arrives, keep them pinned to the bottom. Otherwise leave them where
  // they are and show the "Jump to latest" pill.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (atBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [filteredEvents]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const isAtBottom = distFromBottom <= SCROLL_STICKY_PX;
    atBottomRef.current = isAtBottom;
    setAtBottom((prev) => (prev === isAtBottom ? prev : isAtBottom));
  }, []);

  const jumpToLatest = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    atBottomRef.current = true;
    setAtBottom(true);
  }, []);

  const reconnectSecsRemaining = (() => {
    if (conn.kind !== "reconnecting") return 0;
    return Math.max(0, Math.ceil((conn.retryAtMs - nowMs) / 1000));
  })();

  const isEmpty = events.length === 0;

  return (
    <div className="pulse-transcript" data-testid="pulse-transcript">
      <div className="pulse-transcript__header">
        <label className="pulse-transcript__switcher-label" htmlFor="pulse-hive-switcher">
          hive
        </label>
        <select
          id="pulse-hive-switcher"
          className="pulse-transcript__switcher"
          data-testid="pulse-hive-switcher"
          value={selectedHive}
          onChange={(e) => setSelectedHive(e.target.value)}
        >
          <option value={ALL_HIVES}>All hives</option>
          {hiveOptions.map((h) => (
            <option key={h} value={h}>
              {h}
            </option>
          ))}
        </select>
        <div className="pulse-transcript__status" data-testid="pulse-transcript-status">
          {conn.kind === "connecting" && (
            <span className="pulse-transcript__status-pill pulse-transcript__status-pill--connecting">
              <span className="pulse-transcript__status-dot" />
              Connecting…
            </span>
          )}
          {conn.kind === "open" && (
            <span className="pulse-transcript__status-pill pulse-transcript__status-pill--open">
              <span className="pulse-transcript__status-dot" />
              Live
            </span>
          )}
          {conn.kind === "reconnecting" && (
            <button
              type="button"
              className="pulse-transcript__status-pill pulse-transcript__status-pill--reconnecting"
              onClick={manualRetry}
              data-testid="pulse-transcript-retry-now"
              title="Skip backoff and reconnect now"
            >
              <span className="pulse-transcript__status-dot" />
              Reconnecting in {reconnectSecsRemaining}s · retry now
            </button>
          )}
          {conn.kind === "offline" && (
            <button
              type="button"
              className="pulse-transcript__status-pill pulse-transcript__status-pill--offline"
              onClick={manualRetry}
              data-testid="pulse-transcript-retry"
            >
              Stream offline · click to retry
            </button>
          )}
        </div>
      </div>

      <div
        ref={scrollRef}
        className="pulse-transcript__scroll"
        onScroll={onScroll}
        data-testid="pulse-transcript-scroll"
      >
        {isEmpty && (
          <div className="pulse-transcript__empty">
            No hives streaming · waiting for activity
          </div>
        )}

        {!isEmpty && filteredEvents.length === 0 && (
          <div className="pulse-transcript__empty">
            No events for <code>{selectedHive}</code> in buffer
          </div>
        )}

        {filteredEvents.map((evt) => {
          const accent = hiveAccent(evt.hive);
          return (
            <div
              key={evt._seq}
              className={`pulse-transcript__bubble pulse-transcript__bubble--${accent}`}
              data-testid="pulse-transcript-bubble"
              data-hive={evt.hive}
            >
              <div className="pulse-transcript__bubble-header">
                <span className="pulse-transcript__bubble-hive">{evt.hive}</span>
                <span className="pulse-transcript__bubble-sep">·</span>
                <span className="pulse-transcript__bubble-time">
                  {fmtRelative(evt.ts, nowMs)}
                </span>
              </div>
              <div className="pulse-transcript__bubble-body">{evt.line}</div>
            </div>
          );
        })}
      </div>

      {!atBottom && filteredEvents.length > 0 && (
        <button
          type="button"
          className="pulse-transcript__jump"
          onClick={jumpToLatest}
          data-testid="pulse-transcript-jump"
        >
          ↓ Jump to latest
        </button>
      )}
    </div>
  );
}
