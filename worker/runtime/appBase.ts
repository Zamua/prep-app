/** The origin a page embeds in the absolute links it hands out,
 * `scheme://host`. TLS ends at the ingress, so a forwarded scheme wins over
 * the socket's. */
export function appBase(request: Request): string {
  const url = new URL(request.url);
  const forwarded = request.headers.get('x-forwarded-proto')?.split(',')[0]?.trim();
  const scheme = forwarded || url.protocol.replace(/:$/, '');
  return `${scheme}://${url.host}`;
}
