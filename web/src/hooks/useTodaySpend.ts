import { useState, useEffect } from "react";
import { api } from "@/lib/api";

/** Returns today's estimated spend in USD, or null while loading. */
export function useTodaySpend(): number | null {
  const [spend, setSpend] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    const refresh = async () => {
      try {
        const data = await api.getSpendHistory("1d");
        if (cancelled) return;
        const today = new Date().toISOString().slice(0, 10);
        const total = data.points
          .filter((p) => p.date === today)
          .reduce((sum, p) => sum + p.amountUsd, 0);
        setSpend(total);
      } catch {
        // Keep last value on error
      }
    };

    refresh();
    const timer = setInterval(refresh, 60_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return spend;
}
