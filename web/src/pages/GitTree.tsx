/**
 * GitTree — a "git history river": a tall portrait tree built from REAL git
 * history (the /api/dashboard/git-river endpoint), styled after the neon-HUD
 * reference. main is a bright vertical central artery; oldest commits sit at the
 * bottom and fade into dim blue/grey, newest commits rise bright at the top. Real
 * branches split from the artery as smooth neon curves and (where detectable)
 * merge back via dashed return arcs; recently-active branches near the top are
 * brighter, thicker and more colourful (green on the left, cyan centre,
 * violet/pink on the right). Black-OLED background, radial portal at the origin.
 * All labels come from real branch names / commit metadata — nothing fabricated.
 *
 * Render: canvas2D, geometry from live data, animated with requestAnimationFrame
 * (energy flows up the artery, active branches shimmer, portal pulses, grow-in
 * rises from the root). Honors prefers-reduced-motion. Hover a branch tip or
 * commit node → tooltip; click a session branch → open its thread/PR.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { GitBranch, GitCommit, GitPullRequest, Sparkle } from "lucide-react";
import { fetchJSON } from "@/lib/api";

type RGB = [number, number, number];

interface TrunkCommit {
  sha: string;
  full: string;
  ts: number;
  author: string;
  subject: string;
  pr: number | null;
  age_rank: number;
}
interface RiverBranch {
  name: string;
  short: string;
  ahead: number;
  fork_rank: number | null;
  lead_commits: { sha: string; ts: number; subject: string }[];
  ts: number;
  recency: number;
  active: boolean;
  is_current: boolean;
  thread_id: string | null;
  pr_number: number | null;
  pr_url: string | null;
  merged: boolean;
}
interface River {
  scanned_at: string;
  base: { ref: string; sha: string; total_commits: number } | null;
  trunk: TrunkCommit[];
  branches: RiverBranch[];
  counts: { trunk: number; branches: number; active: number };
}

// ── palette ──────────────────────────────────────────────────────────────
const WHITE: RGB = [226, 246, 255];
const CYAN: RGB = [104, 210, 255];
const DIM: RGB = [70, 92, 130]; // old/faded blue-grey
const GREEN: RGB = [104, 232, 150];
const TEAL: RGB = [60, 224, 204];
const VIOLET: RGB = [168, 124, 255];
const PINK: RGB = [240, 122, 200];

const CANVAS_H = 760;
const TOP_PAD = 44;
const BASE_PAD = 70;
const CHIP_W = 220;

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}
function clamp(x: number, lo: number, hi: number): number {
  return x < lo ? lo : x > hi ? hi : x;
}
function easeOutCubic(x: number): number {
  return 1 - Math.pow(1 - x, 3);
}
// tip/bead radius by limb depth (primary biggest, twigs smaller)
function depthSize(depth: number): number {
  return depth === 0 ? 4.2 : depth === 1 ? 2.8 : 2.0;
}
function mix(a: RGB, b: RGB, t: number): RGB {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
}
function rgbStr(c: RGB): string {
  return `${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])}`;
}
function fmtNum(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}
function timeAgo(ts: number): string {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function hashStr(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
// branch hue by horizontal side: green(left) → cyan(centre) → violet/pink(right)
function sideHue(side: number): RGB {
  // side ∈ [-1 left, +1 right]
  if (side < -0.55) return GREEN;
  if (side < -0.2) return TEAL;
  if (side < 0.2) return CYAN;
  if (side < 0.55) return VIOLET;
  return PINK;
}

const glowCache = new Map<string, HTMLCanvasElement>();
function glow(c: RGB): HTMLCanvasElement {
  const key = rgbStr(c);
  const hit = glowCache.get(key);
  if (hit) return hit;
  const S = 64;
  const cv = document.createElement("canvas");
  cv.width = S; cv.height = S;
  const g = cv.getContext("2d");
  if (g) {
    const grad = g.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2);
    grad.addColorStop(0, `rgba(${key},1)`);
    grad.addColorStop(0.2, `rgba(${key},0.9)`);
    grad.addColorStop(1, `rgba(${key},0)`);
    g.fillStyle = grad;
    g.fillRect(0, 0, S, S);
  }
  glowCache.set(key, cv);
  return cv;
}

// ── geometry ────────────────────────────────────────────────────────────────
interface BeadPt { x: number; y: number; ageT: number; commit: TrunkCommit }
interface BranchGeo {
  branch: RiverBranch;
  pts: { x: number; y: number }[];
  beads: { x: number; y: number }[];
  tipX: number; tipY: number;
  color: RGB;
  glowK: number;     // brightness 0..1
  width: number;
  side: number;
  depth: number;        // 0 = primary limb off the artery, 1+ = twigs
  interactive: boolean; // only primary limbs are hover/click targets
  showTip: boolean;     // draw a ringed tip node
  mergeBack: { x: number; y: number }[] | null; // dashed return arc
}
interface Layout {
  cx: number;
  topY: number;
  botY: number;
  trunkBeads: BeadPt[];
  branchGeo: BranchGeo[];
  laneTip: Map<string, { x: number; y: number }>;
}

function buildRiver(W: number, data: River | null): Layout {
  const cx = W / 2;
  const topY = TOP_PAD;
  const botY = CANVAS_H - BASE_PAD;
  const trunkBeads: BeadPt[] = [];
  const branchGeo: BranchGeo[] = [];
  const laneTip = new Map<string, { x: number; y: number }>();
  if (!data || data.trunk.length === 0) {
    return { cx, topY, botY, trunkBeads, branchGeo, laneTip };
  }

  const N = data.trunk.length;
  // age_rank 0 = newest → top; N-1 = oldest → bottom
  const yOfRank = (rank: number) => lerp(topY, botY, rank / Math.max(1, N - 1));
  data.trunk.forEach((c) => {
    const ageT = c.age_rank / Math.max(1, N - 1); // 0 new → 1 old
    trunkBeads.push({ x: cx, y: yOfRank(c.age_rank), ageT, commit: c });
  });

  const halfW = Math.min(cx - 24, W * 0.46);

  // Recursive limb: grows from (sx,sy) heading at `ang` (radians; -PI/2 = up) for
  // `len` px as a smooth curve that bends a little more vertical toward its tip,
  // then forks into shorter child twigs that splay further out — a real bushy
  // tree limb. Only depth-0 limbs are interactive (carry the real branch).
  function emitLimb(
    sx: number, sy: number, ang: number, len: number, depth: number,
    color: RGB, glowK: number, width: number, rng: () => number,
    b: RiverBranch, interactive: boolean,
  ) {
    // A limb is a SHORT, nearly-straight diagonal that honours the caller's `ang`
    // (so the spread varies per branch instead of a rigid herringbone). It always
    // climbs: rise is forced negative-y by at least 35% of len.
    const reachX = Math.cos(ang) * len;
    let riseY = Math.sin(ang) * len;        // ang up = sin negative → up
    if (riseY > -len * 0.35) riseY = -len * 0.35; // guarantee an upward climb
    const clampedX = clamp(sx + reachX, 22, W - 22);
    const clampedY = clamp(sy + riseY, topY + 4, botY);
    // near-straight: control points sit ON the line, with a tiny upward bow
    const c1 = { x: sx + (clampedX - sx) * 0.33, y: sy + (clampedY - sy) * 0.42 };
    const c2 = { x: sx + (clampedX - sx) * 0.66, y: sy + (clampedY - sy) * 0.74 };
    const K = 14;
    const pts: { x: number; y: number }[] = [];
    for (let s = 0; s <= K; s++) {
      const t = s / K, mt = 1 - t;
      pts.push({
        x: mt * mt * mt * sx + 3 * mt * mt * t * c1.x + 3 * mt * t * t * c2.x + t * t * t * clampedX,
        y: mt * mt * mt * sy + 3 * mt * mt * t * c1.y + 3 * mt * t * t * c2.y + t * t * t * clampedY,
      });
    }
    const beads: { x: number; y: number }[] = [];
    if (depth === 0) {
      const bn = Math.min(b.lead_commits.length, 3);
      for (let k = 0; k < bn; k++) beads.push(pts[Math.round(((k + 1) / (bn + 1)) * K)]);
    } else if (rng() < 0.5) {
      beads.push(pts[Math.round(0.6 * K)]);
    }
    const sideNorm = (clampedX - cx) / Math.max(1, halfW);
    branchGeo.push({
      branch: b, pts, beads, tipX: clampedX, tipY: clampedY, color, glowK,
      width, side: sideNorm, depth, interactive,
      showTip: depth === 0 || rng() < 0.6, mergeBack: null,
    });
    if (interactive && (b.thread_id || b.pr_url)) laneTip.set(b.name, { x: clampedX, y: clampedY });

    // recurse: short child twigs that curl UPWARD into a small local bush
    if (depth < 2 && len > 24) {
      const kids = depth === 0 ? 2 : rng() < 0.55 ? 2 : 1;
      for (let c = 0; c < kids; c++) {
        const splay = (c - (kids - 1) / 2) * (0.7 + rng() * 0.3);
        // children bias strongly upward (toward -PI/2) so twigs reach for the sky
        const childAng = ang * 0.35 + (-Math.PI / 2) * 0.65 + splay;
        emitLimb(clampedX, clampedY, childAng, len * (0.52 + rng() * 0.1),
          depth + 1, color, glowK * 0.85, Math.max(0.6, width * 0.62), rng, b, false);
      }
    }
  }

  // Distribute the real branches along the FULL height of the artery — newest
  // near the top (bushier/brighter), oldest near the bottom (small/dim). Each
  // attaches to the artery and grows a recursive limb up-and-out, alternating
  // sides. This makes a tall portrait tree, not a wide fan.
  const branches = data.branches; // newest-first
  const M = branches.length;
  const attachTop = topY + (botY - topY) * 0.1;  // crown region (newest)
  const attachBot = botY - (botY - topY) * 0.12; // near the root (oldest)
  branches.forEach((b, i) => {
    const rng = mulberry32(hashStr(b.name));
    const hf = M > 1 ? i / (M - 1) : 0;             // 0 newest … 1 oldest
    const sy = lerp(attachTop, attachBot, hf);      // newest high, oldest low
    const dir: 1 | -1 = i % 2 === 0 ? -1 : 1;
    // limbs near the top are longer + climb more (bushy crown); lower = stubby
    const topness = 1 - hf;
    // SHORT absolute-length limbs (tufts) — the tree's width is built from many
    // small nested twigs, NOT one long limb. This is what stops the V-fan.
    // Slightly longer in the middle of the tree, short at top + bottom.
    // Rounded-crown envelope: limbs longer + flung wider in the upper-middle,
    // short + near-vertical at the very top and bottom → a fat oval canopy.
    const env = Math.sin(clamp(hf, 0, 1) * Math.PI);        // 0 ends → 1 middle
    const len = lerp(40, 135, env) * (0.85 + 0.25 * b.recency);
    // outward angle from vertical grows with env (wide middle), plus real jitter
    const outward = lerp(0.32, 1.0, env) + (rng() - 0.5) * 0.45;
    const ang = -Math.PI / 2 + dir * outward;
    const baseHue = sideHue(dir * lerp(0.4, 1, topness));
    const color = mix(DIM, baseHue, clamp(0.3 + 0.7 * b.recency, 0, 1));
    const glowK = clamp(0.3 + 0.7 * topness + (b.active ? 0.25 : 0), 0, 1);
    const width = lerp(1.0, 3.0, topness) + (b.active ? 0.8 : 0);
    emitLimb(cx, sy, ang, len, 0, color, glowK, width, rng, b, true);
  });

  return { cx, topY, botY, trunkBeads, branchGeo, laneTip };
}

// ── component ────────────────────────────────────────────────────────────
export default function GitTree({ reloadKey }: { reloadKey?: number }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [width, setWidth] = useState(900);
  const [data, setData] = useState<River | null>(null);
  const [hover, setHover] = useState<string | null>(null); // "b:<name>" or "c:<sha>"

  useEffect(() => {
    let cancel = false;
    fetchJSON<River>("/api/dashboard/git-river")
      .then((d) => { if (!cancel) setData(d); })
      .catch(() => {});
    return () => { cancel = true; };
  }, [reloadKey]);

  const layout = useMemo(() => buildRiver(width, data), [width, data]);
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const hoverRef = useRef<string | null>(hover);
  hoverRef.current = hover;

  const sig = useMemo(
    () => `${width}|${data?.scanned_at ?? ""}`,
    [width, data],
  );

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const reduce =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let dpr = Math.min(window.devicePixelRatio || 1, 2);
    const syncSize = () => {
      const w = wrap.clientWidth || 900;
      if (Math.abs(w - width) > 1) setWidth(w);
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(CANVAS_H * dpr);
      canvas.style.width = w + "px";
      canvas.style.height = CANVAS_H + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    syncSize();
    const ro = new ResizeObserver(syncSize);
    ro.observe(wrap);
    const start = performance.now();

    const ringNode = (x: number, y: number, r: number, col: RGB, alpha: number, halo: number) => {
      ctx.globalAlpha = alpha * 0.9;
      ctx.drawImage(glow(col), x - r * halo, y - r * halo, r * halo * 2, r * halo * 2);
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = alpha;
      ctx.beginPath();
      ctx.fillStyle = "#04060d";
      ctx.arc(x, y, r * 0.62, 0, Math.PI * 2);
      ctx.fill();
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = `rgba(${rgbStr(col)},1)`;
      ctx.stroke();
      ctx.globalCompositeOperation = "lighter";
    };

    const render = (nowMs: number) => {
      const t = reduce ? 4000 : nowMs - start;
      const L = layoutRef.current;
      const w = wrap.clientWidth || 900;
      const hov = hoverRef.current;
      const { cx, topY, botY, trunkBeads, branchGeo } = L;
      ctx.clearRect(0, 0, w, CANVAS_H);

      // OLED background w/ faint vertical core glow
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = "#04060d";
      ctx.fillRect(0, 0, w, CANVAS_H);
      const core = ctx.createLinearGradient(0, topY, 0, botY);
      core.addColorStop(0, "rgba(104,210,255,0.10)");
      core.addColorStop(1, "rgba(70,92,130,0.0)");
      ctx.fillStyle = core;
      ctx.fillRect(cx - 60, topY, 120, botY - topY);

      if (trunkBeads.length === 0) {
        ctx.globalCompositeOperation = "source-over";
        ctx.fillStyle = "rgba(255,255,255,0.4)";
        ctx.font = "13px ui-monospace, monospace";
        ctx.textAlign = "center";
        ctx.fillText(data ? "no commits" : "loading git history…", cx, CANVAS_H / 2);
        if (!reduce) raf = requestAnimationFrame(render);
        return;
      }

      ctx.globalCompositeOperation = "lighter";
      // grow-in rises from the bottom (oldest) upward
      const grow = reduce ? 1 : easeOutCubic(clamp(t / 1100, 0, 1));
      const growY = lerp(botY, topY, grow); // everything below growY is revealed

      // ── radial portal at the origin (oldest / bottom) ──
      const portalY = botY + 18;
      for (let r = 0; r < 5; r++) {
        const period = 3200;
        const p = reduce ? 0.3 + r * 0.14 : ((t + r * 620) % period) / period;
        const rad = 10 + p * 120;
        ctx.globalAlpha = (1 - p) * 0.34;
        ctx.strokeStyle = `rgba(${rgbStr(CYAN)},1)`;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.ellipse(cx, portalY, rad, rad * 0.26, 0, 0, Math.PI * 2);
        ctx.stroke();
      }

      // ── trunk artery: dim(old,bottom) → bright(new,top) ──
      for (let i = trunkBeads.length - 1; i > 0; i--) {
        const a = trunkBeads[i], b = trunkBeads[i - 1];
        if (a.y < growY && b.y < growY) continue;
        const colA = mix(WHITE, DIM, a.ageT);
        ctx.strokeStyle = `rgba(${rgbStr(colA)},${0.85 * (1 - a.ageT * 0.5)})`;
        ctx.lineWidth = lerp(3.4, 1.2, a.ageT);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        // soft wide glow on the bright (recent) section
        if (a.ageT < 0.5) {
          ctx.strokeStyle = `rgba(${rgbStr(CYAN)},${0.12 * (1 - a.ageT * 2)})`;
          ctx.lineWidth = 9;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
      // energy flowing UP the artery
      if (!reduce) {
        for (let k = 0; k < 6; k++) {
          const fp = (t * 0.00018 + k / 6) % 1; // 0 bottom → 1 top
          const yy = lerp(botY, topY, fp);
          if (yy < growY) continue;
          ctx.globalAlpha = 0.5 * fp;
          ctx.drawImage(glow(CYAN), cx - 3.5, yy - 3.5, 7, 7);
        }
      }

      // ── branches ──
      branchGeo.forEach((bg) => {
        if (bg.pts[0].y < growY && bg.tipY < growY) return;
        const isHover = hov === `b:${bg.branch.name}`;
        const dim = hov && !isHover && hov.startsWith("b:") ? 0.4 : 1;
        // merge-back dashed arc (drawn under)
        if (bg.mergeBack) {
          ctx.setLineDash([2, 5]);
          ctx.strokeStyle = `rgba(${rgbStr(bg.color)},${0.4 * bg.glowK * dim})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          bg.mergeBack.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
          ctx.stroke();
          ctx.setLineDash([]);
        }
        // branch curve, grown along its length
        const rv = reduce ? 1 : easeOutCubic(clamp((grow - 0.2) / 0.8, 0, 1));
        const last = Math.max(1, Math.floor(rv * (bg.pts.length - 1)));
        const sway = reduce ? 0 : Math.sin(t * 0.0006 + bg.tipX) * (bg.branch.active ? 1.6 : 0.6);
        ctx.strokeStyle = `rgba(${rgbStr(bg.color)},${(isHover ? 1 : 0.5 + 0.4 * bg.glowK) * dim})`;
        ctx.lineWidth = bg.width * (isHover ? 1.6 : 1);
        ctx.beginPath();
        for (let i = 0; i <= last; i++) {
          const p = bg.pts[i];
          const k = i / (bg.pts.length - 1);
          const x = p.x + sway * k;
          if (i === 0) ctx.moveTo(x, p.y); else ctx.lineTo(x, p.y);
        }
        ctx.stroke();
        if (rv < 0.98) return;
        // commit beads
        bg.beads.forEach((p) => ringNode(p.x + sway, p.y, depthSize(bg.depth) * 0.7, bg.color, 0.85 * dim, 1.9));
        if (!bg.showTip) return;
        // tip node (brighter; halo + pulse if active primary limb)
        if (bg.depth === 0 && bg.branch.active && !reduce) {
          const pr = 9 + 3 * Math.sin(t * 0.004 + bg.tipX);
          ctx.globalAlpha = 0.5 * dim;
          ctx.drawImage(glow(bg.color), bg.tipX + sway - pr, bg.tipY - pr, pr * 2, pr * 2);
        }
        const tr = depthSize(bg.depth) * (bg.depth === 0 && bg.branch.active ? 1.35 : 1) * (isHover ? 1.3 : 1);
        ringNode(bg.tipX + sway, bg.tipY, tr, bg.color, dim, 2.2);
      });

      // ── trunk commit beads (on top) ──
      trunkBeads.forEach((bd, i) => {
        if (bd.y < growY) return;
        const col = mix(WHITE, DIM, bd.ageT);
        const isHover = hov === `c:${bd.commit.sha}`;
        const r = lerp(4.4, 1.8, bd.ageT) * (isHover ? 1.4 : 1);
        const pulse = reduce || bd.ageT > 0.4 ? 1 : 0.8 + 0.2 * Math.sin(t * 0.0026 + i);
        ringNode(bd.x, bd.y, r * pulse, col, 1 - bd.ageT * 0.35, 2.2);
      });

      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      if (!reduce) raf = requestAnimationFrame(render);
    };
    let raf = requestAnimationFrame(render);

    const pick = (ev: MouseEvent): string | null => {
      const rect = canvas.getBoundingClientRect();
      const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      const L = layoutRef.current;
      let best: string | null = null, bestD = 22;
      L.branchGeo.forEach((bg) => {
        if (!bg.interactive) return;
        const d = Math.hypot(mx - bg.tipX, my - bg.tipY);
        if (d < bestD) { bestD = d; best = `b:${bg.branch.name}`; }
      });
      L.trunkBeads.forEach((bd) => {
        const d = Math.hypot(mx - bd.x, my - bd.y);
        if (d < bestD) { bestD = d; best = `c:${bd.commit.sha}`; }
      });
      return best;
    };
    const onMove = (ev: MouseEvent) => {
      const id = pick(ev);
      setHover(id);
      canvas.style.cursor = id ? "pointer" : "default";
    };
    const onLeave = () => setHover(null);
    const onClick = (ev: MouseEvent) => {
      const id = pick(ev);
      if (!id || !id.startsWith("b:")) return;
      const name = id.slice(2);
      const b = (layoutRef.current.branchGeo.find((g) => g.branch.name === name) || {}).branch as RiverBranch | undefined;
      if (!b) return;
      const url = b.pr_url || (b.thread_id ? `https://discord.com/channels/@me/${b.thread_id}` : null);
      if (url) window.open(url, "_blank", "noopener,noreferrer");
    };
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);
    canvas.addEventListener("click", onClick);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      canvas.removeEventListener("mousemove", onMove);
      canvas.removeEventListener("mouseleave", onLeave);
      canvas.removeEventListener("click", onClick);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  // hover detail
  const hoverInfo = useMemo(() => {
    if (!hover || !data) return null;
    if (hover.startsWith("b:")) {
      const b = data.branches.find((x) => x.name === hover.slice(2));
      const tip = b ? layout.laneTip.get(b.name) : null;
      const g = layout.branchGeo.find((x) => x.branch.name === hover.slice(2));
      return b ? { kind: "branch" as const, b, x: g?.tipX ?? layout.cx, y: g?.tipY ?? 0, tip } : null;
    }
    const c = data.trunk.find((x) => x.sha === hover.slice(2));
    const bead = layout.trunkBeads.find((x) => x.commit.sha === hover.slice(2));
    return c ? { kind: "commit" as const, c, x: bead?.x ?? layout.cx, y: bead?.y ?? 0 } : null;
  }, [hover, data, layout]);

  return (
    <div
      ref={wrapRef}
      className="relative w-full rounded-2xl border border-white/10 overflow-hidden"
      style={{ height: CANVAS_H, background: "#04060d" }}
    >
      {(["tl", "tr", "bl", "br"] as const).map((corner) => (
        <span
          key={corner}
          className="pointer-events-none absolute h-5 w-5"
          style={{
            top: corner[0] === "t" ? 8 : undefined,
            bottom: corner[0] === "b" ? 8 : undefined,
            left: corner[1] === "l" ? 8 : undefined,
            right: corner[1] === "r" ? 8 : undefined,
            borderTop: corner[0] === "t" ? "1px solid rgba(104,210,255,0.3)" : undefined,
            borderBottom: corner[0] === "b" ? "1px solid rgba(104,210,255,0.3)" : undefined,
            borderLeft: corner[1] === "l" ? "1px solid rgba(104,210,255,0.3)" : undefined,
            borderRight: corner[1] === "r" ? "1px solid rgba(104,210,255,0.3)" : undefined,
          }}
        />
      ))}

      <canvas ref={canvasRef} className="absolute inset-0 block" />

      {/* header: newest at top */}
      <div className="pointer-events-none absolute left-1/2 -translate-x-1/2 text-[11px] font-mono text-center" style={{ top: 10, color: `rgb(${rgbStr(CYAN)})` }}>
        <span className="font-semibold">{data?.base?.ref ?? "main"}</span>
        {data?.base?.sha && <span className="text-white/35"> @{data.base.sha}</span>}
        <span className="text-white/30"> · newest ↑</span>
      </div>
      <div className="pointer-events-none absolute left-1/2 -translate-x-1/2 text-[10px] font-mono text-white/25" style={{ bottom: 8 }}>
        oldest ↓ {data?.base ? `· ${fmtNum(data.base.total_commits)} commits` : ""}
      </div>

      {/* hover tooltip */}
      {hoverInfo && (() => {
        const onLeft = hoverInfo.x < width / 2;
        const col = hoverInfo.kind === "branch"
          ? (hoverInfo.b.active ? VIOLET : CYAN)
          : CYAN;
        const colStr = rgbStr(col);
        const style: React.CSSProperties = {
          left: onLeft ? Math.min(width - CHIP_W - 8, hoverInfo.x + 16) : undefined,
          right: onLeft ? undefined : Math.min(width - CHIP_W - 8, width - hoverInfo.x + 16),
          top: clamp(hoverInfo.y, 30, CANVAS_H - 60),
          width: CHIP_W,
        };
        return (
          <div className="pointer-events-none absolute -translate-y-1/2 z-10" style={style}>
            <div
              className="rounded-lg border px-2.5 py-1.5 backdrop-blur-md"
              style={{ borderColor: `rgb(${colStr})`, background: "rgba(6,10,20,0.94)", boxShadow: `0 0 22px rgba(${colStr},0.35)`, textAlign: onLeft ? "left" : "right" }}
            >
              {hoverInfo.kind === "branch" ? (
                <>
                  <div className={`flex items-center gap-1.5 ${onLeft ? "" : "flex-row-reverse"}`}>
                    <GitBranch className="h-3 w-3" style={{ color: `rgb(${colStr})` }} />
                    <span className="font-mono text-[12.5px] text-white/95 truncate">{hoverInfo.b.short}</span>
                    {hoverInfo.b.active && (
                      <span className="inline-flex items-center gap-0.5 text-[9px] font-semibold shrink-0" style={{ color: `rgb(${colStr})` }}>
                        <Sparkle className="h-2.5 w-2.5" /> active
                      </span>
                    )}
                  </div>
                  <div className={`flex items-center gap-2 mt-1 text-[10px] font-mono text-white/55 ${onLeft ? "" : "flex-row-reverse"}`}>
                    <span>↑{fmtNum(hoverInfo.b.ahead)} commits</span>
                    <span>{timeAgo(hoverInfo.b.ts)}</span>
                    {hoverInfo.b.merged && <span style={{ color: `rgb(${rgbStr(VIOLET)})` }}>merged</span>}
                    {hoverInfo.b.pr_number != null && (
                      <span className="inline-flex items-center gap-0.5" style={{ color: `rgb(${rgbStr(GREEN)})` }}>
                        <GitPullRequest className="h-2.5 w-2.5" />#{hoverInfo.b.pr_number}
                      </span>
                    )}
                  </div>
                  <div className={`font-mono text-[9px] text-white/40 truncate mt-1 ${onLeft ? "" : "text-right"}`}>{hoverInfo.b.name}</div>
                  {hoverInfo.tip && <div className={`text-[9px] text-white/35 mt-0.5 ${onLeft ? "" : "text-right"}`}>click to open →</div>}
                </>
              ) : (
                <>
                  <div className={`flex items-center gap-1.5 ${onLeft ? "" : "flex-row-reverse"}`}>
                    <GitCommit className="h-3 w-3" style={{ color: `rgb(${colStr})` }} />
                    <span className="font-mono text-[12px] text-white/90">{hoverInfo.c.sha}</span>
                    {hoverInfo.c.pr != null && <span className="text-[9px]" style={{ color: `rgb(${rgbStr(GREEN)})` }}>#{hoverInfo.c.pr}</span>}
                  </div>
                  <div className={`text-[10px] text-white/70 mt-1 ${onLeft ? "" : "text-right"}`} style={{ lineHeight: 1.35 }}>{hoverInfo.c.subject}</div>
                  <div className={`text-[9px] font-mono text-white/40 mt-1 ${onLeft ? "" : "text-right"}`}>{hoverInfo.c.author} · {timeAgo(hoverInfo.c.ts)}</div>
                </>
              )}
            </div>
          </div>
        );
      })()}

      {/* legend */}
      <div className="pointer-events-none absolute right-3 bottom-2.5 flex items-center gap-3 text-[10px] font-mono text-white/40">
        <span className="inline-flex items-center gap-1"><i className="h-2 w-2 rounded-full" style={{ background: `rgb(${rgbStr(GREEN)})` }} /> active</span>
        <span className="inline-flex items-center gap-1"><i className="h-2 w-2 rounded-full" style={{ background: `rgb(${rgbStr(CYAN)})` }} /> main</span>
        <span className="inline-flex items-center gap-1"><i className="h-2 w-2 rounded-full" style={{ background: `rgb(${rgbStr(DIM)})` }} /> older</span>
      </div>
    </div>
  );
}
