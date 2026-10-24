'use client';

import { useEffect, useRef, useState } from 'react';

const POLL_MS = 2000;
const MAX = 120;

const LEVEL_STYLE = {
  10: 'text-muted-foreground',
  20: 'text-info',
  30: 'text-warning',
  40: 'text-destructive',
  50: 'text-destructive font-semibold',
};

/**
 * The backend's live log tail.
 *
 * A run that takes an hour gives no other way to tell working from wedged, and
 * terminal logs only help if you happen to be looking at the right terminal.
 * This polls the backend's in-memory tail and shows the last lines as they
 * arrive, so the crawl can be followed and stopped on what it says rather than
 * on how long it has been quiet.
 *
 * Polls continuously only while a run is live, but fetches once either way —
 * a finished run has nothing to watch, yet the log is exactly what explains
 * how it ended.
 */
export default function LiveLog({ active = true }) {
  const [lines, setLines] = useState([]);
  const [file, setFile] = useState('');
  const [failed, setFailed] = useState(false);
  const [follow, setFollow] = useState(true);
  const [open, setOpen] = useState(active);
  const boxRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    async function pull() {
      try {
        const res = await fetch(`/api/logs?limit=${MAX}`, { cache: 'no-store' });
        if (!res.ok) throw new Error(String(res.status));
        const body = await res.json();
        if (cancelled) return;
        setLines(body.lines || []);
        setFile(body.file || '');
        setFailed(false);
      } catch {
        if (!cancelled) setFailed(true);
      }
    }

    pull();
    if (active) {
      timer = setInterval(pull, POLL_MS);
    }
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [active]);

  useEffect(() => {
    if (follow && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [lines, follow]);

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between px-4 py-2.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-2 text-sm font-medium text-foreground"
        >
          Log
          {file && <span className="font-mono text-xs font-normal text-muted-foreground/70">{file}</span>}
          <span className="text-xs font-normal text-muted-foreground">{open ? 'Hide' : 'Show'}</span>
        </button>
        {open && (
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
              className="accent-[hsl(var(--primary))]"
            />
            Follow
          </label>
        )}
      </div>
      {open && (
        <div
          ref={boxRef}
          onScroll={(e) => {
            // Scrolling up by hand means the user wants to read, not follow.
            const el = e.currentTarget;
            const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
            if (atBottom !== follow) setFollow(atBottom);
          }}
          className="max-h-56 overflow-y-auto border-t border-border px-4 py-2.5 font-mono text-[11px] leading-relaxed"
        >
          {failed && <p className="text-destructive">Cannot reach the backend log endpoint.</p>}
          {!failed && !lines.length && <p className="text-muted-foreground">Waiting for output…</p>}
          {lines.map((entry, i) => (
            <p
              key={`${entry.at}-${i}`}
              className={`whitespace-pre-wrap break-words ${LEVEL_STYLE[entry.level] || 'text-foreground'}`}
            >
              {entry.line}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
