declare module 'nunjucks/browser/nunjucks-slim.js' {
  import nunjucks from 'nunjucks';
  export default nunjucks;
}
declare module '*/build/templates.js' {
  const templates: Record<string, unknown>;
  export default templates;
}
