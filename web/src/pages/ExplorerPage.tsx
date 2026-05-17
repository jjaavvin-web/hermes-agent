import { useEffect, useRef, useState } from "react";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useTheme } from "@/themes";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

export default function ExplorerPage() {
  const { t } = useI18n();
  const { theme } = useTheme();
  const { setEnd } = usePageHeader();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    setEnd(null);
    return () => setEnd(null);
  }, [setEnd]);

  const handleLoad = () => {
    setLoading(false);
    setError(false);
    try {
      iframeRef.current?.contentWindow?.postMessage(
        { type: "hermes-theme", tokens: theme },
        "*",
      );
    } catch {
      // postMessage is harmless even if the frame is cross-origin
    }
  };

  const handleError = () => {
    setLoading(false);
    setError(true);
  };

  return (
    <div className="relative flex-1 min-h-0 flex flex-col w-full">
      {loading && !error && (
        <div className="absolute inset-0 flex items-center justify-center z-10 bg-background/60">
          <Spinner className="text-2xl text-primary" />
        </div>
      )}
      {error && (
        <div className="flex items-center justify-center flex-1 text-sm text-destructive">
          {t.explorer.title} failed to load — check that the GitNexus service is
          running.
        </div>
      )}
      <iframe
        ref={iframeRef}
        src="/explorer/"
        sandbox="allow-scripts allow-same-origin allow-forms"
        onLoad={handleLoad}
        onError={handleError}
        className="flex-1 min-h-0 w-full border-0"
        style={{ display: error ? "none" : "block" }}
        title={t.explorer.title}
      />
    </div>
  );
}
