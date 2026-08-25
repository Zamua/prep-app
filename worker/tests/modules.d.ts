// The full nunjucks library, used by tests to precompile on the spot.
declare module "nunjucks" {
  export class Environment {
    constructor(loader: unknown, opts?: { autoescape?: boolean });
  }
  export function precompileString(
    src: string,
    opts: { name: string; env?: Environment; wrapper?: (templates: Array<{ name: string; template: string }>) => string },
  ): string;
  const nunjucks: { Environment: typeof Environment; precompileString: typeof precompileString };
  export default nunjucks;
}
