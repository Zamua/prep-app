import { existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const WORKER = new URL('..', import.meta.url).pathname;
const BUILD = join(WORKER, 'build');

// The generated modules the adapters import, never committed: the baked
// templates, icons, service worker and build token. A step that fails
// leaves an empty module so unrelated suites still load; the suite that
// needs the real output fails on its own.
export default async function setup(): Promise<void> {
  const build = await import(join(WORKER, 'scripts', 'build.mjs'));
  const steps: [string, () => unknown][] = [
    ['templates.js', () => build.precompileTemplates(build.TEMPLATES, BUILD)],
    ['icons.js', () => build.bakeIcons(join(build.STATIC, 'icons'), BUILD)],
    ['sw.js', () => build.bakeServiceWorker(build.STATIC, BUILD)],
    ['buildinfo.js', () => build.bakeBuildInfo(process.env.PREP_BUILD_ID, BUILD)],
  ];
  for (const [file, run] of steps) {
    try {
      run();
    } catch (e) {
      console.warn(`global-setup: ${file} not baked (${e instanceof Error ? e.message : e})`);
      if (!existsSync(join(BUILD, file))) writeFileSync(join(BUILD, file), 'export default {};\n');
    }
  }
}
