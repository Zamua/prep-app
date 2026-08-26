// The agent a deploy falls back to when no tier funds one. Every call
// refuses, which is the branch the grading and generation use cases
// already take; the free tier and the BYOK adapters replace it when they
// resolve.
import { AgentUnavailable, type AgentPort } from '../../app/ports.js';

export const NO_FUNDING =
  'AI is not configured. Add a personal API key on /settings/agent, or ask the deploy admin to configure a shared tier.';

export class UnavailableAgent implements AgentPort {
  async complete(): Promise<string> {
    throw new AgentUnavailable(NO_FUNDING);
  }
}
