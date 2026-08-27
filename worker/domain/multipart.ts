// `multipart/form-data` over the raw bytes.
//
// Not `Response.formData()`: the cell runtime decodes a file part as UTF-8,
// which turns every byte above 0x7f into U+FFFD and doubles the length of a
// zip. A file upload has to arrive byte for byte, so the body is split here
// and only the field parts are decoded as text.

const CR = 0x0d;
const LF = 0x0a;
const DASH = 0x2d;

export interface MultipartPart {
  name: string;
  /** Present on a file part, absent on a plain field. */
  filename: string | null;
  bytes: Uint8Array;
}

/** The `boundary` parameter, quoted or bare, or null when there is none. */
export function boundaryOf(contentType: string): string | null {
  const m = /;\s*boundary=(?:"([^"]*)"|([^;\s]+))/i.exec(contentType);
  if (!m) return null;
  return m[1] ?? m[2] ?? null;
}

function indexOf(haystack: Uint8Array, needle: Uint8Array, from: number): number {
  const last = haystack.length - needle.length;
  outer: for (let i = from; i <= last; i++) {
    for (let j = 0; j < needle.length; j++) if (haystack[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

const decoder = new TextDecoder('utf-8');

/** `name` and `filename` off the part's Content-Disposition. */
function disposition(headers: string): { name: string; filename: string | null } | null {
  for (const line of headers.split('\r\n')) {
    if (!/^content-disposition\s*:/i.test(line)) continue;
    const name = /;\s*name="([^"]*)"/i.exec(line) ?? /;\s*name=([^;]+)/i.exec(line);
    if (!name) return null;
    const filename = /;\s*filename="([^"]*)"/i.exec(line) ?? /;\s*filename=([^;]+)/i.exec(line);
    return { name: (name[1] ?? '').trim(), filename: filename ? (filename[1] ?? '').trim() : null };
  }
  return null;
}

const startsAt = (haystack: Uint8Array, needle: Uint8Array, from: number): boolean => {
  if (from + needle.length > haystack.length) return false;
  for (let j = 0; j < needle.length; j++) if (haystack[from + j] !== needle[j]) return false;
  return true;
};

/** The parts of a body, in order. A malformed body yields what it parsed. */
export function parseMultipart(body: Uint8Array, contentType: string): MultipartPart[] {
  const boundary = boundaryOf(contentType);
  if (!boundary) return [];
  const enc = new TextEncoder();
  // RFC 2046: the delimiter is CRLF followed by `--boundary`, and the CRLF is
  // the delimiter's, not the part's. Scanning for `--boundary` alone ends a
  // part wherever the uploaded bytes happen to hold that sequence, which
  // silently truncates a file the client chose a short boundary for.
  const opening = enc.encode(`--${boundary}`);
  const delimiter = enc.encode(`\r\n--${boundary}`);
  const parts: MultipartPart[] = [];

  let at: number;
  if (startsAt(body, opening, 0)) {
    at = opening.length;
  } else {
    // A preamble ahead of the first part; its own CRLF opens the delimiter.
    const first = indexOf(body, delimiter, 0);
    if (first < 0) return parts;
    at = first + delimiter.length;
  }

  while (at < body.length) {
    // `--` closes the body; anything else is transport padding up to the CRLF.
    if (body[at] === DASH && body[at + 1] === DASH) break;
    while (at < body.length && body[at] !== LF) at++;
    at++;

    let headerEnd = at;
    while (headerEnd + 3 < body.length && !(body[headerEnd] === CR && body[headerEnd + 1] === LF && body[headerEnd + 2] === CR && body[headerEnd + 3] === LF)) {
      headerEnd++;
    }
    if (headerEnd + 3 >= body.length) break;

    const headers = decoder.decode(body.subarray(at, headerEnd));
    const start = headerEnd + 4;
    const next = indexOf(body, delimiter, start);
    if (next < 0) break;

    const where = disposition(headers);
    if (where) parts.push({ name: where.name, filename: where.filename, bytes: body.slice(start, next) });
    at = next + delimiter.length;
  }
  return parts;
}
