# Canned LLM fixtures

Served by `worker/scripts/llm-stub.mjs`. One file per request:
`<key[:16]>.json`, where `key` is the sha256 of the canonical `messages`
list (key-sorted, space-free JSON). Each file holds
`{"key", "messages", "body"}`; `body` is the upstream response text,
served byte for byte.

`missing/` (ignored) collects the request bodies of misses so a failing
run names what it needed.

Recording, once, against the real free tier:

    PREP_LLM_UPSTREAM_BASE_URL=<base>/v1 \
    PREP_LLM_UPSTREAM_API_KEY=<key> \
    PREP_LLM_UPSTREAM_MODEL=<model> \
    node scripts/llm-stub.mjs --port 8089 --record

A miss means a prompt stopped being deterministic: fix the prompt, never
the key.

The instant-generation fixture (topic "Postgres MVCC") is authored by hand
in the upstream response shape.
