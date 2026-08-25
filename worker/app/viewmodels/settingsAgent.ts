// How many BYOK providers hold a key: the `selectattr('metadata')` count
// `settings_agent.html` opens its first row on.

export interface SettingsAgentContext {
  byok_sections?: Array<{ metadata?: unknown }> | null;
}

export interface SettingsAgentFields {
  byok_connected_count: number;
}

export function deriveSettingsAgent(context: SettingsAgentContext): SettingsAgentFields {
  const sections = context.byok_sections ?? [];
  return { byok_connected_count: sections.filter((s) => Boolean(s.metadata)).length };
}
