/**
 * draft.js — keep the Search page's in-progress state across navigation.
 *
 * The form lives in local component state, so every trip to another page
 * remounted FilterBuilder and threw the draft away. Tuning a filter means
 * comparing pages — "does this heuristic change the result set?", "what did I
 * last set the cycles to?" — so the wipe happened exactly when the draft was
 * most useful.
 *
 * A draft is deliberately NOT the same thing as the last run's settings:
 *
 *   draft      — unsaved edits, per browser, discarded on purpose
 *   saved      — what the last run actually used, server-side
 *
 * Mixing them is how you end up restarting a search on settings you never
 * meant to keep. Clearing the draft is one button and is stated on the page.
 */

const KEY = 'tesserae.search.draft.v1';

/** Fields worth keeping. Anything else is either derived or per-run noise. */
const KEEP = [
  'category',
  'platforms',
  'media_type',
  'date_preset',
  'ad_age_hours',
  'delivered_to_regions',
  'regions',
  'languages',
  'sort_order',
  'heuristic_patterns',
  'run_mode',
  'cycles',
  'run_until_complete',
  'ad_age',
  'parallel_workers',
];

export function readDraft() {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed;
  } catch {
    // A corrupt draft must never block the page — fall back to the server's.
    return null;
  }
}

export function writeDraft(formData) {
  if (typeof window === 'undefined') return;
  try {
    const trimmed = {};
    for (const field of KEEP) {
      if (formData[field] !== undefined) trimmed[field] = formData[field];
    }
    // Keywords are kept: they are the part you are most likely to lose work on
    // and the least likely to want retyping.
    if (Array.isArray(formData.keywords)) trimmed.keywords = formData.keywords;
    window.localStorage.setItem(KEY, JSON.stringify(trimmed));
  } catch {
    // Quota or private-mode failure. The form still works, it just will not
    // survive navigation — not worth interrupting the user over.
  }
}

export function clearDraft() {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* see above */
  }
}
