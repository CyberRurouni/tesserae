export const STATUS_STYLES = {
  starting: 'bg-info/15 text-info border-info/40',
  running: 'bg-info/15 text-info border-info/40',
  completed: 'bg-success/15 text-success border-success/40',
  failed: 'bg-destructive/15 text-destructive border-destructive/40',
  cancelling: 'bg-warning/15 text-warning border-warning/40',
  cancelled: 'bg-muted text-muted-foreground border-border',
  abandoned: 'bg-muted text-muted-foreground border-border',
  // Partial is a success with a caveat, not a failure: the run stopped early but
  // everything it found was judged and saved. Warning-toned so it is not missed,
  // but it must not read as "Failed" — that is what trained us to ignore these.
  partial: 'bg-warning/15 text-warning border-warning/40',
  pending: 'bg-muted text-muted-foreground border-border',
};

export const STATUS_LABELS = {
  starting: 'Starting',
  running: 'Searching…',
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
  if (runUntilComplete) return 'AI · until done';
  if (mode === 'generate') return 'AI keywords';
  if (mode === 'existing') return 'My keywords';
  return '—';
};