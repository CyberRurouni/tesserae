import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

const PYTHON_BACKEND = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';

/** Proxy to the Python heuristics endpoints. Same single-origin rule as the rest. */
export async function forward(request, path = '') {
  const target = `${PYTHON_BACKEND}/api/heuristics${path}`;
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
    console.error(`Heuristics proxy failed for ${target}:`, error);
    return NextResponse.json(
      { error: 'Cannot reach the Tesserae backend. Is it running?' },
      { status: 503 }
    );
  }
}

export async function GET(request) {
  return forward(request, '');
}

export async function PUT(request) {
  return forward(request, '');
}
