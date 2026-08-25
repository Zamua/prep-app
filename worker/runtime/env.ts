export interface Env {
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
