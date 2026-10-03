'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/Button';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card';
import ProfileForm from '@/components/config/ProfileForm';
import FilterBuilder, { INITIAL_RUN_MODE } from '@/components/config/FilterBuilder';

function Skeleton() {
  return (
    <main className="min-h-screen bg-background p-6 md:p-8">
      <div className="mx-auto max-w-7xl animate-pulse space-y-8">
        <div className="h-9 w-56 rounded-lg bg-muted" />
        <div className="grid gap-6 lg:grid-cols-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-[32rem] rounded-2xl border border-border/60 bg-card/60" />
          ))}
        </div>
      </div>
    </main>
  );
}

export default function ConfigClient() {
  const router = useRouter();
  const [profile, setProfile] = useState({ about_us: '', additional_filters: '' });
  const [lastSelection, setLastSelection] = useState(null);
  const [runMode, setRunMode] = useState(INITIAL_RUN_MODE);
  const [loading, setLoading] = useState(true);
  const [startError, setStartError] = useState('');
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
        setProfile({
          about_us: data.about_us ?? data.aboutUs ?? '',
          additional_filters: data.additional_filters ?? data.additionalFilters ?? '',
        });
        const saved = data.last_selection ?? data.lastSelection ?? null;
        const restored = saved && Object.keys(saved).length ? saved : null;
        selectionRef.current = restored;
        setLastSelection(restored);
        // Always resolve the mode. The saved selection is also written by the
        // Python CLI, which does not store run_mode — falling through to the
        // default here is what keeps the About You notice in sync with the
        // option FilterBuilder shows as selected.
        setRunMode(restored?.run_mode ?? INITIAL_RUN_MODE);
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
    (nextMode) => {
      setRunMode(nextMode);
      persistSelection({ run_mode: nextMode });
    },
    [persistSelection]
  );

  const handleProfileSave = useCallback(async (data) => {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!res.ok) throw new Error('Saving the profile failed. Please try again.');
    // Deliberately not calling setProfile here: it would change the ProfileForm
    // `key` and remount the form, wiping the "saved" confirmation.
  }, []);

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

      persistSelection(payload);
      router.push(`/runs/${runId}`);
    } catch (error) {
      console.error('Start failed:', error);
      setStartError(error.message || 'The search could not be started.');
    }
  }, [router, persistSelection]);

  if (loading) return <Skeleton />;

  return (
    <main className="min-h-screen bg-background p-6 md:p-8">
      <div className="mx-auto max-w-7xl space-y-8">
        <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-2">
            <p className="eyebrow">Step 1 of 2</p>
            <h1 className="text-3xl font-bold tracking-tight md:text-4xl">
              Set up your <span className="text-gradient">search</span>
            </h1>
            <p className="max-w-2xl text-muted-foreground">
              Describe yourself, add a few keywords, and press start. Tesserae handles the rest.
            </p>
          </div>
          <div className="flex gap-3">
            <Link href="/">
              <Button variant="outline">Dashboard</Button>
            </Link>
            <Link href="/runs">
              <Button variant="outline">All runs</Button>
            </Link>
          </div>
        </header>

        {startError && (
          <div className="rounded-xl border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm font-medium text-destructive" role="alert">
            {startError}
          </div>
        )}

        <div className="grid items-start gap-6 lg:grid-cols-2">
          <ProfileForm
            key={`${profile.about_us}::${profile.additional_filters}`}
            onSave={handleProfileSave}
            initialData={profile}
            runMode={runMode}
          />
          <FilterBuilder onStart={handleStart} onRunModeChange={handleRunModeChange} initialData={lastSelection} />
        </div>

        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-lg">What happens next</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-3">
            {[
              { step: '1', title: 'Ads are collected', body: 'Tesserae reads the Meta Ad Library for every keyword across your chosen platforms.' },
              { step: '2', title: 'AI judges each ad', body: 'Each ad is scored against your profile so only genuinely relevant ones survive.' },
              { step: '3', title: 'You export results', body: 'Review the accepted ads and download them as a single Excel sheet.' },
            ].map((item) => (
              <div key={item.step} className="space-y-1.5">
                <span className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/15 text-xs font-bold text-primary">
                  {item.step}
                </span>
                <p className="text-sm font-semibold text-foreground">{item.title}</p>
                <p className="text-xs leading-relaxed text-muted-foreground">{item.body}</p>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </main>
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