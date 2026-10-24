import { Spinner } from '@/components/ui/Spinner';

export default function Loading() {
  return (
    <div className="mx-auto flex w-full max-w-6xl items-center justify-center px-6 py-24 md:px-10">
      <div className="flex flex-col items-center gap-3">
        <Spinner size="lg" />
        <p className="text-sm text-muted-foreground">Loading…</p>
      </div>
    </div>
  );
}
