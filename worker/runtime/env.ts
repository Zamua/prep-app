export interface Env extends InstantLimitEnv {
  USER: DurableObjectNamespace;
  DIRECTORY: DurableObjectNamespace;
  INSTANT_LIMITER: DurableObjectNamespace;
  JOB: DurableObjectNamespace;
  ASSETS: Fetcher;
  PREP_ENV: string;
  /** Parity pins (docs/PARITY-GATE.md section 0): dev and the staging parity
   * host only; the composition root refuses them on prod. */
  PREP_PARITY_MODE?: string;
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
