"use client";

import { useState, useRef, useEffect } from "react";
import { Search, X, Clock, AlertCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { withBasePath } from "@/lib/base-path";

interface SearchResult {
  meeting: {
    id: number;
    platform: string;
    native_id: string;
    status: string;
    start_time?: string;
    data?: Record<string, unknown>;
  };
  matched_segments: Array<{
    meeting_id: number;
    segment_id: string | null;
    text: string;
    speaker: string;
    start_time: number;
    end_time: number;
    timestamp: string;
  }>;
}

type SearchState = "idle" | "loading" | "results" | "empty" | "error";

interface SearchBarProps {
  onSearch?: (query: string) => Promise<SearchResult[]>;
}

export function SearchBar({ onSearch }: SearchBarProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [state, setState] = useState<SearchState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setActive(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const defaultSearch = async (value: string): Promise<SearchResult[]> => {
    const res = await fetch(withBasePath(`/api/vexa/internal/search?q=${encodeURIComponent(value)}`));
    if (!res.ok) {
      if (res.status === 401)
        throw new Error("Authentication required — please sign in to search.");
      throw new Error(`Search failed (${res.status})`);
    }
    const data = await res.json();
    return data.results || [];
  };

  const handleSearch = async () => {
    if (!query.trim()) return;
    setState("loading");
    setError(null);
    setActive(true);
    try {
      const resp = await (onSearch || defaultSearch)(query);
      setResults(resp);
      setState(resp.length > 0 ? "results" : "empty");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
      setState("error");
      setResults([]);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") handleSearch();
    if (e.key === "Escape") {
      setQuery("");
      setState("idle");
      setActive(false);
    }
  };

  return (
    <div ref={ref} className="relative w-full">
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            onFocus={() => query && setActive(true)}
            placeholder="Search meetings, transcripts, highlights..."
            className="w-full pl-9 pr-3 py-2 text-sm border rounded-md bg-background"
          />
          {query && (
            <button
              onClick={() => { setQuery(""); setState("idle"); setActive(false); }}
              className="absolute right-2 top-1/2 -translate-y-1/2"
            >
              <X className="h-3.5 w-3.5 text-muted-foreground" />
            </button>
          )}
        </div>
        <button
          onClick={handleSearch}
          disabled={!query.trim() || state === "loading"}
          className="px-3 py-2 text-sm bg-primary text-primary-foreground rounded-md disabled:opacity-50"
        >
          {state === "loading" ? "Searching…" : "Search"}
        </button>
      </div>

      {active && state !== "idle" && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-popover border rounded-md shadow-lg max-h-[600px] overflow-auto z-50">
          {state === "loading" && (
            <div className="p-6 text-center text-sm text-muted-foreground">
              Searching…
            </div>
          )}
          {state === "empty" && (
            <div className="p-6 text-center text-sm text-muted-foreground">
              No results found for &ldquo;{query}&rdquo;
            </div>
          )}
          {state === "error" && (
            <div className="p-4 flex items-start gap-2 text-sm text-destructive">
              <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
              <span>{error ?? "Search failed"}</span>
            </div>
          )}
          {state === "results" && results.map(r => (
            <div key={r.meeting.id} className="p-3 border-b last:border-0">
              <div className="flex items-center gap-2 mb-1">
                <Badge variant="secondary" className="text-xs">{r.meeting.platform}</Badge>
                <span className="text-xs text-muted-foreground">{r.meeting.native_id}</span>
                {r.meeting.start_time && <Clock className="h-3 w-3 text-muted-foreground" />}
              </div>
              {r.matched_segments.slice(0, 2).map((seg, i) => {
                const link = withBasePath(
                  `/meetings/${seg.meeting_id}` +
                  (seg.segment_id ? `?segment=${encodeURIComponent(seg.segment_id)}` : ""),
                );
                return (
                  <a key={i} href={link} className="block text-xs hover:underline mb-1">
                    <span className="font-mono text-muted-foreground">{seg.timestamp}</span>
                    {seg.speaker && <span className="font-medium"> {seg.speaker}:</span>}
                    <span className="text-muted-foreground">
                      {" "}
                      {seg.text.substring(0, 120)}
                      {seg.text.length > 120 ? "…" : ""}
                    </span>
                  </a>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
