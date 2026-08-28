// Import ceilings. A cell has 128 MB of heap, no disk, and holds the
// upload, the inflated collection and the sql.js linear memory at once.
// Half the isolate is the ceiling, so the renderer keeps the other half.
//
// Every number below is the largest workload measured to answer on a celld
// node running at CELLD_V8_HEAP_LIMIT_MB=64; the numbers above them refused
// with the isolate over its limit. The CSV body is the small one because that
// importer normalises the whole document's line endings before parsing it,
// which costs several copies the other two do not pay.

/** `.apkg`: 5.96 MiB of body inflating to 9.80 MiB, 5,000 notes, 2.0 s. */
export const MAX_APKG_UPLOAD_BYTES = 8 * 1024 * 1024;
/** `.prepdeck`: 1.72 MiB of body, 5,000 cards, 0.6 s. */
export const MAX_PREPDECK_UPLOAD_BYTES = 2 * 1024 * 1024;
/** `.csv`: 1.53 MiB of body, 5,000 rows, 1.3 s. 2.06 MiB does not fit. */
export const MAX_CSV_UPLOAD_BYTES = 1536 * 1024;

/** Read from the central directory, so a bomb is refused before it inflates. */
export const MAX_ZIP_ENTRY_BYTES = 32 * 1024 * 1024;

/** The same ceiling over the entries a codec actually inflates. Per-entry
 * alone bounds nothing: an archive may carry any number of entries, and any
 * number of them under one name. */
export const MAX_ZIP_TOTAL_BYTES = 32 * 1024 * 1024;

export const MAX_IMPORT_ROWS = 5000;

/**
 * `reviews.csv` rows per `.prepdeck` import. A card carries many reviews, so
 * this cannot be `MAX_IMPORT_ROWS`: the narrowest row measured is 53
 * bytes, so a 2 MiB body (prep's own writer stores, it does not deflate)
 * tops out near 39,000 rows and an honest archive never reaches this. A
 * hand-deflated one stops here instead of at the entry ceiling, which admits
 * an order of magnitude more writes than one request should make.
 */
export const MAX_IMPORT_REVIEW_ROWS = 50_000;

export const MAX_EXPORT_QUESTIONS = 5000;

export const uploadTooLarge = (bytes: number): string => `That file is too large. The limit is ${megabytes(bytes)} MB.`;

function megabytes(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  return Number.isInteger(mb) ? String(mb) : mb.toFixed(1);
}

export const ARCHIVE_TOO_LARGE = 'That archive expands past 32 MB.';
export const EXPORT_TOO_LARGE = 'This deck is too large to export in this format.';

/** Rows up to the cap are inserted, which is the partial-insert semantics an
 * import already has; the outcome names what was left. */
export const rowCapMessage = (cap: number = MAX_IMPORT_ROWS): string =>
  `stopped at ${String(cap).replace(/\B(?=(\d{3})+(?!\d))/g, ',')} rows; split the file and import again`;
