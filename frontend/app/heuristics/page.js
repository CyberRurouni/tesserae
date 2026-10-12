// Server Component - uses dynamic export
export const dynamic = 'force-dynamic';

import HeuristicsClient from './HeuristicsClient';

export default function HeuristicsPage() {
  return <HeuristicsClient />;
}
