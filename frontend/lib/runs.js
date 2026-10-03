/**
 * Shared run helpers.
 *
 * A user-initiated run writes exactly one result file per run id. That file's
 * "keywords" array is the authoritative list of every keyword that was searched,
 * including keywords that returned zero ads — deriving keywords from
 * accepted[].source_keyword silently drops those, so it is only a fallback for
 * older files written before "keywords" was recorded.
 */

/** Statuses after which no further polling is useful. */
export const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'];

export const isTerminalStatus = (status) => TERMINAL_STATUSES.includes(status);

export const isLiveStatus = (status) => !isTerminalStatus(status);

const cleanKeyword = (value) => (typeof value === 'string' ? value.trim() : '');

/** Order-preserving, case-insensitive de-duplication (first spelling wins). */
export const dedupeKeywords = (values) => {
  const seen = new Set();
  const result = [];
  for (const value of Array.isArray(values) ? values : []) {
    const keyword = cleanKeyword(value);
    if (!keyword) continue;
    const key = keyword.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(keyword);
  }
  return result;
};

/**
 * The keyword list for one run.
 * Live state wins, then the saved file's "keywords", then (older files only) a
 * list derived from the accepted ads.
 */
export const resolveKeywords = ({ liveKeywords, fileKeywords, accepted } = {}) => {
  const live = dedupeKeywords(liveKeywords);
  if (live.length) return live;
  const saved = dedupeKeywords(fileKeywords);
  if (saved.length) return saved;
  return dedupeKeywords((Array.isArray(accepted) ? accepted : []).map((ad) => ad?.source_keyword));
};

const countMatching = (ads, keyword) => {
  const target = keyword.toLowerCase();
  return (Array.isArray(ads) ? ads : []).reduce((total, ad) => {
    const source = cleanKeyword(ad?.source_keyword).toLowerCase();
    return source === target ? total + 1 : total;
  }, 0);
};

const keywordStatus = ({ runStatus, currentKeyword, keyword }) => {
  if (runStatus === 'completed') return 'completed';
  if (isTerminalStatus(runStatus)) return runStatus;
  if (currentKeyword && cleanKeyword(currentKeyword).toLowerCase() === keyword.toLowerCase()) return 'running';
  return isLiveStatus(runStatus) ? 'pending' : 'completed';
};

/**
 * Turn a keyword list into rows carrying per-keyword counts so the UI can mark
 * keywords that found nothing.
 */
export const buildKeywordRows = ({
  keywords,
  accepted = [],
  judgedRejected = [],
  aiFailed = [],
  status = 'pending',
  currentKeyword = null,
} = {}) => {
  return dedupeKeywords(keywords).map((keyword, index) => {
    const acceptedCount = countMatching(accepted, keyword);
    const rejectedCount = countMatching(judgedRejected, keyword);
    const aiFailedCount = countMatching(aiFailed, keyword);
    return {
      id: `${index}:${keyword.toLowerCase()}`,
      keyword,
      status: keywordStatus({ runStatus: status, currentKeyword, keyword }),
      ads_found: acceptedCount + rejectedCount + aiFailedCount,
      accepted_count: acceptedCount,
      rejected_count: rejectedCount,
      ai_failed_count: aiFailedCount,
    };
  });
};

/** Convenience: resolve + shape in one call. */
export const buildRunKeywords = ({ liveKeywords, fileKeywords, accepted, judgedRejected, aiFailed, status, currentKeyword } = {}) =>
  buildKeywordRows({
    keywords: resolveKeywords({ liveKeywords, fileKeywords, accepted }),
    accepted,
    judgedRejected,
    aiFailed,
    status,
    currentKeyword,
  });