// The device label a study session records, from its user agent.

export function deviceLabelFromUa(ua: string | null | undefined): string {
  if (!ua) return 'unknown device';
  const s = ua.toLowerCase();
  if (s.includes('ipad')) return 'iPad';
  if (s.includes('iphone')) return 'iPhone';
  if (s.includes('mac os x') || s.includes('macintosh')) return 'Mac';
  if (s.includes('android')) return 'Android';
  if (s.includes('windows')) return 'Windows';
  if (s.includes('linux')) return 'Linux';
  return 'browser';
}
