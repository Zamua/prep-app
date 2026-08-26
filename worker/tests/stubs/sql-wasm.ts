// The `.wasm` sidecar as the runtime hands it over: an already-compiled
// module, default-exported. Node is the one place compiling from bytes is
// allowed, which is how the same import shape is served under vitest.
import { readFileSync } from 'node:fs';

const here = import.meta.url.replace('file://', '');
const path = here.slice(0, here.lastIndexOf('/')) + '/../../node_modules/sql.js/dist/sql-wasm.wasm';
const compiled = new (WebAssembly.Module as unknown as new (bytes: BufferSource) => WebAssembly.Module)(readFileSync(path));

export default compiled;
