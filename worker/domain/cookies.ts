// `Set-Cookie` bytes as Starlette emits them, because the contract corpus
// records the header verbatim. `http.cookies.Morsel` prints its attributes
// sorted by their lowercase key, quotes a value that is not entirely legal
// characters (an empty value always is quoted), and renders an integer
// `expires` as an HTTP date computed from the current time.

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = (n: number, width = 2) => String(n).padStart(width, '0');

/** `Tue, 25 Aug 2026 20:38:46 GMT`, as `http.cookies._getdate` builds it. */
export function httpDate(at: Date): string {
  const weekday = WEEKDAYS[(at.getUTCDay() + 6) % 7]!;
  const month = MONTHS[at.getUTCMonth()]!;
  return `${weekday}, ${pad(at.getUTCDate())} ${month} ${pad(at.getUTCFullYear(), 4)} ${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}:${pad(at.getUTCSeconds())} GMT`;
}

const LEGAL = /^[A-Za-z0-9!#$%&'*+\-.^_`|~:]+$/;
// `Morsel._Translator`: the characters http.cookies escapes inside a quoted
// value. Everything else that is not printable ASCII becomes \OOO octal.
const ESCAPED: Record<string, string> = { '\\': '\\\\', '"': '\\"' };

export function quoteCookieValue(value: string): string {
  if (LEGAL.test(value)) return value;
  let out = '"';
  for (const ch of value) {
    const code = ch.codePointAt(0)!;
    if (ESCAPED[ch]) out += ESCAPED[ch];
    else if (code < 0x20 || code > 0x7e) out += `\\${code.toString(8).padStart(3, '0')}`;
    else out += ch;
  }
  return `${out}"`;
}

export interface CookieAttributes {
  expires?: Date;
  maxAge?: number;
  path?: string;
  sameSite?: string;
  secure?: boolean;
  httpOnly?: boolean;
}

/** The header value. Attributes are emitted in a fixed order so the same
 * cookie always serialises to the same bytes. */
export function setCookie(name: string, value: string, attrs: CookieAttributes): string {
  const parts: [key: string, rendered: string][] = [];
  if (attrs.expires !== undefined) parts.push(['expires', `expires=${httpDate(attrs.expires)}`]);
  if (attrs.httpOnly) parts.push(['httponly', 'HttpOnly']);
  if (attrs.maxAge !== undefined) parts.push(['max-age', `Max-Age=${attrs.maxAge}`]);
  if (attrs.path !== undefined) parts.push(['path', `Path=${attrs.path}`]);
  if (attrs.sameSite !== undefined) parts.push(['samesite', `SameSite=${attrs.sameSite}`]);
  if (attrs.secure) parts.push(['secure', 'Secure']);
  parts.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return [`${name}=${quoteCookieValue(value)}`, ...parts.map((p) => p[1])].join('; ');
}

/** The cookies of a request header, last value winning as Starlette reads them. */
export function parseCookieHeader(header: string | null): Record<string, string> {
  const out: Record<string, string> = {};
  for (const piece of (header ?? '').split(';')) {
    const eq = piece.indexOf('=');
    if (eq < 0) continue;
    const name = piece.slice(0, eq).trim();
    if (!name) continue;
    out[name] = unquoteCookieValue(piece.slice(eq + 1).trim());
  }
  return out;
}

/** The inverse of `quoteCookieValue` for the shapes http.cookies emits. */
export function unquoteCookieValue(value: string): string {
  if (value.length < 2 || !value.startsWith('"') || !value.endsWith('"')) return value;
  const body = value.slice(1, -1);
  let out = '';
  for (let i = 0; i < body.length; i++) {
    if (body[i] !== '\\') {
      out += body[i];
      continue;
    }
    const next = body.slice(i + 1, i + 4);
    if (/^[0-7]{3}$/.test(next)) {
      out += String.fromCharCode(parseInt(next, 8));
      i += 3;
    } else {
      out += body[i + 1] ?? '';
      i += 1;
    }
  }
  return out;
}
