import { useEffect } from "react";
import { usePageHeader } from "@/contexts/usePageHeader";
import PulseChips from "@/components/PulseChips";
import PulseConstellation from "@/components/PulseConstellation";
import "@/theme/pulse.css";

export default function PulsePage() {
  const { setTitle } = usePageHeader();

  useEffect(() => {
    setTitle("Pulse");
  }, [setTitle]);

  return (
    <div className="pulse-root min-h-0 flex-1 flex flex-col">
      <div className="pulse-grid">
        <div className="pulse-zone-top">
          <PulseChips />
        </div>
        <div className="pulse-zone-center">
          <PulseConstellation />
        </div>
        {/* H4: replace this placeholder with <PulseTranscript />.
            The constellation's detail panel renders inside .pulse-zone-center
            with z-index 10; if your right-rail content needs to overlay on
            top of that panel, use z-index ≥ 12 (see pulse.css). */}
        <div className="pulse-zone-right pulse-zone-placeholder">
          Live agent transcript — coming in H4
        </div>
        <div className="pulse-zone-bottom pulse-zone-placeholder">
          Task queue strip — coming in H4
        </div>
      </div>
    </div>
  );
}
