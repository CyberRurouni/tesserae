'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/Card';
import { PageShell } from '@/components/Nav';
import { Spinner } from '@/components/ui/Spinner';
import { cn } from '@/lib/utils';

const CLASSIFY_DEBOUNCE_MS = 1200;

function ChangeBanner({ verdict, busy, isNew }) {
  if (busy && !verdict) return <p className="text-xs text-muted-foreground">Reading your edit…</p>;
  if (isNew) {
    return (
      <div className="rounded-md border border-border/60 bg-muted/40 px-4 py-3 text-sm">
        <p className="font-semibold text-foreground">New request</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          This will be saved as a separate request and made active. Ads held back under other
          requests stay parked until they are re-judged.
        </p>
      </div>
    );
  }
  if (!verdict) return null;

  if (!verdict.new_profile_required) {
    return (
      <div className="rounded-md border border-success/40 bg-success/10 px-4 py-3 text-sm">
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
        'rounded-md border px-4 py-3 text-sm',
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
  const [profileId, setProfileId] = useState(null);
  const [requestId, setRequestId] = useState(null);
  const [draft, setDraft] = useState('');
  const [dirty, setDirty] = useState(false);
  const [verdict, setVerdict] = useState(null);
  const [classifying, setClassifying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [profileThreshold, setProfileThreshold] = useState(0.5);
  const [requestThreshold, setRequestThreshold] = useState(0.5);
  const [thresholdBusy, setThresholdBusy] = useState(false);
  const [thresholdMessage, setThresholdMessage] = useState(null);
  const timer = useRef(null);

  const load = useCallback(async () => {
    const res = await fetch('/api/profiles', { cache: 'no-store' });
    const body = await res.json();
    setData(body);
    setProfileId((prev) => {
      if (prev && body.profiles.some((f) => f.profile_id === prev)) return prev;
      return body.active_profile_id || body.profiles[0]?.profile_id || null;
    });
  }, []);

  useEffect(() => {
    load().catch(() => setMessage({ tone: 'error', text: 'Could not load your profiles.' }));
  }, [load]);

  const profiles = data?.profiles ?? [];
  const profile = profiles.find((f) => f.profile_id === profileId) ?? null;
  const requests = profile?.requests ?? [];
  const request = requests.find((r) => r.request_id === requestId) ?? null;
  const activeProfile = data?.active_profile_id === profileId;
  const activeRequest = activeProfile && Boolean(requestId) && data?.active_request_id === requestId;

  useEffect(() => {
    const current = (data?.profiles ?? []).find((f) => f.profile_id === profileId);
    const currentRequests = current?.requests ?? [];
    setRequestId((prev) => {
      if (prev && currentRequests.some((r) => r.request_id === prev)) return prev;
      const activeId = data?.active_profile_id === profileId
        ? data?.active_request_id
        : null;
      const active = currentRequests.find((r) => r.request_id === activeId);
      return active?.request_id ?? currentRequests[0]?.request_id ?? null;
    });
  }, [profileId, data]);

  useEffect(() => {
    setDraft(request?.request_text ?? '');
    setDirty(false);
    setVerdict(null);
  }, [requestId, request?.request_text]);

  useEffect(() => {
    setProfileThreshold(request?.profile_threshold ?? 0.5);
    setRequestThreshold(request?.request_threshold ?? 0.5);
  }, [request?.request_id, request?.profile_threshold, request?.request_threshold]);

  useEffect(() => {
    if (!requestId || !dirty || !draft.trim()) {
      setVerdict(null);
      return undefined;
    }
    setClassifying(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      try {
        const res = await fetch('/api/profiles/request/classify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: draft, profile_id: profileId, request_id: requestId }),
        });
        if (res.ok) setVerdict(await res.json());
      } catch {
        /* keep the previous verdict */
      } finally {
        setClassifying(false);
      }
    }, CLASSIFY_DEBOUNCE_MS);
    return () => clearTimeout(timer.current);
  }, [draft, dirty, profileId, requestId]);

  const activateRequest = async () => {
    if (!profileId || !requestId) return;
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch('/api/profiles/activate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_id: profileId, request_id: requestId }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || 'Could not activate request');
      await load();
      setMessage({ tone: 'success', text: 'This request is now active for searches.' });
    } catch (error) {
      setMessage({ tone: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const deleteRequest = async () => {
    if (!profileId || !requestId) return;
    const lastRequest = requests.length <= 1
      ? ' This is the profile’s last request, so searches will run without one until you add another.'
      : '';
    if (!window.confirm(`Delete this request? Its leads stay with the profile, but this request cannot be restored.${lastRequest}`)) {
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch(
        `/api/profiles/${encodeURIComponent(profileId)}/requests/${encodeURIComponent(requestId)}`,
        { method: 'DELETE' }
      );
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || 'Could not delete request');
      await load();
      setMessage({ tone: 'success', text: 'Request deleted.' });
    } catch (error) {
      setMessage({ tone: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const save = async (accept = true) => {
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch('/api/profiles/request/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: draft,
          profile_id: profileId,
          request_id: requestId,
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
      setDirty(false);
      setVerdict(null);
      setMessage({
        tone: 'success',
        text:
          body.status === 'created'
            ? 'New request saved and active.'
            : 'Request updated.',
      });
      await load();
      if (body.request?.request_id) setRequestId(body.request.request_id);
    } catch (error) {
      setMessage({ tone: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const saveThresholds = async () => {
    if (!profileId || !requestId) return;
    setThresholdBusy(true);
    setThresholdMessage(null);
    try {
      const res = await fetch('/api/profiles/request/thresholds', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          profile_id: profileId,
          request_id: requestId,
          profile_threshold: profileThreshold,
          request_threshold: requestThreshold,
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || 'Could not save strictness');
      setThresholdMessage({
        tone: 'success',
        text: 'Strictness saved. Lowering it re-checks Not sure yet ads on the next run.',
      });
      // Update the loaded request in place. Reloading the whole profile resets
      // the selected request to the first one, which would hide the sliders
      // the user just saved.
      setData((prev) => ({
        ...prev,
        profiles: (prev?.profiles ?? []).map((entry) => (
          entry.profile_id !== profileId
            ? entry
            : {
                ...entry,
                requests: (entry.requests ?? []).map((item) => (
                  item.request_id !== requestId
                    ? item
                    : {
                        ...item,
                        profile_threshold: profileThreshold,
                        request_threshold: requestThreshold,
                      }
                )),
              }
        )),
      }));
    } catch (error) {
      setThresholdMessage({ tone: 'error', text: error.message });
    } finally {
      setThresholdBusy(false);
    }
  };

  if (!data) {
    return (
      <PageShell title="What you are looking for">
        <div className="flex flex-col items-center justify-center gap-3 py-16">
          {message?.tone === 'error' ? (
            <p className="text-sm text-destructive">{message.text}</p>
          ) : (
            <>
              <Spinner />
              <p className="text-sm text-muted-foreground">Loading your requests…</p>
            </>
          )}
        </div>
      </PageShell>
    );
  }

  if (profiles.length === 0) {
    return (
      <PageShell
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
      title="What you are looking for"
      description="This is temporary scoping inside your profile, not a change of who you are. Ads that fit your profile but fall outside this request are held for later instead of being thrown away."
    >
      <div className="grid gap-6 lg:grid-cols-[20rem_1fr]">
        <div className="space-y-6 lg:sticky lg:top-28 lg:self-start">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Profile</CardTitle>
              <CardDescription>Requests belong to one profile.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {profiles.map((f) => (
                <button
                  key={f.profile_id}
                  type="button"
                  onClick={() => setProfileId(f.profile_id)}
                  className={cn(
                    'w-full rounded-md border p-3 text-left transition-colors',
                    f.profile_id === profileId
                      ? 'border-foreground/30 bg-secondary'
                      : 'border-border/60 hover:border-foreground/20 hover:bg-accent/40'
                  )}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-semibold text-foreground">
                      {f.about || 'Untitled'}
                    </span>
                    {f.profile_id === data?.active_profile_id && (
                      <span className="shrink-0 rounded-full bg-success/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-success">
                        Active
                      </span>
                    )}
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
                  key={r.request_id}
                  type="button"
                  onClick={() => {
                    setRequestId(r.request_id);
                    setDraft(r.request_text);
                    setDirty(false);
                    setVerdict(null);
                  }}
                  className={cn(
                    'w-full rounded-md border p-3 text-left text-xs transition-colors',
                    r.request_id === requestId
                      ? 'border-foreground/30 bg-secondary'
                      : 'border-border/60 hover:border-foreground/20 hover:bg-accent/40'
                  )}
                >
                  <span className="flex items-start justify-between gap-2">
                    <span className="line-clamp-2 text-foreground">
                      {r.request_text.slice(0, 120) || '(empty)'}
                    </span>
                    {profileId === data?.active_profile_id &&
                      r.request_id === data?.active_request_id && (
                        <span className="shrink-0 rounded-full bg-success/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-success">
                          Active
                        </span>
                      )}
                  </span>
                </button>
              ))}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-lg">
                {request ? 'Edit this request' : 'New request'}
                {activeRequest && (
                  <span className="rounded-full bg-success/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-success">
                    Active
                  </span>
                )}
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
                className="w-full rounded-md border border-input bg-background p-4 text-sm leading-relaxed text-foreground outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-ring focus:ring-1 focus:ring-ring"
              />

              <ChangeBanner verdict={verdict} busy={classifying} isNew={!requestId} />

              {message && (
                <div
                  className={cn(
                    'rounded-md border px-4 py-3 text-sm',
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
                  {verdict?.new_profile_required ? 'Save as new request' : 'Save request'}
                </Button>
                {verdict?.new_profile_required && (
                  <Button variant="outline" onClick={() => save(false)} disabled={busy}>
                    Keep my current request
                  </Button>
                )}
                {request && !activeRequest && (
                  <Button variant="outline" onClick={activateRequest} disabled={busy}>
                    Use this request
                  </Button>
                )}
                {request && (
                  <Button variant="destructive" onClick={deleteRequest} disabled={busy}>
                    Delete request
                  </Button>
                )}
                {dirty && <span className="text-xs text-muted-foreground">Unsaved changes</span>}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Judging strictness</CardTitle>
              <CardDescription>
                How confident the judge must be before an accepted ad becomes a lead.
                This does not change what you are looking for.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <label className="block space-y-2">
                <span className="flex items-center justify-between text-sm font-medium text-foreground">
                  <span>Profile judge</span>
                  <span className="tabular-nums text-muted-foreground">
                    {Math.round(profileThreshold * 100)}%
                  </span>
                </span>
                <input
                  type="range"
                  min="0.5"
                  max="0.9"
                  step="0.05"
                  value={profileThreshold}
                  disabled={!request}
                  onChange={(event) => setProfileThreshold(Number(event.target.value))}
                  className="w-full accent-[hsl(var(--primary))]"
                />
                <span className="block text-xs leading-relaxed text-muted-foreground">
                  Higher means fewer “this fits my profile” ads are accepted. Rejections are
                  never affected.
                </span>
              </label>

              <label className="block space-y-2">
                <span className="flex items-center justify-between text-sm font-medium text-foreground">
                  <span>Request judge</span>
                  <span className="tabular-nums text-muted-foreground">
                    {Math.round(requestThreshold * 100)}%
                  </span>
                </span>
                <input
                  type="range"
                  min="0.5"
                  max="0.9"
                  step="0.05"
                  value={requestThreshold}
                  disabled={!request}
                  onChange={(event) => setRequestThreshold(Number(event.target.value))}
                  className="w-full accent-[hsl(var(--primary))]"
                />
                <span className="block text-xs leading-relaxed text-muted-foreground">
                  Higher means an ad must match your current request more confidently.
                  Anything uncertain goes to Not sure yet, not the bin.
                </span>
              </label>

              <p className="rounded-md border border-border/60 bg-muted/40 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                Lowering a threshold re-checks Not sure yet ads on the next run. Raising it
                only affects future judgments — it never removes an existing lead.
              </p>

              {thresholdMessage && (
                <p
                  className={cn(
                    'text-xs',
                    thresholdMessage.tone === 'error' ? 'text-destructive' : 'text-success'
                  )}
                >
                  {thresholdMessage.text}
                </p>
              )}

              <Button
                onClick={saveThresholds}
                loading={thresholdBusy}
                disabled={
                  !request ||
                  (profileThreshold === (request.profile_threshold ?? 0.5) &&
                    requestThreshold === (request.request_threshold ?? 0.5))
                }
              >
                Save strictness
              </Button>
            </CardContent>
          </Card>

        </div>
      </div>
    </PageShell>
  );
}