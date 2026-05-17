import { type FC } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@nous-research/ui/ui/components/button";
import HermesFlying from "@/components/mythos/HermesFlying";
import { useMissionSnapshot } from "@/components/mission/useMissionStream";
import { useTodaySpend } from "@/hooks/useTodaySpend";

function formatRelative(iso: string): string {
  const diff = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

export const NowCard: FC = () => {
  const navigate = useNavigate();
  const snapshot = useMissionSnapshot(30_000);
  const spendToday = useTodaySpend();

  const lastActive = snapshot?.recentSessions?.[0]?.createdAt
    ? formatRelative(snapshot.recentSessions[0].createdAt)
    : "—";
  const hivesRunning =
    snapshot?.runtimes.filter((r) => r.status === "online").length ?? 0;
  const spendStr =
    spendToday !== null ? `$${spendToday.toFixed(2)}` : "$—";

  const handleCardClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as Element;
    if (target.closest("button")) return;
    navigate("/chat");
  };

  return (
    <div
      onClick={handleCardClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") navigate("/chat");
      }}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "0 12px",
        height: 56,
        background: "rgba(0,0,0,0.45)",
        borderRadius: "var(--radius, 0.25rem)",
        border: "1px solid rgba(168,192,214,0.18)",
        cursor: "pointer",
        flexShrink: 0,
        userSelect: "none",
      }}
      aria-label="Open last chat session"
    >
      {/* HermesFlying: subtle float animation, disabled when reduced-motion preferred */}
      <HermesFlying
        className="shrink-0 motion-reduce:animate-none"
        style={{ width: 44, height: 44, animation: "hermes-float 3s ease-in-out infinite" }}
      />

      <style>{`
        @keyframes hermes-float {
          0%, 100% { transform: translateY(0); }
          50% { transform: translateY(-3px); }
        }
        @media (prefers-reduced-motion: reduce) {
          .motion-reduce\\:animate-none { animation: none !important; }
        }
      `}</style>

      <span
        style={{
          flex: 1,
          fontSize: 12,
          fontFamily: "ui-monospace, monospace",
          color: "rgba(168,192,214,0.85)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        Last active:{" "}
        <span style={{ color: "#a8c0d6", fontWeight: 600 }}>{lastActive}</span>
        {" · "}
        <span style={{ color: "#a8c0d6", fontWeight: 600 }}>{hivesRunning}</span>
        {" hives running · "}
        <span style={{ color: "#a8c0d6", fontWeight: 600 }}>{spendStr}</span>
      </span>

      <Button
        ghost
        size="sm"
        onClick={(e) => {
          e.stopPropagation();
          navigate("/pantheon");
        }}
        style={{
          flexShrink: 0,
          border: "1px solid rgba(168,192,214,0.35)",
          fontSize: 11,
          whiteSpace: "nowrap",
        }}
      >
        Summon a persona
      </Button>
    </div>
  );
};
