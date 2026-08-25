declare module 'sql.js/dist/sql-wasm-browser.js' {
  const initSqlJs: (config?: Record<string, unknown>) => Promise<any>;
  export default initSqlJs;
}
