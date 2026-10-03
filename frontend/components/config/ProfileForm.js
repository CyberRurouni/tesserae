'use client';

import { useCallback, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from '@/components/ui/Card';
import { cn } from '@/lib/utils';

function Notice({ tone = 'primary', icon, title, children }) {
  const tones = {
    primary: 'border-primary/40 bg-gradient-to-br from-primary/20 via-primary/10 to-transparent text-foreground',
    warning: 'border-warning/40 bg-warning/10 text-foreground',
  };
  const iconTones = {
    primary: 'bg-primary/20 text-primary',
    warning: 'bg-warning/20 text-warning',
  };

  return (
    <div className={cn('flex gap-4 rounded-2xl border p-5', tones[tone])}>
      <span className={cn('flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-lg', iconTones[tone])} aria-hidden="true">
        {icon}
      </span>
      <div className="space-y-1.5">
        <p className="text-sm font-semibold tracking-tight text-foreground">{title}</p>
        <div className="text-sm leading-relaxed text-muted-foreground">{children}</div>
      </div>
    </div>
  );
}

function Field({ id, label, required, value, onChange, placeholder, rows, error, hint, children }) {
  return (
    <div className="space-y-2">
      <label htmlFor={id} className="flex items-center gap-2 text-sm font-semibold text-foreground">
        {label}
        {required ? (
          <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-primary">
            Required
          </span>
        ) : (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            Optional
          </span>
        )}
      </label>
      <textarea
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        rows={rows}
        aria-invalid={error ? 'true' : 'false'}
        className={cn(
          'w-full rounded-xl border bg-background/60 p-4 text-sm leading-relaxed text-foreground transition-colors',
          'placeholder:text-muted-foreground/70 focus:outline-none focus:ring-2 focus:ring-primary/40',
          error ? 'border-destructive' : 'border-input focus:border-primary'
        )}
      />
      {error && (
        <p className="text-sm font-medium text-destructive" role="alert">
          {error}
        </p>
      )}
      {!error && hint && <p className="text-xs leading-relaxed text-muted-foreground">{hint}</p>}
      {children}
    </div>
  );
}

export default function ProfileForm({ onSave, initialData = {}, runMode = 'generate' }) {
  // Parent supplies a `key` derived from the loaded profile, so switching
  // between profiles remounts this component instead of syncing via effects.
  const [aboutUs, setAboutUs] = useState(initialData.about_us || '');
  const [lookingFor, setLookingFor] = useState(initialData.additional_filters || '');
  const [isSaving, setIsSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [aboutUsError, setAboutUsError] = useState('');
  const [lookingForError, setLookingForError] = useState('');

  const isAiMode = runMode === 'generate';

  const handleSave = useCallback(async () => {
    // Only the AI needs the profile to invent keywords, so this is required in
    // AI mode alone. With manual keywords nothing here blocks a search.
    if (isAiMode && !aboutUs.trim()) {
      setAboutUsError('Tell us who you are so Tesserae can write search keywords for you.');
      return;
    }
    setAboutUsError('');

    setIsSaving(true);
    try {
      await onSave?.({ about_us: aboutUs.trim(), additional_filters: lookingFor.trim() });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } finally {
      setIsSaving(false);
    }
  }, [aboutUs, lookingFor, isAiMode, onSave]);

  return (
    <Card className="animate-fade-up">
      <CardHeader>
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary/15 text-lg" aria-hidden="true">
            👤
          </span>
          <div className="space-y-1">
            <CardTitle>About You</CardTitle>
            <CardDescription>
              Two short answers. Tesserae uses them to judge which job ads are actually right for you.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        {isAiMode ? (
          <Notice tone="primary" icon="🤖" title="AI Keyword Generation Enabled">
            The AI will write its own search keywords using your profile above.
            <span className="mt-1.5 block">
              Only <span className="font-medium text-foreground">About You</span> is required. &ldquo;What Are You Looking For&rdquo; is
              optional, but the more detail you give it, the better the AI can tell which job ads are actually right for you.
            </span>
          </Notice>
        ) : (
          <Notice tone="warning" icon="🔍" title="Manual Keyword Mode">
            You are supplying the search keywords yourself in the filters panel, so at least one keyword is required there.
            <span className="mt-1.5 block">
              Both boxes here are optional — filling them in still improves how ads are judged.
            </span>
          </Notice>
        )}

        <Field
          id="about_us"
          label="About You"
          required={isAiMode}
          rows={7}
          value={aboutUs}
          onChange={(value) => {
            setAboutUs(value);
            if (aboutUsError) setAboutUsError('');
          }}
          error={aboutUsError}
          placeholder="I'm a Python backend developer with 5 years of experience building scalable APIs. I've worked with FastAPI, PostgreSQL and AWS, mostly in fintech. I'm looking for senior or lead roles."
          hint={isAiMode
            ? 'Your background, skills, experience and what makes you stand out. The AI turns this into search keywords.'
            : 'Your background, skills, experience and what makes you stand out. Optional here — it only sharpens how ads are judged.'}
        />

        <Field
          id="looking_for"
          label="What Are You Looking For?"
          required={false}
          rows={6}
          value={lookingFor}
          onChange={(value) => {
            setLookingFor(value);
            if (lookingForError) setLookingForError('');
          }}
          error={lookingForError}
          placeholder="Remote or Islamabad-based senior Python roles. Not on-site unless in Islamabad. Prefer fintech, SaaS or AI companies. Open to contract or full-time."
          hint="Your ideal role, location preferences, deal-breakers and preferred type of company. Always optional, but it helps the AI judge ads far more accurately."
        />
      </CardContent>

      <CardFooter className="flex items-center justify-between gap-3 border-t border-border/60 pt-5">
        <span className={cn('text-xs font-medium transition-opacity', saved ? 'text-success opacity-100' : 'opacity-0')} aria-live="polite">
          ✓ Profile saved
        </span>
        <Button onClick={handleSave} loading={isSaving} size="lg" className="w-full sm:w-auto">
          Save Profile
        </Button>
      </CardFooter>
    </Card>
  );
}