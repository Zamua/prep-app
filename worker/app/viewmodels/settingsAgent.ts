// How many BYOK providers hold a key: the count `settings_agent.html`
// opens its first row on. Derived here because the template engine has no
// filter that counts a list by a truthy attribute.

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
