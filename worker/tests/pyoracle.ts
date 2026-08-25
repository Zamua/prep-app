// Runs a snippet against the Python reference in the repo's venv and
// returns its JSON stdout. The Python app is the oracle until cutover.
import { execFileSync } from "node:child_process";
const REPO = new URL("../..", import.meta.url).pathname;

export function pythonJson<T>(snippet: string): T {
  const out = execFileSync(`${REPO}.venv/bin/python`, ["-c", snippet], {
    cwd: REPO,
    encoding: "utf8",
    env: { ...process.env, PREP_BUILD_ID: "ce11d0000000", ROOT_PATH: "" },
  });
  return JSON.parse(out) as T;
}
