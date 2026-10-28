'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { StatusLabel } from '@/components/runs/StatusLabel';
import ExpandableText from '@/components/runs/ExpandableText';
import LiveLog from '@/components/runs/LiveLog';
import { isTerminalStatus } from '@/lib/runs';
import { cn } from '@/lib/utils';

const CONFIDENCE_STYLES = (value) => {
  if (value >= 0.8) return 'bg-success/15 text-success';
  if (value >= 0.5) return 'bg-warning/15 text-warning';
  return 'bg-destructive/15 text-destructive';
};

// Seen is a property of the ad, not of the run: the same ad can appear in
// several runs and you only want to review it once. It is kept in localStorage
// rather than the run file so marking an ad seen never rewrites results.
const SEEN_STORAGE_KEY = 'tesserae.seenAds';

const SEEN_FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'unseen', label: 'Unseen' },
  { value: 'seen', label: 'Seen' },
];

const HELD_KIND_LABELS = {
  borderline: 'Borderline',
  very_low: 'Extremely low confidence',
};

function heldKindLabel(kind) {
  return HELD_KIND_LABELS[kind] || 'Below threshold';
}

function thresholdLabel(value) {
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : null;
}

function adKey(ad, index) {
  return ad.ad_archive_id
    ? `id:${ad.ad_archive_id}`
    : `row:${ad.advertiser_page_url || ad.advertiser_name || 'unknown'}:${index}`;
}

function readSeenIds() {
  if (typeof window === 'undefined') return new Set();
  try {
    const raw = JSON.parse(window.localStorage.getItem(SEEN_STORAGE_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw : []);
  } catch {
    return new Set();
  }
}

function persistSeenIds(seen) {
  try {
    window.localStorage.setItem(SEEN_STORAGE_KEY, JSON.stringify([...seen]));
  } catch {
    // Disabled or full storage must not break the page; it only costs memory.
  }
}

const POLL_INTERVAL_MS = 3000;
// How often to re-read the run after it went terminal but no result file has
// appeared yet, and how often to re-ask when the run is unknown so far.
const TERMINAL_GRACE_POLLS = 6;
const MISSING_POLLS = 5;

const newPollState = () => ({
  status: 'starting',
  resultsSaved: false,
  terminalPolls: 0,
  missingPolls: 0,
  polling: true,
});

function EmptyState({ message, hint }) {
  return (
    <div className="px-6 py-10 text-center">
      <p className="text-sm text-muted-foreground">{message}</p>
      {hint && <p className="mt-1.5 text-xs text-muted-foreground/70">{hint}</p>}
    </div>
  );
}

function adUrl(ad) {
  return ad.ad_archive_id
    ? `https://www.facebook.com/ads/library/?id=${ad.ad_archive_id}`
    : ad.advertiser_page_url;
}

function ConfidenceBadge({ value }) {
  if (typeof value !== 'number') {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <span
      className={cn(
        'inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-semibold',
        CONFIDENCE_STYLES(value)
      )}
    >
      {Math.round(value * 100)}%
    </span>
  );
}

function WhyCell({ ad, bucket }) {
  const threshold = thresholdLabel(ad.held_threshold);
  const note =
    bucket === 'held'
      ? `${heldKindLabel(ad.held_kind)}${threshold ? ` · below ${threshold}` : ''}`
      : bucket === 'deferred' || bucket === 'rejected'
        ? ad.ad_category
        : bucket === 'ai_failed'
          ? 'Retried next run'
          : null;

  return (
    <div className="space-y-1">
      {note && (
        <span
          className={cn(
            'inline-flex max-w-full break-words rounded-full px-2 py-0.5 text-[10px] font-semibold',
            bucket === 'held'
              ? 'bg-warning/15 text-warning'
              : bucket === 'ai_failed'
                ? 'bg-muted text-muted-foreground'
                : 'bg-info/15 text-info'
          )}
        >
          {note}
        </span>
      )}
      {ad.relevance_reason ? (
        <p className="line-clamp-3 break-words text-xs text-muted-foreground" title={ad.relevance_reason}>
          {ad.relevance_reason}
        </p>
      ) : !note ? (
        <span className="text-xs text-muted-foreground">—</span>
      ) : null}
    </div>
  );
}

function ResultTable({ rows, bucket, showSeen, onToggleSeen }) {
  const whyLabel =
    bucket === 'accepted'
      ? 'Why accepted'
      : bucket === 'held'
        ? 'Why held'
        : bucket === 'deferred'
          ? 'Why held back'
          : bucket === 'rejected'
            ? 'Why rejected'
            : 'Why not judged';
  const headings = showSeen
    ? ['Seen', 'Advertiser', 'Keyword', 'Headline', 'Ad text', whyLabel, 'Confidence', 'Link']
    : ['Advertiser', 'Keyword', 'Headline', 'Ad text', whyLabel, 'Confidence', 'Link'];
  const widths = showSeen
    ? ['5%', '15%', '10%', '10%', '29%', '19%', '6%', '6%']
    : ['17%', '11%', '11%', '31%', '19%', '6%', '5%'];

  return (
    <>
      <div className="hidden md:block">
        <table className="w-full table-fixed text-sm">
          <colgroup>
            {widths.map((width, index) => (
              <col key={index} style={{ width }} />
            ))}
          </colgroup>
          <thead className="border-b border-border">
            <tr>
              {headings.map((heading) => (
                <th
                  key={heading}
                  className="px-3 py-2.5 text-left text-[11px] font-medium uppercase tracking-wide text-muted-foreground"
                >
                  {heading}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map(({ ad, key, seen }) => {
              const url = adUrl(ad);
              const label = ad.advertiser_name || 'this ad';
              return (
                <tr
                  key={ad.ad_archive_id || key}
                  className={cn('align-top transition-colors hover:bg-accent/40', seen && 'bg-success/5')}
                >
                  {showSeen && (
                    <td className="px-3 py-3">
                      <input
                        type="checkbox"
                        checked={seen}
                        onChange={() => onToggleSeen(key)}
                        aria-label={seen ? `Mark ${label} as unseen` : `Mark ${label} as seen`}
                        title={seen ? 'Mark as unseen' : 'Mark as seen'}
                        className="h-4 w-4 cursor-pointer accent-[hsl(var(--primary))]"
                      />
                    </td>
                  )}
                  <td className="break-words px-3 py-3 font-medium text-foreground">
                    {ad.advertiser_name || '—'}
                  </td>
                  <td className="break-words px-3 py-3 text-muted-foreground">
                    {ad.source_keyword || '—'}
                  </td>
                  <td className="break-words px-3 py-3">{ad.cta_text || '—'}</td>
                  <td className="px-3 py-3 text-muted-foreground">
                    <ExpandableText
                      text={ad.ad_text}
                      collapsedLines={2}
                      className="break-words text-[13px] leading-relaxed"
                    />
                  </td>
                  <td className="px-3 py-3">
                    <WhyCell ad={ad} bucket={bucket} />
                  </td>
                  <td className="px-3 py-3">
                    <ConfidenceBadge value={ad.relevance_confidence} />
                  </td>
                  <td className="px-3 py-3">
                    {url ? (
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="Open this ad on Meta"
                        className="break-words text-xs font-medium text-primary hover:underline"
                      >
                        View ad ↗
                      </a>
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="divide-y divide-border md:hidden">
        {rows.map(({ ad, key, seen }) => {
          const url = adUrl(ad);
          const label = ad.advertiser_name || 'this ad';
          return (
            <div
              key={ad.ad_archive_id || key}
              className={cn('space-y-2.5 p-4', seen && 'bg-success/5')}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 space-y-1">
                  <p className="break-words text-sm font-medium text-foreground">
                    {ad.advertiser_name || 'Unnamed advertiser'}
                  </p>
                  <p className="break-words text-xs text-muted-foreground">
                    {[ad.source_keyword, ad.cta_text].filter(Boolean).join(' · ') || '—'}
                  </p>
                </div>
                <ConfidenceBadge value={ad.relevance_confidence} />
              </div>
              <ExpandableText
                text={ad.ad_text}
                collapsedLines={2}
                className="break-words text-[13px] leading-relaxed"
              />
              <WhyCell ad={ad} bucket={bucket} />
              <div className="flex flex-wrap items-center justify-between gap-2">
                {showSeen ? (
                  <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={seen}
                      onChange={() => onToggleSeen(key)}
                      aria-label={seen ? `Mark ${label} as unseen` : `Mark ${label} as seen`}
                      className="h-4 w-4 cursor-pointer accent-[hsl(var(--primary))]"
                    />
                    Seen
                  </label>
                ) : (
                  <span />
                )}
                {url && (
                  <a
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="Open this ad on Meta"
                    className="text-xs font-medium text-primary hover:underline"
                  >
                    View ad ↗
                  </a>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

function ResultBucket({
  bucket,
  title,
  description,
  count,
  rows,
  tone,
  showSeen = false,
  onToggleSeen,
  actions,
  emptyState = <EmptyState message="No ads in this outcome." hint="This bucket is empty for this run." />,
  defaultLimit = 0,
}) {
  const [expanded, setExpanded] = useState(false);
  const limit = defaultLimit || rows.length;
  const visible = expanded ? rows : rows.slice(0, limit);

  return (
    <Card id={`bucket-${bucket}`} className="scroll-mt-28 overflow-hidden">
      <CardHeader className="flex flex-col gap-3 space-y-0 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className={cn('h-2 w-2 rounded-full', tone)} aria-hidden="true" />
            <CardTitle className="text-lg">
              {title} ({count})
            </CardTitle>
          </div>
          {description && (
            <p className="max-w-3xl text-xs leading-relaxed text-muted-foreground">{description}</p>
          )}
        </div>
        {actions}
      </CardHeader>
      <CardContent className="p-0">
        {rows.length === 0 ? (
          emptyState
        ) : (
          <>
            <ResultTable
              rows={visible}
              bucket={bucket}
              showSeen={showSeen}
              onToggleSeen={onToggleSeen}
            />
            {defaultLimit > 0 && rows.length > defaultLimit && (
              <div className="border-t border-border px-4 py-2.5 text-center">
                <button
                  type="button"
                  onClick={() => setExpanded((value) => !value)}
                  className="text-xs font-semibold text-primary hover:underline"
                >
                  {expanded ? 'Show fewer' : `Show all ${rows.length}`}
                </button>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** A keyword that matched nothing still belongs in the list — mark it quietly. */
function KeywordChip({ entry }) {
  const keyword = typeof entry === 'string' ? entry : entry.keyword;
  const acceptedCount = typeof entry?.accepted_count === 'number' ? entry.accepted_count : null;
  const foundNothing = acceptedCount === 0;

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium',
        foundNothing
          ? 'border-border bg-transparent text-muted-foreground'
          : 'border-border bg-secondary text-foreground'
      )}
    >
      {keyword}
      {acceptedCount === null ? null : foundNothing ? (
        <span className="italic text-muted-foreground/70">no ads</span>
      ) : (
        <span className="text-success">{acceptedCount} accepted</span>
      )}
    </span>
  );
}

export function RunDetail({ runId }) {
  const [run, setRun] = useState(null);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [missing, setMissing] = useState(false);
  // The poll loop reads its bookkeeping from a ref, but `run`/`missing` are
  // render state, so the effect below re-evaluates after every poll.
  const pollRef = useRef(newPollState());
  const [seenIds, setSeenIds] = useState(() => new Set());
  const [seenFilter, setSeenFilter] = useState('all');
  const [activeBucket, setActiveBucket] = useState('accepted');

  // Read storage after mount: the server has no localStorage, and doing this
  // during the first render would make the markup disagree with the client.
  useEffect(() => {
    setSeenIds(readSeenIds());
  }, []);

  const toggleSeen = useCallback((key) => {
    setSeenIds((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      persistSeenIds(next);
      return next;
    });
  }, []);

  const setSeenFor = useCallback((keys, value) => {
    setSeenIds((prev) => {
      const next = new Set(prev);
      for (const key of keys) {
        if (value) next.add(key);
        else next.delete(key);
      }
      persistSeenIds(next);
      return next;
    });
  }, []);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`/api/runs/${runId}`, { cache: 'no-store' });

      if (res.status === 404) {
        // Neither the backend nor the disk knows this run. That can be a
        // genuinely bad id, or a run whose in-memory state we just missed —
        // retry a few times before giving up.
        const missingPolls = pollRef.current.missingPolls + 1;
        pollRef.current = {
          ...pollRef.current,
          missingPolls,
          polling: missingPolls <= MISSING_POLLS,
        };
        setMissing(missingPolls > MISSING_POLLS);
        return;
      }

      if (!res.ok) throw new Error(`Request failed with ${res.status}`);

      const data = await res.json();
      const terminal = isTerminalStatus(data.status);
      const resultsSaved = data.results_saved !== false;
      const terminalPolls = terminal ? pollRef.current.terminalPolls + 1 : 0;

      // Keep polling until the status is terminal AND the saved results have
      // been re-read at least once, so the file written at the very end of the
      // run is never missed. A terminal run whose file never shows up is
      // retried a few times, then left alone.
      const settled = terminal && (resultsSaved ? terminalPolls >= 2 : terminalPolls > TERMINAL_GRACE_POLLS);

      pollRef.current = { status: data.status, resultsSaved, terminalPolls, missingPolls: 0, polling: !settled };
      setMissing(false);
      setRun(data);
    } catch (error) {
      console.error('Failed to fetch run detail:', error);
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    pollRef.current = newPollState();
    setLoading(true);
    setRun(null);
    setMissing(false);
    setActiveBucket('accepted');
    fetchData();
  }, [fetchData]);

  useEffect(() => {
    if (!pollRef.current.polling) return undefined;
    const timer = setInterval(fetchData, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchData, run, missing]);

  // Stop is cooperative: the backend sets a flag and the run unwinds on its next
  // scroll step, judging and saving everything it had already collected. So the
  // button stays disabled while stopping rather than vanishing — the point is
  // to see it finish cleanly, not to wonder if it worked.
  const stopRun = useCallback(async () => {
    setStopping(true);
    setStopError(null);
    try {
      const res = await fetch(`/api/runs/${runId}/cancel`, { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || body.detail || 'Could not stop the search');
      setRun((prev) => (prev ? { ...prev, status: 'cancelling' } : prev));
      fetchData();
    } catch (error) {
      setStopError(error.message);
      setStopping(false);
    }
  }, [runId, fetchData]);

  const checkAgain = () => {
    pollRef.current = newPollState();
    setLoading(true);
    setMissing(false);
    fetchData();
  };

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="h-10 w-64 animate-pulse rounded-lg bg-muted" />
        <div className="grid gap-4 md:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-28 animate-pulse rounded-lg border border-border/60 bg-card/60" />
          ))}
        </div>
        <div className="h-72 animate-pulse rounded-lg border border-border/60 bg-card/60" />
      </div>
    );
  }

  if (!run) {
    return (
      <div className="panel mx-auto max-w-md p-10 text-center">
        <p className="text-lg font-semibold text-foreground">
          {missing ? 'Run not found' : 'Looking for this run…'}
        </p>
        <p className="mt-2 text-sm text-muted-foreground">
          {missing
            ? 'We could not find a run with that id.'
            : 'The search has not reported anything yet. This page keeps checking on its own.'}
        </p>
        <div className="mt-6 flex justify-center gap-3">
          <Button onClick={checkAgain} variant="outline">
            Check again
          </Button>
          <Link href="/runs">
            <Button variant="ghost">Back to runs</Button>
          </Link>
        </div>
      </div>
    );
  }

  const status = run.status || 'pending';
  const keywords = Array.isArray(run.keywords) ? run.keywords : [];
  const accepted = Array.isArray(run.accepted) ? run.accepted : [];
  const deferred = Array.isArray(run.deferred) ? run.deferred : [];
  const held = Array.isArray(run.held) ? run.held : [];
  const rejected = Array.isArray(run.judged_rejected) ? run.judged_rejected : [];
  const aiFailed = Array.isArray(run.ai_failed) ? run.ai_failed : [];
  const toRows = (ads) => ads.map((ad, index) => {
    const key = adKey(ad, index);
    return { ad, key, seen: seenIds.has(key) };
  });
  const acceptedRows = toRows(accepted);
  const heldRows = toRows(held);
  const deferredRows = toRows(deferred);
  const rejectedRows = toRows(rejected);
  const aiFailedRows = toRows(aiFailed);
  const acceptedKeys = acceptedRows.map((row) => row.key);
  const seenCount = acceptedRows.filter((row) => row.seen).length;
  const unseenCount = acceptedRows.length - seenCount;
  const visibleRows = acceptedRows.filter((row) =>
    seenFilter === 'all' ? true : seenFilter === 'seen' ? row.seen : !row.seen
  );
  const rejectedCount = rejected.length || run.counts?.rejected || 0;
  const aiFailedCount = aiFailed.length || run.counts?.ai_failed || 0;
  const isLive = !isTerminalStatus(status);
  const isManual = run.mode === 'manual';
  const awaitingVerdicts = run.phase === 'awaiting_verdicts';
  const resultsSaved = run.results_saved !== false;
  const keywordsWithAds = keywords.filter((entry) => (entry.accepted_count ?? 0) > 0).length;

  let emptyResults;
  if (isLive) {
    emptyResults = awaitingVerdicts
      ? {
          message: 'The scrape is finished. The assistant is judging these ads now.',
          hint: 'Accepted ads appear here after the verdicts are applied.',
        }
      : {
          message: 'Nothing accepted yet — the search is still running.',
          hint: 'Accepted ads appear here as soon as they are judged.',
        };
  } else if (!resultsSaved) {
    emptyResults = {
      message: 'The search finished, but no results file was saved for it.',
      hint: 'Reloading may help if the results were written moments after the search ended.',
    };
  } else if (deferred.length > 0) {
    emptyResults = {
      message: 'Nothing accepted this time — but the right kind of ads showed up.',
      hint: `${deferred.length} ad(s) fit your profile and are held for a request that asks for them.`,
    };
  } else if (held.length > 0) {
    emptyResults = {
      message: 'Nothing accepted this time — some ads were close, but below your strictness threshold.',
      hint: `${held.length} ad(s) are in Not sure yet. Lower a threshold and they are re-checked next run.`,
    };
  } else if (keywordsWithAds > 0) {
    emptyResults = {
      message: 'The search finished, but none of these ads passed the relevance check.',
      hint: `${rejectedCount} ad${rejectedCount === 1 ? '' : 's'} were reviewed and set aside.`,
    };
  } else {
    emptyResults = {
      message: 'The search finished and no matching ads were found.',
      hint: keywords.length
        ? `All ${keywords.length} keyword${keywords.length === 1 ? '' : 's'} returned nothing.`
        : 'Try widening the filters or using different keywords.',
    };
  }

  const acceptedActions = (
    <div className="flex flex-wrap items-center gap-2">
      <div
        role="group"
        aria-label="Filter accepted ads by seen state"
        className="inline-flex rounded-lg border border-border bg-background p-0.5"
      >
        {SEEN_FILTERS.map(({ value, label }) => {
          const count = value === 'all' ? accepted.length : value === 'seen' ? seenCount : unseenCount;
          return (
            <button
              key={value}
              type="button"
              onClick={() => setSeenFilter(value)}
              aria-pressed={seenFilter === value}
              className={cn(
                'rounded-md px-2.5 py-1 text-xs font-semibold tabular-nums transition-colors',
                seenFilter === value
                  ? 'bg-primary text-primary-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              {label} ({count})
            </button>
          );
        })}
      </div>
      {unseenCount > 0 && (
        <button
          type="button"
          onClick={() => setSeenFor(acceptedKeys, true)}
          className="text-xs font-semibold text-primary hover:underline"
        >
          Mark all seen
        </button>
      )}
      {seenCount > 0 && (
        <button
          type="button"
          onClick={() => setSeenFor(acceptedKeys, false)}
          className="text-xs font-semibold text-muted-foreground hover:text-foreground hover:underline"
        >
          Mark all unseen
        </button>
      )}
    </div>
  );

  let activeResult;
  switch (activeBucket) {
    case 'held':
      activeResult = (
        <ResultBucket
          bucket="held"
          title="Not sure yet"
          count={held.length}
          rows={heldRows}
          tone="bg-warning"
          defaultLimit={6}
          description="The judge accepted these, but with less confidence than your strictness setting asks for. They were not rejected. Lower a threshold and they are re-checked on the next run without being scraped again."
        />
      );
      break;
    case 'deferred':
      activeResult = (
        <ResultBucket
          bucket="deferred"
          title="Held for later"
          count={deferred.length}
          rows={deferredRows}
          tone="bg-info"
          defaultLimit={6}
          description="These fit your profile but are not what you asked for this time. They are kept, not thrown away — edit your request and they get reconsidered."
        />
      );
      break;
    case 'rejected':
      activeResult = (
        <ResultBucket
          bucket="rejected"
          title="Rejected"
          count={rejected.length}
          rows={rejectedRows}
          tone="bg-destructive"
          defaultLimit={6}
          description="The judge set these aside as outside this profile. They stay in Decisions and are not added to your leads."
        />
      );
      break;
    case 'ai_failed':
      activeResult = (
        <ResultBucket
          bucket="ai_failed"
          title="Couldn't judge"
          count={aiFailed.length}
          rows={aiFailedRows}
          tone="bg-muted-foreground"
          defaultLimit={6}
          description="The judge could not reach a verdict on these. They stay eligible and are retried on the next run."
        />
      );
      break;
    case 'accepted':
    default:
      activeResult = (
        <ResultBucket
          bucket="accepted"
          title="Accepted ads"
          count={accepted.length}
          rows={visibleRows}
          tone="bg-success"
          showSeen
          onToggleSeen={toggleSeen}
          description={`${seenCount} seen, ${unseenCount} left to review`}
          actions={acceptedActions}
          emptyState={
            accepted.length === 0 ? (
              <EmptyState message={emptyResults.message} hint={emptyResults.hint} />
            ) : (
              <div className="px-6 py-10 text-center">
                <p className="text-sm text-muted-foreground">
                  {seenFilter === 'unseen'
                    ? 'Nothing left to review — every ad here is marked seen.'
                    : 'No ads are marked seen yet.'}
                </p>
                <button
                  type="button"
                  onClick={() => setSeenFilter('all')}
                  className="mt-3 text-xs font-semibold text-primary hover:underline"
                >
                  Show all {accepted.length} ads
                </button>
              </div>
            )
          }
        />
      );
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <Link href="/runs" className="text-sm text-muted-foreground transition-colors hover:text-foreground">
            ← All runs
          </Link>
          <h1 className="font-mono text-xl font-medium tracking-tight">{runId}</h1>
          <p className="text-sm text-muted-foreground">
            {run.category ? `${run.category.replace(/_/g, ' ')}, ` : ''}
            {run.recorded_at ? new Date(run.recorded_at).toLocaleString() : 'Just started'}
          </p>
          {(thresholdLabel(run.thresholds?.profile) || thresholdLabel(run.thresholds?.request)) && (
            <p className="text-xs text-muted-foreground">
              Strictness this run: profile {thresholdLabel(run.thresholds?.profile) || '50%'},
              request {thresholdLabel(run.thresholds?.request) || '50%'}
            </p>
          )}
        </div>
        <div className="flex items-center gap-3">
          <StatusLabel status={status} className="text-sm" />
          {isLive && !isManual && (
            <Button
              variant="outline"
              onClick={stopRun}
              loading={stopping || status === 'cancelling'}
              disabled={stopping || status === 'cancelling'}
            >
              {status === 'cancelling' ? 'Stopping…' : 'Stop search'}
            </Button>
          )}
        </div>
      </header>

      {stopError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm font-medium text-destructive" role="alert">
          {stopError}
        </div>
      )}

      {awaitingVerdicts && (
        <div className="rounded-md border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground" role="status">
          The scrape is finished. The assistant is judging these ads now; the results appear here
          after the verdicts are applied.
        </div>
      )}

      {status === 'cancelled' && (
        <div className="rounded-md border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground" role="status">
          You stopped this search. Everything it had found was still judged and saved below —
          only the ads it had not reached yet were missed.
        </div>
      )}

      {(run.error || (status === 'failed' && !run.error)) && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm font-medium text-destructive" role="alert">
          {run.error || 'The search stopped because of an error.'}
        </div>
      )}

      {run.keywords_not_searched?.length > 0 && (
        <div
          className="rounded-md border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-warning"
          role="status"
        >
          <span className="font-semibold">Not searched:</span>{' '}
          {run.keywords_not_searched.map((k) => `“${k}”`).join(', ')}
          . The run finished without searching these.
        </div>
      )}

      {(run.note || run.profile_about) && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border bg-muted/30 px-4 py-2.5 text-xs text-muted-foreground">
          {run.profile_about && (
            <span>
              Profile:{' '}
              <span className="font-medium text-foreground">{run.profile_about}</span>
              {run.profile_is_active === false && ' (not the active one)'}
            </span>
          )}
          {run.parallel_workers > 1 && (
            <span>
              {run.parallel_workers} keywords at a time — {run.parallel_workers} tabs
            </span>
          )}
          {run.max_ads_per_keyword > 0 && (
            <span>{Number(run.max_ads_per_keyword).toLocaleString()} ads per keyword max</span>
          )}
          {run.run_mode && <span>Mode: {run.run_mode === 'generate' ? 'AI keywords' : 'your keywords only'}</span>}
        </div>
      )}

      {run.note && (
        <div className="rounded-md border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          {run.note}
        </div>
      )}

      {isLive && (
        <Card>
          <CardContent className="space-y-3 py-5">
            <div className="flex items-center justify-between text-sm">
              <span className="font-medium text-foreground">
                {run.current_keyword
                  ? `${run.phase === 'judging' ? 'Judging' : 'Scraping'} “${run.current_keyword}”…`
                  : run.phase === 'judging'
                    ? 'Judging the ads that were found…'
                    : 'Searching the Meta Ad Library…'}
              </span>
              <span className="font-semibold tabular-nums text-primary">{run.progress || 0}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-primary transition-all duration-500" style={{ width: `${Math.max(4, run.progress || 0)}%` }} />
            </div>

            {/* How far down the CURRENT keyword we are. The bar above is the
                whole run, which barely moves while one large keyword grinds
                through hundreds of ads — so without this, a run doing real work
                looks exactly like a stalled one. That confusion is what the
                idle detector used to act on. */}
            {run.phase !== 'judging' && run.cards_seen > 0 && (
              <div className="space-y-1">
                <div className="flex justify-between text-[11px] text-muted-foreground">
                  <span>this keyword&rsquo;s list</span>
                  <span className="tabular-nums">
                    {Number(run.cards_seen).toLocaleString()} seen
                    {run.estimate ? ` of ~${Number(run.estimate).toLocaleString()} Meta thinks exist` : ''}
                  </span>
                </div>
                {run.cards_target > 0 && (
                  <div className="h-1 w-full overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full bg-primary/50 transition-all duration-500"
                      style={{
                        width: `${Math.min(100, Math.max(2, (Number(run.cards_seen) / Number(run.cards_target)) * 100))}%`,
                      }}
                    />
                  </div>
                )}
              </div>
            )}

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              {run.phase === 'judging' && run.ads_judging_total > 0 && (
                <span className="font-medium text-foreground tabular-nums">
                  judged {Number(run.ads_judged || 0).toLocaleString()} of{' '}
                  {Number(run.ads_judging_total).toLocaleString()} in this group
                </span>
              )}
              {run.harvested_total > 0 && (
                <span className="font-medium text-foreground">
                  {run.harvested_total} ad{run.harvested_total === 1 ? '' : 's'} scraped so far
                </span>
              )}
              {/* What the cheap filter let through. Fetched is always more than
                  judged — that is the sieve working — so without this a keyword
                  yielding 3% is indistinguishable from one yielding 100%. */}
              {run.sieve_totals?.cards > 0 && (
                <span className="tabular-nums">
                  {Number(run.sieve_totals.cards).toLocaleString()} fetched,{' '}
                  {Number(run.sieve_totals.kept).toLocaleString()} passed filters,{' '}
                  {Number(run.sieve_totals.to_ai).toLocaleString()} to AI
                </span>
              )}
              {run.phase !== 'judging' && run.ads_collected > 0 && (
                <span>
                  {run.ads_collected} found for this keyword
                </span>
              )}
              {run.keywords_harvested?.length > 0 && (
                <span>
                  {run.keywords_harvested.length} of {run.total_keywords || '?'} keyword
                  {run.total_keywords === 1 ? '' : 's'} finished
                </span>
              )}
              {run.log_hint && (
                <span className="ml-auto font-mono opacity-70">{run.log_hint}</span>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      <LiveLog active={isLive} />

      <div
        role="group"
        aria-label="Show run outcome"
        className="flex flex-wrap items-center gap-2"
      >
        {[
          { id: 'accepted', label: 'Accepted', value: accepted.length, tone: 'text-success' },
          { id: 'held', label: 'Not sure yet', value: held.length, tone: 'text-warning' },
          { id: 'deferred', label: 'Held for later', value: deferred.length, tone: 'text-info' },
          { id: 'rejected', label: 'Rejected', value: rejectedCount, tone: 'text-destructive' },
          { id: 'ai_failed', label: "Couldn't judge", value: aiFailedCount, tone: 'text-muted-foreground' },
        ].map(({ id, label, value, tone }) => {
          const active = activeBucket === id;
          return (
            <button
              key={id}
              type="button"
              onClick={() => setActiveBucket(id)}
              aria-pressed={active}
              aria-controls={`bucket-${id}`}
              className={cn(
                'inline-flex items-baseline gap-1.5 rounded-md border px-3 py-1.5 transition-colors',
                active
                  ? 'border-primary/50 bg-primary/10'
                  : 'border-border bg-card hover:border-foreground/25 hover:bg-accent/40'
              )}
            >
              <span className={cn('text-lg font-medium tabular-nums', tone)}>{value}</span>
              <span className={cn('text-sm', active ? 'text-foreground' : 'text-muted-foreground')}>
                {label}
              </span>
            </button>
          );
        })}
      </div>

      {keywords.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Keywords ({keywords.length})</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {keywords.map((entry) => (
              <KeywordChip key={entry.id || entry.keyword} entry={entry} />
            ))}
          </CardContent>
        </Card>
      )}

      {activeResult}
    </div>
  );
}