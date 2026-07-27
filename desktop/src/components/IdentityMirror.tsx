import { useEffect, useState } from "react";
import { RefreshCw, Loader2, Eye, EyeOff } from "lucide-react";
import { fetchIdentityMirror, type IdentityMirrorResponse, type IdentityClaim } from "../lib/api";

interface IdentityMirrorProps {
  sidecarReady?: boolean;
}

export function IdentityMirror({ sidecarReady = false }: IdentityMirrorProps) {
  const [mirror, setMirror] = useState<IdentityMirrorResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showEvidence, setShowEvidence] = useState(true);

  const loadMirror = async () => {
    // Only show loading state if we don't have data yet
    if (!mirror) {
      setLoading(true);
    }
    setError(null);
    try {
      const data = await fetchIdentityMirror({ limit_history: 60, include_evidence: showEvidence });
      setMirror(data);
    } catch (e) {
      console.error("Failed to load identity mirror:", e);
      // Only show error if we don't have any data
      if (!mirror) {
        setError("Failed to load identity mirror");
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // Only load mirror when sidecar is ready
    if (sidecarReady) {
      loadMirror();
    }
  }, [sidecarReady, showEvidence]);

  const renderMarkdown = (markdown: string) => {
    // Simple markdown rendering for the mirror content
    const lines = markdown.split('\n');
    return lines.map((line, idx) => {
      if (line.startsWith('## ')) {
        return <h2 key={idx} className="text-xl font-bold mt-6 mb-3">{line.replace('## ', '')}</h2>;
      }
      if (line.startsWith('### ')) {
        return <h3 key={idx} className="text-lg font-semibold mt-4 mb-2">{line.replace('### ', '')}</h3>;
      }
      if (line.startsWith('#### ')) {
        return <h4 key={idx} className="text-base font-medium mt-3 mb-2">{line.replace('#### ', '')}</h4>;
      }
      if (line.startsWith('- ')) {
        return <li key={idx} className="ml-4 mb-1">{line.replace('- ', '')}</li>;
      }
      if (line.startsWith('  - ')) {
        return <li key={idx} className="ml-8 mb-1 text-sm text-muted-foreground">{line.replace('  - ', '')}</li>;
      }
      if (line.startsWith('**') && line.endsWith('**')) {
        return <p key={idx} className="font-semibold mb-2">{line.slice(2, -2)}</p>;
      }
      if (line.startsWith('_') && line.endsWith('_')) {
        return <p key={idx} className="italic text-muted-foreground mb-2">{line.slice(1, -1)}</p>;
      }
      if (line.trim() === '') {
        return <br key={idx} />;
      }
      return <p key={idx} className="mb-2">{line}</p>;
    });
  };

  return (
    <div className="rounded-2xl border border-border bg-card p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="inline-flex items-center gap-1.5 font-medium">
          <RefreshCw className="size-4 text-primary" /> Identity Mirror
        </h2>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowEvidence(!showEvidence)}
            className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-accent"
            title={showEvidence ? "Hide evidence" : "Show evidence"}
          >
            {showEvidence ? <Eye className="size-4" /> : <EyeOff className="size-4" />}
          </button>
          <button
            onClick={loadMirror}
            disabled={loading}
            className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
          >
            {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
          </button>
        </div>
      </div>

      {loading && !mirror ? (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : error ? (
        <div className="text-center py-8">
          <p className="text-sm text-muted-foreground">{error}</p>
          <button
            onClick={loadMirror}
            className="mt-2 text-sm text-primary hover:underline"
          >
            Retry
          </button>
        </div>
      ) : mirror ? (
        <div className="space-y-4">
          <div className="prose prose-sm dark:prose-invert max-w-none">
            {renderMarkdown(mirror.markdown)}
          </div>

          {mirror.history_count > 0 && (
            <div className="mt-6 pt-4 border-t border-border">
              <h3 className="text-sm font-medium mb-2">Recent History ({mirror.history_count} changes)</h3>
              <div className="space-y-2 max-h-64 overflow-y-auto">
                {mirror.history.map((claim: IdentityClaim) => (
                  <div key={claim.claim_id} className="rounded-lg bg-muted/30 p-3 text-xs">
                    <div className="flex items-start justify-between mb-1">
                      <span className="font-medium">{claim.kind}</span>
                      <span className="text-muted-foreground">{claim.status}</span>
                    </div>
                    <p className="text-muted-foreground line-clamp-2">{claim.text}</p>
                    {claim.superseded_by && (
                      <p className="text-muted-foreground mt-1">
                        Superseded by: {claim.superseded_by}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
