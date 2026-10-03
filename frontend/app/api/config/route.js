import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';

export const dynamic = 'force-dynamic';

const ROOT = path.resolve(process.cwd(), '..');
const PROFILE_DIR = path.join(ROOT, 'data', 'profile');
const SELECTION_FILE = path.join(ROOT, 'interface', '.tesserae_filters.json');

function readText(filePath) {
  try {
    return fs.existsSync(filePath) ? fs.readFileSync(filePath, 'utf-8') : '';
  } catch (error) {
    console.error(`Error reading ${filePath}:`, error);
    return '';
  }
}

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

function writeText(filePath, content) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, content, 'utf-8');
}

export async function GET() {
  try {
    return NextResponse.json({
      about_us: readText(path.join(PROFILE_DIR, 'about_us.txt')),
      additional_filters: readText(path.join(PROFILE_DIR, 'additional_filters.txt')),
      last_selection: readSelection(),
    });
  } catch (error) {
    console.error('Error fetching config:', error);
    return NextResponse.json({ error: 'Unable to read configuration' }, { status: 500 });
  }
}

export async function POST(request) {
  try {
    const body = await request.json();
    const aboutUs = body.about_us ?? body.aboutUs;
    const additionalFilters = body.additional_filters ?? body.additionalFilters;
    const lastSelection = body.last_selection ?? body.lastSelection;

    if (typeof aboutUs === 'string') writeText(path.join(PROFILE_DIR, 'about_us.txt'), aboutUs);
    if (typeof additionalFilters === 'string') writeText(path.join(PROFILE_DIR, 'additional_filters.txt'), additionalFilters);
    if (lastSelection && typeof lastSelection === 'object') {
      writeText(SELECTION_FILE, `${JSON.stringify(lastSelection, null, 2)}\n`);
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error('Error saving config:', error);
    return NextResponse.json({ error: 'Unable to save configuration' }, { status: 500 });
  }
}