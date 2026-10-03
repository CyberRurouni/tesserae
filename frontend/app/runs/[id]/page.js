'use client';

import { RunDetail } from '@/components/runs/RunDetail';

export default function RunDetailPage({ params }) {
  return (
    <main className="min-h-screen bg-background p-6 md:p-8">
      <div className="mx-auto max-w-7xl">
        <RunDetail runId={params.id} />
      </div>
    </main>
  );
}