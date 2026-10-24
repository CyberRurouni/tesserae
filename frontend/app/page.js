'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { PageShell } from '@/components/Nav';
import { RunCard } from '@/components/runs/RunCard';
import { Button } from '@/components/ui/Button';
import { isLiveStatus } from '@/lib/runs';

const STEPS = [
  { href: '/about', title: 'About you', body: 'Describe the work you do and the skills you have.' },
  { href: '/looking-for', title: 'Looking for', body: 'Say what you want right now: remote, senior, a particular stack.' },
  { href: '/config', title: 'Search', body: 'Choose keywords and start. Tesserae reads the ads and keeps the ones that fit.' },
];

export default function Dashboard() {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchRuns = useCallback(async () => {
    try {
      const res = await fetch('/api/runs?limit=6', { cache: 'no-store' });
      if (res.ok) {
        const data = await res.json();
        setRuns(Array.isArray(data.runs) ? data.runs : []);
      }
    } catch (error) {
      console.error('Failed to fetch runs:', error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRuns();
    const timer = setInterval(fetchRuns, 5000);
    return () => clearInterval(timer);
  }, [fetchRuns]);

  const hasLiveRun = runs.some((run) => isLiveStatus(run.status || 'pending'));
  const totals = runs.reduce(
    (sum, run) => ({
      accepted: sum.accepted + (run.counts?.accepted ?? 0),
      rejected: sum.rejected + (run.counts?.rejected ?? 0),
      live: sum.live + (isLiveStatus(run.status || 'pending') ? 1 : 0),
    }),
    { accepted: 0, rejected: 0, live: 0 }
  );

  return (
    <PageShell
      title="Dashboard"
      description="Your latest searches. Running searches update on their own."
      actions={
        runs.length > 0 && (
          <>
            <Link href="/runs">
              <Button variant="outline">All runs</Button>
            </Link>
            <Link href="/config">
              <Button>{hasLiveRun ? 'Start another' : 'New search'}</Button>
            </Link>
          </>
        )
      }
    >
      {loading ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-44 animate-pulse rounded-lg border border-border bg-card" />
          ))}
        </div>
      ) : runs.length === 0 ? (
        <div className="max-w-2xl rounded-lg border border-border bg-card p-8">
          <h2 className="text-base font-medium">Start with your profile</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Tesserae needs to know who you are and what you want before it can judge ads for you.
          </p>
          <ol className="mt-6 divide-y divide-border border-y border-border">
            {STEPS.map((step, index) => (
              <li key={step.href}>
                <Link href={step.href} className="flex items-baseline gap-4 px-1 py-3.5 transition-colors hover:bg-secondary/40">
                  <span className="w-3 text-sm tabular-nums text-muted-foreground">{index + 1}</span>
                  <span>
                    <span className="block text-sm font-medium text-foreground">{step.title}</span>
                    <span className="block text-sm text-muted-foreground">{step.body}</span>
                  </span>
                </Link>
              </li>
            ))}
          </ol>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-baseline gap-x-8 gap-y-2">
            <span className="inline-flex items-baseline gap-1.5">
              <span className="text-lg font-medium tabular-nums text-success">{totals.accepted}</span>
              <span className="text-sm text-muted-foreground">accepted</span>
            </span>
            <span className="inline-flex items-baseline gap-1.5">
              <span className="text-lg font-medium tabular-nums">{totals.rejected}</span>
              <span className="text-sm text-muted-foreground">rejected</span>
            </span>
            {totals.live > 0 && (
              <span className="inline-flex items-baseline gap-1.5">
                <span className="text-lg font-medium tabular-nums text-primary">{totals.live}</span>
                <span className="text-sm text-muted-foreground">running</span>
              </span>
            )}
            <span className="text-sm text-muted-foreground">across your latest {runs.length} searches</span>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {runs.map((run) => (
              <RunCard key={run.id} run={run} />
            ))}
          </div>
        </>
      )}
    </PageShell>
  );
}
