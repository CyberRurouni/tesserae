'use client';

import { STATUS_LABELS, STATUS_STYLES } from '@/components/runs/shared';
import { isLiveStatus } from '@/lib/runs';
import { cn } from '@/lib/utils';

/** A dot and a word. Pulses while the run is live. */
export function StatusLabel({ status = 'pending', className }) {
  const live = isLiveStatus(status);
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium',
        STATUS_STYLES[status] || STATUS_STYLES.pending,
        className
      )}
    >
      <span className={cn('h-1.5 w-1.5 rounded-full bg-current', live && 'animate-pulse')} aria-hidden="true" />
      {STATUS_LABELS[status] || status}
    </span>
  );
}
