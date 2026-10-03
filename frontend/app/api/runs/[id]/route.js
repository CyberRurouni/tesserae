import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';
import { buildRunKeywords, isTerminalStatus } from '@/lib/runs';

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

function readRunFile(found) {
  try {
    const parsed = JSON.parse(fs.readFileSync(found.filePath, 'utf-8'));
    return {
      accepted: Array.isArray(parsed.accepted) ? parsed.accepted : [],
      judgedRejected: Array.isArray(parsed.judged_rejected) ? parsed.judged_rejected : [],
      aiFailed: Array.isArray(parsed.ai_failed) ? parsed.ai_failed : [],
      fileKeywords: Array.isArray(parsed.keywords) ? parsed.keywords : [],
      recordedAt: parsed.recorded_at || null,
    };
  } catch (error) {
    console.error(`Error reading run file for ${found.filePath}:`, error);
    return {
      accepted: [],
      judgedRejected: [],
      aiFailed: [],
      fileKeywords: [],
      recordedAt: null,
    };
  }
}

export async function GET(request, { params }) {
  const { id } = await params;

  try {
    // Live run state from the Python API. This is the only source for a run
    // that is still executing — it has no result file yet — so it must not be
    // discarded just because the file is missing.
    let live = null;
    try {
      const res = await fetch(`${PYTHON_BACKEND}/api/runs/${id}`, { cache: 'no-store' });
      if (res.ok) live = await res.json();
    } catch {
      console.log('Python backend unavailable, using saved run file');
    }

    const found = findRunFile(id);

    // Unknown to both the backend (never started / lost on restart) and the
    // disk: this really is a 404.
    if (!live && !found) {
      return NextResponse.json({ error: 'Run not found' }, { status: 404 });
    }

    const saved = found
      ? readRunFile(found)
      : { accepted: [], judgedRejected: [], aiFailed: [], fileKeywords: [], recordedAt: null };

    const { accepted, judgedRejected, aiFailed } = saved;

    // A saved result file means the run is over, whatever the backend says.
    const status = live?.status || (found ? 'completed' : 'pending');
    const progress = live?.progress ?? (found ? 100 : 0);

    const keywordRows = buildRunKeywords({
      liveKeywords: live?.keywords,
      fileKeywords: saved.fileKeywords,
      accepted,
      judgedRejected,
      aiFailed,
      status,
      currentKeyword: live?.current_keyword ?? null,
    });

    return NextResponse.json({
      id,
      category: found?.category || live?.category || 'career_jobs',
      status,
      progress,
      error: live?.error || null,
      terminal: isTerminalStatus(status),
      // Lets the client tell "finished, nothing matched" apart from
      // "finished but the result file never landed".
      results_saved: Boolean(found),
      recorded_at: saved.recordedAt || live?.started_at || null,
      started_at: live?.started_at || saved.recordedAt || null,
      completed_at: live?.completed_at || null,
      config: live?.config || null,
      current_keyword: live?.current_keyword ?? null,
      keywords: keywordRows,
      keyword_list: keywordRows.map((row) => row.keyword),
      accepted,
      judged_rejected: judgedRejected,
      ai_failed: aiFailed,
      counts: {
        accepted: accepted.length,
        rejected: judgedRejected.length,
        ai_failed: aiFailed.length,
      },
    });
  } catch (error) {
    console.error('Error fetching run detail:', error);
    return NextResponse.json({ error: 'Unable to read this run' }, { status: 500 });
  }
}