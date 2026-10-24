'use client';

import { forwardRef } from 'react';
import { cn } from '@/lib/utils';

const fieldBase =
  'flex w-full rounded-md border border-input bg-background px-3 text-sm text-foreground placeholder:text-muted-foreground/70 transition-colors focus-visible:border-ring focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50';

function Field({ label, id, error, hint, errorId, hintId, children }) {
  return (
    <div className="w-full">
      {label && (
        <label htmlFor={id} className="mb-1.5 block text-[13px] font-medium text-foreground">
          {label}
        </label>
      )}
      {children}
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
}

const Input = forwardRef(({ className, type = 'text', label, error, hint, id, ...props }, ref) => {
  const inputId = id || label?.toLowerCase().replace(/\s+/g, '-');
  const errorId = `${inputId}-error`;
  const hintId = `${inputId}-hint`;
  const describedBy = [error && errorId, hint && hintId].filter(Boolean).join(' ') || undefined;

  return (
    <Field label={label} id={inputId} error={error} hint={hint} errorId={errorId} hintId={hintId}>
      <input
        ref={ref}
        type={type}
        id={inputId}
        className={cn(fieldBase, 'h-9 py-2 file:border-0 file:bg-transparent file:text-sm file:font-medium', error && 'border-destructive focus-visible:ring-destructive', className)}
        aria-invalid={error ? 'true' : 'false'}
        aria-describedby={describedBy}
        {...props}
      />
    </Field>
  );
});
Input.displayName = 'Input';

const Textarea = forwardRef(({ className, label, error, hint, id, ...props }, ref) => {
  const textareaId = id || label?.toLowerCase().replace(/\s+/g, '-');
  const errorId = `${textareaId}-error`;
  const hintId = `${textareaId}-hint`;
  const describedBy = [error && errorId, hint && hintId].filter(Boolean).join(' ') || undefined;

  return (
    <Field label={label} id={textareaId} error={error} hint={hint} errorId={errorId} hintId={hintId}>
      <textarea
        ref={ref}
        id={textareaId}
        className={cn(fieldBase, 'min-h-[80px] py-2', error && 'border-destructive focus-visible:ring-destructive', className)}
        aria-invalid={error ? 'true' : 'false'}
        aria-describedby={describedBy}
        {...props}
      />
    </Field>
  );
});
Textarea.displayName = 'Textarea';

export { Input, Textarea };
