'use client';

import { useEffect, useRef, useState } from 'react';
import { usePathname } from 'next/navigation';
import { Spinner } from '@/components/ui/Spinner';

export function NavigationProgress() {
  const pathname = usePathname();
  const [pending, setPending] = useState(false);
  const timeout = useRef(null);

  useEffect(() => {
    setPending(false);
    clearTimeout(timeout.current);
  }, [pathname]);

  useEffect(() => {
    const clear = () => {
      clearTimeout(timeout.current);
      setPending(false);
    };

    const onClick = (event) => {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

      const target = event.target && typeof event.target.closest === 'function'
        ? event.target.closest('a[href]')
        : null;
      if (!target) return;
      if (target.target && target.target !== '_self') return;
      if (target.hasAttribute('download')) return;

      let url;
      try {
        url = new URL(target.href, window.location.href);
      } catch {
        return;
      }
      if (url.origin !== window.location.origin) return;
      if (url.pathname === window.location.pathname && url.search === window.location.search) return;

      setPending(true);
      clearTimeout(timeout.current);
      timeout.current = setTimeout(clear, 10000);
    };

    document.addEventListener('click', onClick, true);
    window.addEventListener('popstate', clear);
    return () => {
      document.removeEventListener('click', onClick, true);
      window.removeEventListener('popstate', clear);
      clearTimeout(timeout.current);
    };
  }, []);

  if (!pending) return null;

  return (
    <>
      <div className="pointer-events-none fixed inset-x-0 top-0 z-[70] h-0.5 overflow-hidden bg-primary/15">
        <div
          className="h-full w-1/3 bg-primary"
          style={{ animation: 'nav-progress 1.1s ease-in-out infinite' }}
        />
      </div>
      <div className="fixed right-4 top-3 z-[70] flex items-center gap-2 rounded-full border border-border bg-card/95 px-3 py-1.5 text-xs text-muted-foreground shadow-lg backdrop-blur">
        <Spinner size="sm" className="h-3.5 w-3.5" />
        Loading
      </div>
    </>
  );
}
