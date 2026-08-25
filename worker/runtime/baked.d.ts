// The modules scripts/build.mjs bakes under build/, declared here so the
// tree typechecks before a build has run.
declare module '*/build/sw.js' {
  export const SW_SOURCE: string;
  export const ASSET_TREE: { css: string[]; js: string[] };
}
declare module '*/build/buildinfo.js' {
  export const BUILD_TOKEN: string;
}
