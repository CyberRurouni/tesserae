'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Spinner } from '@/components/ui';
import { cn } from '@/lib/utils';

/**
 * The harvest picker.
 *
 * Harvest mode is the one part of a run the user genuinely decides: it
 * re-searches keywords that have already been searched, purely to catch ads
 * posted since. Which keywords get that second pass is a judgement about where
 * new opportunities will appear, and the person running the search knows more
 * about their market than the pipeline does.
 *
 * Two ways to say it, and both are offered because they answer different
 * questions:
 *
 *   "harvest the top 20" — where should the system's judgement be trusted?
 *   "these four"          — where do I actually want to look this week?
 *
 * The list is shown ranked best-first so the top N the backend would pick are
 * visibly the top N, and the resolved set is previewed live rather than left
 * as a guess until the run starts.
 *
 * When harvest is inactive the panel says so plainly instead of hiding: the
 * setting only takes effect once the coverage reviewer decides the profile's
 * skills are fully covered, and a control that silently does nothing is worse
 * than one that explains itself.
 */

function Stat({ value, label, tone }) {
  return (
    <span className="inline-flex items-baseline gap-1 text-[11px]">
      <span className={cn('font-semibold tabular-nums', tone)}>{value}</span>
      <span className="text-muted-foreground">{label}</span>
    </span>
  );
}

export default function HarvestPanel({ limit, onLimitChange, keywords, onKeywordsChange }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [preview, setPreview] = useState([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/keywords', { cache: 'no-store' });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || body.error || 'Could not load');
      setData(body);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Live preview: the backend resolves the selection with the exact same
  // function the orchestrator uses, so what is shown here is what gets searched.
  useEffect(() => {
    if (!data) return undefined;
    let cancelled = false;
    const handle = setTimeout(async () => {
      try {
        const res = await fetch('/api/keywords', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ limit, keywords: keywords.length ? keywords : null }),
        });
        const body = await res.json();
        if (!cancelled && res.ok) setPreview(body.keywords || []);
      } catch {
        if (!cancelled) setPreview([]);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [limit, keywords, data]);

  const items = useMemo(() => data?.items || [], [data]);
  const selectedSet = useMemo(() => new Set(keywords), [keywords]);
  const explicit = keywords.length > 0;

  const toggle = useCallback(
    (keyword) => {
      const next = selectedSet.has(keyword)
        ? keywords.filter((k) => k !== keyword)
        : [...keywords, keyword];
      onKeywordsChange(next);
    },
    [keywords, selectedSet, onKeywordsChange]
  );

  if (loading) {
    return (
      <Card>
        <CardContent className="flex justify-center py-8">
          <Spinner />
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <CardContent className="py-6">
          <p className="text-sm text-destructive">{error}</p>
        </CardContent>
      </Card>
    );
  }

  const inactive = !data?.harvest_active;

  return (
    <Card className={cn('border-border/60', inactive && 'opacity-90')}>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle>Harvest</CardTitle>
            <CardDescription>
              {inactive
                ? 'Not active yet. Once Tesserae decides this profile’s skills are fully covered, this search re-searches your best keywords for ads posted since instead of generating new ones.'
                : 'Active. This run re-searches keywords you already used, to catch ads posted since. Choose how many of your best, or pick specific ones.'}
            </CardDescription>
          </div>
          {data?.harvest_active && (
            <Badge variant="info">Active · {data.searchable} available</Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-5">
        {/* How many */}
        <div className="space-y-2">
          <p className="text-sm font-semibold text-foreground">Re-search the top</p>
          <div className="flex flex-wrap items-center gap-2">
            {(data?.quick_limits || [10, 20]).map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => {
                  onLimitChange(n);
                  onKeywordsChange([]);
                }}
                aria-pressed={!explicit && limit === n}
                className={cn(
                  'h-9 rounded-lg border px-3 text-sm font-medium transition-colors',
                  !explicit && limit === n
                    ? 'border-primary bg-primary/10 text-primary'
                    : 'border-border text-muted-foreground hover:bg-accent/40 hover:text-foreground'
                )}
              >
                {n}
              </button>
            ))}
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min={0}
                max={200}
                value={limit}
                onChange={(e) => {
                  onLimitChange(Math.max(0, Number(e.target.value) || 0));
                  onKeywordsChange([]);
                }}
                className="h-9 w-20"
                aria-label="Harvest this many keywords"
              />
              <span className="text-xs text-muted-foreground">keywords</span>
            </div>
            {explicit && (
              <span className="text-xs text-muted-foreground">
                Using your own {keywords.length} selection
              </span>
            )}
          </div>
        </div>

        {/* The resolved set — what will actually be searched */}
        <div className="rounded-xl border border-border/60 bg-muted/30 px-4 py-3">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            This run will re-search {preview.length} keyword{preview.length === 1 ? '' : 's'}
          </p>
          <p className="mt-1 text-sm text-foreground">
            {preview.length ? preview.join(' · ') : 'None — nothing selected.'}
          </p>
          {data?.notes?.length > 0 && (
            <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
              {data.notes.map((n) => (
                <li key={n}>· {n}</li>
              ))}
            </ul>
          )}
        </div>

        {/* Pick them yourself */}
        {items.length > 0 ? (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <p className="text-sm font-semibold text-foreground">
                Your pool, ranked by leads
              </p>
              <button
                type="button"
                onClick={() =>
                  onKeywordsChange(explicit ? [] : preview.slice(0, limit || 20))
                }
                className="text-xs font-semibold text-primary hover:underline"
              >
                {explicit ? 'Back to ranking' : `Pick the top ${limit || 20}`}
              </button>
            </div>
            <ul className="max-h-72 divide-y divide-border overflow-y-auto rounded-xl border border-border/60">
              {items.map((item) => {
                const checked = selectedSet.has(item.keyword);
                return (
                  <li key={item.keyword}>
                    <label className="flex cursor-pointer items-center gap-3 px-4 py-2.5 transition-colors hover:bg-accent/30">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(item.keyword)}
                        className="h-4 w-4 shrink-0 accent-[hsl(var(--primary))]"
                        aria-label={`Harvest ${item.keyword}`}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm text-foreground">
                          {item.keyword}
                        </span>
                      </span>
                      <span className="flex shrink-0 items-center gap-3">
                        <Stat value={item.relevant_ads} label="leads" tone="text-success" />
                        <Stat value={item.ads_fetched} label="ads" />
                        {item.rank === 1 && <Badge variant="outline">top</Badge>}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            No searchable keywords yet — harvest becomes available after the first run.
          </p>
        )}

        {data?.cooling_down?.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {data.cooling_down.length} more keyword
            {data.cooling_down.length === 1 ? ' is' : 's are'} cooling down and cannot be
            re-searched until their next date.
          </p>
        )}
      </CardContent>
    </Card>
  );
}