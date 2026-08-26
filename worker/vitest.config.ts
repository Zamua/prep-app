import { defineConfig } from 'vitest/config';

// `cloudflare:workers` exists only inside the runtime; the stub gives the
// cells a base class so their logic runs under node.
export default defineConfig({
  resolve: {
    alias: {
      'cloudflare:workers': new URL('./tests/stubs/cloudflare-workers.ts', import.meta.url).pathname,
      // celld resolves a `.wasm` import to a compiled module; node has no such
      // loader, so the stub compiles the same sidecar itself.
      'sql.js/dist/sql-wasm.wasm': new URL('./tests/stubs/sql-wasm.ts', import.meta.url).pathname,
    },
  },
  test: {
    include: ['tests/**/*.test.ts'],
    globalSetup: ['tests/global-setup.ts'],
  },
});
