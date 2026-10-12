'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { PageShell } from '@/components/Nav';
import { Button, Input, Badge, Card, CardContent, Spinner } from '@/components/ui';
import { cn } from '@/lib/utils';

const PAGE = 25;

const TABS = [
  {
    key: 'accepted',
    label: 'Accepted leads',
    blurb: 'Ads you already have. Global — every profile skips these.',
    variant: 'success',
    outcomes: ['rejected', 'junk', 'none'],
  },
  {
    key: 'junk',
    label: 'Junk',
    blurb: 'Nobody is hiring anybody here. Global, and grows without limit — check it when a lead goes missing.',
    variant: 'destructive',
    outcomes: ['accepted', 'none'],
  },
  {
    key: 'rejected',
    label: 'Rejected',
    blurb: 'A real employer, just not for this profile. Only the selected profile skips these.',
    variant: 'warning',
    outcomes: ['accepted', 'junk', 'none'],
  },
];

export default function VerdictsClient() {
  const [profiles, setProfiles] = useState([]);
  const [profileId, setProfileId] = useState(null);
  const [tab, setTab] = useState('accepted');
  const [counts, setCounts] = useState({ accepted: 0, junk: 0, rejected: 0 });
  const [data, setData] = useState(null);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [flash, setFlash] = useState(null);
  const [selected, setSelected] = useState(() => new Set());

  const isProfileScoped = tab === 'rejected';

  // ── loaders ───────────────────────────────────────────────────────────────
  const loadCounts = useCallback(async (pid) => {
    const qs = pid ? `?profile_id=${encodeURIComponent(pid)}` : '';
    const res = await fetch(`/api/verdicts/counts${qs}`, { cache: 'no-store' });
    if (res.ok) setCounts(await res.json());
  }, []);

  const loadPage = useCallback(async () => {
    setLoading(true);
    setError(null);
    const qs = [
      `outcome=${encodeURIComponent(tab)}`,
      `limit=${PAGE}`,
      `offset=${offset}`,
      isProfileScoped && profileId ? `profile_id=${encodeURIComponent(profileId)}` : null,
      query ? `search=${encodeURIComponent(query)}` : null,
    ]
      .filter(Boolean)
      .join('&');
    try {
      const res = await fetch(`/api/verdicts?${qs}`, { cache: 'no-store' });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || body.error || 'Could not load');
      setData(body);
    } catch (err) {
      setError(err.message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [tab, offset, profileId, isProfileScoped, query]);

  useEffect(() => {
    fetch('/api/profiles', { cache: 'no-store' })
      .then((r) => r.json())
      .then((body) => {
        setProfiles(body.profiles || []);
        const active = body.active_profile_id;
        if (active) {
          setProfileId(active);
          loadCounts(active);
        }
      })
      .catch(() => setError('Cannot reach the backend. Is it running?'));
  }, [loadCounts]);

  useEffect(() => { loadPage(); }, [loadPage]);
  useEffect(() => { if (profileId) loadCounts(profileId); }, [profileId, loadCounts]);

  // ── mutations ─────────────────────────────────────────────────────────────
  async function apply(outcome, adIds) {
    if (!adIds.length) return;
    setBusy(true);
    setError(null);
    try {
      const body = isProfileScoped
        ? { ad_ids: adIds, outcome, profile_id: profileId }
        : { ad_ids: adIds, outcome };
      const res = await fetch('/api/verdicts/bulk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.detail || 'Change failed');
      setSelected(new Set());
      setFlash(
        adIds.length === 1
          ? `Moved to ${outcome === 'none' ? 'no set' : outcome}.`
          : `Moved ${payload.changed ?? adIds.length} ad(s) to ${outcome === 'none' ? 'no set' : outcome}.`
      );
      await Promise.all([loadPage(), loadCounts(profileId)]);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const activeTab = TABS.find((t) => t.key === tab);
  const items = useMemo(() => data?.items || [], [data]);
  const shownIds = useMemo(() => items.map((i) => i.ad_archive_id), [items]);
  const allSelected = shownIds.length > 0 && shownIds.every((id) => selected.has(id));

  function toggle(id) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const searchValue = search.trim();
  const canSubmitSearch = searchValue !== query;

  return (
    <PageShell
      eyebrow="Decisions"
      title="What Tesserae has decided"
      description="Every ad Tesserae has judged, and the verdict it reached. Correct anything wrong here — the next run will respect the change."
      actions={
        <Button variant="outline" onClick={() => { loadPage(); loadCounts(profileId); }}>
          Refresh
        </Button>
      }
    >
      {/* Scope notice: global vs profile-scoped is the single most confusing
          thing about these sets, so it is stated rather than implied. */}
      <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">Accepted and junk are global.</span>{' '}
        Editing either applies to every profile. Only <span className="font-medium text-foreground">rejected</span> is
        scoped to one profile.
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex flex-wrap gap-2">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => { setTab(t.key); setOffset(0); setSelected(new Set()); }}
              className={cn(
                'rounded-lg border px-4 py-2 text-sm font-medium transition-colors',
                tab === t.key
                  ? 'border-primary bg-primary/15 text-primary'
                  : 'border-border text-muted-foreground hover:bg-accent/40 hover:text-foreground'
              )}
            >
              {t.label}
              <span className="ml-2 tabular-nums opacity-70">{counts[t.key] ?? 0}</span>
            </button>
          ))}
        </div>

        {isProfileScoped && (
          <div className="flex items-center gap-2">
            <label htmlFor="scope" className="text-sm text-muted-foreground">Profile</label>
            <select
              id="scope"
              value={profileId || ''}
              onChange={(e) => { setProfileId(e.target.value); setOffset(0); }}
              className="h-10 rounded-lg border border-border bg-background px-3 text-sm"
            >
              {profiles.map((p) => (
                <option key={p.profile_id} value={p.profile_id}>{p.about}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      <p className="text-sm text-muted-foreground">{activeTab?.blurb}</p>

      <Card>
        <CardContent className="space-y-4">
          <form
            className="flex flex-wrap gap-2"
            onSubmit={(e) => { e.preventDefault(); setQuery(searchValue); setOffset(0); }}
          >
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search advertiser or ad text…"
              className="min-w-[260px] flex-1"
            />
            <Button type="submit" variant="secondary" disabled={!canSubmitSearch}>
              Search
            </Button>
            {query && (
              <Button
                type="button"
                variant="ghost"
                onClick={() => { setSearch(''); setQuery(''); setOffset(0); }}
              >
                Clear
              </Button>
            )}
          </form>

          {/* Bulk bar: the reason a list view exists at all — reviewing 89 junk
              ads one at a time is how a set stops being trustworthy. */}
          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={() =>
                  setSelected(allSelected ? new Set() : new Set(shownIds))
                }
                disabled={!items.length}
              />
              Select this page
            </label>
            <span className="text-sm text-muted-foreground">{selected.size} selected</span>
            <div className="ml-auto flex flex-wrap gap-2">
              {activeTab?.outcomes.map((outcome) => (
                <Button
                  key={outcome}
                  size="sm"
                  variant={outcome === 'none' ? 'outline' : 'secondary'}
                  disabled={!selected.size || busy}
                  loading={busy}
                  onClick={() => apply(outcome, [...selected])}
                >
                  {outcome === 'none'
                    ? 'Remove from all'
                    : outcome === 'accepted'
                    ? 'Restore to accepted'
                    : `Move to ${outcome}`}
                </Button>
              ))}
            </div>
          </div>

          {flash && <p className="text-sm text-green-600 dark:text-green-400">{flash}</p>}
          {error && <p className="text-sm text-destructive">{error}</p>}

          {loading ? (
            <div className="flex justify-center py-12"><Spinner /></div>
          ) : !items.length ? (
            <p className="py-12 text-center text-sm text-muted-foreground">
              {query ? `Nothing matches “${query}”.` : `No ads in ${activeTab?.label.toLowerCase()} yet.`}
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {items.map((item) => (
                <li key={item.ad_archive_id} className="flex gap-3 py-4">
                  <input
                    type="checkbox"
                    className="mt-1.5 h-4 w-4 shrink-0"
                    checked={selected.has(item.ad_archive_id)}
                    onChange={() => toggle(item.ad_archive_id)}
                    aria-label={`Select ad ${item.ad_archive_id}`}
                  />
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      {item.advertiser_page_url ? (
                        <a
                          href={item.advertiser_page_url}
                          target="_blank"
                          rel="noreferrer"
                          className="font-medium text-foreground underline-offset-4 hover:underline"
                        >
                          {item.advertiser_name}
                        </a>
                      ) : (
                        <span className="font-medium text-foreground">{item.advertiser_name}</span>
                      )}
                      {item.source_keyword && (
                        <Badge variant="outline">via “{item.source_keyword}”</Badge>
                      )}
                      {!item.has_record && (
                        <Badge variant="warning">original ad text no longer on disk</Badge>
                      )}
                    </div>
                    {item.excerpt ? (
                      <p className="whitespace-pre-wrap text-sm text-muted-foreground">{item.excerpt}</p>
                    ) : (
                      <p className="text-sm italic text-muted-foreground/60">No ad text stored.</p>
                    )}
                    {item.relevance_reason && (
                      <p className="text-xs text-muted-foreground/80">Why: {item.relevance_reason}</p>
                    )}
                    <p className="font-mono text-[11px] text-muted-foreground/60">{item.ad_archive_id}</p>
                  </div>
                  <div className="flex shrink-0 flex-col gap-1.5">
                    {activeTab?.outcomes.map((outcome) => (
                      <Button
                        key={outcome}
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() => apply(outcome, [item.ad_archive_id])}
                      >
                        {outcome === 'none' ? 'Clear' : outcome === 'accepted' ? 'Accept' : outcome}
                      </Button>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {data && data.total > 0 && (
            <div className="flex items-center justify-between border-t border-border pt-4 text-sm">
              <span className="text-muted-foreground">
                {query
                  ? `${data.matched} of ${data.total} match`
                  : `${data.total} total`}
              </span>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE))}
                >
                  Previous
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={offset + PAGE >= data.matched}
                  onClick={() => setOffset(offset + PAGE)}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </PageShell>
  );
}
