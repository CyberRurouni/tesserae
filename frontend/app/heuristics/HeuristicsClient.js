'use client';

import { useCallback, useEffect, useState } from 'react';
import { PageShell } from '@/components/Nav';
import { Button, Input, Badge, Card, CardContent, CardHeader, CardTitle, CardDescription, Spinner } from '@/components/ui';

const FIELDS = [
  'require_any_ad_text_patterns',
  'exclude_ad_text_patterns',
  'require_ad_text_patterns',
];

const BLURB = {
  require_any_ad_text_patterns:
    'An ad is kept only if its text contains at least one of these. Use this to widen what reaches the AI.',
  exclude_ad_text_patterns:
    'An ad is dropped if its text contains any of these. This is the list you will edit most — it is how a bootcamp or a job board gets filtered out before it costs an AI call.',
  require_ad_text_patterns:
    'An ad is kept only if it contains ALL of these. Left to the category on purpose: every entry narrows the sieve further, and a sieve that matches nothing looks like “no results”, not like a bad filter.',
};

export default function HeuristicsClient() {
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState({});
  const [inputs, setInputs] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [flash, setFlash] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/heuristics', { cache: 'no-store' });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Could not load');
      setData(body);
      setDraft(body.user || {});
      setInputs({});
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  function add(field) {
    const value = (inputs[field] || '').trim();
    if (!value) return;
    setDraft((d) => ({ ...d, [field]: [...(d[field] || []), value] }));
    setInputs((i) => ({ ...i, [field]: '' }));
  }

  function remove(field, pattern) {
    setDraft((d) => ({ ...d, [field]: (d[field] || []).filter((p) => p !== pattern) }));
  }

  async function save() {
    setSaving(true);
    setError(null);
    setFlash(null);
    try {
      const res = await fetch('/api/heuristics', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          require_ad_text_patterns: draft.require_ad_text_patterns || [],
          require_any_ad_text_patterns: draft.require_any_ad_text_patterns || [],
          exclude_ad_text_patterns: draft.exclude_ad_text_patterns || [],
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Save failed');
      setDraft(body.user);
      setFlash('Saved. The next run will use these.');
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  const dirty =
    data &&
    FIELDS.some(
      (f) => JSON.stringify([...(draft[f] || [])].sort()) !== JSON.stringify([...(data.user[f] || [])].sort())
    );

  return (
    <PageShell
      eyebrow="Filters"
      title="Heuristics"
      description="Cheap text rules that run before any AI call. A matching ad is kept or dropped on the spot; everything else goes to the AI judge."
      actions={
        <Button onClick={save} disabled={!dirty || saving} loading={saving}>
          Save patterns
        </Button>
      }
    >
      {loading ? (
        <div className="flex justify-center py-16"><Spinner /></div>
      ) : !data ? (
        <p className="text-sm text-destructive">{error}</p>
      ) : (
        <>
          <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
            {data.note}
          </div>

          {flash && <p className="text-sm text-green-600 dark:text-green-400">{flash}</p>}
          {error && <p className="text-sm text-destructive">{error}</p>}

          {FIELDS.map((field) => (
            <Card key={field}>
              <CardHeader>
                <CardTitle>{data.labels[field]}</CardTitle>
                <CardDescription>{BLURB[field]}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex flex-wrap gap-2">
                  {(data.builtin[field] || []).map((p) => (
                    <Badge key={`b-${p}`} variant="outline">
                      {p} · built in
                    </Badge>
                  ))}
                  {!data.builtin[field]?.length && (
                    <span className="text-sm text-muted-foreground">No built-in patterns.</span>
                  )}
                </div>

                <div className="flex flex-wrap gap-2">
                  {(draft[field] || []).map((p) => {
                    const hits = data.preview?.[field]?.[p];
                    return (
                      <span
                        key={p}
                        className="inline-flex items-center gap-2 rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-sm text-primary"
                      >
                        {p}
                        {typeof hits === 'number' && (
                          <span className="text-xs opacity-70">
                            drops {hits} of {data.sample_size}
                          </span>
                        )}
                        <button
                          onClick={() => remove(field, p)}
                          aria-label={`Remove ${p}`}
                          className="opacity-60 hover:opacity-100"
                        >
                          ×
                        </button>
                      </span>
                    );
                  })}
                  {!draft[field]?.length && (
                    <span className="text-sm text-muted-foreground">
                      You have not added any patterns here.
                    </span>
                  )}
                </div>

                <form
                  className="flex flex-wrap gap-2"
                  onSubmit={(e) => { e.preventDefault(); add(field); }}
                >
                  <Input
                    value={inputs[field] || ''}
                    onChange={(e) => setInputs((i) => ({ ...i, [field]: e.target.value }))}
                    placeholder="e.g. bootcamp"
                    className="max-w-xs"
                  />
                  <Button type="submit" variant="secondary" disabled={!(inputs[field] || '').trim()}>
                    Add
                  </Button>
                </form>
              </CardContent>
            </Card>
          ))}
        </>
      )}
    </PageShell>
  );
}
