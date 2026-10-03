'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { RunCard } from '@/components/runs/RunCard';
import { Button } from '@/components/ui/Button';
import { Card, CardContent } from '@/components/ui/Card';

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

  const hasLiveRun = runs.some((run) => !['completed', 'failed', 'cancelled'].includes(run.status));

  return (
    <main className="min-h-screen bg-background p-6 md:p-8">
      <div className="mx-auto max-w-7xl space-y-8">
        <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-2">
            <p className="eyebrow">Tesserae</p>
            <h1 className="text-3xl font-bold tracking-tight md:text-4xl">
              Your <span className="text-gradient">dashboard</span>
            </h1>
            <p className="max-w-xl text-muted-foreground">
              Every search Tesserae has run, newest first. Live runs update on their own.
            </p>
          </div>
          <div className="flex gap-3">
            <Link href="/runs">
              <Button variant="outline">All runs</Button>
            </Link>
            <Link href="/config">
              <Button>{hasLiveRun ? 'Start another' : 'New search'}</Button>
            </Link>
          </div>
        </header>

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {loading ? (
            Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-64 animate-pulse rounded-2xl border border-border/60 bg-card/60" />
            ))
          ) : runs.length === 0 ? (
            <Card className="col-span-full">
              <CardContent className="flex flex-col items-center gap-4 py-16 text-center">
                <span className="text-4xl" aria-hidden="true">🔍</span>
                <div>
                  <p className="text-lg font-semibold text-foreground">No searches yet</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Tell Tesserae about yourself and it will start finding relevant job ads.
                  </p>
                </div>
                <Link href="/config">
                  <Button>Set up my first search</Button>
                </Link>
              </CardContent>
            </Card>
          ) : (
            runs.map((run) => <RunCard key={run.id} run={run} />)
          )}
        </div>
      </div>
    </main>
  );
}