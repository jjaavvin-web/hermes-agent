import { type FC, useState, useEffect } from "react";
import type React from "react";
import { useNavigate } from "react-router-dom";
import HermesFlying from "@/components/mythos/HermesFlying";
import TriadThrones from "@/components/mythos/TriadThrones";
import WhileYouSleep from "@/components/mythos/WhileYouSleep";

const CHECKLIST_KEY = "hermes-welcome-checklist";

interface ChecklistState {
  soul: boolean;
  orpheus: boolean;
  cron: boolean;
  telegram: boolean;
}

function loadChecklist(): ChecklistState {
  try {
    const raw = localStorage.getItem(CHECKLIST_KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    // ignore
  }
  return { soul: false, orpheus: false, cron: false, telegram: false };
}

function saveChecklist(state: ChecklistState): void {
  try {
    localStorage.setItem(CHECKLIST_KEY, JSON.stringify(state));
  } catch {
    // ignore
  }
}

/** Inline SVG grid representing model swap — no mythos SVG for this card. */
function ModelGridIcon({ className, style }: { className?: string; style?: React.CSSProperties }) {
  return (
    <svg
      className={className}
      style={style}
      viewBox="0 0 120 80"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="Model selection grid"
    >
      {[
        [10, 10, "Nitro", "#00bcd4"],
        [50, 10, "Exacto", "#f5a623"],
        [90, 10, "Auto", "#7b61ff"],
        [10, 45, "BYOK", "#4caf50"],
        [50, 45, "Opus", "#00bcd4"],
        [90, 45, "Haiku", "#f5a623"],
      ].map(([x, y, label, color]) => (
        <g key={String(label)}>
          <rect
            x={Number(x) - 14}
            y={Number(y) - 8}
            width={28}
            height={18}
            rx="3"
            fill="none"
            stroke={String(color)}
            strokeWidth="1.5"
            opacity="0.7"
          />
          <text
            x={Number(x)}
            y={Number(y) + 5}
            textAnchor="middle"
            fontSize="6"
            fill={String(color)}
            opacity="0.9"
            fontFamily="ui-monospace, monospace"
          >
            {String(label)}
          </text>
        </g>
      ))}
      {/* swap arrows */}
      <path
        d="M34 19 L46 19 M40 16 L46 19 L40 22"
        stroke="rgba(168,192,214,0.4)"
        strokeWidth="1"
        fill="none"
      />
      <path
        d="M74 19 L86 19 M80 16 L86 19 L80 22"
        stroke="rgba(168,192,214,0.4)"
        strokeWidth="1"
        fill="none"
      />
    </svg>
  );
}

const WelcomePage: FC = () => {
  const navigate = useNavigate();
  const [checklist, setChecklist] = useState<ChecklistState>(loadChecklist);

  useEffect(() => {
    saveChecklist(checklist);
  }, [checklist]);

  const toggle = (key: keyof ChecklistState) => {
    setChecklist((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const cardStyle: React.CSSProperties = {
    background: "rgba(0,0,0,0.35)",
    border: "1px solid rgba(168,192,214,0.14)",
    borderRadius: "var(--radius, 0.5rem)",
    padding: "24px 20px",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: 12,
    textAlign: "center",
  };

  const btnStyle: React.CSSProperties = {
    marginTop: "auto",
    padding: "6px 16px",
    border: "1px solid rgba(168,192,214,0.35)",
    borderRadius: "var(--radius, 0.25rem)",
    background: "transparent",
    color: "rgba(168,192,214,0.85)",
    fontSize: 11,
    fontFamily: "ui-monospace, monospace",
    cursor: "pointer",
    letterSpacing: "0.08em",
    textTransform: "uppercase" as const,
  };

  const checkItemStyle = (checked: boolean): React.CSSProperties => ({
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "10px 16px",
    borderRadius: "var(--radius, 0.25rem)",
    background: checked ? "rgba(0,188,212,0.06)" : "rgba(0,0,0,0.2)",
    border: `1px solid ${checked ? "rgba(0,188,212,0.25)" : "rgba(168,192,214,0.1)"}`,
    cursor: "pointer",
    userSelect: "none",
    transition: "background 0.15s, border-color 0.15s",
  });

  return (
    <div
      style={{
        maxWidth: 960,
        margin: "0 auto",
        padding: "40px 24px 64px",
        display: "flex",
        flexDirection: "column",
        gap: 48,
        fontFamily: "ui-monospace, monospace",
        color: "rgba(168,192,214,0.85)",
      }}
    >
      {/* ── Hero ── */}
      <section
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 16,
          textAlign: "center",
        }}
      >
        <HermesFlying
          style={{
            width: 280,
            height: 280,
            animation: "welcome-float 4s ease-in-out infinite",
          }}
        />
        <style>{`
          @keyframes welcome-float {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-8px); }
          }
          @media (prefers-reduced-motion: reduce) {
            .welcome-hero-svg { animation: none !important; }
          }
        `}</style>
        <h1
          style={{
            fontSize: 36,
            fontWeight: 700,
            letterSpacing: "0.04em",
            color: "#a8c0d6",
            margin: 0,
            textTransform: "uppercase",
          }}
        >
          Hermes never stops.
        </h1>
        <p
          style={{
            fontSize: 14,
            color: "rgba(168,192,214,0.6)",
            maxWidth: 480,
            lineHeight: 1.6,
            margin: 0,
          }}
        >
          Your agent that learns who you are and works while you sleep.
        </p>
      </section>

      {/* ── Explainer cards ── */}
      <section>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
            gap: 20,
          }}
        >
          {/* The Triad */}
          <div style={cardStyle}>
            <TriadThrones style={{ width: 120, height: 90 }} />
            <strong style={{ fontSize: 13, color: "#a8c0d6", letterSpacing: "0.08em", textTransform: "uppercase" }}>
              The Triad
            </strong>
            <p style={{ fontSize: 12, margin: 0, color: "rgba(168,192,214,0.7)", lineHeight: 1.5 }}>
              <em>Plan. Execute. Critique.</em>
            </p>
            <p style={{ fontSize: 11, margin: 0, color: "rgba(168,192,214,0.5)", lineHeight: 1.6 }}>
              Three personas working in concert — Orpheus plans, Atlas executes, Hermes critiques every output for you.
            </p>
            <button style={btnStyle} onClick={() => navigate("/pantheon")}>
              Meet the Pantheon →
            </button>
          </div>

          {/* While You Sleep */}
          <div style={cardStyle}>
            <WhileYouSleep style={{ width: 140, height: 90 }} />
            <strong style={{ fontSize: 13, color: "#a8c0d6", letterSpacing: "0.08em", textTransform: "uppercase" }}>
              While You Sleep
            </strong>
            <p style={{ fontSize: 12, margin: 0, color: "rgba(168,192,214,0.7)", lineHeight: 1.5 }}>
              <em>Cron jobs in plain English.</em>
            </p>
            <p style={{ fontSize: 11, margin: 0, color: "rgba(168,192,214,0.5)", lineHeight: 1.6 }}>
              Schedule overnight tasks in natural language. Hermes runs them, logs the results, and briefs you in the morning.
            </p>
            <button style={btnStyle} onClick={() => navigate("/cron")}>
              Set up Cron →
            </button>
          </div>

          {/* Hot-swap any model */}
          <div style={cardStyle}>
            <ModelGridIcon style={{ width: 120, height: 80 }} />
            <strong style={{ fontSize: 13, color: "#a8c0d6", letterSpacing: "0.08em", textTransform: "uppercase" }}>
              Hot-swap any model
            </strong>
            <p style={{ fontSize: 12, margin: 0, color: "rgba(168,192,214,0.7)", lineHeight: 1.5 }}>
              <em>OpenRouter modifiers: :nitro, :exacto, auto, BYOK</em>
            </p>
            <p style={{ fontSize: 11, margin: 0, color: "rgba(168,192,214,0.5)", lineHeight: 1.6 }}>
              Switch between Opus, Sonnet, Haiku or any OpenRouter model mid-session. Bring your own key or let Hermes route automatically.
            </p>
            <button style={btnStyle} onClick={() => navigate("/models")}>
              Browse Models →
            </button>
          </div>
        </div>
      </section>

      {/* ── First steps checklist ── */}
      <section
        style={{
          background: "rgba(0,0,0,0.25)",
          border: "1px solid rgba(168,192,214,0.12)",
          borderRadius: "var(--radius, 0.5rem)",
          padding: "24px",
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <h2
          style={{
            fontSize: 13,
            fontWeight: 700,
            letterSpacing: "0.1em",
            textTransform: "uppercase",
            color: "rgba(168,192,214,0.6)",
            margin: "0 0 8px",
          }}
        >
          First steps
        </h2>

        {/* Configure soul.md */}
        <div
          role="checkbox"
          aria-checked={checklist.soul}
          style={checkItemStyle(checklist.soul)}
          onClick={() => toggle("soul")}
          tabIndex={0}
          onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") toggle("soul"); }}
        >
          <span style={{ fontSize: 16, color: checklist.soul ? "#00bcd4" : "rgba(168,192,214,0.4)" }}>
            {checklist.soul ? "☑" : "☐"}
          </span>
          <div style={{ flex: 1 }}>
            <span style={{ fontSize: 12, color: checklist.soul ? "rgba(168,192,214,0.5)" : "rgba(168,192,214,0.85)", textDecoration: checklist.soul ? "line-through" : "none" }}>
              Configure your soul.md
            </span>
            {" — "}
            <a
              href="#"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); }}
              style={{ fontSize: 11, color: "rgba(0,188,212,0.7)", textDecoration: "none" }}
              title="Run: hermes setup soul"
            >
              hermes setup soul
            </a>
          </div>
        </div>

        {/* Summon Orpheus */}
        <div
          role="checkbox"
          aria-checked={checklist.orpheus}
          style={checkItemStyle(checklist.orpheus)}
          onClick={() => toggle("orpheus")}
          tabIndex={0}
          onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") toggle("orpheus"); }}
        >
          <span style={{ fontSize: 16, color: checklist.orpheus ? "#00bcd4" : "rgba(168,192,214,0.4)" }}>
            {checklist.orpheus ? "☑" : "☐"}
          </span>
          <div style={{ flex: 1 }}>
            <span style={{ fontSize: 12, color: checklist.orpheus ? "rgba(168,192,214,0.5)" : "rgba(168,192,214,0.85)", textDecoration: checklist.orpheus ? "line-through" : "none" }}>
              Summon Orpheus
            </span>
            {" — "}
            <button
              onClick={(e) => { e.stopPropagation(); navigate("/pantheon"); }}
              style={{ background: "none", border: "none", color: "rgba(0,188,212,0.7)", fontSize: 11, cursor: "pointer", padding: 0 }}
            >
              Go to Pantheon →
            </button>
          </div>
        </div>

        {/* Queue first overnight task */}
        <div
          role="checkbox"
          aria-checked={checklist.cron}
          style={checkItemStyle(checklist.cron)}
          onClick={() => toggle("cron")}
          tabIndex={0}
          onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") toggle("cron"); }}
        >
          <span style={{ fontSize: 16, color: checklist.cron ? "#00bcd4" : "rgba(168,192,214,0.4)" }}>
            {checklist.cron ? "☑" : "☐"}
          </span>
          <div style={{ flex: 1 }}>
            <span style={{ fontSize: 12, color: checklist.cron ? "rgba(168,192,214,0.5)" : "rgba(168,192,214,0.85)", textDecoration: checklist.cron ? "line-through" : "none" }}>
              Queue your first overnight task
            </span>
            {" — "}
            <button
              onClick={(e) => { e.stopPropagation(); navigate("/cron"); }}
              style={{ background: "none", border: "none", color: "rgba(0,188,212,0.7)", fontSize: 11, cursor: "pointer", padding: 0 }}
            >
              Go to Cron →
            </button>
          </div>
        </div>

        {/* Connect Telegram */}
        <div
          role="checkbox"
          aria-checked={checklist.telegram}
          style={checkItemStyle(checklist.telegram)}
          onClick={() => toggle("telegram")}
          tabIndex={0}
          onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") toggle("telegram"); }}
        >
          <span style={{ fontSize: 16, color: checklist.telegram ? "#00bcd4" : "rgba(168,192,214,0.4)" }}>
            {checklist.telegram ? "☑" : "☐"}
          </span>
          <div style={{ flex: 1 }}>
            <span style={{ fontSize: 12, color: checklist.telegram ? "rgba(168,192,214,0.5)" : "rgba(168,192,214,0.85)", textDecoration: checklist.telegram ? "line-through" : "none" }}>
              Connect Telegram
            </span>
            {" — "}
            <button
              onClick={(e) => { e.stopPropagation(); navigate("/profiles"); }}
              style={{ background: "none", border: "none", color: "rgba(0,188,212,0.7)", fontSize: 11, cursor: "pointer", padding: 0 }}
            >
              Platform setup →
            </button>
          </div>
        </div>
      </section>

      {/* ── Footer ── */}
      <footer
        style={{
          borderTop: "1px solid rgba(168,192,214,0.1)",
          paddingTop: 24,
          textAlign: "center",
          fontSize: 11,
          color: "rgba(168,192,214,0.4)",
          letterSpacing: "0.06em",
        }}
      >
        <button
          onClick={() => navigate("/docs")}
          style={{
            background: "none",
            border: "none",
            color: "rgba(0,188,212,0.6)",
            fontSize: 11,
            cursor: "pointer",
            letterSpacing: "0.06em",
            textDecoration: "underline",
            textUnderlineOffset: 3,
          }}
        >
          Read the full documentation →
        </button>
      </footer>
    </div>
  );
};

export default WelcomePage;
