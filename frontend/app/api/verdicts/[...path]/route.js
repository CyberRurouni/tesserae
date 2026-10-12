import { forward } from '../route';

export const dynamic = 'force-dynamic';

/** Sub-paths: /api/verdicts/counts, /bulk, /restore-accepted */
async function handler(request, { params }) {
  const { path = [] } = await params;
  return forward(request, `/${path.join('/')}`);
}

export const GET = handler;
export const POST = handler;
