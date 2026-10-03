// Server Component - uses dynamic export
export const dynamic = 'force-dynamic';

import ConfigClient from './ConfigClient';

export default function ConfigPage() {
  return <ConfigClient />;
}
