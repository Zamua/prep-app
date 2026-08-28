// What the signed-out landing page needs beyond `anonymousContext`.

/** Sample topic in the instant-start box. Shown, never submitted. */
export const TOPIC_PLACEHOLDER = 'Phases of the moon';

export interface LandingEnv {
  /** The deploy funds generation, so the instant-start hero can offer it. */
  freeTierConfigured: boolean;
}

export function landingContext(env: LandingEnv): Record<string, unknown> {
  return {
    instant_enabled: env.freeTierConfigured,
    topic_placeholder: TOPIC_PLACEHOLDER,
  };
}
