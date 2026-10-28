'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import AgentModeDialog from '@/components/config/AgentModeDialog';
import FilterBuilder from '@/components/config/FilterBuilder';
import { PageShell } from '@/components/Nav';

function Skeleton() {
  return (
    <div className="mx-auto w-full max-w-6xl animate-pulse space-y-6 px-6 py-8 md:px-10">
      <div className="h-8 w-40 rounded-md bg-card" />
      <div className="h-[32rem] rounded-lg border border-border bg-card" />
    </div>
  );
}

export default function ConfigClient() {
  const router = useRouter();
  const [lastSelection, setLastSelection] = useState(null);
  const [loading, setLoading] = useState(true);
  const [startError, setStartError] = useState('');
  const [activeRun, setActiveRun] = useState(null);
  const [agentModeOpen, setAgentModeOpen] = useState(false);
  // Lets the toggle handler persist without re-creating the callback.
  const selectionRef = useRef(null);

  useEffect(() => {
    let active = true;

    const load = async () => {
      try {
        const res = await fetch('/api/config', { cache: 'no-store' });
        if (!res.ok) throw new Error(`Config request failed with ${res.status}`);
        const data = await res.json();
        if (!active) return;
        // One run at a time. Stated here rather than letting the POST fail with
        // a 409 the user would have to decode.
        fetch('/api/runs/active', { cache: 'no-store' })
          .then((r) => (r.ok ? r.json() : null))
          .then((body) => {
            if (active) setActiveRun(body?.active ?? null);
          })
          .catch(() => {});
        const saved = data.last_selection ?? data.lastSelection ?? null;
        const restored = saved && Object.keys(saved).length ? saved : null;
        selectionRef.current = restored;
        setLastSelection(restored);
        // Always resolve the mode. The saved selection is also written by the
        // Python CLI, which does not store run_mode — falling through to the
        // default here is what keeps the About You notice in sync with the
        // option FilterBuilder shows as selected.
      } catch (error) {
        console.error('Failed to load config:', error);
      } finally {
        if (active) setLoading(false);
      }
    };

    load();
    return () => {
      active = false;
    };
  }, []);

  const persistSelection = useCallback((patch) => {
    const next = { ...(selectionRef.current || {}), ...patch };
    selectionRef.current = next;
    setLastSelection(next);
    fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ last_selection: next }),
    }).catch(() => {});
  }, []);

  // Remember the mode even when the user never starts a search, so the notice
  // and the selected option survive a reload.
  const handleRunModeChange = useCallback(
    (nextMode) => persistSelection({ run_mode: nextMode }),
    [persistSelection]
  );

  const handleStart = useCallback(async (filters) => {
    setStartError('');
    try {
      const payload = {
        ...filters,
        platforms: filters.platforms,
        keywords: filters.keywords,
        ad_age: filters.ad_age_hours ? Number(filters.ad_age_hours) : null,
        languages: filters.languages === 'all' ? [] : filters.languages,
        regions: filters.regions || 'ALL',
        max_ads_per_keyword: Math.max(0, Math.floor(Number(filters.max_ads_per_keyword) || 0)),
        harvest_limit: Number(filters.harvest_limit) || 0,
        // Only an explicit selection is sent; a bare number lets the backend rank.
        harvest_keywords: filters.harvest_keywords?.length ? filters.harvest_keywords : null,
        // Named explicitly: the backend resolves the lens from this, so the
        // keyword generator and the judge cannot read different profiles.
        profile_id: filters.profile_id || null,
        request_id: filters.request_id || null,
      };

      const res = await fetch('/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        throw new Error(detailOf(data) || 'The search could not be started.');
      }

      const runId = data.run_id || data.id;
      if (!runId) throw new Error('The server did not return a run id.');

      // Starting a run must not erase the keywords we offer back next time.
      // In AI mode the keyword box is legitimately empty, so keep the previous
      // set under `last_keywords` unless this run actually had keywords.
      const previous = selectionRef.current || {};
      const storedLast = Array.isArray(previous.last_keywords)
        ? previous.last_keywords
        : Array.isArray(previous.keywords)
          ? previous.keywords
          : [];

      persistSelection({
        ...payload,
        last_keywords: payload.keywords?.length ? payload.keywords : storedLast,
      });

      router.push(`/runs/${runId}`);
    } catch (error) {
      console.error('Start failed:', error);
      setStartError(error.message || 'The search could not be started.');
    }
  }, [router, persistSelection]);

  if (loading) return <Skeleton />;

  return (
    <PageShell
      title="Search"
      description="Choose your keywords and start. Your profile and what you are looking for are set on their own pages."
      actions={
        <Button variant="outline" onClick={() => setAgentModeOpen(true)}>
          Agent mode
        </Button>
      }
    >
      {activeRun && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-warning/30 bg-warning/10 px-4 py-3 text-sm">
          <span className="text-foreground">
            {activeRun.mode === 'manual' ? 'An agent run is already in progress' : 'A search is already running'}
            {activeRun.current_keyword ? `, currently on “${activeRun.current_keyword}”` : ''}. Only one run can happen at a time.
          </span>
          <Link href={`/runs/${activeRun.run_id}`}>
            <Button variant="outline" size="sm">Go to it</Button>
          </Link>
        </div>
      )}

      {startError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm font-medium text-destructive" role="alert">
          {startError}
        </div>
      )}

      <FilterBuilder
        onStart={handleStart}
        onRunModeChange={handleRunModeChange}
        initialData={lastSelection}
      />

      <AgentModeDialog open={agentModeOpen} onClose={() => setAgentModeOpen(false)} />
    </PageShell>
  );
}

function detailOf(data) {
  if (!data) return '';
  if (typeof data === 'string') return data;
  if (Array.isArray(data.detail)) {
    return data.detail
      .map((item) => {
        if (typeof item === 'string') return item;
        const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : item.loc;
        return `${field || 'field'}: ${item.msg || 'invalid'}`;
      })
      .join(' · ');
  }
  if (data.detail) return typeof data.detail === 'string' ? data.detail : '';
  if (data.error) return typeof data.error === 'string' ? data.error : '';
  return '';
}