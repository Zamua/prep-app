import { describe, expect, it } from 'vitest';
import { appBase } from '../runtime/appBase.js';

describe('appBase', () => {
  it('is the request origin', () => {
    expect(appBase(new Request('https://parity.example.test/settings/api?x=1'))).toBe('https://parity.example.test');
    expect(appBase(new Request('http://127.0.0.1:8791/'))).toBe('http://127.0.0.1:8791');
  });

  it('prefers the scheme the ingress forwarded', () => {
    expect(appBase(new Request('http://celld:8080/x', { headers: { 'x-forwarded-proto': 'https' } }))).toBe('https://celld:8080');
    expect(appBase(new Request('http://celld:8080/x', { headers: { 'x-forwarded-proto': 'https, http' } }))).toBe('https://celld:8080');
    expect(appBase(new Request('http://celld:8080/x', { headers: { 'x-forwarded-proto': '' } }))).toBe('http://celld:8080');
  });
});
