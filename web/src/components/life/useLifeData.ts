import { useCallback, useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { fetchJSON } from "@/lib/api";
import {
  DEFAULT_VALUES,
  loadValues,
  saveValues,
  type AgendaItem,
  type LifeValues,
} from "./lifeData";

export interface LifeAgenda {
  agenda: AgendaItem[];
  tasksDone: number;
  tasksTotal: number;
}

export interface UseLifeDataResult {
  values: LifeValues;
  setValues: Dispatch<SetStateAction<LifeValues>>;
  save: (next: LifeValues) => Promise<void>;
  loading: boolean;
  error: string | null;
}

function mergeLifeValues(raw: Partial<LifeValues>): LifeValues {
  return {
    ...DEFAULT_VALUES,
    ...raw,
    scores: { ...DEFAULT_VALUES.scores, ...(raw.scores ?? {}) },
  };
}

export function useLifeData(): UseLifeDataResult {
  const [values, setValues] = useState<LifeValues>(() => loadValues());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const serverValues = await fetchJSON<LifeValues>("/api/life/state");
        const merged = mergeLifeValues(serverValues);
        if (!cancelled) {
          setValues(merged);
          saveValues(merged);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          const fallback = loadValues();
          setValues(fallback);
          setError(err instanceof Error ? err.message : "Failed to load life state");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const save = useCallback(async (next: LifeValues) => {
    const merged = mergeLifeValues(next);
    setValues(merged);
    saveValues(merged);
    try {
      const serverValues = await fetchJSON<LifeValues>("/api/life/state", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(merged),
      });
      const confirmed = mergeLifeValues(serverValues);
      setValues(confirmed);
      saveValues(confirmed);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save life state");
      throw err;
    }
  }, []);

  return { values, setValues, save, loading, error };
}

export function useLifeAgenda(intervalMs = 30_000): LifeAgenda | null {
  const [agenda, setAgenda] = useState<LifeAgenda | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchAgenda = async () => {
      try {
        const data = await fetchJSON<LifeAgenda>("/api/life/agenda");
        if (!cancelled) setAgenda(data);
      } catch {
        // Keep the last good live agenda; LifePage falls back to manual values when null.
      }
    };

    fetchAgenda();
    const timer = setInterval(fetchAgenda, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [intervalMs]);

  return agenda;
}
