'use client';

import { cn } from '@/lib/utils';

function Badge({ className, variant = 'default', children, ...props }) {
  const variants = {
    default: 'bg-secondary text-foreground',
    success: 'border-success/20 bg-success/15 text-success',
    warning: 'border-warning/20 bg-warning/15 text-warning',
    error: 'border-destructive/20 bg-destructive/15 text-destructive',
    info: 'border-info/20 bg-info/15 text-info',
    destructive: 'border-destructive/20 bg-destructive/15 text-destructive',
    outline: 'border-border text-muted-foreground',
  };

  return (
    <span
      className={cn(
        'inline-flex items-center rounded border border-transparent px-2 py-0.5 text-xs font-medium',
        variants[variant],
        className
      )}
      {...props}
    >
      {children}
    </span>
  );
}

export { Badge };
