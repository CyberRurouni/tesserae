'use client';

import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import {
  STATUS_STYLES,
  STATUS_LABELS,
  countEmptyKeywords,
  formatKeywordList,
  formatRunDate,
  keywordList,
  modeLabel,
} from '@/components/runs/shared';
import { cn } from '@/lib/utils';

export function RunCard({ run }) {
  const status = run.status || 'pending';
  const counts = run.counts || {};
  const isLive = !['completed', 'failed', 'cancelled'].includes(status);
  const keywords = keywordList(run.keywords);
  const emptyKeywords = isLive ? 0 : countEmptyKeywords(keywords);

  return (
    <Card className="flex h-full flex-col overflow-hidden">
      <CardHeader className="gap-3">
        <div className="flex items-start justify-between gap-3">
          <CardTitle className="truncate font-mono text-base">{run.id}</CardTitle>
          <span className={cn('inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-1 text-[11px] font-semibold', STATUS_STYLES[status] || STATUS_STYLES.pending)}>
            {isLive && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" aria-hidden="true" />}
            {STATUS_LABELS[status] || status}
          </span>
        </div>
        <p className="text-xs text-muted-foreground">
          {formatRunDate(run.started_at)} · {modeLabel(run.mode, run.run_until_complete)}
        </p>
      </CardHeader>

      <CardContent className="flex flex-1 flex-col gap-4">
        <div className="grid grid-cols-3 gap-3 text-center">
          {[
            { label: 'Accepted', value: counts.accepted ?? 0, tone: 'text-success' },
            { label: 'Rejected', value: counts.rejected ?? 0, tone: 'text-muted-foreground' },
            { label: 'Keywords', value: keywords.length, tone: 'text-primary' },
          ].map((stat) => (
            <div key={stat.label} className="rounded-xl border border-border/60 bg-background/40 px-2 py-3">
              <p className={cn('text-2xl font-bold tabular-nums', stat.tone)}>{stat.value}</p>
              <p className="mt-0.5 text-[11px] uppercase tracking-wide text-muted-foreground">{stat.label}</p>
            </div>
          ))}
        </div>

        <div>
          <div className="mb-1.5 flex items-center justify-between text-xs text-muted-foreground">
            <span>Progress</span>
            <span className="font-semibold tabular-nums text-foreground">{run.progress || 0}%</span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-gradient-to-r from-primary to-glow transition-all duration-500"
              style={{ width: `${Math.max(status === 'completed' ? 100 : 3, run.progress || 0)}%` }}
            />
          </div>
        </div>

        <div>
          <p className="truncate text-xs text-muted-foreground" title={keywords.map((entry) => entry.keyword).join(', ')}>
            {formatKeywordList(keywords)}
          </p>
          {emptyKeywords > 0 && (
            <p className="mt-0.5 truncate text-xs italic text-muted-foreground/70">
              {emptyKeywords} returned nothing
            </p>
          )}
        </div>

        <Link href={`/runs/${run.id}`} className="mt-auto">
          <Button variant="outline" size="sm" className="w-full">
            {isLive ? 'Watch progress' : 'View results'}
          </Button>
        </Link>
      </CardContent>
    </Card>
  );
}