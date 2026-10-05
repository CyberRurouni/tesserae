'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/utils';

const LINKS = [
  { href: '/', label: 'Dashboard' },
  { href: '/about', label: 'About you' },
  { href: '/looking-for', label: 'Looking for' },
  { href: '/config', label: 'Search' },
  { href: '/runs', label: 'Runs' },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="border-b border-border/60 bg-background/80 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-1 px-6 py-3 md:px-8">
        <Link href="/" className="mr-4 text-sm font-bold tracking-tight">
          Tesserae
        </Link>
        {LINKS.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className={cn(
              'rounded-lg px-3 py-1.5 text-sm font-medium transition-colors',
              pathname === link.href
                ? 'bg-primary/15 text-primary'
                : 'text-muted-foreground hover:bg-accent/40 hover:text-foreground'
            )}
          >
            {link.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}

export function PageShell({ title, eyebrow, description, children, actions }) {
  return (
    <main className="min-h-screen bg-background">
      <Nav />
      <div className="mx-auto max-w-7xl space-y-8 p-6 md:p-8">
        <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-2">
            {eyebrow && <p className="eyebrow">{eyebrow}</p>}
            <h1 className="text-3xl font-bold tracking-tight md:text-4xl">{title}</h1>
            {description && (
              <p className="max-w-2xl text-sm text-muted-foreground">{description}</p>
            )}
          </div>
          {actions && <div className="flex gap-3">{actions}</div>}
        </header>
        {children}
      </div>
    </main>
  );
}