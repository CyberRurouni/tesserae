'use client';

import { forwardRef } from 'react';
import { cn } from '@/lib/utils';

const Select = forwardRef(({ className, label, error, hint, id, options = [], placeholder, onChange, onBlur, ...props }, ref) => {
  const selectId = id || label?.toLowerCase().replace(/\s+/g, '-');
  const errorId = `${selectId}-error`;
  const hintId = `${selectId}-hint`;
  const describedBy = [error && errorId, hint && hintId].filter(Boolean).join(' ') || undefined;

  return (
    <div className="w-full">
      {label && (
        <label htmlFor={selectId} className="mb-1.5 block text-[13px] font-medium text-foreground">
          {label}
        </label>
      )}
      <select
        ref={ref}
        id={selectId}
        className={cn(
          'flex h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground transition-colors focus-visible:border-ring focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50',
          error && 'border-destructive focus-visible:ring-destructive',
          className
        )}
        aria-invalid={error ? 'true' : 'false'}
        aria-describedby={describedBy}
        {...props}
        onChange={(event) => onChange?.(event.target.value, event)}
        onBlur={(event) => onBlur?.(event)}
      >
        {placeholder && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {error && (
        <p id={errorId} className="mt-1.5 text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      {hint && !error && (
        <p id={hintId} className="mt-1.5 text-xs text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  );
});
Select.displayName = 'Select';

export { Select };
