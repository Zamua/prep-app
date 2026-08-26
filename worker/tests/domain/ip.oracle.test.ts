import { describe, expect, it } from 'vitest';
import { SENTINEL_BUCKET, limiterBucket } from '../../domain/instant/ip';
import { pythonJson } from '../pyoracle';

const ADDRESSES = [
  '203.0.113.5',
  '0.0.0.0',
  '255.255.255.255',
  '01.2.3.4',
  '1.2.3',
  '1.2.3.4.5',
  '256.1.1.1',
  '1.2.3.4 ',
  '1.2.3.4:80',
  '2001:db8::1',
  '2001:0db8:0000:0000:0000:0000:0000:0001',
  '2001:db8:0:0:1:0:0:1',
  '2001:db8:1234:5678:9abc:def0:1234:5678',
  '1:2:3:4:5:6:7:8',
  '1:0:0:1:0:0:0:1',
  '1:0:0:1:0:0:1:1',
  '0:0:0:1::',
  '::0:1:0:0:0:0',
  '1::',
  '0:1::',
  '::',
  '::1',
  '0000::',
  'FFFF::1',
  '::ffff:192.0.2.1',
  '::FFFF:1.2.3.4',
  '::ffff:c000:0201',
  '::ffff:0:0',
  '::ffff:ffff:ffff',
  '::1.2.3.4',
  '64:ff9b::192.0.2.1',
  '1:2:3:4:5:6:1.2.3.4',
  '1:2:3:4:5:6:7:8:9',
  '1:2:3:4:5:6:7',
  '1::2::3',
  ':1::',
  '1:::2',
  '12345::',
  '::g',
  '::ffff:1.2.3.04',
  'not an ip',
];

const payload = Buffer.from(JSON.stringify(ADDRESSES)).toString('base64');
const oracle = pythonJson<string[]>(`
import base64, ipaddress, json
out = []
for value in json.loads(base64.b64decode("${payload}")):
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        out.append("unresolved")
        continue
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        out.append(str(mapped) if mapped is not None else str(ipaddress.ip_network(f"{addr}/64", strict=False)))
    else:
        out.append(str(addr))
print(json.dumps(out))
`);

describe('limiterBucket matches ipaddress', () => {
  it('every address', () => {
    expect(ADDRESSES.map(limiterBucket)).toEqual(oracle);
    expect(oracle.filter((b) => b === 'unresolved').length).toBeGreaterThanOrEqual(15);
    expect(oracle.filter((b) => b.endsWith('/64')).length).toBeGreaterThanOrEqual(15);
  });

  // Accepted divergences: ipaddress parses scoped forms; bracketed forms it
  // rejects too.
  it('empty, scoped and bracketed forms share the sentinel', () => {
    expect(SENTINEL_BUCKET).toBe('unresolved');
    for (const v of ['', 'fe80::1%eth0', '[2001:db8::1]', '[::1]']) expect(limiterBucket(v)).toBe(SENTINEL_BUCKET);
  });
});
