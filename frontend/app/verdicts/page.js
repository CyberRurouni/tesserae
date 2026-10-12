// Server Component - uses dynamic export
export const dynamic = 'force-dynamic';

import VerdictsClient from './VerdictsClient';

export default function VerdictsPage() {
  return <VerdictsClient />;
}
