'use client';

import Link from 'next/link';
import { StatusLabel } from '@/components/runs/StatusLabel';
import {
  countEmptyKeywords,
  formatKeywordList,
  formatRunDate,
  keywordList,
  modeLabel,
} from '@/components/runs/shared';
import { isLiveStatus } from '@/lib/runs';

export function RunTable({ runs = [] }) {
  if (runs.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-card px-6 py-14 text-center">
        <p className="text-sm text-muted-foreground">No runs yet.</p>
        <Link href="/config" className="mt-2 inline-block text-sm font-medium text-foreground underline underline-offset-4">
          Start your first search
        </Link>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-4 py-3 font-medium">Search</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 text-right font-medium">Accepted</th>
              <th className="px-4 py-3 text-right font-medium">Rejected</th>
              <th className="px-4 py-3 font-medium">Started</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {runs.map((run) => {
              const status = run.status || 'pending';
              const counts = run.counts || {};
              const isLive = isLiveStatus(status);
              const keywords = keywordList(run.keywords);
              const emptyKeywords = isLive ? 0 : countEmptyKeywords(keywords);
              const keywordText = formatKeywordList(keywords);
              const title = keywordText !== '—' ? keywordText : modeLabel(run.mode, run.run_until_complete);

              return (
                <tr key={run.id} className="transition-colors hover:bg-secondary/40">
                  <td className="max-w-[22rem] px-4 py-3">
                    <Link
                      href={`/runs/${run.id}`}
                      className="block truncate font-medium text-foreground hover:underline"
                      title={keywords.map((entry) => entry.keyword).join(', ')}
                    >
                      {title}
                    </Link>
                    <span className="block truncate font-mono text-xs text-muted-foreground">
                      {run.id}
                      {emptyKeywords > 0 && `, ${emptyKeywords} found nothing`}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <StatusLabel status={status} />
                  </td>
                  <td className="px-4 py-3 text-right font-medium tabular-nums text-foreground">{counts.accepted ?? 0}</td>
                  <td className="px-4 py-3 text-right tabular-nums text-muted-foreground">{counts.rejected ?? 0}</td>
                  <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">{formatRunDate(run.started_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
