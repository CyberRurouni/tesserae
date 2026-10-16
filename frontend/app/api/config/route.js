import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';

export const dynamic = 'force-dynamic';

const ROOT = path.resolve(process.cwd(), '..');
const SELECTION_FILE = path.join(ROOT, 'interface', '.tesserae_filters.json');
const PYTHON_BACKEND = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';

function readSelection() {
  try {
    if (!fs.existsSync(SELECTION_FILE)) return null;
    const parsed = JSON.parse(fs.readFileSync(SELECTION_FILE, 'utf-8'));
    return parsed && Object.keys(parsed).length ? parsed : null;
  } catch (error) {
    console.error('Error reading last selection:', error);
    return null;
  }
}


/**
 * Profile text is NOT read or written here.
 *
 * This handler used to read and write data/profile/*.txt straight from Node,
 * which made it a third writer for a file that was only ever meant to mirror
 * the profile store. Profiles now live in one place and are edited through
 * /api/profiles; all this endpoint keeps is the last-used filter selection,
 * which genuinely has no backend model.
 */
async function fetchProfileTexts() {
  try {
    const res = await fetch(`${PYTHON_BACKEND}/api/config`, { cache: 'no-store' });
    if (!res.ok) return { profile_text: '', request_text: '' };
    const body = await res.json();
    return {
      profile_text: body.profile_text ?? '',
      request_text: body.request_text ?? '',
    };
  } catch (error) {
    console.error('Could not reach the profile endpoint:', error);
    return { profile_text: '', request_text: '' };
  }
}

export async function GET() {
  try {
    const profile = await fetchProfileTexts();
    return NextResponse.json({ ...profile, last_selection: readSelection() });
  } catch (error) {
    console.error('Error fetching config:', error);
    return NextResponse.json({ error: 'Unable to read configuration' }, { status: 500 });
  }
}

export async function POST(request) {
  try {
    const body = await request.json();
    const lastSelection = body.last_selection ?? body.lastSelection;

    if (lastSelection && typeof lastSelection === 'object') {
      fs.mkdirSync(path.dirname(SELECTION_FILE), { recursive: true });
      fs.writeFileSync(SELECTION_FILE, `${JSON.stringify(lastSelection, null, 2)}\n`);
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error('Error saving config:', error);
    return NextResponse.json({ error: 'Unable to save configuration' }, { status: 500 });
  }
}