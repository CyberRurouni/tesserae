'use client';

import { cn } from '@/lib/utils';

function Badge({ className, variant = 'default', children, ...props }) {
  const variants = {
    default: 'bg-primary/10 text-primary border border-primary/20',
    success: 'bg-green-500/10 text-green-700 dark:text-green-400 border border-green-500/20 dark:border-green-500/30',
    warning: 'bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 border border-yellow-500/20 dark:border-yellow-500/30',
    error: 'bg-red-500/10 text-red-700 dark:text-red-400 border border-red-500/20 dark:border-red-500/30',
    info: 'bg-blue-500/10 text-blue-700 dark:text-blue-400 border border-blue-500/20 dark:border-blue-500/30',
    destructive: 'bg-destructive/10 text-destructive border border-destructive/20',
    outline: 'text-foreground border-border',
  };

  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium border transition-colors',
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