// Life dashboard — data model, persistence, scoring, and the verse rotation.
//
// v1 data strategy: the four life domains (Finance / Health / Faith / Life) have
// no auto-feed source wired yet (Plaid / Oura / etc. come later), so their values
// are user-owned, persisted to localStorage, and editable in-app. The genuinely
// *live* signals on the page are the system pulse (useMissionStream SSE), the
// verse-of-the-day rotation, and the clock/freshness — see LifePage.

export type DomainKey = "finance" | "health" | "faith" | "life";

export interface DomainMeta {
  key: DomainKey;
  label: string;
  accent: string;
  soft: string;
}

// One accent per domain, used consistently across the hero rings, tabs, and cards.
export const DOMAINS: DomainMeta[] = [
  { key: "finance", label: "Finance", accent: "#34d399", soft: "rgba(52,211,153,0.12)" },
  { key: "health", label: "Health", accent: "#fb7185", soft: "rgba(251,113,133,0.12)" },
  { key: "faith", label: "Faith", accent: "#fbbf24", soft: "rgba(251,191,36,0.13)" },
  { key: "life", label: "Life", accent: "#38bdf8", soft: "rgba(56,189,248,0.12)" },
];

export const DOMAIN_BY_KEY: Record<DomainKey, DomainMeta> = DOMAINS.reduce(
  (acc, d) => {
    acc[d.key] = d;
    return acc;
  },
  {} as Record<DomainKey, DomainMeta>,
);

export interface HabitItem {
  label: string;
  done: boolean;
}

export interface AgendaItem {
  time: string;
  title: string;
  meta: string;
  domain: DomainKey;
}

export interface LifeValues {
  name: string;
  // At-a-glance domain scores (0-100) — drive the Four-Ring hero.
  scores: Record<DomainKey, number>;
  // Finance
  freeToSpend: number;
  monthBudget: number;
  netWorth: number;
  netWorthChangePct: number;
  budgetSpentPct: number;
  spendTrend: number[];
  netWorthTrend: number[];
  // Health
  readiness: number;
  sleepHours: number;
  steps: number;
  hrv: number;
  restingHr: number;
  weightLb: number;
  weightTrend: number[];
  // Faith
  readingStreak: number;
  prayerCount: number;
  devotionalDone: boolean;
  readingPlan: string;
  // Life
  tasksDone: number;
  tasksTotal: number;
  habits: HabitItem[];
  agenda: AgendaItem[];
}

export const DEFAULT_VALUES: LifeValues = {
  name: "Josep",
  scores: { finance: 78, health: 82, faith: 71, life: 64 },
  freeToSpend: 1240,
  monthBudget: 3400,
  netWorth: 248000,
  netWorthChangePct: 1.8,
  budgetSpentPct: 68,
  spendTrend: [42, 38, 51, 47, 55, 49, 58, 53, 61, 57, 66, 62],
  netWorthTrend: [231, 234, 233, 238, 240, 239, 243, 245, 244, 247, 246, 248],
  readiness: 82,
  sleepHours: 7.2,
  steps: 8400,
  hrv: 58,
  restingHr: 52,
  weightLb: 178,
  weightTrend: [182, 181, 181, 180, 180, 179, 179, 178, 178, 178],
  readingStreak: 12,
  prayerCount: 3,
  devotionalDone: false,
  readingPlan: "Psalms · Day 9 of 30",
  tasksDone: 3,
  tasksTotal: 6,
  habits: [
    { label: "Prayer", done: true },
    { label: "Move", done: true },
    { label: "Read", done: true },
    { label: "Water", done: false },
    { label: "Sleep", done: false },
  ],
  agenda: [
    { time: "10:30", title: "Standup", meta: "Work · 30m", domain: "life" },
    { time: "13:00", title: "Deep work", meta: "Focus · 2h", domain: "life" },
    { time: "17:30", title: "Gym — push day", meta: "Health · 1h", domain: "health" },
    { time: "18:30", title: "Evening Examen", meta: "Faith · 10m", domain: "faith" },
  ],
};

const STORAGE_KEY = "hermes-life-v1";

export function loadValues(): LifeValues {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_VALUES;
    const parsed = JSON.parse(raw) as Partial<LifeValues>;
    // Shallow-merge over defaults so new fields added in later versions survive.
    return {
      ...DEFAULT_VALUES,
      ...parsed,
      scores: { ...DEFAULT_VALUES.scores, ...(parsed.scores ?? {}) },
    };
  } catch {
    return DEFAULT_VALUES;
  }
}

export function saveValues(values: LifeValues): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(values));
  } catch {
    /* localStorage may be unavailable (private browsing) — non-fatal */
  }
}

export function overallScore(scores: Record<DomainKey, number>): number {
  const vals = DOMAINS.map((d) => scores[d.key]);
  return Math.round(vals.reduce((a, b) => a + b, 0) / vals.length);
}

export function overallLabel(score: number): string {
  if (score >= 80) return "Thriving";
  if (score >= 65) return "On track";
  if (score >= 50) return "Steady";
  return "Needs care";
}

// Verse-of-the-day — rotates deterministically by day-of-year so it is genuinely
// "today's verse" and identical across a reload, no backend required.
export interface Verse {
  text: string;
  ref: string;
}

const VERSES: Verse[] = [
  { text: "Teach us to number our days, that we may gain a heart of wisdom.", ref: "Psalm 90:12" },
  { text: "Commit to the Lord whatever you do, and he will establish your plans.", ref: "Proverbs 16:3" },
  { text: "Be still, and know that I am God.", ref: "Psalm 46:10" },
  { text: "I can do all this through him who gives me strength.", ref: "Philippians 4:13" },
  { text: "Trust in the Lord with all your heart and lean not on your own understanding.", ref: "Proverbs 3:5" },
  { text: "The Lord is my shepherd, I lack nothing.", ref: "Psalm 23:1" },
  { text: "Cast all your anxiety on him because he cares for you.", ref: "1 Peter 5:7" },
  { text: "This is the day the Lord has made; let us rejoice and be glad in it.", ref: "Psalm 118:24" },
  { text: "Let all that you do be done in love.", ref: "1 Corinthians 16:14" },
  { text: "Whatever you do, work at it with all your heart, as working for the Lord.", ref: "Colossians 3:23" },
  { text: "Seek first his kingdom and his righteousness.", ref: "Matthew 6:33" },
  { text: "The steadfast love of the Lord never ceases; his mercies are new every morning.", ref: "Lamentations 3:22-23" },
  { text: "Do not be anxious about anything, but in every situation, by prayer, present your requests to God.", ref: "Philippians 4:6" },
  { text: "She is clothed with strength and dignity; she can laugh at the days to come.", ref: "Proverbs 31:25" },
];

export function verseOfTheDay(now: Date): Verse {
  const start = new Date(now.getFullYear(), 0, 0);
  const diff = now.getTime() - start.getTime();
  const dayOfYear = Math.floor(diff / 86_400_000);
  return VERSES[dayOfYear % VERSES.length];
}

export function greeting(now: Date): string {
  const h = now.getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

export function fmtMoney(n: number): string {
  if (Math.abs(n) >= 1000) {
    return `$${(n / 1000).toFixed(n % 1000 === 0 ? 0 : 1)}K`;
  }
  return `$${n.toLocaleString()}`;
}
