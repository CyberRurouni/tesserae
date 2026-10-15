import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

const PYTHON_BACKEND = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';

/**
 * Proxy to the Python keyword-pool endpoints.
 *
 * Same single-origin rule as the rest: the browser never talks to :8000.
 */
async function forward(request, path = '') {
  const qs = request.nextUrl.search || '';
  const target = `${PYTHON_BACKEND}/api/keywords${path}${qs}`;
  let body;
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    body = await request.text();
  }

  try {
    const res = await fetch(target, {
      method: request.method,
      headers: { 'Content-Type': 'application/json' },
      body,
      cache: 'no-store',
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error(`Keyword proxy failed for ${target}:`, error);
    return NextResponse.json(
      { error: 'Cannot reach the Tesserae backend. Is it running?' },
      { status: 503 }
    );
  }
}

export async function GET(request) {
  return forward(request, '');
}

// Previewing a selection is a POST (it carries a body). The Python router
// mounts it at /preview — the collection itself is read-only.
export async function POST(request) {
  return forward(request, '/preview');
}