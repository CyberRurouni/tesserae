import { cn } from '@/lib/utils';

const base = 'rounded-lg border border-border bg-card text-card-foreground';

function Card({ className, ...props }) {
  return <div className={cn(base, className)} {...props} />;
}

function CardHeader({ className, ...props }) {
  return <div className={cn('flex flex-col space-y-1 p-5 pb-4', className)} {...props} />;
}

function CardTitle({ className, as: Tag = 'h3', ...props }) {
  return <Tag className={cn('text-base font-medium leading-tight tracking-tight', className)} {...props} />;
}

function CardDescription({ className, ...props }) {
  return <p className={cn('text-sm leading-relaxed text-muted-foreground', className)} {...props} />;
}

function CardContent({ className, ...props }) {
  return <div className={cn('p-5 pt-0', className)} {...props} />;
}

function CardFooter({ className, ...props }) {
  return <div className={cn('flex items-center p-5 pt-0', className)} {...props} />;
}

export { Card, CardHeader, CardFooter, CardTitle, CardDescription, CardContent };
