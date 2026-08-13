import { useCallback, useMemo, useState } from "react";
import { FolderOpen, Loader2, Search } from "lucide-react";
import {
  apiErrorDetail,
  resolveRevealPath,
  revealInFinder,
  search,
  type SearchHit,
} from "../lib/api";

const RECENT_KEY = "minion:recent_searches";
const MAX_RECENT = 8;

function loadRecent(): string[] {
  try {
    const raw = sessionStorage.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function saveRecent(query: string): void {
  const trimmed = query.trim();
  if (!trimmed) return;
  const next = [trimmed, ...loadRecent().filter((q) => q !== trimmed)].slice(0, MAX_RECENT);
  try {
    sessionStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* ignore quota */
  }
}

function snippet(text: string, max = 220): string {
  const t = text.replace(/\s+/g, " ").trim();
  return t.length <= max ? t : `${t.slice(0, max)}…`;
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

type MemorySearchProps = {
  profileId?: string | null;
  profileName?: string | null;
};

export function MemorySearch({ profileId, profileName }: MemorySearchProps) {
  const [query, setQuery] = useState("");
  const [searchAllProfiles, setSearchAllProfiles] = useState(false);
  const [results, setResults] = useState<SearchHit[]>([]);
  const [searchedAllProfiles, setSearchedAllProfiles] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recent, setRecent] = useState<string[]>(() => loadRecent());
  const [revealError, setRevealError] = useState<string | null>(null);

  const profileIdsInResults = useMemo(() => {
    const ids = new Set(results.map((r) => r.profile_id).filter(Boolean));
    return ids;
  }, [results]);

  const runSearch = useCallback(
    async (raw: string) => {
      const trimmed = raw.trim();
      if (!trimmed) return;
      setQuery(trimmed);
      setLoading(true);
      setError(null);
      try {
        const res = await search({
          query: trimmed,
          top_k: 12,
          ...(searchAllProfiles
            ? { all_profiles: true }
            : { profile_id: profileId ?? undefined }),
        });
        setResults(res.results);
        setSearchedAllProfiles(Boolean(res.all_profiles ?? searchAllProfiles));
        saveRecent(trimmed);
        setRecent(loadRecent());
      } catch (e) {
        setError(apiErrorDetail(e));
        setResults([]);
        setSearchedAllProfiles(false);
      } finally {
        setLoading(false);
      }
    },
    [profileId, searchAllProfiles],
  );

  async function handleReveal(path: string) {
    setRevealError(null);
    try {
      const resolved = await resolveRevealPath(path);
      await revealInFinder(resolved.reveal_path);
    } catch (e) {
      setRevealError(apiErrorDetail(e));
    }
  }

  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-1.5 font-medium">
          <Search className="size-4 text-primary" /> Search memory
        </h2>
        {profileName && !searchAllProfiles && (
          <span className="text-xs text-muted-foreground">Profile: {profileName}</span>
        )}
        {searchAllProfiles && (
          <span className="text-xs text-violet-700 dark:text-violet-300">All profiles</span>
        )}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {searchAllProfiles
          ? "Searching across every profile on this machine (local UI only)."
          : "Ask your vault — semantic + keyword search on this profile."}
      </p>
      <label className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
        <input
          type="checkbox"
          checked={searchAllProfiles}
          onChange={(e) => setSearchAllProfiles(e.target.checked)}
          className="rounded border-border"
        />
        Search all profiles
      </label>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void runSearch(query);
        }}
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="What do you remember about…"
          className="min-w-0 flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        <button
          type="submit"
          disabled={loading || !query.trim()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {loading ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
          Search
        </button>
      </form>

      {recent.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {recent.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => void runSearch(q)}
              className="rounded-full border border-border bg-muted/50 px-2.5 py-0.5 text-xs hover:bg-accent"
            >
              {q}
            </button>
          ))}
        </div>
      )}

      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      {revealError && <p className="mt-2 text-xs text-amber-700">{revealError}</p>}

      {searchedAllProfiles && profileIdsInResults.size > 1 && (
        <p className="mt-2 text-xs text-violet-700 dark:text-violet-300">
          Results span {profileIdsInResults.size} profiles.
        </p>
      )}

      {results.length > 0 && (
        <ul className="mt-4 divide-y divide-border rounded-lg border border-border">
          {results.map((hit) => (
            <li key={hit.chunk_id} className="p-3 text-sm">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate font-medium">{baseName(hit.path)}</p>
                    {searchedAllProfiles && hit.profile_id && (
                      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                        {hit.profile_id}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground line-clamp-3">{snippet(hit.text)}</p>
                </div>
                <div className="shrink-0 text-right">
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {(hit.score * 100).toFixed(0)}%
                  </span>
                  <button
                    type="button"
                    onClick={() => void handleReveal(hit.path)}
                    className="mt-1 flex items-center gap-1 text-xs text-primary hover:underline"
                  >
                    <FolderOpen className="size-3" /> Reveal
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
