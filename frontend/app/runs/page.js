'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { RunTable } from '@/components/runs/RunTable';
import { Button } from '@/components/ui/Button';

export default function RunsPage() {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchRuns = useCallback(async () => {
    try {
      const res = await fetch('/api/runs', { cache: 'no-store' });
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
    const timer = setInterval(fetchRuns, 3000);
    return () => clearInterval(timer);
  }, [fetchRuns]);

  return (
    <main className="min-h-screen bg-background p-6 md:p-8">
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-2">
            <p className="eyebrow">History</p>
            <h1 className="text-3xl font-bold tracking-tight md:text-4xl">All runs</h1>
            <p className="text-muted-foreground">Every search Tesserae has run, newest first.</p>
          </div>
          <Link href="/config">
            <Button>New search</Button>
          </Link>
        </header>

        {loading ? (
          <div className="h-72 animate-pulse rounded-2xl border border-border/60 bg-card/60" />
        ) : (
          <RunTable runs={runs} />
        )}
      </div>
    </main>
  );
}