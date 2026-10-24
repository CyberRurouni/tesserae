'use client';

import { forwardRef } from 'react';
import { cn } from '@/lib/utils';

const Button = forwardRef(({ className, variant = 'primary', size = 'md', loading = false, disabled = false, children, ...props }, ref) => {
  const baseStyles =
    'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40';

  const variants = {
    primary: 'bg-primary text-primary-foreground shadow-[0_6px_20px_-10px_hsl(var(--primary)/0.65)] hover:bg-primary/90',
    secondary: 'bg-secondary text-secondary-foreground hover:bg-secondary/60 border border-border',
    ghost: 'text-muted-foreground hover:bg-secondary hover:text-foreground',
    outline: 'border border-border text-foreground hover:bg-secondary',
    destructive: 'bg-destructive text-destructive-foreground hover:bg-destructive/85',
  };

  const sizes = {
    sm: 'h-8 px-3 text-[13px]',
    md: 'h-9 px-4 text-sm',
    lg: 'h-10 px-5 text-sm',
    icon: 'h-9 w-9',
  };

  return (
    <button
      ref={ref}
      className={cn(baseStyles, variants[variant], sizes[size], className)}
      disabled={disabled || loading}
      {...props}
    >
      {loading && (
        <svg className="h-3.5 w-3.5 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
        </svg>
      )}
      {children}
    </button>
  );
});

Button.displayName = 'Button';

export { Button };
