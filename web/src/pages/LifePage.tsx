// Life dashboard — a mobile-first, glanceable home for the four life domains
// (Finance / Health / Faith / Life). Design: "Domain Tabs + Today" with the
// concentric Four-Ring hero grafted onto the Today home.
//
// Live signals: the system pulse (useMissionStream SSE), verse-of-the-day, and
// the clock/freshness are genuinely live. Domain metrics are user-owned values
// persisted to localStorage and editable in-app (tap "Edit") until per-domain
// sources — Plaid, Oura/Apple Health, a reading-plan API — are wired.
import { useEffect, useMemo, useState, type ChangeEvent, type ComponentType, type CSSProperties, type ReactNode } from "react";
import {
  BookOpen,
  CalendarClock,
  Check,
  ChevronRight,
  Flame,
  Footprints,
  HeartPulse,
  Moon,
  Pencil,
  Sparkles,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
} from "@/components/ui-shims";
import { cn } from "@/lib/utils";
import { useMissionStream } from "@/components/mission/useMissionStream";
import { FourRingHero, Gauge, Sparkline } from "@/components/life/Rings";
import {
  DOMAINS,
  DOMAIN_BY_KEY,
  DEFAULT_VALUES,
  fmtMoney,
  greeting,
  overallScore,
  verseOfTheDay,
  type DomainKey,
  type LifeValues,
} from "@/components/life/lifeData";
import { useLifeAgenda, useLifeData } from "@/components/life/useLifeData";

type Tab = "today" | DomainKey;

const TABS: { key: Tab; label: string }[] = [
  { key: "today", label: "Today" },
  ...DOMAINS.map((d) => ({ key: d.key as Tab, label: d.label })),
];

export default function LifePage() {
  const { values, setValues, save: saveLifeValues, error: stateError } = useLifeData();
  const liveAgenda = useLifeAgenda(30_000);
  const [tab, setTab] = useState<Tab>("today");
  const [editing, setEditing] = useState(false);
  const [now, setNow] = useState(() => new Date());
  const chips = useMissionStream();

  // Clock tick — drives the greeting, live time, and "synced Xs ago" freshness.
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  // Mark a fresh sync whenever live health chips update. Uses React's sanctioned
  // "adjust state while rendering" pattern (not a setState-in-effect): when the
  // chips reference changes and is non-empty, stamp the current clock tick as the
  // latest sync time. `now` is pure state, so no impure Date.now() in render.
  const [prevChips, setPrevChips] = useState(chips);
  const [syncedAt, setSyncedAt] = useState(now);
  if (prevChips !== chips) {
    setPrevChips(chips);
    if (chips.length) setSyncedAt(now);
  }

  const displayValues: LifeValues = liveAgenda
    ? {
        ...values,
        agenda: liveAgenda.agenda.length ? liveAgenda.agenda : values.agenda,
        tasksDone: liveAgenda.tasksDone,
        tasksTotal: liveAgenda.tasksTotal,
      }
    : values;
  const overall = overallScore(displayValues.scores);
  const verse = useMemo(() => verseOfTheDay(now), [now]);
  const online = chips.filter((c) => c.status === "online").length;
  const total = chips.length;
  const syncedAgo = Math.max(0, Math.round((now.getTime() - syncedAt.getTime()) / 1000));

  const update = async (patch: Partial<LifeValues>) => {
    const next = { ...values, ...patch };
    setValues(next);
    await saveLifeValues(next);
  };

  return (
    <div className="mx-auto w-full max-w-3xl pb-10">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 pt-1">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold text-text-primary sm:text-2xl">
            {greeting(now)}, {displayValues.name}
          </h1>
          <p className="mt-0.5 text-sm text-text-tertiary">
            {now.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <LivePill online={online} total={total} syncedAgo={syncedAgo} />
          <Button
            ghost
            size="icon"
            aria-label="Edit values"
            onClick={() => setEditing(true)}
            className="text-text-secondary hover:text-text-primary"
          >
            <Pencil className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Tabs */}
      {stateError ? (
        <p className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          Offline cache active — server sync will retry on next save.
        </p>
      ) : null}

      <div className="sticky top-0 z-10 -mx-3 mt-3 bg-black/70 px-3 py-2 backdrop-blur-sm sm:mx-0 sm:px-0">
        <div role="tablist" aria-label="Life dashboard sections" className="flex gap-1 overflow-x-auto rounded-xl border border-border bg-card/60 p-1">
          {TABS.map((tb) => {
            const accent = tb.key === "today" ? "#e5e7eb" : DOMAIN_BY_KEY[tb.key as DomainKey].accent;
            const active = tab === tb.key;
            return (
              <button
                key={tb.key}
                type="button"
                onClick={() => setTab(tb.key)}
                role="tab"
                aria-selected={active}
                className={cn(
                  "flex-1 whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
                  active ? "text-text-primary" : "text-text-tertiary hover:text-text-secondary",
                )}
                style={active ? { background: "rgba(255,255,255,0.06)", boxShadow: `inset 0 -2px 0 ${accent}` } : undefined}
              >
                {tb.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="mt-3 space-y-3">
        {tab === "today" && (
          <TodayView values={displayValues} overall={overall} verse={verse} now={now} onPrayer={() => setTab("faith")} />
        )}
        {tab === "finance" && <FinanceView v={displayValues} />}
        {tab === "health" && <HealthView v={displayValues} />}
        {tab === "faith" && <FaithView v={displayValues} verseText={verse.text} verseRef={verse.ref} onToggleDevotional={() => void update({ devotionalDone: !values.devotionalDone })} />}
        {tab === "life" && <LifeView v={displayValues} now={now} />}
      </div>

      {editing && (
        <EditDialog
          values={values}
          onClose={() => setEditing(false)}
          onSave={(next) => {
            void saveLifeValues(next).finally(() => setEditing(false));
          }}
        />
      )}
    </div>
  );
}

/* ------------------------------- Header bits ------------------------------- */

function LivePill({ online, total, syncedAgo }: { online: number; total: number; syncedAgo: number }) {
  const ok = total > 0 && online === total;
  const color = total === 0 ? "#7c91a8" : ok ? "#4ade80" : online === 0 ? "#fb2c36" : "#ffbd38";
  const ago = syncedAgo < 60 ? `${syncedAgo}s` : `${Math.round(syncedAgo / 60)}m`;
  return (
    <div className="flex items-center gap-1.5 rounded-full border border-border bg-card/70 px-2.5 py-1">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60" style={{ background: color }} />
        <span className="relative inline-flex h-2 w-2 rounded-full" style={{ background: color }} />
      </span>
      <span className="text-[0.7rem] text-text-secondary tabular-nums">
        {total > 0 ? `${online}/${total} live · ${ago}` : `synced ${ago} ago`}
      </span>
    </div>
  );
}

/* -------------------------------- Card kit -------------------------------- */

function Tile({ children, className, accent }: { children: ReactNode; className?: string; accent?: string }) {
  return (
    <Card
      className={cn("rounded-2xl border-border bg-card p-4", className)}
      style={accent ? { boxShadow: `inset 0 0 0 1px ${accent}22` } : undefined}
    >
      {children}
    </Card>
  );
}

function TileLabel({ icon: Icon, children, accent }: { icon?: ComponentType<{ className?: string; style?: CSSProperties }>; children: ReactNode; accent?: string }) {
  return (
    <div className="flex items-center gap-1.5 text-[0.7rem] uppercase tracking-[0.12em] text-text-tertiary">
      {Icon ? <Icon className="h-3.5 w-3.5" style={accent ? { color: accent } : undefined} /> : null}
      {children}
    </div>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="px-1 pt-1 text-[0.7rem] uppercase tracking-[0.16em] text-text-tertiary">{children}</h2>;
}

/* -------------------------------- Today ----------------------------------- */

function TodayView({
  values,
  overall,
  verse,
  now,
  onPrayer,
}: {
  values: LifeValues;
  overall: number;
  verse: { text: string; ref: string };
  now: Date;
  onPrayer: () => void;
}) {
  const fin = DOMAIN_BY_KEY.finance;
  const hea = DOMAIN_BY_KEY.health;
  const lif = DOMAIN_BY_KEY.life;
  const next = nextAgenda(values.agenda, now);
  return (
    <>
      {/* Hero */}
      <Tile className="flex flex-col items-center gap-3 py-6">
        <FourRingHero scores={values.scores} overall={overall} size={224} />
        <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1">
          {DOMAINS.map((d) => (
            <span key={d.key} className="flex items-center gap-1.5 text-xs text-text-secondary">
              <span className="h-2 w-2 rounded-full" style={{ background: d.accent }} />
              {d.label} <span className="font-mono text-text-primary tabular-nums">{values.scores[d.key]}</span>
            </span>
          ))}
        </div>
      </Tile>

      <SectionTitle>Today at a glance</SectionTitle>
      <div className="grid grid-cols-2 gap-3">
        <Tile accent={fin.accent}>
          <TileLabel icon={Wallet} accent={fin.accent}>Free to spend</TileLabel>
          <div className="mt-1 font-mono text-2xl font-semibold text-text-primary tabular-nums">{fmtMoney(values.freeToSpend)}</div>
          <div className="text-xs text-text-tertiary">of {fmtMoney(values.monthBudget)} · on pace</div>
          <div className="mt-2"><Sparkline data={values.spendTrend} accent={fin.accent} width={150} height={28} /></div>
        </Tile>
        <Tile accent={hea.accent}>
          <TileLabel icon={HeartPulse} accent={hea.accent}>Readiness</TileLabel>
          <div className="mt-1 flex items-center justify-between">
            <div>
              <div className="font-mono text-2xl font-semibold text-text-primary tabular-nums">{values.readiness}</div>
              <div className="text-xs text-text-tertiary">HRV {values.hrv} · RHR {values.restingHr}</div>
            </div>
            <Gauge value={values.readiness} accent={hea.accent} size={68} />
          </div>
        </Tile>
      </div>

      {/* Faith verse */}
      <Tile accent={DOMAIN_BY_KEY.faith.accent} className="bg-gradient-to-br from-[rgba(251,191,36,0.10)] to-transparent">
        <TileLabel icon={BookOpen} accent={DOMAIN_BY_KEY.faith.accent}>Verse of the day</TileLabel>
        <p className="mt-2 font-serif text-lg leading-snug text-text-primary">“{verse.text}”</p>
        <div className="mt-2 flex items-center justify-between">
          <span className="rounded-full border px-2 py-0.5 text-xs" style={{ color: DOMAIN_BY_KEY.faith.accent, borderColor: "rgba(251,191,36,0.4)" }}>{verse.ref}</span>
          <Button size="sm" onClick={onPrayer} style={{ background: DOMAIN_BY_KEY.faith.accent, color: "#1a1206" }}>Begin prayer</Button>
        </div>
      </Tile>

      {/* Today's focus */}
      <SectionTitle>Today’s focus</SectionTitle>
      <Tile accent={lif.accent}>
        <div className="flex items-center justify-between">
          <TileLabel icon={Check} accent={lif.accent}>Tasks</TileLabel>
          <span className="font-mono text-sm text-text-secondary tabular-nums">{values.tasksDone}/{values.tasksTotal}</span>
        </div>
        <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-white/10">
          <div className="h-full rounded-full transition-all" style={{ width: `${(values.tasksDone / Math.max(1, values.tasksTotal)) * 100}%`, background: lif.accent }} />
        </div>
        {next ? (
          <div className="mt-3 flex items-center gap-2 text-sm text-text-secondary">
            <CalendarClock className="h-4 w-4" style={{ color: DOMAIN_BY_KEY[next.domain].accent }} />
            <span className="font-mono tabular-nums">{next.time}</span>
            <span className="text-text-primary">{next.title}</span>
            <span className="text-text-tertiary">· {next.meta}</span>
          </div>
        ) : null}
      </Tile>
    </>
  );
}

function nextAgenda(agenda: LifeValues["agenda"], now: Date) {
  const hm = now.getHours() * 60 + now.getMinutes();
  const parse = (t: string) => {
    if (t === "All day") return 0;
    const [h = 0, m = 0] = t.split(":").map(Number);
    return h * 60 + m;
  };
  return agenda.find((a) => parse(a.time) >= hm) ?? agenda[0] ?? null;
}

/* ------------------------------- Finance ---------------------------------- */

function FinanceView({ v }: { v: LifeValues }) {
  const a = DOMAIN_BY_KEY.finance.accent;
  return (
    <>
      <Tile accent={a}>
        <TileLabel icon={Wallet} accent={a}>Free to spend · this month</TileLabel>
        <div className="mt-1 font-mono text-3xl font-semibold text-text-primary tabular-nums">{fmtMoney(v.freeToSpend)}</div>
        <div className="text-sm text-text-tertiary">of {fmtMoney(v.monthBudget)} budget</div>
        <div className="mt-2"><Sparkline data={v.spendTrend} accent={a} width={300} height={40} /></div>
      </Tile>
      <div className="grid grid-cols-2 gap-3">
        <Tile accent={a}>
          <TileLabel icon={TrendingUp} accent={a}>Net worth</TileLabel>
          <div className="mt-1 font-mono text-2xl font-semibold text-text-primary tabular-nums">{fmtMoney(v.netWorth)}</div>
          <div className="text-xs" style={{ color: a }}>▲ {v.netWorthChangePct}% · 30d</div>
          <div className="mt-2"><Sparkline data={v.netWorthTrend} accent={a} width={150} height={28} /></div>
        </Tile>
        <Tile accent={a}>
          <TileLabel accent={a}>Budget health</TileLabel>
          <div className="mt-1 flex items-center justify-between">
            <div>
              <div className="font-mono text-2xl font-semibold text-text-primary tabular-nums">{v.budgetSpentPct}%</div>
              <div className="text-xs text-text-tertiary">spent · healthy</div>
            </div>
            <Gauge value={v.budgetSpentPct} accent={a} size={66} />
          </div>
        </Tile>
      </div>
      <SourceNote>Connect Plaid / Copilot to auto-feed accounts. For now, tap ✎ to keep these current.</SourceNote>
    </>
  );
}

/* -------------------------------- Health ---------------------------------- */

function HealthView({ v }: { v: LifeValues }) {
  const a = DOMAIN_BY_KEY.health.accent;
  return (
    <>
      <Tile accent={a} className="flex items-center justify-between">
        <div>
          <TileLabel icon={HeartPulse} accent={a}>Readiness</TileLabel>
          <div className="mt-1 font-mono text-3xl font-semibold text-text-primary tabular-nums">{v.readiness}</div>
          <div className="text-sm text-text-tertiary">A good day to push.</div>
        </div>
        <Gauge value={v.readiness} accent={a} size={104} label="ready" />
      </Tile>
      <div className="grid grid-cols-2 gap-3">
        <Tile accent={a}>
          <TileLabel icon={Moon} accent={a}>Sleep</TileLabel>
          <div className="mt-1 font-mono text-2xl font-semibold text-text-primary tabular-nums">{v.sleepHours}h</div>
          <div className="text-xs text-text-tertiary">HRV {v.hrv} ms</div>
        </Tile>
        <Tile accent={a}>
          <TileLabel icon={Footprints} accent={a}>Steps</TileLabel>
          <div className="mt-1 font-mono text-2xl font-semibold text-text-primary tabular-nums">{(v.steps / 1000).toFixed(1)}k</div>
          <div className="text-xs text-text-tertiary">RHR {v.restingHr} bpm</div>
        </Tile>
      </div>
      <Tile accent={a}>
        <TileLabel icon={TrendingUp} accent={a}>Weight trend</TileLabel>
        <div className="mt-1 flex items-end justify-between">
          <div className="font-mono text-2xl font-semibold text-text-primary tabular-nums">{v.weightLb} lb</div>
          <Sparkline data={v.weightTrend} accent={a} width={180} height={36} />
        </div>
      </Tile>
      <SourceNote>Connect Oura / Whoop / Apple Health to auto-feed. For now, tap ✎ to update.</SourceNote>
    </>
  );
}

/* --------------------------------- Faith ---------------------------------- */

function FaithView({
  v,
  verseText,
  verseRef,
  onToggleDevotional,
}: {
  v: LifeValues;
  verseText: string;
  verseRef: string;
  onToggleDevotional: () => void;
}) {
  const a = DOMAIN_BY_KEY.faith.accent;
  return (
    <>
      <Tile accent={a} className="bg-gradient-to-br from-[rgba(251,191,36,0.12)] to-transparent">
        <TileLabel icon={BookOpen} accent={a}>Verse of the day</TileLabel>
        <p className="mt-2 font-serif text-xl leading-snug text-text-primary">“{verseText}”</p>
        <span className="mt-2 inline-block rounded-full border px-2 py-0.5 text-xs" style={{ color: a, borderColor: "rgba(251,191,36,0.4)" }}>{verseRef}</span>
      </Tile>
      <div className="grid grid-cols-2 gap-3">
        <Tile accent={a}>
          <TileLabel icon={Flame} accent={a}>Reading streak</TileLabel>
          <div className="mt-1 font-mono text-2xl font-semibold text-text-primary tabular-nums">{v.readingStreak} days</div>
          <div className="text-xs text-text-tertiary">{v.readingPlan}</div>
        </Tile>
        <Tile accent={a}>
          <TileLabel accent={a}>Prayer list</TileLabel>
          <div className="mt-1 font-mono text-2xl font-semibold text-text-primary tabular-nums">{v.prayerCount}</div>
          <div className="text-xs text-text-tertiary">people you’re lifting up</div>
        </Tile>
      </div>
      <Tile accent={a}>
        <div className="flex items-center justify-between">
          <div>
            <TileLabel accent={a}>Today’s devotional</TileLabel>
            <div className="mt-1 text-sm text-text-secondary">{v.devotionalDone ? "Done — well done." : "5 min · not yet"}</div>
          </div>
          <Button size="sm" onClick={onToggleDevotional} style={{ background: v.devotionalDone ? "transparent" : a, color: v.devotionalDone ? a : "#1a1206", border: `1px solid ${a}` }}>
            {v.devotionalDone ? "Undo" : "Mark done"}
          </Button>
        </div>
      </Tile>
    </>
  );
}

/* ---------------------------------- Life ---------------------------------- */

function LifeView({ v, now }: { v: LifeValues; now: Date }) {
  const a = DOMAIN_BY_KEY.life.accent;
  return (
    <>
      <Tile accent={a}>
        <div className="flex items-center justify-between">
          <TileLabel icon={Check} accent={a}>Today’s three</TileLabel>
          <span className="font-mono text-sm text-text-secondary tabular-nums">{v.tasksDone}/{v.tasksTotal}</span>
        </div>
        <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-white/10">
          <div className="h-full rounded-full" style={{ width: `${(v.tasksDone / Math.max(1, v.tasksTotal)) * 100}%`, background: a }} />
        </div>
      </Tile>
      <Tile accent={a}>
        <TileLabel icon={Sparkles} accent={a}>Habits</TileLabel>
        <div className="mt-3 flex flex-wrap gap-3">
          {v.habits.map((h) => (
            <div key={h.label} className="flex flex-col items-center gap-1">
              <div
                className="flex h-11 w-11 items-center justify-center rounded-full text-sm"
                style={{
                  background: h.done ? a : "rgba(255,255,255,0.06)",
                  color: h.done ? "#08131c" : "var(--text-tertiary, #7c91a8)",
                }}
              >
                {h.done ? <Check className="h-5 w-5" /> : <span className="h-2 w-2 rounded-full bg-white/25" />}
              </div>
              <span className="text-[0.65rem] text-text-tertiary">{h.label}</span>
            </div>
          ))}
        </div>
      </Tile>
      <SectionTitle>The day ahead</SectionTitle>
      <Tile>
        {v.agenda.length ? (
          <ul className="divide-y divide-border">
            {v.agenda.map((it) => {
              const past = isPast(it.time, now);
              return (
                <li key={it.time + it.title} className="flex items-center gap-3 py-2.5 first:pt-0 last:pb-0">
                  <span className="h-8 w-1 rounded-full" style={{ background: DOMAIN_BY_KEY[it.domain].accent, opacity: past ? 0.3 : 1 }} />
                  <span className="font-mono text-sm text-text-secondary tabular-nums">{it.time}</span>
                  <span className={cn("flex-1 text-sm", past ? "text-text-tertiary line-through" : "text-text-primary")}>{it.title}</span>
                  <span className="text-xs text-text-tertiary">{it.meta}</span>
                  <ChevronRight className="h-4 w-4 text-text-tertiary" />
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-sm text-text-tertiary">No Google Calendar events found for today.</p>
        )}
      </Tile>
    </>
  );
}

function isPast(time: string, now: Date): boolean {
  if (time === "All day") return false;
  const [h = 0, m = 0] = time.split(":").map(Number);
  return now.getHours() * 60 + now.getMinutes() > h * 60 + m;
}

/* ------------------------------- Source note ------------------------------ */

function SourceNote({ children }: { children: ReactNode }) {
  return (
    <p className="px-1 pt-1 text-xs text-text-tertiary">
      <span className="mr-1 rounded bg-white/5 px-1.5 py-0.5 text-[0.6rem] uppercase tracking-[0.1em]">manual</span>
      {children}
    </p>
  );
}

/* ------------------------------- Edit dialog ------------------------------ */

function EditDialog({ values, onClose, onSave }: { values: LifeValues; onClose: () => void; onSave: (v: LifeValues) => void }) {
  const [draft, setDraft] = useState<LifeValues>(values);

  const num = (key: keyof LifeValues, label: string, step = 1, autoFocus = false) => (
    <div className="space-y-1">
      <Label className="text-xs text-text-tertiary">{label}</Label>
      <Input
        autoFocus={autoFocus}
        type="number"
        step={step}
        value={String(draft[key] as number)}
        onChange={(e: ChangeEvent<HTMLInputElement>) => setDraft((d: LifeValues) => ({ ...d, [key]: Number(e.target.value) }))}
      />
    </div>
  );

  const scoreField = (k: DomainKey) => (
    <div className="space-y-1" key={k}>
      <Label className="text-xs text-text-tertiary">{DOMAIN_BY_KEY[k].label} score</Label>
      <Input
        type="number"
        min={0}
        max={100}
        value={String(draft.scores[k])}
        onChange={(e: ChangeEvent<HTMLInputElement>) => setDraft((d: LifeValues) => ({ ...d, scores: { ...d.scores, [k]: Number(e.target.value) } }))}
      />
    </div>
  );

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edit your numbers</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <fieldset>
            <legend className="mb-2 text-[0.7rem] uppercase tracking-[0.12em] text-text-tertiary">Domain scores (0–100)</legend>
            <div className="grid grid-cols-2 gap-3">{DOMAINS.map((d) => scoreField(d.key))}</div>
          </fieldset>
          <fieldset>
            <legend className="mb-2 text-[0.7rem] uppercase tracking-[0.12em] text-text-tertiary">Finance</legend>
            <div className="grid grid-cols-2 gap-3">
              {num("freeToSpend", "Free to spend ($)", 10, true)}
              {num("monthBudget", "Month budget ($)", 50)}
              {num("netWorth", "Net worth ($)", 1000)}
              {num("budgetSpentPct", "Budget spent (%)")}
            </div>
          </fieldset>
          <fieldset>
            <legend className="mb-2 text-[0.7rem] uppercase tracking-[0.12em] text-text-tertiary">Health</legend>
            <div className="grid grid-cols-2 gap-3">
              {num("readiness", "Readiness")}
              {num("sleepHours", "Sleep (h)", 0.1)}
              {num("steps", "Steps", 100)}
              {num("hrv", "HRV (ms)")}
              {num("restingHr", "Resting HR")}
              {num("weightLb", "Weight (lb)", 0.1)}
            </div>
          </fieldset>
          <fieldset>
            <legend className="mb-2 text-[0.7rem] uppercase tracking-[0.12em] text-text-tertiary">Faith &amp; Life</legend>
            <div className="grid grid-cols-2 gap-3">
              {num("readingStreak", "Reading streak (days)")}
              {num("prayerCount", "Prayer list")}
              {num("tasksDone", "Tasks done")}
              {num("tasksTotal", "Tasks total")}
            </div>
          </fieldset>
        </div>
        <DialogFooter>
          <Button ghost onClick={onClose}>Cancel</Button>
          <Button onClick={() => onSave(draft)}>Save</Button>
          <Button ghost onClick={() => onSave(DEFAULT_VALUES)} className="text-text-tertiary">Reset</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
