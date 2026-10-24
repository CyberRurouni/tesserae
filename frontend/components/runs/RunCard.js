'use client';

import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { StatusLabel } from '@/components/runs/StatusLabel';
import {
  countEmptyKeywords,
  formatKeywordList,
  formatRunDate,
  keywordList,
  modeLabel,
} from '@/components/runs/shared';
import { isLiveStatus } from '@/lib/runs';

export function RunCard({ run }) {
  const status = run.status || 'pending';
  const counts = run.counts || {};
  const isLive = isLiveStatus(status);
  const keywords = keywordList(run.keywords);
  const emptyKeywords = isLive ? 0 : countEmptyKeywords(keywords);

  const keywordText = formatKeywordList(keywords);
  const title = keywordText !== '—' ? keywordText : modeLabel(run.mode, run.run_until_complete);
  const progress = Math.max(status === 'completed' ? 100 : 3, run.progress || 0);

  return (
    <Link
      href={`/runs/${run.id}`}
      className="group block rounded-lg focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
    >
      <Card className="flex h-full flex-col gap-5 p-5 transition-colors group-hover:border-foreground/25">
        <div className="flex items-start justify-between gap-3">
          <p className="min-w-0 truncate text-sm font-medium text-foreground" title={keywords.map((k) => k.keyword).join(', ')}>
            {title}
          </p>
          <StatusLabel status={status} />
        </div>

        <div className="flex items-end gap-8">
          <div>
            <p className="text-3xl font-medium tabular-nums text-foreground">{counts.accepted ?? 0}</p>
            <p className="text-xs text-muted-foreground">accepted</p>
          </div>
          <div>
            <p className="text-xl font-medium tabular-nums text-muted-foreground">{counts.rejected ?? 0}</p>
            <p className="text-xs text-muted-foreground">rejected</p>
          </div>
        </div>

        {isLive && (
          <div className="h-1 w-full overflow-hidden rounded-full bg-secondary">
            <div className="h-full rounded-full bg-primary transition-all duration-500" style={{ width: `${progress}%` }} />
          </div>
        )}

        <p className="mt-auto text-xs text-muted-foreground">
          {formatRunDate(run.started_at)}
          {emptyKeywords > 0 && `, ${emptyKeywords} keyword${emptyKeywords === 1 ? '' : 's'} found nothing`}
        </p>
      </Card>
    </Link>
  );
}
