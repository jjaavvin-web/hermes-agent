import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { LearningPanel } from "@/components/learning/LearningPanel";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api, type LearningResponse } from "@/lib/api";

const SNAPSHOT_POLL_MS = 15_000;

export default function LearningPage() {
  const { setTitle } = usePageHeader();
  const [snapshot, setSnapshot] = useState<LearningResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => { setTitle("Learning"); }, [setTitle]);

  const loadSnapshot = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getLearning();
      setSnapshot(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Learning snapshot unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSnapshot();
    const timer = window.setInterval(() => void loadSnapshot(), SNAPSHOT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadSnapshot]);

  if (!snapshot && !error) {
    return <div className="flex min-h-[300px] items-center justify-center bg-background text-sm text-text-secondary"><RefreshCw className="mr-2 h-4 w-4 animate-spin" />Loading Learning…</div>;
  }

  if (!snapshot && error) {
    return (
      <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 bg-background">
        <p className="text-sm text-destructive">{error}</p>
        <button type="button" onClick={() => void loadSnapshot()} className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary transition hover:text-text-primary">Retry</button>
      </div>
    );
  }

  return <LearningPanel snapshot={snapshot as LearningResponse} loading={loading} error={error} onRefresh={() => void loadSnapshot()} />;
}
