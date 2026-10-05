'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/Card';
import { PageShell } from '@/components/Nav';
import { cn } from '@/lib/utils';

const CLASSIFY_DEBOUNCE_MS = 1200;

function ScopeChips({ domain, specs }) {
  if (!domain || !specs?.length) {
    return (
      <span className="text-xs text-muted-foreground/70">
        Not placed in a category yet — this profile can&apos;t be matched to related ones.
      </span>
    );
  }
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      <span className="text-xs text-muted-foreground">{domain}</span>
      {specs.map((spec) => (
        <span
          key={spec}
          className="rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-foreground"
        >
          {spec}
        </span>
      ))}
    </span>
  );
}

/**
 * The live classification banner.
 *
 * This is the whole point of the page: before saving, the user is told whether
 * the edit stays inside the current profile or opens a new one, and why. A
 * structural change is never applied without them agreeing to it.
 */
function ChangeBanner({ verdict, busy }) {
  if (busy && !verdict) {
    return (
      <p className="text-xs text-muted-foreground">Reading your edit…</p>
    );
  }
  if (!verdict) return null;

  if (!verdict.new_family_required) {
    return (
      <div className="rounded-xl border border-success/40 bg-success/10 px-4 py-3 text-sm">
        <p className="font-semibold text-foreground">Stays inside this profile</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          {verdict.explanation || 'Same objective, just clearer wording.'} Saving edits this
          profile in place — nothing is re-judged.
        </p>
      </div>
    );
  }

  const tone =
    verdict.change === 'widening'
      ? 'border-info/40 bg-info/10'
      : 'border-warning/40 bg-warning/10';

  return (
    <div className={cn('rounded-xl border px-4 py-3 text-sm', tone)}>
      <p className="font-semibold text-foreground">
        {verdict.change === 'widening' ? 'This widens your profile' : 'This narrows your profile'}
      </p>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
        {verdict.explanation}
      </p>
      {verdict.change === 'widening' && (
        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
          Ads your current profile rejected or held back get reconsidered under the wider scope.
          Leads you already accepted carry over untouched.
        </p>
      )}
      {verdict.change === 'narrowing' && (
        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
          Nothing is re-judged. Everything already decided still holds under a narrower scope.
        </p>
      )}
      {verdict.related_families?.length > 0 && (
        <p className="mt-1.5 text-xs text-muted-foreground">
          It will also inherit verdicts from {verdict.related_families.length} related profile
          {verdict.related_families.length === 1 ? '' : 's'}.
        </p>
      )}
    </div>
  );
}

export default function AboutClient() {
  const [data, setData] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [draft, setDraft] = useState('');
  const [dirty, setDirty] = useState(false);
  const [verdict, setVerdict] = useState(null);
  const [classifying, setClassifying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [leads, setLeads] = useState([]);
  const timer = useRef(null);

  const load = useCallback(async () => {
    const res = await fetch('/api/families', { cache: 'no-store' });
    const body = await res.json();
    setData(body);
    setSelectedId((prev) => {
      if (prev && body.families.some((f) => f.family_id === prev)) return prev;
      return body.active_family_id || body.families[0]?.family_id || null;
    });
  }, []);

  useEffect(() => {
    load().catch(() => setMessage({ tone: 'error', text: 'Could not load your profiles.' }));
  }, [load]);

  const families = data?.families ?? [];
  const selected = families.find((f) => f.family_id === selectedId) ?? null;

  // Load the editor whenever the selection changes.
  useEffect(() => {
    if (!selected) return;
    setDraft(selected.profile_text);
    setDirty(false);
    setVerdict(null);
    fetch(`/api/families/${selected.family_id}/leads`, { cache: 'no-store' })
      .then((r) => r.json())
      .then((b) => setLeads(b.leads ?? []))
      .catch(() => setLeads([]));
  }, [selectedId, selected?.family_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Debounced classification of whatever is currently typed.
  useEffect(() => {
    if (!dirty || !draft.trim()) {
      setVerdict(null);
      return undefined;
    }
    setClassifying(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      try {
        const res = await fetch('/api/families/profile/classify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: draft, family_id: selectedId }),
        });
        if (res.ok) setVerdict(await res.json());
      } catch {
        /* leave the previous verdict up */
      } finally {
        setClassifying(false);
      }
    }, CLASSIFY_DEBOUNCE_MS);
    return () => clearTimeout(timer.current);
  }, [draft, dirty, selectedId]);

  const onEdit = (value) => {
    setDraft(value);
    setDirty(value !== (selected?.profile_text ?? ''));
    setMessage(null);
  };

const save = async (accept = true) => {
    setBusy(true);
    setMessage(null);
    const wasNewProfile = !selectedId;
    try {
      const res = await fetch('/api/families/profile/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: draft,
          family_id: selectedId,
          accept,
          about: verdict?.about,
          category_domain: verdict?.category_domain,
          specialisations: verdict?.specialisations ?? [],
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || 'Could not save');

      if (!accept) {
        setDraft(selected?.profile_text ?? '');
        setDirty(false);
        setVerdict(null);
        setMessage({ tone: 'info', text: 'Declined — your profile is unchanged.' });
        return;
      }
      // Three distinct outcomes, and conflating them hides what just happened:
      // a first profile, a node forked by a structural change, or an edit.
      let text;
      if (wasNewProfile) {
        text = `Profile created: ${body.family?.about ?? 'untitled'}.`;
      } else if (body.status === 'created') {
        text = `New profile created: ${body.family?.about ?? 'untitled'}. ${
          body.seed_plan?.notes?.[0] ?? ''
        }`;
      } else {
        text = 'Profile updated.';
      }
      setMessage({ tone: 'success', text, plan: body.seed_plan ?? null });
      await load();
      setSelectedId(body.family?.family_id ?? selectedId);
    } catch (error) {
      setMessage({ tone: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const addProfile = async () => {
    setSelectedId(null);
    setDraft('');
    setDirty(false);
    setVerdict(null);
    setLeads([]);
    setMessage({ tone: 'info', text: 'Describe one specific job objective, then save it.' });
  };

  const activate = async (familyId) => {
    setBusy(true);
    try {
      const res = await fetch('/api/families/activate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ family_id: familyId }),
      });
      if (!res.ok) throw new Error('Could not switch profile');
      await load();
      setSelectedId(familyId);
      setMessage({ tone: 'success', text: 'This profile is now the one searches run under.' });
    } catch (error) {
      setMessage({ tone: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <PageShell
      eyebrow="Step 1 of 3"
      title="About you"
      description="One profile = one job objective. The sharper the objective, the better Tesserae can tell which ads are yours and which belong to someone else. Keep a separate profile per objective rather than one broad profile."
    >
      <div className="grid gap-6 lg:grid-cols-[20rem_1fr]">
        <Card className="h-fit">
          <CardHeader>
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="text-base">Your profiles</CardTitle>
              <Button size="sm" variant="outline" onClick={addProfile}>
                New
              </Button>
            </div>
            <CardDescription>
              Each profile keeps its own leads and its own history.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {families.length === 0 && (
              <p className="text-sm text-muted-foreground">No profiles yet.</p>
            )}
            {families.map((family) => {
              const isActive = family.family_id === data?.active_family_id;
              const isSelected = family.family_id === selectedId;
              return (
                <button
                  key={family.family_id}
                  type="button"
                  onClick={() => setSelectedId(family.family_id)}
                  className={cn(
                    'w-full rounded-xl border p-3 text-left transition-colors',
                    isSelected
                      ? 'border-primary bg-primary/10'
                      : 'border-border/60 hover:border-primary/40 hover:bg-accent/40'
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-semibold text-foreground">
                      {family.about || 'Untitled'}
                    </span>
                    {isActive && (
                      <span className="shrink-0 rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-primary">
                        Active
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {family.counts.leads} lead{family.counts.leads === 1 ? '' : 's'} ·{' '}
                    {family.counts.deferred_stored} held · {family.counts.rejected} rejected
                  </p>
                </button>
              );
            })}
          </CardContent>
        </Card>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">
                {selected ? selected.about || 'Untitled profile' : 'New profile'}
              </CardTitle>
              {selected && <ScopeChips domain={selected.category_domain} specs={selected.specialisations} />}
            </CardHeader>
            <CardContent className="space-y-4">
              <textarea
                value={draft}
                onChange={(e) => onEdit(e.target.value)}
                rows={16}
                placeholder="I'm a backend developer specialising in Python and FastAPI..."
                className="w-full rounded-xl border border-input bg-background/60 p-4 text-sm leading-relaxed text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary focus:ring-2 focus:ring-primary/30"
              />

              <ChangeBanner verdict={verdict} busy={classifying} />

              {message && (
                <div
                  className={cn(
                    'rounded-xl border px-4 py-3 text-sm',
                    message.tone === 'error'
                      ? 'border-destructive/50 bg-destructive/10 text-destructive'
                      : message.tone === 'success'
                        ? 'border-success/40 bg-success/10'
                        : 'border-border/60 bg-muted/40'
                  )}
                >
                  <p className="font-medium text-foreground">{message.text}</p>
                  {message.plan && (
                    <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                      {message.plan.notes.map((note) => (
                        <li key={note}>· {note}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              <div className="flex flex-wrap items-center gap-3">
                <Button onClick={() => save(true)} loading={busy} disabled={!dirty || !draft.trim()}>
                  {verdict?.new_family_required ? 'Create new profile' : 'Save profile'}
                </Button>
                {verdict?.new_family_required && (
                  <Button variant="outline" onClick={() => save(false)} disabled={busy}>
                    Keep my current profile
                  </Button>
                )}
                {selected && selected.family_id !== data?.active_family_id && (
                  <Button variant="ghost" onClick={() => activate(selected.family_id)} disabled={busy}>
                    Search under this profile
                  </Button>
                )}
                {dirty && <span className="text-xs text-muted-foreground">Unsaved changes</span>}
              </div>
            </CardContent>
          </Card>

          {selected && (
            <Card>
              <CardHeader>
                <CardTitle className="text-lg">Leads ({leads.length})</CardTitle>
                <CardDescription>
                  Kept for this profile only, and never removed. Switching profiles shows that
                  profile&apos;s own leads.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {leads.length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    No leads yet for this profile. Run a search from the Search page.
                  </p>
                ) : (
                  <div className="space-y-2">
                    {leads.map((lead) => (
                      <div
                        key={lead.ad_archive_id}
                        className="rounded-xl border border-border/60 px-3 py-2"
                      >
                        <p className="text-sm font-medium text-foreground">
                          {lead.advertiser_name || 'Unnamed advertiser'}
                        </p>
                        <p className="mt-0.5 truncate text-xs text-muted-foreground">
                          {lead.source_keyword || '—'}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </PageShell>
  );
}