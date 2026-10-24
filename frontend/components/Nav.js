'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/utils';

const LINKS = [
  { href: '/', label: 'Dashboard' },
  { href: '/about', label: 'About you' },
  { href: '/looking-for', label: 'Looking for' },
  { href: '/config', label: 'Search' },
  { href: '/runs', label: 'Runs' },
  { href: '/verdicts', label: 'Decisions' },
];

function useActiveRun() {
  const [active, setActive] = useState(null);
  useEffect(() => {
    let on = true;
    const pull = async () => {
      try {
        const res = await fetch('/api/runs/active', { cache: 'no-store' });
        const body = res.ok ? await res.json() : null;
        if (on) setActive(body?.active ?? null);
      } catch {
        if (on) setActive(null);
      }
    };
    pull();
    const timer = setInterval(pull, 5000);
    return () => {
      on = false;
      clearInterval(timer);
    };
  }, []);
  return active;
}

export function Nav() {
  const pathname = usePathname();
  const active = useActiveRun();
  const isActive = (href) => (href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`));

  return (
    <header className="sticky top-0 z-40 border-b border-border/70 bg-background/90 backdrop-blur-md">
      <div className="mx-auto w-full max-w-6xl px-6 md:px-10">
        <div className="flex items-center justify-between gap-4 py-4">
          <Link href="/" className="group flex min-w-0 items-baseline gap-3">
            <span className="font-serif text-3xl tracking-tight transition-colors group-hover:text-primary">
              Tesserae
            </span>
            <span className="hidden text-xs text-muted-foreground sm:block">
              Find the ads that fit you.
            </span>
          </Link>
          {active && (
            <Link
              href={`/runs/${active.run_id}`}
              className="flex max-w-[16rem] shrink-0 items-center gap-2 rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-foreground/25 hover:text-foreground"
            >
              <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-primary" aria-hidden="true" />
              <span className="truncate">
                Search running{active.current_keyword ? `: ${active.current_keyword}` : ''}
              </span>
            </Link>
          )}
        </div>

        <nav className="-mb-px flex overflow-x-auto" aria-label="Main">
          {LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              aria-current={isActive(link.href) ? 'page' : undefined}
              className={cn(
                'relative whitespace-nowrap px-4 py-3 text-sm transition-colors first:pl-0',
                isActive(link.href)
                  ? 'text-primary after:absolute after:inset-x-4 after:-bottom-px after:h-0.5 after:rounded-full after:bg-primary first:after:left-0'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              {link.label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}

export function PageShell({ title, description, children, actions }) {
  return (
    <div className="mx-auto w-full max-w-6xl space-y-8 px-6 py-8 md:px-10">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2">
          <h1 className="font-serif text-3xl italic tracking-tight">{title}</h1>
          {description && (
            <p className="max-w-3xl text-sm leading-relaxed text-muted-foreground">{description}</p>
          )}
        </div>
        {actions && <div className="flex shrink-0 gap-2">{actions}</div>}
      </header>
      {children}
    </div>
  );
}
