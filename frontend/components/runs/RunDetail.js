'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { STATUS_LABELS, STATUS_STYLES } from '@/components/runs/shared';
import { isTerminalStatus } from '@/lib/runs';
import { cn } from '@/lib/utils';

const CONFIDENCE_STYLES = (value) => {
  if (value >= 0.8) return 'bg-success/15 text-success';
  if (value >= 0.5) return 'bg-warning/15 text-warning';
  return 'bg-destructive/15 text-destructive';
};

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

function Stat({ label, value, tone }) {
  return (
    <Card>
      <CardContent className="space-y-1 py-6">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
        <p className={cn('text-3xl font-bold tabular-nums', tone)}>{value}</p>
      </CardContent>
    </Card>
  );
}

function EmptyState({ message, hint }) {
  return (
    <div className="px-6 py-10 text-center">
      <p className="text-sm text-muted-foreground">{message}</p>
      {hint && <p className="mt-1.5 text-xs text-muted-foreground/70">{hint}</p>}
    </div>
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
        'inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium',
        foundNothing
          ? 'border-border bg-muted/40 text-muted-foreground'
          : 'border-primary/30 bg-primary/10 text-foreground'
      )}
    >
      {keyword}
      {acceptedCount === null ? null : foundNothing ? (
        <span className="italic text-muted-foreground/70">no ads</span>
      ) : (
        <span className="text-success">· {acceptedCount} accepted</span>
      )}
    </span>
  );
}

export function RunDetail({ runId }) {
  const [run, setRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [missing, setMissing] = useState(false);
  const [exporting, setExporting] = useState(false);
  // The poll loop reads its bookkeeping from a ref, but `run`/`missing` are
  // render state, so the effect below re-evaluates after every poll.
  const pollRef = useRef(newPollState());

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
    fetchData();
  }, [fetchData]);

  useEffect(() => {
    if (!pollRef.current.polling) return undefined;
    const timer = setInterval(fetchData, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchData, run, missing]);

  const handleExport = async () => {
    setExporting(true);
    try {
      const res = await fetch(`/api/export/${runId}`);
      if (!res.ok) return;
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${runId}.xlsx`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Export failed:', error);
    } finally {
      setExporting(false);
    }
  };

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
            <div key={i} className="h-28 animate-pulse rounded-2xl border border-border/60 bg-card/60" />
          ))}
        </div>
        <div className="h-72 animate-pulse rounded-2xl border border-border/60 bg-card/60" />
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
  const rejectedCount = run.counts?.rejected ?? 0;
  const aiFailedCount = run.counts?.ai_failed ?? 0;
  const isLive = !isTerminalStatus(status);
  const finished = status === 'completed';
  const resultsSaved = run.results_saved !== false;
  const keywordsWithAds = keywords.filter((entry) => (entry.accepted_count ?? 0) > 0).length;

  let emptyResults;
  if (isLive) {
    emptyResults = {
      message: 'Nothing accepted yet — the search is still running.',
      hint: 'Accepted ads appear here as soon as they are judged.',
    };
  } else if (!resultsSaved) {
    emptyResults = {
      message: 'The search finished, but no results file was saved for it.',
      hint: 'Reloading may help if the results were written moments after the search ended.',
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

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <Link href="/runs" className="text-sm text-muted-foreground transition-colors hover:text-foreground">
            ← All runs
          </Link>
          <h1 className="font-mono text-2xl font-bold tracking-tight md:text-3xl">{runId}</h1>
          <p className="text-sm text-muted-foreground">
            {run.category ? `${run.category.replace(/_/g, ' ')} · ` : ''}
            {run.recorded_at ? new Date(run.recorded_at).toLocaleString() : 'Just started'}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className={cn('inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm font-semibold', STATUS_STYLES[status] || STATUS_STYLES.pending)}>
            {isLive && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" aria-hidden="true" />}
            {STATUS_LABELS[status] || status}
          </span>
          <Button onClick={handleExport} loading={exporting} disabled={!finished} variant="outline">
            Download Excel
          </Button>
        </div>
      </header>

      {(run.error || (status === 'failed' && !run.error)) && (
        <div className="rounded-xl border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm font-medium text-destructive" role="alert">
          {run.error || 'The search stopped because of an error.'}
        </div>
      )}

      {isLive && (
        <Card>
          <CardContent className="space-y-3 py-5">
            <div className="flex items-center justify-between text-sm">
              <span className="font-medium text-foreground">
                {run.current_keyword ? `Searching “${run.current_keyword}”…` : 'Searching the Meta Ad Library…'}
              </span>
              <span className="font-semibold tabular-nums text-primary">{run.progress || 0}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-gradient-to-r from-primary to-glow transition-all duration-500" style={{ width: `${Math.max(4, run.progress || 0)}%` }} />
            </div>
            <p className="text-xs text-muted-foreground">
              Keep this page open — results appear below as soon as ads are accepted.
            </p>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        <Stat label="Accepted ads" value={accepted.length} tone="text-success" />
        <Stat label="Rejected" value={rejectedCount} tone="text-muted-foreground" />
        <Stat label="Could not judge" value={aiFailedCount} tone="text-warning" />
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

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Accepted ads ({accepted.length})</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {accepted.length === 0 ? (
            <EmptyState message={emptyResults.message} hint={emptyResults.hint} />
          ) : (
            <div className="scrollbar-thin overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-muted/50">
                  <tr>
                    {['Advertiser', 'Keyword', 'Headline', 'Ad text', 'Link', 'Confidence'].map((heading) => (
                      <th key={heading} className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                        {heading}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {accepted.map((ad, index) => {
                    const link = ad.ad_archive_id ? `https://www.facebook.com/ads/library/?id=${ad.ad_archive_id}` : ad.advertiser_page_url;
                    const confidence = typeof ad.relevance_confidence === 'number' ? ad.relevance_confidence : null;
                    return (
                      <tr key={ad.ad_archive_id || index} className="align-top transition-colors hover:bg-accent/40">
                        <td className="max-w-[180px] truncate px-4 py-3 font-medium text-foreground">{ad.advertiser_name || '—'}</td>
                        <td className="max-w-[160px] truncate px-4 py-3 text-muted-foreground">{ad.source_keyword || '—'}</td>
                        <td className="max-w-[160px] truncate px-4 py-3">{ad.cta_text || '—'}</td>
                        <td className="max-w-[320px] px-4 py-3 text-muted-foreground">
                          <span className="line-clamp-3">{ad.ad_text || '—'}</span>
                        </td>
                        <td className="px-4 py-3">
                          {link ? (
                            <a href={link} target="_blank" rel="noopener noreferrer" className="whitespace-nowrap text-xs font-medium text-primary hover:underline">
                              View ad ↗
                            </a>
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className="px-4 py-3">
                          {confidence === null ? (
                            <span className="text-xs text-muted-foreground">—</span>
                          ) : (
                            <span className={cn('inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-semibold', CONFIDENCE_STYLES(confidence))}>
                              {Math.round(confidence * 100)}%
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}