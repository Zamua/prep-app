"""Instant bounded context.

The anonymous first-run generation endpoint described in
docs/INSTANT-START.md: a visitor types a topic, the deploy's free
inference tier writes a small deck, and the cards come back to the
requester's device. No auth, no persistence of the deck server-side;
the only server state is the rate-limiter ledger.

- `routes`  - POST /api/instant/generate + client-IP derivation
- `service` - prompt, free-tier-only adapter resolution, output hygiene
- `repo`    - instant_generations ledger: check-and-reserve limiter
"""
