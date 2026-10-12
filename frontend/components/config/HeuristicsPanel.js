'use client';

import { useCallback, useEffect, useState } from 'react';
import { Button, Input, Badge, Card, CardContent, CardHeader, CardTitle, CardDescription, Spinner } from '@/components/ui';

const FIELDS = ['require_any_ad_text_patterns', 'exclude_ad_text_patterns', 'require_ad_text_patterns'];

const BLURB = {
  require_any_ad_text_patterns:
    'An ad is kept only if its text contains at least one of these. Use this to widen what reaches the AI.',
  exclude_ad_text_patterns:
    'An ad is dropped if its text contains any of these. This is the list you will edit most — it is how a bootcamp or a job board gets filtered out before it costs an AI call.',
  require_ad_text_patterns:
    'An ad is kept only if it contains ALL of these. Left to the category on purpose: every entry narrows the sieve further, and a sieve that matches nothing looks like “no results”, not like a bad filter.',
};

/**
 * The heuristic rules, embedded in the Search page rather than on their own.
 *
 * These were a separate page, which meant every visit to them remounted the
 * search form and threw away the draft — so tuning a rule and then going back
 * to check the effect cost you the search you were setting up. The rule and
 * the thing it acts on belong on one page.
 *
 * `patterns` is controlled by the parent so the draft persists it alongside
 * everything else; `onChange` writes straight back into the form.
 */
export default function HeuristicsPanel({ patterns, onChange }) {
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(true);
  const [inputs, setInputs] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const current = patterns || {};

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/heuristics', { cache: 'no-store' });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Could not load');
      setMeta(body);
      // Seed the form from the saved patterns if it has none yet, so a page
      // reload does not silently reset rules the user already set.
      if (!current.exclude_ad_text_patterns?.length && body.user?.exclude_ad_text_patterns?.length) {
        onChange?.({ ...body.user });
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
    // Intentionally not depending on `current`: this runs once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onChange]);

  useEffect(() => { load(); }, [load]);

  const add = (field) => {
    const value = (inputs[field] || '').trim();
    if (!value) return;
    onChange?.({ ...current, [field]: [...(current[field] || []), value] });
    setInputs((i) => ({ ...i, [field]: '' }));
  };

  const remove = (field, pattern) => {
    onChange?.({ ...current, [field]: (current[field] || []).filter((p) => p !== pattern) });
  };

  async function persist() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/heuristics', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          require_ad_text_patterns: current.require_ad_text_patterns || [],
          require_any_ad_text_patterns: current.require_any_ad_text_patterns || [],
          exclude_ad_text_patterns: current.exclude_ad_text_patterns || [],
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Save failed');
      setMeta(body);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Heuristics</CardTitle>
        <CardDescription>
          Cheap text rules that run before any AI call. A matching ad is kept or dropped on the
          spot; everything else goes to the AI judge.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {error && <p className="text-sm text-destructive">{error}</p>}

        {loading ? (
          <div className="flex justify-center py-6"><Spinner /></div>
        ) : (
          <>
            {FIELDS.map((field) => (
              <div key={field} className="space-y-2.5 border-t border-border pt-4 first:border-0 first:pt-0">
                <div>
                  <p className="text-sm font-semibold text-foreground">
                    {meta?.labels?.[field] || field}
                  </p>
                  <p className="text-xs text-muted-foreground">{BLURB[field]}</p>
                </div>

                <div className="flex flex-wrap gap-2">
                  {(meta?.builtin?.[field] || []).map((p) => (
                    <Badge key={`b-${p}`} variant="outline">{p} · built in</Badge>
                  ))}
                </div>

                <div className="flex flex-wrap gap-2">
                  {(current[field] || []).map((p) => {
                    const hits = meta?.preview?.[field]?.[p];
                    return (
                      <span
                        key={p}
                        className="inline-flex items-center gap-2 rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-sm text-primary"
                      >
                        {p}
                        {typeof hits === 'number' && (
                          <span className="text-xs opacity-70">
                            drops {hits} of {meta.sample_size}
                          </span>
                        )}
                        <button
                          type="button"
                          onClick={() => remove(field, p)}
                          aria-label={`Remove ${p}`}
                          className="opacity-60 hover:opacity-100"
                        >
                          ×
                        </button>
                      </span>
                    );
                  })}
                </div>

                <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); add(field); }}>
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
              </div>
            ))}

            <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
              <Button onClick={persist} loading={busy} variant="secondary">
                Save patterns
              </Button>
              <span className="text-xs text-muted-foreground">
                Saving is what makes them apply to future runs. They are kept with this search draft
                in the meantime.
              </span>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
