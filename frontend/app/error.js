'use client';

/**
 * Stops a crash from being invisible.
 *
 * There was no boundary here before, so any component that threw while rendering
 * produced a plain white page — no message, no stack, nothing to read. That is
 * not hypothetical: the Search page rendered blank on every load because a
 * dependency array read `lens` four lines above its `const`, which throws
 * "Cannot access 'lens' before initialization". It was found by accident, while
 * verifying something else, because the page gave no hint at all.
 *
 * React catches the error and this renders instead. The point is the message:
 * `error.message` is exactly what a developer needs and exactly what a white
 * screen threw away. `reset()` re-renders the segment, which is worth trying for
 * the failures that are transient rather than real.
 *
 * `data-page-error` is not decoration — scripts/page_smoke.py looks for it, so a
 * crash is detected without waiting for a person to open the page.
 */

import { useEffect } from 'react';
import { Button } from '@/components/ui/Button';

export default function Error({ error, reset }) {
  useEffect(() => {
    // Also to the console, so it is in the terminal running `next dev` and not
    // only on the screen.
    console.error('Page crashed:', error);
  }, [error]);

  return (
    <div data-page-error="true" className="flex min-h-[60vh] items-center justify-center p-6">
      <div className="w-full max-w-2xl rounded-xl border border-destructive/40 bg-destructive/5 p-6">
        <div className="flex items-start gap-3">
          <span
            className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-destructive/15 text-lg"
            aria-hidden="true"
          >
            ⚠️
          </span>
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold text-foreground">
              This page failed to load
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Something in the page broke while it was drawing. Nothing was saved or
              lost. Trying again often clears it if the backend was briefly
              unreachable.
            </p>

            <pre
              className="mt-4 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-background/60 p-3 font-mono text-xs text-foreground"
              data-page-error-message="true"
            >
              {error?.message || 'No error message was provided.'}
              {error?.digest ? `\n\nReference: ${error.digest}` : ''}
            </pre>

            <div className="mt-4 flex flex-wrap gap-2">
              <Button variant="primary" onClick={reset}>
                Try again
              </Button>
              <Button variant="outline" onClick={() => window.location.reload()}>
                Reload the page
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}