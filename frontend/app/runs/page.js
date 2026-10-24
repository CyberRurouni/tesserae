'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { PageShell } from '@/components/Nav';
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
    <PageShell
      title="Runs"
      description="Every search Tesserae has run, newest first."
      actions={
        <Link href="/config">
          <Button>New search</Button>
        </Link>
      }
    >
      {loading ? (
        <div className="h-72 animate-pulse rounded-lg border border-border bg-card" />
      ) : (
        <RunTable runs={runs} />
      )}
    </PageShell>
  );
}
