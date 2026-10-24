import { NextResponse } from 'next/server';

const PYTHON_BACKEND = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';

/**
 * Cooperative stop. The run notices on its next scroll step (~3s), unwinds
 * cleanly, and everything it had already collected is judged and saved first —
 * so this is safe to press at any moment.
 *
 * This lives in its own route because the Stop button posts to
 * /api/runs/{id}/cancel. It used to live in the parent [id] route, which is a
 * different URL: Next matched nothing for /cancel and the request died before
 * it ever reached the backend, so Stop silently did nothing.
 */
export async function POST(request, { params }) {
  const { id } = await params;
  try {
    const res = await fetch(`${PYTHON_BACKEND}/api/runs/${id}/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error('Cancel failed:', error);
    return NextResponse.json(
      { error: 'Cannot reach the Tesserae backend. Is it running?' },
      { status: 503 }
    );
  }
}
