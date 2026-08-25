# Canned LLM fixtures

Served by `tests/parity/llm_stub.py`. One file per request:
`<key[:16]>.json`, where `key` is the sha256 of the canonical
`messages` list (`json.dumps(messages, sort_keys=True,
separators=(",", ":"), ensure_ascii=False)`). Each file holds
`{"key", "messages", "body"}`; `body` is the upstream response text,
served byte for byte.

`missing/` (ignored) collects the request bodies of misses so a
failing run names what it needed.

Recording, once, against the real free tier:

    PARITY_LLM_RECORD=1 \
    PARITY_LLM_UPSTREAM_BASE_URL=<base>/v1 \
    PARITY_LLM_UPSTREAM_API_KEY=<key> \
    PARITY_LLM_UPSTREAM_MODEL=<model> \
    python -m tests.parity.llm_stub --port 8089 --fixtures tests/fixtures/parity/llm

A miss in CI means a prompt stopped being deterministic: fix the
prompt, never the key.

The instant-generation fixture (topic "Postgres MVCC") is authored
by hand in the upstream response shape until the golden capture
records the real one.
