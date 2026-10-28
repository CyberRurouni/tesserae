// Status is shown as a small dot plus a word, coloured by tone only.
// Partial is a success with a caveat, not a failure: the run stopped early but
// everything it found was judged and saved. Warning-toned so it is not missed,
// but it must not read as "Failed" — that is what trained us to ignore these.
export const STATUS_STYLES = {
  starting: 'text-info',
  running: 'text-info',
  awaiting_verdicts: 'text-info',
  completed: 'text-success',
  failed: 'text-destructive',
  cancelling: 'text-warning',
  cancelled: 'text-muted-foreground',
  abandoned: 'text-muted-foreground',
  partial: 'text-warning',
  pending: 'text-muted-foreground',
};

export const STATUS_LABELS = {
  starting: 'Starting',
  running: 'Searching…',
  awaiting_verdicts: 'Judging…',
  completed: 'Finished',
  failed: 'Failed',
  cancelling: 'Stopping…',
  cancelled: 'Stopped',
  abandoned: 'Lost',
  partial: 'Partial',
  pending: 'Waiting',
};

export const formatRunDate = (value) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
};

/** Keyword entries are objects from the API; accept bare strings too. */
export const keywordName = (entry) =>
  (typeof entry === 'string' ? entry : entry?.keyword) || '';

export const keywordAcceptedCount = (entry) => {
  if (!entry || typeof entry !== 'object') return null;
  const count = entry.accepted_count;
  return typeof count === 'number' ? count : 0;
};

export const keywordList = (keywords) =>
  (Array.isArray(keywords) ? keywords : []).filter((entry) => keywordName(entry));

export const formatKeywordList = (keywords) => {
  const list = keywordList(keywords).map(keywordName);
  if (!list.length) return '—';
  if (list.length <= 3) return list.join(', ');
  return `${list.slice(0, 3).join(', ')} +${list.length - 3} more`;
};

/** How many keywords in the list finished without producing an accepted ad. */
export const countEmptyKeywords = (keywords) =>
  keywordList(keywords).filter((entry) => keywordAcceptedCount(entry) === 0).length;

export const modeLabel = (mode, runUntilComplete) => {
  if (runUntilComplete) return 'AI keywords, until done';
  if (mode === 'generate') return 'AI keywords';
  if (mode === 'existing') return 'My keywords';
  return '—';
};