// Turning one owner's `AgentConfig` into the adapter that serves it, and the
// lazy port the cells hold. Mirrors prep/agent/selector.py's precedence; the
// decision itself is app-layer policy (app/agent/funding.ts), because only
// the construction is infrastructure.
//
// The key is decrypted here, in the isolate that will use it, and is never
// held past the call: a revoked credential stops the next step because the
// config is read again for it.
import { BYOK_UNUSABLE, NO_FUNDING } from '../../../app/agent/funding.js';
import { AgentUnavailable, type AgentConfig, type AgentPort, type AgentRequest, type Cipher } from '../../../app/ports.js';
import { byokAgent } from './byok.js';
import { FreeTierAgent, freeTierConfig, type FreeTierEnv } from './freeTier.js';

/** Python's `AgentPort.run` default deadline. The job path narrows it to the
 * step budget the fetch ceiling allows. */
export const DEFAULT_TIMEOUT_MS = 120_000;

export interface SelectDeps {
  env: FreeTierEnv;
  /** Null on a deploy with no master key: a stored key cannot be read. */
  cipher: Cipher | null;
  timeoutMs?: number;
  /** The shared tier's output cap; BYOK keeps the adapter default. */
  freeTierMaxTokens?: number;
  warn?: (msg: string) => void;
}

/** Refuses every call with one reason. The single place "AI is not configured"
 * is expressed, so no caller needs a pre-check. */
export class RefusingAgent implements AgentPort {
  constructor(private readonly reason: string) {}
  async complete(): Promise<string> {
    throw new AgentUnavailable(this.reason);
  }
}

export async function agentFor(config: AgentConfig, deps: SelectDeps): Promise<AgentPort> {
  const timeoutMs = deps.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (config.tier === 'none') return new RefusingAgent(config.reason);
  if (config.tier === 'byok') {
    // Fail loud, fail closed: an unreadable key means this owner HAS one, and
    // BYOK is the opt-out from the shared credential.
    try {
      if (!deps.cipher) throw new Error('no master key configured');
      const secret = await deps.cipher.decrypt(config.ciphertext);
      return byokAgent(config.provider, secret, { timeoutMs });
    } catch (e) {
      (deps.warn ?? console.error)(`agent: BYOK ${config.provider} unusable: ${e instanceof Error ? e.message : String(e)}`);
      return new RefusingAgent(BYOK_UNUSABLE);
    }
  }
  const free = freeTierConfig(deps.env, { maxTokens: deps.freeTierMaxTokens, timeoutMs, warn: deps.warn });
  return free ? new FreeTierAgent(free, deps.warn) : new RefusingAgent(NO_FUNDING);
}

/** The port a cell holds: it resolves the credential per call, never across
 * activations, so a key revoked mid-job stops the step after it. */
export class SelectedAgent implements AgentPort {
  constructor(
    private readonly load: () => AgentConfig | Promise<AgentConfig>,
    private readonly deps: SelectDeps,
  ) {}

  async complete(request: AgentRequest): Promise<string> {
    const agent = await agentFor(await this.load(), this.deps);
    return agent.complete(request);
  }
}
