'use client';

import { RunDetail } from '@/components/runs/RunDetail';

export default function RunDetailPage({ params }) {
  return (
    <div className="mx-auto w-full max-w-6xl px-6 py-8 md:px-10">
      <RunDetail runId={params.id} />
    </div>
  );
}
