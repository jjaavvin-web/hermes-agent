import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Plus, Pencil, Zap, X, ChevronDown } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import type { Persona, PersonaCreate, PersonaModelSpec } from "@/lib/api";
import { ModelPickerDialog } from "@/components/ModelPickerDialog";

// ── Model chip ────────────────────────────────────────────────────────────────

function PersonaFrame({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 100 120"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="Marble cameo persona frame"
    >
      <ellipse cx="50" cy="58" rx="46" ry="56" fill="#F4F0E6" opacity="0.92" />
      <ellipse cx="50" cy="58" rx="46" ry="56" fill="none" stroke="#1E3A8A" strokeWidth="1.2" opacity="0.5" />
      <ellipse cx="50" cy="58" rx="42" ry="52" fill="none" stroke="#1E3A8A" strokeWidth="0.7" opacity="0.3" />
      <ellipse cx="50" cy="58" rx="39" ry="49" fill="none" stroke="#1E3A8A" strokeWidth="0.5" opacity="0.2" />
      <path d="M34 8 C38 4, 44 3, 50 2 C56 3, 62 4, 66 8" fill="none" stroke="#D4A017" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="50" cy="2" r="2.5" fill="#D4A017" />
      <path d="M28 18 C24 14, 20 16, 22 20 C24 24, 28 22, 28 18 Z" fill="#D4A017" opacity="0.7" />
      <path d="M72 18 C76 14, 80 16, 78 20 C76 24, 72 22, 72 18 Z" fill="#D4A017" opacity="0.7" />
      <path d="M34 110 C38 114, 44 115, 50 116 C56 115, 62 114, 66 110" fill="none" stroke="#D4A017" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="50" cy="116" r="2.5" fill="#D4A017" />
      <ellipse cx="50" cy="58" rx="32" ry="40" fill="transparent" stroke="#1E3A8A" strokeWidth="1" opacity="0.4" />
    </svg>
  );
}

function ModelChip({ role, spec }: { role: string; spec: PersonaModelSpec }) {
  const roleColors: Record<string, string> = {
    planner: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
    executor: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
    critic: "bg-purple-500/10 text-purple-600 dark:text-purple-400",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-medium font-mono",
        roleColors[role] ?? "bg-muted text-muted-foreground",
      )}
    >
      <span className="opacity-60 capitalize">{role[0]}</span>
      {spec.model}
    </span>
  );
}

// ── Persona card ──────────────────────────────────────────────────────────────

function PersonaCard({
  persona,
  onSummon,
  onEdit,
}: {
  persona: Persona;
  onSummon: (slug: string) => void;
  onEdit: (persona: Persona) => void;
}) {
  const [summoning, setSummoning] = useState(false);

  const handleSummon = async () => {
    setSummoning(true);
    try {
      await onSummon(persona.slug);
    } finally {
      setSummoning(false);
    }
  };

  return (
    <div
      className={cn(
        "flex flex-col gap-3 p-4",
        "border border-current/15 bg-background-base/60",
        "transition-all duration-200 hover:border-current/30",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="shrink-0 w-14 h-16">
          <PersonaFrame className="w-full h-full" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-mondwest text-sm tracking-[0.08em] text-midground truncate">
            {persona.name}
          </h3>
          <p className="text-[11px] text-muted-foreground/80 mt-0.5 line-clamp-2 leading-relaxed">
            {persona.role_one_liner}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-1">
        <ModelChip role="planner" spec={persona.planner} />
        <ModelChip role="executor" spec={persona.executor} />
        <ModelChip role="critic" spec={persona.critic} />
      </div>

      <div className="flex items-center gap-2 pt-1">
        <Button
          size="sm"
          onClick={handleSummon}
          disabled={summoning}
          className="flex-1 gap-1.5 text-xs"
        >
          {summoning ? <Spinner className="text-xs" /> : <Zap className="h-3 w-3" />}
          {summoning ? "Summoning…" : "Summon"}
        </Button>
        <Button
          ghost
          size="sm"
          onClick={() => onEdit(persona)}
          className="gap-1.5 text-xs opacity-60 hover:opacity-100"
        >
          <Pencil className="h-3 w-3" />
          Edit
        </Button>
      </div>
    </div>
  );
}

// ── Model role picker ─────────────────────────────────────────────────────────

function ModelRolePicker({
  role,
  value,
  onChange,
}: {
  role: string;
  value: PersonaModelSpec;
  onChange: (spec: PersonaModelSpec) => void;
}) {
  const [pickerOpen, setPickerOpen] = useState(false);

  return (
    <div className="flex flex-col gap-1">
      <label className="text-[11px] font-medium text-muted-foreground capitalize tracking-[0.08em]">
        {role}
      </label>
      <button
        type="button"
        onClick={() => setPickerOpen(true)}
        className={cn(
          "flex items-center justify-between gap-2 w-full",
          "border border-current/20 bg-background-base/40 px-3 py-2",
          "text-xs font-mono text-left",
          "hover:border-current/40 transition-colors",
        )}
      >
        <span className="truncate">
          {value.model ? `${value.provider}/${value.model}` : "Pick model…"}
        </span>
        <ChevronDown className="h-3 w-3 shrink-0 opacity-50" />
      </button>

      {pickerOpen && (
        <ModelPickerDialog
          title={`${role.charAt(0).toUpperCase() + role.slice(1)} Model`}
          loader={() => api.getModelOptions()}
          alwaysGlobal={false}
          onApply={({ provider, model }) => {
            onChange({ provider, model });
            setPickerOpen(false);
          }}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  );
}

// ── Create/Edit modal ─────────────────────────────────────────────────────────

interface ModalProps {
  initial?: Persona;
  soulTemplate: string;
  onSave: (data: PersonaCreate) => Promise<void>;
  onClose: () => void;
}

function PersonaModal({ initial, soulTemplate, onSave, onClose }: ModalProps) {
  const [name, setName] = useState(initial?.name ?? "");
  const [roleLine, setRoleLine] = useState(initial?.role_one_liner ?? "");
  const [soulMd, setSoulMd] = useState(initial?.soul_md ?? soulTemplate);
  const [planner, setPlanner] = useState<PersonaModelSpec>(
    initial?.planner ?? { provider: "anthropic", model: "claude-sonnet-4-6" },
  );
  const [executor, setExecutor] = useState<PersonaModelSpec>(
    initial?.executor ?? { provider: "anthropic", model: "claude-sonnet-4-6" },
  );
  const [critic, setCritic] = useState<PersonaModelSpec>(
    initial?.critic ?? { provider: "anthropic", model: "claude-sonnet-4-6" },
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave({
        name: name.trim(),
        role_one_liner: roleLine,
        soul_md: soulMd,
        planner,
        executor,
        critic,
      });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
      <div
        className={cn(
          "relative w-full max-w-xl max-h-[90dvh] flex flex-col",
          "border border-current/20 bg-background-base",
          "shadow-[0_24px_48px_-12px_rgba(0,0,0,0.6)]",
        )}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-current/10">
          <h2 className="font-mondwest text-sm tracking-[0.1em] text-midground">
            {initial ? "Edit Persona" : "Create Persona"}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-muted-foreground/60 hover:text-muted-foreground transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-medium text-muted-foreground tracking-[0.08em]">
              NAME
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Orpheus"
              className={cn(
                "border border-current/20 bg-background-base/40 px-3 py-2",
                "text-sm text-foreground placeholder:text-muted-foreground/40",
                "focus:outline-none focus:border-current/50",
              )}
            />
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-medium text-muted-foreground tracking-[0.08em]">
              ROLE
            </label>
            <input
              type="text"
              value={roleLine}
              onChange={(e) => setRoleLine(e.target.value)}
              placeholder="One-line role description"
              className={cn(
                "border border-current/20 bg-background-base/40 px-3 py-2",
                "text-sm text-foreground placeholder:text-muted-foreground/40",
                "focus:outline-none focus:border-current/50",
              )}
            />
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-medium text-muted-foreground tracking-[0.08em]">
              SOUL
            </label>
            <textarea
              value={soulMd}
              onChange={(e) => setSoulMd(e.target.value)}
              rows={8}
              className={cn(
                "border border-current/20 bg-background-base/40 px-3 py-2",
                "text-xs font-mono text-foreground placeholder:text-muted-foreground/40",
                "focus:outline-none focus:border-current/50 resize-y",
              )}
            />
          </div>

          <div className="space-y-3">
            <p className="text-[11px] font-medium text-muted-foreground tracking-[0.08em]">
              TRIAD MODELS
            </p>
            <ModelRolePicker role="planner" value={planner} onChange={setPlanner} />
            <ModelRolePicker role="executor" value={executor} onChange={setExecutor} />
            <ModelRolePicker role="critic" value={critic} onChange={setCritic} />
          </div>

          {error && <p className="text-xs text-red-500">{error}</p>}
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-current/10">
          <Button ghost size="sm" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button size="sm" onClick={handleSave} disabled={saving} className="gap-1.5">
            {saving && <Spinner className="text-xs" />}
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ── PantheonPage ──────────────────────────────────────────────────────────────

export default function PantheonPage() {
  const navigate = useNavigate();
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [soulTemplate, setSoulTemplate] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [editing, setEditing] = useState<Persona | null>(null);

  const loadPersonas = useCallback(() => {
    setLoading(true);
    api
      .getPersonas()
      .then(setPersonas)
      .catch((e) =>
        setError(e instanceof Error ? e.message : "Failed to load personas"),
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadPersonas();
    // Best-effort: load soul template for pre-filling the create modal
    fetch("/api/profiles/default/soul")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.content) setSoulTemplate(d.content);
      })
      .catch(() => {});
  }, [loadPersonas]);

  const handleSummon = useCallback(
    async (slug: string) => {
      const result = await api.summonPersona(slug);
      navigate(`/chat?session=${encodeURIComponent(result.session_id)}`);
    },
    [navigate],
  );

  const handleCreate = useCallback(
    async (data: PersonaCreate) => {
      await api.createPersona(data);
      loadPersonas();
    },
    [loadPersonas],
  );

  const handleEdit = useCallback(
    async (data: PersonaCreate) => {
      if (!editing) return;
      await api.updatePersona(editing.slug, data);
      setEditing(null);
      loadPersonas();
    },
    [editing, loadPersonas],
  );

  return (
    <div className="w-full min-w-0">
      {/* Header */}
      <div className="mb-6">
        <h1 className="font-mondwest text-lg tracking-[0.1em] text-midground">
          Pantheon
        </h1>
        <p className="mt-1 text-sm text-muted-foreground/70">
          Summon a model loadout. Each persona is a soul + triad.
        </p>
      </div>

      {/* Persona grid */}
      {loading ? (
        <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
          <Spinner />
          Loading personas…
        </div>
      ) : error ? (
        <div className="py-8 text-sm text-red-500">{error}</div>
      ) : personas.length === 0 ? (
        <div className="py-12 text-center text-sm text-muted-foreground/60">
          <p>No personas yet.</p>
          <p className="mt-1">
            Run{" "}
            <code className="font-mono text-xs bg-muted/40 px-1">
              hermes setup personas
            </code>{" "}
            to seed defaults, or create one below.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {personas.map((p) => (
            <PersonaCard
              key={p.slug}
              persona={p}
              onSummon={handleSummon}
              onEdit={setEditing}
            />
          ))}
        </div>
      )}

      {/* Floating create button */}
      <div className="mt-6 flex justify-end">
        <Button onClick={() => setShowCreate(true)} className="gap-2">
          <Plus className="h-4 w-4" />
          Create persona
        </Button>
      </div>

      {/* Create modal */}
      {showCreate && (
        <PersonaModal
          soulTemplate={soulTemplate}
          onSave={handleCreate}
          onClose={() => setShowCreate(false)}
        />
      )}

      {/* Edit modal */}
      {editing && (
        <PersonaModal
          initial={editing}
          soulTemplate={soulTemplate}
          onSave={handleEdit}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
