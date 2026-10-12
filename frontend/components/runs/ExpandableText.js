'use client';

import { useState } from 'react';
import { cn } from '@/lib/utils';

/**
 * Ad text that can be opened in place.
 *
 * Judging an ad means reading it, and the whole point of a lead list is that
 * you decide whether to spend your time on it. Truncating to three lines
 * forces a trip to the advertiser's page for every borderline ad, which is
 * exactly the decision the list exists to help you make — so the full text is
 * one click away, here, rather than behind a navigation.
 *
 * Collapsed height is a reading cue, not a limit: anything past it is hidden
 * behind the control, and the control always states how much is hidden.
 */
export default function ExpandableText({
  text,
  collapsedLines = 3,
  className,
  label = 'ad text',
}) {
  const [open, setOpen] = useState(false);
  const body = (text || '').trim();

  if (!body) {
    return <p className="text-sm italic text-muted-foreground/60">No {label} stored.</p>;
  }

  const long = body.length > collapsedLines * 70;

  return (
    <div className="space-y-1.5">
      <p
        className={cn(
          'whitespace-pre-wrap text-sm text-muted-foreground',
          !open && 'line-clamp-3',
          className
        )}
      >
        {body}
      </p>
      {long && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="text-xs font-medium text-primary underline-offset-4 hover:underline"
        >
          {open ? 'Show less' : `Read the rest of this ${label} (${body.length} characters)`}
        </button>
      )}
    </div>
  );
}
