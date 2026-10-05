// Server Component - uses dynamic export
export const dynamic = 'force-dynamic';

import AboutClient from './AboutClient';

export default function AboutPage() {
  return <AboutClient />;
}
