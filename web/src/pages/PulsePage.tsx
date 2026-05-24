import { useEffect } from "react";
import { usePageHeader } from "@/contexts/usePageHeader";
import PulseChips from "@/components/PulseChips";
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
        <div className="pulse-zone-center pulse-zone-placeholder">
          Constellation graph — coming in H3
        </div>
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
