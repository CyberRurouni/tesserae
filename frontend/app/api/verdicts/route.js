import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

const PYTHON_BACKEND = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';

/**
 * Proxy to the Python verdict endpoints.
 *
 * Same reasoning as the profile proxy: the browser never talks to :8000, so
 * there is no CORS to configure and no second origin to reason about. Query
 * strings are forwarded verbatim because the viewer pages on /api/verdicts.
 */
export async function forward(request, path) {
  const qs = request.nextUrl.search || '';
  const target = `${PYTHON_BACKEND}/api/verdicts${path}${qs}`;
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
    console.error(`Verdict proxy failed for ${target}:`, error);
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
