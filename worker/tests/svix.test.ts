import { describe, expect, it } from 'vitest';
import { TOLERANCE_SECONDS, signSvix, svixHeaders, verifySvix } from '../runtime/adapters/svix';
import { req } from './helpers';
import { pythonJson } from './pyoracle';

const SECRET = 'whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw';
const OTHER = 'whsec_' + Buffer.from('a-different-key-32-bytes-long!!!').toString('base64');
const ID = 'msg_p5jXN8AQM9LWM0D4loKWxJek';
const NOW = new Date('2026-03-14T15:00:00Z');
const TS = String(Math.floor(NOW.getTime() / 1000));
const BODY = JSON.stringify({ type: 'user.created', data: { id: 'user_2abc' } });

const headers = (over: Partial<Record<'id' | 'timestamp' | 'signature', string | null>> = {}) => ({
  id: 'id' in over ? over.id! : ID,
  timestamp: 'timestamp' in over ? over.timestamp! : TS,
  signature: 'signature' in over ? over.signature! : null,
});

async function signed(over: Parameters<typeof headers>[0] = {}, secret = SECRET) {
  const h = headers(over);
  return { ...h, signature: h.signature ?? (await signSvix(secret, h.id ?? '', h.timestamp ?? '', BODY)) };
}

describe('the signature', () => {
  it('is the base64 HMAC of `<id>.<timestamp>.<body>`, as svix computes it', async () => {
    const python = pythonJson<{ header: string }>(
      `import json, base64, hmac, hashlib
key = base64.b64decode("${SECRET.slice('whsec_'.length)}")
signed = ${JSON.stringify(ID)} + "." + ${JSON.stringify(TS)} + "." + ${JSON.stringify(BODY)}
mac = hmac.new(key, signed.encode(), hashlib.sha256).digest()
print(json.dumps({"header": "v1," + base64.b64encode(mac).decode()}))`,
    );
    expect(await signSvix(SECRET, ID, TS, BODY)).toBe(python.header);
  });

  it('verifies what the svix library produced for the same message', async () => {
    const python = pythonJson<{ header: string }>(
      `import json
from svix.webhooks import Webhook
wh = Webhook(${JSON.stringify(SECRET)})
print(json.dumps({"header": wh.sign(${JSON.stringify(ID)}, __import__("datetime").datetime.fromtimestamp(${TS}, __import__("datetime").timezone.utc), ${JSON.stringify(BODY)})}))`,
    );
    expect(await verifySvix(SECRET, { id: ID, timestamp: TS, signature: python.header }, BODY, NOW)).toBeNull();
  });

  it('accepts any listed v1 entry, so a rotated secret verifies during the overlap', async () => {
    const mine = await signSvix(SECRET, ID, TS, BODY);
    const theirs = await signSvix(OTHER, ID, TS, BODY);
    expect(await verifySvix(SECRET, { id: ID, timestamp: TS, signature: `${theirs} ${mine}` }, BODY, NOW)).toBeNull();
    expect(await verifySvix(SECRET, { id: ID, timestamp: TS, signature: `v2,notmine ${mine}` }, BODY, NOW)).toBeNull();
  });

  it('refuses a foreign key, a changed body and a changed id', async () => {
    const h = await signed();
    expect(await verifySvix(OTHER, h, BODY, NOW)).toBe('bad_signature');
    expect(await verifySvix(SECRET, h, BODY + ' ', NOW)).toBe('bad_signature');
    expect(await verifySvix(SECRET, { ...h, id: 'msg_other' }, BODY, NOW)).toBe('bad_signature');
    expect(await verifySvix(SECRET, { ...h, signature: 'v1,notbase64' }, BODY, NOW)).toBe('bad_signature');
    expect(await verifySvix(SECRET, { ...h, signature: 'garbage' }, BODY, NOW)).toBe('bad_signature');
  });

  it('holds the timestamp inside five minutes either way', async () => {
    const at = (drift: number) => new Date(NOW.getTime() + drift * 1000);
    const h = await signed();
    expect(await verifySvix(SECRET, h, BODY, at(TOLERANCE_SECONDS))).toBeNull();
    expect(await verifySvix(SECRET, h, BODY, at(-TOLERANCE_SECONDS))).toBeNull();
    expect(await verifySvix(SECRET, h, BODY, at(TOLERANCE_SECONDS + 1))).toBe('bad_timestamp');
    expect(await verifySvix(SECRET, h, BODY, at(-TOLERANCE_SECONDS - 1))).toBe('bad_timestamp');
    expect(await verifySvix(SECRET, await signed({ timestamp: 'not-a-number' }), BODY, NOW)).toBe('bad_timestamp');
  });

  it('names a missing header and a missing secret apart from a bad one', async () => {
    const h = await signed();
    expect(await verifySvix('', h, BODY, NOW)).toBe('no_secret');
    expect(await verifySvix(SECRET, { ...h, id: null }, BODY, NOW)).toBe('missing_headers');
    expect(await verifySvix(SECRET, { ...h, timestamp: null }, BODY, NOW)).toBe('missing_headers');
    expect(await verifySvix(SECRET, { ...h, signature: null }, BODY, NOW)).toBe('missing_headers');
    expect(await verifySvix('whsec_!!!not-base64!!!', h, BODY, NOW)).toBe('no_secret');
  });

  it('reads the three headers off the request', () => {
    const request = req('/webhooks/clerk', { method: 'POST', headers: { 'svix-id': ID, 'svix-timestamp': TS, 'svix-signature': 'v1,abc' } });
    expect(svixHeaders(request)).toEqual({ id: ID, timestamp: TS, signature: 'v1,abc' });
    expect(svixHeaders(req('/webhooks/clerk', { method: 'POST' }))).toEqual({ id: null, timestamp: null, signature: null });
  });
});
