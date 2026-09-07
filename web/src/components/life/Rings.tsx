// Pure-SVG visuals for the Life dashboard: the concentric Four-Ring hero,
// a single readiness gauge, and a lightweight sparkline. No chart libraries.
import { useId } from "react";
import { DOMAINS, overallLabel, type DomainKey } from "./lifeData";

const TAU = Math.PI * 2;

interface FourRingHeroProps {
  scores: Record<DomainKey, number>;
  overall: number;
  size?: number;
}

/**
 * Four concentric rings — one per life domain — each filled to its 0-100 score.
 * The single-glance "how is my whole life today?" centerpiece (grafted from the
 * Bento Glance concept onto the Domain Tabs home).
 */
export function FourRingHero({ scores, overall, size = 220 }: FourRingHeroProps) {
  const center = size / 2;
  const stroke = Math.max(9, size * 0.052);
  const gap = stroke * 0.55;
  // Outer ring = first domain; work inward.
  const rings = DOMAINS.map((d, i) => {
    const radius = center - stroke / 2 - i * (stroke + gap);
    const circumference = TAU * radius;
    const pct = Math.max(0, Math.min(100, scores[d.key])) / 100;
    return { ...d, radius, circumference, pct };
  });

  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
        {rings.map((r) => (
          <g key={r.key}>
            <circle
              cx={center}
              cy={center}
              r={r.radius}
              fill="none"
              stroke="rgba(255,255,255,0.07)"
              strokeWidth={stroke}
            />
            <circle
              cx={center}
              cy={center}
              r={r.radius}
              fill="none"
              stroke={r.accent}
              strokeWidth={stroke}
              strokeLinecap="round"
              strokeDasharray={r.circumference}
              strokeDashoffset={r.circumference * (1 - r.pct)}
              style={{
                transition: "stroke-dashoffset 900ms cubic-bezier(0.33,1,0.68,1)",
                filter: `drop-shadow(0 0 5px ${r.accent}66)`,
              }}
            />
          </g>
        ))}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="font-mono text-4xl font-semibold leading-none text-text-primary tabular-nums">
          {overall}
        </span>
        <span className="mt-1 text-[0.7rem] uppercase tracking-[0.18em] text-text-secondary">
          {overallLabel(overall)}
        </span>
      </div>
    </div>
  );
}

interface GaugeProps {
  value: number; // 0-100
  accent: string;
  size?: number;
  label?: string;
  display?: string;
}

/** Single circular gauge — readiness, budget health, etc. */
export function Gauge({ value, accent, size = 92, label, display }: GaugeProps) {
  const center = size / 2;
  const stroke = Math.max(6, size * 0.1);
  const radius = center - stroke / 2;
  const circumference = TAU * radius;
  const pct = Math.max(0, Math.min(100, value)) / 100;

  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
        <circle cx={center} cy={center} r={radius} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth={stroke} />
        <circle
          cx={center}
          cy={center}
          r={radius}
          fill="none"
          stroke={accent}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - pct)}
          style={{ transition: "stroke-dashoffset 900ms cubic-bezier(0.33,1,0.68,1)" }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="font-mono text-lg font-semibold leading-none text-text-primary tabular-nums">
          {display ?? value}
        </span>
        {label ? (
          <span className="mt-0.5 text-[0.55rem] uppercase tracking-[0.12em] text-text-tertiary">{label}</span>
        ) : null}
      </div>
    </div>
  );
}

interface SparklineProps {
  data: number[];
  accent: string;
  width?: number;
  height?: number;
  filled?: boolean;
}

/** Minimal area+line sparkline. */
export function Sparkline({ data, accent, width = 120, height = 36, filled = true }: SparklineProps) {
  const gid = useId();
  if (!data.length) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const stepX = data.length > 1 ? width / (data.length - 1) : width;
  const pad = 3;
  const pts = data.map((v, i) => {
    const x = i * stepX;
    const y = pad + (1 - (v - min) / span) * (height - pad * 2);
    return [x, y] as const;
  });
  const line = pts.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;
  const last = pts[pts.length - 1];

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="overflow-visible">
      <defs>
        <linearGradient id={`spark-${gid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={accent} stopOpacity={0.28} />
          <stop offset="100%" stopColor={accent} stopOpacity={0} />
        </linearGradient>
      </defs>
      {filled ? <path d={area} fill={`url(#spark-${gid})`} /> : null}
      <path d={line} fill="none" stroke={accent} strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={last[0]} cy={last[1]} r={2.4} fill={accent} />
    </svg>
  );
}
