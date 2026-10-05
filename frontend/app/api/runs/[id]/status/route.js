import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';
import { isTerminalStatus, resolveKeywords } from '@/lib/runs';

export const dynamic = 'force-dynamic';

const PYTHON_BACKEND = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';
const DATA_DIR = path.resolve(process.cwd(), '..', 'data', 'ads');

function findRunFile(runId) {
  if (!fs.existsSync(DATA_DIR)) return null;
  for (const category of fs.readdirSync(DATA_DIR, { withFileTypes: true }).filter((d) => d.isDirectory())) {
    const filePath = path.join(DATA_DIR, category.name, `${runId}.json`);
    if (fs.existsSync(filePath)) return { filePath, category: category.name };
  }
  return null;
}

function statusFromFile(id, found) {
  const data = JSON.parse(fs.readFileSync(found.filePath, 'utf-8'));
  const accepted = Array.isArray(data.accepted) ? data.accepted : [];
  const judgedRejected = Array.isArray(data.judged_rejected) ? data.judged_rejected : [];
  const aiFailed = Array.isArray(data.ai_failed) ? data.ai_failed : [];
  const deferred = Array.isArray(data.deferred) ? data.deferred : [];

  return {
    id,
    category: found.category,
    status: 'completed',
    progress: 100,
    current_keyword: null,
    deferred,
    counts: {
      accepted: accepted.length,
      deferred: deferred.length,
      rejected: judgedRejected.length,
      ai_failed: aiFailed.length,
    },
    totalProcessed: accepted.length + deferred.length + judgedRejected.length + aiFailed.length,
    accepted: accepted.length,
    rejected: judgedRejected.length,
    aiFailed: aiFailed.length,
    keywords: resolveKeywords({ fileKeywords: data.keywords, accepted }),
    results_saved: true,
    error: null,
    recordedAt: data.recorded_at || null,
  };
}

const numericCount = (value) => (typeof value === 'number' ? value : null);

// The lightweight /status payload carries no keyword list, so for a run that is
// still executing we ask the full run endpoint once for it.
async function liveKeywordsFromBackend(id) {
  try {
    const res = await fetch(`${PYTHON_BACKEND}/api/runs/${id}`, { cache: 'no-store' });
    if (!res.ok) return [];
    const data = await res.json();
    return resolveKeywords({ liveKeywords: data.keywords });
  } catch {
    return [];
  }
}

export async function GET(request, { params }) {
  try {
    const { id } = await params;

    let saved;
    const loadSaved = () => {
      if (saved === undefined) {
        const found = findRunFile(id);
        saved = false;
        if (found) {
          try {
            saved = { found, status: statusFromFile(id, found) };
          } catch (error) {
            console.error(`Error parsing run file for ${found.filePath}:`, error);
          }
        }
      }
      return saved || null;
    };

    // The backend knows about a run while it is executing, before any file
    // exists, so ask it first and only fall back to disk when it 404s.
    try {
      const response = await fetch(`${PYTHON_BACKEND}/api/runs/${id}/status`, {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' },
        cache: 'no-store',
      });

      if (response.ok) {
        const data = await response.json();
        const status = data.status || 'pending';
        const reported = Array.isArray(data.keywords) ? resolveKeywords({ liveKeywords: data.keywords }) : [];
        // The backend serves finished runs from disk without echoing the
        // keyword list or the final counts, so top both up from the file.
        const fromFile = status === 'completed' ? loadSaved() : null;
        const backendCounts = data.counts && typeof data.counts === 'object' ? data.counts : {};
        const keywords = reported.length
          ? reported
          : fromFile?.status.keywords ?? (isTerminalStatus(status) ? [] : await liveKeywordsFromBackend(id));

        return NextResponse.json({
          current_keyword: data.current_keyword ?? null,
          error: data.error ?? null,
          ...data,
          id,
          status,
          counts: {
            accepted: numericCount(backendCounts.accepted) ?? fromFile?.status.counts.accepted ?? 0,
            deferred: numericCount(backendCounts.deferred) ?? fromFile?.status.counts.deferred ?? 0,
            rejected: numericCount(backendCounts.rejected) ?? fromFile?.status.counts.rejected ?? 0,
            ai_failed: numericCount(backendCounts.ai_failed) ?? fromFile?.status.counts.ai_failed ?? 0,
          },
          keywords,
          results_saved: Boolean(fromFile),
          terminal: isTerminalStatus(status),
          // Always present, whichever branch answered. The live branch used to
          // omit it, so a caller could not rely on the shape.
          totalProcessed:
            (numericCount(backendCounts.accepted) ?? fromFile?.status.counts.accepted ?? 0) +
            (numericCount(backendCounts.deferred) ?? fromFile?.status.counts.deferred ?? 0) +
            (numericCount(backendCounts.rejected) ?? fromFile?.status.counts.rejected ?? 0) +
            (numericCount(backendCounts.ai_failed) ?? fromFile?.status.counts.ai_failed ?? 0),
        });
      }

      if (response.status >= 500) {
        return NextResponse.json({ error: 'The backend could not report this run' }, { status: 502 });
      }
    } catch {
      console.log('Python backend not available, falling back to file-based status');
    }

    const found = findRunFile(id);
    if (!found) {
      return NextResponse.json({ error: 'Run not found' }, { status: 404 });
    }

    try {
      const status = statusFromFile(id, found);
      return NextResponse.json({ ...status, terminal: true });
    } catch (error) {
      console.error(`Error parsing run file for ${found.filePath}:`, error);
      return NextResponse.json({ error: 'Run file could not be read' }, { status: 500 });
    }
  } catch (error) {
    console.error('Error fetching run status:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}