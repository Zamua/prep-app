// Stands in for `cloudflare:workers` under vitest: the base class only.
export class DurableObject<E = unknown> {
  constructor(
    public ctx: DurableObjectState,
    public env: E,
  ) {}
}
