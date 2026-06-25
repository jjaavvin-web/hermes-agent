import {
  type ComponentPropsWithoutRef,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type LabelHTMLAttributes,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn, themedBody } from "@/lib/utils";

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("border border-border bg-card text-card-foreground shadow-sm", className)}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1.5 p-4", className)} {...props} />;
}

export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3
      className={cn("font-mondwest text-display text-sm tracking-[0.12em]", className)}
      {...props}
    />
  );
}

export function CardDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-xs text-muted-foreground", className)} {...props} />;
}

export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4 pt-0", className)} {...props} />;
}

export function Input({ className, type = "text", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      type={type}
      className={cn(
        "flex h-9 w-full border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors",
        "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn("text-xs font-medium text-muted-foreground", className)}
      {...props}
    />
  );
}

export function Separator({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("h-px w-full bg-border", className)} {...props} />;
}

interface ToastState {
  message: string;
  type?: "success" | "error" | "info";
}

export function Toast({ toast }: { toast: ToastState | null }) {
  if (!toast) return null;

  return createPortal(
    <div className={cn(themedBody, "fixed bottom-4 right-4 z-[250] pointer-events-none")}>
      <div
        role="status"
        className={cn(
          "max-w-sm border px-4 py-3 text-sm shadow-2xl backdrop-blur-sm",
          toast.type === "error"
            ? "border-destructive/50 bg-destructive/15 text-destructive"
            : "border-border bg-popover text-popover-foreground",
        )}
      >
        {toast.message}
      </div>
    </div>,
    document.body,
  );
}

export function useToast() {
  const [toast, setToast] = useState<ToastState | null>(null);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 4000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const showToast = useCallback((message: string, type: ToastState["type"] = "info") => {
    setToast({ message, type });
  }, []);

  return { toast, showToast };
}

interface ConfirmDeleteOptions<T> {
  onDelete: (id: T) => Promise<void> | void;
}

export function useConfirmDelete<T = string>({ onDelete }: ConfirmDeleteOptions<T>) {
  const [pendingId, setPendingId] = useState<T | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const requestDelete = useCallback((id: T) => setPendingId(id), []);
  const cancel = useCallback(() => {
    if (!isDeleting) setPendingId(null);
  }, [isDeleting]);
  const confirm = useCallback(async () => {
    if (pendingId === null) return;
    setIsDeleting(true);
    try {
      await onDelete(pendingId);
      setPendingId(null);
    } finally {
      setIsDeleting(false);
    }
  }, [onDelete, pendingId]);

  return {
    pendingId,
    isOpen: pendingId !== null,
    isDeleting,
    requestDelete,
    cancel,
    confirm,
  };
}

interface ConfirmDialogProps {
  cancelLabel?: string;
  confirmLabel?: string;
  description?: string;
  destructive?: boolean;
  loading?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  open: boolean;
  title: string;
}

export function ConfirmDialog({
  cancelLabel = "Cancel",
  confirmLabel = "Confirm",
  description,
  destructive = false,
  loading = false,
  onCancel,
  onConfirm,
  open,
  title,
}: ConfirmDialogProps) {
  if (!open) return null;

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      className={cn(
        themedBody,
        "fixed inset-0 z-[220] flex items-center justify-center bg-background/85 p-4 backdrop-blur-sm",
      )}
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="w-full max-w-md border border-border bg-card shadow-2xl">
        <div className="border-b border-border p-4">
          <h2 className="font-mondwest text-display text-base tracking-wider">{title}</h2>
          {description ? (
            <p className="mt-1 whitespace-pre-line text-xs leading-relaxed text-muted-foreground">
              {description}
            </p>
          ) : null}
        </div>
        <div className="flex justify-end gap-2 p-3">
          <Button type="button" outlined onClick={onCancel} disabled={loading}>
            {cancelLabel}
          </Button>
          <Button
            type="button"
            destructive={destructive}
            onClick={onConfirm}
            disabled={loading}
          >
            {loading ? "…" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

interface DialogProps {
  children: ReactNode;
  onOpenChange?: (open: boolean) => void;
  open: boolean;
}

export function Dialog({ children, onOpenChange, open }: DialogProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onOpenChange?.(false);
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onOpenChange, open]);

  if (!open) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-[210] flex items-center justify-center bg-background/85 p-4 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onOpenChange?.(false);
      }}
    >
      {children}
    </div>,
    document.body,
  );
}

export function DialogContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      className={cn(
        themedBody,
        "relative w-full max-w-lg max-h-[85vh] overflow-y-auto border border-border bg-card p-5 shadow-2xl",
        className,
      )}
      {...props}
    />
  );
}

export function DialogHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mb-4 flex flex-col gap-1.5", className)} {...props} />;
}

export function DialogTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2
      className={cn("font-mondwest text-display text-base tracking-wider", className)}
      {...props}
    />
  );
}

export function DialogDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-xs leading-relaxed text-muted-foreground", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mt-4 flex justify-end gap-2", className)} {...props} />;
}

interface BottomSheetProps {
  backdropDismissLabel?: string;
  children: ReactNode;
  onClose: () => void;
  open: boolean;
  title: string;
}

export function BottomSheet({ backdropDismissLabel = "Close", children, onClose, open, title }: BottomSheetProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, open]);

  if (!open) return null;

  return createPortal(
    <div
      aria-label={backdropDismissLabel}
      className="fixed inset-0 z-[205] flex items-end bg-background/75 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        className={cn(
          themedBody,
          "w-full max-h-[80vh] overflow-y-auto border-t border-border bg-popover shadow-2xl",
        )}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-popover px-4 py-3">
          <h2 className="font-mondwest text-display text-sm tracking-[0.12em]">{title}</h2>
          <Button ghost size="icon" type="button" onClick={onClose} aria-label={backdropDismissLabel}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className="p-2">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export function useBelowBreakpoint(breakpoint: number | string): boolean {
  const query = normalizeBelowBreakpointQuery(breakpoint);
  const [matches, setMatches] = useState(() => getQueryMatch(query));

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      setMatches(false);
      return;
    }
    const mediaQuery = window.matchMedia(query);
    const update = () => setMatches(mediaQuery.matches);
    update();
    if (typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", update);
      return () => mediaQuery.removeEventListener("change", update);
    }
    mediaQuery.addListener(update);
    return () => mediaQuery.removeListener(update);
  }, [query]);

  return matches;
}

function normalizeBelowBreakpointQuery(breakpoint: number | string): string {
  if (typeof breakpoint === "number") {
    return `(max-width: ${Math.max(0, breakpoint - 0.02)}px)`;
  }
  const value = breakpoint.trim();
  const px = Number.parseFloat(value);
  if (/^-?\d+(?:\.\d+)?px$/.test(value) && Number.isFinite(px)) {
    return `(max-width: ${Math.max(0, px - 0.02)}px)`;
  }
  return `(max-width: ${value})`;
}

function getQueryMatch(query: string): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia(query).matches;
}

export type ButtonProps = ComponentPropsWithoutRef<typeof Button>;
