import { defineConfig } from 'vitest/config';

// `cloudflare:workers` exists only inside the runtime; the stub gives the
// cells a base class so their logic runs under node.
export default defineConfig({
  resolve: {
    alias: {
      'cloudflare:workers': new URL('./tests/stubs/cloudflare-workers.ts', import.meta.url).pathname,
    },
  },
  test: {
    include: ['tests/**/*.test.ts'],
    globalSetup: ['tests/global-setup.ts'],
  },
});
