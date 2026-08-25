import { spawnSync } from 'node:child_process';
import { existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const WORKER = new URL('..', import.meta.url).pathname;
const BUILD = join(WORKER, 'build');

// The generated modules the adapters import, never committed: the fixture
// corpus (lane A) and the baked templates, icons, service worker and build
// token (lane C's build steps, without its typecheck). A step that fails
// leaves an empty module so unrelated suites still load; the suite that
// needs the real output fails on its own.
export default async function setup(): Promise<void> {
  const r = spawnSync(process.execPath, ['scripts/build-pages.mjs'], { cwd: WORKER, stdio: 'inherit' });
  if (r.status !== 0) throw new Error('build-pages failed');
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
