import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

const PYTHON_BACKEND = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';

/**
 * Proxy to the Python family's endpoints.
 *
 * The browser must never talk to :8000 directly — it has no notion of the
 * user's origin and would need CORS. Every /api/families call is forwarded
 * here, preserving method and body, and the status code comes back untouched
 * so a declined save (which answers 200 with saved:false) stays distinguishable
 * from a real failure.
 */
export async function forward(request, path) {
  const target = `${PYTHON_BACKEND}/api/families${path}`;
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
    console.error(`Family proxy failed for ${target}:`, error);
    return NextResponse.json(
      { error: 'Cannot reach the Tesserae backend. Is it running?' },
      { status: 503 }
    );
  }
}

export async function GET(request) {
  return forward(request, '');
}

export async function POST(request) {
  return forward(request, '');
}