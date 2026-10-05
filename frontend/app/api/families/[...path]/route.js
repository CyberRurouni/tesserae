import { forward } from '../route';

export const dynamic = 'force-dynamic';

/** Sub-paths: /api/families/<family_id>/leads, /activate, /profile/apply, ... */
async function handler(request, { params }) {
  const { path = [] } = await params;
  return forward(request, `/${path.join('/')}`);
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;