import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';
import { buildRunKeywords, isTerminalStatus } from '@/lib/runs';

export const dynamic = 'force-dynamic';

const PYTHON_BACKEND = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';
const DATA_DIR = path.resolve(process.cwd(), '..', 'data', 'ads');

function listSavedRuns() {
  if (!fs.existsSync(DATA_DIR)) return [];

  const runs = [];
  for (const category of fs.readdirSync(DATA_DIR, { withFileTypes: true }).filter((d) => d.isDirectory())) {
    const categoryDir = path.join(DATA_DIR, category.name);
    const files = fs
      .readdirSync(categoryDir)
      .filter((f) => f.startsWith('run_') && f.endsWith('.json'))
      .sort()
      .reverse();

    for (const file of files) {
      try {
        const data = JSON.parse(fs.readFileSync(path.join(categoryDir, file), 'utf-8'));
        const accepted = Array.isArray(data.accepted) ? data.accepted : [];
        const rejected = Array.isArray(data.judged_rejected) ? data.judged_rejected : [];
        const aiFailed = Array.isArray(data.ai_failed) ? data.ai_failed : [];

        runs.push({
          id: file.replace(/\.json$/, ''),
          category: category.name,
          status: 'completed',
          progress: 100,
          mode: null,
          cycles: null,
          run_until_complete: null,
          // Prefer the file's own "keywords" list — it also contains keywords
          // that found nothing. Older files fall back to the accepted ads.
          keywords: buildRunKeywords({
            fileKeywords: data.keywords,
            accepted,
            judgedRejected: rejected,
            aiFailed,
            status: 'completed',
          }),
          started_at: data.recorded_at || null,
          counts: { accepted: accepted.length, rejected: rejected.length, ai_failed: aiFailed.length },
        });
      } catch (error) {
        console.error(`Error parsing ${file}:`, error);
      }
    }
  }
  return runs;
}

async function listLiveRuns() {
  try {
    const res = await fetch(`${PYTHON_BACKEND}/api/runs`, { cache: 'no-store' });
    if (!res.ok) return [];
    const { runs = [] } = await res.json();
    return runs
      .filter((run) => !isTerminalStatus(run.status))
      .map((run) => ({
        id: run.id,
        category: run.category || 'career_jobs',
        status: run.status,
        progress: run.progress ?? 0,
        mode: run.config?.run_mode ?? null,
        cycles: run.cycles ?? null,
        run_until_complete: run.config?.run_until_complete ?? null,
        keywords: buildRunKeywords({
          liveKeywords: run.keywords,
          accepted: [],
          status: run.status,
          currentKeyword: run.current_keyword ?? null,
        }),
        started_at: run.started_at || null,
        counts: run.counts || { accepted: 0, rejected: 0, ai_failed: 0 },
      }));
  } catch {
    console.log('Python backend unavailable — listing saved runs only');
    return [];
  }
}

export async function POST(request) {
  try {
    const body = await request.json();

    const response = await fetch(`${PYTHON_BACKEND}/api/runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      const message =
        (typeof data.detail === 'string' && data.detail) ||
        (Array.isArray(data.detail) && data.detail.map((d) => d.msg).join(' · ')) ||
        data.error ||
        'The search could not be started.';
      return NextResponse.json({ error: message }, { status: response.status });
    }

    const runId = data.run_id || data.id;
    return NextResponse.json({ ...data, id: runId, run_id: runId });
  } catch (error) {
    console.error('Error starting run:', error);
    return NextResponse.json({ error: 'Cannot reach the Tesserae backend. Is it running?' }, { status: 503 });
  }
}

export async function GET(request) {
  try {
    const { searchParams } = new URL(request.url);
    const limit = Number(searchParams.get('limit')) || 50;

    const [saved, live] = await Promise.all([Promise.resolve(listSavedRuns()), listLiveRuns()]);

    // The same run id can arrive twice: once as in-flight backend state and
    // once as its saved result file. Collapse them into a single entry, letting
    // the saved file supply the final counts and keyword list while the live
    // entry backfills config details the file does not carry.
    const byId = new Map();
    for (const run of live) byId.set(run.id, run);
    for (const run of saved) {
      const inFlight = byId.get(run.id);
      byId.set(run.id, {
        ...run,
        mode: run.mode ?? inFlight?.mode ?? null,
        cycles: run.cycles ?? inFlight?.cycles ?? null,
        run_until_complete: run.run_until_complete ?? inFlight?.run_until_complete ?? null,
      });
    }

    const runs = [...byId.values()]
      .sort((a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0))
      .slice(0, limit);

    return NextResponse.json({ runs });
  } catch (error) {
    console.error('Error listing runs:', error);
    return NextResponse.json({ error: 'Unable to list runs' }, { status: 500 });
  }
}