/** Clerk's public configuration; a deploy file may carry all of these. */
export interface ClerkVars {
  CLERK_ISSUER?: string;
  CLERK_JWKS_URL?: string;
  CLERK_AUTHORIZED_PARTIES?: string;
  CLERK_ACCOUNTS_URL?: string;
  CLERK_PUBLISHABLE_KEY?: string;
}

/** Secrets, delivered as `CELLD_VAR_*`; never in a deploy file. */
export interface SecretVars {
  CLERK_SECRET_KEY?: string;
  CLERK_WEBHOOK_SECRET?: string;
  PREP_ANON_COOKIE_SECRET?: string;
  PREP_KEY_ENCRYPTION_SECRET?: string;
  PREP_VAPID_PRIVATE_KEY?: string;
  PREP_FREE_INFERENCE_API_KEY?: string;
  /** JSON object merged into every shared-tier request body; a deploy knob
   * for endpoint-specific switches, never the conversation or the cap. */
  PREP_FREE_INFERENCE_EXTRA_BODY?: string;
}

/** Public deploy configuration for the shared tier and web push. */
export interface PublicServiceVars {
  PREP_FREE_INFERENCE_BASE_URL?: string;
  PREP_FREE_INFERENCE_MODEL?: string;
  PREP_VAPID_PUBLIC_KEY?: string;
  PREP_VAPID_SUB?: string;
  /** Which ingress header names the client IP; `x-forwarded-for-last` reads the last entry. */
  PREP_CLIENT_IP_HEADER?: string;
  CELLD_FETCH_TIMEOUT_S?: string;
  /** Ceiling on one LLM step; capped by the fetch timeout above. */
  PREP_JOB_LLM_TIMEOUT_S?: string;
}

export interface Env extends InstantLimitEnv, ClerkVars, SecretVars, PublicServiceVars {
  USER: DurableObjectNamespace;
  DIRECTORY: DurableObjectNamespace;
  INSTANT_LIMITER: DurableObjectNamespace;
  JOB: DurableObjectNamespace;
  ASSETS: Fetcher;
  PREP_ENV: string;
  /** Parity pins (docs/PARITY-GATE.md section 0): dev and the staging parity
   * host only; the composition root refuses them on prod. */
  PREP_PARITY_MODE?: string;
  /** '1' silences the per-user alarm; honoured under parity mode only. */
  PREP_PARITY_NO_PERIODIC?: string;
  PREP_FAKE_NOW?: string;
  PREP_BUILD_ID?: string;
  PREP_PLACEHOLDER_INDEX?: string;
  PREP_INTERNAL_TOKEN?: string;
}

/** The instant limiter's windows, as Python names them; unset means the default. */
export interface InstantLimitEnv {
  PREP_INSTANT_BURST_LIMIT?: string;
  PREP_INSTANT_BURST_WINDOW_S?: string;
  PREP_INSTANT_PER_IP_PER_DAY?: string;
  PREP_INSTANT_PER_ANON_USER_PER_DAY?: string;
  PREP_INSTANT_PER_USER_PER_DAY?: string;
  PREP_INSTANT_GLOBAL_PER_DAY?: string;
  PREP_INSTANT_GLOBAL_PER_MINUTE?: string;
}
