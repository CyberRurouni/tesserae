'use client';

import { useCallback, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from '@/components/ui/Card';
import { Select } from '@/components/ui/Select';
import { cn } from '@/lib/utils';

const PLATFORMS = [
  { value: 'facebook', label: 'Facebook', hint: 'Feed & video ads' },
  { value: 'instagram', label: 'Instagram', hint: 'Feed, stories & reels' },
  { value: 'messenger', label: 'Messenger', hint: 'Inbox ads' },
  { value: 'audience_network', label: 'Audience Network', hint: 'Off-platform apps' },
  { value: 'whatsapp', label: 'WhatsApp', hint: 'Status & inbox ads' },
  { value: 'threads', label: 'Threads', hint: 'Threads ads' },
];

const DATE_OPTIONS = [
  { value: 'any', label: 'Any time' },
  { value: 'last_7_days', label: 'Last 7 days' },
  { value: 'last_30_days', label: 'Last 30 days' },
];

const AD_AGE_PRESETS = [
  { value: '', label: 'Any age' },
  { value: '24', label: 'Last 24 hours' },
  { value: '168', label: 'Last 7 days' },
  { value: '720', label: 'Last 30 days' },
];

// Single source of truth for the default run mode. ConfigClient reads this too,
// so the About You notice can never disagree with the selected option.
export const INITIAL_RUN_MODE = 'existing';

// Kept in step with core.schemas.run.MAX_PARALLEL_WORKERS.
export const MAX_PARALLEL_WORKERS = 3;

const INITIAL = {
  category: 'career_jobs',
  delivery_status: 'active',
  platforms: PLATFORMS.map((p) => p.value),
  media_type: 'all',
  date_preset: 'any',
  ad_age_hours: '',
  regions: 'ALL',
  languages: 'en',
  sort_order: 'relevance',
  keywords: [],
  run_mode: INITIAL_RUN_MODE,
  cycles: 3,
  run_until_complete: false,
  parallel_workers: 1,
};

function formatKeywordPreview(keywords, max = 3) {
  if (!keywords.length) return '';
  const head = keywords.slice(0, max).join(', ');
  return keywords.length > max ? `${head} +${keywords.length - max} more` : head;
}

function Section({ title, hint, children, className, error }) {
  return (
    <div className={cn('rounded-2xl border border-border/60 bg-background/40 p-5 space-y-4', error && 'border-destructive/60', className)}>
      <div className="space-y-1">
        <h4 className="panel-title flex items-center gap-2">{title}</h4>
        {hint && <p className="text-xs leading-relaxed text-muted-foreground">{hint}</p>}
      </div>
      {children}
    </div>
  );
}

function ChoiceCard({ selected, onSelect, title, description, icon, tag, tagTone }) {
  const tagTones = {
    optional: 'bg-muted text-muted-foreground',
    required: 'bg-primary/15 text-primary',
  };

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        'group relative flex w-full items-start gap-3 rounded-xl border-2 p-4 text-left transition-all',
        selected
          ? 'border-primary bg-primary/10 shadow-[0_0_0_1px_hsl(var(--primary)/0.4)]'
          : 'border-border/60 bg-background/40 hover:border-primary/40 hover:bg-accent/40'
      )}
    >
      <span className={cn('mt-0.5 text-lg', !selected && 'opacity-60 group-hover:opacity-100')}>{icon}</span>
      <span className="flex-1 space-y-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-foreground">{title}</span>
          {tag && (
            <span className={cn('rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide', tagTones[tagTone] || tagTones.optional)}>
              {tag}
            </span>
          )}
        </span>
        <span className="block text-xs leading-relaxed text-muted-foreground">{description}</span>
      </span>
      <span
        className={cn(
          'mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2 transition-colors',
          selected ? 'border-primary bg-primary' : 'border-muted-foreground/40'
        )}
      >
        {selected && <span className="h-1.5 w-1.5 rounded-full bg-primary-foreground" />}
      </span>
    </button>
  );
}

function KeywordInput({ keywords, onChange, error }) {
  const [draft, setDraft] = useState('');
  const inputRef = useRef(null);

  const addKeyword = useCallback(
    (raw) => {
      const value = (raw ?? draft).trim().replace(/\s+/g, ' ');
      if (!value) return false;
      const exists = keywords.some((k) => k.toLowerCase() === value.toLowerCase());
      if (exists) {
        setDraft('');
        inputRef.current?.focus();
        return false;
      }
      onChange([...keywords, value]);
      setDraft('');
      return true;
    },
    [draft, keywords, onChange]
  );

  const removeKeyword = useCallback(
    (index) => onChange(keywords.filter((_, i) => i !== index)),
    [keywords, onChange]
  );

  const handleKeyDown = (event) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      if (addKeyword()) inputRef.current?.focus();
    } else if (event.key === 'Backspace' && !draft && keywords.length) {
      removeKeyword(keywords.length - 1);
    }
  };

  return (
    <div className="space-y-3">
      <div
        className={cn(
          'flex flex-col gap-2 rounded-xl border bg-background/60 p-2 transition-colors sm:flex-row sm:items-center',
          error ? 'border-destructive' : 'border-input focus-within:border-primary focus-within:ring-2 focus-within:ring-primary/30'
        )}
      >
        <input
          ref={inputRef}
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          onBlur={() => addKeyword()}
          placeholder="Type a keyword, e.g. python developer"
          aria-label="Add a search keyword"
          className="h-10 min-w-0 flex-1 bg-transparent px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground"
        />
        <Button
          type="button"
          onClick={() => {
            if (addKeyword()) inputRef.current?.focus();
          }}
          size="md"
          className="shrink-0"
          aria-label="Add keyword"
        >
          <span className="text-base leading-none" aria-hidden="true">+</span>
          Add
        </Button>
      </div>

      {keywords.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {keywords.map((keyword, index) => (
            <span
              key={`${keyword}-${index}`}
              className="group inline-flex items-center gap-2 rounded-full border border-primary/30 bg-primary/10 py-1 pl-3 pr-1.5 text-xs font-medium text-foreground"
            >
              <span className="max-w-[16rem] truncate">{keyword}</span>
              <button
                type="button"
                onClick={() => removeKeyword(index)}
                aria-label={`Remove keyword ${keyword}`}
                className="flex h-5 w-5 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-destructive/20 hover:text-destructive"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {error && (
        <p className="text-sm font-medium text-destructive" role="alert">
          {error}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        {keywords.length} keyword{keywords.length === 1 ? '' : 's'} added. Press Enter or the + button to add each one.
      </p>
    </div>
  );
}

export default function FilterBuilder({ onStart, onRunModeChange, initialData }) {
  const initial = useMemo(() => {
    const merged = { ...INITIAL, ...(initialData || {}) };

    // Restored selections arrive with array-valued platform/language fields;
    // native <select> needs a scalar, so flatten them here.
    const restoredLanguages = Array.isArray(merged.languages)
      ? merged.languages.filter((lang) => String(lang).toLowerCase() !== 'all')
      : String(merged.languages ?? 'en')
          .split(',')
          .map((lang) => lang.trim())
          .filter((lang) => lang && lang.toLowerCase() !== 'all');

    const platforms = Array.isArray(merged.platforms) && merged.platforms.length ? merged.platforms : INITIAL.platforms;

    return {
      ...merged,
      platforms,
      // Keywords are deliberately NOT restored. They go stale fast, so the box
      // always starts empty and the last set is opt-in via the checkbox.
      keywords: [],
      languages: restoredLanguages[0] || 'all',
      ad_age_hours: merged.ad_age_hours == null ? '' : String(merged.ad_age_hours),
      cycles: Number(merged.cycles) || INITIAL.cycles,
      parallel_workers: Math.min(
        Math.max(Number(merged.parallel_workers) || INITIAL.parallel_workers, 1),
        MAX_PARALLEL_WORKERS
      ),
    };
  }, [initialData]);

  // The previous run's keywords, kept for the opt-in checkbox only. These live
  // under their own `last_keywords` key so that starting a run with an empty
  // keyword box cannot erase them.
  const lastKeywords = useMemo(() => {
    const stored = Array.isArray(initialData?.last_keywords)
      ? initialData.last_keywords
      : Array.isArray(initialData?.keywords)
        ? initialData.keywords
        : initialData?.keywords
          ? [initialData.keywords]
          : [];
    const seen = new Set();
    return stored.reduce((list, value) => {
      const keyword = String(value ?? '').trim().replace(/\s+/g, ' ');
      if (!keyword) return list;
      const key = keyword.toLowerCase();
      if (seen.has(key)) return list;
      seen.add(key);
      list.push(keyword);
      return list;
    }, []);
  }, [initialData]);

  const [formData, setFormData] = useState(initial);
  const [errors, setErrors] = useState({});
  const [isStarting, setIsStarting] = useState(false);
  const [useLastKeywords, setUseLastKeywords] = useState(false);

  const update = useCallback((field, value) => {
    setFormData((prev) => ({ ...prev, [field]: value }));
    setErrors((prev) => (prev[field] ? { ...prev, [field]: undefined } : prev));
    if (field === 'run_mode') onRunModeChange?.(value);
  }, [onRunModeChange]);

  const setKeywords = useCallback((value) => update('keywords', value), [update]);

  // Seeding is a one-off: once the box is ticked the user owns the list and can
  // add or remove keywords freely without the checkbox clobbering their edits.
  const toggleLastKeywords = useCallback(
    (enabled) => {
      setUseLastKeywords(enabled);
      setKeywords(enabled ? [...lastKeywords] : []);
    },
    [lastKeywords, setKeywords]
  );

  const allPlatformsSelected = formData.platforms.length === PLATFORMS.length;

  const togglePlatform = useCallback((value) => {
    setFormData((prev) => {
      const selected = prev.platforms.includes(value);
      const next = selected ? prev.platforms.filter((p) => p !== value) : [...prev.platforms, value];
      return { ...prev, platforms: next };
    });
    setErrors((prev) => ({ ...prev, platforms: undefined }));
  }, []);

  const selectAllPlatforms = useCallback(() => {
    setFormData((prev) => ({ ...prev, platforms: INITIAL.platforms }));
    setErrors((prev) => ({ ...prev, platforms: undefined }));
  }, []);

  const validate = useCallback(() => {
    const next = {};
    if (!formData.platforms.length) next.platforms = 'Choose at least one platform, or select All.';
    if (!formData.keywords.length && formData.run_mode === 'existing') {
      next.keywords = 'Add at least one keyword, or switch to AI keyword generation.';
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  }, [formData]);

  const handleStart = useCallback(async () => {
    if (!validate()) {
      document.querySelector('[data-invalid="true"]')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    setIsStarting(true);
    try {
      await onStart?.(formData);
    } finally {
      setIsStarting(false);
    }
  }, [formData, onStart, validate]);

  return (
    <Card className="animate-fade-up">
      <CardHeader>
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary/15 text-lg" aria-hidden="true">
            🔍
          </span>
          <div className="space-y-1">
            <CardTitle>Search Filters</CardTitle>
            <CardDescription>
              Tell Tesserae what to look for. Everything here has a sensible default — you only need to add your keywords.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        <Section title="Category" hint="The only category Tesserae searches today.">
          <Select aria-label="Category" className="opacity-60" value="career_jobs" disabled onChange={() => {}} options={[{ value: 'career_jobs', label: 'Career & Job Opportunities' }]} />
        </Section>

        <Section title="Your Keywords" hint="Each keyword can contain spaces — it is searched exactly as you type it." error={!!errors.keywords}>
          {lastKeywords.length > 0 && (
            <label
              className={cn(
                'flex cursor-pointer items-start gap-3 rounded-xl border-2 p-4 transition-all',
                useLastKeywords ? 'border-primary bg-primary/10' : 'border-border/60 hover:border-primary/40'
              )}
            >
              <input
                type="checkbox"
                checked={useLastKeywords}
                onChange={(e) => toggleLastKeywords(e.target.checked)}
                className="mt-0.5 h-4 w-4 shrink-0 accent-[hsl(var(--primary))]"
              />
              <span className="min-w-0 space-y-1">
                <span className="block text-sm font-semibold text-foreground">Use my last keywords</span>
                <span className="block text-xs leading-relaxed text-muted-foreground">
                  Last time you searched for{' '}
                  <span className="font-medium text-foreground">{formatKeywordPreview(lastKeywords)}</span>. Tick this to load
                  them — you can still remove or add keywords afterwards.
                </span>
              </span>
            </label>
          )}

          <div data-invalid={errors.keywords ? 'true' : undefined}>
            <KeywordInput keywords={formData.keywords} onChange={setKeywords} error={errors.keywords} />
          </div>
        </Section>

        <Section title="Where should we look?" hint="Pick the platforms to search. “All” searches every platform at once." error={!!errors.platforms}>
          <div data-invalid={errors.platforms ? 'true' : undefined} className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="text-xs font-medium text-muted-foreground">
                {allPlatformsSelected
                  ? 'All platforms selected'
                  : `${formData.platforms.length} of ${PLATFORMS.length} platforms selected`}
              </span>
              {!allPlatformsSelected && (
                <button type="button" onClick={selectAllPlatforms} className="text-xs font-semibold text-primary hover:underline">
                  Select all
                </button>
              )}
            </div>

            <button
              type="button"
              onClick={selectAllPlatforms}
              aria-pressed={allPlatformsSelected}
              className={cn(
                'flex w-full items-center gap-3 rounded-xl border-2 px-4 py-3 text-left transition-all',
                allPlatformsSelected ? 'border-primary bg-primary/10' : 'border-border/60 hover:border-primary/40'
              )}
            >
              <span className="text-lg" aria-hidden="true">🌐</span>
              <span className="flex-1">
                <span className="block text-sm font-semibold text-foreground">All platforms</span>
                <span className="block text-xs text-muted-foreground">Facebook, Instagram, Messenger, Audience Network, WhatsApp &amp; Threads</span>
              </span>
              <span className="text-xs font-semibold text-primary">Recommended</span>
            </button>

            <div className="grid gap-2 sm:grid-cols-2">
              {PLATFORMS.map((platform) => {
                const selected = formData.platforms.includes(platform.value);
                return (
                  <button
                    key={platform.value}
                    type="button"
                    data-platform={platform.value}
                    onClick={() => togglePlatform(platform.value)}
                    aria-pressed={selected}
                    className={cn(
                      'flex items-center gap-3 rounded-xl border px-4 py-3 text-left transition-all',
                      selected ? 'border-primary/70 bg-primary/5' : 'border-border/60 hover:border-primary/40 hover:bg-accent/40'
                    )}
                  >
                    <span
                      className={cn(
                        'flex h-4 w-4 shrink-0 items-center justify-center rounded border-2 transition-colors',
                        selected ? 'border-primary bg-primary' : 'border-muted-foreground/40'
                      )}
                    >
                      {selected && (
                        <svg viewBox="0 0 10 10" className="h-2.5 w-2.5 fill-none stroke-primary-foreground stroke-[2.5]" aria-hidden="true">
                          <path d="M1.5 5.2 4 7.6 8.6 2.4" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      )}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-foreground">{platform.label}</span>
                      <span className="block truncate text-[11px] text-muted-foreground">{platform.hint}</span>
                    </span>
                  </button>
                );
              })}
            </div>

            {errors.platforms && (
              <p className="text-sm font-medium text-destructive" role="alert">
                {errors.platforms}
              </p>
            )}
          </div>
        </Section>

        <Section title="How should keywords be chosen?">
          <div className="space-y-2">
            <ChoiceCard
              selected={formData.run_mode === 'generate'}
              onSelect={() => update('run_mode', 'generate')}
              icon="🤖"
              tag="Keywords optional"
              tagTone="optional"
              title="Let AI generate keywords for me"
              description="Tesserae reads your About You and What Are You Looking For answers, then invents its own search keywords for you. Any keywords you added above are searched first and are combined with the AI ones — you can leave this box empty entirely."
            />
            <ChoiceCard
              selected={formData.run_mode === 'existing'}
              onSelect={() => update('run_mode', 'existing')}
              icon="🔍"
              tag="Keywords required"
              tagTone="required"
              title="Only use my keywords"
              description="Search exactly the keywords you added above, one time. No AI keywords are generated, so at least one keyword is required."
            />
          </div>
        </Section>

        <Section title="How long should it search?" hint="One cycle = one full pass through all keywords.">
          <div className="space-y-4">
            <label
              className={cn(
                'flex cursor-pointer items-start gap-3 rounded-xl border-2 p-4 transition-all',
                formData.run_until_complete ? 'border-primary bg-primary/10' : 'border-border/60 hover:border-primary/40'
              )}
            >
              <input
                type="checkbox"
                checked={formData.run_until_complete}
                onChange={(e) => update('run_until_complete', e.target.checked)}
                className="mt-1 h-4 w-4 shrink-0 accent-[hsl(var(--primary))]"
              />
              <span>
                <span className="block text-sm font-semibold text-foreground">Keep going until everything is found</span>
                <span className="mt-0.5 block text-xs leading-relaxed text-muted-foreground">
                  Recommended. Tesserae keeps searching and generating until a full pass finds nothing new. It can take a while.
                </span>
              </span>
            </label>

            <div className="flex flex-col gap-3 rounded-xl border border-border/60 p-4 sm:flex-row sm:items-center">
              <label htmlFor="parallel_workers" className="text-sm font-medium text-foreground sm:w-40">
                Search speed
              </label>
              <input
                id="parallel_workers"
                type="range"
                min="1"
                max={MAX_PARALLEL_WORKERS}
                step="1"
                value={formData.parallel_workers}
                onChange={(e) => update('parallel_workers', Number(e.target.value))}
                className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-border accent-[hsl(var(--primary))]"
              />
              <span className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm font-semibold text-foreground sm:w-32 text-center">
                {formData.parallel_workers} at a time
              </span>
            </div>
            <p className="text-xs text-muted-foreground">
              {formData.parallel_workers === 1
                ? 'One keyword at a time — slowest, but least likely to be challenged.'
                : `${formData.parallel_workers} keywords searched at once. Faster, but a higher chance of a captcha. Drop back to 1 if you get blocked.`}
            </p>

            <div className={cn('flex flex-col gap-3 sm:flex-row sm:items-center', formData.run_until_complete && 'opacity-50')}>
              <label htmlFor="cycles" className="text-sm font-medium text-foreground sm:w-40">
                Maximum cycles
              </label>
              <input
                id="cycles"
                type="range"
                min="1"
                max="20"
                step="1"
                disabled={formData.run_until_complete}
                value={formData.cycles}
                onChange={(e) => update('cycles', Number(e.target.value))}
                className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-border accent-[hsl(var(--primary))]"
              />
              <span className="rounded-lg border border-border bg-background px-3 py-1.5 text-sm font-semibold text-foreground sm:w-16 text-center">
                {formData.cycles}
              </span>
            </div>
            <p className="text-xs text-muted-foreground">
              {formData.run_until_complete
                ? 'Cycle limit is switched off — the search runs until it finds nothing new.'
                : `Up to ${formData.cycles} round${formData.cycles === 1 ? '' : 's'} of searching.`}
            </p>
          </div>
        </Section>

        <Section title="Refine results" hint="Optional. Defaults are already good for most people.">
          <div className="grid gap-4 sm:grid-cols-2">
            <Select
              label="Ad status"
              value={formData.delivery_status}
              onChange={(v) => update('delivery_status', v)}
              options={[
                { value: 'active', label: 'Currently running ads only' },
                { value: 'inactive', label: 'Finished ads only' },
                { value: 'all', label: 'All ads, running and finished' },
              ]}
            />
            <Select
              label="Ad format"
              value={formData.media_type}
              onChange={(v) => update('media_type', v)}
              options={[
                { value: 'all', label: 'Any format' },
                { value: 'image', label: 'Image ads' },
                { value: 'video', label: 'Video ads' },
                { value: 'meme', label: 'Meme ads' },
              ]}
            />
            <Select
              label="Posted within"
              value={formData.date_preset}
              onChange={(v) => update('date_preset', v)}
              options={DATE_OPTIONS}
            />
            <Select
              label="Ad age"
              value={formData.ad_age_hours}
              onChange={(v) => update('ad_age_hours', v)}
              options={AD_AGE_PRESETS}
            />
            <Select
              label="Sort by"
              value={formData.sort_order}
              onChange={(v) => update('sort_order', v)}
              options={[
                { value: 'relevance', label: 'Most relevant first' },
                { value: 'date', label: 'Newest first' },
              ]}
            />
            <Select
              label="Ad language"
              value={formData.languages}
              onChange={(v) => update('languages', v)}
              options={[
                { value: 'en', label: 'English' },
                { value: 'all', label: 'Any language' },
              ]}
            />
          </div>
        </Section>
      </CardContent>

      <CardFooter className="flex flex-col gap-3 border-t border-border/60 pt-5 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs leading-relaxed text-muted-foreground">
          {formData.keywords.length
            ? `${formData.keywords.length} keyword${formData.keywords.length === 1 ? '' : 's'} · ${allPlatformsSelected ? 'all platforms' : `${formData.platforms.length} platform${formData.platforms.length === 1 ? '' : 's'}`}`
            : formData.run_mode === 'generate'
              ? 'No keywords added — the AI will write them from your About You.'
              : 'Add at least one keyword to start a search.'}
        </p>
        <Button onClick={handleStart} loading={isStarting} size="lg" className="w-full sm:w-auto">
          Start Search
        </Button>
      </CardFooter>
    </Card>
  );
}