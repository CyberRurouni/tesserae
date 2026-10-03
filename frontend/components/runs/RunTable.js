'use client';

import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card, CardContent } from '@/components/ui/Card';
import {
  STATUS_STYLES,
  STATUS_LABELS,
  countEmptyKeywords,
  formatKeywordList,
  formatRunDate,
  keywordList,
} from '@/components/runs/shared';

export function RunTable({ runs = [] }) {
  return (
    <Card>
      <CardContent className="p-0">
        {runs.length === 0 ? (
          <p className="px-6 py-12 text-center text-sm text-muted-foreground">
            No runs yet. <Link href="/config" className="font-medium text-primary hover:underline">Start your first search</Link>.
          </p>
        ) : (
          <div className="scrollbar-thin overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/40">
                <tr>
                  {['Run', 'Status', 'Keywords', 'Accepted', 'Rejected', 'Started', ''].map((heading, index) => (
                    <th
                      key={heading || index}
                      className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                    >
                      {heading}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {runs.map((run) => {
                  const status = run.status || 'pending';
                  const counts = run.counts || {};
                  const isLive = !['completed', 'failed', 'cancelled'].includes(status);
                  const keywords = keywordList(run.keywords);
                  const emptyKeywords = isLive ? 0 : countEmptyKeywords(keywords);

                  return (
                    <tr key={run.id} className="transition-colors hover:bg-accent/40">
                      <td className="px-4 py-3">
                        <Link href={`/runs/${run.id}`} className="font-mono text-xs text-primary hover:underline">
                          {run.id}
                        </Link>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-semibold ${STATUS_STYLES[status] || STATUS_STYLES.pending}`}>
                          {isLive && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" aria-hidden="true" />}
                          {STATUS_LABELS[status] || status}
                        </span>
                      </td>
                      <td className="max-w-[16rem] px-4 py-3 text-muted-foreground" title={keywords.map((entry) => entry.keyword).join(', ')}>
                        <span className="block truncate">{formatKeywordList(keywords)}</span>
                        {emptyKeywords > 0 && (
                          <span className="block truncate text-xs italic text-muted-foreground/70">
                            {emptyKeywords} returned nothing
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 font-semibold tabular-nums text-success">{counts.accepted ?? 0}</td>
                      <td className="px-4 py-3 font-semibold tabular-nums text-muted-foreground">{counts.rejected ?? 0}</td>
                      <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">{formatRunDate(run.started_at)}</td>
                      <td className="px-4 py-3">
                        <Link href={`/runs/${run.id}`}>
                          <Button variant="ghost" size="sm">
                            View
                          </Button>
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}