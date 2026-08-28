// The build-stable asset token: the value templates render as
// `build_token`, the versioned asset URLs carry, and the service
// worker keys its precache on. `env.PREP_BUILD_ID` overrides the token the
// build wrote into build/buildinfo.js.
import { BUILD_TOKEN } from '../build/buildinfo.js';
import { resolveToken } from './tokenRules.js';

export { isAcceptedVersionToken, sha1Hex } from './tokenRules.js';

export function resolveBuildToken(raw: string | undefined | null): string {
  return resolveToken(raw, BUILD_TOKEN);
}
