// sql.js ships types for the node entry only; the browser glue is the one a
// cell can load, and the `.wasm` sidecar arrives as an already-compiled
// module through the import path.
declare module 'sql.js/dist/sql-wasm-browser.js' {
  const initSqlJs: (config?: Record<string, unknown>) => Promise<unknown>;
  export default initSqlJs;
}
declare module 'sql.js/dist/sql-wasm.wasm' {
  const mod: WebAssembly.Module;
  export default mod;
}
