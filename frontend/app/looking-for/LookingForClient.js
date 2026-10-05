'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/Card';
import { PageShell } from '@/components/Nav';
import { cn } from '@/lib/utils';

const CLASSIFY_DEBOUNCE_MS = 1200;

function ChangeBanner({ verdict, busy }) {
  if (busy && !verdict) return <p className="text-xs text-muted-foreground">Reading your edit…</p>;
  if (!verdict) return null;

  if (!verdict.new_family_required) {
    return (
      <div className="rounded-xl border border-success/40 bg-success/10 px-4 py-3 text-sm">
        <p className="font-semibold text-foreground">No change to what you want</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          {verdict.explanation || 'Same request, better wording.'} Ads already held back stay held
          back.
        </p>
      </div>
    );
  }

  const widening = verdict.change === 'widening';
  return (
    <div
      className={cn(
        'rounded-xl border px-4 py-3 text-sm',
        widening ? 'border-info/40 bg-info/10' : 'border-warning/40 bg-warning/10'
      )}
    >
      <p className="font-semibold text-foreground">
        {widening ? 'This asks for more than before' : 'This asks for less than before'}
      </p>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{verdict.explanation}</p>
      {widening && (
        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
          Ads that were relevant but held back under your previous request become eligible again.
          They are not re-scraped — they are re-judged from what was already stored.
        </p>
      )}
    </div>
  );
}

export default function LookingForClient() {
  const [data, setData] = useState(null);
  const [familyId, setFamilyId] = useState(null);
  const [requestId, setRequestId] = useState(null);
  const [draft, setDraft] = useState('');
  const [dirty, setDirty] = useState(false);
  const [verdict, setVerdict] = useState(null);
  const [classifying, setClassifying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [deferred, setDeferred] = useState([]);
  const timer = useRef(null);

  const load = useCallback(async () => {
    const res = await fetch('/api/families', { cache: 'no-store' });
    const body = await res.json();
    setData(body);
    setFamilyId((prev) => {
      if (prev && body.families.some((f) => f.family_id === prev)) return prev;
      return body.active_family_id || body.families[0]?.family_id || null;
    });
  }, []);

  useEffect(() => {
    load().catch(() => setMessage({ tone: 'error', text: 'Could not load your profiles.' }));
  }, [load]);

  const families = data?.families ?? [];
  const family = families.find((f) => f.family_id === familyId) ?? null;
  const requests = family?.requests ?? [];
  const request = requests.find((r) => r.request_family_id === requestId) ?? null;

  useEffect(() => {
    const current = (data?.families ?? []).find((f) => f.family_id === familyId);
    const first = current?.requests?.[0];
    setRequestId(first?.request_family_id ?? null);
    setDraft(first?.request_text ?? '');
    setDirty(false);
    setVerdict(null);
  }, [familyId, data]);

  useEffect(() => {
    if (!familyId) return;
    const url = requestId
      ? `/api/families/${familyId}/deferred?request_family_id=${requestId}`
      : `/api/families/${familyId}/deferred`;
    fetch(url, { cache: 'no-store' })
      .then((r) => r.json())
      .then((b) => setDeferred(b.deferred ?? []))
      .catch(() => setDeferred([]));
  }, [familyId, requestId]);

  useEffect(() => {
    if (!dirty || !draft.trim()) {
      setVerdict(null);
      return undefined;
    }
    setClassifying(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      try {
        const res = await fetch('/api/families/request/classify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: draft, family_id: familyId, request_family_id: requestId }),
        });
        if (res.ok) setVerdict(await res.json());
      } catch {
        /* keep the previous verdict */
      } finally {
        setClassifying(false);
      }
    }, CLASSIFY_DEBOUNCE_MS);
    return () => clearTimeout(timer.current);
  }, [draft, dirty, familyId, requestId]);

  const save = async (accept = true) => {
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch('/api/families/request/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: draft,
          family_id: familyId,
          request_family_id: requestId,
          accept,
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || 'Could not save');

      if (!accept) {
        setDraft(request?.request_text ?? '');
        setDirty(false);
        setVerdict(null);
        setMessage({ tone: 'info', text: 'Declined — your request is unchanged.' });
        return;
      }
      setMessage({
        tone: 'success',
        text:
          body.status === 'created'
            ? 'New request saved. Ads held back under the old one are eligible again.'
            : 'Request updated.',
      });
      await load();
      if (body.request?.request_family_id) setRequestId(body.request.request_family_id);
    } catch (error) {
      setMessage({ tone: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return (
      <PageShell eyebrow="Step 2 of 3" title="What you are looking for">
        <p className="text-sm text-muted-foreground">Loading…</p>
      </PageShell>
    );
  }

  if (families.length === 0) {
    return (
      <PageShell
        eyebrow="Step 2 of 3"
        title="What you are looking for"
        description="Describe the role you want right now. This is temporary scoping inside your profile — it never changes who you are."
      >
        <Card>
          <CardContent className="py-10 text-center">
            <p className="text-sm text-muted-foreground">
              Write your profile first — a request needs a profile to belong to.
            </p>
          </CardContent>
        </Card>
      </PageShell>
    );
  }

  return (
    <PageShell
      eyebrow="Step 2 of 3"
      title="What you are looking for"
      description="This is temporary scoping inside your profile, not a change of who you are. Ads that fit your profile but fall outside this request are held for later instead of being thrown away."
    >
      <div className="grid gap-6 lg:grid-cols-[20rem_1fr]">
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Profile</CardTitle>
              <CardDescription>Requests belong to one profile.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {families.map((f) => (
                <button
                  key={f.family_id}
                  type="button"
                  onClick={() => setFamilyId(f.family_id)}
                  className={cn(
                    'w-full rounded-xl border p-3 text-left transition-colors',
                    f.family_id === familyId
                      ? 'border-primary bg-primary/10'
                      : 'border-border/60 hover:border-primary/40 hover:bg-accent/40'
                  )}
                >
                  <span className="truncate text-sm font-semibold text-foreground">
                    {f.about || 'Untitled'}
                  </span>
                </button>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Saved requests</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {requests.length === 0 && (
                <p className="text-xs text-muted-foreground">None yet.</p>
              )}
              {requests.map((r) => (
                <button
                  key={r.request_family_id}
                  type="button"
                  onClick={() => {
                    setRequestId(r.request_family_id);
                    setDraft(r.request_text);
                    setDirty(false);
                    setVerdict(null);
                  }}
                  className={cn(
                    'w-full rounded-xl border p-3 text-left text-xs transition-colors',
                    r.request_family_id === requestId
                      ? 'border-primary bg-primary/10'
                      : 'border-border/60 hover:border-primary/40 hover:bg-accent/40'
                  )}
                >
                  <span className="line-clamp-2 text-foreground">
                    {r.request_text.slice(0, 120) || '(empty)'}
                  </span>
                </button>
              ))}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">
                {request ? 'Edit this request' : 'New request'}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <textarea
                value={draft}
                onChange={(e) => {
                  setDraft(e.target.value);
                  setDirty(e.target.value !== (request?.request_text ?? ''));
                  setMessage(null);
                }}
                rows={12}
                placeholder="Must be remote. Backend or AI integration roles. Intermediate to senior."
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
                </div>
              )}

              <div className="flex flex-wrap items-center gap-3">
                <Button onClick={() => save(true)} loading={busy} disabled={!dirty || !draft.trim()}>
                  {verdict?.new_family_required ? 'Save as new request' : 'Save request'}
                </Button>
                {verdict?.new_family_required && (
                  <Button variant="outline" onClick={() => save(false)} disabled={busy}>
                    Keep my current request
                  </Button>
                )}
                {dirty && <span className="text-xs text-muted-foreground">Unsaved changes</span>}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Held for later ({deferred.length})</CardTitle>
              <CardDescription>
                {requestId
                  ? 'Ads the previous request held back, eligible again under this one.'
                  : 'Ads that fit your profile but that you did not ask for.'}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {deferred.length === 0 ? (
                <p className="text-sm text-muted-foreground">Nothing held back.</p>
              ) : (
                <div className="space-y-2">
                  {deferred.map((entry) => (
                    <div key={entry.ad_archive_id} className="rounded-xl border border-border/60 px-3 py-2">
                      <p className="text-sm font-medium text-foreground">
                        {entry.ad?.advertiser_name || 'Unnamed advertiser'}
                      </p>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {entry.ad?.source_keyword || '—'}
                        {entry.ad_category ? ` · ${entry.ad_category}` : ''}
                      </p>
                      {entry.reason && (
                        <p className="mt-0.5 text-xs italic text-muted-foreground/80">{entry.reason}</p>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </PageShell>
  );
}