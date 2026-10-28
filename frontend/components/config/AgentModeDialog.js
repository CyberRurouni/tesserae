'use client';

import { useEffect, useRef } from 'react';
import { Button } from '@/components/ui/Button';

const STEPS = [
  'Set your profile on About you and what you want right now on Looking for.',
  'Save the search settings on this page. Agent mode uses them, and its CLI options update them for next time.',
  'Ask the assistant to run the script. Open Runs while it works to watch the live scrape progress.',
  'The assistant judges every ad that passed the heuristics, then applies the verdicts.',
  'Open Runs and review the finished run. The run page is the result view.',
];

export default function AgentModeDialog({ open, onClose }) {
  const closeRef = useRef(null);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    document.addEventListener('keydown', onKeyDown);
    closeRef.current?.focus();

    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-background/80 backdrop-blur-sm"
        aria-hidden="true"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-mode-title"
        className="relative max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-lg border border-border bg-card p-6 shadow-xl"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Primary workflow
            </p>
            <h2 id="agent-mode-title" className="mt-1 font-serif text-2xl italic tracking-tight">
              Agent mode
            </h2>
          </div>
          <Button
            ref={closeRef}
            variant="ghost"
            size="sm"
            onClick={onClose}
            aria-label="Close agent mode instructions"
          >
            Close
          </Button>
        </div>

        <p className="mt-4 text-sm leading-relaxed text-muted-foreground">
          Agent mode is the primary workflow. The assistant runs the scrape, judges the ads that
          passed the heuristics, and applies the verdicts. You stay in the frontend to review the
          results.
        </p>

        <ol className="mt-5 space-y-3">
          {STEPS.map((step, index) => (
            <li key={step} className="flex gap-3 text-sm leading-relaxed">
              <span className="w-5 shrink-0 text-right tabular-nums text-muted-foreground">
                {index + 1}
              </span>
              <span className="text-foreground">{step}</span>
            </li>
          ))}
        </ol>

        <div className="mt-5 rounded-md border border-border bg-background p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Commands
          </p>
          <pre className="mt-2 overflow-x-auto text-xs leading-relaxed text-foreground">
            <code>{`python3 -m interface.manual scrape
python3 -m interface.manual apply <run_id>`}</code>
          </pre>
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
            The assistant fills{' '}
            <code className="text-foreground">data/agent_audits/verdicts_&lt;run_id&gt;.json</code>{' '}
            between those commands. You do not need to press Start search for this workflow.
          </p>
        </div>

        <div className="mt-5 rounded-md border border-border px-4 py-3 text-xs leading-relaxed text-muted-foreground">
          Legacy mode: <span className="text-foreground">Start search</span> still runs the Gemini
          judge. It remains available, but it is not the primary workflow.
        </div>

        <div className="mt-6 flex justify-end">
          <Button onClick={onClose}>Got it</Button>
        </div>
      </div>
    </div>
  );
}
