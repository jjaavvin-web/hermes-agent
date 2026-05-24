import { useEffect } from "react";
import { usePageHeader } from "@/contexts/usePageHeader";
import PulseChips from "@/components/PulseChips";
import PulseConstellation from "@/components/PulseConstellation";
import PulseQueue from "@/components/PulseQueue";
import PulseTranscript from "@/components/PulseTranscript";
import "@/theme/pulse.css";

export default function PulsePage() {
  const { setTitle } = usePageHeader();

  useEffect(() => {
    setTitle("Pulse");
  }, [setTitle]);

  // Z-index ladder (resolved by H4 — see pulse.css for the same values):
  //   canvas (0) < tooltip (8) < right rail (10) < constellation panel (20) < chips (30)
  // The constellation's slide-in detail panel must overlay the right rail when
  // active; the KPI chip row must remain visible above the panel.
  return (
    <div className="pulse-root min-h-0 flex-1 flex flex-col">
      <div className="pulse-grid">
        <div className="pulse-zone-top">
          <PulseChips />
        </div>
        <div className="pulse-zone-center">
          <PulseConstellation />
        </div>
        <div className="pulse-zone-right">
          <PulseTranscript />
        </div>
        <div className="pulse-zone-bottom">
          <PulseQueue />
        </div>
      </div>
    </div>
  );
}
