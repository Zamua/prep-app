// The rate-limit key for anonymous generation, so both directions bite: a
// bucket that collapses distinct clients is a denial of service, and one that
// splits a single client across buckets is an abuse bypass. The value is a
// header, which is to say it is whatever the caller decided to send.
import { describe, expect, it } from 'vitest';
import { SENTINEL_BUCKET, limiterBucket } from '../../domain/instant/ip';

describe('limiterBucket', () => {
  it('keys an IPv4 address on itself', () => {
    for (const v4 of ['1.2.3.4', '0.0.0.0', '255.255.255.255', '10.0.0.1', '64.233.160.0']) {
      expect(limiterBucket(v4), v4).toBe(v4);
    }
  });

  it('keys an IPv6 address on its /64, whatever spelling it arrives in', () => {
    const table: [string, string][] = [
      ['2001:db8::1', '2001:db8::/64'],
      ['2001:db8:0:0:1:2:3:4', '2001:db8::/64'],
      ['2001:0db8:0000:0000:0000:0000:0000:0001', '2001:db8::/64'],
      ['2001:db8:abcd:1234:5678::9', '2001:db8:abcd:1234::/64'],
      ['a:b:c:d:e:f:1:2', 'a:b:c:d::/64'],
      ['fe80::', 'fe80::/64'],
      ['1::', '1::/64'],
      ['::', '::/64'],
      ['::1', '::/64'],
      ['::2001:db8', '::/64'],
      // NAT64: still an IPv6 client, still keyed on its prefix.
      ['64:ff9b::1.2.3.4', '64:ff9b::/64'],
    ];
    for (const [given, want] of table) expect(limiterBucket(given), given).toBe(want);
  });

  it('collapses a v4-mapped address onto the IPv4 bucket, so one client is one bucket', () => {
    expect(limiterBucket('::ffff:1.2.3.4')).toBe('1.2.3.4');
    expect(limiterBucket('::ffff:102:304')).toBe('1.2.3.4');
    expect(limiterBucket('::ffff:255.255.255.255')).toBe('255.255.255.255');
    expect(limiterBucket('::ffff:1.2.3.4')).toBe(limiterBucket('1.2.3.4'));
    // v4-translated is a different prefix and is not collapsed.
    expect(limiterBucket('::ffff:0:1.2.3.4')).toBe('::/64');
  });

  it('sends anything it cannot resolve to a client to one sentinel bucket', () => {
    const junk = [
      '',
      ' ',
      'not an ip',
      // A zone index does not identify a client.
      'fe80::1%eth0',
      '[2001:db8::1]',
      // Leading zeros are ambiguous between decimal and octal.
      '01.2.3.4',
      '1.2.3.256',
      '1.2.3',
      '1.2.3.4.5',
      ' 1.2.3.4',
      '1.2.3.4 ',
      '2001:db8::1::2',
      '2001:db8:0:0:0:0:0:0:0:1',
      'gggg::1',
      '::ffff:1.2.3.256',
      '2001:db8',
    ];
    for (const value of junk) expect(limiterBucket(value), JSON.stringify(value)).toBe(SENTINEL_BUCKET);
  });

  it('separates what is separate and joins what is one host', () => {
    const distinct = ['1.2.3.4', '1.2.3.5', '2001:db8::1', '2001:db8:0:1::1', '2001:db9::1'].map(limiterBucket);
    expect(new Set(distinct).size).toBe(distinct.length);
    // A /64 is one host's to spend, so every address in it shares a bucket.
    const one = ['2001:db8:1:2::1', '2001:db8:1:2::2', '2001:db8:1:2:ffff:ffff:ffff:ffff'].map(limiterBucket);
    expect(new Set(one)).toEqual(new Set(['2001:db8:1:2::/64']));
  });
});
