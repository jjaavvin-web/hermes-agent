import { useEffect, useState } from "react";
import { api, type ReflectCandidate } from "@/lib/api";

export function ReflectPromotePage() {
  const [candidates, setCandidates] = useState<ReflectCandidate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    try {
      const data = await api.getReflectPromoteCandidates();
      setCandidates(data.candidates);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function approve(id: string) {
    await api.approveReflectCandidate(id);
    await refresh();
  }

  async function reject(id: string) {
    await api.rejectReflectCandidate(id, "operator rejected from dashboard");
    await refresh();
  }

  return (
    <main className="space-y-6 p-6">
      <section>
        <p className="text-xs uppercase tracking-[0.25em] text-emerald-400">Reflect Promote</p>
        <h1 className="text-3xl font-semibold text-white">Reflect candidate review</h1>
        <p className="max-w-3xl text-sm text-neutral-300">
          Queued reflection lessons are proposed here. Nothing is written to MVMS until an
          operator explicitly approves a candidate.
        </p>
      </section>

      {loading ? <p className="text-neutral-300">Loading candidates…</p> : null}
      {error ? <p className="rounded border border-red-500/40 bg-red-950/40 p-3 text-red-200">{error}</p> : null}
      {!loading && candidates.length === 0 ? (
        <p className="rounded border border-neutral-800 bg-neutral-950/60 p-4 text-neutral-300">
          No pending reflect candidates.
        </p>
      ) : null}

      <div className="grid gap-4">
        {candidates.map((candidate) => (
          <article key={candidate.id} className="rounded-xl border border-neutral-800 bg-neutral-950/70 p-4 shadow-lg">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-white">{candidate.situation || candidate.id}</h2>
                <p className="text-xs text-neutral-500">{candidate.project} · {candidate.id}</p>
              </div>
              <span className="rounded-full bg-amber-500/15 px-3 py-1 text-xs font-medium text-amber-200">
                {candidate.status}
              </span>
            </div>
            <dl className="space-y-3 text-sm">
              <div>
                <dt className="text-neutral-500">Insight</dt>
                <dd className="text-neutral-200">{candidate.mistake_or_insight}</dd>
              </div>
              <div>
                <dt className="text-neutral-500">Correction</dt>
                <dd className="text-neutral-200">{candidate.correction}</dd>
              </div>
            </dl>
            <div className="mt-4 flex gap-2">
              <button
                className="rounded bg-emerald-500 px-3 py-2 text-sm font-semibold text-black hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-emerald-500"
                onClick={() => void approve(candidate.id)}
                disabled
                title="Promotion to MVMS is not yet wired (MEM-10 pending). Approval is disabled until the writer is configured; the API returns 501 in the meantime."
              >
                Approve
              </button>
              <button
                className="rounded border border-neutral-700 px-3 py-2 text-sm font-semibold text-neutral-200 hover:bg-neutral-900"
                onClick={() => void reject(candidate.id)}
              >
                Reject
              </button>
            </div>
          </article>
        ))}
      </div>
    </main>
  );
}

export default ReflectPromotePage;
